from __future__ import annotations
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

INCLUSTER_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

@dataclass
class TokenIdentity:
    username: str
    uid: str = ""
    groups: list[str] = field(default_factory=list)


def _make_user_config(token: str, api_url: str, in_cluster: bool):
    from kubernetes import client as k8s_client
    cfg = k8s_client.Configuration()
    if in_cluster:
        cfg.host = "https://kubernetes.default.svc"
        cfg.ssl_ca_cert = INCLUSTER_CA
    else:
        cfg.host = api_url
        cfg.verify_ssl = False
    cfg.api_key = {"authorization": f"Bearer {token}"}
    return cfg


async def validate_token(token: str, api_url: str, in_cluster: bool) -> TokenIdentity | None:
    """Validate a bearer token via TokenReview. Falls back to namespace GET if 403."""
    import asyncio
    from kubernetes import client as k8s_client

    def _do_tokenreview():
        from kubernetes.client.rest import ApiException
        # Use the swarmer SA's in-cluster client (default client)
        auth_api = k8s_client.AuthenticationV1Api()
        body = k8s_client.V1TokenReview(
            spec=k8s_client.V1TokenReviewSpec(token=token)
        )
        try:
            resp = auth_api.create_token_review(body)
            status = resp.status
            if not status.authenticated:
                return None
            return TokenIdentity(
                username=status.user.username or "",
                uid=status.user.uid or "",
                groups=list(status.user.groups or []),
            )
        except ApiException as e:
            if e.status == 403:
                logger.warning("swarmer SA cannot create tokenreviews (RBAC not applied); falling back to namespace probe")
                return "fallback"
            logger.error("TokenReview failed: %s", e)
            return None

    result = await asyncio.to_thread(_do_tokenreview)
    if result == "fallback":
        # Fall back: try a direct namespace GET with the user token to confirm validity
        return await _probe_with_user_token(token, api_url, in_cluster)
    return result


async def _probe_with_user_token(token: str, api_url: str, in_cluster: bool) -> TokenIdentity | None:
    import asyncio
    from kubernetes import client as k8s_client

    def _do_probe():
        from kubernetes.client.rest import ApiException
        cfg = _make_user_config(token, api_url, in_cluster)
        with k8s_client.ApiClient(cfg) as api:
            core = k8s_client.CoreV1Api(api)
            try:
                core.list_namespace(_request_timeout=5)
                return None
            except ApiException as e:
                if e.status == 403:
                    # 403 means authenticated but no list permission — token is valid
                    # but we cannot determine the username, so treat as auth failure
                    return None
                return None

    return await asyncio.to_thread(_do_probe)


async def get_accessible_namespaces(token: str, namespaces: list[str], api_url: str, in_cluster: bool) -> list[str]:
    """Return the subset of namespaces the user token can GET."""
    import asyncio
    from kubernetes import client as k8s_client

    def _check(ns: str) -> str | None:
        from kubernetes.client.rest import ApiException
        cfg = _make_user_config(token, api_url, in_cluster)
        with k8s_client.ApiClient(cfg) as api:
            core = k8s_client.CoreV1Api(api)
            try:
                core.read_namespace(ns, _request_timeout=5)
                return ns
            except ApiException:
                return None

    results = await asyncio.gather(*[asyncio.to_thread(_check, ns) for ns in namespaces])
    return [ns for ns in results if ns is not None]


async def can_create_pods(token: str, namespace: str, api_url: str, in_cluster: bool) -> bool:
    """Check if the user token can create pods in the given namespace via SelfSubjectAccessReview."""
    import asyncio
    from kubernetes import client as k8s_client

    def _do_check():
        from kubernetes.client.rest import ApiException
        cfg = _make_user_config(token, api_url, in_cluster)
        with k8s_client.ApiClient(cfg) as api:
            authz = k8s_client.AuthorizationV1Api(api)
            sar = k8s_client.V1SelfSubjectAccessReview(
                spec=k8s_client.V1SelfSubjectAccessReviewSpec(
                    resource_attributes=k8s_client.V1ResourceAttributes(
                        namespace=namespace,
                        verb="create",
                        resource="pods",
                    )
                )
            )
            try:
                resp = authz.create_self_subject_access_review(sar)
                return resp.status.allowed
            except ApiException:
                return False

    return await asyncio.to_thread(_do_check)

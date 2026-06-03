from __future__ import annotations
import base64
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

INCLUSTER_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def _username_from_jwt(token: str) -> str:
    """Decode the JWT payload and return the 'sub' claim (no signature verification)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("sub", "")
    except Exception:
        return ""

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
    """Validate a bearer token via TokenReview. Falls back to direct probe on 401/403."""
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
                logger.warning("swarmer SA cannot create tokenreviews (RBAC not applied); falling back to direct probe")
                return "fallback"
            if e.status == 401:
                logger.warning("TokenReview got 401 — swarmer kubeconfig credentials may be expired; falling back to direct probe")
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
                # 200 — token is valid; extract username from JWT payload
                return TokenIdentity(username=_username_from_jwt(token))
            except ApiException as e:
                if e.status == 403:
                    # Authenticated but no list-namespace permission — still valid
                    return TokenIdentity(username=_username_from_jwt(token))
                # 401 or other — token itself is invalid
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

"""K8s bearer token validation for Console/API login.

Authenticates a user's identity (username + groups) via TokenReview. As of
ACM-41659, workspace-level authorization is a database-backed ACL (see
``swarmer.workspace_acl``) rather than per-workspace K8s namespace RBAC, so
this module no longer performs any SelfSubjectAccessReview checks.
"""
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
        return str(payload.get("sub") or "")
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
            user = getattr(status, "user", None)
            username = (getattr(user, "username", None) or "").strip() if user else ""
            if not username:
                logger.warning("TokenReview was authenticated but user or username was missing/empty")
                return None
            return TokenIdentity(
                username=username,
                uid=getattr(user, "uid", "") or "",
                groups=list(getattr(user, "groups", []) or []),
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
        jwt_user = _username_from_jwt(token)

        with k8s_client.ApiClient(cfg) as api:
            if jwt_user:
                # Fast path for JWT tokens (e.g. K8s ServiceAccount tokens)
                core = k8s_client.CoreV1Api(api)
                try:
                    core.list_namespace(_request_timeout=5)
                    return TokenIdentity(username=jwt_user)
                except ApiException as e:
                    if e.status == 403:
                        return TokenIdentity(username=jwt_user)
                    return None

            # Non-JWT tokens (e.g. OpenShift OAuth tokens like sha256~...)
            # 1. Try OpenShift User API (~ returns caller's User object)
            try:
                custom_api = k8s_client.CustomObjectsApi(api)
                user_obj = custom_api.get_cluster_custom_object(
                    group="user.openshift.io",
                    version="v1",
                    plural="users",
                    name="~",
                    _request_timeout=5,
                )
                if isinstance(user_obj, dict):
                    username = user_obj.get("metadata", {}).get("name", "")
                    uid = user_obj.get("metadata", {}).get("uid", "")
                    groups = list(user_obj.get("groups") or [])
                    if username:
                        return TokenIdentity(username=username, uid=uid, groups=groups)
            except ApiException as e:
                if e.status == 401:
                    return None
            except Exception:
                pass

            # 2. Try K8s SelfSubjectReview (K8s 1.26+)
            try:
                auth_v1 = k8s_client.AuthenticationV1Api(api)
                ssr = auth_v1.create_self_subject_review(
                    body=k8s_client.V1SelfSubjectReview(), _request_timeout=5
                )
                user_info = getattr(ssr.status, "user_info", None) or getattr(ssr.status, "userInfo", None)
                if user_info:
                    username = getattr(user_info, "username", "") or ""
                    uid = getattr(user_info, "uid", "") or ""
                    groups = list(getattr(user_info, "groups", []) or [])
                    if username:
                        return TokenIdentity(username=username, uid=uid, groups=groups)
            except ApiException as e:
                if e.status == 401:
                    return None
            except Exception:
                pass

            # Reject non-JWT tokens if neither OpenShift User API nor SelfSubjectReview yielded an identity
            logger.warning("Could not resolve identity for non-JWT token via OpenShift User API or SelfSubjectReview")
            return None

    return await asyncio.to_thread(_do_probe)



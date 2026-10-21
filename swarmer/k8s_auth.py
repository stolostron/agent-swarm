"""Bearer token validation for Console/API login.

Kubernetes mode authenticates identity through TokenReview. OpenShell mode
uses a configured bootstrap token or verified OIDC. Workspace authorization is
the database-backed ACL (see ``swarmer.workspace_acl``), not per-workspace K8s
namespace RBAC.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_OIDC_JWKS_CACHE: dict[str, tuple[dict, float]] = {}
_OIDC_JWKS_LAST_REFRESH: dict[str, float] = {}
_OIDC_JWKS_CACHE_TTL = 300
_OIDC_JWKS_REFRESH_MIN_INTERVAL = 1
_OIDC_ALGORITHMS = {
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512", "EdDSA",
}

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


def _https_origin(url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return (parsed.scheme, parsed.hostname.lower(), port)

@dataclass
class TokenIdentity:
    username: str
    uid: str = ""
    groups: list[str] = field(default_factory=list)
    session_id: int | None = None
    workspace_id: int | None = None
    is_bootstrap_admin: bool = False

    @property
    def is_session(self) -> bool:
        return self.session_id is not None and self.workspace_id is not None


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
    """Validate a bearer token through the configured runtime's auth backend."""
    from swarmer.config import settings

    if settings.swarmer_runtime_mode == "openshell":
        admin_identity = await validate_openshell_admin_token(token)
        if admin_identity is not None:
            return admin_identity
        return await validate_openshell_oidc_token(token)

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


async def validate_openshell_admin_token(token: str) -> TokenIdentity | None:
    """Validate the configured bootstrap admin token without logging it."""
    from swarmer.config import settings

    token_file = settings.swarmer_admin_token_file.strip()
    if not token or not token_file:
        return None
    import asyncio

    configured_token = await asyncio.to_thread(_read_admin_token_file, token_file)
    if not configured_token or not hmac.compare_digest(token.strip(), configured_token):
        return None
    return TokenIdentity(
        username=settings.swarmer_admin_username,
        groups=["swarmer-bootstrap-admin"],
        is_bootstrap_admin=True,
    )


def _read_admin_token_file(token_file: str) -> str:
    try:
        with open(token_file, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


async def _load_oidc_jwks(issuer: str, *, refresh: bool = False) -> dict | None:
    """Load cached signing keys only from the configured HTTPS issuer."""
    now = time.monotonic()
    cached = _OIDC_JWKS_CACHE.get(issuer)
    if not refresh and cached and cached[1] > now:
        return cached[0]
    last_refresh = _OIDC_JWKS_LAST_REFRESH.get(issuer, 0)
    if refresh and now - last_refresh < _OIDC_JWKS_REFRESH_MIN_INTERVAL:
        return cached[0] if cached else None
    _OIDC_JWKS_LAST_REFRESH[issuer] = now

    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            discovery_response = await client.get(f"{issuer}/.well-known/openid-configuration")
            discovery_response.raise_for_status()
            metadata = discovery_response.json()
            if not isinstance(metadata, dict):
                return None
            if str(metadata.get("issuer", "")).rstrip("/") != issuer:
                return None
            jwks_uri = metadata.get("jwks_uri")
            issuer_origin = _https_origin(issuer)
            jwks_origin = _https_origin(jwks_uri or "")
            if issuer_origin is None or jwks_origin is None:
                return None
            from swarmer.config import settings

            allowed_origins = {
                origin
                for value in settings.openshell_oidc_jwks_allowed_origins.split(",")
                if (origin := _https_origin(value.strip())) is not None
            }
            if jwks_origin != issuer_origin and jwks_origin not in allowed_origins:
                return None
            jwks_response = await client.get(jwks_uri)
            jwks_response.raise_for_status()
            jwks = jwks_response.json()
            if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
                return None
            _OIDC_JWKS_CACHE[issuer] = (jwks, now + _OIDC_JWKS_CACHE_TTL)
            return jwks
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("OpenShell OIDC key discovery failed (error_type=%s)", type(exc).__name__)
        return None


async def validate_openshell_oidc_token(token: str) -> TokenIdentity | None:
    """Validate an OpenShell OIDC bearer token using issuer JWKS.

    Issuer and audience are deployment configuration and both are mandatory.
    Signature, issuer, audience, expiry, issued-at, and subject are verified;
    unverified JWT claims are never used to establish an identity.
    """
    from swarmer.config import settings

    issuer = settings.openshell_oidc_issuer.strip().rstrip("/")
    audience = settings.openshell_oidc_audience.strip()
    parsed_issuer = urlparse(issuer)
    if (
        not token
        or not audience
        or parsed_issuer.scheme != "https"
        or not parsed_issuer.hostname
        or parsed_issuer.username
        or parsed_issuer.password
        or parsed_issuer.query
        or parsed_issuer.fragment
    ):
        return None

    import jwt

    try:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        key_id = header.get("kid")
        if algorithm not in _OIDC_ALGORITHMS or not key_id:
            return None

        jwks = await _load_oidc_jwks(issuer)
        key_data = next(
            (
                key
                for key in (jwks or {}).get("keys", [])
                if isinstance(key, dict)
                and key.get("kid") == key_id
                and key.get("use", "sig") == "sig"
            ),
            None,
        )
        if key_data is None:
            jwks = await _load_oidc_jwks(issuer, refresh=True)
            key_data = next(
                (
                    key
                    for key in (jwks or {}).get("keys", [])
                    if isinstance(key, dict)
                    and key.get("kid") == key_id
                    and key.get("use", "sig") == "sig"
                ),
                None,
            )
        if key_data is None:
            return None
        if key_data.get("alg") and key_data["alg"] != algorithm:
            return None

        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(key_data).key,
            algorithms=[algorithm],
            audience=audience,
            issuer=issuer,
            leeway=30,
            options={"require": ["exp", "iat", "sub"]},
        )
        subject = str(claims.get("sub") or "").strip()
        username = subject
        groups_claim = settings.openshell_oidc_groups_claim.strip() or "groups"
        raw_groups = claims
        for segment in groups_claim.split("."):
            raw_groups = raw_groups.get(segment) if isinstance(raw_groups, dict) else None
        raw_groups = raw_groups or []
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        groups = [
            str(group)
            for group in raw_groups
            if str(group) != "swarmer-bootstrap-admin"
        ] if isinstance(raw_groups, list) else []
        audiences = claims.get("aud")
        if claims.get("azp") and claims["azp"] != audience:
            return None
        if isinstance(audiences, list) and len(audiences) > 1 and claims.get("azp") != audience:
            return None
        return TokenIdentity(username=username, uid=subject, groups=groups) if username else None
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        logger.warning("OpenShell OIDC token validation failed (error_type=%s)", type(exc).__name__)
        return None


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

"""
Kubernetes utility functions used across the dashboard.

Swarmer uses K8s for: authentication (TokenReview), image pull secrets,
workspace namespace scoping, and extra env var storage (pending migration
to the encrypted DB in ACM-35039). All session lifecycle management goes
through the OpenShell Gateway + Supervisor APIs — no direct pod/PVC/Secret
creation for agent sessions.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kubernetes.client import RbacV1Subject

log = logging.getLogger(__name__)

_image_cache: dict[tuple[str, str], tuple[bool, float]] = {}
_IMAGE_CACHE_TTL = 300  # seconds


async def get_image_available(image: str, namespace: str) -> bool:
    if not image:
        return False
    key = (image, namespace)
    cached = _image_cache.get(key)
    if cached is not None and time.monotonic() - cached[1] < _IMAGE_CACHE_TTL:
        return cached[0]
    result = await check_image_reachable(image, namespace)
    _image_cache[key] = (result, time.monotonic())
    return result


def effective_namespace(workspace_namespace: str) -> str:
    """Return the K8s namespace to use for a workspace.

    When ``settings.k8s_namespace`` is set, all workspaces share that
    single namespace (useful in ephemeral/shared clusters).  Otherwise
    the workspace's own derived namespace is used.
    """
    from swarmer.config import settings
    return settings.k8s_namespace or workspace_namespace


def _b64(value: str) -> str:
    """Base64-encode a string for use in K8s Secret data fields."""
    return base64.b64encode(value.encode()).decode()


def init_k8s(in_cluster: bool) -> None:
    try:
        from kubernetes import config as k8s_config

        if in_cluster:
            k8s_config.load_incluster_config()
            log.info("Kubernetes client initialised (in-cluster)")
        else:
            try:
                k8s_config.load_kube_config()
                log.info("Kubernetes client initialised (kubeconfig)")
            except k8s_config.ConfigException:
                k8s_config.load_incluster_config()
                log.info("Kubernetes client initialised (in-cluster fallback — set K8S_IN_CLUSTER=true to suppress this)")
    except Exception as exc:
        log.error("Kubernetes client not available — all K8s calls will fail as system:anonymous: %s", exc)


# ---------- Namespace helpers ----------

def ensure_namespace(namespace: str) -> None:
    """Create the namespace if it doesn't exist; no-op if it does."""
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.read_namespace(namespace)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespace(
                client.V1Namespace(
                    metadata=client.V1ObjectMeta(name=namespace)
                )
            )
        else:
            raise

    _grant_anyuid_scc(namespace)


SWARMER_USER_CLUSTER_ROLE = "swarmer-user"


def _rbac_subject(identity_username: str) -> RbacV1Subject:
    """Build a RoleBinding subject for a K8s User or ServiceAccount identity."""
    from kubernetes import client

    prefix = "system:serviceaccount:"
    if identity_username.startswith(prefix):
        sa_ns, sa_name = identity_username[len(prefix):].split(":", 1)
        return client.RbacV1Subject(
            kind="ServiceAccount",
            name=sa_name,
            namespace=sa_ns,
        )
    return client.RbacV1Subject(kind="User", name=identity_username)


def _swarmer_user_role_binding_name(identity_username: str) -> str:
    """Return a stable, unique RoleBinding name for *identity_username*."""
    import re

    safe = re.sub(r"[^a-z0-9-]", "-", identity_username.lower()).strip("-")[:40]
    suffix = hashlib.sha256(identity_username.encode()).hexdigest()[:8]
    return f"swarmer-user-{safe or 'user'}-{suffix}"


def grant_swarmer_user_access(namespace: str, identity_username: str) -> None:
    """Grant swarmer-user ClusterRole in *namespace* to *identity_username*."""
    from kubernetes import client

    rb_name = _swarmer_user_role_binding_name(identity_username)

    rbac = client.RbacAuthorizationV1Api()
    rb = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name=rb_name, namespace=namespace),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="ClusterRole",
            name=SWARMER_USER_CLUSTER_ROLE,
        ),
        subjects=[_rbac_subject(identity_username)],
    )
    try:
        rbac.create_namespaced_role_binding(namespace, rb)
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            return
        log.warning(
            "Failed to grant swarmer-user in %s: status=%s reason=%s",
            namespace,
            exc.status,
            exc.reason,
        )


def _grant_anyuid_scc(namespace: str) -> None:
    """Grant the OpenShift anyuid SCC to the default SA in *namespace*.

    Creates a namespace-scoped RoleBinding (matching what `oc adm policy
    add-scc-to-user anyuid` does on OpenShift 4.x).  Silently skips on
    kind/k3s where the anyuid ClusterRole does not exist (404).
    """
    from kubernetes import client

    rbac = client.RbacAuthorizationV1Api()
    rb = client.V1RoleBinding(
        metadata=client.V1ObjectMeta(name="system:openshift:scc:anyuid", namespace=namespace),
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io",
            kind="ClusterRole",
            name="system:openshift:scc:anyuid",
        ),
        subjects=[client.RbacV1Subject(
            kind="ServiceAccount",
            name="default",
            namespace=namespace,
        )],
    )
    try:
        rbac.create_namespaced_role_binding(namespace, rb)
    except client.exceptions.ApiException as exc:
        if exc.status == 409:  # already exists
            pass
        elif exc.status == 404:
            # anyuid ClusterRole absent — not OpenShift, skip silently
            log.debug("anyuid SCC grant skipped for %s (not OpenShift)", namespace)
        elif exc.status == 403:
            log.warning(
                "anyuid SCC grant forbidden for %s: status=%s reason=%s",
                namespace,
                exc.status,
                exc.reason,
            )
        else:
            raise


def delete_namespace(namespace: str) -> None:
    """Delete the namespace; no-op if already gone."""
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespace(namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


# ---------- Secret helpers ----------

def _apply_secret(namespace: str, name: str, data: dict[str, str]) -> None:
    """Create or replace a K8s Opaque Secret."""
    from kubernetes import client

    v1 = client.CoreV1Api()
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        type="Opaque",
        data=data,
    )
    try:
        v1.replace_namespaced_secret(name, namespace, body)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespaced_secret(namespace, body)
        else:
            raise


def _delete_secret(namespace: str, name: str) -> None:
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_secret(name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


PULL_SECRET_NAME = "quay-pull-secret"


def apply_pull_secret(namespace: str, registry: str, username: str, password: str) -> None:
    """Create or update a kubernetes.io/dockerconfigjson pull secret."""
    import json
    from kubernetes import client

    dockerconfig = json.dumps({
        "auths": {
            registry: {
                "username": username,
                "password": password,
                "auth": base64.b64encode(f"{username}:{password}".encode()).decode(),
            }
        }
    })
    body = client.V1Secret(
        metadata=client.V1ObjectMeta(name=PULL_SECRET_NAME, namespace=namespace),
        type="kubernetes.io/dockerconfigjson",
        data={".dockerconfigjson": _b64(dockerconfig)},
    )
    v1 = client.CoreV1Api()
    try:
        v1.replace_namespaced_secret(PULL_SECRET_NAME, namespace, body)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespaced_secret(namespace, body)
        else:
            raise


def get_pull_secret_info(namespace: str) -> dict | None:
    """Return {"registry": ..., "username": ...} if the pull secret exists, else None."""
    import json
    from kubernetes import client

    try:
        v1 = client.CoreV1Api()
        secret = v1.read_namespaced_secret(PULL_SECRET_NAME, namespace)
        raw = base64.b64decode(secret.data[".dockerconfigjson"]).decode()
        config = json.loads(raw)
        auths = config.get("auths", {})
        if auths:
            registry = next(iter(auths))
            username = auths[registry].get("username", "")
            return {"registry": registry, "username": username}
    except Exception:
        pass
    return None


def delete_pull_secret(namespace: str) -> None:
    _delete_secret(namespace, PULL_SECRET_NAME)


async def check_image_reachable(image: str, namespace: str) -> bool:
    """Return True if the image manifest is accessible (with or without a pull secret)."""
    import json
    import httpx
    from kubernetes import client as k8s_client

    # Parse image into registry, repo, tag
    tag = "latest"
    if ":" in image.split("/")[-1]:
        image_no_tag, tag = image.rsplit(":", 1)
    else:
        image_no_tag = image

    parts = image_no_tag.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]):
        registry = parts[0]
        repo = parts[1]
    else:
        registry = "registry-1.docker.io"
        repo = image_no_tag if "/" in image_no_tag else f"library/{image_no_tag}"

    log.debug("check_image_reachable: image=%s registry=%s repo=%s tag=%s namespace=%s",
              image, registry, repo, tag, namespace)

    # Read pull secret credentials
    auth_b64 = ""
    try:
        v1 = k8s_client.CoreV1Api()
        secret = v1.read_namespaced_secret(PULL_SECRET_NAME, namespace)
        raw = base64.b64decode(secret.data[".dockerconfigjson"]).decode()
        config = json.loads(raw)
        auths = config.get("auths", {})
        entry = auths.get(registry) or auths.get(f"https://{registry}")
        if not entry:
            # Longest-prefix match: key "quay.io/org" should match image "quay.io/org/repo"
            image_path = f"{registry}/{repo}"
            for key in sorted(auths, key=len, reverse=True):
                norm = key.removeprefix("https://")
                if image_path.startswith(norm):
                    entry = auths[key]
                    break
        entry = entry or {}
        auth_b64 = entry.get("auth", "")
        if auth_b64:
            log.debug("check_image_reachable: pull secret found, auth present for registry=%s", registry)
        else:
            log.debug("check_image_reachable: pull secret in %s has no auth entry for registry=%s (auths keys=%s)",
                      namespace, registry, list(auths.keys()))
    except k8s_client.exceptions.ApiException as exc:
        if exc.status == 404:
            log.debug("check_image_reachable: no pull secret %s/%s — will try anonymous access",
                      namespace, PULL_SECRET_NAME)
        else:
            log.warning("check_image_reachable: could not read pull secret %s/%s: %s",
                        namespace, PULL_SECRET_NAME, exc)
    except Exception as exc:
        log.warning("check_image_reachable: could not read pull secret %s/%s: %s",
                    namespace, PULL_SECRET_NAME, exc)

    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    accept = (
        "application/vnd.docker.distribution.manifest.v2+json,"
        "application/vnd.oci.image.manifest.v1+json,"
        "application/vnd.oci.image.index.v1+json,"
        "*/*"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as http:
            r = await http.get(url, headers={"Accept": accept})
            log.debug("check_image_reachable: GET %s → %s", url, r.status_code)
            if r.status_code == 200:
                return True

            # Follow Bearer token challenge
            if r.status_code == 401 and "www-authenticate" in r.headers:
                www_auth = r.headers["www-authenticate"]
                if www_auth.lower().startswith("bearer "):
                    params: dict[str, str] = {}
                    for part in www_auth[7:].split(","):
                        k, _, v = part.strip().partition("=")
                        params[k.strip()] = v.strip('"')
                    realm = params.get("realm", "")
                    log.debug("check_image_reachable: bearer challenge realm=%s", realm)
                    if realm:
                        token_params: dict[str, str] = {}
                        if "service" in params:
                            token_params["service"] = params["service"]
                        if "scope" in params:
                            token_params["scope"] = params["scope"]
                        creds = None
                        if auth_b64:
                            decoded = base64.b64decode(auth_b64).decode()
                            user, _, pwd = decoded.partition(":")
                            creds = (user, pwd)
                        used_creds = False
                        bearer_manifest_status = None
                        for attempt_creds in ([creds, None] if creds else [None]):
                            tr = await http.get(realm, params=token_params, auth=attempt_creds)
                            log.debug("check_image_reachable: token fetch (creds=%s) → %s",
                                      attempt_creds is not None, tr.status_code)
                            if tr.status_code == 200:
                                used_creds = attempt_creds is not None
                                token = tr.json().get("token") or tr.json().get("access_token", "")
                                mr = await http.get(url, headers={"Authorization": f"Bearer {token}", "Accept": accept})
                                bearer_manifest_status = mr.status_code
                                log.debug("check_image_reachable: manifest (bearer) → %s", mr.status_code)
                                if mr.status_code == 200:
                                    return True
                                break
                        log.warning(
                            "check_image_reachable: image not accessible %s (pull_secret=%s, used_creds=%s, manifest_status=%s)",
                            url, bool(auth_b64), used_creds, bearer_manifest_status,
                        )
                        return False
            log.warning("check_image_reachable: unhandled response %s for %s", r.status_code, url)
    except Exception as exc:
        log.warning("check_image_reachable: HTTP error for %s: %s", url, exc)
    return False


# ---------- Extra env vars (swarmer-agent-extra-env secret) ----------

AGENT_EXTRA_ENV_SECRET_NAME = "swarmer-agent-extra-env"


def get_extra_env_vars(namespace: str) -> dict[str, str]:
    """Return the key/value pairs stored in the extra-env secret, or {}."""
    from kubernetes import client

    try:
        v1 = client.CoreV1Api()
        secret = v1.read_namespaced_secret(AGENT_EXTRA_ENV_SECRET_NAME, namespace)
        return {
            k: base64.b64decode(v).decode()
            for k, v in (secret.data or {}).items()
        }
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            return {}
        raise


def set_extra_env_var(namespace: str, key: str, value: str) -> None:
    """Set a single key in the extra-env secret (create or update)."""
    existing = get_extra_env_vars(namespace)
    existing[key] = value
    _apply_secret(namespace, AGENT_EXTRA_ENV_SECRET_NAME, {k: _b64(v) for k, v in existing.items()})


def delete_extra_env_var(namespace: str, key: str) -> None:
    """Remove a single key from the extra-env secret."""
    existing = get_extra_env_vars(namespace)
    existing.pop(key, None)
    if existing:
        _apply_secret(namespace, AGENT_EXTRA_ENV_SECRET_NAME, {k: _b64(v) for k, v in existing.items()})
    else:
        _delete_secret(namespace, AGENT_EXTRA_ENV_SECRET_NAME)

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
import logging
import time

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


SWARMER_USER_CLUSTER_ROLE = "swarmer-user"


def list_swarmer_user_role_binding_identities(namespace: str) -> list[str]:
    """Return K8s usernames granted via `swarmer-user` RoleBindings in *namespace*.

    ACM-41659 migration helper: `make grant-workspace-access` (pre-database-ACL)
    created a RoleBinding to the `swarmer-user` ClusterRole for each granted
    user. This reads those bindings back so existing grants can be mirrored
    into `workspace_members` without requiring anyone to be manually re-added.
    Returns [] (never raises) if the namespace doesn't exist or K8s is
    unreachable — this is a best-effort, non-fatal sync.

    Identity strings match the TokenIdentity.username format returned by
    TokenReview: `system:serviceaccount:<sa_namespace>:<sa_name>` for a
    ServiceAccount subject, or the raw subject name for a User subject.
    """
    from kubernetes import client

    identities: list[str] = []
    try:
        rbac = client.RbacAuthorizationV1Api()
        bindings = rbac.list_namespaced_role_binding(namespace)
    except Exception:
        log.debug("list_swarmer_user_role_binding_identities: could not list RoleBindings in %s", namespace, exc_info=True)
        return identities

    for rb in bindings.items or []:
        role_ref = rb.role_ref
        if not role_ref or role_ref.kind != "ClusterRole" or role_ref.name != SWARMER_USER_CLUSTER_ROLE:
            continue
        for subject in rb.subjects or []:
            if subject.kind == "ServiceAccount":
                sa_namespace = subject.namespace or namespace
                identities.append(f"system:serviceaccount:{sa_namespace}:{subject.name}")
            elif subject.kind == "User":
                identities.append(subject.name)
    return identities


# ---------- Add Member / Add Admin candidate discovery (ACM-41659) ----------

INCLUSTER_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
DEFAULT_APP_NAMESPACE = "swarmer"

# ServiceAccounts that are infrastructure, not people — never suggested.
_SYSTEM_SERVICE_ACCOUNTS = frozenset({
    "default", "builder", "deployer", "swarmer", "openshell", "openshell-sandbox",
})


def app_namespace() -> str:
    """Best-effort: the namespace Swarmer itself is deployed into.

    Used to discover the ServiceAccounts `make user-token SA_USER=<name>`
    would create (or already has) for autocomplete suggestions — never
    raises, always returns a usable default.
    """
    from swarmer.config import settings

    if settings.k8s_namespace:
        return settings.k8s_namespace
    try:
        with open(INCLUSTER_NAMESPACE_FILE) as f:
            ns = f.read().strip()
            if ns:
                return ns
    except OSError:
        pass
    return DEFAULT_APP_NAMESPACE


def list_openshift_users() -> list[str]:
    """Return all OpenShift `User` object names (cluster-scoped), or [] if
    not running on OpenShift / no permission / any K8s error. Never raises."""
    from kubernetes import client

    try:
        api = client.CustomObjectsApi()
        result = api.list_cluster_custom_object(
            group="user.openshift.io", version="v1", plural="users"
        )
        return [
            item["metadata"]["name"]
            for item in result.get("items", [])
            if item.get("metadata", {}).get("name")
        ]
    except Exception:
        log.debug("list_openshift_users: not available (not OpenShift, no permission, or unreachable)", exc_info=True)
        return []


def list_user_service_accounts(namespace: str | None = None) -> list[str]:
    """Return `system:serviceaccount:<ns>:<name>` identities for every
    ServiceAccount in *namespace* (defaults to `app_namespace()`) — i.e. the
    identities `make user-token SA_USER=<name>` creates/uses. Excludes
    infrastructure ServiceAccounts. Never raises; [] on any K8s error."""
    from kubernetes import client

    ns = namespace or app_namespace()
    try:
        v1 = client.CoreV1Api()
        result = v1.list_namespaced_service_account(ns)
    except Exception:
        log.debug("list_user_service_accounts: could not list ServiceAccounts in %s", ns, exc_info=True)
        return []

    return [
        f"system:serviceaccount:{ns}:{sa.metadata.name}"
        for sa in result.items or []
        if sa.metadata.name not in _SYSTEM_SERVICE_ACCOUNTS
    ]


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

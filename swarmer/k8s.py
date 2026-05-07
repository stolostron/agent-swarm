"""
Kubernetes utility functions used across the dashboard.
All functions use the official kubernetes-client Python library.
"""
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
        else:
            k8s_config.load_kube_config()
        log.info("Kubernetes client initialised (in_cluster=%s)", in_cluster)
    except Exception as exc:
        log.warning("Kubernetes client not available: %s", exc)


# ---------- Namespace helpers ----------

def ensure_namespace(namespace: str) -> None:
    """Create the namespace if it doesn't exist; no-op if it does.

    On OpenShift, also grants the anyuid SCC to the namespace's default SA so
    that session pods can run as root without requiring privileged host access.
    This is a no-op on kind/k3s where SCCs do not exist.
    """
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
            log.warning("anyuid SCC grant forbidden for %s: %s", namespace, exc.body)
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


def get_namespace_status(namespace: str) -> str:
    """Return the namespace phase string or 'Unknown'."""
    from kubernetes import client

    try:
        v1 = client.CoreV1Api()
        ns = v1.read_namespace(namespace)
        return ns.status.phase or "Unknown"
    except Exception:
        return "Unknown"


# ---------- ConfigMap helpers ----------

def apply_agent_config(
    namespace: str, secret=None, agent_tool: str = "opencode", mcp_servers=None
) -> None:
    """Create or update the agent tool's ConfigMap in the given namespace."""
    from kubernetes import client
    from swarmer.agent_tools.registry import get as get_tool

    tool = get_tool(agent_tool)
    cm_name = tool.get_config_map_name()
    data = tool.build_config_data(secret, mcp_servers=mcp_servers)

    v1 = client.CoreV1Api()
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name=cm_name, namespace=namespace),
        data=data,
    )
    try:
        v1.replace_namespaced_config_map(cm_name, namespace, body)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespaced_config_map(namespace, body)
        else:
            raise


def apply_opencode_config(namespace: str, secret=None) -> None:
    """Backward-compat wrapper — delegates to apply_agent_config."""
    apply_agent_config(namespace, secret=secret, agent_tool="opencode")


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


def apply_agent_secret(
    namespace: str, secret, agent_tool: str = "opencode"
) -> None:
    """Sync the agent tool's K8s Secret from the DB model."""
    from swarmer.agent_tools.registry import get as get_tool

    tool = get_tool(agent_tool)
    data = tool.build_k8s_secret_data(secret)
    if data:
        _apply_secret(namespace, tool.get_secret_name(), data)


def sync_all_agent_secrets(namespace: str, secret) -> None:
    """Sync K8s Secrets for every registered agent tool."""
    from swarmer.agent_tools.registry import all_tools

    for tool in all_tools():
        data = tool.build_k8s_secret_data(secret)
        if data:
            _apply_secret(namespace, tool.get_secret_name(), data)


def apply_opencode_secret(namespace: str, secret) -> None:
    """Backward-compat wrapper — delegates to apply_agent_secret."""
    apply_agent_secret(namespace, secret, agent_tool="opencode")


def apply_github_pat_secret(namespace: str, pat) -> None:
    """Sync github-pat-<slug> K8s Secret from the DB model."""
    _apply_secret(
        namespace,
        pat.k8s_secret_name,
        {
            "GITHUB_PAT": _b64(pat.pat),
            "GITHUB_USERNAME": _b64(pat.github_username),
        },
    )


def delete_github_pat_secret(namespace: str, pat) -> None:
    _delete_secret(namespace, pat.k8s_secret_name)


MCP_SECRET_NAME = "mcp-server-tokens"


def sync_mcp_server_secret(namespace: str, mcp_servers) -> None:
    """Create or update the K8s Secret containing MCP server credentials."""
    data = {}
    for srv in mcp_servers:
        if srv.jira_access_token_enc:
            data["JIRA_SERVER_URL"] = _b64(srv.jira_server_url)
            data["JIRA_ACCESS_TOKEN"] = _b64(srv.jira_access_token)
            data["JIRA_EMAIL"] = _b64(srv.jira_email)

    if data:
        _apply_secret(namespace, MCP_SECRET_NAME, data)
    else:
        _delete_secret(namespace, MCP_SECRET_NAME)


# ---------- Pod / PVC helpers (used by sessions) ----------

# Maps the coarse K8s pod phase to our internal phase vocabulary
_PHASE_MAP = {
    "Pending": "pending",
    "Running": "running",
    "Succeeded": "succeeded",
    "Failed": "failed",
}


def get_pod_status(pod_name: str, namespace: str) -> tuple[str, str]:
    """Return (our_phase, detail) for a pod.

    our_phase: pending | running | succeeded | failed | stopped
    detail:    a human-readable K8s status string, e.g.
               'PodInitializing', 'ErrImagePull', 'ImagePullBackOff',
               'ContainerCreating', 'CrashLoopBackOff', 'OOMKilled', 'Running'

    Priority for detail (most specific first):
      1. Init-container waiting reason  (PodInitializing while init runs)
      2. Main container waiting reason  (ErrImagePull, ContainerCreating, …)
      3. Main container terminated reason (OOMKilled, Error, Completed)
      4. Raw K8s phase string as fallback
    """
    from kubernetes import client

    try:
        v1 = client.CoreV1Api()
        pod = v1.read_namespaced_pod(pod_name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            return "stopped", "Not Found"
        return "pending", "Unknown"
    except Exception:
        return "pending", "Cluster Unavailable"

    k8s_phase = pod.status.phase or "Unknown"
    our_phase = _PHASE_MAP.get(k8s_phase, "pending")
    detail = k8s_phase  # fallback

    # 1. Init-container waiting reason
    for cs in pod.status.init_container_statuses or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason:
            return our_phase, cs.state.waiting.reason

    # 2. Main container waiting reason
    for cs in pod.status.container_statuses or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason:
            return our_phase, cs.state.waiting.reason

    # 3. Main container terminated reason
    for cs in pod.status.container_statuses or []:
        if cs.state and cs.state.terminated and cs.state.terminated.reason:
            return our_phase, cs.state.terminated.reason

    return our_phase, detail


def get_pod_phase(pod_name: str, namespace: str) -> str:
    """Thin wrapper kept for any callers that only need the phase string."""
    phase, _ = get_pod_status(pod_name, namespace)
    return phase


def get_pod_logs(pod_name: str, namespace: str) -> str:
    from kubernetes import client

    try:
        v1 = client.CoreV1Api()
        return v1.read_namespaced_pod_log(pod_name, namespace)
    except Exception:
        return ""


def delete_pod(pod_name: str, namespace: str) -> None:
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_pod(pod_name, namespace)
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


def exec_model_update(
    pod_name: str, namespace: str, model: str, agent_tool: str = "opencode"
) -> None:
    """Update model selection on a running pod via the tool's strategy."""
    from swarmer.agent_tools.registry import get as get_tool

    tool = get_tool(agent_tool)
    tool.exec_model_update(pod_name, namespace, model)


def exec_model_json(pod_name: str, namespace: str, model: str) -> None:
    """Backward-compat wrapper — delegates to exec_model_update."""
    exec_model_update(pod_name, namespace, model, agent_tool="opencode")


def delete_service(service_name: str, namespace: str) -> None:
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_service(service_name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


# ---------- OpenShift Route helpers ----------

_ROUTE_GROUP = "route.openshift.io"
_ROUTE_VERSION = "v1"
_ROUTE_PLURAL = "routes"


def create_session_route(session_id: int, namespace: str, service_name: str, port: int = 4096) -> str:
    """Create an OpenShift edge-TLS Route for a server-mode session.

    Returns the assigned hostname, or '' if Routes are not supported
    (non-OpenShift cluster) or the hostname is not yet available.
    Silently skips on kind/k3s where the route.openshift.io CRD is absent.
    """
    from kubernetes import client

    custom = client.CustomObjectsApi()
    route_name = f"session-{session_id}-chat"
    body = {
        "apiVersion": f"{_ROUTE_GROUP}/{_ROUTE_VERSION}",
        "kind": "Route",
        "metadata": {"name": route_name, "namespace": namespace},
        "spec": {
            "to": {"kind": "Service", "name": service_name},
            "port": {"targetPort": port},
            "tls": {
                "termination": "edge",
                "insecureEdgeTerminationPolicy": "Redirect",
            },
        },
    }
    try:
        result = custom.create_namespaced_custom_object(
            group=_ROUTE_GROUP, version=_ROUTE_VERSION,
            namespace=namespace, plural=_ROUTE_PLURAL, body=body,
        )
        ingresses = result.get("status", {}).get("ingress", [])
        return ingresses[0].get("host", "") if ingresses else ""
    except client.exceptions.ApiException as exc:
        if exc.status == 409:
            return get_session_route_host(session_id, namespace)
        if exc.status in (403, 404):
            log.debug("OpenShift Routes not available (status %s) — skipping", exc.status)
            return ""
        raise


def get_session_route_host(session_id: int, namespace: str) -> str:
    """Return the hostname of an existing session Route, or ''."""
    from kubernetes import client

    custom = client.CustomObjectsApi()
    route_name = f"session-{session_id}-chat"
    try:
        result = custom.get_namespaced_custom_object(
            group=_ROUTE_GROUP, version=_ROUTE_VERSION,
            namespace=namespace, plural=_ROUTE_PLURAL, name=route_name,
        )
        ingresses = result.get("status", {}).get("ingress", [])
        return ingresses[0].get("host", "") if ingresses else ""
    except client.exceptions.ApiException as exc:
        if exc.status in (403, 404):
            return ""
        raise


def delete_session_route(session_id: int, namespace: str) -> None:
    """Delete the session's OpenShift Route; no-op on non-OpenShift or if absent."""
    from kubernetes import client

    custom = client.CustomObjectsApi()
    route_name = f"session-{session_id}-chat"
    try:
        custom.delete_namespaced_custom_object(
            group=_ROUTE_GROUP, version=_ROUTE_VERSION,
            namespace=namespace, plural=_ROUTE_PLURAL, name=route_name,
        )
    except client.exceptions.ApiException as exc:
        if exc.status in (403, 404):
            return
        raise

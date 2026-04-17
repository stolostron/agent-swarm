"""
Kubernetes utility functions used across the dashboard.
All functions use the official kubernetes-client Python library.
"""
import base64
import logging

log = logging.getLogger(__name__)


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

def _build_opencode_config(secret=None) -> str:
    """Return opencode.json content tailored to the available credentials.

    Model priority:
      1. Vertex AI ADC present  → Claude Sonnet 4.6 (best capability)
      2. Google API key present → Gemini 2.5 Flash  (fast, no ADC needed)
      3. Neither               → no default model   (opencode will prompt)
    """
    import json

    if secret and secret.has_adc:
        model = "google-vertex-anthropic/claude-sonnet-4-6@default"
    elif secret and secret.google_api_key_enc:
        model = "google/gemini-2.5-flash"
    else:
        model = None

    config: dict = {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": ["opencode"],
        "server": {
            "hostname": "0.0.0.0",
            "port": 4096,
        },
    }
    if model:
        config["model"] = model

    return json.dumps(config, indent=2)


def apply_opencode_config(namespace: str, secret=None) -> None:
    """Create or update the opencode ConfigMap in the given namespace.

    Pass the workspace's OpencodeSecret so the correct default model is set.
    When called without a secret (e.g. at workspace creation), no model default
    is written and opencode will fall back to its own picker.
    """
    from kubernetes import client

    v1 = client.CoreV1Api()
    body = client.V1ConfigMap(
        metadata=client.V1ObjectMeta(name="opencode-config", namespace=namespace),
        data={
            "opencode.json": _build_opencode_config(secret),
            # Mounted at /etc/gitconfig in session pods so git trusts directories
            # owned by root when opencode runs as a non-root user (e.g. node).
            "gitconfig": "[safe]\n\tdirectory = *\n",
        },
    )
    try:
        v1.replace_namespaced_config_map("opencode-config", namespace, body)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespaced_config_map(namespace, body)
        else:
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


def apply_opencode_secret(namespace: str, secret) -> None:
    """Sync opencode-secret K8s Secret from the DB model."""
    data = {
        "GOOGLE_CLOUD_PROJECT": _b64(secret.google_cloud_project),
        "VERTEX_LOCATION": _b64(secret.vertex_location),
        "GOOGLE_API_KEY": _b64(secret.google_api_key),
    }
    if secret.has_adc:
        data["application_default_credentials.json"] = _b64(
            secret.application_default_credentials
        )
    _apply_secret(namespace, "opencode-secret", data)


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
    """Return True if the image manifest is accessible using the workspace pull secret."""
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
        entry = auths.get(registry) or auths.get(f"https://{registry}") or {}
        auth_b64 = entry.get("auth", "")
        log.debug("check_image_reachable: pull secret found, auth_b64 present=%s, auths keys=%s",
                  bool(auth_b64), list(auths.keys()))
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
    headers: dict[str, str] = {"Accept": accept}
    if auth_b64:
        headers["Authorization"] = f"Basic {auth_b64}"

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as http:
            r = await http.get(url, headers=headers)
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
                        tr = await http.get(realm, params=token_params, auth=creds)
                        log.debug("check_image_reachable: token fetch → %s", tr.status_code)
                        if tr.status_code == 200:
                            token = tr.json().get("token") or tr.json().get("access_token", "")
                            mr = await http.get(url, headers={"Authorization": f"Bearer {token}"})
                            log.debug("check_image_reachable: manifest (bearer) → %s", mr.status_code)
                            return mr.status_code == 200
            log.warning("check_image_reachable: unhandled response %s for %s", r.status_code, url)
    except Exception as exc:
        log.warning("check_image_reachable: HTTP error for %s: %s", url, exc)
    return False


def exec_model_json(pod_name: str, namespace: str, model: str) -> None:
    """Write opencode model.json into a running pod via kubectl exec."""
    import json
    import shlex
    from kubernetes import client
    from kubernetes.stream import stream

    if "/" not in model:
        return
    provider_id, model_id = model.split("/", 1)
    model_data = {
        "recent": [{"providerID": provider_id, "modelID": model_id}],
        "favorite": [],
        "variant": {f"{provider_id}/{model_id}": "default"},
    }
    model_json = json.dumps(model_data)
    cmd = [
        "sh", "-c",
        "mkdir -p /root/.local/state/opencode && "
        f"printf '%s' {shlex.quote(model_json)} > /root/.local/state/opencode/model.json",
    ]
    v1 = client.CoreV1Api()
    stream(
        v1.connect_get_namespaced_pod_exec,
        pod_name, namespace,
        command=cmd,
        stderr=True, stdin=False, stdout=True, tty=False,
    )


def delete_service(service_name: str, namespace: str) -> None:
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_service(service_name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise

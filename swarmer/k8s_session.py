"""
Kubernetes operations specific to sessions:
PVC management, pod spec generation, and Service management.
"""
import json
import logging
import shlex

log = logging.getLogger(__name__)

def ensure_session_pvc(
    namespace: str,
    session_id: int,
    suffix: str,
    pvc_name: str | None = None,
    storage: str = "10Gi",
) -> str:
    """Ensure a PVC exists for the session and return its name.

    If *pvc_name* is given and the PVC still exists, the existing name is
    returned unchanged.  Otherwise a new PVC is created as
    ``session-{session_id}-{suffix}`` so it shares the same identifier as the
    pod created in the same launch.
    """
    from kubernetes import client

    v1 = client.CoreV1Api()
    if pvc_name:
        try:
            v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
            return pvc_name
        except client.exceptions.ApiException as exc:
            if exc.status != 404:
                raise

    new_name = f"session-{session_id}-{suffix}"
    v1.create_namespaced_persistent_volume_claim(
        namespace,
        client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=new_name),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
            ),
        ),
    )
    return new_name


def delete_session_pvc(namespace: str, pvc_name: str) -> None:
    from kubernetes import client

    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise


def build_session_pod(
    session,
    namespace: str,
    image: str,
    suffix: str,
    image_pull_secret: str = "",
    has_adc: bool = False,
    has_gemini: bool = False,
    privileged: bool = False,
) -> "client.V1Pod":
    """
    Build a V1Pod spec for the given session.

    Modes:
    - prompt: opencode run --model <model> [--continue] "<prompt>"
    - server: opencode serve --hostname 0.0.0.0
    - tui:    sleep infinity (user connects via kubectl exec / xterm.js)

    has_adc: when True, the opencode-secret contains an
    'application_default_credentials.json' key that is projected as a file
    and GOOGLE_APPLICATION_CREDENTIALS is set to point at it.
    Either has_adc, a GOOGLE_API_KEY in the secret, or both may be present.
    """
    from kubernetes import client

    pvc_name = session.pvc_name
    pat = session.github_pat  # may be None

    # ---------- env ----------
    # envFrom (below) injects all opencode-secret keys as env vars:
    # GOOGLE_API_KEY, GOOGLE_CLOUD_PROJECT, VERTEX_LOCATION, …
    # GOOGLE_APPLICATION_CREDENTIALS is added only when ADC JSON is present.
    env = []
    if has_adc:
        env.append(client.V1EnvVar(
            name="GOOGLE_APPLICATION_CREDENTIALS",
            value="/app/gcloud/credentials.json",
        ))
    if pat:
        env.append(
            client.V1EnvVar(
                name="GITHUB_PAT",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=pat.k8s_secret_name,
                        key="GITHUB_PAT",
                        optional=True,
                    )
                ),
            )
        )
        env.append(
            client.V1EnvVar(
                name="GITHUB_USERNAME",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=pat.k8s_secret_name,
                        key="GITHUB_USERNAME",
                        optional=True,
                    )
                ),
            )
        )

    # ---------- volumes ----------
    volumes = [
        client.V1Volume(
            name="session-workspace",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=pvc_name
            ),
        ),
        client.V1Volume(
            name="opencode-config",
            config_map=client.V1ConfigMapVolumeSource(name="opencode-config"),
        ),
    ]

    volume_mounts = [
        client.V1VolumeMount(
            name="session-workspace",
            mount_path="/workspace",
        ),
        client.V1VolumeMount(
            name="opencode-config",
            mount_path="/home/node/.config/opencode",
            read_only=True,
        ),
        client.V1VolumeMount(
            name="opencode-config",
            mount_path="/etc/gitconfig",
            sub_path="gitconfig",
            read_only=True,
        ),
    ]

    if has_adc:
        volumes.append(
            client.V1Volume(
                name="gcloud-creds",
                secret=client.V1SecretVolumeSource(
                    secret_name="opencode-secret",
                    items=[
                        client.V1KeyToPath(
                            key="application_default_credentials.json",
                            path="credentials.json",
                        )
                    ],
                ),
            )
        )
        volume_mounts.append(
            client.V1VolumeMount(
                name="gcloud-creds",
                mount_path="/app/gcloud",
                read_only=True,
            )
        )

    # ---------- init container (git clone) ----------
    init_containers = []
    if session.repos:
        clone_cmds = []
        for repo in session.repos:
            clone_cmds.append(
                f"[ -d /workspace/{repo.local_path}/.git ] || "
                f"git clone {repo.repo_url} --branch {repo.branch} "
                f"/workspace/{repo.local_path}"
            )

        # Credential setup: write a .git-credentials file so git can authenticate
        # over HTTPS without any interactive prompt.  The file is written only when
        # a PAT is assigned; public repos work without it.
        credential_setup = (
            "if [ -n \"${GITHUB_PAT}\" ]; then "
            "git config --global credential.helper store && "
            "echo \"https://${GITHUB_USERNAME}:${GITHUB_PAT}@github.com\" "
            "> $HOME/.git-credentials; "
            "fi"
        )
        full_cmd = credential_setup + " && " + " && ".join(clone_cmds)

        git_env = []
        if pat:
            git_env.append(
                client.V1EnvVar(
                    name="GITHUB_PAT",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=pat.k8s_secret_name,
                            key="GITHUB_PAT",
                            optional=True,
                        )
                    ),
                )
            )
            git_env.append(
                client.V1EnvVar(
                    name="GITHUB_USERNAME",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=pat.k8s_secret_name,
                            key="GITHUB_USERNAME",
                            optional=True,
                        )
                    ),
                )
            )

        init_containers.append(
            client.V1Container(
                name="git-init",
                image="alpine/git:latest",
                command=["sh", "-c", full_cmd],
                env=git_env,
                volume_mounts=[
                    client.V1VolumeMount(
                        name="session-workspace", mount_path="/workspace"
                    )
                ],
            )
        )

    # ---------- resolve model (all modes) ----------
    if session.model:
        model = session.model
    elif has_adc:
        model = "google-vertex-anthropic/claude-sonnet-4-6@default"
    elif has_gemini:
        model = "google/gemini-2.5-flash"
    else:
        model = "google/gemini-2.5-flash"  # safest unauthenticated fallback

    # Write providerID + modelID into opencode's state file so it picks the
    # right model automatically in every session mode (prompt / server / TUI).
    # We use a shell preamble so the parent directory is guaranteed to exist.
    if "/" in model:
        provider_id, model_id = model.split("/", 1)
        model_json = json.dumps({
            "recent": [{"providerID": provider_id, "modelID": model_id}],
            "favorite": [],
            "variant": {f"{provider_id}/{model_id}": "default"},
        })
        model_setup = (
            "mkdir -p $HOME/.local/state/opencode && "
            f"printf '%s' {shlex.quote(model_json)} "
            "> $HOME/.local/state/opencode/model.json && "
        )
    else:
        model_setup = ""

    # Symlink opencode's share dir into the PVC so session history persists
    # across pod restarts. Must run before opencode starts.
    share_setup = (
        "mkdir -p /workspace/.opencode $HOME/.local/share && "
        "rm -rf $HOME/.local/share/opencode && "
        "ln -sf /workspace/.opencode $HOME/.local/share/opencode && "
    )

    # ---------- main container command ----------
    ports = []
    if session.mode == "server":
        main_cmd = "opencode serve --hostname 0.0.0.0 --port 4096"
        restart_policy = "Always"
        ports = [client.V1ContainerPort(container_port=4096, name="opencode")]
    elif session.mode == "tui":
        # Pod stays alive; user connects via xterm.js + kubectl exec
        main_cmd = "sleep infinity"
        restart_policy = "Always"
    else:  # prompt
        cmd_parts = ["opencode", "run", "--model", model]
        if session.resume:
            cmd_parts.append("--continue")
        if session.instruction_prompt:
            cmd_parts.append(session.instruction_prompt)
        main_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        restart_policy = "Never"

    args = ["sh", "-c", share_setup + model_setup + main_cmd]

    # ---------- envFrom (opencode-secret provides GCP + Gemini creds) ----------
    env_from = [
        client.V1EnvFromSource(
            secret_ref=client.V1SecretEnvSource(
                name="opencode-secret", optional=True
            )
        )
    ]

    # ---------- container ----------
    security_context = None
    if privileged:
        security_context = client.V1SecurityContext(
            privileged=True,
            run_as_user=0,
        )

    container = client.V1Container(
        name="opencode",
        image=image,
        image_pull_policy="IfNotPresent",
        working_dir="/workspace",
        args=args,
        env=env,
        env_from=env_from,
        volume_mounts=volume_mounts,
        ports=ports or None,
        stdin=session.mode == "tui",
        tty=session.mode == "tui",
        security_context=security_context,
        resources=client.V1ResourceRequirements(
            requests={"memory": "512Mi", "cpu": "500m"},
            limits={"memory": "2Gi", "cpu": "2000m"},
        ),
    )

    # ---------- image pull secret ----------
    image_pull_secrets = []
    if image_pull_secret:
        image_pull_secrets = [
            client.V1LocalObjectReference(name=image_pull_secret)
        ]

    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=f"session-{session.id}-{suffix}",
            namespace=namespace,
            labels={
                "app": "swarmer-session",
                "session-id": str(session.id),
            },
        ),
        spec=client.V1PodSpec(
            restart_policy=restart_policy,
            init_containers=init_containers or None,
            containers=[container],
            volumes=volumes,
            image_pull_secrets=image_pull_secrets or None,
        ),
    )


def create_session_service(session_id: int, namespace: str) -> str:
    """Create a ClusterIP Service for an interactive (server-mode) session.
    Returns the service name."""
    from kubernetes import client

    v1 = client.CoreV1Api()
    svc_name = f"session-{session_id}-svc"
    try:
        v1.read_namespaced_service(svc_name, namespace)
    except client.exceptions.ApiException as exc:
        if exc.status == 404:
            v1.create_namespaced_service(
                namespace,
                client.V1Service(
                    metadata=client.V1ObjectMeta(name=svc_name),
                    spec=client.V1ServiceSpec(
                        selector={"session-id": str(session_id)},
                        ports=[
                            client.V1ServicePort(port=4096, target_port=4096, name="opencode")
                        ],
                        type="ClusterIP",
                    ),
                ),
            )
        else:
            raise
    return svc_name


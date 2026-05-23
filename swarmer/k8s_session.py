"""
Kubernetes operations specific to sessions:
PVC management, pod spec generation, and Service management.
"""
import logging
import shlex

from swarmer.config import settings

log = logging.getLogger(__name__)


def _build_repo_context(repos) -> str:
    """Build a markdown section listing workspace repositories.

    Returns an empty string when *repos* is empty so callers can
    unconditionally concatenate the result.
    """
    if not repos:
        return ""
    lines = [
        "\n\n## Workspace Repositories\n",
        "The following Git repositories are available in this workspace:\n",
        "| Repository | Branch | Path |",
        "|---|---|---|",
    ]
    for repo in repos:
        # Extract org/repo from URL (e.g. "stolostron/agent-swarm")
        org_repo = repo.repo_url.rstrip("/").removesuffix(".git")
        org_repo = "/".join(org_repo.split("/")[-2:])
        lines.append(
            f"| `{org_repo}` | `{repo.branch}` "
            f"| `/workspace/{repo.local_path}` |"
        )
    return "\n".join(lines) + "\n"


def ensure_session_pvc(
    namespace: str,
    session_id: int,
    suffix: str,
    pvc_name: str | None = None,
    storage: str = "5Gi",
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
    agent_tool: str = "opencode",
    mcp_servers=None,
    agent_secret_name: str = "",
    pat_secret_name: str = "",
    mcp_secret_name: str = "",
    resolved_prompt: str = "",
):  # -> client.V1Pod
    """Build a V1Pod spec for the given session.

    Delegates tool-specific behavior (commands, config paths, env vars)
    to the AgentToolStrategy identified by *agent_tool*.
    """
    from kubernetes import client
    from swarmer.agent_tools.registry import get as get_tool

    tool = get_tool(agent_tool)

    pvc_name = session.pvc_name
    pat = session.github_pat  # may be None

    # ---------- env ----------
    env = [
        client.V1EnvVar(name="HOME", value="/workspace"),
        client.V1EnvVar(name="NODE_OPTIONS", value="--max-old-space-size=1536"),
    ]
    env.extend(tool.get_extra_env(has_adc))
    _pat_k8s_name = pat_secret_name or (pat.k8s_secret_name if pat else "")
    if pat and _pat_k8s_name:
        env.append(
            client.V1EnvVar(
                name="GITHUB_PAT",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=_pat_k8s_name,
                        key="GITHUB_PAT",
                        optional=True,
                    )
                ),
            )
        )
        env.append(
            client.V1EnvVar(
                name="GH_TOKEN",
                value_from=client.V1EnvVarSource(
                    secret_key_ref=client.V1SecretKeySelector(
                        name=_pat_k8s_name,
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
                        name=_pat_k8s_name,
                        key="GITHUB_USERNAME",
                        optional=True,
                    )
                ),
            )
        )

    # Append structured repo context so the LLM knows which repos
    # are available and where they live under /workspace/.
    repo_context = _build_repo_context(session.repos) if session.repos else ""
    if repo_context:
        resolved_prompt = (resolved_prompt or "") + repo_context

    if resolved_prompt and session.mode in ("tui", "server"):
        env.append(client.V1EnvVar(name="SWARMER_AGENT_MD", value=resolved_prompt))

    # ---------- volumes ----------
    volumes = [
        client.V1Volume(
            name="session-workspace",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=pvc_name
            ),
        ),
        client.V1Volume(
            name="agent-config",
            config_map=client.V1ConfigMapVolumeSource(
                name=tool.get_config_map_name()
            ),
        ),
    ]
    volumes.extend(tool.get_extra_volumes(has_adc, secret_name=agent_secret_name))

    volume_mounts = [
        client.V1VolumeMount(
            name="session-workspace",
            mount_path="/workspace",
        ),
        client.V1VolumeMount(
            name="agent-config",
            mount_path="/tmp/agent-config-ro",
            read_only=True,
        ),
    ]
    volume_mounts.extend(tool.get_extra_volume_mounts(has_adc))

    # ---------- init container (git clone) ----------
    init_containers = []
    if session.repos:
        clone_cmds = []
        for repo in session.repos:
            lp = shlex.quote(repo.local_path)
            clone_cmds.append(
                f"[ -d /workspace/{lp}/.git ] || "
                f"git clone {shlex.quote(repo.repo_url)} --branch {shlex.quote(repo.branch)} "
                f"/workspace/{lp}"
            )

        credential_setup = (
            "if [ -n \"${GITHUB_PAT}\" ]; then "
            "git config --global credential.helper store && "
            "echo \"https://${GITHUB_USERNAME}:${GITHUB_PAT}@github.com\" "
            "> \"${HOME}/.git-credentials\"; "
            "fi"
        )
        full_cmd = credential_setup + " && " + " && ".join(clone_cmds)

        git_env = [client.V1EnvVar(name="HOME", value="/workspace")]
        if pat and _pat_k8s_name:
            git_env.append(
                client.V1EnvVar(
                    name="GITHUB_PAT",
                    value_from=client.V1EnvVarSource(
                        secret_key_ref=client.V1SecretKeySelector(
                            name=_pat_k8s_name,
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
                            name=_pat_k8s_name,
                            key="GITHUB_USERNAME",
                            optional=True,
                        )
                    ),
                )
            )

        init_containers.append(
            client.V1Container(
                name="git-init",
                image=tool.get_image(),
                command=["sh", "-c", full_cmd],
                env=git_env,
                volume_mounts=[
                    client.V1VolumeMount(
                        name="session-workspace", mount_path="/workspace"
                    )
                ],
                resources=client.V1ResourceRequirements(
                    requests={"memory": "256Mi", "cpu": "100m"},
                    limits={"memory": "512Mi", "cpu": "500m"},
                ),
            )
        )

    # ---------- resolve model ----------
    if session.model and tool.is_valid_model(session.model):
        model = session.model
    else:
        model = tool.get_default_model(has_adc, has_gemini)

    model_setup = tool.build_model_setup_cmd(model)
    share_setup = tool.build_share_setup_cmd()
    agent_md_setup = ""
    if resolved_prompt and session.mode in ("tui", "server"):
        agent_md_setup = "printf '%s' \"${SWARMER_AGENT_MD}\" > /workspace/AGENTS.md && "

    # ---------- main container command ----------
    ports = []
    if session.mode == "server":
        restart_policy = "Always"
        ports = tool.get_server_mode_ports()
    elif session.mode == "tui":
        restart_policy = "Always"
    else:
        restart_policy = "Never"

    main_cmd = tool.build_main_cmd(session, model, resolved_prompt=resolved_prompt)
    config_path = tool.get_config_mount_path()
    config_setup = (
        f'mkdir -p {config_path} && '
        f'cp -n /tmp/agent-config-ro/* {config_path}/ 2>/dev/null || true && '
    )
    safe_dir_setup = ""
    if session.repos:
        safe_dir_setup = "git config --global --add safe.directory '*' && "
    git_setup = (
        'if [ -n "${GITHUB_PAT}" ] && command -v git >/dev/null 2>&1; then '
        'git config --global credential.helper store && '
        'echo "https://${GITHUB_USERNAME}:${GITHUB_PAT}@github.com" > "${HOME}/.git-credentials" && '
        'git config --global user.name "${GITHUB_USERNAME}" && '
        'git config --global user.email "${GITHUB_USERNAME}@users.noreply.github.com"; '
        'fi && '
    )
    branch_setup = ""
    if session.repos and getattr(session, "working_branch", ""):
        branch = shlex.quote(session.working_branch)
        for repo in session.repos:
            lp = shlex.quote(repo.local_path)
            branch_setup += (
                f"cd /workspace/{lp} && "
                f"git checkout -b {branch} 2>/dev/null || git checkout {branch} && "
                f"cd /workspace && "
            )

    # Per-session MCP config override (overwrites the shared ConfigMap config)
    # mcp_servers=None means "not applicable" (no override needed)
    # mcp_servers=[] means "explicitly disabled" (write config without MCP)
    # mcp_servers=[...] means "these specific servers" (write config with them)
    mcp_config_setup = tool.build_mcp_config_cmd(mcp_servers) if mcp_servers is not None else ""

    command = ["sh", "-c", config_setup + mcp_config_setup + safe_dir_setup + git_setup + share_setup + agent_md_setup + model_setup + branch_setup + main_cmd]

    # ---------- envFrom ----------
    from swarmer.k8s import AGENT_EXTRA_ENV_SECRET_NAME, MCP_SECRET_NAME

    env_from = tool.get_env_from_sources(secret_name=agent_secret_name)

    # Inject MCP server credentials from the session-scoped K8s secret
    if mcp_servers:
        env_from.append(
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name=mcp_secret_name or MCP_SECRET_NAME, optional=True
                )
            )
        )

    # Optional workspace Secret (keys → env vars); not managed by Swarmer
    env_from.append(
        client.V1EnvFromSource(
            secret_ref=client.V1SecretEnvSource(
                name=AGENT_EXTRA_ENV_SECRET_NAME, optional=True
            )
        )
    )

    # ---------- container ----------
    # Non-privileged sessions omit runAsUser so OpenShift can assign a UID from
    # the namespace's allowed range (restricted SCC).  The pod-level fsGroup
    # ensures the shared PVC (/workspace) is group-writable regardless of UID.
    # The `privileged` flag forces UID 0 and enables Linux privileged mode
    # (raw sockets, device access, etc.) — requires anyuid or privileged SCC.
    security_context = client.V1SecurityContext()
    if privileged:
        security_context.run_as_user = 0
        security_context.privileged = True

    container = client.V1Container(
        name=tool.get_container_name(),
        image=tool.get_image(),
        image_pull_policy=settings.agent_image_pull_policy,
        working_dir="/workspace",
        command=command,
        env=env,
        env_from=env_from,
        volume_mounts=volume_mounts,
        ports=ports or None,
        stdin=session.mode == "tui",
        tty=session.mode == "tui",
        security_context=security_context,
        resources=client.V1ResourceRequirements(
            requests={"memory": "1Gi", "cpu": "500m"},
            limits={"memory": "8Gi", "cpu": "2000m"},
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
                "agent-tool": agent_tool,
            },
        ),
        spec=client.V1PodSpec(
            restart_policy=restart_policy,
            security_context=client.V1PodSecurityContext(
                fs_group=0 if privileged else 1000,
            ),
            init_containers=init_containers or None,
            containers=[container],
            volumes=volumes,
            image_pull_secrets=image_pull_secrets or None,
        ),
    )


def create_session_service(
    session_id: int, namespace: str, port: int = 4096, port_name: str = "agent"
) -> str:
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
                            client.V1ServicePort(
                                port=port, target_port=port, name=port_name
                            )
                        ],
                        type="ClusterIP",
                    ),
                ),
            )
        else:
            raise
    return svc_name

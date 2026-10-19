from urllib.parse import urlparse

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key_file: str = "auth/secret.key"
    openshift_oauth_url: str = ""   # e.g. https://oauth-openshift.apps.example.com
    redirect_base_url: str = ""     # e.g. https://swarmer-swarmer.apps.example.com  (explicit callback base)
    k8s_api_url: str = "https://kubernetes.default.svc"
    database_url: str = "sqlite+aiosqlite:///data/swarmer.db"
    k8s_in_cluster: bool = False
    host: str = "0.0.0.0"
    port: int = 8080
    agent_image_opencode: str = ""
    # Container image for the shell agent tool. Falls back to agent_image_opencode
    # if unset — the OpenCode image already contains Python 3, bash, and git.
    agent_image_shell: str = ""
    default_agent_tool: str = "opencode"
    k8s_namespace: str = ""
    pod_namespace: str = ""
    max_concurrent_agents: int = 5

    # Workspace access control (ACM-41659) — database-backed ACL replaces
    # per-workspace K8s namespace + RoleBinding RBAC now that OpenShell owns
    # sandbox lifecycle. Comma-separated K8s usernames / OIDC usernames and
    # groups that can see and manage every workspace, regardless of
    # ownership/membership (e.g. "alice,system:serviceaccount:swarmer:bob").
    workspace_admin_users: str = ""
    workspace_admin_groups: str = ""
    # "all" — any authenticated user may create a workspace (and becomes its
    #   owner); "admins" — only workspace_admin_users/workspace_admin_groups
    #   may create workspaces.
    workspace_create_policy: str = "all"
    # Completed prompt-mode runs kept per session (includes logs). 0 = unlimited.
    session_run_history_limit: int = 100
    # Max age (days) of completed prompt-mode runs kept per session. 0 = disabled.
    # Applied together with session_run_history_limit — whichever prunes more wins.
    session_run_history_max_age_days: int = 7

    # OpenShell integration — replaces K8s pod/Secret management (ACM-34850)
    openshell_gateway_url: str = ""
    openshell_supervisor_url: str = ""
    openshell_tls_cert: str = ""        # path to client TLS cert
    openshell_tls_key: str = ""         # path to client TLS key
    openshell_tls_ca: str = ""          # path to CA bundle
    openshell_tls_verify: bool = True   # verify gateway server certificates
    openshell_bearer_token: str = ""    # bearer token for gateway/supervisor auth
    agent_swarm_internal_url: str = ""  # internal URL used by the Agent Swarm MCP
    sandbox_gc_interval: int = 300      # seconds between sandbox GC sweeps
    log_level: str = "INFO"             # Python logging level: DEBUG, INFO, WARNING, ERROR

    # Swarm PR Events Watcher & Session Dispatcher (ACM-42674)
    pr_watcher_enabled: bool = True
    pr_watcher_poll_interval: int = 30
    pr_watcher_sweep_interval: int = 1800
    pr_watcher_debounce_seconds: int = 90
    pr_watcher_max_fix_attempts: int = 3

    # Model preset mappings (ACM-37232) — configurable without code changes.
    # Each preset maps a role (plan/build/small) to a provider/model@version ID.
    # "plan" = interactive/stronger-reasoning model (used by the opencode plan agent)
    # "build" = opencode run / coding agent model (also the model shown as "current")
    # "small" = title generation / housekeeping model
    # OpenCode V2: Vertex models use the canonical "google-vertex" provider ID
    # (the V1 "google-vertex-anthropic" ID is rejected by V2).
    claude_preset_plan_model: str = "google-vertex/claude-opus-4-6@default"
    claude_preset_build_model: str = "google-vertex/claude-sonnet-5@default"
    claude_preset_small_model: str = "google-vertex/claude-haiku-4-5@20251001"
    gemini_preset_plan_model: str = "google/gemini-4-argon"
    gemini_preset_build_model: str = "google/gemini-3.8-flash"
    gemini_preset_small_model: str = "google/gemini-3.5-flash-lite"
    openai_preset_plan_model: str = "openai/gpt-6.1-sol"
    openai_preset_build_model: str = "openai/gpt-6-luna"
    openai_preset_small_model: str = "openai/gpt-6-luna"
    # EnMaaS preset role models — every default role stays within EnMaaS so an
    # EnMaaS session can never silently select a paid provider model. The
    # upstream model IDs contain slashes; references are always split at the
    # FIRST "/" only (provider prefix = "enmaas").
    enmaas_preset_plan_model: str = "enmaas/rits/zai-org/glm-5-3"
    enmaas_preset_build_model: str = "enmaas/rits/zai-org/glm-5-3"
    enmaas_preset_small_model: str = "enmaas/rits/zai-org/glm-5-3"
    # Enables the opencode plan agent so the preset "plan" model is actually used
    # by `opencode run` (see docs/USER_GUIDE.md — Model Selection).
    opencode_experimental_plan_mode: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()


def validate_enmaas_base_url(raw: str) -> str:
    """Validate and normalize a workspace-supplied EnMaaS gateway base URL.

    Returns the normalized URL (trailing slashes stripped, ``/v1`` style path
    retained), or ``""`` when *raw* is empty/unset. Raises ``ValueError`` for
    invalid input: non-HTTPS scheme, missing hostname, embedded credentials
    (user info), query strings, or fragments.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    parsed = urlparse(cleaned)
    if parsed.scheme != "https":
        raise ValueError("EnMaaS gateway base URL must use https.")
    if not parsed.hostname:
        raise ValueError("EnMaaS gateway base URL must include a hostname.")
    if parsed.username or parsed.password:
        raise ValueError("EnMaaS gateway base URL must not include credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("EnMaaS gateway base URL must not include a query string or fragment.")
    # Reassemble without credentials/query/fragment; strip trailing slashes so
    # consumers can append paths predictably. The configured path (e.g. /v1)
    # is retained verbatim.
    netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    path = parsed.path.rstrip("/")
    return f"https://{netloc}{path}"


def sanitize_enmaas_base_url(raw: str) -> str:
    """Return the validated workspace EnMaaS gateway URL, or "" when unset.

    Invalid values also return "" (with a warning log) so UI render paths
    degrade to "not configured" instead of raising. The value comes from the
    workspace's encrypted credential metadata.
    """
    import logging

    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    try:
        return validate_enmaas_base_url(cleaned)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Ignoring invalid EnMaaS gateway base URL (must be an https:// "
            "URL without credentials, query string, or fragment)",
        )
        return ""


def enmaas_endpoint_from_url(base_url: str) -> tuple[str, int] | None:
    """Return the (host, port) of an EnMaaS gateway URL, or None when unset.

    Invalid values also return None — display paths degrade to
    "not configured". Shared by the OpenShell provider profile and the
    sandbox network policy so both always derive their endpoint from the
    same validated workspace URL.
    """
    base = sanitize_enmaas_base_url(base_url)
    if not base:
        return None
    parsed = urlparse(base)
    return (parsed.hostname or "", parsed.port or 443)


def get_agent_swarm_internal_url() -> str:
    """Return the URL used by sandbox MCP clients to reach Swarmer."""
    configured = settings.agent_swarm_internal_url.strip()
    if configured:
        return configured.rstrip("/")
    namespace = (settings.pod_namespace or settings.k8s_namespace).strip()
    if namespace:
        return f"http://swarmer.{namespace}.svc.cluster.local:8080"
    return "http://127.0.0.1:8080"

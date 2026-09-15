from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key_file: str = "auth/secret.key"
    openshift_oauth_url: str = ""   # e.g. https://oauth-openshift.apps.example.com
    redirect_base_url: str = ""     # e.g. https://swarmer-swarmer.apps.example.com  (explicit callback base)
    k8s_api_url: str = "https://kubernetes.default.svc"
    database_url: str = "sqlite+aiosqlite:///data/swarmer.db"
    k8s_in_cluster: bool = False
    # Runtime deployment flavor. Kubernetes keeps TokenReview authentication;
    # OpenShell uses verified OIDC or the file-based bootstrap token.
    swarmer_runtime_mode: str = "kubernetes"
    host: str = "0.0.0.0"
    port: int = 8080
    agent_image_opencode: str = ""
    # Container image for the shell agent tool. Falls back to agent_image_opencode
    # if unset — the OpenCode image already contains Python 3, bash, and git.
    agent_image_shell: str = ""
    default_agent_tool: str = "opencode"
    k8s_namespace: str = ""
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
    openshell_gateway_url: str = Field(
        default="",
        validation_alias=AliasChoices("OPENSHELL_GATEWAY_URL", "SWARMER_GATEWAY_URL"),
    )
    openshell_supervisor_url: str = ""
    openshell_tls_cert: str = Field(
        default="",
        validation_alias=AliasChoices("OPENSHELL_TLS_CERT", "SWARMER_GATEWAY_TLS_CERT"),
    )        # path to client TLS cert
    openshell_tls_key: str = Field(
        default="",
        validation_alias=AliasChoices("OPENSHELL_TLS_KEY", "SWARMER_GATEWAY_TLS_KEY"),
    )         # path to client TLS key
    openshell_tls_ca: str = Field(
        default="",
        validation_alias=AliasChoices("OPENSHELL_TLS_CA", "SWARMER_GATEWAY_TLS_CA"),
    )          # path to CA bundle
    openshell_tls_verify: bool = True   # verify gateway server certificates
    openshell_bearer_token: str = ""    # bearer token for gateway/supervisor auth
    openshell_auth_mode: str = "oidc"
    openshell_oidc_issuer: str = ""
    openshell_oidc_client_id: str = ""
    openshell_oidc_audience: str = ""
    swarmer_admin_token_file: str = ""
    swarmer_admin_username: str = "admin"
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
    claude_preset_plan_model: str = "google-vertex-anthropic/claude-opus-4-6@default"
    claude_preset_build_model: str = "google-vertex-anthropic/claude-sonnet-5@default"
    claude_preset_small_model: str = "google-vertex-anthropic/claude-haiku-4-5@20251001"
    gemini_preset_plan_model: str = "google/gemini-3.7-flash"
    gemini_preset_build_model: str = "google/gemini-3.7-flash"
    gemini_preset_small_model: str = "google/gemini-3.5-flash-lite"
    openai_preset_plan_model: str = "openai/gpt-5.6-terra"
    openai_preset_build_model: str = "openai/gpt-5.6-luna"
    openai_preset_small_model: str = "openai/gpt-5.6-luna-fast"
    # Enables the opencode plan agent so the preset "plan" model is actually used
    # by `opencode run` (see docs/USER_GUIDE.md — Model Selection).
    opencode_experimental_plan_mode: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

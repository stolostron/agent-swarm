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
    default_agent_tool: str = "opencode"
    k8s_namespace: str = ""
    max_concurrent_agents: int = 5
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
    openshell_bearer_token: str = ""    # bearer token for gateway/supervisor auth
    sandbox_gc_interval: int = 300      # seconds between sandbox GC sweeps
    log_level: str = "INFO"             # Python logging level: DEBUG, INFO, WARNING, ERROR

    # Model preset mappings (ACM-37232) — configurable without code changes.
    # Each preset maps a role (plan/build/small) to a provider/model@version ID.
    # "plan" = interactive/stronger-reasoning model (used by the opencode plan agent)
    # "build" = opencode run / coding agent model (also the model shown as "current")
    # "small" = title generation / housekeeping model
    claude_preset_plan_model: str = "google-vertex-anthropic/claude-opus-4-6@default"
    claude_preset_build_model: str = "google-vertex-anthropic/claude-sonnet-5@default"
    claude_preset_small_model: str = "google-vertex-anthropic/claude-haiku-4-5@20251001"
    gemini_preset_plan_model: str = "google/gemini-3.1-pro-preview"
    gemini_preset_build_model: str = "google/gemini-3.5-flash"
    gemini_preset_small_model: str = "google/gemini-3.1-flash-lite"
    # Enables the opencode plan agent so the preset "plan" model is actually used
    # by `opencode run` (see docs/USER_GUIDE.md — Model Selection).
    opencode_experimental_plan_mode: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

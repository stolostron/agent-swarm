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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

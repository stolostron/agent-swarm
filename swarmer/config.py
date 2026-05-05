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
    agent_image: str = ""
    agent_image_opencode: str = ""
    agent_image_crush: str = ""
    crush_version: str = "0.57.0"
    default_agent_tool: str = "opencode"
    crush_server_port: int = 4096
    agent_image_pull_secret: str = ""
    agent_image_pull_policy: str = "IfNotPresent"
    k8s_namespace: str = ""
    # Externally reachable base URL of swarmer (e.g. https://swarmer.apps.example.com).
    # Used to construct the Atlassian OAuth redirect URI.
    # When blank, falls back to deriving from request.base_url at runtime.
    swarmer_public_url: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

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
    default_agent_tool: str = "opencode"
    crush_server_port: int = 4096
    agent_image_pull_secret: str = ""
    agent_image_pull_policy: str = "IfNotPresent"
    k8s_namespace: str = ""
    
    # Chat proxy timeout settings (seconds)
    chat_proxy_timeout_connect: int = 30
    chat_proxy_timeout_read: int = 300    # 5 minutes for thinking operations
    chat_proxy_timeout_write: int = 30
    chat_proxy_timeout_pool: int = 30
    chat_proxy_timeout_standard: int = 120  # 2 minutes for standard requests
    
    # LLM API timeout for commit message generation (seconds)
    llm_api_timeout: int = 60

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

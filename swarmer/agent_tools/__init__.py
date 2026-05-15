from abc import ABC, abstractmethod


class AgentToolStrategy(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        ...

    @abstractmethod
    def get_image(self) -> str:
        ...

    @abstractmethod
    def get_config_map_name(self) -> str:
        ...

    @abstractmethod
    def build_config_data(self, secret=None, mcp_servers=None) -> dict[str, str]:
        ...

    @abstractmethod
    def get_config_mount_path(self) -> str:
        ...

    @abstractmethod
    def get_secret_name(self) -> str:
        ...

    @abstractmethod
    def get_container_name(self) -> str:
        ...

    @abstractmethod
    def get_server_port(self) -> int | None:
        ...

    @abstractmethod
    def get_share_dir(self) -> str:
        ...

    @abstractmethod
    def build_share_setup_cmd(self) -> str:
        ...

    @abstractmethod
    def build_model_setup_cmd(self, model: str) -> str:
        ...

    @abstractmethod
    def build_main_cmd(self, session, model: str, resolved_prompt: str = "") -> str:
        ...

    @abstractmethod
    def get_server_mode_ports(self) -> list:
        ...

    def get_tui_binary(self) -> str:
        """Return the binary name to exec in TUI mode. Defaults to tool name."""
        return self.name

    def is_valid_model(self, model: str) -> bool:
        """Return True if *model* is a valid/compatible model string for this tool."""
        return True

    @abstractmethod
    def get_model_options(self, secret=None) -> list[dict]:
        ...

    @abstractmethod
    def get_default_model(self, has_adc: bool, has_gemini: bool) -> str:
        ...

    @abstractmethod
    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        ...

    @abstractmethod
    def get_env_from_sources(self, secret_name: str = "") -> list:
        ...

    @abstractmethod
    def get_extra_env(self, has_adc: bool) -> list:
        ...

    @abstractmethod
    def get_extra_volumes(self, has_adc: bool, secret_name: str = "") -> list:
        ...

    @abstractmethod
    def get_extra_volume_mounts(self, has_adc: bool) -> list:
        ...

    @abstractmethod
    def build_k8s_secret_data(self, secret) -> dict[str, str]:
        ...

    def build_mcp_config_cmd(self, mcp_servers) -> str:
        """Return a shell command that writes a session-specific config with the
        given MCP servers.  Called at pod startup to override the shared ConfigMap.
        Default implementation returns empty string (no override)."""
        return ""

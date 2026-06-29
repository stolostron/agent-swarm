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
    def build_config_data(self, secret=None, mcp_servers=None, use_inference_local: bool = False, model: str = "") -> dict[str, str]:
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

    def get_tui_binary(self) -> str:
        """Return the binary name to exec in TUI mode. Defaults to tool name."""
        return self.name

    def is_valid_model(self, model: str) -> bool:
        """Return True if *model* is a valid/compatible model string for this tool."""
        return True

    @abstractmethod
    def get_model_options(self, secret=None, has_vertex: bool = False) -> list[dict]:
        ...

    @abstractmethod
    def get_default_model(self, has_adc: bool) -> str:
        ...

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
    def get_model_options(self, secret=None, has_vertex: bool = False, has_gemini: bool = False) -> list[dict]:
        ...

    @abstractmethod
    def get_default_model(self, has_adc: bool) -> str:
        ...

    def get_preset_options(self, has_vertex: bool = False, has_gemini: bool = False) -> list[dict]:
        """Return family-level model presets (e.g. Claude/Gemini) for the UI.

        Each dict has: value (preset name), label, group, and available (bool)
        indicating whether the required provider credential is configured.
        When unavailable, callers should render the option disabled with an
        explanatory hint rather than hiding it silently.
        """
        return []

    def resolve_preset(self, preset: str) -> dict[str, str] | None:
        """Resolve a preset name to its {plan, build, small} model ID mapping.

        Returns None if *preset* is not a recognised preset name for this tool.
        """
        return None

    def is_preset(self, model: str) -> bool:
        """Return True if *model* is a preset name rather than a raw model ID."""
        return self.resolve_preset(model) is not None

    def resolve_build_model(self, model: str) -> str:
        """Return the concrete BUILD-role model ID for *model*.

        If *model* is a preset name, resolves it via resolve_preset() and
        returns its "build" model ID. Otherwise returns *model* unchanged
        (it is assumed to already be a concrete model ID).
        """
        preset = self.resolve_preset(model)
        return preset["build"] if preset else model

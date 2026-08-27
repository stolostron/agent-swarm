from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from swarmer.models.mcp_server import McpServer
    from swarmer.models.opencode_secret import OpencodeSecret
    from swarmer.models.session import Session


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
    def build_config_data(
        self,
        secret: "OpencodeSecret | None" = None,
        mcp_servers: "list[McpServer] | None" = None,
        use_inference_local: bool = False,
        model: str = "",
    ) -> dict[str, str]:
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
    def build_main_cmd(self, session: "Session", model: str, resolved_prompt: str = "") -> str:
        """Return the shell command string to execute inside the sandbox.

        Implementations must handle at least prompt mode.  TUI mode (return a
        long-running command such as ``sleep infinity``) and server mode are
        optional — raise ``ValueError`` for unsupported modes.

        Security contract for implementers:
            The returned string is passed directly to ``["sh", "-c", cmd]``
            by the caller in ``swarmer/routers/sessions.py``.  Do NOT attempt
            to sanitise or escape ``instruction_prompt`` — shell metacharacters
            are intentional for tools that run arbitrary commands (e.g.
            ``ShellStrategy``).  For AI tools the command is a static binary
            invocation that does not interpolate user input, so injection is
            not a concern there either.  The sandbox container (network policy,
            filesystem restriction, process isolation) is the security boundary.
        """
        ...

    def get_tui_binary(self) -> str:
        """Return the binary name to exec in TUI mode. Defaults to tool name."""
        return self.name

    def is_valid_model(self, model: str) -> bool:
        """Return True if *model* is a valid/compatible model string for this tool."""
        return True

    @abstractmethod
    def get_model_options(
        self,
        secret: "OpencodeSecret | None" = None,
        has_vertex: bool = False,
        has_gemini: bool = False,
        has_openai: bool = False,
    ) -> list[dict]:
        ...

    @abstractmethod
    def get_default_model(self, has_adc: bool) -> str:
        ...

    def get_preset_options(
        self,
        has_vertex: bool = False,
        has_gemini: bool = False,
        has_openai: bool = False,
    ) -> list[dict]:
        """Return family-level model presets (e.g. Claude/Gemini/OpenAI) for the UI.

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

    def requires_ai_model(self) -> bool:
        """Return True if this tool needs an AI model and provider credentials.

        Used to gate AI-provider setup (Google AI Studio, Vertex AI, etc.) —
        tools that return False skip provider creation entirely, reducing latency
        and avoiding unnecessary credential injection into the sandbox.

        Defaults to True; override to False for non-AI tools such as ShellStrategy.
        """
        return True

    def supports_server_mode(self) -> bool:
        """Return True if this tool can run in server mode.

        Server mode keeps the sandbox alive and exposes a persistent HTTP
        service.  Tools that don't have a server binary (e.g. ShellStrategy)
        should return False so the UI can disable the option and the router
        can reject such requests before reaching build_main_cmd().

        Defaults to True; override to False for tools that only support prompt
        or TUI mode.
        """
        return True

    def resolve_build_model(self, model: str) -> str:
        """Return the concrete BUILD-role model ID for *model*.

        If *model* is a preset name, resolves it via resolve_preset() and
        returns its "build" model ID. Otherwise returns *model* unchanged
        (it is assumed to already be a concrete model ID).
        """
        preset = self.resolve_preset(model)
        return preset["build"] if preset else model

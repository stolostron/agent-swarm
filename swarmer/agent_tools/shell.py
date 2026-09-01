"""Shell agent tool strategy.

Runs a raw shell command in the sandbox without an AI agent.  The session's
``instruction_prompt`` is treated literally as the command to execute — not as
a natural-language task for an AI.

Use cases:
  - Scheduled scripts (e.g. CVE report, nightly build, data export)
  - Cron-driven automation that doesn't need AI reasoning
  - Any deterministic workload where token cost is undesirable

No AI model, no provider credentials, and no agent config file are needed.
Only prompt mode is meaningful (the command runs and exits); TUI mode is
supported for interactive shell sessions.  Server mode is not supported.

Container image is configured via the ``AGENT_IMAGE_SHELL`` environment
variable (or ``agent_image_shell`` in the ``.env`` file).  A minimal UBI
image with Python 3, bash, and git is recommended — the same image used for
the OpenCode sandbox also works and is the default fallback.
"""

from typing import TYPE_CHECKING

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings

if TYPE_CHECKING:
    from swarmer.models.mcp_server import McpServer
    from swarmer.models.opencode_secret import OpencodeSecret
    from swarmer.models.session import Session


class ShellStrategy(AgentToolStrategy):
    """Run a raw shell command in the sandbox with no AI agent."""

    @property
    def name(self) -> str:
        """Return the canonical tool identifier used in DB and config."""
        return "shell"

    @property
    def display_name(self) -> str:
        """Return the human-readable tool name shown in the UI."""
        return "Shell"

    def get_image(self) -> str:
        """Return the container image to use for shell sessions.

        Reads ``AGENT_IMAGE_SHELL`` from config and falls back to the OpenCode
        image if unset — that image already contains Python 3, bash, git, and
        common CLI tools so no separate image is required to get started.
        """
        return settings.agent_image_shell or settings.agent_image_opencode

    def build_config_data(
        self,
        secret: "OpencodeSecret | None" = None,
        mcp_servers: "list[McpServer] | None" = None,
        use_inference_local: bool = False,
        model: str = "",
    ) -> dict[str, str]:
        """Return an empty config dict — shell tool needs no agent config files.

        The shell tool does not use ``opencode.json`` or any other agent
        configuration, so no files need to be written to the sandbox.
        """
        return {}

    def get_container_name(self) -> str:
        """Return the logical container name for the shell sandbox."""
        return "shell"

    def get_tui_binary(self) -> str:
        """Return the binary launched by the TUI WebSocket handler.

        The inherited default would return ``self.name`` ("shell"), which is
        not an installed binary in the sandbox image.  Override to return
        ``bash`` — an installed interactive shell.  The TUI handler tries
        ``bash --continue`` first (which fails; bash has no such flag) and
        falls back to a plain ``exec bash``, giving the user an interactive
        shell prompt.
        """
        return "bash"

    def get_server_port(self) -> int | None:
        """Return ``None`` — shell tool has no persistent server port."""
        return None

    def get_share_dir(self) -> str:
        """Return the shared workspace directory path inside the sandbox."""
        return "/workspace"

    def build_share_setup_cmd(self) -> str:
        """Return an empty string — no agent-specific share setup is needed."""
        return ""

    def build_model_setup_cmd(self, model: str) -> str:
        """Return an empty string — shell tool does not use an AI model."""
        return ""

    def build_main_cmd(self, session: "Session", model: str, resolved_prompt: str = "") -> str:
        """Return the raw command to execute.

        In prompt mode the command is the session's ``instruction_prompt``
        (or ``resolved_prompt`` if the caller has already resolved it from the
        prompt library).  The command is treated as a shell one-liner and run
        directly inside the sandbox via ``sh -c``.

        In TUI mode the sandbox stays alive (``sleep infinity``) so the user
        can connect interactively and run commands manually.

        Server mode is not supported for the shell tool.

        Security note — no input sanitisation:
            The returned command is injected verbatim into ``sh -c`` by the
            caller.  Shell metacharacters (``; && | $() >`` etc.) are
            intentional — the shell tool's purpose is to run arbitrary commands.
            The sandbox container is the trust boundary, not the command string.
            Only authenticated users with explicit workspace access can set
            ``instruction_prompt`` (enforced by ``require_auth`` + workspace
            ownership checks in the session router).  See the equivalent note in
            ``swarmer/routers/sessions.py`` at the ``sh -c`` call site.
        """
        if session.mode == "tui":
            return "sleep infinity"

        if session.mode == "server":
            raise ValueError(
                "Shell agent tool does not support server mode. "
                "Use prompt mode for scripts or tui mode for interactive sessions."
            )

        # prompt mode: run the instruction as a shell command
        cmd = (resolved_prompt or session.instruction_prompt or "").strip()
        if not cmd:
            raise ValueError(
                "Shell agent tool requires a non-empty instruction_prompt "
                "(the command to run)."
            )
        return cmd

    def requires_ai_model(self) -> bool:
        """Return False — shell tool needs no AI model or provider credentials."""
        return False

    def supports_server_mode(self) -> bool:
        """Return False — shell tool does not support server mode.

        Server mode keeps the sandbox alive indefinitely and exposes a
        persistent HTTP endpoint via the chat proxy.  The shell tool runs a
        one-shot command with no AI agent to mediate incoming requests, so
        there is nothing to serve and no safe way to gate inbound traffic.
        Returning False causes the router to reject server-mode shell sessions
        before reaching ``build_main_cmd`` and disables the option in the UI.
        """
        return False

    def is_valid_model(self, model: str) -> bool:
        """Return ``True`` unconditionally — shell tool requires no AI model.

        Any value (including empty string) is accepted; the model field is
        ignored at runtime.
        """
        return True

    def get_model_options(
        self,
        secret: "OpencodeSecret | None" = None,
        has_vertex: bool = False,
        has_gemini: bool = False,
        has_openai: bool = False,
    ) -> list[dict]:
        """Return an empty list — shell tool needs no AI provider.

        An empty list signals to the UI that the model/provider selector
        should be hidden when the shell tool is selected.
        """
        return []

    def get_default_model(self, has_adc: bool) -> str:
        """Return an empty string — shell tool uses no AI model."""
        return ""

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
        return "shell"

    @property
    def display_name(self) -> str:
        return "Shell"

    def get_image(self) -> str:
        # Fall back to the OpenCode image if no dedicated shell image is set —
        # it already contains Python 3, bash, git, and common CLI tools.
        return settings.agent_image_shell or settings.agent_image_opencode

    def build_config_data(
        self,
        secret: "OpencodeSecret | None" = None,
        mcp_servers: "list[McpServer] | None" = None,
        use_inference_local: bool = False,
        model: str = "",
    ) -> dict[str, str]:
        # No config files needed — the shell tool doesn't use opencode.json
        # or any other agent configuration.
        return {}

    def get_container_name(self) -> str:
        return "shell"

    def get_tui_binary(self) -> str:
        # The inherited default returns self.name ("shell"), which is not an
        # installed binary in the sandbox image. Use bash — an installed
        # interactive shell. `tui_ws.py` tries `bash --continue` first (which
        # fails, as bash has no such flag) and falls back to a plain
        # `exec bash`, giving the user an interactive shell.
        return "bash"

    def get_server_port(self) -> int | None:
        # Shell tool has no persistent server port.
        return None

    def get_share_dir(self) -> str:
        return "/workspace"

    def build_share_setup_cmd(self) -> str:
        # No agent-specific share directory setup needed.
        return ""

    def build_model_setup_cmd(self, model: str) -> str:
        # No model initialisation — shell tool doesn't use an AI model.
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

    def is_valid_model(self, model: str) -> bool:
        # No model required — any value (including empty string) is valid.
        return True

    def get_model_options(
        self,
        secret: "OpencodeSecret | None" = None,
        has_vertex: bool = False,
        has_gemini: bool = False,
    ) -> list[dict]:
        # Shell tool needs no AI provider — return empty list so the UI
        # hides the model/provider selector for this tool.
        return []

    def get_default_model(self, has_adc: bool) -> str:
        # No model needed.
        return ""

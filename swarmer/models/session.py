from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base

# Valid phase values
PHASES = ("idle", "queued", "pending", "running", "succeeded", "failed", "stopped")

AGENT_TOOLS = ("opencode", "crush")

CRON_PRESETS: dict[str, str] = {
    "*/30 * * * *": "Every 30 min",
    "0 * * * *": "Every hour",
    "0 */6 * * *": "Every 6 hours",
    "0 */12 * * *": "Every 12 hours",
    "0 0 * * *": "Daily midnight",
    "0 9 * * 1-5": "Weekdays 9am",
}

# Valid mode values
#   tui    — pod keeps alive (sleep infinity); browser connects via xterm.js and the K8s exec API
#   server — pod runs opencode serve --hostname 0.0.0.0
#   prompt — pod runs opencode run "<prompt>" once and exits
MODES = ("tui", "server", "prompt")


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("workspaces.id"), nullable=False
    )
    github_pat_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("github_pats.id"), nullable=True
    )
    prompt_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workspace_prompts.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="prompt", server_default="prompt"
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    language: Mapped[str] = mapped_column(String(32), nullable=False, default="golang", server_default="golang")
    persist: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    privileged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    agent_tool: Mapped[str] = mapped_column(String(32), nullable=False, default="opencode", server_default="opencode")
    instruction_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    working_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    patch_output: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    commit_msg: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    patch_base_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    cron_schedule: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    cron_next_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mcp_server_ids: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Comma-separated list of K8s Secret names created for this session (for cleanup)
    k8s_secret_names: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Runtime state — managed by dashboard
    pod_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pvc_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status_detail: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    workspace: Mapped["Workspace"] = relationship(  # noqa: F821
        back_populates="sessions"
    )
    github_pat: Mapped["GitHubPAT | None"] = relationship(  # noqa: F821
        back_populates="sessions"
    )
    repos: Mapped[list["SessionRepo"]] = relationship(  # noqa: F821
        back_populates="session", cascade="all, delete-orphan"
    )
    runs: Mapped[list["SessionRun"]] = relationship(  # noqa: F821
        back_populates="session",
        cascade="all, delete-orphan",
    )
    prompt: Mapped["WorkspacePrompt | None"] = relationship(  # noqa: F821
        back_populates="sessions"
    )

    @property
    def run_duration(self) -> str | None:
        if not self.run_started_at:
            return None
        if self.run_completed_at:
            end = self.run_completed_at
        elif self.is_active:
            end = datetime.now(timezone.utc)
        else:
            return None
        total_secs = int((end - self.run_started_at).total_seconds())
        mins, secs = divmod(max(total_secs, 0), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}h {mins}m {secs}s"
        if mins:
            return f"{mins}m {secs}s"
        return f"{secs}s"

    @property
    def interactive_mode(self) -> bool:
        """True for modes that keep the pod running persistently."""
        return self.mode in ("tui", "server")

    @property
    def is_active(self) -> bool:
        return self.phase in ("queued", "pending", "running")

    @property
    def cron_label(self) -> str:
        """Human-readable label for common cron expressions."""
        return CRON_PRESETS.get(self.cron_schedule, self.cron_schedule) if self.cron_schedule else ""

    @property
    def phase_badge_class(self) -> str:
        return {
            "idle": "secondary",
            "queued": "info",
            "pending": "warning",
            "running": "success",
            "succeeded": "primary",
            "failed": "danger",
            "stopped": "secondary",
        }.get(self.phase, "secondary")

    @property
    def enabled_mcp_ids(self) -> list[int]:
        """Parse the comma-separated MCP server ID list."""
        if not self.mcp_server_ids:
            return []
        return [int(x) for x in self.mcp_server_ids.split(",") if x.strip().isdigit()]

    @enabled_mcp_ids.setter
    def enabled_mcp_ids(self, ids: list[int]) -> None:
        self.mcp_server_ids = ",".join(str(i) for i in ids)

from datetime import datetime, timezone

from sqlalchemy import (
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

AGENT_TOOLS = ("opencode",)

CRON_PRESETS: dict[str, str] = {
    "*/30 * * * *": "Every 30 min",
    "0 * * * *": "Every hour",
    "0 */6 * * *": "Every 6 hours",
    "0 */12 * * *": "Every 12 hours",
    "0 0 * * *": "Daily midnight",
    "0 9 * * 1-5": "Weekdays 9am",
}

# Valid mode values
#   tui    — sandbox keeps alive (sleep infinity); browser connects via xterm.js and OpenShell exec
#   server — sandbox runs opencode serve --hostname 0.0.0.0
#   prompt — sandbox runs, exits on completion; sandbox deleted on success
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
    agent_tool: Mapped[str] = mapped_column(String(32), nullable=False, default="opencode", server_default="opencode")
    instruction_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    working_branch: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    patch_output: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    commit_msg: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    patch_base_ref: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    cron_schedule: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    cron_next_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Which SessionSchedule triggered the current run; cleared on stop/completion.
    active_schedule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    mcp_server_ids: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Runtime state — managed by dashboard
    sandbox_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_output: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status_detail: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    # OpenShell draft policy chunks — JSON snapshot from last run (cleared on next launch)
    policy_chunks: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # Session-level custom network rules approved from chunks — JSON array, cumulative
    custom_policies: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
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
    schedules: Mapped[list["SessionSchedule"]] = relationship(  # noqa: F821
        back_populates="session", cascade="all, delete-orphan", lazy="selectin"
    )

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

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
        start = self._as_utc(self.run_started_at)
        end = self._as_utc(end)
        total_secs = int((end - start).total_seconds())
        mins, secs = divmod(max(total_secs, 0), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}h {mins}m {secs}s"
        if mins:
            return f"{mins}m {secs}s"
        return f"{secs}s"

    @property
    def interactive_mode(self) -> bool:
        """True for modes that keep the sandbox running."""
        return self.mode in ("tui", "server")

    @property
    def is_active(self) -> bool:
        return self.phase in ("queued", "pending", "running")

    @property
    def cron_label(self) -> str:
        """Human-readable label for common cron expressions."""
        return CRON_PRESETS.get(self.cron_schedule, self.cron_schedule) if self.cron_schedule else ""

    @property
    def earliest_next_run(self) -> "datetime | None":
        """Earliest cron_next_run across all enabled schedules, or None."""
        enabled = [s for s in (self.schedules or []) if s.enabled and s.cron_next_run is not None]
        if not enabled:
            return None
        return min(s.cron_next_run for s in enabled)

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

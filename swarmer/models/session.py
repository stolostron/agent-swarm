import json
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

AGENT_TOOLS = ("opencode", "shell")

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
    # Stores the selected AI provider ("claude"/"gemini" family preset — the
    # only UX since ACM-37232 removed individual model selection). Named
    # "provider" (not "model") because it identifies which backing AI
    # provider/credential (Vertex AI vs. Google AI Studio) the session uses,
    # not a specific model ID — those are resolved at launch time via
    # AgentToolStrategy.resolve_build_model().
    provider: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
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
    # Vestigial (ACM-39804): was a per-session UI control for the sandbox pod's
    # ephemeral-storage compute resource (ACM-38184), but that resource only bounds
    # the container writable layer / unsized emptyDirs — not the `/sandbox` working
    # directory, which is a gateway-wide PVC (`workspaceDefaultStorageSize`). The
    # dropdown was removed as misleading; ephemeral-storage is now hardcoded to 10Gi
    # in openshell_client.create_sandbox(). Column/migration kept to avoid a
    # destructive schema change; no longer read anywhere.
    ephemeral_disk: Mapped[str] = mapped_column(
        String(32), nullable=False, default="2Gi", server_default="2Gi"
    )
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
    def active_schedule(self) -> "SessionSchedule | None":  # noqa: F821
        """The SessionSchedule currently driving this run, if any.

        `active_schedule_id` is set by the scheduler at launch time and
        cleared on completion/stop. `schedules` is eager-loaded
        (lazy="selectin"), so this never triggers a lazy DB call.
        """
        if not self.active_schedule_id:
            return None
        for sched in self.schedules or []:
            if sched.id == self.active_schedule_id:
                return sched
        return None

    @property
    def has_pending_chunks(self) -> bool:
        """True if session.policy_chunks has at least one draft chunk not
        yet promoted into custom_policies (i.e. needs review on the Net
        Rules tab).

        Mirrors the "is_pending" matching logic in
        sessions/_policy_chunks.html / session_policy_chunks(): a chunk is
        pending unless its rule_name exists in custom_policies AND every
        one of its binary paths is already covered by that rule.
        """
        if not self.policy_chunks:
            return False
        try:
            chunks = json.loads(self.policy_chunks)
        except (ValueError, TypeError):
            return False
        if not chunks:
            return False

        promoted_binaries: dict[str, set[str]] = {}
        if self.custom_policies:
            try:
                for rule in json.loads(self.custom_policies):
                    if not isinstance(rule, dict):
                        continue
                    name = rule.get("name")
                    if name:
                        promoted_binaries[name] = {
                            b.get("path", "") for b in (rule.get("binaries") or []) if isinstance(b, dict)
                        }
            except (ValueError, TypeError):
                pass

        try:
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("status") != "pending":
                    continue
                chunk_bins = {b.get("path", "") for b in (chunk.get("binaries") or []) if isinstance(b, dict)}
                rule_bins = promoted_binaries.get(chunk.get("rule_name"))
                if rule_bins is None or not chunk_bins.issubset(rule_bins):
                    return True
            return False
        except (ValueError, TypeError, AttributeError):
            return False

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

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base
from swarmer.models.session import CRON_PRESETS

TRIGGER_TYPES = ("cron", "event")

EVENT_CONDITIONS: dict[str, str] = {
    "ci_fail_or_conflict": "CI Failure or Merge Conflict",
    "new_pr_or_commit": "New PR or New Commits",
    "review_comments": "Review Comments (CodeRabbit / Human)",
    "any_actionable": "Any Actionable PR State",
}

AUTHOR_SCOPES: dict[str, str] = {
    "self": "My PRs (fix_authors)",
    "team": "Team PRs (Collaborators)",
    "bots": "Bot PRs (CVE / Renovate)",
    "all": "All PRs",
}


class SessionSchedule(Base):
    __tablename__ = "session_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    prompt_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workspace_prompts.id", ondelete="SET NULL"), nullable=True
    )
    trigger_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="cron", server_default="cron"
    )
    event_condition: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    author_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="all", server_default="all"
    )
    fix_authors: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    cron_schedule: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    cron_next_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    instruction_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    include_event_context: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    session: Mapped["Session"] = relationship(  # noqa: F821
        back_populates="schedules"
    )
    # eager (selectin) — sessions/_schedule_items.html accesses sched.prompt
    # synchronously during Jinja rendering; a lazy default would raise
    # MissingGreenlet outside the async context (only triggers for sessions
    # that have at least one schedule, which is why this surfaced late).
    prompt: Mapped["WorkspacePrompt | None"] = relationship(lazy="selectin")  # noqa: F821

    @property
    def cron_label(self) -> str:
        """Human-readable label for common cron expressions."""
        return CRON_PRESETS.get(self.cron_schedule, self.cron_schedule) if self.cron_schedule else ""

    @property
    def fix_author_logins(self) -> set[str]:
        """Set of lowercase GitHub logins matching the 'self' author scope."""
        return {a.strip().lower() for a in self.fix_authors.split(",") if a.strip()}

    @property
    def trigger_label(self) -> str:
        """Human-readable label for the trigger (event or cron)."""
        if self.trigger_type == "event":
            cond = EVENT_CONDITIONS.get(self.event_condition, self.event_condition or "Any Event")
            scope = AUTHOR_SCOPES.get(self.author_scope, self.author_scope)
            return f"⚡ {cond} · {scope}"
        return self.cron_label

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base


class SessionSchedule(Base):
    __tablename__ = "session_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    prompt_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("workspace_prompts.id", ondelete="SET NULL"), nullable=True
    )
    cron_schedule: Mapped[str] = mapped_column(String(128), nullable=False)
    cron_next_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="", server_default="")
    instruction_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
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

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from swarmer.database import Base


class SessionRun(Base):
    """Historical record of one completed session execution."""

    __tablename__ = "session_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status_detail: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_output: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    raw_output: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    session: Mapped["Session"] = relationship(  # noqa: F821
        back_populates="runs"
    )

    @property
    def run_duration(self) -> str:
        total_secs = int((self.completed_at - self.started_at).total_seconds())
        mins, secs = divmod(max(total_secs, 0), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}h {mins}m {secs}s"
        if mins:
            return f"{mins}m {secs}s"
        return f"{secs}s"

    @property
    def phase_badge_class(self) -> str:
        return {
            "succeeded": "blue",
            "failed": "red",
            "stopped": "grey",
        }.get(self.phase, "secondary")

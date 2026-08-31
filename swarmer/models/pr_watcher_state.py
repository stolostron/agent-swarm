from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from swarmer.database import Base

PR_ACTION_STATUSES = ("queued", "dispatched", "completed", "failed", "blocked")


class PRActionState(Base):
    """Per-(repo, PR, head_sha, action) dispatch + circuit-breaker record.

    Stored in swarmer.db and written only via AsyncSession so SQLite single-writer
    invariants are preserved.
    """

    __tablename__ = "pr_action_state"
    __table_args__ = (
        UniqueConstraint(
            "repo", "pr_number", "head_sha", "action",
            name="uq_pr_action_state_key",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # "pr-fix" | "pr-review"
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="dispatched", server_default="dispatched"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    event_context: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RepoETag(Base):
    """ETag cache for GitHub Events API conditional requests."""

    __tablename__ = "repo_etags"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    etag: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

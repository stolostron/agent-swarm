"""Helpers for persisting completed session execution history."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.config import settings
from swarmer.models.session import Session
from swarmer.models.session_run import SessionRun

log = logging.getLogger(__name__)

_TERMINAL_PHASES = frozenset(("succeeded", "failed", "stopped"))
STOPPED_BY_USER_DETAIL = "Stopped by user"


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def record_session_run(
    db: AsyncSession,
    session: Session,
    *,
    phase: str,
    status_detail: str,
    last_output: str,
    raw_output: str = "",
    completed_at: datetime,
) -> SessionRun | None:
    """Append a historical run record for a completed session execution."""
    if phase not in _TERMINAL_PHASES:
        return None
    if not session.run_started_at:
        log.warning(
            "record_session_run: session %d has no run_started_at, skipping",
            session.id,
        )
        return None

    run = SessionRun(
        session_id=session.id,
        phase=phase,
        status_detail=status_detail or "",
        started_at=_as_utc(session.run_started_at),
        completed_at=_as_utc(completed_at),
        last_output=last_output or "",
        raw_output=raw_output or "",
    )
    db.add(run)
    await _prune_old_runs(
        db,
        session.id,
        settings.session_run_history_limit,
        settings.session_run_history_max_age_days,
    )
    log.info("record_session_run: session %d run recorded (phase=%s)", session.id, phase)
    return run


async def _prune_old_runs(
    db: AsyncSession, session_id: int, limit: int, max_age_days: int = 0
) -> None:
    """Drop run records that exceed the retention count or max age.

    Both mechanisms are applied independently — whichever prunes more
    aggressively for a given session wins. Either can be disabled by
    passing 0.
    """
    await _prune_by_count(db, session_id, limit)
    await _prune_by_age(db, session_id, max_age_days)


async def _prune_by_count(db: AsyncSession, session_id: int, limit: int) -> None:
    """Drop oldest run records when a session exceeds the retention limit."""
    if limit <= 0:
        return
    # ORDER BY completed_at DESC + OFFSET limit selects IDs of runs older than the
    # newest `limit` records (everything after the retained window) for deletion.
    result = await db.execute(
        select(SessionRun.id)
        .where(SessionRun.session_id == session_id)
        .order_by(SessionRun.completed_at.desc())
        .offset(limit)
    )
    old_ids = list(result.scalars().all())
    if not old_ids:
        return
    await db.execute(delete(SessionRun).where(SessionRun.id.in_(old_ids)))
    log.info(
        "record_session_run: pruned %d old run(s) (limit=%d)",
        len(old_ids),
        limit,
    )


async def _prune_by_age(db: AsyncSession, session_id: int, max_age_days: int) -> None:
    """Drop run records older than max_age_days."""
    if max_age_days <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    result = await db.execute(
        select(SessionRun.id)
        .where(SessionRun.session_id == session_id)
        .where(SessionRun.completed_at < cutoff)
    )
    old_ids = list(result.scalars().all())
    if not old_ids:
        return
    await db.execute(delete(SessionRun).where(SessionRun.id.in_(old_ids)))
    log.info(
        "record_session_run: pruned %d run(s) older than %d day(s)",
        len(old_ids),
        max_age_days,
    )

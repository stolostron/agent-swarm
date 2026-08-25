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


async def _run_source_snapshot(db: AsyncSession, session: Session) -> tuple[str, str, str, str]:
    """Resolve the (schedule_label, prompt_name, trigger_type, event_context) snapshot for a session.

    Prefers the active schedule's label/prompt (the run was triggered by a
    schedule); falls back to the session's own configured prompt and event_context.
    """
    import json as _json
    from swarmer.models.workspace_prompt import WorkspacePrompt

    schedule_label = ""
    prompt_name = ""
    trigger_type = "manual"
    event_context = ""
    raw_event_context = session.event_context or ""

    active_schedule = session.active_schedule
    if active_schedule:
        trigger_type = active_schedule.trigger_type or "cron"
        schedule_label = active_schedule.label or active_schedule.trigger_label
        if active_schedule.prompt:
            prompt_name = active_schedule.prompt.display_name
    elif raw_event_context:
        trigger_type = "event"

    # Only retain event_context when this run was actually event-triggered —
    # session.event_context is set once by the PR watcher and otherwise never
    # cleared, so a later cron/manual/queued run on the same session must not
    # inherit a prior run's stale PR metadata (ACM-42674 follow-up).
    if trigger_type == "event" and raw_event_context:
        event_context = raw_event_context
        try:
            ctx = _json.loads(event_context)
            pr_num = ctx.get("pr_number")
            action = ctx.get("action")
            if pr_num and action:
                schedule_label = f"PR #{pr_num} ({action})"
            elif pr_num:
                schedule_label = f"PR #{pr_num}"
            elif action:
                schedule_label = f"{action}"
        except Exception:
            schedule_label = "GitHub Event"

    if not prompt_name and session.prompt_id:
        prompt = await db.get(WorkspacePrompt, session.prompt_id)
        if prompt:
            prompt_name = prompt.display_name
    return schedule_label, prompt_name, trigger_type, event_context


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

    schedule_label, prompt_name, trigger_type, event_context = await _run_source_snapshot(db, session)

    run = SessionRun(
        session_id=session.id,
        phase=phase,
        status_detail=status_detail or "",
        started_at=_as_utc(session.run_started_at),
        completed_at=_as_utc(completed_at),
        last_output=last_output or "",
        raw_output=raw_output or "",
        schedule_label=schedule_label,
        prompt_name=prompt_name,
        mode=session.mode or "prompt",
        trigger_type=trigger_type,
        event_context=event_context,
    )
    db.add(run)
    # Reconcile in-flight PR watcher dispatches for this session
    try:
        from swarmer.pr_watcher_store import reconcile_completed
        await reconcile_completed(db, session.id, phase)
    except Exception:
        log.warning("record_session_run: failed to reconcile pr_action_state for session %d", session.id, exc_info=True)

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

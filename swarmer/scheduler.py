import asyncio
import logging
from datetime import datetime, timedelta

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

log = logging.getLogger(__name__)

_POLL_INTERVAL = 30.0
_scheduler_task: asyncio.Task | None = None
_queue_next_check: datetime | None = None


def start_scheduler() -> None:
    global _scheduler_task
    stop_scheduler()
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(),
        name="cron-scheduler",
    )


def stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = None


async def shutdown() -> None:
    task = _scheduler_task
    stop_scheduler()
    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _scheduler_loop() -> None:
    log.warning("scheduler: started, polling every %ds", int(_POLL_INTERVAL))
    try:
        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                await _check_and_launch()
            except Exception:
                log.exception("scheduler: error in check_and_launch cycle")
    except asyncio.CancelledError:
        raise


async def _check_and_launch(db=None) -> None:
    """Run one cron + queue processing cycle.

    Accepts an optional ``db`` session for unit-testing; when omitted, acquires
    one from the application's session factory.
    """
    from swarmer.database import get_db
    from swarmer.models.session import Session

    now = datetime.utcnow()

    async def _run(db) -> None:
        # Process the queue first (launch queued sessions if capacity is available)
        await _process_queue(db)

        from swarmer.config import settings
        from swarmer.routers.sessions import _count_running_sessions

        # Check capacity before claiming cron sessions
        if settings.max_concurrent_agents > 0:
            running = await _count_running_sessions(db)
            available = settings.max_concurrent_agents - running
            if available <= 0:
                log.info(
                    "scheduler: at capacity (%d/%d), skipping cron claims",
                    running, settings.max_concurrent_agents,
                )
                return
        else:
            available = None  # unlimited

        # Build the subquery for sessions to claim (respect capacity if limited)
        due_sub = (
            select(Session.id)
            .where(
                Session.cron_schedule != "",
                Session.cron_next_run <= now,
                Session.mode == "prompt",
                Session.phase.notin_(["pending", "running", "queued"]),
            )
            .order_by(Session.cron_next_run)
        )
        if available is not None:
            due_sub = due_sub.limit(available)

        # Atomically claim due sessions by setting phase='pending'.
        claim_result = await db.execute(
            update(Session)
            .where(Session.id.in_(due_sub.scalar_subquery()))
            .values(phase="pending")
            .returning(Session.id)
        )
        claimed_ids = [row[0] for row in claim_result.fetchall()]
        if not claimed_ids:
            return
        await db.commit()

        # Load the claimed sessions with relationships for processing.
        result = await db.execute(
            select(Session)
            .where(Session.id.in_(claimed_ids))
            .options(
                selectinload(Session.workspace),
                selectinload(Session.github_pat),
                selectinload(Session.repos),
            )
        )
        due_sessions = result.scalars().all()

        for session in due_sessions:
            ws = session.workspace
            if ws is None:
                continue

            log.warning(
                "scheduler: launching session %d (%s), was due at %s",
                session.id, session.name, session.cron_next_run,
            )
            try:
                from swarmer.routers.sessions import _do_launch, _session_launch_user_id
                await _do_launch(session, ws, db, user_id=_session_launch_user_id(session))

                session.cron_next_run = croniter(
                    session.cron_schedule, datetime.utcnow()
                ).get_next(datetime)
                await db.commit()

                log.warning(
                    "scheduler: session %d launched (phase=%s), next run at %s",
                    session.id, session.phase, session.cron_next_run,
                )
            except Exception:
                log.exception("scheduler: failed to launch session %d", session.id)
                await db.rollback()
                session.phase = "idle"
                session.cron_next_run = croniter(
                    session.cron_schedule, datetime.utcnow()
                ).get_next(datetime)
                await db.commit()

    if db is not None:
        await _run(db)
    else:
        async for _db in get_db():
            await _run(_db)
            break


async def _process_queue(db) -> None:
    """Launch queued sessions FIFO when capacity is available."""
    global _queue_next_check
    now = datetime.utcnow()

    if _queue_next_check and now < _queue_next_check:
        return
    _queue_next_check = None

    from swarmer.config import settings
    if settings.max_concurrent_agents <= 0:
        return  # unlimited — nothing should be queued

    from swarmer.models.session import Session
    from swarmer.routers.sessions import _count_running_sessions, _do_launch, _session_launch_user_id

    running = await _count_running_sessions(db)
    available = settings.max_concurrent_agents - running
    if available <= 0:
        _queue_next_check = now + timedelta(minutes=2)
        log.info(
            "queue: still at capacity (%d/%d), next check in 2m",
            running, settings.max_concurrent_agents,
        )
        return

    result = await db.execute(
        select(Session)
        .where(Session.phase == "queued")
        .order_by(Session.created_at)
        .limit(available)
        .options(
            selectinload(Session.workspace),
            selectinload(Session.github_pat),
            selectinload(Session.repos),
        )
    )
    queued_sessions = result.scalars().all()
    if not queued_sessions:
        return

    log.info("queue: %d queued session(s), %d slot(s) available", len(queued_sessions), available)

    for session in queued_sessions:
        ws = session.workspace
        if not ws:
            continue
        session.phase = "idle"  # reset so _do_launch gate sees accurate count
        try:
            await _do_launch(session, ws, db, user_id=_session_launch_user_id(session))
            log.info("queue: launched session %d (%s), phase=%s", session.id, session.name, session.phase)
        except Exception:
            log.exception("queue: failed to launch session %d", session.id)
            session.phase = "idle"
            session.status_detail = ""
            await db.commit()

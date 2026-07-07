import asyncio
import logging
from datetime import datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

log = logging.getLogger(__name__)

_POLL_INTERVAL = 30.0
_scheduler_task: asyncio.Task | None = None
_gc_task: asyncio.Task | None = None
_queue_next_check: datetime | None = None


def start_scheduler() -> None:
    global _scheduler_task, _gc_task
    stop_scheduler()
    _scheduler_task = asyncio.create_task(
        _scheduler_loop(),
        name="cron-scheduler",
    )
    from swarmer.config import settings
    if settings.openshell_gateway_url and settings.sandbox_gc_interval != 0:
        _gc_task = asyncio.create_task(
            _sandbox_gc_loop(),
            name="sandbox-gc",
        )


def stop_scheduler() -> None:
    global _scheduler_task, _gc_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = None
    if _gc_task and not _gc_task.done():
        _gc_task.cancel()
    _gc_task = None


async def shutdown() -> None:
    gc = _gc_task
    task = _scheduler_task
    stop_scheduler()
    for t in (task, gc):
        if t:
            try:
                await t
            except asyncio.CancelledError:
                pass


async def _sandbox_gc_loop() -> None:
    from swarmer.config import settings
    from swarmer.database import get_db

    interval = max(60, settings.sandbox_gc_interval)  # floor at 60s; 0 is handled by start_scheduler (not started)
    log.info("sandbox-gc: started, interval=%ds", interval)
    try:
        while True:
            # Run immediately on first iteration, then sleep between subsequent runs.
            try:
                async for _db in get_db():
                    await _collect_orphaned_sandboxes(_db)
                    break
            except Exception:
                log.exception("sandbox-gc: error in collect cycle")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise


async def _collect_orphaned_sandboxes(db) -> None:
    """Delete live sandboxes that have no matching active session in the DB.

    Three categories of sandboxes are cleaned up:
    1. Orphans: no session has sandbox_name matching them at all.
    2. Zombies: the matching session is in a terminal phase (failed/succeeded/stopped)
       — the agent is done but sandbox cleanup was skipped or failed.
    3. Deleted externally: the session has sandbox_name set but the sandbox no longer
       exists in the gateway — session is moved to 'stopped'.

    A 5-minute age grace period applies to orphans and zombies to avoid deleting
    sandboxes that were just created but whose sandbox_name hasn't been committed
    to the DB yet.

    The GC is skipped entirely if any session is in 'pending' phase to avoid racing
    with sandbox setup (sandbox exists but sandbox_name not yet saved to DB).
    """
    from swarmer import openshell_client
    from swarmer.models.session import Session
    from datetime import datetime

    try:
        live_names = await openshell_client.list_sandboxes()
    except Exception:
        log.exception("sandbox-gc: failed to list sandboxes")
        return

    if not live_names:
        return

    # Skip GC entirely if any session is pending AND has a sandbox_name already set
    # (mid-setup: sandbox exists but sandbox_name not yet committed to DB — deleting
    # it would corrupt the setup). Sessions pending with sandbox_name=NULL are stale
    # and are handled by the stale-running pass below; they must not block GC.
    pending_result = await db.execute(
        select(Session.id).where(
            Session.phase == "pending",
            Session.sandbox_name.is_not(None),
        ).limit(1)
    )
    if pending_result.scalar_one_or_none() is not None:
        log.debug("sandbox-gc: skipping — pending session with sandbox in progress")
        return

    # Collect all sessions with a sandbox_name, split by whether they are active or
    # terminal. Active sessions (pending/running/queued) own their sandbox legitimately.
    # Terminal sessions (failed/succeeded/stopped) have leaked sandboxes — treat them
    # as zombies eligible for GC.
    _TERMINAL_PHASES = {"failed", "succeeded", "stopped"}
    result = await db.execute(
        select(Session.id, Session.sandbox_name, Session.phase).where(
            Session.sandbox_name.is_not(None)
        )
    )
    rows = result.all()
    active: dict[str, int] = {}   # sandbox_name → session_id (session is running)
    zombies: dict[str, int] = {}  # sandbox_name → session_id (session is terminal)
    for sid, sname, sphase in rows:
        if sphase in _TERMINAL_PHASES:
            zombies[sname] = sid
        else:
            active[sname] = sid
    known = {**active, **zombies}  # all sandbox_names tracked in DB

    # Only delete sandboxes that have been around long enough — grace period covers
    # the race window where sandbox_name isn't yet committed to DB after creation.
    import time as _time
    from openshell._proto import openshell_pb2 as _pb
    now_ms = int(_time.time() * 1000)
    _grace_ms = 5 * 60 * 1000  # 5 minutes — covers sandbox creation race window

    def _get_client_local():
        from swarmer import openshell_client as _oc
        return _oc._get_client()

    def _sandbox_age_ok(name: str) -> bool:
        try:
            client = _get_client_local()
            resp = client._stub.GetSandbox(
                _pb.GetSandboxRequest(name=name), timeout=10
            )
            created_ms = resp.sandbox.metadata.created_at_ms if resp.sandbox.metadata else 0
            age_ms = now_ms - created_ms
            return age_ms >= _grace_ms
        except Exception:
            return True  # if we can't check, assume old enough

    # --- Orphans: live sandboxes with no matching session at all ---
    orphaned = [name for name in live_names if name not in known]
    if orphaned:
        stale_orphans = [name for name in orphaned if await asyncio.to_thread(_sandbox_age_ok, name)]
        young_orphans = [name for name in orphaned if name not in stale_orphans]
        if young_orphans:
            log.debug("sandbox-gc: skipping %d young orphan(s) (< 5min): %s", len(young_orphans), young_orphans)
        if stale_orphans:
            log.warning("sandbox-gc: found %d orphaned sandbox(es): %s", len(stale_orphans), stale_orphans)
            for name in stale_orphans:
                try:
                    await openshell_client.delete_sandbox(name)
                    log.info("sandbox-gc: deleted orphaned sandbox %s", name)
                except Exception:
                    log.warning("sandbox-gc: failed to delete orphan %s", name, exc_info=True)

    # --- Zombies: live sandboxes whose session is in a terminal phase ---
    live_zombies = [name for name in live_names if name in zombies]
    if live_zombies:
        stale_zombies = [name for name in live_zombies if await asyncio.to_thread(_sandbox_age_ok, name)]
        young_zombies = [name for name in live_zombies if name not in stale_zombies]
        if young_zombies:
            log.debug("sandbox-gc: skipping %d young zombie(s) (< 5min): %s", len(young_zombies), young_zombies)
        if stale_zombies:
            log.warning("sandbox-gc: found %d zombie sandbox(es) from terminal sessions: %s", len(stale_zombies), stale_zombies)
            db_dirty = False
            for name in stale_zombies:
                session_id = zombies[name]
                try:
                    await openshell_client.delete_sandbox(name)
                    log.info("sandbox-gc: deleted zombie sandbox %s (session %d)", name, session_id)
                    session = await db.get(Session, session_id)
                    if session:
                        session.sandbox_name = None
                        db_dirty = True
                        # Clean up per-session GitHub App IAT and PAT providers.
                        from swarmer.routers.sessions import _delete_github_app_provider, _delete_pat_provider
                        await _delete_github_app_provider(session.workspace_id, session_id)
                        await _delete_pat_provider(session.workspace_id, session.github_pat_id, session_id)
                except Exception:
                    log.warning("sandbox-gc: failed to delete zombie %s", name, exc_info=True)
            if db_dirty:
                await db.commit()

    # --- Deleted externally: session has sandbox_name but sandbox no longer exists ---
    # This runs unconditionally (not gated on orphans/zombies) so reconciliation always
    # happens even when all live sandboxes are accounted for.
    deleted_externally = [name for name in known if name not in live_names]
    if deleted_externally:
        db_dirty = False
        for sandbox_name in deleted_externally:
            session_id = known[sandbox_name]
            session = await db.get(Session, session_id)
            if session and session.phase in ("pending", "running"):
                log.warning(
                    "sandbox-gc: sandbox %s deleted externally — moving session %d to stopped",
                    sandbox_name, session_id,
                )
                session.phase = "stopped"
                session.sandbox_name = None
                session.run_completed_at = datetime.now(timezone.utc)
                db_dirty = True
        if db_dirty:
            await db.commit()

    # --- Stale running/pending with no sandbox_name: sessions stuck in "running" or
    # "pending" with sandbox_name=NULL have no recoverable sandbox — move to "stopped".
    # The pending guard above only skips GC when a pending session *has* a sandbox_name
    # (genuinely mid-setup), so pending+NULL sessions reach this path safely.
    stale_result = await db.execute(
        select(Session).where(
            Session.phase.in_(("running", "pending")),
            Session.sandbox_name.is_(None),
        )
    )
    stale_sessions = stale_result.scalars().all()
    if stale_sessions:
        for s in stale_sessions:
            log.warning(
                "sandbox-gc: session %d is '%s' but has no sandbox_name — moving to stopped",
                s.id, s.phase,
            )
            s.phase = "stopped"
            s.run_completed_at = datetime.now(timezone.utc)
        await db.commit()


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

    now = datetime.now(timezone.utc)

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

        from swarmer.models.session_schedule import SessionSchedule

        # Fetch all due (sched_id, session_id) rows ordered by cron_next_run.
        # SQLite-compatible: avoid correlated aggregates in subqueries.
        due_rows_result = await db.execute(
            select(
                SessionSchedule.id.label("sched_id"),
                SessionSchedule.session_id,
                SessionSchedule.cron_next_run,
            )
            .join(Session, Session.id == SessionSchedule.session_id)
            .where(
                SessionSchedule.enabled == True,  # noqa: E712
                SessionSchedule.cron_next_run <= now,
                Session.phase.notin_(["pending", "running", "queued"]),
            )
            .order_by(SessionSchedule.cron_next_run)
        )
        all_due_rows = due_rows_result.fetchall()

        # Pick the earliest due schedule per session (first occurrence wins since
        # rows are ordered by cron_next_run ascending).
        session_to_sched: dict[int, int] = {}
        for row in all_due_rows:
            sid = row.session_id
            if sid not in session_to_sched:
                session_to_sched[sid] = row.sched_id

        # Respect capacity: trim to `available` sessions if limited.
        if available is not None and len(session_to_sched) > available:
            session_to_sched = dict(list(session_to_sched.items())[:available])

        if not session_to_sched:
            return

        # Atomically claim due sessions by setting phase='pending'.
        claim_result = await db.execute(
            update(Session)
            .where(Session.id.in_(list(session_to_sched.keys())))
            .values(phase="pending")
            .returning(Session.id)
        )
        claimed_ids = [row[0] for row in claim_result.fetchall()]
        if not claimed_ids:
            return
        await db.commit()

        # Filter session_to_sched to only actually-claimed sessions (race safety).
        session_to_sched = {sid: session_to_sched[sid] for sid in claimed_ids if sid in session_to_sched}

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

            sched_id = session_to_sched.get(session.id)
            log.warning(
                "scheduler: launching session %d (%s) via schedule %s",
                session.id, session.name, sched_id,
            )
            try:
                from swarmer.routers.sessions import _do_launch
                session.mode = "prompt"
                session.active_schedule_id = sched_id
                await db.commit()
                await _do_launch(session, ws, db)

                # Advance the fired schedule's next run time.
                if sched_id:
                    sched = await db.get(SessionSchedule, sched_id)
                    if sched:
                        sched.cron_next_run = croniter(
                            sched.cron_schedule, datetime.now(timezone.utc)
                        ).get_next(datetime)
                await db.commit()

                log.warning(
                    "scheduler: session %d launched (phase=%s)",
                    session.id, session.phase,
                )
            except Exception:
                log.exception("scheduler: failed to launch session %d", session.id)
                await db.rollback()
                session.phase = "idle"
                session.active_schedule_id = None
                # Still advance the schedule so it doesn't retry immediately.
                if sched_id:
                    sched = await db.get(SessionSchedule, sched_id)
                    if sched:
                        sched.cron_next_run = croniter(
                            sched.cron_schedule, datetime.now(timezone.utc)
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
    now = datetime.now(timezone.utc)

    if _queue_next_check and now < _queue_next_check:
        return
    _queue_next_check = None

    from swarmer.config import settings
    if settings.max_concurrent_agents <= 0:
        return  # unlimited — nothing should be queued

    from swarmer.models.session import Session
    from swarmer.routers.sessions import _count_running_sessions, _do_launch

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
            await _do_launch(session, ws, db)
            log.info("queue: launched session %d (%s), phase=%s", session.id, session.name, session.phase)
        except Exception:
            log.exception("queue: failed to launch session %d", session.id)
            session.phase = "idle"
            session.status_detail = ""
            await db.commit()

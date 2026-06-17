import asyncio
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_POLL_INTERVAL = 5.0
_TERMINAL_PHASES = frozenset(("succeeded", "failed", "stopped"))

# session_id → running asyncio.Task
_poller_tasks: dict[int, asyncio.Task] = {}


def start_log_poller(session_id: int, pod_name: str, namespace: str, mode: str = "prompt") -> None:
    stop_log_poller(session_id)
    task = asyncio.create_task(
        _poll_loop(session_id, pod_name, namespace, mode),
        name=f"log-poller-{session_id}",
    )
    _poller_tasks[session_id] = task
    task.add_done_callback(lambda t: _poller_tasks.pop(session_id, None))


def stop_log_poller(session_id: int) -> None:
    task = _poller_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()


async def shutdown() -> None:
    """Cancel all running pollers and wait for them to finish. Call from app lifespan shutdown."""
    tasks = list(_poller_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _poller_tasks.clear()


async def _poll_loop(session_id: int, pod_name: str, namespace: str, mode: str) -> None:
    from swarmer import k8s

    try:
        while True:
            phase, detail = await asyncio.to_thread(k8s.get_pod_status, pod_name, namespace)
            logs = await asyncio.to_thread(k8s.get_pod_logs, pod_name, namespace)
            await _save_to_db(session_id, phase, detail, logs)
            if phase in _TERMINAL_PHASES:
                log.info("log_poller: session %d reached terminal phase %s", session_id, phase)
                if phase == "succeeded" and mode == "prompt":
                    await _auto_cleanup_pod(session_id, pod_name, namespace)
                return
            await asyncio.sleep(_POLL_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("log_poller: unexpected error for session %d", session_id)


async def _auto_cleanup_pod(session_id: int, pod_name: str, namespace: str) -> None:
    from swarmer import k8s
    from swarmer import k8s_session as k8s_sess
    from swarmer.database import get_db
    from swarmer.models.session import Session

    deleted = False
    try:
        await asyncio.to_thread(k8s.delete_pod, pod_name, namespace)
        log.info("log_poller: auto-deleted pod %s for session %d", pod_name, session_id)
        deleted = True
    except Exception:
        log.exception("log_poller: pod auto-deletion failed for session %d", session_id)

    if not deleted:
        return

    try:
        async for db in get_db():
            session = await db.get(Session, session_id)
            if session and session.pod_name == pod_name:
                session.pod_name = None
                if not session.persist and session.pvc_name:
                    try:
                        await asyncio.to_thread(k8s_sess.delete_session_pvc, namespace, session.pvc_name)
                        log.info("log_poller: auto-deleted PVC %s for session %d", session.pvc_name, session_id)
                        session.pvc_name = None
                    except Exception:
                        log.exception("log_poller: PVC auto-deletion failed for session %d", session_id)
                # Clean up session-scoped K8s Secrets (skip for scheduled sessions)
                if not session.cron_schedule and session.k8s_secret_names:
                    try:
                        await asyncio.to_thread(k8s.cleanup_session_secrets, namespace, session)
                        log.info("log_poller: auto-deleted secrets for session %d", session_id)
                    except Exception:
                        log.exception("log_poller: secret auto-deletion failed for session %d", session_id)
                await db.commit()
            break
    except Exception:
        log.exception("log_poller: pod_name clear failed for session %d", session_id)


async def _save_to_db(session_id: int, phase: str, detail: str, logs: str) -> None:
    from swarmer.database import get_db
    from swarmer.models.session import Session

    try:
        async for db in get_db():
            session = await db.get(Session, session_id)
            if session is None:
                break
            if phase in _TERMINAL_PHASES and session.phase not in _TERMINAL_PHASES:
                session.run_completed_at = datetime.now(timezone.utc)
                from swarmer.session_runs import record_session_run

                await record_session_run(
                    db,
                    session,
                    phase=phase,
                    status_detail=detail,
                    last_output=logs or session.last_output,
                    completed_at=session.run_completed_at,
                )
            session.phase = phase
            session.status_detail = detail
            if logs:
                session.last_output = logs
            await db.commit()
            break
    except Exception:
        log.exception("log_poller: DB save failed for session %d", session_id)

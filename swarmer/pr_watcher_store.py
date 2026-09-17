from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer.models.pr_watcher_state import GitHubEventReceipt, PRActionState, PRCommentDispatch, RepoETag
from swarmer.models.session import Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.session_schedule import SessionSchedule

log = logging.getLogger(__name__)

# Statuses that block re-dispatch for the same (repo, pr, head_sha, condition).
_BLOCKING_STATUSES = {"queued", "dispatched", "completed", "blocked"}
# Statuses that consume the retry budget.
_ATTEMPT_STATUSES = {"dispatched", "failed"}


def extract_repo_from_url(repo_url: str) -> str:
    """Normalize a GitHub repository URL into 'owner/repo'.

    Examples:
      - 'https://github.com/stolostron/agent-swarm.git' -> 'stolostron/agent-swarm'
      - 'git@github.com:stolostron/agent-swarm.git'     -> 'stolostron/agent-swarm'
      - 'stolostron/agent-swarm'                       -> 'stolostron/agent-swarm'
    """
    if not repo_url:
        return ""
    url = repo_url.strip()
    if "github.com" in url:
        tail = url.split("github.com", 1)[1].lstrip("/:").removesuffix(".git").strip("/")
        parts = tail.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    elif "/" in url and not url.startswith("http"):
        parts = url.removesuffix(".git").strip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return ""


async def resolve_event_triggers(
    db: AsyncSession,
    workspace_id: int | None = None,
) -> dict[int, dict[str, list[tuple[SessionSchedule, Session]]]]:
    """Resolve active event triggers partitioned by workspace and mapped by normalized repo key.

    Returns:
        dict mapping workspace_id -> { 'owner/repo': [(SessionSchedule, Session), ...] }
        Sessions with only cron schedules are never returned.
    """
    query = (
        select(SessionSchedule, Session, SessionRepo)
        .join(Session, Session.id == SessionSchedule.session_id)
        .join(SessionRepo, SessionRepo.session_id == Session.id)
        .where(
            SessionSchedule.enabled.is_(True),
            SessionSchedule.trigger_type == "event",
        )
        .options(
            selectinload(SessionSchedule.prompt),
            selectinload(Session.workspace),
            selectinload(Session.github_pat),
            selectinload(Session.repos),
        )
    )
    if workspace_id is not None:
        query = query.where(Session.workspace_id == workspace_id)

    result = await db.execute(query)
    fan_out: dict[int, dict[str, list[tuple[SessionSchedule, Session]]]] = defaultdict(lambda: defaultdict(list))
    for sched, session, repo in result.all():
        key = extract_repo_from_url(repo.repo_url)
        if key and session.workspace_id:
            fan_out[session.workspace_id][key].append((sched, session))
    return {ws_id: dict(repos) for ws_id, repos in fan_out.items()}


async def get_dispatch_state(
    db: AsyncSession,
    repo: str,
    pr_number: int,
    head_sha: str,
    condition: str = "",
    action: str = "",
    session_id: int | None = None,
) -> PRActionState | None:
    key = condition or action
    stmt = select(PRActionState).where(
        PRActionState.repo == repo,
        PRActionState.pr_number == pr_number,
        PRActionState.head_sha == head_sha,
        PRActionState.action == key,
    )
    if session_id is not None:
        stmt = stmt.where(PRActionState.session_id == session_id)
    result = await db.execute(stmt)
    return result.scalars().first()


get_action_state = get_dispatch_state


async def is_blocked(
    db: AsyncSession,
    repo: str,
    pr_number: int,
    head_sha: str,
    condition: str = "",
    action: str = "",
    session_id: int | None = None,
) -> bool:
    """Check if this condition is already in flight, completed, or blocked."""
    row = await get_dispatch_state(
        db,
        repo,
        pr_number,
        head_sha,
        condition=condition,
        action=action,
        session_id=session_id,
    )
    return bool(row and row.status in _BLOCKING_STATUSES)


async def record_dispatch(
    db: AsyncSession,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    condition: str = "",
    action: str = "",
    session_id: int | None = None,
    status: str = "dispatched",
    error: str = "",
    event_context: str = "",
) -> int:
    """Record or update a dispatch attempt. Returns the new attempt count."""
    now = datetime.now(timezone.utc)
    key = condition or action
    row = await get_dispatch_state(
        db, repo, pr_number, head_sha, condition=key, session_id=session_id
    )
    if row is None:
        row = PRActionState(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            action=key,
            session_id=session_id,
            status=status,
            attempts=1 if status in _ATTEMPT_STATUSES else 0,
            last_error=error,
            event_context=event_context,
            last_dispatched_at=now,
        )
        db.add(row)
    else:
        if status in _ATTEMPT_STATUSES:
            row.attempts += 1
        row.session_id = session_id
        row.status = status
        row.last_error = error
        row.event_context = event_context or row.event_context
        row.last_dispatched_at = now
    await db.commit()
    return row.attempts


async def list_queued_dispatches(db: AsyncSession) -> list[PRActionState]:
    """Return queued dispatch rows ordered oldest-first for FIFO replay."""
    result = await db.execute(
        select(PRActionState)
        .where(PRActionState.status == "queued")
        .order_by(PRActionState.last_dispatched_at.asc(), PRActionState.id.asc())
    )
    return list(result.scalars().all())


async def reconcile_completed(db: AsyncSession, session_id: int, phase: str) -> None:
    """Reconcile in-flight 'dispatched' states when a session reaches terminal phase."""
    from swarmer.config import settings

    result = await db.execute(
        select(PRActionState).where(
            PRActionState.session_id == session_id,
            PRActionState.status == "dispatched",
        )
    )
    for row in result.scalars().all():
        if phase == "succeeded":
            row.status = "completed"
            row.last_error = ""
        else:
            if row.attempts >= settings.pr_watcher_max_fix_attempts:
                row.status = "blocked"
                row.last_error = f"Max dispatch attempts ({row.attempts}) reached; last session ended in phase '{phase}'"
            else:
                row.status = "failed"
                row.last_error = f"session ended in phase '{phase}'"
    await db.commit()


async def get_etag(db: AsyncSession, repo: str) -> str | None:
    result = await db.execute(select(RepoETag).where(RepoETag.repo == repo))
    row = result.scalar_one_or_none()
    return row.etag if row else None


async def save_etag(db: AsyncSession, repo: str, etag: str) -> None:
    result = await db.execute(select(RepoETag).where(RepoETag.repo == repo))
    row = result.scalar_one_or_none()
    if row is None:
        db.add(RepoETag(repo=repo, etag=etag))
    else:
        row.etag = etag
    await db.commit()


async def prune_etags(db: AsyncSession, active_repos: set[str]) -> None:
    """Drop cached ETags for repos no longer in the active event-trigger poll set."""
    result = await db.execute(select(RepoETag.repo))
    for (repo,) in result.all():
        if repo not in active_repos:
            await db.execute(delete(RepoETag).where(RepoETag.repo == repo))
    await db.commit()


async def record_event_receipt(
    db: AsyncSession, *, repo: str, event_id: str, event_type: str,
    pr_number: int, event_created_at: datetime | None,
) -> bool:
    """Record a qualifying event; return False when it was already observed."""
    if not event_id:
        return False
    existing = await db.scalar(select(GitHubEventReceipt).where(GitHubEventReceipt.event_id == event_id))
    if existing:
        return False
    db.add(GitHubEventReceipt(
        repo=repo, event_id=event_id, event_type=event_type,
        pr_number=pr_number, event_created_at=event_created_at,
    ))
    await db.commit()
    return True


async def get_comment_dispatch(db: AsyncSession, repo: str, pr_number: int, schedule_id: int) -> PRCommentDispatch | None:
    return await db.scalar(select(PRCommentDispatch).where(
        PRCommentDispatch.repo == repo,
        PRCommentDispatch.pr_number == pr_number,
        PRCommentDispatch.schedule_id == schedule_id,
    ))


async def upsert_comment_dispatch(
    db: AsyncSession, *, repo: str, pr_number: int, session_id: int, schedule_id: int,
    head_sha: str, event_context: str, event_id: str, event_at: datetime,
    delay_minutes: int,
) -> PRCommentDispatch:
    row = await get_comment_dispatch(db, repo, pr_number, schedule_id)
    not_before = event_at + timedelta(minutes=delay_minutes)
    if row is None:
        row = PRCommentDispatch(
            repo=repo, pr_number=pr_number, session_id=session_id, schedule_id=schedule_id,
            status="queued", head_sha=head_sha, event_context=event_context,
            not_before=not_before, last_comment_event_id=event_id, last_comment_event_at=event_at,
        )
        db.add(row)
    else:
        row.session_id = session_id
        row.status = "queued"
        row.head_sha = head_sha
        row.event_context = event_context
        row.not_before = not_before
        row.last_comment_event_id = event_id
        row.last_comment_event_at = event_at
        row.last_error = ""
    await db.commit()
    await db.refresh(row)
    return row


async def list_due_comment_dispatches(db: AsyncSession, now: datetime) -> list[PRCommentDispatch]:
    result = await db.execute(select(PRCommentDispatch).where(
        PRCommentDispatch.status == "queued",
        PRCommentDispatch.not_before <= now,
    ).order_by(PRCommentDispatch.not_before, PRCommentDispatch.id))
    return list(result.scalars().all())


async def update_comment_dispatch(db: AsyncSession, row: PRCommentDispatch, *, status: str, error: str = "") -> None:
    row.status = status
    row.last_error = error
    if status == "dispatched":
        row.attempts += 1
    await db.commit()


async def reconcile_comment_dispatches(db: AsyncSession, session_id: int, phase: str) -> None:
    result = await db.execute(select(PRCommentDispatch).where(
        PRCommentDispatch.session_id == session_id,
        PRCommentDispatch.status == "dispatched",
    ))
    for row in result.scalars().all():
        row.status = "completed" if phase == "succeeded" else "failed"
        if phase != "succeeded":
            row.last_error = f"session ended in phase '{phase}'"
    await db.commit()

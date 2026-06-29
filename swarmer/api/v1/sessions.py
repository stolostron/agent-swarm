"""REST API — Session CRUD & lifecycle."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pydantic import BaseModel

from swarmer.agent_tools.registry import get as get_tool
from swarmer.database import get_db
from swarmer.api.deps import get_current_user, get_workspace_or_404, require_api_auth
from swarmer.api.schemas import (
    MessageOut,
    ScheduleEntryCreate,
    ScheduleEntryOut,
    ScheduleEntryUpdate,
    ScheduleRequest,
    SessionCreate,
    SessionOut,
    SessionOutput,
    SessionRunOut,
    SessionUpdate,
    SetModeRequest,
    SetModelRequest,
    SetNameRequest,
)
from swarmer.models.session import Session
from swarmer.models.session_run import SessionRun
from swarmer.models.workspace import Workspace


class PatchResult(BaseModel):
    patch: str
    commit_msg: str
    filename: str

log = logging.getLogger(__name__)

_INVALID_REF_RE = re.compile(
    r"[\x00-\x1f\x7f ~^:?*\[\\]"
    r"|\.\.+"
    r"|@\{"
    r"|\.$"
    r"|\.lock$"
    r"|//"
)


def _is_valid_ref_name(name: str) -> bool:
    if not name or name.startswith("/") or name.endswith("/") or name.endswith("."):
        return False
    return _INVALID_REF_RE.search(name) is None


router = APIRouter(
    prefix="/workspaces/{ws_id}/sessions",
    tags=["sessions"],
    dependencies=[Depends(require_api_auth)],
)


async def _get_session_or_404(
    ws_id: int, sid: int, db: AsyncSession
) -> Session:
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.github_pat), selectinload(Session.repos)],
    )
    if session is None or session.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


# ---------- CRUD ----------


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .order_by(Session.name)
    )
    return result.scalars().all()


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    ws_id: int,
    body: SessionCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    if body.mode not in ("tui", "server", "prompt"):
        raise HTTPException(status_code=422, detail="Invalid mode")

    try:
        agent_tool = get_tool(body.agent_tool).name
    except ValueError:
        agent_tool = "opencode"

    wb = body.working_branch.strip()
    if wb and not _is_valid_ref_name(wb):
        raise HTTPException(status_code=422, detail="Invalid working branch name")

    session = Session(
        workspace_id=ws_id,
        github_pat_id=body.github_pat_id,
        prompt_id=body.prompt_id,
        name=body.name.strip(),
        mode=body.mode,
        model=body.model.strip(),
        instruction_prompt=body.instruction_prompt.strip(),
        agent_tool=agent_tool,
        working_branch=wb,
    )
    if body.mcp_server_ids:
        session.enabled_mcp_ids = body.mcp_server_ids
    else:
        session.mcp_server_ids = "none"

    db.add(session)
    try:
        await db.commit()
        await db.refresh(session)
        if not session.working_branch:
            import secrets as _secrets
            session.working_branch = f"swarmer/session-{session.id}-{_secrets.token_hex(4)}"
            await db.commit()
            await db.refresh(session)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A session named '{body.name}' already exists in this workspace.",
        )

    return session


@router.get("/{sid}", response_model=SessionOut)
async def get_session(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    return await _get_session_or_404(ws_id, sid, db)


@router.put("/{sid}", response_model=SessionOut)
async def update_session(
    ws_id: int,
    sid: int,
    body: SessionUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Cannot edit a running session")

    if body.name is not None:
        session.name = body.name.strip()
    if body.mode is not None and body.mode in ("tui", "server", "prompt"):
        session.mode = body.mode
    if body.model is not None:
        session.model = body.model.strip()
    if body.agent_tool is not None:
        try:
            session.agent_tool = get_tool(body.agent_tool).name
        except ValueError:
            pass
    if body.instruction_prompt is not None:
        session.instruction_prompt = body.instruction_prompt.strip()
    if body.github_pat_id is not None:
        session.github_pat_id = body.github_pat_id
    if body.prompt_id is not None:
        session.prompt_id = body.prompt_id
    if body.working_branch is not None:
        wb = body.working_branch.strip()
        if wb and not _is_valid_ref_name(wb):
            raise HTTPException(status_code=422, detail="Invalid working branch name")
        session.working_branch = wb
    if body.mcp_server_ids is not None:
        if body.mcp_server_ids:
            session.enabled_mcp_ids = body.mcp_server_ids
        else:
            session.mcp_server_ids = "none"

    try:
        await db.commit()
        await db.refresh(session)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A session with that name already exists")

    return session


@router.delete("/{sid}", response_model=MessageOut)
async def delete_session(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Stop the session before deleting")

    if session.sandbox_name:
        # OpenShell session — delete sandbox
        from swarmer import openshell_client
        if session.service_url:
            try:
                await openshell_client.delete_service(session.sandbox_name, "agent")
            except Exception:
                pass
        try:
            await openshell_client.delete_sandbox(session.sandbox_name)
        except Exception:
            pass

    name = session.name
    await db.delete(session)
    await db.commit()
    return MessageOut(detail=f"Session '{name}' deleted.")


# ---------- Lifecycle ----------


@router.post("/{sid}/launch", response_model=SessionOut)
async def launch_session(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Session is already active")

    try:
        from swarmer.routers.sessions import _do_launch
        await _do_launch(session, ws, db, user_id=user)
    except Exception as exc:
        log.error("API session_launch failed for session %d: %s", sid, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Launch failed: {exc}")

    await db.refresh(session)
    return session


@router.post("/{sid}/stop", response_model=SessionOut)
async def stop_session(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)

    if session.phase == "queued":
        session.phase = "idle"
        session.status_detail = ""
        await db.commit()
        await db.refresh(session)
        return session

    # Cancel any background tasks for this session before touching the DB so
    # the task cannot race and overwrite the "stopped" phase we're about to set.
    _task_names = (f"openshell-setup-{sid}", f"openshell-agent-{sid}")
    for _t in asyncio.all_tasks():
        if _t.get_name() in _task_names:
            _t.cancel()
            log.info("stop_session: cancelled background task %s", _t.get_name())

    if session.sandbox_name:
        from swarmer import openshell_client
        if session.service_url:
            try:
                await openshell_client.delete_service(session.sandbox_name, "agent")
            except Exception:
                pass
        try:
            await openshell_client.delete_sandbox(session.sandbox_name)
        except Exception:
            pass
        session.sandbox_name = None
        session.service_url = None

    from swarmer.session_runs import STOPPED_BY_USER_DETAIL, record_session_run

    completed_at = datetime.now(timezone.utc)
    if session.run_started_at and session.phase in ("pending", "running"):
        await record_session_run(
            db,
            session,
            phase="stopped",
            status_detail=STOPPED_BY_USER_DETAIL,
            last_output=session.last_output,
            raw_output=session.raw_output,
            completed_at=completed_at,
        )
    session.run_completed_at = completed_at
    session.phase = "stopped"
    session.status_detail = STOPPED_BY_USER_DETAIL

    # Advance cron_next_run so the scheduler doesn't immediately re-launch a
    # cron-scheduled session that was manually stopped.
    if session.cron_schedule and session.cron_next_run is not None:
        try:
            from croniter import croniter as _croniter
            session.cron_next_run = _croniter(
                session.cron_schedule, datetime.now(timezone.utc)
            ).get_next(datetime)
        except Exception:
            log.warning("stop_session: failed to advance cron_next_run for session %d", sid, exc_info=True)

    await db.commit()
    await db.refresh(session)
    return session


# ---------- Run history ----------


@router.get("/{sid}/runs", response_model=list[SessionRunOut])
async def list_session_runs(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
) -> list[SessionRunOut]:
    await _get_session_or_404(ws_id, sid, db)
    result = await db.execute(
        select(SessionRun)
        .where(SessionRun.session_id == sid)
        .order_by(SessionRun.completed_at.desc())
        .limit(100)
    )
    return list(result.scalars().all())


# ---------- Inline edits ----------


@router.post("/{sid}/set-name", response_model=SessionOut)
async def set_name(
    ws_id: int,
    sid: int,
    body: SetNameRequest,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Cannot rename a running session")

    session.name = body.name.strip()
    try:
        await db.commit()
        await db.refresh(session)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A session with that name already exists")

    return session


@router.post("/{sid}/set-mode", response_model=SessionOut)
async def set_mode(
    ws_id: int,
    sid: int,
    body: SetModeRequest,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Cannot change mode while session is active")

    session.mode = body.mode
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{sid}/set-model", response_model=SessionOut)
async def set_model(
    ws_id: int,
    sid: int,
    body: SetModelRequest,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    new_model = body.model.strip()

    session.model = new_model
    await db.commit()
    await db.refresh(session)
    return session


# ---------- Scheduling ----------


@router.post("/{sid}/schedule", response_model=SessionOut)
async def schedule_session(
    ws_id: int,
    sid: int,
    body: ScheduleRequest,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    """Backward-compat: create or replace a single schedule entry on the session."""
    from croniter import croniter
    from swarmer.models.session_schedule import SessionSchedule

    session = await _get_session_or_404(ws_id, sid, db)
    if not croniter.is_valid(body.cron_expr):
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {body.cron_expr}")

    # Write to the deprecated session fields for backward compat (cron_schedule / cron_next_run).
    session.cron_schedule = body.cron_expr
    session.cron_next_run = croniter(body.cron_expr, datetime.now(timezone.utc)).get_next(datetime)

    # Delegate to the new model: upsert a schedule entry with an empty label.
    result = await db.execute(
        select(SessionSchedule)
        .where(SessionSchedule.session_id == sid, SessionSchedule.label == "")
        .limit(1)
    )
    sched = result.scalars().first()
    next_run = croniter(body.cron_expr, datetime.now(timezone.utc)).get_next(datetime)
    if sched is None:
        sched = SessionSchedule(
            session_id=sid,
            cron_schedule=body.cron_expr,
            cron_next_run=next_run,
        )
        db.add(sched)
    else:
        sched.cron_schedule = body.cron_expr
        sched.cron_next_run = next_run
        sched.enabled = True

    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{sid}/unschedule", response_model=SessionOut)
async def unschedule_session(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    """Backward-compat: disable all schedules on the session and clear deprecated fields."""
    from swarmer.models.session_schedule import SessionSchedule

    session = await _get_session_or_404(ws_id, sid, db)

    # Clear deprecated session-level fields.
    session.cron_schedule = ""
    session.cron_next_run = None

    result = await db.execute(
        select(SessionSchedule).where(SessionSchedule.session_id == sid)
    )
    for sched in result.scalars().all():
        sched.enabled = False

    await db.commit()
    await db.refresh(session)
    return session


# ---------- Schedule sub-resource ----------


@router.get("/{sid}/schedules", response_model=list[ScheduleEntryOut])
async def list_schedules(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    from swarmer.models.session_schedule import SessionSchedule
    await _get_session_or_404(ws_id, sid, db)
    result = await db.execute(
        select(SessionSchedule)
        .where(SessionSchedule.session_id == sid)
        .order_by(SessionSchedule.created_at)
    )
    return result.scalars().all()


@router.post("/{sid}/schedules", response_model=ScheduleEntryOut, status_code=201)
async def create_schedule(
    ws_id: int,
    sid: int,
    body: ScheduleEntryCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    from croniter import croniter
    from swarmer.models.session_schedule import SessionSchedule
    await _get_session_or_404(ws_id, sid, db)
    if not croniter.is_valid(body.cron_schedule):
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {body.cron_schedule}")
    sched = SessionSchedule(
        session_id=sid,
        cron_schedule=body.cron_schedule,
        cron_next_run=croniter(body.cron_schedule, datetime.now(timezone.utc)).get_next(datetime),
        label=body.label,
        prompt_id=body.prompt_id,
        instruction_prompt=body.instruction_prompt,
        enabled=body.enabled,
    )
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    return sched


@router.put("/{sid}/schedules/{sched_id}", response_model=ScheduleEntryOut)
async def update_schedule(
    ws_id: int,
    sid: int,
    sched_id: int,
    body: ScheduleEntryUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    from croniter import croniter
    from swarmer.models.session_schedule import SessionSchedule
    await _get_session_or_404(ws_id, sid, db)
    sched = await db.get(SessionSchedule, sched_id)
    if sched is None or sched.session_id != sid:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if body.cron_schedule is not None:
        if not croniter.is_valid(body.cron_schedule):
            raise HTTPException(status_code=422, detail=f"Invalid cron expression: {body.cron_schedule}")
        sched.cron_schedule = body.cron_schedule
        sched.cron_next_run = croniter(body.cron_schedule, datetime.now(timezone.utc)).get_next(datetime)
    if body.label is not None:
        sched.label = body.label
    if body.prompt_id is not None:
        sched.prompt_id = body.prompt_id
    if body.instruction_prompt is not None:
        sched.instruction_prompt = body.instruction_prompt
    if body.enabled is not None:
        sched.enabled = body.enabled
    await db.commit()
    await db.refresh(sched)
    return sched


@router.delete("/{sid}/schedules/{sched_id}", status_code=204)
async def delete_schedule(
    ws_id: int,
    sid: int,
    sched_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    from swarmer.models.session_schedule import SessionSchedule
    await _get_session_or_404(ws_id, sid, db)
    sched = await db.get(SessionSchedule, sched_id)
    if sched is None or sched.session_id != sid:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db.delete(sched)
    await db.commit()


# ---------- Output & Patches ----------


@router.get("/{sid}/output", response_model=SessionOutput)
async def get_output(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    return SessionOutput(output=session.last_output, raw_output=session.raw_output)


@router.post("/{sid}/clear-output", response_model=SessionOut)
async def clear_output(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    session.last_output = ""
    session.raw_output = ""
    await db.commit()
    await db.refresh(session)
    return session


@router.post("/{sid}/generate-patch", response_model=PatchResult)
async def generate_patch(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    from swarmer import openshell_client
    from swarmer.routers.sessions import _build_commit_msg, _patch_filename

    session = await _get_session_or_404(ws_id, sid, db)
    if not session.sandbox_name:
        raise HTTPException(status_code=404, detail="Session is not an OpenShell session")
    if session.phase != "running":
        raise HTTPException(status_code=409, detail="Session must be running to generate a patch")

    if session.patch_base_ref:
        cmd = ["git", "diff", f"origin/{session.patch_base_ref}"]
    else:
        cmd = ["git", "diff"]

    try:
        result = await openshell_client.exec_command(
            sandbox_name=session.sandbox_name,
            cmd=cmd,
            client=openshell_client._get_client(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Git diff failed: {exc}")

    patch = result.stdout or ""
    commit_msg = result.stderr or ""

    if not patch.strip():
        session.patch_output = ""
        session.commit_msg = ""
        await db.commit()
        return PatchResult(patch="", commit_msg="No changes detected.", filename=_patch_filename(session))

    if not commit_msg:
        commit_msg = await _build_commit_msg(patch, session.workspace_id, db)

    session.patch_output = patch
    session.commit_msg = commit_msg
    await db.commit()

    return PatchResult(
        patch=patch,
        commit_msg=commit_msg,
        filename=_patch_filename(session),
    )


@router.get("/{sid}/download-patch")
async def download_patch(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import Response
    from swarmer.routers.sessions import _patch_filename

    session = await _get_session_or_404(ws_id, sid, db)
    if not session.patch_output:
        raise HTTPException(status_code=404, detail="No patch available")

    return Response(
        content=session.patch_output,
        media_type="text/x-patch",
        headers={
            "Content-Disposition": f'attachment; filename="{_patch_filename(session)}"'
        },
    )

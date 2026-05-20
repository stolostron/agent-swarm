"""REST API — Session CRUD & lifecycle."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer import k8s, k8s_session as k8s_sess
from swarmer.agent_tools.registry import get as get_tool
from swarmer.database import get_db
from swarmer.api.deps import get_current_user, get_workspace_or_404, require_api_auth
from swarmer.api.schemas import (
    MessageOut,
    PatchResult,
    ScheduleRequest,
    SessionCreate,
    SessionOut,
    SessionOutput,
    SessionUpdate,
    SetModeRequest,
    SetModelRequest,
    SetNameRequest,
)
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

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
        persist=body.persist,
        resume=body.resume,
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
    if body.persist is not None:
        session.persist = body.persist
    if body.resume is not None:
        session.resume = body.resume
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

    if session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
        except Exception:
            pass
    try:
        k8s.cleanup_session_secrets(ws.k8s_namespace, session)
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

    if session.pod_name:
        from swarmer import log_poller
        log_poller.stop_log_poller(sid)
        try:
            k8s.delete_pod(session.pod_name, ws.k8s_namespace)
            if session.mode == "server":
                k8s.delete_service(f"session-{session.id}-svc", ws.k8s_namespace)
                k8s.delete_session_route(session.id, ws.k8s_namespace)
        except Exception:
            pass

    if not session.persist and session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
            session.pvc_name = None
        except Exception:
            pass

    if not session.cron_schedule:
        try:
            k8s.cleanup_session_secrets(ws.k8s_namespace, session)
        except Exception:
            pass

    session.run_completed_at = datetime.utcnow()
    session.phase = "stopped"
    session.pod_name = None
    await db.commit()
    await db.refresh(session)
    return session


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

    if session.is_active and session.pod_name:
        try:
            tool = get_tool(session.agent_tool)
            tool.exec_model_update(session.pod_name, ws.k8s_namespace, new_model)
        except Exception as exc:
            log.warning("exec_model_update failed for session %d: %s", sid, exc)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to update model on running pod: {exc}",
            )

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
    from croniter import croniter

    session = await _get_session_or_404(ws_id, sid, db)
    if session.mode != "prompt":
        raise HTTPException(status_code=422, detail="Scheduling only supported for prompt-mode sessions")

    if not croniter.is_valid(body.cron_expr):
        raise HTTPException(status_code=422, detail=f"Invalid cron expression: {body.cron_expr}")

    session.cron_schedule = body.cron_expr
    session.cron_next_run = croniter(body.cron_expr, datetime.utcnow()).get_next(datetime)
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
    session = await _get_session_or_404(ws_id, sid, db)
    session.cron_schedule = ""
    session.cron_next_run = None

    if session.k8s_secret_names:
        try:
            k8s.cleanup_session_secrets(ws.k8s_namespace, session)
        except Exception:
            pass

    await db.commit()
    await db.refresh(session)
    return session


# ---------- Output & Patches ----------


@router.get("/{sid}/output", response_model=SessionOutput)
async def get_output(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    return SessionOutput(output=session.last_output)


@router.post("/{sid}/clear-output", response_model=SessionOut)
async def clear_output(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    session.last_output = ""
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
    session = await _get_session_or_404(ws_id, sid, db)
    if not session.pod_name or not session.is_active:
        raise HTTPException(status_code=409, detail="Session must be running to generate a patch")

    from swarmer.routers.sessions import (
        _build_commit_msg,
        _exec_in_pod,
        _patch_filename,
        _prefix_diff_paths,
    )

    combined_diff = ""
    for repo in session.repos:
        workdir = f"/workspace/{repo.local_path}"
        try:
            if session.working_branch:
                diff = await asyncio.to_thread(
                    _exec_in_pod,
                    session.pod_name, ws.k8s_namespace, workdir,
                    ["git", "diff", f"origin/{session.working_branch.split('/')[-1]}..HEAD"],
                    container=get_tool(session.agent_tool).get_container_name(),
                )
            else:
                diff = await asyncio.to_thread(
                    _exec_in_pod,
                    session.pod_name, ws.k8s_namespace, workdir,
                    ["git", "diff"],
                    container=get_tool(session.agent_tool).get_container_name(),
                )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Git diff failed: {exc}")

        if diff.strip() and len(session.repos) > 1:
            diff = _prefix_diff_paths(diff, repo.local_path)
        combined_diff += diff

    if not combined_diff.strip():
        session.patch_output = ""
        session.commit_msg = ""
        await db.commit()
        return PatchResult(patch="", commit_msg="No changes detected.", filename=_patch_filename(session))

    commit_msg = await _build_commit_msg(combined_diff, session.workspace_id, db)
    session.patch_output = combined_diff
    session.commit_msg = commit_msg
    await db.commit()

    return PatchResult(
        patch=combined_diff,
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

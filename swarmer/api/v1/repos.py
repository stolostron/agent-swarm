"""REST API — Git repository management for sessions."""

from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer.database import get_db
from swarmer.api.deps import get_workspace_or_404, require_api_auth
from swarmer.api.schemas import MessageOut, RepoCreate, RepoOut
from swarmer.models.session import Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.workspace import Workspace

router = APIRouter(
    prefix="/workspaces/{ws_id}/sessions/{sid}/repos",
    tags=["repos"],
    dependencies=[Depends(require_api_auth)],
)


async def _get_session_or_404(ws_id: int, sid: int, db: AsyncSession) -> Session:
    session = await db.get(Session, sid, options=[selectinload(Session.repos)])
    if session is None or session.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.get("", response_model=list[RepoOut])
async def list_repos(
    ws_id: int,
    sid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    return session.repos


@router.post("", response_model=RepoOut, status_code=status.HTTP_201_CREATED)
async def add_repo(
    ws_id: int,
    sid: int,
    body: RepoCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Cannot modify repos on a running session")

    local_path = body.local_path.strip()
    if not local_path:
        local_path = body.repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    path = PurePosixPath(local_path)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(status_code=422, detail="local_path must be a relative path without '..' segments")

    repo = SessionRepo(
        session_id=sid,
        repo_url=body.repo_url.strip(),
        branch=body.branch.strip() or "main",
        local_path=local_path,
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)
    return repo


@router.delete("/{rid}", response_model=MessageOut)
async def delete_repo(
    ws_id: int,
    sid: int,
    rid: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session_or_404(ws_id, sid, db)
    if session.is_active:
        raise HTTPException(status_code=409, detail="Cannot modify repos on a running session")

    repo = await db.get(SessionRepo, rid)
    if repo is None or repo.session_id != sid:
        raise HTTPException(status_code=404, detail="Repo not found")

    await db.delete(repo)
    await db.commit()
    return MessageOut(detail="Repo removed.")

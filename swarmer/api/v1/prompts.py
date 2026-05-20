"""REST API — Prompt source and prompt management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer.database import get_db
from swarmer.api.deps import get_current_user, get_workspace_or_404, require_api_auth
from swarmer.api.schemas import (
    MessageOut,
    PromptOut,
    PromptSourceCreate,
    PromptSourceOut,
    PromptSourceUpdate,
)
from swarmer.github import github_slug, list_folder_contents, list_repos_for_pat
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces/{ws_id}/prompts",
    tags=["prompts"],
    dependencies=[Depends(require_api_auth)],
)


@router.get("", response_model=list[PromptSourceOut])
async def list_prompt_sources(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.workspace_id == ws_id)
        .options(selectinload(WorkspacePromptSource.prompts))
        .order_by(WorkspacePromptSource.name)
    )
    return result.scalars().all()


@router.post("", response_model=PromptSourceOut, status_code=status.HTTP_201_CREATED)
async def create_prompt_source(
    ws_id: int,
    body: PromptSourceCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    source = WorkspacePromptSource(
        workspace_id=ws_id,
        name=body.name.strip(),
        github_pat_id=body.github_pat_id,
        repo_url=body.repo_url.strip(),
        branch=body.branch.strip() or "main",
        folder_path=body.folder_path.strip() or ".",
    )
    db.add(source)
    try:
        await db.commit()
        await db.refresh(source)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A prompt source named '{body.name}' already exists.",
        )

    # Trigger initial sync
    from swarmer.routers.prompts import _refresh_source_logic
    await _refresh_source_logic(source, db)
    await db.commit()

    # Re-load with prompts
    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.id == source.id)
        .options(selectinload(WorkspacePromptSource.prompts))
    )
    return result.scalar_one()


@router.get("/{ps_id}", response_model=PromptSourceOut)
async def get_prompt_source(
    ws_id: int,
    ps_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkspacePromptSource)
        .where(
            WorkspacePromptSource.id == ps_id,
            WorkspacePromptSource.workspace_id == ws_id,
        )
        .options(selectinload(WorkspacePromptSource.prompts))
    )
    source = result.scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Prompt source not found")
    return source


@router.put("/{ps_id}", response_model=PromptSourceOut)
async def update_prompt_source(
    ws_id: int,
    ps_id: int,
    body: PromptSourceUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(WorkspacePromptSource, ps_id)
    if source is None or source.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="Prompt source not found")

    if body.name is not None:
        source.name = body.name.strip()
    if body.github_pat_id is not None:
        source.github_pat_id = body.github_pat_id
    if body.repo_url is not None:
        source.repo_url = body.repo_url.strip()
    if body.branch is not None:
        source.branch = body.branch.strip() or "main"
    if body.folder_path is not None:
        source.folder_path = body.folder_path.strip() or "."

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A prompt source with that name already exists")

    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.id == source.id)
        .options(selectinload(WorkspacePromptSource.prompts))
    )
    return result.scalar_one()


@router.delete("/{ps_id}", response_model=MessageOut)
async def delete_prompt_source(
    ws_id: int,
    ps_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(WorkspacePromptSource, ps_id)
    if source is None or source.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="Prompt source not found")

    await db.delete(source)
    await db.commit()
    return MessageOut(detail="Prompt source deleted.")


@router.post("/{ps_id}/refresh", response_model=PromptSourceOut)
async def refresh_prompt_source(
    ws_id: int,
    ps_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(WorkspacePromptSource, ps_id)
    if source is None or source.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="Prompt source not found")

    from swarmer.routers.prompts import _refresh_source_logic
    await _refresh_source_logic(source, db)
    await db.commit()

    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.id == source.id)
        .options(selectinload(WorkspacePromptSource.prompts))
    )
    return result.scalar_one()


@router.get("/{ps_id}/prompts/{prompt_id}/preview", response_model=PromptOut)
async def preview_prompt(
    ws_id: int,
    ps_id: int,
    prompt_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkspacePrompt)
        .join(WorkspacePromptSource)
        .where(
            WorkspacePrompt.id == prompt_id,
            WorkspacePrompt.source_id == ps_id,
            WorkspacePromptSource.workspace_id == ws_id,
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return prompt


@router.get("/browse/repos")
async def browse_repos(
    ws_id: int,
    github_pat_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(GitHubPAT).where(
            GitHubPAT.id == github_pat_id,
            GitHubPAT.workspace_id == ws_id,
            or_(
                GitHubPAT.user_id == user,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            ),
        )
    )
    pat = result.scalar_one_or_none()
    if pat is None:
        raise HTTPException(status_code=404, detail="PAT not found")

    result = await list_repos_for_pat(pat)
    if isinstance(result, str):
        raise HTTPException(status_code=502, detail=result)
    return result


@router.get("/browse/folders")
async def browse_folders(
    ws_id: int,
    repo_url: str,
    branch: str = "main",
    path: str = ".",
    github_pat_id: int | None = None,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    slug = github_slug(repo_url)
    if not slug or slug.count("/") != 1:
        raise HTTPException(status_code=400, detail="Invalid GitHub URL")

    pat_token = None
    if github_pat_id:
        result = await db.execute(
            select(GitHubPAT).where(
                GitHubPAT.id == github_pat_id,
                GitHubPAT.workspace_id == ws_id,
                or_(
                    GitHubPAT.user_id == user,
                    GitHubPAT.shared == True,  # noqa: E712
                    GitHubPAT.user_id == "",
                ),
            )
        )
        pat = result.scalar_one_or_none()
        if pat:
            pat_token = pat.pat

    owner, repo = slug.split("/", 1)
    contents = await list_folder_contents(owner, repo, path, branch, pat_token)
    if isinstance(contents, str):
        raise HTTPException(status_code=502, detail=contents)

    return [c for c in contents if c["type"] == "dir"]

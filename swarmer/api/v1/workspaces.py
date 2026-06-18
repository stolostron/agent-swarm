"""REST API — Workspace CRUD."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.api.deps import (
    filter_accessible_workspaces,
    get_bearer_token,
    get_workspace_or_404,
    require_api_auth,
)
from swarmer.api.schemas import MessageOut, WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from swarmer.k8s_auth import TokenIdentity, can_create_namespaces
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces",
    tags=["workspaces"],
    dependencies=[Depends(require_api_auth)],
)


def _derive_namespace(display_name: str) -> str:
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:63]


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(get_bearer_token),
):
    result = await db.execute(select(Workspace).order_by(Workspace.display_name))
    workspaces = result.scalars().all()
    return await filter_accessible_workspaces(token, workspaces)


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
    token: str = Depends(get_bearer_token),
):
    if settings.k8s_namespace:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace creation is disabled in namespace-scoped deployments.",
        )
    if not await can_create_namespaces(
        token, settings.k8s_api_url, settings.k8s_in_cluster
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to create workspaces.",
        )
    namespace = _derive_namespace(body.display_name)
    if not namespace:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Display name must contain at least one alphanumeric character.",
        )

    ws = Workspace(
        display_name=body.display_name.strip(),
        namespace=namespace,
        description=body.description.strip(),
    )
    db.add(ws)
    try:
        await db.commit()
        await db.refresh(ws)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A workspace with namespace '{namespace}' already exists.",
        )

    # Best-effort K8s setup
    eff_ns = k8s.effective_namespace(namespace)
    try:
        if not settings.k8s_namespace:
            k8s.ensure_namespace(eff_ns)
            k8s.grant_swarmer_user_access(eff_ns, identity.username)
        from swarmer.agent_tools.registry import all_tools
        for tool in all_tools():
            k8s.apply_agent_config(eff_ns, agent_tool=tool.name)
    except Exception:
        pass  # K8s setup failure is non-fatal

    return ws


@router.get("/{ws_id}", response_model=WorkspaceOut)
async def get_workspace(ws: Workspace = Depends(get_workspace_or_404)):
    return ws


@router.put("/{ws_id}", response_model=WorkspaceOut)
async def update_workspace(
    body: WorkspaceUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    ws.display_name = body.display_name.strip()
    ws.description = body.description.strip()
    await db.commit()
    await db.refresh(ws)
    return ws


@router.delete("/{ws_id}", response_model=MessageOut)
async def delete_workspace(
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    name = ws.display_name
    k8s_ns = ws.k8s_namespace

    # Delete DB row first to avoid orphaned rows if K8s cleanup fails
    await db.delete(ws)
    await db.commit()

    # Best-effort K8s namespace cleanup
    try:
        if not settings.k8s_namespace:
            k8s.delete_namespace(k8s_ns)
    except Exception:
        log.warning("Failed to delete K8s namespace %s for workspace '%s'", k8s_ns, name)

    return MessageOut(detail=f"Workspace '{name}' deleted.")

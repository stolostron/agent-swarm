"""REST API — Workspace CRUD."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s, workspace_acl
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.api.deps import (
    filter_accessible_workspaces,
    get_workspace_or_404,
    require_api_auth,
)
from swarmer.api.schemas import (
    MessageOut,
    WorkspaceCreate,
    WorkspaceMemberCreate,
    WorkspaceMemberOut,
    WorkspaceOut,
    WorkspaceUpdate,
)
from swarmer.k8s_auth import TokenIdentity
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_member import WorkspaceMember

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
    identity: TokenIdentity = Depends(require_api_auth),
):
    result = await db.execute(select(Workspace).order_by(Workspace.display_name))
    workspaces = result.scalars().all()
    return await filter_accessible_workspaces(db, workspaces, identity)


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
):
    if settings.k8s_namespace:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workspace creation is disabled in namespace-scoped deployments.",
        )
    if not await workspace_acl.can_create_workspace(db, identity.username, identity.groups):
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
        owner_id=identity.username,
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

    # No K8s namespace is created here (ACM-41659) — access control is now a
    # database ACL (owner_id / workspace_members), not a per-workspace
    # namespace + RoleBinding. A handful of legacy per-workspace K8s Secret
    # features (pull secrets) lazily create their namespace on first use.
    return ws


@router.get("/{ws_id}", response_model=WorkspaceOut)
async def get_workspace(ws: Workspace = Depends(get_workspace_or_404)):
    return ws


async def _require_manage_permission(
    db: AsyncSession, ws: Workspace, identity: TokenIdentity
) -> None:
    """Raise 403 unless *identity* is the workspace owner or a configured admin."""
    if not await workspace_acl.can_manage_members(
        db, ws, identity.username, identity.groups
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner or an admin can do that.",
        )


@router.put("/{ws_id}", response_model=WorkspaceOut)
async def update_workspace(
    body: WorkspaceUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    workspace_acl.claim_ownership_if_unowned(ws, identity.username)
    ws.display_name = body.display_name.strip()
    ws.description = body.description.strip()
    await db.commit()
    await db.refresh(ws)
    return ws


@router.delete("/{ws_id}", response_model=MessageOut)
async def delete_workspace(
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    name = ws.display_name
    k8s_ns = ws.k8s_namespace

    # Delete DB row first to avoid orphaned rows if K8s cleanup fails
    await db.delete(ws)
    await db.commit()

    # Best-effort cleanup of a lazily-created K8s namespace, if one exists
    # (e.g. a pull secret was ever saved for this workspace). No namespace is
    # created at workspace creation time anymore (ACM-41659).
    try:
        if not settings.k8s_namespace:
            k8s.delete_namespace(k8s_ns)
    except Exception:
        log.warning("Failed to delete K8s namespace %s for workspace '%s'", k8s_ns, name)

    return MessageOut(detail=f"Workspace '{name}' deleted.")


# ============================================================
# Members (ACM-41659) — database-backed workspace ACL
# ============================================================


@router.get("/{ws_id}/members", response_model=list[WorkspaceMemberOut])
async def list_workspace_members(
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == ws.id)
        .order_by(WorkspaceMember.user_id)
    )
    return result.scalars().all()


@router.post(
    "/{ws_id}/members",
    response_model=WorkspaceMemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_workspace_member(
    body: WorkspaceMemberCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    workspace_acl.claim_ownership_if_unowned(ws, identity.username)
    user_id = body.user_id.strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id is required.",
        )
    if user_id == ws.owner_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{user_id}' is already the workspace owner.",
        )
    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=user_id,
        role=(body.role or "member").strip() or "member",
    )
    db.add(member)
    try:
        await db.commit()
        await db.refresh(member)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{user_id}' is already a member of this workspace.",
        )
    return member


@router.delete("/{ws_id}/members/{user_id}", response_model=MessageOut)
async def remove_workspace_member(
    user_id: str,
    ws: Workspace = Depends(get_workspace_or_404),
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_permission(db, ws, identity)
    workspace_acl.claim_ownership_if_unowned(ws, identity.username)
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{user_id}' is not a member of this workspace.",
        )
    await db.delete(member)
    await db.commit()
    return MessageOut(detail=f"'{user_id}' removed from workspace.")

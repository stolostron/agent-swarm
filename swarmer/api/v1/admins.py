"""REST API — Global admins and the current-user identity endpoint (ACM-41659).

Global admins can see and manage every workspace. This is the primary,
self-service way to designate admins after the initial deployment bootstrap
(see `admin_bootstrap_available` / `POST /admins/bootstrap`) — it supplements
the static `WORKSPACE_ADMIN_USERS` / `WORKSPACE_ADMIN_GROUPS` env vars, which
remain useful for declarative/GitOps-managed admin lists.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import workspace_acl
from swarmer.api.deps import require_api_auth
from swarmer.api.schemas import (
    GlobalAdminCreate,
    GlobalAdminOut,
    KnownUsersOut,
    MeOut,
    MessageOut,
)
from swarmer.database import get_db
from swarmer.k8s_auth import TokenIdentity

router = APIRouter(tags=["admins"], dependencies=[Depends(require_api_auth)])


@router.get("/me", response_model=MeOut)
async def get_me(
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """Return the caller's identity and admin/create-workspace permissions.

    Console routes only talk to the API (never the DB directly), so this is
    how they render admin-gated UI (the Members "manage" controls, the
    "+ New Workspace" button, the `/admins` page, the bootstrap banner)
    without duplicating ACL logic client-side.
    """
    return MeOut(
        username=identity.username,
        is_admin=await workspace_acl.is_admin(db, identity.username, identity.groups),
        can_create_workspace=await workspace_acl.can_create_workspace(
            db, identity.username, identity.groups
        ),
        admin_bootstrap_available=await workspace_acl.admin_bootstrap_available(db),
    )


@router.get("/users", response_model=KnownUsersOut)
async def get_known_users(
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """Autocomplete suggestions for Add Member / Add Admin forms.

    Visibility-scoped, not a global user directory — see
    `workspace_acl.list_known_users()`. Free-text entry on those forms is
    always still allowed; this only powers suggestions.
    """
    users = await workspace_acl.list_known_users(db, identity.username, identity.groups)
    return KnownUsersOut(users=users)


async def _require_admin(identity: TokenIdentity, db: AsyncSession) -> None:
    if not await workspace_acl.is_admin(db, identity.username, identity.groups):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a Swarmer admin can do that.",
        )


@router.get("/admins", response_model=list[GlobalAdminOut])
async def list_admins(
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_admin(identity, db)
    return await workspace_acl.list_global_admins(db)


@router.post("/admins", response_model=GlobalAdminOut, status_code=status.HTTP_201_CREATED)
async def add_admin(
    body: GlobalAdminCreate,
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_admin(identity, db)
    user_id = body.user_id.strip()
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="user_id is required.",
        )
    try:
        return await workspace_acl.add_global_admin(db, user_id, identity.username)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{user_id}' is already an admin.",
        )


@router.delete("/admins/{user_id}", response_model=MessageOut)
async def remove_admin(
    user_id: str,
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    await _require_admin(identity, db)
    if not await workspace_acl.remove_global_admin(db, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{user_id}' is not an admin.",
        )
    return MessageOut(detail=f"'{user_id}' removed from admins.")


@router.post("/admins/bootstrap", response_model=GlobalAdminOut, status_code=status.HTTP_201_CREATED)
async def bootstrap_admin(
    identity: TokenIdentity = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """One-click self-promotion to global admin — only works while zero
    admins exist (static or DB). Solves the bootstrap problem: a fresh
    deployment has zero friction, and every deployment after that is managed
    entirely through the `/admins` UI/API."""
    if not await workspace_acl.bootstrap_admin(db, identity.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An admin already exists — ask them to add you via the Admins page.",
        )
    result = await workspace_acl.list_global_admins(db)
    return next(a for a in result if a.user_id == identity.username)

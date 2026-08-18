"""FastAPI dependencies for the REST API.

API authentication uses the Authorization header with a K8s bearer token,
validated via the same TokenReview mechanism as the Console login flow.
Workspace-level authorization is a database-backed ACL (ACM-41659) — see
``swarmer.workspace_acl`` — rather than per-workspace K8s namespace RBAC.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import workspace_acl
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.k8s_auth import TokenIdentity, validate_token
from swarmer.models.workspace import Workspace

_bearer_scheme = HTTPBearer()


async def get_bearer_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> str:
    """Return the raw bearer token from the Authorization header."""
    return credentials.credentials


async def require_api_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> TokenIdentity:
    """Validate the K8s bearer token from the Authorization header.

    Returns a TokenIdentity on success; raises 401 on failure.
    """
    token = credentials.credentials
    identity = await validate_token(
        token, settings.k8s_api_url, settings.k8s_in_cluster
    )
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired bearer token",
        )
    return identity


async def get_current_user(
    identity: TokenIdentity = Depends(require_api_auth),
) -> str:
    """Return the username from the validated token."""
    return identity.username


async def user_can_access_workspace(
    db: AsyncSession, ws: Workspace, identity: TokenIdentity
) -> bool:
    """Return True when *identity* has ACL access to the workspace."""
    return await workspace_acl.user_can_access_workspace(
        db, ws, identity.username, identity.groups
    )


async def filter_accessible_workspaces(
    db: AsyncSession, workspaces: list[Workspace], identity: TokenIdentity
) -> list[Workspace]:
    """Return workspaces *identity* can access per the workspace ACL."""
    return await workspace_acl.filter_accessible_workspaces(
        db, workspaces, identity.username, identity.groups
    )


async def get_workspace_or_404(
    ws_id: int,
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
) -> Workspace:
    """Fetch a workspace by ID when the caller has ACL access, else 404."""
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {ws_id} not found",
        )
    if not await user_can_access_workspace(db, ws, identity):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {ws_id} not found",
        )
    return ws

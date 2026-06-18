"""FastAPI dependencies for the REST API.

API authentication uses the Authorization header with a K8s bearer token,
validated via the same TokenReview mechanism as the Console login flow.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.config import settings
from swarmer.database import get_db
from swarmer.k8s_auth import TokenIdentity, get_accessible_namespaces, validate_token
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


async def user_can_access_workspace(token: str, ws: Workspace) -> bool:
    """Return True when *token* has RBAC access to the workspace namespace."""
    accessible = await get_accessible_namespaces(
        token,
        [ws.k8s_namespace],
        settings.k8s_api_url,
        settings.k8s_in_cluster,
    )
    return ws.k8s_namespace in accessible


async def filter_accessible_workspaces(
    token: str, workspaces: list[Workspace]
) -> list[Workspace]:
    """Return workspaces whose K8s namespaces *token* can access via RBAC."""
    if not workspaces:
        return []
    ns_map: dict[str, list[Workspace]] = {}
    for ws in workspaces:
        ns_map.setdefault(ws.k8s_namespace, []).append(ws)
    accessible = set(
        await get_accessible_namespaces(
            token,
            list(ns_map),
            settings.k8s_api_url,
            settings.k8s_in_cluster,
        )
    )
    return [ws for ws in workspaces if ws.k8s_namespace in accessible]


async def get_workspace_or_404(
    ws_id: int,
    db: AsyncSession = Depends(get_db),
    token: str = Depends(get_bearer_token),
) -> Workspace:
    """Fetch a workspace by ID when the caller has namespace access, else 404."""
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {ws_id} not found",
        )
    if not await user_can_access_workspace(token, ws):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace {ws_id} not found",
        )
    return ws

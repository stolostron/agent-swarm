"""REST API — MCP server management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.database import get_db
from swarmer.api.deps import get_current_user, get_workspace_or_404, require_api_auth
from swarmer.api.schemas import (
    McpHealthOut,
    McpServerAddFromCatalog,
    McpServerOut,
    McpServerSaveConfig,
    MessageOut,
)
from swarmer.mcp_catalog import get_catalog_entry
from swarmer.models.mcp_server import McpServer
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces/{ws_id}/mcp-servers",
    tags=["mcp-servers"],
    dependencies=[Depends(require_api_auth)],
)


@router.get("", response_model=list[McpServerOut])
async def list_mcp_servers(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(McpServer).where(
            McpServer.workspace_id == ws_id,
            or_(
                McpServer.user_id == user,
                McpServer.shared == True,  # noqa: E712
                McpServer.user_id == "",
            ),
        ).order_by(McpServer.display_name)
    )
    return result.scalars().all()


@router.post("", response_model=McpServerOut, status_code=status.HTTP_201_CREATED)
async def add_from_catalog(
    ws_id: int,
    body: McpServerAddFromCatalog,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    entry = get_catalog_entry(body.catalog_slug)
    if entry is None:
        raise HTTPException(status_code=400, detail="Unknown MCP server type")

    server = McpServer(
        workspace_id=ws_id,
        slug=entry["slug"],
        display_name=entry["display_name"],
        server_url=entry.get("server_url", ""),
        server_type=entry.get("server_type", "http"),
        jira_server_url=entry.get("default_jira_server_url", ""),
        user_id=user,
    )
    db.add(server)
    try:
        await db.commit()
        await db.refresh(server)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"'{entry['display_name']}' is already added to this workspace.",
        )

    return server


@router.post("/{server_id}/save", response_model=McpServerOut)
async def save_config(
    ws_id: int,
    server_id: int,
    body: McpServerSaveConfig,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="MCP server not found")

    jira_server_url = body.jira_server_url.strip().rstrip("/")
    jira_access_token = body.jira_access_token.strip()
    jira_email = body.jira_email.strip()

    if not jira_server_url or not jira_email:
        raise HTTPException(status_code=422, detail="Server URL and email are required")
    if not jira_access_token and not server.jira_access_token_enc:
        raise HTTPException(status_code=422, detail="API token is required")

    server.jira_server_url = jira_server_url
    if jira_access_token:
        server.jira_access_token = jira_access_token
    server.jira_email = jira_email

    from swarmer.routers.mcp_servers import _probe_jira_token
    probe_token = jira_access_token or server.jira_access_token
    valid = await _probe_jira_token(jira_server_url, jira_email, probe_token)
    if valid:
        server.token_expires_at = None
    else:
        from datetime import datetime
        server.token_expires_at = datetime.utcnow()

    await db.commit()
    await db.refresh(server)
    return server


@router.get("/check", response_model=McpHealthOut)
async def check_health(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(McpServer).where(McpServer.workspace_id == ws_id)
    )
    servers = result.scalars().all()

    statuses = {}
    for srv in servers:
        if srv.jira_access_token_enc:
            from swarmer.routers.mcp_servers import _probe_jira_token
            valid = await _probe_jira_token(
                srv.jira_server_url, srv.jira_email, srv.jira_access_token
            )
            if valid and srv.token_expires_at is not None:
                srv.token_expires_at = None
            elif not valid and srv.token_expires_at is None:
                from datetime import datetime
                srv.token_expires_at = datetime.utcnow()

        statuses[str(srv.id)] = {
            "status": srv.auth_status,
            "label": srv.auth_status_label,
            "color": srv.auth_status_color,
        }

    await db.commit()
    return McpHealthOut(statuses=statuses)


@router.post("/{server_id}/toggle", response_model=McpServerOut)
async def toggle_server(
    ws_id: int,
    server_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="MCP server not found")

    server.enabled = not server.enabled
    await db.commit()
    await db.refresh(server)
    return server


@router.delete("/{server_id}", response_model=MessageOut)
async def delete_mcp_server(
    ws_id: int,
    server_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="MCP server not found")

    name = server.display_name
    await db.delete(server)
    await db.commit()
    return MessageOut(detail=f"Removed {name}.")

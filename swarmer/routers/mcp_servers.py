"""Console routes — MCP server management.

All data access goes through the REST API client (/api/v1/).
Shared helper functions (_probe_jira_token, get_enabled_mcp_servers) are
kept here for backward compatibility with API v1 routes that import them.
"""

import ipaddress
import logging
from datetime import datetime
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.mcp_catalog import MCP_SERVER_CATALOG
from swarmer.routers.api_client import APIError, get_api_client

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


# ============================================================
# MCP Servers List
# ============================================================

@router.get(
    "/workspaces/{ws_id}/mcp-servers",
    dependencies=[Depends(require_auth)],
)
async def mcp_servers_list(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        try:
            servers = await api.list_mcp_servers(ws_id)
        except APIError:
            servers = []

    return templates.TemplateResponse(
        request,
        "mcp_servers/list.html",
        {"ws": ws, "servers": servers, "catalog": MCP_SERVER_CATALOG},
    )


# ============================================================
# Add from catalog
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/add",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_add_from_catalog(
    ws_id: int,
    request: Request,
    catalog_slug: str = Form(...),
):
    async with get_api_client(request) as api:
        try:
            result = await api.add_mcp_from_catalog(ws_id, catalog_slug)
            flash(
                request,
                f"Added {result.get('display_name', catalog_slug)}. "
                "Configure your API token to authenticate.",
                "success",
            )
        except APIError as exc:
            if exc.status_code == 409:
                flash(request, exc.detail, "warning")
            else:
                flash(request, f"Failed to add MCP server: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Save API token configuration
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/save",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_save_config(
    ws_id: int,
    server_id: int,
    request: Request,
    jira_server_url: str = Form(...),
    jira_access_token: str = Form(""),
    jira_email: str = Form(...),
):
    async with get_api_client(request) as api:
        try:
            result = await api.save_mcp_config(
                ws_id,
                server_id,
                jira_server_url=jira_server_url,
                jira_email=jira_email,
                jira_access_token=jira_access_token,
            )
            # Check auth status to determine success message
            if result.get("auth_status") == "valid":
                flash(
                    request,
                    f"Connected to {result.get('display_name', 'MCP server')}! Token validated.",
                    "success",
                )
            else:
                flash(
                    request,
                    "Credentials saved. Token could not be validated "
                    "— check your server URL, email, and API token.",
                    "warning",
                )
        except APIError as exc:
            flash(request, exc.detail, "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Health check (polled by UI auto-refresh)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/mcp-servers/check",
    dependencies=[Depends(require_auth)],
)
async def mcp_servers_check(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            result = await api.check_mcp_health(ws_id)
            return JSONResponse(result.get("statuses", {}))
        except APIError:
            return JSONResponse({})


# ============================================================
# Toggle enabled/disabled
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/toggle",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_toggle(
    ws_id: int,
    server_id: int,
    request: Request,
):
    async with get_api_client(request) as api:
        try:
            result = await api.toggle_mcp_server(ws_id, server_id)
            state = "enabled" if result.get("enabled") else "disabled"
            flash(request, f"{result.get('display_name', 'Server')} {state}.", "info")
        except APIError as exc:
            flash(request, f"Toggle failed: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Delete
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/delete",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_delete(
    ws_id: int,
    server_id: int,
    request: Request,
):
    async with get_api_client(request) as api:
        try:
            result = await api.delete_mcp_server(ws_id, server_id)
            flash(request, result.get("detail", "Removed."), "info")
        except APIError as exc:
            flash(request, f"Delete failed: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Shared helpers — imported by API v1 routes
# ============================================================

def _is_safe_url(url: str) -> bool:
    """Reject non-HTTPS URLs and those targeting localhost/private/link-local addresses."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return False
    except ValueError:
        if hostname in ("localhost", "localhost.localdomain"):
            return False
    return True


async def _probe_jira_token(server_url: str, email: str, token: str) -> bool:
    """Validate a Jira API token by calling GET /rest/api/3/myself."""
    if not _is_safe_url(server_url):
        log.warning("Rejected probe to disallowed URL: %s", server_url)
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{server_url}/rest/api/3/myself",
                auth=(email, token),
            )
            return resp.status_code == 200
    except Exception:
        return False


async def get_enabled_mcp_servers(
    workspace_id: int, db: AsyncSession, user_id: str = ""
) -> list:
    """Return all enabled & authenticated MCP servers for a workspace.

    This is a shared helper imported by API v1 routes — it still needs
    direct DB access.  When the API v1 layer is refactored to not import
    from Console routers, this function should move to a shared module.
    """
    from swarmer.models.mcp_server import McpServer

    filters = [
        McpServer.workspace_id == workspace_id,
        McpServer.enabled == True,  # noqa: E712
        McpServer.jira_access_token_enc != "",
        or_(
            McpServer.token_expires_at == None,  # noqa: E711
            McpServer.token_expires_at > datetime.utcnow(),
        ),
    ]
    if user_id:
        filters.append(
            or_(
                McpServer.user_id == user_id,
                McpServer.shared == True,  # noqa: E712
                McpServer.user_id == "",
            )
        )
    result = await db.execute(select(McpServer).where(*filters))
    return list(result.scalars().all())

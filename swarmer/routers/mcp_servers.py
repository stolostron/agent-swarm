import ipaddress
import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.mcp_catalog import MCP_SERVER_CATALOG, get_catalog_entry
from swarmer.models.mcp_server import McpServer
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


# ============================================================
# MCP Servers List
# ============================================================

@router.get(
    "/workspaces/{ws_id}/mcp-servers",
    dependencies=[Depends(require_auth)],
)
async def mcp_servers_list(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    result = await db.execute(
        select(McpServer).where(McpServer.workspace_id == ws_id).order_by(McpServer.display_name)
    )
    servers = result.scalars().all()

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
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    entry = get_catalog_entry(catalog_slug)
    if entry is None:
        flash(request, "Unknown MCP server type.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server = McpServer(
        workspace_id=ws_id,
        slug=entry["slug"],
        display_name=entry["display_name"],
        server_url=entry.get("server_url", ""),
        server_type=entry.get("server_type", "http"),
        jira_server_url=entry.get("default_jira_server_url", ""),
    )
    db.add(server)
    try:
        await db.commit()
        await db.refresh(server)
    except IntegrityError:
        await db.rollback()
        flash(request, f"'{entry['display_name']}' is already added to this workspace.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    flash(request, f"Added {entry['display_name']}. Configure your API token to authenticate.", "success")
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
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    jira_server_url = jira_server_url.strip().rstrip("/")
    jira_access_token = jira_access_token.strip()
    jira_email = jira_email.strip()

    if not jira_server_url or not jira_email:
        flash(request, "Server URL and email are required.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    if not jira_access_token and not server.jira_access_token_enc:
        flash(request, "API token is required.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server.jira_server_url = jira_server_url
    if jira_access_token:
        server.jira_access_token = jira_access_token
    server.jira_email = jira_email

    probe_token = jira_access_token or server.jira_access_token
    valid = await _probe_jira_token(jira_server_url, jira_email, probe_token)
    if valid:
        server.token_expires_at = None
    else:
        from datetime import datetime
        server.token_expires_at = datetime.utcnow()
    await db.commit()
    await _sync_mcp_to_k8s(ws_id, db, request)

    if valid:
        flash(request, f"Connected to {server.display_name}! Token validated.", "success")
    else:
        flash(
            request,
            "Credentials saved. Token could not be validated — check your server URL, email, and API token.",
            "warning",
        )

    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Health check (polled by UI auto-refresh)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/mcp-servers/check",
    dependencies=[Depends(require_auth)],
)
async def mcp_servers_check(
    ws_id: int, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(McpServer).where(McpServer.workspace_id == ws_id)
    )
    servers = result.scalars().all()

    statuses = {}
    for srv in servers:
        if srv.jira_access_token_enc:
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
    return JSONResponse(statuses)


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
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server.enabled = not server.enabled
    await db.commit()

    await _sync_mcp_to_k8s(ws_id, db, request)
    state = "enabled" if server.enabled else "disabled"
    flash(request, f"{server.display_name} {state}.", "info")
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
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    name = server.display_name
    await db.delete(server)
    await db.commit()

    await _sync_mcp_to_k8s(ws_id, db, request)
    flash(request, f"Removed {name}.", "info")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Helpers
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
        # hostname is a DNS name — block obvious localhost aliases
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


async def get_enabled_mcp_servers(workspace_id: int, db: AsyncSession) -> list[McpServer]:
    """Return all enabled & authenticated MCP servers for a workspace (excluding expired)."""
    from sqlalchemy import or_
    from datetime import datetime
    result = await db.execute(
        select(McpServer).where(
            McpServer.workspace_id == workspace_id,
            McpServer.enabled == True,  # noqa: E712
            McpServer.jira_access_token_enc != "",
            or_(
                McpServer.token_expires_at == None,  # noqa: E711
                McpServer.token_expires_at > datetime.utcnow(),
            ),
        )
    )
    return list(result.scalars().all())


async def _sync_mcp_to_k8s(ws_id: int, db: AsyncSession, request: Request) -> None:
    """Sync MCP server tokens to K8s secret and update agent config maps."""
    from swarmer import k8s as _k8s
    from swarmer.agent_tools.registry import all_tools
    from swarmer.models.opencode_secret import OpencodeSecret

    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return

    mcp_servers = await get_enabled_mcp_servers(ws_id, db)

    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc_secret = oc_result.scalar_one_or_none()

    try:
        _k8s.sync_mcp_server_secret(ws.k8s_namespace, mcp_servers)
        for tool in all_tools():
            _k8s.apply_agent_config(
                ws.k8s_namespace, secret=oc_secret,
                agent_tool=tool.name, mcp_servers=mcp_servers,
            )
    except Exception as exc:
        log.warning("K8s sync for MCP servers failed: %s", exc)
        flash(request, f"K8s sync failed: {exc}", "warning")

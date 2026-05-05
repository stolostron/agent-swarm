import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
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

OAUTH_CALLBACK_PATH = "/mcp-servers/oauth/callback"
OAUTH_LOCALHOST_REDIRECT = "http://localhost:8080/mcp-servers/oauth/callback"


def _get_redirect_uri(request: Request) -> str:
    return OAUTH_LOCALHOST_REDIRECT


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

    pending_oauth = request.session.get("mcp_pending_oauth", {})

    return templates.TemplateResponse(
        request,
        "mcp_servers/list.html",
        {"ws": ws, "servers": servers, "catalog": MCP_SERVER_CATALOG, "pending_oauth": pending_oauth},
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
        server_url=entry["server_url"],
        server_type=entry["server_type"],
        authorization_endpoint=entry["authorization_endpoint"],
        token_endpoint=entry["token_endpoint"],
        registration_endpoint=entry["registration_endpoint"],
        scopes=entry["scopes"],
    )
    db.add(server)
    try:
        await db.commit()
        await db.refresh(server)
    except IntegrityError:
        await db.rollback()
        flash(request, f"'{entry['display_name']}' is already added to this workspace.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    flash(request, f"Added {entry['display_name']}. Connect via OAuth to authenticate.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# OAuth: Initiate authorization
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/connect",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_oauth_connect(
    ws_id: int,
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    server = await db.get(McpServer, server_id)
    if ws is None or server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    redirect_uri = _get_redirect_uri(request)

    # Dynamic client registration if no client_id yet
    if not server.oauth_client_id and server.registration_endpoint:
        try:
            client_id, client_secret = await _dynamic_register(
                server.registration_endpoint, redirect_uri
            )
            server.oauth_client_id = client_id
            if client_secret:
                server.oauth_client_secret = client_secret
            await db.commit()
        except Exception as exc:
            log.error("Dynamic client registration failed: %s", exc)
            flash(request, f"OAuth registration failed: {exc}", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    # Generate PKCE code_verifier + code_challenge
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge_b64 = base64.urlsafe_b64encode(code_challenge).rstrip(b"=").decode("ascii")

    state = secrets.token_urlsafe(32)

    # Store in HTTP session for callback — ws_id travels via state, not the URL
    request.session[f"mcp_oauth_{state}"] = {
        "server_id": server.id,
        "ws_id": ws_id,
        "code_verifier": code_verifier,
    }

    params = {
        "response_type": "code",
        "client_id": server.oauth_client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge_b64,
        "code_challenge_method": "S256",
    }
    if server.scopes:
        params["scope"] = server.scopes

    pending = request.session.get("mcp_pending_oauth", {})
    pending[str(server_id)] = True
    request.session["mcp_pending_oauth"] = pending

    from urllib.parse import urlencode
    auth_url = server.authorization_endpoint
    query = urlencode(params)
    flash(
        request,
        f"Authorize with {server.display_name}, then copy the localhost URL "
        "your browser is redirected to and paste it below.",
        "info",
    )
    return RedirectResponse(url=f"{auth_url}?{query}", status_code=302)


# ============================================================
# OAuth: Complete  (paste the localhost callback URL)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/complete-oauth",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_oauth_complete(
    ws_id: int,
    server_id: int,
    request: Request,
    callback_url: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Parse the pasted localhost callback URL and exchange the code for tokens."""
    ws = await _get_workspace(ws_id, db)
    server = await db.get(McpServer, server_id)
    if ws is None or server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    parsed = urlparse(callback_url.strip())
    params = parse_qs(parsed.query)

    error = params.get("error", [None])[0]
    if error:
        flash(request, f"OAuth authorization failed: {error}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]

    if not code or not state:
        flash(request, "Invalid callback URL — missing code or state parameter.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    session_key = f"mcp_oauth_{state}"
    oauth_state = request.session.pop(session_key, None)

    if not oauth_state:
        flash(request, "OAuth state expired or invalid. Please reconnect.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    if oauth_state["server_id"] != server_id or oauth_state["ws_id"] != ws_id:
        flash(request, "OAuth state mismatch. Please reconnect.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    redirect_uri = _get_redirect_uri(request)

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": server.oauth_client_id,
        "code_verifier": oauth_state["code_verifier"],
    }

    headers = {}
    if server.oauth_client_secret:
        credentials = base64.b64encode(
            f"{server.oauth_client_id}:{server.oauth_client_secret}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                server.token_endpoint,
                data=token_data,
                headers=headers,
            )
            resp.raise_for_status()
            token_response = resp.json()
    except Exception as exc:
        log.error("Token exchange failed for MCP server %s: %s", server.slug, exc)
        flash(request, f"Token exchange failed: {exc}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server.access_token = token_response.get("access_token", "")
    if token_response.get("refresh_token"):
        server.refresh_token = token_response["refresh_token"]

    expires_in = token_response.get("expires_in")
    if expires_in:
        server.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
    else:
        server.token_expires_at = None

    await db.commit()

    pending = request.session.get("mcp_pending_oauth", {})
    pending.pop(str(server_id), None)
    request.session["mcp_pending_oauth"] = pending

    await _sync_mcp_to_k8s(ws_id, db, request)
    flash(request, f"Successfully connected to {server.display_name}!", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# OAuth: Callback  (kept for direct localhost use, but normally
# the user will paste the URL via the complete-oauth form)
# ============================================================

@router.get(
    OAUTH_CALLBACK_PATH,
)
async def mcp_server_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """If someone hits this endpoint directly (e.g. localhost is reachable),
    show a simple page with the full URL for pasting."""
    from fastapi.responses import HTMLResponse
    full_url = str(request.url)
    html = f"""
    <html><head><title>OAuth Callback</title></head>
    <body style="font-family:sans-serif; padding:2em; max-width:800px; margin:auto;">
    <h2>OAuth Authorization Complete</h2>
    <p>Copy the URL below and paste it into the <strong>"Paste Callback URL"</strong>
       field on the MCP Servers page in Swarmer.</p>
    <textarea rows="5" style="width:100%; font-family:monospace; font-size:14px;"
              onclick="this.select()" readonly>{full_url}</textarea>
    <p style="margin-top:1em; color:#666;">You can close this tab after copying.</p>
    </body></html>
    """
    return HTMLResponse(content=html)


# ============================================================
# Disconnect (revoke tokens)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/disconnect",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_disconnect(
    ws_id: int,
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server.access_token = ""
    server.refresh_token = ""
    server.token_expires_at = None
    await db.commit()

    await _sync_mcp_to_k8s(ws_id, db, request)
    flash(request, f"Disconnected from {server.display_name}.", "info")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# Refresh token
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/refresh",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_refresh_token(
    ws_id: int,
    server_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    server = await db.get(McpServer, server_id)
    if server is None or server.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    if not server.refresh_token:
        flash(request, "No refresh token available. Please reconnect.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    token_data = {
        "grant_type": "refresh_token",
        "refresh_token": server.refresh_token,
        "client_id": server.oauth_client_id,
    }

    headers = {}
    if server.oauth_client_secret:
        credentials = base64.b64encode(
            f"{server.oauth_client_id}:{server.oauth_client_secret}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                server.token_endpoint,
                data=token_data,
                headers=headers,
            )
            resp.raise_for_status()
            token_response = resp.json()
    except Exception as exc:
        log.error("Token refresh failed for MCP server %s: %s", server.slug, exc)
        flash(request, f"Token refresh failed: {exc}. Please reconnect.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server.access_token = token_response.get("access_token", "")
    if token_response.get("refresh_token"):
        server.refresh_token = token_response["refresh_token"]

    expires_in = token_response.get("expires_in")
    if expires_in:
        server.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))

    await db.commit()
    await _sync_mcp_to_k8s(ws_id, db, request)
    flash(request, f"Token refreshed for {server.display_name}.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


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

async def _dynamic_register(registration_endpoint: str, redirect_uri: str) -> tuple[str, str]:
    """Perform OAuth 2.0 Dynamic Client Registration (RFC 7591)."""
    reg_data = {
        "client_name": "Swarmer Agent Swarm",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(registration_endpoint, json=reg_data)
        resp.raise_for_status()
        data = resp.json()

    return data.get("client_id", ""), data.get("client_secret", "")


async def get_enabled_mcp_servers(workspace_id: int, db: AsyncSession) -> list[McpServer]:
    """Return all enabled & authenticated MCP servers for a workspace."""
    result = await db.execute(
        select(McpServer).where(
            McpServer.workspace_id == workspace_id,
            McpServer.enabled == True,  # noqa: E712
            McpServer.access_token_enc != "",
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

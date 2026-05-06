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
# Atlassian only allows localhost redirect URIs for dynamic client registrations.
# Nothing actually runs on this port; after OAuth the user copies the failed URL.
_LOCALHOST_CALLBACK = f"http://localhost:18080{OAUTH_CALLBACK_PATH}"


def _get_redirect_uri(request: Request) -> str:  # noqa: ARG001
    return _LOCALHOST_CALLBACK


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

    from urllib.parse import urlencode
    auth_url = server.authorization_endpoint
    query = urlencode(params)
    return RedirectResponse(url=f"{auth_url}?{query}", status_code=302)


# ============================================================
# OAuth: Callback  (workspace-agnostic — ws_id is in the state)
# ============================================================

@router.get(
    OAUTH_CALLBACK_PATH,
    dependencies=[Depends(require_auth)],
)
async def mcp_server_oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
):
    session_key = f"mcp_oauth_{state}"
    oauth_state = request.session.pop(session_key, None)

    if error:
        ws_id = oauth_state["ws_id"] if oauth_state else 0
        flash(request, f"OAuth authorization failed: {error}", "danger")
        if ws_id:
            return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)
        return RedirectResponse(url="/workspaces", status_code=302)

    if not oauth_state:
        flash(request, "Invalid OAuth state. Please try connecting again.", "danger")
        return RedirectResponse(url="/workspaces", status_code=302)

    ws_id = oauth_state["ws_id"]

    server = await db.get(McpServer, oauth_state["server_id"])
    if server is None or server.workspace_id != ws_id:
        flash(request, "MCP server not found.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    try:
        token_response = await _do_token_exchange(
            server, code, _LOCALHOST_CALLBACK, oauth_state["code_verifier"]
        )
    except Exception as exc:
        log.error("Token exchange failed for MCP server %s: %s", server.slug, exc)
        flash(request, f"Token exchange failed: {exc}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    _apply_token_response(server, token_response)
    await db.commit()

    await _sync_mcp_to_k8s(ws_id, db, request)
    flash(request, f"Successfully connected to {server.display_name}!", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


# ============================================================
# OAuth: Complete via pasted callback URL (localhost redirect hack)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/mcp-servers/{server_id}/oauth-complete",
    dependencies=[Depends(require_auth)],
)
async def mcp_server_oauth_complete(
    ws_id: int,
    server_id: int,
    request: Request,
    callback_url: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    parsed = urlparse(callback_url)
    qs = parse_qs(parsed.query)
    code = qs.get("code", [""])[0]
    state = qs.get("state", [""])[0]
    error = qs.get("error", [""])[0]

    if error:
        flash(request, f"OAuth authorization failed: {error}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    if not code or not state:
        flash(request, "Could not extract code/state from the pasted URL. Please try connecting again.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    session_key = f"mcp_oauth_{state}"
    oauth_state = request.session.pop(session_key, None)
    if not oauth_state:
        flash(request, "OAuth session expired or invalid state. Please try connecting again.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    server = await db.get(McpServer, oauth_state["server_id"])
    if server is None or server.workspace_id != ws_id:
        flash(request, "MCP server not found.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    try:
        token_response = await _do_token_exchange(
            server, code, _LOCALHOST_CALLBACK, oauth_state["code_verifier"]
        )
    except Exception as exc:
        log.error("Token exchange failed for MCP server %s: %s", server.slug, exc)
        flash(request, f"Token exchange failed: {exc}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)

    _apply_token_response(server, token_response)
    await db.commit()

    await _sync_mcp_to_k8s(ws_id, db, request)
    flash(request, f"Successfully connected to {server.display_name}!", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/mcp-servers", status_code=302)


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
            resp = await client.post(server.token_endpoint, data=token_data, headers=headers)
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

async def _do_token_exchange(
    server: McpServer,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    """Exchange an authorization code for tokens and return the raw token response."""
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": server.oauth_client_id,
        "code_verifier": code_verifier,
    }

    headers = {}
    if server.oauth_client_secret:
        credentials = base64.b64encode(
            f"{server.oauth_client_id}:{server.oauth_client_secret}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {credentials}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(server.token_endpoint, data=token_data, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _apply_token_response(server: McpServer, token_response: dict) -> None:
    """Write access/refresh tokens and expiry from a token response onto a McpServer."""
    server.access_token = token_response.get("access_token", "")
    if token_response.get("refresh_token"):
        server.refresh_token = token_response["refresh_token"]
    expires_in = token_response.get("expires_in")
    if expires_in:
        server.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
    else:
        server.token_expires_at = None


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

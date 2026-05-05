"""
Atlassian OAuth 2.1 routes for the Rovo MCP Server.

Flow:
  1. GET  /workspaces/{ws_id}/atlassian-oauth/start
       Discovers OAuth metadata, performs Dynamic Client Registration (DCR),
       generates PKCE + CSRF state, redirects the browser to Atlassian.

  2. GET  /workspaces/{ws_id}/atlassian-oauth/callback
       Validates CSRF state, exchanges the auth code for an access_token
       using the PKCE code verifier, stores the token in the Starlette HTTP
       session, and redirects back to the session detail page.

The access_token is never written to the database.  It lives only in the
Starlette HTTP session (in-memory) until the user clicks Launch, at which
point _do_launch() copies it into an ephemeral K8s Secret.
"""
import hashlib
import logging
import os
import re
import secrets as _secrets
import time
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.config import settings
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.atlassian_oauth_app import AtlassianOAuthApp

log = logging.getLogger(__name__)

router = APIRouter()

# The Atlassian Rovo MCP Server's well-known OAuth metadata endpoint.
# See https://support.atlassian.com/atlassian-rovo-mcp-server/
_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp/authv2"
_WELL_KNOWN_SUFFIX = "/.well-known/oauth-authorization-server"

# Session keys
_STATE_KEY = "atlassian_oauth_state"


def _token_session_key(ws_id: int) -> str:
    return f"atlassian_oauth_{ws_id}"


def _make_redirect_uri(request: Request, ws_id: int) -> str:
    """Build the OAuth redirect URI from SWARMER_PUBLIC_URL or request.base_url."""
    base = settings.swarmer_public_url.rstrip("/") if settings.swarmer_public_url else str(request.base_url).rstrip("/")
    return f"{base}/workspaces/{ws_id}/atlassian-oauth/callback"


def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


async def _discover_oauth_metadata(mcp_url: str) -> dict:
    """Fetch OAuth 2.0 Authorization Server Metadata from the MCP server.

    The Atlassian Rovo MCP Server advertises its OAuth metadata at the
    well-known endpoint relative to its base URL.
    """
    well_known_url = mcp_url.rstrip("/") + _WELL_KNOWN_SUFFIX
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(well_known_url)
        resp.raise_for_status()
        return resp.json()


async def _do_dcr(registration_endpoint: str, redirect_uri: str) -> str:
    """Perform Dynamic Client Registration and return the client_id."""
    payload = {
        "redirect_uris": [redirect_uri],
        "client_name": "Swarmer",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(registration_endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()
    client_id = data.get("client_id")
    if not client_id:
        raise ValueError(f"DCR response missing client_id: {data}")
    return client_id


def _safe_state(value: str) -> bool:
    """Return True iff *value* looks like a hex CSRF state token."""
    return bool(value and re.fullmatch(r"[0-9a-f]{32}", value))


# ============================================================
# Start — DCR + redirect to Atlassian authorization URL
# ============================================================

@router.get(
    "/workspaces/{ws_id}/atlassian-oauth/start",
    dependencies=[Depends(require_auth)],
)
async def atlassian_oauth_start(
    ws_id: int,
    request: Request,
    return_session: int = 0,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AtlassianOAuthApp).where(AtlassianOAuthApp.workspace_id == ws_id)
    )
    app_config = result.scalar_one_or_none()
    if app_config is None:
        flash(request, "Configure the Atlassian site in the workspace Secrets page first.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=atlassian-oauth", status_code=302)

    redirect_uri = _make_redirect_uri(request, ws_id)

    try:
        metadata = await _discover_oauth_metadata(_ROVO_MCP_URL)
    except Exception as exc:
        log.error("Atlassian OAuth metadata discovery failed: %s", exc)
        flash(request, "Could not reach the Atlassian Rovo MCP Server. Check your network.", "danger")
        _return_url = f"/workspaces/{ws_id}/sessions/{return_session}" if return_session else f"/workspaces/{ws_id}/sessions"
        return RedirectResponse(url=_return_url, status_code=302)

    authorization_endpoint = metadata.get("authorization_endpoint", "")
    token_endpoint = metadata.get("token_endpoint", "")
    registration_endpoint = metadata.get("registration_endpoint", "")

    if not authorization_endpoint or not token_endpoint:
        flash(request, "Atlassian OAuth metadata is incomplete. Try again later.", "danger")
        _return_url = f"/workspaces/{ws_id}/sessions/{return_session}" if return_session else f"/workspaces/{ws_id}/sessions"
        return RedirectResponse(url=_return_url, status_code=302)

    # Dynamic Client Registration
    client_id = ""
    if registration_endpoint:
        try:
            client_id = await _do_dcr(registration_endpoint, redirect_uri)
        except Exception as exc:
            log.error("Atlassian DCR failed: %s", exc)
            flash(request, "Dynamic Client Registration failed. Your admin may need to allowlist this domain.", "danger")
            _return_url = f"/workspaces/{ws_id}/sessions/{return_session}" if return_session else f"/workspaces/{ws_id}/sessions"
            return RedirectResponse(url=_return_url, status_code=302)
    else:
        flash(request, "Atlassian OAuth server does not support Dynamic Client Registration.", "danger")
        _return_url = f"/workspaces/{ws_id}/sessions/{return_session}" if return_session else f"/workspaces/{ws_id}/sessions"
        return RedirectResponse(url=_return_url, status_code=302)

    # PKCE
    code_verifier, code_challenge = _pkce_pair()

    # CSRF state
    state = _secrets.token_hex(16)

    # Store everything in HTTP session — never touches the DB
    request.session[_STATE_KEY] = {
        "state": state,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "token_endpoint": token_endpoint,
        "redirect_uri": redirect_uri,
        "ws_id": ws_id,
        "return_session": return_session,
        "created_at": int(time.time()),
    }

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = authorization_endpoint + "?" + urlencode(params)
    return RedirectResponse(url=auth_url, status_code=302)


# ============================================================
# Callback — exchange code for token
# ============================================================

@router.get(
    "/workspaces/{ws_id}/atlassian-oauth/callback",
    dependencies=[Depends(require_auth)],
)
async def atlassian_oauth_callback(
    ws_id: int,
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: AsyncSession = Depends(get_db),
):
    stored = request.session.get(_STATE_KEY)
    return_session = (stored or {}).get("return_session", 0)
    _return_url = f"/workspaces/{ws_id}/sessions/{return_session}" if return_session else f"/workspaces/{ws_id}/sessions"

    # Clear state immediately so it cannot be reused
    request.session.pop(_STATE_KEY, None)

    # User denied or an error was returned
    if error:
        msg = error_description or error
        flash(request, f"Atlassian authorization failed: {msg}", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    # Validate stored state
    if not stored:
        flash(request, "OAuth session expired or not found. Please try again.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    if stored.get("ws_id") != ws_id:
        flash(request, "OAuth state mismatch (workspace). Please try again.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    if not _safe_state(state) or state != stored.get("state"):
        flash(request, "OAuth CSRF state mismatch. Please try again.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    if not code:
        flash(request, "No authorization code received from Atlassian.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    # Exchange code for access_token
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                stored["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": stored["redirect_uri"],
                    "client_id": stored["client_id"],
                    "code_verifier": stored["code_verifier"],
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            token_data = resp.json()
    except Exception as exc:
        log.error("Atlassian token exchange failed: %s", exc)
        flash(request, "Token exchange with Atlassian failed. Please try again.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    access_token = token_data.get("access_token")
    if not access_token:
        flash(request, "Atlassian returned no access token. Please try again.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    expires_in = token_data.get("expires_in", 3600)
    expires_at = int(time.time()) + int(expires_in)

    # Store in HTTP session — never touches the DB
    request.session[_token_session_key(ws_id)] = {
        "access_token": access_token,
        "expires_at": expires_at,
    }

    flash(request, "Atlassian connected. You can now launch the session.", "success")
    return RedirectResponse(url=_return_url, status_code=302)

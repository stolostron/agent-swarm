"""
Atlassian OAuth 2.0 (3LO) routes.

Atlassian does NOT support Dynamic Client Registration or discoverable
well-known metadata endpoints.  Authentication uses a pre-registered app
created at https://developer.atlassian.com/console/myapps/ whose
client_id and client_secret are stored per-workspace in the database
(client_secret is Fernet-encrypted).

Flow:
  1. GET  /workspaces/{ws_id}/atlassian-oauth/start
       Validates that client credentials are configured, generates a CSRF
       state token, and redirects the browser to Atlassian's authorization
       URL.  Intended to be opened in a new tab (target="_blank").

  2. GET  /workspaces/{ws_id}/atlassian-oauth/callback
       Validates CSRF state, exchanges the auth code for an access_token
       using client_id + client_secret, stores the token in the Starlette
       HTTP session, and redirects back to the session detail page.

The access_token is never written to the database.  It lives only in the
Starlette HTTP session (in-memory) until the user clicks Launch, at which
point _do_launch() copies it into an ephemeral K8s Secret.
"""
import logging
import re
import secrets as _secrets
import time
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

# Atlassian's standard 3LO endpoints — hardcoded, not discoverable.
_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
_TOKEN_URL = "https://auth.atlassian.com/oauth/token"

# Scopes required for Jira read/write via the Rovo MCP Server.
_SCOPES = "read:jira-work write:jira-work read:jira-user offline_access"

# Session keys
_STATE_KEY = "atlassian_oauth_state"


def _token_session_key(ws_id: int) -> str:
    return f"atlassian_oauth_{ws_id}"


def _make_redirect_uri(request: Request, ws_id: int) -> str:
    """Build the OAuth redirect URI from SWARMER_PUBLIC_URL or request.base_url."""
    base = settings.swarmer_public_url.rstrip("/") if settings.swarmer_public_url else str(request.base_url).rstrip("/")
    return f"{base}/workspaces/{ws_id}/atlassian-oauth/callback"


def _safe_state(value: str) -> bool:
    """Return True iff *value* looks like a hex CSRF state token."""
    return bool(value and re.fullmatch(r"[0-9a-f]{32}", value))


# ============================================================
# Start — redirect to Atlassian authorization URL
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

    _return_url = f"/workspaces/{ws_id}/sessions/{return_session}" if return_session else f"/workspaces/{ws_id}/sessions"

    if app_config is None or not app_config.client_id:
        flash(request, "Configure the Atlassian client ID and secret in the workspace Secrets page first.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=atlassian-oauth", status_code=302)

    redirect_uri = _make_redirect_uri(request, ws_id)

    # CSRF state
    state = _secrets.token_hex(16)

    # Store state in HTTP session — never touches the DB
    request.session[_STATE_KEY] = {
        "state": state,
        "redirect_uri": redirect_uri,
        "ws_id": ws_id,
        "return_session": return_session,
        "created_at": int(time.time()),
    }

    params = {
        "audience": "api.atlassian.com",
        "client_id": app_config.client_id,
        "scope": _SCOPES,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "prompt": "consent",
    }
    auth_url = _AUTHORIZE_URL + "?" + urlencode(params)
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

    # Look up client credentials
    result = await db.execute(
        select(AtlassianOAuthApp).where(AtlassianOAuthApp.workspace_id == ws_id)
    )
    app_config = result.scalar_one_or_none()
    if app_config is None or not app_config.client_id or not app_config.client_secret:
        flash(request, "Atlassian client credentials are missing. Reconfigure in Secrets.", "danger")
        return RedirectResponse(url=_return_url, status_code=302)

    # Exchange code for access_token
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            resp = await http.post(
                _TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": app_config.client_id,
                    "client_secret": app_config.client_secret,
                    "code": code,
                    "redirect_uri": stored["redirect_uri"],
                },
                headers={"Content-Type": "application/json"},
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

"""
Atlassian Rovo MCP OAuth 2.0 (3LO) integration.

Implements a zero-credential OAuth flow using Dynamic Client Registration
(RFC 7591) so users do not need to create an Atlassian Developer Console
app.  The flow:

  1. Swarmer registers itself dynamically via POST /v1/register if no
     client_id is stored for this workspace yet.
  2. A PKCE code_verifier/challenge pair is generated and the user is
     redirected to the Atlassian MCP authorization URL.
  3. On callback, the authorization code is exchanged for an access token
     and (rotating) refresh token, both stored Fernet-encrypted in the DB.
  4. At pod launch time the stored tokens are injected as a K8s Secret and
     written into mcp-auth.json inside the container so OpenCode can use
     the Rovo MCP tools immediately.

OAuth server metadata (discovered 2025-05):
  authorize:    https://mcp.atlassian.com/v1/authorize
  token:        https://cf.mcp.atlassian.com/v1/token
  registration: https://cf.mcp.atlassian.com/v1/register
"""
import base64
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_303_SEE_OTHER

from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.atlassian_token import AtlassianToken
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")

# ---------------------------------------------------------------------------
# Atlassian MCP OAuth endpoints (discovered from .well-known)
# ---------------------------------------------------------------------------
_AUTHORIZE_URL = "https://mcp.atlassian.com/v1/authorize"
_TOKEN_URL = "https://cf.mcp.atlassian.com/v1/token"
_REGISTER_URL = "https://cf.mcp.atlassian.com/v1/register"
_MCP_SERVER_URL = "https://mcp.atlassian.com/v1/mcp"

# Scopes required for Jira + offline (refresh token) access
_SCOPES = " ".join([
    "read:jira-work",
    "write:jira-work",
    "read:jira-user",
    "manage:jira-configuration",
    "read:me",
    "offline_access",
])


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def generate_code_verifier() -> str:
    """Generate a cryptographically random PKCE code_verifier (RFC 7636)."""
    # 32 random bytes → 43 base64url chars (well within 43–128 range)
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def compute_code_challenge(verifier: str) -> str:
    """Compute the S256 code_challenge from a code_verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Authorization URL builder
# ---------------------------------------------------------------------------

def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the Atlassian MCP authorization URL with PKCE."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{_AUTHORIZE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------

async def register_oauth_client(*, redirect_uri: str) -> tuple[str, int]:
    """Register Swarmer as an OAuth client with the Atlassian MCP server.

    Returns (client_id, client_id_issued_at).
    """
    payload = {
        "client_name": "Swarmer Agent Dashboard",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # PKCE public client
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(_REGISTER_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()
    client_id = data["client_id"]
    issued_at = data.get("client_id_issued_at", int(time.time()))
    return client_id, issued_at


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

async def exchange_code_for_tokens(
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Returns the raw JSON response dict from the token endpoint.
    """
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            _TOKEN_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

async def refresh_atlassian_token(
    token: AtlassianToken,
    db: AsyncSession,
) -> bool:
    """Refresh the access token using the stored refresh token.

    Updates *token* in-place and commits to *db*.
    Returns True on success, False on failure.
    """
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
        "client_id": token.client_id,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("Atlassian token refresh failed: %s", exc)
        return False

    token.access_token = data["access_token"]
    # Rotating refresh tokens — always update if present
    if data.get("refresh_token"):
        token.refresh_token = data["refresh_token"]
    expires_in = data.get("expires_in", 3600)
    token.expires_at = datetime.fromtimestamp(
        time.time() + expires_in, tz=timezone.utc
    )
    if data.get("scope"):
        token.scopes = data["scope"]
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# mcp-auth.json builder (matches OpenCode's exact schema)
# ---------------------------------------------------------------------------

def build_mcp_auth_json(
    *,
    access_token: str,
    refresh_token: str | None,
    expires_at_ts: int | None,
    scope: str,
    client_id: str,
    client_id_issued_at: int,
    server_url: str,
) -> str:
    """Build the JSON string that OpenCode stores in mcp-auth.json.

    Schema mirrors OpenCode's McpAuth Entry type (TypeScript):
      { "atlassian-rovo": { serverUrl, clientInfo, tokens } }
    """
    tokens: dict = {"accessToken": access_token, "scope": scope}
    if refresh_token:
        tokens["refreshToken"] = refresh_token
    if expires_at_ts is not None:
        tokens["expiresAt"] = expires_at_ts

    entry = {
        "serverUrl": server_url,
        "clientInfo": {
            "clientId": client_id,
            "clientIdIssuedAt": client_id_issued_at,
        },
        "tokens": tokens,
    }
    return json.dumps({"atlassian-rovo": entry}, indent=2)


def _build_mcp_auth_json_from_token(token: AtlassianToken) -> str:
    """Build mcp-auth.json content from a stored AtlassianToken model."""
    expires_at_ts = None
    if token.expires_at:
        exp = (
            token.expires_at
            if token.expires_at.tzinfo
            else token.expires_at.replace(tzinfo=timezone.utc)
        )
        expires_at_ts = int(exp.timestamp())

    return build_mcp_auth_json(
        access_token=token.access_token,
        refresh_token=token.refresh_token or None,
        expires_at_ts=expires_at_ts,
        scope=token.scopes,
        client_id=token.client_id,
        client_id_issued_at=token.client_id_issued_at,
        server_url=_MCP_SERVER_URL,
    )


# ---------------------------------------------------------------------------
# Redirect URI helper
# ---------------------------------------------------------------------------

def _redirect_uri(request: Request, ws_id: int) -> str:
    """Build the per-workspace OAuth callback URL."""
    base = str(request.base_url).rstrip("/")
    return f"{base}/workspaces/{ws_id}/atlassian/callback"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get(
    "/workspaces/{ws_id}/atlassian/authorize",
    dependencies=[Depends(require_auth)],
)
async def atlassian_authorize(
    ws_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Start the Atlassian OAuth flow.

    If no client_id exists for this workspace, perform dynamic client
    registration first, then redirect to the authorization URL.
    """
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)

    redirect_uri = _redirect_uri(request, ws_id)

    # Fetch or create the token row for this workspace
    result = await db.execute(
        select(AtlassianToken).where(AtlassianToken.workspace_id == ws_id)
    )
    token_row = result.scalar_one_or_none()

    # Determine client_id — register dynamically if not yet stored
    if token_row and token_row.client_id_enc:
        client_id = token_row.client_id
        client_id_issued_at = token_row.client_id_issued_at
    else:
        try:
            client_id, client_id_issued_at = await register_oauth_client(
                redirect_uri=redirect_uri
            )
        except Exception as exc:
            log.error("Atlassian dynamic client registration failed: %s", exc)
            flash(request, f"Failed to register with Atlassian: {exc}", "danger")
            return RedirectResponse(
                f"/workspaces/{ws_id}/secrets?tab=credentials",
                status_code=HTTP_303_SEE_OTHER,
            )

        if token_row is None:
            token_row = AtlassianToken(workspace_id=ws_id)
            db.add(token_row)
        token_row.client_id = client_id
        token_row.client_id_issued_at = client_id_issued_at
        await db.commit()

    # Generate PKCE pair and state
    verifier = generate_code_verifier()
    challenge = compute_code_challenge(verifier)
    state = secrets.token_urlsafe(16)

    # Persist verifier + state in the HTTP session (CSRF protection)
    request.session["atlassian_oauth_state"] = state
    request.session["atlassian_code_verifier"] = verifier
    request.session["atlassian_ws_id"] = ws_id

    auth_url = build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=challenge,
    )
    return RedirectResponse(auth_url, status_code=HTTP_303_SEE_OTHER)


@router.get(
    "/workspaces/{ws_id}/atlassian/callback",
    dependencies=[Depends(require_auth)],
)
async def atlassian_callback(
    ws_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle the OAuth callback from Atlassian.

    Validates the CSRF state, exchanges the authorization code for tokens,
    and stores them encrypted in the database.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        flash(request, f"Atlassian authorization denied: {error}", "danger")
        return RedirectResponse(
            f"/workspaces/{ws_id}/secrets?tab=credentials",
            status_code=HTTP_303_SEE_OTHER,
        )

    expected_state = request.session.pop("atlassian_oauth_state", None)
    verifier = request.session.pop("atlassian_code_verifier", None)

    if not expected_state or state != expected_state:
        flash(request, "Invalid OAuth state. Please try connecting again.", "danger")
        return RedirectResponse(
            f"/workspaces/{ws_id}/secrets?tab=credentials",
            status_code=HTTP_303_SEE_OTHER,
        )

    if not code:
        flash(request, "No authorization code received from Atlassian.", "danger")
        return RedirectResponse(
            f"/workspaces/{ws_id}/secrets?tab=credentials",
            status_code=HTTP_303_SEE_OTHER,
        )

    # Look up the stored client_id for this workspace
    result = await db.execute(
        select(AtlassianToken).where(AtlassianToken.workspace_id == ws_id)
    )
    token_row = result.scalar_one_or_none()
    if token_row is None or not token_row.client_id_enc:
        flash(request, "OAuth client not found. Please try connecting again.", "danger")
        return RedirectResponse(
            f"/workspaces/{ws_id}/secrets?tab=credentials",
            status_code=HTTP_303_SEE_OTHER,
        )

    redirect_uri = _redirect_uri(request, ws_id)

    try:
        token_data = await exchange_code_for_tokens(
            code=code,
            client_id=token_row.client_id,
            redirect_uri=redirect_uri,
            code_verifier=verifier or "",
        )
    except Exception as exc:
        log.error("Atlassian token exchange failed for workspace %d: %s", ws_id, exc)
        flash(request, f"Token exchange with Atlassian failed: {exc}", "danger")
        return RedirectResponse(
            f"/workspaces/{ws_id}/secrets?tab=credentials",
            status_code=HTTP_303_SEE_OTHER,
        )

    # Store tokens
    token_row.access_token = token_data["access_token"]
    if token_data.get("refresh_token"):
        token_row.refresh_token = token_data["refresh_token"]
    expires_in = token_data.get("expires_in", 3600)
    token_row.expires_at = datetime.fromtimestamp(
        time.time() + expires_in, tz=timezone.utc
    )
    if token_data.get("scope"):
        token_row.scopes = token_data["scope"]

    await db.commit()

    log.info(
        "Atlassian Rovo MCP OAuth connected for workspace %d (expires_in=%ds)",
        ws_id, expires_in,
    )
    flash(request, "Atlassian Rovo MCP connected successfully.", "success")
    return RedirectResponse(
        f"/workspaces/{ws_id}/secrets?tab=credentials",
        status_code=HTTP_303_SEE_OTHER,
    )


@router.post(
    "/workspaces/{ws_id}/atlassian/disconnect",
    dependencies=[Depends(require_auth)],
)
async def atlassian_disconnect(
    ws_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Remove the stored Atlassian token for this workspace."""
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)

    result = await db.execute(
        select(AtlassianToken).where(AtlassianToken.workspace_id == ws_id)
    )
    token_row = result.scalar_one_or_none()
    if token_row:
        await db.delete(token_row)
        await db.commit()

    # Best-effort: remove the K8s Secret if it exists
    try:
        from swarmer import k8s
        k8s.delete_atlassian_token_secret(ws.k8s_namespace, ws_id)
    except Exception as exc:
        log.warning("Could not delete Atlassian K8s secret for ws %d: %s", ws_id, exc)

    flash(request, "Atlassian Rovo MCP disconnected.", "success")
    return RedirectResponse(
        f"/workspaces/{ws_id}/secrets?tab=credentials",
        status_code=HTTP_303_SEE_OTHER,
    )

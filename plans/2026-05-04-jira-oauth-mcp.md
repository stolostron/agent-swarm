# Plan: Atlassian Rovo MCP Server Integration for Agent Sessions

**Date:** 2026-05-04  
**Status:** Planned

---

## Goal

Allow agent sessions to access Jira, Confluence, and Compass via the **official Atlassian Rovo MCP Server** (`https://mcp.atlassian.com/v1/mcp/authv2`) using OAuth 2.1. The access token must only exist for the lifetime of the pod — it is never stored persistently in the database. The user authenticates via a browser-based OAuth consent screen before launching the session; the resulting bearer token is injected as a short-lived Kubernetes Secret that is deleted when the session stops or is deleted.

---

## Background & Context

### The official Atlassian Rovo MCP Server

The Atlassian Rovo MCP Server is Atlassian's own hosted, cloud-based MCP endpoint. It is **not** a package you install — it is a remote HTTP server that Atlassian operates.

- **Endpoint**: `https://mcp.atlassian.com/v1/mcp/authv2`
- **Transport**: Streamable HTTP (the `/v1/sse` SSE endpoint is deprecated after **June 30, 2026**)
- **Auth**: OAuth 2.1 (primary); API token (optional, if enabled by org admin)
- **Capabilities**: Jira, Confluence, Compass — search, create, update, cross-reference
- **No app registration required**: The server uses OAuth Dynamic Client Registration (DCR); the client registers itself automatically. No manual Atlassian Developer Console setup needed.
- **Docs**: `https://support.atlassian.com/atlassian-rovo-mcp-server/`

### How OAuth 2.1 works with this server

The MCP client initiates an OAuth 2.1 authorization code flow. The user is redirected to Atlassian's consent screen, grants access, and the client receives a bearer `access_token`. All subsequent MCP requests use:

```
Authorization: Bearer <access_token>
```

The server validates the token, determines which Atlassian apps (Jira, Confluence, etc.) and cloud site (`cloudId`) the user has consented for, and forwards requests accordingly. Tokens are scoped per-user and per-site.

### The `mcp-remote` proxy (for stdio clients)

opencode speaks MCP over **stdio** (local subprocess). The Rovo MCP Server speaks **streamable HTTP**. The bridge is `mcp-remote` (npm package `mcp-remote`, by `geelen`, MIT licensed — **not** `@atlassian/mcp-remote` which does not exist).

```
opencode → mcp-remote (stdio↔HTTP bridge, OAuth handler) → https://mcp.atlassian.com/v1/mcp/authv2
```

`mcp-remote` handles:
- Translating stdio MCP messages to HTTP
- Initiating the OAuth 2.1 browser flow on first connection
- Caching the resulting token locally (in `~/.mcp-remote/` or equivalent)

### The core problem for pod-based agents

The OAuth flow requires a **browser redirect**. An agent pod has no browser. The redirect must happen **before the pod launches**, in the swarmer dashboard itself. Swarmer intercepts the OAuth callback, captures the `access_token`, and injects it directly into the pod — bypassing the need for `mcp-remote`'s interactive flow inside the pod.

Inside the pod, opencode is configured with the Rovo MCP Server using the pre-obtained bearer token directly in the `Authorization` header, with no OAuth dance needed at pod startup.

### Why not store the token?

The user explicitly does not want Atlassian credentials persisted beyond the session. The token is held only in the Starlette HTTP session (in-memory) between the OAuth callback and the pod launch, then in a K8s Secret for the life of the pod. It is never written to SQLite.

---

## User Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Token storage between Connect and Launch | Starlette HTTP session (in-memory) | Never touches disk or DB; lost on swarmer restart (acceptable) |
| Refresh token in pod | Access token only | Token lasts long enough for typical sessions; avoids passing a longer-lived credential |
| OAuth callback URL | Configured via `SWARMER_PUBLIC_URL` setting | Works behind OpenShift Routes; user sets it once at deploy time |
| `mcp-remote` inside the pod | Not used | Token is pre-obtained by swarmer; pod uses the bearer token directly in `opencode.json` |
| Node.js in agent image | Install at pod startup via `nvm`/`curl` if not present, OR use `mcp-remote` only in swarmer dashboard | Node.js is NOT required inside the pod when using direct bearer token auth |

---

## Architecture Overview

```text
                    ┌─────────────────────────────────────────────────┐
                    │  Swarmer Dashboard (browser)                    │
                    │                                                  │
                    │  Session Detail → [Connect Atlassian]           │
                    │  ↓ GET /workspaces/{ws_id}/atlassian-oauth/start│
                    │    builds OAuth 2.1 authorization URL           │
                    │    stores CSRF state in HTTP session             │
                    │    redirects browser →                          │
                    └──────────────────┬──────────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────────┐
                    │  Atlassian OAuth 2.1 Consent Screen             │
                    │  (auth.atlassian.com)                           │
                    │  User logs in, grants Jira/Confluence access    │
                    └──────────────────┬──────────────────────────────┘
                                       │ redirect with auth code
                    ┌──────────────────▼──────────────────────────────┐
                    │  GET /workspaces/{ws_id}/atlassian-oauth/callback│
                    │  Swarmer exchanges code → access_token          │
                    │  Stores token in Starlette HTTP session         │
                    │  Redirects back to session detail page          │
                    └──────────────────┬──────────────────────────────┘
                                       │ user clicks Launch
                    ┌──────────────────▼──────────────────────────────┐
                    │  _do_launch()                                   │
                    │  Reads access_token from HTTP session           │
                    │  Creates K8s Secret: atlassian-oauth-{sid}      │
                    │    ATLASSIAN_MCP_TOKEN = <access_token>         │
                    │  Sets has_atlassian_oauth=True                  │
                    └──────────────────┬──────────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────────┐
                    │  Agent Pod                                      │
                    │                                                  │
                    │  opencode.json mcpServers:                      │
                    │    atlassian-rovo:                              │
                    │      url: https://mcp.atlassian.com/v1/mcp/authv2│
                    │      headers:                                   │
                    │        Authorization: Bearer ${ATLASSIAN_MCP_TOKEN}│
                    │                                                  │
                    │  envFrom: atlassian-oauth-{session_id}          │
                    │    ATLASSIAN_MCP_TOKEN = <access_token>         │
                    └──────────────────┬──────────────────────────────┘
                                       │ session stopped/deleted
                                       ▼
                    K8s Secret atlassian-oauth-{session_id} deleted
```

---

## Implementation Plan

### Step 1 — New DB model: `AtlassianOAuthApp`

**File:** `swarmer/models/atlassian_oauth_app.py` _(new)_

The Rovo MCP Server uses OAuth Dynamic Client Registration — **no client ID/secret is needed from the user**. The only per-workspace configuration required is:
- Which Atlassian site to connect to (the `cloudId` is resolved at token exchange time)
- The redirect URI (derived from `SWARMER_PUBLIC_URL`)
- Optionally, a note/label so the user knows which site is configured

```python
class AtlassianOAuthApp(Base):
    __tablename__ = "atlassian_oauth_apps"

    id: Mapped[int]              # PK
    workspace_id: Mapped[int]    # FK → workspaces.id (unique)
    site_url: Mapped[str]        # e.g. https://yourorg.atlassian.net (display only)
    redirect_uri: Mapped[str]    # derived from SWARMER_PUBLIC_URL at save time
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

No encryption needed — there are no secrets stored here. The Rovo MCP Server's DCR means the OAuth client registers itself dynamically; swarmer needs only the redirect URI to be registered.

**File:** `swarmer/models/__init__.py`

Add: `from swarmer.models.atlassian_oauth_app import AtlassianOAuthApp  # noqa: F401`

No migration needed — new table created by `Base.metadata.create_all`.

---

### Step 2 — Config: `SWARMER_PUBLIC_URL`

**File:** `swarmer/config.py`

Add:
```python
swarmer_public_url: str = ""
```

The externally reachable base URL of the swarmer dashboard (e.g. `https://swarmer.apps.mycluster.example.com`). Used to construct the OAuth redirect URI:
```
{swarmer_public_url}/workspaces/{ws_id}/atlassian-oauth/callback
```

When blank, fall back to deriving from `str(request.base_url).rstrip("/")` (works for local dev).

---

### Step 3 — Secrets UI: "Atlassian" tab

**File:** `swarmer/routers/secrets.py`

Add three new routes:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/workspaces/{ws_id}/secrets` (existing) | Add `atlassian_oauth_app` to template context |
| `POST` | `/workspaces/{ws_id}/secrets/atlassian-oauth` | Create or update `AtlassianOAuthApp` |
| `POST` | `/workspaces/{ws_id}/secrets/atlassian-oauth/delete` | Delete `AtlassianOAuthApp` |

**File:** `swarmer/templates/secrets/_atlassian_oauth_form.html` _(new)_

Simple form consistent with other secrets tabs:

```text
┌─────────────────────────────────────────────────────┐
│  Atlassian Rovo MCP                                 │
│                                                     │
│  Atlassian Site URL                                 │
│  [https://yourorg.atlassian.net               ]     │
│  (Used for display only — no credentials stored)   │
│                                                     │
│  OAuth Redirect URI  (read-only, auto-generated)   │
│  https://swarmer.example.com/workspaces/1/          │
│      atlassian-oauth/callback                       │
│                                                     │
│  Note: No client ID or secret required. Atlassian   │
│  uses Dynamic Client Registration automatically.    │
│  A site admin must complete the OAuth flow once     │
│  before other users can connect.                    │
│                                                     │
│  [Save]  [Delete]                                   │
└─────────────────────────────────────────────────────┘
```

**Secrets tabs template** — add "Atlassian" tab alongside "OpenCode", "GitHub PATs", "Pull Secret".

---

### Step 4 — OAuth routes: `atlassian_oauth.py`

**File:** `swarmer/routers/atlassian_oauth.py` _(new)_

The Rovo MCP Server supports OAuth 2.1 with Dynamic Client Registration (RFC 7591). The flow is:

1. Discover the authorization server metadata from `https://mcp.atlassian.com/v1/mcp/authv2/.well-known/oauth-authorization-server` (or equivalent MCP discovery endpoint)
2. Register swarmer as an OAuth client dynamically via DCR POST
3. Redirect the user to the authorization URL
4. Exchange the code for an `access_token` at the callback

Two routes, both protected by `require_auth`.

#### `GET /workspaces/{ws_id}/atlassian-oauth/start`

Query params:
- `return_session` — session ID to redirect back to after OAuth completes

Logic:
1. Load `AtlassianOAuthApp` for `ws_id`; 404 if not configured.
2. Discover OAuth metadata from the Rovo MCP Server's well-known endpoint.
3. Perform DCR: POST to the registration endpoint with:
   ```json
   {
     "redirect_uris": ["{redirect_uri}"],
     "client_name": "Swarmer",
     "grant_types": ["authorization_code"],
     "response_types": ["code"],
     "token_endpoint_auth_method": "none"
   }
   ```
   Store the returned `client_id` in `request.session["atlassian_oauth_state"]`.
4. Generate CSRF `state` token (16 hex bytes).
5. Store `{"state": state, "client_id": client_id, "ws_id": ws_id, "return_session": sid}` in `request.session["atlassian_oauth_state"]`.
6. Build and redirect to the authorization URL with `response_type=code`, `client_id`, `redirect_uri`, `state`, `code_challenge` (PKCE S256).

#### `GET /workspaces/{ws_id}/atlassian-oauth/callback`

Query params: `code`, `state`

Logic:
1. Validate `state` against `request.session["atlassian_oauth_state"]`; abort with flash error on mismatch.
2. Exchange `code` for `access_token` via POST to the token endpoint, including PKCE `code_verifier`.
3. Store in `request.session[f"atlassian_oauth_{ws_id}"]`:
   ```json
   {
     "access_token": "...",
     "expires_at": "<unix timestamp>"
   }
   ```
4. Clear `atlassian_oauth_state` from session.
5. Redirect to `/workspaces/{ws_id}/sessions/{return_session}`.

**Error handling:** On any failure (CSRF mismatch, token exchange error, DCR failure), redirect back to the session detail page with a flash error. Never expose raw error details to the template.

**File:** `swarmer/main.py`

```python
from swarmer.routers import atlassian_oauth
app.include_router(atlassian_oauth.router)
```

---

### Step 5 — Session detail UI: "Connect Atlassian" button

**File:** `swarmer/templates/sessions/detail.html`

Add a "Connect Atlassian" button near the Launch button. Only shown when an `AtlassianOAuthApp` is configured for the workspace.

| Condition | Button appearance |
|-----------|------------------|
| No `AtlassianOAuthApp` configured | Button not shown |
| Configured, no token in session | `[Connect Atlassian]` (outline style) |
| Token present in session | `[Atlassian Connected ✓]` (success style, still clickable to re-auth) |

The button is a plain `<a>` linking to `/workspaces/{ws_id}/atlassian-oauth/start?return_session={sid}` — full browser redirect, no HTMX.

**Route context changes in `session_detail()`:**

- Load `atlassian_oauth_app` from DB
- Pass `atlassian_connected = f"atlassian_oauth_{ws_id}" in request.session` to the template

---

### Step 6 — `_do_launch()`: create ephemeral K8s Secret

**File:** `swarmer/routers/sessions.py`

Signature change:
```python
async def _do_launch(
    session: Session,
    ws: Workspace,
    db: AsyncSession,
    request: Request | None = None,  # None when called from scheduler
) -> None:
```

After PVC/SCC setup, before `build_session_pod()`:

```python
has_atlassian_oauth = False
if request is not None:
    token_data = request.session.get(f"atlassian_oauth_{session.workspace_id}")
    if token_data:
        await asyncio.to_thread(
            k8s.apply_atlassian_oauth_secret,
            ws.k8s_namespace,
            session.id,
            access_token=token_data["access_token"],
        )
        has_atlassian_oauth = True
```

Pass `has_atlassian_oauth` through to `build_session_pod()`.

Scheduler-triggered sessions will have `request=None` → `has_atlassian_oauth=False` → pod starts without Atlassian MCP. Acceptable; scheduled sessions are headless.

---

### Step 7 — `k8s.py`: `apply_atlassian_oauth_secret` and `delete_atlassian_oauth_secret`

**File:** `swarmer/k8s.py`

Following the existing `_apply_secret()` pattern:

```python
def apply_atlassian_oauth_secret(
    namespace: str,
    session_id: int,
    *,
    access_token: str,
) -> None:
    """Create or replace the ephemeral Atlassian OAuth K8s Secret for a session."""

def delete_atlassian_oauth_secret(namespace: str, session_id: int) -> None:
    """Delete the ephemeral Atlassian OAuth K8s Secret. No-op if not found."""
```

Secret name: `atlassian-oauth-{session_id}`

Secret keys:

| Key | Value |
|-----|-------|
| `ATLASSIAN_MCP_TOKEN` | `access_token` |

That's the only key needed. The MCP server URL is static and goes in the ConfigMap, not the Secret.

---

### Step 8 — `k8s_session.py`: pod spec changes

**File:** `swarmer/k8s_session.py`

Add `has_atlassian_oauth: bool = False` parameter to `build_session_pod()`.

When `has_atlassian_oauth=True`, add an additional `envFrom`:

```python
client.V1EnvFromSource(
    secret_ref=client.V1SecretEnvSource(
        name=f"atlassian-oauth-{session.id}",
        optional=False,
    )
)
```

---

### Step 9 — `opencode.py`: MCP config

**File:** `swarmer/agent_tools/opencode.py`

#### `build_config_data(secret=None, has_atlassian_oauth=False)`

When `has_atlassian_oauth=True`, add `mcpServers` to the generated `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "disabled_providers": ["opencode"],
  "server": { "hostname": "0.0.0.0", "port": 4096 },
  "mcpServers": {
    "atlassian-rovo": {
      "type": "http",
      "url": "https://mcp.atlassian.com/v1/mcp/authv2",
      "headers": {
        "Authorization": "Bearer ${ATLASSIAN_MCP_TOKEN}"
      }
    }
  }
}
```

The `ATLASSIAN_MCP_TOKEN` env var is injected via `envFrom` on the pod from the ephemeral K8s Secret. No OAuth dance occurs inside the pod — the token is already valid.

**Note on env var interpolation in `opencode.json`:** opencode must support `${VAR}` interpolation in header values for this to work. If it does not, the token must be written to the config file at pod startup as a shell step instead (see Step 9b below).

#### Step 9b — fallback: write token to config at startup

If opencode does not interpolate env vars in config headers, add a step to `build_share_setup_cmd()` that rewrites the `mcpServers` section with the literal token value at startup:

```sh
# Rewrite the MCP token into opencode.json at startup
python3 -c "
import json, os
cfg_path = '/workspace/.config/opencode/opencode.json'
with open(cfg_path) as f: cfg = json.load(f)
cfg.setdefault('mcpServers', {})['atlassian-rovo'] = {
    'type': 'http',
    'url': 'https://mcp.atlassian.com/v1/mcp/authv2',
    'headers': {'Authorization': 'Bearer ' + os.environ['ATLASSIAN_MCP_TOKEN']}
}
with open(cfg_path, 'w') as f: json.dump(cfg, f, indent=2)
" && 
```

This writes the literal token into the config file before opencode starts. The file lives on the PVC but the token value is the same short-lived one from the K8s Secret.

**Method signature changes:**

```python
def build_config_data(self, secret=None, has_atlassian_oauth: bool = False) -> dict[str, str]:
def build_share_setup_cmd(self, has_atlassian_oauth: bool = False) -> str:
```

---

### Step 10 — Session stop and delete: clean up the K8s Secret

**File:** `swarmer/routers/sessions.py`

In both `session_stop` and `session_delete`:

```python
await asyncio.to_thread(
    k8s.delete_atlassian_oauth_secret, ws.k8s_namespace, session.id
)
```

Runs after `k8s.delete_pod()`. No-op if the secret doesn't exist.

---

## File Change Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `swarmer/models/atlassian_oauth_app.py` | **New** | `AtlassianOAuthApp` model: workspace-scoped Atlassian site config (site URL + redirect URI, no secrets) |
| `swarmer/models/__init__.py` | Edit | Import `AtlassianOAuthApp` |
| `swarmer/config.py` | Edit | Add `swarmer_public_url: str = ""` |
| `swarmer/routers/atlassian_oauth.py` | **New** | `/start` (DCR + OAuth redirect) and `/callback` (code exchange, token stored in HTTP session) |
| `swarmer/main.py` | Edit | Register `atlassian_oauth` router |
| `swarmer/routers/secrets.py` | Edit | "Atlassian" tab CRUD for `AtlassianOAuthApp` |
| `swarmer/templates/secrets/_atlassian_oauth_form.html` | **New** | Atlassian site URL form + read-only redirect URI display |
| `swarmer/templates/secrets/tabs.html` (or equivalent) | Edit | Add "Atlassian" tab |
| `swarmer/templates/sessions/detail.html` | Edit | "Connect Atlassian" / "Atlassian Connected ✓" button; `atlassian_connected` context var |
| `swarmer/routers/sessions.py` | Edit | `_do_launch()` creates K8s Secret if token present; stop/delete clean up secret; `session_detail()` passes `atlassian_oauth_app` + `atlassian_connected` |
| `swarmer/k8s.py` | Edit | `apply_atlassian_oauth_secret()` and `delete_atlassian_oauth_secret()` |
| `swarmer/k8s_session.py` | Edit | `has_atlassian_oauth` param; `envFrom` for `atlassian-oauth-{session_id}` |
| `swarmer/agent_tools/opencode.py` | Edit | `build_config_data()` adds `mcpServers.atlassian-rovo` when `has_atlassian_oauth=True`; optional token-write step in `build_share_setup_cmd()` |

---

## Data Flow

```text
[Workspace Secrets → Atlassian tab]
    └─ POST /workspaces/{ws_id}/secrets/atlassian-oauth
           Saves AtlassianOAuthApp (site_url, redirect_uri) to SQLite
           No secrets stored — DCR handles client registration at flow time

[Session Detail → "Connect Atlassian"]
    └─ GET /workspaces/{ws_id}/atlassian-oauth/start?return_session={sid}
           Discovers OAuth metadata from https://mcp.atlassian.com/v1/mcp/authv2
           Performs Dynamic Client Registration (POST to DCR endpoint)
           Generates PKCE code_verifier + code_challenge
           Stores state + client_id + code_verifier in Starlette HTTP session
           Redirects browser → Atlassian authorization URL

           User logs in and grants access on Atlassian consent screen

    └─ GET /workspaces/{ws_id}/atlassian-oauth/callback?code=...&state=...
           Validates CSRF state
           Exchanges code + code_verifier for access_token
           Stores {access_token, expires_at} in request.session["atlassian_oauth_{ws_id}"]
           Redirects → /workspaces/{ws_id}/sessions/{sid}
           UI shows "Atlassian Connected ✓"

[User clicks Launch]
    └─ POST /workspaces/{ws_id}/sessions/{sid}/launch
           _do_launch():
             reads atlassian_oauth_{ws_id} from HTTP session
             calls k8s.apply_atlassian_oauth_secret()
               → K8s Secret "atlassian-oauth-{session_id}" with ATLASSIAN_MCP_TOKEN
             calls k8s_sess.build_session_pod(has_atlassian_oauth=True)
               → envFrom: atlassian-oauth-{session_id}
               → opencode.json: mcpServers.atlassian-rovo with bearer token header
             creates pod

[Pod running]
    opencode reads mcpServers config
    Makes HTTP requests to https://mcp.atlassian.com/v1/mcp/authv2
    with Authorization: Bearer <access_token>
    → Atlassian Rovo MCP Server handles Jira/Confluence/Compass queries

[Session stopped or deleted]
    k8s.delete_pod()
    k8s.delete_atlassian_oauth_secret()  ← "atlassian-oauth-{session_id}" gone
```

---

## Prerequisites for the User

1. **Atlassian Cloud site** with Jira and/or Confluence (Standard, Premium, or Enterprise plan — Rovo required for MCP access).

2. **Site admin first-run**: The first user to complete the OAuth consent flow for a site must be a site admin. This installs the Atlassian MCP App on the site via lazy/just-in-time registration. Subsequent users do not need admin access.

3. **Organization admin**: May need to allow the swarmer domain in **Atlassian Administration → Rovo MCP Server settings → Allowed domains** if the organization restricts which external domains can connect.

4. **Configure the Atlassian site URL** in the workspace Secrets page so swarmer knows the redirect URI to generate.

5. **`SWARMER_PUBLIC_URL`** set in the swarmer deployment so the redirect URI is reachable by Atlassian's authorization server.

---

## Edge Cases & Considerations

| Scenario | Handling |
|----------|---------|
| Launch without clicking "Connect Atlassian" | `has_atlassian_oauth=False`; no `mcpServers` in config; no K8s Secret; pod starts normally |
| Scheduler triggers a session | `request=None`; `has_atlassian_oauth=False`; MCP not available in scheduled sessions |
| Swarmer restarts between Connect and Launch | Starlette HTTP session lost; "Atlassian Connected ✓" reverts; user re-authenticates |
| Access token expires mid-session | Rovo MCP tokens are session-scoped; MCP calls will fail after expiry. For long TUI/server sessions, user stops, re-authenticates, and relaunches. |
| Site admin hasn't done first-run | Atlassian returns "Your site admin must authorize this app"; swarmer flashes this error |
| Organization blocks domain | Atlassian returns "Your organization admin must authorize access from this domain"; swarmer flashes this error |
| opencode doesn't interpolate `${VAR}` in headers | Use the Step 9b fallback: write the literal token into `opencode.json` via a Python one-liner in the startup command |
| Multiple sessions in same workspace | Each gets its own `atlassian-oauth-{session_id}` K8s Secret; independent lifecycles |
| Stop fails / pod already gone | `delete_atlassian_oauth_secret()` catches 404 — consistent with `delete_pod()` behavior |
| `SWARMER_PUBLIC_URL` not configured | Falls back to `str(request.base_url).rstrip("/")` at redirect-URI construction time |
| Atlassian consent denied | Callback receives `error=access_denied`; swarmer flashes error and redirects to session detail |
| CSRF state mismatch | Flash error; no token stored; redirect to session detail |

---

## Out of Scope

- API token authentication mode (the Rovo MCP Server also supports API tokens if enabled by org admin; this plan covers OAuth only — the simpler path for "token only lives for the session").
- Compass support (the MCP server supports it; no additional swarmer changes needed beyond the token injection — tools are enabled by the OAuth consent scopes).
- Crush agent tool support (only OpenCode's config is modified; Crush does not use `opencode.json`).
- Token refresh inside the pod (access token only; no refresh token injected).
- Multi-user swarmer (single-user by design; one OAuth session per browser session).

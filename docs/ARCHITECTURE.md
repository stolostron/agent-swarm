# Swarmer — Architecture Reference

## Project Structure

```
agent-swarm/
├── Makefile                    # All build/deploy/dev commands
├── Containerfile               # UBI10 python-312-minimal, runs uvicorn on port 8080
├── Containerfile.crush         # UBI9 minimal + Crush CLI (sleep infinity)
├── requirements.txt            # Pinned minimum versions
├── VERSION                     # Semver used as image tag
├── .env.example                # Copy to .env for local dev
├── k8s/                        # Kubernetes manifests
│   ├── kind-config.yaml
│   ├── swarmer/                # Deployment, Service, RBAC, PVC, Namespace
│   └── openshift/              # OpenShift-specific (Route, OAuthClient, Deployment)
├── kustomize/                  # Declarative Kustomize overlays
│   ├── base/common/            # Shared Deployment, PVC, SA
│   ├── base/cluster-admin/     # Full multi-namespace + OAuthClient
│   └── base/namespace-scoped/  # Single-namespace, no cluster-admin
├── docs/                       # Documentation
│   ├── USER_GUIDE.md           # Full user-facing guide
│   └── ARCHITECTURE.md         # This file
├── mcp-server/                 # Standalone MCP server for session orchestration
├── tests/                      # Test suite
│   ├── test_api.py              # REST API unit tests (in-memory SQLite, no server)
│   ├── test_list_repos_for_pat.py  # GitHub API helpers (respx mocking)
│   └── test_ui_patternfly.py   # Playwright e2e tests (requires running server at :8091)
└── swarmer/                    # Python package (the application)
    ├── main.py                 # FastAPI app, lifespan, middleware, router registration
    ├── config.py               # pydantic-settings Settings singleton
    ├── database.py             # SQLAlchemy async engine + session factory + migrations
    ├── crypto.py               # Fernet encrypt/decrypt from secret key file or env var
    ├── k8s_auth.py             # K8s TokenReview validation, namespace access check, RBAC probing
    ├── deps.py                 # FastAPI dependencies (require_auth, get_user_token)
    ├── k8s.py                  # Kubernetes utility functions (namespace, secret, pod, configmap, route)
    ├── k8s_session.py          # Session-specific K8s ops (PVC, pod spec, service)
    ├── mcp_catalog.py          # Registry of well-known MCP servers (Jira, etc.) with OAuth defaults
    ├── scheduler.py            # Background asyncio cron scheduler for prompt-mode sessions
    ├── log_poller.py           # Background pod log poller with auto-cleanup
    ├── agent_tools/            # Strategy pattern for multi-agent support
    │   ├── __init__.py         # AgentToolStrategy ABC (18 abstract methods)
    │   ├── registry.py         # Global registry + aliases (_init() auto-registers all tools)
    │   ├── opencode.py         # OpenCode strategy (Vertex AI Anthropic/Gemini models)
    │   └── crush.py            # Crush strategy (Vertex AI, Anthropic, OpenAI, Gemini models)
    ├── models/                 # SQLAlchemy ORM models
    │   ├── __init__.py         # Imports all models (required for Base.metadata)
    │   ├── workspace.py        # Workspace → 1:1 K8s namespace (or shared via settings.k8s_namespace)
    │   ├── session.py          # Session (pod lifecycle, modes: tui/server/prompt, cron scheduling)
    │   ├── session_repo.py     # Git repos attached to sessions (cloned by init container)
    │   ├── opencode_secret.py  # Fernet-encrypted provider credentials (GCP/Anthropic/OpenAI/Gemini)
    │   ├── github_pat.py       # Fernet-encrypted GitHub PATs for HTTPS git auth
    │   └── mcp_server.py       # MCP server configs with Fernet-encrypted OAuth tokens
    ├── routers/                # FastAPI route handlers
    │   ├── auth.py             # /login (token paste + OpenShift OAuth), /logout, /auth/callback
    │   ├── workspaces.py       # CRUD for workspaces
    │   ├── sessions.py         # CRUD + launch/stop/schedule/patch generation + repo management
    │   ├── secrets.py          # OpenCode secrets, GitHub PATs, pull secrets
    │   ├── mcp_servers.py      # MCP server CRUD, OAuth 2.1 flow (PKCE + dynamic registration)
    │   ├── chat_proxy.py       # HTTP/SSE/WebSocket reverse proxy for server-mode sessions
    │   └── tui_ws.py           # WebSocket PTY proxy for TUI-mode sessions (K8s exec)
    ├── api/v1/                 # REST API — 51 endpoints under /api/v1/
    └── templates/              # Jinja2 HTML templates (PatternFly 6 dark theme + HTMX)
        ├── base.html           # Layout with masthead, flash messages, PatternFly CDN
        ├── workspaces/         # list, detail, new, edit, _delete_confirm
        ├── sessions/           # list, detail, new, _status_badge, _last_output, _repo_list, crush_chat, etc.
        ├── secrets/            # tabs, opencode_form, github_pat_form, github_pat_list
        └── mcp_servers/        # list (catalog + configured servers with OAuth status)
```

## Domain Model

- **Workspace** maps to a Kubernetes namespace. All resources (sessions, secrets) are scoped to a workspace. When `settings.k8s_namespace` is set (namespace-scoped deployment), all workspaces share a single K8s namespace via `Workspace.k8s_namespace` property.
- **Session** = an agent run. Each session creates a K8s Pod + PVC. Three modes:
  - `prompt` — one-shot: runs the agent with a prompt, pod exits on completion (`restartPolicy: Never`), pod + PVC auto-deleted on success if `persist=False`
  - `server` — persistent: runs the agent in server mode, creates a ClusterIP Service (+ OpenShift Route if available), dashboard proxies HTTP/WS/SSE to it
  - `tui` — persistent: runs `sleep infinity`, user connects via xterm.js WebSocket → K8s exec PTY
- **Session phases**: `idle` → `pending` → `running` → `succeeded`/`failed`/`stopped`
- **Cron scheduling** — prompt-mode sessions can have a cron schedule (`cron_schedule` field). A background asyncio loop (`scheduler.py`) checks every 30s, uses an atomic `UPDATE … RETURNING` to claim due rows (prevents duplicates), then calls the shared `_do_launch()` helper in `sessions.py`.
- **OpencodeSecret** — per-workspace encrypted storage for GCP project, Vertex location, ADC JSON, Google API key, Anthropic API key, OpenAI API key. Despite the legacy name, used by both OpenCode and Crush.
- **GitHubPAT** — per-workspace encrypted GitHub personal access tokens with optional org scope for HTTPS git auth
- **McpServer** — per-workspace MCP server configurations with OAuth 2.1 tokens encrypted at rest. Supports dynamic client registration, PKCE, and token refresh. Enabled servers are injected into agent configs and mounted as K8s secret env vars (`MCP_TOKEN_<SLUG>`). Pre-configured catalog includes Atlassian Jira (Rovo).
- **SessionRepo** — git repositories to clone into the session PVC via init containers

## Agent Tool Strategy Pattern

Multi-agent support uses the Strategy pattern (`agent_tools/`). Each tool (OpenCode, Crush) implements `AgentToolStrategy` with 18+ abstract methods covering:
- Image selection, config map generation, K8s secret layout
- Pod command construction for each session mode
- Model options/validation/selection
- Environment variables, volumes, volume mounts
- Container naming, server ports

The registry (`agent_tools/registry.py`) auto-initializes on import via `_init()`. Tool aliases map legacy names (e.g., `"opencode-golang"` → `"opencode"`).

**To add a new agent tool**: Create `swarmer/agent_tools/new_tool.py` implementing `AgentToolStrategy`, register it in `registry.py:_init()`, add the tool name to `AGENT_TOOLS` in `models/session.py`, and add a corresponding `AGENT_IMAGE_*` setting in `config.py`.

## Authentication

Token-based auth via Kubernetes bearer tokens (not password-based):
- Users paste a K8s ServiceAccount token into the login form
- Token validated via TokenReview API (`k8s_auth.py`); falls back to namespace probe if RBAC for tokenreviews is missing
- Validated token is Fernet-encrypted and stored in the session cookie (`deps.py:get_user_token()`)
- Workspace access controlled by K8s RBAC: `get_accessible_namespaces()` checks pods `list` in each workspace namespace (via namespace-scoped `swarmer-user` RoleBinding), or cluster-scoped `namespaces` `get` for admin bindings
- Optional OpenShift OAuth: implicit grant flow via `/auth/callback` (captures token from URL fragment client-side)
- `swarmer/auth.py` is superseded — just contains a comment pointing to `k8s_auth.py`

## Encryption

All sensitive fields (PATs, API keys, ADC credentials) are Fernet-encrypted at rest in SQLite.

- Key source (in priority order): `SWARMER_SECRET_KEY` env var → `auth/secret.key` file → auto-generated on first run
- Key must decode to exactly 32 bytes (base64url-encoded)
- Session cookie secret uses a separate derivation: `SHA256("session:" + raw_key)`
- `crypto.py` must be initialized via `init_crypto()` before any DB access (model property accessors call `decrypt()`)
- Encrypted fields use `_enc` suffix convention (e.g., `pat_enc`, `google_api_key_enc`)
- Transparent encrypt/decrypt via Python `@property` getters/setters on models
- Decryption failures (rotated key) return empty string with a warning log, not exceptions

## Database

- **SQLite** via `aiosqlite` + SQLAlchemy 2.x async (`AsyncSession`)
- Database file: `data/swarmer.db` (created automatically on first run)
- Schema created via `Base.metadata.create_all` — no Alembic
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent; only suppresses "duplicate column"/"already exists" errors, all others re-raise)
- All models must be imported in `models/__init__.py` for table registration to work
- SQLite single-writer: K8s Deployment uses `strategy: Recreate` (not RollingUpdate)

## Kubernetes Integration

- Uses the official `kubernetes` Python client, imported lazily inside functions (avoids import errors when K8s isn't configured)
- `k8s.init_k8s()` loads either in-cluster or kubeconfig based on `K8S_IN_CLUSTER` setting
- `effective_namespace()` in `k8s.py` returns `settings.k8s_namespace` if set, otherwise the workspace's own namespace — used in namespace-scoped deployments where all workspaces share one namespace
- Workspace creation → `ensure_namespace()` + `apply_agent_config()` (ConfigMap for each tool)
- Session launch → `ensure_session_pvc()` + `build_session_pod()` + pod creation
- Pod naming: `session-{session_id}-{random_hex_suffix}`; PVC naming: `session-{session_id}-{suffix}`
- Init container uses the agent's own image (not alpine/git) to clone configured repos
- OpenShift compatibility: `_grant_anyuid_scc()` creates a RoleBinding for the anyuid SCC; silently skips on non-OpenShift (404/403)
- OpenShift Routes: created automatically for server-mode sessions

## Background Tasks

Two background asyncio systems run during app lifespan:

1. **Log Poller** (`log_poller.py`) — Per-session tasks that poll pod status and logs every 5s. Saves phase/detail/output to DB. Auto-cleans up completed prompt-mode pods (deletes pod + PVC if `persist=False`). Restarted for in-flight sessions on app restart via `_restart_prompt_pollers()`.

2. **Cron Scheduler + Queue Processor** (`scheduler.py`) — Single global task that checks every 30s. Each cycle:
   - **Queue processor** (`_process_queue`): If the global concurrency cap is not reached, fetches sessions in `"queued"` phase ordered by `created_at` (FIFO) and launches them up to the available slot count. Applies a 2-minute in-memory cooldown when still at capacity to avoid tight retry loops.
   - **Cron launcher**: Claims prompt-mode sessions with a due `cron_next_run` via atomic `UPDATE … RETURNING`. Respects the concurrency cap — does not over-claim. On launch failure, resets phase to `idle` and advances `cron_next_run`.

### Concurrency Limiting

`MAX_CONCURRENT_AGENTS` (default 5, configurable via env var) caps the number of simultaneously running agent pods (sessions in `pending` or `running` phase). When this limit is reached:

- All new launches (manual, API, or scheduled) set `phase="queued"` and return immediately without creating a pod.
- The queue processor re-evaluates every 2 minutes and launches queued sessions as capacity frees up.
- Stopping a queued session (no pod exists) returns it directly to `"idle"` without any K8s cleanup.
- Setting `MAX_CONCURRENT_AGENTS=0` disables the limit entirely.

The sessions list shows a workspace-scoped capacity summary ("N active | N slots available | N queued") that refreshes every 3s via HTMX. Queued sessions show their global queue position ("Position N of M") on both the list and detail pages.

### Session Run History

Prompt-mode sessions record each completed execution (phase, timing, status detail, and pod logs) in the `session_runs` table. The History tab on the session detail page and `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/runs` expose this data.

`SESSION_RUN_HISTORY_LIMIT` (default 20) caps how many completed runs are retained per session. When a new run is recorded, the oldest entries beyond the limit are deleted. Set to `0` to disable pruning (unlimited history; may grow SQLite storage quickly on scheduled sessions).

## Chat Proxy

`chat_proxy.py` handles server-mode session access:
- **Crush sessions**: Renders a custom chat UI template (`crush_chat.html`); the JS makes API calls through the same `/chat/{path}` proxy
- **OpenCode sessions on OpenShift**: Redirects to the OpenShift Route hostname (direct browser access)
- **OpenCode sessions elsewhere**: Sub-path HTTP proxy with HTML path rewriting (`<base>` tag injection + asset path rewriting)
- SSE streams are proxied with no read timeout; WebSocket proxy via `websockets` library (bidirectional relay)

## TUI WebSocket Proxy

`tui_ws.py` provides browser-to-pod terminal access:
- One-time UUID auth tokens generated on session detail page, stored in HTTP session, consumed on connect
- Uses `kubernetes.stream` exec API (not kubectl subprocess)
- Background thread reads pod stdout/stderr into an asyncio Queue
- Supports terminal resize via channel 4 JSON messages
- Runs the agent tool's TUI binary (`tool.get_tui_binary()`) with model and resume flags

## Patch Generation

Sessions can generate git diffs from running pods:
- Executes `git diff` (or `git diff origin/{branch}` if using a working branch) via `_exec_in_pod()`
- AI-generated commit messages via Vertex AI Claude, Anthropic API, or Gemini API (falls back to simple file-list summary)
- Patches downloadable as `.patch` files

## UI Pattern

- **Server-rendered HTML** with Jinja2 templates extending `base.html`
- **PatternFly 6** dark theme via CDN (`pf-v6-theme-dark` on `<html>`)
- **HTMX** for partial page updates (status polling, inline forms, repo management) — vendored as `swarmer/static/htmx.min.js`
- Flash messages stored in Starlette session, rendered in `base.html`
- ANSI escape codes in pod output converted to HTML spans via `ansi_to_html` Jinja2 filter

## Adding New Features

### Adding a new model field

1. Add the column to the SQLAlchemy model in `swarmer/models/`
2. If the table already exists in production DBs, add an `ALTER TABLE` migration in `database.py:migrate_db()`
3. Include `server_default=` so existing rows get a valid value

### Adding a new router

1. Create `swarmer/routers/new_feature.py` with `router = APIRouter()`
2. Add `dependencies=[Depends(require_auth)]` to all routes
3. Import and register in `swarmer/main.py`: `app.include_router(new_router.router)`

### Adding a new model

1. Create `swarmer/models/new_model.py` inheriting from `Base`
2. Import it in `swarmer/models/__init__.py` (required for table creation)
3. If it has encrypted fields, follow the `_enc` suffix + `@property` pattern from `github_pat.py`

### Adding secrets/sensitive fields

1. Store the encrypted value with `_enc` suffix
2. Add `@property` getter calling `crypto.decrypt()` and `@setter` calling `crypto.encrypt()`
3. Sync to K8s Secret via the agent tool's `build_k8s_secret_data()` method

### Adding a new agent tool

1. Create `swarmer/agent_tools/new_tool.py` implementing all `AgentToolStrategy` abstract methods
2. Register in `agent_tools/registry.py:_init()`
3. Add the tool name to `AGENT_TOOLS` tuple in `models/session.py`
4. Add `agent_image_new_tool: str = ""` in `config.py:Settings`
5. Add corresponding `AGENT_IMAGE_NEWTOOL` env var in `.env.example` and Makefile placeholders

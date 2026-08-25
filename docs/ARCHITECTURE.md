# Swarmer — Architecture Reference

## Project Structure

```
agent-swarm/
├── Makefile                    # All build/deploy/dev commands
├── Containerfile                # UBI10 python-312-minimal, runs uvicorn on port 8080
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
├── practices/                  # Operational best practices
│   └── autonomous-sdlc/        # Autonomous agent workflows
│       └── swarm-pr-watcher.md # In-process PR watcher operations & troubleshooting
├── prompts/                    # Headless autonomous agent prompts
│   └── auto-pr-fix-agent.md    # Autonomous prompt for fixing conflicts, CI, comments
├── scripts/                    # Automation, smoke tests, and CLI helpers
│   ├── mcp_setup.py            # CLI setup for opencode.json + token discovery
│   ├── openshell_connect.py    # Multi-cluster OpenShell port-forward helper
│   ├── openshell_smoke_test.py # Sandbox runtime e2e verification
│   └── openshell_jira_smoke_test.py # Jira MCP server e2e policy verification
├── mcp-server/                 # Standalone MCP server for session orchestration
│   ├── pyproject.toml          # MCP server packaging
│   ├── agent_swarm_mcp_server/ # FastMCP server, REST API client, auth resolution
│   └── tests/                  # Client & tool unit tests (respx mocked)
├── tests/                      # Swarmer test suite
│   ├── test_api.py              # REST API unit tests (in-memory SQLite, no server)
│   ├── test_token_page.py       # /token UI and mcp_setup script unit tests
│   ├── test_k8s_auth.py         # TokenReview & OpenShift OAuth fallback tests
│   ├── test_list_repos_for_pat.py  # GitHub API helpers (respx mocking)
│   ├── test_openshell_client.py # OpenShell client wrapper tests (mocked SDK, no package required)
│   └── test_ui_patternfly.py   # Playwright e2e tests (requires running server at :8091)
└── swarmer/                    # Python package (the application)
    ├── main.py                 # FastAPI app, lifespan, middleware, router registration
    ├── config.py               # pydantic-settings Settings singleton
    ├── database.py             # SQLAlchemy async engine + session factory + migrations
    ├── crypto.py               # Fernet encrypt/decrypt from secret key file or env var
    ├── k8s_auth.py             # K8s TokenReview validation & OpenShift OAuth fallback
    ├── workspace_acl.py        # Database-backed workspace ACL (owner/member/admin) — ACM-41659
    ├── deps.py                 # FastAPI dependencies (require_auth, get_user_token)
    ├── k8s.py                  # Kubernetes utility functions (namespace, pull secrets, image check, extra env vars)
    ├── mcp_catalog.py          # Registry of well-known MCP servers (Jira, etc.) with OAuth defaults
    ├── scheduler.py            # Background asyncio cron scheduler + queue processor + sandbox GC
    ├── openshell_client.py     # OpenShell sandbox SDK wrapper (async helpers, lazy SDK import)
    ├── openshell_policy.py     # Network policy builder for OpenShell sandboxes
    ├── github_app.py           # Resolve workspace GitHub App credentials (user/shared visibility)
    ├── github_auth.py          # IAT minting (JWT → GitHub REST → token) + refresh loop for long sessions
    ├── csrf.py                 # CSRF token helpers for server-rendered HTML forms
    ├── agent_tools/            # Strategy pattern for agent tool support
    │   ├── __init__.py         # AgentToolStrategy ABC
    │   ├── registry.py         # Global registry + aliases (_init() auto-registers all tools)
    │   └── opencode.py         # OpenCode strategy (Vertex AI Anthropic/Gemini models)
    ├── models/                 # SQLAlchemy ORM models
    │   ├── __init__.py         # Imports all models (required for Base.metadata)
    │   ├── workspace.py        # Workspace (owner_id + derived namespace slug for legacy K8s secrets)
    │   ├── workspace_member.py # Explicit per-user workspace access grants (ACM-41659)
    │   ├── global_admin.py     # Self-service global admins (ACM-41659)
    │   ├── session.py          # Session (sandbox lifecycle, modes: tui/server/prompt, cron scheduling)
    │   ├── session_repo.py     # Git repos attached to sessions (cloned into sandbox at launch)
    │   ├── sandbox_env_var.py  # Per-workspace env vars (encrypted at rest, injected into sandboxes)
    │   ├── opencode_secret.py  # GCP project/location (DB); ADC + Gemini key are gateway-only (ACM-37263)
    │   ├── github_pat.py       # Fernet-encrypted GitHub PATs for HTTPS git auth
    │   ├── github_app.py       # Fernet-encrypted GitHub App credentials (one per workspace)
    │   └── mcp_server.py       # MCP server configs with Fernet-encrypted OAuth tokens
    ├── routers/                # FastAPI route handlers
    │   ├── auth.py             # /login (token paste + OpenShift OAuth), /logout, /token, /auth/callback
    │   ├── workspaces.py       # CRUD for workspaces
    │   ├── sessions.py         # CRUD + launch/stop/schedule/patch generation + repo management
    │   ├── secrets.py          # OpenCode secrets, GitHub PATs, GitHub App, pull secrets
    │   ├── mcp_servers.py      # MCP server CRUD, OAuth 2.1 flow (PKCE + dynamic registration)
    │   ├── chat_proxy.py       # HTTP/SSE/WebSocket reverse proxy for server-mode sessions
    │   └── tui_ws.py           # WebSocket PTY proxy for TUI-mode sessions (K8s exec)
    ├── api/v1/                 # REST API — 51 endpoints under /api/v1/
    └── templates/              # Jinja2 HTML templates (PatternFly 6 dark theme + HTMX)
        ├── base.html           # Layout with masthead, flash messages, PatternFly CDN
        ├── token.html          # Active bearer token, opencode.json snippet, 1-click copy
        ├── workspaces/         # list, detail, new, edit, _delete_confirm, members
        ├── admins/             # list, bootstrap
        ├── sessions/           # list, detail, new, _status_badge, _last_output, _repo_list, etc.
        ├── secrets/            # tabs, opencode_form, github_pat_form, github_pat_list
        └── mcp_servers/        # list (catalog + configured servers with OAuth status)
```

## Design Principles

**Favor encrypted database over Kubernetes objects** — Credentials, configuration, and application state are stored in the encrypted SQLite database (Fernet at rest) rather than K8s Secrets or ConfigMaps. This simplifies RBAC requirements, provides an audit trail via timestamps, and makes the application more portable. The only remaining K8s storage is `swarmer-agent-extra-env` (pending migration in ACM-35039) and image pull secrets (which require K8s to function).

**OpenShell is the sole session runtime** — All agent session lifecycle (create, exec, stop, delete) goes through the OpenShell Gateway + Supervisor APIs. Swarmer does not create K8s pods, PVCs, Services, or Routes for agent sessions.

**Minimal K8s surface** — Swarmer's K8s usage is limited to: authentication (TokenReview via `k8s_auth.py`, identity only — no RBAC checks), image pull secrets (for `check_image_reachable`; the K8s namespace they live in is created lazily on first use, ACM-41659), and Add Member / Add Admin candidate discovery (`k8s.list_openshift_users()`, `k8s.list_user_service_accounts()`, both best-effort/read-only). Workspace access control itself is a database ACL (`workspace_acl.py`), not K8s RBAC or namespace scoping. All credential injection for agent sessions is handled by the OpenShell Gateway.

## Domain Model

- **Workspace** is a logical grouping for sessions/secrets, backed purely by the database (ACM-41659) — access is a database ACL (`Workspace.owner_id` + `workspace_members` rows + configured admin allow-list, see `workspace_acl.py`), not a per-workspace Kubernetes namespace. `Workspace.namespace` is still a derived slug used to name a handful of legacy per-workspace K8s Secrets (pull secrets) that are lazily created on first use via `Workspace.k8s_namespace` (or a single shared namespace when `settings.k8s_namespace` is set).
- **Session** = an agent run inside an OpenShell sandbox. Three modes:
  - `prompt` — one-shot: runs the agent with a prompt, sandbox exits on completion, sandbox auto-deleted on success
  - `server` — persistent: runs the agent in server mode, exposes a service via OpenShell `expose_service()`, dashboard proxies HTTP/WS/SSE to it
  - `tui` — persistent: runs `sleep infinity`; user connects via xterm.js WebSocket → OpenShell `exec_interactive()` PTY
- **Session phases**: `idle` → `pending` → `running` → `succeeded`/`failed`/`stopped`
- **Multi-Trigger Model (`SessionSchedule`)** — sessions can have multiple execution triggers (ACM-35377, ACM-42674) with `trigger_type` set to either `"cron"` or `"event"`:
  - `"cron"`: Scheduled time triggers (`cron_schedule`, `cron_next_run`). A background loop (`scheduler.py`) evaluates due schedules every 30s, atomically claims rows, sets `session.mode = "prompt"`, and launches the session.
  - `"event"`: GitHub PR event triggers (`event_condition`, `author_scope`, `fix_authors`). Evaluated and dispatched by the in-process Swarm PR Watcher loop (`swarmer/pr_watcher.py`) upon receiving actionable GitHub events (CI failures, conflicts, new commits, review comments).
- **SessionRun** — historical record of completed executions (`session_runs` table). Captures phase, duration, dual outputs (`last_output`, `raw_output`), `trigger_type` (`"manual"`, `"cron"`, `"event"`), `schedule_label`, and serialized `event_context` (PR metadata for event-driven runs).
- **OpencodeSecret** — per-workspace storage for GCP project and Vertex location (plain, non-secret SQLite `Text` columns — not Fernet-encrypted, since they carry no sensitive material). Despite the legacy name, used by OpenCode. The ADC JSON and the Google AI Studio (Gemini) API key are **not** persisted here via the UI — both are pushed directly to OpenShell gateway providers at save time (`swarmer-ws-{id}-google-cloud` and `swarmer-ws-{id}-google-ai-studio` respectively) and checked at launch/display time via `provider_exists()` (ACM-37263 completed the Gemini side of this pattern, mirroring the pre-existing ADC behavior). The model's `application_default_credentials_enc` / `google_api_key_enc` columns are retained only for backward compatibility with rows written before each migration and via the raw `POST /api/v1/.../secrets/credentials` API, which still accepts and stores them encrypted. Multiple rows per workspace are tolerated (one per `user_id`); read paths use `.scalars().first()` to avoid `MultipleResultsFound` when users share a workspace. A `UNIQUE (workspace_id, user_id)` constraint prevents future duplicates, with a deduplication migration that keeps the newest row per pair.
- **GitHubPAT** — per-workspace encrypted GitHub personal access tokens with optional org scope for HTTPS git auth. Injected into OpenShell sandboxes via Gateway credential providers. Acts as fallback when no GitHub App is configured.
- **GitHubApp** — one GitHub App installation per workspace, storing `app_id`, `installation_id`, and a Fernet-encrypted RSA private key (`private_key_enc`). At session launch, Swarmer mints a short-lived Installation Access Token (IAT) server-side using PyJWT + GitHub's REST API and injects it into the sandbox via the OpenShell Gateway provider — the raw PEM key never enters the sandbox. For TUI and server-mode sessions that may exceed the 1-hour token lifetime, a background asyncio task (`github_auth.start_token_refresh_loop`) re-mints and re-registers the provider every 50 minutes. See [docs/GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md) for setup steps and required permissions.
- **McpServer** — per-workspace MCP server configurations with OAuth 2.1 tokens encrypted at rest. Enabled servers are configured in the agent config JSON and credentials injected via Gateway env vars.
- **SandboxEnvVar** — per-workspace arbitrary key-value env vars stored encrypted in SQLite, injected into every OpenShell sandbox via `create_provider()`.
- **SessionRepo** — git repositories to clone into the sandbox via OpenShell API at session launch.

## Agent Tool Strategy Pattern

Agent tool support uses the Strategy pattern (`agent_tools/`). Each tool (currently OpenCode) implements `AgentToolStrategy` with abstract methods covering:
- Image selection and container name
- Config data generation (`build_config_data` → written to sandbox via `write_agent_config()`)
- Mode-specific command construction (`build_main_cmd`, `build_model_setup_cmd`, `build_share_setup_cmd`)
- Model options, validation, and defaults
- TUI binary selection (`get_tui_binary`)

Tool instances are accessed via `agent_tools/registry.py`. No K8s-specific methods remain in the strategy interface.

## Authentication

Token-based auth via Kubernetes bearer tokens (not password-based):
- Users paste a K8s ServiceAccount token into the login form
- Token validated via TokenReview API (`k8s_auth.py`); falls back to namespace probe if RBAC for tokenreviews is missing
- Validated token is Fernet-encrypted and stored in the session cookie (`deps.py:get_user_token()`)
- Workspace access is a database ACL (ACM-41659, `workspace_acl.py`), not K8s RBAC: a user may access a workspace as its owner (`Workspace.owner_id`), an explicit `WorkspaceMember` row, a global admin (`WORKSPACE_ADMIN_USERS` / `WORKSPACE_ADMIN_GROUPS` env vars, or the self-service `global_admins` DB table / `/admins` UI), or — while a workspace has no owner yet — any authenticated user ("claim on write": the first management action claims ownership). Shared-namespace deployments (`K8S_NAMESPACE` set) grant every authenticated user access to every workspace. `k8s_auth.py` only authenticates identity (username + groups); it performs no SelfSubjectAccessReview calls.
- `workspace_acl.list_known_users()` / `GET /api/v1/users` back the Add Member / Add Admin autocomplete (both forms use the same discovery — exclusions differ by call site). Merges three sources: DB-known users (visibility-scoped — people who already share a workspace with the caller; admins see everyone, never a global directory for non-admins), `k8s.list_openshift_users()` (OpenShift `User` objects, cluster-scoped, `[]` off-OpenShift), and `k8s.list_user_service_accounts()` (ServiceAccounts `make user-token SA_USER=<name>` would create, formatted as `system:serviceaccount:<ns>:<name>`). Both K8s helpers are best-effort and never raise. Free-text entry on those forms is always still allowed; suggestions never restrict who can actually be granted access.
- Startup migration (`workspace_migration.py` + SQL in `database.py:migrate_db()`) backfills `workspace_members`/`owner_id` from existing per-user credential rows and legacy `swarmer-user` K8s RoleBindings, so upgrading never requires re-adding users.
- Optional OpenShift OAuth: implicit grant flow via `/auth/callback` (captures token from URL fragment client-side)
- `swarmer/auth.py` is superseded — just contains a comment pointing to `k8s_auth.py`

## Encryption

All sensitive fields that are actually persisted in the DB (PATs, GitHub App private key, Jira tokens) are Fernet-encrypted at rest in SQLite. The ADC JSON and Gemini API key are the notable exceptions — via the UI they go to the OpenShell gateway only and are never written to SQLite (see OpencodeSecret above); their `_enc` columns exist solely for the raw API path and backward compatibility.

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
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent; only suppresses "duplicate column"/"already exists"/"no such column" errors, all others re-raise). Also supports `CREATE TABLE IF NOT EXISTS`, `CREATE UNIQUE INDEX IF NOT EXISTS`, and data-fixup `DELETE` statements.
- All models must be imported in `models/__init__.py` for table registration to work
- SQLite single-writer: K8s Deployment uses `strategy: Recreate` (not RollingUpdate)
- **`NullPool` for SQLite** — `init_db()` uses `NullPool` instead of the default `QueuePool` for SQLite connections. `aiosqlite` opens a new OS-level connection on every call and does not benefit from connection pooling; `QueuePool`'s default limit of 5+10 connections would be exhausted under concurrent chat proxy load (one DB lookup per proxied asset). `NullPool` creates and closes connections on-demand with no cap, matching `aiosqlite`'s actual behaviour.

## Kubernetes Integration

Swarmer uses the official `kubernetes` Python client for a limited set of infrastructure operations. All agent session lifecycle is handled by OpenShell — Swarmer does not create pods, PVCs, Services, or Routes for sessions.

**Active K8s usage:**
- `k8s_auth.py` — TokenReview for user authentication (identity only — no RBAC/authorization checks; see Auth Flow above)
- `k8s.init_k8s()` — loads in-cluster or kubeconfig at startup
- `k8s.ensure_namespace()` / `delete_namespace()` — **no longer called at workspace create/delete time** (ACM-41659). A workspace's K8s namespace (`k8s.effective_namespace()`) is now created lazily, only the first time a legacy per-workspace K8s Secret feature (pull secrets) is actually used, and best-effort deleted when the workspace is deleted.
- Pull secret management (`apply_pull_secret`, `get_pull_secret_info`, `delete_pull_secret`) — required for `check_image_reachable`
- `get_extra_env_vars()` / `set_extra_env_var()` / `delete_extra_env_var()` — workspace env var storage via K8s Secret `swarmer-agent-extra-env` (**ACM-35039**: migrating to SQLite)
- `k8s.list_swarmer_user_role_binding_identities()` — read-only, used once at startup by `workspace_migration.py` to mirror legacy `swarmer-user` RoleBinding grants into the DB ACL (ACM-41659); never writes RoleBindings anymore
- `k8s.list_openshift_users()` / `k8s.list_user_service_accounts()` — read-only, back the Add Member / Add Admin candidate discovery (`workspace_acl.list_known_users()` / `GET /api/v1/users`); both best-effort, never raise

All kubernetes client imports remain lazy (inside functions) to avoid import errors when K8s is not configured.

## OpenShell Integration

[NVIDIA OpenShell](https://github.com/nvidia/openshift-ai-openShell) replaces direct K8s pod and Secret management with a Gateway + Supervisor model. Swarmer sends credentials to the Gateway (which injects them securely) and requests sandboxes from the Supervisor (which provides the isolated runtime). Swarmer never writes AI tokens or PATs into K8s Secrets again.

- **Gateway** -- credential injection API; Swarmer sends AI tokens, PATs, and MCP tokens to the Gateway, which injects them as env vars into the sandbox. No K8s Secrets written for session credentials.
- **Supervisor** -- sandboxed agent runtime; `create_sandbox()` provisions the sandbox, `delete_sandbox()` tears it down.
- **Sandbox lifecycle** -- fully managed by OpenShell. No K8s pods, PVCs, or Services created for sessions.
- **`/sandbox` PVC** -- OpenShell creates a per-sandbox PVC (`workspace-{sandbox-name}`) for `/sandbox`, sized by the gateway's `server.workspaceDefaultStorageSize` Helm value; Swarmer does not create this PVC directly (see the OpenShell Client API table below for the distinct, hardcoded `10Gi` pod `ephemeral-storage` compute resource). Repos are cloned fresh each launch via OpenShell API.
- **No session K8s Secrets** -- all credential injection goes through the Gateway provider mechanism.
- **`session.sandbox_name`** -- stores the OpenShell sandbox identifier (nullable `VARCHAR(255)`, `NULL` when session is idle).
- **Network policy** -- `openshell_policy.py` builds per-sandbox YAML policies controlling outbound access (AI provider endpoints, per-repo GitHub, Jira MCP)
- **Client module** -- `swarmer/openshell_client.py` wraps the OpenShell gRPC SDK with async helpers using `asyncio.to_thread`

### OpenShell Client API (`swarmer/openshell_client.py`)

| Function | Signature | Description |
|---|---|---|
| `_get_client()` | `() → SandboxClient` | Internal factory; reads settings, returns configured SDK client |
| `get_client()` | `(gateway_url, tls_ca_path?, tls_cert_path?, tls_key_path?) → SandboxClient` | Public factory for e2e tests |
| `create_provider()` | `async (session, workspace_secret, github_pat, mcp_servers, client?) → dict[str,str]` | Collects DB credentials into env-var dict (no K8s Secrets, no I/O) |
| `create_provider_from_env()` | `async (google_api_key, github_pat, client?) → dict[str,str]` | Builds env-var dict from explicit values (for tests) |
| `ensure_provider()` | `async (name, profile_type, config, credentials?, client?) → None` | Creates or updates a named gateway provider (idempotent) |
| `configure_provider_credential()` | `async (provider_name, credential_key, credential_value, client?) → None` | Stores a static credential on a gateway-managed provider |
| `configure_vertex_provider()` | `async (provider_name, adc_json, project, location, client?) → None` | Configures google-vertex-ai provider with ADC-based token refresh |
| `enable_providers_v2()` | `async (client?) → None` | Enables `providers_v2_enabled` gateway feature flag (required for google-vertex-ai) |
| `set_cluster_inference()` | `async (provider_name, model_id, no_verify?, client?) → None` | Configures inference.local cluster proxy to use a provider+model |
| `create_sandbox()` | `async (image, env_vars, policy, provider_names?, client?) → SandboxRef` | Creates sandbox, waits ready, returns ref. Every sandbox gets a hardcoded `SandboxTemplate.resources` ephemeral-storage request/limit of `SANDBOX_EPHEMERAL_STORAGE` (`10Gi`, ACM-39804 — previously a per-session dropdown, ACM-38184, removed because it only bounded this compute resource, not `/sandbox`). `/sandbox` is a separate PVC (`workspace-{sandbox-name}`) sized by the gateway's `server.workspaceDefaultStorageSize` Helm value (`OPENSHELL_WORKSPACE_STORAGE` in the Makefile) — see ACM-38172 |
| `delete_sandbox()` | `async (sandbox_name, client?) → None` | Deletes sandbox by name |
| `write_agent_config()` | `async (sandbox_name, tool_name, config_json, client?) → None` | Writes tool config JSON to `/sandbox/{tool}.json` |
| `write_agents_md()` | `async (sandbox_name, content, client?) → None` | Writes AGENTS.md to `/sandbox/` |
| `write_file()` | `async (sandbox_name, path, content, client?) → None` | Writes arbitrary file to sandbox |
| `start_agent()` | `async (sandbox_name, cmd, client?) → None` | Starts agent as detached nohup background process (fire-and-forget) |
| `exec_command()` | `async (sandbox_name, cmd, client, stdin?, timeout_seconds?) → ExecResult` | Runs command, returns result with stdout/stderr/exit_code |
| `exec_interactive()` | `(sandbox_name, sandbox_id, command, cols, rows, client?) → (stream, queue)` | Opens interactive PTY gRPC stream for TUI WebSocket bridge |
| `expose_service()` | `async (sandbox_name, service_name, target_port, client?) → str` | Exposes sandbox port via gateway and returns a routable URL |
| `delete_service()` | `async (sandbox_name, service_name, client?) → None` | Deletes an exposed sandbox service endpoint |
| `approve_draft_policy_chunks()` | `async (sandbox_name, expected_hosts?, client?) → list[str]` | Approves pending network policy chunks for expected hosts |

### OpenShell Deployment on OpenShift

Sandbox pods require elevated Linux capabilities (`NET_ADMIN`, `SYS_ADMIN`, `SYS_PTRACE`, `SYSLOG`) that OpenShift's default `restricted` and `anyuid` SCCs do not allow. `make deploy` automatically grants the `privileged` SCC to both the `openshell` and `openshell-sandbox` service accounts in the OpenShell namespace when `oc` is present on the path. This is idempotent and a no-op on plain Kubernetes where `oc` is absent.

The gateway itself runs under `anyuid` (non-root is fine for the gateway process); only sandbox pods need `privileged`. Both grants are applied unconditionally after install **and** re-deploy so that they survive namespace recreation.

`make deploy` also sets `server.auth.allowUnauthenticatedUsers=true` in the Helm chart. The gateway requires a JWT bearer token in addition to mTLS; for port-forward setups the `openshell` CLI uses `auth_mode: mtls` and does not auto-mint tokens against a remote cluster's JWT signing key. Since the gateway is only reachable via `localhost:<port>` (kubectl port-forward), mTLS alone provides adequate mutual authentication and the JWT layer is redundant. This setting is safe for the port-forward pattern.

### OpenShell Local Gateway Registration (`make openshell-register`)

`make openshell-register` creates or refreshes the gateway entry in `~/.config/openshell/gateways/`. Key behaviours:

- **Stable port on refresh** — if a `metadata.json` already exists for this cluster context, the existing port is reused verbatim. No re-probing. This prevents drift between `metadata.json` and the port-forward started by `make connect-openshell`, which always reads the port from `metadata.json`.
- **Port selection for fresh registrations** — starts from `OS_LOCAL_PORT` (default 17671, chosen to avoid the local `openshell-gateway` daemon which binds 17670) and walks up, skipping ports already claimed by other registered gateways or currently bound on localhost.
- **`make connect-openshell`** reads every `~/.config/openshell/gateways/*/metadata.json`, extracts the port, and starts a `kubectl port-forward` for each — one per registered cluster. Port-forwards run until Ctrl-C; if one exits unexpectedly a `[warn]` is printed.

### OpenShell Config Settings

All settings live in `swarmer/config.py` (`Settings` class) and are read from env vars:

| Setting | Env Var | Type | Default | Purpose |
|---|---|---|---|---|
| `openshell_gateway_url` | `OPENSHELL_GATEWAY_URL` | `str` | `""` | Gateway API base URL for credential injection |
| `openshell_supervisor_url` | `OPENSHELL_SUPERVISOR_URL` | `str` | `""` | Supervisor API base URL for sandbox lifecycle |
| `openshell_tls_cert` | `OPENSHELL_TLS_CERT` | `str` | `""` | Path to client TLS certificate (mTLS) |
| `openshell_tls_key` | `OPENSHELL_TLS_KEY` | `str` | `""` | Path to client TLS private key (mTLS) |
| `openshell_tls_ca` | `OPENSHELL_TLS_CA` | `str` | `""` | Path to CA bundle for server cert verification |
| `openshell_bearer_token` | `OPENSHELL_BEARER_TOKEN` | `str` | `""` | Bearer token for Gateway/Supervisor authentication |
| `sandbox_gc_interval` | `SANDBOX_GC_INTERVAL` | `int` | `300` | Seconds between sandbox garbage-collection sweeps |

> **Ephemeral disk (ACM-39804):** Sandbox pod ephemeral-storage is a **hardcoded** value (`openshell_client.SANDBOX_EPHEMERAL_STORAGE`, `10Gi`), applied to every sandbox — not a per-session or env-var setting. A per-session dropdown (`Session.ephemeral_disk`, ACM-38184) previously existed but was removed: it only bounded the sandbox pod's ephemeral-storage compute resource (container writable layer / unsized emptyDirs), which users don't perceive as "disk size" and which is not the `/sandbox` working directory — see `workspaceDefaultStorageSize` below. There is no OpenShell API (verified through gateway/SDK 0.0.97) to size `/sandbox` per sandbox.

### Model Preset Settings (ACM-37232)

Claude/Gemini preset → model-ID mappings, read from env vars via `swarmer/config.py`'s `Settings`
class. In a cluster deployment these are sourced from a dedicated `ConfigMap`
(`k8s/swarmer/configmap.yaml`, name `swarmer-model-presets`), referenced by the `swarmer`
Deployment via `envFrom: configMapRef` — kept separate from the Deployment's own env vars
(image, URLs, credentials) so it can be edited independently:

```sh
kubectl edit configmap swarmer-model-presets -n swarmer   # or: kubectl apply -f k8s/swarmer/configmap.yaml
kubectl rollout restart deployment/swarmer -n swarmer     # env vars are only read at container start
```

No code change or image rebuild needed when Vertex AI / Google ship new model versions — just
edit the ConfigMap and restart the deployment. `make deploy` applies it automatically before the
Deployment; `make delete` removes it.

| Setting | Env Var | Default | Purpose |
|---|---|---|---|
| `claude_preset_plan_model` | `CLAUDE_PRESET_PLAN_MODEL` | `google-vertex-anthropic/claude-opus-4-6@default` | Claude preset's PLAN-role model |
| `claude_preset_build_model` | `CLAUDE_PRESET_BUILD_MODEL` | `google-vertex-anthropic/claude-sonnet-5@default` | Claude preset's BUILD-role model |
| `claude_preset_small_model` | `CLAUDE_PRESET_SMALL_MODEL` | `google-vertex-anthropic/claude-haiku-4-5@20251001` | Claude preset's small/housekeeping model |
| `gemini_preset_plan_model` | `GEMINI_PRESET_PLAN_MODEL` | `google/gemini-3.7-flash` | Gemini preset's PLAN-role model |
| `gemini_preset_build_model` | `GEMINI_PRESET_BUILD_MODEL` | `google/gemini-3.7-flash` | Gemini preset's BUILD-role model |
| `gemini_preset_small_model` | `GEMINI_PRESET_SMALL_MODEL` | `google/gemini-3.5-flash-lite` | Gemini preset's small/housekeeping model |
| `opencode_experimental_plan_mode` | `OPENCODE_EXPERIMENTAL_PLAN_MODE` | `true` | Enables the opencode plan agent so the PLAN-role model above is actually used |

For local dev (`make dev`), the same env vars are set via `.env` (see `.env.example`) — no
ConfigMap involved outside a real cluster deployment.

## Agent Container Data Interface

Every data item Swarmer currently pushes into agent pods, its source model, the current K8s mechanism, and the target OpenShell API call. This table is the migration contract for ACM-34850.

| Category | Data | Source Model | Current K8s Mechanism | Target OpenShell API |
|---|---|---|---|---|
| AI Credentials | GCP Project, Vertex Location (DB); ADC JSON, Gemini key (Gateway-only, ACM-37263) | `OpencodeSecret` | Gateway provider (no K8s Secret) | `configure_google_cloud_provider()` (ADC) / `ensure_provider()` (Gemini) at credential-save time; `provider_exists()` checked at session launch |
| Git Auth | PAT token, GitHub username | `GitHubPAT` | K8s Secret → `secretKeyRef` + init container credential store | Gateway credential injection + `clone_repos()` |
| Git Repos | repo_url, branch, local_path (per repo) | `SessionRepo` | Init container git clone | `openshell_client.clone_repos()` |
| MCP Tokens | Jira URL, Jira access token, Jira email | `McpServer` | K8s Secret → `envFrom` | Gateway env injection |
| Agent Config | Tool-specific JSON + gitconfig | ConfigMap | Volume mount at `/tmp/agent-config-ro` | `write_agent_config()` into sandbox |
| MCP Config | MCP server definitions in agent config JSON | `McpServer` | Startup script overwrites config | `write_file()` into sandbox |
| Model Config | model.json (OpenCode) | `Session.model` | Startup script writes JSON file | `write_file()` into sandbox |
| Prompt | instruction_prompt + base_prompt + repo_context | `Session` + `WorkspacePrompt` | CLI arg (prompt mode) or `SWARMER_AGENT_MD` env → AGENTS.md (TUI/server) | `write_agents_md()` to `/sandbox/AGENTS.md` for **all modes**; prompt mode reads it via `$(</sandbox/AGENTS.md)` shell expansion; TUI/server agent reads it automatically |
| Env Vars | HOME, NODE_OPTIONS, GOOGLE_APPLICATION_CREDENTIALS | Hardcoded | Pod env spec | Sandbox env vars via Gateway |
| Extra Env | Arbitrary workspace key-value pairs | External K8s Secret | `envFrom` (`swarmer-agent-extra-env`, optional) | Gateway env injection |
| Volumes | PVC → /workspace, ConfigMap → /tmp/agent-config-ro, ADC → /app/gcloud | N/A | Pod volume spec | Sandbox filesystem — `/sandbox` is backed by an OpenShell-managed PVC (`workspace-{sandbox-name}`, sized by `server.workspaceDefaultStorageSize`), not a Swarmer-created volume; no ConfigMap/ADC volume mounts |
| Startup Script | Config copy, safe dir, git creds, symlinks, AGENTS.md write, model write, branch checkout | N/A | `sh -c` command chain | Simplified script — removes credential setup and git clone stages |
| Pod Config | Resources (1Gi-8Gi/500m-2000m), fsGroup, runAsUser, imagePullPolicy, restartPolicy | `Session` + `Settings` | Pod spec | Sandbox resource config — ephemeral storage hardcoded to `10Gi` for every sandbox (`openshell_client.SANDBOX_EPHEMERAL_STORAGE`, ACM-39804; no longer per-session, see ACM-38184). The OpenShell gateway's `workspaceDefaultStorageSize` Helm value (`OPENSHELL_WORKSPACE_STORAGE` in the Makefile) is a separate, gateway-wide ceiling for the `/sandbox` PVC — only applied on first OpenShell install |
| Networking | Container port 4096 (server mode), ClusterIP Service, OpenShift Route | `Session.mode` | K8s Service/Route | OpenShell network endpoint |

- **Startup script simplification** -- the OpenShell startup script removes: credential helper setup, git clone, `envFrom` secret injection, ADC volume mount. Keeps: config copy, MCP config overwrite, model JSON write, AGENTS.md write, branch checkout, agent binary invocation
- **Filesystem layout** -- `/sandbox` replaces `/workspace` as the agent HOME and git clone root in OpenShell mode; `stolostron/agent-containers` images must support this path
- **Init containers removed** -- repo cloning moves to `openshell_client.clone_repos()`; the `git-init` init container is eliminated

## Background Tasks

One background asyncio system runs during app lifespan:

**Cron Scheduler + Queue Processor** (`scheduler.py`) — Single global task that checks every 30s. Each cycle:
   - **Queue processor** (`_process_queue`): If the global concurrency cap is not reached, fetches sessions in `"queued"` phase ordered by `created_at` (FIFO) and launches them up to the available slot count. Applies a 2-minute in-memory cooldown when still at capacity to avoid tight retry loops.
   - **Cron launcher**: Claims sessions of any mode with a due `cron_next_run` via atomic `UPDATE … RETURNING`. Sets `session.mode = "prompt"` before `_do_launch()` — scheduled runs always execute in prompt mode. Respects the concurrency cap — does not over-claim. On launch failure, resets phase to `idle` and advances `cron_next_run`.

A **sandbox GC loop** also runs every `SANDBOX_GC_INTERVAL` seconds, collecting orphaned sandboxes whose sessions are no longer active in the DB.

### Concurrency Limiting

`MAX_CONCURRENT_AGENTS` (default 5, configurable via env var) caps the number of simultaneously running agent sandboxes (sessions in `pending` or `running` phase). When this limit is reached:

- All new launches (manual, API, or scheduled) set `phase="queued"` and return immediately without creating a sandbox.
- The queue processor re-evaluates every 2 minutes and launches queued sessions as capacity frees up.
- Stopping a queued session (no sandbox exists) returns it directly to `"idle"` without any sandbox cleanup.
- Setting `MAX_CONCURRENT_AGENTS=0` disables the limit entirely.

The sessions list shows a workspace-scoped capacity summary ("N active | N slots available | N queued") that refreshes every 3s via HTMX. Queued sessions show their global queue position ("Position N of M") on both the list and detail pages.

### Session Run History

Prompt-mode sessions record each completed execution (phase, timing, status detail, and output) in the `session_runs` table. The History tab on the session detail page and `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/runs` expose this data.

Retention is hybrid — two independent pruning mechanisms run on every new record, and whichever removes more rows for a session wins:

- `SESSION_RUN_HISTORY_LIMIT` (default 100) caps how many completed runs are retained per session, oldest first. Set to `0` to disable count-based pruning.
- `SESSION_RUN_HISTORY_MAX_AGE_DAYS` (default 7) deletes completed runs older than N days regardless of count. Set to `0` to disable age-based pruning.

Both set to `0` disables pruning entirely (unlimited history; may grow SQLite storage quickly on scheduled sessions). Pruning logic lives in `swarmer/session_runs.py` (`_prune_by_count` / `_prune_by_age`), called from `record_session_run()` after each new run is inserted.

**Dual output fields** — each run record stores two output fields:

- `last_output` — the processed agent response: the clean assistant conversation extracted from OpenCode's SQLite DB (`/sandbox/.opencode/opencode.db`) via `read_opencode_response()`.
- `raw_output` — the raw stdout+stderr streamed from the sandbox process (ANSI escape codes, tool call traces, progress output). Always preserved regardless of agent tool.

The live Output tab in the session detail page shows both via a toggle (Output / Raw Log) when the fields differ. The History tab shows a second "View raw log" `<details>` expandable alongside "View output" when they differ.

## Chat Proxy

`chat_proxy.py` handles server-mode session access. All sessions use `session.service_url` set by `expose_service()` after the server agent starts:

- Routes HTTP/SSE/WebSocket to `session.service_url` — an OpenShell gateway domain URL (e.g. `https://<name>.openshell.localhost:<port>`). The port is rewritten in `expose_service` to match `OPENSHELL_GATEWAY_URL`. **DNS rewriting**: the gateway assigns virtual-host domain names (e.g. `oriented-lizardfish--agent.openshell.localhost`) that are not resolvable from the Swarmer pod. `_resolve_upstream()` rewrites the hostname to the gateway's real address (from `OPENSHELL_GATEWAY_URL`) at connect time, while setting the HTTP `Host` header to the original virtual domain so the gateway can route to the correct sandbox. The gateway requires **mutual TLS** — the proxy presents the client cert/key from `OPENSHELL_TLS_CERT`/`OPENSHELL_TLS_KEY` and skips server cert verification (`verify=False`) since the gateway uses a self-signed cert. Without the client cert the gateway returns `TLSV13_ALERT_CERTIFICATE_REQUIRED`.

Server-mode lifecycle: session stays in `pending` until `expose_service` returns a URL, which is stored and the session transitions to `running` atomically — preventing the Chat tab from opening before the URL is set.

SSE streams proxied with no read timeout; WebSocket proxy via `websockets` library (bidirectional relay) with TLS bypass for `wss://` upstreams.

## TUI WebSocket Proxy

`tui_ws.py` provides browser-to-agent terminal access via OpenShell:

- One-time UUID auth tokens generated on session detail page, stored in HTTP session, consumed on connect
- Resolves `sandbox_id` via `_sandbox_id()`
- Opens an `ExecSandboxInteractive` gRPC stream via `exec_interactive()`
- Background thread drains the gRPC response stream into an asyncio Queue
- Async read/write tasks bridge the browser xterm.js WebSocket and the gRPC stream
- Resize events forwarded as `ExecSandboxWindowResize` messages
- Agent is NOT started here — the TUI WebSocket handler starts it interactively; `_run_openshell_agent` skips `start_agent` for TUI mode
- Network policy probe runs during `_setup_openshell_sandbox` so AI API endpoints are approved before the user connects
- Workspace env vars and MCP credentials injected from `SandboxEnvVar` DB rows and provider environment

Runs the agent tool's TUI binary (`tool.get_tui_binary()`) with model and resume flags.

## Patch Generation

Sessions can generate git diffs from running sandboxes:
- Executes `git diff` (or `git diff origin/{branch}` if using a working branch) via `openshell_client.exec_command()` in the sandbox
- AI-generated commit messages via the Gemini API, called directly from the Swarmer process (`_llm_commit_msg_gemini`), falling back to a simple file-list summary (`_fallback_commit_msg`) when unavailable
- Since ACM-37263, the Gemini key is stored only on the OpenShell gateway (write-only, never returned in plaintext), so this feature only works for workspaces with a legacy key still present in `OpencodeSecret.google_api_key_enc` from before the key was rotated/migrated; new or rotated keys always fall through to the file-list summary
- Patches downloadable as `.patch` files

## UI Pattern

- **Server-rendered HTML** with Jinja2 templates extending `base.html`
- **PatternFly 6** dark theme via CDN (`pf-v6-theme-dark` on `<html>`)
- **HTMX** for partial page updates (status polling, inline forms, repo management) — vendored as `swarmer/static/htmx.min.js`
- Flash messages stored in Starlette session, rendered in `base.html`
- ANSI escape codes in pod output converted to HTML spans via `ansi_to_html` Jinja2 filter

### Session Detail Page Layout

The session detail page (`sessions/detail.html`) uses a two-column grid inside the Details tab:

- **Left column (4-col)** — two stacked cards: Configuration and Schedule. The Configuration card contains agent tool pills, model select, working branch, and MCP server checkboxes. The Schedule card is always visible regardless of session mode; the scheduler coerces to prompt at run time.
- **Right column (8-col)** — Git Repositories card only.

**Action bar** (below session title, above Prompt/tabs):

| State | Layout |
|---|---|
| Idle | `(Status) ∙ [▶ TUI] [▶ CHAT] [▶ PROMPT] · · · · · · [Delete]` |
| Active (Chat) | `(Status) ∙ [■ Stop] sandbox-name [Chat ↗] · · · · · [Delete]` |
| Active (other) | `(Status) ∙ [■ Stop] sandbox-name · · · · · · · · · · [Delete]` |

### Pill UX Architecture

Swarmer uses branded, styled interactive pills across the UI for tool selection, execution modes, logs, and status:

- **Agent Tool Pills** (`.agent-pill`, `.agent-pill-oc`, `.agent-pill-shell`):
  - Branded button pills used on both the New Session form (`new.html`) and the Configuration card (`detail.html`).
  - **OpenCode**: Official 4×5 block-pixel SVG wordmark (`78×14px`), dark background (`#2d2d2d`) when inactive, and a 6-stop rainbow gradient border (`linear-gradient(135deg, #e06c75, #e5c07b, #98c379, #56b6c2, #61afef, #c678dd)`) on `#1e1e1e` when selected.
  - **Shell**: Matching 4×5 block-pixel `>_ SHeLL` SVG wordmark (`66×14px`) in phosphor matrix green (`#38ef7d`) with cyan prompt glyph (`#58a6ff`), and an emerald-to-cyan terminal gradient border (`linear-gradient(135deg, #38ef7d, #11998e, #00f2fe, #4facfe)`) on `#0d130e` when selected.
  - **Interaction**: Clicking a pill updates the underlying hidden input (`agent_tool`), toggles AI provider selector visibility (hidden for Shell), updates helper text dynamically, and triggers auto-save (`_cfgSave()`) on the detail page. In `server` mode, the Shell pill is automatically disabled (`cursor: not-allowed`, `opacity: 0.5`).

- **Launch Pills** (`.launch-pill-green`, `.launch-pill-muted`):
  - Action bar launch buttons ordered `TERM.UI` → `CHAT` → `PROMPT` (most-used first).
  - `TERM.UI` and `CHAT` use green fill (`.launch-pill-green`); `PROMPT` uses a dark charcoal fill with green border (`.launch-pill-muted`).
  - Submitting any launch pill POSTs the full configuration form to `/launch` with `mode` and `save_config=1` atomically.

- **Log-View Toggle Pills** (`.log-pill`):
  - Used on the Output tab and History expandable rows to toggle between processed Output and raw console logs.
  - Selected state applies the signature rainbow gradient border on `#1e1e1e`.

- **Cluster Capacity Indicator Pill**:
  - A status pill labelled `Sessions: X / Y active` with optional `· N queued` appended.
  - Color escalates dynamically: outline (0 active) → green (healthy) → gold (near/at capacity: `active >= max-1` for `max > 2`, `active == max` for `max ≤ 2`) → red (any queued). Rendered in both `detail.html` and `_list_rows.html`.

- **History Source Pills**:
  - Denormalized source pills in Run History rows: purple schedule pills for cron runs (`[📅 schedule-name · prompt-name]`), gold event pills for event-driven runs (`[⚡ Event: PR #104 (pr-fix)]`), green `[TERM.UI]` / `[CHAT]` pills for interactive runs, and prompt name pills for manual prompt runs.

## Event-Driven PR Events Watcher & Session Dispatcher

The **Swarm PR Events Watcher** (`swarmer/pr_watcher.py`) is an in-process, firewall-safe asynchronous background loop that runs inside the Swarmer pod's FastAPI lifespan, alongside `scheduler.py`. It monitors GitHub repositories for Pull Request state changes and dispatches Swarm sessions only when actionable work is needed from trusted contributors.

There is **no standalone CLI or static JSON configuration** — all triggers, repositories, conditions, and author scopes are discovered directly from `swarmer.db` (configured entirely via the Web UI's Scheduling section). Dispatches happen in-process via `_do_launch()`, inheriting capacity limiting and `MAX_CONCURRENT_AGENTS` queueing automatically.

```text
GitHub Events API (Outbound ETag Polling)
                 │
  ┌──────────────┴──────────────┐
  ▼                             ▼
304 Not Modified              200 OK (New Events Detected)
(0 rate-limit cost)             │
                                ▼
                       Scan Open PRs for Repo
                                │
                                ▼
               ┌────────────────────────────────┐
               │    Author & Trust Filtering    │
               │  - Resolve author scope        │
               │  - Enforce 3-layer trust model │
               └────────────────┬───────────────┘
                                │
                                ▼
               ┌────────────────────────────────┐
               │  CI Completion & Debounce Bar  │
               │  - 0 IN_PROGRESS / QUEUED      │
               │  - 90–120s quiet period        │
               └────────────────┬───────────────┘
                                │
                                ▼
               ┌────────────────────────────────┐
               │   Circuit Breaker & Dedup DB   │
               │  - SQLite (repo, pr, sha, act) │
               │  - Max 3 attempts per SHA      │
               └────────────────┬───────────────┘
                                │
                                ▼
                    Dispatch Swarm Session
               (In-Process via _do_launch())
```

### 1. Fast Path vs. Slow Path Architecture

- **Fast Path (Event-Driven Polling):** Polls `GET https://api.github.com/repos/{owner}/{repo}/events` with `If-None-Match: <etag>`. When no activity occurred, GitHub returns `304 Not Modified` consuming **0 rate-limit cost**. On `200 OK`, the daemon wakes up and scans open PRs for that repo.
- **Slow Path (Hybrid Safety Net):** Runs a full periodic sweep every 30–60 minutes across event-scoped repos to catch untracked backend state transitions such as merge conflicts (`mergeable: dirty` generates no GitHub event stream payload).

### 2. Scoped Watched-Repo Resolution

To minimize API consumption and avoid unnecessary network calls:
- **Rule:** The watcher **only polls repositories that have at least one enabled `event` trigger**.
- Repositories configured with cron schedules (e.g. daily CVE audits or weekly package updates) are handled in-process by Swarmer's internal `swarmer/scheduler.py` loop and are **never polled** by the watcher daemon.
- The watched-repo set is dynamically refreshed on an interval; stale ETags are purged when a repository is removed.

### 3. Author Routing Taxonomy & "My PRs" Resolution

Each trigger defines an **Author Scope** and **Event Condition**:

| Author Scope | Who Matches | Target Action | Default Behavior & Prompts |
|---|---|---|---|
| **`My PRs` (`self`)** | Configured `fix_authors` (comma-separated logins in schedule) | `pr-fix` | Resolves conflicts, reproduces & fixes CI failures, addresses review comments, pushes to PR branch, and tags `@coderabbitai review and approve`. Fork PRs require maintainer edit permissions. |
| **`Team PRs` (`team`)** | Trusted collaborators (`OWNER`, `MEMBER`, `COLLABORATOR`, or on allowlist) | `pr-review` | Detached worktree analysis, scored multi-lens review, inline feedback. Read-only on PR branches. |
| **`Bot PRs` (`bots`)** | Automated bot logins (`dependabot[bot]`, `renovate[bot]`, `cve-*`, `app/*`) | `auto-merge-defer` | Defers to repo's GitHub Actions (`auto-merge-approved.yaml`) or triggers autonomous bot fix prompts. |
| **`All PRs` (`all`)** | **My PRs + Team PRs + Bot PRs + External PRs** | Context-dependent | Evaluates any matching author against the trigger condition while strictly enforcing the 3-layer trust model. |

### 4. 3-Layer Team-PR Trust Model & Security Guardrails

To prevent arbitrary code execution, compute/token exhaustion, and prompt injection attacks from untrusted external contributors:

1. **Layer 1: Native GitHub Author Association (Default)**
   - Automatically trusts PR authors with `OWNER`, `MEMBER`, or `COLLABORATOR` associations.
   - Treats `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, and `NONE` as **untrusted** by default.
2. **Layer 2: Workspace Trust Policy**
   - Configurable explicit allowlist of logins or GitHub organization team memberships (`GET /orgs/{org}/teams/{slug}/members`).
3. **Layer 3: The `ok-to-review` Label Gate**
   - Untrusted external PRs remain ignored until a repository collaborator applies the `ok-to-review` label (Kubernetes/Prow convention).
   - **RBAC Protected:** Applying labels on GitHub requires Triage, Write, or Admin permissions on the base repository; external fork authors cannot self-apply this label. When label timeline events are available, the watcher verifies the label applier's identity and falls back to base repository RBAC when timeline data is unavailable.
   - **Invalidation:** New commits pushed to an external PR automatically invalidate prior approval and require re-evaluation.

### 5. Resilience, Circuit Breaker & Concurrency

- **CI Completion Barrier & Debounce:** Check runs must show 0 `IN_PROGRESS` or `QUEUED` checks, plus a 90–120s quiet-period debounce before dispatching `pr-fix`.
- **Circuit Breaker:** Maximum 3 fix attempts per `head_sha`. If the agent fails to resolve CI after 3 attempts, the status is marked `blocked`. A new human commit to the branch resets the counter.
- **Self-Trigger Guard:** Events generated by the bot agent's own commits are ignored to prevent feedback loops.
- **Capacity Back-pressure:** Dispatches automatically inherit Swarmer's `MAX_CONCURRENT_AGENTS` queueing.

### 6. UI & Observability

- **Trigger Type Selector:** Available in both Add Schedule and inline Edit forms (`_schedule_items.html`), supporting live conversion between Cron and Event triggers.
- **Visual Pills:** Gold `⚡ Event: <label>` pills render in the Session List, Status Badges, and Run History table.
- **Drawer Context Logging:** Expanding a run record in Run History displays full triggering metadata (`repo`, `PR #`, `head_sha`, `action`, `title`).

---

## Debugging & Log Retrieval Reference

When diagnosing system behavior, Swarmer provides multiple layers of logs and state observability:

### 1. Swarmer Server & In-Process Watcher Logs
- **In-Cluster Pod Logs:**
  ```sh
  kubectl logs -n swarmer -l app=swarmer -f --tail=200
  ```
- **Log Level Adjustment:** Set `LOG_LEVEL=DEBUG` in `swarmer-extra-env` or `.env` to enable verbose logging for `swarmer.pr_watcher`, `swarmer.scheduler`, `swarmer.openshell_client`, and `swarmer.routers.sessions`.
- **Local Dev Server:** Look at terminal stdout where `make dev` is running.

### 2. Agent Execution Outputs (Processed vs. Raw Logs)
- **Web UI:**
  - On the **Output Tab** of any session: use the toggle button (`[Output] | [Raw Log]`) to switch between the clean assistant response and the raw ANSI console log.
  - On the **History Tab**: click the expand chevron on any past run to open the execution drawer. Use the `[Output]` and `[Raw Log]` pills to view logs from that specific run.
- **REST API:**
  ```sh
  # Get latest run output (clean + raw)
  curl -s -H "Authorization: Bearer $TOKEN" "$SWARMER_URL/api/v1/workspaces/$WS_ID/sessions/$SID/output"
  
  # List all historical runs with metadata
  curl -s -H "Authorization: Bearer $TOKEN" "$SWARMER_URL/api/v1/workspaces/$WS_ID/sessions/$SID/runs"
  ```

### 3. Watcher State & Circuit Breaker Inspection
All watcher dispatch history, attempt counters, and cached ETags are stored in the SQLite database (`$SWARMER_DB_PATH`):
```sh
# Inspect circuit breaker and dispatch status
sqlite3 "$SWARMER_DB_PATH" "SELECT repo, pr_number, head_sha, action, status, attempts, last_dispatched_at, last_error FROM pr_action_state ORDER BY updated_at DESC LIMIT 20;"

# Inspect cached GitHub Events ETags
sqlite3 "$SWARMER_DB_PATH" "SELECT repo, etag, last_checked_at FROM repo_etags;"
```

### 4. OpenShell Sandbox & Gateway Logs
- **Gateway Logs (Credential Injection & Proxy Routing):**
  ```sh
  kubectl logs -n openshell -l app.kubernetes.io/component=gateway -f --tail=100
  ```
- **Supervisor Logs (Sandbox Lifecycle & gRPC Exec):**
  ```sh
  kubectl logs -n openshell -l app.kubernetes.io/component=supervisor -f --tail=100
  ```
- **Active Sandbox Pod Logs (Raw container logs):**
  ```sh
  # Find sandbox pod
  kubectl get pods -n openshell-sandboxes
  kubectl logs -n openshell-sandboxes <sandbox-pod-name> -f
  ```

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
3. Credentials are injected at sandbox launch time via the OpenShell Gateway — no K8s Secret sync required

### Adding a new agent tool

1. Create `swarmer/agent_tools/new_tool.py` implementing the `AgentToolStrategy` abstract methods: `get_image`, `build_config_data`, `get_container_name`, `get_server_port`, `get_share_dir`, `build_share_setup_cmd`, `build_model_setup_cmd`, `build_main_cmd`, `get_model_options`, `get_default_model`
2. Register in `agent_tools/registry.py:_init()`
3. Add the tool name to `AGENT_TOOLS` tuple in `models/session.py`
4. Add `agent_image_new_tool: str = ""` in `config.py:Settings`
5. Add corresponding `AGENT_IMAGE_NEWTOOL` env var in `.env.example` and Makefile placeholders

### Adding a new MCP server (OpenShell sandbox network policy)

OpenShell sandboxes enforce outbound network access at **two layers** — both must be configured
or the MCP server will fail to connect even if one layer is open:

1. **OPA/Landlock** — controls which binary processes may open which network connections.
   Configured via `swarmer/openshell_policy.py`. Each rule is a `{host, port, binary}` triplet.
2. **Egress proxy** (`HTTP_PROXY=10.200.0.1:3128`) — a CONNECT proxy that gates all sandbox
   HTTPS traffic. It enforces the same OPA policy at the proxy layer. A wildcard like
   `*.example.com` in the OPA rule may not be sufficient; the proxy may require a literal
   host match as well (confirmed with `redhat.atlassian.net` — wildcard `*.atlassian.net`
   alone produced a 403 at the proxy; adding the literal host fixed it).

**The key gotcha — OPA resolves canonical binary paths:**

OPA identifies processes by resolving symlinks via `/proc/{pid}/root`. It sees the canonical
binary path, not the symlink. For example, `/usr/bin/python3 → python3.14`, so the rule
must list `/usr/local/bin/python3.14` (confirmed via OPA draft chunks), not just
`/usr/bin/python3`. Always check draft chunks after the first run to discover the actual
path OPA reports.

**Step-by-step: adding a new MCP server**

1. **Add the catalog entry** in `swarmer/mcp_catalog.py` with `slug`, `display_name`,
   `command`, and any credential defaults.

2. **Add credential fields** to `McpServer` in `swarmer/models/mcp_server.py` following the
   `_enc` suffix + `@property` encrypt/decrypt pattern. Add an `ALTER TABLE` migration in
   `database.py:migrate_db()`.

3. **Inject credentials into the sandbox** in `swarmer/openshell_client.py:create_provider()`.
   Match on `"<slug-keyword>" in getattr(mcp, "slug", "")` (loose match) and populate
   `env_vars` from the model's direct fields. Credentials go into `SandboxSpec.environment`
   at sandbox creation time. Note: `spec.environment` reaches the supervisor-launched agent
   process but **not** ad-hoc `client.exec()` calls — write a `/sandbox/.tool.env` file via
   `stdin` if exec commands also need the vars.

4. **Add the network policy block** in `swarmer/openshell_policy.py`:
   - Add a `_TOOL_MCP_BLOCK` constant with `endpoints` and `binaries`.
   - For `binaries`: list both the entry-point binary (`/usr/local/bin/tool-server`) and
     the underlying interpreter if it is a scripted tool (e.g. `python3.14`, `node`).
     Use `_bin(path)` for every entry — `harness=True` is mandatory.
   - For `endpoints`: list both a wildcard (`*.example.com`) **and** the specific literal
     hostname (`tenant.example.com`) used in production. Wildcards alone are unreliable at
     the proxy layer.
   - Wire the block into `build_session_network_policies()` with a slug keyword check:
     `if any("<keyword>" in getattr(mcp, "slug", "") for mcp in (mcp_servers or []))`.

5. **Update the unit tests** in `tests/test_openshell_policy.py` and
   `tests/test_openshell_client.py`. Use the real `slug` value from the catalog (not a
   synthetic `"jira"` shorthand) so the tests catch slug-mismatch bugs.

6. **Write a dedicated e2e smoke test** at `scripts/openshell_<tool>_smoke_test.py`.
   Use `scripts/openshell_jira_smoke_test.py` as the template. The test must:
   - Read credentials from the **process environment** (never from Python variables) —
     source the tool's `.env` file before running: `set -a && source .env && set +a`.
   - Write credentials into the sandbox via `stdin` to `/sandbox/.tool.env`, then
     `source` that file in subsequent `exec()` calls.
   - Validate network access with `curl -v` using env var refs (`$VAR`) inside the sandbox
     shell — the `-v` output reveals whether the failure is a proxy 403 (policy gap) or a
     DNS/TLS error (different problem).
   - Run the MCP server binary with a JSON-RPC `initialize` request over stdin. A valid
     MCP `initialize` response confirms the full stack works end-to-end.
   - After the run, query `GetDraftPolicy` on the sandbox to surface any pending OPA draft
     chunks — these are the **policy sub-bumps** (missing binary or host entries) that need
     to be added to the policy block before the tool will work reliably.

   **Iterating on policy gaps (the sub-bump loop):**

   ```
   Run smoke test
     → step 9 fails with ProxyError / ConnectionError
     → query GetDraftPolicy on the sandbox (before cleanup)
     → draft chunk shows: binary=/usr/local/bin/python3.14 host=tenant.example.com
     → add that binary + literal host to the policy block constant
     → re-run smoke test
     → repeat until 18/18 (or N/N) passes with "No OPA network denials"
   ```

   To inspect draft chunks mid-test without cleanup, temporarily add this after the
   jira-mcp-server exec step:
   ```python
   req = openshell_pb2.GetDraftPolicyRequest()
   req.name = sandbox_name
   resp = client._stub.GetDraftPolicy(req, timeout=10)
   for c in resp.chunks:
       print(c.proposed_rule.binaries[0].path, c.proposed_rule.endpoints[0].host)
   ```

7. **Run the smoke test against a real cluster** (OpenShell gateway must be reachable via
   port-forward or direct URL):
   ```sh
   set -a && source ../my-mcp-server/.env && set +a
   python3 scripts/openshell_<tool>_smoke_test.py
   ```

**Reference implementation:** `scripts/openshell_jira_smoke_test.py` + `_JIRA_MCP_BLOCK`
in `swarmer/openshell_policy.py` — worked through the full sub-bump loop to reach 18/18.

## Agent Swarm MCP Server (`mcp-server/`)

The standalone Agent Swarm MCP Server (`agent-swarm-mcp-server`) exposes Swarmer's full REST API (`/api/v1/`) as Model Context Protocol tools for AI agent orchestration. This enables developer-agent interfaces (OpenCode, Claude Code) or agent-in-sandbox workloads to launch, monitor, configure, and orchestrate other Swarmer sessions programmatically.

### Architecture & Data Flow

```text
AI Coding Agent (OpenCode / Claude Code)
         │
         ▼  (stdio / SSE MCP transport)
Agent Swarm MCP Server (`mcp-server/`)
  ├── FastMCP Server (`agent_swarm_mcp_server/server.py`)
  ├── API Client (`agent_swarm_mcp_server/client.py`)
  └── Auth Token Resolver (`agent_swarm_mcp_server/auth.py`)
         │
         ▼  (HTTPS Bearer Token Authorization)
Swarmer REST API (`/api/v1/`)
  ├── Workspaces & ACL Memberships
  ├── Global Admin & User Identity (/me)
  ├── Agent Sessions (Launch / Stop / Monitor / History)
  └── Prompts, Repositories, PATs, & Schedules
```

### Available Tool Capabilities

| Domain | MCP Tools | Description |
|---|---|---|
| **Workspaces & ACL** | `list_workspaces`, `get_workspace`, `create_workspace`, `update_workspace`, `delete_workspace`, `list_workspace_members`, `add_workspace_member`, `remove_workspace_member` | Full workspace CRUD and explicit member access management (ACM-41659 database ACL). |
| **Identity & Admins** | `get_me`, `list_known_users`, `list_admins`, `add_admin`, `remove_admin`, `bootstrap_admin` | Query authenticated caller identity and permissions; manage global Swarmer admins. |
| **Session Lifecycle** | `list_sessions`, `find_sessions_by_repo`, `get_session`, `create_session`, `update_session`, `delete_session`, `launch_session`, `stop_session`, `get_session_status`, `get_session_output`, `wait_for_session` | Launch, stop, monitor, and await agent execution runs across OpenCode and Shell tools. |
| **Repos & Prompts** | `add_repo_to_session`, `remove_repo_from_session`, `list_workspace_prompts`, `set_session_prompt`, `list_github_pats` | Attach git repositories and configure prompts or private git PAT credentials. |
| **Schedules** | `list_session_schedules`, `add_session_schedule`, `update_session_schedule`, `delete_session_schedule` | Manage automated cron schedules and schedule-specific prompt overrides. |

### Authentication & Token Resolution

The MCP server resolves Kubernetes bearer tokens in `agent_swarm_mcp_server/auth.py` in priority order:

1. **`AGENT_SWARM_API_TOKEN`** env var (explicit token override; always wins).
2. **In-cluster ServiceAccount token** at `/var/run/secrets/kubernetes.io/serviceaccount/token` (used when deployed as a sidecar or in-pod agent).
3. **Kubeconfig Context** (`$KUBECONFIG` or default kubeconfig file):
   - Direct `token` field on current user.
   - Exec credential provider output (common with `oc login` and cloud IAM providers).
   - Validated against Swarmer's `/api/v1/` endpoints with fallback resolution for OpenShift OAuth tokens.

### Setup & CLI Automation

- **Web UI (`/token`):** Authenticated users can visit `/token` directly from the masthead navigation to view their active token, API endpoint URL, and a ready-to-copy `opencode.json` configuration block.
- **CLI Automation (`make mcp-setup` / `make api-info`):**
  - `make mcp-setup`: Configures the local `opencode.json` file in the project with the detected Swarmer route and token.
  - `make api-info`: Prints current API endpoint, decoded user identity, and the MCP JSON snippet.

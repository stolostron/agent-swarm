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
├── mcp-server/                 # Standalone MCP server for session orchestration
├── tests/                      # Test suite
│   ├── test_api.py              # REST API unit tests (in-memory SQLite, no server)
│   ├── test_list_repos_for_pat.py  # GitHub API helpers (respx mocking)
│   ├── test_openshell_client.py # OpenShell client wrapper tests (mocked SDK, no package required)
│   └── test_ui_patternfly.py   # Playwright e2e tests (requires running server at :8091)
└── swarmer/                    # Python package (the application)
    ├── main.py                 # FastAPI app, lifespan, middleware, router registration
    ├── config.py               # pydantic-settings Settings singleton
    ├── database.py             # SQLAlchemy async engine + session factory + migrations
    ├── crypto.py               # Fernet encrypt/decrypt from secret key file or env var
    ├── k8s_auth.py             # K8s TokenReview validation, namespace access check, RBAC probing
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
    │   ├── workspace.py        # Workspace → K8s namespace (or shared via settings.k8s_namespace)
    │   ├── session.py          # Session (sandbox lifecycle, modes: tui/server/prompt, cron scheduling)
    │   ├── session_repo.py     # Git repos attached to sessions (cloned into sandbox at launch)
    │   ├── sandbox_env_var.py  # Per-workspace env vars (encrypted at rest, injected into sandboxes)
    │   ├── opencode_secret.py  # Fernet-encrypted provider credentials (GCP/Gemini)
    │   ├── github_pat.py       # Fernet-encrypted GitHub PATs for HTTPS git auth
    │   ├── github_app.py       # Fernet-encrypted GitHub App credentials (one per workspace)
    │   └── mcp_server.py       # MCP server configs with Fernet-encrypted OAuth tokens
    ├── routers/                # FastAPI route handlers
    │   ├── auth.py             # /login (token paste + OpenShift OAuth), /logout, /auth/callback
    │   ├── workspaces.py       # CRUD for workspaces
    │   ├── sessions.py         # CRUD + launch/stop/schedule/patch generation + repo management
    │   ├── secrets.py          # OpenCode secrets, GitHub PATs, GitHub App, pull secrets
    │   ├── mcp_servers.py      # MCP server CRUD, OAuth 2.1 flow (PKCE + dynamic registration)
    │   ├── chat_proxy.py       # HTTP/SSE/WebSocket reverse proxy for server-mode sessions
    │   └── tui_ws.py           # WebSocket PTY proxy for TUI-mode sessions (K8s exec)
    ├── api/v1/                 # REST API — 51 endpoints under /api/v1/
    └── templates/              # Jinja2 HTML templates (PatternFly 6 dark theme + HTMX)
        ├── base.html           # Layout with masthead, flash messages, PatternFly CDN
        ├── workspaces/         # list, detail, new, edit, _delete_confirm
        ├── sessions/           # list, detail, new, _status_badge, _last_output, _repo_list, etc.
        ├── secrets/            # tabs, opencode_form, github_pat_form, github_pat_list
        └── mcp_servers/        # list (catalog + configured servers with OAuth status)
```

## Design Principles

**Favor encrypted database over Kubernetes objects** — Credentials, configuration, and application state are stored in the encrypted SQLite database (Fernet at rest) rather than K8s Secrets or ConfigMaps. This simplifies RBAC requirements, provides an audit trail via timestamps, and makes the application more portable. The only remaining K8s storage is `swarmer-agent-extra-env` (pending migration in ACM-35039) and image pull secrets (which require K8s to function).

**OpenShell is the sole session runtime** — All agent session lifecycle (create, exec, stop, delete) goes through the OpenShell Gateway + Supervisor APIs. Swarmer does not create K8s pods, PVCs, Services, or Routes for agent sessions.

**Minimal K8s surface** — Swarmer's K8s usage is limited to: authentication (TokenReview via `k8s_auth.py`), image pull secrets (for `check_image_reachable`), and workspace namespace scoping. All credential injection for agent sessions is handled by the OpenShell Gateway.

## Domain Model

- **Workspace** maps to a Kubernetes namespace for scoping purposes. All resources (sessions, secrets) are scoped to a workspace. When `settings.k8s_namespace` is set (namespace-scoped deployment), all workspaces share a single K8s namespace via `Workspace.k8s_namespace` property.
- **Session** = an agent run inside an OpenShell sandbox. Three modes:
  - `prompt` — one-shot: runs the agent with a prompt, sandbox exits on completion, sandbox auto-deleted on success
  - `server` — persistent: runs the agent in server mode, exposes a service via OpenShell `expose_service()`, dashboard proxies HTTP/WS/SSE to it
  - `tui` — persistent: runs `sleep infinity`; user connects via xterm.js WebSocket → OpenShell `exec_interactive()` PTY
- **Session phases**: `idle` → `pending` → `running` → `succeeded`/`failed`/`stopped`
- **Cron scheduling** — sessions of any mode can have a cron schedule (`cron_schedule` field). A background asyncio loop (`scheduler.py`) checks every 30s, uses an atomic `UPDATE … RETURNING` to claim due rows (prevents duplicates), sets `session.mode = "prompt"` before calling `_do_launch()` (scheduled runs always execute in prompt mode regardless of the session's configured mode), then calls the shared `_do_launch()` helper in `sessions.py`.
- **OpencodeSecret** — per-workspace encrypted storage for GCP project, Vertex location, ADC JSON, and Google API key. Stored in SQLite via Fernet encryption. Despite the legacy name, used by OpenCode. Multiple rows per workspace are tolerated (one per `user_id`); read paths use `.scalars().first()` to avoid `MultipleResultsFound` when users share a workspace. A `UNIQUE (workspace_id, user_id)` constraint prevents future duplicates, with a deduplication migration that keeps the newest row per pair.
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
- Manual migrations in `database.py:migrate_db()` — uses `ALTER TABLE ... ADD COLUMN` wrapped in try/except (idempotent; only suppresses "duplicate column"/"already exists"/"no such column" errors, all others re-raise). Also supports `CREATE TABLE IF NOT EXISTS`, `CREATE UNIQUE INDEX IF NOT EXISTS`, and data-fixup `DELETE` statements.
- All models must be imported in `models/__init__.py` for table registration to work
- SQLite single-writer: K8s Deployment uses `strategy: Recreate` (not RollingUpdate)
- **`NullPool` for SQLite** — `init_db()` uses `NullPool` instead of the default `QueuePool` for SQLite connections. `aiosqlite` opens a new OS-level connection on every call and does not benefit from connection pooling; `QueuePool`'s default limit of 5+10 connections would be exhausted under concurrent chat proxy load (one DB lookup per proxied asset). `NullPool` creates and closes connections on-demand with no cap, matching `aiosqlite`'s actual behaviour.

## Kubernetes Integration

Swarmer uses the official `kubernetes` Python client for a limited set of infrastructure operations. All agent session lifecycle is handled by OpenShell — Swarmer does not create pods, PVCs, Services, or Routes for sessions.

**Active K8s usage:**
- `k8s_auth.py` — TokenReview for user authentication; namespace access validation
- `k8s.init_k8s()` — loads in-cluster or kubeconfig at startup
- `k8s.ensure_namespace()` / `delete_namespace()` — workspace namespace lifecycle
- `k8s.effective_namespace()` — resolves the effective K8s namespace for a workspace
- Pull secret management (`apply_pull_secret`, `get_pull_secret_info`, `delete_pull_secret`) — required for `check_image_reachable`
- `get_extra_env_vars()` / `set_extra_env_var()` / `delete_extra_env_var()` — workspace env var storage via K8s Secret `swarmer-agent-extra-env` (**ACM-35039**: migrating to SQLite)

All kubernetes client imports remain lazy (inside functions) to avoid import errors when K8s is not configured.

## OpenShell Integration

[NVIDIA OpenShell](https://github.com/nvidia/openshift-ai-openShell) replaces direct K8s pod and Secret management with a Gateway + Supervisor model. Swarmer sends credentials to the Gateway (which injects them securely) and requests sandboxes from the Supervisor (which provides the isolated runtime). Swarmer never writes AI tokens or PATs into K8s Secrets again.

- **Gateway** -- credential injection API; Swarmer sends AI tokens, PATs, and MCP tokens to the Gateway, which injects them as env vars into the sandbox. No K8s Secrets written for session credentials.
- **Supervisor** -- sandboxed agent runtime; `create_sandbox()` provisions the sandbox, `delete_sandbox()` tears it down.
- **Sandbox lifecycle** -- fully managed by OpenShell. No K8s pods, PVCs, or Services created for sessions.
- **No PVCs** -- sandbox filesystem is ephemeral; repos are cloned fresh each launch via OpenShell API.
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
| `create_sandbox()` | `async (image, env_vars, policy, provider_names?, client?) → SandboxRef` | Creates sandbox, waits ready, returns ref |
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
| `gemini_preset_plan_model` | `GEMINI_PRESET_PLAN_MODEL` | `google/gemini-3.1-pro-preview` | Gemini preset's PLAN-role model |
| `gemini_preset_build_model` | `GEMINI_PRESET_BUILD_MODEL` | `google/gemini-3.6-flash` | Gemini preset's BUILD-role model |
| `gemini_preset_small_model` | `GEMINI_PRESET_SMALL_MODEL` | `google/gemini-3.5-flash-lite` | Gemini preset's small/housekeeping model |
| `opencode_experimental_plan_mode` | `OPENCODE_EXPERIMENTAL_PLAN_MODE` | `true` | Enables the opencode plan agent so the PLAN-role model above is actually used |

For local dev (`make dev`), the same env vars are set via `.env` (see `.env.example`) — no
ConfigMap involved outside a real cluster deployment.

## Agent Container Data Interface

Every data item Swarmer currently pushes into agent pods, its source model, the current K8s mechanism, and the target OpenShell API call. This table is the migration contract for ACM-34850.

| Category | Data | Source Model | Current K8s Mechanism | Target OpenShell API |
|---|---|---|---|---|
| AI Credentials | GCP Project, Vertex Location, ADC JSON, Gemini key | `OpencodeSecret` | K8s Secret → `envFrom` | Gateway `create_provider()` env injection |
| Git Auth | PAT token, GitHub username | `GitHubPAT` | K8s Secret → `secretKeyRef` + init container credential store | Gateway credential injection + `clone_repos()` |
| Git Repos | repo_url, branch, local_path (per repo) | `SessionRepo` | Init container git clone | `openshell_client.clone_repos()` |
| MCP Tokens | Jira URL, Jira access token, Jira email | `McpServer` | K8s Secret → `envFrom` | Gateway env injection |
| Agent Config | Tool-specific JSON + gitconfig | ConfigMap | Volume mount at `/tmp/agent-config-ro` | `write_agent_config()` into sandbox |
| MCP Config | MCP server definitions in agent config JSON | `McpServer` | Startup script overwrites config | `write_file()` into sandbox |
| Model Config | model.json (OpenCode) | `Session.model` | Startup script writes JSON file | `write_file()` into sandbox |
| Prompt | instruction_prompt + base_prompt + repo_context | `Session` + `WorkspacePrompt` | CLI arg (prompt mode) or `SWARMER_AGENT_MD` env → AGENTS.md (TUI/server) | `write_agents_md()` to `/sandbox/AGENTS.md` for **all modes**; prompt mode reads it via `$(</sandbox/AGENTS.md)` shell expansion; TUI/server agent reads it automatically |
| Env Vars | HOME, NODE_OPTIONS, GOOGLE_APPLICATION_CREDENTIALS | Hardcoded | Pod env spec | Sandbox env vars via Gateway |
| Extra Env | Arbitrary workspace key-value pairs | External K8s Secret | `envFrom` (`swarmer-agent-extra-env`, optional) | Gateway env injection |
| Volumes | PVC → /workspace, ConfigMap → /tmp/agent-config-ro, ADC → /app/gcloud | N/A | Pod volume spec | Sandbox filesystem (no separate volumes) |
| Startup Script | Config copy, safe dir, git creds, symlinks, AGENTS.md write, model write, branch checkout | N/A | `sh -c` command chain | Simplified script — removes credential setup and git clone stages |
| Pod Config | Resources (1Gi-8Gi/500m-2000m), fsGroup, runAsUser, imagePullPolicy, restartPolicy | `Session` + `Settings` | Pod spec | Sandbox resource config |
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
- AI-generated commit messages via Vertex AI Claude, Anthropic API, or Gemini API (falls back to simple file-list summary)
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

Launch pills are ordered TUI → CHAT → PROMPT (most-used first). TUI and CHAT use green fill (`.launch-pill-green`); PROMPT uses a dark charcoal fill with green border (`.launch-pill-muted`). Each pill POSTs the full config form to `/launch` with `mode` and `save_config=1` — no separate save step required.

**Agent tool pill** — OpenCode is currently the only supported agent tool, shown as a static, always-selected pill rendering the official block-pixel SVG logo inline at 78×14px with a rainbow gradient border. The underlying registry/strategy pattern supports adding more tools in the future without further UI changes.

**Cluster capacity indicator** — a single pill labelled `Sessions: X / Y active` with optional `· N queued` appended. Colour escalates: outline (0 active) → green (healthy) → gold (near/at capacity: `active >= max-1` for `max > 2`, `active == max` for `max ≤ 2`) → red (any queued). Rendered in both `detail.html` and `_list_rows.html`.

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

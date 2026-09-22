# Swarmer Architecture Reference

Agent-first reference for locating ownership, following runtime flows, and avoiding
incorrect changes. Read this before searching the repository. Paths are relative to the
repository root; symbols are the preferred code entry points.

## Architectural Invariants

- **OpenShell owns agent runtimes.** Swarmer does not create session pods, PVCs, Services,
  Routes, or session credential Secrets. It calls the OpenShell Gateway and Supervisor.
- **The Gateway owns credential injection.** AI credentials, GitHub credentials, MCP
  credentials, and workspace environment variables are registered with Gateway providers
  and injected into sandboxes at launch.
- **Kubernetes authenticates identity, not workspace authorization.** `k8s_auth.py` uses
  TokenReview or its fallback identity probe. Workspace access is enforced by the database
  ACL in `workspace_acl.py`; do not add K8s RBAC checks to workspace operations.
- **SQLite is the application state store.** SQLAlchemy async + `aiosqlite` is used with
  `NullPool`; schema creation and manual migrations are in `database.py`. The deployment
  uses `strategy: Recreate` because SQLite has a single-writer model.
- **Sensitive persisted fields use Fernet.** Encrypted model columns end in `_enc` and
  expose transparent `@property` accessors. `init_crypto()` must run before DB/model use.
- **Network policy fails closed.** OpenShell policy must allow both the destination host
  and the canonical executable path. Wildcard hosts alone are not sufficient for the
  egress proxy.
- **`/sandbox` is the agent workspace and home.** It is an OpenShell-managed PVC. Agent
  tools and runtime caches that must survive or be accessed without approval belong in
  user-home paths, not `/tmp`.
- **REST and MCP surfaces stay aligned.** Changes to `/api/v1/`, schemas, session options,
  or tools require corresponding updates under `mcp-server/`.

## Subsystem Routing Table

| Concern | Read first | Key symbols / facts |
|---|---|---|
| App boot and shutdown | `swarmer/main.py` | `app`, `lifespan`; initializes crypto/DB, starts background loops, resumes sessions |
| Configuration | `swarmer/config.py` | `Settings`, module-level `settings`; env var bindings and defaults |
| HTTP/UI routing | `swarmer/routers/`, `swarmer/api/v1/` | Router modules; REST API is under `/api/v1/` |
| Authn | `swarmer/k8s_auth.py`, `swarmer/deps.py`, `swarmer/routers/auth.py` | TokenReview, `require_auth`, `get_user_token`, login/OAuth callbacks |
| Workspace authz | `swarmer/workspace_acl.py`, `swarmer/models/workspace_member.py`, `swarmer/models/global_admin.py` | Owner/member/admin checks; `can_access_workspace()` and user discovery |
| Session orchestration | `swarmer/routers/sessions.py` | `_do_launch()`, `_setup_openshell_sandbox()`, `_run_openshell_agent()`, stop/delete paths |
| Cron and queue | `swarmer/scheduler.py` | 30-second cron/queue loop; `_process_queue()`; sandbox GC; capacity limit |
| GitHub PR events | `swarmer/pr_watcher.py` | In-process ETag polling, trust filters, debounce, circuit breaker, event dispatch |
| OpenShell SDK boundary | `swarmer/openshell_client.py` | `create_sandbox()`, `start_agent()`, `exec_command()`, `exec_interactive()`, `expose_service()` |
| OpenShell policy | `swarmer/openshell_policy.py` | `build_session_network_policies()`; OPA/Landlock + egress proxy rules |
| Gateway selection | `swarmer/openshell_client.py`, `swarmer/openshell_oidc.py` | `GatewayConfig`, `resolve_gateway_config()`, `get_client_for_workspace()`, OIDC refresh |
| Credentials | `swarmer/crypto.py`, `swarmer/github_auth.py`, `swarmer/github_app.py` | Fernet; GitHub PAT/App; IAT minting and refresh |
| Persistence | `swarmer/database.py`, `swarmer/models/` | `init_db()`, `migrate_db()`, `Base`; import all models in `models/__init__.py` |
| Agent tool selection | `swarmer/agent_tools/` | `AgentToolStrategy`, `registry.py`, `opencode.py`, `shell.py` |
| Server-mode proxy | `swarmer/routers/chat_proxy.py` | HTTP/SSE/WebSocket proxy; `_resolve_upstream()` rewrites Gateway virtual host |
| TUI proxy | `swarmer/routers/tui_ws.py` | One-time browser token; xterm.js WebSocket to OpenShell interactive PTY |
| Run history | `swarmer/session_runs.py`, `swarmer/models/session.py` | `record_session_run()`; processed and raw output retention |
| K8s integration | `swarmer/k8s.py`, `swarmer/k8s_auth.py` | Auth identity, legacy pull secrets, candidate discovery; imports are lazy |
| MCP server | `mcp-server/agent_swarm_mcp_server/` | FastMCP server/client/auth mirror the Swarmer REST API |

## Domain Model

| Entity | Source | Meaning |
|---|---|---|
| `Workspace` | `swarmer/models/workspace.py` | Logical owner of sessions and credentials; namespace is only a legacy Secret slug |
| `WorkspaceMember` / `GlobalAdmin` | `swarmer/models/` | Database ACL membership and global administration |
| `Session` | `swarmer/models/session.py` | Agent run: `prompt`, `server`, or `tui`; phases include `idle`, `queued`, `pending`, `running`, `succeeded`, `failed`, `stopped` |
| `SessionRun` | `swarmer/models/session_run.py` | Historical completed execution, trigger metadata, processed/raw output |
| `SessionSchedule` | `swarmer/models/session.py` | `cron` or GitHub `event` trigger; cron sets prompt mode at launch |
| `SessionRepo` | `swarmer/models/session_repo.py` | Repository cloned into `/sandbox` at launch |
| `GitHubPAT` / `GitHubApp` | `swarmer/models/` | Encrypted Git credentials; App credentials produce short-lived IATs |
| `McpServer` / `SandboxEnvVar` | `swarmer/models/` | Encrypted MCP credentials and workspace environment injection |
| `WorkspaceGateway` | `swarmer/models/workspace_gateway.py` | Optional per-workspace OpenShell endpoint; otherwise global Gateway settings apply |

## Core Runtime Flows

### Application startup

`main.py:lifespan()` -> `init_crypto()` -> `init_db()` / `migrate_db()` -> initialize K8s
and settings -> restart surviving server/TUI sessions and GitHub IAT refresh loops -> start
`scheduler.py` and `pr_watcher.py` background tasks.

### Session launch

1. HTTP route, cron scheduler, or PR watcher calls `routers/sessions.py:_do_launch()`.
2. If `MAX_CONCURRENT_AGENTS` is full, persist `phase="queued"`; do not create a sandbox.
3. Resolve the workspace Gateway with `resolve_gateway_config()`.
4. Resolve providers and credentials; mint a GitHub App IAT when needed.
5. `openshell_client.create_sandbox()` creates the sandbox and applies policy/providers.
6. Clone `SessionRepo` entries and write tool config plus `/sandbox/AGENTS.md`.
7. Run the selected mode:
   - **prompt:** start a detached one-shot agent, stream output, record `SessionRun`, and
     delete the sandbox after successful completion.
   - **server:** start OpenCode server on port 4096, call `expose_service()`, store the
     virtual URL, and serve it through `chat_proxy.py`.
   - **tui:** keep the sandbox alive with `sleep infinity`; `tui_ws.py` launches the
     agent through `exec_interactive()` when the browser connects.

### Authentication and authorization

Bearer token -> `deps.py:require_auth()` -> `k8s_auth.py` TokenReview/fallback -> identity
in session cookie -> `workspace_acl.py:can_access_workspace()` -> owner, explicit member,
global admin, or configured shared-namespace policy. Handlers must use dependencies and
ACL helpers; they must not inspect bearer headers or implement ad hoc access checks.

### Cron and queue scheduling

`scheduler.py` checks every 30 seconds -> atomically claims due schedules -> changes the
scheduled run to prompt mode -> calls shared `_do_launch()` -> queues when capacity is full.
The same task performs FIFO queue processing and periodic orphan sandbox GC. Queued sessions
are not failures and have no sandbox to delete.

### Event-driven PR watcher

`pr_watcher.py` polls GitHub events with ETags -> scans open PRs only for repositories with
enabled event schedules -> applies author/trust filtering -> waits for completed CI and the
quiet period -> checks durable dedup/circuit-breaker state -> dispatches `_do_launch()`.
The watcher is in-process; there is no standalone watcher deployment or static trigger file.

## OpenCode and OpenShell Contract

OpenCode is the default agent tool selected by `swarmer/agent_tools/opencode.py`. Its image,
model configuration, command construction, and server/TUI binaries are provided by the
`AgentToolStrategy` interface. Do not hardcode OpenCode behavior in session orchestration;
add tool-specific behavior to `agent_tools/`.

| Use case | Swarmer behavior | OpenShell behavior |
|---|---|---|
| Headless prompt | Starts agent and monitors output; reads processed response from `.opencode/opencode.db` | Creates isolated sandbox, injects providers, deletes sandbox on success |
| Persistent server | Stores exposed service URL and proxies HTTP/SSE/WebSocket | Runs OpenCode server; `expose_service()` returns Gateway virtual host |
| Interactive TUI | One-time UUID auth then WebSocket PTY bridge | `exec_interactive()` runs the selected TUI binary |
| GitHub App auth | `github_auth.py` mints IAT; refresh loop restarts after app restart | Gateway provider receives refreshed token; raw private key never enters sandbox |
| MCP access | `mcp_catalog.py`, `McpServer`, `create_provider()` | Gateway env injection plus policy allowlist |

The server proxy must preserve the Gateway virtual `Host` header while connecting to the
real Gateway address. The virtual hostname is not necessarily resolvable from the Swarmer
pod. TUI agent startup belongs in the WebSocket flow, not sandbox setup.

## Container Image and Version Flow

`agent-containers` produces the runtime image consumed by this repository.

```text
agent-containers/Makefile pins
  -> make update-deps
  -> Containerfile.agents build args
  -> make publish-opencode
  -> agent-swarm/.push-defaults (REGISTRY + IMAGE_TAG, tracked source of truth)
  -> make sync-images
  -> .env: AGENT_IMAGE_OPENCODE
  -> deploy substitution: AGENT_IMAGE_OPENCODE_VALUE
  -> Settings.agent_image_opencode
  -> agent_tools/opencode.py:get_image()
  -> OpenShell create_sandbox(image=...)
```

### Producer: `../agent-containers`

- `Makefile` owns pinned `GO_VERSION`, `PYTHON_VERSION`, `OPENCODE_VERSION`, GitHub CLI,
  `rg`, `fzf`, LSP, MCP, and vulnerability scanner versions.
- `make update-deps` discovers and validates upstream versions, then updates the pins.
- Every version must flow through `Makefile` -> `scripts/build.sh` build args -> `ARG`
  declarations in `containerfiles/Containerfile.agents` (including the target stage).
- `make publish-opencode` builds and pushes the `opencode` image and updates the shared
  `agent-swarm/.push-defaults` contract. Commit tag/default changes with related code.
- `agent-containers/docs/ARCHITECTURE.md` and its `AGENTS.md` contain the producer-side
  version wiring and test requirement (`tests/test_lsp_version_pinning.sh`).

### Consumer: `agent-swarm`

- `make sync-images` reads `REGISTRY` and `IMAGE_TAG` from `.push-defaults`, validates both,
  and writes `AGENT_IMAGE_OPENCODE` to `.env`.
- `make image-build` depends on `sync-images`; `make deploy` substitutes the image and
  other deployment placeholders into `k8s/swarmer/deployment.yaml`.
- `swarmer/config.py` reads `AGENT_IMAGE_OPENCODE`; `opencode.py:get_image()` supplies it
  to sandbox creation. The image is not selected from the model or hardcoded in Python.
- OpenShell SDK and chart versions are pinned independently in this repository's `Makefile`
  as `OPENSHELL_VERSION` and `AGENT_SANDBOX_VERSION`; `make update-deps` updates both and
  the matching `requirements.txt` SDK pin.

## OpenShell Deployment Contract

`make deploy` is the canonical deployment path. The relevant implementation is in the
root `Makefile`; deployment manifests contain placeholders intentionally replaced at deploy
time. Do not replace those placeholders with environment-specific values in git.

1. Apply Swarmer namespace, RBAC, PVC, and model ConfigMap.
2. Install Agent Sandbox CRDs from the pinned `AGENT_SANDBOX_VERSION` if needed.
3. Install or upgrade the OCI chart
   `oci://ghcr.io/nvidia/openshell/helm-chart` at `OPENSHELL_VERSION`.
4. Use these required chart settings:
   - `server.auth.allowUnauthenticatedUsers=true`: port-forward/mTLS deployment does not
     require an additional Gateway JWT.
   - `server.drivers.kubernetes.workspaceMode=shared`.
   - Kubernetes Secret and Vault credential drivers disabled.
   - `server.policyValidationFailureMode=fail_closed`.
   - `server.workspaceDefaultStorageSize=$(OPENSHELL_WORKSPACE_STORAGE)`: size for newly
     created `/sandbox` PVCs; distinct from sandbox pod ephemeral-storage compute, which is
     hardcoded in `openshell_client.py`.
5. Extract `openshell-client-tls` to `auth/openshell/`, mirror it as `openshell-tls` in the
   Swarmer namespace, and mount it at `/auth/openshell`.
6. Discover the Gateway service URL and substitute it, the agent image, OAuth URL, pull
   policy, filesystem group, and concurrency limit into the Swarmer Deployment.
7. On OpenShift, apply the Route/OAuthClient and grant `anyuid` plus `privileged` SCCs to
   the OpenShell and sandbox service accounts. Generic Kubernetes uses the standard Service
   path and token-only login.

`make openshell-register` registers the current cluster Gateway under
`~/.config/openshell/gateways/<context>/` and preserves its local port. `make
connect-openshell` starts port-forwards for registered Gateways. `make connect` forwards
the Swarmer dashboard, not the agent sandbox.

## Security and Policy Details

- OpenShell sandbox network access has two enforcement layers: OPA/Landlock process rules
  and the egress proxy. Add both canonical binaries and literal production hosts when
  adding an MCP integration; see `openshell_policy.py` and its tests.
- OpenShift sandbox capabilities require `NET_ADMIN`, `SYS_ADMIN`, `SYS_PTRACE`, and
  `SYSLOG`; the deployment grants the required SCCs automatically when `oc` is available.
- Swarmer runs as non-root UID 1001. `/data` and `/auth` must be writable through the
  deployment's group permissions.
- `OpencodeSecret` is a legacy model name for AI provider settings. UI-saved ADC and Gemini
  credentials go to Gateway providers; legacy encrypted columns remain for compatibility.

## Change Routing

- Add an API feature: router -> API schema -> tests -> matching MCP client/tool/schema.
- Add persisted state: model -> `models/__init__.py` import -> `migrate_db()` -> tests.
- Add a credential: encrypted model property -> provider injection -> policy rules -> smoke
  and unit tests; never add a session Kubernetes Secret.
- Add an agent tool: implement `AgentToolStrategy`, register it, add model/config/image
  settings, then update OpenShell launch tests.
- Change a deployment version or image tag: update the owning Makefile/default contract and
  commit the version files together; run the version-pinning test.

# AGENTS.md — Swarmer

This file provides guidance to Claude Code (claude.ai/code) and other AI agents when working with code in this repository. CLAUDE.md is a symlink to this file.

A FastAPI + HTMX dashboard for managing AI coding agent workloads on Kubernetes. Uses OpenCode as its agent tool, via a pluggable strategy interface that supports adding more tools in the future. Server-rendered UI with PatternFly 6 dark theme. Token-based auth via Kubernetes ServiceAccount bearer tokens (+ optional OpenShift OAuth).

## Tool Availability

**GitHub:** MCP tools (`mcp__github-*`) and the `gh` CLI are equally valid — use whichever is configured. Never mix both in the same operation.

**Jira:** MCP tools (`mcp__jira-mcp-server__*`) and the `jira` CLI are equally valid — use whichever is configured. Never run raw `curl` against the Jira API.

## Commands

```sh
# Setup
make setup-secret        # Generate SWARMER_SECRET_KEY → auth/secret.key

# Development  (requires auth/secret.key — run make setup-secret first)
make dev                 # pip install + uvicorn at localhost:8090 with --reload, K8S_IN_CLUSTER=false
make lint                # ruff check swarmer/
rm -f data/swarmer.db   # Delete SQLite database (fresh schema on next start)

# Tests
make test                                            # Run all unit tests + mcp-server tests (excludes Playwright)
pytest tests/ -q --ignore=tests/test_ui_patternfly.py  # equivalent
pytest tests/test_api.py -q                          # Run a single test file
pytest tests/test_ui_patternfly.py                   # Playwright UI tests (requires running dev server at :8091 with SWARMER_DEV_AUTH=1)

# Container image
make image-build         # Build container image (podman by default; SILENT=1 to skip version prompt)
make image-push REGISTRY=...  # Push to registry

# Local kind cluster
make kind-deploy         # One-shot: create cluster + build + load image + deploy (includes OpenShell)
make kind-delete         # Tear down kind cluster

# Deploy / manage (OpenShell is installed automatically; auto-detects OpenShift vs generic K8s)
make deploy              # Deploy swarmer + OpenShell to current kubectl context
make delete              # Remove swarmer + OpenShell from current kubectl context
make status              # Show OpenShell and swarmer deployment status
make connect             # Port-forward localhost:8080 → swarmer dashboard
make connect-openshell   # Port-forward OpenShell gateway gRPC port
# See docs/OPENSHELL_LOCAL_SETUP.md for full setup walkthrough

# OpenShell e2e sandbox smoke tests (require port-forward to gateway + credentials in env)
# Source credentials first: set -a && source ../jira-mcp-server/.env && set +a
python3 scripts/openshell_smoke_test.py                          # OpenCode + Gemini (Google AI Studio)
python3 scripts/openshell_smoke_test.py --vertex                 # OpenCode + Claude via VertexAI
python3 scripts/openshell_smoke_test.py --policy-extract --repo https://github.com/org/repo  # git clone + policy
python3 scripts/openshell_jira_smoke_test.py                     # Jira MCP: env → policy → binary → mcp-server
# See docs/ARCHITECTURE.md "Adding a new MCP server" for how to write new smoke tests

# User management
make user-token SA_USER=alice                                      # Issue a K8s login token (default 8h)
make grant-workspace-access SA_USER=alice WORKSPACE_NS=my-proj     # Grant access to an existing workspace
make grant-workspace-create SA_USER=alice                          # Allow user to create new workspaces
# For OpenShift OAuth/OIDC users (e.g. GitHub identity provider) instead of a ServiceAccount
# token, use OIDC_USER=<name> in place of SA_USER=<name> — these are different RBAC
# principals (User vs ServiceAccount) and a grant for one does not apply to the other.
make grant-workspace-access OIDC_USER=<name> WORKSPACE_NS=my-proj
make grant-workspace-create OIDC_USER=<name>
```

## Architecture

For system architecture, data flows, module layout, and guidance on adding new features, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Sensitive Data Policy

**NEVER include any of the following in generated code, templates, configs, or comments:**

- API keys, tokens, passwords, or secrets (real or example-looking)
- User IDs, email addresses, or usernames
- GCP project IDs, Vertex locations, or service account details
- Container registry URLs or image references tied to a specific deployment
- Local filesystem paths (e.g. `/home/username/...`, `~/Desktop/...`)
- OAuth client IDs/secrets, kubeconfig contents, or cluster URLs
- Database connection strings with real hostnames or credentials

Use placeholder patterns instead: `<YOUR_PROJECT>`, `example.com`, `your-registry.example.com`, generic variable references (`settings.foo`), or environment variable lookups. Encrypted values must always go through the `crypto.encrypt()`/`crypto.decrypt()` pattern — never store or log plaintext secrets.

**GitHub App private keys** (RSA PEM) are stored encrypted via `GitHubApp.private_key_enc` and accessed only through the `private_key` property. The raw PEM is never logged, never included in API responses (`has_private_key: bool` is returned instead), and never passed to the OpenShell sandbox — only short-lived Installation Access Tokens (IATs) minted by `github_auth.mint_installation_token()` are injected.

## Code Conventions

### Python Style

- Python 3.12, type hints throughout (using `X | None` union syntax, not `Optional`)
- `Mapped[type]` for all SQLAlchemy columns (SQLAlchemy 2.x declarative style)
- Module-level singleton pattern: `settings = Settings()`, `_fernet: Fernet | None = None`
- Lazy kubernetes imports inside functions (avoid import errors when K8s isn't configured) — K8s is used only for auth, pull secrets, and namespace management
- `noqa: F401` on model imports in `__init__.py` and forward-reference strings in relationships

### Router Pattern

- Each router creates its own `templates = Jinja2Templates(directory="swarmer/templates")`
- Auth enforced via `dependencies=[Depends(require_auth)]` on every route (except `/login`, `/auth/callback`)
- DB access via `db: AsyncSession = Depends(get_db)`
- POST routes return `RedirectResponse(status_code=302)` (PRG pattern)
- HTMX endpoints return `HTMLResponse` or partial template renders
- Helper functions prefixed with `_` (e.g., `_get_workspace`, `_do_launch`)
- Error handling: `IntegrityError` → rollback + re-render form with error message

### Naming Conventions

- Model files: singular noun (`workspace.py`, `session.py`)
- Router files: plural noun matching the resource (`workspaces.py`, `sessions.py`)
- Template directories: plural noun matching the resource
- HTMX partial templates: prefixed with `_` (e.g., `_status_badge.html`, `_repo_list.html`, `_list_rows.html`)
- OpenShell sandbox names: `swarmer-session-{session_id}-{hex}` (auto-generated at launch)
- K8s resource names (infrastructure only): workspace namespace, `quay-pull-secret`, `swarmer-agent-extra-env` (env vars, pending ACM-35039 migration)
- URL pattern: `/workspaces/{ws_id}/sessions/{sid}/action`

### Design Principles

- **Favor encrypted database over Kubernetes objects** — Store credentials, configuration, and state in the encrypted SQLite database rather than K8s Secrets/ConfigMaps. Values go through `crypto.encrypt()`/`crypto.decrypt()` and are never stored or logged in plaintext.
- **OpenShell is the sole session runtime** — All agent session lifecycle goes through OpenShell Gateway + Supervisor APIs. Never create K8s pods, PVCs, Services, or Routes for sessions.
- **Minimal K8s surface** — New features should not add K8s dependencies. If you need to store data, use the DB. If you need to inject something into a sandbox, use the OpenShell Gateway API.

### Configuration

- `pydantic-settings` with `.env` file support, `extra="ignore"` (unrecognized env vars silently ignored)
- All settings have sensible defaults for local development
- Key env vars: `DATABASE_URL`, `SWARMER_SECRET_KEY`, `K8S_IN_CLUSTER`, `K8S_API_URL`, `OPENSHIFT_OAUTH_URL`, `OPENSHELL_GATEWAY_URL`, `OPENSHELL_SUPERVISOR_URL`
- Agent images: `AGENT_IMAGE_OPENCODE`, `DEFAULT_AGENT_TOOL`
- Concurrency: `MAX_CONCURRENT_AGENTS` (default 5) — global cap on concurrent agent pods; set to 0 to disable

### Testing

- Unit tests use `pytest` + `pytest-asyncio` + `respx` for HTTP mocking
- Tests stub model objects with plain classes (`_FakePAT`) to avoid SQLAlchemy/FastAPI dependencies
- Playwright e2e tests require a running dev server with `SWARMER_DEV_AUTH=1` at port 8091
- Test files use `sys.path.insert()` to add the parent dir for imports

## Gotchas & Non-Obvious Patterns

1. **Crypto init order matters**: `init_crypto()` must run before `init_db()` / `create_tables()` because model property accessors call `decrypt()`. The lifespan function in `main.py` enforces this order.

2. **`auth.py` is dead code**: The file `swarmer/auth.py` contains only a comment "superseded by k8s_auth.py". All authentication logic is in `k8s_auth.py` and `routers/auth.py`.

3. **Deployment image placeholder**: `k8s/swarmer/deployment.yaml` uses literal strings like `SWARMER_IMAGE`, `OPENSHIFT_OAUTH_URL_VALUE`, `AGENT_IMAGE_OPENCODE_VALUE` which are replaced at deploy time via `sed` in the Makefile. Don't replace them with actual values.

4. **SQLite single-writer**: The K8s Deployment uses `strategy: Recreate` (not RollingUpdate) because SQLite doesn't support concurrent writers. Only one replica is safe.

5. **Session mode affects sandbox lifecycle**:
   - `prompt` mode: sandbox runs the agent command once; on success, `_run_openshell_agent` auto-deletes the sandbox. On app restart, `_restart_prompt_pollers()` resumes monitoring via `exec_command_streaming`.
   - `server` mode: sandbox runs the agent serve command indefinitely; `expose_service()` creates a routable URL stored in `session.service_url`. The chat proxy rewrites the gateway-assigned virtual hostname to the real gateway address at connect time (`_resolve_upstream()`) and sets the `Host` header to the virtual domain for gateway routing — the domain itself is not DNS-resolvable from the Swarmer pod.
   - `tui` mode: sandbox runs `sleep infinity`; browser connects via xterm.js → WebSocket → OpenShell `exec_interactive()` PTY
   - Stopping always calls `openshell_client.delete_sandbox()` (and `delete_service()` for server mode)

6. **OpenCode model format quirk**: Model strings use `provider/model@version` format (e.g., `google-vertex-anthropic/claude-sonnet-5@default`). The `@version` suffix is part of the model ID; Haiku uses a date suffix (e.g., `@20251001`).

7. **TUI auth tokens**: TUI WebSocket connections use one-time UUID tokens stored in the HTTP session. Tokens are generated on the session detail page and consumed on WebSocket connect. Invalid/reused tokens are rejected with close code 4001.

8. **Session launch saves working branch**: If no working branch is specified, `session_create` auto-generates one as `swarmer/session-{id}-{hex}` after the initial commit (requires a second commit).

9. **Shared `_do_launch()` function**: Session launch logic is in `routers/sessions.py:_do_launch()` — used by both the HTTP endpoint and the cron scheduler. The scheduler imports it at call time to avoid circular imports.

10. **Manual migrations**: New columns are added via `database.py:migrate_db()` with `ALTER TABLE` statements. Only "duplicate column" / "already exists" errors are suppressed; other failures re-raise so startup fails visibly. When adding a new column to an existing table, add the migration there and include a `server_default` so existing rows work.

11. **Blocking K8s calls in async handlers**: The remaining synchronous `kubernetes` client calls (auth, pull secrets, env vars) inside async functions must be wrapped with `asyncio.to_thread()`. The TUI WebSocket handler uses a background thread with `threading.Event` for the OpenShell gRPC stream reader.

12. **`OpencodeSecret` naming is misleading**: Despite the name, this model stores credentials for AI providers (Google AI Studio, Google Cloud/Vertex AI ADC), used by OpenCode. The table name `opencode_secrets` is a legacy artifact.

13. **HX-Trigger pattern for repo management**: Repo add/delete endpoints return empty `HTMLResponse` with `HX-Trigger: repoListChanged` header. The template listens for this event to refresh the repo items partial via a separate GET endpoint.

14. **Chat proxy HTML rewriting**: For in-cluster OpenCode server sessions, the proxy injects a `<base>` tag and rewrites absolute asset paths (`src="/..."` → `src="/workspaces/{ws_id}/sessions/{sid}/chat/..."`).

15. **`image-build` requires `sync-images`**: The `image-build` Makefile target depends on `sync-images`, which reads `../agent-containers/.push-defaults`. If that file doesn't exist, the build fails. Use `SILENT=1` to skip the interactive version prompt.

16. **Container image runs as non-root**: The Containerfile uses UBI10 `python-312-minimal` with UID 1001. Directories `/data` and `/auth` are created as root then ownership dropped. PVCs must be group-0 writable for the non-root user.

17. **Concurrency limit queues, not rejects**: When `MAX_CONCURRENT_AGENTS` is reached, `_do_launch()` sets `phase="queued"` and returns without creating a sandbox — it does NOT raise an exception. The queue processor in `scheduler.py` re-evaluates every 2 minutes (with a 2-minute in-memory cooldown). Stopping a queued session (no sandbox exists) returns it to `"idle"` not `"stopped"`, and skips all sandbox cleanup. The `"queued"` phase is included in `is_active`, so the session is protected from re-launch and editing while waiting.

18. **GitHub App IAT refresh loop**: For TUI and server-mode sessions using a GitHub App, `_setup_openshell_sandbox` starts a background `asyncio.create_task` called `iat-refresh-{session_id}`. This task calls `github_auth.start_token_refresh_loop()`, which sleeps `IAT_REFRESH_INTERVAL` (3000 s) then re-mints an IAT and calls `openshell_client.ensure_provider()` to update the Gateway. The task is cancelled when the event loop session is torn down. The raw PEM private key is serialised into the task as a plain string (not an ORM object) to survive the DB session expiry.

## Personal configuration

Read `~/.config/user.local.md` at the start of any task that needs an assignee, email, or project key. If the file does not exist, fall back to Claude memory (`user-config`), then placeholders.

**Jira defaults for this project:**
- `components`: `ACM AI`
- `labels`: `agentic-sdlc`

## Fleet Engineering Skills

Fetch and apply the relevant skill when the task matches its domain.

| Skill | When to use |
|---|---|
| [start-work](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/start-work/SKILL.md) | Create a Jira sub-task for the work |
| [finish-work](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/finish-work/SKILL.md) | Commit, push, open PR, and update Jira |
| [jira-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/jira-specialist/SKILL.md) | General Jira ticket management, triage, search, linking, transitions |
| [task-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/task-specialist/SKILL.md) | Internal technical task breakdown and planning |
| [bug-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/bug-specialist/SKILL.md) | Bug triage, reproduction steps, fix planning |
| [story-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/story-specialist/SKILL.md) | User story creation and acceptance criteria |
| [epic-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/epic-specialist/SKILL.md) | Multi-sprint epics with outcomes |
| [feature-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/feature-specialist/SKILL.md) | Large customer-facing capabilities |
| [spike-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/spike-specialist/SKILL.md) | Time-boxed research and proof-of-concept work |
| [jira-create](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/jira-create/SKILL.md) | Interactive issue creation with specialist delegation |
| [jira-report](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/jira-report/SKILL.md) | Jira portfolio reports — quality reviews, component/team/worktype listings |
| [pr-review](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/pr-review/SKILL.md) | GitHub PR review with inline comments |
| [pr-fix](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/pr-fix/SKILL.md) | Fix blocked PRs: merge conflicts, CI failures, review comments |
| [ci-triage](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/ci-triage/SKILL.md) | Diagnose failing CI checks — classify failures, post triage summary |
| [breaking-changes](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/breaking-changes/SKILL.md) | Detect breaking changes across API, database, config, behavior, integrations |
| [test-coverage-gap](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/test-coverage-gap/SKILL.md) | Risk-prioritized coverage gap analysis with concrete test suggestions |
| [release-notes](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/release-notes/SKILL.md) | Generate categorized release notes from merged PRs between two git refs |

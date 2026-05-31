# AGENTS.md — Swarmer

This file provides guidance to Claude Code (claude.ai/code) and other AI agents when working with code in this repository. CLAUDE.md is a symlink to this file.

A FastAPI + HTMX dashboard for managing AI coding agent workloads on Kubernetes. Supports multiple agent tools (OpenCode, Crush). Server-rendered UI with PatternFly 6 dark theme. Token-based auth via Kubernetes ServiceAccount bearer tokens (+ optional OpenShift OAuth).

## Commands

```sh
# Setup
make setup-secret        # Generate SWARMER_SECRET_KEY → auth/secret.key
make install             # pip install -r requirements.txt

# Development  (requires auth/secret.key — run make setup-secret first)
make dev                 # uvicorn at localhost:8090 with --reload, K8S_IN_CLUSTER=false
make lint                # ruff check swarmer/
make db-reset            # Delete SQLite database (fresh schema on next start)

# Tests
make test                                            # Run all unit tests + mcp-server tests (excludes Playwright)
pytest tests/ -q --ignore=tests/test_ui_patternfly.py  # equivalent
pytest tests/test_api.py -q                          # Run a single test file
pytest tests/test_ui_patternfly.py                   # Playwright UI tests (requires running dev server at :8091 with SWARMER_DEV_AUTH=1)

# Container image
make image-build         # Build container image (podman by default; SILENT=1 to skip version prompt)
make image-push REGISTRY=...  # Push to registry
make image-build-crush   # Build Crush agent container image

# Local kind cluster
make kind-create         # Create kind cluster with NodePort 30080→8080
make kind-load           # Load swarmer image into kind
make kind-load-opencode  # Load opencode agent image into kind
make kind-load-crush     # Load crush agent image into kind
make kind-deploy         # Full one-shot: create cluster + build + load + deploy
make kind-delete         # Tear down kind cluster

# Production Kubernetes
make k8s-deploy          # Deploy to current kubectl context
make k8s-connect         # Port-forward localhost:8080 → swarmer service
make k8s-delete          # Remove all swarmer resources

# User management
make user-token SA_USER=alice                           # Issue a K8s login token (default 8h)
make grant-workspace SA_USER=alice WORKSPACE_NS=my-proj # Grant workspace access
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

## Code Conventions

### Python Style

- Python 3.12, type hints throughout (using `X | None` union syntax, not `Optional`)
- `Mapped[type]` for all SQLAlchemy columns (SQLAlchemy 2.x declarative style)
- Module-level singleton pattern: `settings = Settings()`, `_fernet: Fernet | None = None`
- Lazy kubernetes imports inside functions (avoid import errors when K8s isn't configured)
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
- K8s resource names: `session-{session_id}-{suffix}` (pods, PVCs), `session-{session_id}-svc` (services), `session-{session_id}-chat` (routes)
- K8s secret names: derived from model fields (e.g., `github-pat-{slug}`, `opencode-secret`, `crush-secret`); optional unmanaged `swarmer-agent-extra-env` in the workspace namespace injects extra agent env vars (`envFrom`, optional)
- URL pattern: `/workspaces/{ws_id}/sessions/{sid}/action`

### Configuration

- `pydantic-settings` with `.env` file support, `extra="ignore"` (unrecognized env vars silently ignored)
- All settings have sensible defaults for local development
- Key env vars: `DATABASE_URL`, `SWARMER_SECRET_KEY`, `K8S_IN_CLUSTER`, `K8S_API_URL`, `OPENSHIFT_OAUTH_URL`
- Agent images: `AGENT_IMAGE_OPENCODE`, `AGENT_IMAGE_CRUSH`, `CRUSH_VERSION`, `DEFAULT_AGENT_TOOL`
- Container runtime defaults to `podman` (override with `CONTAINER_CMD=docker`)

### Testing

- Unit tests use `pytest` + `pytest-asyncio` + `respx` for HTTP mocking
- Tests stub model objects with plain classes (`_FakePAT`) to avoid SQLAlchemy/FastAPI dependencies
- Playwright e2e tests require a running dev server with `SWARMER_DEV_AUTH=1` at port 8091
- Test files use `sys.path.insert()` to add the parent dir for imports

## Gotchas & Non-Obvious Patterns

1. **Crypto init order matters**: `init_crypto()` must run before `init_db()` / `create_tables()` because model property accessors call `decrypt()`. The lifespan function in `main.py` enforces this order.

2. **`auth.py` is dead code**: The file `swarmer/auth.py` contains only a comment "superseded by k8s_auth.py". All authentication logic is in `k8s_auth.py` and `routers/auth.py`.

3. **Deployment image placeholder**: `k8s/swarmer/deployment.yaml` uses literal strings like `SWARMER_IMAGE`, `OPENSHIFT_OAUTH_URL_VALUE`, `AGENT_IMAGE_OPENCODE_VALUE`, `AGENT_IMAGE_CRUSH_VALUE` which are replaced at deploy time via `sed` in the Makefile. Don't replace them with actual values.

4. **SQLite single-writer**: The K8s Deployment uses `strategy: Recreate` (not RollingUpdate) because SQLite doesn't support concurrent writers. Only one replica is safe.

5. **Session mode affects pod lifecycle**:
   - `prompt` mode: `restartPolicy: Never`, pod exits after agent finishes, auto-cleaned by log_poller
   - `server`/`tui` modes: `restartPolicy: Always`, pod runs indefinitely
   - Stopping a session always deletes the pod; if `persist=False`, the PVC is also deleted

6. **OpenCode model format quirk**: Model strings use `provider/model@version` format (e.g., `google-vertex-anthropic/claude-sonnet-4-6@default`). The `@version` suffix is part of the model ID. Crush uses simpler `provider/model` format (e.g., `vertexai/claude-sonnet-4-6`).

7. **TUI auth tokens**: TUI WebSocket connections use one-time UUID tokens stored in the HTTP session. Tokens are generated on the session detail page and consumed on WebSocket connect. Invalid/reused tokens are rejected with close code 4001.

8. **Session launch saves working branch**: If no working branch is specified, `session_create` auto-generates one as `swarmer/session-{id}-{hex}` after the initial commit (requires a second commit).

9. **Shared `_do_launch()` function**: Session launch logic is in `routers/sessions.py:_do_launch()` — used by both the HTTP endpoint and the cron scheduler. The scheduler imports it at call time to avoid circular imports.

10. **Manual migrations**: New columns are added via `database.py:migrate_db()` with `ALTER TABLE` statements. Only "duplicate column" / "already exists" errors are suppressed; other failures re-raise so startup fails visibly. When adding a new column to an existing table, add the migration there and include a `server_default` so existing rows work.

11. **Blocking K8s calls in async handlers**: All synchronous `kubernetes` client calls inside async functions must be wrapped with `asyncio.to_thread()` to avoid blocking the event loop. The TUI WebSocket handler uses a background thread with `threading.Event` for the pod exec stream reader.

12. **`OpencodeSecret` naming is misleading**: Despite the name, this model stores credentials for all agent tools (OpenCode, Crush), including Anthropic and OpenAI API keys. The table name `opencode_secrets` is a legacy artifact.

13. **HX-Trigger pattern for repo management**: Repo add/delete endpoints return empty `HTMLResponse` with `HX-Trigger: repoListChanged` header. The template listens for this event to refresh the repo items partial via a separate GET endpoint.

14. **Chat proxy HTML rewriting**: For in-cluster OpenCode server sessions, the proxy injects a `<base>` tag and rewrites absolute asset paths (`src="/..."` → `src="/workspaces/{ws_id}/sessions/{sid}/chat/..."`). Crush sessions skip this and render a custom chat template instead.

15. **`image-build` requires `sync-images`**: The `image-build` Makefile target depends on `sync-images`, which reads `../agent-containers/.push-defaults`. If that file doesn't exist, the build fails. Use `SILENT=1` to skip the interactive version prompt.

16. **Container image runs as non-root**: The Containerfile uses UBI10 `python-312-minimal` with UID 1001. Directories `/data` and `/auth` are created as root then ownership dropped. PVCs must be group-0 writable for the non-root user.

## Personal configuration

Read `.claude/user.local.md` at the start of any task that needs an assignee, email, or project key. If the file does not exist, fall back to Claude memory (`user-config`), then placeholders.

## Fleet Engineering Skills

Fetch and apply the relevant skill when the task matches its domain.

| Skill | When to use |
|---|---|
| [start-work](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/sdlc/start-work/SKILL.md) | Create a Jira sub-task and register the session in plans/INDEX.md |
| [finish-work](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/sdlc/finish-work/SKILL.md) | Commit, push, open PR, update Jira and plans/INDEX.md |
| [jira-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/jira/jira-specialist/SKILL.md) | General Jira ticket management, triage, search, linking, transitions |
| [task-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/sdlc/task-specialist/SKILL.md) | Internal technical task breakdown and planning |
| [bug-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/jira/bug-specialist/SKILL.md) | Bug triage, reproduction steps, fix planning |
| [story-specialist](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/jira/story-specialist/SKILL.md) | User story creation and acceptance criteria |
| [pr-review](https://raw.githubusercontent.com/OpenShift-Fleet/agentic-sdlc/main/skills/sdlc/pr-review/SKILL.md) | GitHub PR review with inline comments |

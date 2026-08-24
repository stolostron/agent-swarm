# agent-swarm

A FastAPI + HTMX dashboard for managing AI coding agent workloads on Kubernetes.

> **For full documentation, see [docs/USER_GUIDE.md](docs/USER_GUIDE.md).**

## Capabilities

- **Workspaces** — each workspace maps 1:1 to a Kubernetes namespace; create, rename, and delete workspaces from the UI
- **Secrets** — Fernet-encrypted storage for provider credentials (GCP/Vertex AI, Gemini, Anthropic, OpenAI), GitHub PATs for HTTPS git auth, and OCI registry pull secrets
- **Session lifecycle** — create → launch → monitor → stop → delete sessions backed by OpenShell sandboxes
- **Three session modes:**
  - **Prompt** — one-shot: run a prompt, stream output, sandbox exits when done
  - **Server** — persistent agent web API with in-dashboard chat link
  - **TUI** — full xterm.js browser terminal connected via WebSocket + OpenShell PTY
- **Git cloning** — repos cloned into the sandbox via OpenShell API at session launch
- **Live UI** — HTMX polling for session status and output; no page reloads needed
- **Dual output capture** — prompt-mode sessions preserve both the processed agent response (`last_output`) and the raw console log (`raw_output`); an Output / Raw Log toggle appears in the UI when they differ
- **Agent tool support** — OpenCode (Go) coding agent, with pluggable tooling for future agents
- **MCP server integration** — Model Context Protocol servers per workspace (e.g., Atlassian Jira)
- **Agent Swarm MCP Server** — Standalone MCP server (`agent-swarm-mcp-server`) exposing full session, schedule, gateway, and workspace management tools for external AI agent orchestration
- **API Token & MCP Setup** — Dedicated `/token` web dashboard view with 1-click token copying and `opencode.json` snippets, plus automated CLI configuration (`make mcp-setup` / `make api-info`)
- **Prompt library** — workspace-level prompt library with git-backed folders and per-session picker
- **Cron scheduling** — recurring prompt-mode sessions on a cron schedule
- **REST API** — full `/api/v1/` REST API alongside the HTMX Console

## Quick Start (Kind)

```sh
make setup-secret    # generate encryption key
make kind-deploy     # create cluster + build + deploy
```

Dashboard: http://localhost:8080

See the [User Guide](docs/USER_GUIDE.md) for OpenShift deployment, Kustomize overlays, and all other options.

Additional setup guides: [Slack notifications](docs/SLACK_NOTIFICATIONS.md) · [GitHub App auth](docs/GITHUB_APP_SETUP.md) · [OpenShell local dev](docs/OPENSHELL_LOCAL_SETUP.md)

## Configuration

Copy `.env.example` to `.env` and adjust as needed:

```sh
cp .env.example .env
```

Key variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///data/swarmer.db` | SQLite database path |
| `K8S_IN_CLUSTER` | `false` | Set to `true` when running inside a pod |
| `AGENT_IMAGE` | _(empty)_ | Fallback image used for session pods |
| `AGENT_IMAGE_OPENCODE` | _(empty)_ | OpenCode agent image |
| `AGENT_IMAGE_PULL_SECRET` | _(empty)_ | Pull secret name in the workspace namespace |

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for the full environment variable reference.

## Access Control

Workspace access is a **database-backed ACL** (ACM-41659) — a workspace no longer maps to a
dedicated Kubernetes namespace or RBAC grant. A user can see and manage a workspace if they are
its owner (the user who created it), have been added as a member via the **Members** tab (or the
`/api/v1/workspaces/{id}/members` API), or are a global admin (see below).

**Upgrading from an older Swarmer version?** Nobody needs to be manually re-added. On first
startup after upgrading, Swarmer automatically:
- backfills `workspace_members` and each workspace's owner from existing per-user credential
  records (AI provider credentials, GitHub PATs, MCP servers, GitHub Apps) already in the database;
- mirrors any legacy `make grant-workspace-access` K8s RoleBinding grants into the same table
  (best-effort — skipped if K8s is unreachable, never blocks startup);
- for shared-namespace deployments (`K8S_NAMESPACE` set), every authenticated user keeps access to
  every workspace, matching that deployment flavor's original flat trust model.

A workspace that genuinely has no recoverable owner (e.g. it was created but never used) stays
open to any authenticated user — the first person to rename it, delete it, or add/remove a member
automatically becomes its owner.

### Global Admins — simple setup

Global admins can see and manage every workspace. There are two ways to grant admin rights,
and you can mix both:

- **Self-service (recommended for day-to-day use):** the very first user to log in sees a
  **"Become the first Admin"** button on the Workspaces page (or `/admins`) — one click, zero
  configuration. Once at least one admin exists, admins manage the rest from the `/admins` page
  (or `POST`/`DELETE /api/v1/admins`).
- **Declarative (for GitOps-managed deployments):** set `WORKSPACE_ADMIN_USERS`
  (comma-separated K8s usernames) and/or `WORKSPACE_ADMIN_GROUPS` (comma-separated groups)
  in the deployment environment. These always take effect and don't need the bootstrap step.

### Issue a login token

Creates a Kubernetes ServiceAccount for the user (if it doesn't exist) and prints a bearer token they paste into the Swarmer login page:

```sh
make user-token SA_USER=alice
make user-token SA_USER=alice TOKEN_DURATION=24h   # default: 8h
```

Share the printed token with the user — it expires after `TOKEN_DURATION`. (Users authenticating
via OpenShift OAuth / OIDC don't need this step — they log in through the OAuth flow instead.)

### Grant workspace access

Self-service — no `kubectl` or cluster access required. Open the workspace, go to the **Members**
tab, and add the user's exact K8s username (`system:serviceaccount:<ns>:<name>` for a ServiceAccount
token, or their OpenShift OAuth/OIDC username). The username field suggests candidates from people
you already share a workspace with (global admins see every known username), plus every OpenShift
`User` and every ServiceAccount `make user-token SA_USER=<name>` would create — but it's always
still a free-text field, so you can invite someone who hasn't logged in yet. Only the workspace
owner or a configured admin can add or remove members. This can also be done via the API:

```sh
curl -sX POST "$SWARMER_URL/api/v1/workspaces/<id>/members" \
  -H "Authorization: Bearer <owner-or-admin-token>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice"}'
```

### Allow a user to create new workspaces

By default (`WORKSPACE_CREATE_POLICY=all`), any authenticated user can create a workspace
and becomes its owner. To restrict workspace creation to admins, set
`WORKSPACE_CREATE_POLICY=admins` and list admins via `WORKSPACE_ADMIN_USERS`
(comma-separated K8s usernames) and/or `WORKSPACE_ADMIN_GROUPS` (comma-separated groups)
in the deployment environment.

### Typical onboarding flow

```sh
make user-token SA_USER=alice   # 1. create user + print token, share with alice
# 2. alice logs in and either creates her own workspace, or an existing
#    workspace owner adds her via the Members tab / API above.
```

> Deprecated: `make grant-workspace-access` / `make grant-workspace-create` (Kubernetes namespace
> RoleBindings) are kept only as legacy no-op-equivalent commands for existing automation — Swarmer
> no longer consults K8s RBAC for workspace authorization. Use the Members tab / admin env vars above.

## Other useful targets

```sh
make help                      # list all Makefile targets
make lint                      # run ruff linter
make test                      # run unit tests and mcp-server test suite
make mcp-setup                 # configure local opencode.json for Agent Swarm
make api-info                  # display Swarmer API URL, current user, and MCP config snippet
make db-reset                  # delete the SQLite database (fresh schema on next start)
```

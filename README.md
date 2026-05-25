# agent-swarm

A FastAPI + HTMX dashboard for managing AI coding agent workloads on Kubernetes.

> **For full documentation, see [docs/USER_GUIDE.md](docs/USER_GUIDE.md).**

## Capabilities

- **Workspaces** — each workspace maps 1:1 to a Kubernetes namespace; create, rename, and delete workspaces from the UI
- **Secrets** — Fernet-encrypted storage for provider credentials (GCP/Vertex AI, Gemini, Anthropic, OpenAI), GitHub PATs for HTTPS git auth, and OCI registry pull secrets; all auto-synced to Kubernetes Secrets
- **Session lifecycle** — create → launch → monitor → stop → delete sessions backed by Kubernetes Pods and PVCs
- **Three session modes:**
  - **Prompt** — one-shot: run a prompt, stream output, pod exits when done
  - **Server** — persistent agent web API with in-dashboard chat link
  - **TUI** — full xterm.js browser terminal connected via WebSocket + `kubectl exec` PTY
- **Git cloning** — init containers clone configured repos into the PVC-backed workspace before the agent starts
- **Live UI** — HTMX polling for session status and output; no page reloads needed
- **Multi-agent support** — OpenCode (Go) and Crush (Rust) coding agents
- **MCP server integration** — Model Context Protocol servers per workspace (e.g., Atlassian Jira)
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
| `AGENT_IMAGE_CRUSH` | _(empty)_ | Crush agent image |
| `AGENT_IMAGE_PULL_SECRET` | _(empty)_ | Pull secret name in the workspace namespace |

See [docs/USER_GUIDE.md](docs/USER_GUIDE.md) for the full environment variable reference.

## Access Control

> **These two commands are the primary way to onboard users and control workspace access.**

### Issue a login token

Creates a Kubernetes ServiceAccount for the user (if it doesn't exist) and prints a bearer token they paste into the Swarmer login page:

```sh
make user-token SA_USER=alice
make user-token SA_USER=alice TOKEN_DURATION=24h   # default: 8h
```

Share the printed token with the user — it expires after `TOKEN_DURATION`.

### Grant workspace access

Binds a user to a specific workspace namespace so they can see and manage sessions in it:

```sh
make grant-workspace SA_USER=alice WORKSPACE_NS=my-project
```

Run this once per user per namespace. A user with no workspace grants can log in but will see no workspaces.

### Typical onboarding flow

```sh
make user-token SA_USER=alice                          # 1. create user + print token
make grant-workspace SA_USER=alice WORKSPACE_NS=team-a # 2. give access to a workspace
make grant-workspace SA_USER=alice WORKSPACE_NS=team-b # 3. repeat for additional workspaces
```

## Other useful targets

```sh
make help          # list all Makefile targets
make lint          # run ruff linter
make db-reset      # delete the SQLite database (fresh schema on next start)
```

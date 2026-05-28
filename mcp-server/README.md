# Agent Swarm MCP Server

An MCP server that exposes Agent Swarm session management as tools for AI agents. Enables Claude Code (on a laptop) or an Agent Swarm session to orchestrate other sessions programmatically.

## Orchestration Flow

1. **Find** an existing session by git repository (`find_sessions_by_repo`)
2. **Create** a session if none found (`create_session`)
3. **Configure** it — attach repos (`add_repo_to_session`), set prompt (`set_session_prompt`)
4. **Launch** it in prompt mode (`launch_session`)
5. **Wait** for completion (`wait_for_session`) → returns output

## Tools

| Tool | Purpose |
|------|---------|
| `list_workspaces` | Discover available workspaces |
| `list_sessions` | List sessions (optional phase filter) |
| `find_sessions_by_repo` | Find sessions with a specific git repo attached |
| `get_session` | Full session details including repos |
| `create_session` | Create a new session |
| `update_session` | Modify a non-running session |
| `delete_session` | Delete a session |
| `add_repo_to_session` | Attach a git repository |
| `remove_repo_from_session` | Detach a git repository |
| `list_workspace_prompts` | Browse the workspace prompt library |
| `set_session_prompt` | Set base prompt and/or additional instructions |
| `launch_session` | Start the session pod |
| `stop_session` | Abort a running session |
| `get_session_status` | Check phase and run duration |
| `get_session_output` | Retrieve captured output |
| `wait_for_session` | Poll until terminal state, return output |
| `list_github_pats` | List GitHub PATs for private repo access |

## Installation

```bash
cd mcp-server
pip install -e .
```

## Authentication

The server resolves a Kubernetes bearer token automatically in this order:

1. **`AGENT_SWARM_API_TOKEN`** env var — explicit override, always wins
2. **In-cluster service account token** — `/var/run/secrets/kubernetes.io/serviceaccount/token` (pod sidecar deployment, zero config)
3. **Kubeconfig** — parses `$KUBECONFIG` or `~/.kube/config` and extracts the token from the current context (works after `oc login` or `kubectl login`)

For OpenShift users: just run `oc login` and the kubeconfig token is picked up automatically. Tokens expire (~24h); re-run `oc login` and restart the MCP server if you get auth errors.

## Configuration

| Environment Variable | Required | Description |
|---------------------|----------|-------------|
| `AGENT_SWARM_API_URL` | Yes | Base URL of your agent-swarm instance |
| `AGENT_SWARM_API_TOKEN` | No | K8s bearer token (overrides auto-detection) |
| `AGENT_SWARM_WORKSPACE` | No | Default workspace name (informational) |
| `AGENT_SWARM_VERIFY_SSL` | No | Set to `false` to skip SSL verification (self-signed certs) |
| `AGENT_SWARM_SSL_CA_BUNDLE` | No | Path to a PEM CA bundle to trust for TLS (use instead of disabling verification) |

## Claude Code Setup

Add to your `~/.claude/settings.json` (or project `.claude/settings.json`):

```json
{
  "mcpServers": {
    "agent-swarm": {
      "command": "agent-swarm-mcp-server",
      "env": {
        "AGENT_SWARM_API_URL": "https://swarmer-swarmer.apps.your-cluster.example.com"
      }
    }
  }
}
```

For an explicit token (CI/CD or when kubeconfig is not available):

```json
{
  "mcpServers": {
    "agent-swarm": {
      "command": "agent-swarm-mcp-server",
      "env": {
        "AGENT_SWARM_API_URL": "https://swarmer-swarmer.apps.your-cluster.example.com",
        "AGENT_SWARM_API_TOKEN": "your-k8s-token"
      }
    }
  }
}
```

## Sidecar (SSE) Deployment

Run inside the cluster with SSE transport:

```bash
agent-swarm-mcp-server --transport sse --host 0.0.0.0 --port 8080
```

The in-cluster SA token and internal service URL are used automatically.

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -v
```

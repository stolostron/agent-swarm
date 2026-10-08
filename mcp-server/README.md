# Agent Swarm MCP Server

An MCP server that exposes Agent Swarm session management as tools for AI agents. Enables Claude Code (on a laptop) or an Agent Swarm session to orchestrate other sessions programmatically.

## Orchestration Flow

1. **Find** an existing session by git repository (`find_sessions_by_repo`)
2. **Create** a session if none found (`create_session`)
3. **Configure** it — attach repos (`add_repo_to_session`), set prompt (`set_session_prompt`)
4. **Launch** it in prompt mode (`launch_session`)
5. **Wait** for completion (`wait_for_session`) → returns output

To inspect older executions, call `list_session_runs` for bounded metadata and
then `get_session_run` with a selected run ID. The detail tool returns the full
stored processed output, raw log, launch-time prompt and startup context, and
separate event context. Older records without a captured context snapshot are
explicitly marked unavailable; current session settings are never substituted.

## Tools

| Tool | Purpose |
|------|---------|
| `list_workspaces` | Discover available workspaces |
| `get_workspace` | Get details of a specific workspace |
| `create_workspace` | Create a new workspace |
| `update_workspace` | Modify a workspace's display name or description |
| `delete_workspace` | Delete a workspace |
| `list_workspace_members` | List members granted access to a workspace |
| `add_workspace_member` | Grant a user access to a workspace |
| `remove_workspace_member` | Revoke a user's access from a workspace |
| `get_me` | Get authenticated user identity and ACL permissions |
| `list_known_users` | List known users for member/admin autocomplete |
| `list_admins` | List global Swarmer admins |
| `add_admin` | Grant a user global admin rights |
| `remove_admin` | Revoke global admin rights from a user |
| `bootstrap_admin` | Self-promote to global admin when zero admins exist |
| `list_sessions` | List sessions (optional phase filter) |
| `find_sessions_by_repo` | Find sessions with a specific git repo attached |
| `get_session` | Full session details including repos |
| `create_session` | Create a new session |
| `update_session` | Modify a non-running session |
| `delete_session` | Delete a session |
| `add_repo_to_session` | Attach a git repository |
| `remove_repo_from_session` | Detach a git repository |
| `list_workspace_prompts` | Browse the workspace prompt library |
| `list_workspace_mcp_servers` | List caller-visible workspace MCP servers with safe metadata |
| `set_session_prompt` | Set base prompt and/or additional instructions |
| `launch_session` | Start the session pod |
| `stop_session` | Abort a running session |
| `get_session_status` | Check phase and run duration |
| `get_session_output` | Retrieve captured output |
| `list_session_runs` | List bounded, metadata-only summaries of historical runs |
| `get_session_run` | Retrieve all stored output, logs, prompt, and context for one run |
| `wait_for_session` | Poll until terminal state, return output |
| `list_github_pats` | List GitHub PATs for private repo access |
| `list_session_schedules` | List schedules configured for a session |
| `add_session_schedule` | Add a new schedule to a session |
| `update_session_schedule` | Update an existing session schedule |
| `delete_session_schedule` | Delete a session schedule |
| `get_workspace_gateway` | Get a workspace's dedicated OpenShell gateway config |
| `set_workspace_gateway` | Configure a dedicated/remote OpenShell gateway for a workspace |
| `delete_workspace_gateway` | Revert a workspace to the cluster default OpenShell gateway |
| `test_workspace_gateway` | Test connectivity/auth to an OpenShell gateway |
| `parse_gateway_command` | Parse a pasted `openshell gateway add ...` command or JSON metadata |
| `parse_gateway_token` | Parse a pasted OIDC token/credential payload |

### Session MCP access

New sessions have MCP access disabled unless `mcp_server_ids` contains one or
more IDs from `list_workspace_mcp_servers`. Passing an empty list also disables
all MCPs. Set `mcp_selection` to `inherit` to opt into all enabled,
authenticated, unexpired MCP servers visible to the caller in that workspace.
Session updates leave MCP configuration unchanged when the selection arguments
are omitted or null; an empty ID list disables all, a non-empty list replaces
the saved selection, and `mcp_selection: inherit` restores inheritance.
When opting into inheritance, omit `mcp_server_ids` or pass null.

Session readback includes `mcp_selection` (`inherit`, `disabled`, or `selected`),
the caller-visible saved `mcp_server_ids`, and `runtime_mcp_server_ids` containing
only MCP servers currently eligible for access. Inventory and session responses
never include MCP credentials or another caller's private server metadata.

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

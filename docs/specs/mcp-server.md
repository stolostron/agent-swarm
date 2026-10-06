# Agent Swarm MCP Server Specification

## 1. Purpose

The standalone MCP server exposes Agent Swarm orchestration to AI agents. It
uses an asynchronous HTTP client against the Swarmer REST API and publishes
FastMCP tools over stdio or SSE.

The MCP server is an adapter, not a second persistence layer. It must preserve
REST authentication, authorization, status codes, and lifecycle semantics.

## 2. Runtime configuration

| Variable | Required | Description |
|---|---:|---|
| `AGENT_SWARM_API_URL` | yes | Base URL of Swarmer |
| `AGENT_SWARM_API_TOKEN` | no | Explicit Kubernetes bearer token |
| `AGENT_SWARM_WORKSPACE` | no | Informational default workspace name |
| `AGENT_SWARM_VERIFY_SSL` | no | Defaults to `true`; `false` disables verification |
| `AGENT_SWARM_SSL_CA_BUNDLE` | no | PEM CA file/directory; takes precedence over boolean verification |

The API URL is normalized by removing trailing slashes.

## 3. Authentication

Token resolution order:

1. `AGENT_SWARM_API_TOKEN`
2. In-cluster service account token at the Kubernetes standard path
3. Current kubeconfig context token
4. Kubeconfig exec credential provider

If no token is found, startup fails with a configuration error. A `401` from
the API is converted into an `AgentSwarmAPIError` explaining that the token
may have expired.

The MCP server must never log the token value.

## 4. Transport

Default command behavior uses stdio. SSE mode is selected by the server
launcher and accepts configurable host and port values.

The underlying HTTP client uses `httpx.AsyncClient`, sends the bearer token on
every request, verifies TLS according to configuration, and uses a 30-second
request timeout.

## 5. Error contract

The client raises:

```python
AgentSwarmAPIError(status_code: int, detail: str)
```

`status_code == 0` indicates a transport-level failure. HTTP failures retain
the API status code and extract `detail` from the JSON response when possible.

MCP tools should not convert authorization errors into successful empty lists.

## 6. Tool catalog

### 6.1 Workspace and identity tools

| Tool | Inputs | Result |
|---|---|---|
| `list_workspaces` | none | Accessible workspace summaries |
| `get_workspace` | `workspace_id` | Workspace detail |
| `create_workspace` | `display_name`, optional `description` | Created workspace |
| `update_workspace` | `workspace_id`, `display_name`, optional `description` | Updated workspace |
| `delete_workspace` | `workspace_id` | Deletion message |
| `list_workspace_members` | `workspace_id` | Member summaries |
| `add_workspace_member` | `workspace_id`, `user_id`, optional `role` | Created member |
| `remove_workspace_member` | `workspace_id`, `user_id` | Deletion message |
| `get_me` | none | Identity and permission flags |
| `list_known_users` | none | Visibility-scoped user suggestions |
| `list_admins` | none | Global admin summaries |
| `add_admin` | `user_id` | Created admin |
| `remove_admin` | `user_id` | Deletion message |
| `bootstrap_admin` | none | Initial admin record |

The MCP server must not assume the caller can create or manage workspaces just
because the tool exists. The REST API remains authoritative.

### 6.2 Gateway tools

| Tool | Purpose |
|---|---|
| `get_workspace_gateway` | Read dedicated gateway metadata |
| `set_workspace_gateway` | Replace dedicated gateway configuration |
| `delete_workspace_gateway` | Revert to default gateway |
| `test_workspace_gateway` | Test connectivity/authentication |
| `parse_gateway_command` | Parse OpenShell command or metadata |
| `parse_gateway_token` | Parse token input |

Secret-bearing tool arguments must be treated as sensitive. Tool descriptions
must warn that `set_workspace_gateway` is replacement-oriented and omitted
optional values may clear existing saved configuration.

### 6.3 Session tools

| Tool | Purpose |
|---|---|
| `list_sessions` | List sessions, optionally filtered by phase |
| `get_session` | Get session plus attached repositories |
| `find_sessions_by_repo` | Find sessions with a normalized matching repository |
| `create_session` | Create session configuration |
| `update_session` | Update inactive session configuration |
| `delete_session` | Delete inactive session |
| `launch_session` | Start or queue a session |
| `stop_session` | Stop queued or active session |
| `get_session_status` | Read compact lifecycle status |
| `get_session_output` | Read processed and raw output |
| `wait_for_session` | Poll until terminal phase or timeout |

Supported phases are:

```text
idle, queued, pending, running, succeeded, failed, stopped
```

The `queued` phase is active and must not be treated as terminal.

#### `create_session`

Inputs:

```text
workspace_id
name
agent_tool = "opencode"
mode = "prompt"
provider = ""
working_branch = ""
instruction_prompt = ""
github_pat_id = null
prompt_id = null
```

`create_session` and `update_session` expose the MCP selection contract through
`mcp_server_ids` and `mcp_selection`. New sessions default to disabled; non-empty
IDs select caller-visible workspace servers, and an empty list disables all.
`mcp_selection: "inherit"` opts into currently enabled, authenticated, unexpired
servers visible to the caller. Updates leave selection unchanged when fields are
omitted or null; an empty list disables all, non-empty IDs replace the saved
selection, and explicit inheritance restores it. Every ID must exist in the
target workspace and be visible to the caller or the whole request is rejected.
When selecting inheritance, omit `mcp_server_ids` or pass null; combining
inheritance with an ID list is rejected.

`list_workspace_mcp_servers` provides the caller-visible inventory using safe
metadata only. Session results expose `mcp_selection` (`inherit`, `disabled`, or
`selected`), saved caller-visible `mcp_server_ids`, and
`runtime_mcp_server_ids` containing only servers currently eligible for access.
Credentials and private server metadata are never included.

Valid modes: `prompt`, `server`, `tui`.

Valid agent tools: `opencode`, `shell`.

#### `update_session`

Updates only non-null supplied fields. The operation is invalid while the
session is active. Empty strings are meaningful for clearing optional text
fields; omitted arguments mean no change.

#### `launch_session`

Starts a run using the configured session. It may return a `queued` session
when concurrency capacity is exhausted. It does not mean the agent has
started executing yet.

#### `wait_for_session`

Inputs:

```text
workspace_id
session_id
poll_interval = 10
timeout = 3600
```

The tool reports progress through MCP context when available. It returns when
the phase is `succeeded`, `failed`, or `stopped`. On timeout it returns:

```json
{
  "phase": "timeout",
  "status_detail": "Timed out after <n>s",
  "run_duration": "<n>s",
  "output": "",
  "raw_output": ""
}
```

Timeout is a client observation, not a persisted session phase.

### 6.4 Repository tools

| Tool | Inputs | Result |
|---|---|---|
| `add_repo_to_session` | workspace/session IDs, URL, branch, local path | Created attachment |
| `remove_repo_from_session` | workspace/session IDs, repository ID | Deletion message |

Repositories are cloned under the OpenShell sandbox workspace. Documentation
must refer to `/sandbox/<local_path>`, not `/workspace/<local_path>`.

`find_sessions_by_repo` normalizes URL host case, trailing slashes, and a
trailing `.git` suffix before comparison.

### 6.5 Prompt tools

| Tool | Purpose |
|---|---|
| `list_workspace_prompts` | Flatten prompt sources into selectable prompt summaries |
| `set_session_prompt` | Set base prompt ID and/or additional instructions |

The base prompt must belong to the target workspace. Additional instructions
are prepended to the base prompt at launch time.

### 6.6 GitHub PAT and schedule tools

| Tool | Purpose |
|---|---|
| `list_github_pats` | List usable PAT metadata and IDs |
| `list_session_schedules` | List cron/event triggers |
| `add_session_schedule` | Create cron/event trigger |
| `update_session_schedule` | Partially update trigger |
| `delete_session_schedule` | Delete trigger |

Schedule tool inputs include:

```text
trigger_type
cron_schedule
event_condition
author_scope
fix_authors
label
prompt_id
provider
instruction_prompt
include_event_context
enabled
delay_minutes
```

Cron schedules require a valid cron expression. Event schedules support:

```text
ci_fail_or_conflict
new_pr_or_commit
review_comments
review_requested
review_approved
pr_comment
any_actionable
```

## 7. Recommended orchestration workflow

1. Call `list_workspaces`.
2. Call `find_sessions_by_repo` for the target repository.
3. Reuse a suitable inactive session or call `create_session`.
4. Call `add_repo_to_session` when the repository is not attached.
5. Call `list_workspace_prompts` and select a prompt if required.
6. Call `set_session_prompt`.
7. Call `launch_session`.
8. Call `wait_for_session` for prompt-mode work.
9. Call `get_session_output` if additional retrieval is needed.

For server/TUI sessions, launch completion only means setup has been requested;
the caller should inspect status before assuming the service is ready.

## 8. Formatting rules

MCP results should be concise but preserve machine-useful fields:

- IDs remain integers.
- Phase and trigger values remain exact enum strings.
- Empty optional values remain empty strings or null according to REST output.
- Do not return secret values when formatting PATs, gateways, or credentials.
- Repository lists returned by `get_session` include `id`, `repo_url`, `branch`,
  and `local_path`.

## 9. MCP implementation acceptance criteria

1. Every tool delegates authorization to REST.
2. Every REST operation added to session options is evaluated for MCP parity.
3. `queued` is included in status documentation and polling behavior.
4. MCP does not expose plaintext persisted secrets.
5. URL normalization behavior is stable and tested.
6. HTTP status and server error detail are preserved in `AgentSwarmAPIError`.
7. Tool descriptions accurately describe side effects and lifecycle timing.
8. Tool documentation uses `/sandbox` for repository paths.

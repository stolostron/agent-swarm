# Swarmer REST API Specification

## 1. Overview

The public programmatic API is mounted at `/api/v1` and implemented with
FastAPI. All API endpoints use bearer-token authentication and database-backed
workspace authorization.

## 2. Authentication

Every API request must include:

```http
Authorization: Bearer <Kubernetes-token>
```

The token is validated through Kubernetes TokenReview or the configured
fallback identity probe. Invalid or expired tokens return `401`.

The API does not accept a Swarmer database session cookie as API
authentication.

## 3. Authorization

Workspace endpoints hide inaccessible resources as `404` to avoid revealing
their existence. Access is granted when any of the following applies:

- The deployment uses a shared namespace.
- The caller is a global administrator.
- The caller is `Workspace.owner_id`.
- The caller has a matching `WorkspaceMember` row.
- The workspace is unclaimed (`owner_id == ""`).

Management operations additionally require the owner or global administrator,
except that an unclaimed workspace may be claimed by the first management
caller.

Credential records have an additional owner/shared visibility layer. Shared
credential mutation should be treated as manager-only even where current
legacy handlers are more permissive.

## 4. Response and error conventions

Successful JSON errors and messages use:

```json
{ "detail": "..." }
```

Resource deletion commonly returns `200` with `MessageOut`; schedule deletion
returns `204` without a body.

Expected status codes:

- `200`: successful read or mutation
- `201`: successful creation
- `204`: successful schedule deletion
- `400`: malformed, unsupported, or gateway-invalid request
- `401`: unauthenticated/invalid token
- `403`: insufficient permission
- `404`: resource missing or inaccessible
- `409`: duplicate resource or invalid lifecycle transition
- `422`: schema or semantic validation failure
- `502`: OpenShell/Jira/GitHub dependency failure

## 5. Common schemas

### 5.1 `MessageOut`

```json
{ "detail": "string" }
```

### 5.2 `WorkspaceOut`

```json
{
  "id": 1,
  "display_name": "string",
  "namespace": "string",
  "description": "string",
  "owner_id": "string",
  "gateway": null,
  "created_at": "datetime",
  "updated_at": "datetime",
  "ai_provider_warning": false,
  "missing_ai_providers": []
}
```

`gateway` is either `null` or a `WorkspaceGatewayOut` containing presence
booleans rather than secret material.

### 5.3 `SessionOut`

Fields include:

```text
id, workspace_id, name, mode, provider, agent_tool,
instruction_prompt, github_pat_id, prompt_id, working_branch,
phase, status_detail, sandbox_name, service_url,
cron_schedule, cron_label, run_started_at, run_completed_at,
run_duration, is_active, created_at, updated_at, schedules
```

The `cron_*` fields are compatibility fields. New scheduling uses the
`schedules` collection.

### 5.4 `SessionRunOut`

```text
id, session_id, phase, status_detail, started_at, completed_at,
run_duration, last_output, raw_output, schedule_label, prompt_name,
mode, trigger_type, event_context
```

## 6. Endpoint catalog

### 6.1 Identity and global administration

#### `GET /api/v1/me`

Returns the authenticated username and permission flags.

Response:

```json
{
  "username": "string",
  "is_admin": false,
  "can_create_workspace": true,
  "admin_bootstrap_available": false
}
```

#### `GET /api/v1/users`

Returns visibility-scoped autocomplete identities:

```json
{ "users": ["string"] }
```

This is not a general user directory.

#### `GET /api/v1/admins`

Admin-only list of `GlobalAdminOut` records.

#### `POST /api/v1/admins`

Admin-only. Body:

```json
{ "user_id": "string" }
```

Returns `201` and `GlobalAdminOut`; duplicate identity returns `409`.

#### `DELETE /api/v1/admins/{user_id}`

Admin-only. Returns `404` when the identity is not a database administrator.

#### `POST /api/v1/admins/bootstrap`

Allows the authenticated caller to become the first database administrator
only when no static or database admin exists. A later call returns `409`.

### 6.2 Workspace CRUD

#### `GET /api/v1/workspaces`

Lists accessible workspaces ordered by display name. The response may include
provider-warning fields identifying missing configured OpenShell providers.

#### `POST /api/v1/workspaces`

Body:

```json
{
  "display_name": "string",
  "description": "string",
  "gateway": {
    "gateway_url": "string",
    "auth_mode": "oidc",
    "oidc_issuer": "string|null",
    "oidc_client_id": "string|null",
    "oidc_audience": "string|null",
    "refresh_token": "string|null",
    "access_token": "string|null",
    "client_secret": "string|null",
    "service_account_subject": "string|null",
    "bearer_token": "string|null",
    "tls_ca": "string|null",
    "tls_cert": "string|null",
    "tls_key": "string|null",
    "tls_verify": true
  }
}
```

Workspace creation may be disabled in namespace-scoped deployments. Namespace
collisions return `409`.

#### `GET /api/v1/workspaces/{ws_id}`

Returns an accessible workspace or `404`.

#### `PUT /api/v1/workspaces/{ws_id}`

Body:

```json
{ "display_name": "string", "description": "string" }
```

Owner/admin only. The namespace is not regenerated when the display name is
changed.

#### `DELETE /api/v1/workspaces/{ws_id}`

Owner/admin only. Deletes database state first and performs best-effort legacy
Kubernetes cleanup afterward.

### 6.3 Workspace members

#### `GET /api/v1/workspaces/{ws_id}/members`

Returns `WorkspaceMemberOut[]`, ordered by `user_id`.

#### `POST /api/v1/workspaces/{ws_id}/members`

Body:

```json
{ "user_id": "string", "role": "member" }
```

Owner/admin only. The owner cannot also be inserted as a member. Duplicate
membership returns `409`.

#### `DELETE /api/v1/workspaces/{ws_id}/members/{user_id}`

Owner/admin only. Removes an explicit membership.

### 6.4 Dedicated gateway

#### `POST /api/v1/workspaces/gateway/parse-command`

Body:

```json
{ "command": "string" }
```

Returns parsed gateway fields, a suggested name, and parser errors. Submitted
credentials must not be logged.

#### `POST /api/v1/workspaces/gateway/parse-token`

Body:

```json
{ "token_input": "string" }
```

Returns parsed token metadata and detected format. Treat the request and
response as sensitive.

#### `POST /api/v1/workspaces/gateway/test-connection`

Tests a supplied gateway configuration. When `workspace_id` is supplied, the
caller must be able to access and manage that workspace to reuse stored
credentials.

#### `GET /api/v1/workspaces/{ws_id}/gateway`

Returns `404` when the workspace uses the cluster-default gateway.

#### `POST /api/v1/workspaces/{ws_id}/gateway`

Owner/admin only. Replaces the dedicated gateway configuration. Optional
credentials supplied as `null` or empty values may clear existing state;
clients must send values they intend to retain.

#### `DELETE /api/v1/workspaces/{ws_id}/gateway`

Owner/admin only. Deletes dedicated configuration and reverts to the cluster
default gateway.

### 6.5 Sessions and lifecycle

#### `GET /api/v1/workspaces/{ws_id}/sessions`

Lists sessions ordered by name.

#### `POST /api/v1/workspaces/{ws_id}/sessions`

Body:

```json
{
  "name": "string",
  "mode": "prompt",
  "provider": "",
  "agent_tool": "opencode",
  "instruction_prompt": "",
  "github_pat_id": null,
  "prompt_id": null,
  "working_branch": ""
}
```

Valid modes are `prompt`, `server`, and `tui`. Valid tools are `opencode` and
`shell`. If no branch is provided, the server generates a unique
`swarmer/session-...` branch after insert.

MCP selection is disabled by default for newly created sessions. Supplying a
non-empty `mcp_server_ids` list selects those caller-visible MCPs; an empty list
disables all MCPs. Set `mcp_selection` to `"inherit"` to opt into all currently
eligible MCPs in the workspace. IDs must belong to the workspace and be visible
to the caller; the request fails as a whole if any ID is invalid or inaccessible.
When requesting inheritance, omit `mcp_server_ids` (or send `null`); combining
inheritance with an ID list is rejected.
The workspace MCP inventory is available from `GET /api/v1/workspaces/{ws_id}/mcp-servers`
and contains caller-visible safe metadata only. Endpoint URLs omit userinfo,
query strings, and fragments; credentials and ownership identifiers are never returned.
The session UI uses only explicit per-server checkboxes, unchecked by default for
new sessions. The API inheritance mode remains supported for existing clients
and legacy sessions. Configured workspace MCP servers and credentials are shared
with sessions in the workspace, including scheduled/background sessions.

#### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}`

Returns the complete session resource, including schedule entries.

#### `PUT /api/v1/workspaces/{ws_id}/sessions/{sid}`

Updates only supplied fields. Active sessions cannot be edited and return
`409`. Name collisions return `409`. Working branches are validated as Git ref
names. For MCPs, omitted or `null` selection fields leave the existing
configuration unchanged; `mcp_server_ids: []` disables all, a non-empty list
replaces the selection, and `mcp_selection: "inherit"` restores inheritance.
An inheritance update must omit `mcp_server_ids` or set it to `null`.
`mcp_selection` readback is `inherit`, `disabled`, or `selected`;
`mcp_server_ids` reports the caller-visible saved selection, while
`runtime_mcp_server_ids` contains only enabled, authenticated, unexpired MCPs
that are eligible for this session.

#### `DELETE /api/v1/workspaces/{ws_id}/sessions/{sid}`

Active sessions cannot be deleted. For inactive sessions, the server attempts
to delete the OpenShell service and sandbox before deleting the database row.

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/launch`

Optional body:

```json
{
  "pr_context": {},
  "event_context": "string",
  "instruction_prompt": "string"
}
```

Starts a run through the shared launch path. If the concurrency limit is full,
the session transitions to `queued` rather than returning a capacity error.

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/stop`

Stops a queued or executing session. Queued sessions return to `idle`; active
runtime sessions become `stopped` and have their sandbox/service cleaned up.

#### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/runs`

Returns up to 100 runs, newest completion first.

#### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/output`

Returns:

```json
{ "output": "string", "raw_output": "string" }
```

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/clear-output`

Clears processed and raw latest output.

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/generate-patch`

Requires a running OpenShell session. Executes `git diff` or
`git diff origin/{patch_base_ref}` and persists the result.

Response:

```json
{ "patch": "string", "commit_msg": "string", "filename": "string" }
```

#### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/download-patch`

Returns the persisted patch as `text/x-patch`. Returns `404` when no patch is
available.

### 6.6 Schedule APIs

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/schedule`

Compatibility endpoint for one cron expression. It writes deprecated session
fields and upserts an unlabeled `SessionSchedule`.

Body:

```json
{ "cron_expr": "0 9 * * 1-5" }
```

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/unschedule`

Disables all schedule entries and clears deprecated session cron fields.

#### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/schedules`

Lists schedule entries ordered by creation time.

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/schedules`

Body:

```json
{
  "trigger_type": "cron",
  "cron_schedule": "0 9 * * 1-5",
  "event_condition": "",
  "author_scope": "all",
  "fix_authors": "",
  "label": "Weekday maintenance",
  "prompt_id": 1,
  "provider": "",
  "instruction_prompt": "",
  "include_event_context": true,
  "enabled": true,
  "delay_minutes": 0
}
```

`prompt_id` is required and must reference a prompt in the same workspace. Cron schedules
require a valid cron expression. Event schedules use event filters and do not
require a cron expression.

#### `PUT /api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}`

Partially updates an existing schedule. Changing between cron and event
triggers resets fields that do not apply to the selected trigger type.

#### `DELETE /api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}`

Deletes the schedule and returns `204`.

### 6.7 Repository APIs

#### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/repos`

Lists repository attachments.

#### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/repos`

Body:

```json
{ "repo_url": "https://github.com/org/repo", "branch": "main", "local_path": "" }
```

The URL must be a valid GitHub URL. An omitted local path is derived from the
repository name. Absolute paths and paths containing `..` are rejected.

#### `DELETE /api/v1/workspaces/{ws_id}/sessions/{sid}/repos/{rid}`

Removes an attachment. Repository changes are forbidden while the session is
active.

### 6.8 Credentials and secrets

Credential responses return metadata and presence markers, not secret values.

Endpoints:

```text
GET/POST  /api/v1/workspaces/{ws_id}/secrets/credentials
DELETE    /api/v1/workspaces/{ws_id}/secrets/credentials/{provider}
GET/POST  /api/v1/workspaces/{ws_id}/secrets/pats
PUT/DELETE /api/v1/workspaces/{ws_id}/secrets/pats/{pat_id}
GET/PUT/DELETE /api/v1/workspaces/{ws_id}/secrets/github-app
GET/POST/DELETE /api/v1/workspaces/{ws_id}/secrets/pull-secret
```

AI credential saves may configure OpenShell providers and therefore can return
`502` when the gateway operation fails. Shared provider configuration is
manager-only.

### 6.9 Environment variables

```text
GET    /api/v1/workspaces/{ws_id}/env-vars
POST   /api/v1/workspaces/{ws_id}/env-vars
DELETE /api/v1/workspaces/{ws_id}/env-vars/{key}
```

`POST` upserts by key. The current response includes the decrypted value; this
is an intentional but high-risk contract requiring authenticated workspace
access and careful logging.

### 6.10 MCP server configuration

```text
GET    /api/v1/workspaces/{ws_id}/mcp-servers
POST   /api/v1/workspaces/{ws_id}/mcp-servers
POST   /api/v1/workspaces/{ws_id}/mcp-servers/{server_id}/save
GET    /api/v1/workspaces/{ws_id}/mcp-servers/check
POST   /api/v1/workspaces/{ws_id}/mcp-servers/{server_id}/toggle
DELETE /api/v1/workspaces/{ws_id}/mcp-servers/{server_id}
```

Creation accepts a catalog slug. Current catalog support is Jira-oriented.
Saving validates server URL, email, and token, then probes Jira and records
health status. Inventory visibility follows MCP ownership and sharing rules;
credential values and ownership identifiers are not returned.

### 6.11 Prompt sources

```text
GET/POST /api/v1/workspaces/{ws_id}/prompts
GET/PUT/DELETE /api/v1/workspaces/{ws_id}/prompts/{ps_id}
POST /api/v1/workspaces/{ws_id}/prompts/{ps_id}/refresh
GET /api/v1/workspaces/{ws_id}/prompts/{ps_id}/prompts/{prompt_id}/preview
GET /api/v1/workspaces/{ws_id}/prompts/browse/repos
GET /api/v1/workspaces/{ws_id}/prompts/browse/folders
```

Creating a source performs an initial synchronization. Refresh updates prompt
contents and records `last_synced_at` or `sync_error`.

## 7. Runtime semantics

### Prompt mode

The agent runs once. Swarmer captures processed and raw output, records a
terminal run, and deletes the sandbox after successful completion.

### Server mode

The agent server runs persistently. OpenShell exposes a service and Swarmer
stores its virtual URL. The HTTP/SSE/WebSocket proxy connects to the real
gateway while preserving the virtual `Host` header required for routing.

### TUI mode

The sandbox remains alive. A one-time browser token authorizes a WebSocket PTY
connection, and the agent starts through OpenShell interactive execution.

## 8. API implementation acceptance criteria

1. Every endpoint requires API bearer authentication.
2. Every workspace resource is authorization-scoped.
3. Inaccessible workspace IDs return `404` rather than leaking existence.
4. Secret responses contain presence flags or masked values only. The
   environment-variable API currently returns a decrypted value, which is a
   known deviation requiring remediation; this is the target contract.
5. Session lifecycle transitions preserve queued/no-sandbox semantics.
6. Schedule prompt references are workspace-scoped.
7. Repository paths cannot escape the sandbox workspace.
8. REST schema changes are evaluated for corresponding MCP changes.

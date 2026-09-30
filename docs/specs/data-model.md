# Swarmer Data Model Specification

## 1. Purpose

Swarmer stores application state in SQLite through SQLAlchemy async models.
SQLite is the authoritative store for workspaces, access control, session
configuration, schedules, execution history, credentials metadata, prompt
sources, and watcher state.

The deployment uses a single application writer. Schema creation and
incremental migrations are implemented in `swarmer/database.py`.

## 2. Conventions

### 2.1 Identifiers and timestamps

- Primary keys are integer IDs unless explicitly documented otherwise.
- Foreign keys identify parent records but are not always declared with an
  ORM relationship. Consumers must still treat them as references.
- Timestamps are server-generated where possible.
- Timestamps are persisted as database datetimes and serialized as JSON
  datetimes by the REST API.
- Runtime duration is a derived human-readable string, not a persisted
  numeric duration.

### 2.2 Null, empty, and legacy values

- `NULL` means no value or no association.
- Empty strings are used extensively for optional legacy text fields.
- Empty `owner_id` means a legacy or unclaimed workspace.
- Empty `sandbox_name` means no active OpenShell sandbox is recorded.
- Empty `cron_schedule` means no cron schedule.
- Deprecated fields remain in the database to support upgrades and old rows.

### 2.3 Encryption

Sensitive persisted values are encrypted with the application Fernet key.
Encrypted columns use the `_enc` suffix and model properties provide plaintext
access only inside trusted server code.

The following values must never be returned in normal resource responses:

- GitHub PATs
- GitHub App private keys
- OpenShell bearer, refresh, access, client-secret, and TLS-key values
- Jira access tokens
- Workspace environment-variable ciphertext/plaintext, except where the
  explicit environment-variable API currently exposes the decrypted value
- AI provider secrets

### 2.4 Cascading behavior

Workspace-owned records are deleted with the workspace where relationships use
`delete-orphan` or database `ON DELETE CASCADE`. Session repositories,
schedules, and runs are session-owned. Deleting a session therefore removes
its persisted execution history, but OpenShell cleanup must happen before or
alongside deletion to avoid orphaned runtime resources.

## 3. Entity catalog

### 3.1 `workspaces`

Represents a logical tenant and the parent of sessions and workspace-scoped
configuration.

| Column | Type | Constraints/default | Description |
|---|---|---|---|
| `id` | integer | PK | Workspace identifier |
| `display_name` | string(255) | required | User-visible name |
| `namespace` | string(63) | required, unique | Derived slug used by legacy Kubernetes features |
| `description` | text | required, default `""` | User-visible description |
| `owner_id` | text | required, default `""` | Authenticated identity that owns the workspace |
| `created_at` | datetime | server default | Creation time |
| `updated_at` | datetime | server default/on update | Last modification time |

The namespace is derived by lowercasing the display name, replacing runs of
non-alphanumeric characters with `-`, trimming leading/trailing hyphens, and
truncating to 63 characters. A name with no alphanumeric content is invalid.

Relationships:

- Optional one-to-one `WorkspaceGateway`
- One-to-many `Session`
- One-to-many `GitHubPAT`
- Optional one-to-one `GitHubApp`
- One-to-many `McpServer`
- One-to-many `WorkspacePromptSource`
- One-to-many `SandboxEnvVar`
- One-to-many `WorkspaceMember`
- Zero or more user-scoped `OpencodeSecret` records

### 3.2 `workspace_members`

Explicit database-backed workspace access grants.

| Column | Type | Constraints/default |
|---|---|---|
| `id` | integer | PK |
| `workspace_id` | integer | FK, required |
| `user_id` | text | required |
| `role` | string(32) | required, default `member` |
| `created_at` | datetime | server default |

Unique key: `(workspace_id, user_id)`.

The current authorization implementation uses owner/admin checks for
management. The persisted `role` is retained for future role-specific policy
but is not currently a complete permission model.

### 3.3 `global_admins`

Self-service global administrators.

| Column | Type | Constraints/default |
|---|---|---|
| `id` | integer | PK |
| `user_id` | text | required, unique |
| `created_by` | text | required |
| `created_at` | datetime | server default |

Static administrators configured through environment variables are also
recognized but are not rows in this table.

### 3.4 `workspace_gateways`

Optional dedicated OpenShell Gateway configuration. The primary key is also a
foreign key to `workspaces.id`, enforcing one gateway per workspace.

| Column | Type | Exposure |
|---|---|---|
| `workspace_id` | integer | public identifier |
| `gateway_url` | string(1024) | returned |
| `gateway_version` | string(64) | returned |
| `auth_mode` | string(32) | returned |
| `oidc_issuer` | string(1024) | returned |
| `oidc_client_id` | string(255) | returned |
| `oidc_audience` | string(255) | returned |
| `refresh_token_enc` | text | `has_refresh_token` only |
| `access_token_enc` | text | `has_access_token` and expiry only |
| `access_token_expires_at` | datetime | returned |
| `client_secret_enc` | text | `has_client_secret` only |
| `service_account_subject` | string(255) | returned |
| `bearer_token_enc` | text | `has_bearer_token` only |
| `tls_ca` | text | returned currently |
| `tls_cert` | text | `has_tls_cert` only |
| `tls_key_enc` | text | `has_tls_key` only |
| `tls_verify` | boolean | returned |
| `created_at`, `updated_at` | datetime | returned |

If no row exists, the workspace uses the cluster default gateway.

### 3.5 `openshell_gateway_versions`

Global cache of the last observed version per gateway endpoint.

| Column | Type | Constraints |
|---|---|---|
| `gateway_url` | string(1024) | PK |
| `gateway_version` | string(64) | required |
| `updated_at` | datetime | server default/on update |

### 3.6 `opencode_secrets`

Legacy table name for AI provider credentials and provider configuration.

Unique key: `(workspace_id, user_id)`.

| Column | Description |
|---|---|
| `workspace_id`, `user_id` | Scope and owner |
| `shared` | Whether workspace users may use the record |
| `google_cloud_project`, `vertex_location` | Vertex metadata |
| `application_default_credentials_enc` | Encrypted ADC JSON |
| `google_api_key_enc` | Encrypted legacy Google AI Studio key |
| `openai_api_key_enc` | Encrypted compatibility key |
| `gemini_configured`, `openai_configured`, `vertex_configured` | Non-secret provider markers |
| `created_at`, `updated_at` | Audit timestamps |

New provider credentials are normally registered with OpenShell. These rows
remain for compatibility and for safe UI status reporting.

### 3.7 `github_pats`

Workspace GitHub Personal Access Tokens.

Unique key: `(workspace_id, name)`.

| Column | Description |
|---|---|
| `id` | PAT identifier |
| `workspace_id` | Owning workspace |
| `user_id`, `shared` | Visibility and ownership |
| `name` | Workspace-unique label |
| `github_username`, `github_org` | Display metadata |
| `pat_enc` | Encrypted token |
| `description` | User description |
| `created_at`, `updated_at` | Audit timestamps |

### 3.8 `github_apps`

One GitHub App installation configuration per workspace.

| Column | Description |
|---|---|
| `id` | Record identifier |
| `workspace_id` | Unique workspace FK |
| `user_id`, `shared` | Visibility/ownership |
| `app_id` | GitHub App identifier |
| `installation_id` | GitHub installation identifier |
| `private_key_enc` | Encrypted PEM private key |
| `created_at`, `updated_at` | Audit timestamps |

The private key is used only server-side to mint short-lived installation
access tokens. It must never enter a sandbox or API response.

### 3.9 `sessions`

Represents a configured agent workload and its current runtime state.

Unique key: `(workspace_id, name)`.

| Column | Description |
|---|---|
| `id`, `workspace_id` | Identity and parent |
| `github_pat_id` | Optional GitHub PAT reference |
| `prompt_id` | Optional workspace prompt reference |
| `name` | Workspace-unique session name |
| `mode` | `prompt`, `server`, or `tui` |
| `provider` | Provider family selection, usually `claude`, `gemini`, `openai`, or empty |
| `agent_tool` | `opencode` or `shell` |
| `instruction_prompt` | Additional instructions or shell command |
| `working_branch` | Git branch/ref used by the agent |
| `mcp_server_ids` | Comma-separated enabled MCP IDs; exposed as a list |
| `phase` | Runtime lifecycle state |
| `sandbox_name` | OpenShell sandbox name |
| `service_url` | Exposed server-mode service URL |
| `last_output`, `raw_output` | Latest processed/raw output |
| `status_detail` | Current status text |
| `event_context` | Serialized launch/event context |
| `policy_chunks`, `custom_policies` | Network-policy state snapshots |
| `run_started_at`, `run_completed_at` | Current/last run timestamps |
| `patch_output`, `commit_msg`, `patch_base_ref` | Patch-generation state |
| `active_schedule_id` | Schedule responsible for current run |
| `cron_schedule`, `cron_next_run` | Deprecated compatibility state |
| `ephemeral_disk` | Vestigial compatibility column; no longer used |
| `created_at`, `updated_at` | Audit timestamps |

Valid phases:

```text
idle, queued, pending, running, succeeded, failed, stopped
```

`queued`, `pending`, and `running` are active. A queued session has no sandbox
and is waiting for the global concurrency limit.

### 3.10 `session_repos`

Repositories cloned into a session workspace.

Unique key: `(session_id, local_path)`.

| Column | Description |
|---|---|
| `id` | Repository attachment ID |
| `session_id` | Parent session |
| `repo_url` | GitHub HTTPS repository URL |
| `branch` | Clone branch, default `main` |
| `local_path` | Relative path inside the sandbox workspace |
| `created_at` | Creation timestamp |

### 3.11 `session_schedules`

Cron and GitHub event triggers for sessions.

| Column | Description |
|---|---|
| `id`, `session_id` | Identity and parent |
| `prompt_id` | Prompt required for scheduled execution |
| `provider` | Optional provider override; empty inherits session provider |
| `trigger_type` | `cron` or `event` |
| `event_condition` | GitHub event filter |
| `author_scope` | `self`, `team`, `bots`, or `all` |
| `fix_authors` | Comma-separated GitHub logins |
| `cron_schedule` | Cron expression for cron triggers |
| `cron_next_run` | Derived next execution time |
| `label` | Human-readable trigger name |
| `instruction_prompt` | Trigger-specific instructions |
| `include_event_context` | Include GitHub event data |
| `delay_minutes` | Quiet period/debounce from 0 to 1440 |
| `enabled` | Whether trigger is active |
| `created_at`, `updated_at` | Audit timestamps |

Supported event conditions:

```text
ci_fail_or_conflict
new_pr_or_commit
review_comments
review_requested
review_approved
pr_comment
any_actionable
```

### 3.12 `session_runs`

Terminal execution history. A run is recorded only for `succeeded`, `failed`,
or `stopped` executions with a known start time.

| Column | Description |
|---|---|
| `id`, `session_id` | Identity and parent |
| `phase` | Terminal phase |
| `status_detail` | Terminal reason |
| `started_at`, `completed_at` | Execution interval |
| `last_output`, `raw_output` | Output snapshots |
| `schedule_label`, `prompt_name` | Denormalized source names |
| `mode` | Mode snapshot |
| `trigger_type` | `manual`, `cron`, or `event` |
| `event_context` | Event snapshot |
| `created_at` | Record timestamp |

History is pruned by configurable per-session count and age limits.

### 3.13 `mcp_servers`

Workspace MCP server registrations.

Unique key: `(workspace_id, slug)`.

| Column | Description |
|---|---|
| `id`, `workspace_id` | Identity and scope |
| `user_id`, `shared` | Visibility and ownership |
| `slug`, `display_name` | Catalog and display identity |
| `server_url`, `server_type` | Connection metadata |
| `enabled` | Whether available to sessions |
| `token_expires_at` | Health marker |
| `jira_server_url`, `jira_email` | Jira metadata |
| `jira_access_token_enc` | Encrypted Jira token |
| `created_at`, `updated_at` | Audit timestamps |

### 3.14 `workspace_prompt_sources` and `workspace_prompts`

Prompt sources point to Git repositories. Prompts are synchronized files.

`workspace_prompt_sources` unique key: `(workspace_id, name)`.

Source fields:

- `workspace_id`
- `name`
- optional `github_pat_id`
- `repo_url`
- `branch`, default `main`
- `folder_path`, default `.`
- `last_synced_at`
- `sync_error`
- `created_at`, `updated_at`

Prompt fields:

- `id`, `source_id`
- `filename`, `display_name`
- `content`, `content_hash`
- `created_at`, `updated_at`

Deleting a source deletes its synchronized prompts. Sessions referencing a
deleted prompt become null through `ON DELETE SET NULL` behavior.

### 3.15 `workspace_env_vars`

Workspace environment variables injected directly into agent processes.

Unique key: `(workspace_id, key)`.

| Column | Description |
|---|---|
| `id`, `workspace_id` | Identity and scope |
| `key` | Environment variable name |
| `value_enc` | Fernet-encrypted value |
| `created_at`, `updated_at` | Audit timestamps |

Valid keys match:

```text
^[A-Za-z_][A-Za-z0-9_]{0,254}$
```

### 3.16 PR watcher state

These tables are internal durability and deduplication records.

#### `pr_action_state`

Tracks dispatch attempts and circuit-breaker state for a repository, PR,
commit, action condition, session, and event.

#### `repo_etags`

Stores GitHub Events API ETags per repository.

#### `github_event_receipts`

Deduplicates qualifying events by `(repo, event_id)`.

#### `pr_comment_dispatches`

Stores schedule-scoped sliding debounce state for PR comments, including
`not_before`, latest comment event, attempts, and errors.

These tables are not public REST resources and are not exposed by MCP.

## 4. Lifecycle invariants

1. A session cannot be edited while `queued`, `pending`, or `running`.
2. A session cannot be deleted while active.
3. A queued session has no sandbox to clean up.
4. Stopping a queued session returns it to `idle`.
5. Stopping an executing session records a `stopped` run when possible.
6. Successful prompt-mode completion deletes the sandbox.
7. Server/TUI sessions retain a sandbox while active.
8. Server mode stores a gateway virtual service URL; the virtual hostname may
   not be DNS-resolvable from Swarmer.
9. GitHub App private keys never enter a sandbox.
10. Workspace credentials are encrypted before persistence.
11. Schedule run history stores source snapshots so later schedule/prompt edits
    do not rewrite historical meaning.

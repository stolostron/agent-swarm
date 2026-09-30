# Cross-Layer Contracts and Compatibility Notes

## 1. Purpose

Swarmer has three independently maintained interfaces:

1. SQLAlchemy persistence models
2. `/api/v1` REST resources
3. The standalone Agent Swarm MCP adapter

Any change to one layer must be evaluated against the other two. This document
records the invariants and current discrepancies that must be resolved or
explicitly preserved.

## 2. Required change flow

### New persisted state

1. Add or update the SQLAlchemy model.
2. Import it in `swarmer/models/__init__.py`.
3. Add an idempotent migration in `swarmer/database.py`.
4. Define encryption/accessor behavior for sensitive values.
5. Add unit tests for creation, migration, relationships, and deletion.

### New REST feature

1. Add Pydantic request/response schemas.
2. Add the API route under `swarmer/api/v1/`.
3. Apply `require_api_auth` and workspace ACL dependencies.
4. Define status codes and error bodies.
5. Add API tests.
6. Update MCP client methods, tool wrappers, descriptions, and tests.

### New MCP feature

1. Add an HTTP client method that maps exactly to REST.
2. Add a testable server implementation method.
3. Register the FastMCP tool.
4. Document input defaults and lifecycle behavior.
5. Add client and server tests.

## 3. Security invariants

1. Never commit or document real credentials, usernames, service-account
   identities, project IDs, registry URLs, or cluster URLs.
2. Never log raw bearer tokens, PATs, private keys, API keys, refresh tokens,
   client secrets, or Jira tokens.
3. Encrypt sensitive values before persistence.
4. Return presence booleans or masked values rather than secrets.
5. Never inject a GitHub App private key into OpenShell.
6. Treat gateway/token parsing requests and responses as sensitive.
7. Keep workspace access checks in `workspace_acl.py`; do not add ad hoc
   authorization logic to individual handlers.
8. Keep OpenShell policy fail-closed.

## 4. Lifecycle invariants

1. `queued` means capacity wait and has no sandbox.
2. `pending` means launch/setup is in progress.
3. `running` means the session has an active runtime.
4. `succeeded`, `failed`, and `stopped` are terminal for a run.
5. Prompt mode is one-shot and normally deletes its sandbox after success.
6. Server and TUI modes retain their sandbox while active.
7. Stopping a queued session returns it to `idle`.
8. Editing, repository changes, and deletion are blocked while active.
9. Run-history records capture trigger metadata at completion and are not
   rewritten when schedules/prompts later change.

## 5. Known implementation discrepancies

### 5.1 `persist` session option

Some MCP code and historical documentation describe a `persist` session option,
but the current REST request schemas and active persistence model do not define
it. The contract must choose one of:

- Remove `persist` from MCP signatures and documentation; or
- Reintroduce it consistently in REST schemas, persistence, lifecycle behavior,
  tests, and MCP.

Until resolved, clients must not rely on `persist` being accepted or stored.

### 5.2 Session MCP server assignment

REST create/update schemas support `mcp_server_ids`, while current MCP tool
signatures do not expose it consistently. MCP must expose the field or clearly
document that REST is required for session MCP assignment.

### 5.3 Schedule provider on create

The schedule create schema and MCP tool accept `provider`, but the REST create
handler must persist it. Otherwise the value is silently discarded. Add a
regression test covering create, response, and subsequent launch resolution.

### 5.4 Schedule prompt validation on update

When changing `prompt_id`, the prompt must belong to the same workspace. The
REST implementation performs this validation; any future schedule mutation
path must preserve it.

### 5.5 Gateway auth-mode enumeration

The model comments mention `oidc`, `bearer`, `mtls`, and `none`, while public
schemas/tool descriptions do not consistently expose the same set. Define and
validate one enum. If mTLS remains supported, document required certificate,
key, CA, and verification behavior.

### 5.6 Repository path documentation

The runtime workspace is `/sandbox`. Any MCP or REST documentation that says
`/workspace` is incorrect and must be changed to `/sandbox`.

### 5.7 Environment variable response exposure

The environment-variable API currently returns decrypted values. This is a
high-risk but observable contract. A safer future contract would return names
and presence markers, with an explicit reveal operation if required.

### 5.8 Credential ownership consistency

AI credential writes enforce manager restrictions for shared provider setup.
PAT, GitHub App, MCP, and environment-variable ownership checks should be
reviewed and made consistent. Shared credentials should have one documented
policy across all resource types.

### 5.9 Foreign-key scope validation

Session references such as `github_pat_id`, `prompt_id`, and
`mcp_server_ids` must be validated against the same workspace and caller
visibility. Database foreign keys alone do not enforce this tenant boundary.

## 6. Compatibility fields

The following fields are retained for legacy clients or migration safety:

- `Session.cron_schedule`
- `Session.cron_next_run`
- `Session.cron_label`
- `Session.ephemeral_disk`
- Legacy encrypted AI credential columns
- Namespace-related workspace behavior for pull-secret features

New clients should use schedule sub-resources and OpenShell provider state
instead of extending these legacy fields.

## 7. Required test matrix

### Data model

- Fresh schema creation
- Re-running all migrations
- Encryption round trips
- Workspace cascade deletion
- Session run retention by count and age
- Schedule/prompt deletion behavior
- Unique constraint conflicts

### REST

- Invalid/expired bearer token
- Owner/member/admin/unclaimed/shared-namespace access
- Hidden workspace returns `404`
- Shared credential authorization
- Session phase transitions
- Capacity queue behavior
- Prompt and schedule workspace scoping
- Repository URL and path validation
- Gateway secret redaction
- OpenShell dependency failures

### MCP

- Environment token resolution precedence
- Kubeconfig and in-cluster token resolution
- HTTP error conversion
- URL normalization
- Session formatting
- Schedule formatting
- `wait_for_session` terminal and timeout behavior
- No plaintext secret formatting
- REST/MCP option parity

## 8. Definition of done for contract changes

A change is complete only when:

1. Data model and migrations are updated where needed.
2. REST schemas, routes, status codes, and tests are updated.
3. MCP client, server tool, descriptions, and tests are updated.
4. Documentation no longer describes removed or renamed behavior.
5. Sensitive values remain encrypted and redacted.
6. Workspace authorization is preserved.
7. Lifecycle semantics are covered by tests.

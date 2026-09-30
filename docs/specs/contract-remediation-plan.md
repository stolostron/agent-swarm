# Contract Discrepancy Remediation Plan

## 1. Goal

Resolve the discrepancies identified in [Cross-Layer Contracts](cross-layer-contracts.md) so that the persistence model, REST API, UI/API client, and standalone MCP server describe and implement the same behavior.

This is a corrective plan, not a feature redesign. It prioritizes authorization and tenant-boundary correctness first, removes no-op public options next, then closes REST/MCP parity and documentation gaps.

## 2. Delivery principles

1. Make one canonical contract decision before changing code.
2. Preserve existing valid persisted data; use migrations only when a persisted model changes.
3. Treat an API field as supported only when it is accepted, persisted or acted upon, returned where appropriate, documented, and tested.
4. Validate every resource reference against both its workspace and caller visibility.
5. Do not expose plaintext secrets while improving parity.
6. Make breaking changes explicit, versioned where necessary, and documented in release notes.
7. Update REST and MCP in the same pull request unless the change intentionally removes an MCP capability.

## 3. Prioritized workstreams

| Priority | Workstream | Risk addressed | Recommended disposition |
|---|---|---|---|
| P0 | Tenant-scoped session references | Cross-workspace credential/prompt/MCP use | Add server-side scope and visibility validation |
| P0 | Shared credential authorization | Unauthorized changes to shared secrets/configuration | Define and enforce one owner/manager policy |
| P1 | `persist` contract removal | MCP silently sends a no-op field | Remove from MCP and tests; do not reintroduce without approved runtime semantics |
| P1 | Session MCP assignment parity | MCP cannot configure a REST-supported session feature | Add `mcp_server_ids` to MCP create/update/read output |
| P1 | Schedule provider creation | Accepted provider override is silently lost | Persist and return `provider` on create |
| P1 | Gateway auth-mode contract | Invalid/ambiguous security configuration | Define one enum and mode-specific validation |
| P2 | Environment-variable secret exposure | Plaintext secret disclosure to API/MCP consumers | Introduce redacted default response and deliberate reveal/compatibility path |
| P2 | Documentation and generated contract audit | Stale path/phase/field documentation | Correct docs and add contract regression checks |

## 4. Phase 0 — establish the baseline

### Objective

Prevent further drift while remediation is underway.

### Actions

1. Add an issue or tracking epic with one child item per workstream.
2. Record the current API schema in automated tests or an OpenAPI snapshot.
3. Add a REST-to-MCP parity inventory listing each REST operation, the matching MCP client method/tool, and intentional exclusions.
4. Mark `persist` as unsupported in MCP release notes before removal.
5. Add a reviewer checklist requiring changes to `swarmer/api/schemas.py`, `swarmer/api/v1/`, `mcp-server/`, and `docs/specs/` to be considered together.

### Acceptance criteria

- Every discrepancy has an owner, priority, and planned release.
- CI contains a baseline parity test or explicit allowlist.
- No new session, schedule, credential, or gateway field is added without schema and MCP review.

## 5. Phase 1 — close tenant-boundary and authorization gaps

### 5.1 Validate session references

#### Problem

Session create/update accepts `github_pat_id`, `prompt_id`, and `mcp_server_ids` without a single central validation layer proving that each referenced record belongs to the target workspace and is visible to the caller.

#### Implementation plan

1. Add a service/helper module, for example `swarmer/session_references.py`, rather than duplicating SQL in create/update handlers.
2. Implement validation functions for:
   - GitHub PAT: ID exists, belongs to `ws_id`, and is caller-owned, shared, or legacy-visible.
   - Prompt: ID exists through a prompt source belonging to `ws_id`.
   - MCP server: every ID exists, belongs to `ws_id`, is visible to the caller, and is eligible for session use.
3. Pass caller identity to REST create/update handlers; use the same helper from HTML form handlers where applicable.
4. Return `422` for invalid request references and `404` when policy requires non-disclosure of an inaccessible resource. Select one behavior per resource type and document it.
5. Reject duplicate MCP IDs; preserve input ordering only if runtime ordering is meaningful. Otherwise canonicalize to sorted unique IDs before persistence.
6. Add an explicit clear representation for nullable references. For partial updates, use a field-set-aware request model so omitted means “unchanged” and explicit `null` means “clear.”

#### Tests

- PAT from another workspace is rejected.
- Private PAT owned by another user is rejected.
- Shared PAT in the same workspace is accepted.
- Prompt from another workspace is rejected on create and update.
- MCP server IDs from another workspace, invisible servers, missing IDs, and duplicates are rejected.
- Explicit clear of PAT/prompt/MCP assignment has defined behavior.
- Valid same-workspace references survive a create/read/update round trip.

### 5.2 Standardize shared-resource authorization

#### Problem

Credential resource types apply inconsistent ownership and shared-state restrictions.

#### Contract decision

Adopt this policy:

- A private record may be read/updated/deleted only by its owner or a workspace manager.
- A workspace manager may create, modify, share, unshare, or delete shared records.
- A non-manager may create a private record for themselves but cannot set `shared=true` or alter another owner’s record.
- Legacy ownerless records are treated as shared manager-controlled records until claimed/migrated.

#### Implementation plan

1. Introduce reusable authorization helpers for owned/shared resources.
2. Apply them to `GitHubPAT`, `GitHubApp`, `McpServer`, `OpencodeSecret`, and `SandboxEnvVar` operations.
3. Decide whether environment variables are always workspace-manager-owned; recommended: yes, because they are injected into every workspace sandbox.
4. Add manager checks before creating or toggling shared PAT/App/MCP configurations.
5. Ensure deletion of a PAT/App/provider cannot remove a provider still actively needed by another authorized session without a documented cleanup strategy.

#### Tests

- Member can create/manage own private PAT but cannot make it shared.
- Member cannot alter/delete a shared PAT/App/MCP configuration.
- Owner/admin can perform each shared-resource operation.
- Ownerless legacy resource follows documented migration policy.
- Authorization is enforced on both REST and UI form paths.

## 6. Phase 2 — restore session and schedule contract parity

### 6.1 Remove the unsupported `persist` option

#### Decision

Remove `persist` from MCP public tools, internal formatter output, client request payloads, README examples, and tests. The current database/runtime model has no persistence flag, so retaining it creates an observable false promise.

#### Implementation plan

1. Remove `persist` from `AgentSwarmMCPServer._create_session`, `_update_session`, and registered tool signatures.
2. Remove `persist` from MCP client request construction.
3. Remove it from `_fmt_session` output and MCP tests/fixtures.
4. Add a release-note compatibility statement: callers must omit `persist`; it was previously ignored by the REST schema.
5. Add a negative test confirming extra `persist` input is either rejected by a strict schema in the next API version or explicitly ignored only for a documented deprecation window.

#### Alternative requiring product approval

If persistent workspace volume semantics are required, design a new field with exact ownership, cleanup, quota, restart, and OpenShell behavior before adding it back. Do not reuse the name without a full end-to-end implementation.

### 6.2 Add MCP support for `mcp_server_ids`

#### Implementation plan

1. Add `mcp_server_ids: list[int] | None` to MCP create/update method and tool signatures.
2. Pass the value through `AgentSwarmClient.create_session` and `update_session`.
3. Add a read-only `mcp_server_ids` list to `SessionOut` and MCP formatted session output, derived from the persisted comma-separated representation.
4. Validate IDs through Phase 1 helper logic.
5. Document that an empty list disables all session-level MCP servers and omission leaves an update unchanged.

#### Tests

- MCP create/update payload includes IDs.
- REST response and MCP `get_session` return the same IDs.
- Empty list behavior is deterministic.
- Cross-workspace and inaccessible IDs are rejected.

### 6.3 Persist schedule provider on creation

#### Implementation plan

1. In `swarmer/api/v1/sessions.py:create_schedule`, pass `provider=body.provider.strip()` to `SessionSchedule`.
2. Normalize and validate provider values consistently with the update endpoint and Pydantic enum/pattern.
3. Confirm launch resolution prefers a non-empty schedule provider over the session provider.
4. Add migration only if legacy repair/backfill is required; a code-only fix is sufficient for new rows.

#### Tests

- Create with `provider="openai"` returns `openai`.
- Created schedule retains provider after reload.
- A scheduled launch resolves the schedule provider over the session provider.
- Empty provider inherits the session provider.
- Invalid providers fail with `422`.

## 7. Phase 3 — define and enforce gateway authentication modes

### Decision gate

Before coding, decide whether mTLS-only authentication is a supported public mode. Recommended supported values:

```text
none, bearer, oidc, mtls
```

If `mtls` is unsupported by the OpenShell client, remove it from comments and documentation instead of advertising it.

### Implementation plan

1. Define `GatewayAuthMode` as a shared `StrEnum` or equivalent literal type used by Pydantic schemas and server-side validation.
2. Replace unbounded `auth_mode: str` fields with the shared enum.
3. Define requirements by mode:
   - `none`: no auth credential required.
   - `bearer`: bearer token required.
   - `oidc`: issuer/client ID plus refresh token or client secret required; define whether existing stored credentials satisfy the requirement.
   - `mtls`: TLS certificate and private key required; define CA/verification behavior.
4. Reject irrelevant conflicting fields only where doing so will not break safe updates; otherwise ignore but document them.
5. Ensure the test-connection endpoint and stored-gateway endpoint use the same normalization and validation helper.
6. Update MCP tool descriptions and parameter docs.

### Tests

- Every accepted enum value works in schema validation.
- Unknown mode returns `422`.
- Each mode rejects missing mandatory credentials.
- Stored credential reuse is allowed only for the exact saved gateway URL.
- Responses never expose secret key/token material.

## 8. Phase 4 — reduce secret exposure without breaking users

### 8.1 Environment variables

#### Target contract

Default list responses return metadata, not plaintext values:

```json
[{"key":"EXAMPLE_TOKEN","has_value":true,"created_at":"...","updated_at":"..."}]
```

#### Migration plan

1. Introduce a new redacted response schema and update list endpoints.
2. Add an explicit, manager-restricted reveal endpoint only if the product has a valid use case. Prefer overwrite-without-read workflows.
3. Deprecate plaintext `EnvVarOut.value` with a documented release window.
4. Remove plaintext values from MCP output entirely.
5. Verify log and exception paths do not interpolate environment values.

#### Tests

- List responses contain no plaintext value.
- Non-manager cannot reveal or mutate workspace variables.
- Existing variables can be updated without reading their old value.
- API errors/log capture do not include values.

### 8.2 Gateway parser responses

1. Confirm parser endpoints do not log raw request payloads.
2. Add response headers preventing storage where appropriate.
3. Consider making token parsing local/client-side if no server-side processing is necessary.
4. Add redaction tests for access tokens, refresh tokens, bearer tokens, and client secrets.

## 9. Phase 5 — documentation, parity automation, and release management

### Documentation updates

1. Correct all repository-path references to `/sandbox/<local_path>`.
2. Include `queued` in every phase enum/documentation table.
3. Update `docs/specs/data-model.md`, `rest-api.md`, and `mcp-server.md` as each change ships.
4. Add a changelog entry for removed no-op options and redacted response fields.

### Automated parity checks

Implement a focused test that compares:

- `SessionCreate`/`SessionUpdate` fields against MCP create/update arguments.
- `SessionOut` fields against MCP `_fmt_session` fields, allowing an explicit documented omission list.
- `ScheduleEntryCreate`/`ScheduleEntryUpdate` fields against MCP schedule tool arguments.
- API phase constants against MCP terminal/non-terminal documentation tests.

The test should fail when a newly added REST field has neither MCP support nor an explicit reason for omission.

### Release sequencing

1. Ship P0 authorization/scope fixes first; they are security and integrity corrections.
2. Ship schedule provider persistence and MCP assignment parity next.
3. Remove `persist` in the same minor release with a migration note because it was previously ineffective.
4. Ship gateway enum validation with examples and preflight validation.
5. Stage environment-variable redaction as a documented breaking API change or under `/api/v2` if compatibility requirements prohibit changing v1.

## 10. Completion criteria

The remediation program is complete when:

- No MCP input or output field is silently ignored by REST.
- Every session reference is tenant-scoped and authorization-checked.
- Shared-resource ownership rules are implemented consistently.
- Schedule create/update have equivalent provider behavior.
- Gateway modes are a validated, documented enum.
- Repository path and phase documentation match runtime behavior.
- Plaintext environment values are no longer returned by default.
- REST/MCP parity tests and regression tests pass.
- The discrepancy section in `cross-layer-contracts.md` is updated from open findings to resolved decisions with links to the implementing changes.

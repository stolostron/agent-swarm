# Session Network Rules Component

## Purpose

Network rules expose OpenShell draft policy chunks and allow users to promote
or revoke selected rules for a session.

## Sources

- Templates: `swarmer/templates/sessions/_policy_rules.html`,
  `_policy_chunks.html`
- Router: `swarmer/routers/sessions.py` policy handlers
- Runtime: `swarmer/openshell_client.py`
- Model fields: `Session.custom_policies`, `Session.policy_chunks`

## Methods and tooling

- HTMX `/policy-chunks` polling every 30 seconds for active sandboxes.
- Persisted snapshot fallback for inactive/completed sessions.
- HTMX `/policy-rules-partial` refresh on `policyChanged`.
- OpenShell draft-chunk approval and undo APIs for live apply/revoke.
- JSON persistence for normalized custom rules.
- Authenticated versioned REST operations under
  `/api/v1/workspaces/{ws_id}/sessions/{sid}/policy-chunks`, `policy-rules`,
  and `policy-chunks/approve` for automation and declarative export.
- FastMCP tools `list_session_policy_chunks`, `list_session_policy_rules`, and
  `approve_session_policy_chunks` delegate to those REST operations.

The built-in Agent Swarm rule is generated at launch as `swarm_mcp` and allows
the MCP binary to reach the internal Swarmer REST service. Its endpoint is
derived from `POD_NAMESPACE`/`K8S_NAMESPACE` or the explicit internal URL.
This rule is distinct from the OpenShell provider profile: the provider
profile supplies endpoint enforcement, binary attribution, and the bearer
credential binding required for `AGENT_SWARM_API_TOKEN` injection.

For `enmaas` sessions, the `agent_api` policy block grants HTTPS egress only to
the workspace's validated EnMaaS gateway `(host, port)` and OpenCode metadata
endpoints (`opencode.ai`, `models.opencode.ai`, `models.dev`), omitting Google,
Vertex AI, and OpenAI inference endpoints. Draft-chunk auto-approval
(`_build_expected_hosts`) includes the workspace EnMaaS gateway hostname.

## Promotion algorithm

1. Load draft chunks live from OpenShell when active, otherwise use snapshot.
2. Determine already promoted chunks by rule name, binary-path coverage, and
   endpoint coverage. Every chunk binary path and endpoint must be covered by
   the same-name persisted rule; extra persisted coverage is allowed.
   Endpoints are normalized before matching, then compared as canonical JSON
   objects (key order is ignored).
3. Resolve selected chunks by stable ID; normalize endpoints, enforcement, and
   L7 access fields. Empty endpoints are rejected and never persisted.
4. Merge endpoint and binary coverage into existing same-name rules without
   discarding existing rule metadata, or append a new rule.
5. Persist the rule even if live application fails.
6. If active, approve available chunk IDs in OpenShell and report whether live
   application succeeded.

## Versioned REST contract

Every operation requires the standard bearer authentication and workspace ACL.
The session must belong to the workspace in the path; inaccessible workspace or
session IDs return 404.

### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/policy-chunks`

Returns `workspace_id`, `session_id`, `source` (`live` or `snapshot`), `active`,
and `chunks`. An active sandbox is queried through OpenShell; otherwise the
stored `Session.policy_chunks` snapshot is returned. Each chunk keeps its stable
`id`, native status, rule name, structured endpoints/destinations and binaries,
and adds `is_promoted`, `promotion_status` (`promoted`/`pending`), and `pending`.
Promotion status uses the same rule-name and binary-coverage test as the UI.

### `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/policy-rules`

Returns `rules` from `Session.custom_policies` in complete declarative form.
Endpoints retain host or allowed IP destinations, port/port range, protocol,
enforcement, and L7 access/permissions. Empty invalid endpoints are excluded;
L7 endpoints include `access: full` and `enforcement: enforce`. Binary paths
and rule metadata are preserved for backup, migration, query, and golden-rule
discovery workflows.

### `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/policy-chunks/approve`

JSON body accepts either `{"chunk_ids": ["stable-id"]}` or
`{"approve_all": true}`. `chunk_ids` are stable OpenShell IDs from the listing;
the two selection modes are mutually exclusive. `approve_all` evaluates all
currently pending chunks at request time. Unknown or already-promoted selected
IDs reject the batch. Invalid chunks (including chunks whose endpoints
normalize to empty) are returned in `rejected_chunks` and not persisted.

The response contains `result_code` (`APPROVED_LIVE`,
`APPROVED_PERSISTED_ONLY`, or `REJECTED`), `persisted_count`,
`persisted_chunk_ids`, `rejected_chunks` with reasons, live-attempt/application
booleans and counts, `detail`, and a `processed_at` timestamp. Rules are
committed before live approval, so gateway failure still leaves them effective
for the next launch. A partial or failed live approval returns
`APPROVED_PERSISTED_ONLY`, never a false success signal for live application.
This API approves only OpenShell-detected chunks; it does not accept
hand-authored network rules.

The MCP client mirrors the REST payloads without flattening IDs or statuses and
preserves API errors. Monitoring agents can compare draft endpoints against
session logs before approving explicit IDs and store the response as audit
context. Backup/migration agents can serialize the normalized rule list into a
manifest, retain pending chunk IDs separately, and re-evaluate detected drafts
after session restoration. Structured destinations and normalized endpoints
also allow cross-session URL/policy pattern analysis without parsing templates.

## Revoke algorithm

1. Remove the selected persisted rule by index.
2. If active, use stored chunk ID or rule-name lookup to undo the live draft.
3. Persist removal regardless of live revoke result; the rule will not apply on
   the next launch.
4. Emit policy change metadata for the toast/partial refresh.

## Acceptance checks

- Cross-session policy access is rejected.
- Empty/invalid endpoint hosts are not sent to OpenShell.
- Same rule name with different binaries merges rather than drops data.
- Live failures are visible but do not roll back a valid persisted decision.
- Agent Swarm launches have both a non-secret `swarm_mcp` network rule and a
  session-scoped provider; either missing policy is treated as a launch defect.

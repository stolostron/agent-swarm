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

The built-in Agent Swarm rule is generated at launch as `swarm_mcp` and allows
the MCP binary to reach the internal Swarmer REST service. Its endpoint is
derived from `POD_NAMESPACE`/`K8S_NAMESPACE` or the explicit internal URL.
This rule is distinct from the OpenShell provider profile: the provider
profile supplies endpoint enforcement, binary attribution, and the bearer
credential binding required for `AGENT_SWARM_API_TOKEN` injection.

## Promotion algorithm

1. Load draft chunks live from OpenShell when active, otherwise use snapshot.
2. Determine already promoted binaries by rule name.
3. Parse selected chunk JSON and normalize endpoints, enforcement, and L7
   access fields.
4. Merge binaries into existing same-name rules or append a new rule.
5. Persist the rule even if live application fails.
6. If active, approve available chunk IDs in OpenShell and report whether live
   application succeeded.

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

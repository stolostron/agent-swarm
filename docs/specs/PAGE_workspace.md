# Workspace Component

## Purpose

The workspace component is the tenancy and navigation boundary for sessions and
workspace-scoped configuration. It presents the workspace identity, namespace
status, description, session summary, and links to workspace capabilities.

## Sources

- Template: `swarmer/templates/workspaces/detail.html`
- Router: `swarmer/routers/workspaces.py`
- REST API: `swarmer/api/v1/workspaces.py`
- Models: `swarmer/models/workspace.py`, `swarmer/models/session.py`
- Related contracts: [REST API](rest-api.md), [Data Model](data-model.md),
  [Cross-Layer Contracts](cross-layer-contracts.md)

## Methods and tooling

- Server-rendered Jinja2 and PatternFly 6 dark-theme components.
- POST/redirect/GET for mutations and navigation.
- SQLAlchemy async queries for workspace and session state.
- Authorization through `require_auth` and workspace access checks.
- OpenShell/Kubernetes status is display-only at this boundary; Swarmer does
  not create per-session Kubernetes resources.

## Behavior

1. Load the workspace by ID and reject or redirect if it is unavailable or not
   accessible to the current user.
2. Load sessions belonging to the workspace for the summary table.
3. Display workspace name, namespace, description, namespace status, and links
   to sessions, prompts, AI credentials, MCP servers, and editing.
4. Offer creation of a new session under the same workspace.
5. Preserve the workspace ID in every child navigation and mutation.

## API references

- `GET /api/v1/workspaces`
- `POST /api/v1/workspaces`
- `GET /api/v1/workspaces/{ws_id}`
- `PUT /api/v1/workspaces/{ws_id}`
- `DELETE /api/v1/workspaces/{ws_id}`
- `GET /api/v1/workspaces/{ws_id}/gateway`
- `POST /api/v1/workspaces/{ws_id}/gateway`
- `DELETE /api/v1/workspaces/{ws_id}/gateway`

The REST representation is the automation contract; the HTML router is the
browser component contract.

## Invariants and failure handling

- Workspace ownership/access is checked before loading child resources.
- Destructive workspace deletion must respect active sessions and cleanup
  OpenShell-owned resources before deleting persisted state.
- Credentials and gateway configuration are never rendered as plaintext.
- Namespace labels must not be interpreted as proof that Swarmer owns a
  session pod, PVC, Service, or Route.

## Acceptance checks

- A user sees only accessible workspaces.
- Workspace links preserve the selected workspace.
- Session status and duration agree with persisted session state.
- Failed or unavailable namespace status is represented without exposing an
  internal exception or secret.

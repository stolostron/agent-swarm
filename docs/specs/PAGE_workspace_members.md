# Workspace Members Component

## Purpose

Workspace members define the database access-control list for a workspace.
This is separate from Kubernetes RBAC.

## Sources

- Template: `swarmer/templates/workspaces/members.html`
- Router: `swarmer/routers/workspaces.py`
- REST API: `swarmer/api/v1/workspaces.py`
- Model: `swarmer/models/workspace_member.py`
- Contract: [REST API](rest-api.md), [Data Model](data-model.md)

## Methods and tooling

- Authenticated HTML GET and POST/redirect/GET mutations.
- REST list/add/remove operations.
- Known-user lookup for member selection.
- Async SQLAlchemy transaction with workspace authorization.

## Algorithm

1. Confirm the caller can administer the workspace.
2. Load members and display identity plus role.
3. Validate the requested user identity and avoid duplicate membership.
4. Insert or delete the membership and commit atomically.
5. Redirect with a flash result.

## API references

- `GET /api/v1/workspaces/{ws_id}/members`
- `POST /api/v1/workspaces/{ws_id}/members`
- `DELETE /api/v1/workspaces/{ws_id}/members/{user_id}`
- `GET /api/v1/admins/users` for known-user lookup

## Invariants

- Membership is an application/database ACL, not a Kubernetes RoleBinding.
- The workspace owner/global administrator cannot be accidentally removed by a
  normal member operation.
- User IDs are authorization data and must not be logged unnecessarily.

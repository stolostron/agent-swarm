# Workspace Sessions Component

## Purpose

The workspace sessions component lists sessions, exposes creation, and gives a
near-real-time overview of lifecycle phase, tool, mode, start time, duration,
credential selection, and capacity.

## Sources

- Templates: `swarmer/templates/sessions/list.html`,
  `swarmer/templates/sessions/_list_rows.html`
- Router: `swarmer/routers/sessions.py`
- REST API: `swarmer/api/v1/sessions.py`
- Model: `swarmer/models/session.py`
- Contracts: [REST API](rest-api.md), [Data Model](data-model.md)

## Methods and tooling

- Initial server-rendered HTML.
- HTMX polling of `/workspaces/{ws_id}/sessions/rows` every three seconds.
- PatternFly table, labels, empty state, and capacity indicator.
- Launch opens the same confirmation dialog as session detail, in Prompt mode;
  cancel, Escape, and backdrop dismissal do not persist selection changes.
- Async SQLAlchemy queries for status and queue position.
- REST list/create endpoints for non-browser clients.

## Algorithm

1. Resolve the workspace and visible sessions.
2. Render an empty state when no sessions exist.
3. Otherwise render each session with canonical tool name, mode, phase,
   timestamps, duration, and credential label.
4. Refresh only table rows during polling so navigation and settings are not
   replaced.
5. Display active/maximum capacity and queued count when concurrency limiting
   is enabled.

`queued` is an active lifecycle state for protection from edits and duplicate
   launches. It is not equivalent to a running sandbox.

## API references

- `GET /api/v1/workspaces/{ws_id}/sessions`
- `POST /api/v1/workspaces/{ws_id}/sessions`
- `GET /api/v1/workspaces/{ws_id}/sessions/{sid}`
- `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/launch`
- `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/stop`

## Acceptance checks

- Polling updates phase, duration, queue, and status detail without a full page
  reload.
- Active sessions cannot be edited or deleted from the list.
- Empty state links to session creation.
- Provider warnings link to workspace AI-token configuration.

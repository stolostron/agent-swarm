# Session Overview Component

## Purpose

The session overview is the control surface for one persisted agent workload.
It combines identity, lifecycle actions, capacity, prompt summary, and tabs for
mode-specific output and configuration components.

## Sources

- Template: `swarmer/templates/sessions/detail.html`
- Router: `swarmer/routers/sessions.py`
- REST API: `swarmer/api/v1/sessions.py`
- Models: `swarmer/models/session.py`, `session_run.py`, `session_schedule.py`
- Runtime: `swarmer/openshell_client.py`, `swarmer/scheduler.py`

## Methods and tooling

- Server-rendered Jinja2 page with PatternFly tabs.
- HTMX partial polling and event-triggered refreshes.
- POST/redirect/GET lifecycle actions.
- REST session lifecycle endpoints.
- OpenShell Gateway and Supervisor APIs for sandbox lifecycle.
- Agent strategy interface (`opencode` and `shell`).

## Load algorithm

1. Resolve workspace and session, including PAT, repos, and selected prompt.
2. Resolve visible credentials and generate/store a one-time TUI token only for
   a running TUI session.
3. Resolve provider options, MCP servers, prompt sources, queue position,
   capacity, policy state, and up to 100 run records.
4. Render tabs conditionally by mode and available state.

## Lifecycle actions

- Launch `tui`, `server`, or `prompt` through the shared `_do_launch()` path.
- Stop always deletes the OpenShell sandbox and server service where present.
- Delete is blocked while active and performs runtime/credential cleanup first.
- Rename and configuration changes are blocked while active.

## API references

- `GET/PUT/DELETE /api/v1/workspaces/{ws_id}/sessions/{sid}`
- `POST .../{sid}/launch`
- `POST .../{sid}/stop`
- `POST .../{sid}/set-name`
- `POST .../{sid}/set-mode`
- `POST .../{sid}/set-provider`

## Invariants

- OpenShell is the sole session runtime.
- `queued` sessions have no sandbox yet and remain protected from duplicate
  launch/edit operations.
- Session state is persisted before the UI claims an operation succeeded.
- Errors become safe flash messages or partial responses, never raw secrets.

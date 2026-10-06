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
2. Resolve visible credentials, provider options, MCP servers, prompt sources,
   queue position, capacity, policy state, and up to 100 run records.
3. Generate and store a one-time TUI token only for a running TUI session.
4. Render tabs conditionally by mode and available state.

## Lifecycle actions

- Launch `tui`, `server`, or `prompt` through the shared `_do_launch()` path.
- Manual launches from each mode use the same accessible confirmation dialog.
  Prompt, provider, and mode selections are stored on the session and reused by
  all modes. Additional Instructions are run-only; they never update session or
  schedule defaults. The queued override column is nullable so an explicit
  blank remains distinct from no override, and is consumed when dispatch starts.
- Scheduled/event launches continue to compose schedule instructions with the
  session defaults when a schedule field is empty. Manual overrides do not enter
  that fallback path.
- Stop always deletes the OpenShell sandbox and server service where present.
- Delete is blocked while active and performs runtime/credential cleanup first.
- Agent Swarm provider credentials are refreshed for long-running TUI/server
  sessions and deleted after the sandbox is deleted.
- Rename and configuration changes are blocked while active.

## API references

- `GET/PUT/DELETE /api/v1/workspaces/{ws_id}/sessions/{sid}`
- `POST .../{sid}/launch`
- `POST .../{sid}/stop`
- `POST .../{sid}/set-name`
- `POST .../{sid}/set-mode`
- `POST .../{sid}/set-provider`
- `GET /workspaces/{ws_id}/sessions/{sid}/launch-dialog`

## Invariants

- OpenShell is the sole session runtime.
- `queued` sessions have no sandbox yet and remain protected from duplicate
  launch/edit operations.
- Session state is persisted before the UI claims an operation succeeded.
- Errors become safe flash messages or partial responses, never raw secrets.
- The persisted session model stores selected MCP IDs in
  `Session.mcp_server_ids`; the session-scoped OpenShell provider and token are
  runtime resources and are not persisted as plaintext model fields.

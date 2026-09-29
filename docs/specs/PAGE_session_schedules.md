# Session Schedules Component

## Purpose

Schedules launch a session prompt either on a cron timetable or in response to
an actionable GitHub event.

## Sources

- Templates: `swarmer/templates/sessions/_schedule_list.html`,
  `_schedule_items.html`
- Router: `swarmer/routers/sessions.py` schedule handlers
- REST API: `swarmer/api/v1/sessions.py` schedule endpoints
- Models: `swarmer/models/session_schedule.py`, `session_run.py`
- Runtime: scheduler/event trigger integration

## Methods and tooling

- HTMX list refresh driven by `HX-Trigger: scheduleListChanged`.
- `croniter` validates expressions and calculates the next UTC run.
- REST CRUD supports automation clients.
- Event schedules carry author scope, optional fixed authors, event context,
  and quiet-period delay.

## Create/edit algorithm

1. Verify workspace/session ownership.
2. Normalize trigger type to `cron` or `event`.
3. For cron, require a valid expression and calculate `cron_next_run`.
4. For event, store condition and author scope and clear cron fields.
5. Require a prompt belonging to the same workspace.
6. Validate provider override and delay range `0..1440` minutes.
7. Persist enabled state on create; toggle it only through the toggle action.
8. Emit `scheduleListChanged` and reload the list partial.

## API references

- `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/schedules`
- `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/schedules`
- `PUT .../{sid}/schedules/{sched_id}`
- `DELETE .../{sid}/schedules/{sched_id}`
- HTML schedule items/create/edit/delete/toggle endpoints

## Acceptance checks

- Invalid cron, missing prompt, cross-workspace prompt, and invalid delay are
  rejected without persistence.
- Editing a schedule does not unexpectedly disable it.
- Event context is included only when configured.
- Each scheduler invocation records its source in run history.

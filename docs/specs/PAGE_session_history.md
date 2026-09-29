# Session History Component

## Purpose

History provides an auditable list of completed and stopped runs, including
duration, phase, trigger source, event context, and expandable output.

## Sources

- Template: `swarmer/templates/sessions/_run_history.html`
- Router: `swarmer/routers/sessions.py:session_detail`
- REST API: `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/runs`
- Model: `swarmer/models/session_run.py`

## Methods and tooling

- Detail page queries the newest 100 runs ordered by completion time.
- PatternFly expandable table and client-side row toggles.
- Output/Raw Log toggles are client-side.
- ANSI output is rendered through the shared output filter.

## Algorithm

1. Create a run record when a launch/trigger begins.
2. Update phase, status detail, timestamps, output, and trigger metadata as the
   runtime progresses.
3. Render source labels for TUI, Chat, Prompt, schedules, and events.
4. Expose event repository, PR, SHA, condition, and title when available.
5. Expand only rows with output or event context.

## Acceptance checks

- History remains after current output is cleared.
- Failed and stopped runs retain status detail.
- Event context is bounded and does not expose secrets.
- The newest 100 records are deterministic and ordered descending.

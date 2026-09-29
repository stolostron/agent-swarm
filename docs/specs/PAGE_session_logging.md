# Session Logging Component

## Purpose

Logging exposes the current run's processed output and raw stream output while
preserving enough state to inspect completed work.

## Sources

- Templates: `swarmer/templates/sessions/_last_output.html`
- Router: `swarmer/routers/sessions.py` status/output handlers
- REST API: `GET .../{sid}/output`, `POST .../{sid}/clear-output`
- Models: `Session.last_output`, `Session.raw_output`, `SessionRun`

## Methods and tooling

- HTMX polls `/last-output` every five seconds during pending/running phases.
- ANSI output is converted for HTML display by the template filter.
- Client-side pills switch between processed `last_output` and raw
  `raw_output` without a round trip.
- OpenShell stream readers feed persisted output through the session runtime.

## Algorithm

1. Render the stream when raw output is available; otherwise render processed
   output.
2. During active execution, replace only the output partial on each poll.
3. On completion, show Output/Raw Log toggles only when both values exist and
   differ.
4. For server mode with no output, direct the user to Chat.
5. Clear output by blanking both persisted fields; history remains separate.

## API references

- `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/output`
- `POST /api/v1/workspaces/{ws_id}/sessions/{sid}/clear-output`
- HTML `/last-output` partial endpoint

## Invariants

- Raw logs may contain tool diagnostics but must not contain credentials.
- Current output polling stops after terminal lifecycle states.
- Clearing current output does not delete `SessionRun` history.

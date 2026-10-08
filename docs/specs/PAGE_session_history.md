# Session History Component

## Purpose

History provides an auditable list of completed and stopped runs, including
duration, phase, trigger source, immutable startup context, event context, and
expandable output.

## Sources

- Template: `swarmer/templates/sessions/_run_history.html`
- Router: `swarmer/routers/sessions.py:session_detail`
- REST API: `GET /api/v1/workspaces/{ws_id}/sessions/{sid}/runs`
- MCP history: `list_session_runs` followed by `get_session_run`
- Model: `swarmer/models/session_run.py`

## Methods and tooling

- Detail page queries the newest 100 runs ordered by completion time, then `id`
  for stable ordering when completion times tie.
- PatternFly expandable table and client-side row toggles.
- Every row expands, including runs with neither output nor event context.
- Each expanded run has independent Output, optional Raw Log, and Context views;
  Output is selected by default and Context is always available.
- Raw Log is available only when raw output is non-empty and differs from the
  processed output. Empty output is represented within its Output view.
- Context shows the immutable composed startup context, its selected prompt
  name, and identifiable event details. Older records without a captured
  context show the unavailable message instead of rebuilding from current
  settings.
- View controls expose their selected state through `aria-pressed`.
- ANSI output is rendered through the shared output filter.

## Algorithm

1. Capture prompt identity/content, additional instructions, composed startup
   context, mode, trigger, and separate event context before runtime setup.
2. Retain the snapshot through queue dispatch/restarts and copy it into the run
   record when the session reaches a terminal phase.
3. Render source labels for TUI, Chat, Prompt, schedules, and events.
4. Expose event repository, PR, SHA, condition, and title when available.
5. Show captured startup context even when output is empty; identify older runs
   without a captured snapshot as unavailable instead of rebuilding it from
   current settings. TUI/Chat conversation messages after startup are excluded.

## Acceptance checks

- History remains after current output is cleared.
- Failed and stopped runs retain status detail.
- Event context is bounded and does not expose secrets.
- The newest 100 records are deterministic and ordered by `completed_at` and
  `id`, descending.
- The existing REST `/runs` response remains unchanged for the session UI.
- `GET /runs/summaries?limit=N` returns at most 100 metadata-only summaries,
  ordered by completion time descending then run ID descending.
- `GET /runs/{run_id}` validates the run belongs to the requested session and
  returns complete stored output and context. The workspace dependency preserves
  workspace-scoped authorization; normal output secret redaction remains applied
  before stored run data is returned.
- MCP `list_session_runs` accepts a limit from 1 to 100 (default 20), and
  `get_session_run` returns the same full detail payload without truncation.

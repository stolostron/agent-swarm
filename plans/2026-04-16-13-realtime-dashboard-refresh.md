# Plan: Real-Time Dashboard Refresh & UI Fixes

**Date:** 2026-04-16  
**Plan number:** 13

---

## Goals

1. **Real-time refresh** across all list and detail views — changes (especially session status) update automatically without a full page reload.
2. **Fix: delete confirm card** (the "saved card") must appear **inside** the table row where the button was pressed, not replacing the button cell.
3. **Fix: [X] / Cancel button** on the inline delete confirm card must actually remove the card and restore the original button.
4. Apply all changes to: Workspaces list, Workspace detail (sessions table), Sessions list, and individual Session detail.

---

## Problem Analysis

### Real-Time Refresh (current state)
- The **Session detail** page already polls `_status_badge.html` and `_last_output.html` every 10 s via HTMX.
- The **Workspaces list**, **Workspace detail**, and **Sessions list** have **no polling** — they are static server-rendered snapshots. If a session goes from `pending` → `running` the user sees no update until they refresh manually.

### Delete Confirm Card Bug
- In `workspaces/list.html`, the Delete button uses `hx-target="#delete-btn-{{ ws.id }}"` and `hx-swap="outerHTML"`. This replaces the `<span>` wrapper in the **Actions column** with the entire `_delete_confirm.html` card — a wide multi-line box that appears in a narrow table cell. This is visually broken.
- The Cancel button in `_delete_confirm.html` uses a complex inline `onclick` that regenerates the original button HTML as a string with escaped quotes. This is fragile and can fail silently, leaving the confirm card stuck on screen.

---

## Implementation Plan

### 1. HTMX Partial Templates (new files)

| New template | Polls every | Stops when |
|---|---|---|
| `workspaces/_list_rows.html` | 10 s | never (always poll — workspaces don't have phases) |
| `workspaces/_sessions_table.html` | 5 s | no active sessions |
| `sessions/_list_rows.html` | 5 s | no sessions are pending/running |

All partials return only the `<tbody>` rows (plus a wrapper div carrying the `hx-*` polling attributes) so the `<table>` skeleton is never replaced.

### 2. New Backend Endpoints

| Route | Returns |
|---|---|
| `GET /workspaces/rows` | `workspaces/_list_rows.html` partial |
| `GET /workspaces/{ws_id}/sessions/rows` | `workspaces/_sessions_table.html` partial |
| `GET /workspaces/{ws_id}/sessions/list-rows` | `sessions/_list_rows.html` partial |

These endpoints are protected by `require_auth` and return `HTMLResponse`.

### 3. Workspace List — Real-Time Refresh

- Wrap `<tbody>` in a `<div id="ws-list-body">` that carries `hx-get`, `hx-trigger="every 10s"`, `hx-swap="outerHTML"`.
- The polling partial re-renders only the tbody content.

### 4. Workspace Detail — Session Status Refresh

- The sessions table in `workspaces/detail.html` gains a polling wrapper.
- The endpoint queries live session phases from the DB (no K8s calls — the session detail page's own status poll updates the DB phase already).
- Polling interval: 5 s. Stops when `all(s.phase not in ('pending', 'running') for s in sessions)`.

### 5. Sessions List — Status Auto-Update

- The sessions list `<tbody>` gains a polling wrapper.
- Same endpoint logic as above but for the full sessions list page (has more columns: Repos count).
- The status badge in each row reflects the current DB phase.

### 6. Fix Delete Confirm Card — Inline in the Row

**New approach:**
- Add a hidden `<tr id="delete-row-{{ ws.id }}" class="d-none">` with `colspan` spanning all columns immediately after each workspace row.
- The Delete button targets `#delete-row-{{ ws.id }}` and swaps `innerHTML`.
- `_delete_confirm.html` is rendered inside this hidden row's `<td>`.
- Cancel restores `d-none` on the row (simple `hx-on:click` or inline JS that adds the class back).

**Why:** The confirm card appears as a full-width row directly below the workspace it belongs to — no table layout breakage.

### 7. Fix [X] / Cancel — Properly Removes the Card

- Cancel button uses `hx-delete` or simple JS: `document.getElementById('delete-row-{{ ws.id }}').classList.add('d-none')` + clear innerHTML.
- Alternatively: add `hx-on:click="this.closest('tr').classList.add('d-none')"` on the Cancel button.
- This is simpler and more reliable than the current string-rebuilding approach.

---

## Files Changed

| File | Change |
|---|---|
| `swarmer/templates/workspaces/list.html` | Add polling wrapper; fix delete confirm to use hidden row |
| `swarmer/templates/workspaces/_list_rows.html` | **NEW** — HTMX partial for workspace list rows |
| `swarmer/templates/workspaces/_delete_confirm.html` | Fix Cancel button to close hidden row |
| `swarmer/templates/workspaces/detail.html` | Add polling wrapper for sessions table |
| `swarmer/templates/workspaces/_sessions_table.html` | **NEW** — HTMX partial for workspace-detail session rows |
| `swarmer/templates/sessions/list.html` | Add polling wrapper for session rows |
| `swarmer/templates/sessions/_list_rows.html` | **NEW** — HTMX partial for sessions list rows |
| `swarmer/routers/workspaces.py` | Add `/workspaces/rows` and `/workspaces/{ws_id}/sessions/rows` endpoints |
| `swarmer/routers/sessions.py` | Add `/workspaces/{ws_id}/sessions/list-rows` endpoint |

---

## Notes

- Polling intervals: **5 s** for session status (fast feedback), **10 s** for workspace list (less volatile).
- The session detail page already polls at 10 s; its interval is left unchanged.
- No JavaScript framework changes — all updates use existing HTMX on the page.
- The workspace `_delete_confirm.html` partial fix uses only Bootstrap classes and a minimal inline JS one-liner, no new dependencies.

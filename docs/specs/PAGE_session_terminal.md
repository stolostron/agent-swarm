# Session Terminal Component

## Purpose

The terminal component provides an interactive browser terminal for TUI-mode
sessions through an OpenShell PTY.

## Sources

- Template: `swarmer/templates/sessions/detail.html`
- WebSocket router: `swarmer/routers/tui_ws.py`
- Runtime: `swarmer/openshell_client.py`
- Browser tooling: xterm.js 5.3.0

## Methods and tooling

- Conditional xterm CSS/script loading.
- Lazy terminal initialization when the Terminal tab is first selected.
- Browser WebSocket endpoint `/ws/{ws_id}/sessions/{sid}/tui`.
- OpenShell `exec_interactive()` PTY stream.
- One-time UUID token stored in the authenticated HTTP session.

## Connection algorithm

1. Detail render creates a token only for a running TUI session.
2. Browser passes the token when opening the WebSocket.
3. Server validates workspace/session, confirms TUI mode, active phase, authentication, and
   token freshness, then consumes the token.
4. Bidirectionally forward terminal bytes between xterm.js and OpenShell.
5. Close the socket and PTY on stop, disconnect, invalid token, or runtime
   failure.

## Acceptance checks

- Tokens are single-use and cannot be replayed.
- Non-TUI and non-running sessions cannot open a PTY.
- Terminal output is not persisted as a credential-bearing log by accident.
- Resize/input/output behavior remains usable on mobile and desktop widths.

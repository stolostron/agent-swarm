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
- OpenCode TUI explicitly connects to the shared service at `127.0.0.1:4096`;
  it reads the service password from the sandbox's OpenCode service registration
  and supplies it as `OPENCODE_PASSWORD`. Other agent-tool TUI launch commands
  are unchanged.

## Connection algorithm

1. Detail render creates a token only for a running TUI session.
2. Browser passes the token when opening the WebSocket.
3. Server validates workspace/session, confirms TUI mode, active phase, authentication, and
   token freshness, then consumes the token.
4. Bidirectionally forward terminal bytes between xterm.js and OpenShell.
5. Close the socket and PTY on stop, disconnect, invalid token, or runtime
   failure.

For OpenCode, the PTY command bypasses automatic service discovery because the
registration's wildcard URL (`0.0.0.0:4096`) is not connectable in some
OpenShell environments. The command reads the per-session password inside the
sandbox, excludes loopback from HTTP proxies, and connects to the existing
service over `127.0.0.1`; it does not start a second service or change the
service's wildcard bind. A missing password fails the launch rather than
falling back to automatic service startup. The selected Chat conversation ID
is preserved when supplied.

## Acceptance checks

- Tokens are single-use and cannot be replayed.
- Non-TUI and non-running sessions cannot open a PTY.
- OpenCode Terminal attaches to the authenticated shared service and preserves
  the selected Chat conversation without starting a duplicate service.
- Terminal output is not persisted as a credential-bearing log by accident.
- Resize/input/output behavior remains usable on mobile and desktop widths.

# Session Chat Component

## Purpose

Chat exposes the OpenCode server UI for a running server-mode session without
requiring the browser to reach the internal OpenShell virtual hostname.

## Sources

- Template/link: `swarmer/templates/sessions/detail.html`
- Proxy: `swarmer/routers/chat_proxy.py`
- Runtime: `swarmer/openshell_client.py`

## Methods and tooling

- New-tab link is shown only when `mode=server` and `phase=running`.
- HTTP proxy forwards browser requests to the OpenShell exposed service.
- WebSocket route handles `/chat/{path}` upgrades.
- `_resolve_upstream()` maps the gateway-assigned virtual hostname to the
  configured gateway address and preserves the virtual `Host` for routing.
- HTML receives a `<base>` injection and absolute asset paths are rewritten to
  the Swarmer chat prefix.

## Algorithm

1. Verify authenticated workspace/session access and running server state.
2. Resolve the service URL and gateway upstream.
3. Forward HTTP/WebSocket traffic while preserving required routing headers.
4. Rewrite browser-relative navigation/assets when the upstream assumes `/`.
5. Return upstream failures as safe proxy errors.

## Acceptance checks

- Chat is unavailable for prompt/TUI/non-running sessions.
- The internal virtual hostname is never presented as a required DNS target.
- WebSocket chat connections survive proxy path rewriting.
- Proxy responses do not leak gateway credentials or internal configuration.

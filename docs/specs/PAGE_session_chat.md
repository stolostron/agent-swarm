# Session Chat Component

## Purpose

Chat exposes the OpenCode server UI for a running interactive session through
Swarmer. Browser requests never connect directly to an OpenShell virtual host or
publicly exposed sandbox service.

## Sources

- Template/link: `swarmer/templates/sessions/detail.html`
- Proxy: `swarmer/routers/chat_proxy.py`
- Runtime: `swarmer/openshell_client.py`

## Methods and tooling

- New-tab link is shown for OpenCode sessions when `mode in (tui, server)` and
  `phase=running`; shell TUI sessions do not have a Chat service.
- HTTP/SSE and WebSocket proxy routes carry requests through OpenShell
  `ForwardTcp` to the fixed `127.0.0.1:4096` OpenCode endpoint in the authorized
  sandbox.
- A temporary listener bound only to Swarmer pod loopback adapts the byte stream
  to HTTP and WebSocket clients. Its destination is server-derived; browser input
  cannot choose a sandbox, host, or port.
- Remote gateways require verified TLS and gateway authentication. Workspace
  gateways always require both — in-cluster hostname spellings (`.svc`,
  `cluster.local`) are not trust evidence; only the operator-configured
  default gateway may use the private-network exemption. The gRPC
  credentials stay server-side; the proxy sends per-session OpenCode Basic auth
  through the tunnel and removes browser Authorization and Cookie headers.
- Upstream WebSocket connections use a preconnected socket so the upstream
  cannot redirect the session or credential to another destination; proxied
  WebSocket handshake logging redacts credentials.
- Admission control (global tunnel cap plus per-user quota) runs before any
  gateway work and rejects overload with an explicit 503 / WebSocket 1013.
- Stopping or deleting a session aborts its live Chat tunnels; access
  revocation applies when a connection ends, bounded by the tunnel maximum
  lifetime.
- `service_url` is not used as a Chat upstream. It remains only for compatibility
  and cleanup of endpoints exposed by older versions.
- HTML receives a `<base>` injection and absolute asset paths are rewritten to
  the Swarmer chat prefix.

## Algorithm

1. Verify the authenticated browser session, same-origin mutation/WebSocket
   requests, workspace ACL, and active OpenCode sandbox.
2. Check admission (global tunnel cap plus per-user quota) before any gateway
   work; reject overload with an explicit 503 / WebSocket 1013.
3. Resolve the workspace gateway's authenticated OpenShell client (shared,
   cached per gateway) and enforce verified TLS plus gateway authentication —
   workspace gateways get no hostname-based exemption.
4. Allocate a bounded loopback-only relay and connect it to the server-selected
   sandbox and OpenCode port over `ForwardTcp`, opening the stream off the
   event loop so a blocking OIDC refresh cannot stall other requests.
5. Forward HTTP, SSE, or WebSocket traffic. Strip browser cookies and auth
   headers, apply per-session OpenCode Basic authentication, and suppress auth
   query parameters from legacy clients. WebSocket upstreams connect over a
   preconnected socket so redirects are impossible.
6. Close both sides on disconnect, timeout, session stop, or error; rewrite
   browser-relative navigation/assets and return safe proxy errors.

## Security boundary

The browser-facing Chat UI is still served under the Swarmer origin. The tunnel
prevents direct browser access to the sandbox and does not forward browser
credentials to OpenCode, but it is not browser-origin isolation: script content
served through Chat can act with the signed-in browser's Swarmer origin. A
separate Chat origin with scoped authentication would be needed to isolate a
compromised upstream UI from other same-origin dashboard actions.

## Remote gateway connectivity

The remote (hosted) gateway scenario is the reason this tunnel exists: a hosted
gateway's public ingress does not route the sandbox virtual-host service URLs
that `ExposeService` returns (observed as upstream 404s), so Chat rides the
same authenticated gRPC connection Swarmer already uses for sandbox lifecycle.
One Chat request travels this path:

1. The browser sends an authenticated HTTPS/WSS request to the Swarmer Chat
   route; workspace ACL and same-origin checks run first.
2. Admission control reserves a tunnel slot before any gateway work.
3. `resolve_gateway_config()` loads the workspace's `WorkspaceGateway` row —
   gateway URL, auth mode, encrypted OIDC/bearer/mTLS material — and
   `_require_secure_gateway()` rejects anything except certificate-verified
   TLS (`https`/`grpcs`) plus a configured authentication mode. A remote
   workspace gateway gets no private-network exemption.
4. `_gateway_client_for()` returns the shared `SandboxClient` for the
   (gateway URL, workspace) pair. The client's bearer interceptor calls the
   workspace's `OidcGatewayAuth.current_access_token`, which refreshes the
   access token against the issuer when stale and persists the refreshed
   bundle back to the encrypted `WorkspaceGateway` row. When gateway
   credentials change, `oidc_manager.invalidate()` swaps the callable and the
   cache replaces (and closes) the old client on the next request.
5. The proxy opens the `ForwardTcp` stream on a worker thread under a bounded
   timeout — the SDK's bearer interceptor may block on an OIDC discovery or
   token refresh — and a reaper cancels streams whose caller timed out or was
   cancelled.
6. Request bytes travel inside the TLS-verified, authenticated gRPC channel to
   the remote gateway, which relays them to the sandbox's `127.0.0.1:4096`
   OpenCode endpoint. OpenCode sees only the per-session Basic credential;
   the gateway credential never enters the sandbox or the browser.
7. The response returns over the same stream; SSE and WebSocket connections
   hold their stream open under the tunnel lifetime and idle-read bounds.

Failure modes in the remote scenario, all fail-closed:

- Missing or unverifiable gateway TLS, or no configured authentication →
  HTTP 503 / WebSocket close 1011 before any tunnel is opened.
- Slow or failed OIDC refresh → bounded by the stream-open timeout; Chat
  fails closed rather than stalling other requests.
- Gateway overload (global or per-user tunnel quota) → explicit 503 /
  WebSocket 1013 "try again later".
- Credential rotation → the shared client is replaced on the next request;
  in-flight tunnels on the old client fail and the browser reconnects.

## Acceptance checks

- Chat is unavailable for prompt-mode and non-running sessions.
- No public `ExposeService` endpoint is created for new interactive sessions.
- HTTP, SSE, and WebSocket Chat connections work through the gRPC tunnel.
- Browser cookies, browser Authorization, gateway credentials, and internal
  gateway details do not leak to OpenCode or proxy logs — including
  WebSocket handshake debug logging.
- Tunnel capacity, stream queues, request/response body buffering, and maximum
  connection lifetime are bounded, with per-user admission and explicit
  overload responses; cancellation and failure paths release every resource.
- Workspace gateways never use hostname-based TLS exemptions.
- Upstream WebSocket connections cannot be redirected off the tunnel.
- Stopping or deleting a session aborts its live Chat tunnels.
- Transport encryption boundaries are explicit: browser-to-ingress HTTPS/WSS;
  verified TLS plus authentication from Swarmer to a remote gateway; application
  HTTP after the gateway terminates the tunnel to sandbox loopback. OpenShift
  edge termination leaves ingress-to-pod traffic HTTP unless separately secured.

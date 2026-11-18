"""Authenticated HTTP/SSE/WebSocket proxy to sandbox-local OpenCode.

The proxy carries requests through OpenShell ``ForwardTcp`` to a fixed
loopback target selected server-side. It never connects to a public
``ExposeService`` URL. HTML asset paths are rewritten to the Swarmer Chat
prefix so the browser remains on the authenticated dashboard origin.
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import queue
import re
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import websockets
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.database import get_db
from swarmer.deps import require_auth, require_websocket_auth
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

if TYPE_CHECKING:
    from swarmer.openshell_client import GatewayConfig

router = APIRouter()
log = logging.getLogger(__name__)
# Chat proxy emits one log line per proxied asset (JS chunks, CSS, API calls).
# Pin this logger to WARNING so it stays quiet at the default INFO level.
# Set LOG_LEVEL=DEBUG in the environment to enable full per-request tracing.
log.setLevel(logging.WARNING)


class _HttpxQueryRedactionFilter(logging.Filter):
    """Keep HTTPX request logs from recording query-string credentials/data."""

    _url_with_query = re.compile(r"(https?://[^\s?'\"]+)\?[^\s'\"]*")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._url_with_query.sub(r"\1?<redacted>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(item, _HttpxQueryRedactionFilter) for item in _httpx_logger.filters):
    _httpx_logger.addFilter(_HttpxQueryRedactionFilter())


class _WebsocketSecretRedactionFilter(logging.Filter):
    """Redact credentials the websockets library logs from handshake headers.

    ``websockets`` logs every outgoing handshake header at DEBUG level
    (``"> %s: %s"``, key/value as log args), which would otherwise include
    the per-session OpenCode Basic credential.
    """

    _SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if args and len(args) == 2:
            try:
                key = str(args[0]).lower()
            except Exception:  # noqa: BLE001 - malformed args must not break logging
                return True
            if key in self._SENSITIVE_HEADERS:
                record.args = (args[0], "<redacted>")
        return True


# Dedicated logger for proxied WebSocket connections so handshake debug
# output never bypasses the redaction filter.
_ws_tunnel_logger = logging.getLogger("swarmer.chat.websocket")
if not any(isinstance(item, _WebsocketSecretRedactionFilter) for item in _ws_tunnel_logger.filters):
    _ws_tunnel_logger.addFilter(_WebsocketSecretRedactionFilter())

_CHAT_TARGET_HOST = "127.0.0.1"
# Tunnel streams multiplex over one shared gRPC channel per gateway, so the
# global cap counts streams, not connections. Per-user admission stops one
# user from monopolizing Chat across every workspace.
_CHAT_MAX_TUNNELS = 96
_CHAT_MAX_TUNNELS_PER_USER = 24
_CHAT_TUNNEL_QUEUE_CHUNKS = 8
_CHAT_TUNNEL_CHUNK_SIZE = 64 * 1024
_CHAT_MAX_BUFFERED_BODY = 32 * 1024 * 1024
_CHAT_TUNNEL_MAX_LIFETIME = 2 * 60 * 60
# Opening a stream invokes the SDK's bearer interceptor, which may block on
# an OIDC refresh; bound how long the event loop waits for it.
_CHAT_STREAM_OPEN_TIMEOUT = 30.0
# Idle bound for long-lived SSE reads (OpenShift routes cut connections at
# 600s anyway; this bounds direct-access deployments).
_CHAT_SSE_READ_TIMEOUT = 900.0
_CHAT_CONNECT_TIMEOUT = 10.0

_chat_tunnel_count = 0
_chat_user_tunnels: dict[str, int] = {}
_session_tunnels: dict[int, set["_ChatTunnelHandle"]] = {}
_session_run_fences: dict[int, "_SessionRunFence"] = {}
_gateway_clients: dict[tuple[int | str, ...], tuple[Any, Any]] = {}
_gateway_clients_lock = asyncio.Lock()


class ChatTunnelCapacityError(RuntimeError):
    """Chat admission control rejected a new tunnel (overload protection)."""


class ChatBodyTooLargeError(ValueError):
    """A Chat upload exceeded the configured request-body limit."""


class _SessionRunFence:
    """Identity token for one interactive run; requests retain it while in flight."""

    __slots__ = ("revoked",)

    def __init__(self) -> None:
        self.revoked = False


class _ChatAdmissionReservation:
    """Own a reserved slot until a tunnel context takes responsibility for it."""

    __slots__ = ("user_key", "owned")

    def __init__(self, user_key: str | None) -> None:
        self.user_key = user_key
        self.owned = True

    def transfer(self) -> None:
        self.owned = False

    def release(self) -> None:
        if self.owned:
            self.owned = False
            _release_chat_tunnel(self.user_key)


class _ChatTunnelHandle:
    """Live handle for one open Chat tunnel, used for stop/delete revocation."""

    __slots__ = ("_loop", "close_event", "stopped", "writer")

    def __init__(self) -> None:
        self.close_event = asyncio.Event()
        self.stopped = threading.Event()
        self.writer: asyncio.StreamWriter | None = None
        self._loop = asyncio.get_running_loop()

    def attach(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer

    def request_close(self) -> None:
        """Close the tunnel; safe to call from the event loop or any thread."""
        self.close_event.set()
        self.stopped.set()
        writer = self.writer
        if writer is not None:
            self._loop.call_soon_threadsafe(_abort_writer, writer)


def _abort_writer(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.transport.abort()


async def close_session_chat_tunnels(session_id: int) -> int:
    """Close every open Chat tunnel for a session.

    Called from session stop/delete so proxied Chat connections cannot
    outlive their sandbox. Access revocation (membership removal) applies
    when the connection ends; the tunnel maximum lifetime bounds that delay.
    The active fence is removed after being revoked; in-flight requests retain
    the object itself and cannot mistake a later run for their original one.
    """
    fence = _session_run_fences.pop(session_id, None)
    if fence is not None:
        fence.revoked = True
    handles = _session_tunnels.pop(session_id, None)
    if not handles:
        return 0
    for handle in list(handles):
        handle.request_close()
    return len(handles)


def activate_session_chat_tunnels(session_id: int) -> _SessionRunFence:
    """Open a fresh run fence when an interactive session becomes ready."""
    fence = _SessionRunFence()
    _session_run_fences[session_id] = fence
    return fence


def _chat_admission_error(user_key: str | None) -> str | None:
    """Return the overload reason when no tunnel slot is available."""
    if _chat_tunnel_count >= _CHAT_MAX_TUNNELS:
        return "Chat tunnel capacity is full"
    if user_key and _chat_user_tunnels.get(user_key, 0) >= _CHAT_MAX_TUNNELS_PER_USER:
        return "Chat tunnel limit reached for this user"
    return None


def _admit_chat_tunnel(user_key: str | None) -> None:
    """Reserve one tunnel slot. Runs on the event loop with no awaits, so the
    check and increment are atomic against other request tasks."""
    error = _chat_admission_error(user_key)
    if error:
        raise ChatTunnelCapacityError(error)
    global _chat_tunnel_count
    _chat_tunnel_count += 1
    if user_key:
        _chat_user_tunnels[user_key] = _chat_user_tunnels.get(user_key, 0) + 1


def _release_chat_tunnel(user_key: str | None) -> None:
    global _chat_tunnel_count
    _chat_tunnel_count = max(0, _chat_tunnel_count - 1)
    if user_key:
        remaining = _chat_user_tunnels.get(user_key, 1) - 1
        if remaining > 0:
            _chat_user_tunnels[user_key] = remaining
        else:
            _chat_user_tunnels.pop(user_key, None)

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})

# ── OpenShell TCP tunnel ──────────────────────────────────────────────────────

def _gateway_is_private(gateway_config) -> bool:
    """Return whether the gateway may use the private-network exemption.

    Only the deployment's default gateway — configured by the operator via
    ``OPENSHELL_GATEWAY_URL`` — may use the exemption, because that
    environment variable is the administrator-controlled trust boundary for
    in-cluster gRPC. Workspace-configured gateways are user-managed and must
    always use certificate-verified TLS with gateway authentication,
    regardless of how their hostname is spelled: a ``.svc`` or
    ``cluster.local`` name is not evidence of a private deployment.
    """
    if gateway_config.workspace_id is not None:
        return False
    raw_url = (gateway_config.gateway_url or "").strip()
    if not raw_url:
        return True
    parsed = urlparse(raw_url if "://" in raw_url else f"//{raw_url}")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "openshell-gateway", "openshell-gateway.default"}:
        return True
    if host.endswith((".svc", ".cluster.local")):
        return True
    try:
        address = ip_address(host)
        return address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        return False


def _require_secure_gateway(gateway_config) -> None:
    """Fail closed for internet-facing gateways without authenticated TLS."""
    if _gateway_is_private(gateway_config):
        return

    raw_url = (gateway_config.gateway_url or "").strip()
    parsed = urlparse(raw_url if "://" in raw_url else f"//{raw_url}")
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "grpcs"} or not gateway_config.tls_verify:
        raise ValueError("remote Chat gateways require verified TLS")

    authenticated = (
        gateway_config.auth_mode == "oidc" and gateway_config.bearer_callable
    ) or (
        gateway_config.auth_mode == "bearer" and gateway_config.bearer_token
    ) or (
        gateway_config.auth_mode == "mtls"
        and gateway_config.tls_cert
        and gateway_config.tls_key
    )
    if not authenticated:
        raise ValueError("remote Chat gateways require gateway authentication")


async def _queue_put(queue_: queue.Queue, item: bytes | None, stopped: threading.Event) -> None:
    """Put a bounded TCP chunk into the synchronous gRPC request queue."""
    while not stopped.is_set():
        try:
            queue_.put_nowait(item)
            return
        except queue.Full:
            await asyncio.sleep(0.005)


async def _open_forward_stream(
    sandbox_name: str,
    target_port: int,
    client: Any,
) -> tuple[Iterator[Any], queue.Queue[bytes | None]]:
    """Open a ForwardTcp stream without blocking the event loop.

    Creating the stream invokes the OpenShell SDK's bearer interceptor
    synchronously; for OIDC gateways that interceptor may perform a blocking
    discovery/token refresh against the identity provider. Running it on the
    event loop would stall every other request, so the open happens on a
    worker thread under a bounded timeout. Requires the OpenShell SDK's
    bearer interceptor to run off-loop.
    """
    from swarmer import openshell_client

    holder: dict[str, Any] = {}
    ownership_lock = threading.Lock()
    abandoned = False

    def _dispose(stream, input_q) -> None:
        if input_q is not None:
            with contextlib.suppress(queue.Full):
                input_q.put_nowait(None)
        cancel = getattr(stream, "cancel", None)
        if cancel is not None:
            with contextlib.suppress(Exception):
                cancel()

    def _open():
        stream, input_q = openshell_client.forward_tcp(
            sandbox_name, _CHAT_TARGET_HOST, target_port, client=client
        )
        nonlocal abandoned
        with ownership_lock:
            if abandoned:
                dispose = True
            else:
                holder["stream"] = stream
                holder["queue"] = input_q
                dispose = False
        if dispose:
            _dispose(stream, input_q)
        return stream, input_q

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_open), timeout=_CHAT_STREAM_OPEN_TIMEOUT
        )
        with ownership_lock:
            abandoned = True  # caller now owns the returned pair
        return result
    except (TimeoutError, asyncio.CancelledError):
        with ownership_lock:
            abandoned = True
            stream = holder.get("stream")
            input_q = holder.get("queue")
        if stream is not None or input_q is not None:
            _dispose(stream, input_q)
        # If the worker has not created its RPC yet, it observes `abandoned`
        # when it returns and remains responsible for disposing the late result.
        raise


async def _tunnel_socket(local_base: str) -> socket.socket:
    """Connect a raw preconnected socket to the tunnel's loopback listener.

    Handing ``websockets.connect`` a preconnected socket makes upstream
    redirects impossible: the library refuses to follow any redirect when it
    did not create the connection itself, so a hostile upstream cannot move
    the session (or the OpenCode credential) to another destination.
    """
    parsed = urlparse(local_base)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.sock_connect(sock, (parsed.hostname, parsed.port)),
            timeout=_CHAT_CONNECT_TIMEOUT,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            sock.close()
        raise
    return sock


@asynccontextmanager
async def _open_sandbox_tunnel(
    sandbox_name: str,
    target_port: int,
    client: Any,
    session_id: int | None = None,
    user_key: str | None = None,
    admission_reserved: bool = False,
    run_fence: _SessionRunFence | None = None,
    admission: _ChatAdmissionReservation | None = None,
) -> AsyncIterator[str]:
    """Expose one bounded loopback listener backed by OpenShell ForwardTcp.

    The listener exists only inside the Swarmer pod and is bound to IPv4
    loopback. It lets the existing HTTP and WebSocket clients speak to a raw
    TCP stream without exposing a gateway virtual-host service URL. Admission
    (global + per-user quotas) is reserved for the tunnel's full lifetime,
    the tunnel is registered for stop/delete revocation while open, and every
    exit path — including cancellation and partial initialization — closes
    the listener, the gRPC stream, its reader thread, and the admission slot.
    """
    if session_id is not None:
        if run_fence is not None and (
            run_fence.revoked or _session_run_fences.get(session_id) is not run_fence
        ):
            raise RuntimeError("Chat session run was revoked")
    if not admission_reserved and admission is None:
        _admit_chat_tunnel(user_key)
    owns_admission = admission is None
    handle = _ChatTunnelHandle()
    if session_id is not None:
        _session_tunnels.setdefault(session_id, set()).add(handle)
    if admission is not None:
        admission.transfer()
        owns_admission = True

    stopped = handle.stopped
    bridge_task: asyncio.Task | None = None
    accepted = False

    async def _bridge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if handle.close_event.is_set():
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        try:
            response_stream, input_q = await _open_forward_stream(
                sandbox_name, target_port, client
            )
        except Exception as exc:  # noqa: BLE001 - report failure by closing the relay socket
            log.warning("Chat TCP tunnel open failed for sandbox (%s)", type(exc).__name__)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        downstream: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_CHAT_TUNNEL_QUEUE_CHUNKS
        )

        def _read_gateway() -> None:
            try:
                for frame in response_stream:
                    if stopped.is_set():
                        break
                    if frame.WhichOneof("payload") != "data" or not frame.data:
                        continue
                    future = asyncio.run_coroutine_threadsafe(downstream.put(frame.data), handle._loop)
                    while not stopped.is_set():
                        try:
                            future.result(timeout=0.2)
                            break
                        except TimeoutError:
                            continue
                    if stopped.is_set() and not future.done():
                        future.cancel()
            except Exception as exc:  # noqa: BLE001 - gRPC SDK stream errors are implementation-specific
                if not stopped.is_set():
                    log.warning("Chat TCP tunnel read failed for sandbox (%s)", type(exc).__name__)
            finally:
                if not stopped.is_set():
                    finish_future = asyncio.run_coroutine_threadsafe(downstream.put(None), handle._loop)
                    while not stopped.is_set():
                        try:
                            finish_future.result(timeout=0.2)
                            break
                        except TimeoutError:
                            continue
                        except Exception:  # noqa: BLE001 - a cancelled loop future ends this relay
                            break
                    if stopped.is_set() and not finish_future.done():
                        finish_future.cancel()

        reader_thread = threading.Thread(target=_read_gateway, daemon=True)
        reader_thread.start()

        async def _socket_to_gateway() -> None:
            while data := await reader.read(_CHAT_TUNNEL_CHUNK_SIZE):
                await _queue_put(input_q, data, stopped)

        async def _gateway_to_socket() -> None:
            while True:
                data = await downstream.get()
                if data is None:
                    return
                writer.write(data)
                await writer.drain()

        async def _close_watcher() -> None:
            await handle.close_event.wait()
            _abort_writer(writer)

        socket_task = asyncio.create_task(_socket_to_gateway())
        gateway_task = asyncio.create_task(_gateway_to_socket())
        close_task = asyncio.create_task(_close_watcher())
        try:
            done, pending = await asyncio.wait(
                {socket_task, gateway_task, close_task},
                timeout=_CHAT_TUNNEL_MAX_LIFETIME,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                log.warning("Chat TCP tunnel reached its maximum lifetime")
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            for task in (socket_task, gateway_task, close_task):
                task.cancel()
            await asyncio.gather(socket_task, gateway_task, close_task, return_exceptions=True)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize tunnel/socket failures at the relay boundary
            for task in (socket_task, gateway_task, close_task):
                task.cancel()
            await asyncio.gather(socket_task, gateway_task, close_task, return_exceptions=True)
            if not stopped.is_set():
                log.warning("Chat TCP tunnel failed for sandbox (%s)", type(exc).__name__)
        finally:
            stopped.set()
            # Drain queued chunks so the sentinel always fits, then half-close
            # the request stream; this unblocks the SDK's consumption thread
            # even when it is parked on queue.get().
            while True:
                try:
                    input_q.get_nowait()
                except queue.Empty:
                    break
            with contextlib.suppress(queue.Full):
                input_q.put_nowait(None)
            cancel = getattr(response_stream, "cancel", None)
            if cancel is not None:
                with contextlib.suppress(Exception):
                    cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.to_thread(reader_thread.join, 1.0)
            writer.close()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await writer.wait_closed()

    async def _accept_one(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accepted
        nonlocal bridge_task
        peer = writer.get_extra_info("peername")
        if accepted or not peer or peer[0] != "127.0.0.1":
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        accepted = True
        bridge_task = asyncio.current_task()
        handle.attach(writer)
        await _bridge(reader, writer)

    server = None
    try:
        server = await asyncio.start_server(_accept_one, host="127.0.0.1", port=0, limit=64 * 1024)
        port = server.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        try:
            if session_id is not None:
                handles = _session_tunnels.get(session_id)
                if handles is not None:
                    handles.discard(handle)
                    if not handles:
                        _session_tunnels.pop(session_id, None)
            if server is not None:
                server.close()
            try:
                if bridge_task is not None:
                    bridge_task.cancel()
                    if handle.writer is not None:
                        _abort_writer(handle.writer)
                    with contextlib.suppress(asyncio.CancelledError):
                        await bridge_task
            finally:
                if server is not None:
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await server.wait_closed()
        finally:
            if owns_admission:
                _release_chat_tunnel(user_key)


# ── HTML rewriting ───────────────────────────────────────────────────────────

def _proxy_intercept_script(prefix: str) -> str:
    """Return script content that keeps same-origin OpenCode calls proxied."""
    return f"""(function() {{
  var _prefix = {prefix!r};

  function rewrite(url) {{
    if (!url) return url;
    var value = String(url);
    if (value.indexOf(_prefix) === 0) return value;
    if (/^(https?:|wss?:)/i.test(value)) {{
      try {{
        var parsed = new URL(value, location.href);
        if (parsed.origin !== location.origin) return value;
        // Preserve already-proxied absolute URLs exactly. This is critical
        // for POST Requests with streamed bodies: rebuilding them can throw.
        if (parsed.pathname.indexOf(_prefix) === 0) return value;
        parsed.pathname = _prefix + (parsed.pathname.charAt(0) === '/'
          ? parsed.pathname
          : '/' + parsed.pathname);
        return parsed.href;
      }} catch (e) {{ return value; }}
    }}
    if (value.charAt(0) === '/') return _prefix + value;
    return value;
  }}

  var originalFetch = window.fetch.bind(window);
  window.fetch = function(input, init) {{
    if (typeof input === 'string') input = rewrite(input);
    else if (input instanceof Request) {{
      var rewritten = rewrite(input.url);
      if (rewritten !== input.url) input = new Request(rewritten, input);
    }} else if (input && typeof input === 'object' && 'href' in input) {{
      var rewrittenUrl = rewrite(input.href);
      if (rewrittenUrl !== input.href)
        input = new URL(rewrittenUrl, location.href);
    }}
    return originalFetch(input, init);
  }};

  var OriginalEventSource = window.EventSource;
  window.EventSource = function(url, init) {{
    return new OriginalEventSource(rewrite(url), init);
  }};
  window.EventSource.prototype = OriginalEventSource.prototype;

  var OriginalWebSocket = window.WebSocket;
  window.WebSocket = function(url, protocols) {{
    var rewritten = String(url);
    var wsPrefix = _prefix.replace(/^https:/, 'wss:').replace(/^http:/, 'ws:');
    if (/^(https?:|wss?:)/i.test(rewritten)) {{
      try {{
        var parsedWs = new URL(rewritten, location.href);
        var sameHost = parsedWs.host === location.host;
        var compatibleProtocol = (location.protocol === 'https:' && parsedWs.protocol === 'wss:')
          || (location.protocol === 'http:' && parsedWs.protocol === 'ws:');
        if (sameHost && compatibleProtocol) {{
          rewritten = parsedWs.pathname + parsedWs.search;
          if (rewritten.indexOf(_prefix) === 0)
            rewritten = wsPrefix + rewritten;
          else
            rewritten = wsPrefix + _prefix + rewritten;
        }}
      }} catch (e) {{}}
    }} else if (rewritten.charAt(0) === '/') {{
      rewritten = wsPrefix + rewritten;
    }}
    return protocols === undefined
      ? new OriginalWebSocket(rewritten)
      : new OriginalWebSocket(rewritten, protocols);
  }};
  window.WebSocket.prototype = OriginalWebSocket.prototype;
  window.WebSocket.CONNECTING = OriginalWebSocket.CONNECTING;
  window.WebSocket.OPEN = OriginalWebSocket.OPEN;
  window.WebSocket.CLOSING = OriginalWebSocket.CLOSING;
  window.WebSocket.CLOSED = OriginalWebSocket.CLOSED;
}})();"""


def _proxy_compat_script(prefix: str) -> str:
    """Return script content for OpenCode storage and history compatibility."""
    return (
        'try{'
        f'var __swProxy="{prefix}";'
        'localStorage.setItem("opencode.settings.dat:defaultServerUrl",location.origin+__swProxy);'
        # OpenCode V2 registers a service worker at /sw.js, which would escape
        # the proxy prefix and cache Swarmer-origin responses. Neutralize
        # registration with a never-settling promise (registration is
        # fire-and-forget in the V2 bundle).
        'if(navigator.serviceWorker){navigator.serviceWorker.register=function(){return new Promise(function(){})}}'
        'var __origPush=history.pushState.bind(history);'
        'var __origReplace=history.replaceState.bind(history);'
        'function __swPrefixUrl(u){'
        'if(typeof u!=="string")return u;'
        'if(u.startsWith(__swProxy))return u;'
        'if(u.startsWith("/"))return __swProxy+u;'
        'return u;'
        '}'
        'history.pushState=function(s,t,u){return __origPush(s,t,u!=null?__swPrefixUrl(u):u);};'
        'history.replaceState=function(s,t,u){return __origReplace(s,t,u!=null?__swPrefixUrl(u):u);};'
        '}catch(e){}'
    )


def _script_tag(script: str) -> str:
    return f"<script>{script}</script>"


def _script_hash(script: str) -> str:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


def _rewrite_content_security_policy(csp: str, scripts: list[str]) -> str:
    """Allow the exact proxy scripts without weakening the upstream CSP."""
    hashes = " ".join(_script_hash(script) for script in scripts)
    match = re.search(r"(^|;)\s*script-src(?P<value>[^;]*)", csp, flags=re.IGNORECASE)
    if match:
        value = match.group("value")
        if hashes:
            value = f"{value} {hashes}"
        return csp[:match.start("value")] + value + csp[match.end("value"):]

    # script-src falls back to default-src when absent. Insert it before the
    # first directive so the injected scripts are explicitly authorized.
    directive = f"script-src {hashes}"
    return f"{directive}; {csp}" if csp else directive


def _rewrite_html(content: bytes, prefix: str) -> bytes:
    """Rewrite absolute asset paths in HTML so they resolve through the proxy.

    Also injects a script that sets the OpenCode localStorage key
    ``opencode.settings.dat:defaultServerUrl`` to the proxy prefix so that
    OpenCode's JS uses the Swarmer proxy as its API base instead of falling
    back to ``location.origin`` (which would point at the Swarmer host rather
    than the OpenCode API).
    """
    text = content.decode("utf-8", errors="replace")

    # Rewrite absolute asset paths first, before injecting our own tags
    # (so the injected tags don't get double-rewritten).
    for old, new in [
        ('src="/',    f'src="{prefix}/'),
        ("src='/",    f"src='{prefix}/"),
        ('href="/',   f'href="{prefix}/'),
        ("href='/",   f"href='{prefix}/"),
        ('action="/', f'action="{prefix}/'),
    ]:
        text = text.replace(old, new)

    # Inject <base> and proxy-compat scripts so OpenCode's SPA and API calls
    # work correctly through the Swarmer proxy prefix.
    #
    # 1. <base href> makes all relative URLs (CSS, JS chunks) resolve via proxy.
    # 2. defaultServerUrl in localStorage: OpenCode reads this key as its
    #    preferred API base before falling back to location.origin, so all API
    #    calls go through the proxy rather than hitting the Swarmer root.
    # 3. history.pushState/replaceState interceptors: when OpenCode's SPA router
    #    navigates to "/Lw/session/...", the interceptor prepends the proxy
    #    prefix so the URL stays within /workspaces/{ws_id}/sessions/{sid}/chat/.
    #    Without this, a pushState("/Lw/...") would leave the proxy entirely and
    #    produce a Swarmer 404 on the next page interaction.
    base_tag = f'<base href="{prefix}/">'
    compat_script = _proxy_compat_script(prefix)
    proxy_compat_script = _script_tag(compat_script)
    inject = _script_tag(_proxy_intercept_script(prefix)) + base_tag + proxy_compat_script
    if "<head>" in text:
        text = text.replace("<head>", f"<head>{inject}", 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", f"<HEAD>{inject}", 1)

    return text.encode("utf-8")


def _rewrite_js(content: bytes, prefix: str) -> bytes:
    """Rewrite Vite's base-path function in JS so dynamic imports resolve through the proxy.

    Vite's __vitePreload helper uses a base-path function of the form
    ``function(t){return"/"+t}`` to construct absolute asset URLs for
    dynamically imported chunks (CSS, JS, workers).  When OpenCode's UI is
    served through the Swarmer proxy, these absolute paths resolve against the
    Swarmer host root rather than the ``/workspaces/{ws_id}/sessions/{sid}/chat/``
    proxy prefix, causing 404s.  The ``<base>`` tag injected by ``_rewrite_html``
    does not help because Vite sets ``link.href`` directly to the already-absolute
    path.  Replacing the base-path function makes all dynamic imports go through
    the correct proxy prefix.

    Also rewrites the OpenCode server-URL fallback so it returns the proxy
    prefix rather than bare ``location.origin``, keeping the server list clean.
    """
    text = content.decode("utf-8", errors="replace")

    # Vite/Rolldown preload base-path function. V1 builds use double quotes
    # (function(t){return"/"+t}); the V2 bundle uses a backtick template
    # literal (function(e){return`/`+e}). Match both so dynamic imports and
    # lazy chunks resolve through the proxy prefix.
    text = re.sub(
        r'function\((\w)\)\{return["\'`]/["\'`]\+\1\}',
        lambda m: f'function({m.group(1)}){{return"{prefix}/"+{m.group(1)}}}',
        text,
    )

    # OpenCode V2's router reads window.location.pathname+window.location.search
    # directly and does not configure a router base. Strip the Swarmer proxy
    # prefix so its routes (/, /new-session, /:dir/session/:id) still match.
    route_prefix = json.dumps(prefix)
    route_iife = (
        "(function(){var p=window.location.pathname,s=" + route_prefix + ";"
        "var r=p.indexOf(s)===0?p.slice(s.length):p;"
        'return r||"/"})()'
    )
    text = text.replace(
        "window.location.pathname+window.location.search",
        route_iife + "+window.location.search",
    )

    # OpenCode V1 (SolidJS router) base-path form — kept for compatibility with
    # images still shipping the V1 web UI during a rolling upgrade.
    route_pattern = re.compile(
        r'''const r=window\.location\.pathname\.replace\([^)]*\)\+window\.location\.search'''
    )
    route_replacement = (
        f"const _swRoutePrefix={route_prefix};"
        "const _swRawPath=window.location.pathname;"
        "const _swRoutePath=_swRawPath.indexOf(_swRoutePrefix)===0?"
        "_swRawPath.slice(_swRoutePrefix.length):_swRawPath;"
        'const r=(_swRoutePath.startsWith("/")?_swRoutePath:"/"+_swRoutePath)'
        '.replace(/^\\/+/,"/")+window.location.search'
    )
    text = route_pattern.sub(route_replacement, text, count=1)

    # Rewrite every quoted absolute asset reference. OpenCode V2 serves its
    # bundle from /_assets/ (plus /icons/); V1 used /assets/. All quote styles
    # (", ', `) are matched so worker, sprite, image, and media assets stay
    # behind the Swarmer proxy prefix.
    clean_prefix = prefix.rstrip("/")
    text = re.sub(
        r"(?P<quote>[\"'`])/(?P<asset>(?:_?assets|icons)/)",
        lambda match: f"{match.group('quote')}{clean_prefix}/{match.group('asset')}",
        text,
    )

    # OpenCode V2 resolves its API base through a currentServerUrl() helper
    # that returns location.origin. Rewrite it to include the proxy prefix so
    # there is exactly one server in the UI's list, pointed at the proxy.
    text = re.sub(
        r"function(?P<name>\s+\w+)?\s*\(\)\{return location\.origin\}",
        lambda m: f'function{m.group("name") or ""}(){{return location.origin+"{prefix}"}}',
        text,
        count=1,
    )

    # OpenCode V1's server URL fallback — kept for rolling-upgrade compat.
    text = text.replace(
        'location.hostname.includes("opencode.ai")?"http://localhost:4096":location.origin',
        f'location.hostname.includes("opencode.ai")?"http://localhost:4096":location.origin+"{prefix}"',
    )

    return text.encode("utf-8")


def _rewrite_css(content: bytes, prefix: str) -> bytes:
    """Rewrite absolute CSS asset URLs so fonts and images stay proxied."""
    text = content.decode("utf-8", errors="replace")

    def replace_url(match: re.Match[str]) -> str:
        whitespace, quote, path, closing_quote, trailing = match.groups()
        if path.startswith(f"{prefix}/"):
            return match.group(0)
        return f"url({whitespace}{quote}{prefix}{path}{closing_quote}{trailing})"

    text = re.sub(
        r"url\((\s*)(['\"]?)(/[^)'\"]+)(['\"]?)(\s*)\)",
        replace_url,
        text,
    )
    return text.encode("utf-8")


# ── Session lookup helper ─────────────────────────────────────────────────────

async def _load(ws_id: int, sid: int, db: AsyncSession):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    ws_obj = await db.get(Workspace, ws_id)
    result = await db.execute(
        select(Session)
        .where(Session.id == sid)
        .options(selectinload(Session.repos))
    )
    session = result.scalar_one_or_none()
    return ws_obj, session


def _session_ok(ws_obj, session, ws_id: int) -> tuple[str, int] | None:
    """Return (error_message, status_code) if the session can't be proxied, else None."""
    if ws_obj is None or session is None or session.workspace_id != ws_id:
        return ("Not found", 404)
    # Only OpenCode's persistent service is reachable through the fixed
    # loopback TCP relay. Shell TUI sessions have no Chat service.
    if (
        session.mode not in ("server", "tui")
        or session.agent_tool != "opencode"
        or session.phase != "running"
        or not session.sandbox_name
    ):
        return ("Session is not running", 503)
    return None


async def _workspace_accessible(db: AsyncSession, ws_obj: Workspace, auth_session) -> bool:
    """Check the authenticated Console identity against the workspace ACL."""
    from swarmer.workspace_acl import user_can_access_workspace

    username = auth_session.get("username", "")
    groups = auth_session.get("groups", [])
    return await user_can_access_workspace(db, ws_obj, username, groups)


def _opencode_auth_token(session_id: int) -> str:
    """Return OpenCode's per-session Basic credential for the private tunnel."""
    import base64

    from swarmer.crypto import opencode_service_password

    password = opencode_service_password(session_id)
    return base64.b64encode(f"opencode:{password}".encode()).decode()


def _origin_matches_host(origin: str | None, host: str | None) -> bool:
    """Check a browser Origin against the public Swarmer Host header."""
    if not origin or not host:
        return False
    try:
        parsed = urlparse(origin)
        origin_hostname = parsed.hostname
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not origin_hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return False

    default_port = 443 if parsed.scheme == "https" else 80

    def _normalized_authority(value: str) -> tuple[str, int | None] | None:
        try:
            parsed_authority = urlparse(f"//{value}")
            hostname = parsed_authority.hostname
        except ValueError:
            return None
        if not hostname:
            return None
        try:
            port = parsed_authority.port
        except ValueError:
            return None
        if port == default_port:
            port = None
        return hostname.lower(), port

    parsed_authority = _normalized_authority(parsed.netloc)
    request_authority = _normalized_authority(host)
    return parsed_authority is not None and request_authority is not None and parsed_authority == request_authority


def _chat_headers(headers, sid: int, session) -> dict[str, str]:
    """Sanitize browser headers and add only the session's OpenCode credential."""
    connection_tokens = {
        token.strip().lower()
        for token in headers.get("connection", "").split(",")
        if token.strip()
    }
    excluded = _HOP_BY_HOP | connection_tokens | {"authorization", "cookie", "host"}
    forwarded = {k: v for k, v in headers.items() if k.lower() not in excluded}
    forwarded["accept-encoding"] = "identity"
    forwarded["host"] = "localhost:4096"
    forwarded["authorization"] = f"Basic {_opencode_auth_token(sid)}"
    if "x-opencode-directory" not in {key.lower() for key in forwarded}:
        repos = list(session.repos or [])
        local_path = repos[0].local_path.strip("/") if repos else ""
        forwarded["x-opencode-directory"] = f"/sandbox/{local_path}" if local_path else "/sandbox"
    return forwarded


def _chat_query(raw_query: str) -> str:
    """Drop legacy/browser-supplied OpenCode auth query parameters."""
    return urlencode(
        [
            (key, value)
            for key, value in parse_qsl(raw_query, keep_blank_values=True)
            if key.lower() != "auth_token"
        ]
    )


def _chat_target_port(session) -> int:
    from swarmer.agent_tools.registry import get

    tool = get(session.agent_tool)
    port = tool.get_server_port()
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Agent tool has no valid Chat port")
    return port


def _callable_provider_identity(value: Any) -> Any:
    provider = getattr(value, "__self__", None)
    return provider if provider is not None else value


def _client_config_matches(cached: "GatewayConfig", current: "GatewayConfig") -> bool:
    """Return whether a cached client was built from an equivalent config.

    ``bearer_callable`` is compared by identity: ``oidc_manager.invalidate()``
    replaces the callable when a workspace's gateway credentials change, so
    an identity mismatch forces a new client (and closes the old one).
    """
    return (
        cached.gateway_url == current.gateway_url
        and cached.auth_mode == current.auth_mode
        and cached.tls_verify == current.tls_verify
        and cached.tls_ca == current.tls_ca
        and cached.tls_cert == current.tls_cert
        and cached.tls_key == current.tls_key
        and (
            (cached.bearer_token is None and current.bearer_token is None)
            or (
                cached.bearer_token is not None
                and current.bearer_token is not None
                and hmac.compare_digest(
                    cached.bearer_token.encode(), current.bearer_token.encode()
                )
            )
        )
        and _callable_provider_identity(cached.bearer_callable)
        is _callable_provider_identity(current.bearer_callable)
    )


def _safe_close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


async def _gateway_client_for(gateway_config: "GatewayConfig") -> Any:
    """Return a shared OpenShell client for the gateway.

    ForwardTcp streams multiplex over one gRPC channel, so a page of Chat
    requests must not pay a channel (and TLS handshake) per request. This
    cache owns the clients' lifecycle: when a workspace's gateway
    configuration changes, the replaced client is closed — in-flight tunnels
    from it fail and reconnect with the new credentials — and the next
    request builds a fresh one.
    """
    from swarmer import openshell_client

    key = (gateway_config.workspace_id if gateway_config.workspace_id is not None else "default",)
    async with _gateway_clients_lock:
        cached = _gateway_clients.get(key)
        if cached is not None and _client_config_matches(cached[1], gateway_config):
            return cached[0]
        client = await asyncio.to_thread(
            openshell_client.get_client_for_config, gateway_config
        )
        _gateway_clients[key] = (client, gateway_config)
        if cached is not None:
            log.info("Chat gateway client replaced after gateway configuration change")
            await asyncio.to_thread(_safe_close_client, cached[0])
        return client


async def _gateway_and_client(
    ws_obj: Workspace | int | None,
    db: AsyncSession | None = None,
) -> tuple["GatewayConfig", Any]:
    """Resolve the workspace gateway and its authenticated OpenShell client."""
    from swarmer import openshell_client

    gateway_config = await openshell_client.resolve_gateway_config(ws_obj, db)
    _require_secure_gateway(gateway_config)
    client = await _gateway_client_for(gateway_config)
    return gateway_config, client


def _validate_declared_body_size(request: Request) -> None:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            raise ChatBodyTooLargeError("invalid Chat request body length") from None
        if declared_size < 0 or declared_size > _CHAT_MAX_BUFFERED_BODY:
            raise ChatBodyTooLargeError("Chat request body exceeds the configured limit")


async def _limited_request_body(request: Request) -> AsyncIterator[bytes]:
    _validate_declared_body_size(request)
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _CHAT_MAX_BUFFERED_BODY:
            raise ChatBodyTooLargeError("Chat request body exceeds the configured limit")
        yield chunk


def _is_body_limit_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ChatBodyTooLargeError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def _read_limited_response(upstream_resp: httpx.Response) -> bytes:
    content = bytearray()
    async for chunk in upstream_resp.aiter_bytes():
        if len(content) + len(chunk) > _CHAT_MAX_BUFFERED_BODY:
            raise ValueError("Chat response body exceeds the configured limit")
        content.extend(chunk)
    return bytes(content)


def _response_headers(upstream_headers) -> dict[str, str]:
    excluded = _HOP_BY_HOP | {"set-cookie", "set-cookie2"}
    return {k: v for k, v in upstream_headers.items() if k.lower() not in excluded}


def _selected_opencode_session_id(path: str, method: str, response_body: bytes) -> str | None:
    """Return the Chat conversation selected by this OpenCode API request."""
    normalized_path = path.strip("/")
    match = re.fullmatch(r"session/([^/]+)", normalized_path)
    if method.upper() == "GET" and match and match.group(1) != "status":
        return match.group(1)

    if method.upper() != "POST" or normalized_path != "session":
        return None
    try:
        payload = json.loads(response_body)
    except (TypeError, ValueError):
        return None
    session_id = payload.get("id") if isinstance(payload, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _deleted_opencode_session_id(path: str, method: str) -> str | None:
    """Return the conversation deleted by this OpenCode API request, if any."""
    if method.upper() != "DELETE":
        return None
    match = re.fullmatch(r"session/([^/]+)", path.strip("/"))
    if not match or match.group(1) == "status":
        return None
    return match.group(1)


# ── Entry point: /chat (no trailing slash) ────────────────────────────────────

async def _proxy_root(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    # Proxy the root path directly to OpenCode's native web UI running
    # inside the sandbox.  _rewrite_html() will inject a <base> tag and
    # rewrite absolute asset paths so they load through the /chat/{path}
    # proxy prefix.
    return await _chat_http_proxy(ws_id, sid, request, "", db)


# ── Sub-path proxy (in-cluster) ───────────────────────────────────────────────

async def _chat_http_proxy(
    ws_id: int,
    sid: int,
    request: Request,
    path: str,
    db: AsyncSession,
    *,
    _run_fence: _SessionRunFence | None = None,
) -> Response:
    """Proxy HTTP/SSE through an authenticated OpenShell TCP relay."""
    # Capture run identity before session loading or any other await. If stop or
    # relaunch happens during ACL evaluation, this request cannot adopt the new
    # run's fence for the old sandbox snapshot.
    if _run_fence is None:
        _run_fence = _session_run_fences.get(sid)

    ws_obj, session = await _load(ws_id, sid, db)
    if ws_obj is not None and not await _workspace_accessible(db, ws_obj, request.session):
        return Response("Not found", status_code=404, media_type="text/plain")
    err = _session_ok(ws_obj, session, ws_id)
    if err:
        msg, code = err
        return Response(msg, status_code=code, media_type="text/plain")
    assert session is not None
    if (
        _run_fence is None
        or _run_fence.revoked
        or _session_run_fences.get(sid) is not _run_fence
    ):
        return Response("Session is stopping", status_code=503, media_type="text/plain")

    origin = request.headers.get("origin")
    if origin and not _origin_matches_host(origin, request.headers.get("host")):
        return Response("Forbidden", status_code=403, media_type="text/plain")
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not _origin_matches_host(
        origin, request.headers.get("host")
    ):
        return Response("Forbidden", status_code=403, media_type="text/plain")
    try:
        _validate_declared_body_size(request)
    except ChatBodyTooLargeError:
        return Response(
            "Chat request body is too large", status_code=413, media_type="text/plain"
        )

    # Reserve admission before gateway resolution so concurrent requests
    # cannot start expensive gateway work without capacity.
    user_key = request.session.get("username") or "anonymous"
    observed_run_started_at = session.run_started_at
    observed_sandbox_name = session.sandbox_name
    # Revalidate after the awaited ACL check, before reserving resources.
    if _run_fence.revoked or _session_run_fences.get(sid) is not _run_fence:
        return Response("Session is stopping", status_code=503, media_type="text/plain")
    try:
        _admit_chat_tunnel(user_key)
    except ChatTunnelCapacityError as exc:
        return Response(
            f"{exc} — try again shortly",
            status_code=503,
            media_type="text/plain",
        )
    admission = _ChatAdmissionReservation(user_key)
    try:
        return await _chat_http_proxy_admitted(
            ws_id,
            sid,
            request,
            path,
            db,
            ws_obj,
            session,
            _run_fence,
            admission,
            user_key,
            observed_run_started_at,
            observed_sandbox_name,
        )
    finally:
        # Cancellation or any failure before the tunnel registers its cleanup
        # handler releases capacity. A transferred reservation is idempotent.
        admission.release()



async def _chat_http_proxy_admitted(
    ws_id: int,
    sid: int,
    request: Request,
    path: str,
    db: AsyncSession,
    ws_obj: Workspace,
    session: Session,
    run_fence: _SessionRunFence,
    admission: _ChatAdmissionReservation,
    user_key: str,
    observed_run_started_at: datetime | None,
    observed_sandbox_name: str,
) -> Response:
    """Forward an HTTP request using the snapshot already authorized by the caller."""
    from starlette.responses import StreamingResponse

    try:
        _gateway_config, tunnel_client = await _gateway_and_client(ws_obj, db)
        target_port = _chat_target_port(session)
    except Exception as exc:  # noqa: BLE001 - gateway SDK/auth failures must fail closed
        log.warning("Chat tunnel setup failed for session %d (%s)", sid, type(exc).__name__)
        return Response("Chat tunnel is unavailable", status_code=503, media_type="text/plain")

    try:
        upstream_path = f"/{path}"
        query = _chat_query(str(request.url.query))
        if query:
            upstream_path += f"?{query}"
        fwd_headers = _chat_headers(request.headers, sid, session)
    except Exception as exc:  # noqa: BLE001 - release admission on request setup errors
        log.warning("Chat request setup failed for session %d (%s)", sid, type(exc).__name__)
        return Response("Chat tunnel is unavailable", status_code=503, media_type="text/plain")

    accept = request.headers.get("accept", "")
    is_sse = (
        "text/event-stream" in accept
        or path == "event"
        or path.endswith("/event")
    )
    log.debug("chat proxy: %s %s (sse=%s)", request.method, path, is_sse)

    if is_sse:
        stack = AsyncExitStack()
        try:
            local_base = await stack.enter_async_context(
                _open_sandbox_tunnel(
                    session.sandbox_name, target_port, tunnel_client,
                    session_id=sid, user_key=user_key, admission_reserved=True,
                    run_fence=run_fence, admission=admission,
                )
            )
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    trust_env=False,
                    timeout=httpx.Timeout(
                        connect=10, read=_CHAT_SSE_READ_TIMEOUT, write=10, pool=10
                    ),
                )
            )
            upstream_resp = await stack.enter_async_context(
                http_client.stream(
                    request.method,
                    local_base + upstream_path,
                    headers=fwd_headers,
                    content=(
                        _limited_request_body(request)
                        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
                        else b""
                    ),
                )
            )
        except (ChatTunnelCapacityError, ChatBodyTooLargeError) as exc:
            await stack.aclose()
            return Response(
                f"{exc}",
                status_code=413 if isinstance(exc, ChatBodyTooLargeError) else 503,
                media_type="text/plain",
            )
        except Exception as exc:  # noqa: BLE001 - tunnel setup may fail in gRPC or HTTPX
            await stack.aclose()
            if _is_body_limit_error(exc):
                return Response(
                    "Chat request body is too large", status_code=413, media_type="text/plain"
                )
            log.warning("Chat SSE tunnel failed for session %d (%s)", sid, type(exc).__name__)
            return Response("Could not connect to session", status_code=503, media_type="text/plain")
        except BaseException:
            # Cancellation during setup must still release the tunnel,
            # listener, HTTP client, and admission slot.
            await stack.aclose()
            raise

        async def sse_generator():
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    yield chunk
            except httpx.HTTPError as exc:
                # Once StreamingResponse has sent headers, an upstream read
                # failure must end the body normally rather than escape the
                # ASGI response task and leave the client response incomplete.
                log.warning("Chat SSE stream failed for session %d (%s)", sid, type(exc).__name__)

        class _StackClosingStreamingResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    close_task = asyncio.create_task(stack.aclose())
                    try:
                        await asyncio.shield(close_task)
                    except asyncio.CancelledError:
                        await close_task
                        raise

        response_headers = _response_headers(upstream_resp.headers)
        response_headers["Cache-Control"] = "no-cache"
        response_headers["X-Accel-Buffering"] = "no"
        response_headers.pop("content-length", None)
        response_headers.pop("content-encoding", None)
        return _StackClosingStreamingResponse(
            sse_generator(),
            status_code=upstream_resp.status_code,
            headers=response_headers,
            media_type=upstream_resp.headers.get("content-type", "text/event-stream"),
        )

    try:
        async with (
            _open_sandbox_tunnel(
                session.sandbox_name, target_port, tunnel_client,
                session_id=sid, user_key=user_key, admission_reserved=True,
                run_fence=run_fence, admission=admission,
            ) as local_base,
            httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0),
            ) as http_client,
        ):
            outbound = http_client.build_request(
                method=request.method,
                url=local_base + upstream_path,
                headers=fwd_headers,
                content=(
                    _limited_request_body(request)
                    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
                    else b""
                ),
            )
            upstream_resp = await http_client.send(outbound, stream=True, follow_redirects=False)
            try:
                content = await _read_limited_response(upstream_resp)
                upstream_status = upstream_resp.status_code
                upstream_headers = upstream_resp.headers
            finally:
                await upstream_resp.aclose()
    except ChatTunnelCapacityError as exc:
        return Response(
            f"{exc} — try again shortly", status_code=503, media_type="text/plain"
        )
    except Exception as exc:  # noqa: BLE001 - translate gRPC and HTTPX errors to a safe response
        log.warning("Chat proxy tunnel failed for session %d (%s)", sid, type(exc).__name__)
        too_large = _is_body_limit_error(exc) or "exceeds the configured limit" in str(exc)
        message = "Chat request or response is too large" if too_large else "Could not connect to session"
        return Response(message, status_code=413 if too_large else 503, media_type="text/plain")

    if 200 <= upstream_status < 300:
        selected_session_id = _selected_opencode_session_id(path, request.method, content)
        deleted_session_id = _deleted_opencode_session_id(path, request.method)
        selected_value = selected_session_id or None
        if deleted_session_id:
            from sqlalchemy import update

            statement = (
                update(Session)
                .where(
                    Session.id == sid,
                    Session.run_started_at == observed_run_started_at,
                    Session.sandbox_name == observed_sandbox_name,
                    Session.opencode_session_id == deleted_session_id,
                )
                .values(opencode_session_id=None)
                .execution_options(synchronize_session=False)
            )
        elif selected_value:
            from sqlalchemy import update

            statement = (
                update(Session)
                .where(
                    Session.id == sid,
                    Session.run_started_at == observed_run_started_at,
                    Session.sandbox_name == observed_sandbox_name,
                )
                .values(opencode_session_id=selected_value)
                .execution_options(synchronize_session=False)
            )
        else:
            statement = None
        if statement is not None:
            try:
                await db.execute(statement)
                await db.commit()
            except SQLAlchemyError as exc:
                await db.rollback()
                log.warning(
                    "chat proxy: could not persist selected OpenCode session for session %d (%s)",
                    sid,
                    type(exc).__name__,
                )

    log.debug("chat proxy: %s %s → %d", request.method, path, upstream_status)
    if upstream_status >= 400:
        log.warning(
            "chat proxy: upstream %d for session %d: %s %s",
            upstream_status,
            sid,
            request.method,
            path,
        )

    content_type = upstream_headers.get("content-type", "")
    prefix = f"/workspaces/{ws_id}/sessions/{sid}/chat"
    if "text/html" in content_type:
        content = _rewrite_html(content, prefix)
    elif "text/css" in content_type:
        content = _rewrite_css(content, prefix)
    elif "javascript" in content_type:
        content = _rewrite_js(content, prefix)

    resp_headers = _response_headers(upstream_headers)
    resp_headers.pop("content-length", None)
    resp_headers.pop("content-encoding", None)
    if "text/html" in content_type:
        csp_key = next(
            (key for key in resp_headers if key.lower() == "content-security-policy"),
            None,
        )
        if csp_key:
            resp_headers[csp_key] = _rewrite_content_security_policy(
                resp_headers[csp_key],
                [_proxy_intercept_script(prefix), _proxy_compat_script(prefix)],
            )
    if 300 <= upstream_status < 400:
        location = resp_headers.get("location", "")
        if location.startswith("/"):
            resp_headers["location"] = prefix + "/" + location.lstrip("/")

    return Response(
        content=content,
        status_code=upstream_status,
        headers=resp_headers,
        media_type=content_type or None,
    )


async def _proxy_path(
    ws_id: int,
    sid: int,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    return await _chat_http_proxy(ws_id, sid, request, path, db)


_PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

router.add_api_route(
    "/workspaces/{ws_id}/sessions/{sid}/chat",
    _proxy_root,
    methods=_PROXY_METHODS,
    dependencies=[Depends(require_auth)],
)
router.add_api_route(
    "/workspaces/{ws_id}/sessions/{sid}/chat/{path:path}",
    _proxy_path,
    methods=_PROXY_METHODS,
    dependencies=[Depends(require_auth)],
)


# ── WebSocket proxy ───────────────────────────────────────────────────────────

@router.websocket(
    "/workspaces/{ws_id}/sessions/{sid}/chat/{path:path}",
    dependencies=[Depends(require_websocket_auth)],
)
async def chat_ws_proxy(
    websocket: WebSocket,
    ws_id: int,
    sid: int,
    path: str,
) -> None:
    # Capture the immutable run token before any database or ACL await.
    run_fence = _session_run_fences.get(sid)
    if not websocket.session.get("authenticated"):
        await websocket.close(code=4001, reason="Not authenticated")
        return
    if not _origin_matches_host(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=4403, reason="Forbidden origin")
        return

    ws_obj: Workspace | None = None
    session: Session | None = None
    allowed = False
    async for db in get_db():
        ws_obj = await db.get(Workspace, ws_id)
        session = await db.get(Session, sid)
        if ws_obj is not None:
            allowed = await _workspace_accessible(db, ws_obj, websocket.session)
        break

    if not allowed:
        await websocket.close(code=4404, reason="Session unavailable")
        return
    err = _session_ok(ws_obj, session, ws_id)
    if err:
        await websocket.close(code=4404, reason="Session unavailable")
        return
    assert session is not None
    if (
        run_fence is None
        or run_fence.revoked
        or _session_run_fences.get(sid) is not run_fence
    ):
        await websocket.close(code=4404, reason="Session unavailable")
        return

    # Revalidate after awaited ACL evaluation, then own the reservation until
    # the tunnel context registers its cleanup handler.
    user_key = websocket.session.get("username") or "anonymous"
    if run_fence.revoked or _session_run_fences.get(sid) is not run_fence:
        await websocket.close(code=4404, reason="Session unavailable")
        return
    try:
        _admit_chat_tunnel(user_key)
    except ChatTunnelCapacityError:
        await websocket.accept()
        await websocket.close(code=1013, reason="Chat is busy")
        return

    reservation = _ChatAdmissionReservation(user_key)
    try:
        await _chat_ws_proxy_admitted(
            websocket,
            ws_obj,
            session,
            sid,
            path,
            user_key,
            run_fence,
            reservation,
        )
    finally:
        reservation.release()


async def _chat_ws_proxy_admitted(
    websocket: WebSocket,
    ws_obj: Workspace,
    session: Session,
    sid: int,
    path: str,
    user_key: str,
    run_fence: _SessionRunFence,
    reservation: _ChatAdmissionReservation,
) -> None:
    """Complete an authorized WebSocket request under its admission lease."""
    try:
        _gateway_config, tunnel_client = await _gateway_and_client(ws_obj)
        target_port = _chat_target_port(session)
    except Exception as exc:  # noqa: BLE001 - gateway SDK/auth failures must fail closed
        await websocket.accept()
        log.warning("Chat WS tunnel setup failed for session %d (%s)", sid, type(exc).__name__)
        await websocket.close(code=1011, reason="Chat tunnel unavailable")
        return

    websocket_accepted = False
    upstream_path = f"/{path}"
    query = _chat_query(websocket.url.query)
    if query:
        upstream_path += f"?{query}"

    try:
        async with _open_sandbox_tunnel(
            session.sandbox_name, target_port, tunnel_client,
            session_id=sid, user_key=user_key, admission_reserved=True,
            run_fence=run_fence, admission=reservation,
        ) as local_base:
            # Connect a preconnected socket and hand it to websockets: the
            # library cannot follow any redirect on a socket it did not
            # create, so the upstream can never move this session (or the
            # OpenCode credential) outside the sandbox tunnel.
            sock = await _tunnel_socket(local_base)
            try:
                protocols = [
                    protocol.strip()
                    for protocol in websocket.headers.get("sec-websocket-protocol", "").split(",")
                    if protocol.strip()
                ]
                connect_options = {
                    "additional_headers": {"Authorization": f"Basic {_opencode_auth_token(sid)}"},
                    "origin": websocket.headers.get("origin"),
                    "open_timeout": 10,
                    "ping_interval": 20,
                    "ping_timeout": 20,
                    "max_size": 8 * 1024 * 1024,
                    "proxy": None,
                    "sock": sock,
                    # Filtered logger: the library logs handshake headers at
                    # DEBUG, including Authorization.
                    "logger": _ws_tunnel_logger,
                }
                if protocols:
                    connect_options["subprotocols"] = protocols
                # The URI fixes the handshake Host header to the sandbox-local
                # OpenCode endpoint; the preconnected socket above is the
                # actual transport.
                async with websockets.connect(
                    "ws://localhost:4096" + upstream_path, **connect_options
                ) as upstream_ws:
                    await websocket.accept(subprotocol=upstream_ws.subprotocol)
                    websocket_accepted = True
                    await _relay_websocket(websocket, upstream_ws)
            finally:
                # websockets owns the socket once connected; closing here only
                # matters when the handshake failed before taking ownership.
                with contextlib.suppress(OSError):
                    sock.close()
    except ChatTunnelCapacityError:
        with contextlib.suppress(Exception):
            await websocket.close(code=1013, reason="Chat is busy")
    except Exception as exc:  # noqa: BLE001 - translate upstream WebSocket errors to a safe close
        log.warning("Chat WS tunnel failed for session %d (%s)", sid, type(exc).__name__)
        with contextlib.suppress(Exception):
            if not websocket_accepted:
                await websocket.accept()
            await websocket.close(code=1011, reason="Chat tunnel unavailable")
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


async def _relay_websocket(websocket: WebSocket, upstream_ws: Any) -> None:
    """Pump frames between the browser and the tunneled OpenCode WebSocket."""
    async def client_to_upstream() -> None:
        try:
            while True:
                data = await websocket.receive()
                if data.get("type") == "websocket.disconnect":
                    return
                if "text" in data:
                    await upstream_ws.send(data["text"])
                elif "bytes" in data:
                    await upstream_ws.send(data["bytes"])
        except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed, OSError):
            return

    async def upstream_to_client() -> None:
        try:
            async for message in upstream_ws:
                if isinstance(message, bytes):
                    await websocket.send_bytes(message)
                else:
                    await websocket.send_text(message)
        except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed, OSError):
            return

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

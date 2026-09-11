"""
Reverse-proxy router for server-mode sessions.

Swarmer proxies HTTP/WebSocket traffic through /chat/{path} using
httpx + websockets, connecting to the session's OpenShell service URL.
HTML asset paths are rewritten so they resolve through the proxy prefix.

Gateway routing: the OpenShell gateway assigns virtual-host domain URLs
(e.g. https://oriented-lizardfish--agent.openshell.localhost:8080) that
are not DNS-resolvable from the Swarmer pod.  _resolve_upstream() rewrites
the hostname to the gateway's real address while preserving the original
domain as the Host header so the gateway can route to the right sandbox.

OpenCode sessions: the root /chat path proxies directly to OpenCode's
native web UI running inside the sandbox on port 4096.  OpenCode's built-in
UI is served as-is; HTML is rewritten so asset paths resolve through the
proxy prefix.
"""
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import ssl
from urllib.parse import urlparse, urlunparse

import httpx
import websockets
from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace

router = APIRouter()
log = logging.getLogger(__name__)
# Chat proxy emits one log line per proxied asset (JS chunks, CSS, API calls).
# Pin this logger to WARNING so it stays quiet at the default INFO level.
# Set LOG_LEVEL=DEBUG in the environment to enable full per-request tracing.
log.setLevel(logging.WARNING)

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
})

# ── OpenShell mTLS helpers ────────────────────────────────────────────────────

def _openshell_ssl_context(gateway_config=None) -> ssl.SSLContext | None:
    """Return an SSL context for an HTTPS OpenShell gateway, when configured."""
    from swarmer.config import settings

    cert = (gateway_config.tls_cert if gateway_config else settings.openshell_tls_cert) or ""
    key = (gateway_config.tls_key if gateway_config else settings.openshell_tls_key) or ""
    ca = (gateway_config.tls_ca if gateway_config else settings.openshell_tls_ca) or None
    verify = gateway_config.tls_verify if gateway_config else settings.openshell_tls_verify

    if verify:
        ctx = ssl.create_default_context()
        if ca:
            if "BEGIN " in ca:
                ctx.load_verify_locations(cadata=ca)
            else:
                ctx.load_verify_locations(cafile=ca)
    else:
        # Insecure TLS is opt-in for development gateways with self-signed
        # certificates. Production/default configurations always verify.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if cert and key:
        # Workspace gateway credentials may be inline PEM content, while the
        # global configuration normally contains filesystem paths.
        from swarmer.openshell_client import _tls_material_path

        cert_path, cert_temp = _tls_material_path(cert)
        key_path, key_temp = _tls_material_path(key)
        try:
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
        finally:
            if cert_temp:
                cert_path.unlink(missing_ok=True)
            if key_temp:
                key_path.unlink(missing_ok=True)

    return ctx


def _openshell_httpx_kwargs(gateway_config=None) -> dict:
    """Return httpx kwargs for connecting to an OpenShell gateway service URL."""
    from swarmer.config import settings

    ca = (gateway_config.tls_ca if gateway_config else settings.openshell_tls_ca) or None
    verify = gateway_config.tls_verify if gateway_config else True

    context = _openshell_ssl_context(gateway_config)
    if context is not None:
        return {"verify": context}

    return {"verify": ca if ca else verify}


def _resolve_upstream(service_url: str, gateway_config=None) -> tuple[str, str]:
    """Return (connectable_url, virtual_host) for an OpenShell service URL.

    The OpenShell gateway assigns virtual-host domain URLs like
      https://oriented-lizardfish--agent.openshell.localhost:8080
    whose hostnames are not resolvable from the Swarmer pod.  We rewrite
    the hostname to the gateway's real address (derived from
    OPENSHELL_GATEWAY_URL or workspace gateway_url) so httpx/websockets can
    open a TCP connection, while returning the original domain as the Host
    header value so the gateway's HTTP router can identify the target sandbox.

    If gateway URL is not configured, the original URL is returned
    unchanged (useful when wildcard DNS is set up at the cluster level).
    """
    from swarmer.config import settings

    parsed = urlparse(service_url)
    virtual_host = parsed.hostname or ""
    port = parsed.port

    gw = (gateway_config.gateway_url if gateway_config else settings.openshell_gateway_url) or ""
    if gw:
        # Parse the real gateway hostname from the gateway URL.
        # The URL may be bare "host:port" (no scheme) or "grpc://host:port".
        gw_with_scheme = gw if "://" in gw else f"https://{gw}"
        gw_parsed = urlparse(gw_with_scheme)
        real_host = gw_parsed.hostname or virtual_host
        # Port is already rewritten to the gateway port by expose_service().
        netloc = f"{real_host}:{port}" if port else real_host
        connectable_url = urlunparse(parsed._replace(netloc=netloc))
    else:
        connectable_url = service_url

    host_header = f"{virtual_host}:{port}" if port else virtual_host
    return connectable_url, host_header


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
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return content

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
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return content

    # Vite's __vitePreload base path function.  Vite uses a single-char variable
    # name that may vary across builds (t, e, n, r, ...).  Use a regex so the
    # rewrite is robust across minification variants.
    text = re.sub(
        r'function\((\w)\)\{return"/"\+\1\}',
        lambda m: f'function({m.group(1)}){{return"{prefix}/"+{m.group(1)}}}',
        text,
    )

    # OpenCode's SolidJS router reads window.location.pathname directly and
    # does not configure a router base. Strip the Swarmer proxy prefix so its
    # routes (/, /new-session, and /:dir/session/:id) still match.
    clean_prefix = prefix.rstrip("/")
    route_prefix = json.dumps(clean_prefix)
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

    # Rewrite every quoted absolute asset reference. OpenCode's bundle has
    # multiple worker, sprite, image, and media assets; limiting this to one
    # worker filename leaves other runtime dependencies at the Swarmer root.
    clean_prefix = prefix.rstrip("/")
    text = re.sub(
        r"(?P<quote>[\"'`])/(?P<asset>assets/)",
        lambda match: f"{match.group('quote')}{clean_prefix}/{match.group('asset')}",
        text,
    )

    # OpenCode's server URL fallback returns location.origin when not on
    # opencode.ai.  This adds the Swarmer root as the "canonical local server",
    # conflicting with the defaultServerUrl we set in localStorage.  Rewrite it
    # to include the proxy prefix so there is exactly one server in the list.
    text = text.replace(
        'location.hostname.includes("opencode.ai")?"http://localhost:4096":location.origin',
        f'location.hostname.includes("opencode.ai")?"http://localhost:4096":location.origin+"{prefix}"',
    )

    return text.encode("utf-8")


def _rewrite_css(content: bytes, prefix: str) -> bytes:
    """Rewrite absolute CSS asset URLs so fonts and images stay proxied."""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return content

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
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
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
    if session.mode != "server" or not session.is_active:
        return ("Session is not running in server mode", 503)
    if not session.service_url:
        return ("Session has no service URL yet", 503)
    return None


# ── Entry point: /chat (no trailing slash) ────────────────────────────────────

async def _proxy_root(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    ws_obj, session = await _load(ws_id, sid, db)
    err = _session_ok(ws_obj, session, ws_id)
    if err:
        msg, code = err
        return Response(msg, status_code=code, media_type="text/plain")

    assert session is not None  # _session_ok returns error if session is None

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
) -> Response:
    from starlette.responses import StreamingResponse

    ws_obj, session = await _load(ws_id, sid, db)
    err = _session_ok(ws_obj, session, ws_id)
    if err:
        msg, code = err
        return Response(msg, status_code=code, media_type="text/plain")

    assert session is not None  # _session_ok returns error if session is None

    from swarmer.openshell_client import resolve_gateway_config
    gw_config = await resolve_gateway_config(ws_obj, db)

    connectable_base, virtual_host = _resolve_upstream(session.service_url or "", gw_config)
    upstream_base = connectable_base.rstrip("/")

    query = str(request.url.query)
    upstream_url = f"{upstream_base}/{path}"
    if query:
        upstream_url += f"?{query}"

    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # Set the virtual host so the OpenShell gateway routes to the right sandbox.
    fwd_headers["host"] = virtual_host
    if gw_config.bearer_callable:
        try:
            # bearer_callable is a synchronous callable (the OpenShell SDK's
            # gRPC interceptor contract) that may perform a blocking HTTP
            # refresh against the OIDC IdP when the cached access token is
            # stale. Off-load it so a slow/stale refresh can't stall the
            # asyncio event loop for every other in-flight request.
            token = await asyncio.to_thread(gw_config.bearer_callable)
            fwd_headers["authorization"] = f"Bearer {token}"
        except Exception:
            pass
    elif gw_config.bearer_token:
        fwd_headers["authorization"] = f"Bearer {gw_config.bearer_token}"
    # Set x-opencode-directory only if the client did not already supply it.
    # OpenCode's JS SDK passes directory as a ?directory= query param and also
    # sets x-opencode-directory; we honour whatever the client sent and only
    # fall back to the first repo path when the header is absent entirely.
    if "x-opencode-directory" not in fwd_headers:
        try:
            _repos = list(session.repos or [])  # type: ignore[union-attr]
            if _repos:
                _local_path = _repos[0].local_path.strip("/")
                fwd_headers["x-opencode-directory"] = f"/sandbox/{_local_path}"
            else:
                fwd_headers["x-opencode-directory"] = "/sandbox"
        except Exception:
            fwd_headers["x-opencode-directory"] = "/sandbox"

    # Detect SSE connections.  OpenCode's SDK uses fetch()-based streaming
    # rather than the browser EventSource API, so the client does NOT set
    # Accept: text/event-stream automatically.  We also match on path because
    # fetch() SSE requests look identical to ordinary GET requests.
    # Known SSE endpoints: "event", "global/event", "api/event".
    accept = request.headers.get("accept", "")
    is_sse = (
        "text/event-stream" in accept
        or path == "event"
        or path.endswith("/event")
    )

    log.debug("chat proxy: %s %s → %s (sse=%s)", request.method, path, upstream_url, is_sse)

    _tls_kwargs = _openshell_httpx_kwargs(gw_config)

    if is_sse:
        # Stream SSE responses — long-lived connection, no timeout buffering
        client = httpx.AsyncClient(**_tls_kwargs, timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10))

        async def sse_generator():
            try:
                async with client.stream(
                    "GET",
                    upstream_url,
                    headers=fwd_headers,
                ) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except Exception as exc:
                log.warning("SSE stream error for session %d: %s", sid, exc)
            finally:
                await client.aclose()

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Standard request/response proxy
    try:
        async with httpx.AsyncClient(**_tls_kwargs) as client:
            upstream_resp = await client.request(
                method=request.method,
                url=upstream_url,
                headers=fwd_headers,
                content=await request.body(),
                follow_redirects=False,
                timeout=httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0),
            )
    except Exception as exc:
        log.warning("chat proxy: upstream error %s for session %d url=%s: %s",
                    type(exc).__name__, sid, upstream_url, exc, exc_info=True)
        return Response(
            f"Could not connect to session ({virtual_host}): {type(exc).__name__}: {exc}",
            status_code=503, media_type="text/plain",
        )

    log.debug("chat proxy: %s %s → %d", request.method, upstream_url, upstream_resp.status_code)
    if upstream_resp.status_code >= 400:
        log.warning("chat proxy: upstream %d for session %d: %s %s — body: %.200s",
                    upstream_resp.status_code, sid, request.method, upstream_url,
                    upstream_resp.text)

    content_type = upstream_resp.headers.get("content-type", "")
    content = upstream_resp.content
    prefix = f"/workspaces/{ws_id}/sessions/{sid}/chat"

    if "text/html" in content_type:
        content = _rewrite_html(content, prefix)
    elif "text/css" in content_type:
        content = _rewrite_css(content, prefix)
    elif "javascript" in content_type:
        content = _rewrite_js(content, prefix)

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    resp_headers.pop("content-length", None)
    # httpx auto-decompresses the body; drop the encoding header so the browser
    # doesn't try to decompress already-decompressed bytes.
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

    # Rewrite redirect Location headers so the browser stays inside the proxy.
    # Without this, a 302 Location: /Lw/session/... from OpenCode's SPA
    # would send the browser to a Swarmer 404 instead of /chat/Lw/session/...
    if 300 <= upstream_resp.status_code < 400:
        location = resp_headers.get("location", "")
        if location.startswith("/"):
            resp_headers["location"] = prefix + "/" + location.lstrip("/")

    return Response(
        content=content,
        status_code=upstream_resp.status_code,
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

@router.websocket("/workspaces/{ws_id}/sessions/{sid}/chat/{path:path}")
async def chat_ws_proxy(
    websocket: WebSocket,
    ws_id: int,
    sid: int,
    path: str,
):
    await websocket.accept()

    if not websocket.session.get("authenticated"):
        await websocket.close(code=4001, reason="Not authenticated")
        return

    ws_obj: Workspace | None = None
    session: Session | None = None
    async for db in get_db():
        ws_obj = await db.get(Workspace, ws_id)
        session = await db.get(Session, sid)
        break

    if (
        ws_obj is None
        or session is None
        or session.workspace_id != ws_id
        or session.mode != "server"
        or not session.is_active
        or not session.service_url
    ):
        await websocket.close(code=4004, reason="Session unavailable")
        return

    from swarmer.openshell_client import resolve_gateway_config
    gw_config = await resolve_gateway_config(ws_obj)

    connectable_base, virtual_host = _resolve_upstream(session.service_url or "", gw_config)
    upstream_base = connectable_base.rstrip("/")

    query = websocket.url.query
    upstream_url = upstream_base.replace("http://", "ws://").replace("https://", "wss://") + f"/{path}"
    if query:
        upstream_url += f"?{query}"

    _ws_ssl = _openshell_ssl_context(gw_config) if upstream_url.startswith("wss://") else None
    _ws_extra_headers = {"Host": virtual_host} if virtual_host else {}
    if gw_config.bearer_callable:
        try:
            # See the HTTP proxy path above: bearer_callable() can block on a
            # synchronous OIDC refresh, so run it off the event loop.
            token = await asyncio.to_thread(gw_config.bearer_callable)
            _ws_extra_headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass
    elif gw_config.bearer_token:
        _ws_extra_headers["Authorization"] = f"Bearer {gw_config.bearer_token}"

    try:
        async with websockets.connect(upstream_url, ssl=_ws_ssl, additional_headers=_ws_extra_headers) as upstream_ws:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        data = await websocket.receive()
                        if "text" in data:
                            await upstream_ws.send(data["text"])
                        elif "bytes" in data:
                            await upstream_ws.send(data["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    except Exception as exc:
        log.warning("Chat WS proxy error for session %d: %s", sid, exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()

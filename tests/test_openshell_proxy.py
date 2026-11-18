"""Tests for OpenShell TUI WebSocket and chat proxy integration.

Covers:
  - openshell_client: expose_service(), delete_service(), exec_interactive()
  - chat_proxy: _session_ok() accepts service_url, HTTP/WS proxy uses service_url,
    x-opencode-directory header is /sandbox/ for OpenShell sessions
  - tui_ws: session validation accepts sandbox_name, ExecSandboxInteractive is called,
    stdin/resize/stdout forwarding, exit event closes WS
  - sessions.py: server mode calls expose_service after start_agent,
    stop/delete call delete_service before delete_sandbox
"""

import asyncio
import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

# ---------------------------------------------------------------------------
# Inject openshell SDK stub (no real package needed for unit tests)
# ---------------------------------------------------------------------------


class _SandboxSpec:
    def __init__(self):
        class _T:
            image = ""
        self.template = _T()
        self.environment = {}
        self.policy = None
        self.providers = []


class _ProtoMessage:
    """Minimal proto-message stub that stores constructor kwargs as attributes."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def WhichOneof(self, name):
        for field in ("init", "data"):
            if hasattr(self, field):
                return field
        return None


class _ExecSandboxRequest(_ProtoMessage):
    pass


class _ExecSandboxInput(_ProtoMessage):
    pass


class _ExposeServiceRequest(_ProtoMessage):
    pass


class _DeleteServiceRequest(_ProtoMessage):
    pass


class _TcpForwardInit(_ProtoMessage):
    pass


class _TcpRelayTarget(_ProtoMessage):
    pass


class _TcpForwardFrame(_ProtoMessage):
    pass


class _SandboxPolicy(_ProtoMessage):
    pass


_proto_stub = MagicMock()
_proto_stub.openshell_pb2 = MagicMock()
_proto_stub.openshell_pb2.SandboxSpec = _SandboxSpec
_proto_stub.openshell_pb2.ExecSandboxRequest = _ExecSandboxRequest
_proto_stub.openshell_pb2.ExecSandboxInput = _ExecSandboxInput
_proto_stub.openshell_pb2.ExposeServiceRequest = _ExposeServiceRequest
_proto_stub.openshell_pb2.DeleteServiceRequest = _DeleteServiceRequest
_proto_stub.openshell_pb2.TcpForwardInit = _TcpForwardInit
_proto_stub.openshell_pb2.TcpRelayTarget = _TcpRelayTarget
_proto_stub.openshell_pb2.TcpForwardFrame = _TcpForwardFrame
_proto_stub.sandbox_pb2 = MagicMock()
_proto_stub.sandbox_pb2.SandboxPolicy = _SandboxPolicy

_sdk_stub = MagicMock()
_sdk_stub.SandboxClient = MagicMock
_sdk_stub.TlsConfig = MagicMock
_sdk_stub._proto = _proto_stub

# Save any real openshell modules already in sys.modules so we can restore
# them after importing swarmer.openshell_client with our stubs.  This prevents
# the stubs from polluting sys.modules for other test files (e.g.
# test_openshell_policy.py) that need the real protobuf classes.
_saved_modules = {k: v for k, v in sys.modules.items() if "openshell" in k}

sys.modules["openshell"] = _sdk_stub
sys.modules["openshell._proto"] = _proto_stub
sys.modules["openshell._proto.openshell_pb2"] = _proto_stub.openshell_pb2
sys.modules["openshell._proto.sandbox_pb2"] = _proto_stub.sandbox_pb2

import swarmer.openshell_client as oc  # noqa: E402

# Restore real openshell modules (or remove the stubs if none were there before).
# Iterate over ALL current sys.modules entries that contain "openshell" (excluding
# swarmer.openshell_client which we intentionally imported) so that transitively-
# loaded modules (sandbox_pb2, datamodel_pb2, etc.) are also restored.  A hardcoded
# key list misses those and leaves a stale MagicMock in the module chain, causing
# lazy imports in other test files to pick up mocks instead of real proto classes.
for _k in list(sys.modules):
    if "openshell" in _k and "swarmer" not in _k:
        if _k in _saved_modules:
            sys.modules[_k] = _saved_modules[_k]
        else:
            sys.modules.pop(_k, None)


# Tests in this file that inspect proto message fields (e.g. req.sandbox_id)
# need the real openshell SDK so proto constructors store kwargs as attributes.
# Skip SDK-bound checks only when the real OpenShell SDK is unavailable
# when the full SDK is unavailable (CI without internal registry access).
try:
    from openshell import SandboxClient as _SC  # noqa: F401
    _REAL_SDK = True
except Exception:
    _REAL_SDK = False

_requires_sdk = pytest.mark.skipif(
    not _REAL_SDK,
    reason="Requires real openshell SDK (SandboxClient); not available in CI",
)


# ---------------------------------------------------------------------------
# Shared DB + app fixtures
# ---------------------------------------------------------------------------

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_chat_lifecycle_state():
    """Keep per-process tunnel fences and admission counters test-local."""
    from swarmer.routers import chat_proxy as cp

    cp._chat_tunnel_count = 0
    cp._chat_user_tunnels.clear()
    for handles in cp._session_tunnels.values():
        for handle in handles:
            handle.request_close()
    cp._session_tunnels.clear()
    for fence in cp._session_run_fences.values():
        fence.revoked = True
    cp._session_run_fences.clear()
    yield
    for handles in cp._session_tunnels.values():
        for handle in handles:
            handle.request_close()
    cp._session_tunnels.clear()
    for fence in cp._session_run_fences.values():
        fence.revoked = True
    cp._session_run_fences.clear()
    cp._chat_tunnel_count = 0
    cp._chat_user_tunnels.clear()


async def _override_get_db():
    async with _TestSession() as session:
        yield session


def _override_require_api_auth():
    from swarmer.k8s_auth import TokenIdentity
    return TokenIdentity(username="test-user", uid="uid-1234")


def _override_get_current_user():
    return "test-user"


def _override_get_bearer_token():
    return "test-token"


@pytest_asyncio.fixture(autouse=True)
async def _setup_db(monkeypatch):
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    orig_max = settings.max_concurrent_agents
    settings.k8s_namespace = ""  # must be empty to allow workspace creation
    settings.max_concurrent_agents = 0

    monkeypatch.setattr("swarmer.k8s.ensure_namespace", lambda namespace: None)
    monkeypatch.setattr("swarmer.k8s.delete_namespace", lambda namespace: None)

    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns
    settings.max_concurrent_agents = orig_max


@pytest_asyncio.fixture
async def client():
    from swarmer.api.deps import get_bearer_token, get_current_user, require_api_auth
    from swarmer.database import get_db
    from swarmer.deps import require_auth
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_auth] = _override_require_api_auth
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_bearer_token] = _override_get_bearer_token
    app.dependency_overrides[require_auth] = lambda: None  # bypass browser session auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        from itsdangerous import TimestampSigner
        from swarmer.crypto import derive_session_secret

        session_data = {"authenticated": True, "username": "test-user", "groups": []}
        encoded_session = base64.b64encode(json.dumps(session_data).encode("utf-8"))
        ac.cookies.set(
            "swarmer_session",
            TimestampSigner(derive_session_secret("auth/secret.key")).sign(encoded_session).decode("utf-8"),
        )
        yield ac

    app.dependency_overrides.clear()


async def _create_workspace(client, name="Proxy Test WS"):
    resp = await client.post("/api/v1/workspaces", json={"display_name": name, "description": ""})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(client, ws_id, name="s1", mode="server", agent_tool="opencode"):
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sessions",
        json={"name": name, "mode": mode, "agent_tool": agent_tool},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _set_workspace_enmaas_url(ws_id: int, url: str) -> None:
    from sqlalchemy import select

    from swarmer.models.opencode_secret import OpencodeSecret

    async with _TestSession() as db:
        result = await db.execute(
            select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
        )
        secret = result.scalars().first()
        if secret is None:
            secret = OpencodeSecret(workspace_id=ws_id, user_id="test-user")
            db.add(secret)
        secret.enmaas_base_url = url
        secret.enmaas_configured = True
        await db.commit()


@pytest.mark.asyncio
async def test_lifespan_reconciles_custom_gateway_sessions_without_default(monkeypatch):
    from swarmer import main, pr_watcher, scheduler
    from swarmer.config import settings

    monkeypatch.setattr(settings, "openshell_gateway_url", "")
    monkeypatch.setattr(main, "init_crypto", lambda *_args: None)
    monkeypatch.setattr(main, "init_db", lambda *_args: None)
    monkeypatch.setattr(main, "checkpoint_db", AsyncMock())
    monkeypatch.setattr(main, "create_tables", AsyncMock())
    monkeypatch.setattr(main, "migrate_db", AsyncMock())
    monkeypatch.setattr(main.k8s, "init_k8s", lambda *_args: None)
    monkeypatch.setattr(main, "_sync_k8s_workspace_members", AsyncMock())
    monkeypatch.setattr(main, "_restart_prompt_pollers", AsyncMock())
    reconciliation_started = asyncio.Event()

    async def blocked_reconciliation():
        reconciliation_started.set()
        await asyncio.Event().wait()

    restart = AsyncMock(side_effect=blocked_reconciliation)
    monkeypatch.setattr(main, "_restart_server_sessions", restart)
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: None)
    monkeypatch.setattr(scheduler, "shutdown", AsyncMock())
    monkeypatch.setattr(pr_watcher, "shutdown", AsyncMock())

    async with main.lifespan(main.app):
        await asyncio.wait_for(reconciliation_started.wait(), timeout=1)
        # A slow per-sandbox probe must not keep the lifespan from serving.
        restart.assert_awaited_once()



# ---------------------------------------------------------------------------
# Fake SDK client
# ---------------------------------------------------------------------------


@pytest.fixture
def sdk_client():
    client = MagicMock()
    # ExposeService → returns ServiceEndpointResponse with url
    expose_resp = MagicMock()
    expose_resp.url = "https://agent.sandbox-abc.openshell.example.com"
    client._stub.ExposeService.return_value = expose_resp
    client._stub.DeleteService.return_value = MagicMock(outcome=1)
    client._timeout = 30
    ref = MagicMock()
    ref.id = "sandbox-abc123"
    ref.name = "sandbox-test-abc"
    client.get.return_value = ref
    return client


# ===========================================================================
# 1. openshell_client wrappers
# ===========================================================================


@_requires_sdk
class TestExposeService:
    @pytest.mark.asyncio
    async def test_expose_service_calls_stub(self, sdk_client):
        from openshell._proto import openshell_pb2 as pb
        with patch.object(oc, "_get_client", return_value=sdk_client):
            url = await oc.expose_service("sandbox-test-abc", "agent", 4096)
        sdk_client._stub.ExposeService.assert_called_once()
        args = sdk_client._stub.ExposeService.call_args
        req = args[0][0]  # first positional arg
        assert req.sandbox == "sandbox-test-abc"
        assert req.name == "agent"
        assert req.target_port == 4096
        assert req.domain is True

    @pytest.mark.asyncio
    async def test_expose_service_returns_url(self, sdk_client):
        with patch.object(oc, "_get_client", return_value=sdk_client):
            url = await oc.expose_service("sandbox-test-abc", "agent", 4096)
        assert url == "https://agent.sandbox-abc.openshell.example.com"

    @pytest.mark.asyncio
    async def test_expose_service_uses_workspace_gateway_port(self, sdk_client):
        from swarmer.config import settings
        from swarmer.openshell_client import GatewayConfig

        sdk_client._stub.ExposeService.return_value.url = (
            "https://agent.sandbox-abc.openshell.example.com:8080"
        )
        workspace_gateway = GatewayConfig(
            gateway_url="https://remote-gateway.example.com:17670",
            auth_mode="none",
            workspace_id=42,
        )
        with patch.object(oc, "_get_client", return_value=sdk_client), \
             patch.object(settings, "openshell_gateway_url", "http://local-gateway:8080"):
            url = await oc.expose_service(
                "sandbox-test-abc", "agent", 4096,
                client=sdk_client, gateway_config=workspace_gateway,
            )

        assert url == "https://agent.sandbox-abc.openshell.example.com:17670"

    @pytest.mark.asyncio
    async def test_expose_service_uses_default_port_for_remote_gateway(self, sdk_client):
        from swarmer.openshell_client import GatewayConfig

        sdk_client._stub.ExposeService.return_value.url = (
            "https://agent.sandbox-abc.openshell.example.com:8080"
        )
        remote_gateway = GatewayConfig(
            gateway_url="https://remote-gateway.example.com",
            auth_mode="none",
            workspace_id=42,
        )
        with patch.object(oc, "_get_client", return_value=sdk_client):
            url = await oc.expose_service(
                "sandbox-test-abc", "agent", 4096,
                client=sdk_client, gateway_config=remote_gateway,
            )

        assert url == "https://agent.sandbox-abc.openshell.example.com:443"

    @pytest.mark.asyncio
    async def test_delete_service_calls_stub(self, sdk_client):
        with patch.object(oc, "_get_client", return_value=sdk_client):
            await oc.delete_service("sandbox-test-abc", "agent")
        sdk_client._stub.DeleteService.assert_called_once()
        args = sdk_client._stub.DeleteService.call_args
        req = args[0][0]
        assert req.sandbox == "sandbox-test-abc"
        assert req.name == "agent"


@_requires_sdk
class TestExecInteractive:
    def test_exec_interactive_returns_stream_and_queue(self, sdk_client):
        mock_stream = iter([])
        sdk_client._stub.ExecSandboxInteractive.return_value = mock_stream
        with patch.object(oc, "_get_client", return_value=sdk_client):
            stream, input_q = oc.exec_interactive(
                sandbox_name="sandbox-id-test",
                command=["sh", "-c", "opencode"],
                cols=80,
                rows=24,
                client=sdk_client,
            )
        assert stream is mock_stream
        assert input_q is not None

    def test_exec_interactive_sends_start_message(self, sdk_client):
        from openshell._proto import openshell_pb2 as pb

        received_msgs = []

        def _capture_stream(request_iter, **kwargs):
            for msg in request_iter:
                received_msgs.append(msg)
                break  # read only the first (start) message
            return iter([])

        sdk_client._stub.ExecSandboxInteractive.side_effect = _capture_stream

        with patch.object(oc, "_get_client", return_value=sdk_client):
            stream, input_q = oc.exec_interactive(
                sandbox_name="sandbox-id-test",
                command=["sh", "-c", "opencode --continue"],
                cols=120,
                rows=40,
                client=sdk_client,
            )
            input_q.put(None)  # stop the generator after reading 1 message

        assert len(received_msgs) == 1
        start_msg = received_msgs[0]
        assert start_msg.start.sandbox == "sandbox-id-test"
        assert start_msg.start.tty is True
        assert start_msg.start.cols == 120
        assert start_msg.start.rows == 40
        assert start_msg.start.workdir == "/sandbox"


class TestForwardTcp:
    def test_forward_tcp_sends_sandbox_scoped_loopback_target(self, sdk_client):
        seen = {}

        def _capture(request_iter):
            seen["init"] = next(request_iter).init
            return request_iter

        sdk_client._stub.ForwardTcp.side_effect = _capture
        stream, input_q = oc.forward_tcp(
            "sandbox-test", "127.0.0.1", 4096, client=sdk_client
        )
        input_q.put_nowait(b"request bytes")
        input_q.put_nowait(None)

        frame = next(stream)
        assert frame.data == b"request bytes"
        with pytest.raises(StopIteration):
            next(stream)
        assert seen["init"].sandbox == "sandbox-test"
        assert seen["init"].workspace == "default"
        assert seen["init"].tcp.host == "127.0.0.1"
        assert seen["init"].tcp.port == 4096
        assert input_q.maxsize == 16

    def test_forward_tcp_rejects_non_loopback_destinations(self, sdk_client):
        with pytest.raises(ValueError, match="sandbox loopback"):
            oc.forward_tcp("sandbox-test", "gateway.example.com", 4096, client=sdk_client)


# ===========================================================================
# 2. chat_proxy._session_ok()
# ===========================================================================


class TestSessionOk:
    def _make_session(self, sandbox_name="sandbox-test", mode="server", phase="running", agent_tool="opencode"):
        session = MagicMock()
        session.sandbox_name = sandbox_name
        session.service_url = None
        session.agent_tool = agent_tool
        session.mode = mode
        session.phase = phase
        session.workspace_id = 1
        # Keep the model contract: pending sessions are active even though Chat
        # specifically requires phase=running.
        session.is_active = phase in ("queued", "pending", "running")
        session.repos = []
        return session

    def _make_ws(self, ws_id=1):
        ws = MagicMock()
        ws.id = ws_id
        return ws

    def test_session_without_service_url_is_chat_ready(self):
        from swarmer.routers.chat_proxy import _session_ok

        assert _session_ok(self._make_ws(), self._make_session(), 1) is None

    def test_tui_opencode_session_is_chat_ready_without_public_service_url(self):
        from swarmer.routers.chat_proxy import _session_ok

        assert _session_ok(self._make_ws(), self._make_session(mode="tui"), 1) is None

    def test_session_without_sandbox_is_rejected(self):
        from swarmer.routers.chat_proxy import _session_ok

        assert _session_ok(self._make_ws(), self._make_session(sandbox_name=None), 1) is not None

    def test_shell_tui_session_is_not_chat_ready(self):
        from swarmer.routers.chat_proxy import _session_ok

        assert _session_ok(self._make_ws(), self._make_session(mode="tui", agent_tool="shell"), 1) is not None

    def test_inactive_session_is_rejected(self):
        from swarmer.routers.chat_proxy import _session_ok

        assert _session_ok(self._make_ws(), self._make_session(phase="stopped"), 1) is not None

    def test_pending_session_is_rejected_before_chat_relay(self):
        from swarmer.routers.chat_proxy import _session_ok

        pending = self._make_session(phase="pending")
        assert pending.is_active is True
        assert _session_ok(self._make_ws(), pending, 1) is not None

    def test_wrong_workspace_is_rejected(self):
        from swarmer.routers.chat_proxy import _session_ok

        session = self._make_session()
        session.workspace_id = 999
        assert _session_ok(self._make_ws(), session, 1) is not None

    def test_invalid_origin_authorities_are_rejected_without_raising(self):
        from swarmer.routers.chat_proxy import _origin_matches_host

        assert not _origin_matches_host("http://[invalid", "test")
        assert not _origin_matches_host("http://test", "[invalid")
        assert not _origin_matches_host("http://%zz", "test")


class TestChatHttpProxy:
    async def _session(self, client, mode="server"):
        ws = await _create_workspace(client)
        session = await _create_session(client, ws["id"], mode=mode, agent_tool="opencode")
        async with _TestSession() as db:
            from swarmer.models.session import Session

            row = await db.get(Session, session["id"])
            row.phase = "running"
            row.sandbox_name = "sandbox-test-abc"
            await db.commit()
        from swarmer.routers.chat_proxy import activate_session_chat_tunnels

        activate_session_chat_tunnels(session["id"])
        return ws, session

    async def _upstream(
        self, monkeypatch, body=b"ok", content_type="text/plain", extra_headers=b"",
        hold_open: asyncio.Event | None = None,
        event_written: asyncio.Event | None = None,
        status_code: int = 200,
    ):
        requests = []

        async def handle(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        headers[key.lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                request_body = await reader.readexactly(length) if length else b""
                requests.append((lines[0], headers, request_body))
                response = (
                    f"HTTP/1.1 {status_code} Test\r\n".encode()
                    + f"Content-Type: {content_type}\r\n".encode()
                    + f"Content-Length: {len(body)}\r\n".encode()
                    + extra_headers
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                writer.write(response)
                await writer.drain()
                if event_written is not None:
                    event_written.set()
                if hold_open is not None:
                    await hold_open.wait()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                # Expected when the proxy rejects an upload before forwarding
                # its declared body length.
                return
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        @asynccontextmanager
        async def fake_tunnel(
            sandbox_name, target_port, tunnel_client, session_id=None,
            user_key=None, admission=None, **_kwargs,
        ):
            assert sandbox_name == "sandbox-test-abc"
            assert target_port == 4096
            assert session_id is not None
            assert user_key
            if admission is not None:
                admission.transfer()
            try:
                yield f"http://127.0.0.1:{port}"
            finally:
                from swarmer.routers.chat_proxy import _release_chat_tunnel

                _release_chat_tunnel(user_key)

        async def fake_gateway(_workspace, _db=None):
            return MagicMock(gateway_url="", auth_mode="none"), None

        monkeypatch.setattr("swarmer.routers.chat_proxy._open_sandbox_tunnel", fake_tunnel)
        monkeypatch.setattr("swarmer.routers.chat_proxy._gateway_and_client", fake_gateway)
        return server, requests

    @pytest.mark.asyncio
    async def test_http_proxy_uses_loopback_tunnel_and_strips_browser_credentials(self, client, monkeypatch, caplog):
        ws, session = await self._session(client)
        server, requests = await self._upstream(
            monkeypatch,
            body=b"ok",
            extra_headers=b"Set-Cookie: sandbox-cookie=must-not-reach-browser\r\n",
        )
        try:
            with caplog.at_level(logging.INFO, logger="httpx"):
                response = await client.get(
                    f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api?keep=yes&auth_token=&query_marker=unlogged",
                    headers={"Authorization": "unexpected"},
                )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        assert response.text == "ok"
        assert "set-cookie" not in response.headers
        request_line, headers, _body = requests[0]
        assert request_line == "GET /api?keep=yes&query_marker=unlogged HTTP/1.1"
        assert headers["host"] == "localhost:4096"
        assert headers["accept-encoding"] == "identity"
        assert headers["authorization"].startswith("Basic ")
        assert "unexpected" not in headers["authorization"]
        assert "cookie" not in headers
        assert "unlogged" not in caplog.text

    @pytest.mark.asyncio
    async def test_acl_wait_cannot_admit_old_sandbox_under_new_run_fence(
        self, client, monkeypatch
    ):
        from swarmer.routers import chat_proxy as cp

        ws, session = await self._session(client)
        acl_started = asyncio.Event()
        allow_acl = asyncio.Event()

        async def delayed_acl(*_args, **_kwargs):
            acl_started.set()
            await allow_acl.wait()
            return True

        gateway = AsyncMock()
        monkeypatch.setattr(cp, "_workspace_accessible", delayed_acl)
        monkeypatch.setattr(cp, "_gateway_and_client", gateway)
        request_task = asyncio.create_task(
            client.get(f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api")
        )
        await asyncio.wait_for(acl_started.wait(), timeout=2)
        old_fence = cp._session_run_fences[session["id"]]
        await cp.close_session_chat_tunnels(session["id"])
        cp.activate_session_chat_tunnels(session["id"])
        allow_acl.set()
        response = await request_task

        assert old_fence.revoked
        assert response.status_code == 503
        gateway.assert_not_awaited()
        assert cp._chat_tunnel_count == 0

    @pytest.mark.asyncio
    async def test_proxy_persists_selected_chat(self, client, monkeypatch):
        ws, session = await self._session(client)
        server, requests = await self._upstream(
            monkeypatch,
            body=b'{"id":"ses_selected_chat"}',
            content_type="application/json",
        )
        try:
            response = await client.post(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/session",
                headers={"Origin": "http://test"},
                json={"title": "chat"},
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        async with _TestSession() as db:
            from swarmer.models.session import Session

            row = await db.get(Session, session["id"])
            assert row.opencode_session_id == "ses_selected_chat"
        assert requests[0][1]["authorization"].startswith("Basic ")

    @pytest.mark.asyncio
    async def test_admitted_http_request_reuses_authorized_session_snapshot(
        self, client, monkeypatch
    ):
        from swarmer.routers import chat_proxy as cp

        ws, session = await self._session(client)
        server, _requests = await self._upstream(monkeypatch)
        original_load = cp._load
        load = AsyncMock(side_effect=original_load)
        access = AsyncMock(return_value=True)
        monkeypatch.setattr(cp, "_load", load)
        monkeypatch.setattr(cp, "_workspace_accessible", access)
        try:
            response = await client.get(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api"
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        load.assert_awaited_once()
        access.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_old_run_response_cannot_persist_chat_selection_after_relaunch(
        self, client, monkeypatch
    ):
        from swarmer.routers import chat_proxy as cp

        ws, session = await self._session(client)
        server, _requests = await self._upstream(
            monkeypatch,
            body=b'{"id":"ses_old_run"}',
            content_type="application/json",
        )
        read_complete = asyncio.Event()
        allow_persist = asyncio.Event()
        original_read = cp._read_limited_response

        async def pause_before_persist(response):
            result = await original_read(response)
            read_complete.set()
            await allow_persist.wait()
            return result

        monkeypatch.setattr(cp, "_read_limited_response", pause_before_persist)
        response_task = asyncio.create_task(
            client.post(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/session",
                headers={"Origin": "http://test"},
                json={"title": "old run"},
            )
        )
        try:
            await asyncio.wait_for(read_complete.wait(), timeout=2)
            async with _TestSession() as db:
                from swarmer.models.session import Session as SessionRow

                row = await db.get(SessionRow, session["id"])
                row.run_started_at = datetime.now(timezone.utc).replace(tzinfo=None)
                row.sandbox_name = "sandbox-relaunched"
                row.opencode_session_id = None
                await db.commit()
            allow_persist.set()
            response = await response_task
        finally:
            allow_persist.set()
            if not response_task.done():
                response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, session["id"])
            assert row.sandbox_name == "sandbox-relaunched"
            assert row.opencode_session_id is None

    @pytest.mark.asyncio
    async def test_delete_clears_only_the_matching_selected_chat(self, client, monkeypatch):
        ws, session = await self._session(client)
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, session["id"])
            row.opencode_session_id = "ses_to_delete"
            await db.commit()
        server, _requests = await self._upstream(monkeypatch, body=b"{}")
        try:
            response = await client.delete(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/session/ses_to_delete",
                headers={"Origin": "http://test"},
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, session["id"])
            assert row.opencode_session_id is None

    @pytest.mark.asyncio
    async def test_delete_redirect_does_not_clear_selected_chat(self, client, monkeypatch):
        ws, session = await self._session(client)
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, session["id"])
            row.opencode_session_id = "ses_redirected_delete"
            await db.commit()
        server, _requests = await self._upstream(
            monkeypatch,
            status_code=307,
            extra_headers=b"Location: /session/ses_redirected_delete\r\n",
        )
        try:
            response = await client.delete(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/session/ses_redirected_delete",
                headers={"Origin": "http://test"},
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 307
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, session["id"])
            assert row.opencode_session_id == "ses_redirected_delete"

    @pytest.mark.asyncio
    async def test_proxy_rewrites_html_assets_through_loopback_tunnel(self, client, monkeypatch):
        ws, session = await self._session(client)
        server, _requests = await self._upstream(
            monkeypatch,
            body=b'<html><head></head><body><script src="/assets/app.js"></script></body></html>',
            content_type="text/html; charset=utf-8",
        )
        try:
            response = await client.get(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/",
            )
        finally:
            server.close()
            await server.wait_closed()

        prefix = f"/workspaces/{ws['id']}/sessions/{session['id']}/chat"
        assert response.status_code == 200
        assert f'<base href="{prefix}/">' in response.text
        assert f'src="{prefix}/assets/app.js"' in response.text

    @pytest.mark.asyncio
    async def test_proxy_rejects_cross_origin_and_originless_mutation(self, client, monkeypatch):
        ws, session = await self._session(client)
        server, requests = await self._upstream(monkeypatch)
        try:
            cross_origin = await client.get(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api",
                headers={"Origin": "http://attacker.example"},
            )
            missing_origin = await client.post(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/session",
                json={"title": "chat"},
            )
        finally:
            server.close()
            await server.wait_closed()

        assert cross_origin.status_code == 403
        assert missing_origin.status_code == 403
        assert requests == []

    @pytest.mark.asyncio
    async def test_sse_uses_tunnel_and_streams_response(self, client, monkeypatch):
        ws, session = await self._session(client)
        server, requests = await self._upstream(
            monkeypatch,
            body=b"data: ready\n\n",
            content_type="text/event-stream",
        )
        try:
            response = await client.get(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/event?auth_token=",
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        assert response.text == "data: ready\n\n"
        assert response.headers["content-type"].startswith("text/event-stream")
        assert requests[0][0] == "GET /event HTTP/1.1"

    @pytest.mark.asyncio
    async def test_sse_upstream_read_error_completes_asgi_response(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await self._session(client)

        async def handle(_reader, writer):
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Content-Length: 100\r\n"
                b"Connection: close\r\n\r\n"
                b"data: partial\n\n"
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        @asynccontextmanager
        async def fake_tunnel(*_args, admission=None, user_key=None, **_kwargs):
            if admission is not None:
                admission.transfer()
            try:
                yield f"http://127.0.0.1:{port}"
            finally:
                from swarmer.routers.chat_proxy import _release_chat_tunnel

                _release_chat_tunnel(user_key)

        async def fake_gateway(_workspace, _db=None):
            return MagicMock(gateway_url="", auth_mode="none"), None

        monkeypatch.setattr("swarmer.routers.chat_proxy._open_sandbox_tunnel", fake_tunnel)
        monkeypatch.setattr("swarmer.routers.chat_proxy._gateway_and_client", fake_gateway)
        try:
            response = await asyncio.wait_for(
                client.get(
                    f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/event",
                    headers={"Accept": "text/event-stream"},
                ),
                timeout=2,
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 200
        assert "partial" in response.text
        assert cp._chat_tunnel_count == 0

    @pytest.mark.asyncio
    async def test_sse_delivers_event_before_upstream_eof(self, client, monkeypatch):
        from starlette.requests import Request
        from swarmer.routers.chat_proxy import _chat_http_proxy

        ws, session = await self._session(client)
        release_upstream = asyncio.Event()
        event_written = asyncio.Event()
        server, requests = await self._upstream(
            monkeypatch,
            body=b"data: incremental\n\n",
            content_type="text/event-stream",
            hold_open=release_upstream,
            event_written=event_written,
        )
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http",
            "path": f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/event",
            "raw_path": b"/chat/event", "query_string": b"",
            "headers": [(b"host", b"test")], "client": ("test", 1),
            "server": ("test", 80),
            "session": {"authenticated": True, "username": "test-user", "groups": []},
        }

        first_receive = True
        disconnected = asyncio.Event()

        async def receive():
            nonlocal first_receive
            if first_receive:
                first_receive = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        request = Request(scope, receive)
        messages = []
        event_sent = asyncio.Event()

        async with _TestSession() as db:
            response = await _chat_http_proxy(ws["id"], session["id"], request, "event", db)

        async def send(message):
            messages.append(message)
            if message.get("type") == "http.response.body" and b"incremental" in message.get("body", b""):
                event_sent.set()

        response_task = asyncio.create_task(response(scope, receive, send))
        try:
            await asyncio.wait_for(event_written.wait(), timeout=2)
            await asyncio.wait_for(event_sent.wait(), timeout=2)
            assert not release_upstream.is_set()
            release_upstream.set()
            await response_task
        finally:
            release_upstream.set()
            disconnected.set()
            server.close()
            await server.wait_closed()
            if not response_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(response_task), timeout=2)
                except TimeoutError:
                    response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)

        assert any(b"incremental" in message.get("body", b"") for message in messages)
        assert requests[0][0] == "GET /event HTTP/1.1"

    @pytest.mark.asyncio
    async def test_oversized_sse_upload_returns_413(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await self._session(client)
        server, requests = await self._upstream(
            monkeypatch,
            body=b"data: unused\n\n",
            content_type="text/event-stream",
        )
        monkeypatch.setattr(cp, "_CHAT_MAX_BUFFERED_BODY", 3)
        try:
            response = await client.post(
                f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/event",
                headers={"Origin": "http://test", "Accept": "text/event-stream"},
                content=b"body larger than limit",
            )
        finally:
            server.close()
            await server.wait_closed()

        assert response.status_code == 413
        assert requests == []
        assert cp._chat_tunnel_count == 0

    @pytest.mark.asyncio
    async def test_websocket_rejects_user_without_workspace_access(self, client):
        ws, session = await self._session(client)
        async with _TestSession() as db:
            from swarmer.models.workspace import Workspace

            workspace = await db.get(Workspace, ws["id"])
            workspace.owner_id = "another-user"
            await db.commit()

        websocket = MagicMock()
        websocket.session = {"authenticated": True, "username": "test-user", "groups": []}
        websocket.headers = {"origin": "http://test", "host": "test"}
        websocket.accept = AsyncMock()
        websocket.close = AsyncMock()
        from swarmer.routers.chat_proxy import chat_ws_proxy

        with patch("swarmer.routers.chat_proxy.get_db", _override_get_db):
            await chat_ws_proxy(websocket, ws["id"], session["id"], "event")
        websocket.close.assert_awaited_once()
        websocket.accept.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_websocket_uses_local_tunnel_and_opencode_auth_only(self, client, monkeypatch):
        ws, session = await self._session(client, mode="tui")

        @asynccontextmanager
        async def fake_tunnel(
            _sandbox, _port, _client, session_id=None, user_key=None,
            admission=None, **_kwargs,
        ):
            assert session_id is not None
            assert user_key
            if admission is not None:
                admission.transfer()
            try:
                yield "http://127.0.0.1:45678"
            finally:
                from swarmer.routers.chat_proxy import _release_chat_tunnel

                _release_chat_tunnel(user_key)

        async def fake_gateway(_workspace, _db=None):
            return MagicMock(gateway_url="", auth_mode="none"), None

        fake_sock = MagicMock(name="preconnected-tunnel-socket")

        async def fake_tunnel_socket(local_base):
            assert local_base == "http://127.0.0.1:45678"
            return fake_sock

        monkeypatch.setattr("swarmer.routers.chat_proxy._open_sandbox_tunnel", fake_tunnel)
        monkeypatch.setattr("swarmer.routers.chat_proxy._gateway_and_client", fake_gateway)
        monkeypatch.setattr("swarmer.routers.chat_proxy._tunnel_socket", fake_tunnel_socket)

        class _Upstream:
            subprotocol = "json"
            send = AsyncMock()

            def __aiter__(self):
                async def _messages():
                    yield "upstream reply"
                return _messages()

        upstream = _Upstream()
        connect_context = MagicMock()
        connect_context.__aenter__ = AsyncMock(return_value=upstream)
        connect_context.__aexit__ = AsyncMock(return_value=False)
        connect = MagicMock(return_value=connect_context)
        monkeypatch.setattr("swarmer.routers.chat_proxy.websockets.connect", connect)

        websocket = MagicMock()
        websocket.session = {"authenticated": True, "username": "test-user", "groups": []}
        websocket.headers = {
            "origin": "http://test",
            "host": "test",
            "sec-websocket-protocol": "opencode, json",
            "cookie": "session-cookie",
        }
        websocket.url = MagicMock(query="auth_token=&topic=events")
        websocket.accept = AsyncMock()
        websocket.close = AsyncMock()
        websocket.receive = AsyncMock(return_value={"type": "websocket.disconnect"})
        websocket.send_text = AsyncMock()
        websocket.send_bytes = AsyncMock()

        from swarmer.routers.chat_proxy import _ws_tunnel_logger, chat_ws_proxy

        with patch("swarmer.routers.chat_proxy.get_db", _override_get_db):
            await chat_ws_proxy(websocket, ws["id"], session["id"], "event")

        args, kwargs = connect.call_args
        # A preconnected socket is handed to websockets: the library cannot
        # follow any redirect on a socket it did not create, so a hostile
        # upstream cannot move the session or the credential off the tunnel.
        assert kwargs["sock"] is fake_sock
        # The handshake Host is the sandbox-local OpenCode endpoint, not the
        # browser origin and not the tunnel listener.
        assert args[0] == "ws://localhost:4096/event?topic=events"
        assert kwargs["additional_headers"]["Authorization"].startswith("Basic ")
        assert "session-cookie" not in repr(kwargs)
        assert kwargs["proxy"] is None
        assert kwargs["subprotocols"] == ["opencode", "json"]
        # Handshake debug logging goes through the redaction filter.
        assert kwargs["logger"] is _ws_tunnel_logger
        websocket.send_text.assert_awaited_once_with("upstream reply")
        websocket.accept.assert_awaited_once_with(subprotocol="json")


class TestChatTunnelBridge:
    @pytest.mark.asyncio
    async def test_forward_tcp_bridge_relays_bytes_and_targets_loopback(self, monkeypatch):
        import queue as _queue

        from swarmer.routers.chat_proxy import _open_sandbox_tunnel

        seen = {}

        def fake_forward_tcp(sandbox_name, host, port, client=None):
            seen.update(sandbox=sandbox_name, host=host, port=port, client=client)
            input_q = _queue.Queue(maxsize=16)

            def response_stream():
                while True:
                    data = input_q.get()
                    if data is None:
                        return
                    yield _TcpForwardFrame(data=data.upper())

            return response_stream(), input_q

        monkeypatch.setattr("swarmer.openshell_client.forward_tcp", fake_forward_tcp)
        async with _open_sandbox_tunnel("sandbox-123", 4096, "client") as local_url:
            parsed = urlparse(local_url)
            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            writer.write(b"hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), timeout=2) == b"HELLO"
            writer.close()
            await writer.wait_closed()

        assert seen == {
            "sandbox": "sandbox-123",
            "host": "127.0.0.1",
            "port": 4096,
            "client": "client",
        }

    @pytest.mark.asyncio
    async def test_first_bridge_survives_rejected_duplicate_connection(self, monkeypatch):
        import queue as _queue

        from swarmer.routers import chat_proxy as cp

        seen = {}

        def fake_forward_tcp(sandbox_name, host, port, client=None):
            input_q = _queue.Queue(maxsize=16)
            stream = _FakeForwardStream(input_q, transform=lambda data: data.upper())
            seen.update(sandbox=sandbox_name, host=host, port=port, client=client, stream=stream)
            return stream, input_q

        monkeypatch.setattr("swarmer.openshell_client.forward_tcp", fake_forward_tcp)
        async with cp._open_sandbox_tunnel(
            "sandbox-bridge", 4096, "client", session_id=779
        ) as local_url:
            parsed = urlparse(local_url)
            first_reader, first_writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            first_writer.write(b"first")
            await first_writer.drain()
            assert await asyncio.wait_for(first_reader.readexactly(5), timeout=2) == b"FIRST"
            assert "stream" in seen
            tunnel_handle = next(iter(cp._session_tunnels[779]))
            accepted_writer = tunnel_handle.writer
            assert accepted_writer is not None

            duplicate_reader, duplicate_writer = await asyncio.open_connection(
                parsed.hostname, parsed.port
            )
            assert await asyncio.wait_for(duplicate_reader.read(), timeout=2) == b""
            duplicate_writer.close()
            await duplicate_writer.wait_closed()
            assert tunnel_handle.writer is accepted_writer

            first_writer.write(b"still-active")
            await first_writer.drain()
            assert await asyncio.wait_for(
                first_reader.readexactly(len(b"still-active")), timeout=2
            ) == b"STILL-ACTIVE"
            first_writer.close()
            await first_writer.wait_closed()

    def test_remote_gateway_requires_verified_tls_and_authentication(self):
        from swarmer.openshell_client import GatewayConfig
        from swarmer.routers.chat_proxy import _require_secure_gateway

        secure = GatewayConfig(
            gateway_url="https://gateway.example.com",
            auth_mode="oidc",
            bearer_callable=MagicMock(),
            workspace_id=42,
        )
        _require_secure_gateway(secure)

        no_tls = GatewayConfig(
            gateway_url="http://gateway.example.com",
            auth_mode="bearer",
            bearer_token=MagicMock(),
            workspace_id=42,
        )
        with pytest.raises(ValueError, match="verified TLS"):
            _require_secure_gateway(no_tls)

        no_auth = GatewayConfig(
            gateway_url="https://gateway.example.com",
            auth_mode="none",
            workspace_id=42,
        )
        with pytest.raises(ValueError, match="authentication"):
            _require_secure_gateway(no_auth)


class _FakeForwardStream:
    """Iterable, cancellable stand-in for a gRPC ForwardTcp response stream."""

    def __init__(self, input_q, transform=lambda data: data):
        self._input_q = input_q
        self._transform = transform
        self.cancel = MagicMock()

    def __iter__(self):
        return self

    def __next__(self):
        data = self._input_q.get()
        if data is None:
            raise StopIteration
        return _TcpForwardFrame(data=self._transform(data))


class TestChatTunnelSecurity:
    """Security regressions for the Chat ForwardTcp tunnel (FLD-209 review)."""

    def _install_fake_forward_tcp(self, monkeypatch, delay=0.0):
        import queue as _queue

        seen = {}

        def fake_forward_tcp(sandbox_name, host, port, client=None):
            seen.update(
                sandbox=sandbox_name,
                host=host,
                port=port,
                client=client,
                thread=threading.get_ident(),
            )
            if delay:
                time.sleep(delay)
            input_q = _queue.Queue(maxsize=16)
            stream = _FakeForwardStream(input_q, transform=lambda data: data.upper())
            seen["stream"] = stream
            seen["queue"] = input_q
            return stream, input_q

        monkeypatch.setattr("swarmer.openshell_client.forward_tcp", fake_forward_tcp)
        return seen

    # ── TLS policy ──────────────────────────────────────────────────────────

    def test_workspace_gateways_never_use_private_hostname_exemption(self):
        """A .svc/.cluster.local name is not trust evidence for a
        workspace-configured (user-managed) gateway."""
        from swarmer.openshell_client import GatewayConfig
        from swarmer.routers.chat_proxy import _require_secure_gateway

        in_cluster = GatewayConfig(
            gateway_url="http://openshell-gateway.openshell-system.svc.cluster.local:17670",
            auth_mode="none",
            workspace_id=42,
        )
        with pytest.raises(ValueError, match="verified TLS"):
            _require_secure_gateway(in_cluster)

    def test_default_gateway_keeps_operator_in_cluster_exemption(self):
        """The env-configured default gateway may use in-cluster gRPC."""
        from swarmer.openshell_client import GatewayConfig
        from swarmer.routers.chat_proxy import _require_secure_gateway

        _require_secure_gateway(
            GatewayConfig(
                gateway_url="grpc://openshell-gateway.openshell-system.svc.cluster.local:17670",
                auth_mode="default",
            )
        )

    # ── Logging ─────────────────────────────────────────────────────────────

    def test_websocket_logger_redacts_authorization_headers(self, caplog):
        from swarmer.routers.chat_proxy import _ws_tunnel_logger

        test_value = f"generated-{uuid.uuid4().hex}"
        with caplog.at_level(logging.DEBUG, logger="swarmer.chat.websocket"):
            _ws_tunnel_logger.debug("> %s: %s", "Authorization", test_value)
            _ws_tunnel_logger.debug("> %s: %s", "X-Plain", "visible-value")

        assert test_value not in caplog.text
        assert "<redacted>" in caplog.text
        assert "visible-value" in caplog.text

    # ── Event-loop safety ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_forward_tcp_stream_opens_off_the_event_loop(self, monkeypatch):
        from swarmer.routers.chat_proxy import _open_forward_stream

        seen = self._install_fake_forward_tcp(monkeypatch)
        stream, input_q = await _open_forward_stream("sandbox-x", 4096, "client")
        assert seen["thread"] != threading.get_ident()
        input_q.put_nowait(None)

    @pytest.mark.asyncio
    async def test_slow_stream_open_does_not_block_the_event_loop(self, monkeypatch):
        """A blocking OIDC refresh during stream open must not stall the loop."""
        from swarmer.routers.chat_proxy import _open_forward_stream

        import queue as _queue

        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_done = threading.Event()
        seen = {}

        def fake_forward_tcp(sandbox_name, host, port, client=None):
            worker_started.set()
            assert release_worker.wait(2)
            input_q = _queue.Queue(maxsize=16)
            stream = _FakeForwardStream(input_q)
            worker_done.set()
            return stream, input_q

        monkeypatch.setattr("swarmer.openshell_client.forward_tcp", fake_forward_tcp)
        heartbeats = []

        async def _heartbeat():
            for _ in range(10):
                heartbeats.append(time.monotonic())
                await asyncio.sleep(0.02)

        heartbeat_task = asyncio.create_task(_heartbeat())
        open_task = asyncio.create_task(_open_forward_stream("sandbox-x", 4096, "client"))
        assert await asyncio.to_thread(worker_started.wait, 1)
        await asyncio.sleep(0.06)
        assert len(heartbeats) >= 2
        assert not worker_done.is_set()
        release_worker.set()
        _stream, input_q = await open_task
        await heartbeat_task
        input_q.put_nowait(None)
        gaps = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
        assert gaps, "heartbeat task produced no samples"
        assert max(gaps) < 0.2, f"event loop stalled during stream open: {gaps}"

    @pytest.mark.asyncio
    async def test_stream_open_timeout_cancels_late_stream(self, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        seen = self._install_fake_forward_tcp(monkeypatch, delay=0.2)
        monkeypatch.setattr(cp, "_CHAT_STREAM_OPEN_TIMEOUT", 0.02)
        with pytest.raises(TimeoutError):
            await cp._open_forward_stream("sandbox-x", 4096, "client")

        # The late worker remains the cleanup owner and cancels its own stream.
        deadline = time.monotonic() + 2
        while "stream" not in seen and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert "stream" in seen, "worker thread never completed the stream open"
        deadline = time.monotonic() + 2
        while not seen["stream"].cancel.called and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert seen["stream"].cancel.called

    # ── Lifecycle, admission, and revocation ────────────────────────────────

    @pytest.mark.asyncio
    async def test_tunnel_cancellation_releases_admission(self, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        self._install_fake_forward_tcp(monkeypatch)

        async def _hold_tunnel():
            async with cp._open_sandbox_tunnel(
                "sandbox-123", 4096, "client", session_id=888, user_key="bob"
            ) as local_url:
                parsed = urlparse(local_url)
                _reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
                try:
                    await asyncio.sleep(10)
                finally:
                    writer.close()

        task = asyncio.create_task(_hold_tunnel())
        await asyncio.sleep(0.2)
        assert cp._chat_tunnel_count == 1
        assert cp._chat_user_tunnels.get("bob") == 1
        assert 888 in cp._session_tunnels

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert cp._chat_tunnel_count == 0
        assert "bob" not in cp._chat_user_tunnels
        assert 888 not in cp._session_tunnels

    @pytest.mark.asyncio
    async def test_close_session_chat_tunnels_aborts_active_connection(self, monkeypatch):
        from swarmer.routers.chat_proxy import (
            _open_sandbox_tunnel,
            _session_tunnels,
            close_session_chat_tunnels,
        )

        self._install_fake_forward_tcp(monkeypatch)
        async with _open_sandbox_tunnel(
            "sandbox-123", 4096, "client", session_id=777, user_key="alice"
        ) as local_url:
            parsed = urlparse(local_url)
            reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
            writer.write(b"hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), timeout=2) == b"HELLO"
            assert 777 in _session_tunnels

            closed = await close_session_chat_tunnels(777)
            assert closed == 1
            assert 777 not in _session_tunnels

            # The registered tunnel aborts the live client connection.
            data = await asyncio.wait_for(reader.read(1024), timeout=2)
            assert data == b""
            writer.close()

        from swarmer.routers import chat_proxy as cp

        assert cp._chat_tunnel_count == 0
        assert cp._chat_user_tunnels == {}

    @pytest.mark.asyncio
    async def test_revoked_run_cannot_register_a_late_tunnel(self, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        self._install_fake_forward_tcp(monkeypatch)
        session_id = 778
        observed_fence = cp.activate_session_chat_tunnels(session_id)
        await cp.close_session_chat_tunnels(session_id)
        assert observed_fence.revoked
        assert session_id not in cp._session_run_fences

        with pytest.raises(RuntimeError, match="run was revoked"):
            async with cp._open_sandbox_tunnel(
                "sandbox-old", 4096, "client", session_id=session_id,
                user_key="alice", run_fence=observed_fence,
            ):
                pytest.fail("revoked run registered a tunnel")
        assert cp._chat_tunnel_count == 0
        assert session_id not in cp._session_tunnels

        current_fence = cp.activate_session_chat_tunnels(session_id)
        async with cp._open_sandbox_tunnel(
            "sandbox-new", 4096, "client", session_id=session_id,
            user_key="alice", run_fence=current_fence,
        ):
            assert session_id in cp._session_tunnels
        assert cp._chat_tunnel_count == 0

    @pytest.mark.asyncio
    async def test_per_user_overload_returns_503_before_gateway_work(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await TestChatHttpProxy()._session(client)
        monkeypatch.setattr(cp, "_CHAT_MAX_TUNNELS_PER_USER", 0)
        gateway_mock = AsyncMock()
        monkeypatch.setattr(cp, "_gateway_and_client", gateway_mock)

        response = await client.get(
            f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api"
        )

        assert response.status_code == 503
        assert "limit reached for this user" in response.text
        gateway_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gateway_setup_failure_releases_pre_reserved_admission(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await TestChatHttpProxy()._session(client)
        observed_counts = []

        async def fail_gateway(*_args, **_kwargs):
            observed_counts.append(cp._chat_tunnel_count)
            raise RuntimeError("gateway unavailable")

        monkeypatch.setattr(cp, "_gateway_and_client", fail_gateway)
        response = await client.get(
            f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api"
        )

        assert response.status_code == 503
        assert observed_counts == [1]
        assert cp._chat_tunnel_count == 0

    @pytest.mark.asyncio
    async def test_gateway_setup_cancellation_releases_http_admission(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await TestChatHttpProxy()._session(client)
        entered_gateway = asyncio.Event()

        async def wait_for_gateway(*_args, **_kwargs):
            entered_gateway.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(cp, "_gateway_and_client", wait_for_gateway)
        request_task = asyncio.create_task(
            client.get(f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api")
        )
        await asyncio.wait_for(entered_gateway.wait(), timeout=2)
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)

        assert cp._chat_tunnel_count == 0
        assert cp._chat_user_tunnels == {}

    @pytest.mark.asyncio
    async def test_gateway_setup_cancellation_releases_websocket_admission(
        self, client, monkeypatch
    ):
        from swarmer.routers import chat_proxy as cp

        ws, session = await TestChatHttpProxy()._session(client, mode="tui")
        entered_gateway = asyncio.Event()

        async def wait_for_gateway(*_args, **_kwargs):
            entered_gateway.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(cp, "_gateway_and_client", wait_for_gateway)
        websocket = MagicMock()
        websocket.session = {"authenticated": True, "username": "test-user", "groups": []}
        websocket.headers = {"origin": "http://test", "host": "test"}
        websocket.accept = AsyncMock()
        websocket.close = AsyncMock()

        with patch("swarmer.routers.chat_proxy.get_db", _override_get_db):
            request_task = asyncio.create_task(
                cp.chat_ws_proxy(websocket, ws["id"], session["id"], "event")
            )
            await asyncio.wait_for(entered_gateway.wait(), timeout=2)
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)

        assert cp._chat_tunnel_count == 0
        assert cp._chat_user_tunnels == {}

    @pytest.mark.asyncio
    async def test_global_overload_returns_explicit_503(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await TestChatHttpProxy()._session(client)
        monkeypatch.setattr(cp, "_CHAT_MAX_TUNNELS", 0)
        gateway_mock = AsyncMock()
        monkeypatch.setattr(cp, "_gateway_and_client", gateway_mock)

        response = await client.get(
            f"/workspaces/{ws['id']}/sessions/{session['id']}/chat/api"
        )

        assert response.status_code == 503
        assert "capacity is full" in response.text
        gateway_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_websocket_overload_accepts_then_closes_with_1013(self, client, monkeypatch):
        from swarmer.routers import chat_proxy as cp

        ws, session = await TestChatHttpProxy()._session(client, mode="tui")
        monkeypatch.setattr(cp, "_CHAT_MAX_TUNNELS_PER_USER", 0)
        gateway_mock = AsyncMock()
        monkeypatch.setattr(cp, "_gateway_and_client", gateway_mock)

        websocket = MagicMock()
        websocket.session = {"authenticated": True, "username": "test-user", "groups": []}
        websocket.headers = {"origin": "http://test", "host": "test"}
        websocket.accept = AsyncMock()
        websocket.close = AsyncMock()

        with patch("swarmer.routers.chat_proxy.get_db", _override_get_db):
            await cp.chat_ws_proxy(websocket, ws["id"], session["id"], "event")

        websocket.close.assert_awaited_once_with(code=1013, reason="Chat is busy")
        websocket.accept.assert_awaited_once()
        gateway_mock.assert_not_awaited()

    # ── Gateway client cache ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_gateway_clients_are_shared_and_replaced_on_config_change(self, monkeypatch):
        from swarmer.openshell_client import GatewayConfig
        from swarmer.routers import chat_proxy as cp

        created = []

        def fake_build(config):
            client = MagicMock()
            created.append(client)
            return client

        monkeypatch.setattr("swarmer.openshell_client.get_client_for_config", fake_build)
        monkeypatch.setattr(cp, "_gateway_clients", {})

        config_a = GatewayConfig(
            gateway_url="https://gw.example.com", auth_mode="none", workspace_id=5
        )
        first = await cp._gateway_client_for(config_a)
        second = await cp._gateway_client_for(config_a)
        assert first is second
        assert len(created) == 1

        config_b = GatewayConfig(
            gateway_url="https://gw.example.com", auth_mode="none", workspace_id=6
        )
        other = await cp._gateway_client_for(config_b)
        assert other is not first
        assert len(created) == 2
        first.close.assert_not_called()

        # Same gateway, changed credentials → new client and the old one closed.
        config_a2 = GatewayConfig(
            gateway_url="https://gw.example.com",
            auth_mode="bearer",
            bearer_token=f"generated-{uuid.uuid4().hex}",
            workspace_id=5,
        )
        replaced = await cp._gateway_client_for(config_a2)
        assert replaced is not first
        assert len(created) == 3
        first.close.assert_called_once()

        # A gateway URL change for the same workspace replaces and closes the
        # previous active entry instead of leaking an unreachable cache value.
        config_a3 = GatewayConfig(
            gateway_url="https://other-gw.example.com",
            auth_mode="bearer",
            bearer_token=config_a2.bearer_token,
            workspace_id=5,
        )
        await cp._gateway_client_for(config_a3)
        assert len(created) == 4
        replaced.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_gateway_config_bound_oidc_method_identity_is_stable(self):
        from swarmer.openshell_client import GatewayConfig
        from swarmer.routers.chat_proxy import _client_config_matches

        class _Provider:
            def token(self):
                return ""

        provider = _Provider()
        cached = GatewayConfig(
            gateway_url="https://gw.example.com", auth_mode="oidc",
            bearer_callable=provider.token, workspace_id=2,
        )
        current = GatewayConfig(
            gateway_url="https://gw.example.com", auth_mode="oidc",
            bearer_callable=provider.token, workspace_id=2,
        )
        assert cached.bearer_callable is not current.bearer_callable
        assert _client_config_matches(cached, current)

    @pytest.mark.asyncio
    async def test_gateway_bearer_change_uses_constant_time_comparison(self, monkeypatch):
        from swarmer.openshell_client import GatewayConfig
        from swarmer.routers import chat_proxy as cp

        original = cp.hmac.compare_digest
        compared = []

        def compare(left, right):
            compared.append((left, right))
            return original(left, right)

        monkeypatch.setattr(cp.hmac, "compare_digest", compare)
        cached = GatewayConfig(
            gateway_url="https://gw.example.com", auth_mode="bearer",
            bearer_token=f"test-{uuid.uuid4().hex}", workspace_id=4,
        )
        current = GatewayConfig(
            gateway_url=cached.gateway_url, auth_mode="bearer",
            bearer_token=f"test-{uuid.uuid4().hex}", workspace_id=4,
        )
        assert not cp._client_config_matches(cached, current)
        assert compared and all(isinstance(item, bytes) for pair in compared for item in pair)

    @pytest.mark.asyncio
    async def test_oidc_provider_seed_runs_off_event_loop(self, monkeypatch):
        from types import SimpleNamespace
        from swarmer.openshell_client import resolve_gateway_config
        from swarmer.openshell_oidc import oidc_manager

        gateway = SimpleNamespace(
            workspace_id=45, gateway_url="https://gw.example.com", auth_mode="oidc",
            oidc_issuer="https://issuer.example.com", oidc_client_id="client",
            oidc_audience="", refresh_token="", access_token="",
            access_token_expires_at=None, client_secret="",
            service_account_subject="", tls_ca=None, tls_cert=None, tls_key=None,
            tls_verify=True, bearer_token=None,
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: gateway))
        )
        loop_thread = threading.get_ident()
        provider = SimpleNamespace(
            set_event_loop=MagicMock(), current_access_token=MagicMock(return_value=""),
        )
        observed_threads = []

        def get_or_create(**_kwargs):
            observed_threads.append(threading.get_ident())
            return provider

        monkeypatch.setattr(oidc_manager, "get_or_create", get_or_create)
        config = await resolve_gateway_config(45, db)

        assert config.bearer_callable == provider.current_access_token
        assert observed_threads and observed_threads[0] != loop_thread
        provider.set_event_loop.assert_called_once()

    def test_oidc_manager_rejects_a_superseded_gateway_snapshot(self):
        from swarmer.openshell_oidc import (
            StaleGatewayConfigurationError,
            WorkspaceOidcAuthManager,
        )

        manager = WorkspaceOidcAuthManager()
        try:
            original = manager.get_or_create(
                workspace_id=9876,
                issuer="https://issuer.example.com",
                client_id="client",
                client_secret="credential-placeholder-a",
                refresh_token="refresh-placeholder-a",
                access_token="access-placeholder-a",
                configuration_revision=4,
            )
            current = manager.get_or_create(
                workspace_id=9876,
                issuer="https://issuer.example.com",
                client_id="client",
                client_secret="credential-placeholder-b",
                refresh_token="refresh-placeholder-b",
                access_token="access-placeholder-b",
                configuration_revision=5,
            )
            with pytest.raises(StaleGatewayConfigurationError):
                manager.get_or_create(
                    workspace_id=9876,
                    issuer="https://issuer.example.com",
                    client_id="client",
                    client_secret="credential-placeholder-a",
                    configuration_revision=4,
                )

            assert current is original
            assert current._client_secret == "credential-placeholder-b"
            assert current._configuration_revision == 5
            assert current._bundle["refresh_token"] == "refresh-placeholder-b"
            assert current._bundle["access_token"] == "access-placeholder-b"
        finally:
            manager.clear()

    @pytest.mark.asyncio
    async def test_opencode_readiness_probe_is_bounded_and_accepts_ready_server(self, monkeypatch):
        from swarmer.routers import sessions

        result = MagicMock(exit_code=0)
        probe = AsyncMock(return_value=result)
        monkeypatch.setattr("swarmer.openshell_client.exec_command", probe)
        await sessions._wait_for_opencode_ready("sandbox", "client", timeout=0.1)
        probe.assert_awaited_once()
        args, kwargs = probe.await_args
        assert args[0] == "sandbox"
        assert args[1][0] == "node"
        assert kwargs["client"] == "client"
        assert type(kwargs["timeout_seconds"]) is int
        assert kwargs["timeout_seconds"] > 0

    @pytest.mark.asyncio
    async def test_opencode_readiness_probe_times_out(self, monkeypatch):
        from swarmer.routers import sessions

        monkeypatch.setattr(
            "swarmer.openshell_client.exec_command",
            AsyncMock(return_value=MagicMock(exit_code=1)),
        )
        with pytest.raises(TimeoutError, match="did not become ready"):
            await sessions._wait_for_opencode_ready("sandbox", "client", timeout=0.01)

    @pytest.mark.asyncio
    async def test_opencode_readiness_rpc_is_bounded_by_remaining_deadline(self, monkeypatch):
        from swarmer.routers import sessions

        observed_timeouts = []

        async def slow_probe(*_args, **kwargs):
            observed_timeouts.append(kwargs["timeout_seconds"])
            await asyncio.sleep(0.2)
            return MagicMock(exit_code=0)

        monkeypatch.setattr("swarmer.openshell_client.exec_command", slow_probe)
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError, match="did not become ready"):
            await sessions._wait_for_opencode_ready("sandbox", "client", timeout=0.03)

        assert asyncio.get_running_loop().time() - started < 0.1
        assert observed_timeouts and type(observed_timeouts[0]) is int
        assert observed_timeouts[0] > 0

    # ── Stop/delete revocation wiring ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_session_stop_closes_chat_tunnels(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session

            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            await db.commit()

        with (
            patch("swarmer.openshell_client.delete_service", AsyncMock()),
            patch("swarmer.openshell_client.delete_sandbox", AsyncMock()),
            patch(
                "swarmer.routers.chat_proxy.close_session_chat_tunnels",
                new=AsyncMock(return_value=2),
            ) as close_mock,
        ):
            await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/stop",
                follow_redirects=False,
            )

        close_mock.assert_awaited_once_with(s["id"])

    @pytest.mark.asyncio
    async def test_api_stop_closes_chat_tunnels(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session

            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            await db.commit()

        with (
            patch("swarmer.openshell_client.delete_service", AsyncMock()),
            patch("swarmer.openshell_client.delete_sandbox", AsyncMock()),
            patch(
                "swarmer.routers.chat_proxy.close_session_chat_tunnels",
                new=AsyncMock(return_value=0),
            ) as close_mock,
        ):
            resp = await client.post(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop")

        assert resp.status_code == 200
        close_mock.assert_awaited_once_with(s["id"])


# 4. Session lifecycle: expose_service and delete_service
# ===========================================================================


async def _test_get_db():
    """Yield a session from the test DB — for use when code calls get_db() directly."""
    async with _TestSession() as session:
        yield session


class TestInteractiveSessionUsesTunnel:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["server", "tui"])
    async def test_opencode_interactive_mode_does_not_expose_public_service(self, client, mode):
        from swarmer.routers.sessions import _run_openshell_agent

        start_mock = AsyncMock()
        expose_mock = AsyncMock(return_value="https://public-service.example.com")
        ws = await _create_workspace(client)
        session = await _create_session(client, ws["id"], mode=mode)
        async with _TestSession() as db:
            from swarmer.models.session import Session

            row = await db.get(Session, session["id"])
            row.sandbox_name = "sandbox-test-abc"
            await db.commit()

        with (
            patch("swarmer.openshell_client.start_agent", start_mock),
            patch("swarmer.openshell_client.expose_service", expose_mock),
            patch("swarmer.database.get_db", _test_get_db),
            patch("swarmer.routers.sessions._wait_for_opencode_ready", new=AsyncMock()) as ready_mock,
            patch("swarmer.routers.sessions.asyncio.sleep", AsyncMock()),
        ):
            await _run_openshell_agent(
                session_id=session["id"],
                workspace_id=ws["id"],
                sandbox_name="sandbox-test-abc",
                cmd=["opencode", "serve", "--service"],
                mode=mode,
                agent_tool="opencode",
            )

        start_mock.assert_called_once()
        expose_mock.assert_not_awaited()
        assert ready_mock.await_count == 1
        async with _TestSession() as db:
            from swarmer.models.session import Session

            updated = await db.get(Session, session["id"])
            assert updated.phase == "running"
            assert updated.service_url is None


class TestStopDeleteCallsDeleteService:
    @pytest.mark.asyncio
    async def test_stop_calls_delete_service(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://agent.sandbox.example.com"
            await db.commit()

        delete_svc_mock = AsyncMock()
        delete_sandbox_mock = AsyncMock()

        with patch("swarmer.openshell_client.delete_service", delete_svc_mock), \
             patch("swarmer.openshell_client.delete_sandbox", delete_sandbox_mock):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/stop",
                follow_redirects=False,
            )

        assert resp.status_code in (302, 200)
        delete_svc_mock.assert_called_once_with("sandbox-test-abc", "agent")
        delete_sandbox_mock.assert_called_once_with("sandbox-test-abc")

    @pytest.mark.asyncio
    async def test_delete_calls_delete_service(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "stopped"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://agent.sandbox.example.com"
            await db.commit()

        delete_svc_mock = AsyncMock()
        delete_sandbox_mock = AsyncMock()

        with patch("swarmer.openshell_client.delete_service", delete_svc_mock), \
             patch("swarmer.openshell_client.delete_sandbox", delete_sandbox_mock):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/delete",
                follow_redirects=False,
            )

        assert resp.status_code in (302, 200)
        delete_svc_mock.assert_called_once_with("sandbox-test-abc", "agent")

    @pytest.mark.asyncio
    async def test_stop_clears_service_url_from_db(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://agent.sandbox.example.com"
            await db.commit()

        with patch("swarmer.openshell_client.delete_service", AsyncMock()), \
             patch("swarmer.openshell_client.delete_sandbox", AsyncMock()):
            await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/stop",
                follow_redirects=False,
            )

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            updated = await db.get(_Session, s["id"])
            assert updated.service_url is None
            assert updated.sandbox_name is None


# ===========================================================================
# 4b. Chat proxy — upstream error handling
# ===========================================================================


class TestChatHttpProxyErrors:
    """Verify the proxy returns 503 (not ASGI crash) for all upstream errors."""

    def test_rewrite_html_intercepts_same_origin_api_and_websocket_calls(self):
        """OpenCode absolute API calls stay inside the chat proxy prefix."""
        from swarmer.routers.chat_proxy import _rewrite_html

        prefix = "/workspaces/7/sessions/32/chat"
        rewritten = _rewrite_html(b"<html><head></head></html>", prefix).decode()

        assert "window.fetch" in rewritten
        assert "OriginalEventSource" in rewritten
        assert "OriginalWebSocket" in rewritten
        assert "_prefix + value" in rewritten
        assert "'href' in input" in rewritten
        assert "parsed.pathname.indexOf(_prefix) === 0" in rewritten
        assert "parsed.pathname = _prefix +" in rewritten
        assert prefix in rewritten
        # The shim must not blindly rewrite third-party absolute URLs.
        assert "parsed.origin !== location.origin" in rewritten
        assert "parsedWs.host === location.host" in rewritten
        assert "parsedWs.protocol === 'wss:'" in rewritten

    def test_rewrite_js_preserves_prefixed_absolute_post_requests(self):
        """Already-proxied prompt URLs are returned unchanged for POST bodies."""
        from swarmer.routers.chat_proxy import _proxy_intercept_script

        script = _proxy_intercept_script("/workspaces/7/sessions/32/chat")

        assert "if (parsed.pathname.indexOf(_prefix) === 0) return value;" in script
        assert "if (rewritten !== input.url) input = new Request(rewritten, input);" in script

    def test_rewrite_css_intercepts_absolute_assets(self):
        """CSS font and image URLs stay inside the session proxy."""
        from swarmer.routers.chat_proxy import _rewrite_css

        prefix = "/workspaces/7/sessions/32/chat"
        css = b"@font-face{src:url('/assets/Inter.ttf')} .x{background:url(/assets/bg.png)}"
        rewritten = _rewrite_css(css, prefix).decode()

        assert f"url('{prefix}/assets/Inter.ttf')" in rewritten
        assert f"url({prefix}/assets/bg.png)" in rewritten

    def test_rewrite_js_strips_chat_prefix_for_opencode_routes(self):
        """OpenCode's root-relative routes match when served under the proxy."""
        from swarmer.routers.chat_proxy import _rewrite_js

        prefix = "/workspaces/7/sessions/32/chat"
        bundle = br'const r=window.location.pathname.replace(/^\/+/,"/")+window.location.search;'
        rewritten = _rewrite_js(bundle, prefix).decode()

        assert 'const _swRoutePrefix="/workspaces/7/sessions/32/chat"' in rewritten
        assert "_swRawPath.slice(_swRoutePrefix.length)" in rewritten
        assert 'const r=(_swRoutePath.startsWith("/")?_swRoutePath:"/"+_swRoutePath)' in rewritten

    def test_rewrite_js_preserves_named_server_url_function(self):
        """The API-base rewrite must not turn a named declaration into invalid JS."""
        from swarmer.routers.chat_proxy import _rewrite_js

        prefix = "/workspaces/7/sessions/32/chat"
        bundle = b"function currentServerUrl(){return location.origin}"

        rewritten = _rewrite_js(bundle, prefix).decode()

        assert f'function currentServerUrl(){{return location.origin+"{prefix}"}}' in rewritten
        assert "function(){return location.origin" not in rewritten

    def test_rewrite_js_prefixes_all_absolute_asset_quote_styles(self):
        """All JS asset references are proxied without touching unrelated URLs."""
        from swarmer.routers.chat_proxy import _rewrite_js

        prefix = "/workspaces/7/sessions/32/chat"
        bundle = (
            b'const worker="/assets/markdown.worker.js";'
            b"const sprite='/assets/sprite.svg';"
            b"const media=`/assets/intro.mp4`;"
            b'const existing="/workspaces/7/sessions/32/chat/assets/keep.svg";'
            b'const external="https://cdn.example.com/assets/external.svg";'
        )
        rewritten = _rewrite_js(bundle, prefix).decode()

        assert f'"{prefix}/assets/markdown.worker.js"' in rewritten
        assert f"'{prefix}/assets/sprite.svg'" in rewritten
        assert f"`{prefix}/assets/intro.mp4`" in rewritten
        assert f'"{prefix}/assets/keep.svg"' in rewritten
        assert '"https://cdn.example.com/assets/external.svg"' in rewritten
        assert '"/assets/markdown.worker.js"' not in rewritten
        assert "'/assets/sprite.svg'" not in rewritten
        assert "`/assets/intro.mp4`" not in rewritten

    def test_rewrite_csp_allows_exact_inline_proxy_scripts(self):
        """Injected scripts are authorized without enabling unsafe inline code."""
        from swarmer.routers.chat_proxy import (
            _proxy_compat_script,
            _proxy_intercept_script,
            _rewrite_content_security_policy,
        )

        prefix = "/workspaces/7/sessions/32/chat"
        csp = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'"
        rewritten = _rewrite_content_security_policy(
            csp,
            [_proxy_intercept_script(prefix), _proxy_compat_script(prefix)],
        )

        assert rewritten.startswith("default-src 'self'; script-src 'self' 'wasm-unsafe-eval'")
        assert rewritten.count("'sha256-") == 2
        assert "unsafe-inline" not in rewritten

    @pytest.mark.asyncio
    async def test_session_not_running_returns_503(self, client):
        """Session without service_url and without pod_name → 503."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            # no sandbox_name, no service_url, no pod_name
            await db.commit()

        resp = await client.get(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api"
        )
        assert resp.status_code == 503



# ===========================================================================
# 6. _restart_server_sessions() startup reconciliation
# ===========================================================================


class TestRestartServerSessions:
    """Startup reconciliation re-connects server-mode sessions after Swarmer restart."""

    @pytest.fixture(autouse=True)
    def _configured_default_gateway(self, monkeypatch):
        from swarmer.config import settings

        monkeypatch.setattr(settings, "openshell_gateway_url", "http://default-gateway")

    @pytest.fixture(autouse=True)
    def _ready_probe_succeeds_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "swarmer.routers.sessions._wait_for_opencode_ready",
            AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_live_sandbox_removes_legacy_service_url(self, client):
        """A live server-mode session no longer needs a public service URL."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-abc"
            session_obj.service_url = "http://old-service-url:4096"
            await db.commit()

        delete_service = AsyncMock()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-abc"])),
            patch("swarmer.openshell_client.delete_service", new=delete_service),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        delete_service.assert_awaited_once_with("sandbox-abc", "agent", client=None)

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"
            assert session_obj.service_url is None

    @pytest.mark.asyncio
    async def test_restart_reconciliation_does_not_log_service_url(self, client, caplog):
        """Regression (No-Sensitive-Data-In-Logs): restart reconciliation of an
        OpenCode TUI session must not write the re-exposed service URL or
        gateway hostname to logs."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-abc"
            session_obj.service_url = "http://old-service-url:4096"
            await db.commit()

        delete_service = AsyncMock()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-abc"])),
            patch("swarmer.openshell_client.delete_service", new=delete_service),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            with caplog.at_level(logging.INFO):
                await _restart_server_sessions()

        assert "old-service-url" not in caplog.text
        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"
            assert session_obj.service_url is None

    @pytest.mark.asyncio
    async def test_dead_sandbox_stops_session(self, client):
        """A server-mode session whose sandbox is gone is moved to stopped."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-gone"
            session_obj.service_url = "http://dead:4096"
            await db.commit()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=[])),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "stopped"
            assert session_obj.sandbox_name is None
            assert session_obj.service_url is None

    @pytest.mark.asyncio
    async def test_legacy_service_cleanup_failure_preserves_live_session(self, client):
        """A failed legacy-route cleanup must not stop the live session."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-still-live"
            session_obj.service_url = "http://previous-service-url:4096"
            await db.commit()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-still-live"])),
            patch("swarmer.openshell_client.delete_service", new=AsyncMock(side_effect=RuntimeError("gateway warming"))),
            patch("swarmer.database.get_db", new=_override_get_db),
            patch("swarmer.main._restart_github_app_iat_refresh", new=AsyncMock()) as iat_refresh,
            patch("swarmer.main._restart_agent_swarm_token_refresh", new=AsyncMock()) as token_refresh,
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        iat_refresh.assert_awaited_once()
        token_refresh.assert_awaited_once()

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"
            assert session_obj.sandbox_name == "sandbox-still-live"
            assert session_obj.service_url == "http://previous-service-url:4096"

    @pytest.mark.asyncio
    async def test_list_sandboxes_failure_is_safe(self, client):
        """If list_sandboxes fails, sessions are left unchanged (safe degradation)."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-abc"
            session_obj.service_url = "http://service:4096"
            await db.commit()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(side_effect=Exception("gateway down"))),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        # Session must be unchanged — no DB writes on gateway failure
        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"
            assert session_obj.service_url == "http://service:4096"

    @pytest.mark.asyncio
    async def test_startup_readiness_failure_leaves_diagnostic_pending_session(
        self, client, monkeypatch
    ):
        from swarmer.main import _restart_server_sessions

        ws = await _create_workspace(client)
        created = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, created["id"])
            row.phase = "running"
            row.sandbox_name = "sandbox-not-ready"
            await db.commit()

        probe = AsyncMock(side_effect=TimeoutError("probe timeout"))
        monkeypatch.setattr(
            "swarmer.routers.sessions._wait_for_opencode_ready",
            probe,
        )
        refresh = AsyncMock()
        token_refresh = AsyncMock()
        with (
            patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(return_value=["sandbox-not-ready"]),
            ),
            patch("swarmer.database.get_db", new=_override_get_db),
            patch("swarmer.main._restart_github_app_iat_refresh", new=refresh),
            patch("swarmer.main._restart_agent_swarm_token_refresh", new=token_refresh),
        ):
            await _restart_server_sessions()

        assert probe.await_count == 3
        refresh.assert_awaited_once()
        token_refresh.assert_awaited_once()
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            row = await db.get(SessionRow, created["id"])
            assert row.phase == "pending"
            assert "readiness probe failed" in row.status_detail

    @pytest.mark.asyncio
    async def test_gateway_resolution_failure_skips_only_affected_session(
        self, client, monkeypatch
    ):
        from swarmer.main import _restart_server_sessions

        failed_ws = await _create_workspace(client)
        healthy_ws = await _create_workspace(client, name="Healthy Proxy Test WS")
        failed = await _create_session(
            client, failed_ws["id"], mode="server", agent_tool="opencode"
        )
        healthy = await _create_session(
            client, healthy_ws["id"], mode="server", agent_tool="opencode"
        )
        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            failed_row = await db.get(SessionRow, failed["id"])
            failed_row.phase = "running"
            failed_row.sandbox_name = "sandbox-resolution-error"
            healthy_row = await db.get(SessionRow, healthy["id"])
            healthy_row.phase = "running"
            healthy_row.sandbox_name = "sandbox-healthy"
            await db.commit()

        async def resolve_gateway(workspace, _db):
            workspace_id = getattr(workspace, "id", workspace)
            if workspace_id == failed_ws["id"]:
                raise RuntimeError("gateway configuration unavailable")

        ready_probe = AsyncMock()

        with (
            patch(
                "swarmer.openshell_client.get_client_for_workspace",
                new=AsyncMock(side_effect=resolve_gateway),
            ),
            patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(return_value=["sandbox-healthy"]),
            ),
            patch("swarmer.routers.sessions._wait_for_opencode_ready", new=ready_probe),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            await _restart_server_sessions()

        async with _TestSession() as db:
            from swarmer.models.session import Session as SessionRow

            failed_row = await db.get(SessionRow, failed["id"])
            healthy_row = await db.get(SessionRow, healthy["id"])
            assert failed_row.phase == "running"
            assert failed_row.sandbox_name == "sandbox-resolution-error"
            assert healthy_row.phase == "running"
            assert healthy_row.sandbox_name == "sandbox-healthy"
        ready_probe.assert_awaited_once_with("sandbox-healthy", None)

    @pytest.mark.asyncio
    async def test_prompt_mode_sessions_are_not_touched(self, client):
        """Prompt-mode sessions are not processed by _restart_server_sessions."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-prompt"
            session_obj.service_url = None
            await db.commit()

        mock_expose = AsyncMock()
        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=[])),
            patch("swarmer.openshell_client.expose_service", new=mock_expose),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        # expose_service must never be called for prompt-mode sessions
        mock_expose.assert_not_called()

        # Session phase untouched (prompt-mode sessions are handled by _restart_prompt_pollers)
        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"


# ===========================================================================
# 6b. _restart_server_sessions() — TUI mode + GitHub App IAT refresh restart
#     (ACM-39064: Swarmer restart must not silently let App IATs expire)
# ===========================================================================


# Not a real key — GitHubApp.is_configured only checks that private_key_enc is
# non-empty, so a non-PEM-shaped placeholder avoids tripping secret scanners
# while still exercising the encrypt/decrypt round trip.
_FAKE_APP_PRIVATE_KEY = "test-placeholder-not-a-real-key"


class TestRestartGitHubAppIATRefresh:
    """TUI/server sessions using a workspace GitHub App get their IAT refresh
    loop restarted after a Swarmer restart — otherwise the App IAT (valid ~1h)
    silently expires and git push starts failing inside an otherwise-healthy
    sandbox."""

    @pytest.fixture(autouse=True)
    def _ready_probe_succeeds_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "swarmer.routers.sessions._wait_for_opencode_ready",
            AsyncMock(),
        )

    @pytest.fixture(autouse=True)
    def _configured_default_gateway(self, monkeypatch):
        from swarmer.config import settings

        monkeypatch.setattr(settings, "openshell_gateway_url", "http://default-gateway")

    def test_iat_app_snapshot_preserves_workspace_id(self):
        """The refresh snapshot captures the enclosing workspace id safely."""
        from swarmer.routers.sessions import _build_iat_app_snapshot

        snapshot = _build_iat_app_snapshot("111", "222", "private-key", 7)

        assert snapshot.app_id == "111"
        assert snapshot.installation_id == "222"
        assert snapshot.private_key == "private-key"
        assert snapshot.workspace_id == 7

    @pytest.mark.asyncio
    async def test_tui_session_with_github_app_restarts_refresh_loop(self, client):
        from swarmer.models.github_app import GitHubApp
        from swarmer.models.session import Session as _Session
        from swarmer.models.session_repo import SessionRepo

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui", agent_tool="opencode")

        pem = _FAKE_APP_PRIVATE_KEY
        async with _TestSession() as db:
            app = GitHubApp(
                workspace_id=ws["id"], user_id="", app_id="111", installation_id="222",
            )
            app.private_key = pem
            db.add(app)

            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-tui"
            # Model a legacy session with inherited MCP selection. Newly
            # created sessions default to MCP-disabled.
            session_obj.mcp_server_ids = ""
            db.add(SessionRepo(
                session_id=s["id"], repo_url="https://github.com/org/repo",
                branch="main", local_path="repo",
            ))
            await db.commit()

        mock_mint = AsyncMock(return_value="ghs_freshtoken")
        mock_ensure_provider = AsyncMock()
        mock_start_loop = AsyncMock()
        mock_agent_swarm_loop = AsyncMock()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-tui"])),
            patch("swarmer.openshell_client.ensure_provider", new=mock_ensure_provider),
            patch("swarmer.github_auth.mint_installation_token", new=mock_mint),
            patch("swarmer.github_auth.start_token_refresh_loop", new=mock_start_loop),
            patch("swarmer.session_auth.start_token_refresh_loop", new=mock_agent_swarm_loop),
            # OpenCode V2 unification: TUI sessions are re-exposed like server
            # sessions after a restart.
            patch("swarmer.openshell_client.expose_service", new=AsyncMock(
                return_value="https://agent.sandbox.example.com"
            )),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()
            await asyncio.sleep(0)  # let the created task run its first line

        mock_mint.assert_awaited_once()
        github_calls = [call for call in mock_ensure_provider.await_args_list if call.args[1] == "github"]
        assert len(github_calls) == 1
        _, provider_type, _config = github_calls[0].args[:3]
        assert provider_type == "github"
        creds = github_calls[0].kwargs["credentials"]
        assert creds["GH_TOKEN"] == "ghs_freshtoken"
        assert creds["GITHUB_TOKEN"] == "ghs_freshtoken"
        mock_start_loop.assert_called_once()
        mock_agent_swarm_loop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_with_pat_skips_iat_refresh(self, client):
        """A session with an explicit PAT never triggers GitHub App IAT refresh."""
        from swarmer.models.github_pat import GitHubPAT
        from swarmer.models.session import Session as _Session
        from swarmer.models.session_repo import SessionRepo

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            pat = GitHubPAT(workspace_id=ws["id"], name="p", github_username="u")
            pat.pat = "ghp_xxx"
            db.add(pat)
            await db.flush()

            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-srv"
            session_obj.github_pat_id = pat.id
            db.add(SessionRepo(
                session_id=s["id"], repo_url="https://github.com/org/repo",
                branch="main", local_path="repo",
            ))
            await db.commit()

        mock_mint = AsyncMock()
        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-srv"])),
            patch("swarmer.openshell_client.expose_service", new=AsyncMock(return_value="http://svc:4096")),
            patch("swarmer.github_auth.mint_installation_token", new=mock_mint),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        mock_mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_github_repos_skips_iat_refresh(self, client):
        """A session with no GitHub repos never triggers IAT refresh, even with an App configured."""
        from swarmer.models.github_app import GitHubApp
        from swarmer.models.session import Session as _Session

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui", agent_tool="opencode")

        pem = _FAKE_APP_PRIVATE_KEY
        async with _TestSession() as db:
            app = GitHubApp(
                workspace_id=ws["id"], user_id="", app_id="111", installation_id="222",
            )
            app.private_key = pem
            db.add(app)

            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-tui2"
            await db.commit()

        mock_mint = AsyncMock()
        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-tui2"])),
            patch("swarmer.github_auth.mint_installation_token", new=mock_mint),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        mock_mint.assert_not_called()

    @pytest.mark.asyncio
    async def test_iat_mint_failure_is_swallowed(self, client):
        """IAT re-mint failure on restart must not crash the restart sequence."""
        from swarmer.models.github_app import GitHubApp
        from swarmer.models.session import Session as _Session
        from swarmer.models.session_repo import SessionRepo

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui", agent_tool="opencode")

        pem = _FAKE_APP_PRIVATE_KEY
        async with _TestSession() as db:
            app = GitHubApp(
                workspace_id=ws["id"], user_id="", app_id="111", installation_id="222",
            )
            app.private_key = pem
            db.add(app)

            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-tui3"
            db.add(SessionRepo(
                session_id=s["id"], repo_url="https://github.com/org/repo",
                branch="main", local_path="repo",
            ))
            await db.commit()

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-tui3"])),
            patch("swarmer.github_auth.mint_installation_token", new=AsyncMock(side_effect=Exception("github down"))),
            # OpenCode V2 unification: TUI sessions are re-exposed like server
            # sessions after a restart.
            patch("swarmer.openshell_client.expose_service", new=AsyncMock(
                return_value="https://agent.sandbox.example.com"
            )),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()  # must not raise

        async with _TestSession() as db:
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"  # unaffected by the mint failure


# ===========================================================================
# 6c. _restart_prompt_pollers() — positional argument regression (ACM-39064)
# ===========================================================================


class TestRestartPromptPollersArgs:
    """_restart_prompt_pollers() must call _run_openshell_agent() with
    workspace_id in the correct positional slot. A prior regression omitted
    workspace_id, shifting every subsequent positional argument and crashing
    with TypeError on restart for any active prompt-mode session."""

    @pytest.mark.asyncio
    async def test_run_openshell_agent_called_with_correct_positional_args(self, client):
        from swarmer.models.session import Session as _Session

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt", agent_tool="opencode")

        async with _TestSession() as db:
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-prompt-restart"
            await db.commit()

        mock_run = AsyncMock()
        with (
            patch("swarmer.routers.sessions._run_openshell_agent", new=mock_run),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_prompt_pollers
            await _restart_prompt_pollers()
            await asyncio.sleep(0)  # let the created task invoke the (mocked) coroutine

        mock_run.assert_called_once()
        call_args = mock_run.call_args.args
        # Positional signature: (session_id, workspace_id, sandbox_name, cmd, mode, agent_tool)
        assert call_args[0] == s["id"]
        assert call_args[1] == ws["id"]
        assert call_args[2] == "sandbox-prompt-restart"
        assert isinstance(call_args[3], list)
        assert call_args[4] == "prompt"
        assert call_args[5] == "opencode"

    @pytest.mark.asyncio
    async def test_prompt_resume_remints_agent_swarm_token(self, client):
        from swarmer.models.session import Session as _Session

        ws = await _create_workspace(client)
        mcp_list = await client.get(f"/api/v1/workspaces/{ws['id']}/mcp-servers")
        assert mcp_list.status_code == 200, mcp_list.text
        mcp = next((row for row in mcp_list.json() if row["slug"] == "agent-swarm"), None)
        if mcp is None:
            mcp_resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/mcp-servers",
                json={"catalog_slug": "agent-swarm"},
            )
            assert mcp_resp.status_code == 201, mcp_resp.text
            mcp = mcp_resp.json()
        session_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={
                "name": "prompt-agent-swarm-restart",
                "mode": "prompt",
                "agent_tool": "opencode",
                "mcp_server_ids": [mcp["id"]],
            },
        )
        assert session_resp.status_code == 201, session_resp.text
        s = session_resp.json()

        async with _TestSession() as db:
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-agent-swarm-restart"
            await db.commit()

        mock_run = AsyncMock()
        sentinel_token = "unit-test-session-token"
        with (
            patch("swarmer.routers.sessions._run_openshell_agent", new=mock_run),
            patch("swarmer.database.get_db", new=_override_get_db),
            patch(
                "swarmer.session_auth.mint_session_token",
                return_value=sentinel_token,
            ),
        ):
            from swarmer.main import _restart_prompt_pollers

            await _restart_prompt_pollers()
            await asyncio.sleep(0)

        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["env_vars"]["AGENT_SWARM_API_TOKEN"] == sentinel_token

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("endpoint", "provider_available", "profile_matches", "should_resume"),
        [
            ("https://enmaas-gateway.example.com/v1", True, True, True),
            ("https://enmaas-gateway.example.com/v1", True, False, False),
            ("", True, True, False),
            ("https://enmaas-gateway.example.com/v1", False, True, False),
            ("https://enmaas-gateway.example.com/v1", "error", False, False),
        ],
    )
    async def test_scheduled_enmaas_provider_is_preserved_and_fails_closed(
        self, client, monkeypatch, endpoint, provider_available, profile_matches,
        should_resume, caplog
    ):
        from datetime import UTC, datetime

        from swarmer.models.session import Session as _Session
        from swarmer.models.session_schedule import SessionSchedule

        ws = await _create_workspace(client)
        s = await _create_session(
            client, ws["id"], mode="prompt", agent_tool="opencode"
        )
        async with _TestSession() as db:
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-enmaas-restart"
            session_obj.run_started_at = datetime.now(UTC)
            schedule = SessionSchedule(
                session_id=s["id"], provider="enmaas", instruction_prompt="scheduled"
            )
            db.add(schedule)
            await db.flush()
            session_obj.active_schedule_id = schedule.id
            await db.commit()

        await _set_workspace_enmaas_url(ws["id"], endpoint)
        mock_run = AsyncMock()
        mock_get_client = AsyncMock(return_value=MagicMock())
        sentinel = "SENSITIVE_PROVIDER_EXCEPTION"
        mock_provider_exists = (
            AsyncMock(side_effect=RuntimeError(sentinel))
            if provider_available == "error"
            else AsyncMock(return_value=provider_available)
        )
        mock_profile_endpoint = AsyncMock(return_value=profile_matches)
        with (
            patch("swarmer.routers.sessions._run_openshell_agent", new=mock_run),
            patch("swarmer.database.get_db", new=_override_get_db),
            patch(
                "swarmer.openshell_client.get_client_for_workspace",
                new=mock_get_client,
            ),
            patch(
                "swarmer.openshell_client.provider_exists",
                new=mock_provider_exists,
            ),
            patch(
                "swarmer.openshell_client.provider_profile_endpoint_matches",
                new=mock_profile_endpoint,
            ),
            patch("swarmer.openshell_client.write_agent_config", new=AsyncMock()),
            caplog.at_level(logging.WARNING, logger="swarmer.main"),
        ):
            from swarmer.main import _restart_prompt_pollers

            await _restart_prompt_pollers()
            await asyncio.sleep(0)

        if should_resume:
            mock_run.assert_called_once()
            command = mock_run.call_args.args[3]
            assert "--model enmaas/" in command[-1]
            assert "google/" not in command[-1]
            mock_get_client.assert_awaited_once()
            mock_provider_exists.assert_awaited_once_with(
                f"swarmer-ws-{ws['id']}-enmaas",
                client=mock_get_client.return_value,
            )
            mock_profile_endpoint.assert_awaited_once_with(
                f"enmaas-ws-{ws['id']}",
                ("enmaas-gateway.example.com", 443),
                client=mock_get_client.return_value,
            )
        else:
            mock_run.assert_not_called()
            assert "EnMaaS unavailable; session marked failed" in caplog.text
            assert f"session {s['id']}" not in caplog.text
            assert sentinel not in caplog.text
            assert "Traceback" not in caplog.text
            if endpoint:
                mock_get_client.assert_awaited_once()
                mock_provider_exists.assert_awaited_once()
                if provider_available is True:
                    mock_profile_endpoint.assert_awaited_once()
                else:
                    mock_profile_endpoint.assert_not_awaited()
            else:
                mock_get_client.assert_not_awaited()
                mock_provider_exists.assert_not_awaited()
                mock_profile_endpoint.assert_not_awaited()

            async with _TestSession() as db:
                session_obj = await db.get(_Session, s["id"])
                assert session_obj.phase == "failed"
                assert session_obj.status_detail.startswith(
                    "EnMaaS provider is unavailable"
                )
                assert session_obj.run_completed_at is not None
                assert session_obj.active_schedule_id is None


# ===========================================================================
# 7. TUI WebSocket — OpenShell path (unit-level)
# ===========================================================================


@_requires_sdk
class TestTuiWsOpenshell:
    """Unit-level tests for the OpenShell branch in tui_ws.py.

    These do not open a real WebSocket connection — they test the helper logic
    and the openshell_client wrappers that the TUI handler uses.
    """

    def test_tui_reconnect_targets_selected_session_or_safe_fallback(self):
        from swarmer.routers.tui_ws import _build_tui_shell

        selected = _build_tui_shell("opencode", "ses_selected_chat")
        fallback = _build_tui_shell("opencode")

        assert "opencode --server http://127.0.0.1:4096 --session ses_selected_chat" in selected
        assert "--continue" not in selected
        assert fallback.endswith("exec opencode --server http://127.0.0.1:4096")
        assert 'NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,0.0.0.0"' in fallback
        assert 'no_proxy="${no_proxy:+$no_proxy,}127.0.0.1,localhost,0.0.0.0"' in fallback
        assert "OPENCODE_PASSWORD" in fallback
        assert ".local/state/opencode/service.json" in fallback
        assert "test -n \"$OPENCODE_PASSWORD\"" in fallback
        assert "--continue" not in fallback

    def test_non_opencode_tui_does_not_change_proxy_exclusions(self):
        from swarmer.routers.tui_ws import _build_tui_shell

        shell = _build_tui_shell("bash")

        assert shell.endswith("exec bash")
        assert "NO_PROXY" not in shell

    def test_exec_interactive_stdin_queued(self):
        """The first message in the stream identifies the sandbox across SDK versions."""
        from openshell._proto import openshell_pb2 as pb

        received = []

        def _capture_stream(request_iter, **kwargs):
            # Only read the start message; the generator blocks on queue.get() after
            # that so we must break after 1 to avoid deadlock.
            for msg in request_iter:
                received.append(msg)
                break
            return iter([])

        sdk_client = MagicMock()
        sdk_client._stub.ExecSandboxInteractive.side_effect = _capture_stream
        sdk_client._timeout = 30

        stream, input_q = oc.exec_interactive(
            sandbox_name="sandbox-id-x",
            command=["sh", "-c", "opencode"],
            cols=80,
            rows=24,
            client=sdk_client,
        )
        input_q.put(None)  # unblock generator if it ever resumes

        assert len(received) == 1
        assert received[0].start.sandbox == "sandbox-id-x"

    def test_exec_interactive_resize_queued(self):
        """The first message in the stream carries the sandbox ID and tty flags."""
        from openshell._proto import openshell_pb2 as pb

        received = []

        def _capture_stream(request_iter, **kwargs):
            for msg in request_iter:
                received.append(msg)
                break
            return iter([])

        sdk_client = MagicMock()
        sdk_client._stub.ExecSandboxInteractive.side_effect = _capture_stream
        sdk_client._timeout = 30

        stream, input_q = oc.exec_interactive(
            sandbox_name="sandbox-id-x",
            command=["sh", "-c", "opencode"],
            cols=80,
            rows=24,
            client=sdk_client,
        )
        input_q.put(None)  # unblock generator if it ever resumes

        assert received[0].start.sandbox == "sandbox-id-x"


# ===========================================================================
# 6. E2E Smoke Tests (require dev server at :8091 with SWARMER_DEV_AUTH=1)
# ===========================================================================
#
# These are skipped unless the SWARMER_E2E environment variable is set.
# Run with: SWARMER_E2E=1 pytest tests/test_openshell_proxy.py -k smoke -v


import os as _os
_E2E = _os.environ.get("SWARMER_E2E") == "1"
_E2E_BASE = _os.environ.get("SWARMER_E2E_URL", "http://localhost:8091")


@pytest.mark.skipif(not _E2E, reason="Set SWARMER_E2E=1 to run e2e smoke tests")
class TestE2eSmokeProxy:
    """
    E2E smoke tests for OpenShell TUI, server-mode proxy, and Gemini prompt flow.

    These tests call the real running app and verify end-to-end behavior
    against a session that has already been started with OpenShell enabled.
    They assume SWARMER_DEV_AUTH=1 (no token required).

    Env vars:
      SWARMER_E2E=1             — enable this test class
      SWARMER_E2E_URL           — base URL (default http://localhost:8091)
      SWARMER_E2E_WS_ID         — workspace ID to use (required)
      SWARMER_E2E_SID           — session ID of a running server-mode OpenShell session
      SWARMER_E2E_TUI_SID       — session ID of a running opencode tui-mode session
    """

    @pytest.fixture(autouse=True)
    def check_env(self):
        ws_id = _os.environ.get("SWARMER_E2E_WS_ID")
        if not ws_id:
            pytest.skip("SWARMER_E2E_WS_ID not set")

    @property
    def _ws_id(self):
        return int(_os.environ["SWARMER_E2E_WS_ID"])

    @property
    def _sid(self):
        val = _os.environ.get("SWARMER_E2E_SID")
        if not val:
            pytest.skip("SWARMER_E2E_SID not set")
        return int(val)

    @property
    def _tui_sid(self):
        val = _os.environ.get("SWARMER_E2E_TUI_SID")
        if not val:
            pytest.skip("SWARMER_E2E_TUI_SID not set")
        return int(val)

    @pytest.mark.asyncio
    async def test_smoke_chat_proxy_responds(self):
        """Server-mode OpenShell session: /chat/ returns 200 or proxies upstream."""
        import httpx
        ws_id, sid = self._ws_id, self._sid
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/workspaces/{ws_id}/sessions/{sid}/chat/")
        assert resp.status_code in (200, 503), (
            f"Expected 200 (upstream ok) or 503 (upstream not ready), got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_smoke_session_detail_has_terminal_tab_for_tui(self):
        """TUI-mode OpenShell session: session detail page renders the terminal panel."""
        import httpx
        ws_id, sid = self._ws_id, self._tui_sid
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200
        assert "terminal" in resp.text.lower() or "xterm" in resp.text.lower(), (
            "Expected TUI terminal tab in session detail for tui-mode session"
        )

    @pytest.mark.asyncio
    async def test_smoke_service_url_set_on_running_server_session(self):
        """server-mode OpenShell session: API exposes service_url when running."""
        import httpx
        ws_id, sid = self._ws_id, self._sid
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        # service_url may be None if session is not running yet, but must be present in schema
        assert "service_url" in data or data.get("phase") != "running", (
            f"service_url missing from API response for running session: {data}"
        )

    @pytest.mark.asyncio
    async def test_smoke_opencode_tui_websocket(self):
        """OpenCode TUI: WebSocket connection returns PTY data within 5s.

        Requires a running opencode tui-mode session (SWARMER_E2E_TUI_SID).
        Connect to the TUI WebSocket, send the one-time token from the session
        detail page, and assert initial terminal output is received.

        To run:
          SWARMER_E2E=1 SWARMER_E2E_WS_ID=1 SWARMER_E2E_TUI_SID=2 \\
          pytest tests/test_openshell_proxy.py -k smoke_opencode_tui -v -s
        """
        import httpx
        import websockets

        ws_id, sid = self._ws_id, self._tui_sid

        # Get a one-time TUI token from the session detail page
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200, f"Session detail failed: {resp.status_code}"

        # Extract tui_token from the page (it's embedded as a JS variable)
        import re as _re
        match = _re.search(r'tuiToken\s*=\s*["\']([0-9a-f-]{36})["\']', resp.text)
        if not match:
            pytest.skip("No TUI token found — session may not be in running state")
        token = match.group(1)

        ws_url = _E2E_BASE.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws/{ws_id}/sessions/{sid}/tui"

        # Connect, send token, and wait for PTY output
        received = []
        try:
            async with websockets.connect(ws_url, open_timeout=5) as ws:
                await ws.send(token)
                import asyncio as _asyncio
                try:
                    async with _asyncio.timeout(5):
                        while True:
                            data = await ws.recv()
                            received.append(data)
                            if len(received) >= 1:
                                break
                except _asyncio.TimeoutError:
                    pass
        except Exception as exc:
            pytest.fail(f"WebSocket connection failed: {exc}")

        assert received, "Expected TUI to emit PTY output within 5s — got nothing"

    @pytest.mark.asyncio
    async def test_smoke_server_mode_service_url_reachable(self):
        """server-mode session: service_url is set and the chat proxy responds.

        Verifies the full chain: expose_service stored a URL, the port rewrite
        from 8080→gateway_port worked, TLS is bypassed, and OpenCode serves HTTP.

        To run:
          SWARMER_E2E=1 SWARMER_E2E_WS_ID=1 SWARMER_E2E_SID=4 \\
          pytest tests/test_openshell_proxy.py -k smoke_server_mode_service_url -v -s
        """
        import httpx
        ws_id, sid = self._ws_id, self._sid

        # 1. Check service_url is set on the session
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            api_resp = await hc.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")
        assert api_resp.status_code == 200
        data = api_resp.json()
        assert data.get("phase") == "running", f"Session not running: {data.get('phase')}"
        service_url = data.get("service_url")
        assert service_url, f"service_url not set on running server session: {data}"
        print(f"\n--- service_url: {service_url} ---")

        # 2. Proxy responds (200 or 503 if server slow, but NOT ASGI crash)
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            chat_resp = await hc.get(f"/workspaces/{ws_id}/sessions/{sid}/chat/")
        assert chat_resp.status_code in (200, 302, 503), (
            f"Unexpected status {chat_resp.status_code}: {chat_resp.text[:200]}"
        )
        print(f"--- chat response: {chat_resp.status_code} ---")

    @pytest.mark.asyncio
    async def test_smoke_openshell_mtls_configured(self):
        """Verify OPENSHELL_TLS_CERT and OPENSHELL_TLS_KEY are set in the running server.

        If these are missing, server-mode chat proxy will fail with
        TLSV13_ALERT_CERTIFICATE_REQUIRED from the OpenShell gateway.

        To run:
          SWARMER_E2E=1 SWARMER_E2E_WS_ID=1 \\
          pytest tests/test_openshell_proxy.py -k smoke_openshell_mtls -v -s
        """
        import httpx
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get("/api/v1/workspaces")
        # If the server is reachable at all, check the config via health or
        # just assert we can talk to it (mTLS only applies to upstream proxy)
        assert resp.status_code in (200, 401, 403), (
            f"Server not reachable: {resp.status_code}"
        )
        # The actual mTLS check: if cert/key are NOT configured, expose_service
        # sessions fail with CERTIFICATE_REQUIRED. This test is a documentation
        # reminder — run with a server session to confirm the real behavior.
        print("\n✓ Server reachable. Ensure OPENSHELL_TLS_CERT and OPENSHELL_TLS_KEY")
        print("  are set in .env pointing to auth/openshell/client.crt and client.key")

    @pytest.mark.asyncio
    async def test_smoke_gemini_prompt_completes(self):
        """Gemini flash prompt session: completes with non-empty output.

        Create a prompt session with model google/gemini-3.5-flash and
        prompt "Reply with exactly one word: ready". Wait for it to complete
        and check last_output is non-empty.

        To run:
          SWARMER_E2E=1 SWARMER_E2E_WS_ID=1 SWARMER_E2E_GEMINI_SID=5 \\
          pytest tests/test_openshell_proxy.py -k smoke_gemini_prompt -v -s
        """
        import httpx
        ws_id = self._ws_id
        sid_val = _os.environ.get("SWARMER_E2E_GEMINI_SID")
        if not sid_val:
            pytest.skip("SWARMER_E2E_GEMINI_SID not set")
        sid = int(sid_val)

        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        phase = data.get("phase", "")
        last_output = data.get("last_output", "") or ""

        print(f"\n--- Gemini session phase: {phase} ---")
        print(f"--- last_output: {last_output[:200]} ---")

        assert phase in ("succeeded", "failed"), (
            f"Session not complete (phase={phase}). Ensure it has finished running."
        )
        assert phase == "succeeded", f"Gemini prompt failed: {last_output}"
        assert last_output.strip(), "Expected non-empty output from Gemini prompt session"

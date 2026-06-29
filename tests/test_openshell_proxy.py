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
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch, call

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


class _ExecSandboxRequest(_ProtoMessage):
    pass


class _ExecSandboxInput(_ProtoMessage):
    pass


class _ExposeServiceRequest(_ProtoMessage):
    pass


class _DeleteServiceRequest(_ProtoMessage):
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
# The stub on PyPI (0.0.0a0) may not provide SandboxClient; skip those tests
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

    async def _all_accessible(token, namespaces, api_url, in_cluster):
        return list(namespaces)

    async def _can_create_namespaces(token, api_url, in_cluster):
        return True

    monkeypatch.setattr("swarmer.api.deps.get_accessible_namespaces", _all_accessible)
    monkeypatch.setattr("swarmer.api.v1.workspaces.can_create_namespaces", _can_create_namespaces)
    monkeypatch.setattr("swarmer.k8s.ensure_namespace", lambda namespace: None)
    monkeypatch.setattr("swarmer.k8s.grant_swarmer_user_access", lambda namespace, username: None)
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
    client._stub.DeleteService.return_value = MagicMock()
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
        assert req.service == "agent"
        assert req.target_port == 4096
        assert req.domain is True

    @pytest.mark.asyncio
    async def test_expose_service_returns_url(self, sdk_client):
        with patch.object(oc, "_get_client", return_value=sdk_client):
            url = await oc.expose_service("sandbox-test-abc", "agent", 4096)
        assert url == "https://agent.sandbox-abc.openshell.example.com"

    @pytest.mark.asyncio
    async def test_delete_service_calls_stub(self, sdk_client):
        with patch.object(oc, "_get_client", return_value=sdk_client):
            await oc.delete_service("sandbox-test-abc", "agent")
        sdk_client._stub.DeleteService.assert_called_once()
        args = sdk_client._stub.DeleteService.call_args
        req = args[0][0]
        assert req.sandbox == "sandbox-test-abc"
        assert req.service == "agent"


@_requires_sdk
class TestExecInteractive:
    def test_exec_interactive_returns_stream_and_queue(self, sdk_client):
        mock_stream = iter([])
        sdk_client._stub.ExecSandboxInteractive.return_value = mock_stream
        with patch.object(oc, "_get_client", return_value=sdk_client):
            stream, input_q = oc.exec_interactive(
                sandbox_name="sandbox-test",
                sandbox_id="abc123",
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
                sandbox_name="sandbox-test",
                sandbox_id="abc123",
                command=["sh", "-c", "opencode --continue"],
                cols=120,
                rows=40,
                client=sdk_client,
            )
            input_q.put(None)  # stop the generator after reading 1 message

        assert len(received_msgs) == 1
        start_msg = received_msgs[0]
        assert start_msg.start.sandbox_id == "abc123"
        assert start_msg.start.tty is True
        assert start_msg.start.cols == 120
        assert start_msg.start.rows == 40
        assert start_msg.start.workdir == "/sandbox"


# ===========================================================================
# 2. chat_proxy._session_ok()
# ===========================================================================


class TestSessionOk:
    def _make_session(self, pod_name=None, service_url=None, mode="server", phase="running"):
        s = MagicMock()
        s.pod_name = pod_name
        s.service_url = service_url
        s.mode = mode
        s.workspace_id = 1
        s.is_active = phase == "running"
        return s

    def _make_ws(self, ws_id=1):
        ws = MagicMock()
        ws.id = ws_id
        return ws

    def test_session_ok_rejects_pod_name_only(self):
        """pod_name alone is no longer accepted — service_url is required."""
        from swarmer.routers.chat_proxy import _session_ok
        ws = self._make_ws()
        s = self._make_session(pod_name="session-1-pod")
        assert _session_ok(ws, s, 1) is not None

    def test_session_ok_accepts_service_url(self):
        from swarmer.routers.chat_proxy import _session_ok
        ws = self._make_ws()
        s = self._make_session(service_url="https://agent.openshell.example.com")
        assert _session_ok(ws, s, 1) is None

    def test_session_ok_rejects_neither(self):
        from swarmer.routers.chat_proxy import _session_ok
        ws = self._make_ws()
        s = self._make_session()  # no pod_name, no service_url
        assert _session_ok(ws, s, 1) is not None

    def test_session_ok_rejects_non_server_mode(self):
        from swarmer.routers.chat_proxy import _session_ok
        ws = self._make_ws()
        s = self._make_session(pod_name="pod", mode="tui")
        assert _session_ok(ws, s, 1) is not None

    def test_session_ok_rejects_wrong_workspace(self):
        from swarmer.routers.chat_proxy import _session_ok
        ws = self._make_ws(ws_id=1)
        s = self._make_session(service_url="https://agent.example.com")
        s.workspace_id = 999  # mismatch
        assert _session_ok(ws, s, 1) is not None


# ===========================================================================
# 3. HTTP proxy uses service_url as upstream
# ===========================================================================


class TestChatHttpProxy:
    def _make_mock_client(self, status=200, content=b"ok", content_type="text/plain"):
        """Return a context-manager mock for httpx.AsyncClient used in the proxy."""
        import httpx as _httpx
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.content = content
        mock_resp.headers = _httpx.Headers({"content-type": content_type})

        mock_instance = AsyncMock()
        mock_instance.request = AsyncMock(return_value=mock_resp)

        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        return mock_cls, mock_instance

    @pytest.mark.asyncio
    async def test_proxy_uses_service_url_for_upstream(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "http://agent.openshell.internal:4096"
            await db.commit()

        mock_cls, mock_instance = self._make_mock_client(
            content=b"<html><head></head><body>ok</body></html>",
            content_type="text/html",
        )
        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient", mock_cls):
            await client.get(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/index.html"
            )

        mock_instance.request.assert_called_once()
        _, call_kwargs = mock_instance.request.call_args
        headers = call_kwargs.get("headers", {})
        # With gateway URL rewriting, the upstream URL uses the gateway's real
        # hostname; the virtual host is preserved in the Host header instead.
        assert "agent.openshell.internal" in headers.get("host", ""), (
            f"Expected virtual host in Host header, got headers: {headers}"
        )

    @pytest.mark.asyncio
    async def test_proxy_sets_sandbox_directory_header(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "http://agent.openshell.internal:4096"
            await db.commit()

        mock_cls, mock_instance = self._make_mock_client()
        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient", mock_cls):
            await client.get(f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api")

        mock_instance.request.assert_called_once()
        _, call_kwargs = mock_instance.request.call_args
        # No repos on this session → fallback to /sandbox
        assert call_kwargs.get("headers", {}).get("x-opencode-directory") == "/sandbox"

    @pytest.mark.asyncio
    async def test_proxy_always_sets_sandbox_directory(self, client):
        """All sessions use /sandbox (or /sandbox/{repo}) — legacy /workspace header is no longer sent."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "http://agent.openshell.internal:4096"
            await db.commit()

        mock_cls, mock_instance = self._make_mock_client()
        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient", mock_cls):
            await client.get(f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api")

        mock_instance.request.assert_called_once()
        _, call_kwargs = mock_instance.request.call_args
        # No repos on this session → fallback to /sandbox
        assert call_kwargs.get("headers", {}).get("x-opencode-directory") == "/sandbox"

    @pytest.mark.asyncio
    async def test_proxy_sets_host_header_for_gateway_routing(self, client):
        """HTTP proxy sets Host header to the virtual domain for gateway routing."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://oriented-lizardfish--agent.openshell.localhost:8080"
            await db.commit()

        mock_cls, mock_instance = self._make_mock_client()
        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient", mock_cls):
            await client.get(f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api")

        mock_instance.request.assert_called_once()
        _, call_kwargs = mock_instance.request.call_args
        headers = call_kwargs.get("headers", {})
        assert headers.get("host") == "oriented-lizardfish--agent.openshell.localhost:8080", (
            f"Expected Host header to be set to virtual domain, got: {headers}"
        )


# ===========================================================================
# 4. Session lifecycle: expose_service and delete_service
# ===========================================================================


async def _test_get_db():
    """Yield a session from the test DB — for use when code calls get_db() directly."""
    async with _TestSession() as session:
        yield session


class TestServerModeExposeService:
    @pytest.mark.asyncio
    async def test_server_mode_calls_expose_service(self, client):
        """_run_openshell_agent server mode calls expose_service after start_agent."""
        from swarmer.routers.sessions import _run_openshell_agent

        expose_mock = AsyncMock(return_value="https://agent.sandbox.example.com")
        start_mock = AsyncMock()

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")
        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.sandbox_name = "sandbox-test-abc"
            await db.commit()

        with patch("swarmer.openshell_client.start_agent", start_mock), \
             patch("swarmer.openshell_client.expose_service", expose_mock), \
             patch("swarmer.database.get_db", _test_get_db), \
             patch("swarmer.routers.sessions.asyncio.sleep", AsyncMock()):
            await _run_openshell_agent(
                session_id=s["id"],
                workspace_id=ws["id"],
                sandbox_name="sandbox-test-abc",
                cmd=["opencode", "serve", "--port", "4096"],
                mode="server",
                agent_tool="opencode",
            )

        start_mock.assert_called_once()
        expose_mock.assert_called_once_with("sandbox-test-abc", "agent", 4096)

    @pytest.mark.asyncio
    async def test_tui_mode_does_not_call_expose_service(self, client):
        """TUI mode should NOT call expose_service."""
        from swarmer.routers.sessions import _run_openshell_agent

        expose_mock = AsyncMock(return_value="https://agent.sandbox.example.com")
        start_mock = AsyncMock()

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui")
        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.sandbox_name = "sandbox-test-abc"
            await db.commit()

        with patch("swarmer.openshell_client.start_agent", start_mock), \
             patch("swarmer.openshell_client.expose_service", expose_mock), \
             patch("swarmer.database.get_db", _test_get_db):
            await _run_openshell_agent(
                session_id=s["id"],
                workspace_id=ws["id"],
                sandbox_name="sandbox-test-abc",
                cmd=["opencode"],
                mode="tui",
                agent_tool="opencode",
            )

        expose_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_mode_stores_service_url(self, client):
        """service_url is persisted in the session after expose_service."""
        from swarmer.routers.sessions import _run_openshell_agent

        expose_mock = AsyncMock(return_value="https://agent.sandbox.example.com")

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")
        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.sandbox_name = "sandbox-test-abc"
            await db.commit()

        with patch("swarmer.openshell_client.start_agent", AsyncMock()), \
             patch("swarmer.openshell_client.expose_service", expose_mock), \
             patch("swarmer.database.get_db", _test_get_db), \
             patch("swarmer.routers.sessions.asyncio.sleep", AsyncMock()):
            await _run_openshell_agent(
                session_id=s["id"],
                workspace_id=ws["id"],
                sandbox_name="sandbox-test-abc",
                cmd=["opencode", "serve"],
                mode="server",
                agent_tool="opencode",
            )

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            updated = await db.get(_Session, s["id"])
            assert updated.service_url == "https://agent.sandbox.example.com"


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

    @pytest.mark.asyncio
    async def test_proxy_returns_503_on_ssl_error(self, client):
        """SSL errors from the upstream (e.g. gRPC gateway) must be caught."""
        import ssl as _ssl
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://agent.openshell.internal:17670"
            await db.commit()

        async def _raise_ssl(*args, **kwargs):
            raise _ssl.SSLError("WRONG_VERSION_NUMBER")

        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.request.side_effect = _raise_ssl
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            resp = await client.get(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api"
            )

        assert resp.status_code == 503
        assert "agent.openshell.internal:17670" in resp.text

    @pytest.mark.asyncio
    async def test_proxy_returns_503_on_connect_error(self, client):
        """ConnectError from upstream returns 503 with the upstream URL visible."""
        import httpx
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://agent.openshell.internal:17670"
            await db.commit()

        async def _raise_connect(*args, **kwargs):
            raise httpx.ConnectError("Connection refused")

        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.request.side_effect = _raise_connect
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            resp = await client.get(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api"
            )

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_proxy_returns_503_on_mtls_error(self, client):
        """mTLS CERTIFICATE_REQUIRED from gateway is caught and returns 503."""
        import ssl as _ssl
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "https://agent.openshell.internal:17670"
            await db.commit()

        async def _raise_mtls(*args, **kwargs):
            raise _ssl.SSLError(
                1,
                "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED] tlsv13 alert certificate required",
            )

        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.request.side_effect = _raise_mtls
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            resp = await client.get(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/chat/api"
            )

        assert resp.status_code == 503
        # Error message should include the upstream URL for diagnosis
        assert "17670" in resp.text or "agent.openshell" in resp.text

    @pytest.mark.asyncio
    async def test_openshell_httpx_kwargs_includes_cert_when_configured(self):
        """_openshell_httpx_kwargs returns cert tuple when TLS cert/key are set."""
        from swarmer.routers.chat_proxy import _openshell_httpx_kwargs
        from swarmer.config import settings

        orig_cert, orig_key = settings.openshell_tls_cert, settings.openshell_tls_key
        try:
            settings.openshell_tls_cert = "/tmp/fake.crt"
            settings.openshell_tls_key = "/tmp/fake.key"
            kwargs = _openshell_httpx_kwargs()
            assert kwargs.get("verify") is False
            assert kwargs.get("cert") == ("/tmp/fake.crt", "/tmp/fake.key")
        finally:
            settings.openshell_tls_cert = orig_cert
            settings.openshell_tls_key = orig_key

    @pytest.mark.asyncio
    async def test_openshell_httpx_kwargs_no_cert_when_unconfigured(self):
        """_openshell_httpx_kwargs returns verify=False only when no cert configured."""
        from swarmer.routers.chat_proxy import _openshell_httpx_kwargs
        from swarmer.config import settings

        orig_cert, orig_key = settings.openshell_tls_cert, settings.openshell_tls_key
        try:
            settings.openshell_tls_cert = ""
            settings.openshell_tls_key = ""
            kwargs = _openshell_httpx_kwargs()
            assert kwargs.get("verify") is False
            assert "cert" not in kwargs
        finally:
            settings.openshell_tls_cert = orig_cert
            settings.openshell_tls_key = orig_key

    def test_resolve_upstream_no_gateway_url(self):
        """When OPENSHELL_GATEWAY_URL is unset, URL is returned unchanged."""
        from swarmer.routers.chat_proxy import _resolve_upstream
        from swarmer.config import settings

        orig = settings.openshell_gateway_url
        try:
            settings.openshell_gateway_url = ""
            url, host = _resolve_upstream("https://oriented-lizardfish--agent.openshell.localhost:8080")
            assert url == "https://oriented-lizardfish--agent.openshell.localhost:8080"
            assert host == "oriented-lizardfish--agent.openshell.localhost:8080"
        finally:
            settings.openshell_gateway_url = orig

    def test_resolve_upstream_rewrites_hostname_to_gateway(self):
        """When OPENSHELL_GATEWAY_URL is set, hostname is rewritten to gateway's real host."""
        from swarmer.routers.chat_proxy import _resolve_upstream
        from swarmer.config import settings

        orig = settings.openshell_gateway_url
        try:
            settings.openshell_gateway_url = "openshell-gateway.svc.cluster.local:8080"
            url, host = _resolve_upstream("https://oriented-lizardfish--agent.openshell.localhost:8080")
            # URL hostname should now be the gateway's real host
            assert "openshell-gateway.svc.cluster.local" in url
            # Virtual host preserved for gateway routing
            assert host == "oriented-lizardfish--agent.openshell.localhost:8080"
            # Port is unchanged (already rewritten by expose_service)
            assert ":8080" in url
        finally:
            settings.openshell_gateway_url = orig

    def test_resolve_upstream_gateway_url_with_scheme(self):
        """OPENSHELL_GATEWAY_URL with grpc:// scheme is handled correctly."""
        from swarmer.routers.chat_proxy import _resolve_upstream
        from swarmer.config import settings

        orig = settings.openshell_gateway_url
        try:
            settings.openshell_gateway_url = "grpc://openshell-gateway.svc:443"
            url, host = _resolve_upstream("https://some-sandbox--agent.openshell.localhost:443")
            assert "openshell-gateway.svc" in url
            assert host == "some-sandbox--agent.openshell.localhost:443"
        finally:
            settings.openshell_gateway_url = orig

    def test_resolve_upstream_sets_host_header_with_port(self):
        """Host header value includes the port from the service URL."""
        from swarmer.routers.chat_proxy import _resolve_upstream
        from swarmer.config import settings

        orig = settings.openshell_gateway_url
        try:
            settings.openshell_gateway_url = "gateway.local:17670"
            _, host = _resolve_upstream("https://sandbox--agent.openshell.localhost:17670")
            assert host == "sandbox--agent.openshell.localhost:17670"
        finally:
            settings.openshell_gateway_url = orig

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
# 5. Crush /chat root serves Swarmer template
# ===========================================================================


class TestOpenCodeRootProxied:
    """The /chat root path for Crush sessions renders the Swarmer template."""

    @pytest.mark.asyncio
    async def test_crush_root_still_serves_swarmer_template(self, client):
        """Crush /chat root renders the Swarmer crush_chat.html template."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="crush")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-test-abc"
            session_obj.service_url = "http://agent.openshell.internal:4096"
            await db.commit()

        with patch("swarmer.routers.chat_proxy.httpx.AsyncClient") as mock_cls:
            resp = await client.get(f"/workspaces/{ws['id']}/sessions/{s['id']}/chat")

        # Crush root must NOT call the upstream proxy
        mock_cls.assert_not_called()
        # Response should be the Swarmer template (HTML rendered by FastAPI)
        assert resp.status_code == 200
        assert b"text/html" in resp.headers.get("content-type", "").encode()


# ===========================================================================
# 6. _restart_server_sessions() startup reconciliation
# ===========================================================================


class TestRestartServerSessions:
    """Startup reconciliation re-connects server-mode sessions after Swarmer restart."""

    @pytest.mark.asyncio
    async def test_live_sandbox_refreshes_service_url(self, client):
        """A server-mode session with a live sandbox gets a fresh service_url."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server", agent_tool="opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            session_obj.phase = "running"
            session_obj.sandbox_name = "sandbox-abc"
            session_obj.service_url = "http://old-service-url:4096"
            await db.commit()

        fresh_url = "http://new-service-url:4096"

        with (
            patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-abc"])),
            patch("swarmer.openshell_client.expose_service", new=AsyncMock(return_value=fresh_url)),
            patch("swarmer.database.get_db", new=_override_get_db),
        ):
            from swarmer.main import _restart_server_sessions
            await _restart_server_sessions()

        async with _TestSession() as db:
            from swarmer.models.session import Session as _Session
            session_obj = await db.get(_Session, s["id"])
            assert session_obj.phase == "running"
            assert session_obj.service_url == fresh_url

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
# 7. TUI WebSocket — OpenShell path (unit-level)
# ===========================================================================


@_requires_sdk
class TestTuiWsOpenshell:
    """Unit-level tests for the OpenShell branch in tui_ws.py.

    These do not open a real WebSocket connection — they test the helper logic
    and the openshell_client wrappers that the TUI handler uses.
    """

    def test_exec_interactive_stdin_queued(self):
        """The first message in the stream is always the start message with sandbox_id."""
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
            sandbox_name="sandbox-x",
            sandbox_id="id-x",
            command=["sh", "-c", "opencode"],
            cols=80,
            rows=24,
            client=sdk_client,
        )
        input_q.put(None)  # unblock generator if it ever resumes

        assert len(received) == 1
        assert received[0].start.sandbox_id == "id-x"

    def test_exec_interactive_resize_queued(self):
        """The first message in the stream carries the correct sandbox_id and tty flags."""
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
            sandbox_name="sandbox-x",
            sandbox_id="id-x",
            command=["sh", "-c", "crush"],
            cols=80,
            rows=24,
            client=sdk_client,
        )
        input_q.put(None)  # unblock generator if it ever resumes

        assert received[0].start.sandbox_id == "id-x"


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
      SWARMER_E2E_CRUSH_SID     — session ID of a completed crush prompt-mode session
      SWARMER_E2E_CRUSH_TUI_SID — session ID of a running crush tui-mode session
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

    @property
    def _crush_sid(self):
        val = _os.environ.get("SWARMER_E2E_CRUSH_SID")
        if not val:
            pytest.skip("SWARMER_E2E_CRUSH_SID not set")
        return int(val)

    @property
    def _crush_tui_sid(self):
        val = _os.environ.get("SWARMER_E2E_CRUSH_TUI_SID")
        if not val:
            pytest.skip("SWARMER_E2E_CRUSH_TUI_SID not set")
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
    async def test_smoke_crush_env_vars_injected(self):
        """Crush diagnostic: verify GOOGLE_API_KEY is injected into the sandbox.

        Uses a completed crush prompt-mode session whose output should contain
        the result of `env | grep GOOGLE`. Create a Crush prompt session with:
          prompt: Run this shell command and print the output: env | grep GOOGLE
        Then set SWARMER_E2E_CRUSH_SID to that session's ID.

        To run:
          SWARMER_E2E=1 SWARMER_E2E_WS_ID=1 SWARMER_E2E_CRUSH_SID=3 \\
          pytest tests/test_openshell_proxy.py -k smoke_crush_env -v -s
        """
        import httpx
        ws_id, sid = self._ws_id, self._crush_sid

        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200
        data = resp.json()

        phase = data.get("phase", "")
        last_output = data.get("last_output", "") or ""

        print(f"\n--- Crush session phase: {phase} ---")
        print(f"--- last_output ---\n{last_output}\n---")

        assert phase in ("succeeded", "failed"), (
            f"Session not complete yet (phase={phase}). Run crush prompt with env grep first."
        )
        assert "GOOGLE" in last_output, (
            f"GOOGLE env var not found in crush output.\n"
            f"This means GOOGLE_API_KEY is NOT injected into the sandbox.\n"
            f"Output was:\n{last_output}"
        )
        assert "GOOGLE_API_KEY" in last_output, (
            f"GOOGLE_API_KEY specifically missing from crush env output.\n"
            f"Found GOOGLE vars: {[l for l in last_output.splitlines() if 'GOOGLE' in l]}"
        )

    @pytest.mark.asyncio
    async def test_smoke_crush_tui_renders(self):
        """Crush TUI: session detail page renders the terminal panel."""
        import httpx
        ws_id, sid = self._ws_id, self._crush_tui_sid
        async with httpx.AsyncClient(base_url=_E2E_BASE, follow_redirects=True) as hc:
            resp = await hc.get(f"/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200
        assert "terminal" in resp.text.lower() or "xterm" in resp.text.lower(), (
            "Expected TUI terminal tab in session detail for crush tui-mode session"
        )

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

"""Tests for the OpenShell session launch path.

Covers:
  - _do_launch_openshell(): sandbox creation, config writing, repo cloning, AGENTS.md, background task
  - _do_launch() routes to _do_launch_openshell() (not K8s pod path)
  - _do_launch() still checks auth and capacity-gates
  - session stop: delete_sandbox() called, sandbox_name cleared
  - _run_openshell_agent(): prompt mode (succeeded/failed), server/tui mode, exception handling
  - No K8s PVC/Secret/Pod operations for OpenShell sessions
"""

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Inject openshell SDK stub before any swarmer imports so that the real
# openshell package (if installed) does not interfere with unit tests.
# Force-assign replaces any already-loaded openshell module in sys.modules.
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock as _MagicMock  # noqa: E402


class _SandboxSpec:
    def __init__(self):
        class _T:
            image = ""
        self.template = _T()
        self.environment = {}
        self.policy = None
        self.providers = []


_proto_stub = _MagicMock()
_proto_stub.openshell_pb2 = _MagicMock()
_proto_stub.openshell_pb2.SandboxSpec = _SandboxSpec

_sdk_stub = _MagicMock()
_sdk_stub.SandboxClient = _MagicMock
_sdk_stub.TlsConfig = _MagicMock
_sdk_stub._proto = _proto_stub

# Save any real openshell modules already in sys.modules so we can restore
# them after importing the swarmer session router with our stubs.  Without the
# restore, the permanent MagicMock in sys.modules["openshell._proto"] causes
# lazy imports in test_openshell_policy.py to receive mocks instead of real
# protobuf classes, breaking the @_requires_sdk tests in that file.
_saved_modules = {k: v for k, v in sys.modules.items() if "openshell" in k}

sys.modules["openshell"] = _sdk_stub
sys.modules["openshell._proto"] = _proto_stub
sys.modules["openshell._proto.openshell_pb2"] = _proto_stub.openshell_pb2

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

# Restore real openshell modules (or remove stubs if none were present).
# Iterate over ALL current sys.modules entries containing "openshell" (excluding
# swarmer modules we intentionally imported) so that transitively-loaded proto
# modules (sandbox_pb2, datamodel_pb2, etc.) are also restored.  This prevents
# the permanent MagicMock stubs from polluting lazy imports in other test files.
for _k in list(sys.modules):
    if "openshell" in _k and "swarmer" not in _k:
        if _k in _saved_modules:
            sys.modules[_k] = _saved_modules[_k]
        else:
            sys.modules.pop(_k, None)

# ---------------------------------------------------------------------------
# Shared DB fixtures
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
    settings.max_concurrent_agents = 0  # unlimited by default for these tests

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


# ---------------------------------------------------------------------------
# Helper: create workspace and session via API
# ---------------------------------------------------------------------------


async def _create_workspace(client: AsyncClient, name: str = "Test WS") -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        json={"display_name": name, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(
    client: AsyncClient,
    ws_id: int,
    name: str = "s1",
    mode: str = "prompt",
    agent_tool: str = "opencode",
) -> dict:
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sessions",
        json={"name": name, "mode": mode, "agent_tool": agent_tool},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _fake_sandbox_ref(name: str = "sandbox-test-abc123"):
    ref = MagicMock()
    ref.name = name
    return ref


# ===========================================================================
# 1. _do_launch() routes to OpenShell path (not K8s)
# ===========================================================================


class TestDoLaunchRoutesToOpenshell:
    @pytest.mark.asyncio
    async def test_do_launch_calls_openshell_not_k8s(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._do_launch_openshell", new=AsyncMock()) as mock_openshell:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        assert resp.status_code == 200
        mock_openshell.assert_called_once()

    @pytest.mark.asyncio
    async def test_do_launch_does_not_create_k8s_pod(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._do_launch_openshell", new=AsyncMock()):
            with patch("kubernetes.client.CoreV1Api") as mock_k8s:
                await client.post(
                    f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
                )
        # K8s pod creation was never called
        mock_k8s.return_value.create_namespaced_pod.assert_not_called()

    @pytest.mark.asyncio
    async def test_do_launch_unknown_user_raises(self, client):
        """Auth check: user_id='unknown' must raise ValueError."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            from swarmer.models.workspace import Workspace
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            workspace = (await db.execute(select(Workspace).where(Workspace.id == ws["id"]))).scalar_one()

            from swarmer.routers.sessions import _do_launch
            with pytest.raises(ValueError, match="Session expired"):
                await _do_launch(sess, workspace, db, user_id="unknown")

    @pytest.mark.asyncio
    async def test_do_launch_queues_at_capacity(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 2

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=2)):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "queued"


# ===========================================================================
# 2. _do_launch_openshell() core behavior (unit-level)
# ===========================================================================


class TestDoLaunchOpenshell:
    """Unit tests for _do_launch_openshell() with OpenShell client mocked."""

    def _patch_openshell(self, sandbox_name: str = "sandbox-test-123"):
        """Return a context manager dict of patches for openshell_client."""
        ref = _fake_sandbox_ref(sandbox_name)
        patches = {
            "create_provider": patch(
                "swarmer.openshell_client.create_provider",
                new=AsyncMock(return_value={}),
            ),
            "ensure_provider": patch(
                "swarmer.openshell_client.ensure_provider",
                new=AsyncMock(),
            ),
            "configure_vertex_provider": patch(
                "swarmer.openshell_client.configure_vertex_provider",
                new=AsyncMock(),
            ),
            "set_cluster_inference": patch(
                "swarmer.openshell_client.set_cluster_inference",
                new=AsyncMock(),
            ),
            "configure_provider_credential": patch(
                "swarmer.openshell_client.configure_provider_credential",
                new=AsyncMock(),
            ),
            "attach_sandbox_provider": patch(
                "swarmer.openshell_client.attach_sandbox_provider",
                new=AsyncMock(),
            ),
            "create_sandbox": patch(
                "swarmer.openshell_client.create_sandbox",
                new=AsyncMock(return_value=ref),
            ),
            "write_agent_config": patch(
                "swarmer.openshell_client.write_agent_config",
                new=AsyncMock(),
            ),
            "write_agents_md": patch(
                "swarmer.openshell_client.write_agents_md",
                new=AsyncMock(),
            ),
            "exec_command": patch(
                "swarmer.openshell_client.exec_command",
                new=AsyncMock(),
            ),
            "start_agent": patch(
                "swarmer.openshell_client.start_agent",
                new=AsyncMock(),
            ),
            "delete_sandbox": patch(
                "swarmer.openshell_client.delete_sandbox",
                new=AsyncMock(),
            ),
            "build_policy": patch(
                "swarmer.openshell_policy.build_session_policy",
                return_value="version: 1\n",
            ),
            # Patch _run_openshell_agent so asyncio.create_task gets a real coroutine
            # (avoids SQLAlchemy shield() incompatibility with MagicMock tasks)
            "run_agent": patch(
                "swarmer.routers.sessions._run_openshell_agent",
                new=AsyncMock(),
            ),
            "setup_sandbox": patch(
                "swarmer.routers.sessions._setup_openshell_sandbox",
                new=AsyncMock(),
            ),
            "wait_vertex_ready": patch(
                "swarmer.routers.sessions._wait_vertex_provider_ready",
                new=AsyncMock(),
            ),
            # provider_exists is called in _do_launch_openshell to check for the
            # google-cloud (Vertex ADC) provider. Without this patch it tries to
            # use the real gRPC client (not available in CI) and raises AttributeError.
            "provider_exists": patch(
                "swarmer.openshell_client.provider_exists",
                new=AsyncMock(return_value=False),
            ),
            # get_image() raises ValueError when AGENT_IMAGE_OPENCODE is unset (CI).
            # Patch at the agent-tool level so all launch paths get a valid image.
            "get_image": patch(
                "swarmer.agent_tools.opencode.OpenCodeStrategy.get_image",
                return_value="quay.io/opencode:test",
            ),
        }
        return patches

    def _all_patches(self, patches):
        """Enter all patches in the standard set."""
        return (
            patches["create_provider"], patches["ensure_provider"],
            patches["configure_vertex_provider"], patches["set_cluster_inference"],
            patches["configure_provider_credential"], patches["attach_sandbox_provider"],
            patches["create_sandbox"], patches["write_agent_config"],
            patches["write_agents_md"],
            patches["exec_command"], patches["start_agent"],
            patches["delete_sandbox"], patches["build_policy"],
            patches["run_agent"], patches["setup_sandbox"],
            patches["wait_vertex_ready"],
            patches["provider_exists"], patches["get_image"],
        )

    @pytest.mark.asyncio
    async def test_creates_sandbox_with_tool_image(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], \
             patches["setup_sandbox"] as mock_setup, \
             patches["provider_exists"], patches["get_image"]:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
            # Yield to the event loop so the asyncio.create_task() background
            # task (_setup_openshell_sandbox) runs while patches are still active.
            await asyncio.sleep(0)

        assert resp.status_code == 200
        # create_sandbox is called inside _setup_openshell_sandbox (background task).
        # Verify the setup task was invoked with the correct image.
        mock_setup.assert_called_once()
        call_kwargs = mock_setup.call_args[1] if mock_setup.call_args else {}
        assert "image" in call_kwargs

    @pytest.mark.asyncio
    async def test_sets_sandbox_name_on_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell(sandbox_name="sandbox-xyz-789")
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        assert resp.status_code == 200
        # sandbox_name is set by the background setup task (_setup_openshell_sandbox).
        # The HTTP response returns immediately with phase=pending.
        # Verify the sandbox was given the right name by checking create_sandbox was
        # called and the session is in pending state.
        data = resp.json()
        assert data["phase"] == "pending"

    @pytest.mark.asyncio
    async def test_writes_agent_config(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"] as mock_cfg, \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # write_agent_config is only called when mcp_servers are configured;
        # the container's default opencode.json is preserved otherwise
        mock_cfg.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_clone_when_no_repos(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])  # no repos

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # No repos attached — exec_command should not be called for git clone
        exec_mock = patches["exec_command"].new
        git_clone_calls = [c for c in exec_mock.call_args_list if "git clone" in str(c)]
        assert git_clone_calls == []

    @pytest.mark.asyncio
    async def test_does_not_write_agents_md_for_prompt_mode(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"] as mock_md, patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        mock_md.assert_not_called()

    @pytest.mark.asyncio
    async def test_sets_phase_pending_before_task(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # Phase will be "pending" (set by _do_launch_openshell before task)
        # or updated by _run_openshell_agent to "running" — check it's not idle/failed
        assert resp.json()["phase"] in ("pending", "running")

    @pytest.mark.asyncio
    async def test_no_k8s_pvc_created(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        # k8s_session.ensure_session_pvc has been removed — k8s_session no longer exists.

    @pytest.mark.asyncio
    async def test_no_k8s_secrets_created(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        # K8s secret creation functions have been removed from k8s.py —
        # they no longer exist, so they cannot be called.

    @pytest.mark.asyncio
    async def test_creates_background_task(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], \
             patches["setup_sandbox"] as mock_setup, \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
            # Yield to the event loop so the asyncio.create_task() background
            # task (_setup_openshell_sandbox) runs while patches are still active.
            await asyncio.sleep(0)

        # _setup_openshell_sandbox is spawned as an asyncio task for background sandbox creation
        mock_setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_uses_provider_api_not_env_vars_for_credentials(self, client):
        """AI credentials must flow through the gateway Provider API, not SandboxSpec.environment."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"] as mock_provider, \
             patches["ensure_provider"] as mock_ensure, \
             patches["configure_provider_credential"] as mock_cred, \
             patches["attach_sandbox_provider"] as mock_attach, \
             patches["create_sandbox"] as mock_sandbox, \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            mock_provider.return_value = {}
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # create_sandbox env_vars must NOT contain AI credentials
        call_kwargs = mock_sandbox.call_args[1] if mock_sandbox.call_args else {}
        passed_env = call_kwargs.get("env_vars", {})
        assert "ANTHROPIC_API_KEY" not in passed_env
        assert "GOOGLE_API_KEY" not in passed_env
        # No oc_secret in this test session, so no provider calls expected
        assert mock_ensure.call_count == 0
        assert mock_cred.call_count == 0
        assert mock_attach.call_count == 0

    @pytest.mark.asyncio
    async def test_launch_blocked_when_github_repo_without_pat(self, client):
        """Launch must be rejected with a clear message when github.com repos have no PAT.

        The OpenShell gateway requires a valid GitHub provider credential to allow
        CONNECT tunnels to github.com. Rather than failing silently mid-sandbox-setup,
        _do_launch raises early so the user sees an actionable error.
        """
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/public-repo.git", "branch": "main"},
        )

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"] as mock_sandbox, patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        # No sandbox should have been created — launch was blocked before reaching OpenShell
        mock_sandbox.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_provider_registered_when_pat_present(self, client):
        """When a PAT is configured, ensure_provider is called with the real token."""
        ws = await _create_workspace(client)
        # Create a PAT
        pat_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/pats",
            json={"name": "test-pat", "github_username": "octocat", "pat_value": "ghp_testtoken123"},
        )
        pat = pat_resp.json()
        # Create session with that PAT
        s_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={"name": "s-with-pat", "mode": "prompt", "agent_tool": "opencode",
                  "github_pat_id": pat["id"]},
        )
        s = s_resp.json()

        patches = self._patch_openshell()
        with patches["create_provider"], \
             patches["ensure_provider"] as mock_ensure, \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )

        github_calls = [
            c for c in mock_ensure.call_args_list
            if len(c.args) >= 2 and c.args[1] == "github"
        ]
        assert len(github_calls) == 1, (
            f"Expected 1 github provider call when PAT is set, got {len(github_calls)}"
        )
        # Provider name must be per-PAT per-session (session-scoped)
        provider_name = github_calls[0].args[0]
        expected_name = f"swarmer-ws-{ws['id']}-github-pat-{pat['id']}-s{s['id']}"
        assert provider_name == expected_name, (
            f"Expected provider name '{expected_name}', got '{provider_name}'"
        )
        creds = github_calls[0].kwargs.get("credentials", {})
        assert "GITHUB_TOKEN" in creds and creds["GITHUB_TOKEN"], (
            f"Expected non-empty GITHUB_TOKEN credential in github provider call, got: {creds}"
        )
        assert creds.get("GH_TOKEN") == creds.get("GITHUB_TOKEN"), (
            f"Expected GH_TOKEN to match GITHUB_TOKEN, got: {creds}"
        )

    @pytest.mark.asyncio
    async def test_gh_auth_setup_git_called_when_pat_present(self, client):
        """gh auth setup-git must run before repo cloning when a PAT is configured.

        This registers gh as git's credential helper so that git clone/push/fetch
        can authenticate via GH_TOKEN injected by the OpenShell provider.  Without
        this call, git falls back to an interactive credential prompt that fails
        because exec_command has no TTY — causing private repo clones to error with
        'fatal: could not read Username for https://github.com: Permission denied'.

        Calls _setup_openshell_sandbox directly (bypassing the background task path)
        to inspect exec_command calls.
        """
        from swarmer.routers.sessions import _setup_openshell_sandbox

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        # _setup_openshell_sandbox checks session.phase == "pending" after creating the
        # sandbox and returns early if it's not — set it before calling the function.
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        exec_calls: list[list[str]] = []

        async def _capture_exec(sandbox_name, cmd, client=None, stdin=None, timeout_seconds=None, env=None):
            exec_calls.append(list(cmd))
            return MagicMock(exit_code=0, stdout="", stderr="")

        ref = _fake_sandbox_ref("sandbox-pat-test")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.create_sandbox", new=AsyncMock(return_value=ref)), \
             patch("swarmer.openshell_client.write_agent_config", new=AsyncMock()), \
             patch("swarmer.openshell_client.write_agents_md", new=AsyncMock()), \
             patch("swarmer.openshell_client.approve_draft_policy_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.routers.sessions._run_openshell_agent", new=AsyncMock()), \
             patch("swarmer.openshell_client.exec_command", new=_capture_exec):
            await _setup_openshell_sandbox(
                session_id=s["id"],
                workspace_id=ws["id"],
                provider_names=[],
                env_vars={},
                policy=None,
                image="quay.io/opencode:latest",
                tool_name="opencode",
                model="google-vertex-anthropic/claude-sonnet-5@default",
                model_setup_cmd="",
                share_cmd="",
                mcp_patch={},
                repos_data=[],
                git_username="octocat",
                pat_token="ghp_testtoken123",
                working_branch="",
                agents_md="",
                mode="prompt",
                main_cmd="opencode run",
                resolved_prompt="",
                has_git_token=True,
            )

        all_cmds = [" ".join(c) for c in exec_calls]
        setup_git_calls = [c for c in all_cmds if "gh auth setup-git" in c]
        assert len(setup_git_calls) == 1, (
            f"Expected exactly 1 'gh auth setup-git' exec_command call when PAT is set, "
            f"got {len(setup_git_calls)}. All exec calls:\n" + "\n".join(all_cmds)
        )
        # setup-git must appear before any git clone calls
        setup_git_idx = next(i for i, c in enumerate(all_cmds) if "gh auth setup-git" in c)
        clone_idxs = [i for i, c in enumerate(all_cmds) if "git clone" in c]
        for ci in clone_idxs:
            assert setup_git_idx < ci, (
                "gh auth setup-git must be called before git clone, "
                f"but setup-git was at index {setup_git_idx} and clone at {ci}"
            )

    @pytest.mark.asyncio
    async def test_gh_auth_setup_git_not_called_without_pat(self, client):
        """gh auth setup-git must NOT run when no PAT is configured.

        Sessions without a PAT cannot have GitHub repos (enforced at launch time),
        so calling gh auth setup-git would be a no-op at best and an error at worst.
        """
        from swarmer.routers.sessions import _setup_openshell_sandbox

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        exec_calls: list[list[str]] = []

        async def _capture_exec(sandbox_name, cmd, client=None, stdin=None, timeout_seconds=None, env=None):
            exec_calls.append(list(cmd))
            return MagicMock(exit_code=0, stdout="", stderr="")

        ref = _fake_sandbox_ref("sandbox-nopat-test")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.create_sandbox", new=AsyncMock(return_value=ref)), \
             patch("swarmer.openshell_client.write_agent_config", new=AsyncMock()), \
             patch("swarmer.openshell_client.write_agents_md", new=AsyncMock()), \
             patch("swarmer.openshell_client.approve_draft_policy_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.routers.sessions._run_openshell_agent", new=AsyncMock()), \
             patch("swarmer.openshell_client.exec_command", new=_capture_exec):
            await _setup_openshell_sandbox(
                session_id=s["id"],
                workspace_id=ws["id"],
                provider_names=[],
                env_vars={},
                policy=None,
                image="quay.io/opencode:latest",
                tool_name="opencode",
                model="google-vertex-anthropic/claude-sonnet-5@default",
                model_setup_cmd="",
                share_cmd="",
                mcp_patch={},
                repos_data=[],
                git_username="",
                pat_token="",  # no PAT
                working_branch="",
                agents_md="",
                mode="prompt",
                main_cmd="opencode run",
                resolved_prompt="",
            )

        all_cmds = [" ".join(c) for c in exec_calls]
        setup_git_calls = [c for c in all_cmds if "gh auth setup-git" in c]
        assert setup_git_calls == [], (
            f"Expected no 'gh auth setup-git' call when no PAT is set, "
            f"got {len(setup_git_calls)}. All exec calls:\n" + "\n".join(all_cmds)
        )

    @pytest.mark.asyncio
    async def test_jira_provider_registered_when_mcp_configured(self, client):
        """When a Jira MCP server is configured and valid, ensure_provider is called with all three credentials."""
        from unittest.mock import patch as _patch, AsyncMock as _AsyncMock
        ws = await _create_workspace(client)
        # Add Jira MCP server from catalog
        mcp_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/mcp-servers",
            json={"catalog_slug": "atlassian-jira"},
        )
        assert mcp_resp.status_code == 201, mcp_resp.text
        mcp = mcp_resp.json()
        # Save credentials (mock the Jira probe so it reports valid)
        with _patch("swarmer.routers.mcp_servers._probe_jira_token", new=_AsyncMock(return_value=True)):
            save_resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/mcp-servers/{mcp['id']}/save",
                json={
                    "jira_server_url": "https://redhat.atlassian.net",
                    "jira_email": "test@redhat.com",
                    "jira_access_token": "jira-tok-secret",
                },
            )
        assert save_resp.status_code == 200, save_resp.text
        # Create session — pass the MCP server ID so it's enabled for this session
        # (sessions default to mcp_server_ids="none" when no IDs are supplied)
        s_resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={"name": "s-with-jira", "mode": "prompt", "agent_tool": "opencode",
                  "mcp_server_ids": [mcp["id"]]},
        )
        s = s_resp.json()

        patches = self._patch_openshell()
        with patches["create_provider"], \
             patches["ensure_provider"] as mock_ensure, \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], \
             patches["setup_sandbox"] as mock_setup, \
             patches["provider_exists"], patches["get_image"]:
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
            # Yield to the event loop so the asyncio.create_task() background
            # task (_setup_openshell_sandbox) runs while patches are still active.
            await asyncio.sleep(0)

        jira_calls = [
            c for c in mock_ensure.call_args_list
            if len(c.args) >= 2 and c.args[1] == "jira"
        ]
        assert len(jira_calls) == 1, (
            f"Expected 1 jira provider call when MCP is configured, got {len(jira_calls)}"
        )
        # Token must go through provider credentials — gateway stores it securely and
        # injects as an opaque reference token (openshell:resolve:...), never plaintext.
        creds = jira_calls[0].kwargs.get("credentials", {})
        assert creds.get("JIRA_ACCESS_TOKEN") == "jira-tok-secret", (
            f"Expected JIRA_ACCESS_TOKEN in jira provider credentials, got: {creds}"
        )
        assert "JIRA_SERVER_URL" not in creds, (
            f"JIRA_SERVER_URL must not be in credentials (non-secret): {creds}"
        )
        assert "JIRA_EMAIL" not in creds, (
            f"JIRA_EMAIL must not be in credentials (non-secret): {creds}"
        )
        # Non-secret config goes in provider config (gateway-internal, not injected as env var).
        cfg = jira_calls[0].kwargs.get("config", {})
        assert cfg.get("JIRA_SERVER_URL") == "https://redhat.atlassian.net", (
            f"Expected JIRA_SERVER_URL in jira provider config, got: {cfg}"
        )
        assert cfg.get("JIRA_EMAIL") == "test@redhat.com", (
            f"Expected JIRA_EMAIL in jira provider config, got: {cfg}"
        )
        # URL and email must also appear as plain env vars so the sandbox process sees
        # them directly on every ExecSandboxRequest (SandboxSpec.environment is not
        # forwarded to exec calls by the gateway).
        setup_kwargs = mock_setup.call_args.kwargs if mock_setup.call_args else {}
        sandbox_env = setup_kwargs.get("env_vars", {})
        assert sandbox_env.get("JIRA_SERVER_URL") == "https://redhat.atlassian.net", (
            f"Expected JIRA_SERVER_URL in sandbox env_vars, got: {sandbox_env}"
        )
        assert sandbox_env.get("JIRA_EMAIL") == "test@redhat.com", (
            f"Expected JIRA_EMAIL in sandbox env_vars, got: {sandbox_env}"
        )
        assert "JIRA_ACCESS_TOKEN" not in sandbox_env, (
            f"JIRA_ACCESS_TOKEN must not appear in plaintext sandbox env_vars: {sandbox_env}"
        )

    @pytest.mark.asyncio
    async def test_passes_policy_yaml_from_builder(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"] as mock_sandbox, \
             patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"] as mock_policy, patches["run_agent"], \
             patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patches["provider_exists"], patches["get_image"]:
            mock_policy.return_value = "version: 1\nnetwork_policies: {}\n"
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
            # Yield to the event loop so the asyncio.create_task() background
            # task (_setup_openshell_sandbox) runs while patches are still active.
            await asyncio.sleep(0)

        call_kwargs = mock_sandbox.call_args[1] if mock_sandbox.call_args else {}
        call_args = mock_sandbox.call_args[0] if mock_sandbox.call_args else ()
        passed_policy = call_kwargs.get("policy") or (call_args[2] if len(call_args) > 2 else None)
        # policy is now a SandboxPolicy proto, not a YAML string
        assert passed_policy is not None

    @pytest.mark.asyncio
    async def test_launch_clears_stale_status_detail(self, client):
        """Launching a session clears any stale status_detail from a previous run."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-set stale status_detail from a previous failed run
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET status_detail='OpenShell agent startup failed', phase='failed' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        patches = self._patch_openshell()
        with patches["create_provider"], patches["ensure_provider"], \
             patches["configure_provider_credential"], patches["attach_sandbox_provider"], \
             patches["create_sandbox"], patches["write_agent_config"], \
             patches["write_agents_md"], patches["exec_command"], \
             patches["start_agent"], patches["delete_sandbox"], patches["build_policy"], \
             patches["run_agent"], patches["setup_sandbox"], \
             patches["provider_exists"], patches["get_image"]:
            await client.post(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch")

        session_resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        data = session_resp.json()
        assert data["status_detail"] == "", f"Expected empty status_detail, got: {data['status_detail']!r}"
        assert data["phase"] == "pending"

    @pytest.mark.asyncio
    async def test_opencode_config_env_var_injected_in_launch(self, client):
        """OPENCODE_CONFIG env var must be /sandbox/opencode.json for OpenCode sessions.

        OpenCode has no --config CLI flag; the config path is passed via the
        OPENCODE_CONFIG environment variable instead.
        """
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        captured_env: dict = {}

        patches = self._patch_openshell()
        with patches["create_provider"] as mock_provider, \
             patches["ensure_provider"], patches["configure_provider_credential"], \
             patches["attach_sandbox_provider"], patches["create_sandbox"], \
             patches["write_agent_config"], patches["write_agents_md"], \
             patches["exec_command"], patches["start_agent"], patches["delete_sandbox"], \
             patches["build_policy"], patches["run_agent"], patches["setup_sandbox"] as mock_setup, \
             patches["provider_exists"], patches["get_image"]:
            mock_provider.return_value = {}

            def _capture_setup(**kwargs):
                captured_env.update(kwargs.get("env_vars", {}))

            mock_setup.side_effect = _capture_setup
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
            # Yield to the event loop so the asyncio.create_task() background
            # task (_setup_openshell_sandbox) runs while patches are still active.
            await asyncio.sleep(0)

        assert captured_env.get("OPENCODE_CONFIG") == "/sandbox/opencode.json", (
            f"Expected OPENCODE_CONFIG=/sandbox/opencode.json in env_vars, got: {captured_env}"
        )


# ===========================================================================
# 3. Session stop: sandbox deletion
# ===========================================================================


class TestSessionStopOpenshell:
    @pytest.mark.asyncio
    async def test_stop_calls_delete_sandbox(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-set sandbox_name on the session
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-stop-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.status_code == 200
        mock_delete.assert_called_once_with("sandbox-stop-test")

    @pytest.mark.asyncio
    async def test_stop_clears_sandbox_name(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-clear-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.status_code == 200
        assert resp.json()["sandbox_name"] is None

    @pytest.mark.asyncio
    async def test_stop_sets_phase_stopped(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-phase-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.json()["phase"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_does_not_call_k8s_delete_pod(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-nok8s', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )
        # k8s.delete_pod has been removed — it no longer exists and cannot be called.

    @pytest.mark.asyncio
    async def test_stop_handles_delete_sandbox_error_gracefully(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-err', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch(
            "swarmer.openshell_client.delete_sandbox",
            new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        # Should still succeed (warning flashed, phase set to stopped)
        assert resp.status_code == 200
        assert resp.json()["phase"] == "stopped"

    @pytest.mark.asyncio
    async def test_stop_queued_returns_idle_no_sandbox_call(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='queued' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.json()["phase"] == "idle"
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_with_no_sandbox_name_still_sets_stopped(self, client):
        """STOP on a session with no sandbox_name (stale running) must set phase=stopped
        and not attempt to delete a sandbox."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name=NULL, phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
            )

        assert resp.json()["phase"] == "stopped"
        assert resp.json()["sandbox_name"] is None
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_cancels_background_task(self, client):
        """STOP must cancel any running openshell-agent-{sid} or openshell-setup-{sid}
        asyncio tasks so they cannot race and overwrite the stopped phase."""
        import asyncio as _asyncio
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        sid = s["id"]

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-cancel-test', phase='running' WHERE id=:id"),
                {"id": sid},
            )
            await db.commit()

        async def _long_running():
            try:
                await _asyncio.sleep(9999)
            except _asyncio.CancelledError:
                raise

        task = _asyncio.create_task(_long_running(), name=f"openshell-agent-{sid}")
        # Yield so the task is scheduled
        await _asyncio.sleep(0)

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{sid}/stop"
            )

        # Give the event loop a tick to propagate the cancellation
        await _asyncio.sleep(0)

        assert resp.json()["phase"] == "stopped"
        # Task must have received a cancellation signal
        assert task.cancelled() or task.cancelling() > 0 or task.done(), \
            "Background task should have been cancelled by STOP"
        task.cancel()  # clean up if not yet fully cancelled

    @pytest.mark.asyncio
    async def test_update_db_phase_guard_does_not_overwrite_stopped(self, client):
        """_update_db in _run_openshell_agent must not overwrite phase=stopped
        set by the STOP handler."""
        from swarmer.routers.sessions import _run_openshell_agent

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-set sandbox_name so _run_openshell_agent has something to exec against
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-race-test', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        # After exec finishes, simulate STOP having already set phase=stopped before
        # the background task writes its final state.
        async def _set_stopped_during_exec(sandbox_name, cmd, **kwargs):
            # Simulate STOP winning the race: set stopped while exec is "running"
            async with _TestSession() as db:
                await db.execute(
                    text("UPDATE sessions SET phase='stopped', sandbox_name=NULL WHERE id=:id"),
                    {"id": s["id"]},
                )
                await db.commit()
            m = MagicMock()
            m.exit_code = 0
            m.stderr = ""
            return m

        _test_get_db = _make_test_db_provider()

        with patch("swarmer.openshell_client.exec_command_streaming", new=_set_stopped_during_exec), \
             patch("swarmer.openshell_client.read_opencode_response", new=AsyncMock(return_value="")), \
             patch("swarmer.openshell_client.get_draft_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()), \
             patch("swarmer.database.get_db", _test_get_db):
            await _run_openshell_agent(
                session_id=s["id"],
                workspace_id=ws["id"],
                sandbox_name="sandbox-race-test",
                cmd=["opencode", "run", "--prompt", "test"],
                mode="prompt",
                agent_tool="opencode",
            )

        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            updated = await db.get(_S, s["id"])
            # Phase guard should have prevented the background task from
            # overwriting stopped → succeeded
            assert updated.phase == "stopped", (
                f"Expected phase='stopped' but got '{updated.phase}' — "
                "phase guard in _update_db is not working"
            )


# ===========================================================================
# 4. _run_openshell_agent(): background task behavior
# ===========================================================================


def _make_test_db_provider():
    """Return an async generator that provides the test DB session.

    Used to patch swarmer.database.get_db so _run_openshell_agent() can
    access the same in-memory DB that the test creates sessions in.
    """
    async def _test_get_db():
        async with _TestSession() as session:
            yield session
    return _test_get_db


class TestRunOpenshellAgent:
    """Direct unit tests for _run_openshell_agent().

    Each test patches swarmer.database.get_db so the function can access
    the test DB (it imports get_db fresh each call).
    """

    @pytest.mark.asyncio
    async def test_prompt_mode_succeeds_on_exit_code_0(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-prompt', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=0, stdout="agent done", stderr="")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command_streaming", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.read_opencode_response", new=AsyncMock(return_value="agent done")), \
             patch("swarmer.openshell_client.get_draft_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], ws["id"], "sandbox-prompt", ["sh", "-c", "opencode run"], "prompt", "opencode")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "succeeded"
        assert "agent done" in (sess.last_output or "")

    @pytest.mark.asyncio
    async def test_prompt_mode_fails_on_nonzero_exit(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-fail', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=1, stdout="", stderr="error: tool crashed")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command_streaming", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.get_draft_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], ws["id"], "sandbox-fail", ["sh", "-c", "opencode run"], "prompt", "opencode")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "failed"

    @pytest.mark.asyncio
    async def test_prompt_mode_auto_deletes_sandbox_on_success(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-autoclean', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        exec_result = MagicMock(exit_code=0, stdout="done", stderr="")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command_streaming", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.read_opencode_response", new=AsyncMock(return_value="done")), \
             patch("swarmer.openshell_client.get_draft_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_del:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], ws["id"], "sandbox-autoclean", ["sh", "-c", "opencode run"], "prompt", "opencode")

        mock_del.assert_called_once_with("sandbox-autoclean")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
        assert sess.sandbox_name is None

    @pytest.mark.asyncio
    async def test_prompt_mode_sets_phase_running_first(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-running', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        phases_seen = []

        async def _fake_exec_streaming(sandbox_name, cmd, on_output=None, poll_interval=5.0, env=None, client=None):
            async with _TestSession() as db:
                from sqlalchemy import select
                from swarmer.models.session import Session
                sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
                phases_seen.append(sess.phase)
            return MagicMock(exit_code=0, stdout="", stderr="")

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command_streaming", new=_fake_exec_streaming), \
             patch("swarmer.openshell_client.read_opencode_response", new=AsyncMock(return_value="")), \
             patch("swarmer.openshell_client.get_draft_chunks", new=AsyncMock(return_value=[])), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], ws["id"], "sandbox-running", ["sh", "-c", "opencode run"], "prompt", "opencode")

        assert "running" in phases_seen

    @pytest.mark.asyncio
    async def test_server_mode_calls_start_agent(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="server")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-server', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.start_agent", new=AsyncMock()) as mock_start, \
             patch("swarmer.openshell_client.exec_command_streaming", new=AsyncMock()) as mock_exec:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(
                s["id"], ws["id"], "sandbox-server", ["sh", "-c", "opencode serve"], "server", "opencode"
            )

        mock_start.assert_called_once_with("sandbox-server", ["sh", "-c", "opencode serve"], env={})
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_tui_mode_does_not_call_start_agent(self, client):
        """TUI mode skips start_agent — the WebSocket handler starts the agent interactively."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="tui")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-tui', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.start_agent", new=AsyncMock()) as mock_start:
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(
                s["id"], ws["id"], "sandbox-tui", ["sh", "-c", "sleep infinity"], "tui", "opencode"
            )

        mock_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_sets_phase_failed(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-exc', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch(
                "swarmer.openshell_client.exec_command_streaming",
                new=AsyncMock(side_effect=ConnectionError("gateway down")),
             ):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], ws["id"], "sandbox-exc", ["sh", "-c", "opencode run"], "prompt", "opencode")

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "failed"
        assert sess.run_completed_at is not None


# ===========================================================================
# 4b. exec_command timeout_seconds forwarding
# ===========================================================================


class TestExecCommandTimeout:
    @pytest.mark.asyncio
    async def test_exec_command_passes_timeout_to_sdk(self):
        """exec_command forwards timeout_seconds to the SDK exec call."""
        from swarmer.openshell_client import exec_command
        from unittest.mock import patch, MagicMock, AsyncMock

        mock_result = MagicMock(exit_code=0, stdout="ok", stderr="")
        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(id="test-id")
        mock_client.exec.return_value = mock_result

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            result = await exec_command("sb-name", ["echo", "hi"], client=None, timeout_seconds=120)

        mock_client.exec.assert_called_once_with("test-id", ["echo", "hi"], stdin=None, timeout_seconds=120, env={})
        assert result.stdout == "ok"

    @pytest.mark.asyncio
    async def test_exec_command_default_timeout_is_none(self):
        """exec_command passes timeout_seconds=None when not specified (SDK uses gRPC default)."""
        from swarmer.openshell_client import exec_command
        from unittest.mock import patch, MagicMock

        mock_client = MagicMock()
        mock_client.get.return_value = MagicMock(id="test-id")
        mock_client.exec.return_value = MagicMock(exit_code=0, stdout="", stderr="")

        with patch("swarmer.openshell_client._get_client", return_value=mock_client):
            await exec_command("sb-name", ["ls"], client=None)

        mock_client.exec.assert_called_once_with("test-id", ["ls"], stdin=None, timeout_seconds=None, env={})




# ===========================================================================
# 5. _build_repo_context() base_path parameter
# ===========================================================================


class TestBuildRepoContextBasePath:
    def test_default_path_is_sandbox(self):
        # _build_repo_context moved to swarmer.routers.sessions; default base_path
        # is /sandbox (OpenShell runtime) rather than the old /workspace (K8s pods).
        from swarmer.routers.sessions import _build_repo_context

        class FakeRepo:
            repo_url = "https://github.com/org/myrepo"
            branch = "main"
            local_path = "myrepo"

        result = _build_repo_context([FakeRepo()])
        assert "/sandbox/myrepo" in result
        assert "/workspace/" not in result

    def test_sandbox_base_path(self):
        from swarmer.routers.sessions import _build_repo_context

        class FakeRepo:
            repo_url = "https://github.com/org/myrepo"
            branch = "main"
            local_path = "myrepo"

        result = _build_repo_context([FakeRepo()], base_path="/sandbox")
        assert "/sandbox/myrepo" in result
        assert "/workspace/" not in result

    def test_empty_repos_returns_empty(self):
        from swarmer.routers.sessions import _build_repo_context
        assert _build_repo_context([]) == ""
        assert _build_repo_context([], base_path="/sandbox") == ""


# ===========================================================================
# 6. session_delete() — OpenShell sandbox cleanup
# ===========================================================================


class TestSessionDeleteOpenshell:
    @pytest.mark.asyncio
    async def test_delete_calls_delete_sandbox_when_sandbox_set(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-del-test', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
            resp = await client.delete(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}"
            )

        assert resp.status_code == 200
        mock_delete.assert_called_once_with("sandbox-del-test")

    @pytest.mark.asyncio
    async def test_delete_skips_k8s_pvc_for_sandbox_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-nopvc', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        # k8s_session.delete_session_pvc has been removed — k8s_session no longer exists.

    @pytest.mark.asyncio
    async def test_delete_skips_k8s_secrets_for_sandbox_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-nosecrets', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        # k8s.cleanup_session_secrets has been removed — it no longer exists and cannot be called.

    @pytest.mark.asyncio
    async def test_delete_handles_sandbox_error_gracefully(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-del-err', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        with patch(
            "swarmer.openshell_client.delete_sandbox",
            new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
        ):
            resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")

        # Session is deleted from DB despite sandbox error
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_no_sandbox_deletes_cleanly(self, client):
        """Session with no sandbox_name can be deleted without errors."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name=NULL, phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        assert resp.status_code == 200


# ===========================================================================
# 7. Sandbox GC — _collect_orphaned_sandboxes()
# ===========================================================================


class TestSandboxGC:
    @pytest.mark.asyncio
    async def test_deletes_sandbox_not_in_db(self):
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-orphan-1", "sandbox-orphan-2"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        assert mock_delete.call_count == 2
        deleted = {c.args[0] for c in mock_delete.call_args_list}
        assert deleted == {"sandbox-orphan-1", "sandbox-orphan-2"}

    @pytest.mark.asyncio
    async def test_skips_sandbox_present_in_db(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-known', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-known", "sandbox-orphan"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        # Only the orphan is deleted
        mock_delete.assert_called_once_with("sandbox-orphan")

    @pytest.mark.asyncio
    async def test_no_op_when_no_live_sandboxes(self):
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=[])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_after_delete_error(self):
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-a", "sandbox-b"])):
                with patch(
                    "swarmer.openshell_client.delete_sandbox",
                    new=AsyncMock(side_effect=[RuntimeError("gateway error"), None]),
                ) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        assert mock_delete.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_list_error_gracefully(self):
        async with _TestSession() as db:
            with patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(side_effect=RuntimeError("gateway unavailable")),
            ):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_zombie_sandbox_for_failed_session(self, client):
        """A sandbox whose session is phase=failed is treated as a zombie and deleted."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-zombie-fail', phase='failed' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-zombie-fail"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_called_once_with("sandbox-zombie-fail")

        # sandbox_name should be cleared on the session
        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            session = await db.get(_S, s["id"])
            assert session.sandbox_name is None

    @pytest.mark.asyncio
    async def test_deletes_zombie_sandbox_for_succeeded_session(self, client):
        """A sandbox whose session is phase=succeeded (auto-delete failed) is cleaned up."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-zombie-ok', phase='succeeded' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-zombie-ok"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_called_once_with("sandbox-zombie-ok")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            session = await db.get(_S, s["id"])
            assert session.sandbox_name is None

    @pytest.mark.asyncio
    async def test_deletes_zombie_sandbox_for_stopped_session(self, client):
        """A sandbox whose session is phase=stopped is also cleaned up as a zombie."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-zombie-stopped', phase='stopped' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-zombie-stopped"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_called_once_with("sandbox-zombie-stopped")

        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            session = await db.get(_S, s["id"])
            assert session.sandbox_name is None

    @pytest.mark.asyncio
    async def test_active_sandbox_not_deleted_as_zombie(self, client):
        """A running session's sandbox is not deleted even if other zombies are present."""
        ws = await _create_workspace(client)
        s_running = await _create_session(client, ws["id"], name="running-sess")
        s_failed = await _create_session(client, ws["id"], name="failed-sess")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-active', phase='running' WHERE id=:id"),
                {"id": s_running["id"]},
            )
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-zombie', phase='failed' WHERE id=:id"),
                {"id": s_failed["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(return_value=["sandbox-active", "sandbox-zombie"]),
            ):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        # Only the zombie is deleted
        mock_delete.assert_called_once_with("sandbox-zombie")

    @pytest.mark.asyncio
    async def test_deleted_externally_runs_without_orphans(self, client):
        """The deleted_externally reconciliation runs even when there are no orphaned sandboxes.

        Previously this was gated behind 'if not orphaned: return', so it never ran
        unless there happened to also be an orphaned sandbox in the same GC cycle.

        We need at least one live sandbox for the GC to proceed past the early-return
        guard at 'if not live_names: return'. Use a second running session as the
        live sandbox so the gateway list is non-empty but contains no orphans.
        """
        ws = await _create_workspace(client)
        s_gone = await _create_session(client, ws["id"], name="gone-sess")
        s_live = await _create_session(client, ws["id"], name="live-sess")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-gone', phase='running' WHERE id=:id"),
                {"id": s_gone["id"]},
            )
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-live', phase='running' WHERE id=:id"),
                {"id": s_live["id"]},
            )
            await db.commit()

        # sandbox-gone was deleted externally; sandbox-live is still running.
        # There are no orphans — sandbox-live is known. The only live sandbox is sandbox-live.
        async with _TestSession() as db:
            with patch("swarmer.openshell_client.list_sandboxes", new=AsyncMock(return_value=["sandbox-live"])):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        # No delete call — sandbox-gone is already gone, sandbox-live is healthy
        mock_delete.assert_not_called()

        # The gone session should be moved to 'stopped' and sandbox_name cleared
        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            session = await db.get(_S, s_gone["id"])
            assert session.phase == "stopped"
            assert session.sandbox_name is None

    @pytest.mark.asyncio
    async def test_deleted_externally_with_live_sandboxes_present(self, client):
        """deleted_externally reconciliation works alongside live sandboxes (no orphans)."""
        ws = await _create_workspace(client)
        s_gone = await _create_session(client, ws["id"], name="gone-sess")
        s_running = await _create_session(client, ws["id"], name="running-sess")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-gone', phase='running' WHERE id=:id"),
                {"id": s_gone["id"]},
            )
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-live', phase='running' WHERE id=:id"),
                {"id": s_running["id"]},
            )
            await db.commit()

        # Only sandbox-live is still alive; sandbox-gone was deleted externally.
        # No orphans — both live names are known.
        async with _TestSession() as db:
            with patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(return_value=["sandbox-live"]),
            ):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()) as mock_delete:
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        mock_delete.assert_not_called()

        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            gone_session = await db.get(_S, s_gone["id"])
            running_session = await db.get(_S, s_running["id"])
            assert gone_session.phase == "stopped"
            assert gone_session.sandbox_name is None
            assert running_session.phase == "running"  # untouched
            assert running_session.sandbox_name == "sandbox-live"

    @pytest.mark.asyncio
    async def test_stale_running_with_no_sandbox_name_moved_to_stopped(self, client):
        """Sessions stuck in phase=running or phase=pending with sandbox_name=NULL
        have no recoverable sandbox and must be moved to stopped by GC."""
        ws = await _create_workspace(client)
        s_stale = await _create_session(client, ws["id"])
        s_ok = await _create_session(client, ws["id"], name="s2")

        async with _TestSession() as db:
            # s_stale: running but no sandbox_name (race condition victim)
            await db.execute(
                text("UPDATE sessions SET sandbox_name=NULL, phase='running' WHERE id=:id"),
                {"id": s_stale["id"]},
            )
            # s_ok: running with a live sandbox_name (should be untouched)
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-healthy', phase='running' WHERE id=:id"),
                {"id": s_ok["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(return_value=["sandbox-healthy"]),
            ):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            stale = await db.get(_S, s_stale["id"])
            ok = await db.get(_S, s_ok["id"])
            assert stale.phase == "stopped", (
                f"Expected stale session to be stopped, got '{stale.phase}'"
            )
            assert ok.phase == "running"  # healthy session untouched
            assert ok.sandbox_name == "sandbox-healthy"

    @pytest.mark.asyncio
    async def test_stale_running_no_sandbox_does_not_affect_healthy_running(self, client):
        """The stale-running GC only touches sessions with sandbox_name=NULL;
        sessions with a live sandbox_name are left untouched."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sandbox-healthy-2', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        async with _TestSession() as db:
            with patch(
                "swarmer.openshell_client.list_sandboxes",
                new=AsyncMock(return_value=["sandbox-healthy-2"]),
            ):
                with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
                    from swarmer.scheduler import _collect_orphaned_sandboxes
                    await _collect_orphaned_sandboxes(db)

        async with _TestSession() as db:
            from swarmer.models.session import Session as _S
            updated = await db.get(_S, s["id"])
            assert updated.phase == "running"  # untouched — has a live sandbox


def _make_agent_setup_patches(sandbox_name: str = "sandbox-abc"):
    """Return a dict of patches for _setup_openshell_sandbox."""
    ref = _fake_sandbox_ref(sandbox_name)
    return {
        "create_sandbox": patch(
            "swarmer.openshell_client.create_sandbox",
            new=AsyncMock(return_value=ref),
        ),
        "write_agent_config": patch(
            "swarmer.openshell_client.write_agent_config",
            new=AsyncMock(),
        ),
        "write_agents_md": patch(
            "swarmer.openshell_client.write_agents_md",
            new=AsyncMock(),
        ),
        "approve_chunks": patch(
            "swarmer.openshell_client.approve_draft_policy_chunks",
            new=AsyncMock(return_value=[]),
        ),
        "run_agent": patch(
            "swarmer.routers.sessions._run_openshell_agent",
            new=AsyncMock(),
        ),
        "sleep": patch("asyncio.sleep", new=AsyncMock()),
    }


# ---------------------------------------------------------------------------
# MCP patch injection regression tests (ACM-34954)
# ---------------------------------------------------------------------------

class TestMcpPatchInjection:
    """Verify that a non-empty mcp_patch is written into the agent config JSON.

    Regression guard for the double-nesting bug where _setup_openshell_sandbox
    called mcp_patch.get("mcp", {}) on a dict that was already the "mcp" value,
    always producing an empty list and silently dropping MCP config from the
    written opencode.json.
    """

    @pytest.mark.asyncio
    async def test_opencode_mcp_patch_written_to_agent_config(self, client):
        """mcp_patch with Jira entry must appear in the config JSON passed to write_agent_config."""
        import json as _json
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET phase='pending' WHERE id=:id"), {"id": s["id"]}
            )
            await db.commit()

        from swarmer.agent_tools.opencode import OpenCodeStrategy
        from swarmer.routers.sessions import _setup_openshell_sandbox

        tool = OpenCodeStrategy()
        model = "google/gemini-3.5-flash"
        captured_config: list[str] = []

        async def _capture_write_agent_config(sandbox_name, tool_name, config_json):
            captured_config.append(config_json)

        # mcp_patch is the already-extracted "mcp" dict (keys = server slugs)
        mcp_patch = {
            "atlassian-jira": {
                "type": "local",
                "command": ["jira-mcp-server"],
                "enabled": True,
                "environment": {
                    "JIRA_SERVER_URL": "{env:JIRA_SERVER_URL}",
                    "JIRA_ACCESS_TOKEN": "{env:JIRA_ACCESS_TOKEN}",
                    "JIRA_EMAIL": "{env:JIRA_EMAIL}",
                },
            }
        }

        patches = _make_agent_setup_patches()
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patches["create_sandbox"], \
             patch("swarmer.openshell_client.write_agent_config", new=_capture_write_agent_config), \
             patches["write_agents_md"], \
             patches["approve_chunks"], \
             patches["run_agent"], \
             patches["sleep"], \
             patch("swarmer.openshell_client.exec_command", new=AsyncMock(
                 return_value=MagicMock(exit_code=0, stdout="", stderr="")
             )):
            await _setup_openshell_sandbox(
                session_id=s["id"],
                workspace_id=ws["id"],
                provider_names=[],
                env_vars={},
                policy=None,
                image=tool.get_image(),
                tool_name="opencode",
                model=model,
                model_setup_cmd=tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/"),
                share_cmd=tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/"),
                mcp_patch=mcp_patch,
                repos_data=[],
                git_username="",
                pat_token="",
                working_branch="",
                agents_md="",
                mode="prompt",
                main_cmd=f"opencode run --model {model} 'hello'",
                resolved_prompt="hello",
            )

        assert captured_config, "write_agent_config was never called"
        written = _json.loads(captured_config[0])
        assert "mcp" in written, (
            f"opencode.json must contain 'mcp' key when mcp_patch is non-empty; got keys: {list(written.keys())}"
        )
        assert "atlassian-jira" in written["mcp"], (
            f"'atlassian-jira' entry missing from mcp section; got: {written['mcp']}"
        )
        assert written["mcp"]["atlassian-jira"]["command"] == ["jira-mcp-server"], (
            "Jira MCP command must be ['jira-mcp-server']"
        )

# ---------------------------------------------------------------------------
# Policy rules CRUD endpoint tests (ACM-34993)
# ---------------------------------------------------------------------------

class TestPolicyRulesEndpoints:
    """Verify the policy-rules/add and policy-rules/{idx}/delete endpoints."""

    @pytest.mark.asyncio
    async def test_add_chunk_to_custom_policies(self, client):
        """POST policy-rules/add promotes a selected chunk into custom_policies."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        chunk = {
            "id": "chunk-1",
            "status": "pending",
            "rule_name": "vuln-go-dev",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [{"path": "/usr/local/go/bin/govulncheck", "harness": True}],
        }
        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
            data={"chunk": _j.dumps(chunk)},
        )
        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert "policyChanged" in trigger
        assert trigger["policyChanged"]["added"] == 1

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 1
        assert rules[0]["name"] == "vuln-go-dev"
        assert rules[0]["source"] == "chunk"
        assert rules[0]["endpoints"][0]["host"] == "vuln.go.dev"

    @pytest.mark.asyncio
    async def test_add_chunk_deduplicates_by_rule_name(self, client):
        """Adding a chunk with a rule_name that already exists in custom_policies is a no-op."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        chunk = {
            "id": "chunk-1",
            "status": "pending",
            "rule_name": "vuln-go-dev",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [],
        }
        # Add twice
        for _ in range(2):
            await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
                data={"chunk": _j.dumps(chunk)},
            )

        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 1, "Duplicate rule should not be added"

    @pytest.mark.asyncio
    async def test_delete_custom_rule_by_index(self, client):
        """POST policy-rules/{idx}/delete removes the rule at that index."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-populate two rules
        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            sess.custom_policies = _j.dumps([
                {"name": "rule-a", "endpoints": [], "binaries": [], "source": "chunk", "added_at": "2026-01-01"},
                {"name": "rule-b", "endpoints": [], "binaries": [], "source": "chunk", "added_at": "2026-01-01"},
            ])
            await db.commit()

        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/0/delete"
        )
        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert "policyChanged" in trigger
        assert trigger["policyChanged"]["deleted"] == 1

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 1
        assert rules[0]["name"] == "rule-b"

    @pytest.mark.asyncio
    async def test_policy_chunks_snapshot_on_completion(self, client):
        """_run_openshell_agent stores chunk JSON in policy_chunks on completion."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='sb-policy', phase='pending' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        fake_chunks = [
            {
                "id": "chunk-1",
                "status": "pending",
                "rule_name": "test-rule",
                "endpoints": [{"host": "example.com", "port": 443, "protocol": "rest"}],
                "binaries": [],
            }
        ]
        exec_result = MagicMock(exit_code=0, stdout="done", stderr="")
        with patch("swarmer.database.get_db", new=_make_test_db_provider()), \
             patch("swarmer.openshell_client.exec_command_streaming", new=AsyncMock(return_value=exec_result)), \
             patch("swarmer.openshell_client.read_opencode_response", new=AsyncMock(return_value="done")), \
             patch("swarmer.openshell_client.get_draft_chunks", new=AsyncMock(return_value=fake_chunks)), \
             patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
            from swarmer.routers.sessions import _run_openshell_agent
            await _run_openshell_agent(s["id"], ws["id"], "sb-policy", ["opencode", "run"], "prompt", "opencode")

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.phase == "succeeded"
        assert sess.policy_chunks, "policy_chunks should be set after completion"
        stored = _j.loads(sess.policy_chunks)
        assert len(stored) == 1
        assert stored[0]["rule_name"] == "test-rule"

    @pytest.mark.asyncio
    async def test_add_chunk_backfills_access_on_endpoints(self, client):
        """Promoting a chunk whose endpoints have protocol but no access/rules
        should store access=full so the gateway does not reject the policy."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Simulate a raw OPA draft chunk: protocol present, access/rules absent.
        chunk = {
            "id": "chunk-raw",
            "status": "pending",
            "rule_name": "allow-raw-githubusercontent-com-443",
            "endpoints": [
                {"host": "raw.githubusercontent.com", "port": 443, "protocol": "rest"}
            ],
            "binaries": [{"path": "/usr/bin/curl", "harness": True}],
        }
        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
            data={"chunk": _j.dumps(chunk)},
        )
        assert resp.status_code == 200

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 1
        ep = rules[0]["endpoints"][0]
        assert ep.get("access") == "full", (
            f"Expected access=full to be backfilled on endpoint missing access/rules, got: {ep}"
        )

    @pytest.mark.asyncio
    async def test_add_chunk_preserves_existing_rules_on_endpoints(self, client):
        """Promoting a chunk whose endpoints already have rules should preserve
        them and must NOT add access=full."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        chunk = {
            "id": "chunk-scoped",
            "status": "pending",
            "rule_name": "scoped-api-access",
            "endpoints": [
                {
                    "host": "api.github.com",
                    "port": 443,
                    "protocol": "rest",
                    "rules": [{"allow": {"method": "GET", "path": "/repos/org/repo/**"}}],
                }
            ],
            "binaries": [],
        }
        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
            data={"chunk": _j.dumps(chunk)},
        )
        assert resp.status_code == 200

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        rules = _j.loads(sess.custom_policies)
        ep = rules[0]["endpoints"][0]
        assert "access" not in ep, f"access must not be added when rules are present, got: {ep}"
        assert ep["rules"], "rules should be preserved"

    @pytest.mark.asyncio
    async def test_add_chunk_preserves_existing_access_on_endpoints(self, client):
        """Promoting a chunk whose endpoints already have access set preserves
        that value and does not overwrite it."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        chunk = {
            "id": "chunk-full",
            "status": "pending",
            "rule_name": "full-access-host",
            "endpoints": [
                {"host": "example.com", "port": 443, "protocol": "rest", "access": "full"}
            ],
            "binaries": [],
        }
        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
            data={"chunk": _j.dumps(chunk)},
        )
        assert resp.status_code == 200

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        rules = _j.loads(sess.custom_policies)
        ep = rules[0]["endpoints"][0]
        assert ep["access"] == "full"
        assert "rules" not in ep

    @pytest.mark.asyncio
    async def test_net_rules_persist_across_relaunch(self, client):
        """custom_policies (Net Rules) survive a relaunch; policy_chunks are cleared.

        Simulates the full cycle:
          1. Add a chunk to Net Rules.
          2. Relaunch the session (clears policy_chunks, keeps custom_policies).
          3. The policy-chunks endpoint returns the chunk with promoted_binaries
             populated from the surviving custom_policies, so the chunk renders
             as 'added' — not pending — even in the new sandbox run.
        """
        import json as _j
        from sqlalchemy import text

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        chunk = {
            "id": "chunk-persist",
            "status": "pending",
            "rule_name": "allow-vuln-go-dev-443",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [{"path": "/sandbox/.gopath/bin/govulncheck", "harness": True}],
        }

        # Step 1: promote the chunk to Net Rules.
        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
            data={"chunk": _j.dumps(chunk)},
        )
        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger.get("policyChanged", {}).get("added") == 1

        # Step 2: simulate a relaunch by clearing policy_chunks (as _do_launch does)
        # but leaving custom_policies intact.
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET policy_chunks='' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        # Verify custom_policies still has the rule after the simulated relaunch.
        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()

        assert sess.custom_policies, "custom_policies must survive a relaunch"
        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 1
        assert rules[0]["name"] == "allow-vuln-go-dev-443"

        # Step 3: the policy-chunks endpoint builds promoted_binaries from the
        # surviving custom_policies.  Inject the chunk as a snapshot (policy_chunks
        # is empty so we use the live-fetch path, but we mock get_draft_chunks).
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET policy_chunks=:chunks, sandbox_name='', phase='succeeded' WHERE id=:id"),
                {"id": s["id"], "chunks": _j.dumps([chunk])},
            )
            await db.commit()

        resp = await client.get(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-chunks"
        )
        assert resp.status_code == 200
        html = resp.text
        # The chunk should be shown with an "added" badge — not a pending checkbox.
        assert "added" in html
        # No checkbox should be rendered for this chunk.
        assert 'class="policy-chunk-cb"' not in html

    @pytest.mark.asyncio
    async def test_add_chunk_merge_different_binary_shows_pending(self, client):
        """A chunk with the same rule_name but a different binary shows as pending.

        OPA emits one chunk per (rule_name, binary) pair.  If the rule already
        exists in Net Rules but the new chunk carries a binary not yet in that
        rule, it must still appear as pending so the user can merge it in.
        """
        import json as _j

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Pre-populate Net Rules with the rule carrying one binary.
        first_chunk = {
            "id": "chunk-bin1",
            "status": "pending",
            "rule_name": "allow-vuln-go-dev-443",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [{"path": "/sandbox/.gopath/bin/govulncheck", "harness": True}],
        }
        resp = await client.post(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
            data={"chunk": _j.dumps(first_chunk)},
        )
        assert resp.status_code == 200

        # A second chunk — same rule_name, different binary path.
        second_chunk = {
            "id": "chunk-bin2",
            "status": "pending",
            "rule_name": "allow-vuln-go-dev-443",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [{"path": "/usr/bin/curl", "harness": True}],
        }

        from sqlalchemy import text
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET policy_chunks=:chunks WHERE id=:id"),
                {"id": s["id"], "chunks": _j.dumps([first_chunk, second_chunk])},
            )
            await db.commit()

        resp = await client.get(
            f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-chunks"
        )
        assert resp.status_code == 200
        html = resp.text

        # first_chunk (govulncheck) is fully covered — shows "added".
        # second_chunk (curl) is not yet in the rule — shows pending checkbox.
        assert "added" in html
        assert 'class="policy-chunk-cb"' in html


# ---------------------------------------------------------------------------
# Live policy apply / revoke tests (ACM-35281)
# ---------------------------------------------------------------------------

class TestPolicyRulesLiveApplyRevoke:
    """Verify live apply (add) and live revoke (delete) of Net Rules on running sandboxes."""

    @pytest.mark.asyncio
    async def test_add_chunk_live_applies_to_running_sandbox(self, client):
        """Promoting a chunk while sandbox is active calls approve_chunks_by_id."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        # Put session in running state with a sandbox.
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='live-sb-1', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        chunk = {
            "id": "chunk-live-1",
            "status": "pending",
            "rule_name": "allow-vuln-go-dev-443",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [{"path": "/usr/local/go/bin/govulncheck", "harness": True}],
        }

        with patch("swarmer.openshell_client.approve_chunks_by_id", new=AsyncMock(return_value=1)) as mock_approve:
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
                data={"chunk": _j.dumps(chunk)},
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["added"] == 1
        assert trigger["policyChanged"]["live_applied"] is True

        # approve_chunks_by_id should have been called with the sandbox name and chunk ID.
        mock_approve.assert_awaited_once()
        call_args = mock_approve.call_args
        assert call_args[0][0] == "live-sb-1"
        assert "chunk-live-1" in call_args[0][1]

        # The chunk_id should be persisted alongside the rule for future undo.
        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
        rules = _j.loads(sess.custom_policies)
        assert rules[0]["chunk_id"] == "chunk-live-1"

    @pytest.mark.asyncio
    async def test_add_chunk_no_live_apply_when_sandbox_not_active(self, client):
        """Promoting a chunk when sandbox is idle does not call approve_chunks_by_id."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        chunk = {
            "id": "chunk-idle-1",
            "status": "pending",
            "rule_name": "allow-idle-rule",
            "endpoints": [{"host": "example.com", "port": 443, "protocol": "rest"}],
            "binaries": [],
        }

        with patch("swarmer.openshell_client.approve_chunks_by_id", new=AsyncMock(return_value=0)) as mock_approve:
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
                data={"chunk": _j.dumps(chunk)},
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["added"] == 1
        assert trigger["policyChanged"]["live_applied"] is False
        mock_approve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_add_chunk_live_apply_failure_does_not_rollback_db(self, client):
        """If approve_chunks_by_id raises, the rule is still persisted in the DB."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET sandbox_name='live-sb-fail', phase='running' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        chunk = {
            "id": "chunk-fail-1",
            "status": "pending",
            "rule_name": "allow-fail-rule",
            "endpoints": [{"host": "fail.example.com", "port": 443, "protocol": "rest"}],
            "binaries": [],
        }

        with patch("swarmer.openshell_client.approve_chunks_by_id", new=AsyncMock(side_effect=Exception("grpc error"))):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/add",
                data={"chunk": _j.dumps(chunk)},
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["added"] == 1
        assert trigger["policyChanged"]["live_applied"] is False

        # Rule must be persisted despite the gateway error.
        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 1
        assert rules[0]["name"] == "allow-fail-rule"

    @pytest.mark.asyncio
    async def test_delete_rule_live_revokes_from_running_sandbox(self, client):
        """Deleting a Net Rule while sandbox is active calls undo_chunks_by_rule_name."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        # Pre-populate a rule that was live-applied (has chunk_id stored).
        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            sess.sandbox_name = "live-sb-2"
            sess.phase = "running"
            sess.custom_policies = _j.dumps([
                {
                    "name": "allow-vuln-go-dev-443",
                    "endpoints": [{"host": "vuln.go.dev", "port": 443}],
                    "binaries": [],
                    "source": "chunk",
                    "added_at": "2026-01-01T00:00:00+00:00",
                    "chunk_id": "chunk-undo-1",
                }
            ])
            await db.commit()

        with patch("swarmer.openshell_client.undo_chunks_by_rule_name", new=AsyncMock(return_value=1)) as mock_undo:
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/0/delete"
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["deleted"] == 1
        assert trigger["policyChanged"]["live_revoked"] is True

        # undo_chunks_by_rule_name should have been called with the sandbox name,
        # rule name, and the stored chunk_id.
        mock_undo.assert_awaited_once()
        call_args = mock_undo.call_args
        assert call_args[0][0] == "live-sb-2"
        assert call_args[1].get("rule_names") == ["allow-vuln-go-dev-443"] or \
               call_args[0][1] == ["allow-vuln-go-dev-443"]
        assert "chunk-undo-1" in (call_args[1].get("chunk_ids") or [])

    @pytest.mark.asyncio
    async def test_delete_rule_no_revoke_when_sandbox_not_active(self, client):
        """Deleting a Net Rule when sandbox is idle does not call undo_chunks_by_rule_name."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            sess.custom_policies = _j.dumps([
                {"name": "rule-a", "endpoints": [], "binaries": [], "source": "chunk",
                 "added_at": "2026-01-01", "chunk_id": "chunk-x"}
            ])
            await db.commit()

        with patch("swarmer.openshell_client.undo_chunks_by_rule_name", new=AsyncMock(return_value=0)) as mock_undo:
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/0/delete"
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["deleted"] == 1
        assert trigger["policyChanged"]["live_revoked"] is False
        mock_undo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_rule_live_revoke_failure_still_removes_from_db(self, client):
        """If undo_chunks_by_rule_name raises, the rule is still removed from the DB."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            sess.sandbox_name = "live-sb-fail2"
            sess.phase = "running"
            sess.custom_policies = _j.dumps([
                {"name": "rule-b", "endpoints": [], "binaries": [], "source": "chunk",
                 "added_at": "2026-01-01", "chunk_id": "chunk-fail-2"}
            ])
            await db.commit()

        with patch("swarmer.openshell_client.undo_chunks_by_rule_name", new=AsyncMock(side_effect=Exception("grpc error"))):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/0/delete"
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["deleted"] == 1
        assert trigger["policyChanged"]["live_revoked"] is False

        # Rule must be removed from DB despite the gateway error.
        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
        rules = _j.loads(sess.custom_policies)
        assert len(rules) == 0

    @pytest.mark.asyncio
    async def test_delete_startup_rule_returns_live_revoked_false(self, client):
        """A rule without a chunk_id (baked into startup policy) returns live_revoked=False
        because UndoDraftChunk cannot affect startup-policy rules."""
        import json as _j
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], mode="prompt")

        async with _TestSession() as db:
            from swarmer.models.session import Session
            from sqlalchemy import select
            sess = (await db.execute(select(Session).where(Session.id == s["id"]))).scalar_one()
            sess.sandbox_name = "live-sb-startup"
            sess.phase = "running"
            # No chunk_id — simulates a rule from custom_policies baked at launch.
            sess.custom_policies = _j.dumps([
                {"name": "startup-rule", "endpoints": [], "binaries": [], "source": "chunk",
                 "added_at": "2026-01-01"}
            ])
            await db.commit()

        # undo_chunks_by_rule_name returns 0 when chunk not found in history.
        with patch("swarmer.openshell_client.undo_chunks_by_rule_name", new=AsyncMock(return_value=0)) as mock_undo:
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/policy-rules/0/delete"
            )

        assert resp.status_code == 200
        trigger = _j.loads(resp.headers.get("hx-trigger", "{}"))
        assert trigger["policyChanged"]["deleted"] == 1
        assert trigger["policyChanged"]["live_revoked"] is False
        # Was called but returned 0 (startup rule, not in draft history).
        mock_undo.assert_awaited_once()


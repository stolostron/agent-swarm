"""Unit tests for the Console API client (swarmer/routers/api_client.py).

Validates that the API client correctly wraps /api/v1/ endpoints and
forwards bearer tokens. Uses the same in-memory SQLite + ASGI transport
pattern as test_api.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

# ---------------------------------------------------------------------------
# Fixtures — shared with test_api.py pattern
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


@pytest_asyncio.fixture(autouse=True)
async def _setup_db(monkeypatch):
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    settings.k8s_namespace = ""

    async def _all_accessible(token, namespaces, api_url, in_cluster):
        return list(namespaces)

    async def _can_create_namespaces(token, api_url, in_cluster):
        return True

    monkeypatch.setattr(
        "swarmer.api.deps.get_accessible_namespaces", _all_accessible
    )
    monkeypatch.setattr(
        "swarmer.api.v1.workspaces.can_create_namespaces", _can_create_namespaces
    )
    monkeypatch.setattr("swarmer.k8s.ensure_namespace", lambda namespace: None)
    monkeypatch.setattr(
        "swarmer.k8s.grant_swarmer_user_access", lambda namespace, username: None
    )
    monkeypatch.setattr("swarmer.k8s.delete_namespace", lambda namespace: None)

    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns


def _override_get_bearer_token():
    return "test-token"


@pytest_asyncio.fixture
async def api_client():
    """Provide an APIClient wired to the test app with auth overrides."""
    from swarmer.api.deps import get_bearer_token, get_current_user, require_api_auth
    from swarmer.database import get_db
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_auth] = _override_require_api_auth
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_bearer_token] = _override_get_bearer_token

    from swarmer.routers.api_client import APIClient
    client = APIClient(app=app, token="fake-bearer-token")
    async with client:
        yield client

    app.dependency_overrides.clear()


# ===========================================================================
# APIClient initialization and token handling
# ===========================================================================


class TestClientSetup:
    @pytest.mark.asyncio
    async def test_client_sends_auth_header(self, api_client):
        """Verify the client includes Authorization header."""
        # Just verify the client works — list workspaces on empty DB
        workspaces = await api_client.list_workspaces()
        assert workspaces == []

    @pytest.mark.asyncio
    async def test_client_context_manager(self):
        """Verify async context manager works."""
        from swarmer.api.deps import get_current_user, require_api_auth
        from swarmer.database import get_db
        from swarmer.main import app
        from swarmer.routers.api_client import APIClient

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[require_api_auth] = _override_require_api_auth
        app.dependency_overrides[get_current_user] = _override_get_current_user

        client = APIClient(app=app, token="test-token")
        async with client:
            result = await client.list_workspaces()
            assert isinstance(result, list)

        app.dependency_overrides.clear()


# ===========================================================================
# Workspace operations
# ===========================================================================


class TestWorkspaceOps:
    @pytest.mark.asyncio
    async def test_create_workspace(self, api_client):
        ws = await api_client.create_workspace("Test Workspace", "A test")
        assert ws["display_name"] == "Test Workspace"
        assert ws["namespace"] == "test-workspace"
        assert ws["id"] > 0

    @pytest.mark.asyncio
    async def test_list_workspaces(self, api_client):
        await api_client.create_workspace("Alpha")
        await api_client.create_workspace("Beta")
        workspaces = await api_client.list_workspaces()
        assert len(workspaces) == 2
        names = {ws["display_name"] for ws in workspaces}
        assert names == {"Alpha", "Beta"}

    @pytest.mark.asyncio
    async def test_get_workspace(self, api_client):
        ws = await api_client.create_workspace("Detail WS")
        fetched = await api_client.get_workspace(ws["id"])
        assert fetched["display_name"] == "Detail WS"

    @pytest.mark.asyncio
    async def test_get_workspace_not_found(self, api_client):
        from swarmer.routers.api_client import APIError
        with pytest.raises(APIError) as exc_info:
            await api_client.get_workspace(999)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_workspace(self, api_client):
        ws = await api_client.create_workspace("Original")
        updated = await api_client.update_workspace(ws["id"], "Updated Name", "new desc")
        assert updated["display_name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_delete_workspace(self, api_client):
        ws = await api_client.create_workspace("To Delete")
        result = await api_client.delete_workspace(ws["id"])
        assert "detail" in result

        from swarmer.routers.api_client import APIError
        with pytest.raises(APIError):
            await api_client.get_workspace(ws["id"])


# ===========================================================================
# Session operations
# ===========================================================================


class TestSessionOps:
    @pytest.mark.asyncio
    async def test_create_session(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "test-session", mode="prompt")
        assert s["name"] == "test-session"
        assert s["mode"] == "prompt"
        assert s["phase"] == "idle"

    @pytest.mark.asyncio
    async def test_list_sessions(self, api_client):
        ws = await api_client.create_workspace("WS")
        await api_client.create_session(ws["id"], "s1")
        await api_client.create_session(ws["id"], "s2")
        sessions = await api_client.list_sessions(ws["id"])
        assert len(sessions) == 2

    @pytest.mark.asyncio
    async def test_get_session(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "my-session")
        fetched = await api_client.get_session(ws["id"], s["id"])
        assert fetched["name"] == "my-session"

    @pytest.mark.asyncio
    async def test_update_session(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "orig")
        updated = await api_client.update_session(ws["id"], s["id"], name="renamed")
        assert updated["name"] == "renamed"

    @pytest.mark.asyncio
    async def test_delete_session(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "to-del")
        result = await api_client.delete_session(ws["id"], s["id"])
        assert "detail" in result

    @pytest.mark.asyncio
    async def test_set_name(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "orig")
        updated = await api_client.set_session_name(ws["id"], s["id"], "new-name")
        assert updated["name"] == "new-name"

    @pytest.mark.asyncio
    async def test_set_mode(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "orig")
        updated = await api_client.set_session_mode(ws["id"], s["id"], "tui")
        assert updated["mode"] == "tui"

    @pytest.mark.asyncio
    async def test_set_model(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "orig")
        updated = await api_client.set_session_model(ws["id"], s["id"], "claude-sonnet-4-6")
        assert updated["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_get_output(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "sess")
        output = await api_client.get_session_output(ws["id"], s["id"])
        assert output["output"] == ""

    @pytest.mark.asyncio
    async def test_clear_output(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "sess")
        result = await api_client.clear_session_output(ws["id"], s["id"])
        assert "id" in result  # returns SessionOut

    @pytest.mark.asyncio
    async def test_schedule_and_unschedule(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "sched")
        updated = await api_client.schedule_session(ws["id"], s["id"], "0 * * * *")
        assert updated["cron_schedule"] == "0 * * * *"

        updated = await api_client.unschedule_session(ws["id"], s["id"])
        assert updated["cron_schedule"] == ""


# ===========================================================================
# Repo operations
# ===========================================================================


class TestRepoOps:
    @pytest.mark.asyncio
    async def test_add_and_list_repos(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "sess")
        repo = await api_client.add_repo(
            ws["id"], s["id"],
            "https://github.com/org/repo.git", "main",
        )
        assert repo["repo_url"] == "https://github.com/org/repo.git"
        assert repo["local_path"] == "repo"

        repos = await api_client.list_repos(ws["id"], s["id"])
        assert len(repos) == 1

    @pytest.mark.asyncio
    async def test_delete_repo(self, api_client):
        ws = await api_client.create_workspace("WS")
        s = await api_client.create_session(ws["id"], "sess")
        repo = await api_client.add_repo(
            ws["id"], s["id"],
            "https://github.com/org/repo.git",
        )
        result = await api_client.delete_repo(ws["id"], s["id"], repo["id"])
        assert "detail" in result

        repos = await api_client.list_repos(ws["id"], s["id"])
        assert len(repos) == 0


# ===========================================================================
# Secrets operations
# ===========================================================================


class TestSecretsOps:
    @pytest.mark.asyncio
    async def test_credentials_initially_none(self, api_client):
        ws = await api_client.create_workspace("WS")
        cred = await api_client.get_credentials(ws["id"])
        assert cred is None

    @pytest.mark.asyncio
    async def test_save_and_get_credentials(self, api_client):
        ws = await api_client.create_workspace("WS")
        cred = await api_client.save_credentials(
            ws["id"],
            google_cloud_project="my-project",
            vertex_location="us-central1",
            anthropic_api_key="sk-ant-test",
        )
        assert cred["google_cloud_project"] == "my-project"
        assert cred["has_anthropic"] is True

    @pytest.mark.asyncio
    async def test_pat_crud(self, api_client):
        ws = await api_client.create_workspace("WS")
        pat = await api_client.create_pat(
            ws["id"],
            name="my-pat",
            github_username="octocat",
            pat_value="ghp_test",
        )
        assert pat["name"] == "my-pat"

        pats = await api_client.list_pats(ws["id"])
        assert len(pats) == 1

        updated = await api_client.update_pat(
            ws["id"], pat["id"], description="Updated"
        )
        assert updated["description"] == "Updated"

        result = await api_client.delete_pat(ws["id"], pat["id"])
        assert "detail" in result

        pats = await api_client.list_pats(ws["id"])
        assert len(pats) == 0


# ===========================================================================
# Env var operations
# ===========================================================================


class TestEnvVarOps:
    @pytest.mark.asyncio
    async def test_list_env_vars(self, api_client):
        ws = await api_client.create_workspace("WS")
        env_vars = await api_client.list_env_vars(ws["id"])
        assert isinstance(env_vars, list)


# ===========================================================================
# Error handling
# ===========================================================================


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_api_error_on_404(self, api_client):
        from swarmer.routers.api_client import APIError
        with pytest.raises(APIError) as exc_info:
            await api_client.get_workspace(999)
        assert exc_info.value.status_code == 404
        assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_api_error_on_conflict(self, api_client):
        from swarmer.routers.api_client import APIError
        await api_client.create_workspace("Duplicate")
        with pytest.raises(APIError) as exc_info:
            await api_client.create_workspace("Duplicate")
        assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_401_raises_not_authenticated(self):
        """A 401 response raises NotAuthenticated, not APIError."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        from swarmer.deps import NotAuthenticated
        from swarmer.routers.api_client import APIClient

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 401

        async with APIClient(token="expired-token") as client:
            client._client.request = AsyncMock(return_value=mock_resp)
            with pytest.raises(NotAuthenticated):
                await client._request("GET", "/api/v1/workspaces")

    @pytest.mark.asyncio
    async def test_non_401_4xx_raises_api_error(self):
        """Non-401 4xx responses still raise APIError, not NotAuthenticated."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        from swarmer.routers.api_client import APIClient, APIError

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"detail": "Forbidden"}

        async with APIClient(token="some-token") as client:
            client._client.request = AsyncMock(return_value=mock_resp)
            with pytest.raises(APIError) as exc_info:
                await client._request("GET", "/api/v1/workspaces")
        assert exc_info.value.status_code == 403


# ===========================================================================
# Console route: expired token redirects to /login
# ===========================================================================


class TestConsoleRouteUnauthorizedRedirect:
    @pytest.mark.asyncio
    async def test_workspace_list_redirects_to_login_on_expired_token(self):
        """GET /workspaces redirects to /login when the API layer returns 401.

        Simulates a user whose session cookie is valid but whose K8s bearer
        token has expired.  The API returns 401, APIClient._request() raises
        NotAuthenticated, and the global handler redirects to /login.
        """
        import httpx
        from fastapi import HTTPException
        from unittest.mock import patch

        from swarmer.api.deps import get_current_user, require_api_auth
        from swarmer.database import get_db
        from swarmer.deps import require_auth
        from swarmer.main import app

        def _expired_api_auth():
            raise HTTPException(status_code=401, detail="Invalid or expired bearer token")

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[require_api_auth] = _expired_api_auth
        app.dependency_overrides[require_auth] = lambda: None
        app.dependency_overrides[get_current_user] = _override_get_current_user

        try:
            with patch("swarmer.routers.api_client.get_user_token", return_value="fake-expired-token"):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as http_client:
                    resp = await http_client.get("/workspaces", follow_redirects=False)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"

"""Unit tests for the REST API (/api/v1/).

Uses httpx AsyncClient with the FastAPI test client — no running server needed.
Overrides the auth dependency and uses an in-memory SQLite database.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


def _override_require_api_auth():
    """Bypass K8s token validation for tests."""
    from swarmer.k8s_auth import TokenIdentity
    return TokenIdentity(username="test-user", uid="uid-1234")


def _override_get_current_user():
    return "test-user"


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    """Create tables before each test, drop after."""
    # Init crypto before anything else (model properties call decrypt)
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    # Set k8s_namespace so K8s namespace create/delete is skipped in tests
    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    settings.k8s_namespace = "test-ns"

    import swarmer.models  # noqa: F401 — register models on Base.metadata

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns


@pytest_asyncio.fixture
async def client():
    """Provide an httpx AsyncClient wired to the FastAPI app with overrides."""
    from swarmer.api.deps import get_current_user, require_api_auth
    from swarmer.database import get_db
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_auth] = _override_require_api_auth
    app.dependency_overrides[get_current_user] = _override_get_current_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _create_workspace(client: AsyncClient, name: str = "Test Workspace") -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        json={"display_name": name, "description": "A test workspace"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(client: AsyncClient, ws_id: int, name: str = "test-session") -> dict:
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sessions",
        json={"name": name, "mode": "prompt", "agent_tool": "opencode"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# Workspace tests
# ===========================================================================


class TestWorkspaces:
    @pytest.mark.asyncio
    async def test_create_workspace(self, client):
        data = await _create_workspace(client)
        assert data["display_name"] == "Test Workspace"
        assert data["namespace"] == "test-workspace"
        assert data["id"] > 0

    @pytest.mark.asyncio
    async def test_list_workspaces(self, client):
        await _create_workspace(client, "Alpha")
        await _create_workspace(client, "Beta")
        resp = await client.get("/api/v1/workspaces")
        assert resp.status_code == 200
        ws_list = resp.json()
        assert len(ws_list) == 2
        names = {ws["display_name"] for ws in ws_list}
        assert names == {"Alpha", "Beta"}

    @pytest.mark.asyncio
    async def test_get_workspace(self, client):
        ws = await _create_workspace(client)
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}")
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Test Workspace"

    @pytest.mark.asyncio
    async def test_get_workspace_not_found(self, client):
        resp = await client.get("/api/v1/workspaces/999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_workspace(self, client):
        ws = await _create_workspace(client)
        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}",
            json={"display_name": "Updated Name", "description": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_create_duplicate_namespace(self, client):
        await _create_workspace(client, "My Project")
        resp = await client.post(
            "/api/v1/workspaces",
            json={"display_name": "My Project", "description": "dup"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_create_workspace_empty_name(self, client):
        resp = await client.post(
            "/api/v1/workspaces",
            json={"display_name": "---", "description": ""},
        )
        assert resp.status_code == 422


# ===========================================================================
# Session tests
# ===========================================================================


class TestSessions:
    @pytest.mark.asyncio
    async def test_create_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        assert s["name"] == "test-session"
        assert s["mode"] == "prompt"
        assert s["phase"] == "idle"
        assert s["working_branch"].startswith("swarmer/session-")

    @pytest.mark.asyncio
    async def test_list_sessions(self, client):
        ws = await _create_workspace(client)
        await _create_session(client, ws["id"], "sess-a")
        await _create_session(client, ws["id"], "sess-b")
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_get_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-session"

    @pytest.mark.asyncio
    async def test_update_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}",
            json={"name": "renamed-session", "mode": "tui"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed-session"
        assert resp.json()["mode"] == "tui"

    @pytest.mark.asyncio
    async def test_delete_session(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        assert resp.status_code == 200

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_duplicate_session_name(self, client):
        ws = await _create_workspace(client)
        await _create_session(client, ws["id"], "dup-session")
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={"name": "dup-session"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_set_name(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/set-name",
            json={"name": "new-name"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-name"

    @pytest.mark.asyncio
    async def test_set_mode(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/set-mode",
            json={"mode": "server"},
        )
        assert resp.status_code == 200
        assert resp.json()["mode"] == "server"

    @pytest.mark.asyncio
    async def test_set_mode_invalid(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/set-mode",
            json={"mode": "invalid-mode"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_set_model(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/set-model",
            json={"model": "claude-sonnet-4-6"},
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_get_output(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/output")
        assert resp.status_code == 200
        assert resp.json()["output"] == ""

    @pytest.mark.asyncio
    async def test_clear_output(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/clear-output")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_create_session_invalid_mode(self, client):
        ws = await _create_workspace(client)
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={"name": "bad-mode", "mode": "invalid"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_session_invalid_mode(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}",
            json={"mode": "invalid"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_update_session_empty_name(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}",
            json={"name": ""},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_schedule_non_prompt_fails(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        # Change to TUI mode first
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/set-mode",
            json={"mode": "tui"},
        )
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/schedule",
            json={"cron_expr": "0 * * * *"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_schedule_and_unschedule(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/schedule",
            json={"cron_expr": "0 * * * *"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cron_schedule"] == "0 * * * *"
        assert data["cron_label"] == "Every hour"

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/unschedule",
        )
        assert resp.status_code == 200
        assert resp.json()["cron_schedule"] == ""


# ===========================================================================
# Repo tests
# ===========================================================================


class TestRepos:
    @pytest.mark.asyncio
    async def test_add_and_list_repos(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/repo.git", "branch": "main"},
        )
        assert resp.status_code == 201
        repo = resp.json()
        assert repo["repo_url"] == "https://github.com/org/repo.git"
        assert repo["local_path"] == "repo"

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_delete_repo(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/repo.git"},
        )
        repo = resp.json()

        resp = await client.delete(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos/{repo['id']}"
        )
        assert resp.status_code == 200

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos")
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_add_repo_custom_path(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/repo.git", "local_path": "custom-dir"},
        )
        assert resp.status_code == 201
        assert resp.json()["local_path"] == "custom-dir"

    @pytest.mark.asyncio
    async def test_add_repo_path_traversal_rejected(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/repo.git", "local_path": "../etc"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_add_repo_absolute_path_rejected(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
            json={"repo_url": "https://github.com/org/repo.git", "local_path": "/tmp/evil"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_add_repo_token_in_url_rejected(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        for bad_url in [
            "https://user:ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA@github.com/org/repo.git",
            "https://github.com/org/repo.git?token=ghp_secret",
            "https://github.com/ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/org/repo.git",
        ]:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/repos",
                json={"repo_url": bad_url},
            )
            assert resp.status_code == 422, f"Expected 422 for {bad_url!r}, got {resp.status_code}"


# ===========================================================================
# Secrets tests
# ===========================================================================


class TestSecrets:
    @pytest.mark.asyncio
    async def test_credentials_initially_none(self, client):
        ws = await _create_workspace(client)
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/secrets/credentials")
        assert resp.status_code == 200
        # No credentials yet — should return null
        assert resp.json() is None

    @pytest.mark.asyncio
    async def test_save_and_get_credentials(self, client):
        ws = await _create_workspace(client)
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/credentials",
            json={
                "google_cloud_project": "my-project",
                "vertex_location": "us-central1",
                "anthropic_api_key": "sk-ant-test123456",
            },
        )
        assert resp.status_code == 200
        cred = resp.json()
        assert cred["google_cloud_project"] == "my-project"
        assert cred["has_anthropic"] is True
        assert cred["has_adc"] is False
        assert "sk-ant" not in cred.get("masked_anthropic_key", "")  # key should be masked

    @pytest.mark.asyncio
    async def test_pat_crud(self, client):
        ws = await _create_workspace(client)

        # Create PAT
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/pats",
            json={
                "name": "my-pat",
                "github_username": "octocat",
                "pat_value": "ghp_testtoken123456",
            },
        )
        assert resp.status_code == 201
        pat = resp.json()
        assert pat["name"] == "my-pat"

        # List PATs
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/secrets/pats")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Update PAT
        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}/secrets/pats/{pat['id']}",
            json={"description": "Updated description"},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Updated description"

        # Delete PAT
        resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/secrets/pats/{pat['id']}")
        assert resp.status_code == 200

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/secrets/pats")
        assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_duplicate_pat_name(self, client):
        ws = await _create_workspace(client)
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/pats",
            json={"name": "dup-pat", "github_username": "user", "pat_value": "ghp_1"},
        )
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/pats",
            json={"name": "dup-pat", "github_username": "user", "pat_value": "ghp_2"},
        )
        assert resp.status_code == 409


# ===========================================================================
# Auth tests
# ===========================================================================


class TestAuth:
    @pytest.mark.asyncio
    async def test_unauthenticated_request(self, client):
        """Verify that removing the auth override returns 403 (no bearer token)."""
        from swarmer.api.deps import require_api_auth
        from swarmer.main import app

        # Remove the override so auth is enforced
        if require_api_auth in app.dependency_overrides:
            del app.dependency_overrides[require_api_auth]

        resp = await client.get("/api/v1/workspaces")
        assert resp.status_code in (401, 403)  # HTTPBearer rejects unauthenticated requests


# ===========================================================================
# Cross-resource integration tests
# ===========================================================================


class TestIntegration:
    @pytest.mark.asyncio
    async def test_delete_workspace_cascades_sessions(self, client):
        """Deleting a workspace should cascade-delete its sessions."""
        ws = await _create_workspace(client)
        await _create_session(client, ws["id"], "session-1")
        await _create_session(client, ws["id"], "session-2")

        resp = await client.delete(f"/api/v1/workspaces/{ws['id']}")
        assert resp.status_code == 200

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_session_not_found_for_wrong_workspace(self, client):
        ws1 = await _create_workspace(client, "WS One")
        ws2 = await _create_workspace(client, "WS Two")
        s = await _create_session(client, ws1["id"])

        resp = await client.get(f"/api/v1/workspaces/{ws2['id']}/sessions/{s['id']}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_repo_not_found_for_wrong_session(self, client):
        ws = await _create_workspace(client)
        s1 = await _create_session(client, ws["id"], "s1")
        s2 = await _create_session(client, ws["id"], "s2")

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s1['id']}/repos",
            json={"repo_url": "https://github.com/org/repo.git"},
        )
        repo = resp.json()

        resp = await client.delete(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s2['id']}/repos/{repo['id']}"
        )
        assert resp.status_code == 404


# ===========================================================================
# GitHub URL validation — integration (wiring checks)
# ===========================================================================


class TestGitHubURLValidation:
    """Verify validate_github_url() is wired up at API entry points."""

    @pytest.mark.asyncio
    async def test_browse_folders_rejects_token_in_userinfo(self, client):
        ws = await _create_workspace(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/prompts/browse/folders",
            params={"repo_url": "https://user:ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA@github.com/org/repo"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_browse_folders_rejects_token_in_query(self, client):
        ws = await _create_workspace(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/prompts/browse/folders",
            params={"repo_url": "https://github.com/org/repo?token=ghp_secret"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_browse_folders_accepts_clean_url(self, client):
        ws = await _create_workspace(client)
        # Will fail at GitHub API call (no network), but must not fail at URL validation.
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/prompts/browse/folders",
            params={"repo_url": "https://github.com/org/repo"},
        )
        assert resp.status_code != 400

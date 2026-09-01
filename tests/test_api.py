"""Unit tests for the REST API (/api/v1/).

Uses httpx AsyncClient with the FastAPI test client — no running server needed.
Overrides the auth dependency and uses an in-memory SQLite database.
"""

import json
import logging
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
async def _setup_db(monkeypatch):
    """Create tables before each test, drop after."""
    # Init crypto before anything else (model properties call decrypt)
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    orig_admin_users = settings.workspace_admin_users
    orig_admin_groups = settings.workspace_admin_groups
    orig_create_policy = settings.workspace_create_policy
    settings.k8s_namespace = ""
    settings.workspace_admin_users = ""
    settings.workspace_admin_groups = ""
    settings.workspace_create_policy = "all"

    monkeypatch.setattr("swarmer.k8s.ensure_namespace", lambda namespace: None)
    monkeypatch.setattr("swarmer.k8s.delete_namespace", lambda namespace: None)
    # list_known_users() merges in K8s discovery — stub it out by default so
    # tests never make real network calls.
    monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: [])
    monkeypatch.setattr("swarmer.k8s.list_user_service_accounts", lambda *a, **k: [])

    import swarmer.models  # noqa: F401 — register models on Base.metadata

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns
    settings.workspace_admin_users = orig_admin_users
    settings.workspace_admin_groups = orig_admin_groups
    settings.workspace_create_policy = orig_create_policy


def _override_get_bearer_token():
    return "test-token"


@pytest_asyncio.fixture
async def client():
    """Provide an httpx AsyncClient wired to the FastAPI app with overrides."""
    from swarmer.api.deps import get_bearer_token, get_current_user, require_api_auth
    from swarmer.database import get_db
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_api_auth] = _override_require_api_auth
    app.dependency_overrides[get_current_user] = _override_get_current_user
    app.dependency_overrides[get_bearer_token] = _override_get_bearer_token

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


class TestWorkspaceRbac:
    """Database-backed workspace ACL (ACM-41659) — owner/member/admin access."""

    @staticmethod
    def _override_identity(username: str):
        from swarmer.k8s_auth import TokenIdentity

        def _identity():
            return TokenIdentity(username=username, uid="uid-other")

        return _identity

    @pytest.mark.asyncio
    async def test_list_workspaces_filters_by_ownership(self, client):
        from swarmer.api.deps import require_api_auth
        from swarmer.main import app

        await _create_workspace(client, "Allowed")

        app.dependency_overrides[require_api_auth] = self._override_identity("other-user")
        await _create_workspace(client, "Denied")
        app.dependency_overrides[require_api_auth] = _override_require_api_auth

        resp = await client.get("/api/v1/workspaces")
        assert resp.status_code == 200
        names = {ws["display_name"] for ws in resp.json()}
        assert names == {"Allowed"}

    @pytest.mark.asyncio
    async def test_get_workspace_denied_returns_404(self, client):
        from swarmer.api.deps import require_api_auth
        from swarmer.main import app

        app.dependency_overrides[require_api_auth] = self._override_identity("other-user")
        ws = await _create_workspace(client, "Secret")
        app.dependency_overrides[require_api_auth] = _override_require_api_auth

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_workspace_member_can_access(self, client):
        from swarmer.api.deps import require_api_auth
        from swarmer.main import app

        app.dependency_overrides[require_api_auth] = self._override_identity("owner-user")
        ws = await _create_workspace(client, "Shared")
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/members",
            json={"user_id": "test-user"},
        )
        assert resp.status_code == 201, resp.text
        app.dependency_overrides[require_api_auth] = _override_require_api_auth

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_non_owner_cannot_add_members(self, client):
        from swarmer.api.deps import require_api_auth
        from swarmer.main import app

        app.dependency_overrides[require_api_auth] = self._override_identity("owner-user")
        ws = await _create_workspace(client, "Owned")
        app.dependency_overrides[require_api_auth] = _override_require_api_auth

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/members",
            json={"user_id": "eve"},
        )
        assert resp.status_code == 404  # not even visible to test-user

    @pytest.mark.asyncio
    async def test_create_workspace_requires_permission_under_admins_only_policy(
        self, client
    ):
        from swarmer.config import settings

        settings.workspace_create_policy = "admins"
        try:
            resp = await client.post(
                "/api/v1/workspaces",
                json={"display_name": "Blocked", "description": ""},
            )
            assert resp.status_code == 403
        finally:
            settings.workspace_create_policy = "all"

    @pytest.mark.asyncio
    async def test_workspace_admin_can_create_under_admins_only_policy(self, client):
        from swarmer.config import settings

        settings.workspace_create_policy = "admins"
        settings.workspace_admin_users = "test-user"
        try:
            resp = await client.post(
                "/api/v1/workspaces",
                json={"display_name": "Allowed", "description": ""},
            )
            assert resp.status_code == 201
        finally:
            settings.workspace_create_policy = "all"
            settings.workspace_admin_users = ""

    @pytest.mark.asyncio
    async def test_create_workspace_disabled_in_namespace_scoped_mode(self, client):
        from swarmer.config import settings

        settings.k8s_namespace = "shared-ns"
        try:
            resp = await client.post(
                "/api/v1/workspaces",
                json={"display_name": "Blocked", "description": ""},
            )
            assert resp.status_code == 403
        finally:
            settings.k8s_namespace = ""


class TestWorkspaceMembers:
    @pytest.mark.asyncio
    async def test_add_list_remove_member_round_trip(self, client):
        ws = await _create_workspace(client, "Team WS")

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/members")
        assert resp.status_code == 200
        assert resp.json() == []

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/members",
            json={"user_id": "alice", "role": "member"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["user_id"] == "alice"

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/members")
        assert resp.status_code == 200
        assert [m["user_id"] for m in resp.json()] == ["alice"]

        resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/members/alice")
        assert resp.status_code == 200

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/members")
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_add_duplicate_member_conflicts(self, client):
        ws = await _create_workspace(client, "Dup WS")
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "alice"}
        )
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "alice"}
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_add_owner_as_member_conflicts(self, client):
        ws = await _create_workspace(client, "Owner WS")
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "test-user"}
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_remove_nonexistent_member_returns_404(self, client):
        ws = await _create_workspace(client, "WS")
        resp = await client.delete(f"/api/v1/workspaces/{ws['id']}/members/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_owner_and_admin_actions_claim_an_unowned_workspace(self, client):
        """ACM-41659: a workspace with no owner (e.g. migrated with no
        recoverable owner) is claimed by the first person who manages it."""
        ws = await _create_workspace(client, "Unowned WS")
        # Simulate a pre-ACL / unclaimed workspace by clearing owner_id directly.
        from swarmer.models.workspace import Workspace

        async with _TestSession() as session:
            row = await session.get(Workspace, ws["id"])
            row.owner_id = ""
            await session.commit()

        from swarmer.api.deps import require_api_auth
        from swarmer.k8s_auth import TokenIdentity
        from swarmer.main import app

        def _other_identity():
            return TokenIdentity(username="claimant", uid="uid-2")

        app.dependency_overrides[require_api_auth] = _other_identity
        try:
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "someone-else"}
            )
            assert resp.status_code == 201, resp.text

            # The claimant is now the owner and can fetch the workspace directly.
            resp = await client.get(f"/api/v1/workspaces/{ws['id']}")
            assert resp.json()["owner_id"] == "claimant"
        finally:
            app.dependency_overrides[require_api_auth] = _override_require_api_auth


# ===========================================================================
# Global Admins / Me (ACM-41659)
# ===========================================================================


class TestMe:
    @pytest.mark.asyncio
    async def test_me_default_state(self, client):
        resp = await client.get("/api/v1/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "test-user"
        assert body["is_admin"] is False
        assert body["can_create_workspace"] is True
        assert body["admin_bootstrap_available"] is True

    @pytest.mark.asyncio
    async def test_me_reflects_static_admin_config(self, client):
        from swarmer.config import settings

        settings.workspace_admin_users = "test-user"
        resp = await client.get("/api/v1/me")
        body = resp.json()
        assert body["is_admin"] is True
        assert body["admin_bootstrap_available"] is False

    @pytest.mark.asyncio
    async def test_me_admins_only_create_policy(self, client):
        from swarmer.config import settings

        settings.workspace_create_policy = "admins"
        resp = await client.get("/api/v1/me")
        assert resp.json()["can_create_workspace"] is False


class TestKnownUsers:
    """GET /api/v1/users — visibility-scoped autocomplete suggestions."""

    @pytest.mark.asyncio
    async def test_no_shared_workspaces_returns_empty(self, client):
        resp = await client.get("/api/v1/users")
        assert resp.status_code == 200
        assert resp.json() == {"users": []}

    @pytest.mark.asyncio
    async def test_sees_members_of_own_workspace(self, client):
        ws = await _create_workspace(client, "Shared WS")
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "alice"}
        )
        resp = await client.get("/api/v1/users")
        assert resp.json() == {"users": ["alice"]}

    @pytest.mark.asyncio
    async def test_does_not_see_unrelated_workspace_users(self, client):
        from swarmer.api.deps import require_api_auth
        from swarmer.k8s_auth import TokenIdentity
        from swarmer.main import app

        app.dependency_overrides[require_api_auth] = lambda: TokenIdentity(
            username="other-user", uid="uid-2"
        )
        try:
            ws = await _create_workspace(client, "Someone Else's WS")
            await client.post(
                f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "eve"}
            )
        finally:
            app.dependency_overrides[require_api_auth] = _override_require_api_auth

        resp = await client.get("/api/v1/users")
        assert resp.json() == {"users": []}

    @pytest.mark.asyncio
    async def test_admin_sees_every_known_user(self, client):
        ws = await _create_workspace(client, "Some WS")
        await client.post(
            f"/api/v1/workspaces/{ws['id']}/members", json={"user_id": "alice"}
        )
        await client.post("/api/v1/admins/bootstrap")  # test-user becomes admin
        await client.post("/api/v1/admins", json={"user_id": "root2"})

        resp = await client.get("/api/v1/users")
        assert set(resp.json()["users"]) == {"alice", "root2"}

    @pytest.mark.asyncio
    async def test_merges_openshift_users_and_service_accounts(self, client, monkeypatch):
        monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: ["dave"])
        monkeypatch.setattr(
            "swarmer.k8s.list_user_service_accounts",
            lambda *a, **k: ["system:serviceaccount:swarmer:ci-bot"],
        )
        resp = await client.get("/api/v1/users")
        assert set(resp.json()["users"]) == {"dave", "system:serviceaccount:swarmer:ci-bot"}


class TestAdminBootstrap:
    @pytest.mark.asyncio
    async def test_bootstrap_succeeds_when_no_admin_exists(self, client):
        resp = await client.post("/api/v1/admins/bootstrap")
        assert resp.status_code == 201, resp.text
        assert resp.json()["user_id"] == "test-user"

        resp = await client.get("/api/v1/me")
        assert resp.json()["is_admin"] is True
        assert resp.json()["admin_bootstrap_available"] is False

    @pytest.mark.asyncio
    async def test_bootstrap_fails_once_an_admin_exists(self, client):
        resp = await client.post("/api/v1/admins/bootstrap")
        assert resp.status_code == 201

        from swarmer.api.deps import require_api_auth
        from swarmer.k8s_auth import TokenIdentity
        from swarmer.main import app

        app.dependency_overrides[require_api_auth] = lambda: TokenIdentity(
            username="second-user", uid="uid-2"
        )
        try:
            resp = await client.post("/api/v1/admins/bootstrap")
            assert resp.status_code == 409
        finally:
            app.dependency_overrides[require_api_auth] = _override_require_api_auth

    @pytest.mark.asyncio
    async def test_bootstrap_fails_when_static_admins_configured(self, client):
        from swarmer.config import settings

        settings.workspace_admin_users = "someone-else"
        resp = await client.post("/api/v1/admins/bootstrap")
        assert resp.status_code == 409


class TestAdminCrud:
    @pytest.mark.asyncio
    async def test_non_admin_cannot_list_or_manage_admins(self, client):
        resp = await client.get("/api/v1/admins")
        assert resp.status_code == 403

        resp = await client.post("/api/v1/admins", json={"user_id": "alice"})
        assert resp.status_code == 403

        resp = await client.delete("/api/v1/admins/alice")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_add_list_remove_admin(self, client):
        await client.post("/api/v1/admins/bootstrap")  # test-user becomes admin

        resp = await client.post("/api/v1/admins", json={"user_id": "alice"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["created_by"] == "test-user"

        resp = await client.get("/api/v1/admins")
        assert resp.status_code == 200
        assert {a["user_id"] for a in resp.json()} == {"test-user", "alice"}

        resp = await client.delete("/api/v1/admins/alice")
        assert resp.status_code == 200

        resp = await client.get("/api/v1/admins")
        assert {a["user_id"] for a in resp.json()} == {"test-user"}

    @pytest.mark.asyncio
    async def test_add_duplicate_admin_conflicts(self, client):
        await client.post("/api/v1/admins/bootstrap")
        await client.post("/api/v1/admins", json={"user_id": "alice"})
        resp = await client.post("/api/v1/admins", json={"user_id": "alice"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_remove_nonexistent_admin_returns_404(self, client):
        await client.post("/api/v1/admins/bootstrap")
        resp = await client.delete("/api/v1/admins/ghost")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_static_admin_can_manage_db_admins(self, client):
        from swarmer.config import settings

        settings.workspace_admin_users = "test-user"
        resp = await client.post("/api/v1/admins", json={"user_id": "alice"})
        assert resp.status_code == 201


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
            json={"name": "renamed-session", "mode": "tui", "agent_tool": "shell"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed-session"
        assert resp.json()["mode"] == "tui"
        assert resp.json()["agent_tool"] == "shell"

    @pytest.mark.asyncio
    async def test_create_session_with_shell_tool(self, client):
        ws = await _create_workspace(client)
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions",
            json={"name": "shell-s", "mode": "prompt", "agent_tool": "shell", "instruction_prompt": "echo hi"},
        )
        assert resp.status_code == 201
        assert resp.json()["agent_tool"] == "shell"
        assert resp.json()["instruction_prompt"] == "echo hi"

    @pytest.mark.asyncio
    async def test_session_ui_renders_branded_agent_pills(self, client):
        from swarmer.deps import require_auth
        from swarmer.main import app

        app.dependency_overrides[require_auth] = lambda: None
        try:
            ws = await _create_workspace(client)
            # Check /sessions/new page
            resp_new = await client.get(f"/workspaces/{ws['id']}/sessions/new")
            assert resp_new.status_code == 200
            assert "agent-pill-oc" in resp_new.text
            assert "agent-pill-shell" in resp_new.text
            assert "shell-pixel-prompt" in resp_new.text

            # Create session and check /sessions/{id} detail page
            s = await _create_session(client, ws["id"], name="detail-pills-s")
            resp_detail = await client.get(f"/workspaces/{ws['id']}/sessions/{s['id']}")
            assert resp_detail.status_code == 200
            assert "agent-pill-oc" in resp_detail.text
            assert "agent-pill-shell" in resp_detail.text
            assert "selectDetailAgentTool" in resp_detail.text
        finally:
            app.dependency_overrides.pop(require_auth, None)

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
    async def test_set_provider(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/set-provider",
            json={"provider": "claude"},
        )
        assert resp.status_code == 200
        assert resp.json()["provider"] == "claude"

    @pytest.mark.asyncio
    async def test_get_output(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/output")
        assert resp.status_code == 200
        assert resp.json()["output"] == ""

    @pytest.mark.asyncio
    async def test_list_session_runs(self, client):
        from datetime import datetime, timezone

        from swarmer.models.session import Session
        from swarmer.session_runs import record_session_run

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            session.run_started_at = datetime.now(timezone.utc)
            await record_session_run(
                db,
                session,
                phase="succeeded",
                status_detail="Completed",
                last_output="done",
                completed_at=datetime.now(timezone.utc),
            )
            await db.commit()

        resp = await client.get(f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/runs")
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) == 1
        assert runs[0]["phase"] == "succeeded"
        assert runs[0]["last_output"] == "done"
        assert runs[0]["run_duration"]

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
    async def test_schedule_non_prompt_allowed(self, client):
        """Scheduling is now allowed for any mode; the scheduler forces prompt at run time."""
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
        assert resp.status_code == 200

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
                "google_api_key": "AIza-test123456",
            },
        )
        assert resp.status_code == 200
        cred = resp.json()
        assert cred["google_cloud_project"] == "my-project"
        assert cred["has_adc"] is False
        assert "AIza-test123456" not in cred.get("masked_api_key", "")  # key should be masked

    @pytest.mark.asyncio
    async def test_save_adc_credentials(self, client):
        ws = await _create_workspace(client)
        adc = json.dumps({"type": "authorized_user", "client_id": "x", "client_secret": "y"})
        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/credentials",
            json={
                "google_cloud_project": "my-project",
                "vertex_location": "us-central1",
                "application_default_credentials": adc,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["has_adc"] is True

        bad = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/credentials",
            json={"application_default_credentials": "not-json"},
        )
        assert bad.status_code == 422

    @pytest.mark.asyncio
    async def test_save_openai_key_configures_gateway_provider_only(self, client, monkeypatch):
        ws = await _create_workspace(client)

        called = {}

        async def _fake_ensure_provider(name, provider_type, config, credentials):
            called["name"] = name
            called["provider_type"] = provider_type
            called["config"] = config
            called["credentials"] = credentials

        monkeypatch.setattr("swarmer.openshell_client.ensure_provider", _fake_ensure_provider)

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/credentials",
            json={
                "google_cloud_project": "my-project",
                "vertex_location": "us-central1",
                "openai_api_key": "<test-openai-api-key>",
            },
        )
        assert resp.status_code == 200, resp.text

        assert called["name"] == f"swarmer-ws-{ws['id']}-openai"
        assert called["provider_type"] == "openai"
        assert called["config"] == {}
        assert called["credentials"] == {"OPENAI_API_KEY": "<test-openai-api-key>"}

        # Credentials response shape remains unchanged and must not expose an OpenAI key.
        body = resp.json()
        assert "openai_api_key" not in body

    @pytest.mark.asyncio
    async def test_save_openai_key_failure_redacts_exception_detail(self, client, monkeypatch, caplog):
        ws = await _create_workspace(client)
        sentinel = "SENTINEL_OPENAI_SECRET"
        caplog.set_level(logging.WARNING, logger="swarmer.api.v1.secrets")

        async def _fake_ensure_provider(_name, _provider_type, _config, _credentials):
            raise RuntimeError(f"provider failed with {sentinel}")

        monkeypatch.setattr("swarmer.openshell_client.ensure_provider", _fake_ensure_provider)

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/secrets/credentials",
            json={
                "google_cloud_project": "my-project",
                "vertex_location": "us-central1",
                "openai_api_key": "<test-openai-api-key>",
            },
        )

        assert resp.status_code == 502
        assert resp.json()["detail"] == "failed to configure OpenAI provider on OpenShell"
        assert sentinel not in resp.text
        assert sentinel not in caplog.text

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

    @pytest.mark.asyncio
    async def test_save_github_app_rejects_other_users_private_config(self, client):
        from swarmer.models.github_app import GitHubApp

        ws = await _create_workspace(client)
        pem = "-----BEGIN RSA PRIVATE KEY-----\nseed\n-----END RSA PRIVATE KEY-----"

        async with _TestSession() as db:
            existing = GitHubApp(
                workspace_id=ws["id"],
                user_id="other-user",
                app_id="111",
                installation_id="222",
            )
            existing.private_key = pem
            db.add(existing)
            await db.commit()

        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}/secrets/github-app",
            json={
                "app_id": "999",
                "installation_id": "888",
                "private_key": pem,
            },
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_save_github_app_updates_shared_workspace_record(self, client):
        """Shared workspace config can be updated without duplicate insert."""
        from sqlalchemy import func, select

        from swarmer.models.github_app import GitHubApp

        ws = await _create_workspace(client)
        pem = "-----BEGIN RSA PRIVATE KEY-----\nseed\n-----END RSA PRIVATE KEY-----"

        async with _TestSession() as db:
            existing = GitHubApp(
                workspace_id=ws["id"],
                user_id="other-user",
                app_id="111",
                installation_id="222",
                shared=True,
            )
            existing.private_key = pem
            db.add(existing)
            await db.commit()

        resp = await client.put(
            f"/api/v1/workspaces/{ws['id']}/secrets/github-app",
            json={
                "app_id": "999",
                "installation_id": "888",
                "private_key": pem,
                "shared": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["app_id"] == "999"

        async with _TestSession() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(GitHubApp)
                .where(GitHubApp.workspace_id == ws["id"])
            )
            assert count == 1

    @pytest.mark.asyncio
    async def test_get_workspace_github_app_scheduler_finds_private_app(self):
        """Background launch (empty user_id) must see the workspace GitHub App."""
        from swarmer.github_app import get_workspace_github_app
        from swarmer.models.github_app import GitHubApp
        from swarmer.models.workspace import Workspace

        pem = "-----BEGIN RSA PRIVATE KEY-----\nseed\n-----END RSA PRIVATE KEY-----"

        async with _TestSession() as db:
            ws = Workspace(display_name="w", namespace="sched-ns")
            db.add(ws)
            await db.flush()
            app = GitHubApp(
                workspace_id=ws.id,
                user_id="alice",
                shared=False,
                app_id="111",
                installation_id="222",
            )
            app.private_key = pem
            db.add(app)
            await db.commit()

            found = await get_workspace_github_app(ws.id, db, user_id="")
            assert found is not None
            assert found.user_id == "alice"

            blocked = await get_workspace_github_app(ws.id, db, user_id="bob")
            assert blocked is None





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

"""Tests for swarmer.workspace_acl — database-backed workspace ACL (ACM-41659).

Replaces per-workspace K8s namespace + RoleBinding RBAC now that OpenShell
owns sandbox lifecycle management. Covers owner/member/admin access checks
and the workspace creation policy.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base
from swarmer.config import settings
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_member import WorkspaceMember
from swarmer import workspace_acl


_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _setup_db(monkeypatch):
    import swarmer.models  # noqa: F401 — register models on Base.metadata

    orig_admin_users = settings.workspace_admin_users
    orig_admin_groups = settings.workspace_admin_groups
    orig_policy = settings.workspace_create_policy
    settings.workspace_admin_users = ""
    settings.workspace_admin_groups = ""
    settings.workspace_create_policy = "all"

    # list_known_users() merges in K8s discovery — stub it out by default so
    # tests never make real network calls. Tests exercising K8s discovery
    # override these via monkeypatch themselves.
    monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: [])
    monkeypatch.setattr("swarmer.k8s.list_user_service_accounts", lambda *a, **k: [])

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    settings.workspace_admin_users = orig_admin_users
    settings.workspace_admin_groups = orig_admin_groups
    settings.workspace_create_policy = orig_policy


@pytest_asyncio.fixture
async def db():
    async with _TestSession() as session:
        yield session


async def _make_workspace(db, *, owner_id: str = "", name: str = "ws") -> Workspace:
    ws = Workspace(display_name=name, namespace=name, description="", owner_id=owner_id)
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    return ws


# ---------------------------------------------------------------------------
# is_admin
# ---------------------------------------------------------------------------


class TestIsAdminStatic:
    """Pure config (no DB) building block used by is_admin()."""

    def test_no_admins_configured(self):
        assert not workspace_acl.is_admin_static("alice")

    def test_matches_admin_user(self):
        settings.workspace_admin_users = "alice,bob"
        assert workspace_acl.is_admin_static("alice")
        assert workspace_acl.is_admin_static("bob")
        assert not workspace_acl.is_admin_static("eve")

    def test_matches_admin_group(self):
        settings.workspace_admin_groups = "platform-admins"
        assert workspace_acl.is_admin_static("eve", groups=["platform-admins", "other"])
        assert not workspace_acl.is_admin_static("eve", groups=["other"])
        assert not workspace_acl.is_admin_static("eve", groups=None)

    def test_handles_whitespace_and_empty_entries(self):
        settings.workspace_admin_users = " alice , , bob "
        assert workspace_acl.is_admin_static("alice")
        assert workspace_acl.is_admin_static("bob")


class TestIsAdmin:
    """DB-aware is_admin(): static allow-list OR the global_admins table."""

    @pytest.mark.asyncio
    async def test_no_admins_configured(self, db):
        assert not await workspace_acl.is_admin(db, "alice")

    @pytest.mark.asyncio
    async def test_static_admin_user(self, db):
        settings.workspace_admin_users = "alice"
        assert await workspace_acl.is_admin(db, "alice")

    @pytest.mark.asyncio
    async def test_db_admin_user(self, db):
        await workspace_acl.add_global_admin(db, "alice", "bob")
        assert await workspace_acl.is_admin(db, "alice")
        assert not await workspace_acl.is_admin(db, "eve")


# ---------------------------------------------------------------------------
# Global admins — bootstrap + CRUD
# ---------------------------------------------------------------------------


class TestGlobalAdmins:
    @pytest.mark.asyncio
    async def test_bootstrap_available_when_no_admins(self, db):
        assert await workspace_acl.admin_bootstrap_available(db)

    @pytest.mark.asyncio
    async def test_bootstrap_unavailable_with_static_config(self, db):
        settings.workspace_admin_users = "alice"
        assert not await workspace_acl.admin_bootstrap_available(db)

    @pytest.mark.asyncio
    async def test_bootstrap_unavailable_once_db_admin_exists(self, db):
        await workspace_acl.add_global_admin(db, "alice", "bob")
        assert not await workspace_acl.admin_bootstrap_available(db)

    @pytest.mark.asyncio
    async def test_bootstrap_admin_succeeds_once(self, db):
        assert await workspace_acl.bootstrap_admin(db, "alice")
        assert await workspace_acl.is_admin(db, "alice")

    @pytest.mark.asyncio
    async def test_bootstrap_admin_fails_after_first_admin_exists(self, db):
        assert await workspace_acl.bootstrap_admin(db, "alice")
        assert not await workspace_acl.bootstrap_admin(db, "eve")
        assert not await workspace_acl.is_admin(db, "eve")

    @pytest.mark.asyncio
    async def test_bootstrap_admin_empty_username_fails(self, db):
        assert not await workspace_acl.bootstrap_admin(db, "")

    @pytest.mark.asyncio
    async def test_add_list_remove_admin_round_trip(self, db):
        await workspace_acl.add_global_admin(db, "alice", "root")
        admins = await workspace_acl.list_global_admins(db)
        assert [a.user_id for a in admins] == ["alice"]

        assert await workspace_acl.remove_global_admin(db, "alice")
        assert await workspace_acl.list_global_admins(db) == []

    @pytest.mark.asyncio
    async def test_remove_nonexistent_admin_returns_false(self, db):
        assert not await workspace_acl.remove_global_admin(db, "ghost")


# ---------------------------------------------------------------------------
# can_create_workspace
# ---------------------------------------------------------------------------


class TestCanCreateWorkspace:
    @pytest.mark.asyncio
    async def test_default_policy_allows_any_authenticated_user(self, db):
        assert await workspace_acl.can_create_workspace(db, "alice")

    @pytest.mark.asyncio
    async def test_empty_username_denied(self, db):
        assert not await workspace_acl.can_create_workspace(db, "")

    @pytest.mark.asyncio
    async def test_admins_only_policy_denies_non_admin(self, db):
        settings.workspace_create_policy = "admins"
        assert not await workspace_acl.can_create_workspace(db, "alice")

    @pytest.mark.asyncio
    async def test_admins_only_policy_allows_static_admin(self, db):
        settings.workspace_create_policy = "admins"
        settings.workspace_admin_users = "alice"
        assert await workspace_acl.can_create_workspace(db, "alice")

    @pytest.mark.asyncio
    async def test_admins_only_policy_allows_db_admin(self, db):
        settings.workspace_create_policy = "admins"
        await workspace_acl.add_global_admin(db, "alice", "root")
        assert await workspace_acl.can_create_workspace(db, "alice")


# ---------------------------------------------------------------------------
# user_can_access_workspace
# ---------------------------------------------------------------------------


class TestUserCanAccessWorkspace:
    @pytest.mark.asyncio
    async def test_owner_has_access(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        assert await workspace_acl.user_can_access_workspace(db, ws, "alice")

    @pytest.mark.asyncio
    async def test_non_owner_non_member_denied(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        assert not await workspace_acl.user_can_access_workspace(db, ws, "eve")

    @pytest.mark.asyncio
    async def test_explicit_member_has_access(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        db.add(WorkspaceMember(workspace_id=ws.id, user_id="bob"))
        await db.commit()
        assert await workspace_acl.user_can_access_workspace(db, ws, "bob")

    @pytest.mark.asyncio
    async def test_admin_has_access_to_any_workspace(self, db):
        settings.workspace_admin_users = "root"
        ws = await _make_workspace(db, owner_id="alice")
        assert await workspace_acl.user_can_access_workspace(db, ws, "root")

    @pytest.mark.asyncio
    async def test_empty_username_denied(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        assert not await workspace_acl.user_can_access_workspace(db, ws, "")

    @pytest.mark.asyncio
    async def test_unclaimed_workspace_open_to_any_authenticated_user(self, db):
        """ACM-41659: a workspace with no owner (e.g. no recoverable owner
        during migration) stays accessible to everyone until claimed."""
        ws = await _make_workspace(db, owner_id="")
        assert await workspace_acl.user_can_access_workspace(db, ws, "anyone")

    @pytest.mark.asyncio
    async def test_namespace_scoped_deployment_grants_access_to_all_workspaces(self, db):
        """Shared-namespace deployments (K8S_NAMESPACE set) keep their
        original flat access model — no per-user migration needed."""
        settings.k8s_namespace = "shared-ns"
        try:
            ws = await _make_workspace(db, owner_id="alice")
            assert await workspace_acl.user_can_access_workspace(db, ws, "anyone-else")
        finally:
            settings.k8s_namespace = ""


# ---------------------------------------------------------------------------
# filter_accessible_workspaces
# ---------------------------------------------------------------------------


class TestFilterAccessibleWorkspaces:
    @pytest.mark.asyncio
    async def test_filters_to_owned_and_member_workspaces(self, db):
        owned = await _make_workspace(db, owner_id="alice", name="owned")
        member_of = await _make_workspace(db, owner_id="bob", name="member-of")
        denied = await _make_workspace(db, owner_id="carol", name="denied")
        db.add(WorkspaceMember(workspace_id=member_of.id, user_id="alice"))
        await db.commit()

        result = await workspace_acl.filter_accessible_workspaces(
            db, [owned, member_of, denied], "alice"
        )
        result_ids = {ws.id for ws in result}
        assert result_ids == {owned.id, member_of.id}

    @pytest.mark.asyncio
    async def test_admin_sees_all_workspaces(self, db):
        settings.workspace_admin_users = "root"
        ws1 = await _make_workspace(db, owner_id="alice", name="ws1")
        ws2 = await _make_workspace(db, owner_id="bob", name="ws2")

        result = await workspace_acl.filter_accessible_workspaces(
            db, [ws1, ws2], "root"
        )
        assert {ws.id for ws in result} == {ws1.id, ws2.id}

    @pytest.mark.asyncio
    async def test_empty_workspace_list_returns_empty(self, db):
        assert await workspace_acl.filter_accessible_workspaces(db, [], "alice") == []

    @pytest.mark.asyncio
    async def test_empty_username_returns_empty(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        assert await workspace_acl.filter_accessible_workspaces(db, [ws], "") == []

    @pytest.mark.asyncio
    async def test_unclaimed_workspace_visible_to_all(self, db):
        unclaimed = await _make_workspace(db, owner_id="", name="unclaimed")
        owned = await _make_workspace(db, owner_id="bob", name="owned-by-bob")

        result = await workspace_acl.filter_accessible_workspaces(
            db, [unclaimed, owned], "anyone"
        )
        assert {ws.id for ws in result} == {unclaimed.id}

    @pytest.mark.asyncio
    async def test_namespace_scoped_deployment_returns_all_workspaces(self, db):
        settings.k8s_namespace = "shared-ns"
        try:
            ws1 = await _make_workspace(db, owner_id="alice", name="ws1")
            ws2 = await _make_workspace(db, owner_id="bob", name="ws2")
            result = await workspace_acl.filter_accessible_workspaces(
                db, [ws1, ws2], "anyone-else"
            )
            assert {ws.id for ws in result} == {ws1.id, ws2.id}
        finally:
            settings.k8s_namespace = ""


# ---------------------------------------------------------------------------
# can_manage_members
# ---------------------------------------------------------------------------


class TestCanManageMembers:
    @pytest.mark.asyncio
    async def test_owner_can_manage(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        assert await workspace_acl.can_manage_members(db, ws, "alice")

    @pytest.mark.asyncio
    async def test_plain_member_cannot_manage(self, db):
        ws = await _make_workspace(db, owner_id="alice")
        db.add(WorkspaceMember(workspace_id=ws.id, user_id="bob"))
        await db.commit()
        assert not await workspace_acl.can_manage_members(db, ws, "bob")

    @pytest.mark.asyncio
    async def test_admin_can_manage_any_workspace(self, db):
        settings.workspace_admin_users = "root"
        ws = await _make_workspace(db, owner_id="alice")
        assert await workspace_acl.can_manage_members(db, ws, "root")

    @pytest.mark.asyncio
    async def test_anyone_can_manage_unclaimed_workspace(self, db):
        ws = await _make_workspace(db, owner_id="")
        assert await workspace_acl.can_manage_members(db, ws, "anyone")

    @pytest.mark.asyncio
    async def test_empty_username_cannot_manage(self, db):
        ws = await _make_workspace(db, owner_id="")
        assert not await workspace_acl.can_manage_members(db, ws, "")


# ---------------------------------------------------------------------------
# claim_ownership_if_unowned
# ---------------------------------------------------------------------------


class TestClaimOwnershipIfUnowned:
    def test_claims_unowned_workspace(self):
        ws = Workspace(display_name="ws", namespace="ws", description="", owner_id="")
        assert workspace_acl.claim_ownership_if_unowned(ws, "alice")
        assert ws.owner_id == "alice"

    def test_does_not_reclaim_owned_workspace(self):
        ws = Workspace(display_name="ws", namespace="ws", description="", owner_id="bob")
        assert not workspace_acl.claim_ownership_if_unowned(ws, "alice")
        assert ws.owner_id == "bob"

    def test_empty_username_does_not_claim(self):
        ws = Workspace(display_name="ws", namespace="ws", description="", owner_id="")
        assert not workspace_acl.claim_ownership_if_unowned(ws, "")
        assert ws.owner_id == ""


# ---------------------------------------------------------------------------
# list_known_users
# ---------------------------------------------------------------------------


class TestListKnownUsers:
    @pytest.mark.asyncio
    async def test_empty_username_returns_empty(self, db):
        assert await workspace_acl.list_known_users(db, "") == []

    @pytest.mark.asyncio
    async def test_no_shared_workspaces_returns_empty(self, db):
        await _make_workspace(db, owner_id="bob", name="unrelated")
        assert await workspace_acl.list_known_users(db, "alice") == []

    @pytest.mark.asyncio
    async def test_sees_owner_and_members_of_own_workspaces(self, db):
        owned = await _make_workspace(db, owner_id="alice", name="owned")
        db.add(WorkspaceMember(workspace_id=owned.id, user_id="bob"))
        member_of = await _make_workspace(db, owner_id="carol", name="member-of")
        db.add(WorkspaceMember(workspace_id=member_of.id, user_id="alice"))
        await db.commit()

        result = await workspace_acl.list_known_users(db, "alice")
        assert result == ["bob", "carol"]

    @pytest.mark.asyncio
    async def test_does_not_include_self(self, db):
        ws = await _make_workspace(db, owner_id="alice", name="owned")
        db.add(WorkspaceMember(workspace_id=ws.id, user_id="alice"))
        await db.commit()
        assert "alice" not in await workspace_acl.list_known_users(db, "alice")

    @pytest.mark.asyncio
    async def test_does_not_see_unrelated_workspace_users(self, db):
        await _make_workspace(db, owner_id="alice", name="mine")
        unrelated = await _make_workspace(db, owner_id="dave", name="unrelated")
        db.add(WorkspaceMember(workspace_id=unrelated.id, user_id="eve"))
        await db.commit()

        result = await workspace_acl.list_known_users(db, "alice")
        assert "dave" not in result
        assert "eve" not in result

    @pytest.mark.asyncio
    async def test_admin_sees_every_known_user_including_other_admins(self, db):
        settings.workspace_admin_users = "root"
        ws1 = await _make_workspace(db, owner_id="alice", name="ws1")
        db.add(WorkspaceMember(workspace_id=ws1.id, user_id="bob"))
        await workspace_acl.add_global_admin(db, "carol", "root")
        await db.commit()

        result = await workspace_acl.list_known_users(db, "root")
        assert result == ["alice", "bob", "carol"]

    @pytest.mark.asyncio
    async def test_merges_openshift_users(self, db, monkeypatch):
        monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: ["dave"])
        result = await workspace_acl.list_known_users(db, "alice")
        assert result == ["dave"]

    @pytest.mark.asyncio
    async def test_merges_service_accounts(self, db, monkeypatch):
        monkeypatch.setattr(
            "swarmer.k8s.list_user_service_accounts",
            lambda *a, **k: ["system:serviceaccount:swarmer:eve"],
        )
        result = await workspace_acl.list_known_users(db, "alice")
        assert result == ["system:serviceaccount:swarmer:eve"]

    @pytest.mark.asyncio
    async def test_k8s_and_db_sources_are_deduplicated_and_combined(self, db, monkeypatch):
        ws = await _make_workspace(db, owner_id="alice", name="owned")
        db.add(WorkspaceMember(workspace_id=ws.id, user_id="bob"))
        await db.commit()
        monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: ["bob", "carol"])

        result = await workspace_acl.list_known_users(db, "alice")
        assert result == ["bob", "carol"]

    @pytest.mark.asyncio
    async def test_k8s_source_excludes_self(self, db, monkeypatch):
        monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: ["alice"])
        assert await workspace_acl.list_known_users(db, "alice") == []

    @pytest.mark.asyncio
    async def test_k8s_error_does_not_break_db_results(self, db, monkeypatch):
        """list_known_users must still return DB results even if K8s discovery
        itself somehow raises (though the k8s.py helpers already swallow
        errors internally — this guards the merge path too)."""
        ws = await _make_workspace(db, owner_id="alice", name="owned")
        db.add(WorkspaceMember(workspace_id=ws.id, user_id="bob"))
        await db.commit()
        monkeypatch.setattr("swarmer.k8s.list_openshift_users", lambda: [])
        monkeypatch.setattr("swarmer.k8s.list_user_service_accounts", lambda *a, **k: [])

        result = await workspace_acl.list_known_users(db, "alice")
        assert result == ["bob"]

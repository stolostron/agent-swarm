"""Tests for swarmer.workspace_migration — startup K8s RoleBinding sync
(ACM-41659 follow-up).

Mirrors legacy `make grant-workspace-access` RoleBinding grants into
workspace_members so nobody has to be manually re-added after upgrading away
from per-workspace namespace RBAC. Best-effort and non-fatal: K8s errors are
swallowed, never raised.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.config import settings
from swarmer.database import Base
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_member import WorkspaceMember
from swarmer.workspace_migration import sync_k8s_workspace_members


_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    import swarmer.models  # noqa: F401

    orig_ns = settings.k8s_namespace
    settings.k8s_namespace = ""

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    settings.k8s_namespace = orig_ns


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


class TestSyncK8sWorkspaceMembers:
    @pytest.mark.asyncio
    async def test_migrates_role_binding_subjects_into_members(self, db):
        ws = await _make_workspace(db, owner_id="", name="team-a")

        with patch(
            "swarmer.k8s.list_swarmer_user_role_binding_identities",
            return_value=["system:serviceaccount:swarmer:alice", "bob"],
        ):
            await sync_k8s_workspace_members(db)

        result = await db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == ws.id)
        )
        member_ids = {row[0] for row in result.all()}
        assert member_ids == {"system:serviceaccount:swarmer:alice", "bob"}

        await db.refresh(ws)
        # First identity returned becomes owner since the workspace had none.
        assert ws.owner_id == "system:serviceaccount:swarmer:alice"

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_owner(self, db):
        ws = await _make_workspace(db, owner_id="carol", name="team-b")

        with patch(
            "swarmer.k8s.list_swarmer_user_role_binding_identities",
            return_value=["dave"],
        ):
            await sync_k8s_workspace_members(db)

        await db.refresh(ws)
        assert ws.owner_id == "carol"
        result = await db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == ws.id)
        )
        assert result.scalar_one() == "dave"

    @pytest.mark.asyncio
    async def test_skips_existing_members_and_owner_duplicate(self, db):
        ws = await _make_workspace(db, owner_id="alice", name="team-c")
        db.add(WorkspaceMember(workspace_id=ws.id, user_id="bob"))
        await db.commit()

        with patch(
            "swarmer.k8s.list_swarmer_user_role_binding_identities",
            return_value=["alice", "bob"],  # alice is owner, bob already a member
        ):
            await sync_k8s_workspace_members(db)

        result = await db.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == ws.id)
        )
        assert list(result.scalars().all()) == ["bob"]

    @pytest.mark.asyncio
    async def test_no_op_when_k8s_namespace_shared(self, db):
        """Shared-namespace deployments already grant everyone access to
        everything (see workspace_acl.py) — nothing to sync."""
        await _make_workspace(db, owner_id="", name="team-d")
        settings.k8s_namespace = "shared-ns"

        with patch("swarmer.k8s.list_swarmer_user_role_binding_identities") as mock_list:
            await sync_k8s_workspace_members(db)
            mock_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_k8s_error_for_one_workspace_does_not_block_others(self, db):
        ws1 = await _make_workspace(db, owner_id="", name="team-e")
        ws2 = await _make_workspace(db, owner_id="", name="team-f")

        def _side_effect(namespace):
            if namespace == "team-e":
                raise RuntimeError("K8s unreachable")
            return ["eve"]

        with patch(
            "swarmer.k8s.list_swarmer_user_role_binding_identities",
            side_effect=_side_effect,
        ):
            await sync_k8s_workspace_members(db)  # must not raise

        await db.refresh(ws1)
        await db.refresh(ws2)
        assert ws1.owner_id == ""
        assert ws2.owner_id == "eve"

    @pytest.mark.asyncio
    async def test_no_workspaces_is_a_no_op(self, db):
        with patch("swarmer.k8s.list_swarmer_user_role_binding_identities") as mock_list:
            await sync_k8s_workspace_members(db)
            mock_list.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_identities_leaves_workspace_untouched(self, db):
        ws = await _make_workspace(db, owner_id="", name="team-g")

        with patch(
            "swarmer.k8s.list_swarmer_user_role_binding_identities",
            return_value=[],
        ):
            await sync_k8s_workspace_members(db)

        await db.refresh(ws)
        assert ws.owner_id == ""
        result = await db.execute(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id)
        )
        assert result.scalar_one_or_none() is None

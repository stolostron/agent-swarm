"""Tests for concurrent agent container limiting and the queue mechanism.

Written test-first (TDD) before implementation. Tests cover:
  - Session model: 'queued' phase, is_active, badge class
  - DB helpers: _count_running_sessions, _get_queue_position, _get_capacity_summary
  - _do_launch() queue gate (sessions queue when at cap; proceed when under cap)
  - Stop handler: queued session returns to idle without pod cleanup
  - API: launch returns queued (HTTP 200) when at cap
  - Scheduler: _process_queue() FIFO, cooldown, cron capacity check
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from swarmer.database import Base

# ---------------------------------------------------------------------------
# Shared DB fixtures (same pattern as test_api.py)
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
    orig_max = settings.max_concurrent_agents
    settings.k8s_namespace = ""

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


def _override_get_bearer_token():
    return "test-token"


@pytest_asyncio.fixture
async def client():
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
# Helper: create workspace and session via API
# ---------------------------------------------------------------------------


async def _create_workspace(client: AsyncClient, name: str = "Test WS") -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        json={"display_name": name, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(client: AsyncClient, ws_id: int, name: str = "s1", mode: str = "prompt") -> dict:
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sessions",
        json={"name": name, "mode": mode, "agent_tool": "opencode"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _set_phase(session_id: int, phase: str, created_at: datetime | None = None) -> None:
    """Directly set a session's phase (and optionally created_at) in the test DB."""
    async with _TestSession() as db:
        if created_at:
            await db.execute(
                text("UPDATE sessions SET phase=:ph, created_at=:ca WHERE id=:id"),
                {"ph": phase, "ca": created_at.isoformat(sep=" "), "id": session_id},
            )
        else:
            await db.execute(
                text("UPDATE sessions SET phase=:ph WHERE id=:id"),
                {"ph": phase, "id": session_id},
            )
        await db.commit()


# ===========================================================================
# 1. Session model: 'queued' phase
# ===========================================================================


class TestQueuedPhaseModel:
    def test_queued_in_phases(self):
        from swarmer.models.session import PHASES
        assert "queued" in PHASES

    def test_is_active_includes_queued(self):
        from swarmer.models.session import Session
        s = Session(name="t", mode="prompt", phase="queued")
        assert s.is_active is True

    def test_is_active_idle_is_false(self):
        from swarmer.models.session import Session
        s = Session(name="t", mode="prompt", phase="idle")
        assert s.is_active is False

    def test_is_active_pending_is_true(self):
        from swarmer.models.session import Session
        s = Session(name="t", mode="prompt", phase="pending")
        assert s.is_active is True

    def test_is_active_succeeded_is_false(self):
        from swarmer.models.session import Session
        s = Session(name="t", mode="prompt", phase="succeeded")
        assert s.is_active is False

    def test_phase_badge_class_queued(self):
        from swarmer.models.session import Session
        s = Session(name="t", mode="prompt", phase="queued")
        assert s.phase_badge_class == "info"


# ===========================================================================
# 2. DB helpers: _count_running_sessions
# ===========================================================================


class TestCountRunningSessions:
    @pytest.mark.asyncio
    async def test_counts_pending_and_running(self, client):
        ws = await _create_workspace(client, "WS A")
        s1 = await _create_session(client, ws["id"], "s1")
        s2 = await _create_session(client, ws["id"], "s2")
        s3 = await _create_session(client, ws["id"], "s3")
        await _set_phase(s1["id"], "pending")
        await _set_phase(s2["id"], "running")
        await _set_phase(s3["id"], "idle")

        async with _TestSession() as db:
            from swarmer.routers.sessions import _count_running_sessions
            count = await _count_running_sessions(db)
        assert count == 2

    @pytest.mark.asyncio
    async def test_excludes_queued_and_terminal(self, client):
        ws = await _create_workspace(client, "WS B")
        s1 = await _create_session(client, ws["id"], "s1")
        s2 = await _create_session(client, ws["id"], "s2")
        s3 = await _create_session(client, ws["id"], "s3")
        await _set_phase(s1["id"], "queued")
        await _set_phase(s2["id"], "succeeded")
        await _set_phase(s3["id"], "failed")

        async with _TestSession() as db:
            from swarmer.routers.sessions import _count_running_sessions
            count = await _count_running_sessions(db)
        assert count == 0

    @pytest.mark.asyncio
    async def test_counts_globally_across_workspaces(self, client):
        ws1 = await _create_workspace(client, "WS 1")
        ws2 = await _create_workspace(client, "WS 2")
        s1 = await _create_session(client, ws1["id"], "s1")
        s2 = await _create_session(client, ws2["id"], "s2")
        await _set_phase(s1["id"], "running")
        await _set_phase(s2["id"], "pending")

        async with _TestSession() as db:
            from swarmer.routers.sessions import _count_running_sessions
            count = await _count_running_sessions(db)
        assert count == 2

    @pytest.mark.asyncio
    async def test_zero_when_no_active(self, client):
        ws = await _create_workspace(client)
        await _create_session(client, ws["id"])  # idle by default

        async with _TestSession() as db:
            from swarmer.routers.sessions import _count_running_sessions
            count = await _count_running_sessions(db)
        assert count == 0


# ===========================================================================
# 3. DB helpers: _get_queue_position
# ===========================================================================


class TestGetQueuePosition:
    @pytest.mark.asyncio
    async def test_single_queued_is_position_one(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_queue_position
            pos, total = await _get_queue_position(s["id"], db)
        assert pos == 1
        assert total == 1

    @pytest.mark.asyncio
    async def test_fifo_ordering_by_created_at(self, client):
        ws = await _create_workspace(client)
        s1 = await _create_session(client, ws["id"], "first")
        s2 = await _create_session(client, ws["id"], "second")
        s3 = await _create_session(client, ws["id"], "third")
        now = datetime.now(timezone.utc)
        await _set_phase(s1["id"], "queued", created_at=now - timedelta(minutes=10))
        await _set_phase(s2["id"], "queued", created_at=now - timedelta(minutes=5))
        await _set_phase(s3["id"], "queued", created_at=now)

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_queue_position
            pos1, total1 = await _get_queue_position(s1["id"], db)
            pos2, total2 = await _get_queue_position(s2["id"], db)
            pos3, total3 = await _get_queue_position(s3["id"], db)

        assert pos1 == 1 and total1 == 3
        assert pos2 == 2 and total2 == 3
        assert pos3 == 3 and total3 == 3

    @pytest.mark.asyncio
    async def test_position_updates_after_cancel(self, client):
        ws = await _create_workspace(client)
        s1 = await _create_session(client, ws["id"], "first")
        s2 = await _create_session(client, ws["id"], "second")
        now = datetime.now(timezone.utc)
        await _set_phase(s1["id"], "queued", created_at=now - timedelta(minutes=5))
        await _set_phase(s2["id"], "queued", created_at=now)

        # s1 is cancelled (goes back to idle)
        await _set_phase(s1["id"], "idle")

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_queue_position
            pos2, total2 = await _get_queue_position(s2["id"], db)
        assert pos2 == 1
        assert total2 == 1

    @pytest.mark.asyncio
    async def test_global_queue_includes_all_workspaces(self, client):
        ws1 = await _create_workspace(client, "WS 1")
        ws2 = await _create_workspace(client, "WS 2")
        now = datetime.now(timezone.utc)
        s1 = await _create_session(client, ws1["id"], "s1")
        s2 = await _create_session(client, ws2["id"], "s2")
        await _set_phase(s1["id"], "queued", created_at=now - timedelta(minutes=1))
        await _set_phase(s2["id"], "queued", created_at=now)

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_queue_position
            pos2, total = await _get_queue_position(s2["id"], db)
        assert pos2 == 2
        assert total == 2


# ===========================================================================
# 4. DB helpers: _get_capacity_summary
# ===========================================================================


class TestGetCapacitySummary:
    @pytest.mark.asyncio
    async def test_workspace_active_count(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 5

        ws = await _create_workspace(client, "My WS")
        ws2 = await _create_workspace(client, "Other WS")
        s1 = await _create_session(client, ws["id"], "s1")
        s2 = await _create_session(client, ws["id"], "s2")
        s3 = await _create_session(client, ws2["id"], "other")
        await _set_phase(s1["id"], "running")
        await _set_phase(s2["id"], "pending")
        await _set_phase(s3["id"], "running")  # other workspace

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_capacity_summary
            summary = await _get_capacity_summary(ws["id"], db)

        assert summary["ws_active"] == 2  # only this workspace
        assert summary["slots_available"] == 2  # 5 max - 3 global running

    @pytest.mark.asyncio
    async def test_workspace_queued_count(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 3

        ws = await _create_workspace(client, "My WS")
        ws2 = await _create_workspace(client, "Other WS")
        s1 = await _create_session(client, ws["id"], "s1")
        s2 = await _create_session(client, ws["id"], "s2")
        s3 = await _create_session(client, ws2["id"], "other")
        await _set_phase(s1["id"], "queued")
        await _set_phase(s2["id"], "queued")
        await _set_phase(s3["id"], "queued")  # other workspace

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_capacity_summary
            summary = await _get_capacity_summary(ws["id"], db)

        assert summary["ws_queued"] == 2   # only this workspace
        assert summary["max"] == 3
        assert summary["slots_available"] == 3  # 3 max - 0 running

    @pytest.mark.asyncio
    async def test_slots_available_not_negative(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 2

        ws = await _create_workspace(client)
        for i in range(3):
            s = await _create_session(client, ws["id"], f"s{i}")
            await _set_phase(s["id"], "running")

        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_capacity_summary
            summary = await _get_capacity_summary(ws["id"], db)

        assert summary["slots_available"] == 0  # clamped, not negative

    @pytest.mark.asyncio
    async def test_unlimited_mode(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 0

        ws = await _create_workspace(client)
        async with _TestSession() as db:
            from swarmer.routers.sessions import _get_capacity_summary
            summary = await _get_capacity_summary(ws["id"], db)

        assert summary["max"] == 0
        assert summary["slots_available"] is None  # no limit


# ===========================================================================
# 5. Queue gate in _do_launch(): all modes queue when at capacity
# ===========================================================================


class TestDoLaunchQueueGate:
    @pytest.mark.asyncio
    async def test_prompt_queues_at_capacity(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 2

        ws = await _create_workspace(client)
        # Fill capacity: 2 running sessions
        for i in range(2):
            s = await _create_session(client, ws["id"], f"running-{i}")
            await _set_phase(s["id"], "running")

        # Launch a third — should queue without hitting K8s
        new_s = await _create_session(client, ws["id"], "newcomer", mode="prompt")
        # Setting phase to idle so it's launchable
        await _set_phase(new_s["id"], "idle")

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=2)):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{new_s['id']}/launch"
            )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "queued"

    @pytest.mark.asyncio
    async def test_server_mode_queues_at_capacity(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 1

        ws = await _create_workspace(client)
        s_running = await _create_session(client, ws["id"], "active", mode="server")
        await _set_phase(s_running["id"], "running")

        s_new = await _create_session(client, ws["id"], "waiting", mode="server")

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=1)):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s_new['id']}/launch"
            )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "queued"

    @pytest.mark.asyncio
    async def test_tui_mode_queues_at_capacity(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 1

        ws = await _create_workspace(client)
        s_running = await _create_session(client, ws["id"], "active", mode="tui")
        await _set_phase(s_running["id"], "running")

        s_new = await _create_session(client, ws["id"], "waiting", mode="tui")

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=1)):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s_new['id']}/launch"
            )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "queued"

    @pytest.mark.asyncio
    async def test_unlimited_skips_queue(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 0  # disabled

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        # Even with many running, limit=0 means no queuing
        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=999)):
            # This will try to do real K8s — patch _do_launch instead
            with patch("swarmer.routers.sessions._do_launch", new=AsyncMock()) as mock_launch:
                await client.post(
                    f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
                )
        # _do_launch was called (not short-circuited to queued)
        mock_launch.assert_called_once()

    @pytest.mark.asyncio
    async def test_queued_status_detail_contains_counts(self, client):
        from swarmer.config import settings
        settings.max_concurrent_agents = 3

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=3)):
            resp = await client.post(
                f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/launch"
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["phase"] == "queued"
        assert "3" in (data.get("status_detail") or "")


# ===========================================================================
# 6. Stop for queued sessions
# ===========================================================================


class TestStopQueuedSession:
    @pytest.mark.asyncio
    async def test_stop_queued_returns_idle(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")
        from swarmer.models.session import Session
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            session.queued_instruction_prompt = "run only"
            await db.commit()

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
        )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "idle"
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            assert session.queued_instruction_prompt is None

    @pytest.mark.asyncio
    async def test_stop_queued_clears_status_detail(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")
        from swarmer.models.session import Session as SessionModel
        async with _TestSession() as db:
            session = await db.get(SessionModel, s["id"])
            session.queued_instruction_prompt = "pending run"
            await db.commit()
        async with _TestSession() as db:
            await db.execute(
                text("UPDATE sessions SET status_detail='Waiting for capacity' WHERE id=:id"),
                {"id": s["id"]},
            )
            await db.commit()

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
        )
        assert resp.status_code == 200
        assert resp.json().get("status_detail", "") == ""
        async with _TestSession() as db:
            session = await db.get(SessionModel, s["id"])
            assert session.queued_instruction_prompt is None

    @pytest.mark.asyncio
    async def test_stop_queued_does_not_set_stopped(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")
        from swarmer.models.session import Session as SessionModel
        async with _TestSession() as db:
            session = await db.get(SessionModel, s["id"])
            session.queued_instruction_prompt = "discard on failure"
            await db.commit()

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
        )
        assert resp.json()["phase"] != "stopped"
        async with _TestSession() as db:
            session = await db.get(SessionModel, s["id"])
            assert session.queued_instruction_prompt is None

    @pytest.mark.asyncio
    async def test_stop_queued_no_pod_name_in_response(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
        )
        assert resp.json().get("pod_name") is None


# ===========================================================================
# 7. Scheduler: _process_queue()
# ===========================================================================


class TestProcessQueue:
    @pytest.mark.asyncio
    async def test_cooldown_when_at_capacity(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 2

        ws = await _create_workspace(client)
        # 2 running = at capacity
        for i in range(2):
            s = await _create_session(client, ws["id"], f"r{i}")
            await _set_phase(s["id"], "running")

        async with _TestSession() as db:
            with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=2)):
                await sched._process_queue(db)

        assert sched._queue_next_check is not None
        # cooldown is approximately 2 minutes from now
        diff = (sched._queue_next_check - datetime.now(timezone.utc)).total_seconds()
        assert 100 < diff <= 125

    @pytest.mark.asyncio
    async def test_launches_queued_sessions_fifo(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 3

        ws = await _create_workspace(client)
        now = datetime.now(timezone.utc)
        s1 = await _create_session(client, ws["id"], "first")
        s2 = await _create_session(client, ws["id"], "second")
        await _set_phase(s1["id"], "queued", created_at=now - timedelta(minutes=10))
        await _set_phase(s2["id"], "queued", created_at=now)

        launched = []

        async def mock_launch(session, ws, db, **kw):
            launched.append(session.id)
            session.phase = "pending"

        async with _TestSession() as db:
            with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=0)):
                with patch("swarmer.routers.sessions._do_launch", new=mock_launch):
                    await sched._process_queue(db)

        # Both launched, s1 (older) first
        assert launched[0] == s1["id"]
        assert launched[1] == s2["id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("owner_at_dequeue,visible", [("test-user", True), ("other-user", False)])
    async def test_manual_queued_launch_revalidates_private_mcp_visibility(
        self, client, monkeypatch, owner_at_dequeue, visible
    ):
        import swarmer.scheduler as sched
        from swarmer.config import settings
        from swarmer.models.mcp_server import McpServer
        from swarmer.models.session import Session
        from swarmer.models.workspace import Workspace
        from swarmer.routers.sessions import _do_launch

        sched._queue_next_check = None
        settings.max_concurrent_agents = 1
        ws_data = await _create_workspace(client, "Private MCP Queue")
        session_data = await _create_session(client, ws_data["id"], "private-mcp-queued")
        async with _TestSession() as db:
            session = await db.get(Session, session_data["id"])
            server = McpServer(
                workspace_id=ws_data["id"],
                user_id="test-user",
                shared=False,
                slug="private-queue-test",
                display_name="Private queue test",
                server_url="https://mcp.example.com",
                server_type="http",
                enabled=True,
                jira_access_token_enc="encrypted-token",
            )
            db.add(server)
            await db.flush()
            session.enabled_mcp_ids = [server.id]
            await db.commit()
            server_id = server.id

        count_running = AsyncMock(side_effect=[1, 0, 0])
        monkeypatch.setattr("swarmer.routers.sessions._count_running_sessions", count_running)
        async with _TestSession() as db:
            session = await db.get(Session, session_data["id"], options=[selectinload(Session.repos)])
            workspace = await db.get(Workspace, ws_data["id"])
            await _do_launch(session, workspace, db, user_id="test-user")
            assert session.phase == "queued"
            assert session.queued_user_id == "test-user"

        async with _TestSession() as db:
            server = await db.get(McpServer, server_id)
            server.user_id = owner_at_dequeue
            await db.commit()

        captured = {}

        async def capture_launch(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("swarmer.openshell_client.provider_exists", AsyncMock(return_value=False))
        monkeypatch.setattr("swarmer.routers.sessions._get_prompt_sources", AsyncMock(return_value=[]))
        monkeypatch.setattr("swarmer.routers.sessions._do_launch_openshell", capture_launch)

        async with _TestSession() as db:
            await sched._process_queue(db)

        selected = captured.get("mcp_servers") or []
        assert (server_id in [server.id for server in selected]) is visible
        async with _TestSession() as db:
            session = await db.get(Session, session_data["id"])
            assert session.queued_user_id == ""

    @pytest.mark.asyncio
    async def test_limits_launches_to_available_slots(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 3

        ws = await _create_workspace(client)
        now = datetime.now(timezone.utc)
        sessions = []
        for i in range(3):
            s = await _create_session(client, ws["id"], f"q{i}")
            await _set_phase(s["id"], "queued", created_at=now + timedelta(seconds=i))
            sessions.append(s)

        launched = []

        async def mock_launch(session, ws, db, **kw):
            launched.append(session.id)
            session.phase = "pending"

        async with _TestSession() as db:
            # Only 1 slot available (2 running)
            with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=2)):
                with patch("swarmer.routers.sessions._do_launch", new=mock_launch):
                    await sched._process_queue(db)

        assert len(launched) == 1  # only 1 slot was available

    @pytest.mark.asyncio
    async def test_skips_cooldown_when_past(self, client):
        import swarmer.scheduler as sched

        # Cooldown already expired
        sched._queue_next_check = datetime.now(timezone.utc) - timedelta(seconds=1)
        from swarmer.config import settings
        settings.max_concurrent_agents = 5

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")

        launched = []

        async def mock_launch(session, ws, db, **kw):
            launched.append(session.id)
            session.phase = "pending"

        async with _TestSession() as db:
            with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=0)):
                with patch("swarmer.routers.sessions._do_launch", new=mock_launch):
                    await sched._process_queue(db)

        assert len(launched) == 1

    @pytest.mark.asyncio
    async def test_no_op_when_unlimited(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 0  # unlimited — nothing should be queued

        async with _TestSession() as db:
            with patch("swarmer.routers.sessions._do_launch") as mock_launch:
                await sched._process_queue(db)
        mock_launch.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_launch_resets_to_idle(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 5

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")
        from swarmer.models.session import Session as SessionModel
        async with _TestSession() as db:
            session = await db.get(SessionModel, s["id"])
            session.queued_instruction_prompt = "discard on failure"
            await db.commit()

        async def failing_launch(session, ws, db, **kw):
            raise RuntimeError("K8s unavailable")

        async with _TestSession() as db:
            with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=0)):
                with patch("swarmer.routers.sessions._do_launch", new=failing_launch):
                    await sched._process_queue(db)

        # Session should be idle (not stuck in queued) after launch failure
        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            result = await db.execute(select(Session).where(Session.id == s["id"]))
            sess = result.scalar_one()
        assert sess.phase == "idle"
        assert sess.queued_instruction_prompt is None


# ===========================================================================
# 8. Scheduler: cron claiming respects capacity
# ===========================================================================


class TestCronCapacityCheck:
    @pytest.mark.asyncio
    async def test_cron_does_not_claim_when_at_capacity(self, client):
        import swarmer.scheduler as sched

        from swarmer.config import settings
        settings.max_concurrent_agents = 2

        ws = await _create_workspace(client)
        # 2 running = at capacity
        for i in range(2):
            s = await _create_session(client, ws["id"], f"running-{i}")
            await _set_phase(s["id"], "running")

        # Create a cron-due session
        cron_s = await _create_session(client, ws["id"], "cron-session")
        async with _TestSession() as db:
            await db.execute(
                text(
                    "UPDATE sessions SET cron_schedule='*/30 * * * *', "
                    "cron_next_run=datetime('now','-1 minute'), phase='idle' "
                    "WHERE id=:id"
                ),
                {"id": cron_s["id"]},
            )
            await db.commit()

        with patch("swarmer.routers.sessions._count_running_sessions", new=AsyncMock(return_value=2)):
            async with _TestSession() as db:
                await sched._check_and_launch(db)

        # Cron session should still be idle (not claimed as pending)
        async with _TestSession() as db:
            from sqlalchemy import select
            from swarmer.models.session import Session
            result = await db.execute(select(Session).where(Session.id == cron_s["id"]))
            sess = result.scalar_one()
        assert sess.phase == "idle"


# ===========================================================================
# Cron scheduler — mode coercion (ACM-35280)
# Scheduled runs always execute in prompt mode regardless of the session's
# configured mode. The scheduler sets session.mode = "prompt" before _do_launch.
# ===========================================================================


class TestCronModeCoercion:
    @pytest.mark.asyncio
    async def test_cron_sets_mode_to_prompt_for_tui_session(self, client):
        """Scheduler coerces a TUI-mode session to prompt before launching."""
        import swarmer.scheduler as sched

        ws = await _create_workspace(client)
        cron_s = await _create_session(client, ws["id"], "tui-cron-session")

        # Set mode to TUI and insert an overdue session_schedules entry.
        async with _TestSession() as db:
            await db.execute(
                text(
                    "UPDATE sessions SET mode='tui', cron_schedule='*/30 * * * *', "
                    "cron_next_run=datetime('now','-1 minute'), phase='idle' "
                    "WHERE id=:id"
                ),
                {"id": cron_s["id"]},
            )
            await db.execute(
                text(
                    "INSERT INTO session_schedules "
                    "(session_id, cron_schedule, cron_next_run, label, enabled) "
                    "VALUES (:sid, '*/30 * * * *', datetime('now','-1 minute'), '', 1)"
                ),
                {"sid": cron_s["id"]},
            )
            await db.commit()

        launched = []

        async def _fake_do_launch(session, ws, db):
            launched.append(session.mode)

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            async with _TestSession() as db:
                await sched._check_and_launch(db)

        assert launched == ["prompt"], (
            f"Expected scheduler to coerce mode to 'prompt', got {launched}"
        )

    @pytest.mark.asyncio
    async def test_cron_sets_mode_to_prompt_for_server_session(self, client):
        """Scheduler coerces a server-mode session to prompt before launching."""
        import swarmer.scheduler as sched

        ws = await _create_workspace(client)
        cron_s = await _create_session(client, ws["id"], "server-cron-session")

        async with _TestSession() as db:
            await db.execute(
                text(
                    "UPDATE sessions SET mode='server', cron_schedule='*/30 * * * *', "
                    "cron_next_run=datetime('now','-1 minute'), phase='idle' "
                    "WHERE id=:id"
                ),
                {"id": cron_s["id"]},
            )
            await db.execute(
                text(
                    "INSERT INTO session_schedules "
                    "(session_id, cron_schedule, cron_next_run, label, enabled) "
                    "VALUES (:sid, '*/30 * * * *', datetime('now','-1 minute'), '', 1)"
                ),
                {"sid": cron_s["id"]},
            )
            await db.commit()

        launched = []

        async def _fake_do_launch(session, ws, db):
            launched.append(session.mode)

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            async with _TestSession() as db:
                await sched._check_and_launch(db)

        assert launched == ["prompt"], (
            f"Expected scheduler to coerce mode to 'prompt', got {launched}"
        )

    @pytest.mark.asyncio
    async def test_cron_claims_non_prompt_sessions(self, client):
        """Scheduler now claims sessions of any mode (not just prompt-mode)."""
        import swarmer.scheduler as sched

        ws = await _create_workspace(client)
        cron_s = await _create_session(client, ws["id"], "tui-cron-any-mode")

        async with _TestSession() as db:
            await db.execute(
                text(
                    "UPDATE sessions SET mode='tui', cron_schedule='*/30 * * * *', "
                    "cron_next_run=datetime('now','-1 minute'), phase='idle' "
                    "WHERE id=:id"
                ),
                {"id": cron_s["id"]},
            )
            await db.execute(
                text(
                    "INSERT INTO session_schedules "
                    "(session_id, cron_schedule, cron_next_run, label, enabled) "
                    "VALUES (:sid, '*/30 * * * *', datetime('now','-1 minute'), '', 1)"
                ),
                {"sid": cron_s["id"]},
            )
            await db.commit()

        launched = []

        async def _fake_do_launch(session, ws, db):
            launched.append(session.id)

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            async with _TestSession() as db:
                await sched._check_and_launch(db)

        assert cron_s["id"] in launched, (
            "Expected TUI-mode session to be claimed and launched by cron scheduler"
        )


# ===========================================================================
# List-page launch — mode coercion
# The list-page "Launch" button sends no mode or save_config, so the endpoint
# must default to prompt mode regardless of the session's configured mode.
# ===========================================================================


class TestListPageLaunchModeCoercion:
    """The list-page Launch button sends no mode/save_config, so the endpoint
    must default to prompt mode regardless of the session's configured mode."""

    @pytest_asyncio.fixture(autouse=True)
    async def _patch_auth(self):
        """Override require_auth so the HTML launch endpoint is accessible."""
        from swarmer.deps import require_auth
        from swarmer.main import app

        app.dependency_overrides[require_auth] = lambda: None
        yield
        app.dependency_overrides.pop(require_auth, None)

    @pytest.mark.asyncio
    async def test_list_launch_coerces_tui_to_prompt(self, client):
        """List-page launch (no save_config) coerces a TUI-mode session to prompt."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], "tui-list-launch", mode="tui")

        launched_modes = []

        async def _fake_do_launch(session, workspace, db, user_id=""):
            launched_modes.append(session.mode)

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={"redirect_to": "list"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303)
        assert launched_modes == ["prompt"], (
            f"Expected list-page launch to coerce mode to 'prompt', got {launched_modes}"
        )

    @pytest.mark.asyncio
    async def test_list_launch_coerces_server_to_prompt(self, client):
        """List-page launch (no save_config) coerces a server-mode session to prompt."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], "server-list-launch", mode="server")

        launched_modes = []

        async def _fake_do_launch(session, workspace, db, user_id=""):
            launched_modes.append(session.mode)

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={"redirect_to": "list"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303)
        assert launched_modes == ["prompt"], (
            f"Expected list-page launch to coerce mode to 'prompt', got {launched_modes}"
        )

    @pytest.mark.asyncio
    async def test_detail_launch_preserves_explicit_mode(self, client):
        """Detail-page launch (with save_config + mode) preserves the chosen mode."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], "tui-detail-launch", mode="prompt")

        launched_modes = []

        async def _fake_do_launch(session, workspace, db, user_id=""):
            launched_modes.append(session.mode)

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={"save_config": "1", "mode": "tui", "redirect_to": "detail"},
                follow_redirects=False,
            )

        assert resp.status_code in (302, 303)
        assert launched_modes == ["tui"], (
            f"Expected detail-page launch to preserve 'tui' mode, got {launched_modes}"
        )

    @pytest.mark.asyncio
    async def test_ui_launch_clears_stale_event_context(self, client):
        """ACM-42674 regression: manually clicking Launch in the UI after a
        prior event-triggered run must clear the stale session.event_context,
        otherwise Run History mislabels the new manual run as event-driven."""
        import json

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], "manual-after-event-launch", mode="prompt")

        from swarmer.models.session import Session

        async with _TestSession() as db:
            sess = await db.get(Session, s["id"])
            sess.event_context = json.dumps({"pr_number": 104, "event_condition": "ci_fail_or_conflict"})
            await db.commit()

        async def _fake_do_launch(session, workspace, db, user_id=""):
            pass

        with patch("swarmer.routers.sessions._do_launch", new=_fake_do_launch):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={"redirect_to": "list"},
                follow_redirects=False,
            )
        assert resp.status_code in (302, 303)

        async with _TestSession() as db:
            sess = await db.get(Session, s["id"])
            assert sess.event_context == ""

    @pytest.mark.asyncio
    async def test_confirmed_launch_remembers_settings_but_not_instructions(self, client):
        from swarmer.models.session import Session
        from swarmer.models.workspace_prompt import (
            WorkspacePrompt,
            WorkspacePromptSource,
        )

        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], "remember-settings")

        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            session.instruction_prompt = "scheduled/session default"
            source = WorkspacePromptSource(
                workspace_id=ws["id"], name="shared prompts", repo_url="https://example.com/prompts"
            )
            db.add(source)
            await db.flush()
            prompt = WorkspacePrompt(
                source_id=source.id, filename="task.md", display_name="Task",
                content="base content", content_hash="hash",
            )
            db.add(prompt)
            await db.flush()
            prompt_id = prompt.id
            await db.commit()

        captured = {}

        async def _fake_launch(session, workspace, db, user_id="", manual_instruction_prompt=None):
            captured["mode"] = session.mode
            captured["instruction"] = manual_instruction_prompt

        available = [{"type": "preset", "value": "claude", "available": True}]
        with (
            patch("swarmer.routers.sessions._get_provider_options", new=AsyncMock(return_value=available)),
            patch("swarmer.routers.sessions._do_launch", new=_fake_launch),
        ):
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={
                    "launch_confirmed": "1",
                    "mode": "tui",
                    "provider": "claude",
                    "prompt_id": str(prompt_id),
                    "instruction_prompt": "this run only",
                },
                follow_redirects=False,
            )
        assert resp.status_code in (302, 303)
        assert captured == {"mode": "tui", "instruction": "this run only"}
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            assert session.mode == "tui"
            assert session.provider == "claude"
            assert session.prompt_id == prompt_id
            assert session.instruction_prompt == "scheduled/session default"

    @pytest.mark.asyncio
    async def test_confirmed_launch_rejects_foreign_workspace_prompt(self, client):
        from swarmer.models.session import Session
        from swarmer.models.workspace_prompt import (
            WorkspacePrompt,
            WorkspacePromptSource,
        )

        ws = await _create_workspace(client, "Prompt Owner")
        other_ws = await _create_workspace(client, "Other Prompt Owner")
        s = await _create_session(client, ws["id"], "foreign-prompt")
        async with _TestSession() as db:
            source = WorkspacePromptSource(
                workspace_id=other_ws["id"], name="foreign", repo_url="https://example.com/repo"
            )
            db.add(source)
            await db.flush()
            prompt = WorkspacePrompt(
                source_id=source.id, filename="prompt.md", display_name="Foreign",
                content="private", content_hash="hash",
            )
            db.add(prompt)
            await db.commit()
            foreign_prompt_id = prompt.id

        with patch("swarmer.routers.sessions._do_launch", new=AsyncMock()) as launch:
            resp = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={
                    "launch_confirmed": "1", "mode": "prompt", "provider": "",
                    "prompt_id": str(foreign_prompt_id), "instruction_prompt": "run text",
                },
                follow_redirects=False,
            )
        assert resp.status_code in (302, 303)
        assert resp.headers["location"] == f"/workspaces/{ws['id']}/sessions/{s['id']}"
        launch.assert_not_awaited()
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            assert session.prompt_id is None

    @pytest.mark.asyncio
    async def test_launch_dialog_renders_blank_run_instructions(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"], "dialog")
        with patch("swarmer.routers.sessions._get_provider_options", new=AsyncMock(return_value=[])):
            response = await client.get(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch-dialog?mode=server"
            )
        assert response.status_code == 200
        assert "Launch mode" in response.text
        assert "Prompt preview" in response.text
        assert "Additional Instructions" in response.text
        assert "this manual run only" in response.text
        assert 'id="launch-instruction-prompt"' in response.text

    @pytest.mark.asyncio
    async def test_confirmed_launch_rejects_unavailable_provider(self, client):
        from swarmer.models.session import Session

        ws = await _create_workspace(client, "Unavailable Provider")
        s = await _create_session(client, ws["id"], "unavailable-provider")
        with (
            patch(
                "swarmer.routers.sessions._get_provider_options",
                new=AsyncMock(return_value=[{"type": "preset", "value": "claude", "available": False}]),
            ),
            patch("swarmer.routers.sessions._do_launch", new=AsyncMock()) as launch,
        ):
            response = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={
                    "launch_confirmed": "1", "mode": "prompt", "provider": "claude",
                    "prompt_id": "", "instruction_prompt": "run text",
                },
                follow_redirects=False,
            )
        assert response.status_code in (302, 303)
        assert response.headers["location"] == f"/workspaces/{ws['id']}/sessions/{s['id']}"
        launch.assert_not_awaited()
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            assert session.provider == ""

    @pytest.mark.asyncio
    async def test_confirmed_shell_launch_ignores_stale_provider(self, client):
        from swarmer.models.session import Session

        ws = await _create_workspace(client, "Shell with stale provider")
        s = await _create_session(client, ws["id"], "shell-stale-provider")
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            session.agent_tool = "shell"
            session.provider = "claude"
            await db.commit()

        with (
            patch(
                "swarmer.routers.sessions._get_provider_options",
                new=AsyncMock(),
            ) as provider_options,
            patch("swarmer.routers.sessions._do_launch", new=AsyncMock()) as launch,
        ):
            response = await client.post(
                f"/workspaces/{ws['id']}/sessions/{s['id']}/launch",
                data={
                    "launch_confirmed": "1", "mode": "prompt", "provider": "claude",
                    "prompt_id": "", "instruction_prompt": "run text",
                },
                follow_redirects=False,
            )

        assert response.status_code in (302, 303)
        provider_options.assert_not_awaited()
        launch.assert_awaited_once()
        async with _TestSession() as db:
            session = await db.get(Session, s["id"])
            assert session.provider == ""


@pytest.mark.asyncio
async def test_manual_prompt_override_blank_does_not_inherit_session_instructions(client, monkeypatch):
    from swarmer.config import settings
    from swarmer.models.session import Session
    from swarmer.models.workspace import Workspace
    from swarmer.routers.sessions import _do_launch

    settings.max_concurrent_agents = 2
    ws_data = await _create_workspace(client)
    session_data = await _create_session(client, ws_data["id"], "queued-run")
    captured = {}

    async def _capture_launch(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("swarmer.routers.sessions._count_running_sessions", AsyncMock(return_value=0))
    monkeypatch.setattr("swarmer.openshell_client.provider_exists", AsyncMock(return_value=False))
    monkeypatch.setattr("swarmer.routers.mcp_servers.get_enabled_mcp_servers", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarmer.routers.sessions._get_prompt_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr("swarmer.routers.sessions._do_launch_openshell", _capture_launch)

    async with _TestSession() as db:
        session = await db.get(Session, session_data["id"])
        session.instruction_prompt = "stored session default"
        session.queued_instruction_prompt = ""
        await db.commit()

    async with _TestSession() as db:
        session = await db.get(Session, session_data["id"], options=[selectinload(Session.repos)])
        workspace = await db.get(Workspace, ws_data["id"])
        assert session.queued_instruction_prompt == ""
        await _do_launch(session, workspace, db, manual_instruction_prompt="")
        assert captured["resolved_prompt"] == ""
        assert session.queued_instruction_prompt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("instructions", ["", "manual queued text"])
async def test_queued_manual_instruction_is_durable_and_preserves_blank(client, monkeypatch, instructions):
    from swarmer.config import settings
    from swarmer.models.session import Session
    from swarmer.models.workspace import Workspace
    from swarmer.routers.sessions import _do_launch

    settings.max_concurrent_agents = 1
    ws_data = await _create_workspace(client)
    session_data = await _create_session(client, ws_data["id"], "queued-manual")
    monkeypatch.setattr("swarmer.routers.sessions._count_running_sessions", AsyncMock(return_value=1))
    async with _TestSession() as db:
        session = await db.get(Session, session_data["id"], options=[selectinload(Session.repos)])
        workspace = await db.get(Workspace, ws_data["id"])
        await _do_launch(session, workspace, db, manual_instruction_prompt=instructions)
        assert session.phase == "queued"

    async with _TestSession() as db:
        session = await db.get(Session, session_data["id"])
        assert session.queued_instruction_prompt == instructions


@pytest.mark.asyncio
async def test_manual_and_scheduled_prompt_composition_are_separate(client):
    from swarmer.models.session import Session
    from swarmer.models.session_schedule import SessionSchedule
    from swarmer.models.workspace_prompt import (
        WorkspacePrompt,
        WorkspacePromptSource,
    )
    from swarmer.routers.sessions import (
        _resolve_manual_prompt,
        _resolve_schedule_prompt,
    )

    ws = await _create_workspace(client, "Composition")
    s = await _create_session(client, ws["id"], "composition")
    async with _TestSession() as db:
        session = await db.get(Session, s["id"])
        source = WorkspacePromptSource(workspace_id=ws["id"], name="prompts", repo_url="https://example.com/repo")
        db.add(source)
        await db.flush()
        prompt = WorkspacePrompt(
            source_id=source.id, filename="base.md", display_name="Base",
            content="base prompt", content_hash="hash",
        )
        db.add(prompt)
        await db.flush()
        session.prompt_id = prompt.id
        session.instruction_prompt = "session default"
        schedule = SessionSchedule(
            session_id=session.id, prompt_id=None, cron_schedule="0 0 * * *",
            label="nightly", instruction_prompt="scheduled instructions", enabled=True,
        )
        db.add(schedule)
        await db.flush()
        assert await _resolve_manual_prompt(session, db, "") == "base prompt"
        assert await _resolve_manual_prompt(session, db, "manual run") == "manual run\n\nbase prompt"
        assert await _resolve_schedule_prompt(schedule.id, session, db) == "scheduled instructions\n\nbase prompt"


# ===========================================================================
# WAL mode — database.py must enable WAL so the scheduler can write
# concurrently while a route handler holds an open read transaction.
# ===========================================================================


class TestSQLiteWALMode:
    """Verify that init_db() enables WAL journal mode on SQLite engines."""

    @pytest.mark.asyncio
    async def test_wal_mode_and_busy_timeout_enabled_by_init_db(self, tmp_path):
        import swarmer.database as db_module

        db_path = tmp_path / "test_wal.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"

        orig_engine = db_module._engine
        orig_session = db_module._AsyncSessionLocal

        try:
            db_module.init_db(db_url)
            async with db_module._engine.connect() as conn:
                mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
                timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
            assert mode == "wal", f"Expected WAL journal mode, got: {mode!r}"
            assert int(timeout) >= 5000, f"Expected busy_timeout >= 5000ms, got: {timeout}"
        finally:
            await db_module._engine.dispose()
            db_module._engine = orig_engine
            db_module._AsyncSessionLocal = orig_session

    @pytest.mark.asyncio
    async def test_in_memory_db_skips_wal(self):
        """In-memory SQLite doesn't persist so WAL isn't required; guard is present."""
        import swarmer.database as db_module

        orig_engine = db_module._engine
        orig_session = db_module._AsyncSessionLocal

        try:
            # In-memory URL still starts with "sqlite" so WAL is attempted;
            # in-memory SQLite silently stays in "memory" mode but must not raise.
            db_module.init_db("sqlite+aiosqlite://")
            async with db_module._engine.connect() as conn:
                result = await conn.execute(text("PRAGMA journal_mode"))
                mode = result.scalar()
            # In-memory SQLite ignores WAL and stays in "memory" mode — that's fine.
            assert mode in ("wal", "memory")
        finally:
            await db_module._engine.dispose()
            db_module._engine = orig_engine
            db_module._AsyncSessionLocal = orig_session

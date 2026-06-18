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
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
                {"ph": phase, "ca": created_at, "id": session_id},
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
        now = datetime.utcnow()
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
        now = datetime.utcnow()
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
        now = datetime.utcnow()
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

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
        )
        assert resp.status_code == 200
        assert resp.json()["phase"] == "idle"

    @pytest.mark.asyncio
    async def test_stop_queued_clears_status_detail(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")
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

    @pytest.mark.asyncio
    async def test_stop_queued_does_not_set_stopped(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        await _set_phase(s["id"], "queued")

        resp = await client.post(
            f"/api/v1/workspaces/{ws['id']}/sessions/{s['id']}/stop"
        )
        assert resp.json()["phase"] != "stopped"

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
        diff = (sched._queue_next_check - datetime.utcnow()).total_seconds()
        assert 100 < diff <= 125

    @pytest.mark.asyncio
    async def test_launches_queued_sessions_fifo(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 3

        ws = await _create_workspace(client)
        now = datetime.utcnow()
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
    async def test_limits_launches_to_available_slots(self, client):
        import swarmer.scheduler as sched

        sched._queue_next_check = None
        from swarmer.config import settings
        settings.max_concurrent_agents = 3

        ws = await _create_workspace(client)
        now = datetime.utcnow()
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
        sched._queue_next_check = datetime.utcnow() - timedelta(seconds=1)
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

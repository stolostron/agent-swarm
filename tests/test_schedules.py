"""Tests for multi-schedule support (ACM-35377).

Covers:
  - SessionSchedule model CRUD and cascade delete
  - REST API /schedules sub-resource (GET / POST / PUT / DELETE)
  - Backward-compat /schedule and /unschedule delegation
  - Scheduler multi-schedule queuing: only the earliest-due schedule fires per poll
  - MCP server schedule tool round-trip
"""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
import respx
import httpx
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

# ---------------------------------------------------------------------------
# Shared in-memory DB fixtures (mirrored from test_api.py)
# ---------------------------------------------------------------------------

from sqlalchemy import event as _sa_event

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)


@_sa_event.listens_for(_engine.sync_engine, "connect")
def _set_fk_pragma(dbapi_conn, _):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


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
    settings.k8s_namespace = ""  # must be empty to allow workspace creation

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
# Helpers
# ---------------------------------------------------------------------------


async def _create_workspace(client: AsyncClient, name: str = "Test WS") -> dict:
    resp = await client.post(
        "/api/v1/workspaces",
        json={"display_name": name, "description": ""},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _create_session(client: AsyncClient, ws_id: int, name: str = "s1") -> dict:
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/sessions",
        json={"name": name, "mode": "prompt", "agent_tool": "opencode"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ===========================================================================
# Model-level tests (direct DB access via ORM)
# ===========================================================================


class TestSessionScheduleModel:
    @pytest.mark.asyncio
    async def test_create_and_retrieve(self):
        from swarmer.models.session import Session
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace

        async with _TestSession() as db:
            ws = Workspace(display_name="WS", namespace="ws", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(workspace_id=ws.id, name="sess", mode="prompt",
                              model="", agent_tool="opencode", instruction_prompt="")
            db.add(session)
            await db.commit()
            await db.refresh(session)

            sched = SessionSchedule(
                session_id=session.id,
                cron_schedule="0 * * * *",
                label="hourly",
                enabled=True,
            )
            db.add(sched)
            await db.commit()
            await db.refresh(sched)

            assert sched.id is not None
            assert sched.session_id == session.id
            assert sched.cron_schedule == "0 * * * *"
            assert sched.label == "hourly"
            assert sched.enabled is True

    @pytest.mark.asyncio
    async def test_cascade_delete(self):
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from swarmer.models.session import Session
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace

        session_id = None
        sched_id = None

        async with _TestSession() as db:
            ws = Workspace(display_name="WS2", namespace="ws2", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(workspace_id=ws.id, name="s2", mode="prompt",
                              model="", agent_tool="opencode", instruction_prompt="")
            db.add(session)
            await db.commit()
            await db.refresh(session)
            session_id = session.id

            sched = SessionSchedule(
                session_id=session.id,
                cron_schedule="0 0 * * *",
            )
            db.add(sched)
            await db.commit()
            sched_id = sched.id

        # Re-open the session in a new context, load with schedules, then delete.
        async with _TestSession() as db:
            result = await db.execute(
                select(Session)
                .where(Session.id == session_id)
                .options(selectinload(Session.schedules))
            )
            sess = result.scalars().first()
            await db.delete(sess)
            await db.commit()

        # Verify cascade: the schedule should be gone.
        async with _TestSession() as db:
            result = await db.get(SessionSchedule, sched_id)
            assert result is None

    @pytest.mark.asyncio
    async def test_earliest_next_run(self):
        from swarmer.models.session import Session
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace

        async with _TestSession() as db:
            ws = Workspace(display_name="WS3", namespace="ws3", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(workspace_id=ws.id, name="s3", mode="prompt",
                              model="", agent_tool="opencode", instruction_prompt="")
            db.add(session)
            await db.commit()
            await db.refresh(session)

            now = datetime.now(timezone.utc)
            soon = now + timedelta(hours=1)
            later = now + timedelta(hours=5)

            s1 = SessionSchedule(session_id=session.id, cron_schedule="0 * * * *",
                                 cron_next_run=later, enabled=True)
            s2 = SessionSchedule(session_id=session.id, cron_schedule="0 */6 * * *",
                                 cron_next_run=soon, enabled=True)
            db.add_all([s1, s2])
            await db.commit()

            await db.refresh(session)
            assert session.earliest_next_run == soon


# ===========================================================================
# API tests: /schedules sub-resource
# ===========================================================================


class TestScheduleAPI:
    @pytest.mark.asyncio
    async def test_create_and_list(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        ws_id, sid = ws["id"], s["id"]

        # Create a schedule
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules",
            json={"cron_schedule": "0 * * * *", "label": "hourly"},
        )
        assert resp.status_code == 201, resp.text
        sched = resp.json()
        assert sched["cron_schedule"] == "0 * * * *"
        assert sched["label"] == "hourly"
        assert sched["enabled"] is True
        assert sched["cron_next_run"] is not None

        # List schedules
        resp = await client.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules")
        assert resp.status_code == 200
        schedules = resp.json()
        assert len(schedules) == 1
        assert schedules[0]["id"] == sched["id"]

    @pytest.mark.asyncio
    async def test_update_schedule(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        ws_id, sid = ws["id"], s["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules",
            json={"cron_schedule": "0 * * * *"},
        )
        sched_id = resp.json()["id"]

        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}",
            json={"cron_schedule": "0 0 * * *", "label": "daily", "enabled": False},
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["cron_schedule"] == "0 0 * * *"
        assert updated["label"] == "daily"
        assert updated["enabled"] is False

    @pytest.mark.asyncio
    async def test_delete_schedule(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        ws_id, sid = ws["id"], s["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules",
            json={"cron_schedule": "*/30 * * * *"},
        )
        sched_id = resp.json()["id"]

        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}"
        )
        assert resp.status_code == 204

        resp = await client.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules")
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_invalid_cron_rejected(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        ws_id, sid = ws["id"], s["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules",
            json={"cron_schedule": "not a cron"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_session_out_includes_schedules(self, client):
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        ws_id, sid = ws["id"], s["id"]

        await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules",
            json={"cron_schedule": "0 * * * *", "label": "hourly"},
        )

        resp = await client.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")
        assert resp.status_code == 200
        session_data = resp.json()
        assert "schedules" in session_data
        assert len(session_data["schedules"]) == 1
        assert session_data["schedules"][0]["label"] == "hourly"

    @pytest.mark.asyncio
    async def test_backward_compat_schedule_endpoint(self, client):
        """POST /schedule creates a SessionSchedule entry."""
        ws = await _create_workspace(client)
        s = await _create_session(client, ws["id"])
        ws_id, sid = ws["id"], s["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedule",
            json={"cron_expr": "0 9 * * 1-5"},
        )
        assert resp.status_code == 200

        # The new schedule sub-resource should have an entry.
        resp = await client.get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules")
        assert len(resp.json()) == 1
        assert resp.json()[0]["cron_schedule"] == "0 9 * * 1-5"


# ===========================================================================
# Scheduler tests: multi-schedule queuing
# ===========================================================================


class TestSchedulerMultiSchedule:
    @pytest.mark.asyncio
    async def test_only_one_due_schedule_fires_per_poll(self):
        """Two due schedules for the same session: only the earliest fires per poll."""
        from sqlalchemy import select
        from swarmer.models.session import Session
        from swarmer.models.session_schedule import SessionSchedule
        from swarmer.models.workspace import Workspace
        from swarmer.scheduler import _check_and_launch

        launched = []

        async def _fake_launch(session, ws, db):
            launched.append((session.id, session.active_schedule_id))

        import swarmer.scheduler as _sched_mod
        import swarmer.routers.sessions as _sess_mod
        original_do_launch = getattr(_sess_mod, "_do_launch", None)

        async with _TestSession() as db:
            ws = Workspace(display_name="SchedWS", namespace="schedws", description="")
            db.add(ws)
            await db.commit()
            await db.refresh(ws)

            session = Session(workspace_id=ws.id, name="multi-sched", mode="prompt",
                              model="", agent_tool="opencode", instruction_prompt="",
                              phase="idle")
            db.add(session)
            await db.commit()
            await db.refresh(session)

            now = datetime.now(timezone.utc)
            # Both schedules are past-due (cron_next_run in the past).
            past1 = now - timedelta(minutes=10)
            past2 = now - timedelta(minutes=5)

            s1 = SessionSchedule(session_id=session.id, cron_schedule="0 * * * *",
                                 cron_next_run=past1, enabled=True)
            s2 = SessionSchedule(session_id=session.id, cron_schedule="0 */6 * * *",
                                 cron_next_run=past2, enabled=True)
            db.add_all([s1, s2])
            await db.commit()
            s1_id, s2_id = s1.id, s2.id

            # Patch _do_launch to avoid real sandbox creation.
            async def _mock_launch(session_obj, ws_obj, db_obj):
                launched.append((session_obj.id, session_obj.active_schedule_id))
                # Simulate what _do_launch does: mark session as pending→running→succeeded
                # so phase check gates the second schedule correctly.
                session_obj.phase = "running"
                await db_obj.commit()

            _sess_mod._do_launch = _mock_launch

            try:
                # Poll 1: session is idle → should pick s1 (earliest past-due).
                await _check_and_launch(db)

                assert len(launched) == 1
                assert launched[0][1] == s1_id

                # After poll 1, session is running — s2 must wait.
                await db.refresh(session)
                assert session.phase == "running"

                # Poll 2: session still running → no new launch.
                launched.clear()
                await _check_and_launch(db)
                assert len(launched) == 0

                # Simulate session completion.
                session.phase = "idle"
                session.active_schedule_id = None
                await db.commit()

                # Poll 3: s2 is now due and session is idle → fires.
                await _check_and_launch(db)
                assert len(launched) == 1
                assert launched[0][1] == s2_id

            finally:
                if original_do_launch is not None:
                    _sess_mod._do_launch = original_do_launch


# ===========================================================================
# MCP server schedule tool tests
# ===========================================================================


class TestMCPScheduleTools:
    @pytest.mark.asyncio
    async def test_list_add_update_delete_round_trip(self):
        """MCP tools exercise the full schedule lifecycle via HTTP mocks."""
        from agent_swarm_mcp_server.server import AgentSwarmMCPServer
        from agent_swarm_mcp_server.config import AgentSwarmConfig

        config = AgentSwarmConfig(api_url="http://fake", token="tok", verify_ssl=False)
        server = AgentSwarmMCPServer(config)

        sched_data = {
            "id": 1,
            "session_id": 10,
            "cron_schedule": "0 * * * *",
            "cron_next_run": "2026-06-16T00:00:00",
            "label": "hourly",
            "prompt_id": None,
            "instruction_prompt": "",
            "enabled": True,
            "created_at": "2026-06-15T00:00:00",
            "updated_at": "2026-06-15T00:00:00",
        }

        with respx.mock:
            respx.get("http://fake/api/v1/workspaces/1/sessions/10/schedules").mock(
                return_value=httpx.Response(200, json=[sched_data])
            )
            result = await server._list_session_schedules(1, 10)
            assert len(result) == 1
            assert result[0]["cron_schedule"] == "0 * * * *"

            respx.post("http://fake/api/v1/workspaces/1/sessions/10/schedules").mock(
                return_value=httpx.Response(201, json=sched_data)
            )
            created = await server._add_session_schedule(
                1, 10, "0 * * * *", label="hourly"
            )
            assert created["label"] == "hourly"

            updated_data = {**sched_data, "label": "every-hour", "enabled": False}
            respx.put("http://fake/api/v1/workspaces/1/sessions/10/schedules/1").mock(
                return_value=httpx.Response(200, json=updated_data)
            )
            updated = await server._update_session_schedule(
                1, 10, 1, label="every-hour", enabled=False
            )
            assert updated["label"] == "every-hour"
            assert updated["enabled"] is False

            respx.delete("http://fake/api/v1/workspaces/1/sessions/10/schedules/1").mock(
                return_value=httpx.Response(204)
            )
            await server._delete_session_schedule(1, 10, 1)  # should not raise

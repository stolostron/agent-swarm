"""Tests for session run history recording."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base
from swarmer.models.session import Session
from swarmer.session_runs import STOPPED_BY_USER_DETAIL, record_session_run

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    from swarmer.crypto import init_crypto

    init_crypto("auth/secret.key")
    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _make_prompt_session(db) -> Session:
    from swarmer.models.workspace import Workspace

    ws = Workspace(display_name="test-ws", namespace="test-ns")
    db.add(ws)
    await db.flush()
    session = Session(workspace_id=ws.id, name="my-session", mode="prompt")
    session.run_started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    session.last_output = "agent finished"
    session.status_detail = "Completed"
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@pytest.mark.asyncio
async def test_record_session_run_persists_history():
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        completed_at = datetime.now(timezone.utc)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="Completed",
            last_output="agent finished",
            completed_at=completed_at,
        )
        await db.commit()

        assert run is not None
        assert run.session_id == session.id
        assert run.phase == "succeeded"
        assert run.status_detail == "Completed"
        assert run.last_output == "agent finished"
        assert run.run_duration.endswith("s")


@pytest.mark.asyncio
async def test_record_session_run_skips_without_start_time():
    from swarmer.models.workspace import Workspace

    async with _TestSession() as db:
        ws = Workspace(display_name="test-ws", namespace="test-ns-2")
        db.add(ws)
        await db.flush()
        session = Session(workspace_id=ws.id, name="no-start", mode="prompt")
        db.add(session)
        await db.commit()
        await db.refresh(session)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="",
            completed_at=datetime.now(timezone.utc),
        )
        assert run is None


@pytest.mark.asyncio
async def test_record_session_run_stopped_by_user_detail():
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        session.status_detail = "Running"
        completed_at = datetime.now(timezone.utc)

        run = await record_session_run(
            db,
            session,
            phase="stopped",
            status_detail=STOPPED_BY_USER_DETAIL,
            last_output="partial output",
            completed_at=completed_at,
        )
        await db.commit()

        assert run is not None
        assert run.status_detail == "Stopped by user"


def test_session_run_duration_active_with_naive_start():
    """Legacy naive run_started_at must not break live run_duration display."""
    session = Session(workspace_id=1, name="active", mode="prompt", phase="running")
    session.run_started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert session.run_duration is not None
    assert session.run_duration.endswith("s")


@pytest.mark.asyncio
async def test_record_session_run_normalizes_mixed_timezone_awareness():
    """Legacy naive run_started_at + aware completed_at must not break run_duration."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        session.run_started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        completed_at = datetime.now(timezone.utc)

        run = await record_session_run(
            db,
            session,
            phase="stopped",
            status_detail=STOPPED_BY_USER_DETAIL,
            last_output="",
            completed_at=completed_at,
        )
        await db.commit()

        assert run is not None
        assert run.run_duration.endswith("s")


@pytest.mark.asyncio
async def test_record_session_run_prunes_old_runs(monkeypatch):
    from sqlalchemy import func, select

    from swarmer.models.session_run import SessionRun

    monkeypatch.setattr("swarmer.session_runs.settings.session_run_history_limit", 3)

    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        for i in range(5):
            await record_session_run(
                db,
                session,
                phase="succeeded",
                status_detail=f"run-{i}",
                last_output=f"log-{i}",
                completed_at=datetime.now(timezone.utc) + timedelta(seconds=i),
            )
        await db.commit()

        count = await db.scalar(
            select(func.count())
            .select_from(SessionRun)
            .where(SessionRun.session_id == session.id)
        )
        assert count == 3
        result = await db.execute(
            select(SessionRun.status_detail)
            .where(SessionRun.session_id == session.id)
            .order_by(SessionRun.completed_at)
        )
        details = list(result.scalars().all())
        assert details == ["run-2", "run-3", "run-4"]


@pytest.mark.asyncio
async def test_record_session_run_stores_raw_output():
    """raw_output is preserved separately from last_output in session_runs."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        completed_at = datetime.now(timezone.utc)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="clean assistant response from opencode db",
            raw_output="raw console: \x1b[32m[tool] reading file\x1b[0m\n...\nDone.",
            completed_at=completed_at,
        )
        await db.commit()

        assert run is not None
        assert run.last_output == "clean assistant response from opencode db"
        assert run.raw_output == "raw console: \x1b[32m[tool] reading file\x1b[0m\n...\nDone."
        # They differ — this is the OpenCode case where the console log is preserved
        assert run.raw_output != run.last_output


@pytest.mark.asyncio
async def test_record_session_run_raw_output_defaults_empty():
    """raw_output defaults to empty string when not provided (backward compat)."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="some output",
            completed_at=datetime.now(timezone.utc),
            # raw_output not passed — should default to ""
        )
        await db.commit()

        assert run is not None
        assert run.raw_output == ""

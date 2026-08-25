"""Tests for session run history recording."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base
from swarmer.models.session import Session
from swarmer.models.workspace_prompt import WorkspacePrompt
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
async def test_record_session_run_prunes_by_age(monkeypatch):
    """Runs older than the configured max age are pruned regardless of count."""
    from sqlalchemy import func, select

    from swarmer.models.session_run import SessionRun

    # Disable count-based pruning so only age-based pruning is exercised.
    monkeypatch.setattr("swarmer.session_runs.settings.session_run_history_limit", 0)
    monkeypatch.setattr("swarmer.session_runs.settings.session_run_history_max_age_days", 2)

    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        now = datetime.now(timezone.utc)
        ages_days = [10, 5, 3, 1, 0]
        for i, age_days in enumerate(ages_days):
            session.run_started_at = now - timedelta(days=age_days, minutes=2)
            await record_session_run(
                db,
                session,
                phase="succeeded",
                status_detail=f"run-{i}",
                last_output=f"log-{i}",
                completed_at=now - timedelta(days=age_days),
            )
        await db.commit()

        result = await db.execute(
            select(SessionRun.status_detail)
            .where(SessionRun.session_id == session.id)
            .order_by(SessionRun.completed_at)
        )
        details = list(result.scalars().all())
        # Only runs completed within the last 2 days (age_days 1 and 0) survive.
        assert details == ["run-3", "run-4"]

        count = await db.scalar(
            select(func.count())
            .select_from(SessionRun)
            .where(SessionRun.session_id == session.id)
        )
        assert count == 2


@pytest.mark.asyncio
async def test_record_session_run_age_pruning_disabled(monkeypatch):
    """max_age_days=0 disables age-based pruning; old runs are retained."""
    from sqlalchemy import func, select

    from swarmer.models.session_run import SessionRun

    monkeypatch.setattr("swarmer.session_runs.settings.session_run_history_limit", 0)
    monkeypatch.setattr("swarmer.session_runs.settings.session_run_history_max_age_days", 0)

    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        now = datetime.now(timezone.utc)
        session.run_started_at = now - timedelta(days=30, minutes=2)
        await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="ancient-run",
            last_output="log",
            completed_at=now - timedelta(days=30),
        )
        await db.commit()

        count = await db.scalar(
            select(func.count())
            .select_from(SessionRun)
            .where(SessionRun.session_id == session.id)
        )
        assert count == 1


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


async def _make_prompt(db, *, source_id: int, display_name: str) -> "WorkspacePrompt":
    from swarmer.models.workspace_prompt import WorkspacePrompt

    prompt = WorkspacePrompt(
        source_id=source_id,
        filename=f"{display_name}.md",
        display_name=display_name,
        content="do the thing",
        content_hash="abc123",
    )
    db.add(prompt)
    await db.flush()
    return prompt


async def _make_prompt_source(db, ws_id: int) -> int:
    from swarmer.models.workspace_prompt import WorkspacePromptSource

    source = WorkspacePromptSource(
        workspace_id=ws_id,
        name="prompts-repo",
        repo_url="https://example.com/prompts.git",
    )
    db.add(source)
    await db.flush()
    return source.id


@pytest.mark.asyncio
async def test_record_session_run_persists_mode():
    """mode is snapshotted from the session at record time."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        session.mode = "tui"

        run = await record_session_run(
            db,
            session,
            phase="stopped",
            status_detail=STOPPED_BY_USER_DETAIL,
            last_output="",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.mode == "tui"


@pytest.mark.asyncio
async def test_record_session_run_mode_defaults_prompt():
    """mode defaults to 'prompt' when the session has no mode set."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        session.mode = ""

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="done",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.mode == "prompt"


@pytest.mark.asyncio
async def test_record_session_run_captures_session_prompt_when_no_schedule():
    """Non-scheduled runs snapshot the session's own configured prompt."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        source_id = await _make_prompt_source(db, session.workspace_id)
        prompt = await _make_prompt(db, source_id=source_id, display_name="Nightly Cleanup")
        session.prompt_id = prompt.id
        await db.flush()

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="done",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.schedule_label == ""
        assert run.prompt_name == "Nightly Cleanup"


@pytest.mark.asyncio
async def test_record_session_run_captures_active_schedule():
    """Scheduled runs snapshot the schedule's label and its own prompt (overriding the session prompt)."""
    from swarmer.models.session_schedule import SessionSchedule

    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        source_id = await _make_prompt_source(db, session.workspace_id)
        session_prompt = await _make_prompt(db, source_id=source_id, display_name="Default Prompt")
        schedule_prompt = await _make_prompt(db, source_id=source_id, display_name="Nightly Prompt")
        session.prompt_id = session_prompt.id

        schedule = SessionSchedule(
            session_id=session.id,
            prompt_id=schedule_prompt.id,
            cron_schedule="0 0 * * *",
            label="Nightly Run",
            enabled=True,
        )
        db.add(schedule)
        await db.flush()
        session.active_schedule_id = schedule.id
        await db.commit()
        await db.refresh(session)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="done",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.schedule_label == "Nightly Run"
        assert run.prompt_name == "Nightly Prompt"


@pytest.mark.asyncio
async def test_record_session_run_event_trigger_captures_context():
    """Event-triggered runs snapshot trigger_type='event' and preserve event_context."""
    import json

    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        session.event_context = json.dumps({"pr_number": 104, "action": "pr-fix", "repo": "org/repo"})
        await db.commit()
        await db.refresh(session)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="fixed it",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.trigger_type == "event"
        assert run.schedule_label == "PR #104 (pr-fix)"
        assert run.event_info.get("pr_number") == 104


@pytest.mark.asyncio
async def test_record_session_run_cron_does_not_inherit_stale_event_context():
    """ACM-42674 regression: a cron run following a prior event-triggered run on
    the same session must NOT inherit the stale session.event_context — otherwise
    Run History renders the '⚡ Event Context' drawer header on a cron entry."""
    import json

    from swarmer.models.session_schedule import SessionSchedule

    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        # Simulate a prior event-triggered run having set event_context on the
        # session row — session.event_context is never cleared after the run.
        session.event_context = json.dumps({"pr_number": 104, "action": "pr-fix"})

        schedule = SessionSchedule(
            session_id=session.id,
            cron_schedule="0 9 * * 1-5",
            label="Weekdays 9am",
            trigger_type="cron",
            enabled=True,
        )
        db.add(schedule)
        await db.flush()
        session.active_schedule_id = schedule.id
        await db.commit()
        await db.refresh(session)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="cron output",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.trigger_type == "cron"
        assert run.schedule_label == "Weekdays 9am"
        assert run.event_context == ""
        assert run.event_info == {}


@pytest.mark.asyncio
async def test_record_session_run_manual_with_no_event_context_is_manual():
    """A manual run (no active schedule, no event_context set) records as
    trigger_type='manual', never 'event'. The launch-time clearing of stale
    event_context (routers/sessions.py:session_launch and
    api/v1/sessions.py:launch_session) is what prevents a manual UI/API
    launch from inheriting a prior event run's context in the first place —
    this test asserts the resulting snapshot once that clearing has happened."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)
        session.event_context = ""
        session.active_schedule_id = None
        await db.commit()
        await db.refresh(session)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="manual output",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.trigger_type == "manual"
        assert run.event_context == ""
        assert run.event_info == {}


@pytest.mark.asyncio
async def test_record_session_run_no_prompt_or_schedule():
    """Manual runs with no configured prompt leave schedule_label/prompt_name empty."""
    async with _TestSession() as db:
        session = await _make_prompt_session(db)

        run = await record_session_run(
            db,
            session,
            phase="succeeded",
            status_detail="",
            last_output="done",
            completed_at=datetime.now(timezone.utc),
        )
        await db.commit()

        assert run is not None
        assert run.schedule_label == ""
        assert run.prompt_name == ""

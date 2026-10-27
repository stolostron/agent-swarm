"""Regression tests for legacy/removed agent_tool values (ACM-37174 CRUSH removal).

Covers:
  - migrate_db() normalizes any session with a stale/removed agent_tool
    value (e.g. "crush") to "opencode"
  - _get_provider_options() falls back to "opencode" instead of raising
    ValueError when given an unknown agent_tool, so session list/detail
    pages never 500 on legacy data
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base

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


async def _make_session_with_tool(db, agent_tool: str):
    from swarmer.models.session import Session
    from swarmer.models.workspace import Workspace

    ws = Workspace(display_name="test-ws", namespace="test-ns")
    db.add(ws)
    await db.flush()
    session = Session(workspace_id=ws.id, name="legacy-session", mode="prompt")
    # Bypass the column default by setting directly, simulating pre-existing
    # data written before CRUSH was removed from AGENT_TOOLS.
    session.agent_tool = agent_tool
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return ws, session


@pytest.mark.asyncio
async def test_migrate_db_normalizes_legacy_crush_agent_tool():
    """The startup migration rewrites any non-opencode agent_tool to opencode."""
    import swarmer.database as database_module
    from swarmer.models.session import Session

    async with _TestSession() as db:
        await _make_session_with_tool(db, "crush")

    # Exercise the real production migration function (not a re-implementation
    # of its SQL) against our in-memory test engine, so this test fails if
    # migrate_db() ever stops applying the agent_tool normalization.
    original_engine = database_module._engine
    database_module._engine = _engine
    try:
        await database_module.migrate_db()
    finally:
        database_module._engine = original_engine

    async with _TestSession() as db:
        from sqlalchemy import select

        result = await db.execute(select(Session))
        session = result.scalars().first()
        assert session is not None
        assert session.agent_tool == "opencode"


@pytest.mark.asyncio
async def test_get_provider_options_falls_back_for_unknown_agent_tool(monkeypatch, caplog):
    """_get_provider_options() must not raise for a removed tool name like 'crush'."""
    import logging

    from swarmer.routers.sessions import _get_provider_options

    async def _no_vertex(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "swarmer.openshell_client.provider_exists",
        _no_vertex,
    )

    async with _TestSession() as db:
        ws, _session = await _make_session_with_tool(db, "crush")

        # Must not raise ValueError — falls back to the opencode tool's options.
        with caplog.at_level(logging.WARNING, logger="swarmer.routers.sessions"):
            options = await _get_provider_options(ws.id, db, "crush")
        assert isinstance(options, list)
        assert any(
            "unknown agent_tool" in rec.message and "'crush'" in rec.message
            for rec in caplog.records
        )


@pytest.mark.asyncio
async def test_get_provider_options_still_works_for_valid_tool(monkeypatch, caplog):
    """Sanity check: valid agent_tool values are unaffected by the fallback."""
    import logging

    from swarmer.routers.sessions import _get_provider_options

    async def _no_vertex(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "swarmer.openshell_client.provider_exists",
        _no_vertex,
    )

    async with _TestSession() as db:
        ws, _session = await _make_session_with_tool(db, "opencode")

        with caplog.at_level(logging.WARNING, logger="swarmer.routers.sessions"):
            options = await _get_provider_options(ws.id, db, "opencode")
        assert isinstance(options, list)
        assert not any("unknown agent_tool" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_get_provider_options_probes_workspace_gateway_for_all_providers(monkeypatch):
    """Every AI-provider availability probe must target the workspace's gateway.

    The OpenAI check historically omitted the workspace client and probed the
    cluster-shared default gateway instead — on remote-gateway workspaces the
    OpenAI option was permanently unavailable (disabled radio) even though the
    key was configured on the workspace gateway. All four probes (Vertex AI,
    Gemini, OpenAI, EnMaaS) must carry the workspace-resolved client.
    """
    from unittest.mock import AsyncMock

    from swarmer.routers.sessions import _get_provider_options

    sentinel_client = object()

    async def _record(name, client=None):
        return True

    provider_exists_mock = AsyncMock(side_effect=_record)
    get_client_mock = AsyncMock(return_value=sentinel_client)

    monkeypatch.setattr(
        "swarmer.openshell_client.provider_exists", provider_exists_mock
    )
    monkeypatch.setattr(
        "swarmer.openshell_client.get_client_for_workspace", get_client_mock
    )

    async with _TestSession() as db:
        ws, _session = await _make_session_with_tool(db, "opencode")

        options = await _get_provider_options(ws.id, db, "opencode")

    probed_names = [c.args[0] if c.args else c.kwargs.get("name") for c in provider_exists_mock.call_args_list]
    assert probed_names == [
        f"swarmer-ws-{ws.id}-google-cloud",
        f"swarmer-ws-{ws.id}-google-ai-studio",
        f"swarmer-ws-{ws.id}-enmaas",
        f"swarmer-ws-{ws.id}-openai",
    ]
    for c in provider_exists_mock.call_args_list:
        assert c.kwargs.get("client") is sentinel_client, (
            f"provider_exists probe {c.args[0] if c.args else c.kwargs} did not "
            "use the workspace gateway client"
        )
    # With every provider reported present, the OpenAI preset must be offered.
    openai_opts = [
        o for o in options
        if o.get("value") == "openai" and o.get("type") == "preset"
    ]
    assert openai_opts and openai_opts[0]["available"] is True

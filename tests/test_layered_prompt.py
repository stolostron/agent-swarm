"""Unit tests for layered prompt composition logic.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base
from swarmer.models.session import Session
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource
from swarmer.routers.sessions import _resolve_session_prompt

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _setup_db():
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    settings.k8s_namespace = "test-ns"

    import swarmer.models  # noqa: F401
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with _TestSession() as db:
        db.add(Workspace(id=1, display_name="test-ws", namespace="test-ns"))
        db.add(WorkspacePromptSource(
            id=1, workspace_id=1, name="test-source",
            repo_url="https://example.com/repo", branch="main", folder_path=".",
        ))
        await db.commit()
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns


@pytest.mark.asyncio
async def test_resolve_prompt_both_set():
    async with _TestSession() as db:
        prompt = WorkspacePrompt(
            id=101,
            filename="test.md",
            display_name="test",
            content="Base Git Prompt Content",
            content_hash="hash1",
            source_id=1,
        )
        db.add(prompt)
        await db.commit()

        session = Session(
            id=1,
            name="test-session",
            workspace_id=1,
            mode="prompt",
            agent_tool="opencode",
            prompt_id=101,
            instruction_prompt="Some session-specific instruction",
        )
        
        resolved = await _resolve_session_prompt(session, db)
        assert resolved == "Some session-specific instruction\n\nBase Git Prompt Content"


@pytest.mark.asyncio
async def test_resolve_prompt_only_instruction():
    async with _TestSession() as db:
        session = Session(
            id=2,
            name="test-session-2",
            workspace_id=1,
            mode="prompt",
            agent_tool="opencode",
            prompt_id=None,
            instruction_prompt="Only custom instructions here",
        )
        resolved = await _resolve_session_prompt(session, db)
        assert resolved == "Only custom instructions here"


@pytest.mark.asyncio
async def test_resolve_prompt_only_git():
    async with _TestSession() as db:
        prompt = WorkspacePrompt(
            id=102,
            filename="test2.md",
            display_name="test2",
            content="Only Git content",
            content_hash="hash2",
            source_id=1,
        )
        db.add(prompt)
        await db.commit()

        session = Session(
            id=3,
            name="test-session-3",
            workspace_id=1,
            mode="prompt",
            agent_tool="opencode",
            prompt_id=102,
            instruction_prompt="",
        )
        resolved = await _resolve_session_prompt(session, db)
        assert resolved == "Only Git content"


@pytest.mark.asyncio
async def test_resolve_prompt_neither():
    async with _TestSession() as db:
        session = Session(
            id=4,
            name="test-session-4",
            workspace_id=1,
            mode="prompt",
            agent_tool="opencode",
            prompt_id=None,
            instruction_prompt="",
        )
        resolved = await _resolve_session_prompt(session, db)
        assert resolved == ""

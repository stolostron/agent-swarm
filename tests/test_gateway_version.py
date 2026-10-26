from types import SimpleNamespace
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from swarmer.database import Base
from swarmer.gateway_version import observe_gateway_version
import swarmer.models  # noqa: F401
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.workspace import Workspace


@pytest.mark.asyncio
async def test_gateway_version_change_resets_ai_provider_flags():
    """Clear configured AI provider markers when the shared gateway changes."""
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        workspace = Workspace(display_name="Test", namespace="test", owner_id="user")
        db.add(workspace)
        await db.flush()
        db.add(
            OpencodeSecret(
                workspace_id=workspace.id,
                gemini_configured=True,
                openai_configured=True,
                vertex_configured=True,
                enmaas_configured=True,
            )
        )
        await db.commit()

        config = SimpleNamespace(gateway_url="https://gateway.example", workspace_id=None)
        info = SimpleNamespace(gateway_version="0.1.4")
        client = SimpleNamespace(_stub=SimpleNamespace(GetGatewayInfo=lambda *args, **kwargs: info))

        await observe_gateway_version(config, client, db)
        secret = (await db.execute(select(OpencodeSecret))).scalar_one()
        await db.refresh(secret)
        assert secret.gemini_configured is True

        info.gateway_version = "0.1.5"
        await observe_gateway_version(config, client, db)
        secret = (await db.execute(select(OpencodeSecret))).scalar_one()
        await db.refresh(secret)
        assert secret.gemini_configured is False
        assert secret.openai_configured is False
        assert secret.vertex_configured is False
        assert secret.enmaas_configured is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_dedicated_gateway_version_change_resets_all_matching_workspaces():
    from swarmer.models.workspace_gateway import WorkspaceGateway

    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        ws1 = Workspace(display_name="WS1", namespace="ws1", owner_id="user")
        ws2 = Workspace(display_name="WS2", namespace="ws2", owner_id="user")
        db.add_all([ws1, ws2])
        await db.flush()

        gw_url = "https://dedicated-gw.example:443"
        gw1 = WorkspaceGateway(workspace_id=ws1.id, gateway_url=gw_url, auth_mode="none")
        gw2 = WorkspaceGateway(workspace_id=ws2.id, gateway_url=gw_url, auth_mode="none")
        db.add_all([gw1, gw2])

        sec1 = OpencodeSecret(workspace_id=ws1.id, gemini_configured=True, openai_configured=True, vertex_configured=True)
        sec2 = OpencodeSecret(workspace_id=ws2.id, gemini_configured=True, openai_configured=True, vertex_configured=True)
        db.add_all([sec1, sec2])
        await db.commit()

        config = SimpleNamespace(gateway_url=gw_url, workspace_id=ws1.id)
        info = SimpleNamespace(gateway_version="0.1.4")
        client = SimpleNamespace(_stub=SimpleNamespace(GetGatewayInfo=lambda *args, **kwargs: info))

        await observe_gateway_version(config, client, db)
        await db.refresh(gw1)
        await db.refresh(gw2)
        assert gw1.gateway_version == "0.1.4"
        assert gw2.gateway_version == "0.1.4"

        info.gateway_version = "0.1.5"
        await observe_gateway_version(config, client, db)
        await db.refresh(sec1)
        await db.refresh(sec2)
        await db.refresh(gw1)
        await db.refresh(gw2)
        assert sec1.gemini_configured is False
        assert sec2.gemini_configured is False
        assert gw1.gateway_version == "0.1.5"
        assert gw2.gateway_version == "0.1.5"

    await engine.dispose()


@pytest.mark.asyncio
async def test_observe_gateway_version_does_not_expire_caller_session_instances():
    """The gateway-version UPDATE must not expire attributes on the caller's ORM instances.

    The workspace_gateways UPDATE fires the updated_at column's onupdate.
    With ORM synchronize_session, the server-generated value expires that
    attribute on identity-mapped instances, and the caller's next attribute
    access raises MissingGreenlet in async context — this 500'd
    GET /api/v1/workspaces on remote-gateway workspaces whose gateway
    reports a version (the 60s observation cache made it intermittent).
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from swarmer.models.workspace_gateway import WorkspaceGateway

    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as db:
        ws = Workspace(display_name="WS", namespace="ws", owner_id="user")
        db.add(ws)
        await db.flush()
        gw = WorkspaceGateway(
            workspace_id=ws.id, gateway_url="https://gw.example", auth_mode="none"
        )
        db.add(gw)
        await db.commit()

        # Load through the relationship exactly like the workspaces list
        # endpoint does (selectinload), so the instance is identity-mapped.
        result = await db.execute(
            select(Workspace)
            .options(selectinload(Workspace.gateway))
            .where(Workspace.id == ws.id)
        )
        loaded = result.scalar_one()
        gw_loaded = loaded.__dict__["gateway"]
        assert "updated_at" not in sa_inspect(gw_loaded).unloaded

        config = SimpleNamespace(gateway_url="https://gw.example", workspace_id=ws.id)
        info = SimpleNamespace(gateway_version="0.1.3-rhaiv.3")
        client = SimpleNamespace(
            _stub=SimpleNamespace(GetGatewayInfo=lambda *args, **kwargs: info)
        )

        version = await observe_gateway_version(config, client, db)
        assert version == "0.1.3-rhaiv.3"

        # No lazy refresh may be required after the observation — this access
        # raises MissingGreenlet if the UPDATE expired the attribute.
        assert "updated_at" not in sa_inspect(gw_loaded).unloaded
        assert gw_loaded.updated_at is not None
        await db.rollback()

    await engine.dispose()

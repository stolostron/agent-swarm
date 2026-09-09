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
            )
        )
        await db.commit()

        config = SimpleNamespace(gateway_url="https://gateway.example", workspace_id=None)
        info = SimpleNamespace(gateway_version="0.0.116")
        client = SimpleNamespace(_stub=SimpleNamespace(GetGatewayInfo=lambda *args, **kwargs: info))

        await observe_gateway_version(config, client, db)
        secret = (await db.execute(select(OpencodeSecret))).scalar_one()
        assert secret.gemini_configured is True

        info.gateway_version = "0.0.117"
        await observe_gateway_version(config, client, db)
        secret = (await db.execute(select(OpencodeSecret))).scalar_one()
        assert secret.gemini_configured is False
        assert secret.openai_configured is False
        assert secret.vertex_configured is False

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
        info = SimpleNamespace(gateway_version="0.0.116")
        client = SimpleNamespace(_stub=SimpleNamespace(GetGatewayInfo=lambda *args, **kwargs: info))

        await observe_gateway_version(config, client, db)
        assert gw1.gateway_version == "0.0.116"
        assert gw2.gateway_version == "0.0.116"

        info.gateway_version = "0.0.117"
        await observe_gateway_version(config, client, db)
        await db.refresh(sec1)
        await db.refresh(sec2)
        assert sec1.gemini_configured is False
        assert sec2.gemini_configured is False
        assert gw1.gateway_version == "0.0.117"
        assert gw2.gateway_version == "0.0.117"

    await engine.dispose()

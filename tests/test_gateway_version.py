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

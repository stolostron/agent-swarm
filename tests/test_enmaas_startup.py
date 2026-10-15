"""Startup reconciliation must fail closed on workspace gateway resolution."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_reconcile_skips_workspace_when_gateway_resolution_fails(monkeypatch):
    from swarmer import main, openshell_client
    from swarmer.config import settings

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [type("Secret", (), {"workspace_id": 17})()]

    class _Database:
        async def execute(self, _query):
            return _Result()

    async def _get_db():
        yield _Database()

    monkeypatch.setattr(settings, "enmaas_base_url", "https://operator.example/v1")
    monkeypatch.setattr("swarmer.database.get_db", _get_db)
    resolve = AsyncMock(side_effect=RuntimeError("gateway unavailable"))
    import_profile = AsyncMock()
    monkeypatch.setattr(openshell_client, "get_client_for_workspace", resolve)
    monkeypatch.setattr(openshell_client, "import_enmaas_profile", import_profile)

    await main._reconcile_workspace_enmaas_profiles()

    resolve.assert_awaited_once_with(17)
    import_profile.assert_not_awaited()

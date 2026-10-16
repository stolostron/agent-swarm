"""Startup reconciliation must fail closed on workspace gateway resolution."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_reconcile_skips_workspace_when_gateway_resolution_fails(monkeypatch):
    from swarmer import main, openshell_client

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [
                type(
                    "Secret",
                    (),
                    {"workspace_id": 17, "enmaas_base_url": "https://operator.example/v1"},
                )()
            ]

    class _Database:
        async def execute(self, _query):
            return _Result()

    async def _get_db():
        yield _Database()

    monkeypatch.setattr("swarmer.database.get_db", _get_db)
    resolve = AsyncMock(side_effect=RuntimeError("gateway unavailable"))
    import_profile = AsyncMock()
    monkeypatch.setattr(openshell_client, "get_client_for_workspace", resolve)
    monkeypatch.setattr(openshell_client, "import_enmaas_profile", import_profile)

    await main._reconcile_workspace_enmaas_profiles()

    resolve.assert_awaited_once_with(17)
    import_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_imports_each_workspace_with_its_stored_url(monkeypatch):
    """Reconciliation binds each workspace profile to that workspace's own
    stored gateway URL (not a deployment-level setting)."""
    from swarmer import main, openshell_client

    secrets = [
        type(
            "Secret",
            (),
            {"workspace_id": 1, "enmaas_base_url": "https://alpha.example/v1"},
        )(),
        type("Secret", (), {"workspace_id": 2, "enmaas_base_url": ""})(),
        type(
            "Secret",
            (),
            {"workspace_id": 3, "enmaas_base_url": "https://gamma.example/v1"},
        )(),
    ]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return secrets

    class _Database:
        async def execute(self, _query):
            return _Result()

    async def _get_db():
        yield _Database()

    async def _resolve(ws_id):
        return object()  # any non-None client

    monkeypatch.setattr("swarmer.database.get_db", _get_db)
    monkeypatch.setattr(
        openshell_client, "get_client_for_workspace", AsyncMock(side_effect=_resolve)
    )
    import_profile = AsyncMock(return_value=True)
    monkeypatch.setattr(openshell_client, "import_enmaas_profile", import_profile)

    await main._reconcile_workspace_enmaas_profiles()

    # Workspace 2 is skipped (no URL); 1 and 3 import with their own URLs.
    assert import_profile.await_count == 2
    first = import_profile.await_args_list[0]
    second = import_profile.await_args_list[1]
    assert first.args[0] == 1
    assert first.kwargs["base_url"] == "https://alpha.example/v1"
    assert second.args[0] == 3
    assert second.kwargs["base_url"] == "https://gamma.example/v1"

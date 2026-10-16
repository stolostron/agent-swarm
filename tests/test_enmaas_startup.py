"""Startup reconciliation must fail closed on gateway resolution."""

from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_reconcile_skips_workspace_when_gateway_resolution_fails(monkeypatch, caplog):
    """Skip an unresolved workspace and keep identifiers out of its warning."""
    from swarmer import main, openshell_client
    from swarmer.config import settings

    monkeypatch.setattr(settings, "enmaas_base_url", "https://operator.example/v1")

    class _Result:
        def scalars(self):
            """Provide the scalar result interface used by the query handler."""
            return self

        def all(self):
            """Return the configured fake workspace credential rows."""
            return [
                type(
                    "Secret",
                    (),
                    {"workspace_id": 17},
                )()
            ]

    class _Database:
        async def execute(self, _query):
            """Return fake configured EnMaaS credentials for reconciliation."""
            return _Result()

    async def _get_db():
        """Yield the fake database session consumed by startup reconciliation."""
        yield _Database()

    monkeypatch.setattr("swarmer.database.get_db", _get_db)
    sentinel = "internal-gateway-host"
    resolve = AsyncMock(side_effect=RuntimeError(f"gateway unavailable: {sentinel}"))
    import_profile = AsyncMock()
    monkeypatch.setattr(openshell_client, "get_client_for_workspace", resolve)
    monkeypatch.setattr(openshell_client, "import_enmaas_profile", import_profile)

    await main._reconcile_workspace_enmaas_profiles()

    resolve.assert_awaited_once_with(17)
    import_profile.assert_not_awaited()
    assert "17" not in caplog.text
    assert sentinel not in caplog.text
    assert "Traceback" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_profile_failure_logs_only_safe_classification(monkeypatch, caplog):
    """Redact workspace IDs and exception details on profile import failure."""
    from swarmer import main, openshell_client
    from swarmer.config import settings

    monkeypatch.setattr(settings, "enmaas_base_url", "https://operator.example/v1")

    class _Result:
        def scalars(self):
            """Provide the scalar result interface used by the query handler."""
            return self

        def all(self):
            """Return the fake workspace credential row for reconciliation."""
            return [type("Secret", (), {"workspace_id": 27})()]

    class _Database:
        async def execute(self, _query):
            """Return fake configured EnMaaS credentials for reconciliation."""
            return _Result()

    async def _get_db():
        """Yield the fake database session consumed by startup reconciliation."""
        yield _Database()

    monkeypatch.setattr("swarmer.database.get_db", _get_db)
    client = type("Client", (), {})()
    monkeypatch.setattr(
        openshell_client, "get_client_for_workspace", AsyncMock(return_value=client)
    )
    sentinel = "internal-gateway-host"
    monkeypatch.setattr(
        openshell_client,
        "import_enmaas_profile",
        AsyncMock(side_effect=RuntimeError(f"profile import failed: {sentinel}")),
    )

    await main._reconcile_workspace_enmaas_profiles()

    assert "27" not in caplog.text
    assert sentinel not in caplog.text
    assert "Traceback" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_reconcile_uses_same_operator_url_for_each_workspace(monkeypatch):
    """Every workspace profile uses the shared operator-managed endpoint."""
    from swarmer import main, openshell_client
    from swarmer.config import settings

    monkeypatch.setattr(settings, "enmaas_base_url", "https://operator.example/v1")

    secrets = [
        type(
            "Secret",
            (),
            {"workspace_id": 1},
        )(),
        type(
            "Secret",
            (),
            {"workspace_id": 3},
        )(),
    ]

    class _Result:
        def scalars(self):
            """Expose the fake query result through SQLAlchemy's scalar API."""
            return self

        def all(self):
            """Return each fake workspace credential selected for reconciliation."""
            return secrets

    class _Database:
        async def execute(self, _query):
            """Return the fake workspace credential query result."""
            return _Result()

    async def _get_db():
        """Yield a fake database session for the reconciliation helper."""
        yield _Database()

    async def _resolve(ws_id):
        """Return a gateway client placeholder for each workspace ID."""
        return object()  # any non-None client

    monkeypatch.setattr("swarmer.database.get_db", _get_db)
    monkeypatch.setattr(
        openshell_client, "get_client_for_workspace", AsyncMock(side_effect=_resolve)
    )
    import_profile = AsyncMock(return_value=True)
    monkeypatch.setattr(openshell_client, "import_enmaas_profile", import_profile)

    await main._reconcile_workspace_enmaas_profiles()

    # Both configured workspaces use the same operator endpoint.
    assert import_profile.await_count == 2
    first = import_profile.await_args_list[0]
    second = import_profile.await_args_list[1]
    assert first.args[0] == 1
    assert first.kwargs["base_url"] == "https://operator.example/v1"
    assert second.args[0] == 3
    assert second.kwargs["base_url"] == "https://operator.example/v1"

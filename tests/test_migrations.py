"""Tests for database.py migrate_db() — verifies that legacy schema columns are
handled correctly.

These tests exercise the migration path that normal unit tests miss: the in-memory
SQLite DB used by test_api.py is always built from the current model via
create_all, so columns removed from the model (like `persist`) are never present.
Here we manually add legacy columns, then run migrate_db() and verify the schema is
correct and session INSERT succeeds.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from unittest.mock import AsyncMock, patch

from swarmer.database import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_TestSession = async_sessionmaker(_engine, expire_on_commit=False)


async def _override_get_db():
    async with _TestSession() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _setup(monkeypatch):
    from swarmer.crypto import init_crypto
    init_crypto("auth/secret.key")

    from swarmer.config import settings
    orig_ns = settings.k8s_namespace
    settings.k8s_namespace = ""  # must be empty to allow workspace creation

    async def _all_accessible(token, namespaces, api_url, in_cluster):
        return list(namespaces)

    async def _can_create_namespaces(token, api_url, in_cluster):
        return True

    monkeypatch.setattr("swarmer.api.deps.get_accessible_namespaces", _all_accessible)
    monkeypatch.setattr("swarmer.api.v1.workspaces.can_create_namespaces", _can_create_namespaces)
    monkeypatch.setattr("swarmer.k8s.ensure_namespace", lambda namespace: None)
    monkeypatch.setattr("swarmer.k8s.grant_swarmer_user_access", lambda namespace, username: None)
    monkeypatch.setattr("swarmer.k8s.delete_namespace", lambda namespace: None)

    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    settings.k8s_namespace = orig_ns


@pytest_asyncio.fixture
async def client():
    """httpx client wired to the FastAPI app with auth and DB overridden."""
    from swarmer.database import get_db
    from swarmer.deps import require_auth
    from swarmer.main import app

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_auth] = lambda: None  # bypass cookie auth

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMigrateDbDropsLegacyColumns:
    """ACM-35375: migrate_db() must DROP columns removed in ACM-34863 so that
    session INSERT no longer fails with NOT NULL constraint violations."""

    @pytest.mark.asyncio
    async def test_persist_column_dropped_by_migration(self):
        """Simulate a pre-ACM-34863 database that still has `persist NOT NULL`.

        After migrate_db() runs, a new Session INSERT must succeed without a
        NOT NULL constraint error on the `persist` column.
        """
        # Inject the legacy `persist` column with NOT NULL (DEFAULT 0 needed to
        # ADD the column; SQLite doesn't support DROP DEFAULT, but the real bug
        # was that SQLAlchemy's INSERT omits the column entirely, so any NOT NULL
        # column without a server_default triggers the constraint).
        async with _engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE sessions ADD COLUMN persist BOOLEAN NOT NULL DEFAULT 0")
            )

        # Confirm the column is present before migration
        async with _engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(sessions)"))
            cols = [row[1] for row in result.fetchall()]
        assert "persist" in cols, "Test setup failed: persist column should exist"

        # Run the migration — should DROP persist (and other legacy columns)
        import swarmer.database as db_module

        orig_engine = db_module._engine
        db_module._engine = _engine
        try:
            await db_module.migrate_db()
        finally:
            db_module._engine = orig_engine

        # Confirm persist was dropped
        async with _engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(sessions)"))
            cols_after = [row[1] for row in result.fetchall()]
        assert "persist" not in cols_after, (
            "migrate_db() should have dropped the `persist` column"
        )

        # Confirm a session INSERT now works (the actual bug fix)
        from swarmer.models.workspace import Workspace

        async with _TestSession() as session:
            ws = Workspace(display_name="mig-test", description="", namespace="test-ns")
            session.add(ws)
            await session.commit()
            await session.refresh(ws)

        from swarmer.models.session import Session

        async with _TestSession() as session:
            s = Session(workspace_id=ws.id, name="mig-session")
            session.add(s)
            # This must not raise IntegrityError for persist
            await session.commit()

    @pytest.mark.asyncio
    async def test_migrate_db_idempotent_on_fresh_schema(self):
        """migrate_db() must not raise when run against a fresh schema
        (columns already absent — 'no such column' suppressed)."""
        import swarmer.database as db_module

        orig_engine = db_module._engine
        db_module._engine = _engine
        try:
            # Should complete without raising
            await db_module.migrate_db()
        finally:
            db_module._engine = orig_engine


class TestSessionFormCreatePath:
    """ACM-35375: The HTML form POST /workspaces/{ws_id}/sessions must succeed.

    The REST API (/api/v1/...) tests use create_all on the current model so the
    `persist` column is never present — they never exercised the form handler.
    These tests go through the actual HTML router used by the browser.
    """

    @pytest.mark.asyncio
    async def test_form_create_session_succeeds(self, client):
        """POST /workspaces/{ws_id}/sessions with form data must redirect (302),
        not return a 500 Internal Server Error."""
        # Create a workspace via the API first
        from swarmer.api.deps import get_current_user, require_api_auth
        from swarmer.k8s_auth import TokenIdentity
        from swarmer.main import app

        from swarmer.api.deps import get_bearer_token
        app.dependency_overrides[require_api_auth] = lambda: TokenIdentity(
            username="test-user", uid="uid-1"
        )
        app.dependency_overrides[get_current_user] = lambda: "test-user"
        app.dependency_overrides[get_bearer_token] = lambda: "test-token"
        ws_resp = await client.post(
            "/api/v1/workspaces",
            json={"display_name": "Form Test WS", "description": ""},
        )
        app.dependency_overrides.pop(require_api_auth, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_bearer_token, None)
        assert ws_resp.status_code == 201, ws_resp.text
        ws_id = ws_resp.json()["id"]

        # Submit the session create form — this is the path that was broken
        resp = await client.post(
            f"/workspaces/{ws_id}/sessions",
            data={"name": "my-session"},
            follow_redirects=False,
        )

        # Must redirect to the session detail page, not 500
        assert resp.status_code in (302, 303), (
            f"Expected redirect after form session create, got {resp.status_code}: {resp.text[:200]}"
        )
        assert f"/workspaces/{ws_id}/sessions/" in resp.headers.get("location", ""), (
            f"Expected redirect to session detail, got location: {resp.headers.get('location')}"
        )

    @pytest.mark.asyncio
    async def test_form_create_duplicate_name_returns_422(self, client):
        """POST /workspaces/{ws_id}/sessions with a duplicate name must return 422
        and re-render the form — not crash with a 500 from missing template context."""
        from swarmer.api.deps import get_current_user, require_api_auth
        from swarmer.k8s_auth import TokenIdentity
        from swarmer.main import app

        from swarmer.api.deps import get_bearer_token
        app.dependency_overrides[require_api_auth] = lambda: TokenIdentity(
            username="test-user", uid="uid-1"
        )
        app.dependency_overrides[get_current_user] = lambda: "test-user"
        app.dependency_overrides[get_bearer_token] = lambda: "test-token"
        ws_resp = await client.post(
            "/api/v1/workspaces",
            json={"display_name": "Dup Test WS", "description": ""},
        )
        app.dependency_overrides.pop(require_api_auth, None)
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_bearer_token, None)
        assert ws_resp.status_code == 201, ws_resp.text
        ws_id = ws_resp.json()["id"]

        # Patch k8s image check — not available in unit test environment
        with patch("swarmer.k8s.get_image_available", new=AsyncMock(return_value=False)):
            # Create the session once via the form
            resp1 = await client.post(
                f"/workspaces/{ws_id}/sessions",
                data={"name": "dup-session"},
                follow_redirects=False,
            )
            assert resp1.status_code in (302, 303), f"First create failed: {resp1.status_code}"

            # Try the same name again — must get 422 with the form re-rendered, not 500
            # (Before the fix this returned 500 because mcp_servers/prompt_sources were
            # missing from the IntegrityError handler's template context.)
            resp2 = await client.post(
                f"/workspaces/{ws_id}/sessions",
                data={"name": "dup-session"},
                follow_redirects=False,
            )
        assert resp2.status_code == 422, (
            f"Expected 422 for duplicate name, got {resp2.status_code}: {resp2.text[:200]}"
        )

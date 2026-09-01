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

    monkeypatch.setattr("swarmer.k8s.ensure_namespace", lambda namespace: None)
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


class TestWorkspaceMemberBackfill:
    """ACM-41659 follow-up: migrate_db() must backfill workspace_members and
    Workspace.owner_id from existing per-user credential tables, so nobody
    has to be manually re-added to a workspace they already had access to."""

    @pytest.mark.asyncio
    async def test_backfills_members_and_owner_from_secrets_and_pats(self):
        async with _engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO workspaces (id, display_name, namespace, description, owner_id) "
                "VALUES (1, 'Team A', 'team-a', '', '')"
            ))
            await conn.execute(text(
                "INSERT INTO opencode_secrets "
                "(workspace_id, user_id, google_cloud_project, vertex_location, "
                " application_default_credentials_enc, google_api_key_enc, created_at) "
                "VALUES (1, 'alice', '', '', '', '', '2020-01-01T00:00:00')"
            ))
            await conn.execute(text(
                "INSERT INTO github_pats "
                "(workspace_id, user_id, name, github_username, github_org, pat_enc, description, created_at) "
                "VALUES (1, 'bob', 'x', 'bob', '', 'enc', '', '2019-01-01T00:00:00')"
            ))
            # A second, never-used workspace — must remain ownerless (falls
            # through to workspace_acl's "claim on write" fallback).
            await conn.execute(text(
                "INSERT INTO workspaces (id, display_name, namespace, description, owner_id) "
                "VALUES (2, 'Team B', 'team-b', '', '')"
            ))

        import swarmer.database as db_module

        orig_engine = db_module._engine
        db_module._engine = _engine
        try:
            await db_module.migrate_db()
        finally:
            db_module._engine = orig_engine

        async with _engine.begin() as conn:
            result = await conn.execute(text("SELECT id, owner_id FROM workspaces ORDER BY id"))
            owners = dict(result.fetchall())
            result = await conn.execute(
                text("SELECT workspace_id, user_id FROM workspace_members ORDER BY workspace_id, user_id")
            )
            members = result.fetchall()

        # bob's PAT predates alice's secret (2019 < 2020) — bob becomes owner.
        assert owners[1] == "bob"
        assert owners[2] == ""
        assert (1, "alice") in members
        assert (1, "bob") in members
        assert not any(row[0] == 2 for row in members)

    @pytest.mark.asyncio
    async def test_does_not_overwrite_existing_owner(self):
        async with _engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO workspaces (id, display_name, namespace, description, owner_id) "
                "VALUES (3, 'Team C', 'team-c', '', 'carol')"
            ))
            await conn.execute(text(
                "INSERT INTO opencode_secrets "
                "(workspace_id, user_id, google_cloud_project, vertex_location, "
                " application_default_credentials_enc, google_api_key_enc, created_at) "
                "VALUES (3, 'dave', '', '', '', '', '2018-01-01T00:00:00')"
            ))

        import swarmer.database as db_module

        orig_engine = db_module._engine
        db_module._engine = _engine
        try:
            await db_module.migrate_db()
        finally:
            db_module._engine = orig_engine

        async with _engine.begin() as conn:
            result = await conn.execute(text("SELECT owner_id FROM workspaces WHERE id = 3"))
            assert result.scalar_one() == "carol"
            result = await conn.execute(
                text("SELECT user_id FROM workspace_members WHERE workspace_id = 3")
            )
            # dave is still added as a member even though carol keeps ownership
            assert result.scalar_one() == "dave"

    @pytest.mark.asyncio
    async def test_global_admins_table_created(self):
        import swarmer.database as db_module

        orig_engine = db_module._engine
        db_module._engine = _engine
        try:
            await db_module.migrate_db()
        finally:
            db_module._engine = orig_engine

        async with _engine.begin() as conn:
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='global_admins'")
            )
            assert result.fetchone() is not None


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


class TestPrActionStateMigration:
    """ACM-43054: migrate_db() must idempotently add session_id to pr_action_state
    and recreate uq_pr_action_state_key index including session_id."""

    @pytest.mark.asyncio
    async def test_migrate_db_adds_session_id_to_legacy_pr_action_state(self) -> None:
        # Recreate a legacy pr_action_state table without session_id
        async with _engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS pr_action_state"))
            await conn.execute(text("""
                CREATE TABLE pr_action_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo VARCHAR(255) NOT NULL,
                    pr_number INTEGER NOT NULL,
                    head_sha VARCHAR(64) NOT NULL,
                    action VARCHAR(32) NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'dispatched',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_dispatched_at DATETIME,
                    created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
                    updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
                )
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX uq_pr_action_state_key
                ON pr_action_state (repo, pr_number, head_sha, action)
            """))

        # Verify session_id does not exist before migration
        async with _engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(pr_action_state)"))
            cols_before = [row[1] for row in result.fetchall()]
        assert "session_id" not in cols_before

        # Run migrate_db()
        import swarmer.database as db_module

        orig_engine = db_module._engine
        db_module._engine = _engine
        try:
            await db_module.migrate_db()

            # Verify session_id column exists
            async with _engine.begin() as conn:
                result = await conn.execute(text("PRAGMA table_info(pr_action_state)"))
                cols_after = [row[1] for row in result.fetchall()]
            assert "session_id" in cols_after

            # Verify the unique index includes all 5 columns
            async with _engine.begin() as conn:
                result = await conn.execute(text("PRAGMA index_info(uq_pr_action_state_key)"))
                indexed_cols = [row[2] for row in result.fetchall()]
            assert indexed_cols == ["repo", "pr_number", "head_sha", "action", "session_id"]

            # Verify multiple sessions can now record actions for the same PR and head SHA
            async with _engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO pr_action_state (repo, pr_number, head_sha, action, session_id)
                    VALUES ('test-repository', 1, 'sha1', 'ci_fail_or_conflict', 10)
                """))
                await conn.execute(text("""
                    INSERT INTO pr_action_state (repo, pr_number, head_sha, action, session_id)
                    VALUES ('test-repository', 1, 'sha1', 'ci_fail_or_conflict', 20)
                """))

            # Verify unique constraint enforces duplicate prevention for identical keys
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):
                async with _engine.begin() as conn:
                    await conn.execute(text("""
                        INSERT INTO pr_action_state (repo, pr_number, head_sha, action, session_id)
                        VALUES ('test-repository', 1, 'sha1', 'ci_fail_or_conflict', 10)
                    """))

            # Running migrate_db() again must be idempotent and succeed without error
            await db_module.migrate_db()
        finally:
            db_module._engine = orig_engine


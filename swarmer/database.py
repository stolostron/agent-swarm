import logging
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_AsyncSessionLocal: async_sessionmaker | None = None


def init_db(database_url: str) -> None:
    global _engine, _AsyncSessionLocal
    connect_args = {}
    engine_kwargs: dict = {"echo": False}
    if database_url.startswith("sqlite"):
        connect_args["timeout"] = 15
        # aiosqlite opens a new connection per call and doesn't benefit from
        # SQLAlchemy's QueuePool.  NullPool creates connections on-demand and
        # closes them immediately after use, eliminating pool exhaustion under
        # concurrent load (the chat proxy makes one DB call per proxied asset).
        engine_kwargs["poolclass"] = NullPool
    engine_kwargs["connect_args"] = connect_args
    _engine = create_async_engine(database_url, **engine_kwargs)

    if database_url.startswith("sqlite"):
        @event.listens_for(_engine.sync_engine, "connect")
        def _set_wal(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            # WAL lets readers and the scheduler writer proceed concurrently.
            cursor.execute("PRAGMA journal_mode=WAL")
            # Retry for up to 5 s before raising "database is locked".
            cursor.execute("PRAGMA busy_timeout=5000")
            # Clear any stale WAL state left by a previous unclean shutdown.
            cursor.execute("PRAGMA wal_checkpoint(PASSIVE)")
            cursor.close()

    _AsyncSessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


async def create_tables() -> None:
    # Import models so their tables are registered on Base.metadata
    import swarmer.models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def migrate_db() -> None:
    """Lightweight migrations for columns added after initial schema creation."""
    from sqlalchemy import text

    migrations = [
        "ALTER TABLE sessions ADD COLUMN agent_tool VARCHAR(32) NOT NULL DEFAULT 'opencode'",
        "ALTER TABLE opencode_secrets ADD COLUMN anthropic_api_key_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE opencode_secrets ADD COLUMN openai_api_key_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN status_detail VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN run_started_at DATETIME",
        "ALTER TABLE sessions ADD COLUMN run_completed_at DATETIME",
        "ALTER TABLE sessions ADD COLUMN working_branch VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN patch_output TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN commit_msg TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN patch_base_ref VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN cron_schedule VARCHAR(128) NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN cron_next_run DATETIME",
        "ALTER TABLE github_pats ADD COLUMN github_org TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN mcp_server_ids TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN jira_server_url TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN jira_access_token_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN jira_email TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE opencode_secrets ADD COLUMN user_id VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE opencode_secrets ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE github_pats ADD COLUMN user_id VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE github_pats ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE mcp_servers ADD COLUMN user_id VARCHAR(255) NOT NULL DEFAULT ''",
        "ALTER TABLE mcp_servers ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE sessions ADD COLUMN prompt_id INTEGER REFERENCES workspace_prompts(id) ON DELETE SET NULL",
        "ALTER TABLE sessions DROP COLUMN resume",
        "ALTER TABLE sessions ADD COLUMN sandbox_name VARCHAR(255)",
        "ALTER TABLE sessions ADD COLUMN service_url VARCHAR(512)",
        "ALTER TABLE sessions ADD COLUMN policy_chunks TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN custom_policies TEXT NOT NULL DEFAULT ''",
        # ACM-35375: drop columns removed from Session model in ACM-34863 (K8s cleanup)
        # Error suppression ("no such column") handles fresh databases safely.
        "ALTER TABLE sessions DROP COLUMN persist",
        "ALTER TABLE sessions DROP COLUMN privileged",
        "ALTER TABLE sessions DROP COLUMN pod_name",
        "ALTER TABLE sessions DROP COLUMN pvc_name",
        "ALTER TABLE sessions DROP COLUMN k8s_secret_names",
        # ACM-35377: multi-schedule support
        "ALTER TABLE sessions ADD COLUMN active_schedule_id INTEGER",
        """CREATE TABLE IF NOT EXISTS session_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            prompt_id INTEGER REFERENCES workspace_prompts(id) ON DELETE SET NULL,
            cron_schedule VARCHAR(128) NOT NULL,
            cron_next_run DATETIME,
            label VARCHAR(128) NOT NULL DEFAULT '',
            instruction_prompt TEXT NOT NULL DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )""",
        # Migrate existing per-session schedules into the new table.
        # Runs on every startup — the INSERT ... WHERE NOT EXISTS makes it idempotent.
        """INSERT INTO session_schedules (session_id, cron_schedule, cron_next_run, label, enabled)
           SELECT id, cron_schedule, cron_next_run, '', 1
           FROM sessions
           WHERE cron_schedule != ''
             AND NOT EXISTS (
               SELECT 1 FROM session_schedules ss WHERE ss.session_id = sessions.id
             )""",
        # ACM-35068: session run history table (prompt-mode execution records)
        """CREATE TABLE IF NOT EXISTS session_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            phase VARCHAR(32) NOT NULL,
            status_detail VARCHAR(255) NOT NULL DEFAULT '',
            started_at DATETIME NOT NULL,
            completed_at DATETIME NOT NULL,
            last_output TEXT NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )""",
        # ACM-35750: raw streaming console output preserved alongside processed last_output
        "ALTER TABLE sessions ADD COLUMN raw_output TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE session_runs ADD COLUMN raw_output TEXT NOT NULL DEFAULT ''",
        # ACM-34850: deduplicate opencode_secrets rows then enforce (workspace_id, user_id) uniqueness.
        # Keep the newest row per (workspace_id, user_id) pair before creating the index.
        """DELETE FROM opencode_secrets WHERE id NOT IN (
            SELECT MAX(id) FROM opencode_secrets GROUP BY workspace_id, user_id
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_opencode_secrets_workspace_user
           ON opencode_secrets (workspace_id, user_id)""",
        # ACM-35370: GitHub App credentials for server-side IAT minting
        """CREATE TABLE IF NOT EXISTS github_apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL UNIQUE REFERENCES workspaces(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL DEFAULT '',
            shared BOOLEAN NOT NULL DEFAULT 0,
            app_id TEXT NOT NULL DEFAULT '',
            installation_id TEXT NOT NULL DEFAULT '',
            private_key_enc TEXT NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
            updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now'))
        )""",
        # ACM-37190: CRUSH agent tool removed (ACM-37174) — normalize any existing
        # sessions still carrying the retired 'crush' value so registry lookups
        # (get_tool) don't raise ValueError when rendering session list/detail pages.
        "UPDATE sessions SET agent_tool = 'opencode' WHERE agent_tool != 'opencode'",
    ]
    async with _engine.begin() as conn:
        for stmt in migrations:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg or "no such column" in msg:
                    continue
                log.error("Migration failed for %r: %s", stmt, e)
                raise


async def checkpoint_db() -> None:
    """Force a WAL TRUNCATE checkpoint at startup.

    Consolidates any stale .db-wal data left by an unclean shutdown into the
    main database file, then truncates the WAL so subsequent connections start
    clean. Must be called after init_db() and before the server starts serving
    requests. No-op for non-SQLite engines.
    """
    if _engine is None:
        return
    url = str(_engine.url)
    if not url.startswith("sqlite"):
        return
    from sqlalchemy import text
    try:
        async with _engine.connect() as conn:
            result = await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            row = result.fetchone()
            # row = (busy, log_pages, checkpointed_pages)
            if row and row[0]:
                log.warning("db: WAL checkpoint incomplete — %d page(s) still busy", row[0])
            else:
                log.info("db: WAL checkpoint complete, WAL truncated")
            await conn.commit()
    except Exception:
        log.warning("db: WAL checkpoint failed (DB may be held by another process)", exc_info=True)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _AsyncSessionLocal() as session:
        yield session

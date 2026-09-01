from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from swarmer import k8s
from swarmer.config import settings
from swarmer.crypto import derive_session_secret, init_crypto
from swarmer.database import checkpoint_db, create_tables, migrate_db, init_db
from swarmer.deps import NotAuthenticated
from swarmer.api.v1 import router as api_v1_router
from swarmer.routers import admins as admins_router
from swarmer.routers import auth as auth_router
from swarmer.routers import chat_proxy as chat_proxy_router
from swarmer.routers import env_vars as env_vars_router
from swarmer.routers import mcp_servers as mcp_servers_router
from swarmer.routers import office as office_router
from swarmer.routers import prompts as prompts_router
from swarmer.routers import sessions as sessions_router
from swarmer.routers import secrets as secrets_router
from swarmer.routers import tui_ws as tui_router
from swarmer.routers import workspaces as workspaces_router

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from swarmer.models.session import Session

log = logging.getLogger(__name__)

# Strong references to fire-and-forget IAT-refresh restart tasks (one per surviving
# server/TUI session in _restart_github_app_iat_refresh). asyncio only holds a weak
# reference to tasks created via asyncio.create_task(); without this registry the
# task object could be garbage-collected mid-refresh with no warning. Each task
# removes itself via add_done_callback once it completes (or is cancelled).
_iat_refresh_restart_tasks: set[asyncio.Task] = set()

# Custom provider profiles swarmer registers in the OpenShell gateway at startup.
# google-vertex-ai is built-in since OpenShell 0.0.55 — no need to import it.
_OPENSHELL_CUSTOM_PROFILES = [
    {
        "id": "google-ai-studio",
        "display_name": "Google AI Studio",
        "inference_capable": True,
        "credentials": [
            {
                # Credential name IS the env var injected into the sandbox.
                # env_vars is used by the gateway proxy for HTTP request rewriting.
                "name": "GOOGLE_API_KEY",
                "env_vars": ["GOOGLE_API_KEY"],
                "required": True,
                "auth_style": "header",
                "header_name": "x-goog-api-key",
            }
        ],
    },
    {
        "id": "jira",
        "display_name": "Jira",
        "inference_capable": False,
        "credentials": [
            # JIRA_ACCESS_TOKEN is a secret credential — the gateway stores it securely
            # and injects it as an opaque reference token (openshell:resolve:...) into
            # the sandbox via GetSandboxProviderEnvironment.
            # JIRA_SERVER_URL and JIRA_EMAIL are non-secret; they go into provider config
            # (not credentials) and the gateway injects them as plain env vars alongside
            # the credential reference tokens.
            {"name": "JIRA_ACCESS_TOKEN", "env_vars": ["JIRA_ACCESS_TOKEN"], "required": True},
        ],
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configure logging first so all startup messages are captured at the right level.
    # LOG_LEVEL env var (or .env) controls verbosity: DEBUG, INFO, WARNING, ERROR.
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(levelname)s:%(name)s:%(message)s",
    )
    # Crypto must be initialised before any DB access (model properties call decrypt)
    init_crypto(settings.secret_key_file)
    init_db(settings.database_url)
    await checkpoint_db()
    await create_tables()
    await migrate_db()
    k8s.init_k8s(settings.k8s_in_cluster)
    await _sync_k8s_workspace_members()
    if settings.openshell_gateway_url:
        await _ensure_openshell_provider_profiles()
    await _restart_prompt_pollers()
    if settings.openshell_gateway_url:
        await _restart_server_sessions()
    from swarmer import pr_watcher, scheduler
    scheduler.start_scheduler()
    if settings.openshell_gateway_url:
        pr_watcher.start_pr_watcher()
    yield
    await pr_watcher.shutdown()
    await scheduler.shutdown()


async def _sync_k8s_workspace_members() -> None:
    """Best-effort startup migration (ACM-41659): mirror legacy K8s RBAC
    workspace grants into workspace_members. Never blocks startup."""
    try:
        from swarmer.database import get_db
        from swarmer.workspace_migration import sync_k8s_workspace_members

        async for db in get_db():
            await sync_k8s_workspace_members(db)
            break
    except Exception:
        log.warning("K8s workspace-member sync skipped (non-fatal)", exc_info=True)


async def _ensure_openshell_provider_profiles() -> None:
    """Import custom provider profiles into the OpenShell gateway (idempotent)."""
    from swarmer import openshell_client
    try:
        # Enable providers_v2 so google-vertex-ai type is supported for inference routing.
        await openshell_client.enable_providers_v2()
        log.info("OpenShell providers_v2_enabled set")
    except Exception:
        log.warning("Failed to enable OpenShell providers_v2 — VertexAI inference routing may not work", exc_info=True)
    try:
        await openshell_client.import_provider_profiles(_OPENSHELL_CUSTOM_PROFILES)
        log.info("OpenShell provider profiles registered: %s", [p["id"] for p in _OPENSHELL_CUSTOM_PROFILES])
    except Exception:
        log.warning("Failed to import OpenShell provider profiles — sessions may lack Google AI Studio support", exc_info=True)


async def _restart_prompt_pollers() -> None:
    """Re-launch background monitors for prompt sessions still active after a restart."""
    from sqlalchemy import select

    from swarmer.database import get_db
    from swarmer.models.sandbox_env_var import SandboxEnvVar
    from swarmer.models.session import Session

    async for db in get_db():
        result = await db.execute(
            select(Session)
            .where(
                Session.mode == "prompt",
                Session.phase.in_(["pending", "running"]),
                Session.sandbox_name.isnot(None),
            )
        )
        for s in result.scalars().all():
            import shlex as _shlex

            from swarmer.agent_tools.registry import get as _get_tool
            from swarmer.routers.sessions import (
                _resolve_schedule_prompt,
                _resolve_session_prompt,
                _run_openshell_agent,
            )

            # Reconstruct workspace extra env vars (arbitrary key-value pairs stored
            # in the DB and injected into the sandbox at initial launch via
            # exec_command_streaming(env=...).  These are NOT the AI credentials
            # (those live in the OpenShell gateway provider layer, which persists
            # across Swarmer restarts).  Without this, shell/prompt sessions that
            # rely on JIRA_SERVER_URL, GOOGLE_API_KEY, or any other workspace env
            # var would restart with an empty environment and silently fail.
            _ev_result = await db.execute(
                select(SandboxEnvVar).where(SandboxEnvVar.workspace_id == s.workspace_id)
            )
            _env_vars: dict[str, str] = {row.key: row.value for row in _ev_result.scalars().all()}

            _tool = _get_tool(s.agent_tool)
            if s.agent_tool == "shell":
                # Shell tool: reconstruct the command exactly as it was resolved
                # at initial launch. build_main_cmd() at launch time is passed
                # resolved_prompt (instruction_prompt layered with any
                # prompt_id/schedule override — see _resolve_session_prompt /
                # _resolve_schedule_prompt), which can differ from the raw
                # instruction_prompt. Re-resolve the same way here so a restart
                # reruns the identical command rather than a stale or empty one.
                if s.active_schedule_id:
                    _raw_cmd = (await _resolve_schedule_prompt(s.active_schedule_id, s, db)).strip()
                else:
                    _raw_cmd = (await _resolve_session_prompt(s, db)).strip()
                # Security note: _raw_cmd is the re-resolved instruction_prompt injected
                # into a compound sh -c string without sanitisation — intentional by
                # design (sandbox is the security boundary; see sessions.py equivalent).
                _main_cmd = (
                    f"export HOME=/sandbox PATH=\"/sandbox/.local/bin:$PATH\" && "
                    f"cd /sandbox && {_raw_cmd}"
                )
            else:
                _raw_model = s.provider or _tool.get_default_model(False)
                # s.provider is a family preset name ("claude"/"gemini"/"openai", ACM-37232);
                # resolve it to a concrete provider/model@version ID for the CLI flag.
                _model = _tool.resolve_build_model(_raw_model)
                # Reconstruct the same AGENTS.md-reading command used at initial launch
                # (ACM-35060).  build_main_cmd would embed a CLI arg that is unavailable
                # at restart time; AGENTS.md already exists in the sandbox from launch.
                _tool_bin = {"opencode": "opencode run"}.get(s.agent_tool, "opencode run")
                _model_arg = _shlex.quote(_model) if _model else ""
                _main_cmd = f"HOME=/sandbox {_tool_bin} --model {_model_arg} \"$(</sandbox/AGENTS.md)\""
            asyncio.create_task(
                _run_openshell_agent(
                    s.id, s.workspace_id, s.sandbox_name, ["sh", "-c", _main_cmd], s.mode, s.agent_tool,
                    env_vars=_env_vars,
                    pat_id=s.github_pat_id,
                ),
                name=f"openshell-agent-{s.id}",
            )
        break


async def _restart_server_sessions() -> None:
    """Re-establish proxy connections and IAT refresh loops for server/TUI sessions
    still active after a restart.

    For each server/TUI-mode session that was running/pending with a live sandbox:
    - server mode: re-call expose_service() to get a fresh service_url (handles
      gateway restarts). Sessions whose sandbox has disappeared are moved to
      'stopped'.
    - server/TUI mode: if the session is using a workspace GitHub App (no PAT
      assigned), restart the background IAT refresh loop. Swarmer's own restart
      does not affect the sandbox or the OpenShell provider — the previously
      minted IAT is still installed on the gateway and keeps working — but the
      refresh task that would keep it from expiring was an in-process asyncio
      task that died with the old process. Without restarting it here, the App
      IAT silently expires ~1 hour after the Swarmer restart and git push starts
      failing inside an otherwise-healthy sandbox.

    This allows Swarmer to restart while OpenCode continues running in the sandbox
    without losing the proxy connection or GitHub App authentication.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from swarmer import openshell_client
    from swarmer.database import get_db
    from swarmer.models.session import Session
    from swarmer.agent_tools.registry import get as _get_tool

    try:
        default_live_sandboxes = set(await openshell_client.list_sandboxes())
    except Exception:
        log.warning("_restart_server_sessions: could not list sandboxes — skipping", exc_info=True)
        return

    async for db in get_db():
        result = await db.execute(
            select(Session)
            .where(
                Session.mode.in_(["server", "tui"]),
                Session.phase.in_(["pending", "running"]),
                Session.sandbox_name.isnot(None),
            )
            .options(selectinload(Session.github_pat), selectinload(Session.repos))
        )
        sessions = result.scalars().all()
        if not sessions:
            break

        for s in sessions:
            sandbox_name = s.sandbox_name
            oc_client = await openshell_client.get_client_for_workspace(s.workspace_id, db)
            if oc_client is not None:
                try:
                    live_sandboxes = set(await openshell_client.list_sandboxes(client=oc_client))
                except Exception:
                    log.warning("_restart_server_sessions: could not list sandboxes for workspace %d — skipping", s.workspace_id, exc_info=True)
                    continue
            else:
                live_sandboxes = default_live_sandboxes

            if sandbox_name not in live_sandboxes:
                # Sandbox is gone — stop the session cleanly.
                log.warning(
                    "restart: %s-mode session %d sandbox %s not found — marking stopped",
                    s.mode, s.id, sandbox_name,
                )
                s.phase = "stopped"
                s.sandbox_name = None
                s.service_url = None
                s.run_completed_at = datetime.now(timezone.utc)
                continue

            if s.mode == "server":
                # Sandbox is alive — re-expose the service to get a fresh URL.
                try:
                    _tool = _get_tool(s.agent_tool)
                    port = _tool.get_server_port() or 4096
                    service_url = await openshell_client.expose_service(sandbox_name, "agent", port, client=oc_client)
                    s.service_url = service_url
                    s.phase = "running"
                    log.info(
                        "restart: server-mode session %d re-connected to sandbox %s at %s",
                        s.id, sandbox_name, service_url,
                    )
                except Exception:
                    log.warning(
                        "restart: could not re-expose service for session %d sandbox %s — marking stopped",
                        s.id, sandbox_name, exc_info=True,
                    )
                    s.phase = "stopped"
                    s.sandbox_name = None
                    s.service_url = None
                    s.run_completed_at = datetime.now(timezone.utc)
                    continue

            await _restart_github_app_iat_refresh(s, db)

        await db.commit()
        break


async def _restart_github_app_iat_refresh(session: Session, db: AsyncSession) -> None:
    """Restart the IAT refresh background task for a surviving server/TUI session.

    No-op when the session has an explicit PAT assigned, has no GitHub repos, or
    the workspace has no GitHub App configured. Failures are logged and swallowed
    — a missed refresh restart degrades gracefully (existing IAT keeps working
    until it expires) rather than blocking the rest of the restart sequence.
    """
    if session.github_pat:
        return
    _has_github_repos = any("github.com" in (r.repo_url or "") for r in (session.repos or []))
    if not _has_github_repos:
        return

    try:
        from swarmer import openshell_client
        from swarmer.github import github_slug as _github_slug
        from swarmer.github_app import get_workspace_github_app
        from swarmer.github_auth import mint_installation_token, start_token_refresh_loop
        from swarmer.routers.sessions import _github_app_provider_name

        app = await get_workspace_github_app(session.workspace_id, db, user_id="")
        if not app:
            return

        repo_names: list[str] = []
        for r in (session.repos or []):
            if "github.com" not in (r.repo_url or ""):
                continue
            try:
                slug = _github_slug(r.repo_url)
                if slug:
                    repo_names.append(slug.split("/", 1)[1])
            except Exception:
                pass

        oc_client = await openshell_client.get_client_for_workspace(session.workspace_id, db)
        provider_name = _github_app_provider_name(session.workspace_id, session.id)
        iat = await mint_installation_token(app, repo_names=repo_names or None)
        await openshell_client.ensure_provider(
            provider_name, "github", {},
            credentials={"GITHUB_TOKEN": iat, "GH_TOKEN": iat},
            client=oc_client,
        )
        # Keep a strong reference in _iat_refresh_restart_tasks — asyncio's own
        # reference to the task is weak, so without this the task could be
        # garbage-collected before it ever sleeps through its first refresh
        # interval. The done-callback removes it from the set once it finishes
        # (normal completion, exception, or cancellation via session stop/delete).
        refresh_task = asyncio.create_task(
            start_token_refresh_loop(
                app, session.id, provider_name, repo_names=repo_names or None,
                workspace_id=session.workspace_id, client=oc_client,
                resolve_workspace_client=False,
            ),
            name=f"iat-refresh-{session.id}",
        )
        _iat_refresh_restart_tasks.add(refresh_task)
        refresh_task.add_done_callback(_iat_refresh_restart_tasks.discard)
        log.info(
            "restart: re-minted GitHub App IAT and restarted refresh loop for session %d",
            session.id,
        )
    except Exception:
        log.warning(
            "restart: failed to restart GitHub App IAT refresh for session %d — "
            "existing IAT will keep working until it expires",
            session.id, exc_info=True,
        )


app = FastAPI(title="Swarmer", lifespan=lifespan)

# Session middleware must be added before routes are registered
app.add_middleware(
    SessionMiddleware,
    secret_key=derive_session_secret(settings.secret_key_file),
    session_cookie="swarmer_session",
    same_site="lax",
    https_only=False,  # set True in production behind TLS
)

app.mount("/static", StaticFiles(directory="swarmer/static"), name="static")

# Exception handler: redirect to /login when not authenticated
@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request: Request, _exc: NotAuthenticated):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# Routers
app.include_router(auth_router.router)
app.include_router(admins_router.router)
app.include_router(workspaces_router.router)
app.include_router(secrets_router.router)
app.include_router(env_vars_router.router)
app.include_router(mcp_servers_router.router)
app.include_router(prompts_router.router)
app.include_router(sessions_router.router)
app.include_router(office_router.router)
app.include_router(chat_proxy_router.router)
app.include_router(tui_router.router)

# REST API — mounted under /api/v1/
app.include_router(api_v1_router)

templates = Jinja2Templates(directory="swarmer/templates")


@app.get("/")
async def root():
    return RedirectResponse(url="/workspaces", status_code=302)

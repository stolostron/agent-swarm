import logging
from contextlib import asynccontextmanager

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
from swarmer.routers import auth as auth_router
from swarmer.routers import chat_proxy as chat_proxy_router
from swarmer.routers import env_vars as env_vars_router
from swarmer.routers import mcp_servers as mcp_servers_router
from swarmer.routers import prompts as prompts_router
from swarmer.routers import sessions as sessions_router
from swarmer.routers import secrets as secrets_router
from swarmer.routers import tui_ws as tui_router
from swarmer.routers import workspaces as workspaces_router

log = logging.getLogger(__name__)

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
    if settings.openshell_gateway_url:
        await _ensure_openshell_provider_profiles()
    await _restart_prompt_pollers()
    if settings.openshell_gateway_url:
        await _restart_server_sessions()
    from swarmer import scheduler
    scheduler.start_scheduler()
    yield
    await scheduler.shutdown()


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
    import asyncio
    from sqlalchemy import select

    from swarmer.database import get_db
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
            from swarmer.routers.sessions import _run_openshell_agent
            from swarmer.agent_tools.registry import get as _get_tool
            _tool = _get_tool(s.agent_tool)
            _raw_model = s.provider or _tool.get_default_model(False)
            # s.provider is a family preset name ("claude"/"gemini", ACM-37232);
            # resolve it to a concrete provider/model@version ID for the CLI flag.
            _model = _tool.resolve_build_model(_raw_model)
            # Reconstruct the same AGENTS.md-reading command used at initial launch
            # (ACM-35060).  build_main_cmd would embed a CLI arg that is unavailable
            # at restart time; AGENTS.md already exists in the sandbox from launch.
            _tool_bin = {"opencode": "opencode run"}.get(s.agent_tool, "opencode run")
            _model_arg = _shlex.quote(_model) if _model else ""
            _main_cmd = f"HOME=/sandbox {_tool_bin} --model {_model_arg} \"$(</sandbox/AGENTS.md)\""
            asyncio.create_task(
                _run_openshell_agent(s.id, s.sandbox_name, ["sh", "-c", _main_cmd], s.mode, s.agent_tool),
                name=f"openshell-agent-{s.id}",
            )
        break


async def _restart_server_sessions() -> None:
    """Re-establish proxy connections for server-mode sessions still active after a restart.

    For each server-mode session that was running/pending with a live sandbox:
    - Re-call expose_service() to get a fresh service_url (handles gateway restarts).
    - Sessions whose sandbox has disappeared are moved to 'stopped'.

    This allows Swarmer to restart while OpenCode continues running in the sandbox
    without losing the proxy connection.
    """
    from datetime import datetime, timezone
    from sqlalchemy import select

    from swarmer import openshell_client
    from swarmer.database import get_db
    from swarmer.models.session import Session
    from swarmer.agent_tools.registry import get as _get_tool

    try:
        live_sandboxes = set(await openshell_client.list_sandboxes())
    except Exception:
        log.warning("_restart_server_sessions: could not list sandboxes — skipping", exc_info=True)
        return

    async for db in get_db():
        result = await db.execute(
            select(Session).where(
                Session.mode == "server",
                Session.phase.in_(["pending", "running"]),
                Session.sandbox_name.isnot(None),
            )
        )
        sessions = result.scalars().all()
        if not sessions:
            break

        for s in sessions:
            sandbox_name = s.sandbox_name
            if sandbox_name not in live_sandboxes:
                # Sandbox is gone — stop the session cleanly.
                log.warning(
                    "restart: server-mode session %d sandbox %s not found — marking stopped",
                    s.id, sandbox_name,
                )
                s.phase = "stopped"
                s.sandbox_name = None
                s.service_url = None
                s.run_completed_at = datetime.now(timezone.utc)
                continue

            # Sandbox is alive — re-expose the service to get a fresh URL.
            try:
                _tool = _get_tool(s.agent_tool)
                port = _tool.get_server_port() or 4096
                service_url = await openshell_client.expose_service(sandbox_name, "agent", port)
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

        await db.commit()
        break


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
app.include_router(workspaces_router.router)
app.include_router(secrets_router.router)
app.include_router(env_vars_router.router)
app.include_router(mcp_servers_router.router)
app.include_router(prompts_router.router)
app.include_router(sessions_router.router)
app.include_router(chat_proxy_router.router)
app.include_router(tui_router.router)

# REST API — mounted under /api/v1/
app.include_router(api_v1_router)

templates = Jinja2Templates(directory="swarmer/templates")


@app.get("/")
async def root():
    return RedirectResponse(url="/workspaces", status_code=302)

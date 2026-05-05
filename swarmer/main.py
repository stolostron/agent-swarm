from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from swarmer import k8s
from swarmer.config import settings
from swarmer.crypto import derive_session_secret, init_crypto
from swarmer.database import create_tables, migrate_db, init_db
from swarmer.deps import NotAuthenticated
from swarmer.routers import auth as auth_router
from swarmer.routers import chat_proxy as chat_proxy_router
from swarmer.routers import sessions as sessions_router
from swarmer.routers import secrets as secrets_router
from swarmer.routers import tui_ws as tui_router
from swarmer.routers import workspaces as workspaces_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Crypto must be initialised before any DB access (model properties call decrypt)
    init_crypto(settings.secret_key_file)
    init_db(settings.database_url)
    await create_tables()
    await migrate_db()
    k8s.init_k8s(settings.k8s_in_cluster)
    await _restart_prompt_pollers()
    from swarmer import scheduler
    scheduler.start_scheduler()
    yield
    await scheduler.shutdown()
    from swarmer import log_poller
    await log_poller.shutdown()


async def _restart_prompt_pollers() -> None:
    """Re-launch background log pollers for prompt sessions still active after a restart."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from swarmer import log_poller
    from swarmer.database import get_db
    from swarmer.models.session import Session

    async for db in get_db():
        result = await db.execute(
            select(Session)
            .where(
                Session.mode == "prompt",
                Session.phase.in_(["pending", "running"]),
                Session.pod_name.isnot(None),
            )
            .options(selectinload(Session.workspace))
        )
        for s in result.scalars().all():
            log_poller.start_log_poller(s.id, s.pod_name, s.workspace.k8s_namespace)
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
async def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/login", status_code=302)


# Routers
app.include_router(auth_router.router)
app.include_router(workspaces_router.router)
app.include_router(secrets_router.router)
app.include_router(sessions_router.router)
app.include_router(chat_proxy_router.router)
app.include_router(tui_router.router)

templates = Jinja2Templates(directory="swarmer/templates")


@app.get("/")
async def root():
    return RedirectResponse(url="/workspaces", status_code=302)

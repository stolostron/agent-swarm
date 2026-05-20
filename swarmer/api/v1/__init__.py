"""API v1 router — aggregates all resource routers under /api/v1/."""

from fastapi import APIRouter

from swarmer.api.v1 import (
    env_vars,
    mcp_servers,
    prompts,
    repos,
    secrets,
    sessions,
    workspaces,
)

router = APIRouter(prefix="/api/v1", tags=["api-v1"])

router.include_router(workspaces.router)
router.include_router(sessions.router)
router.include_router(repos.router)
router.include_router(secrets.router)
router.include_router(env_vars.router)
router.include_router(mcp_servers.router)
router.include_router(prompts.router)

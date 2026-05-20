"""REST API — Environment variable management."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from swarmer import k8s
from swarmer.api.deps import get_workspace_or_404, require_api_auth
from swarmer.api.schemas import EnvVarCreate, EnvVarOut, MessageOut
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces/{ws_id}/env-vars",
    tags=["env-vars"],
    dependencies=[Depends(require_api_auth)],
)


@router.get("", response_model=list[EnvVarOut])
async def list_env_vars(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
):
    try:
        env_vars = await asyncio.to_thread(k8s.get_extra_env_vars, ws.k8s_namespace)
    except Exception as exc:
        log.warning("Could not read extra env vars for %s: %s", ws.k8s_namespace, exc)
        env_vars = {}

    return [EnvVarOut(key=k, value=v) for k, v in sorted(env_vars.items())]


@router.post("", response_model=MessageOut)
async def add_env_var(
    ws_id: int,
    body: EnvVarCreate,
    ws: Workspace = Depends(get_workspace_or_404),
):
    try:
        await asyncio.to_thread(k8s.ensure_namespace, ws.k8s_namespace)
        await asyncio.to_thread(k8s.set_extra_env_var, ws.k8s_namespace, body.key, body.value)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save variable: {exc}")

    return MessageOut(detail=f"Environment variable '{body.key}' saved.")


@router.delete("/{key}", response_model=MessageOut)
async def delete_env_var(
    ws_id: int,
    key: str,
    ws: Workspace = Depends(get_workspace_or_404),
):
    try:
        await asyncio.to_thread(k8s.delete_extra_env_var, ws.k8s_namespace, key)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete variable: {exc}")

    return MessageOut(detail=f"Environment variable '{key}' deleted.")

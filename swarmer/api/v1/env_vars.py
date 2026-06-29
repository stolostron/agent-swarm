"""REST API — Environment variable management (DB-backed)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.api.deps import get_workspace_or_404, require_api_auth
from swarmer.api.schemas import EnvVarCreate, EnvVarOut, MessageOut
from swarmer.database import get_db
from swarmer.models.workspace import Workspace
from swarmer.models.sandbox_env_var import SandboxEnvVar

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
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SandboxEnvVar)
        .where(SandboxEnvVar.workspace_id == ws_id)
        .order_by(SandboxEnvVar.key)
    )
    rows = result.scalars().all()
    return [EnvVarOut(key=row.key, value=row.value) for row in rows]


@router.post("", response_model=MessageOut)
async def add_env_var(
    ws_id: int,
    body: EnvVarCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    try:
        # Upsert: select-then-update (or insert) so the encrypted property
        # accessor handles Fernet encryption transparently.
        existing = await db.execute(
            select(SandboxEnvVar).where(
                SandboxEnvVar.workspace_id == ws_id,
                SandboxEnvVar.key == body.key,
            )
        )
        row = existing.scalar_one_or_none()
        if row is not None:
            row.value = body.value
        else:
            row = SandboxEnvVar(workspace_id=ws_id, key=body.key)
            row.value = body.value
            db.add(row)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save variable: {exc}")

    return MessageOut(detail=f"Environment variable '{body.key}' saved.")


@router.delete("/{key}", response_model=MessageOut)
async def delete_env_var(
    ws_id: int,
    key: str,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    try:
        await db.execute(
            delete(SandboxEnvVar).where(
                SandboxEnvVar.workspace_id == ws_id,
                SandboxEnvVar.key == key,
            )
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete variable: {exc}")

    return MessageOut(detail=f"Environment variable '{key}' deleted.")

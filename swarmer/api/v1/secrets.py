"""REST API — Agent credentials, GitHub PATs, and pull secrets."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.database import get_db
from swarmer.api.deps import get_current_user, get_workspace_or_404, require_api_auth
from swarmer.api.schemas import (
    CredentialsOut,
    CredentialsSave,
    MessageOut,
    PATCreate,
    PATOut,
    PATUpdate,
    PullSecretCreate,
    PullSecretOut,
)
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.workspace import Workspace

router = APIRouter(
    prefix="/workspaces/{ws_id}/secrets",
    tags=["secrets"],
    dependencies=[Depends(require_api_auth)],
)


# ============================================================
# Credentials (agent AI provider keys)
# ============================================================


@router.get("/credentials", response_model=CredentialsOut | None)
async def get_credentials(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(OpencodeSecret).where(
            OpencodeSecret.workspace_id == ws_id,
            or_(
                OpencodeSecret.user_id == user,
                OpencodeSecret.shared == True,  # noqa: E712
                OpencodeSecret.user_id == "",
            ),
        )
    )
    all_secrets = result.scalars().all()
    secret = None
    for s in all_secrets:
        if s.user_id == user:
            secret = s
            break
    if secret is None and all_secrets:
        secret = all_secrets[0]
    return secret


@router.post("/credentials", response_model=CredentialsOut)
async def save_credentials(
    ws_id: int,
    body: CredentialsSave,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(OpencodeSecret).where(
            OpencodeSecret.workspace_id == ws_id,
            or_(
                OpencodeSecret.user_id == user,
                OpencodeSecret.user_id == "",
            ),
        )
    )
    all_matches = result.scalars().all()
    secret = None
    for s in all_matches:
        if s.user_id == user:
            secret = s
            break
    if secret is None and all_matches:
        secret = all_matches[0]
    if secret is None:
        secret = OpencodeSecret(workspace_id=ws_id, user_id=user)
        db.add(secret)
    elif not secret.user_id:
        secret.user_id = user

    secret.google_cloud_project = body.google_cloud_project.strip()
    secret.vertex_location = body.vertex_location.strip()
    secret.shared = body.shared

    if body.google_api_key.strip():
        secret.google_api_key = body.google_api_key.strip()
    if body.anthropic_api_key.strip():
        secret.anthropic_api_key = body.anthropic_api_key.strip()
    if body.openai_api_key.strip():
        secret.openai_api_key = body.openai_api_key.strip()

    await db.commit()
    await db.refresh(secret)

    # Best-effort K8s sync
    try:
        from swarmer.agent_tools.registry import all_tools
        from swarmer.routers.mcp_servers import get_enabled_mcp_servers
        mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=user)
        for tool in all_tools():
            k8s.apply_agent_config(ws.k8s_namespace, secret=secret, agent_tool=tool.name, mcp_servers=mcp_servers)
    except Exception:
        pass

    return secret


# ============================================================
# GitHub PATs
# ============================================================


@router.get("/pats", response_model=list[PATOut])
async def list_pats(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(GitHubPAT).where(
            GitHubPAT.workspace_id == ws_id,
            or_(
                GitHubPAT.user_id == user,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            ),
        ).order_by(GitHubPAT.name)
    )
    return result.scalars().all()


@router.post("/pats", response_model=PATOut, status_code=status.HTTP_201_CREATED)
async def create_pat(
    ws_id: int,
    body: PATCreate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
):
    pat = GitHubPAT(
        workspace_id=ws_id,
        name=body.name.strip(),
        github_username=body.github_username.strip(),
        github_org=body.github_org.strip(),
        description=body.description.strip(),
        user_id=user,
        shared=body.shared,
    )
    pat.pat = body.pat_value.strip()
    db.add(pat)
    try:
        await db.commit()
        await db.refresh(pat)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A PAT named '{body.name}' already exists in this workspace.",
        )
    return pat


@router.put("/pats/{pat_id}", response_model=PATOut)
async def update_pat(
    ws_id: int,
    pat_id: int,
    body: PATUpdate,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    pat = await db.get(GitHubPAT, pat_id)
    if pat is None or pat.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="PAT not found")

    if body.name is not None:
        pat.name = body.name.strip()
    if body.github_username is not None:
        pat.github_username = body.github_username.strip()
    if body.github_org is not None:
        pat.github_org = body.github_org.strip()
    if body.description is not None:
        pat.description = body.description.strip()
    if body.shared is not None:
        pat.shared = body.shared
    if body.pat_value is not None and body.pat_value.strip():
        pat.pat = body.pat_value.strip()

    try:
        await db.commit()
        await db.refresh(pat)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A PAT with that name already exists")

    return pat


@router.delete("/pats/{pat_id}", response_model=MessageOut)
async def delete_pat(
    ws_id: int,
    pat_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
):
    pat = await db.get(GitHubPAT, pat_id)
    if pat is None or pat.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="PAT not found")

    await db.delete(pat)
    await db.commit()
    return MessageOut(detail="PAT deleted.")


# ============================================================
# Pull Secret
# ============================================================


@router.get("/pull-secret", response_model=PullSecretOut)
async def get_pull_secret(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
):
    try:
        info = k8s.get_pull_secret_info(ws.k8s_namespace)
        if info:
            return PullSecretOut(exists=True, registry=info.get("registry"))
    except Exception:
        pass
    return PullSecretOut(exists=False)


@router.post("/pull-secret", response_model=MessageOut)
async def create_pull_secret(
    ws_id: int,
    body: PullSecretCreate,
    ws: Workspace = Depends(get_workspace_or_404),
):
    try:
        k8s.apply_pull_secret(
            ws.k8s_namespace, body.registry.strip(), body.username.strip(), body.password.strip()
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to create pull secret: {exc}")

    return MessageOut(detail=f"Pull secret saved in namespace {ws.k8s_namespace}.")


@router.delete("/pull-secret", response_model=MessageOut)
async def delete_pull_secret(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
):
    try:
        k8s.delete_pull_secret(ws.k8s_namespace)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete pull secret: {exc}")

    return MessageOut(detail="Pull secret deleted.")

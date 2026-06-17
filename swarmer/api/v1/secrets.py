"""REST API — Agent credentials, GitHub PATs, and pull secrets."""

from __future__ import annotations

import asyncio
import json

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
    GitHubAppOut,
    GitHubAppSave,
    MessageOut,
    PATCreate,
    PATOut,
    PATUpdate,
    PullSecretCreate,
    PullSecretOut,
)
from swarmer.models.github_app import GitHubApp
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

    adc = body.application_default_credentials.strip()
    if adc:
        try:
            json.loads(adc)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="application_default_credentials must be valid JSON",
            ) from exc
        secret.application_default_credentials = adc

    await db.commit()
    await db.refresh(secret)

    # Best-effort K8s sync
    try:
        from swarmer.agent_tools.registry import all_tools
        from swarmer.routers.mcp_servers import get_enabled_mcp_servers
        mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=user)
        await asyncio.to_thread(k8s.sync_all_agent_secrets, ws.k8s_namespace, secret)
        for tool in all_tools():
            await asyncio.to_thread(
                k8s.apply_agent_config,
                ws.k8s_namespace,
                secret=secret,
                agent_tool=tool.name,
                mcp_servers=mcp_servers,
            )
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
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(GitHubPAT).where(
            GitHubPAT.id == pat_id,
            GitHubPAT.workspace_id == ws_id,
            or_(
                GitHubPAT.user_id == user,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            ),
        )
    )
    pat = result.scalar_one_or_none()
    if pat is None:
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
    user: str = Depends(get_current_user),
):
    result = await db.execute(
        select(GitHubPAT).where(
            GitHubPAT.id == pat_id,
            GitHubPAT.workspace_id == ws_id,
            or_(
                GitHubPAT.user_id == user,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            ),
        )
    )
    pat = result.scalar_one_or_none()
    if pat is None:
        raise HTTPException(status_code=404, detail="PAT not found")

    await db.delete(pat)
    await db.commit()
    return MessageOut(detail="PAT deleted.")


# ============================================================
# GitHub App (workspace installation)
# ============================================================


def _github_app_out(app: GitHubApp) -> GitHubAppOut:
    return GitHubAppOut(
        id=app.id,
        workspace_id=app.workspace_id,
        app_id=app.app_id,
        installation_id=app.installation_id,
        has_private_key=bool(app.private_key_enc),
        shared=app.shared,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


@router.get("/github-app", response_model=GitHubAppOut | None)
async def get_github_app(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
) -> GitHubAppOut | None:
    result = await db.execute(
        select(GitHubApp).where(
            GitHubApp.workspace_id == ws_id,
            or_(
                GitHubApp.user_id == user,
                GitHubApp.shared == True,  # noqa: E712
                GitHubApp.user_id == "",
            ),
        )
    )
    apps = result.scalars().all()
    app = None
    for candidate in apps:
        if candidate.user_id == user:
            app = candidate
            break
    if app is None and apps:
        app = apps[0]
    if app is None or not app.is_configured:
        return None
    return _github_app_out(app)


@router.put("/github-app", response_model=GitHubAppOut)
async def save_github_app(
    ws_id: int,
    body: GitHubAppSave,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
) -> GitHubAppOut:
    result = await db.execute(
        select(GitHubApp).where(GitHubApp.workspace_id == ws_id)
    )
    app = result.scalar_one_or_none()
    if app is None:
        app = GitHubApp(workspace_id=ws_id, user_id=user)
        db.add(app)
    elif not app.user_id:
        app.user_id = user
    elif app.user_id != user and not app.shared:
        raise HTTPException(status_code=403, detail="GitHub App is owned by another user")

    app.app_id = body.app_id.strip()
    app.installation_id = body.installation_id.strip()
    app.shared = body.shared
    if body.private_key.strip():
        app.private_key = body.private_key.strip()
    elif not app.private_key_enc:
        raise HTTPException(status_code=400, detail="private_key is required")

    if not app.is_configured:
        raise HTTPException(status_code=400, detail="GitHub App credentials are incomplete")

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="GitHub App already exists for this workspace",
        )
    await db.refresh(app)
    return _github_app_out(app)


@router.delete("/github-app", response_model=MessageOut)
async def delete_github_app(
    ws_id: int,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    user: str = Depends(get_current_user),
) -> MessageOut:
    result = await db.execute(
        select(GitHubApp).where(
            GitHubApp.workspace_id == ws_id,
            or_(
                GitHubApp.user_id == user,
                GitHubApp.shared == True,  # noqa: E712
                GitHubApp.user_id == "",
            ),
        )
    )
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(status_code=404, detail="GitHub App not configured")

    await db.delete(app)
    await db.commit()
    return MessageOut(detail="GitHub App credentials deleted.")


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

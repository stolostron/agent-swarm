"""REST API — Agent credentials, GitHub PATs, and pull secrets."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.database import get_db
from swarmer.k8s_auth import TokenIdentity
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

log = logging.getLogger(__name__)


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
    identity: TokenIdentity = Depends(require_api_auth),
):
    user = identity.username
    from swarmer import workspace_acl

    is_manager = await workspace_acl.can_manage_members(
        db, ws, identity.username, identity.groups
    )

    if body.shared and not is_manager:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace managers can configure shared credentials.",
        )

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
        candidate = all_matches[0]
        if (candidate.shared or candidate.user_id != user) and not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can update shared credentials.",
            )
        secret = candidate
    if secret is None:
        secret = OpencodeSecret(workspace_id=ws_id, user_id=user)
        db.add(secret)
    elif not secret.user_id:
        secret.user_id = user

    if secret.shared and not is_manager:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only workspace managers can update shared credentials.",
        )

    secret.google_cloud_project = body.google_cloud_project.strip()
    secret.vertex_location = body.vertex_location.strip()
    secret.shared = body.shared
    if body.gemini_configured is not None:
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can configure workspace AI providers.",
            )
        secret.gemini_configured = body.gemini_configured
    if body.openai_configured is not None:
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can configure workspace AI providers.",
            )
        secret.openai_configured = body.openai_configured

    if body.google_api_key.strip():
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can configure workspace AI providers.",
            )
        gemini_key = body.google_api_key.strip()
        secret.google_api_key = gemini_key
        secret.gemini_configured = True
        try:
            from swarmer import openshell_client

            await openshell_client.ensure_provider(
                f"swarmer-ws-{ws_id}-google-ai-studio",
                "google-ai-studio",
                {},
                credentials={
                    "GOOGLE_API_KEY": gemini_key,
                    "GOOGLE_GENERATIVE_AI_API_KEY": gemini_key,
                },
            )
        except Exception as exc:
            log.warning(
                "save_credentials: failed to configure Gemini provider for workspace %d (error_type=%s)",
                ws_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="failed to configure Gemini provider on OpenShell",
            ) from exc
    openai_key = body.openai_api_key.strip()
    if openai_key:
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can configure workspace AI providers.",
            )
        secret.openai_configured = True
        # OpenAI key is gateway-only: store/update the workspace-scoped provider
        # on OpenShell, never in Swarmer's DB.
        try:
            from swarmer import openshell_client

            await openshell_client.ensure_provider(
                f"swarmer-ws-{ws_id}-openai",
                "openai",
                {},
                credentials={"OPENAI_API_KEY": openai_key},
            )
        except Exception as exc:
            log.warning(
                "save_credentials: failed to configure OpenAI provider for workspace %d (error_type=%s)",
                ws_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="failed to configure OpenAI provider on OpenShell",
            ) from exc
    adc = body.application_default_credentials.strip()
    if adc:
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can configure workspace AI providers.",
            )
        try:
            json.loads(adc)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="application_default_credentials must be valid JSON",
            ) from exc
        secret.application_default_credentials = adc

    if (
        secret.application_default_credentials_enc
        and secret.google_cloud_project
        and secret.vertex_location
    ):
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace managers can configure workspace AI providers.",
            )
        try:
            from swarmer import openshell_client

            provider_name = f"swarmer-ws-{ws_id}-google-cloud"
            await openshell_client.create_google_cloud_provider(
                provider_name, secret.google_cloud_project, secret.vertex_location
            )
            await openshell_client.configure_google_cloud_provider(
                provider_name, secret.application_default_credentials
            )
        except Exception as exc:
            log.warning(
                "save_credentials: failed to configure Vertex AI provider for workspace %d (error_type=%s)",
                ws_id,
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="failed to configure Vertex AI provider on OpenShell",
            ) from exc

    await db.commit()
    await db.refresh(secret)

    return secret


@router.delete("/credentials/{provider}", response_model=MessageOut)
async def delete_credential(
    ws_id: int,
    provider: str,
    ws: Workspace = Depends(get_workspace_or_404),
    db: AsyncSession = Depends(get_db),
    identity: TokenIdentity = Depends(require_api_auth),
) -> MessageOut:
    user = identity.username
    provider_names = {
        "vertex": "google-cloud",
        "google-cloud": "google-cloud",
        "gemini": "google-ai-studio",
        "google-ai-studio": "google-ai-studio",
        "openai": "openai",
    }
    provider_suffix = provider_names.get(provider.lower())
    if provider_suffix is None:
        raise HTTPException(status_code=400, detail="Unsupported credential provider")

    from swarmer import workspace_acl

    is_manager = await workspace_acl.can_manage_members(
        db, ws, identity.username, identity.groups
    )

    result = await db.execute(
        select(OpencodeSecret).where(
            OpencodeSecret.workspace_id == ws_id,
            or_(OpencodeSecret.user_id == user, OpencodeSecret.shared == True, OpencodeSecret.user_id == ""),  # noqa: E712
        )
    )
    secrets = result.scalars().all()
    secret = next((item for item in secrets if item.user_id == user), None)
    if secret is None and secrets:
        candidate = secrets[0]
        if not is_manager:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the credential owner, workspace owner, or an admin can delete shared credentials.",
            )
        secret = candidate
    if secret is None:
        raise HTTPException(status_code=404, detail="Credentials not configured")

    if secret.shared and not is_manager:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the credential owner, workspace owner, or an admin can delete shared credentials.",
        )

    # OpenShell providers are workspace-scoped (swarmer-ws-{ws_id}-{provider_suffix})
    # and shared by all sessions in the workspace. Only workspace managers (owner or
    # global admin) can detach and delete the workspace-scoped provider on OpenShell.
    if is_manager:
        provider_name = f"swarmer-ws-{ws_id}-{provider_suffix}"
        try:
            from swarmer import openshell_client
            from swarmer.config import settings

            if settings.openshell_gateway_url:
                sandboxes = await openshell_client.list_sandboxes()
                for sandbox_name in sandboxes:
                    await openshell_client.detach_sandbox_provider(sandbox_name, provider_name)
                await openshell_client.delete_provider(provider_name)
        except Exception as exc:
            log.warning("delete_credential: failed to remove provider %s", provider_name, exc_info=True)
            raise HTTPException(status_code=502, detail="failed to delete provider from OpenShell") from exc

    if provider_suffix == "google-cloud":
        secret.google_cloud_project = ""
        secret.vertex_location = ""
        secret.application_default_credentials = ""
    elif provider_suffix == "google-ai-studio":
        secret.google_api_key = ""
        secret.gemini_configured = False
    else:
        secret.openai_api_key_enc = ""
        secret.openai_configured = False

    if not secret.google_cloud_project and not secret.vertex_location and not secret.application_default_credentials_enc and not secret.google_api_key_enc and not secret.openai_api_key_enc and not secret.gemini_configured and not secret.openai_configured:
        await db.delete(secret)
    await db.commit()
    return MessageOut(detail=f"{provider_suffix} credentials deleted.")


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

    # Clean up the OpenShell gateway provider for this PAT before deleting the DB record.
    # Best-effort: log errors but do not block deletion if OpenShell is unavailable.
    provider_name = f"swarmer-ws-{ws_id}-github-pat-{pat_id}"
    try:
        from swarmer import openshell_client
        from swarmer.config import settings
        if settings.openshell_gateway_url:
            sandboxes = await openshell_client.list_sandboxes()
            for sandbox_name in sandboxes:
                await openshell_client.detach_sandbox_provider(sandbox_name, provider_name)
            await openshell_client.delete_provider(provider_name)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to clean up OpenShell provider %s during PAT deletion — continuing",
            provider_name,
            exc_info=True,
        )

    await db.delete(pat)
    await db.commit()
    return MessageOut(detail="PAT deleted.")


# ============================================================
# GitHub App
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
        raise HTTPException(status_code=400, detail="private_key is required on first save")

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
    from swarmer.config import settings

    try:
        # ACM-41659: workspaces no longer get a K8s namespace at creation
        # time — lazily create one here on first use of this legacy
        # per-workspace K8s Secret feature.
        if not settings.k8s_namespace:
            k8s.ensure_namespace(ws.k8s_namespace)
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

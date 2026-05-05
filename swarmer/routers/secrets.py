import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.atlassian_token import AtlassianToken
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.workspace import Workspace

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")

_VALID_TABS = ("credentials", "pats", "pull-secret", "atlassian")


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


async def _secrets_context(ws_id: int, ws, db: AsyncSession) -> dict:
    """Fetch all data needed to render the tabbed secrets page."""
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    opencode_secret = result.scalar_one_or_none()

    pats_result = await db.execute(
        select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()

    atlassian_result = await db.execute(
        select(AtlassianToken).where(AtlassianToken.workspace_id == ws_id)
    )
    atlassian_token = atlassian_result.scalar_one_or_none()

    pull_secret_info = None
    try:
        pull_secret_info = k8s.get_pull_secret_info(ws.k8s_namespace)
    except Exception:
        pass

    return {
        "secret": opencode_secret,
        "pats": pats,
        "atlassian_token": atlassian_token,
        "pull_secret_info": pull_secret_info,
    }


# ============================================================
# Combined tabbed secrets page
# ============================================================

@router.get(
    "/workspaces/{ws_id}/secrets",
    dependencies=[Depends(require_auth)],
)
async def secrets_tabs(
    ws_id: int, request: Request, tab: str = "credentials", db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    if tab not in _VALID_TABS:
        tab = "credentials"

    ctx = await _secrets_context(ws_id, ws, db)
    return templates.TemplateResponse(
        request,
        "secrets/tabs.html",
        {"ws": ws, "tab": tab, **ctx},
    )


# Redirect legacy per-tab GET URLs to the tabbed page
@router.get("/workspaces/{ws_id}/secrets/opencode", dependencies=[Depends(require_auth)])
async def opencode_redirect(ws_id: int):
    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=credentials", status_code=302)


@router.get("/workspaces/{ws_id}/secrets/pats", dependencies=[Depends(require_auth)])
async def pats_redirect(ws_id: int):
    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)


# ============================================================
# OpenCode Secret
# ============================================================

@router.post(
    "/workspaces/{ws_id}/secrets/opencode",
    dependencies=[Depends(require_auth)],
)
async def opencode_secret_save(
    ws_id: int,
    request: Request,
    google_cloud_project: str = Form(""),
    vertex_location: str = Form(""),
    google_api_key: str = Form(""),
    anthropic_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    adc_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    secret = result.scalar_one_or_none()
    if secret is None:
        secret = OpencodeSecret(workspace_id=ws_id)
        db.add(secret)

    secret.google_cloud_project = google_cloud_project.strip()
    secret.vertex_location = vertex_location.strip()

    if google_api_key.strip():
        secret.google_api_key = google_api_key.strip()
    if anthropic_api_key.strip():
        secret.anthropic_api_key = anthropic_api_key.strip()
    if openai_api_key.strip():
        secret.openai_api_key = openai_api_key.strip()

    if adc_file and adc_file.filename:
        content = await adc_file.read()
        try:
            json.loads(content)
        except json.JSONDecodeError:
            ctx = await _secrets_context(ws_id, ws, db)
            ctx["secret"] = secret  # show in-progress values
            return templates.TemplateResponse(
                request,
                "secrets/tabs.html",
                {
                    "ws": ws,
                    "tab": "credentials",
                    "opencode_error": "ADC file must be valid JSON.",
                    **ctx,
                },
                status_code=422,
            )
        secret.application_default_credentials = content.decode()

    await db.commit()

    try:
        k8s.sync_all_agent_secrets(ws.k8s_namespace, secret)
        from swarmer.agent_tools.registry import all_tools
        for tool in all_tools():
            k8s.apply_agent_config(ws.k8s_namespace, secret=secret, agent_tool=tool.name)
    except Exception as exc:
        flash(request, f"Saved, but K8s sync failed: {exc}", "warning")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=credentials", status_code=302)


# ============================================================
# GitHub PATs
# ============================================================

@router.get(
    "/workspaces/{ws_id}/secrets/pats/new",
    dependencies=[Depends(require_auth)],
)
async def github_pat_new(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    return templates.TemplateResponse(
        request,
        "secrets/github_pat_form.html",
        {"ws": ws, "pat": None},
    )


@router.post(
    "/workspaces/{ws_id}/secrets/pats",
    dependencies=[Depends(require_auth)],
)
async def github_pat_create(
    ws_id: int,
    request: Request,
    name: str = Form(...),
    github_username: str = Form(...),
    github_org: str = Form(""),
    pat_value: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    pat = GitHubPAT(
        workspace_id=ws_id,
        name=name.strip(),
        github_username=github_username.strip(),
        github_org=github_org.strip(),
        description=description.strip(),
    )
    pat.pat = pat_value.strip()
    db.add(pat)
    try:
        await db.commit()
        await db.refresh(pat)
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            request,
            "secrets/github_pat_form.html",
            {
                "ws": ws,
                "pat": None,
                "error": f"A PAT named '{name}' already exists in this workspace.",
                "form": {"name": name, "github_username": github_username, "github_org": github_org, "description": description},
            },
            status_code=422,
        )

    try:
        k8s.apply_github_pat_secret(ws.k8s_namespace, pat)
    except Exception as exc:
        flash(request, f"PAT saved, but K8s sync failed: {exc}", "warning")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)


@router.get(
    "/workspaces/{ws_id}/secrets/pats/{pat_id}/edit",
    dependencies=[Depends(require_auth)],
)
async def github_pat_edit_form(
    ws_id: int, pat_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    pat = await db.get(GitHubPAT, pat_id)
    if ws is None or pat is None or pat.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)
    return templates.TemplateResponse(
        request,
        "secrets/github_pat_form.html",
        {"ws": ws, "pat": pat},
    )


@router.post(
    "/workspaces/{ws_id}/secrets/pats/{pat_id}/edit",
    dependencies=[Depends(require_auth)],
)
async def github_pat_update(
    ws_id: int,
    pat_id: int,
    request: Request,
    name: str = Form(...),
    github_username: str = Form(...),
    github_org: str = Form(""),
    pat_value: str = Form(""),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    pat = await db.get(GitHubPAT, pat_id)
    if ws is None or pat is None or pat.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)

    pat.name = name.strip()
    pat.github_username = github_username.strip()
    pat.github_org = github_org.strip()
    pat.description = description.strip()
    if pat_value.strip():
        pat.pat = pat_value.strip()

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, "A PAT with that name already exists.", "danger")
        return RedirectResponse(
            url=f"/workspaces/{ws_id}/secrets/pats/{pat_id}/edit", status_code=302
        )

    try:
        k8s.apply_github_pat_secret(ws.k8s_namespace, pat)
    except Exception as exc:
        flash(request, f"PAT saved, but K8s sync failed: {exc}", "warning")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)


@router.post(
    "/workspaces/{ws_id}/secrets/pats/{pat_id}/delete",
    dependencies=[Depends(require_auth)],
)
async def github_pat_delete(
    ws_id: int,
    pat_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    pat = await db.get(GitHubPAT, pat_id)
    if ws is None or pat is None or pat.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)

    try:
        k8s.delete_github_pat_secret(ws.k8s_namespace, pat)
    except Exception as exc:
        flash(request, f"K8s secret deletion failed: {exc}", "warning")

    await db.delete(pat)
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)


# ============================================================
# Pull Secret
# ============================================================

@router.post(
    "/workspaces/{ws_id}/secrets/pull-secret",
    dependencies=[Depends(require_auth)],
)
async def pull_secret_save(
    ws_id: int,
    request: Request,
    registry: str = Form("quay.io"),
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    try:
        k8s.apply_pull_secret(ws.k8s_namespace, registry.strip(), username.strip(), password.strip())
    except Exception as exc:
        flash(request, f"Failed to create pull secret: {exc}", "danger")
    else:
        flash(
            request,
            f"Pull secret '{k8s.PULL_SECRET_NAME}' saved in namespace {ws.k8s_namespace}.",
            "success",
        )

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pull-secret", status_code=302)


@router.post(
    "/workspaces/{ws_id}/secrets/pull-secret/delete",
    dependencies=[Depends(require_auth)],
)
async def pull_secret_delete(
    ws_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    try:
        k8s.delete_pull_secret(ws.k8s_namespace)
    except Exception as exc:
        flash(request, f"Failed to delete pull secret: {exc}", "warning")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pull-secret", status_code=302)

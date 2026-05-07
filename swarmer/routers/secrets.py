import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer import k8s
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.workspace import Workspace

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")

_VALID_TABS = ("credentials", "pats", "pull-secret")


def _current_user(request: Request) -> str:
    """Return the K8s username from the session, or '' if not set."""
    return request.session.get("username", "")


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


async def _secrets_context(ws_id: int, ws, db: AsyncSession, user_id: str = "") -> dict:
    """Fetch all data needed to render the tabbed secrets page.

    Filters credentials by user_id: shows own credentials + shared + legacy (user_id='').
    """
    result = await db.execute(
        select(OpencodeSecret).where(
            OpencodeSecret.workspace_id == ws_id,
            or_(
                OpencodeSecret.user_id == user_id,
                OpencodeSecret.shared == True,  # noqa: E712
                OpencodeSecret.user_id == "",
            ),
        ).order_by(
            # Prefer own credentials, then legacy, then shared
            OpencodeSecret.user_id == user_id if user_id else OpencodeSecret.id,
        )
    )
    all_secrets = result.scalars().all()
    # Prefer own credentials; fall back to shared/legacy
    opencode_secret = None
    for s in all_secrets:
        if s.user_id == user_id:
            opencode_secret = s
            break
    if opencode_secret is None and all_secrets:
        opencode_secret = all_secrets[0]

    pats_result = await db.execute(
        select(GitHubPAT).where(
            GitHubPAT.workspace_id == ws_id,
            or_(
                GitHubPAT.user_id == user_id,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            ),
        ).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()

    pull_secret_info = None
    try:
        pull_secret_info = k8s.get_pull_secret_info(ws.k8s_namespace)
    except Exception:
        pass

    return {"secret": opencode_secret, "pats": pats, "pull_secret_info": pull_secret_info}


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

    ctx = await _secrets_context(ws_id, ws, db, user_id=_current_user(request))
    return templates.TemplateResponse(
        request,
        "secrets/tabs.html",
        {"ws": ws, "tab": tab, "current_user": _current_user(request), **ctx},
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
    shared: str = Form(""),
    adc_file: UploadFile | None = File(None),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    result = await db.execute(
        select(OpencodeSecret).where(
            OpencodeSecret.workspace_id == ws_id,
            or_(
                OpencodeSecret.user_id == _current_user(request),
                OpencodeSecret.user_id == "",
            ),
        )
    )
    all_matches = result.scalars().all()
    secret = None
    for s in all_matches:
        if s.user_id == _current_user(request):
            secret = s
            break
    if secret is None and all_matches:
        secret = all_matches[0]
    if secret is None:
        secret = OpencodeSecret(workspace_id=ws_id, user_id=_current_user(request))
        db.add(secret)
    elif not secret.user_id:
        secret.user_id = _current_user(request)

    secret.google_cloud_project = google_cloud_project.strip()
    secret.vertex_location = vertex_location.strip()
    secret.shared = bool(shared)

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
            ctx = await _secrets_context(ws_id, ws, db, user_id=_current_user(request))
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
        from swarmer.agent_tools.registry import all_tools
        from swarmer.routers.mcp_servers import get_enabled_mcp_servers
        mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
        for tool in all_tools():
            k8s.apply_agent_config(ws.k8s_namespace, secret=secret, agent_tool=tool.name, mcp_servers=mcp_servers)
    except Exception as exc:
        flash(request, f"Saved, but K8s config sync failed: {exc}", "warning")

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
    shared: str = Form(""),
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
        user_id=_current_user(request),
        shared=bool(shared),
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
    shared: str = Form(""),
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
    pat.shared = bool(shared)
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

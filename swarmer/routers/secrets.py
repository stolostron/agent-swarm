"""Console routes — Secrets management (credentials, PATs, pull secrets).

All data access goes through the REST API client (/api/v1/).
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.routers.api_client import APIError, get_api_client

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")

_VALID_TABS = ("credentials", "pats", "pull-secret")


def _current_user(request: Request) -> str:
    """Return the K8s username from the session, or '' if not set."""
    return request.session.get("username", "")


async def _secrets_context(api, ws_id: int) -> dict:
    """Fetch all data needed to render the tabbed secrets page via API."""
    try:
        secret = await api.get_credentials(ws_id)
    except APIError:
        secret = None

    try:
        pats = await api.list_pats(ws_id)
    except APIError:
        pats = []

    try:
        pull_secret_resp = await api.get_pull_secret(ws_id)
        pull_secret_info = pull_secret_resp if pull_secret_resp.get("exists") else None
    except APIError:
        pull_secret_info = None

    return {"secret": secret, "pats": pats, "pull_secret_info": pull_secret_info}


# ============================================================
# Combined tabbed secrets page
# ============================================================

@router.get(
    "/workspaces/{ws_id}/secrets",
    dependencies=[Depends(require_auth)],
)
async def secrets_tabs(
    ws_id: int, request: Request, tab: str = "credentials",
):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        if tab not in _VALID_TABS:
            tab = "credentials"

        ctx = await _secrets_context(api, ws_id)

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
):
    adc_json = ""
    if adc_file and adc_file.filename:
        import json

        content = await adc_file.read()
        try:
            json.loads(content)
            adc_json = content.decode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            async with get_api_client(request) as api:
                try:
                    ws = await api.get_workspace(ws_id)
                except APIError:
                    return RedirectResponse(url="/workspaces", status_code=302)
                ctx = await _secrets_context(api, ws_id)
            return templates.TemplateResponse(
                request,
                "secrets/tabs.html",
                {
                    "ws": ws,
                    "tab": "credentials",
                    "opencode_error": "ADC file must be valid JSON.",
                    "current_user": _current_user(request),
                    **ctx,
                },
                status_code=422,
            )
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        try:
            await api.save_credentials(
                ws_id,
                google_cloud_project=google_cloud_project,
                vertex_location=vertex_location,
                google_api_key=google_api_key,
                anthropic_api_key=anthropic_api_key,
                openai_api_key=openai_api_key,
                application_default_credentials=adc_json,
                shared=bool(shared),
            )
        except APIError as exc:
            flash(request, f"Failed to save credentials: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=credentials", status_code=302)


# ============================================================
# GitHub PATs
# ============================================================

@router.get(
    "/workspaces/{ws_id}/secrets/pats/new",
    dependencies=[Depends(require_auth)],
)
async def github_pat_new(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
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
):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        try:
            await api.create_pat(
                ws_id,
                name=name.strip(),
                github_username=github_username.strip(),
                github_org=github_org.strip(),
                pat_value=pat_value.strip(),
                description=description.strip(),
                shared=bool(shared),
            )
        except APIError as exc:
            return templates.TemplateResponse(
                request,
                "secrets/github_pat_form.html",
                {
                    "ws": ws,
                    "pat": None,
                    "error": exc.detail,
                    "form": {
                        "name": name,
                        "github_username": github_username,
                        "github_org": github_org,
                        "description": description,
                    },
                },
                status_code=422,
            )

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302)


@router.get(
    "/workspaces/{ws_id}/secrets/pats/{pat_id}/edit",
    dependencies=[Depends(require_auth)],
)
async def github_pat_edit_form(
    ws_id: int, pat_id: int, request: Request,
):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(
                url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302
            )

        # Find the PAT from the list
        pats = await api.list_pats(ws_id)
        pat = None
        for p in pats:
            if p["id"] == pat_id:
                pat = p
                break
        if pat is None:
            return RedirectResponse(
                url=f"/workspaces/{ws_id}/secrets?tab=pats", status_code=302
            )

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
):
    fields: dict = {
        "name": name.strip(),
        "github_username": github_username.strip(),
        "github_org": github_org.strip(),
        "description": description.strip(),
        "shared": bool(shared),
    }
    if pat_value.strip():
        fields["pat_value"] = pat_value.strip()

    async with get_api_client(request) as api:
        try:
            await api.update_pat(ws_id, pat_id, **fields)
        except APIError as exc:
            flash(request, exc.detail, "danger")
            return RedirectResponse(
                url=f"/workspaces/{ws_id}/secrets/pats/{pat_id}/edit",
                status_code=302,
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
):
    async with get_api_client(request) as api:
        try:
            await api.delete_pat(ws_id, pat_id)
        except APIError:
            pass

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
):
    async with get_api_client(request) as api:
        try:
            result = await api.create_pull_secret(
                ws_id, registry.strip(), username.strip(), password.strip()
            )
            flash(request, result.get("detail", "Pull secret saved."), "success")
        except APIError as exc:
            flash(request, f"Failed to create pull secret: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pull-secret", status_code=302)


@router.post(
    "/workspaces/{ws_id}/secrets/pull-secret/delete",
    dependencies=[Depends(require_auth)],
)
async def pull_secret_delete(
    ws_id: int,
    request: Request,
):
    async with get_api_client(request) as api:
        try:
            await api.delete_pull_secret(ws_id)
        except APIError as exc:
            flash(request, f"Failed to delete pull secret: {exc.detail}", "warning")

    return RedirectResponse(url=f"/workspaces/{ws_id}/secrets?tab=pull-secret", status_code=302)

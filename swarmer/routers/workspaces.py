"""Console routes — Workspace management.

All data access goes through the REST API client (/api/v1/).
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer.deps import get_user_token, require_auth
from swarmer.config import settings
from swarmer.flash import flash
from swarmer.k8s_auth import can_create_namespaces
from swarmer.routers.api_client import APIError, get_api_client

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


# ---------- Workspace list ----------

@router.get("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_list(request: Request):
    async with get_api_client(request) as api:
        workspaces = await api.list_workspaces()

    can_create = False
    if not settings.k8s_namespace:
        token = get_user_token(request)
        can_create = await can_create_namespaces(
            token, settings.k8s_api_url, settings.k8s_in_cluster
        )

    return templates.TemplateResponse(
        request,
        "workspaces/list.html",
        {"workspaces": workspaces, "can_create_workspaces": can_create},
    )


# ---------- Namespace preview (HTMX) ----------

@router.get(
    "/workspaces/preview-namespace",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def preview_namespace(name: str = ""):
    import re
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    namespace = slug.strip("-")[:63]
    return HTMLResponse(namespace or "&nbsp;")


# ---------- Create ----------

@router.get("/workspaces/new", dependencies=[Depends(require_auth)])
async def workspace_new(request: Request):
    if settings.k8s_namespace:
        flash(request, "Workspace creation is disabled in this deployment.", "error")
        return RedirectResponse("/workspaces", status_code=302)

    token = get_user_token(request)
    if not await can_create_namespaces(
        token, settings.k8s_api_url, settings.k8s_in_cluster
    ):
        flash(request, "You do not have permission to create workspaces.", "error")
        return RedirectResponse("/workspaces", status_code=302)

    return templates.TemplateResponse(
        request,
        "workspaces/new.html",
    )


@router.post("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_create(
    request: Request,
    display_name: str = Form(...),
    description: str = Form(""),
):
    async with get_api_client(request) as api:
        try:
            ws = await api.create_workspace(display_name, description)
        except APIError as exc:
            return templates.TemplateResponse(
                request,
                "workspaces/new.html",
                {
                    "error": exc.detail,
                    "display_name": display_name,
                    "description": description,
                },
                status_code=exc.status_code,
            )

    flash(request, f"Workspace '{ws['display_name']}' created.", "success")
    return RedirectResponse(url=f"/workspaces/{ws['id']}", status_code=302)


# ---------- Detail ----------

@router.get("/workspaces/{ws_id}", dependencies=[Depends(require_auth)])
async def workspace_detail(ws_id: int):
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)


# ---------- Edit ----------

@router.get("/workspaces/{ws_id}/edit", dependencies=[Depends(require_auth)])
async def workspace_edit_form(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)
    return templates.TemplateResponse(
        request,
        "workspaces/edit.html",
        {"ws": ws},
    )


@router.post("/workspaces/{ws_id}/edit", dependencies=[Depends(require_auth)])
async def workspace_update(
    ws_id: int,
    request: Request,
    display_name: str = Form(...),
    description: str = Form(""),
):
    async with get_api_client(request) as api:
        try:
            await api.update_workspace(ws_id, display_name, description)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)
    flash(request, "Workspace updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}", status_code=302)


# ---------- Delete ----------

@router.get(
    "/workspaces/{ws_id}/delete",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def workspace_delete_confirm(ws_id: int, request: Request):
    """Return an HTMX partial: the inline delete confirmation box."""
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "workspaces/_delete_confirm.html",
        {"ws": ws, "error": None},
    )


@router.post("/workspaces/{ws_id}/delete", dependencies=[Depends(require_auth)])
async def workspace_delete(
    ws_id: int,
    request: Request,
    confirm_name: str = Form(""),
):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        if confirm_name != ws["display_name"]:
            return templates.TemplateResponse(
                request,
                "workspaces/_delete_confirm.html",
                {
                    "ws": ws,
                    "error": "Name does not match. Please type the workspace name exactly.",
                },
            )

        try:
            await api.delete_workspace(ws_id)
        except APIError as exc:
            return templates.TemplateResponse(
                request,
                "workspaces/_delete_confirm.html",
                {
                    "ws": ws,
                    "error": f"Delete failed: {exc.detail}",
                },
            )

    flash(request, f"Workspace '{ws['display_name']}' deleted.", "success")
    return RedirectResponse(url="/workspaces", status_code=302)

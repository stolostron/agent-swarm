"""Console routes — Workspace management.

All data access goes through the REST API client (/api/v1/).
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer.deps import require_auth
from swarmer.config import settings
from swarmer.flash import flash
from swarmer.routers.api_client import APIError, get_api_client

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


# ---------- Workspace list ----------

@router.get("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_list(request: Request):
    async with get_api_client(request) as api:
        workspaces = await api.list_workspaces()
        try:
            me = await api.get_me()
        except APIError:
            me = {}

    can_create = bool(not settings.k8s_namespace and me.get("can_create_workspace"))

    return templates.TemplateResponse(
        request,
        "workspaces/list.html",
        {
            "workspaces": workspaces,
            "can_create_workspaces": can_create,
            "is_admin": me.get("is_admin", False),
            "admin_bootstrap_available": me.get("admin_bootstrap_available", False),
        },
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

    async with get_api_client(request) as api:
        try:
            me = await api.get_me()
        except APIError:
            me = {}

    if not me.get("can_create_workspace"):
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


# ---------- Members (ACM-41659) — database-backed workspace ACL ----------

@router.get("/workspaces/{ws_id}/members", dependencies=[Depends(require_auth)])
async def workspace_members(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)
        try:
            members = await api.list_workspace_members(ws_id)
        except APIError:
            members = []
        try:
            me = await api.get_me()
        except APIError:
            me = {}
        try:
            known_users = await api.list_known_users()
        except APIError:
            known_users = []

    current_user = request.session.get("username", "")
    # Unclaimed workspace (no owner yet): anyone can manage it (and claims it
    # on the first management action — see workspace_acl.claim_ownership_if_unowned).
    can_manage = (
        me.get("is_admin")
        or current_user == ws.get("owner_id")
        or not ws.get("owner_id")
    )

    # Autocomplete suggestions only — free-text entry is always still allowed.
    # Drop people already granted access so we're only suggesting new names.
    already_granted = {ws.get("owner_id")} | {m["user_id"] for m in members}
    known_users = [u for u in known_users if u not in already_granted]

    return templates.TemplateResponse(
        request,
        "workspaces/members.html",
        {"ws": ws, "members": members, "can_manage": can_manage, "known_users": known_users},
    )


@router.post("/workspaces/{ws_id}/members", dependencies=[Depends(require_auth)])
async def workspace_members_add(
    request: Request,
    ws_id: int,
    user_id: str = Form(...),
    role: str = Form("member"),
):
    async with get_api_client(request) as api:
        try:
            await api.add_workspace_member(ws_id, user_id.strip(), role.strip() or "member")
            flash(request, f"'{user_id.strip()}' added to the workspace.", "success")
        except APIError as exc:
            flash(request, f"Failed to add member: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/members", status_code=302)


@router.post(
    "/workspaces/{ws_id}/members/{user_id}/delete", dependencies=[Depends(require_auth)]
)
async def workspace_members_remove(request: Request, ws_id: int, user_id: str):
    async with get_api_client(request) as api:
        try:
            await api.remove_workspace_member(ws_id, user_id)
            flash(request, f"'{user_id}' removed from the workspace.", "success")
        except APIError as exc:
            flash(request, f"Failed to remove member: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/members", status_code=302)

"""Console routes — Global admin management (ACM-41659).

All data access goes through the REST API client (/api/v1/).
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.routers.api_client import APIError, get_api_client

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


@router.get("/admins", dependencies=[Depends(require_auth)])
async def admins_list(request: Request):
    async with get_api_client(request) as api:
        try:
            me = await api.get_me()
        except APIError:
            me = {}

        admins: list[dict] = []
        known_users: list[str] = []
        if me.get("is_admin"):
            try:
                admins = await api.list_admins()
            except APIError:
                admins = []
            try:
                known_users = await api.list_known_users()
            except APIError:
                known_users = []

    # Autocomplete suggestions only — free-text entry is always still allowed.
    already_admins = {a["user_id"] for a in admins}
    known_users = [u for u in known_users if u not in already_admins]

    return templates.TemplateResponse(
        request,
        "admins/list.html",
        {
            "admins": admins,
            "is_admin": me.get("is_admin", False),
            "admin_bootstrap_available": me.get("admin_bootstrap_available", False),
            "known_users": known_users,
        },
    )


@router.post("/admins", dependencies=[Depends(require_auth)])
async def admins_add(request: Request, user_id: str = Form(...)):
    async with get_api_client(request) as api:
        try:
            await api.add_admin(user_id.strip())
            flash(request, f"'{user_id.strip()}' added as an admin.", "success")
        except APIError as exc:
            flash(request, f"Failed to add admin: {exc.detail}", "danger")

    return RedirectResponse(url="/admins", status_code=302)


@router.post("/admins/{user_id}/delete", dependencies=[Depends(require_auth)])
async def admins_remove(request: Request, user_id: str):
    async with get_api_client(request) as api:
        try:
            await api.remove_admin(user_id)
            flash(request, f"'{user_id}' removed from admins.", "success")
        except APIError as exc:
            flash(request, f"Failed to remove admin: {exc.detail}", "danger")

    return RedirectResponse(url="/admins", status_code=302)


@router.post("/admins/bootstrap", dependencies=[Depends(require_auth)])
async def admins_bootstrap(request: Request):
    """One-click self-promotion — only succeeds while zero admins exist."""
    async with get_api_client(request) as api:
        try:
            await api.bootstrap_admin()
            flash(request, "You are now a Swarmer admin.", "success")
        except APIError as exc:
            flash(request, f"Could not become admin: {exc.detail}", "danger")

    return RedirectResponse(url="/admins", status_code=302)

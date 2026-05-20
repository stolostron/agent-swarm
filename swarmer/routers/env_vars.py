"""Console routes — Environment variable management.

All data access goes through the REST API client (/api/v1/).
"""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.routers.api_client import APIError, get_api_client

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")
log = logging.getLogger(__name__)


@router.get("/workspaces/{ws_id}/env-vars", dependencies=[Depends(require_auth)])
async def env_vars_list(request: Request, ws_id: int):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        try:
            env_var_list = await api.list_env_vars(ws_id)
        except APIError:
            env_var_list = []

    # Template expects a dict {key: value} for env_vars.items()
    env_vars = {ev["key"]: ev["value"] for ev in env_var_list}

    return templates.TemplateResponse(
        request,
        "env_vars/list.html",
        {"ws": ws, "env_vars": env_vars},
    )


@router.post("/workspaces/{ws_id}/env-vars", dependencies=[Depends(require_auth)])
async def env_vars_add(
    request: Request,
    ws_id: int,
    key: str = Form(...),
    value: str = Form(...),
):
    import re
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", key):
        flash(
            request,
            "Invalid environment variable name. Must start with a letter or underscore "
            "and contain only letters, digits, and underscores (max 255 characters).",
            "danger",
        )
        return RedirectResponse(url=f"/workspaces/{ws_id}/env-vars", status_code=302)

    async with get_api_client(request) as api:
        try:
            await api.add_env_var(ws_id, key, value)
            flash(request, f"Environment variable '{key}' saved.", "success")
        except APIError as exc:
            flash(request, f"Failed to save variable: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/env-vars", status_code=302)


@router.post("/workspaces/{ws_id}/env-vars/{key}/delete", dependencies=[Depends(require_auth)])
async def env_vars_delete(
    request: Request,
    ws_id: int,
    key: str,
):
    async with get_api_client(request) as api:
        try:
            await api.delete_env_var(ws_id, key)
            flash(request, f"Environment variable '{key}' deleted.", "success")
        except APIError as exc:
            flash(request, f"Failed to delete variable: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/env-vars", status_code=302)

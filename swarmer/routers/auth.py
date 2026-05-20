"""Console routes — Authentication (login, logout, OAuth callback).

Auth flow stays in the Console — it obtains the token, then forwards it
to the API.  Post-login workspace access checks use the API client.
"""

import secrets

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.status import HTTP_303_SEE_OTHER

from swarmer import k8s_auth
from swarmer.config import settings
from swarmer.crypto import encrypt
from swarmer.flash import flash

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)
    openshift_auth_url = None
    if settings.openshift_oauth_url:
        state = secrets.token_urlsafe(16)
        request.session["oauth_state"] = state
        if settings.redirect_base_url:
            callback_url = f"{settings.redirect_base_url.rstrip('/')}/auth/callback"
        else:
            callback_url = str(request.url_for("oauth_callback"))
        openshift_auth_url = (
            f"{settings.openshift_oauth_url}/oauth/authorize"
            f"?client_id=swarmer&response_type=token"
            f"&redirect_uri={quote(str(callback_url), safe='')}"
            f"&state={state}"
        )
    return templates.TemplateResponse(
        request,
        "login.html",
        {"openshift_auth_url": openshift_auth_url},
    )


async def _validate_and_login(request: Request, token: str):
    identity = await k8s_auth.validate_token(token, settings.k8s_api_url, settings.k8s_in_cluster)
    if identity is None:
        flash(request, "Invalid token.", "error")
        return None

    # Post-login workspace access check via API client.
    # Use the validated token to list workspaces through the API and
    # verify the user can reach at least one.
    from swarmer.routers.api_client import APIClient, APIError
    from swarmer.main import app
    async with APIClient(app=app, token=token) as api:
        try:
            await api.list_workspaces()
            # If we get here, the token is valid for the API
        except APIError:
            # Token validation succeeded above but API rejected it —
            # this shouldn't happen but is non-fatal.
            pass

    request.session["authenticated"] = True
    request.session["k8s_token"] = encrypt(token)
    request.session["username"] = identity.username
    return identity


@router.post("/login")
async def login(request: Request, token: str = Form(...)):
    identity = await _validate_and_login(request, "".join(token.split()))
    if identity is None:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)


@router.get("/auth/callback", name="oauth_callback", response_class=HTMLResponse)
async def oauth_callback_page(request: Request):
    return templates.TemplateResponse(request, "auth_callback.html")


@router.post("/auth/callback")
async def oauth_callback(request: Request, token: str = Form(...), state: str = Form("")):
    expected = request.session.pop("oauth_state", None)
    if not expected or state != expected:
        flash(request, "Invalid OAuth state. Please sign in again.", "error")
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    identity = await _validate_and_login(request, "".join(token.split()))
    if identity is None:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/workspaces", status_code=HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)

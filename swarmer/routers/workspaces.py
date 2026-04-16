import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from swarmer import k8s
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.workspace import Workspace

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


def _derive_namespace(display_name: str) -> str:
    slug = display_name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")[:63]


# ---------- Workspace list ----------

@router.get("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_list(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Workspace).order_by(Workspace.display_name))
    workspaces = result.scalars().all()
    return templates.TemplateResponse(
        "workspaces/list.html", {"request": request, "workspaces": workspaces}
    )


# ---------- Workspace list rows (HTMX polling partial) ----------

@router.get(
    "/workspaces/rows",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def workspace_list_rows(request: Request, db: AsyncSession = Depends(get_db)):
    """Return only the tbody rows for the workspaces table (HTMX polling partial)."""
    result = await db.execute(select(Workspace).order_by(Workspace.display_name))
    workspaces = result.scalars().all()
    return templates.TemplateResponse(
        "workspaces/_list_rows.html",
        {"request": request, "workspaces": workspaces},
    )


# ---------- Namespace preview (HTMX) ----------

@router.get(
    "/workspaces/preview-namespace",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def preview_namespace(name: str = ""):
    return HTMLResponse(_derive_namespace(name) or "&nbsp;")


# ---------- Create ----------

@router.get("/workspaces/new", dependencies=[Depends(require_auth)])
async def workspace_new(request: Request):
    return templates.TemplateResponse(
        "workspaces/new.html", {"request": request}
    )


@router.post("/workspaces", dependencies=[Depends(require_auth)])
async def workspace_create(
    request: Request,
    display_name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    namespace = _derive_namespace(display_name)
    if not namespace:
        return templates.TemplateResponse(
            "workspaces/new.html",
            {"request": request, "error": "Display name must contain at least one alphanumeric character."},
            status_code=422,
        )

    ws = Workspace(
        display_name=display_name.strip(),
        namespace=namespace,
        description=description.strip(),
    )
    db.add(ws)
    try:
        await db.commit()
        await db.refresh(ws)
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            "workspaces/new.html",
            {
                "request": request,
                "error": f"A workspace with namespace '{namespace}' already exists.",
                "display_name": display_name,
                "description": description,
            },
            status_code=422,
        )

    # Best-effort: create K8s namespace and default opencode config
    try:
        k8s.ensure_namespace(namespace)
        k8s.apply_opencode_config(namespace)
    except Exception as exc:
        flash(request, f"Workspace created but K8s namespace creation failed: {exc}", "warning")

    flash(request, f"Workspace '{ws.display_name}' created.", "success")
    return RedirectResponse(url=f"/workspaces/{ws.id}", status_code=302)


# ---------- Detail ----------

@router.get("/workspaces/{ws_id}", dependencies=[Depends(require_auth)])
async def workspace_detail(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from swarmer.models.session import Session

    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    ns_status = k8s.get_namespace_status(ws.namespace)
    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat))
        .order_by(Session.name)
    )
    sessions = result.scalars().all()
    return templates.TemplateResponse(
        "workspaces/detail.html",
        {"request": request, "ws": ws, "ns_status": ns_status, "sessions": sessions},
    )


# ---------- Workspace detail sessions rows (HTMX polling partial) ----------

@router.get(
    "/workspaces/{ws_id}/sessions/rows",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def workspace_sessions_rows(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return only the session tbody rows for the workspace detail page (HTMX polling partial)."""
    from swarmer.models.session import Session

    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return HTMLResponse("")
    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat))
        .order_by(Session.name)
    )
    sessions = result.scalars().all()
    return templates.TemplateResponse(
        "workspaces/_sessions_table.html",
        {"request": request, "ws": ws, "sessions": sessions},
    )


# ---------- Edit ----------

@router.get("/workspaces/{ws_id}/edit", dependencies=[Depends(require_auth)])
async def workspace_edit_form(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    return templates.TemplateResponse(
        "workspaces/edit.html", {"request": request, "ws": ws}
    )


@router.post("/workspaces/{ws_id}/edit", dependencies=[Depends(require_auth)])
async def workspace_update(
    ws_id: int,
    request: Request,
    display_name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    ws.display_name = display_name.strip()
    ws.description = description.strip()
    await db.commit()
    flash(request, "Workspace updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}", status_code=302)


# ---------- Delete ----------

@router.get(
    "/workspaces/{ws_id}/delete",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def workspace_delete_confirm(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return an HTMX partial: the inline delete confirmation box."""
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "workspaces/_delete_confirm.html",
        {"request": request, "ws": ws, "error": None},
    )


@router.post("/workspaces/{ws_id}/delete", dependencies=[Depends(require_auth)])
async def workspace_delete(
    ws_id: int,
    request: Request,
    confirm_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await db.get(Workspace, ws_id)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    if confirm_name != ws.display_name:
        return templates.TemplateResponse(
            "workspaces/_delete_confirm.html",
            {
                "request": request,
                "ws": ws,
                "error": "Name does not match. Please type the workspace name exactly.",
            },
        )

    # Delete K8s namespace first; abort if it fails for a non-404 reason
    try:
        k8s.delete_namespace(ws.namespace)
    except Exception as exc:
        return templates.TemplateResponse(
            "workspaces/_delete_confirm.html",
            {
                "request": request,
                "ws": ws,
                "error": f"Kubernetes error: {exc}",
            },
        )

    await db.delete(ws)
    await db.commit()
    flash(request, f"Workspace '{ws.display_name}' deleted.", "success")
    return RedirectResponse(url="/workspaces", status_code=302)

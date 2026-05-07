import asyncio
import logging
import re
import shlex
import uuid
from datetime import datetime

import httpx

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer import k8s, k8s_session as k8s_sess
from swarmer.agent_tools.registry import get as get_tool, all_tools
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.ansi import ansi_to_html
from swarmer.flash import flash
from swarmer.github import fetch_repo_info as _fetch_repo_info
from swarmer.github import list_repos_for_pat as _list_repos_for_pat
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.session import CRON_PRESETS, Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.workspace import Workspace

log = logging.getLogger(__name__)

_INVALID_REF_RE = re.compile(
    r"[\x00-\x1f\x7f ~^:?*\[\\]"
    r"|\.\.+"
    r"|@\{"
    r"|\.$"
    r"|\.lock$"
    r"|//"
)


def _is_valid_ref_name(name: str) -> bool:
    """Check whether *name* is a legal git ref component."""
    if not name or name.startswith("/") or name.endswith("/") or name.endswith("."):
        return False
    return _INVALID_REF_RE.search(name) is None


async def _get_model_options(
    ws_id: int, db: AsyncSession, agent_tool: str = "opencode"
) -> list[dict]:
    """Return the available model choices for this workspace's sessions."""
    tool = get_tool(agent_tool)
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc = result.scalar_one_or_none()
    return tool.get_model_options(oc)

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")
templates.env.filters['ansi_to_html'] = ansi_to_html


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


# ============================================================
# Model options (HTMX partial — reloads when agent tool changes)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/model-options",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def model_options_partial(
    ws_id: int,
    request: Request,
    agent_tool: str = "opencode",
    selected_model: str = "",
    db: AsyncSession = Depends(get_db),
):
    model_options = await _get_model_options(ws_id, db, agent_tool)
    return templates.TemplateResponse(
        request,
        "sessions/_model_select.html",
        {
            "model_options": model_options,
            "selected_model": selected_model,
        },
    )


def _session_mode_label(session: Session) -> str:
    """Return a human-readable mode label for the session."""
    return {"tui": "TUI", "server": "Server", "prompt": "Prompt"}.get(session.mode, session.mode)


def _session_mode_badge_class(session: Session) -> str:
    return {"tui": "primary", "server": "info", "prompt": "secondary"}.get(session.mode, "secondary")


# ============================================================
# Session list
# ============================================================

async def _list_sessions_data(ws_id: int, db: AsyncSession):
    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat), selectinload(Session.repos))
        .order_by(Session.name)
    )
    return result.scalars().all()


async def _sync_session_phases(sessions, ws, db: AsyncSession):
    dirty = False
    for s in sessions:
        if s.phase in ("pending", "running") and s.pod_name:
            live_phase, _ = await asyncio.to_thread(k8s.get_pod_status, s.pod_name, ws.k8s_namespace)
            if live_phase != s.phase:
                s.phase = live_phase
                dirty = True
    if dirty:
        await db.commit()


@router.get("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_list(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    sessions = await _list_sessions_data(ws_id, db)
    await _sync_session_phases(sessions, ws, db)

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    return templates.TemplateResponse(
        request,
        "sessions/list.html",
        {
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
        },
    )


@router.get(
    "/workspaces/{ws_id}/sessions/rows",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_list_rows(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return HTMLResponse("")

    sessions = await _list_sessions_data(ws_id, db)
    await _sync_session_phases(sessions, ws, db)

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    return templates.TemplateResponse(
        request,
        "sessions/_list_rows.html",
        {
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
        },
    )


# ============================================================
# Create
# ============================================================

@router.get("/workspaces/{ws_id}/sessions/new", dependencies=[Depends(require_auth)])
async def session_new(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    pats_result = await db.execute(
        select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()
    _tools = all_tools()
    try:
        default_agent_tool = get_tool(settings.default_agent_tool).name
    except ValueError:
        default_agent_tool = "opencode"
    model_options = await _get_model_options(ws_id, db, default_agent_tool)
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    mcp_servers = await get_enabled_mcp_servers(ws_id, db)
    return templates.TemplateResponse(
        request,
        "sessions/new.html",
        {
            "ws": ws,
            "pats": pats,
            "model_options": model_options,
            "selected_model": "",
            "agent_tools": _tools,
            "default_agent_tool": default_agent_tool,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "mcp_servers": mcp_servers,
        },
    )


@router.post("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_create(
    ws_id: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    resume: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    agent_tool: str = Form("opencode"),
    working_branch: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    pat_id = int(github_pat_id) if github_pat_id else None
    if mode not in ("tui", "server", "prompt"):
        mode = "prompt"

    try:
        agent_tool = get_tool(agent_tool).name
    except ValueError:
        agent_tool = "opencode"

    if not model.strip():
        opts = await _get_model_options(ws_id, db, agent_tool)
        model = opts[0]["value"] if opts else ""

    wb = working_branch.strip()
    if wb and not _is_valid_ref_name(wb):
        flash(request, "Invalid working branch name.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

    session = Session(
        workspace_id=ws_id,
        github_pat_id=pat_id,
        name=name.strip(),
        mode=mode,
        model=model.strip(),
        persist=persist,
        resume=resume,
        instruction_prompt=instruction_prompt.strip(),
        agent_tool=agent_tool,
        working_branch=wb,
    )
    # Gather MCP server checkbox selections from the multi-value form field
    form_data = await request.form()
    selected_mcp_ids = [int(v) for v in form_data.getlist("mcp_server_ids") if str(v).isdigit()]
    if selected_mcp_ids:
        session.enabled_mcp_ids = selected_mcp_ids
    else:
        session.mcp_server_ids = "none"
    db.add(session)
    try:
        await db.commit()
        await db.refresh(session)
        if not session.working_branch:
            import secrets as _secrets
            session.working_branch = f"swarmer/session-{session.id}-{_secrets.token_hex(4)}"
            await db.commit()
    except IntegrityError:
        await db.rollback()
        pats_result = await db.execute(
            select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id)
        )
        pats = pats_result.scalars().all()
        _tools = all_tools()
        try:
            default_agent_tool = get_tool(settings.default_agent_tool).name
        except ValueError:
            default_agent_tool = "opencode"
        model_options = await _get_model_options(ws_id, db, default_agent_tool)
        _avail = await asyncio.gather(
            *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
        )
        return templates.TemplateResponse(
            request,
            "sessions/new.html",
            {
                "ws": ws,
                "pats": pats,
                "error": f"A session named '{name}' already exists in this workspace.",
                "form": {"name": name, "instruction_prompt": instruction_prompt},
                "model_options": model_options,
                "selected_model": model,
                "agent_tools": _tools,
                "default_agent_tool": default_agent_tool,
                "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            },
            status_code=422,
        )

    await db.commit()

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{session.id}", status_code=302)


# ============================================================
# Detail
# ============================================================

@router.get("/workspaces/{ws_id}/sessions/{sid}", dependencies=[Depends(require_auth)])
async def session_detail(
    ws_id: int, sid: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.github_pat), selectinload(Session.repos)],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    pats_result = await db.execute(
        select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()

    # Fetch live K8s detail for the initial page render and sync phase
    status_detail = ""
    if session.pod_name:
        live_phase, status_detail = await asyncio.to_thread(k8s.get_pod_status, session.pod_name, ws.k8s_namespace)
        if live_phase != session.phase:
            session.phase = live_phase
            await db.commit()

    # Generate one-time TUI token after K8s sync so phase is current
    tui_token = None
    if session.mode == "tui" and session.phase == "running":
        tui_token = str(uuid.uuid4())
        tokens = list(request.session.get("tui_tokens", []))
        tokens.append(tui_token)
        request.session["tui_tokens"] = tokens

    model_options = await _get_model_options(ws_id, db, session.agent_tool)
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    try:
        canonical_agent_tool = get_tool(session.agent_tool).name
    except ValueError:
        canonical_agent_tool = session.agent_tool
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    mcp_servers = await get_enabled_mcp_servers(ws_id, db)
    return templates.TemplateResponse(
        request,
        "sessions/detail.html",
        {
            "ws": ws,
            "ws_id": ws_id,
            "session": session,
            "canonical_agent_tool": canonical_agent_tool,
            "pats": pats,
            "tui_token": tui_token,
            "mode_label": _session_mode_label(session),
            "mode_badge": _session_mode_badge_class(session),
            "status_detail": status_detail,
            "model_options": model_options,
            "repo_info": repo_info,
            "agent_tools": _tools,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "patch_filename": _patch_filename(session),
            "cron_presets": CRON_PRESETS,
            "mcp_servers": mcp_servers,
        },
    )


# ============================================================
# Edit
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/edit",
    dependencies=[Depends(require_auth)],
)
async def session_edit(
    ws_id: int,
    sid: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    resume: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    agent_tool: str = Form("opencode"),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Cannot edit a running session. Stop it first.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    session.name = name.strip()
    session.github_pat_id = int(github_pat_id) if github_pat_id else None
    session.instruction_prompt = instruction_prompt.strip()
    session.persist = persist
    session.resume = resume
    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    session.model = model.strip()
    try:
        session.agent_tool = get_tool(agent_tool).name
    except ValueError:
        pass

    form_data = await request.form()
    if "working_branch" in form_data:
        branch_val = form_data["working_branch"].strip()
        if branch_val and not _is_valid_ref_name(branch_val):
            flash(request, "Invalid working branch name.", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)
        session.working_branch = branch_val

    selected_mcp_ids = [int(v) for v in form_data.getlist("mcp_server_ids") if str(v).isdigit()]
    if selected_mcp_ids:
        session.enabled_mcp_ids = selected_mcp_ids
    else:
        session.mcp_server_ids = "none"

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, f"A session named '{name}' already exists in this workspace.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    flash(request, "Session updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Launch / Stop
# ============================================================

async def _do_launch(session: Session, ws: Workspace, db: AsyncSession) -> None:
    """Core launch logic shared by the HTTP endpoint and the background scheduler."""
    import secrets as _secrets
    session.last_output = ""

    suffix = _secrets.token_hex(4)

    pvc_name = await asyncio.to_thread(
        k8s_sess.ensure_session_pvc, ws.k8s_namespace, session.id, suffix, session.pvc_name,
    )
    if pvc_name != session.pvc_name:
        session.pvc_name = pvc_name

    from swarmer import k8s as _k8s
    await asyncio.to_thread(_k8s._grant_anyuid_scc, ws.k8s_namespace)

    from swarmer.models.opencode_secret import OpencodeSecret
    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == session.workspace_id)
    )
    oc_secret = oc_result.scalar_one_or_none()
    has_adc = oc_secret.has_adc if oc_secret else False
    has_gemini = bool(oc_secret and oc_secret.google_api_key_enc)

    # Fetch enabled & authenticated MCP servers for this workspace
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    all_ws_mcp = await get_enabled_mcp_servers(session.workspace_id, db)
    ws_mcp_servers = [s for s in all_ws_mcp if s.auth_status != "expired"]

    # Filter to only the MCP servers enabled for this specific session
    # mcp_servers=None  → no MCP configured in workspace (skip override)
    # mcp_servers=[]    → user explicitly disabled all (override with clean config)
    # mcp_servers=[...] → user selected specific servers
    if not all_ws_mcp:
        mcp_servers = None
    elif session.mcp_server_ids == "none":
        mcp_servers = []
    elif session.enabled_mcp_ids:
        enabled_ids = set(session.enabled_mcp_ids)
        mcp_servers = [s for s in ws_mcp_servers if s.id in enabled_ids]
    else:
        # No MCP selection stored — default to all workspace-enabled servers
        mcp_servers = ws_mcp_servers

    # Sync MCP server tokens to K8s secret and update agent config
    if mcp_servers:
        from swarmer import k8s as _k8s2
        try:
            await asyncio.to_thread(_k8s2.sync_mcp_server_secret, ws.k8s_namespace, mcp_servers)
            await asyncio.to_thread(
                _k8s2.apply_agent_config, ws.k8s_namespace,
                secret=oc_secret, agent_tool=session.agent_tool, mcp_servers=mcp_servers,
            )
        except Exception:
            log.warning("MCP server sync failed, continuing launch without MCP", exc_info=True)
            mcp_servers = []

    pod_spec = k8s_sess.build_session_pod(
        session=session,
        namespace=ws.k8s_namespace,
        image=settings.agent_image,
        suffix=suffix,
        image_pull_secret=k8s.PULL_SECRET_NAME,
        has_adc=has_adc,
        has_gemini=has_gemini,
        privileged=session.privileged,
        agent_tool=session.agent_tool,
        mcp_servers=mcp_servers,
    )
    from kubernetes import client as k8s_client

    v1 = k8s_client.CoreV1Api()

    if session.pod_name:
        await asyncio.to_thread(k8s.delete_pod, session.pod_name, ws.k8s_namespace)

    pod = await asyncio.to_thread(v1.create_namespaced_pod, ws.k8s_namespace, pod_spec)
    session.pod_name = pod.metadata.name
    session.phase = "pending"
    session.run_started_at = datetime.utcnow()
    session.run_completed_at = None

    if session.mode == "server":
        tool = get_tool(session.agent_tool)
        port = tool.get_server_port() or 4096
        svc_name = await asyncio.to_thread(
            k8s_sess.create_session_service, session.id, ws.k8s_namespace, port,
        )
        await asyncio.to_thread(
            k8s.create_session_route, session.id, ws.k8s_namespace, svc_name, port,
        )

    await db.commit()
    if session.mode in ("prompt", "server"):
        from swarmer import log_poller
        log_poller.start_log_poller(session.id, session.pod_name, ws.k8s_namespace, session.mode)


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/launch",
    dependencies=[Depends(require_auth)],
)
async def session_launch(
    ws_id: int,
    sid: int,
    request: Request,
    agent_tool: str = Form(""),
    save_config: str = Form(""),
    name: str = Form(""),
    github_pat_id: str = Form(""),
    instruction_prompt: str = Form(""),
    persist: bool = Form(False),
    resume: bool = Form(False),
    mode: str = Form(""),
    model: str = Form(""),
    redirect_to: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.github_pat), selectinload(Session.repos)],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if save_config:
        if name.strip():
            session.name = name.strip()
        session.github_pat_id = int(github_pat_id) if github_pat_id else None
        session.instruction_prompt = instruction_prompt.strip()
        session.persist = persist
        session.resume = resume
        if mode in ("tui", "server", "prompt"):
            session.mode = mode
        if model.strip():
            session.model = model.strip()

    if agent_tool:
        try:
            canonical = get_tool(agent_tool).name
            if canonical != session.agent_tool:
                session.agent_tool = canonical
                session.model = ""  # stale model from previous tool may be incompatible
        except ValueError:
            pass

    try:
        await _do_launch(session, ws, db)
    except Exception as exc:
        log.error("session_launch failed for session %d: %s", sid, exc, exc_info=True)
        flash(request, f"Launch failed: {exc}", "danger")

    dest = (
        f"/workspaces/{ws_id}/sessions"
        if redirect_to == "list"
        else f"/workspaces/{ws_id}/sessions/{sid}"
    )
    return RedirectResponse(url=dest, status_code=302)


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/stop",
    dependencies=[Depends(require_auth)],
)
async def session_stop(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.pod_name:
        from swarmer import log_poller
        log_poller.stop_log_poller(sid)
        try:
            k8s.delete_pod(session.pod_name, ws.k8s_namespace)
            if session.mode == "server":
                k8s.delete_service(f"session-{session.id}-svc", ws.k8s_namespace)
                k8s.delete_session_route(session.id, ws.k8s_namespace)
        except Exception as exc:
            flash(request, f"Pod deletion failed: {exc}", "warning")

    if not session.persist and session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
            session.pvc_name = None
        except Exception as exc:
            flash(request, f"PVC deletion failed: {exc}", "warning")

    session.run_completed_at = datetime.utcnow()
    session.phase = "stopped"
    session.pod_name = None
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Schedule / Unschedule
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/schedule",
    dependencies=[Depends(require_auth)],
)
async def session_schedule(
    ws_id: int,
    sid: int,
    request: Request,
    cron_expr: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    from croniter import croniter

    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    cron_expr = cron_expr.strip()
    if not cron_expr:
        flash(request, "Cron expression is required.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    if len(cron_expr) > 128:
        flash(request, "Cron expression is too long (max 128 characters).", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    if session.mode != "prompt":
        flash(request, "Scheduling is only supported for prompt-mode sessions.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    if not croniter.is_valid(cron_expr):
        flash(request, f"Invalid cron expression: {cron_expr}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    session.cron_schedule = cron_expr
    session.cron_next_run = croniter(cron_expr, datetime.utcnow()).get_next(datetime)
    await db.commit()

    flash(request, f"Schedule set: {session.cron_label or cron_expr}. Next run: {session.cron_next_run.strftime('%b %d %H:%M UTC')}", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/unschedule",
    dependencies=[Depends(require_auth)],
)
async def session_unschedule(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.cron_schedule = ""
    session.cron_next_run = None
    await db.commit()

    flash(request, "Schedule cancelled.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)


# ============================================================
# Status polling (HTMX)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/status",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_status(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None:
        return HTMLResponse("")

    # Sync live pod phase into the DB so the badge reflects actual K8s state.
    # The log_poller handles this for prompt mode; for TUI/server we do it here
    # on each poll so the JS handler can detect the running→Active transition.
    status_detail = session.status_detail
    if session.pod_name and session.phase in ("pending", "running"):
        live_phase, live_detail = await asyncio.to_thread(
            k8s.get_pod_status, session.pod_name, ws.k8s_namespace
        )
        if live_phase != session.phase or live_detail != session.status_detail:
            session.phase = live_phase
            session.status_detail = live_detail
            await db.commit()
        status_detail = live_detail

    return templates.TemplateResponse(
        request,
        "sessions/_status_badge.html",
        {
            "ws": ws,
            "session": session,
            "mode_label": _session_mode_label(session),
            "status_detail": status_detail,
        },
    )


# ============================================================
# Last-output polling (HTMX)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/last-output",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_last_output(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request,
        "sessions/_last_output.html",
        {"ws_id": ws_id, "session": session},
    )


# ============================================================
# Clear output
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/clear-output",
    dependencies=[Depends(require_auth)],
)
async def session_clear_output(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.last_output = ""
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Delete
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/delete",
    dependencies=[Depends(require_auth)],
)
async def session_delete(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Stop the session before deleting it.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.k8s_namespace, session.pvc_name)
        except Exception as exc:
            flash(request, f"PVC deletion failed: {exc}", "warning")

    await db.delete(session)
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)


# ============================================================
# Set name (inline rename on detail page)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-name",
    dependencies=[Depends(require_auth)],
)
async def session_set_name(
    ws_id: int,
    sid: int,
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Cannot rename a running session. Stop it first.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    session.name = name.strip()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, f"A session named '{name}' already exists in this workspace.", "danger")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Set mode (inline dropdown on detail page)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-mode",
    dependencies=[Depends(require_auth)],
)
async def session_set_mode(
    ws_id: int,
    sid: int,
    request: Request,
    mode: str = Form("run"),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if session.is_active:
        flash(request, "Cannot change mode while session is active. Stop it first.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Set model (server / TUI modes — works while running)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-model",
    dependencies=[Depends(require_auth)],
)
async def session_set_model(
    ws_id: int,
    sid: int,
    request: Request,
    model: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.model = model.strip()
    await db.commit()

    if session.is_active and session.pod_name:
        try:
            tool = get_tool(session.agent_tool)
            tool.exec_model_update(session.pod_name, ws.k8s_namespace, session.model)
            flash(request, "Model applied to running pod.", "success")
        except Exception as exc:
            flash(request, f"Model saved but could not apply to running pod: {exc}", "warning")
    else:
        flash(request, "Model saved; will apply on next launch.", "success")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Git Repos
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/repos",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_add(
    ws_id: int,
    sid: int,
    request: Request,
    repo_url: str = Form(...),
    branch: str = Form("main"),
    local_path: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(Session, sid, options=[selectinload(Session.repos)])
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    if session.is_active:
        return HTMLResponse("", status_code=409)

    if not local_path:
        local_path = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    repo = SessionRepo(
        session_id=sid,
        repo_url=repo_url.strip(),
        branch=branch.strip() or "main",
        local_path=local_path.strip(),
    )
    db.add(repo)
    await db.commit()

    return HTMLResponse("", headers={"HX-Trigger": "repoListChanged"})


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/repos/{rid}/delete",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_delete(
    ws_id: int,
    sid: int,
    rid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    session_check = await db.get(Session, sid)
    if session_check is None or session_check.workspace_id != ws_id:
        return HTMLResponse("")
    if session_check.is_active:
        return HTMLResponse("", status_code=409)

    repo = await db.get(SessionRepo, rid)
    if repo and repo.session_id == sid:
        await db.delete(repo)
        await db.commit()

    session = await db.get(Session, sid, options=[selectinload(Session.repos), selectinload(Session.github_pat)])
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    return HTMLResponse("", headers={"HX-Trigger": "repoListChanged"})


# ============================================================
# Patch generation / download
# ============================================================

def _patch_filename(session: Session) -> str:
    """Build a safe filename for the downloadable patch."""
    safe_name = re.sub(r'[^\w\-.]', '_', session.name)[:80]
    return f"session-{session.id}-{safe_name}.patch"


def _prefix_diff_paths(diff: str, prefix: str) -> str:
    """Rewrite diff header paths to include the repo subdirectory prefix."""
    lines = diff.split("\n")
    out: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            line = re.sub(r" a/", f" a/{prefix}/", line, count=1)
            line = re.sub(r" b/", f" b/{prefix}/", line, count=1)
        elif line.startswith("--- a/"):
            line = f"--- a/{prefix}/{line[6:]}"
        elif line.startswith("+++ b/"):
            line = f"+++ b/{prefix}/{line[6:]}"
        out.append(line)
    return "\n".join(out)


def _exec_in_pod(
    pod_name: str,
    namespace: str,
    workdir: str,
    command: list[str],
    container: str = "opencode",
) -> str:
    """Run a command in a running pod and return its stdout.

    Raises RuntimeError if the command exits with a non-zero status.
    """
    from kubernetes import client
    from kubernetes.stream import stream

    v1 = client.CoreV1Api()
    full_cmd = ["sh", "-c", f"cd {shlex.quote(workdir)} && {shlex.join(command)}"]
    resp = stream(
        v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=full_cmd,
        container=container,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
        _preload_content=False,
    )
    stdout_data = ""
    stderr_data = ""
    while resp.is_open():
        resp.update(timeout=5)
        if resp.peek_stdout():
            stdout_data += resp.read_stdout()
        if resp.peek_stderr():
            stderr_data += resp.read_stderr()
    resp.close()

    rc = resp.returncode
    if rc and rc != 0:
        raise RuntimeError(
            f"Command failed in pod {pod_name} ({namespace}) "
            f"workdir={workdir} rc={rc}: {stderr_data.strip()}"
        )
    return stdout_data


async def _build_commit_msg(patch: str, workspace_id: int, db: AsyncSession) -> str:
    """Use an LLM to generate a commit message from the diff."""
    truncated = patch[:8000] if len(patch) > 8000 else patch

    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == workspace_id)
    )
    oc = oc_result.scalar_one_or_none()
    if not oc:
        return _fallback_commit_msg(patch)

    try:
        if oc.has_adc and oc.google_cloud_project:
            return await _llm_commit_msg_vertex(truncated, oc)
        elif oc.anthropic_api_key:
            return await _llm_commit_msg_anthropic(truncated, oc.anthropic_api_key)
        elif oc.google_api_key:
            return await _llm_commit_msg_gemini(truncated, oc.google_api_key)
    except Exception as exc:
        log.warning("LLM commit msg generation failed: %s", exc)

    return _fallback_commit_msg(patch)


_COMMIT_MSG_PROMPT = (
    "Write a concise git commit message for the following diff. "
    "Use imperative mood (e.g. 'Add', 'Fix', 'Update'). "
    "First line is the subject (max 72 chars), then a blank line, "
    "then 1-3 bullet points summarizing the key changes. "
    "Output ONLY the commit message, nothing else.\n\n"
)


async def _llm_commit_msg_vertex(patch: str, oc: OpencodeSecret) -> str:
    """Call Vertex AI Anthropic Claude to generate a commit message."""
    import json
    import google.auth.transport.requests

    adc_info = json.loads(oc.application_default_credentials)
    adc_type = adc_info.get("type", "")

    if adc_type == "service_account":
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(
            adc_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    elif adc_type == "authorized_user":
        from google.oauth2.credentials import Credentials as UserCredentials
        creds = UserCredentials(
            token=None,
            refresh_token=adc_info["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=adc_info["client_id"],
            client_secret=adc_info["client_secret"],
        )
    else:
        raise ValueError(f"Unsupported ADC type: {adc_type}")

    creds.refresh(google.auth.transport.requests.Request())
    token = creds.token

    project = oc.google_cloud_project
    location = oc.vertex_location or "global"
    model = "claude-haiku-4-5@20251001"

    if location == "global":
        host = "aiplatform.googleapis.com"
    else:
        host = f"{location}-aiplatform.googleapis.com"
    url = (
        f"https://{host}/v1/"
        f"projects/{project}/locations/{location}/publishers/anthropic/models/{model}:rawPredict"
    )

    body = {
        "anthropic_version": "vertex-2023-10-16",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": _COMMIT_MSG_PROMPT + patch}],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()


async def _llm_commit_msg_anthropic(patch: str, api_key: str) -> str:
    """Call Anthropic API directly."""
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": _COMMIT_MSG_PROMPT + patch}],
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            json=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()


async def _llm_commit_msg_gemini(patch: str, api_key: str) -> str:
    """Call Google Gemini API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    body = {"contents": [{"parts": [{"text": _COMMIT_MSG_PROMPT + patch}]}]}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _fallback_commit_msg(patch: str) -> str:
    """Simple fallback when no LLM is available."""
    files = []
    for line in patch.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                files.append(parts[1])
    if not files:
        return ""
    if len(files) == 1:
        return f"Update {files[0]}"
    elif len(files) <= 3:
        return f"Update {', '.join(files)}"
    return f"Update {len(files)} files"


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/generate-patch",
    dependencies=[Depends(require_auth)],
)
async def session_generate_patch(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(
        Session,
        sid,
        options=[selectinload(Session.repos)],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    if not session.repos:
        flash(request, "No repos attached — nothing to diff.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    if not session.pod_name or session.phase != "running":
        flash(request, "Session must be running to generate a patch.", "warning")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    try:
        tool = get_tool(session.agent_tool)
        container_name = tool.get_container_name()
    except ValueError:
        container_name = "opencode"

    diff_parts: list[str] = []
    failures: list[str] = []
    for repo in session.repos:
        try:
            if session.working_branch:
                diff_cmd = ["git", "diff", f"origin/{repo.branch}"]
            else:
                diff_cmd = ["git", "diff"]
            diff = await asyncio.to_thread(
                _exec_in_pod,
                session.pod_name,
                ws.k8s_namespace,
                f"/workspace/{repo.local_path}",
                diff_cmd,
                container_name,
            )
            if diff.strip():
                diff_parts.append(diff)
        except Exception as exc:
            log.warning("git diff failed for repo %s: %s", repo.local_path, exc)
            failures.append(f"{repo.local_path}: {exc}")

    if failures:
        flash(request, f"Diff failed for: {'; '.join(failures)}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#patch", status_code=302)

    # Capture the base commit SHA so the "Apply locally" instructions pin the exact ref
    base_ref = ""
    if diff_parts and session.repos:
        repo = session.repos[0]
        try:
            base_ref = await asyncio.to_thread(
                _exec_in_pod,
                session.pod_name,
                ws.k8s_namespace,
                f"/workspace/{repo.local_path}",
                ["git", "rev-parse", f"origin/{repo.branch}"],
                container_name,
            )
            base_ref = base_ref.strip()
        except Exception:
            base_ref = ""

    raw_patch = "\n".join(diff_parts) if diff_parts else ""
    session.patch_output = "\n".join(ln.rstrip() for ln in raw_patch.split("\n"))
    session.patch_base_ref = base_ref
    session.commit_msg = await _build_commit_msg(session.patch_output, ws_id, db) if session.patch_output.strip() else ""
    await db.commit()

    if not session.patch_output.strip():
        flash(request, "No changes detected.", "info")
    else:
        flash(request, "Patch generated.", "success")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#patch", status_code=302)


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/download-patch",
    dependencies=[Depends(require_auth)],
)
async def session_download_patch(
    ws_id: int,
    sid: int,
    db: AsyncSession = Depends(get_db),
):
    from starlette.responses import Response

    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id or not session.patch_output:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    filename = _patch_filename(session)
    return Response(
        content=session.patch_output,
        media_type="text/x-patch",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""},
    )


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/repos/items",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_items(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return the _repo_items.html partial for #repo-list innerHTML refresh."""
    result = await db.execute(
        select(Session)
        .where(Session.id == sid)
        .options(selectinload(Session.repos), selectinload(Session.github_pat))
    )
    session = result.scalar_one_or_none()
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    return templates.TemplateResponse(
        request,
        "sessions/_repo_items.html",
        {"ws_id": ws_id, "session": session, "repo_info": repo_info},
    )


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/repos/pick",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_pick(
    ws_id: int,
    sid: int,
    pat_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return an HTMX partial listing all repos for the selected PAT."""
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    if session.is_active:
        return HTMLResponse("", status_code=409)

    pat = await db.get(GitHubPAT, pat_id)
    if pat is None or pat.workspace_id != ws_id:
        return templates.TemplateResponse(
            request,
            "sessions/_repo_picker.html",
            {"error": "PAT not found.", "repos": [], "truncated": False,
             "ws_id": ws_id, "session": session},
        )

    result = await _list_repos_for_pat(pat)
    if isinstance(result, str):
        return templates.TemplateResponse(
            request,
            "sessions/_repo_picker.html",
            {"error": result, "repos": [], "truncated": False,
             "ws_id": ws_id, "session": session},
        )

    truncated = len(result) >= 500
    return templates.TemplateResponse(
        request,
        "sessions/_repo_picker.html",
        {"repos": result, "truncated": truncated, "error": None,
         "ws_id": ws_id, "session": session},
    )

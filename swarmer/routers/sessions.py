import asyncio
import json as _json_filter
import logging
import re
import shlex
import uuid
from datetime import datetime, timezone

import httpx

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer import k8s
from swarmer.agent_tools.registry import get as get_tool, all_tools
from swarmer.config import settings
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.ansi import ansi_to_html
from swarmer.flash import flash
from swarmer.github import fetch_repo_info as _fetch_repo_info
from swarmer.github import list_repos_for_pat as _list_repos_for_pat
from swarmer.github import list_repos_for_github_app as _list_repos_for_github_app
from swarmer.github_url_validator import GitHubURLError, validate_github_url
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.session import CRON_PRESETS, Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource

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


async def _resolve_token_for_repo_check(
    session,
    db: AsyncSession,
    user_id: str = "",
) -> str | None:
    """Return a token for repo visibility/access checks, or None (= do nothing).

    Priority:
      1. Session PAT — if assigned, use it and ignore any GitHub App.
      2. GitHub App IAT — minted on the fly if no PAT and an App is configured.
      3. None — no credential configured; caller skips the check entirely.
    """
    if session.github_pat:
        token = session.github_pat.pat or None
        log.debug(
            "_resolve_token_for_repo_check: session %d using PAT id=%d token_present=%s",
            session.id, session.github_pat.id, bool(token),
        )
        return token
    # No PAT — try minting a short-lived App IAT.
    try:
        from swarmer.github_app import get_workspace_github_app
        from swarmer.github_auth import mint_installation_token
        app = await get_workspace_github_app(session.workspace_id, db, user_id=user_id)
        if app:
            return await mint_installation_token(app)
    except Exception:
        log.warning(
            "_resolve_token_for_repo_check: IAT mint failed for session %d",
            session.id, exc_info=True,
        )
    return None


def _github_app_provider_name(workspace_id: int, session_id: int) -> str:
    """Deterministic per-session GitHub App provider name.

    Session-scoped so multiple sessions in the same workspace each get an
    independent provider + IAT.  Derivable from IDs alone — no DB lookup needed
    at cleanup time.
    """
    return f"swarmer-ws-{workspace_id}-github-app-s{session_id}"


async def _delete_github_app_provider(workspace_id: int, session_id: int) -> None:
    """Delete the session-scoped GitHub App provider from the OpenShell Gateway.

    Safe to call even when no GitHub App was used — delete_provider is a no-op
    if the provider does not exist (NOT_FOUND is suppressed by the client).
    Also cancels the iat-refresh background task for the session.
    """
    from swarmer import openshell_client

    pname = _github_app_provider_name(workspace_id, session_id)
    # Cancel refresh loop first so it cannot race and re-create the provider
    # after we delete it.
    for _t in asyncio.all_tasks():
        if _t.get_name() == f"iat-refresh-{session_id}":
            _t.cancel()
            log.info("_delete_github_app_provider: cancelled iat-refresh task for session %d", session_id)
            break
    try:
        await openshell_client.delete_provider(pname)
        log.info("_delete_github_app_provider: deleted provider %s", pname)
    except Exception:
        log.warning("_delete_github_app_provider: failed to delete provider %s", pname, exc_info=True)


async def _delete_pat_provider(workspace_id: int, pat_id: int | None, session_id: int | None = None) -> None:
    """Delete the session-scoped PAT provider from the OpenShell Gateway.

    Safe to call when pat_id is None (no PAT was used) — returns immediately.
    Also cleans up the legacy workspace-scoped name (swarmer-ws-{ws}-github-pat-{pat})
    for sessions launched before the per-session naming change.
    """
    if not pat_id:
        return
    from swarmer import openshell_client

    # Session-scoped name (current format).
    if session_id:
        pname = f"swarmer-ws-{workspace_id}-github-pat-{pat_id}-s{session_id}"
        try:
            await openshell_client.delete_provider(pname)
            log.info("_delete_pat_provider: deleted provider %s", pname)
        except Exception:
            log.warning("_delete_pat_provider: failed to delete provider %s", pname, exc_info=True)

    # Legacy workspace-scoped name — clean up if still present.
    legacy_pname = f"swarmer-ws-{workspace_id}-github-pat-{pat_id}"
    try:
        await openshell_client.delete_provider(legacy_pname)
        log.info("_delete_pat_provider: deleted legacy provider %s", legacy_pname)
    except Exception:
        log.warning("_delete_pat_provider: failed to delete legacy provider %s", legacy_pname, exc_info=True)


def _build_repo_context(repos, base_path: str = "/sandbox") -> str:
    """Build a markdown section listing workspace repositories.

    Returns an empty string when *repos* is empty so callers can
    unconditionally concatenate the result.
    """
    if not repos:
        return ""
    lines = [
        "\n\n## Workspace Repositories\n",
        "The following Git repositories are available in this workspace:\n",
        "| Repository | Branch | Path |",
        "|---|---|---|",
    ]
    for repo in repos:
        org_repo = repo.repo_url.rstrip("/").removesuffix(".git")
        org_repo = "/".join(org_repo.split("/")[-2:])
        lines.append(
            f"| `{org_repo}` | `{repo.branch}` "
            f"| `{base_path}/{repo.local_path}` |"
        )
    return "\n".join(lines) + "\n"


async def _get_provider_options(
    ws_id: int, db: AsyncSession, agent_tool: str = "opencode"
) -> list[dict]:
    """Return the available AI provider choices for this workspace's sessions."""
    try:
        tool = get_tool(agent_tool)
    except ValueError:
        # Defensive fallback for legacy/removed tool names (e.g. "crush",
        # ACM-37174) that may still be stored on old session rows if the
        # startup migration hasn't run yet. Never 500 on a display path.
        log.warning(
            "_get_provider_options: unknown agent_tool %r, falling back to opencode",
            agent_tool,
        )
        tool = get_tool("opencode")
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc = result.scalars().first()
    # Check gateway for Vertex AI provider — ADC is stored on OpenShell, not Swarmer DB.
    has_vertex = False
    try:
        from swarmer import openshell_client
        has_vertex = await openshell_client.provider_exists(f"swarmer-ws-{ws_id}-google-cloud")
    except Exception:
        pass
    # Google AI Studio key still lives encrypted in the DB pending ACM-37263.
    has_gemini = bool(oc and oc.google_api_key_enc)
    return tool.get_model_options(oc, has_vertex=has_vertex, has_gemini=has_gemini)

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")
templates.env.filters['ansi_to_html'] = ansi_to_html
templates.env.filters['from_json'] = lambda s: _json_filter.loads(s) if s else []


def _current_user(request: Request) -> str:
    """Return the K8s username from the session, or '' if not set."""
    return request.session.get("username", "")


async def _visible_pats(ws_id: int, db: AsyncSession, user_id: str = "") -> list:
    """Return PATs visible to the given user (own + shared + legacy)."""
    filters = [GitHubPAT.workspace_id == ws_id]
    if user_id:
        filters.append(
            or_(
                GitHubPAT.user_id == user_id,
                GitHubPAT.shared == True,  # noqa: E712
                GitHubPAT.user_id == "",
            )
        )
    result = await db.execute(
        select(GitHubPAT).where(*filters).order_by(GitHubPAT.name)
    )
    return list(result.scalars().all())


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


async def _get_prompt_sources(ws_id: int, db: AsyncSession) -> list[WorkspacePromptSource]:
    """Return all prompt sources and their prompts for this workspace."""
    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.workspace_id == ws_id)
        .options(selectinload(WorkspacePromptSource.prompts))
        .order_by(WorkspacePromptSource.name)
    )
    return list(result.scalars().all())


# ============================================================
# Provider options (HTMX partial — reloads when agent tool changes)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/provider-options",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def provider_options_partial(
    ws_id: int,
    request: Request,
    agent_tool: str = "opencode",
    selected_provider: str = "",
    db: AsyncSession = Depends(get_db),
):
    provider_options = await _get_provider_options(ws_id, db, agent_tool)
    return templates.TemplateResponse(
        request,
        "sessions/_provider_select.html",
        {
            "provider_options": provider_options,
            "selected_provider": selected_provider,
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
    pass


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

    queued_ids = [s.id for s in sessions if s.phase == "queued"]
    queue_positions: dict[int, tuple[int, int]] = {}
    for sid in queued_ids:
        queue_positions[sid] = await _get_queue_position(sid, db)

    capacity = await _get_capacity_summary(ws_id, db)

    return templates.TemplateResponse(
        request,
        "sessions/_list_rows.html",
        {
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "queue_positions": queue_positions,
            "capacity": capacity,
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
    pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
    _tools = all_tools()
    try:
        default_agent_tool = get_tool(settings.default_agent_tool).name
    except ValueError:
        default_agent_tool = "opencode"
    provider_options = await _get_provider_options(ws_id, db, default_agent_tool)
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
    prompt_sources = await _get_prompt_sources(ws_id, db)
    return templates.TemplateResponse(
        request,
        "sessions/new.html",
        {
            "ws": ws,
            "pats": pats,
            "provider_options": provider_options,
            "selected_provider": "",
            "agent_tools": _tools,
            "default_agent_tool": default_agent_tool,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "mcp_servers": mcp_servers,
            "prompt_sources": prompt_sources,
        },
    )


@router.post("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_create(
    ws_id: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    provider: str = Form(""),
    agent_tool: str = Form("opencode"),
    working_branch: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    pat_id = int(github_pat_id) if github_pat_id else None
    pid = None
    if prompt_id:
        try:
            pid = int(prompt_id)
            # Verify prompt ownership
            from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource
            prompt = await db.get(WorkspacePrompt, pid)
            if not prompt:
                flash(request, "Selected prompt not found.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

            # WorkspacePrompt -> WorkspacePromptSource -> Workspace
            # We need to load the source to check workspace_id
            result = await db.execute(
                select(WorkspacePromptSource).where(WorkspacePromptSource.id == prompt.source_id)
            )
            source = result.scalar_one_or_none()
            if not source or source.workspace_id != ws_id:
                flash(request, "Selected prompt does not belong to this workspace.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)
        except ValueError:
            flash(request, "Invalid prompt selection.", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

    try:
        agent_tool = get_tool(agent_tool).name
    except ValueError:
        agent_tool = "opencode"

    if not provider.strip():
        opts = await _get_provider_options(ws_id, db, agent_tool)
        # Always select an available provider when at least one AI token/credential
        # is configured. Only leave the selection empty when NO provider is
        # available at all — there is nothing usable to default to.
        _available = [o for o in opts if o.get("available", True)]
        provider = _available[0].get("value", "") if _available else ""

    wb = working_branch.strip()
    if wb and not _is_valid_ref_name(wb):
        flash(request, "Invalid working branch name.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/new", status_code=302)

    session = Session(
        workspace_id=ws_id,
        github_pat_id=pat_id,
        prompt_id=pid,
        name=name.strip(),
        provider=provider.strip(),
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
        # Rollback expires all ORM objects; refresh ws so the template can read
        # ws.display_name without triggering a lazy-load outside async context.
        await db.refresh(ws)
        pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
        _tools = all_tools()
        try:
            default_agent_tool = get_tool(settings.default_agent_tool).name
        except ValueError:
            default_agent_tool = "opencode"
        provider_options = await _get_provider_options(ws_id, db, default_agent_tool)
        _avail = await asyncio.gather(
            *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
        )
        from swarmer.routers.mcp_servers import get_enabled_mcp_servers
        mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
        prompt_sources = await _get_prompt_sources(ws_id, db)
        return templates.TemplateResponse(
            request,
            "sessions/new.html",
            {
                "ws": ws,
                "pats": pats,
                "error": f"A session named '{name}' already exists in this workspace.",
                "form": {"name": name, "instruction_prompt": instruction_prompt},
                "provider_options": provider_options,
                "selected_provider": provider,
                "agent_tools": _tools,
                "default_agent_tool": default_agent_tool,
                "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
                "mcp_servers": mcp_servers,
                "prompt_sources": prompt_sources,
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
        options=[
            selectinload(Session.github_pat),
            selectinload(Session.repos),
            selectinload(Session.prompt),
        ],
    )
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    pats = await _visible_pats(ws_id, db, user_id=_current_user(request))

    status_detail = ""

    # Generate one-time TUI token
    tui_token = None
    if session.mode == "tui" and session.phase == "running":
        tui_token = str(uuid.uuid4())
        tokens = list(request.session.get("tui_tokens", []))
        tokens.append(tui_token)
        request.session["tui_tokens"] = tokens

    provider_options = await _get_provider_options(ws_id, db, session.agent_tool)
    # Resolve repo check token BEFORE any additional DB queries — extra queries
    # on the same async session can interfere with loaded relationship attributes.
    _repo_check_token = await _resolve_token_for_repo_check(
        session, db, user_id=_current_user(request)
    )
    repo_info = await _fetch_repo_info(session.repos, _repo_check_token)
    from swarmer.github_app import get_workspace_github_app as _get_ws_github_app
    _ws_github_app = await _get_ws_github_app(ws_id, db, user_id=_current_user(request))

    _tools = all_tools()
    _avail = await asyncio.gather(
        *[k8s.get_image_available(t.get_image(), ws.k8s_namespace) for t in _tools]
    )
    try:
        canonical_agent_tool = get_tool(session.agent_tool).name
    except ValueError:
        canonical_agent_tool = session.agent_tool
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    mcp_servers = await get_enabled_mcp_servers(ws_id, db, user_id=_current_user(request))
    prompt_sources = await _get_prompt_sources(ws_id, db)
    queue_position = None
    if session.phase == "queued":
        queue_position = await _get_queue_position(session.id, db)
    capacity = await _get_capacity_summary(ws_id, db)
    from swarmer.models.session_run import SessionRun

    runs_result = await db.execute(
        select(SessionRun)
        .where(SessionRun.session_id == sid)
        .order_by(desc(SessionRun.completed_at))
        .limit(100)
    )
    session_runs = list(runs_result.scalars().all())
    return templates.TemplateResponse(
        request,
        "sessions/detail.html",
        {
            "ws": ws,
            "ws_id": ws_id,
            "session": session,
            "session_runs": session_runs,
            "canonical_agent_tool": canonical_agent_tool,
            "pats": pats,
            "tui_token": tui_token,
            "mode_label": _session_mode_label(session),
            "mode_badge": _session_mode_badge_class(session),
            "status_detail": status_detail,
            "queue_position": queue_position,
            "capacity": capacity,
            "provider_options": provider_options,
            "selected_provider": session.provider,
            "provider_select_disabled": session.is_active,
            "repo_info": repo_info,
            "has_github_app": bool(_ws_github_app),
            "agent_tools": _tools,
            "tool_image_available": dict(zip([t.name for t in _tools], _avail, strict=False)),
            "patch_filename": _patch_filename(session),
            "cron_presets": CRON_PRESETS,
            "mcp_servers": mcp_servers,
            "prompt_sources": prompt_sources,
            "custom_policy_rules": _json_filter.loads(session.custom_policies) if session.custom_policies else [],
            "show_policy_tab": bool(
                settings.openshell_gateway_url
                or session.sandbox_name
                or session.policy_chunks
                or session.custom_policies
            ),
            "schedules": session.schedules or [],
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
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    mode: str = Form("prompt"),
    provider: str = Form(""),
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

    if prompt_id:
        try:
            pid = int(prompt_id)
            from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource
            prompt = await db.get(WorkspacePrompt, pid)
            if not prompt:
                flash(request, "Selected prompt not found.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

            result = await db.execute(
                select(WorkspacePromptSource).where(WorkspacePromptSource.id == prompt.source_id)
            )
            source = result.scalar_one_or_none()
            if not source or source.workspace_id != ws_id:
                flash(request, "Selected prompt does not belong to this workspace.", "danger")
                return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)
            session.prompt_id = pid
        except ValueError:
            flash(request, "Invalid prompt selection.", "danger")
            return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)
    else:
        session.prompt_id = None

    session.instruction_prompt = instruction_prompt.strip()
    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    session.provider = provider.strip()
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

async def _resolve_session_prompt(session: Session, db: AsyncSession) -> str:
    """Resolve the session prompt using layered composition.
    
    Layers:
    1. Additional Instructions (session.instruction_prompt)
    2. Git-referenced prompt content (session.prompt_id)
    """
    base_prompt = ""
    if session.prompt_id:
        p = await db.get(WorkspacePrompt, session.prompt_id)
        if p:
            base_prompt = p.content

    additional = session.instruction_prompt.strip() if session.instruction_prompt else ""
    if additional and base_prompt:
        resolved_prompt = additional + "\n\n" + base_prompt
    elif additional:
        resolved_prompt = additional
    else:
        resolved_prompt = base_prompt
    return resolved_prompt


async def _count_running_sessions(db: AsyncSession) -> int:
    """Count sessions globally with pods: pending or running only."""
    result = await db.execute(
        select(func.count()).select_from(Session).where(
            Session.phase.in_(["pending", "running"])
        )
    )
    return result.scalar_one()


async def _get_queue_position(session_id: int, db: AsyncSession) -> tuple[int, int]:
    """Return (1-based position, total queued) for a queued session, global FIFO by created_at."""
    result = await db.execute(
        select(Session.id, Session.created_at)
        .where(Session.phase == "queued")
        .order_by(Session.created_at)
    )
    rows = result.all()
    total = len(rows)
    for i, (sid, _) in enumerate(rows):
        if sid == session_id:
            return i + 1, total
    return 0, total


async def _get_capacity_summary(workspace_id: int, db: AsyncSession) -> dict:
    """Return workspace-scoped capacity info for the sessions list display."""
    global_running = await _count_running_sessions(db)

    ws_active_result = await db.execute(
        select(func.count()).select_from(Session).where(
            Session.workspace_id == workspace_id,
            Session.phase.in_(["pending", "running"]),
        )
    )
    ws_active = ws_active_result.scalar_one()

    ws_queued_result = await db.execute(
        select(func.count()).select_from(Session).where(
            Session.workspace_id == workspace_id,
            Session.phase == "queued",
        )
    )
    ws_queued = ws_queued_result.scalar_one()

    max_agents = settings.max_concurrent_agents
    if max_agents <= 0:
        slots_available = None
    else:
        slots_available = max(0, max_agents - global_running)

    return {
        "ws_active": ws_active,
        "slots_available": slots_available,
        "ws_queued": ws_queued,
        "max": max_agents,
    }


def _build_expected_hosts(model: str, repos_data: list[dict], tool_name: str, mode: str) -> set[str]:
    """Return the set of hostnames this session is expected to reach.

    Only draft policy chunks matching these hosts will be auto-approved.
    Anything outside this set is left pending for human review.
    """
    hosts: set[str] = set()

    # AI provider endpoints based on model prefix
    provider = model.split("/")[0] if "/" in model else ""
    if provider in ("google", "vertexai", "google-vertex-anthropic"):
        hosts.add("generativelanguage.googleapis.com")
        hosts.add("aiplatform.googleapis.com")
        hosts.add("oauth2.googleapis.com")
    if provider == "gemini":
        hosts.add("generativelanguage.googleapis.com")
    if provider == "openai":
        hosts.add("api.openai.com")

    # OpenCode fetches model metadata from models.dev
    if tool_name == "opencode":
        hosts.add("models.dev")
        hosts.add("opencode.ai")

    # GitHub access for each attached repo
    if repos_data:
        hosts.add("github.com")
        hosts.add("api.github.com")
        # Pack-file CDN and shallow clone host — required for git clone to complete
        hosts.add("objects.githubusercontent.com")
        hosts.add("codeload.github.com")
        # Raw content for public repos
        hosts.add("raw.githubusercontent.com")

    return hosts


async def _resolve_schedule_prompt(schedule_id: int, session: Session, db: AsyncSession) -> str:
    """Resolve a per-schedule prompt, falling back to session defaults for empty fields."""
    from swarmer.models.session_schedule import SessionSchedule
    sched = await db.get(SessionSchedule, schedule_id)
    if sched is None:
        return await _resolve_session_prompt(session, db)

    # Effective prompt_id: schedule overrides session, empty means use session default.
    effective_prompt_id = sched.prompt_id if sched.prompt_id is not None else session.prompt_id
    base_prompt = ""
    if effective_prompt_id:
        p = await db.get(WorkspacePrompt, effective_prompt_id)
        if p:
            base_prompt = p.content

    # Effective instruction: schedule overrides session, empty means use session default.
    effective_instruction = (
        sched.instruction_prompt.strip()
        if sched.instruction_prompt and sched.instruction_prompt.strip()
        else (session.instruction_prompt.strip() if session.instruction_prompt else "")
    )

    if effective_instruction and base_prompt:
        return effective_instruction + "\n\n" + base_prompt
    elif effective_instruction:
        return effective_instruction
    else:
        return base_prompt


async def _do_launch(session: Session, ws: Workspace, db: AsyncSession, user_id: str = "") -> None:
    """Core launch logic shared by the HTTP endpoint and the background scheduler."""
    if user_id == "unknown":
        raise ValueError("Session expired — please log in again")

    _github_repos = [r for r in (session.repos or []) if "github.com" in (r.repo_url or "")]
    if _github_repos and not session.github_pat:
        # Allow launch if the workspace has a GitHub App configured — it will mint an IAT.
        from swarmer.github_app import get_workspace_github_app
        _ws_github_app = await get_workspace_github_app(session.workspace_id, db, user_id=user_id)
        if not _ws_github_app:
            raise ValueError(
                "A GitHub PAT is required for repos on github.com — add one in AI Tokens."
            )

    if settings.max_concurrent_agents > 0:
        running = await _count_running_sessions(db)
        if running >= settings.max_concurrent_agents:
            session.phase = "queued"
            session.status_detail = f"Waiting for capacity ({running}/{settings.max_concurrent_agents} active)"
            await db.commit()
            return

    import secrets as _secrets
    suffix = _secrets.token_hex(4)

    from swarmer.models.opencode_secret import OpencodeSecret
    _user_filter = [OpencodeSecret.workspace_id == session.workspace_id]
    if user_id:
        _user_filter.append(
            or_(
                OpencodeSecret.user_id == user_id,
                OpencodeSecret.shared == True,  # noqa: E712
                OpencodeSecret.user_id == "",
            )
        )
    oc_result = await db.execute(
        select(OpencodeSecret).where(*_user_filter)
    )
    _oc_all = oc_result.scalars().all()
    oc_secret = None
    if user_id:
        for s in _oc_all:
            if s.user_id == user_id:
                oc_secret = s
                break
    if oc_secret is None and _oc_all:
        oc_secret = _oc_all[0]
    # has_adc: check gateway for google-cloud provider (ADC stored on OpenShell, not DB)
    has_adc = False
    try:
        from swarmer import openshell_client as _oc_client
        has_adc = await _oc_client.provider_exists(f"swarmer-ws-{session.workspace_id}-google-cloud")
    except Exception:
        pass
    # Fetch enabled & authenticated MCP servers for this workspace
    from swarmer.routers.mcp_servers import get_enabled_mcp_servers
    all_ws_mcp = await get_enabled_mcp_servers(session.workspace_id, db, user_id=user_id)
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

    # Resolve prompt: if a schedule triggered this run, use its prompt config
    # (falling back to session defaults for empty fields).
    if session.active_schedule_id:
        resolved_prompt = await _resolve_schedule_prompt(session.active_schedule_id, session, db)
    else:
        resolved_prompt = await _resolve_session_prompt(session, db)

    # Fetch workspace prompt sources for network policy scoping.
    # Agents inside the sandbox may curl raw.githubusercontent.com to fetch
    # prompt documents or files referenced by the prompt.  The policy is
    # scoped to the configured prompt source repos (org/repo/branch).
    prompt_sources = await _get_prompt_sources(ws.id, db)

    await _do_launch_openshell(
        session=session,
        ws=ws,
        db=db,
        suffix=suffix,
        oc_secret=oc_secret,
        has_adc=has_adc,
        mcp_servers=mcp_servers,
        resolved_prompt=resolved_prompt,
        prompt_sources=prompt_sources,
        user_id=user_id,
    )


async def _do_launch_openshell(
    session: Session,
    ws: Workspace,
    db: AsyncSession,
    suffix: str,
    oc_secret,
    has_adc: bool,
    mcp_servers: list | None,
    resolved_prompt: str,
    prompt_sources: list | None = None,
    user_id: str = "",
) -> None:
    """Launch a session via the OpenShell sandbox API."""
    from swarmer import openshell_client
    from swarmer.openshell_policy import build_session_policy

    tool = get_tool(session.agent_tool)

    # Resolve the provider first so it is available for provider registration and
    # policy building. session.provider is a family preset name ("claude"/"gemini",
    # ACM-37232) — build_config_data() understands it directly. Everything else
    # (network policy, CLI --model flag, model.json state) needs a concrete model
    # ID, so it uses `model` — the provider resolved to its BUILD-role model —
    # instead. Raw provider/model@version strings from pre-ACM-37232 sessions are
    # also still accepted for backward compatibility.
    if session.provider and tool.is_valid_model(session.provider):
        raw_model = session.provider
        log.info(
            "_do_launch_openshell: session %d using stored provider %r (tool=%s)",
            session.id, raw_model, tool.name,
        )
    else:
        raw_model = tool.get_default_model(has_adc)
        log.info(
            "_do_launch_openshell: session %d stored provider %r invalid/empty — "
            "falling back to default %r (tool=%s, has_adc=%s)",
            session.id, session.provider, raw_model, tool.name, has_adc,
        )
    raw_model = raw_model.strip("\r\n")  # strip any stray line endings before embedding in shell commands
    model = tool.resolve_build_model(raw_model)

    # Query workspace env vars from DB before releasing the connection.
    from sqlalchemy import select as sa_select
    from swarmer.models.sandbox_env_var import SandboxEnvVar
    _ev_result = await db.execute(
        sa_select(SandboxEnvVar).where(SandboxEnvVar.workspace_id == session.workspace_id)
    )
    extra_env: dict[str, str] = {row.key: row.value for row in _ev_result.scalars().all()}

    # Extract GitHub repo names BEFORE committing — session.repos relationship
    # attributes are guaranteed accessible while the DB session is still live.
    #
    # Credential routing: if a PAT is assigned to the session, use it exclusively
    # and skip the GitHub App entirely.  The PAT is an explicit user choice and
    # has the right write permissions for that session's repos.  The GitHub App
    # is only used when no PAT is assigned.
    from swarmer.github import github_slug as _github_slug
    _session_repo_names: list[str] = []
    _has_github_repos = False

    for _r in (session.repos or []):
        _url = _r.repo_url or ""
        if "github.com" not in _url:
            continue
        _has_github_repos = True
        try:
            _slug = _github_slug(_url)
            if _slug:
                _session_repo_names.append(_slug.split("/", 1)[1])
        except Exception:
            log.warning("_do_launch_openshell: could not extract repo name from %r", _url, exc_info=True)

    # Resolve GitHub App before committing (requires DB access).
    # Skipped entirely when the session has a PAT assigned.
    _github_app = None
    if _has_github_repos and not session.github_pat:
        try:
            from swarmer.github_app import get_workspace_github_app
            _github_app = await get_workspace_github_app(session.workspace_id, db, user_id=user_id)
        except Exception:
            log.warning("_do_launch_openshell: failed to resolve GitHub App for session %d", session.id, exc_info=True)

    log.info(
        "_do_launch_openshell: session %d has_github_repos=%s has_pat=%s has_app=%s repos=%s",
        session.id, _has_github_repos, bool(session.github_pat), bool(_github_app), _session_repo_names,
    )

    # Release the DB connection before long-running gRPC operations. The route
    # handler's session holds an autobegin transaction from earlier SELECTs in
    # _do_launch(); committing here ends that transaction and returns the
    # connection to the pool so the scheduler can write without being blocked.
    # session attributes remain valid because expire_on_commit=False.
    await db.commit()

    # 1. Collect sandbox extra env vars (non-credential; AI creds go through provider API)
    env_vars = await openshell_client.create_provider(
        session=session,
        workspace_secret=oc_secret,
        github_pat=session.github_pat,
        mcp_servers=mcp_servers or [],
        extra_env=extra_env,
    )
    # Point OpenCode at the config file written by write_agent_config() via the
    # OPENCODE_CONFIG env var (there is no --config CLI flag).
    if tool.name == "opencode":
        env_vars["OPENCODE_CONFIG"] = "/sandbox/opencode.json"
        # Enables the opencode plan agent so a preset's PLAN model (written into
        # opencode.json's agent.plan.model by build_config_data()) is actually
        # used (ACM-37232). Without this flag the plan agent/tool never engages.
        if settings.opencode_experimental_plan_mode:
            env_vars["OPENCODE_EXPERIMENTAL_PLAN_MODE"] = "true"

    # 1b. Create/update gateway providers for each available credential.
    #     Must happen BEFORE sandbox creation: provider names go into SandboxSpec.providers
    #     so the supervisor can call GetSandboxProviderEnvironment at startup and receive
    #     the injected env vars (GOOGLE_API_KEY, ANTHROPIC_API_KEY, GH_TOKEN, etc.).
    provider_names: list[str] = []
    ws_id = session.workspace_id
    if oc_secret and oc_secret.google_api_key:
        pname = f"swarmer-ws-{ws_id}-google-ai-studio"
        await openshell_client.ensure_provider(pname, "google-ai-studio", {}, credentials={
                "GOOGLE_API_KEY": oc_secret.google_api_key,
                "GOOGLE_GENERATIVE_AI_API_KEY": oc_secret.google_api_key,
            })
        provider_names.append(pname)
    # Vertex AI via google-cloud provider — ADC is stored on the gateway (not in Swarmer DB).
    # Attach the provider if it already exists (created via the secrets UI).
    _vertex_pname = f"swarmer-ws-{ws_id}-google-cloud"
    _has_google_cloud_provider = False
    try:
        if await openshell_client.provider_exists(_vertex_pname):
            provider_names.append(_vertex_pname)
            _has_google_cloud_provider = True
    except Exception:
        log.warning(
            "_do_launch_openshell: could not check google-cloud provider for session %d",
            session.id, exc_info=True,
        )
    # 1b cont. GitHub App IAT — minted above before commit; now register the provider.
    _app_pname: str | None = None

    if _github_app:
        try:
            from swarmer.github_auth import mint_installation_token
            iat = await mint_installation_token(
                _github_app,
                repo_names=_session_repo_names or None,  # empty list → None → full-installation scope
            )
            pname = f"swarmer-ws-{ws_id}-github-app-s{session.id}"
            await openshell_client.ensure_provider(pname, "github", {}, credentials={
                "GITHUB_TOKEN": iat,
                "GH_TOKEN": iat,
            })
            provider_names.append(pname)
            _app_pname = pname
            log.info(
                "_do_launch_openshell: using GitHub App IAT for session %d (repos=%s)",
                session.id, _session_repo_names or "all",
            )
        except Exception:
            log.warning(
                "_do_launch_openshell: GitHub App IAT minting failed for session %d — "
                "falling back to PAT if available",
                session.id, exc_info=True,
            )
            _github_app = None  # signal fallback below

    if not _github_app and session.github_pat:
        pname = f"swarmer-ws-{ws_id}-github-pat-{session.github_pat.id}-s{session.id}"
        pat_token = session.github_pat.pat or ""
        await openshell_client.ensure_provider(pname, "github", {}, credentials={
            "GITHUB_TOKEN": pat_token,
            "GH_TOKEN": pat_token,
        })
        provider_names.append(pname)
    for mcp in (mcp_servers or []):
        if "jira" in getattr(mcp, "slug", "") and getattr(mcp, "jira_access_token_enc", ""):
            pname = f"swarmer-ws-{ws_id}-jira"
            # All three Jira vars go through the gateway Provider API.
            # JIRA_ACCESS_TOKEN is a credential (injected as openshell:resolve:... token).
            # JIRA_SERVER_URL and JIRA_EMAIL are non-secret config — the gateway injects
            # them as plain env vars into the sandbox (and into every exec call) alongside
            # the credential reference tokens, so no separate env_vars entry is needed.
            await openshell_client.ensure_provider(
                pname, "jira",
                config={
                    "JIRA_SERVER_URL": mcp.jira_server_url or "",
                    "JIRA_EMAIL": mcp.jira_email or "",
                },
                credentials={"JIRA_ACCESS_TOKEN": mcp.jira_access_token},
            )
            provider_names.append(pname)
            # URL and email are non-secret; pass them as plain env vars so the
            # sandbox process sees them directly on every exec call.
            env_vars["JIRA_SERVER_URL"] = mcp.jira_server_url or ""
            env_vars["JIRA_EMAIL"] = mcp.jira_email or ""
            break  # only one Jira provider per workspace

    # 2. Build policy YAML (pure computation, no I/O)
    # Parse session-level custom rules (approved from draft chunks) so they are
    # merged into the static policy and take effect on this sandbox launch.
    import json as _json
    _custom_policies: list[dict] = []
    if session.custom_policies:
        try:
            _custom_policies = _json.loads(session.custom_policies)
        except Exception:
            pass

    policy = build_session_policy(
        session=session,
        repos=list(session.repos or []),
        mcp_servers=list(mcp_servers or []),
        agent_tool=session.agent_tool,
        model=model,
        prompt_sources=list(prompt_sources or []),
        custom_policies=_custom_policies or None,
        has_google_cloud_provider=_has_google_cloud_provider,
    )

    # Capture serialisable data for the background task before committing.
    # ORM objects cannot be used across DB sessions.
    mcp_patch: dict = {}
    if mcp_servers:
        config_data = tool.build_config_data(secret=oc_secret, mcp_servers=mcp_servers, model=raw_model)
        config_json = config_data.get(f"{tool.name}.json", "{}")
        try:
            mcp_patch = _json.loads(config_json).get("mcp", {})
        except Exception:
            pass

    repos_data = [
        {"url": r.repo_url, "local_path": r.local_path, "branch": r.branch}
        for r in (session.repos or [])
    ]
    git_username = (session.github_pat.github_username or "") if session.github_pat else ""
    # Serialise GitHub App credentials before ORM object detaches after commit.
    # These are passed to _setup_openshell_sandbox so the refresh loop can re-mint
    # without holding an ORM session.
    if _github_app and _app_pname:
        _iat_app_id = _github_app.app_id
        _iat_installation_id = _github_app.installation_id
        _iat_private_key = _github_app.private_key  # decrypts here; stored as plain str
        _iat_repo_names: list[str] = _session_repo_names  # type: ignore[possibly-undefined]
    else:
        _iat_app_id = ""
        _iat_installation_id = ""
        _iat_private_key = ""
        _iat_repo_names = []
    # pat_token drives `gh auth setup-git` and git clone in the background task.
    # Only used when no GitHub App is active (PAT-only sessions).
    pat_token = (session.github_pat.pat or "") if (not _github_app and session.github_pat) else ""
    # has_git_token signals that a git credential is available (App IAT or PAT).
    has_git_token = bool(_app_pname or pat_token)
    # Build AGENTS.md content for all modes: resolved prompt + repo context table.
    # TUI/server: written to /sandbox/AGENTS.md, read automatically by the agent.
    # Prompt mode: same content written to /sandbox/AGENTS.md, then piped as the
    # CLI argument via "$(</sandbox/AGENTS.md)" shell expansion so the agent gets
    # the full context (prompt + repo layout) identically to TUI mode.
    repo_context = _build_repo_context(list(session.repos or []), base_path="/sandbox")
    agents_md = (resolved_prompt or "") + repo_context
    # main_cmd is used for tui/server modes only; prompt mode command is built
    # in _setup_openshell_sandbox to read from /sandbox/AGENTS.md at runtime.
    main_cmd = tool.build_main_cmd(session, model, resolved_prompt=resolved_prompt)
    resolved_prompt_safe = resolved_prompt or ""
    model_setup_cmd = tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/")
    share_cmd = tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/")
    # Resolve image early so a missing config (e.g. AGENT_IMAGE_OPENCODE not set) fails
    # before we mark the session pending and commit — keeps the error visible.
    image = tool.get_image()
    if not image:
        raise ValueError(
            f"No container image configured for agent tool '{tool.name}'. "
            f"Set the corresponding AGENT_IMAGE_* environment variable."
        )

    # Mark pending and commit — HTTP handler returns immediately; browser unblocks.
    session.phase = "pending"
    session.last_output = ""
    session.raw_output = ""
    session.status_detail = ""   # clear stale status from any previous run
    session.policy_chunks = ""   # clear stale chunks; fresh snapshot at completion
    session.run_started_at = datetime.now(timezone.utc)
    session.run_completed_at = None
    await db.commit()

    # All slow gRPC work (create_sandbox wait_ready, exec setup) runs in the background.
    asyncio.create_task(
        _setup_openshell_sandbox(
            session_id=session.id,
            workspace_id=session.workspace_id,
            provider_names=provider_names,
            env_vars=env_vars,
            policy=policy,
            image=image,
            tool_name=tool.name,
            model=model,
            config_model=raw_model,
            model_setup_cmd=model_setup_cmd,
            share_cmd=share_cmd,
            mcp_patch=mcp_patch,
            repos_data=repos_data,
            git_username=git_username,
            pat_token=pat_token,
            has_git_token=has_git_token,
            working_branch=session.working_branch or "",
            agents_md=agents_md,
            mode=session.mode,
            main_cmd=main_cmd,
            resolved_prompt=resolved_prompt_safe,
            iat_app_id=_iat_app_id,
            iat_installation_id=_iat_installation_id,
            iat_private_key=_iat_private_key,
            iat_provider_name=_app_pname or "",
            iat_repo_names=_iat_repo_names,
            pat_id=session.github_pat.id if session.github_pat else None,
        ),
        name=f"openshell-setup-{session.id}",
    )


async def _setup_openshell_sandbox(
    session_id: int,
    workspace_id: int,
    provider_names: list[str],
    env_vars: dict,
    policy,
    image: str,
    tool_name: str,
    model: str,
    model_setup_cmd: str,
    share_cmd: str,
    mcp_patch: dict,
    repos_data: list[dict],
    git_username: str,
    pat_token: str,
    working_branch: str,
    agents_md: str,
    mode: str,
    main_cmd: str,
    resolved_prompt: str = "",
    config_model: str = "",
    has_git_token: bool = False,
    # GitHub App IAT refresh loop params (all empty when using PAT fallback)
    iat_app_id: str = "",
    iat_installation_id: str = "",
    iat_private_key: str = "",
    iat_provider_name: str = "",
    iat_repo_names: list[str] | None = None,
    pat_id: int | None = None,  # PAT DB ID for provider cleanup on completion
) -> None:
    """Background task: create sandbox and run all setup steps, then launch agent."""
    from swarmer import openshell_client
    from swarmer.database import get_db as _get_db
    from swarmer.models.session import Session as _Session

    async def _update_db(**fields) -> None:
        async for _db in _get_db():
            _s = await _db.get(_Session, session_id)
            if _s:
                # Do not overwrite a "stopped" state set by the STOP handler —
                # this task may have been cancelled or lost the race with STOP.
                # "idle" is the initial launch state so we must not block it.
                if _s.phase == "stopped" and "phase" in fields:
                    log.info(
                        "_update_db: session %d is already 'stopped'; skipping task write (would set phase=%s)",
                        session_id, fields["phase"],
                    )
                    break
                for k, v in fields.items():
                    setattr(_s, k, v)
                await _db.commit()
            break

    try:
        await _update_db(status_detail="Creating sandbox…")
        ref = await openshell_client.create_sandbox(
            image=image,
            env_vars=env_vars,
            policy=policy,
            provider_names=provider_names,
        )
        await _update_db(sandbox_name=ref.name, status_detail="Applying network policies…")

        # Check if the session was stopped while sandbox was being created (race condition).
        # If so, delete the sandbox and exit cleanly rather than starting the agent.
        async def _session_phase() -> str:
            async for _db in _get_db():
                _s = await _db.get(_Session, session_id)
                return _s.phase if _s else "unknown"
            return "unknown"

        current_phase = await _session_phase()
        if current_phase != "pending":
            log.info("_setup_openshell_sandbox: session %d no longer pending (phase=%s), cleaning up sandbox %s",
                     session_id, current_phase, ref.name)
            try:
                await openshell_client.delete_sandbox(ref.name)
            except Exception:
                pass
            return

        # Write a schema-valid tool config to /sandbox/{tool}.json.
        # The container image ships an opencode.json with an outdated LSP schema
        # (missing required 'extensions' field). Always overwrite it with a valid
        # config that includes enabled_providers and any MCP config.
        from swarmer.agent_tools.registry import get as _get_tool
        _tool = _get_tool(tool_name)
        # mcp_patch is already the extracted "mcp" dict (keys are server slugs, values
        # are the per-server config dicts).  Do NOT call .get("mcp", {}) again here —
        # that double-nesting always returns {} and silently drops MCP config from the
        # written agent config JSON (ACM-34954).
        _mcp_list = [
            type("_MCP", (), {"slug": k, **v})()
            for k, v in (mcp_patch or {}).items()
        ] if mcp_patch else []
        _config_data = _tool.build_config_data(
            mcp_servers=_mcp_list,
            model=config_model or model,
        )
        _config_json = _config_data.get(f"{tool_name}.json", "{}")
        await openshell_client.write_agent_config(
            sandbox_name=ref.name,
            tool_name=tool_name,
            config_json=_config_json,
        )

        # Share/state dir setup runs FIRST so the $HOME/.local/share/<tool> symlink
        # is in place before model_setup_cmd writes the model config through it.
        # Both commands need HOME=/sandbox to match the agent's runtime environment.
        # Provider env vars (GOOGLE_API_KEY etc.) are inherited from the sandbox
        # environment automatically — no explicit injection needed.
        if share_cmd.strip():
            clean_share = share_cmd.rstrip().rstrip(";").rstrip()
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", f"export HOME=/sandbox; {clean_share}"],
                client=None,
            )

        # Model selection config
        if model_setup_cmd.strip():
            clean_cmd = model_setup_cmd.rstrip().rstrip("&").rstrip()
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", f"export HOME=/sandbox; {clean_cmd}"],
                client=None,
            )

        # Write AGENTS.md for all modes (prompt, tui, server).
        # TUI/server: the agent reads it automatically as system-level instructions.
        # Prompt mode: the agent command reads it via "$(</sandbox/AGENTS.md)" shell
        # expansion, giving the full context (prompt + repo layout) identically to TUI.
        if agents_md:
            await openshell_client.write_agents_md(sandbox_name=ref.name, content=agents_md)

        await _update_db(status_detail="")

        # Register gh as the git credential helper so that git clone/push/fetch
        # can authenticate via GH_TOKEN injected by the OpenShell provider.
        # Must run before any git clone calls.  Guarded by has_git_token: sessions
        # without any git credential cannot have GitHub repos (enforced at launch time).
        if has_git_token:
            await openshell_client.exec_command(
                ref.name,
                ["sh", "-c", "export HOME=/sandbox; gh auth setup-git"],
                client=None,
            )

        # Clone repos — network policies are pre-applied via SandboxSpec.policy so the
        # git binary has Landlock network access to github.com immediately at sandbox
        # creation time.  No probe-deny-approve cycle needed (ACM-34909).
        if repos_data:
            for rd in repos_data:
                local_path = rd["local_path"]
                repo_url = rd["url"]
                # HOME=/sandbox must match the gh auth setup-git call above so git
                # reads /sandbox/.gitconfig where the credential helper was registered.
                clone_cmd = f"cd /sandbox && HOME=/sandbox git clone {shlex.quote(repo_url)} {shlex.quote(local_path)}"
                result = await openshell_client.exec_command(
                    ref.name, ["sh", "-c", clone_cmd], client=None
                )
                if getattr(result, "exit_code", 0) != 0:
                    _stdout = getattr(result, "stdout", "") or ""
                    _stderr = getattr(result, "stderr", "") or ""
                    log.warning(
                        "sandbox setup: git clone failed for %s (exit %s):\n%s",
                        local_path,
                        getattr(result, "exit_code", "?"),
                        (_stdout + _stderr).strip(),
                    )
            await openshell_client.exec_command(
                ref.name, ["sh", "-c", "git config --global --add safe.directory '*'"], client=None
            )
            # Checkout each repo's configured branch before creating the working
            # branch so the working branch is based on the correct upstream ref
            # (e.g. "develop") rather than always the repository default branch.
            for rd in repos_data:
                repo_branch = rd.get("branch", "")
                if repo_branch:
                    checkout_base_cmd = (
                        f"cd /sandbox/{shlex.quote(rd['local_path'])} && "
                        f"git checkout {shlex.quote(repo_branch)}"
                    )
                    result = await openshell_client.exec_command(
                        ref.name, ["sh", "-c", checkout_base_cmd], client=None
                    )
                    if getattr(result, "exit_code", 0) != 0:
                        _stdout = getattr(result, "stdout", "") or ""
                        _stderr = getattr(result, "stderr", "") or ""
                        log.warning(
                            "sandbox setup: git checkout %s failed for %s (exit %s):\n%s",
                            repo_branch,
                            rd["local_path"],
                            getattr(result, "exit_code", "?"),
                            (_stdout + _stderr).strip(),
                        )
            if working_branch:
                for rd in repos_data:
                    branch_cmd = (
                        f"cd /sandbox/{shlex.quote(rd['local_path'])} && "
                        f"git checkout -b {shlex.quote(working_branch)} 2>/dev/null "
                        f"|| git checkout {shlex.quote(working_branch)}"
                    )
                    result = await openshell_client.exec_command(
                        ref.name, ["sh", "-c", branch_cmd], client=None
                    )
                    if getattr(result, "exit_code", 0) != 0:
                        _stdout = getattr(result, "stdout", "") or ""
                        _stderr = getattr(result, "stderr", "") or ""
                        log.warning(
                            "sandbox setup: git checkout working branch %s failed for %s (exit %s):\n%s",
                            working_branch,
                            rd["local_path"],
                            getattr(result, "exit_code", "?"),
                            (_stdout + _stderr).strip(),
                        )

        # Build the agent command.
        # Prompt mode: AGENTS.md was written above (prompt + repo context); read it at
        # runtime via "$(</sandbox/AGENTS.md)" shell expansion — no newlines in args,
        # full context identical to TUI mode.
        # TUI/server: main_cmd is "sleep infinity" / "opencode serve …"; agent is
        # started later by the WebSocket handler or start_agent().
        if mode == "prompt":
            _tool_bin = {"opencode": "opencode run"}.get(tool_name, "opencode run")
            if agents_md:
                # Read the full AGENTS.md (prompt + repo context) as the CLI argument.
                _model_arg = shlex.quote(model) if model else ""
                agent_cmd = f"HOME=/sandbox {_tool_bin} --model {_model_arg} \"$(</sandbox/AGENTS.md)\""
            else:
                # No prompt configured — launch without a message argument.
                agent_cmd = f"HOME=/sandbox {main_cmd}"
        else:
            # Server and TUI modes: export HOME and PATH, cd into /sandbox/.
            # Mirrors tui_ws.py exactly: HOME=/sandbox, PATH includes /sandbox/.local/bin.
            agent_cmd = (
                f"export HOME=/sandbox PATH=\"/sandbox/.local/bin:$PATH\" && "
                f"{main_cmd}"
            )

        # Launch agent — pass env_vars so JIRA_* (and any other non-provider
        # credentials) are forwarded into the agent process via ExecSandboxRequest.
        # spec.environment is stored on the sandbox but is NOT forwarded to exec
        # calls by the OpenShell gateway; env_vars in ExecSandboxRequest IS.
        asyncio.create_task(
            _run_openshell_agent(
                session_id=session_id,
                workspace_id=workspace_id,
                sandbox_name=ref.name,
                cmd=["sh", "-c", agent_cmd],
                mode=mode,
                agent_tool=tool_name,
                env_vars=env_vars,
                pat_id=pat_id,
            ),
            name=f"openshell-agent-{session_id}",
        )

        # GitHub App IAT refresh loop: for TUI and server sessions that may run
        # longer than 1 hour, re-mint the IAT before it expires and update the
        # OpenShell Gateway provider so GH_TOKEN stays valid in the sandbox.
        # Prompt-mode sessions are typically short-lived — no refresh needed.
        if iat_app_id and iat_installation_id and iat_private_key and iat_provider_name and mode != "prompt":
            from swarmer.github_auth import start_token_refresh_loop

            # Build a lightweight snapshot object so the refresh loop does not
            # hold a reference to the ORM session or require DB access.
            class _AppSnap:
                app_id = iat_app_id
                installation_id = iat_installation_id
                private_key = iat_private_key

            asyncio.create_task(
                start_token_refresh_loop(
                    app=_AppSnap(),  # type: ignore[arg-type]
                    session_id=session_id,
                    provider_name=iat_provider_name,
                    repo_names=iat_repo_names or None,
                ),
                name=f"iat-refresh-{session_id}",
            )

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("_setup_openshell_sandbox failed for session %d", session_id)
        await _update_db(phase="failed", run_completed_at=datetime.now(timezone.utc))


async def _run_openshell_agent(
    session_id: int,
    workspace_id: int,
    sandbox_name: str,
    cmd: list[str],
    mode: str,
    agent_tool: str,
    env_vars: dict | None = None,
    pat_id: int | None = None,
) -> None:
    """Background task: starts the agent in the sandbox and tracks completion."""
    from swarmer import openshell_client
    from swarmer.database import get_db as _get_db
    from swarmer.models.session import Session as _Session

    _TERMINAL_PHASES = frozenset(("succeeded", "failed", "stopped"))

    async def _update_db(**fields) -> None:
        from swarmer.session_runs import record_session_run as _record_run

        async for _db in _get_db():
            _s = await _db.get(_Session, session_id)
            if _s:
                # Do not overwrite a "stopped" state set by the STOP handler —
                # this task may have been cancelled or lost the race with STOP.
                # "idle" is the initial launch state so we must not block it.
                if _s.phase == "stopped" and "phase" in fields:
                    log.info(
                        "_update_db: session %d is already 'stopped'; skipping task write (would set phase=%s)",
                        session_id, fields["phase"],
                    )
                    break
                new_phase = fields.get("phase")
                if new_phase in _TERMINAL_PHASES and _s.phase not in _TERMINAL_PHASES:
                    completed_at = fields.get("run_completed_at") or datetime.now(timezone.utc)
                    await _record_run(
                        _db,
                        _s,
                        phase=new_phase,
                        status_detail=fields.get("status_detail", ""),
                        last_output=fields.get("last_output", _s.last_output or ""),
                        raw_output=fields.get("raw_output", _s.raw_output or ""),
                        completed_at=completed_at,
                    )
                for k, v in fields.items():
                    setattr(_s, k, v)
                await _db.commit()
            break

    try:
        if mode != "server":
            # Server mode stays "pending" until expose_service succeeds and the
            # service_url is stored — the Chat tab only becomes accessible then.
            await _update_db(phase="running")

        if mode == "prompt":
            # Stream exec with no timeout — agent runs to natural completion.
            # on_output is called every 5 s with accumulated stdout/stderr so the
            # HTMX UI updates incrementally without waiting for the run to finish.
            # OpenCode writes minimal stdout (content lives in its SQLite DB), so
            # we do a final read_opencode_response call after the exec completes.
            _streamed: list[str] = []  # tracks last value passed to on_output

            async def _on_output(text: str) -> None:
                _streamed[:] = [text]
                await _update_db(last_output=text, raw_output=text)

            result = await openshell_client.exec_command_streaming(
                sandbox_name, cmd,
                on_output=_on_output,
                poll_interval=5.0,
                env=env_vars or {},
            )
            exit_code = getattr(result, "exit_code", None)
            stderr = getattr(result, "stderr", "") or ""
            phase = "succeeded" if exit_code == 0 else "failed"

            # OpenCode stores the response in its SQLite DB, not stdout.
            # On success: prefer the SQLite response (full conversation).
            # On failure: SQLite may be empty; prefer the accumulated streaming
            # output over the sparse ExecResult.stdout (which is the same
            # incremental stdout that on_output already captured, but only the
            # last chunk — the accumulated buffer has everything).
            _streamed_text = _streamed[0] if _streamed else ""
            output = (
                await openshell_client.read_opencode_response(sandbox_name)
                or _streamed_text
                or stderr
            )

            # Snapshot draft policy chunks before any sandbox deletion so the
            # Policy tab can show what was denied/proposed during this run.
            chunks_json = ""
            try:
                chunks = await openshell_client.get_draft_chunks(sandbox_name)
                if chunks:
                    import json as _json_chunks
                    chunks_json = _json_chunks.dumps(chunks)
            except Exception:
                log.warning("Failed to snapshot policy chunks for %s", sandbox_name, exc_info=True)

            new_sandbox_name: str | None = sandbox_name
            if phase == "succeeded":
                try:
                    await openshell_client.delete_sandbox(sandbox_name)
                    new_sandbox_name = None
                except Exception:
                    log.warning("Auto-cleanup of sandbox %s failed", sandbox_name, exc_info=True)
                # Providers can only be deleted after sandbox is gone (Gateway rejects
                # DeleteProvider with FAILED_PRECONDITION while sandbox is still attached).
                await _delete_github_app_provider(workspace_id, session_id)
                await _delete_pat_provider(workspace_id, pat_id, session_id)
            else:
                # failed/stopped — sandbox left running for debugging; providers stay
                # attached and will be cleaned up when the session is stopped/deleted.
                log.info(
                    "_run_openshell_agent: skipping provider cleanup for phase=%s session=%d "
                    "(sandbox still attached — cleanup on explicit stop/delete)",
                    phase, session_id,
                )

            await _update_db(
                phase=phase,
                last_output=output,
                raw_output=_streamed_text,  # preserve raw console log regardless of agent tool
                status_detail="",  # clear any stale status from previous runs
                policy_chunks=chunks_json,
                run_completed_at=datetime.now(timezone.utc),
                sandbox_name=new_sandbox_name,
                active_schedule_id=None,
            )
        else:
            if mode == "server":
                # Launch server agent as a background nohup process so exec() returns
                # immediately; the sandbox stays alive serving HTTP.
                # Provider env vars (GOOGLE_API_KEY etc.) are inherited from the
                # sandbox environment automatically — no explicit injection needed.
                await openshell_client.start_agent(sandbox_name, cmd, env=env_vars or {})

            # TUI mode: the sandbox is ready; the TUI WebSocket handler starts the
            # agent interactively via exec_interactive when the user connects.

            # For server mode, expose the agent HTTP port so the chat proxy can reach it
            if mode == "server":
                from swarmer.agent_tools.registry import get as _get_tool
                _tool = _get_tool(agent_tool)
                port = _tool.get_server_port() or 4096
                try:
                    await _update_db(status_detail="Waiting for server to start…")
                    await asyncio.sleep(8)  # let the server process start listening
                    service_url = await openshell_client.expose_service(
                        sandbox_name, "agent", port
                    )
                    # Transition to running and store the URL atomically so the
                    # Chat tab is only accessible once it's reachable.
                    await _update_db(phase="running", service_url=service_url, status_detail="")
                except Exception:
                    log.exception(
                        "ExposeService failed for session %d sandbox %s",
                        session_id, sandbox_name,
                    )
                    await _update_db(
                        phase="failed",
                        status_detail="Failed to expose service URL — check server logs",
                        run_completed_at=datetime.now(timezone.utc),
                    )

    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("_run_openshell_agent failed for session %d", session_id)
        await _update_db(
            phase="failed",
            status_detail="OpenShell agent startup failed",
            run_completed_at=datetime.now(timezone.utc),
        )


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
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    mode: str = Form(""),
    provider: str = Form(""),
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
        # Persist prompt_id selection
        if prompt_id:
            try:
                pid = int(prompt_id)
                p = await db.get(WorkspacePrompt, pid)
                if p:
                    result = await db.execute(
                        select(WorkspacePromptSource).where(WorkspacePromptSource.id == p.source_id)
                    )
                    source = result.scalar_one_or_none()
                    if source and source.workspace_id == ws_id:
                        session.prompt_id = pid
                    else:
                        session.prompt_id = None
                else:
                    session.prompt_id = None
            except ValueError:
                session.prompt_id = None
        else:
            session.prompt_id = None
        session.instruction_prompt = instruction_prompt.strip()
        if mode in ("tui", "server", "prompt"):
            session.mode = mode
        if provider.strip():
            session.provider = provider.strip()
    else:
        # List-page launch: no explicit mode chosen — default to prompt so the
        # session runs once and exits rather than starting a TUI or server.
        session.mode = "prompt"

    if agent_tool:
        try:
            canonical = get_tool(agent_tool).name
            if canonical != session.agent_tool:
                session.agent_tool = canonical
                session.provider = ""  # stale provider from previous tool may be incompatible
        except ValueError:
            pass

    try:
        await _do_launch(session, ws, db, user_id=request.session.get("username", ""))
        if session.phase == "queued":
            flash(request, f"Session queued — {session.status_detail}", "info")
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

    if session.phase == "queued":
        session.phase = "idle"
        session.status_detail = ""
        await db.commit()
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    # Cancel any background tasks for this session before touching the DB so
    # the task cannot race and overwrite the "stopped" phase we're about to set.
    _task_names = (f"openshell-setup-{sid}", f"openshell-agent-{sid}")
    for _t in asyncio.all_tasks():
        if _t.get_name() in _task_names:
            _t.cancel()
            log.info("session_stop: cancelled background task %s", _t.get_name())

    if session.sandbox_name:
        from swarmer import openshell_client
        # Snapshot draft policy chunks before deleting the sandbox so the
        # Policy tab remains useful after the session is stopped.
        try:
            chunks = await openshell_client.get_draft_chunks(session.sandbox_name)
            if chunks:
                import json as _json_stop
                session.policy_chunks = _json_stop.dumps(chunks)
        except Exception:
            log.warning("Failed to snapshot policy chunks on stop for session %d", sid, exc_info=True)
        if session.service_url:
            try:
                await openshell_client.delete_service(session.sandbox_name, "agent")
            except Exception as exc:
                log.warning("DeleteService failed for session %d: %s", sid, exc)
        try:
            await openshell_client.delete_sandbox(session.sandbox_name)
        except Exception as exc:
            flash(request, f"Sandbox deletion failed: {exc}", "warning")
        session.sandbox_name = None
        session.service_url = None

    # Clean up GitHub credentials providers AFTER sandbox deletion — the Gateway
    # rejects DeleteProvider with FAILED_PRECONDITION if the sandbox is still attached.
    await _delete_github_app_provider(ws_id, sid)
    await _delete_pat_provider(ws_id, session.github_pat_id, sid)

    from swarmer.session_runs import STOPPED_BY_USER_DETAIL, record_session_run

    completed_at = datetime.now(timezone.utc)
    if session.run_started_at and session.phase in ("pending", "running"):
        await record_session_run(
            db,
            session,
            phase="stopped",
            status_detail=STOPPED_BY_USER_DETAIL,
            last_output=session.last_output,
            raw_output=session.raw_output,
            completed_at=completed_at,
        )
    session.run_completed_at = completed_at
    session.phase = "stopped"
    session.status_detail = STOPPED_BY_USER_DETAIL
    session.active_schedule_id = None

    # Advance cron_next_run so the scheduler doesn't immediately re-launch a
    # cron-scheduled session that was manually stopped — it would re-queue it
    # within the next 30-second poll if cron_next_run is already in the past.
    if session.cron_schedule and session.cron_next_run is not None:
        try:
            from croniter import croniter as _croniter
            session.cron_next_run = _croniter(
                session.cron_schedule, datetime.now(timezone.utc)
            ).get_next(datetime)
            log.info(
                "session_stop: advanced cron_next_run for session %d to %s",
                sid, session.cron_next_run,
            )
        except Exception:
            log.warning("session_stop: failed to advance cron_next_run for session %d", sid, exc_info=True)

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

    if not croniter.is_valid(cron_expr):
        flash(request, f"Invalid cron expression: {cron_expr}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}#schedule", status_code=302)

    session.cron_schedule = cron_expr
    session.cron_next_run = croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime)
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
# Session Schedules (HTMX sub-resource)
# ============================================================


async def _get_schedule_items_context(ws_id: int, sid: int, db: AsyncSession) -> dict:
    """Return context dict for the _schedule_items.html partial."""
    from swarmer.models.session_schedule import SessionSchedule
    result = await db.execute(
        select(SessionSchedule)
        .where(SessionSchedule.session_id == sid)
        .order_by(SessionSchedule.created_at)
    )
    schedules = result.scalars().all()
    prompt_sources = await _get_prompt_sources(ws_id, db)
    return {
        "schedules": schedules,
        "prompt_sources": prompt_sources,
        "cron_presets": CRON_PRESETS,
    }


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/schedules/items",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def schedule_items(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    ctx = await _get_schedule_items_context(ws_id, sid, db)
    return templates.TemplateResponse(
        request,
        "sessions/_schedule_items.html",
        {"ws": ws, "session": session, **ctx},
    )


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/schedules",
    dependencies=[Depends(require_auth)],
)
async def schedule_create(
    ws_id: int,
    sid: int,
    request: Request,
    cron_expr: str = Form(""),
    label: str = Form(""),
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    enabled: str = Form("on"),
    db: AsyncSession = Depends(get_db),
):
    from croniter import croniter as _croniter
    from swarmer.models.session_schedule import SessionSchedule

    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return HTMLResponse("", status_code=404)

    cron_expr = cron_expr.strip()
    if not cron_expr or not _croniter.is_valid(cron_expr):
        return HTMLResponse("", status_code=422, headers={"HX-Trigger": "scheduleFormError"})

    pid = int(prompt_id) if prompt_id.strip().isdigit() else None
    sched = SessionSchedule(
        session_id=sid,
        cron_schedule=cron_expr,
        cron_next_run=_croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime),
        label=label.strip(),
        prompt_id=pid,
        instruction_prompt=instruction_prompt,
        enabled=(enabled == "on"),
    )
    db.add(sched)
    await db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "scheduleListChanged"})


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}/edit",
    dependencies=[Depends(require_auth)],
)
async def schedule_edit(
    ws_id: int,
    sid: int,
    sched_id: int,
    request: Request,
    cron_expr: str = Form(""),
    label: str = Form(""),
    prompt_id: str = Form(""),
    instruction_prompt: str = Form(""),
    enabled: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    from croniter import croniter as _croniter
    from swarmer.models.session_schedule import SessionSchedule

    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    sched = await db.get(SessionSchedule, sched_id)
    if ws is None or session is None or session.workspace_id != ws_id or sched is None or sched.session_id != sid:
        return HTMLResponse("", status_code=404)

    cron_expr = cron_expr.strip()
    if not cron_expr or not _croniter.is_valid(cron_expr):
        return HTMLResponse("", status_code=422, headers={"HX-Trigger": "scheduleFormError"})

    sched.cron_schedule = cron_expr
    sched.cron_next_run = _croniter(cron_expr, datetime.now(timezone.utc)).get_next(datetime)
    sched.label = label.strip()
    sched.prompt_id = int(prompt_id) if prompt_id.strip().isdigit() else None
    sched.instruction_prompt = instruction_prompt
    sched.enabled = (enabled == "on")
    await db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "scheduleListChanged"})


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}/delete",
    dependencies=[Depends(require_auth)],
)
async def schedule_delete(
    ws_id: int,
    sid: int,
    sched_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from swarmer.models.session_schedule import SessionSchedule

    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    sched = await db.get(SessionSchedule, sched_id)
    if ws is None or session is None or session.workspace_id != ws_id or sched is None or sched.session_id != sid:
        return HTMLResponse("", status_code=404)

    await db.delete(sched)
    await db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "scheduleListChanged"})


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}/toggle",
    dependencies=[Depends(require_auth)],
)
async def schedule_toggle(
    ws_id: int,
    sid: int,
    sched_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from swarmer.models.session_schedule import SessionSchedule

    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    sched = await db.get(SessionSchedule, sched_id)
    if ws is None or session is None or session.workspace_id != ws_id or sched is None or sched.session_id != sid:
        return HTMLResponse("", status_code=404)

    sched.enabled = not sched.enabled
    await db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "scheduleListChanged"})


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

    status_detail = session.status_detail
    queue_position = None

    if session.phase == "queued":
        queue_position = await _get_queue_position(session.id, db)

    return templates.TemplateResponse(
        request,
        "sessions/_status_badge.html",
        {
            "ws": ws,
            "session": session,
            "mode_label": _session_mode_label(session),
            "status_detail": status_detail,
            "queue_position": queue_position,
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
# Policy chunks (HTMX) + custom policy rules CRUD
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/policy-chunks",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_policy_chunks(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial: return draft policy chunks.

    While the session has a live sandbox, fetch chunks directly from the
    gateway.  Otherwise return the snapshot stored in session.policy_chunks.
    """
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")

    chunks: list[dict] = []
    if session.sandbox_name and session.is_active:
        # Live fetch from gateway while sandbox is running
        from swarmer import openshell_client
        try:
            chunks = await openshell_client.get_draft_chunks(session.sandbox_name)
        except Exception:
            pass  # get_draft_chunks logs internally; [] is the safe fallback
    elif session.policy_chunks:
        # Completed run — use snapshot
        import json as _j
        try:
            chunks = _j.loads(session.policy_chunks)
        except Exception:
            pass

    # Build a mapping of rule_name → set-of-binary-paths for all rules already
    # added this session.  The template uses this to determine per-chunk "added"
    # status: a chunk is fully added only when its rule exists AND every binary
    # it lists is already covered.  Same rule_name + different binary = still pending.
    import json as _j2
    promoted_binaries: dict[str, set[str]] = {}
    if session.custom_policies:
        try:
            for r in _j2.loads(session.custom_policies):
                name = r.get("name")
                if name:
                    promoted_binaries[name] = {
                        b.get("path", "") for b in r.get("binaries", [])
                    }
        except Exception:
            pass

    return templates.TemplateResponse(
        request,
        "sessions/_policy_chunks.html",
        {"ws_id": ws_id, "session": session, "chunks": chunks, "promoted_binaries": promoted_binaries},
    )


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/policy-rules-partial",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_policy_rules_partial(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """HTMX partial: refresh the custom policy rules list."""
    import json as _j
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    rules: list[dict] = []
    if session.custom_policies:
        try:
            rules = _j.loads(session.custom_policies)
        except Exception:
            pass
    return templates.TemplateResponse(
        request,
        "sessions/_policy_rules.html",
        {"ws_id": ws_id, "session": session, "rules": rules},
    )


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/policy-rules/add",
    dependencies=[Depends(require_auth)],
)
async def session_policy_rules_add(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Promote selected draft chunks to session-level custom_policies."""
    import json as _j

    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("", status_code=404)

    form = await request.form()
    # The form submits chunk JSON blobs for each selected chunk under key "chunk"
    # (multiple values possible when multiple checkboxes are ticked).
    selected_raw = form.getlist("chunk")

    existing: list[dict] = []
    if session.custom_policies:
        try:
            existing = _j.loads(session.custom_policies)
        except Exception:
            pass

    # Index existing rules by name for O(1) lookup and in-place merge.
    existing_by_name: dict[str, dict] = {
        str(r["name"]): r for r in existing if r.get("name")
    }
    added = 0
    from datetime import timezone
    now_iso = datetime.now(timezone.utc).isoformat()

    def _normalize_endpoints(raw_eps: list) -> list:
        """Ensure every L7-protocol endpoint has access or rules.

        Draft chunks from OPA include host/port/protocol but omit these fields,
        which causes gateway validation to fail with 'protocol requires rules or
        access to define allowed traffic'. Default to access=full for
        user-approved traffic.
        """
        result = []
        for ep in raw_eps:
            ep = dict(ep)
            if ep.get("protocol") and not ep.get("access") and not ep.get("rules"):
                ep["access"] = "full"
            result.append(ep)
        return result

    for raw in selected_raw:
        try:
            chunk = _j.loads(raw)
        except Exception:
            continue
        rule_name = chunk.get("rule_name") or chunk.get("name") or ""
        if not rule_name:
            continue

        new_eps = _normalize_endpoints(chunk.get("endpoints", []))
        new_bins = chunk.get("binaries", [])

        if rule_name in existing_by_name:
            # Same rule name already exists — merge binaries rather than
            # dropping the chunk. OPA emits one chunk per (rule_name, binary)
            # pair so two chunks can share a name but differ only in binary.
            rule = existing_by_name[rule_name]
            existing_bin_paths = {b.get("path") for b in rule.get("binaries", [])}
            merged = False
            for b in new_bins:
                if b.get("path") not in existing_bin_paths:
                    rule.setdefault("binaries", []).append(b)
                    existing_bin_paths.add(b.get("path"))
                    merged = True
            if merged:
                added += 1
        else:
            chunk_id = chunk.get("id") or ""
            existing.append({
                "name": rule_name,
                "endpoints": new_eps,
                "binaries": new_bins,
                "source": "chunk",
                "added_at": now_iso,
                # Store the chunk ID so live-revoke (delete path) can call
                # UndoDraftChunk directly without a GetDraftHistory lookup.
                # Empty string means the rule was merged into an existing entry
                # or the ID was unavailable; revoke falls back to history lookup.
                "chunk_id": chunk_id,
            })
            existing_by_name[rule_name] = existing[-1]
            added += 1

    if added:
        session.custom_policies = _j.dumps(existing)
        await db.commit()

        # Live-apply to running sandbox: approve the draft chunks immediately
        # so new network rules take effect without a session restart.
        live_applied = False
        if session.sandbox_name and session.is_active:
            import swarmer.openshell_client as _oc
            # Collect chunk IDs for newly-promoted rules (may be empty strings
            # for binary-merge updates — those were already approved earlier).
            chunk_ids = [
                chunk.get("id", "")
                for raw in selected_raw
                for chunk in [(_j.loads(raw) if isinstance(raw, str) else raw)]
                if chunk.get("id")
            ]
            if chunk_ids:
                try:
                    n = await _oc.approve_chunks_by_id(session.sandbox_name, chunk_ids)
                    live_applied = n > 0
                except Exception as exc:
                    log.warning(
                        "session %d: live-apply to sandbox %s failed (rule persisted, "
                        "will apply on next launch): %s",
                        sid, session.sandbox_name, exc,
                    )

        trigger_val = _j.dumps({"policyChanged": {"added": added, "live_applied": live_applied}})
        return HTMLResponse("", headers={"HX-Trigger": trigger_val})

    # Nothing added — either no checkboxes were submitted or all were duplicates.
    if selected_raw:
        return HTMLResponse("", headers={"HX-Trigger": "policyNoop"})
    return HTMLResponse("", headers={"HX-Trigger": "policyChanged"})


@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/policy-rules/{idx}/delete",
    dependencies=[Depends(require_auth)],
)
async def session_policy_rules_delete(
    ws_id: int,
    sid: int,
    idx: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Remove a custom policy rule by index."""
    import json as _j

    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("", status_code=404)

    rules: list[dict] = []
    if session.custom_policies:
        try:
            rules = _j.loads(session.custom_policies)
        except Exception:
            pass

    deleted = False
    live_revoked = False
    deleted_rule: dict | None = None
    if 0 <= idx < len(rules):
        deleted_rule = rules.pop(idx)
        session.custom_policies = _j.dumps(rules)
        await db.commit()
        deleted = True

    # Live-revoke from running sandbox: undo the approved draft chunk so the
    # rule is removed from the active policy without requiring a restart.
    # Only possible for rules that were approved via the draft mechanism during
    # this sandbox session. Rules baked into the startup policy cannot be
    # revoked live; the caller is informed via live_revoked=False.
    if deleted and deleted_rule and session.sandbox_name and session.is_active:
        import swarmer.openshell_client as _oc
        rule_name = deleted_rule.get("name", "")
        # Use stored chunk_id if available (fast path); fall back to history lookup.
        stored_chunk_id = deleted_rule.get("chunk_id", "")
        chunk_ids = [stored_chunk_id] if stored_chunk_id else []
        try:
            n = await _oc.undo_chunks_by_rule_name(
                session.sandbox_name,
                rule_names=[rule_name],
                chunk_ids=chunk_ids or None,
            )
            live_revoked = n > 0
        except Exception as exc:
            log.warning(
                "session %d: live-revoke of rule '%s' from sandbox %s failed "
                "(rule removed from DB, will not apply on next launch): %s",
                sid, rule_name, session.sandbox_name, exc,
            )

    trigger_val = _j.dumps({
        "policyChanged": {
            "deleted": 1 if deleted else 0,
            "live_revoked": live_revoked,
        }
    })
    return HTMLResponse("", headers={"HX-Trigger": trigger_val})


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
    session.raw_output = ""
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

    if session.sandbox_name:
        # OpenShell session — delete sandbox
        from swarmer import openshell_client
        if session.service_url:
            try:
                await openshell_client.delete_service(session.sandbox_name, "agent")
            except Exception as exc:
                log.warning("DeleteService failed for session %d: %s", sid, exc)
        try:
            await openshell_client.delete_sandbox(session.sandbox_name)
        except Exception as exc:
            flash(request, f"Sandbox deletion failed: {exc}", "warning")

    # Clean up GitHub credentials providers (App IAT and PAT).
    await _delete_github_app_provider(ws_id, sid)
    await _delete_pat_provider(ws_id, session.github_pat_id, sid)

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
# Set provider (server / TUI modes — works while running)
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/set-provider",
    dependencies=[Depends(require_auth)],
)
async def session_set_provider(
    ws_id: int,
    sid: int,
    request: Request,
    provider: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    session = await db.get(Session, sid)
    if ws is None or session is None or session.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions", status_code=302)

    session.provider = provider.strip()
    await db.commit()

    flash(request, "Provider saved; will apply on next launch.", "success")

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

    try:
        validate_github_url(repo_url.strip())
    except GitHubURLError as exc:
        log.warning(
            "repo_add: rejected URL with embedded token for session %s: %s",
            sid,
            exc.redacted_url,
        )
        flash(request, f"Repository URL rejected: {exc.reason}", "danger")
        return HTMLResponse("", status_code=422, headers={"HX-Trigger": "repoAddError"})

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


async def _build_commit_msg(patch: str, workspace_id: int, db: AsyncSession) -> str:
    """Use an LLM to generate a commit message from the diff."""
    truncated = patch[:8000] if len(patch) > 8000 else patch

    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == workspace_id)
    )
    oc = oc_result.scalars().first()
    if not oc:
        return _fallback_commit_msg(patch)

    try:
        if oc.google_api_key:
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
    # Resolve token BEFORE additional DB queries to avoid async session interference.
    _repo_check_token = await _resolve_token_for_repo_check(
        session, db, user_id=_current_user(request)
    )
    repo_info = await _fetch_repo_info(session.repos, _repo_check_token)
    from swarmer.github_app import get_workspace_github_app
    _app = await get_workspace_github_app(ws_id, db, user_id=_current_user(request))
    log.debug("repo_items: session %d pat=%s token_present=%s repo_info=%s",
              session.id, bool(session.github_pat), bool(_repo_check_token), repo_info)
    return templates.TemplateResponse(
        request,
        "sessions/_repo_items.html",
        {"ws_id": ws_id, "session": session, "repo_info": repo_info,
         "has_github_app": bool(_app)},
    )


@router.get(
    "/workspaces/{ws_id}/sessions/{sid}/repos/pick",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def repo_pick(
    ws_id: int,
    sid: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    pat_id: int = 0,
):
    """Return an HTMX partial listing repos for the selected PAT or GitHub App.

    pat_id=0 (default) means: use the workspace GitHub App installation repos.
    Any non-zero pat_id fetches repos accessible via that PAT.
    """
    session = await db.get(Session, sid)
    if session is None or session.workspace_id != ws_id:
        return HTMLResponse("")
    if session.is_active:
        return HTMLResponse("", status_code=409)

    # GitHub App path: pat_id=0 → list repos via installation token.
    if pat_id == 0:
        from swarmer.github_app import get_workspace_github_app
        from swarmer.github_auth import mint_installation_token
        app = await get_workspace_github_app(
            ws_id, db, user_id=_current_user(request)
        )
        if not app:
            return templates.TemplateResponse(
                request,
                "sessions/_repo_picker.html",
                {"error": "No GitHub App configured for this workspace.",
                 "repos": [], "truncated": False,
                 "ws_id": ws_id, "session": session},
            )
        try:
            iat = await mint_installation_token(app)
        except Exception as exc:
            return templates.TemplateResponse(
                request,
                "sessions/_repo_picker.html",
                {"error": f"Failed to mint GitHub App token: {exc}",
                 "repos": [], "truncated": False,
                 "ws_id": ws_id, "session": session},
            )
        result = await _list_repos_for_github_app(iat)
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

    # PAT path: fetch repos via the named PAT.
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

import asyncio
import re
import uuid

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer import k8s, k8s_session as k8s_sess
from swarmer.config import settings, LANGUAGE_OPTIONS
from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.opencode_secret import OpencodeSecret
from swarmer.models.session import Session
from swarmer.models.session_repo import SessionRepo
from swarmer.models.workspace import Workspace

# Model options offered in Prompt mode.
# Each entry: (value_passed_to_opencode_run, display_label, requires)
# requires: "gemini" | "claude"
_GEMINI_MODELS = [
    ("google/gemini-2.5-flash",  "Gemini 2.5 Flash (fast)"),
    ("google/gemini-2.5-pro",    "Gemini 2.5 Pro"),
]
_CLAUDE_MODELS = [
    ("google-vertex-anthropic/claude-haiku-4-5@20251001",  "Claude Haiku 4.5 (fast)"),
    ("google-vertex-anthropic/claude-sonnet-4-6@default",  "Claude Sonnet 4.6 (balanced)"),
    ("google-vertex-anthropic/claude-opus-4-6@default",    "Claude Opus 4.6 (most capable)"),
]


async def _get_model_options(ws_id: int, db: AsyncSession) -> list[dict]:
    """Return the available model choices for this workspace's Prompt sessions."""
    result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc = result.scalar_one_or_none()
    options = []
    if oc and oc.google_api_key_enc:
        for value, label in _GEMINI_MODELS:
            options.append({"value": value, "label": label, "group": "Gemini"})
    if oc and oc.has_adc:
        for value, label in _CLAUDE_MODELS:
            options.append({"value": value, "label": label, "group": "Claude (Vertex)"})
    return options

def _github_slug(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, or None if not a GitHub URL."""
    m = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


async def _fetch_repo_info(repos: list, pat: str | None) -> dict:
    """Return per-repo visibility and push-access info via the GitHub API.

    Result shape: {repo_id: {"is_public": bool|None, "can_push": bool|None}}
    None means the check could not be performed (non-GitHub URL, API error, etc.)
    """
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"token {pat}"

    async def _check(repo) -> tuple[int, dict]:
        slug = _github_slug(repo.repo_url)
        if not slug:
            return repo.id, {"is_public": None, "can_push": None}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"https://api.github.com/repos/{slug}", headers=headers
                )
            if r.status_code == 200:
                data = r.json()
                perms = data.get("permissions", {})
                return repo.id, {
                    "is_public": not data.get("private", True),
                    "can_push": perms.get("push"),
                }
            # 404 with no auth → private repo that the token can't see
            return repo.id, {"is_public": None, "can_push": False if pat else None}
        except Exception:
            return repo.id, {"is_public": None, "can_push": None}

    results = await asyncio.gather(*[_check(r) for r in repos])
    return dict(results)


router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


def _session_mode_label(session: Session) -> str:
    """Return a human-readable mode label for the session."""
    return {"tui": "TUI", "server": "Server", "prompt": "Prompt"}.get(session.mode, session.mode)


def _session_mode_badge_class(session: Session) -> str:
    return {"tui": "primary", "server": "info", "prompt": "secondary"}.get(session.mode, "secondary")


# ============================================================
# Session list
# ============================================================

@router.get("/workspaces/{ws_id}/sessions", dependencies=[Depends(require_auth)])
async def session_list(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat), selectinload(Session.repos))
        .order_by(Session.name)
    )
    sessions = result.scalars().all()
    return templates.TemplateResponse(
        "sessions/list.html",
        {
            "request": request,
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
        },
    )


# ============================================================
# Sessions list rows (HTMX polling partial)
# ============================================================

@router.get(
    "/workspaces/{ws_id}/sessions/list-rows",
    dependencies=[Depends(require_auth)],
    response_class=HTMLResponse,
)
async def session_list_rows(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    """Return only the tbody rows for the sessions list table (HTMX polling partial)."""
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return HTMLResponse("")

    result = await db.execute(
        select(Session)
        .where(Session.workspace_id == ws_id)
        .options(selectinload(Session.github_pat), selectinload(Session.repos))
        .order_by(Session.name)
    )
    sessions = result.scalars().all()
    return templates.TemplateResponse(
        "sessions/_list_rows.html",
        {
            "request": request,
            "ws": ws,
            "sessions": sessions,
            "mode_label": _session_mode_label,
            "mode_badge": _session_mode_badge_class,
        },
    )


# ============================================================
# Create
# ============================================================

async def _image_status(namespace: str) -> dict[str, bool]:
    """Return reachability status for each language image variant."""
    results = await asyncio.gather(
        *[k8s.check_image_reachable(settings.image_for_language(lang), namespace)
          for lang in LANGUAGE_OPTIONS],
        return_exceptions=True,
    )
    return {
        lang: (r is True)
        for lang, r in zip(LANGUAGE_OPTIONS, results)
    }


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
    model_options = await _get_model_options(ws_id, db)
    image_status = await _image_status(ws.namespace)
    return templates.TemplateResponse(
        "sessions/new.html",
        {
            "request": request,
            "ws": ws,
            "pats": pats,
            "model_options": model_options,
            "image_status": image_status,
            "language_options": LANGUAGE_OPTIONS,
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
    privileged: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    language: str = Form("golang"),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    pat_id = int(github_pat_id) if github_pat_id else None
    if mode not in ("tui", "server", "prompt"):
        mode = "prompt"
    if language not in LANGUAGE_OPTIONS:
        language = "golang"

    session = Session(
        workspace_id=ws_id,
        github_pat_id=pat_id,
        name=name.strip(),
        mode=mode,
        model=model.strip(),
        language=language,
        persist=persist,
        resume=resume,
        privileged=privileged,
        instruction_prompt=instruction_prompt.strip(),
    )
    db.add(session)
    try:
        await db.commit()
        await db.refresh(session)
    except IntegrityError:
        await db.rollback()
        pats_result = await db.execute(
            select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id)
        )
        pats = pats_result.scalars().all()
        return templates.TemplateResponse(
            "sessions/new.html",
            {
                "request": request,
                "ws": ws,
                "pats": pats,
                "error": f"A session named '{name}' already exists in this workspace.",
                "form": {"name": name, "instruction_prompt": instruction_prompt},
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

    # Generate one-time TUI token for TUI-mode sessions
    tui_token = None
    if session.mode == "tui" and session.phase == "running":
        tui_token = str(uuid.uuid4())
        tokens = request.session.setdefault("tui_tokens", [])
        tokens.append(tui_token)

    pats_result = await db.execute(
        select(GitHubPAT).where(GitHubPAT.workspace_id == ws_id).order_by(GitHubPAT.name)
    )
    pats = pats_result.scalars().all()

    # Fetch live K8s detail for the initial page render
    status_detail = ""
    if session.pod_name:
        _, status_detail = k8s.get_pod_status(session.pod_name, ws.namespace)

    model_options = await _get_model_options(ws_id, db)
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    image_status = await _image_status(ws.namespace)

    return templates.TemplateResponse(
        "sessions/detail.html",
        {
            "request": request,
            "ws": ws,
            "ws_id": ws_id,
            "session": session,
            "pats": pats,
            "tui_token": tui_token,
            "mode_label": _session_mode_label(session),
            "mode_badge": _session_mode_badge_class(session),
            "status_detail": status_detail,
            "model_options": model_options,
            "repo_info": repo_info,
            "image_status": image_status,
            "language_options": LANGUAGE_OPTIONS,
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
    privileged: bool = Form(False),
    mode: str = Form("prompt"),
    model: str = Form(""),
    language: str = Form("golang"),
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
    session.privileged = privileged
    if mode in ("tui", "server", "prompt"):
        session.mode = mode
    session.model = model.strip()
    if language in LANGUAGE_OPTIONS:
        session.language = language
    await db.commit()
    flash(request, "Session updated.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Launch / Stop
# ============================================================

@router.post(
    "/workspaces/{ws_id}/sessions/{sid}/launch",
    dependencies=[Depends(require_auth)],
)
async def session_launch(
    ws_id: int,
    sid: int,
    request: Request,
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

    # Generate a shared suffix so the pod and PVC share the same identifier
    import secrets as _secrets
    suffix = _secrets.token_hex(4)

    # Ensure PVC exists; if it was deleted (persist=False), a new one is created
    # with the same suffix as the pod about to be launched.
    try:
        pvc_name = k8s_sess.ensure_session_pvc(ws.namespace, session.id, suffix, session.pvc_name)
        if pvc_name != session.pvc_name:
            session.pvc_name = pvc_name
    except Exception as exc:
        flash(request, f"PVC error: {exc}", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)

    # Check whether the workspace has an ADC JSON stored (affects pod spec)
    from swarmer.models.opencode_secret import OpencodeSecret
    oc_result = await db.execute(
        select(OpencodeSecret).where(OpencodeSecret.workspace_id == ws_id)
    )
    oc_secret = oc_result.scalar_one_or_none()
    has_adc    = oc_secret.has_adc if oc_secret else False
    has_gemini = bool(oc_secret and oc_secret.google_api_key_enc)

    # Build and create pod
    try:
        pod_spec = k8s_sess.build_session_pod(
            session=session,
            namespace=ws.namespace,
            image=settings.image_for_language(session.language),
            suffix=suffix,
            image_pull_secret=k8s.PULL_SECRET_NAME,
            has_adc=has_adc,
            has_gemini=has_gemini,
            privileged=session.privileged,
        )
        from kubernetes import client as k8s_client

        v1 = k8s_client.CoreV1Api()

        if session.pod_name:
            k8s.delete_pod(session.pod_name, ws.namespace)

        pod = v1.create_namespaced_pod(ws.namespace, pod_spec)
        session.pod_name = pod.metadata.name
        session.phase = "pending"

        # Create a Service for server-mode sessions (exposes opencode serve port)
        if session.mode == "server":
            k8s_sess.create_session_service(session.id, ws.namespace)

        await db.commit()
    except Exception as exc:
        flash(request, f"Launch failed: {exc}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


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
        k8s.delete_pod(session.pod_name, ws.namespace)
        if session.mode == "server":
            k8s.delete_service(f"session-{session.id}-svc", ws.namespace)

    if not session.persist and session.pvc_name:
        try:
            k8s_sess.delete_session_pvc(ws.namespace, session.pvc_name)
            session.pvc_name = None
        except Exception as exc:
            flash(request, f"PVC deletion failed: {exc}", "warning")

    session.phase = "stopped"
    session.pod_name = None
    await db.commit()
    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


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

    status_detail = ""
    if session.pod_name:
        phase, status_detail = k8s.get_pod_status(session.pod_name, ws.namespace)
        session.phase = phase
        logs = k8s.get_pod_logs(session.pod_name, ws.namespace)
        if logs:
            session.last_output = logs
        await db.commit()

    return templates.TemplateResponse(
        "sessions/_status_badge.html",
        {
            "request": request,
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
        "sessions/_last_output.html",
        {"request": request, "ws_id": ws_id, "session": session},
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
            k8s_sess.delete_session_pvc(ws.namespace, session.pvc_name)
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
            k8s.exec_model_json(session.pod_name, ws.namespace, session.model)
            flash(request, "Model applied to running pod.", "success")
        except Exception as exc:
            flash(request, f"Model saved but could not apply to running pod: {exc}", "warning")
    else:
        flash(request, "Model saved; will apply on next launch.", "success")

    return RedirectResponse(url=f"/workspaces/{ws_id}/sessions/{sid}", status_code=302)


# ============================================================
# Git Repos (HTMX)
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

    if not local_path:
        local_path = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    repo = SessionRepo(
        session_id=sid,
        repo_url=repo_url.strip(),
        branch=branch.strip() or "main",
        local_path=local_path.strip(),
    )
    db.add(repo)
    try:
        await db.commit()
        await db.refresh(session)
    except IntegrityError:
        await db.rollback()

    session = await db.get(Session, sid, options=[selectinload(Session.repos), selectinload(Session.github_pat)])
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    return templates.TemplateResponse(
        "sessions/_repo_list.html",
        {"request": request, "ws_id": ws_id, "session": session, "repo_info": repo_info},
    )


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
    repo = await db.get(SessionRepo, rid)
    if repo and repo.session_id == sid:
        await db.delete(repo)
        await db.commit()

    session = await db.get(Session, sid, options=[selectinload(Session.repos), selectinload(Session.github_pat)])
    pat_token = session.github_pat.pat if session.github_pat else None
    repo_info = await _fetch_repo_info(session.repos, pat_token)
    return templates.TemplateResponse(
        "sessions/_repo_list.html",
        {"request": request, "ws_id": ws_id, "session": session, "repo_info": repo_info},
    )

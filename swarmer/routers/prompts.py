import html
import logging

from fastapi import APIRouter, Depends, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from swarmer.database import get_db
from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.github import (
    fetch_folder_prompts,
    github_slug,
    list_folder_contents,
    list_repos_for_pat,
)
from swarmer.models.github_pat import GitHubPAT
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_prompt import WorkspacePrompt, WorkspacePromptSource

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


def _current_user(request: Request) -> str:
    return request.session.get("username", "")


async def _get_workspace(ws_id: int, db: AsyncSession) -> Workspace | None:
    return await db.get(Workspace, ws_id)


async def _visible_pats(ws_id: int, db: AsyncSession, user_id: str = "") -> list:
    from swarmer.routers.sessions import _visible_pats as _pats
    return await _pats(ws_id, db, user_id)


@router.get("/workspaces/{ws_id}/prompts", dependencies=[Depends(require_auth)])
async def prompt_source_list(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    result = await db.execute(
        select(WorkspacePromptSource)
        .where(WorkspacePromptSource.workspace_id == ws_id)
        .options(selectinload(WorkspacePromptSource.prompts))
        .order_by(WorkspacePromptSource.name)
    )
    sources = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "prompts/list.html",
        {"ws": ws, "sources": sources},
    )


@router.get("/workspaces/{ws_id}/prompts/new", dependencies=[Depends(require_auth)])
async def prompt_source_new(
    ws_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)
    
    pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
    return templates.TemplateResponse(
        request,
        "prompts/form.html",
        {"ws": ws, "source": None, "pats": pats},
    )


@router.post("/workspaces/{ws_id}/prompts", dependencies=[Depends(require_auth)])
async def prompt_source_create(
    ws_id: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    repo_url: str = Form(...),
    branch: str = Form("main"),
    folder_path: str = Form("."),
    db: AsyncSession = Depends(get_db),
):
    ws = await _get_workspace(ws_id, db)
    if ws is None:
        return RedirectResponse(url="/workspaces", status_code=302)

    try:
        pat_id = int(github_pat_id) if github_pat_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid github_pat_id")
    
    source = WorkspacePromptSource(
        workspace_id=ws_id,
        name=name.strip(),
        github_pat_id=pat_id,
        repo_url=repo_url.strip(),
        branch=branch.strip() or "main",
        folder_path=folder_path.strip() or ".",
    )
    db.add(source)
    try:
        await db.commit()
        await db.refresh(source)
    except IntegrityError:
        await db.rollback()
        flash(request, f"A prompt source named '{name}' already exists.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/prompts/new", status_code=302)

    # Trigger initial sync
    await _refresh_source_logic(source, db)
    await db.commit()

    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


@router.get("/workspaces/{ws_id}/prompts/{ps_id}/edit", dependencies=[Depends(require_auth)])
async def prompt_source_edit(
    ws_id: int, ps_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    ws = await _get_workspace(ws_id, db)
    source = await db.get(WorkspacePromptSource, ps_id)
    if ws is None or source is None or source.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)
    
    pats = await _visible_pats(ws_id, db, user_id=_current_user(request))
    return templates.TemplateResponse(
        request,
        "prompts/form.html",
        {"ws": ws, "source": source, "pats": pats},
    )


@router.post("/workspaces/{ws_id}/prompts/{ps_id}/edit", dependencies=[Depends(require_auth)])
async def prompt_source_update(
    ws_id: int,
    ps_id: int,
    request: Request,
    name: str = Form(...),
    github_pat_id: str = Form(""),
    repo_url: str = Form(...),
    branch: str = Form("main"),
    folder_path: str = Form("."),
    db: AsyncSession = Depends(get_db),
):
    source = await db.get(WorkspacePromptSource, ps_id)
    if source is None or source.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)

    source.name = name.strip()
    try:
        source.github_pat_id = int(github_pat_id) if github_pat_id else None
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid github_pat_id")
    source.repo_url = repo_url.strip()
    source.branch = branch.strip() or "main"
    source.folder_path = folder_path.strip() or "."

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        flash(request, "A prompt source with that name already exists.", "danger")
        return RedirectResponse(url=f"/workspaces/{ws_id}/prompts/{ps_id}/edit", status_code=302)

    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


@router.post("/workspaces/{ws_id}/prompts/{ps_id}/delete", dependencies=[Depends(require_auth)])
async def prompt_source_delete(
    ws_id: int, ps_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    source = await db.get(WorkspacePromptSource, ps_id)
    if source is None or source.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)

    await db.delete(source)
    await db.commit()
    flash(request, "Prompt source deleted.", "success")
    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


@router.post("/workspaces/{ws_id}/prompts/{ps_id}/refresh", dependencies=[Depends(require_auth)])
async def prompt_source_refresh(
    ws_id: int, ps_id: int, request: Request, db: AsyncSession = Depends(get_db)
):
    source = await db.get(WorkspacePromptSource, ps_id)
    if source is None or source.workspace_id != ws_id:
        return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)

    await _refresh_source_logic(source, db)
    await db.commit()
    
    if source.sync_error:
        flash(request, f"Refresh failed: {source.sync_error}", "danger")
    else:
        flash(request, "Prompts refreshed successfully.", "success")
        
    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


async def _refresh_source_logic(source: WorkspacePromptSource, db: AsyncSession):
    slug = github_slug(source.repo_url)
    if not slug or slug.count("/") != 1:
        source.sync_error = "Invalid GitHub URL"
        return

    pat_token = None
    if source.github_pat_id:
        pat = await db.get(GitHubPAT, source.github_pat_id)
        if pat:
            pat_token = pat.pat

    owner, repo = slug.split("/", 1)
    results = await fetch_folder_prompts(
        owner, repo, source.folder_path, source.branch, pat_token
    )

    if isinstance(results, str):
        source.sync_error = results
        return

    source.sync_error = ""
    source.last_synced_at = func.now()

    # Track existing prompts to detect deletions
    result = await db.execute(
        select(WorkspacePrompt).where(WorkspacePrompt.source_id == source.id)
    )
    existing_prompts = {p.filename: p for p in result.scalars()}
    new_filenames = {r["filename"] for r in results}

    # Delete prompts no longer in the folder
    for filename, p in existing_prompts.items():
        if filename not in new_filenames:
            await db.delete(p)

    # Create or update prompts
    for r in results:
        filename = r["filename"]
        content = r["content"]
        sha = r["sha"]
        
        # Calculate display name
        display_name = filename.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        
        if filename in existing_prompts:
            p = existing_prompts[filename]
            if p.content_hash != sha:
                p.content = content
                p.content_hash = sha
                p.display_name = display_name
        else:
            p = WorkspacePrompt(
                source_id=source.id,
                filename=filename,
                display_name=display_name,
                content=content,
                content_hash=sha,
            )
            db.add(p)


# ============================================================
# HTMX Partials for browsing
# ============================================================

@router.get("/workspaces/{ws_id}/prompts/browse-repo", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def browse_repo(
    ws_id: int, request: Request, github_pat_id: str = "", db: AsyncSession = Depends(get_db)
):
    try:
        pat_id = int(github_pat_id) if github_pat_id else None
    except ValueError:
        return HTMLResponse("Invalid github_pat_id", status_code=400)
    
    repos = []
    if pat_id:
        pat = await db.get(GitHubPAT, pat_id)
        if pat:
            res = await list_repos_for_pat(pat)
            if isinstance(res, list):
                repos = res
    
    return templates.TemplateResponse(
        request,
        "sessions/_repo_picker.html",
        {"repos": repos},
    )


@router.get("/workspaces/{ws_id}/prompts/browse-folder", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def browse_folder(
    ws_id: int,
    request: Request,
    repo_url: str,
    branch: str = "main",
    path: str = ".",
    github_pat_id: str = "",
    db: AsyncSession = Depends(get_db),
):
    slug = github_slug(repo_url)
    if not slug or slug.count("/") != 1:
        return HTMLResponse("Invalid GitHub URL")

    pat_token = None
    if github_pat_id:
        try:
            pat = await db.get(GitHubPAT, int(github_pat_id))
            if pat:
                pat_token = pat.pat
        except ValueError:
            return HTMLResponse("Invalid github_pat_id", status_code=400)

    owner, repo = slug.split("/", 1)
    contents = await list_folder_contents(owner, repo, path, branch, pat_token)
    
    if isinstance(contents, str):
        log.error("browse_folder error: %s", contents)
        return HTMLResponse("An error occurred while listing folder contents")

    # Filter for directories
    dirs = [c for c in contents if c["type"] == "dir"]
    
    return templates.TemplateResponse(
        request,
        "prompts/_folder_picker.html",
        {
            "ws_id": ws_id,
            "repo_url": repo_url,
            "branch": branch,
            "current_path": path.strip("/"),
            "dirs": dirs,
            "github_pat_id": github_pat_id,
        },
    )


@router.get("/workspaces/{ws_id}/sessions/prompt-preview", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def prompt_preview(
    ws_id: int, request: Request, prompt_id: str = "", db: AsyncSession = Depends(get_db)
):
    if not prompt_id:
        return HTMLResponse("")
    
    try:
        pid = int(prompt_id)
    except ValueError:
        return HTMLResponse("Invalid prompt_id", status_code=400)

    prompt = await db.get(WorkspacePrompt, pid)
    if not prompt:
        return HTMLResponse("")
    
    escaped_content = html.escape(prompt.content)
    return HTMLResponse(f"""
        <div style="margin-top:var(--pf-t--global--spacer--sm); padding:var(--pf-t--global--spacer--sm); background:var(--pf-t--global--background--color--secondary--default); border-radius:var(--pf-t--global--border-radius--sm); border:1px solid var(--pf-t--global--border--color--default);">
          <div style="font-size:var(--pf-t--global--font--size--xs); color:var(--pf-t--global--text--color--subtle); margin-bottom:4px; text-transform:uppercase;">Preview</div>
          <pre style="font-size:var(--pf-t--global--font--size--sm); white-space:pre-wrap; margin:0; max-height:200px; overflow-y:auto;">{escaped_content}</pre>
        </div>
    """)

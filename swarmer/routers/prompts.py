"""Console routes — Prompt source management.

All data access goes through the REST API client (/api/v1/) for CRUD
operations.  Shared helper function (_refresh_source_logic) is kept for
backward compatibility with API v1 routes that import it.
"""

import html
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from swarmer.deps import require_auth
from swarmer.flash import flash
from swarmer.github import (
    fetch_folder_prompts,
    github_slug,
)
from swarmer.routers.api_client import APIError, get_api_client

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="swarmer/templates")


def _current_user(request: Request) -> str:
    return request.session.get("username", "")


@router.get("/workspaces/{ws_id}/prompts", dependencies=[Depends(require_auth)])
async def prompt_source_list(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        try:
            sources = await api.list_prompt_sources(ws_id)
        except APIError:
            sources = []

    # Enrich sources with github_pat info for template (list.html uses s.github_pat.name)
    # The API PromptSourceOut includes github_pat_id but not the PAT object.
    # We add a stub github_pat dict with name for template compatibility.
    async with get_api_client(request) as api:
        try:
            pats = await api.list_pats(ws_id)
            pat_map = {p["id"]: p for p in pats}
        except APIError:
            pat_map = {}

    for source in sources:
        pat_id = source.get("github_pat_id")
        if pat_id and pat_id in pat_map:
            source["github_pat"] = pat_map[pat_id]
        else:
            source["github_pat"] = None

    return templates.TemplateResponse(
        request,
        "prompts/list.html",
        {"ws": ws, "sources": sources},
    )


@router.get("/workspaces/{ws_id}/prompts/new", dependencies=[Depends(require_auth)])
async def prompt_source_new(ws_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
        except APIError:
            return RedirectResponse(url="/workspaces", status_code=302)

        try:
            pats = await api.list_pats(ws_id)
        except APIError:
            pats = []

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
):
    pat_id = int(github_pat_id) if github_pat_id else None

    async with get_api_client(request) as api:
        try:
            await api.create_prompt_source(
                ws_id,
                name=name.strip(),
                repo_url=repo_url.strip(),
                branch=branch.strip() or "main",
                folder_path=folder_path.strip() or ".",
                github_pat_id=pat_id,
            )
        except APIError as exc:
            flash(request, exc.detail, "danger")
            return RedirectResponse(
                url=f"/workspaces/{ws_id}/prompts/new", status_code=302
            )

    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


@router.get("/workspaces/{ws_id}/prompts/{ps_id}/edit", dependencies=[Depends(require_auth)])
async def prompt_source_edit(ws_id: int, ps_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            ws = await api.get_workspace(ws_id)
            source = await api.get_prompt_source(ws_id, ps_id)
        except APIError:
            return RedirectResponse(
                url=f"/workspaces/{ws_id}/prompts", status_code=302
            )

        try:
            pats = await api.list_pats(ws_id)
        except APIError:
            pats = []

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
):
    pat_id = int(github_pat_id) if github_pat_id else None

    async with get_api_client(request) as api:
        try:
            await api.update_prompt_source(
                ws_id,
                ps_id,
                name=name.strip(),
                repo_url=repo_url.strip(),
                branch=branch.strip() or "main",
                folder_path=folder_path.strip() or ".",
                github_pat_id=pat_id,
            )
        except APIError as exc:
            flash(request, exc.detail, "danger")
            return RedirectResponse(
                url=f"/workspaces/{ws_id}/prompts/{ps_id}/edit",
                status_code=302,
            )

    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


@router.post("/workspaces/{ws_id}/prompts/{ps_id}/delete", dependencies=[Depends(require_auth)])
async def prompt_source_delete(ws_id: int, ps_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            await api.delete_prompt_source(ws_id, ps_id)
            flash(request, "Prompt source deleted.", "success")
        except APIError:
            pass

    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


@router.post("/workspaces/{ws_id}/prompts/{ps_id}/refresh", dependencies=[Depends(require_auth)])
async def prompt_source_refresh(ws_id: int, ps_id: int, request: Request):
    async with get_api_client(request) as api:
        try:
            source = await api.refresh_prompt_source(ws_id, ps_id)
            sync_err = source.get("sync_error", "")
            if sync_err:
                flash(request, f"Refresh failed: {sync_err}", "danger")
            else:
                flash(request, "Prompts refreshed successfully.", "success")
        except APIError as exc:
            flash(request, f"Refresh failed: {exc.detail}", "danger")

    return RedirectResponse(url=f"/workspaces/{ws_id}/prompts", status_code=302)


# ============================================================
# Shared helper — imported by API v1 routes
# ============================================================

async def _refresh_source_logic(source, db: AsyncSession):
    """Refresh prompts from GitHub for the given source.

    This is a shared helper imported by API v1 routes — it still needs
    direct DB access.  The ``source`` parameter is a SQLAlchemy model
    instance.
    """
    from swarmer.models.github_pat import GitHubPAT
    from swarmer.models.workspace_prompt import WorkspacePrompt

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

    result = await db.execute(
        select(WorkspacePrompt).where(WorkspacePrompt.source_id == source.id)
    )
    existing_prompts = {p.filename: p for p in result.scalars()}
    new_filenames = {r["filename"] for r in results}

    for filename, p in existing_prompts.items():
        if filename not in new_filenames:
            await db.delete(p)

    for r in results:
        filename = r["filename"]
        content = r["content"]
        sha = r["sha"]
        display_name = (
            filename.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        )

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

@router.get(
    "/workspaces/{ws_id}/prompts/browse-repo",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def browse_repo(
    ws_id: int,
    request: Request,
    github_pat_id: str = "",
):
    repos = []
    if github_pat_id:
        try:
            pat_id = int(github_pat_id)
        except ValueError:
            return HTMLResponse("")

        async with get_api_client(request) as api:
            try:
                result = await api.browse_repos(ws_id, pat_id)
                repos = result if isinstance(result, list) else []
            except APIError:
                repos = []

    return templates.TemplateResponse(
        request,
        "prompts/_repo_picker.html",
        {"repos": repos},
    )


@router.get(
    "/workspaces/{ws_id}/prompts/browse-folder",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def browse_folder(
    ws_id: int,
    request: Request,
    repo_url: str,
    branch: str = "main",
    path: str = ".",
    github_pat_id: str = "",
):
    slug = github_slug(repo_url)
    if not slug or slug.count("/") != 1:
        return HTMLResponse("Invalid GitHub URL")

    pat_id = int(github_pat_id) if github_pat_id else None

    async with get_api_client(request) as api:
        try:
            dirs = await api.browse_folders(
                ws_id, repo_url, branch, path, github_pat_id=pat_id
            )
        except APIError as exc:
            log.error("browse_folder error: %s", exc.detail)
            return HTMLResponse("An error occurred while listing folder contents")

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


@router.get(
    "/workspaces/{ws_id}/sessions/prompt-preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_auth)],
)
async def prompt_preview(
    ws_id: int, request: Request, prompt_id: str = "",
):
    if not prompt_id:
        return HTMLResponse("")

    try:
        pid = int(prompt_id)
    except ValueError:
        return HTMLResponse("")

    # The API doesn't have a direct prompt preview by ID endpoint without
    # the source ID.  We need to find the source first, or use a custom query.
    # For now, we search across all sources in this workspace.
    async with get_api_client(request) as api:
        try:
            sources = await api.list_prompt_sources(ws_id)
            for source in sources:
                for prompt in source.get("prompts", []):
                    if prompt.get("id") == pid:
                        escaped = html.escape(prompt.get("content", ""))
                        return HTMLResponse(f"""
                            <div style="margin-top:var(--pf-t--global--spacer--sm); padding:var(--pf-t--global--spacer--sm); background:var(--pf-t--global--background--color--secondary--default); border-radius:var(--pf-t--global--border-radius--sm); border:1px solid var(--pf-t--global--border--color--default);">
                              <div style="font-size:var(--pf-t--global--font--size--xs); color:var(--pf-t--global--text--color--subtle); margin-bottom:4px; text-transform:uppercase;">Preview</div>
                              <pre style="font-size:var(--pf-t--global--font--size--sm); white-space:pre-wrap; margin:0; max-height:200px; overflow-y:auto;">{escaped}</pre>
                            </div>
                        """)
        except APIError:
            pass

    return HTMLResponse("")

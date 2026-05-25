"""Internal API client for Console routes to call /api/v1/ endpoints.

The Console (HTMX) routes use this client instead of direct DB/K8s access.
The client forwards the user's K8s bearer token as an Authorization header
so the API layer handles auth, validation, and business logic.

Usage in a Console route handler::

    from swarmer.routers.api_client import get_api_client

    @router.get("/workspaces")
    async def workspace_list(request: Request):
        async with get_api_client(request) as api:
            workspaces = await api.list_workspaces()
        return templates.TemplateResponse(...)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from fastapi import Request
from httpx import ASGITransport

from swarmer.deps import get_user_token

log = logging.getLogger(__name__)

# Date fields that should be parsed into datetime objects for template
# compatibility (templates call .strftime() on these).
_DATE_FIELDS = frozenset({
    "created_at", "updated_at", "last_synced_at",
    "run_started_at", "run_completed_at",
    "cron_next_run", "token_expires_at",
})


class DotDict(dict):
    """Dict subclass that supports attribute access and datetime parsing.

    Jinja2 uses ``getattr`` then falls back to ``__getitem__``, so plain
    dicts already work for most templates.  However, some templates call
    ``.strftime()`` on datetime fields, which requires actual datetime
    objects.  DotDict transparently parses ISO-format date strings on
    construction.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        for key in _DATE_FIELDS:
            val = self.get(key)
            if isinstance(val, str) and val:
                try:
                    # Handle ISO format with or without timezone
                    self[key] = datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    pass

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                f"'{type(self).__name__}' has no attribute '{name}'"
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None


def _wrap(data: Any) -> Any:
    """Recursively wrap dicts in DotDict and lists of dicts in lists of DotDict."""
    if isinstance(data, dict):
        return DotDict({k: _wrap(v) for k, v in data.items()})
    if isinstance(data, list):
        return [_wrap(item) for item in data]
    return data


class APIError(Exception):
    """Raised when an API call returns a non-2xx status."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


class APIClient:
    """Async HTTP client that calls /api/v1/ endpoints via ASGI transport.

    Parameters
    ----------
    app : FastAPI | None
        The FastAPI application instance. When provided, uses ASGI transport
        (in-process, no network hop). When None, uses HTTP transport.
    token : str
        K8s bearer token forwarded as ``Authorization: Bearer <token>``.
    base_url : str
        Base URL for API requests. Defaults to ``http://localhost``.
    """

    def __init__(
        self,
        *,
        app: Any = None,
        token: str,
        base_url: str = "http://localhost",
    ):
        self._token = token
        if app is not None:
            transport = ASGITransport(app=app)
            self._client = httpx.AsyncClient(
                transport=transport,
                base_url=base_url,
                headers={"Authorization": f"Bearer {token}"},
            )
        else:
            self._client = httpx.AsyncClient(
                base_url=base_url,
                headers={"Authorization": f"Bearer {token}"},
            )

    async def __aenter__(self) -> APIClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | list | None = None,
        params: dict | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Send an HTTP request and return parsed JSON or raw response."""
        resp = await self._client.request(method, path, json=json, params=params)
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail", str(body))
            except Exception:
                detail = resp.text
            raise APIError(resp.status_code, detail)
        if not expect_json:
            return resp
        if resp.status_code == 204 or not resp.content:
            return None
        data = resp.json()
        return _wrap(data)

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def _put(self, path: str, **kwargs: Any) -> Any:
        return await self._request("PUT", path, **kwargs)

    async def _delete(self, path: str, **kwargs: Any) -> Any:
        return await self._request("DELETE", path, **kwargs)

    # ==================================================================
    # Workspaces
    # ==================================================================

    @staticmethod
    def _enrich_workspace(ws: Any) -> Any:
        """Add ``k8s_namespace`` to workspace dict for template compat.

        The Workspace model has a ``k8s_namespace`` property that returns
        ``settings.k8s_namespace or self.namespace``.  We replicate that
        logic here so templates continue to work.
        """
        if isinstance(ws, dict):
            from swarmer.config import settings
            ws["k8s_namespace"] = settings.k8s_namespace or ws.get("namespace", "")
        return ws

    async def list_workspaces(self) -> list[dict]:
        result = await self._get("/api/v1/workspaces")
        return [self._enrich_workspace(ws) for ws in result]

    async def create_workspace(
        self, display_name: str, description: str = ""
    ) -> dict:
        ws = await self._post(
            "/api/v1/workspaces",
            json={"display_name": display_name, "description": description},
        )
        return self._enrich_workspace(ws)

    async def get_workspace(self, ws_id: int) -> dict:
        ws = await self._get(f"/api/v1/workspaces/{ws_id}")
        return self._enrich_workspace(ws)

    async def update_workspace(
        self, ws_id: int, display_name: str, description: str = ""
    ) -> dict:
        ws = await self._put(
            f"/api/v1/workspaces/{ws_id}",
            json={"display_name": display_name, "description": description},
        )
        return self._enrich_workspace(ws)

    async def delete_workspace(self, ws_id: int) -> dict:
        return await self._delete(f"/api/v1/workspaces/{ws_id}")

    # ==================================================================
    # Sessions
    # ==================================================================

    async def list_sessions(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions")

    async def create_session(
        self,
        ws_id: int,
        name: str,
        *,
        mode: str = "prompt",
        model: str = "",
        agent_tool: str = "opencode",
        instruction_prompt: str = "",
        github_pat_id: int | None = None,
        prompt_id: int | None = None,
        persist: bool = False,
        working_branch: str = "",
        mcp_server_ids: list[int] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "mode": mode,
            "model": model,
            "agent_tool": agent_tool,
            "instruction_prompt": instruction_prompt,
            "persist": persist,
            "working_branch": working_branch,
        }
        if github_pat_id is not None:
            body["github_pat_id"] = github_pat_id
        if prompt_id is not None:
            body["prompt_id"] = prompt_id
        if mcp_server_ids is not None:
            body["mcp_server_ids"] = mcp_server_ids
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions", json=body
        )

    async def get_session(self, ws_id: int, sid: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")

    async def update_session(self, ws_id: int, sid: int, **fields: Any) -> dict:
        return await self._put(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}", json=fields
        )

    async def delete_session(self, ws_id: int, sid: int) -> dict:
        return await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")

    async def launch_session(self, ws_id: int, sid: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/launch"
        )

    async def stop_session(self, ws_id: int, sid: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/stop"
        )

    async def set_session_name(self, ws_id: int, sid: int, name: str) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/set-name",
            json={"name": name},
        )

    async def set_session_mode(self, ws_id: int, sid: int, mode: str) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/set-mode",
            json={"mode": mode},
        )

    async def set_session_model(
        self, ws_id: int, sid: int, model: str
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/set-model",
            json={"model": model},
        )

    async def schedule_session(
        self, ws_id: int, sid: int, cron_expr: str
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedule",
            json={"cron_expr": cron_expr},
        )

    async def unschedule_session(self, ws_id: int, sid: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/unschedule",
        )

    async def get_session_output(self, ws_id: int, sid: int) -> dict:
        return await self._get(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/output"
        )

    async def clear_session_output(self, ws_id: int, sid: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/clear-output"
        )

    async def generate_patch(self, ws_id: int, sid: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/generate-patch"
        )

    async def download_patch(self, ws_id: int, sid: int) -> httpx.Response:
        return await self._request(
            "GET",
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/download-patch",
            expect_json=False,
        )

    # ==================================================================
    # Repos
    # ==================================================================

    async def list_repos(self, ws_id: int, sid: int) -> list[dict]:
        return await self._get(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos"
        )

    async def add_repo(
        self,
        ws_id: int,
        sid: int,
        repo_url: str,
        branch: str = "main",
        local_path: str = "",
    ) -> dict:
        body: dict[str, str] = {
            "repo_url": repo_url,
            "branch": branch,
        }
        if local_path:
            body["local_path"] = local_path
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos", json=body
        )

    async def delete_repo(self, ws_id: int, sid: int, rid: int) -> dict:
        return await self._delete(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos/{rid}"
        )

    # ==================================================================
    # Secrets / Credentials
    # ==================================================================

    async def get_credentials(self, ws_id: int) -> dict | None:
        return await self._get(f"/api/v1/workspaces/{ws_id}/secrets/credentials")

    async def save_credentials(
        self,
        ws_id: int,
        *,
        google_cloud_project: str = "",
        vertex_location: str = "",
        google_api_key: str = "",
        anthropic_api_key: str = "",
        openai_api_key: str = "",
        shared: bool = False,
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/secrets/credentials",
            json={
                "google_cloud_project": google_cloud_project,
                "vertex_location": vertex_location,
                "google_api_key": google_api_key,
                "anthropic_api_key": anthropic_api_key,
                "openai_api_key": openai_api_key,
                "shared": shared,
            },
        )

    async def list_pats(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/secrets/pats")

    async def create_pat(
        self,
        ws_id: int,
        *,
        name: str,
        github_username: str,
        pat_value: str,
        github_org: str = "",
        description: str = "",
        shared: bool = False,
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/secrets/pats",
            json={
                "name": name,
                "github_username": github_username,
                "pat_value": pat_value,
                "github_org": github_org,
                "description": description,
                "shared": shared,
            },
        )

    async def update_pat(self, ws_id: int, pat_id: int, **fields: Any) -> dict:
        return await self._put(
            f"/api/v1/workspaces/{ws_id}/secrets/pats/{pat_id}", json=fields
        )

    async def delete_pat(self, ws_id: int, pat_id: int) -> dict:
        return await self._delete(
            f"/api/v1/workspaces/{ws_id}/secrets/pats/{pat_id}"
        )

    async def get_pull_secret(self, ws_id: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}/secrets/pull-secret")

    async def create_pull_secret(
        self, ws_id: int, registry: str, username: str, password: str
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/secrets/pull-secret",
            json={
                "registry": registry,
                "username": username,
                "password": password,
            },
        )

    async def delete_pull_secret(self, ws_id: int) -> dict:
        return await self._delete(
            f"/api/v1/workspaces/{ws_id}/secrets/pull-secret"
        )

    # ==================================================================
    # Environment Variables
    # ==================================================================

    async def list_env_vars(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/env-vars")

    async def add_env_var(self, ws_id: int, key: str, value: str) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/env-vars",
            json={"key": key, "value": value},
        )

    async def delete_env_var(self, ws_id: int, key: str) -> dict:
        return await self._delete(f"/api/v1/workspaces/{ws_id}/env-vars/{key}")

    # ==================================================================
    # MCP Servers
    # ==================================================================

    async def list_mcp_servers(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/mcp-servers")

    async def add_mcp_from_catalog(
        self, ws_id: int, catalog_slug: str
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/mcp-servers",
            json={"catalog_slug": catalog_slug},
        )

    async def save_mcp_config(
        self,
        ws_id: int,
        server_id: int,
        *,
        jira_server_url: str,
        jira_email: str,
        jira_access_token: str = "",
    ) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/mcp-servers/{server_id}/save",
            json={
                "jira_server_url": jira_server_url,
                "jira_email": jira_email,
                "jira_access_token": jira_access_token,
            },
        )

    async def check_mcp_health(self, ws_id: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}/mcp-servers/check")

    async def toggle_mcp_server(self, ws_id: int, server_id: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/mcp-servers/{server_id}/toggle"
        )

    async def delete_mcp_server(self, ws_id: int, server_id: int) -> dict:
        return await self._delete(
            f"/api/v1/workspaces/{ws_id}/mcp-servers/{server_id}"
        )

    # ==================================================================
    # Prompts
    # ==================================================================

    async def list_prompt_sources(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/prompts")

    async def create_prompt_source(
        self,
        ws_id: int,
        *,
        name: str,
        repo_url: str,
        branch: str = "main",
        folder_path: str = ".",
        github_pat_id: int | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "repo_url": repo_url,
            "branch": branch,
            "folder_path": folder_path,
        }
        if github_pat_id is not None:
            body["github_pat_id"] = github_pat_id
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/prompts", json=body
        )

    async def get_prompt_source(self, ws_id: int, ps_id: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}/prompts/{ps_id}")

    async def update_prompt_source(
        self, ws_id: int, ps_id: int, **fields: Any
    ) -> dict:
        return await self._put(
            f"/api/v1/workspaces/{ws_id}/prompts/{ps_id}", json=fields
        )

    async def delete_prompt_source(self, ws_id: int, ps_id: int) -> dict:
        return await self._delete(
            f"/api/v1/workspaces/{ws_id}/prompts/{ps_id}"
        )

    async def refresh_prompt_source(self, ws_id: int, ps_id: int) -> dict:
        return await self._post(
            f"/api/v1/workspaces/{ws_id}/prompts/{ps_id}/refresh"
        )

    async def preview_prompt(
        self, ws_id: int, ps_id: int, prompt_id: int
    ) -> dict:
        return await self._get(
            f"/api/v1/workspaces/{ws_id}/prompts/{ps_id}/prompts/{prompt_id}/preview"
        )

    async def browse_repos(
        self, ws_id: int, github_pat_id: int
    ) -> list[dict]:
        return await self._get(
            f"/api/v1/workspaces/{ws_id}/prompts/browse/repos",
            params={"github_pat_id": github_pat_id},
        )

    async def browse_folders(
        self,
        ws_id: int,
        repo_url: str,
        branch: str = "main",
        path: str = ".",
        github_pat_id: int | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "repo_url": repo_url,
            "branch": branch,
            "path": path,
        }
        if github_pat_id is not None:
            params["github_pat_id"] = github_pat_id
        return await self._get(
            f"/api/v1/workspaces/{ws_id}/prompts/browse/folders",
            params=params,
        )


def get_api_client(request: Request) -> APIClient:
    """Create an APIClient from a Console request.

    Extracts the user's K8s bearer token from the session cookie and
    creates an ASGI-transport client against the co-located FastAPI app.
    """
    token = get_user_token(request)
    from swarmer.main import app
    return APIClient(app=app, token=token)

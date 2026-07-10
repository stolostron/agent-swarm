"""HTTP client wrapping the agent-swarm /api/v1/ REST API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class AgentSwarmAPIError(Exception):
    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")


class AgentSwarmClient:
    """Async httpx client for the agent-swarm REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        verify_ssl: bool = True,
        ssl_ca_bundle: str | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        # ssl_ca_bundle (path to PEM file/dir) takes precedence over the boolean flag
        verify: bool | str = ssl_ca_bundle if ssl_ca_bundle else verify_ssl
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
            verify=verify,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AgentSwarmClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

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
    ) -> Any:
        try:
            resp = await self._client.request(method, path, json=json, params=params)
        except httpx.HTTPError as e:
            raise AgentSwarmAPIError(0, f"Request failed: {e}") from e
        if resp.status_code == 401:
            raise AgentSwarmAPIError(
                401,
                "Unauthorized. Your K8s token may have expired. "
                "Re-run 'oc login' and restart the MCP server, "
                "or set AGENT_SWARM_API_TOKEN.",
            )
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail", str(body))
            except Exception:
                detail = resp.text
            raise AgentSwarmAPIError(resp.status_code, detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

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

    async def list_workspaces(self) -> list[dict]:
        return await self._get("/api/v1/workspaces")

    async def get_workspace(self, ws_id: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}")

    # ==================================================================
    # Sessions
    # ==================================================================

    async def list_sessions(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions")

    async def get_session(self, ws_id: int, sid: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")

    async def create_session(
        self,
        ws_id: int,
        name: str,
        *,
        mode: str = "prompt",
        provider: str = "",
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
            "provider": provider,
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
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions", json=body)

    async def update_session(self, ws_id: int, sid: int, **fields: Any) -> dict:
        return await self._put(f"/api/v1/workspaces/{ws_id}/sessions/{sid}", json=fields)

    async def delete_session(self, ws_id: int, sid: int) -> dict:
        return await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")

    async def launch_session(self, ws_id: int, sid: int) -> dict:
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/launch")

    async def stop_session(self, ws_id: int, sid: int) -> dict:
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/stop")

    async def get_session_output(self, ws_id: int, sid: int) -> dict:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/output")

    # ==================================================================
    # Repos
    # ==================================================================

    async def list_repos(self, ws_id: int, sid: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos")

    async def add_repo(
        self,
        ws_id: int,
        sid: int,
        repo_url: str,
        branch: str = "main",
        local_path: str = "",
    ) -> dict:
        body: dict[str, str] = {"repo_url": repo_url, "branch": branch}
        if local_path:
            body["local_path"] = local_path
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos", json=body)

    async def delete_repo(self, ws_id: int, sid: int, rid: int) -> dict:
        return await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos/{rid}")

    # ==================================================================
    # Prompts
    # ==================================================================

    async def list_prompt_sources(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/prompts")

    # ==================================================================
    # Session Schedules
    # ==================================================================

    async def list_session_schedules(self, ws_id: int, sid: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules")

    async def create_session_schedule(
        self,
        ws_id: int,
        sid: int,
        cron_schedule: str,
        *,
        label: str = "",
        prompt_id: int | None = None,
        instruction_prompt: str = "",
        enabled: bool = True,
    ) -> dict:
        body: dict = {
            "cron_schedule": cron_schedule,
            "label": label,
            "instruction_prompt": instruction_prompt,
            "enabled": enabled,
        }
        if prompt_id is not None:
            body["prompt_id"] = prompt_id
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules", json=body)

    async def update_session_schedule(
        self,
        ws_id: int,
        sid: int,
        sched_id: int,
        **fields: Any,
    ) -> dict:
        return await self._put(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}",
            json=fields,
        )

    async def delete_session_schedule(self, ws_id: int, sid: int, sched_id: int) -> None:
        await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}")

    # ==================================================================
    # GitHub PATs
    # ==================================================================

    async def list_pats(self, ws_id: int) -> list[dict]:
        return await self._get(f"/api/v1/workspaces/{ws_id}/secrets/pats")

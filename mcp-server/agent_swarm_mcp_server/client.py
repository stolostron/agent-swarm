"""HTTP client wrapping the agent-swarm /api/v1/ REST API."""

from __future__ import annotations

import logging
import urllib.parse
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
        """List all accessible workspaces."""
        return await self._get("/api/v1/workspaces")

    async def get_workspace(self, ws_id: int) -> dict:
        """Get details of a specific workspace by ID."""
        return await self._get(f"/api/v1/workspaces/{ws_id}")

    async def create_workspace(
        self,
        display_name: str,
        description: str = "",
    ) -> dict:
        """Create a new workspace."""
        body: dict[str, Any] = {
            "display_name": display_name,
            "description": description,
        }
        return await self._post("/api/v1/workspaces", json=body)

    async def update_workspace(
        self,
        ws_id: int,
        display_name: str,
        description: str | None = None,
    ) -> dict:
        """Update an existing workspace's name and optional description."""
        body: dict[str, Any] = {
            "display_name": display_name,
        }
        if description is not None:
            body["description"] = description
        return await self._put(f"/api/v1/workspaces/{ws_id}", json=body)

    async def delete_workspace(self, ws_id: int) -> dict:
        """Delete a workspace by ID."""
        return await self._delete(f"/api/v1/workspaces/{ws_id}")

    # ==================================================================
    # Workspace Members (ACM-41659)
    # ==================================================================

    async def list_workspace_members(self, ws_id: int) -> list[dict]:
        """List all members of a workspace."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/members")

    async def add_workspace_member(
        self,
        ws_id: int,
        user_id: str,
        role: str = "member",
    ) -> dict:
        """Add a user as a member or owner of a workspace."""
        body = {"user_id": user_id, "role": role}
        return await self._post(f"/api/v1/workspaces/{ws_id}/members", json=body)

    async def remove_workspace_member(self, ws_id: int, user_id: str) -> dict:
        """Remove a member from a workspace."""
        quoted_user = urllib.parse.quote(user_id, safe="")
        return await self._delete(f"/api/v1/workspaces/{ws_id}/members/{quoted_user}")

    # ==================================================================
    # Me / Identity & Global Admins (ACM-41659)
    # ==================================================================

    async def get_me(self) -> dict:
        """Get the authenticated caller's identity and global admin status."""
        return await self._get("/api/v1/me")

    async def list_known_users(self) -> list[str]:
        """List known users in the cluster."""
        data = await self._get("/api/v1/users")
        if isinstance(data, dict):
            return data.get("users", [])
        return data or []

    async def list_admins(self) -> list[dict]:
        """List all global Swarmer administrators."""
        return await self._get("/api/v1/admins")

    async def add_admin(self, user_id: str) -> dict:
        """Grant global administrator privileges to a user."""
        return await self._post("/api/v1/admins", json={"user_id": user_id})

    async def remove_admin(self, user_id: str) -> dict:
        """Revoke global administrator privileges from a user."""
        quoted_user = urllib.parse.quote(user_id, safe="")
        return await self._delete(f"/api/v1/admins/{quoted_user}")

    async def bootstrap_admin(self) -> dict:
        """Claim the initial global administrator role if none exist."""
        return await self._post("/api/v1/admins/bootstrap")

    # ==================================================================
    # Sessions
    # ==================================================================

    async def list_sessions(self, ws_id: int) -> list[dict]:
        """List all sessions in a workspace."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions")

    async def get_session(self, ws_id: int, sid: int) -> dict:
        """Get details and status of a session."""
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
        """Create a new agent session in a workspace."""
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
        """Update configuration fields of an existing session."""
        return await self._put(f"/api/v1/workspaces/{ws_id}/sessions/{sid}", json=fields)

    async def delete_session(self, ws_id: int, sid: int) -> dict:
        """Delete a non-running session."""
        return await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}")

    async def launch_session(self, ws_id: int, sid: int) -> dict:
        """Launch an idle or stopped session."""
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/launch")

    async def stop_session(self, ws_id: int, sid: int) -> dict:
        """Stop an active session."""
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/stop")

    async def get_session_output(self, ws_id: int, sid: int) -> dict:
        """Fetch execution logs / output of a session."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/output")

    # ==================================================================
    # Repos
    # ==================================================================

    async def list_repos(self, ws_id: int, sid: int) -> list[dict]:
        """List git repositories attached to a session."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos")

    async def add_repo(
        self,
        ws_id: int,
        sid: int,
        repo_url: str,
        branch: str = "main",
        local_path: str = "",
    ) -> dict:
        """Attach a git repository to a session."""
        body: dict[str, str] = {"repo_url": repo_url, "branch": branch}
        if local_path:
            body["local_path"] = local_path
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos", json=body)

    async def delete_repo(self, ws_id: int, sid: int, rid: int) -> dict:
        """Detach a git repository from a session."""
        return await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/repos/{rid}")

    # ==================================================================
    # Prompts
    # ==================================================================

    async def list_prompt_sources(self, ws_id: int) -> list[dict]:
        """List prompt sources in a workspace."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/prompts")

    # ==================================================================
    # Session Schedules
    # ==================================================================

    async def list_session_schedules(self, ws_id: int, sid: int) -> list[dict]:
        """List all schedules (cron and event triggers) for a session."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules")

    async def create_session_schedule(
        self,
        ws_id: int,
        sid: int,
        cron_schedule: str = "",
        *,
        trigger_type: str = "cron",
        event_condition: str = "",
        author_scope: str = "all",
        fix_authors: str = "",
        label: str = "",
        prompt_id: int,
        instruction_prompt: str = "",
        include_event_context: bool = True,
        enabled: bool = True,
    ) -> dict:
        """Create a cron or event execution schedule for a session.

        Event conditions include: ci_fail_or_conflict, new_pr_or_commit,
        review_comments, and any_actionable.
        """
        body: dict = {
            "trigger_type": trigger_type,
            "cron_schedule": cron_schedule,
            "event_condition": event_condition,
            "author_scope": author_scope,
            "fix_authors": fix_authors,
            "label": label,
            "instruction_prompt": instruction_prompt,
            "include_event_context": include_event_context,
            "enabled": enabled,
        }
        body["prompt_id"] = prompt_id
        return await self._post(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules", json=body)

    async def update_session_schedule(
        self,
        ws_id: int,
        sid: int,
        sched_id: int,
        **fields: Any,
    ) -> dict:
        """Update an existing session schedule."""
        return await self._put(
            f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}",
            json=fields,
        )

    async def delete_session_schedule(self, ws_id: int, sid: int, sched_id: int) -> None:
        """Delete a session schedule."""
        await self._delete(f"/api/v1/workspaces/{ws_id}/sessions/{sid}/schedules/{sched_id}")

    # ==================================================================
    # GitHub PATs
    # ==================================================================

    async def list_pats(self, ws_id: int) -> list[dict]:
        """List saved GitHub PAT credentials in a workspace."""
        return await self._get(f"/api/v1/workspaces/{ws_id}/secrets/pats")

"""FastMCP server exposing Agent Swarm operations as MCP tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from fastmcp import Context, FastMCP

from .client import AgentSwarmClient
from .config import AgentSwarmConfig

log = logging.getLogger(__name__)

_TERMINAL_PHASES = frozenset({"succeeded", "failed", "stopped"})


def _normalize_repo_url(url: str) -> str:
    """Normalize a GitHub HTTPS URL for comparison.

    Strips scheme, trailing .git suffix, trailing slashes, and lowercases host.
    """
    url = url.strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower().rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}{path}"


def _fmt_schedule(sc: dict) -> dict:
    return {
        "id": sc.get("id"),
        "trigger_type": sc.get("trigger_type", "cron"),
        "event_condition": sc.get("event_condition", ""),
        "author_scope": sc.get("author_scope", "all"),
        "fix_authors": sc.get("fix_authors", ""),
        "cron_schedule": sc.get("cron_schedule"),
        "cron_next_run": sc.get("cron_next_run"),
        "label": sc.get("label", ""),
        "prompt_id": sc.get("prompt_id"),
        "instruction_prompt": sc.get("instruction_prompt", ""),
        "include_event_context": sc.get("include_event_context", True),
        "enabled": sc.get("enabled", True),
    }


def _fmt_session(s: dict, repos: list[dict] | None = None) -> dict:
    result = {
        "id": s.get("id"),
        "name": s.get("name"),
        "phase": s.get("phase"),
        "mode": s.get("mode"),
        "provider": s.get("provider"),
        "agent_tool": s.get("agent_tool"),
        "persist": s.get("persist"),
        "working_branch": s.get("working_branch"),
        "prompt_id": s.get("prompt_id"),
        "instruction_prompt": s.get("instruction_prompt"),
        "status_detail": s.get("status_detail"),
        "run_duration": s.get("run_duration"),
        "run_started_at": s.get("run_started_at"),
        "run_completed_at": s.get("run_completed_at"),
        "is_active": s.get("is_active"),
        "workspace_id": s.get("workspace_id"),
        "schedules": [_fmt_schedule(sc) for sc in s.get("schedules", [])],
    }
    if repos is not None:
        result["repos"] = [
            {
                "id": r.get("id"),
                "repo_url": r.get("repo_url"),
                "branch": r.get("branch"),
                "local_path": r.get("local_path"),
            }
            for r in repos
        ]
    return result


class AgentSwarmMCPServer:
    def __init__(self, config: AgentSwarmConfig | None = None):
        self.mcp = FastMCP("Agent Swarm")
        self.config = config or AgentSwarmConfig.from_env()
        self.client = AgentSwarmClient(
            self.config.api_url,
            self.config.token,
            verify_ssl=self.config.verify_ssl,
            ssl_ca_bundle=self.config.ssl_ca_bundle,
        )
        self._register_tools()

    # ==================================================================
    # Tool implementations (testable as instance methods)
    # ==================================================================

    async def _list_workspaces(self) -> list[dict]:
        workspaces = await self.client.list_workspaces()
        return [
            {
                "id": ws.get("id"),
                "display_name": ws.get("display_name"),
                "namespace": ws.get("namespace"),
                "description": ws.get("description"),
                "gateway": ws.get("gateway"),
                "owner_id": ws.get("owner_id", ""),
            }
            for ws in workspaces
        ]

    async def _get_workspace(self, workspace_id: int) -> dict:
        return await self.client.get_workspace(workspace_id)

    async def _create_workspace(self, display_name: str, description: str = "") -> dict:
        return await self.client.create_workspace(display_name, description)

    async def _update_workspace(self, workspace_id: int, display_name: str, description: str | None = None) -> dict:
        return await self.client.update_workspace(workspace_id, display_name, description)

    async def _delete_workspace(self, workspace_id: int) -> dict:
        return await self.client.delete_workspace(workspace_id)

    async def _list_workspace_members(self, workspace_id: int) -> list[dict]:
        members = await self.client.list_workspace_members(workspace_id)
        return [
            {
                "id": m.get("id"),
                "workspace_id": m.get("workspace_id"),
                "user_id": m.get("user_id"),
                "role": m.get("role"),
            }
            for m in members
        ]

    async def _add_workspace_member(self, workspace_id: int, user_id: str, role: str = "member") -> dict:
        return await self.client.add_workspace_member(workspace_id, user_id, role)

    async def _remove_workspace_member(self, workspace_id: int, user_id: str) -> dict:
        return await self.client.remove_workspace_member(workspace_id, user_id)

    async def _get_me(self) -> dict:
        return await self.client.get_me()

    async def _list_known_users(self) -> list[str]:
        return await self.client.list_known_users()

    async def _list_admins(self) -> list[dict]:
        admins = await self.client.list_admins()
        return [
            {
                "id": a.get("id"),
                "user_id": a.get("user_id"),
                "created_by": a.get("created_by"),
            }
            for a in admins
        ]

    async def _add_admin(self, user_id: str) -> dict:
        return await self.client.add_admin(user_id)

    async def _remove_admin(self, user_id: str) -> dict:
        return await self.client.remove_admin(user_id)

    async def _bootstrap_admin(self) -> dict:
        return await self.client.bootstrap_admin()

    async def _get_workspace_gateway(self, workspace_id: int) -> dict:
        return await self.client.get_workspace_gateway(workspace_id)

    async def _set_workspace_gateway(
        self,
        workspace_id: int,
        gateway_url: str,
        auth_mode: str = "oidc",
        oidc_issuer: str | None = None,
        oidc_client_id: str | None = None,
        oidc_audience: str | None = None,
        refresh_token: str | None = None,
        bearer_token: str | None = None,
        tls_ca: str | None = None,
        tls_verify: bool = True,
    ) -> dict:
        payload = {
            "gateway_url": gateway_url,
            "auth_mode": auth_mode,
            "oidc_issuer": oidc_issuer,
            "oidc_client_id": oidc_client_id,
            "oidc_audience": oidc_audience,
            "refresh_token": refresh_token,
            "bearer_token": bearer_token,
            "tls_ca": tls_ca,
            "tls_verify": tls_verify,
        }
        return await self.client.set_workspace_gateway(workspace_id, payload)

    async def _delete_workspace_gateway(self, workspace_id: int) -> dict:
        return await self.client.delete_workspace_gateway(workspace_id)

    async def _test_workspace_gateway(
        self,
        gateway_url: str,
        auth_mode: str = "oidc",
        oidc_issuer: str | None = None,
        oidc_client_id: str | None = None,
        oidc_audience: str | None = None,
        refresh_token: str | None = None,
        bearer_token: str | None = None,
        tls_ca: str | None = None,
        tls_verify: bool = True,
    ) -> dict:
        payload = {
            "gateway_url": gateway_url,
            "auth_mode": auth_mode,
            "oidc_issuer": oidc_issuer,
            "oidc_client_id": oidc_client_id,
            "oidc_audience": oidc_audience,
            "refresh_token": refresh_token,
            "bearer_token": bearer_token,
            "tls_ca": tls_ca,
            "tls_verify": tls_verify,
        }
        return await self.client.test_gateway_connection(payload)

    async def _parse_gateway_command(self, command: str) -> dict:
        return await self.client.parse_gateway_command(command)

    async def _parse_gateway_token(self, token_input: str) -> dict:
        return await self.client.parse_gateway_token(token_input)

    async def _list_sessions(
        self,
        workspace_id: int,
        phase: str | None = None,
    ) -> list[dict]:
        sessions = await self.client.list_sessions(workspace_id)
        if phase:
            sessions = [s for s in sessions if s.get("phase") == phase]
        return [_fmt_session(s) for s in sessions]

    async def _get_session(self, workspace_id: int, session_id: int) -> dict:
        session, repos = await asyncio.gather(
            self.client.get_session(workspace_id, session_id),
            self.client.list_repos(workspace_id, session_id),
        )
        return _fmt_session(session, repos)

    async def _find_sessions_by_repo(
        self,
        workspace_id: int,
        repo_url: str,
    ) -> list[dict]:
        target = _normalize_repo_url(repo_url)
        sessions = await self.client.list_sessions(workspace_id)

        async def _check(s: dict) -> tuple[dict, list[dict]]:
            repos = await self.client.list_repos(workspace_id, s["id"])
            return s, repos

        results = await asyncio.gather(*[_check(s) for s in sessions])

        matched = []
        for session, repos in results:
            for repo in repos:
                if _normalize_repo_url(repo.get("repo_url", "")) == target:
                    matched.append(_fmt_session(session, repos))
                    break
        return matched

    async def _create_session(
        self,
        workspace_id: int,
        name: str,
        agent_tool: str = "opencode",
        mode: str = "prompt",
        provider: str = "",
        persist: bool = False,
        working_branch: str = "",
        instruction_prompt: str = "",
        github_pat_id: int | None = None,
        prompt_id: int | None = None,
    ) -> dict:
        session = await self.client.create_session(
            workspace_id,
            name,
            mode=mode,
            provider=provider,
            agent_tool=agent_tool,
            instruction_prompt=instruction_prompt,
            github_pat_id=github_pat_id,
            prompt_id=prompt_id,
            persist=persist,
            working_branch=working_branch,
        )
        return _fmt_session(session)

    async def _update_session(
        self,
        workspace_id: int,
        session_id: int,
        name: str | None = None,
        mode: str | None = None,
        provider: str | None = None,
        agent_tool: str | None = None,
        instruction_prompt: str | None = None,
        prompt_id: int | None = None,
        persist: bool | None = None,
        working_branch: str | None = None,
        github_pat_id: int | None = None,
    ) -> dict:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if mode is not None:
            fields["mode"] = mode
        if provider is not None:
            fields["provider"] = provider
        if agent_tool is not None:
            fields["agent_tool"] = agent_tool
        if instruction_prompt is not None:
            fields["instruction_prompt"] = instruction_prompt
        if prompt_id is not None:
            fields["prompt_id"] = prompt_id
        if persist is not None:
            fields["persist"] = persist
        if working_branch is not None:
            fields["working_branch"] = working_branch
        if github_pat_id is not None:
            fields["github_pat_id"] = github_pat_id
        session = await self.client.update_session(workspace_id, session_id, **fields)
        return _fmt_session(session)

    async def _delete_session(self, workspace_id: int, session_id: int) -> dict:
        return await self.client.delete_session(workspace_id, session_id)

    async def _add_repo_to_session(
        self,
        workspace_id: int,
        session_id: int,
        repo_url: str,
        branch: str = "main",
        local_path: str = "",
    ) -> dict:
        return await self.client.add_repo(workspace_id, session_id, repo_url, branch, local_path)

    async def _remove_repo_from_session(
        self,
        workspace_id: int,
        session_id: int,
        repo_id: int,
    ) -> dict:
        return await self.client.delete_repo(workspace_id, session_id, repo_id)

    async def _list_workspace_prompts(self, workspace_id: int) -> list[dict]:
        sources = await self.client.list_prompt_sources(workspace_id)
        prompts = []
        for source in sources:
            source_name = source.get("name", "")
            for p in source.get("prompts") or []:
                prompts.append({
                    "id": p.get("id"),
                    "display_name": p.get("display_name"),
                    "filename": p.get("filename"),
                    "source_name": source_name,
                    "source_id": source.get("id"),
                })
        return prompts

    async def _set_session_prompt(
        self,
        workspace_id: int,
        session_id: int,
        prompt_id: int | None = None,
        instruction_prompt: str | None = None,
    ) -> dict:
        fields: dict[str, Any] = {}
        if prompt_id is not None:
            fields["prompt_id"] = prompt_id
        if instruction_prompt is not None:
            fields["instruction_prompt"] = instruction_prompt
        session = await self.client.update_session(workspace_id, session_id, **fields)
        return _fmt_session(session)

    async def _launch_session(self, workspace_id: int, session_id: int) -> dict:
        session = await self.client.launch_session(workspace_id, session_id)
        return _fmt_session(session)

    async def _stop_session(self, workspace_id: int, session_id: int) -> dict:
        session = await self.client.stop_session(workspace_id, session_id)
        return _fmt_session(session)

    async def _get_session_status(self, workspace_id: int, session_id: int) -> dict:
        s = await self.client.get_session(workspace_id, session_id)
        return {
            "id": s.get("id"),
            "name": s.get("name"),
            "phase": s.get("phase"),
            "status_detail": s.get("status_detail"),
            "is_active": s.get("is_active"),
            "run_duration": s.get("run_duration"),
            "run_started_at": s.get("run_started_at"),
            "run_completed_at": s.get("run_completed_at"),
        }

    async def _get_session_output(self, workspace_id: int, session_id: int) -> dict:
        result = await self.client.get_session_output(workspace_id, session_id)
        if not result:
            return {"output": "", "raw_output": ""}
        return {
            "output": result.get("output", ""),
            "raw_output": result.get("raw_output", ""),
        }

    async def _wait_for_session(
        self,
        workspace_id: int,
        session_id: int,
        poll_interval: int = 10,
        timeout: int = 3600,
        ctx: Context | None = None,
    ) -> dict:
        poll = max(1, poll_interval)
        elapsed = 0
        while elapsed < timeout:
            s = await self.client.get_session(workspace_id, session_id)
            phase = s.get("phase", "unknown")
            duration = s.get("run_duration", "")

            if ctx:
                await ctx.info(
                    f"Session '{s.get('name')}' phase={phase} "
                    f"elapsed={duration or f'{elapsed}s'}"
                )

            if phase in _TERMINAL_PHASES:
                output_result = await self.client.get_session_output(workspace_id, session_id)
                output = output_result.get("output", "") if output_result else ""
                raw_output = output_result.get("raw_output", "") if output_result else ""
                return {
                    "phase": phase,
                    "status_detail": s.get("status_detail"),
                    "run_duration": s.get("run_duration"),
                    "output": output,
                    "raw_output": raw_output,
                }

            await asyncio.sleep(poll)
            elapsed += poll

        return {
            "phase": "timeout",
            "status_detail": f"Timed out after {timeout}s",
            "run_duration": f"{timeout}s",
            "output": "",
            "raw_output": "",
        }

    async def _list_github_pats(self, workspace_id: int) -> list[dict]:
        pats = await self.client.list_pats(workspace_id)
        return [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "github_username": p.get("github_username"),
                "github_org": p.get("github_org"),
                "description": p.get("description"),
                "shared": p.get("shared"),
            }
            for p in pats
        ]

    async def _list_session_schedules(self, workspace_id: int, session_id: int) -> list[dict]:
        schedules = await self.client.list_session_schedules(workspace_id, session_id)
        return [_fmt_schedule(sc) for sc in schedules]

    async def _add_session_schedule(
        self,
        workspace_id: int,
        session_id: int,
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
        sc = await self.client.create_session_schedule(
            workspace_id, session_id, cron_schedule,
            trigger_type=trigger_type, event_condition=event_condition,
            author_scope=author_scope, fix_authors=fix_authors,
            label=label, prompt_id=prompt_id,
            instruction_prompt=instruction_prompt,
            include_event_context=include_event_context, enabled=enabled,
        )
        return _fmt_schedule(sc)

    async def _update_session_schedule(
        self,
        workspace_id: int,
        session_id: int,
        schedule_id: int,
        **fields: Any,
    ) -> dict:
        sc = await self.client.update_session_schedule(workspace_id, session_id, schedule_id, **fields)
        return _fmt_schedule(sc)

    async def _delete_session_schedule(
        self, workspace_id: int, session_id: int, schedule_id: int
    ) -> None:
        await self.client.delete_session_schedule(workspace_id, session_id, schedule_id)

    # ==================================================================
    # Tool registration
    # ==================================================================

    def _register_tools(self) -> None:
        mcp = self.mcp

        @mcp.tool()
        async def list_workspaces() -> list[dict]:
            """List all accessible Agent Swarm workspaces.

            Returns workspace id, display_name, namespace, description, and owner_id.
            Use the workspace id in subsequent calls.
            """
            return await self._list_workspaces()

        @mcp.tool()
        async def get_workspace(workspace_id: int) -> dict:
            """Get details of a specific workspace by ID.

            Args:
                workspace_id: The workspace id.
            """
            return await self._get_workspace(workspace_id)

        @mcp.tool()
        async def create_workspace(display_name: str, description: str = "") -> dict:
            """Create a new workspace.

            Args:
                display_name: Workspace display name.
                description: Optional workspace description.
            """
            return await self._create_workspace(display_name, description)

        @mcp.tool()
        async def update_workspace(workspace_id: int, display_name: str, description: str | None = None) -> dict:
            """Update a workspace's display name or description.

            Args:
                workspace_id: The workspace id.
                display_name: New workspace display name.
                description: New workspace description.
            """
            return await self._update_workspace(workspace_id, display_name, description)

        @mcp.tool()
        async def delete_workspace(workspace_id: int) -> dict:
            """Delete a workspace.

            Args:
                workspace_id: The workspace id.
            """
            return await self._delete_workspace(workspace_id)

        @mcp.tool()
        async def list_workspace_members(workspace_id: int) -> list[dict]:
            """List all members granted access to a workspace.

            Args:
                workspace_id: The workspace id.
            """
            return await self._list_workspace_members(workspace_id)

        @mcp.tool()
        async def add_workspace_member(workspace_id: int, user_id: str, role: str = "member") -> dict:
            """Add a user as a member of a workspace.

            Args:
                workspace_id: The workspace id.
                user_id: Username or ServiceAccount identity (e.g. 'system:serviceaccount:<NAMESPACE>:<USER>').
                role: Member role (default 'member').
            """
            return await self._add_workspace_member(workspace_id, user_id, role)

        @mcp.tool()
        async def remove_workspace_member(workspace_id: int, user_id: str) -> dict:
            """Remove a member from a workspace.

            Args:
                workspace_id: The workspace id.
                user_id: Username or ServiceAccount identity to remove.
            """
            return await self._remove_workspace_member(workspace_id, user_id)

        @mcp.tool()
        async def get_me() -> dict:
            """Get current authenticated user identity and permissions.

            Returns username, is_admin, can_create_workspace, and admin_bootstrap_available.
            """
            return await self._get_me()

        @mcp.tool()
        async def list_known_users() -> list[str]:
            """List known users and ServiceAccounts for member/admin autocomplete."""
            return await self._list_known_users()

        @mcp.tool()
        async def list_admins() -> list[dict]:
            """List all global Swarmer admins."""
            return await self._list_admins()

        @mcp.tool()
        async def add_admin(user_id: str) -> dict:
            """Add a user as a global Swarmer admin.

            Args:
                user_id: Username to grant global admin rights.
            """
            return await self._add_admin(user_id)

        @mcp.tool()
        async def remove_admin(user_id: str) -> dict:
            """Remove a user from global Swarmer admins.

            Args:
                user_id: Username to revoke admin rights from.
            """
            return await self._remove_admin(user_id)

        @mcp.tool()
        async def bootstrap_admin() -> dict:
            """Self-promote the current user to global admin when zero admins exist."""
            return await self._bootstrap_admin()

        @mcp.tool()
        async def get_workspace_gateway(workspace_id: int) -> dict:
            """Get dedicated OpenShell gateway configuration for a workspace.

            Args:
                workspace_id: The workspace id.
            """
            return await self._get_workspace_gateway(workspace_id)

        @mcp.tool()
        async def set_workspace_gateway(
            workspace_id: int,
            gateway_url: str,
            auth_mode: str = "oidc",
            oidc_issuer: str | None = None,
            oidc_client_id: str | None = None,
            oidc_audience: str | None = None,
            refresh_token: str | None = None,
            bearer_token: str | None = None,
            tls_ca: str | None = None,
            tls_verify: bool = True,
        ) -> dict:
            """Configure a dedicated OpenShell gateway for a workspace.

            This operation replaces the full workspace gateway configuration. Any
            omitted optional fields may clear previously saved values (for
            example OIDC settings or TLS materials). Pass all values you intend
            to retain.

            Args:
                workspace_id: The workspace id.
                gateway_url: The gateway endpoint URL (e.g. https://gw-xyz.example.com:443).
                auth_mode: Authentication mode ('oidc', 'bearer', 'none').
                oidc_issuer: OIDC issuer URL (when auth_mode is 'oidc').
                oidc_client_id: OIDC client ID (when auth_mode is 'oidc').
                oidc_audience: Optional OIDC audience.
                refresh_token: Optional OIDC refresh token.
                bearer_token: Optional static bearer token.
                tls_ca: Optional CA cert content/path.
                tls_verify: Whether to verify TLS certificate (default True).
            """
            return await self._set_workspace_gateway(
                workspace_id=workspace_id,
                gateway_url=gateway_url,
                auth_mode=auth_mode,
                oidc_issuer=oidc_issuer,
                oidc_client_id=oidc_client_id,
                oidc_audience=oidc_audience,
                refresh_token=refresh_token,
                bearer_token=bearer_token,
                tls_ca=tls_ca,
                tls_verify=tls_verify,
            )

        @mcp.tool()
        async def delete_workspace_gateway(workspace_id: int) -> dict:
            """Revert a workspace to use the cluster default OpenShell gateway.

            Args:
                workspace_id: The workspace id.
            """
            return await self._delete_workspace_gateway(workspace_id)

        @mcp.tool()
        async def test_workspace_gateway(
            gateway_url: str,
            auth_mode: str = "oidc",
            oidc_issuer: str | None = None,
            oidc_client_id: str | None = None,
            oidc_audience: str | None = None,
            refresh_token: str | None = None,
            bearer_token: str | None = None,
            tls_ca: str | None = None,
            tls_verify: bool = True,
        ) -> dict:
            """Test connection and authentication to an OpenShell gateway.

            Args:
                gateway_url: The gateway endpoint URL.
                auth_mode: Authentication mode ('oidc', 'bearer', 'none').
                oidc_issuer: Optional OIDC issuer URL.
                oidc_client_id: Optional OIDC client ID.
                oidc_audience: Optional OIDC audience.
                refresh_token: Optional OIDC refresh token.
                bearer_token: Optional bearer token.
                tls_ca: Optional CA cert.
                tls_verify: Whether to verify TLS.
            """
            return await self._test_workspace_gateway(
                gateway_url=gateway_url,
                auth_mode=auth_mode,
                oidc_issuer=oidc_issuer,
                oidc_client_id=oidc_client_id,
                oidc_audience=oidc_audience,
                refresh_token=refresh_token,
                bearer_token=bearer_token,
                tls_ca=tls_ca,
                tls_verify=tls_verify,
            )

        @mcp.tool()
        async def parse_gateway_command(command: str) -> dict:
            """Parse a pasted OpenShell CLI command or JSON metadata blob into
            structured gateway fields for use with set_workspace_gateway /
            test_workspace_gateway.

            Args:
                command: Raw text — an 'openshell gateway add ...' command line,
                    or a JSON metadata snippet describing the gateway.
            """
            return await self._parse_gateway_command(command)

        @mcp.tool()
        async def parse_gateway_token(token_input: str) -> dict:
            """Parse a pasted OIDC token/credential payload (raw token string,
            an oidc_token.json bundle, or a REFRESH_TOKEN=... line) into
            structured fields for use with set_workspace_gateway.

            Args:
                token_input: Raw pasted token text.
            """
            return await self._parse_gateway_token(token_input)

        @mcp.tool()
        async def list_sessions(
            workspace_id: int,
            phase: str | None = None,
        ) -> list[dict]:
            """List sessions in a workspace.

            Args:
                workspace_id: The workspace id (from list_workspaces).
                phase: Optional filter. One of: idle, pending, running,
                       succeeded, failed, stopped.
            """
            return await self._list_sessions(workspace_id, phase)

        @mcp.tool()
        async def get_session(workspace_id: int, session_id: int) -> dict:
            """Get full session details including attached git repositories.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._get_session(workspace_id, session_id)

        @mcp.tool()
        async def find_sessions_by_repo(
            workspace_id: int,
            repo_url: str,
        ) -> list[dict]:
            """Find sessions that have a specific git repository attached.

            Use this before creating a new session to check if one already exists
            for the target repository. URL matching is normalized (strips .git suffix,
            trailing slashes, case-insensitive host).

            Args:
                workspace_id: The workspace id to search within.
                repo_url: GitHub repository URL (e.g. https://github.com/org/repo).
            """
            return await self._find_sessions_by_repo(workspace_id, repo_url)

        @mcp.tool()
        async def create_session(
            workspace_id: int,
            name: str,
            agent_tool: str = "opencode",
            mode: str = "prompt",
            provider: str = "",
            persist: bool = False,
            working_branch: str = "",
            instruction_prompt: str = "",
            github_pat_id: int | None = None,
            prompt_id: int | None = None,
        ) -> dict:
            """Create a new agent session.

            Args:
                workspace_id: The workspace id.
                name: Unique session name within the workspace.
                agent_tool: Agent tool. One of: opencode, shell. Default: opencode.
                mode: Execution mode. One of: prompt, tui, server. Default: prompt.
                provider: AI provider preset. One of: claude, gemini, openai. Empty string
                          uses the tool default (based on configured credentials).
                persist: Keep workspace volume between runs. Default: false.
                working_branch: Git branch to create/checkout in the sandbox.
                instruction_prompt: Additional instructions prepended to the base prompt (or raw command for shell).
                github_pat_id: GitHub PAT id for private repos (from list_github_pats).
                prompt_id: Base prompt id (from list_workspace_prompts).
            """
            return await self._create_session(
                workspace_id, name, agent_tool, mode, provider,
                persist, working_branch, instruction_prompt, github_pat_id, prompt_id,
            )

        @mcp.tool()
        async def update_session(
            workspace_id: int,
            session_id: int,
            name: str | None = None,
            mode: str | None = None,
            provider: str | None = None,
            agent_tool: str | None = None,
            instruction_prompt: str | None = None,
            prompt_id: int | None = None,
            persist: bool | None = None,
            working_branch: str | None = None,
            github_pat_id: int | None = None,
        ) -> dict:
            """Update a non-running session's configuration (only changed fields needed).

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                name: New session name.
                mode: New mode (prompt/tui/server).
                provider: New AI provider preset (claude/gemini/openai).
                agent_tool: New agent tool (opencode/shell).
                instruction_prompt: New additional instructions (or raw command for shell).
                prompt_id: New base prompt id.
                persist: New persistence setting.
                working_branch: New working branch.
                github_pat_id: New GitHub PAT id.
            """
            return await self._update_session(
                workspace_id, session_id, name, mode, provider, agent_tool,
                instruction_prompt, prompt_id, persist, working_branch, github_pat_id,
            )

        @mcp.tool()
        async def delete_session(workspace_id: int, session_id: int) -> dict:
            """Delete a session (must not be running).

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._delete_session(workspace_id, session_id)

        @mcp.tool()
        async def add_repo_to_session(
            workspace_id: int,
            session_id: int,
            repo_url: str,
            branch: str = "main",
            local_path: str = "",
        ) -> dict:
            """Attach a git repository to a session.

            The repo will be cloned into /workspace/<local_path> when the sandbox starts.
            local_path is derived from the repo name if omitted.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                repo_url: GitHub HTTPS repository URL.
                branch: Branch to clone. Default: main.
                local_path: Path under /workspace/ for the clone.
            """
            return await self._add_repo_to_session(workspace_id, session_id, repo_url, branch, local_path)

        @mcp.tool()
        async def remove_repo_from_session(
            workspace_id: int,
            session_id: int,
            repo_id: int,
        ) -> dict:
            """Remove a git repository from a session.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                repo_id: The repo id (from get_session repos list).
            """
            return await self._remove_repo_from_session(workspace_id, session_id, repo_id)

        @mcp.tool()
        async def list_workspace_prompts(workspace_id: int) -> list[dict]:
            """List all available prompts in a workspace's prompt library.

            Prompts are synced from git repositories configured as prompt sources.
            Use prompt id with set_session_prompt or create_session.

            Args:
                workspace_id: The workspace id.
            """
            return await self._list_workspace_prompts(workspace_id)

        @mcp.tool()
        async def set_session_prompt(
            workspace_id: int,
            session_id: int,
            prompt_id: int | None = None,
            instruction_prompt: str | None = None,
        ) -> dict:
            """Set the prompt configuration for a session.

            instruction_prompt (additional instructions) is prepended to the
            git-referenced base prompt (prompt_id) at launch time.
            Either or both can be set independently.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                prompt_id: Base prompt id from list_workspace_prompts.
                instruction_prompt: Additional instructions prepended to base prompt.
            """
            return await self._set_session_prompt(workspace_id, session_id, prompt_id, instruction_prompt)

        @mcp.tool()
        async def launch_session(workspace_id: int, session_id: int) -> dict:
            """Launch a session sandbox.

            Starts the agent tool in the configured mode. For prompt mode, the session
            runs once and exits — use wait_for_session to block until completion.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._launch_session(workspace_id, session_id)

        @mcp.tool()
        async def stop_session(workspace_id: int, session_id: int) -> dict:
            """Stop a running session.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._stop_session(workspace_id, session_id)

        @mcp.tool()
        async def get_session_status(workspace_id: int, session_id: int) -> dict:
            """Get the current status of a session.

            Returns phase, status_detail, run_duration, run_started_at, run_completed_at.
            Phases: idle, pending, running, succeeded, failed, stopped.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._get_session_status(workspace_id, session_id)

        @mcp.tool()
        async def get_session_output(workspace_id: int, session_id: int) -> dict:
            """Retrieve the captured output from the last session run.

            For prompt-mode sessions returns both the processed agent output
            (output) and the raw console log (raw_output). For OpenCode sessions
            these differ: output contains the clean assistant conversation from
            OpenCode's SQLite DB; raw_output contains the raw stdout/stderr stream.
            For TUI/server-mode sessions they are identical.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._get_session_output(workspace_id, session_id)

        @mcp.tool()
        async def wait_for_session(
            workspace_id: int,
            session_id: int,
            poll_interval: int = 10,
            timeout: int = 3600,
            ctx: Context | None = None,
        ) -> dict:
            """Poll a session until it reaches a terminal state, then return output.

            Blocks until phase is succeeded, failed, or stopped, or until timeout.
            Reports progress at each poll interval.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                poll_interval: Seconds between status checks. Default: 10.
                timeout: Maximum seconds to wait. Default: 3600 (1 hour).
            """
            return await self._wait_for_session(workspace_id, session_id, poll_interval, timeout, ctx)

        @mcp.tool()
        async def list_github_pats(workspace_id: int) -> list[dict]:
            """List GitHub Personal Access Tokens for a workspace.

            Use a PAT id when creating sessions that need private repo access.

            Args:
                workspace_id: The workspace id.
            """
            return await self._list_github_pats(workspace_id)

        @mcp.tool()
        async def list_session_schedules(workspace_id: int, session_id: int) -> list[dict]:
            """List all schedules configured for a session.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
            """
            return await self._list_session_schedules(workspace_id, session_id)

        @mcp.tool()
        async def add_session_schedule(
            workspace_id: int,
            session_id: int,
            prompt_id: int,
            cron_schedule: str = "",
            trigger_type: str = "cron",
            event_condition: str = "",
            author_scope: str = "all",
            fix_authors: str = "",
            label: str = "",
            instruction_prompt: str = "",
            include_event_context: bool = True,
            enabled: bool = True,
        ) -> dict:
            """Add a new schedule or event trigger to a session.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                cron_schedule: Cron expression (e.g. '0 9 * * 1-5'). Required for cron triggers.
                trigger_type: 'cron' for scheduled runs, 'event' for GitHub event triggers.
                event_condition: Event trigger condition (e.g. 'ci_fail_or_conflict', 'new_pr_or_commit', 'review_comments', 'any_actionable').
                author_scope: PR author scope (e.g. 'self', 'team', 'bots', 'all').
                fix_authors: Comma-separated GitHub logins for 'self' author scope.
                label: Human-readable name for this trigger.
                prompt_id: Required ID of the workspace prompt to run.
                instruction_prompt: Additional instructions; overrides session default when set.
                include_event_context: Include triggering event data in the agent prompt.
                enabled: Whether the schedule is active. Default: True.
            """
            return await self._add_session_schedule(
                workspace_id, session_id, cron_schedule,
                trigger_type=trigger_type, event_condition=event_condition,
                author_scope=author_scope, fix_authors=fix_authors,
                label=label, prompt_id=prompt_id,
                instruction_prompt=instruction_prompt,
                include_event_context=include_event_context, enabled=enabled,
            )

        @mcp.tool()
        async def update_session_schedule(
            workspace_id: int,
            session_id: int,
            schedule_id: int,
            cron_schedule: str | None = None,
            trigger_type: str | None = None,
            event_condition: str | None = None,
            author_scope: str | None = None,
            fix_authors: str | None = None,
            label: str | None = None,
            prompt_id: int | None = None,
            instruction_prompt: str | None = None,
            include_event_context: bool | None = None,
            enabled: bool | None = None,
        ) -> dict:
            """Update an existing session schedule or event trigger.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                schedule_id: The schedule id to update.
                cron_schedule: New cron expression.
                trigger_type: 'cron' or 'event'.
                event_condition: New event condition ('ci_fail_or_conflict', 'new_pr_or_commit', 'review_comments', 'any_actionable').
                author_scope: New author scope ('self', 'team', 'bots', 'all').
                fix_authors: Comma-separated GitHub logins for 'self' author scope.
                label: New label.
                prompt_id: New prompt id. Existing schedules retain their prompt when omitted.
                instruction_prompt: New additional instructions.
                include_event_context: Include triggering event data in the agent prompt.
                enabled: Enable or disable the schedule.
            """
            fields: dict[str, Any] = {}
            if cron_schedule is not None:
                fields["cron_schedule"] = cron_schedule
            if trigger_type is not None:
                fields["trigger_type"] = trigger_type
            if event_condition is not None:
                fields["event_condition"] = event_condition
            if author_scope is not None:
                fields["author_scope"] = author_scope
            if fix_authors is not None:
                fields["fix_authors"] = fix_authors
            if label is not None:
                fields["label"] = label
            if prompt_id is not None:
                fields["prompt_id"] = prompt_id
            if instruction_prompt is not None:
                fields["instruction_prompt"] = instruction_prompt
            if include_event_context is not None:
                fields["include_event_context"] = include_event_context
            if enabled is not None:
                fields["enabled"] = enabled
            return await self._update_session_schedule(workspace_id, session_id, schedule_id, **fields)

        @mcp.tool()
        async def delete_session_schedule(
            workspace_id: int,
            session_id: int,
            schedule_id: int,
        ) -> dict:
            """Delete a session schedule.

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                schedule_id: The schedule id to delete.
            """
            await self._delete_session_schedule(workspace_id, session_id, schedule_id)
            return {"detail": "deleted"}

    def run(self, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8080) -> None:
        if transport == "sse":
            self.mcp.run(transport="sse", host=host, port=port)
        else:
            self.mcp.run()

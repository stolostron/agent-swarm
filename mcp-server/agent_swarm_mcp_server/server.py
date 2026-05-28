"""FastMCP server exposing Agent Swarm operations as MCP tools."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

from fastmcp import Context, FastMCP

from .client import AgentSwarmClient, AgentSwarmAPIError
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


def _fmt_session(s: dict, repos: list[dict] | None = None) -> dict:
    result = {
        "id": s.get("id"),
        "name": s.get("name"),
        "phase": s.get("phase"),
        "mode": s.get("mode"),
        "model": s.get("model"),
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
            }
            for ws in workspaces
        ]

    async def _list_sessions(
        self,
        workspace_id: int,
        phase: Optional[str] = None,
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
        model: str = "",
        persist: bool = False,
        working_branch: str = "",
        instruction_prompt: str = "",
        github_pat_id: Optional[int] = None,
        prompt_id: Optional[int] = None,
    ) -> dict:
        session = await self.client.create_session(
            workspace_id,
            name,
            mode=mode,
            model=model,
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
        name: Optional[str] = None,
        mode: Optional[str] = None,
        model: Optional[str] = None,
        instruction_prompt: Optional[str] = None,
        prompt_id: Optional[int] = None,
        persist: Optional[bool] = None,
        working_branch: Optional[str] = None,
        github_pat_id: Optional[int] = None,
    ) -> dict:
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
        if mode is not None:
            fields["mode"] = mode
        if model is not None:
            fields["model"] = model
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
        prompt_id: Optional[int] = None,
        instruction_prompt: Optional[str] = None,
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

    async def _get_session_output(self, workspace_id: int, session_id: int) -> str:
        result = await self.client.get_session_output(workspace_id, session_id)
        return result.get("output", "") if result else ""

    async def _wait_for_session(
        self,
        workspace_id: int,
        session_id: int,
        poll_interval: int = 10,
        timeout: int = 3600,
        ctx: Optional[Context] = None,
    ) -> dict:
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
                return {
                    "phase": phase,
                    "status_detail": s.get("status_detail"),
                    "run_duration": s.get("run_duration"),
                    "output": output,
                }

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return {
            "phase": "timeout",
            "status_detail": f"Timed out after {timeout}s",
            "run_duration": f"{timeout}s",
            "output": "",
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

    # ==================================================================
    # Tool registration
    # ==================================================================

    def _register_tools(self) -> None:
        mcp = self.mcp

        @mcp.tool()
        async def list_workspaces() -> list[dict]:
            """List all accessible Agent Swarm workspaces.

            Returns workspace id, display_name, namespace, and description.
            Use the workspace id in subsequent calls.
            """
            return await self._list_workspaces()

        @mcp.tool()
        async def list_sessions(
            workspace_id: int,
            phase: Optional[str] = None,
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
            model: str = "",
            persist: bool = False,
            working_branch: str = "",
            instruction_prompt: str = "",
            github_pat_id: Optional[int] = None,
            prompt_id: Optional[int] = None,
        ) -> dict:
            """Create a new agent session.

            Args:
                workspace_id: The workspace id.
                name: Unique session name within the workspace.
                agent_tool: Agent tool. One of: opencode, crush. Default: opencode.
                mode: Execution mode. One of: prompt, tui, server. Default: prompt.
                model: LLM model identifier. Empty string uses the tool default.
                       OpenCode: google-vertex-anthropic/claude-sonnet-4-6@default
                       Crush: vertexai/claude-sonnet-4-6
                persist: Keep workspace PVC between runs. Default: false.
                working_branch: Git branch to create/checkout in the pod.
                instruction_prompt: Additional instructions prepended to the base prompt.
                github_pat_id: GitHub PAT id for private repos (from list_github_pats).
                prompt_id: Base prompt id (from list_workspace_prompts).
            """
            return await self._create_session(
                workspace_id, name, agent_tool, mode, model,
                persist, working_branch, instruction_prompt, github_pat_id, prompt_id,
            )

        @mcp.tool()
        async def update_session(
            workspace_id: int,
            session_id: int,
            name: Optional[str] = None,
            mode: Optional[str] = None,
            model: Optional[str] = None,
            instruction_prompt: Optional[str] = None,
            prompt_id: Optional[int] = None,
            persist: Optional[bool] = None,
            working_branch: Optional[str] = None,
            github_pat_id: Optional[int] = None,
        ) -> dict:
            """Update a non-running session's configuration (only changed fields needed).

            Args:
                workspace_id: The workspace id.
                session_id: The session id.
                name: New session name.
                mode: New mode (prompt/tui/server).
                model: New model identifier.
                instruction_prompt: New additional instructions.
                prompt_id: New base prompt id.
                persist: New persistence setting.
                working_branch: New working branch.
                github_pat_id: New GitHub PAT id.
            """
            return await self._update_session(
                workspace_id, session_id, name, mode, model,
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

            The repo will be cloned into /workspace/<local_path> when the pod starts.
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
            prompt_id: Optional[int] = None,
            instruction_prompt: Optional[str] = None,
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
            """Launch a session pod.

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
        async def get_session_output(workspace_id: int, session_id: int) -> str:
            """Retrieve the captured output from the last session run.

            For prompt-mode sessions this is the full agent output.
            For TUI/server-mode sessions this is recent pod logs.

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
            ctx: Context = None,
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

    def run(self, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8080) -> None:
        if transport == "sse":
            self.mcp.run(transport="sse", host=host, port=port)
        else:
            self.mcp.run()

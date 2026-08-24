"""Tests for server tool logic: instantiation, URL normalization, find_sessions_by_repo, wait_for_session."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_swarm_mcp_server.server import _normalize_repo_url, AgentSwarmMCPServer
from agent_swarm_mcp_server.config import AgentSwarmConfig
from agent_swarm_mcp_server.client import AgentSwarmClient


# ------------------------------------------------------------------
# Server instantiation and tool registration
# ------------------------------------------------------------------

EXPECTED_TOOLS = {
    "list_workspaces",
    "get_workspace",
    "create_workspace",
    "update_workspace",
    "delete_workspace",
    "list_sessions",
    "get_session",
    "find_sessions_by_repo",
    "create_session",
    "update_session",
    "delete_session",
    "add_repo_to_session",
    "remove_repo_from_session",
    "list_workspace_prompts",
    "set_session_prompt",
    "launch_session",
    "stop_session",
    "get_session_status",
    "get_session_output",
    "wait_for_session",
    "list_github_pats",
    # ACM-35377: schedule management tools
    "list_session_schedules",
    "add_session_schedule",
    "update_session_schedule",
    "delete_session_schedule",
    # ACM-41659 / ACM-42585: workspace members, admins, me
    "list_workspace_members",
    "add_workspace_member",
    "remove_workspace_member",
    "get_me",
    "list_known_users",
    "list_admins",
    "add_admin",
    "remove_admin",
    "bootstrap_admin",
}


def test_server_can_be_imported():
    """Verify AgentSwarmMCPServer and helpers can be imported without errors."""
    from agent_swarm_mcp_server.server import AgentSwarmMCPServer, _normalize_repo_url  # noqa: F401
    from agent_swarm_mcp_server.config import AgentSwarmConfig  # noqa: F401
    from agent_swarm_mcp_server.client import AgentSwarmClient  # noqa: F401
    from agent_swarm_mcp_server.auth import resolve_token  # noqa: F401


def test_server_instantiates_with_config():
    """Verify AgentSwarmMCPServer constructs without raising."""
    server = make_server()
    assert server is not None
    assert server.config.api_url == "https://swarmer.example.com"


def test_server_registers_all_expected_tools():
    """Verify all 34 MCP tools are registered on the FastMCP instance.

    This test catches regressions where a tool is removed, renamed, or
    fails to register due to an import/decorator error.
    """
    registered_tools: set[str] = set()

    config = AgentSwarmConfig(
        api_url="https://swarmer.example.com",
        token="test-token",
    )

    with patch("agent_swarm_mcp_server.server.FastMCP") as mock_mcp_cls:
        mock_mcp = MagicMock()
        registered_names: list[str] = []

        def capture_tool():
            """Capture the name of each @mcp.tool() decorated function."""
            def decorator(fn):
                registered_names.append(fn.__name__)
                return fn
            return decorator

        mock_mcp.tool.side_effect = capture_tool
        mock_mcp_cls.return_value = mock_mcp

        with patch("agent_swarm_mcp_server.server.AgentSwarmClient"):
            AgentSwarmMCPServer(config=config)

        registered_tools = set(registered_names)

    assert registered_tools == EXPECTED_TOOLS, (
        f"Tool mismatch.\n"
        f"  Missing: {EXPECTED_TOOLS - registered_tools}\n"
        f"  Extra:   {registered_tools - EXPECTED_TOOLS}"
    )


# ------------------------------------------------------------------
# URL normalization
# ------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://github.com/org/repo", "github.com/org/repo"),
    ("https://github.com/org/repo.git", "github.com/org/repo"),
    ("https://github.com/org/repo/", "github.com/org/repo"),
    ("https://github.com/org/repo.git/", "github.com/org/repo"),
    ("https://GITHUB.COM/Org/Repo", "github.com/org/repo"),
    ("https://github.com/stolostron/agent-swarm", "github.com/stolostron/agent-swarm"),
])
def test_normalize_repo_url(url, expected):
    assert _normalize_repo_url(url) == expected


def test_normalize_matches_with_and_without_git_suffix():
    a = _normalize_repo_url("https://github.com/org/repo.git")
    b = _normalize_repo_url("https://github.com/org/repo")
    assert a == b


# ------------------------------------------------------------------
# Server instance fixture (bypasses MCP registration for tool logic tests)
# ------------------------------------------------------------------

def make_server() -> AgentSwarmMCPServer:
    """Create an AgentSwarmMCPServer with mocked client and FastMCP (no real server)."""
    config = AgentSwarmConfig(
        api_url="https://swarmer.example.com",
        token="test-token",
    )
    # Patch FastMCP to a no-op mock so _register_tools doesn't fail
    with patch("agent_swarm_mcp_server.server.FastMCP") as mock_mcp_cls:
        mock_mcp = MagicMock()
        mock_mcp.tool.return_value = lambda f: f  # passthrough decorator
        mock_mcp_cls.return_value = mock_mcp
        # Also patch AgentSwarmClient so it doesn't make real connections
        with patch("agent_swarm_mcp_server.server.AgentSwarmClient"):
            server = AgentSwarmMCPServer(config=config)

    # Replace client with a proper async mock
    server.client = MagicMock(spec=AgentSwarmClient)
    for name in dir(AgentSwarmClient):
        if not name.startswith("_") and callable(getattr(AgentSwarmClient, name, None)):
            setattr(server.client, name, AsyncMock())
    return server


# ------------------------------------------------------------------
# create_session & update_session with agent_tool
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_session_with_shell_tool():
    server = make_server()
    server.client.create_session = AsyncMock(return_value={
        "id": 10, "name": "cron-report", "phase": "idle", "mode": "prompt",
        "provider": "", "agent_tool": "shell", "persist": False, "working_branch": "",
        "prompt_id": None, "instruction_prompt": "python3 report.py",
        "status_detail": "", "run_duration": None, "run_started_at": None,
        "run_completed_at": None, "is_active": False, "workspace_id": 1,
    })
    result = await server._create_session(
        1, "cron-report", agent_tool="shell", instruction_prompt="python3 report.py"
    )
    server.client.create_session.assert_awaited_once_with(
        1, "cron-report", mode="prompt", provider="", agent_tool="shell",
        instruction_prompt="python3 report.py", github_pat_id=None, prompt_id=None,
        persist=False, working_branch=""
    )
    assert result["agent_tool"] == "shell"
    assert result["name"] == "cron-report"


@pytest.mark.asyncio
async def test_update_session_agent_tool():
    server = make_server()
    server.client.update_session = AsyncMock(return_value={
        "id": 10, "name": "cron-report", "phase": "idle", "mode": "prompt",
        "provider": "", "agent_tool": "shell", "persist": False, "working_branch": "",
        "prompt_id": None, "instruction_prompt": "echo hello",
        "status_detail": "", "run_duration": None, "run_started_at": None,
        "run_completed_at": None, "is_active": False, "workspace_id": 1,
    })
    result = await server._update_session(1, 10, agent_tool="shell", instruction_prompt="echo hello")
    server.client.update_session.assert_awaited_once_with(1, 10, agent_tool="shell", instruction_prompt="echo hello")
    assert result["agent_tool"] == "shell"


# ------------------------------------------------------------------
# list_workspace_prompts flattening
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_workspace_prompts_flattens_sources():
    server = make_server()
    server.client.list_prompt_sources = AsyncMock(return_value=[
        {
            "id": 1,
            "name": "CVE Prompts",
            "prompts": [
                {"id": 10, "display_name": "CVE Triage", "filename": "cve-triage.md"},
                {"id": 11, "display_name": "CVE Fix", "filename": "cve-fix.md"},
            ],
        },
        {
            "id": 2,
            "name": "Start Work Prompts",
            "prompts": [
                {"id": 20, "display_name": "Start Work", "filename": "start-work.md"},
            ],
        },
    ])

    result = await server._list_workspace_prompts(1)
    assert len(result) == 3
    assert result[0]["source_name"] == "CVE Prompts"
    assert result[0]["id"] == 10
    assert result[2]["source_name"] == "Start Work Prompts"
    assert result[2]["id"] == 20


# ------------------------------------------------------------------
# find_sessions_by_repo
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_sessions_by_repo_matches_normalized():
    server = make_server()
    sessions = [
        {
            "id": 1, "name": "agent-swarm-session", "phase": "idle",
            "mode": "prompt", "provider": "", "agent_tool": "opencode",
            "persist": False, "working_branch": "", "prompt_id": None,
            "instruction_prompt": "", "status_detail": "", "run_duration": None,
            "run_started_at": None, "run_completed_at": None,
            "is_active": False, "workspace_id": 1,
        },
        {
            "id": 2, "name": "other-session", "phase": "idle",
            "mode": "prompt", "provider": "", "agent_tool": "opencode",
            "persist": False, "working_branch": "", "prompt_id": None,
            "instruction_prompt": "", "status_detail": "", "run_duration": None,
            "run_started_at": None, "run_completed_at": None,
            "is_active": False, "workspace_id": 1,
        },
    ]
    server.client.list_sessions = AsyncMock(return_value=sessions)

    repos_by_sid = {
        1: [{"id": 1, "repo_url": "https://github.com/stolostron/agent-swarm.git",
             "branch": "main", "local_path": "agent-swarm"}],
        2: [{"id": 2, "repo_url": "https://github.com/stolostron/unrelated-repo",
             "branch": "main", "local_path": "unrelated-repo"}],
    }

    async def mock_list_repos(ws_id, sid):
        return repos_by_sid.get(sid, [])

    server.client.list_repos = mock_list_repos

    result = await server._find_sessions_by_repo(
        1, "https://github.com/stolostron/agent-swarm"
    )
    assert len(result) == 1
    assert result[0]["name"] == "agent-swarm-session"
    assert len(result[0]["repos"]) == 1


@pytest.mark.asyncio
async def test_find_sessions_by_repo_no_match():
    server = make_server()
    server.client.list_sessions = AsyncMock(return_value=[
        {
            "id": 1, "name": "s", "phase": "idle", "mode": "prompt", "provider": "",
            "agent_tool": "opencode", "persist": False, "working_branch": "",
            "prompt_id": None, "instruction_prompt": "", "status_detail": "",
            "run_duration": None, "run_started_at": None, "run_completed_at": None,
            "is_active": False, "workspace_id": 1,
        },
    ])
    server.client.list_repos = AsyncMock(return_value=[
        {"id": 1, "repo_url": "https://github.com/org/other", "branch": "main", "local_path": "other"}
    ])
    result = await server._find_sessions_by_repo(1, "https://github.com/org/target")
    assert result == []


@pytest.mark.asyncio
async def test_find_sessions_by_repo_empty_workspace():
    server = make_server()
    server.client.list_sessions = AsyncMock(return_value=[])
    result = await server._find_sessions_by_repo(1, "https://github.com/org/repo")
    assert result == []


# ------------------------------------------------------------------
# wait_for_session polling
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_for_session_returns_on_terminal_phase():
    server = make_server()
    call_count = 0

    async def mock_get_session(ws_id, sid):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return {
                "id": sid, "name": "s", "phase": "running", "status_detail": "",
                "run_duration": f"{call_count * 10}s", "run_started_at": None,
                "run_completed_at": None, "is_active": True,
            }
        return {
            "id": sid, "name": "s", "phase": "succeeded", "status_detail": "Completed",
            "run_duration": "30s", "run_started_at": None,
            "run_completed_at": None, "is_active": False,
        }

    server.client.get_session = mock_get_session
    server.client.get_session_output = AsyncMock(return_value={"output": "task done"})

    result = await server._wait_for_session(1, 10, poll_interval=0, timeout=60)
    assert result["phase"] == "succeeded"
    assert result["output"] == "task done"
    assert call_count == 3


@pytest.mark.asyncio
async def test_wait_for_session_timeout():
    server = make_server()
    server.client.get_session = AsyncMock(return_value={
        "id": 10, "name": "s", "phase": "running", "status_detail": "",
        "run_duration": "5s", "run_started_at": None,
        "run_completed_at": None, "is_active": True,
    })

    result = await server._wait_for_session(1, 10, poll_interval=1, timeout=2)
    assert result["phase"] == "timeout"


@pytest.mark.asyncio
async def test_wait_for_session_already_terminal():
    server = make_server()
    server.client.get_session = AsyncMock(return_value={
        "id": 10, "name": "s", "phase": "succeeded", "status_detail": "done",
        "run_duration": "5s", "run_started_at": None,
        "run_completed_at": None, "is_active": False,
    })
    server.client.get_session_output = AsyncMock(return_value={"output": "already done"})

    result = await server._wait_for_session(1, 10, poll_interval=0, timeout=60)
    assert result["phase"] == "succeeded"
    assert result["output"] == "already done"


# ------------------------------------------------------------------
# Workspace CRUD, Members, and Admin Server Methods
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_workspace_crud_server_methods():
    server = make_server()
    server.client.get_workspace = AsyncMock(return_value={"id": 1, "display_name": "ws1"})
    server.client.create_workspace = AsyncMock(return_value={"id": 2, "display_name": "ws2", "description": "d"})
    server.client.update_workspace = AsyncMock(return_value={"id": 2, "display_name": "ws2-renamed", "description": "d2"})
    server.client.delete_workspace = AsyncMock(return_value={"detail": "deleted"})

    assert (await server._get_workspace(1))["display_name"] == "ws1"
    assert (await server._create_workspace("ws2", "d"))["id"] == 2
    assert (await server._update_workspace(2, "ws2-renamed", "d2"))["display_name"] == "ws2-renamed"
    assert (await server._delete_workspace(2))["detail"] == "deleted"


@pytest.mark.asyncio
async def test_workspace_members_server_methods():
    server = make_server()
    server.client.list_workspace_members = AsyncMock(return_value=[
        {"id": 10, "workspace_id": 1, "user_id": "alice", "role": "member"}
    ])
    server.client.add_workspace_member = AsyncMock(return_value={
        "id": 11, "workspace_id": 1, "user_id": "bob", "role": "admin"
    })
    server.client.remove_workspace_member = AsyncMock(return_value={"detail": "removed"})

    members = await server._list_workspace_members(1)
    assert len(members) == 1
    assert members[0]["user_id"] == "alice"

    added = await server._add_workspace_member(1, "bob", role="admin")
    assert added["user_id"] == "bob"

    removed = await server._remove_workspace_member(1, "bob")
    assert removed == {"detail": "removed"}


@pytest.mark.asyncio
async def test_admins_and_me_server_methods():
    server = make_server()
    server.client.get_me = AsyncMock(return_value={"username": "alice", "is_admin": True})
    server.client.list_known_users = AsyncMock(return_value=["alice", "bob"])
    server.client.list_admins = AsyncMock(return_value=[{"id": 1, "user_id": "alice", "created_by": "bootstrap"}])
    server.client.add_admin = AsyncMock(return_value={"id": 2, "user_id": "bob", "created_by": "alice"})
    server.client.remove_admin = AsyncMock(return_value={"detail": "removed"})
    server.client.bootstrap_admin = AsyncMock(return_value={"id": 1, "user_id": "alice", "created_by": "bootstrap"})

    assert (await server._get_me())["username"] == "alice"
    assert (await server._list_known_users()) == ["alice", "bob"]
    assert len(await server._list_admins()) == 1
    assert (await server._add_admin("bob"))["user_id"] == "bob"
    assert (await server._remove_admin("bob"))["detail"] == "removed"
    assert (await server._bootstrap_admin())["created_by"] == "bootstrap"


# ------------------------------------------------------------------
# AgentSwarmConfig.from_env — SSL options
# ------------------------------------------------------------------

def test_config_ssl_ca_bundle_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_SWARM_API_URL", "https://swarmer.example.com")
    monkeypatch.setenv("AGENT_SWARM_SSL_CA_BUNDLE", "/etc/ssl/custom-ca.crt")
    monkeypatch.setenv("AGENT_SWARM_VERIFY_SSL", "true")
    with patch("agent_swarm_mcp_server.config.resolve_token", return_value="tok"):
        cfg = AgentSwarmConfig.from_env()
    assert cfg.ssl_ca_bundle == "/etc/ssl/custom-ca.crt"
    assert cfg.verify_ssl is True


def test_config_ssl_ca_bundle_unset(monkeypatch):
    monkeypatch.setenv("AGENT_SWARM_API_URL", "https://swarmer.example.com")
    monkeypatch.delenv("AGENT_SWARM_SSL_CA_BUNDLE", raising=False)
    with patch("agent_swarm_mcp_server.config.resolve_token", return_value="tok"):
        cfg = AgentSwarmConfig.from_env()
    assert cfg.ssl_ca_bundle is None

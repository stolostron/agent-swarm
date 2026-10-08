"""Tests for the AgentSwarmClient using respx HTTP mocking."""

from __future__ import annotations

import json

import pytest
import respx
import httpx

from agent_swarm_mcp_server.client import AgentSwarmClient, AgentSwarmAPIError

BASE_URL = "https://swarmer.example.com"


@pytest.fixture
def client():
    return AgentSwarmClient(BASE_URL, "test-token", verify_ssl=False)


def test_ssl_ca_bundle_takes_precedence_over_verify_ssl(monkeypatch):
    """ssl_ca_bundle path is passed as httpx verify, overriding the boolean flag."""
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("agent_swarm_mcp_server.client.httpx.AsyncClient", FakeAsyncClient)
    AgentSwarmClient(BASE_URL, "tok", verify_ssl=False, ssl_ca_bundle="/etc/ssl/custom-ca.crt")
    assert captured["verify"] == "/etc/ssl/custom-ca.crt"


def test_ssl_ca_bundle_none_falls_back_to_verify_ssl(monkeypatch):
    """When ssl_ca_bundle is None, the boolean verify_ssl is used."""
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("agent_swarm_mcp_server.client.httpx.AsyncClient", FakeAsyncClient)
    AgentSwarmClient(BASE_URL, "tok", verify_ssl=False, ssl_ca_bundle=None)
    assert captured["verify"] is False


@pytest.mark.asyncio
async def test_list_workspaces(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "display_name": "ws1"}])
        )
        result = await client.list_workspaces()
    assert result == [{"id": 1, "display_name": "ws1"}]


@pytest.mark.asyncio
async def test_create_session_sends_correct_body(client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/v1/workspaces/1/sessions").mock(
            return_value=httpx.Response(201, json={"id": 5, "name": "my-session"})
        )
        result = await client.create_session(
            1, "my-session", mode="prompt", provider="", agent_tool="opencode"
        )
        assert route.called
        sent_body = route.calls[0].request
        import json
        body = json.loads(sent_body.content)
        assert body["name"] == "my-session"
        assert body["mode"] == "prompt"
        assert body["agent_tool"] == "opencode"
    assert result["id"] == 5


@pytest.mark.asyncio
async def test_session_mcp_selection_is_propagated(client):
    with respx.mock(base_url=BASE_URL) as mock:
        create_route = mock.post("/api/v1/workspaces/1/sessions").mock(
            return_value=httpx.Response(201, json={"id": 5})
        )
        update_route = mock.put("/api/v1/workspaces/1/sessions/5").mock(
            return_value=httpx.Response(200, json={"id": 5})
        )
        await client.create_session(1, "selected", mcp_server_ids=[3])
        create_body = json.loads(create_route.calls[0].request.content)
        assert create_body["mcp_server_ids"] == [3]
        assert "mcp_selection" not in create_body

        await client.create_session(1, "inherited", mcp_selection="inherit")
        inherit_body = json.loads(create_route.calls[1].request.content)
        assert inherit_body["mcp_selection"] == "inherit"
        assert "mcp_server_ids" not in inherit_body

        await client.update_session(1, 5, mcp_server_ids=[])
        disable_body = json.loads(update_route.calls[0].request.content)
        assert disable_body["mcp_server_ids"] == []

        await client.update_session(1, 5, mcp_selection="inherit")
        inherit_update_body = json.loads(update_route.calls[1].request.content)
        assert inherit_update_body["mcp_selection"] == "inherit"


@pytest.mark.asyncio
async def test_list_mcp_servers(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces/1/mcp-servers").mock(
            return_value=httpx.Response(200, json=[{"id": 2, "slug": "jira"}])
        )
        result = await client.list_mcp_servers(1)
    assert result == [{"id": 2, "slug": "jira"}]


@pytest.mark.asyncio
async def test_create_session_with_shell_tool(client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/v1/workspaces/1/sessions").mock(
            return_value=httpx.Response(201, json={"id": 6, "name": "shell-session", "agent_tool": "shell"})
        )
        result = await client.create_session(
            1, "shell-session", mode="prompt", agent_tool="shell", instruction_prompt="echo hello"
        )
        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["name"] == "shell-session"
        assert body["agent_tool"] == "shell"
        assert body["instruction_prompt"] == "echo hello"
    assert result["id"] == 6


@pytest.mark.asyncio
async def test_update_session_agent_tool(client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.put("/api/v1/workspaces/1/sessions/5").mock(
            return_value=httpx.Response(200, json={"id": 5, "name": "my-session", "agent_tool": "shell"})
        )
        result = await client.update_session(1, 5, agent_tool="shell")
        assert route.called
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["agent_tool"] == "shell"
    assert result["agent_tool"] == "shell"


@pytest.mark.asyncio
async def test_launch_session(client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/v1/workspaces/1/sessions/5/launch").mock(
            return_value=httpx.Response(200, json={"id": 5, "phase": "pending"})
        )
        result = await client.launch_session(1, 5, instruction_prompt="run-only context")
        import json
        body = json.loads(route.calls[0].request.content)
        assert body == {"instruction_prompt": "run-only context"}
    assert result["phase"] == "pending"


@pytest.mark.asyncio
async def test_get_session_output(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces/1/sessions/5/output").mock(
            return_value=httpx.Response(200, json={"output": "hello world"})
        )
        result = await client.get_session_output(1, 5)
    assert result["output"] == "hello world"


@pytest.mark.asyncio
async def test_session_run_history_client_methods(client):
    large_content = "complete stored output " * 10_000
    detail = {"id": 19, "last_output": large_content, "raw_output": "raw"}
    with respx.mock(base_url=BASE_URL) as mock:
        summaries_route = mock.get(
            "/api/v1/workspaces/1/sessions/5/runs/summaries",
            params={"limit": "7"},
        ).mock(return_value=httpx.Response(200, json=[{"id": 19, "status": "succeeded"}]))
        detail_route = mock.get(
            "/api/v1/workspaces/1/sessions/5/runs/19"
        ).mock(return_value=httpx.Response(200, json=detail))
        summaries = await client.list_session_runs(1, 5, limit=7)
        result = await client.get_session_run(1, 5, 19)
    assert summaries_route.called
    assert summaries == [{"id": 19, "status": "succeeded"}]
    assert detail_route.called
    assert result["last_output"] == large_content


@pytest.mark.asyncio
async def test_add_repo(client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/api/v1/workspaces/1/sessions/5/repos").mock(
            return_value=httpx.Response(201, json={"id": 3, "repo_url": "https://github.com/org/repo"})
        )
        result = await client.add_repo(1, 5, "https://github.com/org/repo", "main")
        assert route.called
    assert result["id"] == 3


@pytest.mark.asyncio
async def test_list_prompt_sources(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces/1/prompts").mock(
            return_value=httpx.Response(200, json=[
                {"id": 1, "name": "CVE Prompts", "prompts": [
                    {"id": 10, "display_name": "CVE Triage", "filename": "cve-triage.md"}
                ]}
            ])
        )
        result = await client.list_prompt_sources(1)
    assert len(result) == 1
    assert result[0]["name"] == "CVE Prompts"
    assert len(result[0]["prompts"]) == 1


@pytest.mark.asyncio
async def test_prompt_source_crud_and_refresh_use_workspace_scoped_paths(client):
    source = {
        "id": 7,
        "workspace_id": 3,
        "name": "Team prompts",
        "repo_url": "https://github.com/example/prompts",
        "branch": "main",
        "folder_path": ".",
        "github_pat_id": 12,
        "last_synced_at": "2026-10-08T12:00:00",
        "sync_error": "",
        "prompts": [],
    }
    with respx.mock(base_url=BASE_URL) as mock:
        create_route = mock.post("/api/v1/workspaces/3/prompts").mock(
            return_value=httpx.Response(201, json=source)
        )
        update_route = mock.put("/api/v1/workspaces/3/prompts/7").mock(
            return_value=httpx.Response(200, json={**source, "name": "Renamed"})
        )
        delete_route = mock.delete("/api/v1/workspaces/3/prompts/7").mock(
            return_value=httpx.Response(200, json={"detail": "Prompt source deleted."})
        )
        refresh_route = mock.post("/api/v1/workspaces/3/prompts/7/refresh").mock(
            return_value=httpx.Response(200, json=source)
        )

        created = await client.create_prompt_source(
            3, "Team prompts", "https://github.com/example/prompts", github_pat_id=12
        )
        create_body = json.loads(create_route.calls[0].request.content)
        assert create_body == {
            "name": "Team prompts",
            "repo_url": "https://github.com/example/prompts",
            "branch": "main",
            "folder_path": ".",
            "github_pat_id": 12,
        }
        assert created["id"] == 7

        updated = await client.update_prompt_source(3, 7, name="Renamed")
        update_body = json.loads(update_route.calls[0].request.content)
        assert update_body == {"name": "Renamed"}
        assert updated["name"] == "Renamed"

        deleted = await client.delete_prompt_source(3, 7)
        assert deleted == {"detail": "Prompt source deleted."}

        refreshed = await client.refresh_prompt_source(3, 7)
        assert refreshed["id"] == 7

    assert create_route.called and update_route.called
    assert delete_route.called and refresh_route.called


@pytest.mark.asyncio
async def test_update_prompt_source_omits_null_fields_including_pat(client):
    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.put("/api/v1/workspaces/5/prompts/19").mock(
            return_value=httpx.Response(200, json={"id": 19})
        )
        await client.update_prompt_source(
            5, 19, name="Updated", repo_url=None, github_pat_id=None
        )
        assert json.loads(route.calls[0].request.content) == {"name": "Updated"}


@pytest.mark.asyncio
async def test_prompt_source_not_found_and_refresh_errors_are_surfaced(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.put("/api/v1/workspaces/5/prompts/99").mock(
            return_value=httpx.Response(404, json={"detail": "Prompt source not found"})
        )
        mock.post("/api/v1/workspaces/5/prompts/99/refresh").mock(
            return_value=httpx.Response(502, json={"detail": "GitHub sync unavailable"})
        )

        with pytest.raises(AgentSwarmAPIError, match="404") as missing:
            await client.update_prompt_source(5, 99, name="missing")
        assert missing.value.detail == "Prompt source not found"

        with pytest.raises(AgentSwarmAPIError, match="502") as failed_refresh:
            await client.refresh_prompt_source(5, 99)
        assert failed_refresh.value.detail == "GitHub sync unavailable"


@pytest.mark.asyncio
async def test_401_raises_api_error_with_message(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces").mock(
            return_value=httpx.Response(401, json={"detail": "Unauthorized"})
        )
        with pytest.raises(AgentSwarmAPIError) as exc_info:
            await client.list_workspaces()
    assert exc_info.value.status_code == 401
    assert "expired" in exc_info.value.detail.lower() or "unauthorized" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_404_raises_api_error(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces/999").mock(
            return_value=httpx.Response(404, json={"detail": "Not Found"})
        )
        with pytest.raises(AgentSwarmAPIError) as exc_info:
            await client.get_workspace(999)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_repo(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.delete("/api/v1/workspaces/1/sessions/5/repos/3").mock(
            return_value=httpx.Response(200, json={"detail": "deleted"})
        )
        result = await client.delete_repo(1, 5, 3)
    assert result is not None


@pytest.mark.asyncio
async def test_workspace_crud(client):
    with respx.mock(base_url=BASE_URL) as mock:
        post_route = mock.post("/api/v1/workspaces").mock(
            return_value=httpx.Response(201, json={"id": 2, "display_name": "ws2", "description": "desc"})
        )
        put_route = mock.put("/api/v1/workspaces/2").mock(
            return_value=httpx.Response(200, json={"id": 2, "display_name": "ws2-updated", "description": "new-desc"})
        )
        del_route = mock.delete("/api/v1/workspaces/2").mock(
            return_value=httpx.Response(200, json={"detail": "deleted"})
        )

        created = await client.create_workspace("ws2", "desc")
        assert post_route.called
        assert created["id"] == 2
        assert created["display_name"] == "ws2"

        updated = await client.update_workspace(2, "ws2-updated", "new-desc")
        assert put_route.called
        assert updated["display_name"] == "ws2-updated"

        deleted = await client.delete_workspace(2)
        assert del_route.called
        assert deleted == {"detail": "deleted"}


@pytest.mark.asyncio
async def test_workspace_members(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/workspaces/1/members").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "workspace_id": 1, "user_id": "alice", "role": "member"}])
        )
        mock.post("/api/v1/workspaces/1/members").mock(
            return_value=httpx.Response(201, json={"id": 2, "workspace_id": 1, "user_id": "bob", "role": "admin"})
        )
        mock.delete("/api/v1/workspaces/1/members/bob").mock(
            return_value=httpx.Response(200, json={"detail": "bob removed"})
        )

        members = await client.list_workspace_members(1)
        assert len(members) == 1
        assert members[0]["user_id"] == "alice"

        added = await client.add_workspace_member(1, "bob", role="admin")
        assert added["user_id"] == "bob"

        removed = await client.remove_workspace_member(1, "bob")
        assert removed == {"detail": "bob removed"}


@pytest.mark.asyncio
async def test_me_and_admins(client):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.get("/api/v1/me").mock(
            return_value=httpx.Response(200, json={
                "username": "alice",
                "is_admin": True,
                "can_create_workspace": True,
                "admin_bootstrap_available": False,
            })
        )
        mock.get("/api/v1/users").mock(
            return_value=httpx.Response(200, json={"users": ["alice", "bob"]})
        )
        mock.get("/api/v1/admins").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "user_id": "alice", "created_by": "bootstrap"}])
        )
        mock.post("/api/v1/admins").mock(
            return_value=httpx.Response(201, json={"id": 2, "user_id": "bob", "created_by": "alice"})
        )
        mock.delete("/api/v1/admins/bob").mock(
            return_value=httpx.Response(200, json={"detail": "bob removed from admins."})
        )
        mock.post("/api/v1/admins/bootstrap").mock(
            return_value=httpx.Response(201, json={"id": 1, "user_id": "alice", "created_by": "bootstrap"})
        )

        me = await client.get_me()
        assert me["username"] == "alice"
        assert me["is_admin"] is True

        users = await client.list_known_users()
        assert users == ["alice", "bob"]

        admins = await client.list_admins()
        assert len(admins) == 1

        added = await client.add_admin("bob")
        assert added["user_id"] == "bob"

        removed = await client.remove_admin("bob")
        assert removed == {"detail": "bob removed from admins."}

        bootstrapped = await client.bootstrap_admin()
        assert bootstrapped["created_by"] == "bootstrap"

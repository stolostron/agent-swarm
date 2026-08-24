"""Tests for the AgentSwarmClient using respx HTTP mocking."""

from __future__ import annotations

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
        mock.post("/api/v1/workspaces/1/sessions/5/launch").mock(
            return_value=httpx.Response(200, json={"id": 5, "phase": "pending"})
        )
        result = await client.launch_session(1, 5)
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

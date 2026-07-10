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

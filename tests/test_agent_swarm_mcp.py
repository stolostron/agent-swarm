import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer import crypto
from swarmer.agent_tools.opencode import OpenCodeStrategy
from swarmer.config import settings
from swarmer.openshell_policy import build_session_network_policies
from swarmer.session_auth import mint_session_token, validate_session_token


def test_session_token_is_workspace_bound_and_tamper_evident(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "secret_key_file", str(tmp_path / "secret.key"))
    crypto.init_crypto(settings.secret_key_file)

    token = mint_session_token(12, 34)
    assert validate_session_token(token) == {"session_id": 12, "workspace_id": 34}
    assert validate_session_token(token + "x") is None


def test_session_token_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "secret_key_file", str(tmp_path / "secret.key"))
    crypto.init_crypto(settings.secret_key_file)
    token = mint_session_token(12, 34, ttl=-1)
    assert validate_session_token(token) is None


def test_opencode_builds_each_mcp_command():
    server = SimpleNamespace(slug="agent-swarm", command="agent-swarm-mcp-server")
    config = OpenCodeStrategy().build_config_data(mcp_servers=[server], model="gemini")
    assert config["opencode.json"].find('"agent-swarm"') >= 0
    assert '"agent-swarm-mcp-server"' in config["opencode.json"]
    assert '"AGENT_SWARM_VERIFY_SSL": "false"' in config["opencode.json"]


def test_agent_swarm_policy_is_conditional(monkeypatch):
    monkeypatch.setattr(settings, "agent_swarm_internal_url", "http://swarmer.test:8080")
    session = SimpleNamespace(language="python")
    without = build_session_network_policies(session, [], [], "shell", "")
    with_mcp = build_session_network_policies(
        session, [], [SimpleNamespace(slug="agent-swarm")], "shell", ""
    )
    assert "swarm_mcp" not in without
    assert with_mcp["swarm_mcp"]["endpoints"][0]["host"] == "swarmer.test"
    assert with_mcp["swarm_mcp"]["endpoints"][0]["port"] == 8080


def test_agent_swarm_provider_binds_token_to_internal_endpoint(monkeypatch):
    from swarmer import openshell_client

    monkeypatch.setattr(settings, "agent_swarm_internal_url", "http://swarmer.test:8080")
    profile = openshell_client._agent_swarm_profile()
    assert profile["credentials"][0]["env_vars"] == ["AGENT_SWARM_API_TOKEN"]
    assert profile["credentials"][0]["header_name"] == "authorization"
    assert profile["endpoints"] == [{
        "host": "swarmer.test",
        "port": 8080,
        "protocol": "rest",
        "access": "read-write",
        "enforcement": "enforce",
    }]
    assert {binary["path"] for binary in profile["binaries"]} >= {
        "/usr/local/bin/agent-swarm-mcp-server",
        "/usr/local/bin/python3.14",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
        "/sandbox/.venv/bin/python*",
    }


@pytest.mark.asyncio
async def test_agent_swarm_token_refresh_updates_provider(monkeypatch):
    from swarmer import openshell_client, session_auth

    ensure_provider = AsyncMock()
    monkeypatch.setattr(openshell_client, "ensure_provider", ensure_provider)
    sleeps = 0

    async def sleep(_interval):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_auth.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await session_auth.start_token_refresh_loop(12, 34, "agent-swarm-provider", refresh_interval=1)

    ensure_provider.assert_awaited_once()
    args = ensure_provider.await_args
    assert args.args[:3] == ("agent-swarm-provider", "agent-swarm", {})
    assert "AGENT_SWARM_API_TOKEN" in args.kwargs["credentials"]

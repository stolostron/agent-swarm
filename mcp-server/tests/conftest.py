"""Shared test fixtures for agent-swarm MCP server tests."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_swarm_mcp_server.client import AgentSwarmClient
from agent_swarm_mcp_server.config import AgentSwarmConfig


SAMPLE_KUBECONFIG = textwrap.dedent("""\
    apiVersion: v1
    kind: Config
    current-context: my-cluster
    contexts:
    - name: my-cluster
      context:
        cluster: my-cluster
        user: my-user
    clusters:
    - name: my-cluster
      cluster:
        server: https://api.example.com:6443
    users:
    - name: my-user
      user:
        token: kubeconfig-token-abc123
""")

SAMPLE_KUBECONFIG_EXEC = textwrap.dedent("""\
    apiVersion: v1
    kind: Config
    current-context: exec-cluster
    contexts:
    - name: exec-cluster
      context:
        cluster: exec-cluster
        user: exec-user
    clusters:
    - name: exec-cluster
      cluster:
        server: https://api.example.com:6443
    users:
    - name: exec-user
      user:
        exec:
          command: echo
          args: ['{"apiVersion":"client.authentication.k8s.io/v1beta1","status":{"token":"exec-token-xyz"}}']
""")


@pytest.fixture
def kubeconfig_file(tmp_path: Path) -> Path:
    kc = tmp_path / "kubeconfig"
    kc.write_text(SAMPLE_KUBECONFIG)
    return kc


@pytest.fixture
def mock_client() -> AgentSwarmClient:
    client = MagicMock(spec=AgentSwarmClient)
    # Make all methods async by default
    for attr in dir(client):
        if not attr.startswith("_"):
            method = getattr(client, attr)
            if callable(method):
                setattr(client, attr, AsyncMock())
    return client


@pytest.fixture
def sample_workspace() -> dict:
    return {"id": 1, "display_name": "My Workspace", "namespace": "my-ns", "description": "test"}


@pytest.fixture
def sample_session() -> dict:
    return {
        "id": 10,
        "workspace_id": 1,
        "name": "my-session",
        "phase": "idle",
        "mode": "prompt",
        "model": "google-vertex-anthropic/claude-sonnet-4-6@default",
        "agent_tool": "opencode",
        "persist": False,
        "working_branch": "swarmer/session-10-abc",
        "prompt_id": None,
        "instruction_prompt": "",
        "status_detail": "",
        "run_duration": None,
        "run_started_at": None,
        "run_completed_at": None,
        "is_active": False,
    }


@pytest.fixture
def sample_repos() -> list[dict]:
    return [
        {
            "id": 1,
            "session_id": 10,
            "repo_url": "https://github.com/stolostron/agent-swarm",
            "branch": "main",
            "local_path": "agent-swarm",
        }
    ]

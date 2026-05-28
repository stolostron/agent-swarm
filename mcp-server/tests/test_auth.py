"""Tests for the cascading token resolution in auth.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_swarm_mcp_server import auth


def test_env_var_takes_precedence(monkeypatch, tmp_path):
    """AGENT_SWARM_API_TOKEN wins over all other sources."""
    monkeypatch.setenv("AGENT_SWARM_API_TOKEN", "explicit-token")
    assert auth.resolve_token() == "explicit-token"


def test_in_cluster_token(monkeypatch, tmp_path):
    """Reads from in-cluster SA token file when env var is absent."""
    token_file = tmp_path / "token"
    token_file.write_text("in-cluster-token")
    monkeypatch.delenv("AGENT_SWARM_API_TOKEN", raising=False)
    with patch.object(auth, "_IN_CLUSTER_TOKEN", token_file):
        result = auth.resolve_token()
    assert result == "in-cluster-token"


def test_in_cluster_skipped_when_file_missing(monkeypatch, tmp_path):
    """Falls through to kubeconfig when SA token file doesn't exist."""
    monkeypatch.delenv("AGENT_SWARM_API_TOKEN", raising=False)
    missing_file = tmp_path / "nonexistent"
    kubeconfig_file = tmp_path / "kubeconfig"
    kubeconfig_file.write_text(
        "apiVersion: v1\nkind: Config\ncurrent-context: ctx\n"
        "contexts:\n- name: ctx\n  context:\n    user: u\n"
        "users:\n- name: u\n  user:\n    token: kube-token\n"
        "clusters: []\n"
    )
    with (
        patch.object(auth, "_IN_CLUSTER_TOKEN", missing_file),
        patch.object(auth, "_resolve_kubeconfig_path", return_value=kubeconfig_file),
    ):
        result = auth.resolve_token()
    assert result == "kube-token"


def test_kubeconfig_token(monkeypatch, kubeconfig_file):
    """Extracts direct token from kubeconfig."""
    monkeypatch.delenv("AGENT_SWARM_API_TOKEN", raising=False)
    missing = Path("/nonexistent/sa/token")
    with (
        patch.object(auth, "_IN_CLUSTER_TOKEN", missing),
        patch.object(auth, "_resolve_kubeconfig_path", return_value=kubeconfig_file),
    ):
        result = auth.resolve_token()
    assert result == "kubeconfig-token-abc123"


def test_no_token_raises(monkeypatch, tmp_path):
    """RuntimeError when no token source is available."""
    monkeypatch.delenv("AGENT_SWARM_API_TOKEN", raising=False)
    missing = Path("/nonexistent/sa/token")
    with (
        patch.object(auth, "_IN_CLUSTER_TOKEN", missing),
        patch.object(auth, "_resolve_kubeconfig_path", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="No K8s bearer token found"):
            auth.resolve_token()


def test_env_var_overrides_in_cluster(monkeypatch, tmp_path):
    """Explicit env var takes priority over in-cluster token."""
    monkeypatch.setenv("AGENT_SWARM_API_TOKEN", "override-token")
    token_file = tmp_path / "token"
    token_file.write_text("in-cluster-token")
    with patch.object(auth, "_IN_CLUSTER_TOKEN", token_file):
        result = auth.resolve_token()
    assert result == "override-token"


def test_kubeconfig_env_var_path(monkeypatch, tmp_path):
    """Respects KUBECONFIG environment variable path."""
    kc = tmp_path / "my-kubeconfig"
    kc.write_text(
        "apiVersion: v1\nkind: Config\ncurrent-context: ctx\n"
        "contexts:\n- name: ctx\n  context:\n    user: u\n"
        "users:\n- name: u\n  user:\n    token: env-kube-token\n"
        "clusters: []\n"
    )
    monkeypatch.setenv("KUBECONFIG", str(kc))
    result = auth._resolve_kubeconfig_path()
    assert result == kc


def test_exec_credential_provider(monkeypatch, tmp_path):
    """Runs exec provider command and extracts token from JSON output."""
    exec_output = json.dumps({
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "status": {"token": "exec-provider-token"},
    })
    exec_config = {
        "command": "echo",
        "args": [exec_output],
    }
    result = auth._exec_credential_provider(exec_config)
    assert result == "exec-provider-token"


def test_exec_provider_failure_returns_none():
    """Returns None when exec provider exits non-zero."""
    exec_config = {"command": "false", "args": []}
    result = auth._exec_credential_provider(exec_config)
    assert result is None

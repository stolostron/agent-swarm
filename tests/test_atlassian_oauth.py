"""
Tests for the Atlassian Rovo MCP Server integration.

The integration now works by embedding the MCP server entry directly in
opencode.json via build_config_data().  OpenCode handles the OAuth flow
itself at runtime — no swarmer-side OAuth plumbing is needed.

Covers:
  - opencode.build_config_data includes atlassian-rovo MCP entry
  - opencode.build_share_setup_cmd (no has_atlassian_oauth param)
  - crush.build_share_setup_cmd (no has_atlassian_oauth param)
  - k8s_session.build_session_pod (no has_atlassian_oauth param)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# opencode.build_config_data — Atlassian Rovo MCP entry
# ---------------------------------------------------------------------------

class TestOpencodeConfigData:
    """build_config_data embeds the atlassian-rovo MCP server in opencode.json."""

    def _make_strategy(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        return OpenCodeStrategy()

    def test_atlassian_rovo_mcp_present(self):
        strategy = self._make_strategy()
        data = strategy.build_config_data()
        config = json.loads(data["opencode.json"])
        assert "mcp" in config
        assert "atlassian-rovo" in config["mcp"]

    def test_atlassian_rovo_type_is_remote(self):
        strategy = self._make_strategy()
        config = json.loads(strategy.build_config_data()["opencode.json"])
        assert config["mcp"]["atlassian-rovo"]["type"] == "remote"

    def test_atlassian_rovo_url(self):
        strategy = self._make_strategy()
        config = json.loads(strategy.build_config_data()["opencode.json"])
        assert config["mcp"]["atlassian-rovo"]["url"] == "https://mcp.atlassian.com/v1/mcp"

    def test_atlassian_rovo_enabled(self):
        strategy = self._make_strategy()
        config = json.loads(strategy.build_config_data()["opencode.json"])
        assert config["mcp"]["atlassian-rovo"]["enabled"] is True

    def test_no_hardcoded_token_in_config(self):
        """The static config must never embed a bearer token."""
        strategy = self._make_strategy()
        raw = strategy.build_config_data()["opencode.json"]
        assert "Bearer" not in raw
        assert "Authorization" not in raw


# ---------------------------------------------------------------------------
# opencode.build_share_setup_cmd — no atlassian_oauth param
# ---------------------------------------------------------------------------

class TestOpencodeShareSetupCmd:
    """build_share_setup_cmd no longer takes has_atlassian_oauth."""

    def _make_strategy(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        return OpenCodeStrategy()

    def test_returns_string(self):
        strategy = self._make_strategy()
        cmd = strategy.build_share_setup_cmd()
        assert isinstance(cmd, str) and len(cmd) > 0

    def test_sets_up_opencode_symlink(self):
        strategy = self._make_strategy()
        cmd = strategy.build_share_setup_cmd()
        assert "mkdir -p /workspace/.opencode" in cmd
        assert "ln -sf /workspace/.opencode" in cmd

    def test_no_atlassian_token_injection(self):
        """Token injection is no longer done at startup — opencode handles it."""
        strategy = self._make_strategy()
        cmd = strategy.build_share_setup_cmd()
        assert "ATLASSIAN_MCP_TOKEN" not in cmd
        assert "atlassian" not in cmd.lower()

    def test_signature_has_no_has_atlassian_oauth(self):
        """Ensure the old has_atlassian_oauth parameter is gone."""
        import inspect
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        sig = inspect.signature(OpenCodeStrategy.build_share_setup_cmd)
        assert "has_atlassian_oauth" not in sig.parameters


# ---------------------------------------------------------------------------
# crush.build_share_setup_cmd — no atlassian_oauth param
# ---------------------------------------------------------------------------

class TestCrushShareSetupCmd:
    def test_signature_has_no_has_atlassian_oauth(self):
        import inspect
        from swarmer.agent_tools.crush import CrushStrategy
        sig = inspect.signature(CrushStrategy.build_share_setup_cmd)
        assert "has_atlassian_oauth" not in sig.parameters

    def test_returns_string(self):
        from swarmer.agent_tools.crush import CrushStrategy
        cmd = CrushStrategy().build_share_setup_cmd()
        assert isinstance(cmd, str)


# ---------------------------------------------------------------------------
# k8s_session.build_session_pod — no has_atlassian_oauth param
# ---------------------------------------------------------------------------

class TestBuildSessionPod:
    """build_session_pod no longer accepts has_atlassian_oauth."""

    def _make_session(self, session_id: int = 1):
        session = MagicMock()
        session.id = session_id
        session.pvc_name = "pvc-1"
        session.github_pat = None
        session.repos = []
        session.instruction_prompt = ""
        session.mode = "prompt"
        session.model = ""
        session.resume = False
        session.privileged = False
        session.working_branch = ""
        session.agent_tool = "opencode"
        return session

    def test_signature_has_no_has_atlassian_oauth(self):
        import inspect
        from swarmer.k8s_session import build_session_pod
        sig = inspect.signature(build_session_pod)
        assert "has_atlassian_oauth" not in sig.parameters

    def test_builds_pod_without_atlassian_env_from(self):
        session = self._make_session(1)
        mock_k8s_client = MagicMock()
        mock_k8s_client.V1EnvFromSource = lambda **kw: {"type": "envFrom", **kw}
        mock_k8s_client.V1SecretEnvSource = lambda **kw: {"secretEnvSource": kw}
        mock_k8s_client.V1EnvVar = lambda **kw: {"envVar": kw}
        mock_k8s_client.V1Container = lambda **kw: kw
        mock_k8s_client.V1Pod = lambda **kw: kw
        mock_k8s_client.V1PodSpec = lambda **kw: kw
        mock_k8s_client.V1ObjectMeta = lambda **kw: kw
        mock_k8s_client.V1PodSecurityContext = lambda **kw: kw
        mock_k8s_client.V1SecurityContext = lambda **kw: kw
        mock_k8s_client.V1ResourceRequirements = lambda **kw: kw
        mock_k8s_client.V1VolumeMount = lambda **kw: kw
        mock_k8s_client.V1Volume = lambda **kw: kw
        mock_k8s_client.V1PersistentVolumeClaimVolumeSource = lambda **kw: kw
        mock_k8s_client.V1ConfigMapVolumeSource = lambda **kw: kw
        mock_k8s_client.V1LocalObjectReference = lambda **kw: kw
        mock_k8s_client.V1ContainerPort = lambda **kw: kw

        import kubernetes
        with patch.object(kubernetes, "client", mock_k8s_client):
            with patch("swarmer.k8s_session.settings") as mock_settings:
                mock_settings.agent_image_pull_policy = "IfNotPresent"
                from swarmer.k8s_session import build_session_pod
                pod = build_session_pod(
                    session=session,
                    namespace="test-ns",
                    image="test-image:latest",
                    suffix="abcd",
                    agent_tool="opencode",
                )

        env_from = pod["spec"]["containers"][0]["env_from"]
        # No atlassian-oauth secret should be in envFrom
        secret_names = []
        for s in env_from:
            if isinstance(s, dict) and "secret_ref" in s:
                ref = s["secret_ref"]
                if isinstance(ref, dict) and "secretEnvSource" in ref:
                    secret_names.append(ref["secretEnvSource"].get("name", ""))
        assert not any("atlassian-oauth" in n for n in secret_names)

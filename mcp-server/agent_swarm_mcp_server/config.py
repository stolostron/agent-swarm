"""Configuration for the Agent Swarm MCP server.

Environment variables:
    AGENT_SWARM_API_URL      Required. Base URL of the agent-swarm REST API.
                             e.g. https://swarmer-swarmer.apps.example.com
    AGENT_SWARM_API_TOKEN    Optional. K8s bearer token. Falls back to
                             in-cluster SA token or kubeconfig.
    AGENT_SWARM_WORKSPACE    Optional. Default workspace name used when
                             workspace_id is omitted from tool calls.
    AGENT_SWARM_VERIFY_SSL   Optional. Set to 'false' to disable SSL
                             verification (self-signed certs). Default: true.
                             Ignored when AGENT_SWARM_SSL_CA_BUNDLE is set.
    AGENT_SWARM_SSL_CA_BUNDLE Optional. Path to a PEM-encoded CA certificate
                             file or directory to trust for SSL verification.
                             Use this to trust a self-signed or private CA
                             without disabling SSL verification entirely.
"""

from __future__ import annotations

import os

from .auth import resolve_token


class AgentSwarmConfig:
    def __init__(
        self,
        api_url: str,
        token: str,
        default_workspace: str | None = None,
        verify_ssl: bool = True,
        ssl_ca_bundle: str | None = None,
    ):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.default_workspace = default_workspace
        self.verify_ssl = verify_ssl
        self.ssl_ca_bundle = ssl_ca_bundle

    @classmethod
    def from_env(cls) -> "AgentSwarmConfig":
        api_url = os.environ.get("AGENT_SWARM_API_URL", "").strip()
        if not api_url:
            raise RuntimeError(
                "AGENT_SWARM_API_URL is required. "
                "Set it to the base URL of your agent-swarm instance."
            )
        token = resolve_token()
        default_workspace = os.environ.get("AGENT_SWARM_WORKSPACE", "").strip() or None
        verify_ssl = os.environ.get("AGENT_SWARM_VERIFY_SSL", "true").strip().lower() != "false"
        ssl_ca_bundle = os.environ.get("AGENT_SWARM_SSL_CA_BUNDLE", "").strip() or None
        return cls(
            api_url=api_url,
            token=token,
            default_workspace=default_workspace,
            verify_ssl=verify_ssl,
            ssl_ca_bundle=ssl_ca_bundle,
        )

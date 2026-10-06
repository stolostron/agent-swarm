"""Focused coverage for launch-time session MCP selection."""

from types import SimpleNamespace

from swarmer.models.session import Session
from swarmer.routers.sessions import _select_session_mcp_servers


def test_launch_mcp_selection_honors_disabled_inherited_and_explicit_modes():
    first = SimpleNamespace(id=1, slug="jira", enabled=True, auth_status="active")
    second = SimpleNamespace(id=2, slug="agent-swarm", enabled=True, auth_status="active")
    disabled = SimpleNamespace(id=3, slug="disabled", enabled=False, auth_status="active")
    expired = SimpleNamespace(id=4, slug="expired", enabled=True, auth_status="expired")
    eligible = [first, second, disabled, expired]

    disabled = Session(mcp_server_ids="none")
    assert _select_session_mcp_servers(disabled, eligible) == []

    inherited = Session(mcp_server_ids="")
    assert _select_session_mcp_servers(inherited, eligible) == [first, second]
    assert _select_session_mcp_servers(inherited, []) is None

    selected = Session(mcp_server_ids="2,3,4")
    assert _select_session_mcp_servers(selected, eligible) == [second]

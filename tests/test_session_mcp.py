"""Focused coverage for launch-time session MCP selection."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData

from swarmer.models.session import Session
from swarmer.routers.sessions import (
    _apply_form_mcp_selection,
    _parse_mcp_server_ids,
    _select_session_mcp_servers,
    session_create,
    session_edit,
)


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


def test_unrelated_session_form_save_preserves_mcp_configuration():
    session = Session(mcp_server_ids="17,23")

    _apply_form_mcp_selection(
        session, FormData([("name", "renamed")]), [], creating=False
    )

    assert session.mcp_server_ids == "17,23"


def test_intentional_empty_mcp_selection_disables_mcp_access():
    session = Session(mcp_server_ids="17,23")
    form_data = FormData([("mcp_settings_changed", "1")])

    _apply_form_mcp_selection(session, form_data, [], creating=False)

    assert session.mcp_selection == "disabled"


def test_explicit_server_selection_overrides_inherit_and_limits_launch_access():
    selected_server = SimpleNamespace(
        id=23, slug="jira", enabled=True, auth_status="active"
    )
    other_server = SimpleNamespace(
        id=17, slug="agent-swarm", enabled=True, auth_status="active"
    )
    session = Session(mcp_server_ids="")
    form_data = FormData(
        [
            ("mcp_settings_changed", "1"),
            ("mcp_selection", "inherit"),
            ("mcp_server_ids", "23"),
        ]
    )

    _apply_form_mcp_selection(
        session, form_data, _parse_mcp_server_ids(form_data.getlist("mcp_server_ids")),
        creating=False,
    )

    assert session.mcp_selection == "selected"
    assert session.enabled_mcp_ids == [23]
    assert _select_session_mcp_servers(session, [selected_server, other_server]) == [
        selected_server
    ]


def test_mcp_form_ids_reject_malformed_values_instead_of_dropping_them():
    with pytest.raises(HTTPException) as exc_info:
        _parse_mcp_server_ids(["17", "not-an-id"])

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_malformed_edit_mcp_id_does_not_mutate_session():
    session = SimpleNamespace(
        workspace_id=1, name="before", phase="idle", is_active=False
    )

    class RequestWithForm:
        def __init__(self):
            self.session = {"username": "test-user"}

        async def form(self):
            return FormData([("mcp_server_ids", "invalid")])

    class Database:
        async def get(self, model, session_id):
            return session

    with pytest.raises(HTTPException) as exc_info:
        await session_edit(
            1,
            2,
            RequestWithForm(),
            name="after",
            github_pat_id="",
            mode="prompt",
            provider="",
            agent_tool="opencode",
            db=Database(),
        )

    assert exc_info.value.status_code == 422
    assert session.name == "before"


@pytest.mark.asyncio
async def test_malformed_create_mcp_id_does_not_add_session():
    class RequestWithForm:
        def __init__(self):
            self.session = {"username": "test-user"}

        async def form(self):
            return FormData([("mcp_server_ids", "invalid")])

    class Database:
        def __init__(self):
            self.added = []

        async def get(self, model, workspace_id):
            return SimpleNamespace(id=workspace_id)

        def add(self, item):
            self.added.append(item)

    db = Database()
    with pytest.raises(HTTPException) as exc_info:
        await session_create(
            1,
            RequestWithForm(),
            name="not-created",
            github_pat_id="",
            provider="configured-provider",
            agent_tool="opencode",
            working_branch="",
            db=db,
        )

    assert exc_info.value.status_code == 422
    assert db.added == []

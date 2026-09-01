import json
import os
import sys

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest_asyncio.fixture
async def client_unauthenticated():
    from swarmer.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def client_authenticated():
    from swarmer.deps import require_auth
    from swarmer.main import app

    app.dependency_overrides[require_auth] = lambda: None
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_workspace_parse_command_requires_console_auth(client_unauthenticated):
    resp = await client_unauthenticated.post(
        "/workspaces/gateway/parse-command",
        json={"command": "openshell gateway add https://gw-stage.example.com:443"},
    )
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/login"


@pytest.mark.asyncio
async def test_workspace_parse_command_with_console_auth(client_authenticated):
    cmd = (
        "openshell gateway add "
        "https://gw-stage.example.com:443 "
        "--name test-gw "
        "--oidc-issuer https://idp.example.com "
        "--oidc-client-id client-123 "
        "--oidc-audience client-123"
    )
    resp = await client_authenticated.post(
        "/workspaces/gateway/parse-command",
        json={"command": cmd},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["gateway_url"] == "https://gw-stage.example.com:443"
    assert data["auth_mode"] == "oidc"
    assert data["suggested_name"] == "test-gw"
    assert data["oidc_issuer"] == "https://idp.example.com"
    assert data["oidc_client_id"] == "client-123"
    assert data["oidc_audience"] == "client-123"


@pytest.mark.asyncio
async def test_workspace_parse_token_with_console_auth(client_authenticated):
    token_input = json.dumps(
        {
            "refresh_token": "rt-secret-12345",
            "expires_at": 1755000000,
        }
    )
    resp = await client_authenticated.post(
        "/workspaces/gateway/parse-token",
        json={"token_input": token_input},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "valid"
    assert data["refresh_token"] == "rt-secret-12345"
    assert data["expires_at"] == 1755000000

import os
import ssl
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import respx

from swarmer.crypto import init_crypto
from swarmer.models.workspace import Workspace
from swarmer.models.workspace_gateway import WorkspaceGateway
from swarmer.openshell_client import (
    GatewayConfig,
    resolve_gateway_config,
    probe_gateway_connectivity,
)
from swarmer.openshell_oidc import OidcGatewayAuth


@pytest.fixture(autouse=True)
def init_test_crypto(tmp_path):
    key_file = tmp_path / "secret.key"
    init_crypto(str(key_file))


@respx.mock
def test_oidc_auth_refresh_flow():
    issuer = "https://keycloak.example.com/realms/test"
    client_id = "test-client"

    # Mock discovery
    respx.get(f"{issuer}/.well-known/openid-configuration").respond(
        200,
        json={"issuer": issuer, "token_endpoint": f"{issuer}/protocol/openid-connect/token"},
    )

    # Mock token endpoint
    respx.post(f"{issuer}/protocol/openid-connect/token").respond(
        200,
        json={
            "access_token": "new-access-token",
            "refresh_token": "rotated-refresh-token",
            "expires_in": 300,
        },
    )

    auth = OidcGatewayAuth(issuer=issuer, client_id=client_id, workspace_id=1)
    auth.seed(refresh_token="initial-refresh-token")

    token = auth.current_access_token()
    assert token == "new-access-token"
    assert auth._bundle["refresh_token"] == "rotated-refresh-token"
    assert auth._bundle["access_token"] == "new-access-token"


@pytest.mark.asyncio
async def test_resolve_gateway_config_default():
    cfg = await resolve_gateway_config(None)
    assert cfg.auth_mode in ("default", "mtls", "bearer")


@pytest.mark.asyncio
async def test_resolve_gateway_config_custom_workspace():
    ws = Workspace(id=42, display_name="Custom WS", namespace="custom-ws")
    gw = WorkspaceGateway(
        workspace_id=42,
        gateway_url="https://gw-42.example.com:443",
        auth_mode="bearer",
    )
    gw.bearer_token = "secret-bearer-42"
    ws.gateway = gw

    cfg = await resolve_gateway_config(ws)
    assert cfg.gateway_url == "https://gw-42.example.com:443"
    assert cfg.auth_mode == "bearer"
    assert cfg.bearer_token == "secret-bearer-42"
    assert cfg.workspace_id == 42


@pytest.mark.asyncio
async def test_probe_gateway_connectivity_mock():
    cfg = GatewayConfig(gateway_url="https://gw.example.com", auth_mode="none")
    mock_client = MagicMock(spec_set=["list"])
    mock_client.list.return_value = ["sb-1", "sb-2"]

    with patch("swarmer.openshell_client.get_client_for_config", return_value=mock_client):
        res = await probe_gateway_connectivity(cfg)
        assert res["status"] == "ok"
        assert res["sandboxes_count"] == 2
        mock_client.list.assert_called_once_with()


@respx.mock
@pytest.mark.asyncio
async def test_oidc_auth_refresh_inside_event_loop():
    """Verify current_access_token() does not deadlock when executed on the event loop thread."""
    issuer = "https://keycloak.example.com/realms/test"
    client_id = "test-client"

    respx.get(f"{issuer}/.well-known/openid-configuration").respond(
        200,
        json={"issuer": issuer, "token_endpoint": f"{issuer}/protocol/openid-connect/token"},
    )
    respx.post(f"{issuer}/protocol/openid-connect/token").respond(
        200,
        json={
            "access_token": "loop-access-token",
            "refresh_token": "loop-refresh-token",
            "expires_in": 300,
        },
    )

    auth = OidcGatewayAuth(issuer=issuer, client_id=client_id, workspace_id=99)
    auth.seed(refresh_token="initial-loop-token")

    token = auth.current_access_token()
    assert token == "loop-access-token"
    auth.close()


@pytest.mark.asyncio
async def test_scheduler_gc_isolates_gateway_outage():
    """Verify an unreachable gateway does not cause active sessions on it to be marked stopped."""
    from unittest.mock import AsyncMock
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from swarmer.database import Base
    from swarmer.models.session import Session
    from swarmer.scheduler import _collect_orphaned_sandboxes

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async with session_maker() as db:
        ws1 = Workspace(id=1, display_name="WS Default", namespace="ws-default")
        ws2 = Workspace(id=2, display_name="WS Custom", namespace="ws-custom")
        gw2 = WorkspaceGateway(
            workspace_id=2,
            gateway_url="https://gw2.example.com:443",
            auth_mode="bearer",
        )
        gw2.bearer_token = "token-gw2"
        ws2.gateway = gw2

        s1 = Session(id=1, workspace_id=1, name="sess-1", agent_tool="opencode", sandbox_name="sb-ws1", phase="running")
        s2 = Session(id=2, workspace_id=2, name="sess-2", agent_tool="opencode", sandbox_name="sb-ws2", phase="running")

        db.add_all([ws1, ws2, gw2, s1, s2])
        await db.commit()

        async def fake_list_sandboxes(client=None):
            if client is not None:
                raise RuntimeError("Gateway 2 unreachable")
            return ["sb-ws1"]

        with patch("swarmer.openshell_client.list_sandboxes", side_effect=fake_list_sandboxes):
            with patch("swarmer.openshell_client.delete_sandbox", new=AsyncMock()):
                await _collect_orphaned_sandboxes(db)

        # Reload sessions from DB
        sess1 = await db.get(Session, 1)
        sess2 = await db.get(Session, 2)

        assert sess1.phase == "running"
        assert sess1.sandbox_name == "sb-ws1"

        # Session 2 on unreachable gateway 2 must NOT be marked stopped
        assert sess2.phase == "running"
        assert sess2.sandbox_name == "sb-ws2"


def test_oidc_gateway_auth_inline_tls_ca_uses_ssl_context():
    """Inline PEM CA content should not be passed to httpx as a file path."""
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    with patch("swarmer.openshell_oidc.ssl.create_default_context") as create_ctx, patch(
        "swarmer.openshell_oidc.httpx.Client"
    ) as http_client:
        ctx = MagicMock(spec=ssl.SSLContext)
        create_ctx.return_value = ctx

        auth = OidcGatewayAuth(
            issuer="https://idp.example.com/realms/test",
            client_id="client-123",
            tls_ca=pem,
        )
        auth.close()

    create_ctx.assert_called_once_with()
    ctx.load_verify_locations.assert_called_once_with(cadata=pem.strip())
    assert http_client.call_args.kwargs["verify"] is ctx

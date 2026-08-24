"""Unit tests for the /token page and scripts/mcp_setup.py."""
from __future__ import annotations

import base64
import json
import os
import sys
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.mcp_setup import _decode_jwt_sub, _parse_pasted_token_or_json
from swarmer.crypto import encrypt, init_crypto
from swarmer.deps import NotAuthenticated
from swarmer.routers.auth import router as auth_router


@pytest.fixture(autouse=True)
def setup_crypto(tmp_path):
    key_file = tmp_path / "secret.key"
    import base64
    key_file.write_text(base64.urlsafe_b64encode(os.urandom(32)).decode())
    init_crypto(str(key_file))


@pytest_asyncio.fixture
async def app_with_auth():
    app = FastAPI()
    app.add_middleware(
        SessionMiddleware,
        secret_key="test-secret-key-for-token-page!",
        session_cookie="swarmer_session",
    )

    @app.exception_handler(NotAuthenticated)
    async def not_auth_handler(request: Request, exc: NotAuthenticated):
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    @app.get("/_seed")
    async def _seed(request: Request, user: str = "alice", token: str = "fake-k8s-bearer-token"):
        request.session["authenticated"] = True
        request.session["username"] = user
        request.session["k8s_token"] = encrypt(token)
        return JSONResponse({"ok": True})

    app.include_router(auth_router)
    return app


class TestTokenPage:
    @pytest.mark.asyncio
    async def test_unauthenticated_redirects_to_login(self, app_with_auth):
        transport = httpx.ASGITransport(app=app_with_auth)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/token", follow_redirects=False)
            assert resp.status_code == 302
            assert resp.headers["location"] == "/login"

    @pytest.mark.asyncio
    async def test_authenticated_renders_token_and_mcp_config(self, app_with_auth):
        transport = httpx.ASGITransport(app=app_with_auth)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Seed session
            seed_resp = await client.get("/_seed?user=jnpacker&token=my-secret-bearer-token")
            assert seed_resp.status_code == 200

            # Access /token
            resp = await client.get("/token")
            assert resp.status_code == 200
            content = resp.text
            assert "jnpacker" in content
            assert "my-secret-bearer-token" in content
            assert "agent-swarm" in content
            assert "AGENT_SWARM_API_TOKEN" in content
            assert "AGENT_SWARM_API_URL" in content
            assert "make mcp-setup TOKEN=" in content


class TestMcpSetupScript:
    def test_decode_jwt_sub(self):
        payload = json.dumps({"sub": "system:serviceaccount:default:alice"}).encode()
        b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        fake_jwt = f"eyJhbGciOiJSUzI1NiJ9.{b64}.fakesig"
        assert _decode_jwt_sub(fake_jwt) == "system:serviceaccount:default:alice"

        assert _decode_jwt_sub("not-a-jwt") == ""

    def test_parse_pasted_raw_token(self):
        token, url = _parse_pasted_token_or_json("my-bearer-token-12345")
        assert token == "my-bearer-token-12345"
        assert url is None

    def test_parse_pasted_json_snippet(self):
        raw_json = json.dumps({
            "mcp": {
                "agent-swarm": {
                    "environment": {
                        "AGENT_SWARM_API_URL": "https://swarmer.example.com",
                        "AGENT_SWARM_API_TOKEN": "token-from-json"
                    }
                }
            }
        })
        token, url = _parse_pasted_token_or_json(raw_json)
        assert token == "token-from-json"
        assert url == "https://swarmer.example.com"

    def test_parse_pasted_key_value_fragment(self):
        raw = 'AGENT_SWARM_API_TOKEN="token-kv" AGENT_SWARM_API_URL="https://kv.example.com"'
        token, url = _parse_pasted_token_or_json(raw)
        assert token == "token-kv"
        assert url == "https://kv.example.com"

    def test_mcp_setup_updates_opencode_json(self, tmp_path):
        from scripts.mcp_setup import main

        config_file = tmp_path / "opencode.json"
        initial_content = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "jira-mcp-server": {"type": "local", "enabled": True}
            }
        }
        config_file.write_text(json.dumps(initial_content))

        test_args = [
            "mcp_setup.py",
            "--token", "test-token-value-xyz",
            "--url", "https://swarmer.test.example.com",
            "--config", str(config_file),
        ]
        with patch.object(sys, "argv", test_args):
            main()

        updated_data = json.loads(config_file.read_text())
        assert "jira-mcp-server" in updated_data["mcp"]
        assert "agent-swarm" in updated_data["mcp"]
        aswarm = updated_data["mcp"]["agent-swarm"]
        assert aswarm["enabled"] is True
        assert aswarm["environment"]["AGENT_SWARM_API_URL"] == "https://swarmer.test.example.com"
        assert aswarm["environment"]["AGENT_SWARM_API_TOKEN"] == "test-token-value-xyz"
        assert aswarm["environment"]["AGENT_SWARM_VERIFY_SSL"] == "false"


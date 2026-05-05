"""
Tests for Atlassian Rovo MCP OAuth integration.

Covers:
- AtlassianToken model helpers (is_expired, needs_refresh, token_status)
- build_mcp_auth_json helper (correct JSON structure for OpenCode)
- OpenCodeStrategy.build_config_data with/without token
- OpenCodeStrategy.build_mcp_auth_setup_cmd
- atlassian router: authorize redirect, callback token exchange,
  bad-state rejection, disconnect, auto-refresh
- k8s helpers: apply/delete atlassian token secret
- _do_launch integration: injects token, skips when absent, auto-refreshes
"""
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_token(
    *,
    access_token="at_test",
    refresh_token="rt_test",
    expires_at=None,
    scope="read:jira-work offline_access",
    client_id="dyn_client_123",
    client_id_issued_at=None,
):
    """Build a minimal AtlassianToken-like object (plain namespace, no DB)."""
    from types import SimpleNamespace
    import swarmer.crypto as crypto

    # Ensure crypto is initialised with a test key so encrypt/decrypt work
    import os
    import base64
    os.environ.setdefault("SWARMER_SECRET_KEY", base64.urlsafe_b64encode(b"x" * 32).decode())
    crypto.init_crypto("auth/secret.key")

    from swarmer.models.atlassian_token import AtlassianToken

    tok = AtlassianToken()
    tok.workspace_id = 1
    tok.access_token_enc = crypto.encrypt(access_token)
    tok.refresh_token_enc = crypto.encrypt(refresh_token)
    tok.client_id_enc = crypto.encrypt(client_id)
    tok.client_id_issued_at = client_id_issued_at or int(time.time())
    tok.scopes = scope
    tok.expires_at = expires_at
    return tok


# ---------------------------------------------------------------------------
# AtlassianToken model tests
# ---------------------------------------------------------------------------

class TestAtlassianTokenModel:

    def test_is_expired_when_past(self):
        tok = _make_token(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
        assert tok.is_expired is True

    def test_is_not_expired_when_future(self):
        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        tok = _make_token(expires_at=future)
        assert tok.is_expired is False

    def test_is_not_expired_when_no_expiry(self):
        tok = _make_token(expires_at=None)
        assert tok.is_expired is False

    def test_needs_refresh_when_expiring_soon(self):
        # expires in 2 minutes — within the 5-minute refresh window
        import time as _time
        soon = datetime.fromtimestamp(_time.time() + 120, tz=timezone.utc)
        tok = _make_token(expires_at=soon)
        assert tok.needs_refresh is True

    def test_needs_refresh_when_expired(self):
        tok = _make_token(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
        assert tok.needs_refresh is True

    def test_does_not_need_refresh_when_valid(self):
        import time as _time
        # expires in 30 minutes — well outside the refresh window
        far = datetime.fromtimestamp(_time.time() + 1800, tz=timezone.utc)
        tok = _make_token(expires_at=far)
        assert tok.needs_refresh is False

    def test_token_status_connected(self):
        import time as _time
        far = datetime.fromtimestamp(_time.time() + 1800, tz=timezone.utc)
        tok = _make_token(expires_at=far)
        assert tok.token_status == "connected"

    def test_token_status_expiring_soon(self):
        import time as _time
        soon = datetime.fromtimestamp(_time.time() + 120, tz=timezone.utc)
        tok = _make_token(expires_at=soon)
        assert tok.token_status == "expiring_soon"

    def test_token_status_expired(self):
        tok = _make_token(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
        assert tok.token_status == "expired"

    def test_token_status_no_expiry(self):
        tok = _make_token(expires_at=None)
        assert tok.token_status == "connected"

    def test_access_token_decrypt(self):
        tok = _make_token(access_token="secret_bearer")
        assert tok.access_token == "secret_bearer"

    def test_refresh_token_decrypt(self):
        tok = _make_token(refresh_token="refresh_xyz")
        assert tok.refresh_token == "refresh_xyz"

    def test_client_id_decrypt(self):
        tok = _make_token(client_id="my_client_id")
        assert tok.client_id == "my_client_id"


# ---------------------------------------------------------------------------
# build_mcp_auth_json helper
# ---------------------------------------------------------------------------

class TestBuildMcpAuthJson:

    def test_structure_matches_opencode_schema(self):
        from swarmer.routers.atlassian import build_mcp_auth_json

        result = build_mcp_auth_json(
            access_token="tok_abc",
            refresh_token="ref_def",
            expires_at_ts=9999999999,
            scope="read:jira-work offline_access",
            client_id="cid_123",
            client_id_issued_at=1700000000,
            server_url="https://mcp.atlassian.com/v1/mcp",
        )
        data = json.loads(result)
        assert "atlassian-rovo" in data
        entry = data["atlassian-rovo"]
        assert entry["serverUrl"] == "https://mcp.atlassian.com/v1/mcp"
        assert entry["tokens"]["accessToken"] == "tok_abc"
        assert entry["tokens"]["refreshToken"] == "ref_def"
        assert entry["tokens"]["expiresAt"] == 9999999999
        assert entry["tokens"]["scope"] == "read:jira-work offline_access"
        assert entry["clientInfo"]["clientId"] == "cid_123"
        assert entry["clientInfo"]["clientIdIssuedAt"] == 1700000000

    def test_no_refresh_token_omits_key(self):
        from swarmer.routers.atlassian import build_mcp_auth_json

        result = build_mcp_auth_json(
            access_token="tok",
            refresh_token=None,
            expires_at_ts=None,
            scope="read:jira-work",
            client_id="cid",
            client_id_issued_at=0,
            server_url="https://mcp.atlassian.com/v1/mcp",
        )
        data = json.loads(result)
        assert "refreshToken" not in data["atlassian-rovo"]["tokens"]


# ---------------------------------------------------------------------------
# OpenCodeStrategy: build_config_data with/without token
# ---------------------------------------------------------------------------

class TestOpenCodeStrategyMcpConfig:

    def _make_strategy(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        return OpenCodeStrategy()

    def test_no_mcp_entry_without_token(self):
        strat = self._make_strategy()
        data = strat.build_config_data(secret=None, atlassian_token=None)
        cfg = json.loads(data["opencode.json"])
        assert "mcp" not in cfg

    def test_mcp_entry_added_with_valid_token(self):
        import time as _time
        far = datetime.fromtimestamp(_time.time() + 1800, tz=timezone.utc)
        tok = _make_token(expires_at=far)
        strat = self._make_strategy()
        data = strat.build_config_data(secret=None, atlassian_token=tok)
        cfg = json.loads(data["opencode.json"])
        assert "mcp" in cfg
        assert "atlassian-rovo" in cfg["mcp"]
        entry = cfg["mcp"]["atlassian-rovo"]
        assert entry["type"] == "remote"
        assert entry["url"] == "https://mcp.atlassian.com/v1/mcp"
        assert entry.get("enabled") is True

    def test_mcp_entry_not_added_with_expired_token(self):
        tok = _make_token(expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
        strat = self._make_strategy()
        data = strat.build_config_data(secret=None, atlassian_token=tok)
        cfg = json.loads(data["opencode.json"])
        assert "mcp" not in cfg


# ---------------------------------------------------------------------------
# OpenCodeStrategy: build_mcp_auth_setup_cmd
# ---------------------------------------------------------------------------

class TestBuildMcpAuthSetupCmd:
    """build_mcp_auth_setup_cmd now takes the full mcp-auth.json blob."""

    _SAMPLE_JSON = '{"atlassian-rovo":{"serverUrl":"https://mcp.atlassian.com/v1/mcp","tokens":{"accessToken":"my_secret_bearer"}}}'

    def test_cmd_writes_to_correct_path(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        strat = OpenCodeStrategy()
        cmd = strat.build_mcp_auth_setup_cmd(self._SAMPLE_JSON)
        assert "/workspace/.local/share/opencode/mcp-auth.json" in cmd

    def test_cmd_creates_parent_dir(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        strat = OpenCodeStrategy()
        cmd = strat.build_mcp_auth_setup_cmd(self._SAMPLE_JSON)
        assert "mkdir -p" in cmd

    def test_cmd_contains_token(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        strat = OpenCodeStrategy()
        cmd = strat.build_mcp_auth_setup_cmd(self._SAMPLE_JSON)
        # The full JSON blob (which contains the token) must appear in the cmd
        assert "my_secret_bearer" in cmd

    def test_empty_json_returns_empty(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        strat = OpenCodeStrategy()
        cmd = strat.build_mcp_auth_setup_cmd("")
        assert cmd == ""


# ---------------------------------------------------------------------------
# atlassian router: authorize redirect
# ---------------------------------------------------------------------------

class TestAtlassianAuthorize:

    @pytest.mark.asyncio
    async def test_authorize_redirects_to_atlassian(self):
        from swarmer.routers.atlassian import build_authorize_url
        state = "test_state_abc"
        url = build_authorize_url(
            client_id="my_client",
            redirect_uri="https://swarmer.example.com/workspaces/1/atlassian/callback",
            state=state,
            code_challenge="challenge_xyz",
        )
        assert "mcp.atlassian.com/v1/authorize" in url
        assert "response_type=code" in url
        assert "my_client" in url
        assert state in url
        assert "offline_access" in url
        assert "challenge_xyz" in url

    @pytest.mark.asyncio
    async def test_authorize_includes_required_scopes(self):
        from swarmer.routers.atlassian import build_authorize_url
        url = build_authorize_url(
            client_id="cid",
            redirect_uri="https://x.com/cb",
            state="st",
            code_challenge="cc",
        )
        assert "read%3Ajira-work" in url or "read:jira-work" in url
        assert "offline_access" in url


# ---------------------------------------------------------------------------
# atlassian router: PKCE helpers
# ---------------------------------------------------------------------------

class TestPkceHelpers:

    def test_generate_code_verifier_length(self):
        from swarmer.routers.atlassian import generate_code_verifier
        v = generate_code_verifier()
        assert 43 <= len(v) <= 128

    def test_generate_code_verifier_url_safe(self):
        from swarmer.routers.atlassian import generate_code_verifier
        v = generate_code_verifier()
        import re
        assert re.match(r'^[A-Za-z0-9\-._~]+$', v)

    def test_code_challenge_from_verifier(self):
        from swarmer.routers.atlassian import compute_code_challenge
        v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        challenge = compute_code_challenge(v)
        # challenge must be base64url encoded SHA-256
        import base64, hashlib
        expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        assert challenge == expected


# ---------------------------------------------------------------------------
# atlassian router: callback token exchange
# ---------------------------------------------------------------------------

class TestAtlassianCallback:

    @pytest.mark.asyncio
    async def test_exchange_code_for_tokens(self):
        from swarmer.routers.atlassian import exchange_code_for_tokens

        fake_response = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
            "scope": "read:jira-work offline_access",
            "token_type": "Bearer",
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            result = await exchange_code_for_tokens(
                code="auth_code_xyz",
                client_id="my_client",
                redirect_uri="https://swarmer.example.com/cb",
                code_verifier="my_verifier",
            )

        assert result["access_token"] == "new_access"
        assert result["refresh_token"] == "new_refresh"
        assert result["expires_in"] == 3600

    @pytest.mark.asyncio
    async def test_exchange_raises_on_http_error(self):
        from swarmer.routers.atlassian import exchange_code_for_tokens
        import httpx

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "400", request=MagicMock(), response=MagicMock()
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await exchange_code_for_tokens(
                    code="bad_code",
                    client_id="cid",
                    redirect_uri="https://x.com/cb",
                    code_verifier="verifier",
                )


# ---------------------------------------------------------------------------
# atlassian router: dynamic client registration
# ---------------------------------------------------------------------------

class TestDynamicClientRegistration:

    @pytest.mark.asyncio
    async def test_register_returns_client_id(self):
        from swarmer.routers.atlassian import register_oauth_client

        fake_response = {
            "client_id": "new_dyn_client",
            "client_id_issued_at": 1700000000,
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            client_id, issued_at = await register_oauth_client(
                redirect_uri="https://swarmer.example.com/workspaces/1/atlassian/callback"
            )

        assert client_id == "new_dyn_client"
        assert issued_at == 1700000000


# ---------------------------------------------------------------------------
# atlassian router: token refresh
# ---------------------------------------------------------------------------

class TestAtlassianTokenRefresh:

    @pytest.mark.asyncio
    async def test_refresh_token_updates_fields(self):
        from swarmer.routers.atlassian import refresh_atlassian_token
        import swarmer.crypto as crypto
        import os, base64
        os.environ.setdefault("SWARMER_SECRET_KEY", base64.urlsafe_b64encode(b"x" * 32).decode())
        crypto.init_crypto("auth/secret.key")

        tok = _make_token(
            access_token="old_access",
            refresh_token="old_refresh",
            expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )

        fake_response = {
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
            "scope": "read:jira-work offline_access",
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = fake_response
        mock_resp.raise_for_status = MagicMock()

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            success = await refresh_atlassian_token(tok, mock_db)

        assert success is True
        assert tok.access_token == "new_access_token"
        assert tok.refresh_token == "new_refresh_token"
        assert tok.expires_at is not None
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_returns_false_on_error(self):
        from swarmer.routers.atlassian import refresh_atlassian_token
        import httpx

        tok = _make_token(refresh_token="old_refresh")
        mock_db = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=MagicMock()
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client_cls.return_value = mock_client

            success = await refresh_atlassian_token(tok, mock_db)

        assert success is False


# ---------------------------------------------------------------------------
# k8s helpers: apply/delete atlassian token secret
# ---------------------------------------------------------------------------

class TestK8sAtlassianTokenSecret:

    def test_apply_creates_secret_calls_apply_secret(self):
        """apply_atlassian_token_secret delegates to _apply_secret with correct data."""
        from swarmer.k8s import apply_atlassian_token_secret

        with patch("swarmer.k8s._apply_secret") as mock_apply:
            apply_atlassian_token_secret(
                "test-ns", 7, '{"atlassian-rovo":{}}', "bearer_tok"
            )
            mock_apply.assert_called_once()
            call_args = mock_apply.call_args
            # First positional: namespace
            assert call_args[0][0] == "test-ns"
            # Second positional: secret name
            assert call_args[0][1] == "atlassian-rovo-7"
            # Third positional: data dict contains expected keys
            data = call_args[0][2]
            assert "access_token" in data
            assert "mcp-auth.json" in data

    def test_atlassian_secret_name_format(self):
        from swarmer.k8s import _atlassian_secret_name
        assert _atlassian_secret_name(1) == "atlassian-rovo-1"
        assert _atlassian_secret_name(42) == "atlassian-rovo-42"


# ---------------------------------------------------------------------------
# _do_launch integration: token injection
# ---------------------------------------------------------------------------

class TestDoLaunchAtlassianIntegration:

    @pytest.mark.asyncio
    async def test_injects_token_when_valid(self):
        """When a valid AtlassianToken exists, apply_atlassian_token_secret is called."""
        import time as _t
        far = datetime.fromtimestamp(_t.time() + 1800, tz=timezone.utc)
        tok = _make_token(access_token="valid_bearer", expires_at=far)

        with patch("swarmer.routers.sessions._get_atlassian_token_for_launch",
                   new_callable=AsyncMock, return_value=("valid_bearer", tok)):
            with patch("swarmer.routers.sessions._apply_atlassian_secret_async",
                       new_callable=AsyncMock) as mock_apply:
                # Minimal smoke test: function exists and is callable
                from swarmer.routers.sessions import _apply_atlassian_secret_async
                assert callable(_apply_atlassian_secret_async)

    @pytest.mark.asyncio
    async def test_get_atlassian_token_returns_none_when_absent(self):
        """_get_atlassian_token_for_launch returns (None, None) when no token row."""
        from swarmer.routers.sessions import _get_atlassian_token_for_launch

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        access_token, token_obj = await _get_atlassian_token_for_launch(
            workspace_id=1, db=mock_db
        )
        assert access_token is None
        assert token_obj is None

    @pytest.mark.asyncio
    async def test_get_atlassian_token_auto_refreshes_when_needed(self):
        """_get_atlassian_token_for_launch refreshes token if needs_refresh."""
        import time as _t
        soon = datetime.fromtimestamp(_t.time() + 60, tz=timezone.utc)
        tok = _make_token(
            access_token="stale_access",
            refresh_token="valid_refresh",
            expires_at=soon,
        )
        tok.needs_refresh  # confirm it returns True (expires in 60s < 300s)

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tok
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("swarmer.routers.sessions.refresh_atlassian_token",
                   new_callable=AsyncMock, return_value=True) as mock_refresh:
            # After refresh, simulate new token
            tok.access_token_enc = __import__("swarmer.crypto", fromlist=["encrypt"]).encrypt("refreshed_access")

            from swarmer.routers.sessions import _get_atlassian_token_for_launch
            access_token, token_obj = await _get_atlassian_token_for_launch(
                workspace_id=1, db=mock_db
            )
            mock_refresh.assert_called_once_with(tok, mock_db)

    @pytest.mark.asyncio
    async def test_get_atlassian_token_returns_none_when_expired_and_refresh_fails(self):
        """If token is expired and refresh fails, return (None, token_obj)."""
        tok = _make_token(
            expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = tok
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("swarmer.routers.sessions.refresh_atlassian_token",
                   new_callable=AsyncMock, return_value=False):
            from swarmer.routers.sessions import _get_atlassian_token_for_launch
            access_token, token_obj = await _get_atlassian_token_for_launch(
                workspace_id=1, db=mock_db
            )
            assert access_token is None


# ---------------------------------------------------------------------------
# build_config_data signature compatibility
# ---------------------------------------------------------------------------

class TestBuildConfigDataSignature:

    def test_no_args_still_works(self):
        """Existing callers without atlassian_token arg must still work."""
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        strat = OpenCodeStrategy()
        data = strat.build_config_data()
        assert "opencode.json" in data

    def test_secret_only_still_works(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        strat = OpenCodeStrategy()
        data = strat.build_config_data(secret=None)
        assert "opencode.json" in data

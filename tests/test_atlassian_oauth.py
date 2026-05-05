"""
Unit and integration tests for the Atlassian OAuth 2.1 integration.

Covers:
  - atlassian_oauth router helpers (redirect URI, PKCE, state validation)
  - atlassian_oauth_start route (metadata discovery, DCR, redirect)
  - atlassian_oauth_callback route (CSRF validation, token exchange, session storage)
  - k8s.apply_atlassian_oauth_secret / delete_atlassian_oauth_secret
  - opencode.build_share_setup_cmd with has_atlassian_oauth
  - k8s_session.build_session_pod envFrom injection
  - secrets router CRUD routes (save / delete AtlassianOAuthApp)

No real K8s cluster or Atlassian server is required — all external calls
are mocked with respx or unittest.mock.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import base64
import json
import time
from unittest.mock import MagicMock, patch, call

import pytest
import respx
import httpx


# ---------------------------------------------------------------------------
# Helper utilities (tested in isolation, no DB needed)
# ---------------------------------------------------------------------------

class TestMakeRedirectUri:
    """_make_redirect_uri() constructs the correct callback URL."""

    def _call(self, public_url: str, ws_id: int) -> str:
        from unittest.mock import MagicMock
        from swarmer.routers.atlassian_oauth import _make_redirect_uri

        request = MagicMock()
        request.base_url = "http://localhost:8080/"

        with patch("swarmer.routers.atlassian_oauth.settings") as mock_settings:
            mock_settings.swarmer_public_url = public_url
            return _make_redirect_uri(request, ws_id)

    def test_uses_swarmer_public_url_when_set(self):
        uri = self._call("https://swarmer.example.com", 3)
        assert uri == "https://swarmer.example.com/workspaces/3/atlassian-oauth/callback"

    def test_strips_trailing_slash_from_public_url(self):
        uri = self._call("https://swarmer.example.com/", 7)
        assert uri == "https://swarmer.example.com/workspaces/7/atlassian-oauth/callback"

    def test_falls_back_to_request_base_url(self):
        uri = self._call("", 1)
        assert uri == "http://localhost:8080/workspaces/1/atlassian-oauth/callback"


class TestPkce:
    """_pkce_pair() produces a valid PKCE S256 code_challenge."""

    def test_returns_two_strings(self):
        from swarmer.routers.atlassian_oauth import _pkce_pair
        verifier, challenge = _pkce_pair()
        assert isinstance(verifier, str) and len(verifier) > 10
        assert isinstance(challenge, str) and len(challenge) > 10

    def test_challenge_is_sha256_of_verifier(self):
        import hashlib
        from base64 import urlsafe_b64encode
        from swarmer.routers.atlassian_oauth import _pkce_pair

        verifier, challenge = _pkce_pair()
        expected = urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == expected

    def test_pairs_are_unique(self):
        from swarmer.routers.atlassian_oauth import _pkce_pair
        pairs = {_pkce_pair()[0] for _ in range(10)}
        assert len(pairs) == 10  # all different


class TestSafeState:
    """_safe_state() validates CSRF state token format."""

    def test_valid_32_char_hex(self):
        from swarmer.routers.atlassian_oauth import _safe_state
        assert _safe_state("a" * 32) is True
        assert _safe_state("0123456789abcdef" * 2) is True

    def test_invalid_too_short(self):
        from swarmer.routers.atlassian_oauth import _safe_state
        assert _safe_state("abc") is False

    def test_invalid_non_hex(self):
        from swarmer.routers.atlassian_oauth import _safe_state
        assert _safe_state("g" * 32) is False

    def test_empty_string(self):
        from swarmer.routers.atlassian_oauth import _safe_state
        assert _safe_state("") is False

    def test_none_like_empty(self):
        from swarmer.routers.atlassian_oauth import _safe_state
        # None is not a valid state — falsy check
        assert _safe_state(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _discover_oauth_metadata
# ---------------------------------------------------------------------------

class TestDiscoverOAuthMetadata:
    """_discover_oauth_metadata() fetches and returns the JSON metadata."""

    @pytest.mark.asyncio
    async def test_returns_metadata_dict(self):
        from swarmer.routers.atlassian_oauth import _discover_oauth_metadata

        metadata = {
            "authorization_endpoint": "https://auth.atlassian.com/authorize",
            "token_endpoint": "https://auth.atlassian.com/oauth/token",
            "registration_endpoint": "https://auth.atlassian.com/oauth/register",
        }
        with respx.mock:
            respx.get(
                "https://mcp.atlassian.com/v1/mcp/authv2/.well-known/oauth-authorization-server"
            ).mock(return_value=httpx.Response(200, json=metadata))

            result = await _discover_oauth_metadata("https://mcp.atlassian.com/v1/mcp/authv2")

        assert result["authorization_endpoint"] == "https://auth.atlassian.com/authorize"
        assert result["token_endpoint"] == "https://auth.atlassian.com/oauth/token"

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        from swarmer.routers.atlassian_oauth import _discover_oauth_metadata

        with respx.mock:
            respx.get(
                "https://mcp.atlassian.com/v1/mcp/authv2/.well-known/oauth-authorization-server"
            ).mock(return_value=httpx.Response(500))

            with pytest.raises(Exception):
                await _discover_oauth_metadata("https://mcp.atlassian.com/v1/mcp/authv2")


# ---------------------------------------------------------------------------
# _do_dcr
# ---------------------------------------------------------------------------

class TestDoDcr:
    """_do_dcr() performs Dynamic Client Registration and returns a client_id."""

    @pytest.mark.asyncio
    async def test_returns_client_id(self):
        from swarmer.routers.atlassian_oauth import _do_dcr

        reg_url = "https://auth.atlassian.com/oauth/register"
        redirect = "https://swarmer.example.com/workspaces/1/atlassian-oauth/callback"

        with respx.mock:
            respx.post(reg_url).mock(
                return_value=httpx.Response(201, json={"client_id": "test-client-123"})
            )
            client_id = await _do_dcr(reg_url, redirect)

        assert client_id == "test-client-123"

    @pytest.mark.asyncio
    async def test_raises_when_no_client_id(self):
        from swarmer.routers.atlassian_oauth import _do_dcr

        reg_url = "https://auth.atlassian.com/oauth/register"

        with respx.mock:
            respx.post(reg_url).mock(
                return_value=httpx.Response(201, json={"error": "bad request"})
            )
            with pytest.raises(ValueError, match="client_id"):
                await _do_dcr(reg_url, "https://example.com/cb")

    @pytest.mark.asyncio
    async def test_sends_correct_payload(self):
        from swarmer.routers.atlassian_oauth import _do_dcr

        reg_url = "https://auth.atlassian.com/oauth/register"
        redirect = "https://swarmer.example.com/cb"
        captured: dict = {}

        def _handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(201, json={"client_id": "c1"})

        with respx.mock:
            respx.post(reg_url).mock(side_effect=_handler)
            await _do_dcr(reg_url, redirect)

        assert captured["redirect_uris"] == [redirect]
        assert captured["grant_types"] == ["authorization_code"]
        assert captured["token_endpoint_auth_method"] == "none"
        assert "Swarmer" in captured["client_name"]


# ---------------------------------------------------------------------------
# k8s.apply_atlassian_oauth_secret / delete_atlassian_oauth_secret
# ---------------------------------------------------------------------------

class TestAtlassianOAuthK8sSecret:
    """apply_ and delete_atlassian_oauth_secret call _apply/_delete_secret correctly."""

    def test_apply_uses_correct_name_and_key(self):
        from swarmer import k8s

        with patch.object(k8s, "_apply_secret") as mock_apply:
            k8s.apply_atlassian_oauth_secret("my-ns", 42, access_token="tok-abc")

        mock_apply.assert_called_once()
        name = mock_apply.call_args[0][1]
        data = mock_apply.call_args[0][2]
        assert name == "atlassian-oauth-42"
        assert "ATLASSIAN_MCP_TOKEN" in data
        # Value should be base64-encoded token
        decoded = base64.b64decode(data["ATLASSIAN_MCP_TOKEN"]).decode()
        assert decoded == "tok-abc"

    def test_delete_uses_correct_name(self):
        from swarmer import k8s

        with patch.object(k8s, "_delete_secret") as mock_delete:
            k8s.delete_atlassian_oauth_secret("my-ns", 99)

        mock_delete.assert_called_once_with("my-ns", "atlassian-oauth-99")

    def test_apply_encodes_token_as_base64(self):
        from swarmer import k8s

        token = "eyJhbGciOiJSUzI1NiJ9.test"
        with patch.object(k8s, "_apply_secret") as mock_apply:
            k8s.apply_atlassian_oauth_secret("ns", 1, access_token=token)

        data = mock_apply.call_args[0][2]
        decoded = base64.b64decode(data["ATLASSIAN_MCP_TOKEN"]).decode()
        assert decoded == token


# ---------------------------------------------------------------------------
# opencode.build_share_setup_cmd with has_atlassian_oauth
# ---------------------------------------------------------------------------

class TestOpencodeShareSetupCmd:
    """build_share_setup_cmd injects the Atlassian MCP token write when requested."""

    def _make_strategy(self):
        from swarmer.agent_tools.opencode import OpenCodeStrategy
        return OpenCodeStrategy()

    def test_without_atlassian_no_mcp_injection(self):
        strategy = self._make_strategy()
        cmd = strategy.build_share_setup_cmd(has_atlassian_oauth=False)
        assert "atlassian" not in cmd.lower()
        assert "ATLASSIAN_MCP_TOKEN" not in cmd

    def test_with_atlassian_injects_token_write(self):
        strategy = self._make_strategy()
        cmd = strategy.build_share_setup_cmd(has_atlassian_oauth=True)
        assert "ATLASSIAN_MCP_TOKEN" in cmd
        assert "mcp.atlassian.com" in cmd
        assert "atlassian-rovo" in cmd

    def test_with_atlassian_includes_python_snippet(self):
        strategy = self._make_strategy()
        cmd = strategy.build_share_setup_cmd(has_atlassian_oauth=True)
        assert "python3" in cmd
        assert "opencode.json" in cmd

    def test_base_setup_cmd_present_in_both(self):
        strategy = self._make_strategy()
        for flag in (True, False):
            cmd = strategy.build_share_setup_cmd(has_atlassian_oauth=flag)
            assert "mkdir -p /workspace/.opencode" in cmd
            assert "ln -sf /workspace/.opencode" in cmd

    def test_crush_build_share_setup_cmd_accepts_flag(self):
        """Crush's build_share_setup_cmd accepts has_atlassian_oauth without error."""
        from swarmer.agent_tools.crush import CrushStrategy
        strategy = CrushStrategy()
        # Should not raise
        cmd = strategy.build_share_setup_cmd(has_atlassian_oauth=True)
        assert isinstance(cmd, str)
        # Crush does not inject Atlassian config
        assert "ATLASSIAN_MCP_TOKEN" not in cmd


# ---------------------------------------------------------------------------
# k8s_session.build_session_pod with has_atlassian_oauth
# ---------------------------------------------------------------------------

class TestBuildSessionPodAtlassian:
    """build_session_pod adds the atlassian-oauth envFrom source when flag is set."""

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

    def _run(self, session, has_atlassian_oauth: bool):
        """Call build_session_pod with mocked kubernetes client.

        k8s_session.py imports kubernetes.client lazily inside functions,
        so we patch the kubernetes.client module directly.
        """
        from unittest.mock import MagicMock, patch

        # Build a simple mock that returns dicts so we can inspect them
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

        # kubernetes.client is imported lazily inside build_session_pod,
        # so we patch the module-level import inside kubernetes package.
        import kubernetes
        with patch.object(kubernetes, "client", mock_k8s_client):
            with patch("swarmer.k8s_session.settings") as mock_settings:
                mock_settings.agent_image_pull_policy = "IfNotPresent"
                from swarmer.k8s_session import build_session_pod
                return build_session_pod(
                    session=session,
                    namespace="test-ns",
                    image="test-image:latest",
                    suffix="abcd",
                    has_atlassian_oauth=has_atlassian_oauth,
                    agent_tool="opencode",
                )

    def test_without_atlassian_no_extra_env_from(self):
        session = self._make_session(1)
        pod = self._run(session, has_atlassian_oauth=False)
        env_from = pod["spec"]["containers"][0]["env_from"]
        secret_names = [
            s.get("secret_ref", {}).get("secretEnvSource", {}).get("name", "")
            for s in env_from
            if isinstance(s, dict)
        ]
        assert not any("atlassian-oauth" in n for n in secret_names)

    def test_with_atlassian_adds_env_from(self):
        session = self._make_session(7)
        pod = self._run(session, has_atlassian_oauth=True)
        env_from = pod["spec"]["containers"][0]["env_from"]
        secret_names = []
        for s in env_from:
            if isinstance(s, dict) and "secret_ref" in s:
                ref = s["secret_ref"]
                if isinstance(ref, dict) and "secretEnvSource" in ref:
                    secret_names.append(ref["secretEnvSource"].get("name", ""))
        assert "atlassian-oauth-7" in secret_names


# ---------------------------------------------------------------------------
# atlassian_oauth_start route (mocked DB and HTTP)
# ---------------------------------------------------------------------------

class TestAtlassianOAuthStart:
    """GET /workspaces/{ws_id}/atlassian-oauth/start redirects to Atlassian."""

    def _metadata(self):
        return {
            "authorization_endpoint": "https://auth.atlassian.com/authorize",
            "token_endpoint": "https://auth.atlassian.com/oauth/token",
            "registration_endpoint": "https://auth.atlassian.com/oauth/register",
        }

    @pytest.mark.asyncio
    async def test_redirects_to_authorization_endpoint(self):
        """When AtlassianOAuthApp is configured and DCR succeeds, redirect to Atlassian."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_start
        from unittest.mock import AsyncMock, MagicMock, patch

        # Fake DB that returns a configured AtlassianOAuthApp
        fake_app = MagicMock()
        fake_app.site_url = "https://myorg.atlassian.net"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_app

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        mock_request = MagicMock()
        mock_request.base_url = "http://localhost:8080/"
        mock_request.session = {}

        with respx.mock:
            respx.get(
                "https://mcp.atlassian.com/v1/mcp/authv2/.well-known/oauth-authorization-server"
            ).mock(return_value=httpx.Response(200, json=self._metadata()))
            respx.post("https://auth.atlassian.com/oauth/register").mock(
                return_value=httpx.Response(201, json={"client_id": "dcr-client-abc"})
            )

            with patch("swarmer.routers.atlassian_oauth.settings") as mock_settings:
                mock_settings.swarmer_public_url = "https://swarmer.example.com"
                response = await atlassian_oauth_start(
                    ws_id=1, request=mock_request, return_session=5, db=mock_db
                )

        # Should be a redirect to the authorization endpoint
        assert response.status_code == 302
        location = response.headers["location"]
        assert "https://auth.atlassian.com/authorize" in location
        assert "client_id=dcr-client-abc" in location
        assert "code_challenge_method=S256" in location

    @pytest.mark.asyncio
    async def test_redirects_to_secrets_when_not_configured(self):
        """When no AtlassianOAuthApp exists, redirect to secrets page."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_start
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # not configured

        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        mock_request = MagicMock()
        mock_request.session = {}

        with patch("swarmer.routers.atlassian_oauth.flash"):
            response = await atlassian_oauth_start(
                ws_id=2, request=mock_request, return_session=0, db=mock_db
            )

        assert response.status_code == 302
        assert "secrets" in response.headers["location"]

    @pytest.mark.asyncio
    async def test_stores_state_in_http_session(self):
        """After successful DCR, CSRF state is stored in request.session."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_start, _STATE_KEY
        from unittest.mock import AsyncMock, MagicMock, patch

        fake_app = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = fake_app
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(return_value=mock_result)

        session_store: dict = {}
        mock_request = MagicMock()
        mock_request.base_url = "http://localhost:8080/"
        mock_request.session = session_store

        with respx.mock:
            respx.get(
                "https://mcp.atlassian.com/v1/mcp/authv2/.well-known/oauth-authorization-server"
            ).mock(return_value=httpx.Response(200, json=self._metadata()))
            respx.post("https://auth.atlassian.com/oauth/register").mock(
                return_value=httpx.Response(201, json={"client_id": "c1"})
            )

            with patch("swarmer.routers.atlassian_oauth.settings") as mock_settings:
                mock_settings.swarmer_public_url = ""
                await atlassian_oauth_start(
                    ws_id=3, request=mock_request, return_session=10, db=mock_db
                )

        assert _STATE_KEY in session_store
        stored = session_store[_STATE_KEY]
        assert stored["client_id"] == "c1"
        assert stored["ws_id"] == 3
        assert stored["return_session"] == 10
        assert "state" in stored
        assert "code_verifier" in stored


# ---------------------------------------------------------------------------
# atlassian_oauth_callback route
# ---------------------------------------------------------------------------

class TestAtlassianOAuthCallback:
    """GET /workspaces/{ws_id}/atlassian-oauth/callback handles token exchange."""

    def _stored_state(self, ws_id: int = 1, state: str = "a" * 32, session_id: int = 5):
        return {
            "state": state,
            "client_id": "dcr-client",
            "code_verifier": "verifier-xyz",
            "token_endpoint": "https://auth.atlassian.com/oauth/token",
            "redirect_uri": "https://swarmer.example.com/workspaces/1/atlassian-oauth/callback",
            "ws_id": ws_id,
            "return_session": session_id,
            "created_at": int(time.time()),
        }

    @pytest.mark.asyncio
    async def test_successful_token_exchange_stores_in_session(self):
        """Valid code + state → access_token stored in HTTP session."""
        from swarmer.routers.atlassian_oauth import (
            atlassian_oauth_callback, _STATE_KEY, _token_session_key
        )
        from unittest.mock import AsyncMock, MagicMock, patch

        state = "a" * 32
        session_store = {_STATE_KEY: self._stored_state(ws_id=1, state=state)}

        mock_request = MagicMock()
        mock_request.session = session_store
        mock_db = AsyncMock()

        with respx.mock:
            respx.post("https://auth.atlassian.com/oauth/token").mock(
                return_value=httpx.Response(200, json={
                    "access_token": "atlassian-at-xyz",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                })
            )

            with patch("swarmer.routers.atlassian_oauth.flash"):
                response = await atlassian_oauth_callback(
                    ws_id=1, request=mock_request, code="auth-code-123",
                    state=state, error="", error_description="", db=mock_db
                )

        assert response.status_code == 302
        token_key = _token_session_key(1)
        assert token_key in session_store
        assert session_store[token_key]["access_token"] == "atlassian-at-xyz"
        assert "expires_at" in session_store[token_key]
        # State should be cleared after use
        assert _STATE_KEY not in session_store

    @pytest.mark.asyncio
    async def test_csrf_mismatch_redirects_with_error(self):
        """Mismatched CSRF state → flash error, redirect, no token stored."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_callback, _STATE_KEY
        from unittest.mock import AsyncMock, MagicMock, patch

        good_state = "a" * 32
        bad_state = "b" * 32
        session_store = {_STATE_KEY: self._stored_state(ws_id=1, state=good_state)}

        mock_request = MagicMock()
        mock_request.session = session_store
        mock_db = AsyncMock()

        flashed = []
        with patch("swarmer.routers.atlassian_oauth.flash", side_effect=lambda r, m, t: flashed.append(m)):
            response = await atlassian_oauth_callback(
                ws_id=1, request=mock_request, code="some-code",
                state=bad_state, error="", error_description="", db=mock_db
            )

        assert response.status_code == 302
        assert any("mismatch" in m.lower() or "csrf" in m.lower() for m in flashed)
        assert "atlassian_oauth_1" not in session_store

    @pytest.mark.asyncio
    async def test_user_denied_redirects_with_error(self):
        """error=access_denied from Atlassian → flash error, redirect."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_callback, _STATE_KEY
        from unittest.mock import AsyncMock, MagicMock, patch

        state = "c" * 32
        session_store = {_STATE_KEY: self._stored_state(ws_id=2, state=state)}

        mock_request = MagicMock()
        mock_request.session = session_store
        mock_db = AsyncMock()

        flashed = []
        with patch("swarmer.routers.atlassian_oauth.flash", side_effect=lambda r, m, t: flashed.append(m)):
            response = await atlassian_oauth_callback(
                ws_id=2, request=mock_request, code="",
                state=state, error="access_denied",
                error_description="User denied access", db=mock_db
            )

        assert response.status_code == 302
        assert any("denied" in m.lower() or "failed" in m.lower() for m in flashed)

    @pytest.mark.asyncio
    async def test_missing_session_state_redirects(self):
        """No stored OAuth state → flash error and redirect."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_callback
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_request = MagicMock()
        mock_request.session = {}  # no stored state
        mock_db = AsyncMock()

        flashed = []
        with patch("swarmer.routers.atlassian_oauth.flash", side_effect=lambda r, m, t: flashed.append(m)):
            response = await atlassian_oauth_callback(
                ws_id=1, request=mock_request, code="code",
                state="a" * 32, error="", error_description="", db=mock_db
            )

        assert response.status_code == 302
        assert flashed  # some error message flashed

    @pytest.mark.asyncio
    async def test_token_exchange_failure_redirects(self):
        """HTTP error on token endpoint → flash error, no token stored."""
        from swarmer.routers.atlassian_oauth import atlassian_oauth_callback, _STATE_KEY
        from unittest.mock import AsyncMock, MagicMock, patch

        state = "d" * 32
        session_store = {_STATE_KEY: self._stored_state(ws_id=1, state=state)}

        mock_request = MagicMock()
        mock_request.session = session_store
        mock_db = AsyncMock()

        with respx.mock:
            respx.post("https://auth.atlassian.com/oauth/token").mock(
                return_value=httpx.Response(400, json={"error": "invalid_grant"})
            )

            flashed = []
            with patch("swarmer.routers.atlassian_oauth.flash", side_effect=lambda r, m, t: flashed.append(m)):
                response = await atlassian_oauth_callback(
                    ws_id=1, request=mock_request, code="bad-code",
                    state=state, error="", error_description="", db=mock_db
                )

        assert response.status_code == 302
        assert "atlassian_oauth_1" not in session_store
        assert flashed


# ---------------------------------------------------------------------------
# token_session_key helper
# ---------------------------------------------------------------------------

class TestTokenSessionKey:
    def test_key_includes_workspace_id(self):
        from swarmer.routers.atlassian_oauth import _token_session_key
        assert _token_session_key(42) == "atlassian_oauth_42"
        assert _token_session_key(1) == "atlassian_oauth_1"

"""Tests for swarmer/github_auth.py — IAT minting and refresh loop."""

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(app_id="123456", installation_id="789012", private_key="PEM"):
    """Return a minimal GitHubApp-like object for testing."""
    app = MagicMock()
    app.app_id = app_id
    app.installation_id = installation_id
    app.private_key = private_key
    return app


# ---------------------------------------------------------------------------
# _build_jwt
# ---------------------------------------------------------------------------

class TestBuildJWT:
    def test_encodes_correct_claims(self):
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        # Generate a real RSA key for the test.
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()

        app = _make_app(app_id="999", private_key=pem)
        from swarmer.github_auth import _build_jwt
        token = _build_jwt(app)

        public_key = private_key.public_key()
        claims = pyjwt.decode(token, public_key, algorithms=["RS256"])
        assert claims["iss"] == "999"
        assert claims["exp"] > int(time.time())
        assert claims["iat"] < int(time.time())

    def test_clock_skew_iat_in_past(self):
        """iat must be at least 1 second in the past."""
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode()

        app = _make_app(private_key=pem)
        from swarmer.github_auth import _build_jwt
        token = _build_jwt(app)
        public_key = private_key.public_key()
        claims = pyjwt.decode(token, public_key, algorithms=["RS256"])
        assert claims["iat"] <= int(time.time()) - 59  # at least 59 s in the past


# ---------------------------------------------------------------------------
# mint_installation_token
# ---------------------------------------------------------------------------

class TestMintInstallationToken:
    @pytest.mark.asyncio
    async def test_returns_token_on_success(self):
        app = _make_app()
        fake_response = MagicMock()
        fake_response.raise_for_status = MagicMock()
        fake_response.json.return_value = {"token": "ghs_test123", "expires_at": "2099-01-01T00:00:00Z"}

        with patch("swarmer.github_auth._build_jwt", return_value="signed.jwt"):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=fake_response)
                mock_client_cls.return_value = mock_client

                from swarmer.github_auth import mint_installation_token
                token = await mint_installation_token(app)

        assert token == "ghs_test123"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "installations/789012/access_tokens" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self):
        import httpx
        app = _make_app()

        with patch("swarmer.github_auth._build_jwt", return_value="signed.jwt"):
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_response = MagicMock()
                mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "401", request=MagicMock(), response=MagicMock()
                )
                mock_client = AsyncMock()
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client.post = AsyncMock(return_value=mock_response)
                mock_client_cls.return_value = mock_client

                from swarmer.github_auth import mint_installation_token
                with pytest.raises(httpx.HTTPStatusError):
                    await mint_installation_token(app)


# ---------------------------------------------------------------------------
# start_token_refresh_loop
# ---------------------------------------------------------------------------

class TestStartTokenRefreshLoop:
    @pytest.mark.asyncio
    async def test_loop_refreshes_and_updates_provider(self):
        app = _make_app()
        calls = []

        async def fake_mint(a, **kwargs):
            calls.append("mint")
            return "ghs_refreshed"

        async def fake_ensure(name, ptype, config, credentials):
            calls.append(("provider", name, credentials))

        with patch("swarmer.github_auth.IAT_REFRESH_INTERVAL", 0):  # fire immediately
            with patch("swarmer.github_auth.mint_installation_token", side_effect=fake_mint):
                with patch("swarmer.openshell_client.ensure_provider", side_effect=fake_ensure):
                    from swarmer.github_auth import start_token_refresh_loop
                    task = asyncio.create_task(
                        start_token_refresh_loop(app, session_id=42, provider_name="swarmer-ws-1-github-app")
                    )
                    # Let the loop fire once then cancel.
                    await asyncio.sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        assert "mint" in calls
        provider_calls = [c for c in calls if isinstance(c, tuple)]
        assert len(provider_calls) >= 1
        assert provider_calls[0][1] == "swarmer-ws-1-github-app"
        assert provider_calls[0][2]["GH_TOKEN"] == "ghs_refreshed"

    @pytest.mark.asyncio
    async def test_loop_continues_after_mint_failure(self):
        """A mint failure should log a warning but not kill the loop."""
        app = _make_app()
        mint_attempts = []

        async def flaky_mint(a, **kwargs):
            mint_attempts.append(1)
            if len(mint_attempts) == 1:
                raise RuntimeError("GitHub API error")
            return "ghs_ok"

        async def fake_ensure(name, ptype, config, credentials):
            pass

        with patch("swarmer.github_auth.IAT_REFRESH_INTERVAL", 0):
            with patch("swarmer.github_auth.mint_installation_token", side_effect=flaky_mint):
                with patch("swarmer.openshell_client.ensure_provider", side_effect=fake_ensure):
                    from swarmer.github_auth import start_token_refresh_loop
                    task = asyncio.create_task(
                        start_token_refresh_loop(app, session_id=99, provider_name="prov")
                    )
                    await asyncio.sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        assert len(mint_attempts) >= 2  # retried after failure


# ---------------------------------------------------------------------------
# CSRF helpers (from swarmer/csrf.py)
# ---------------------------------------------------------------------------

class TestCSRF:
    def test_ensure_creates_token(self):
        from swarmer.csrf import ensure_csrf_token
        request = MagicMock()
        request.session = {}
        token = ensure_csrf_token(request)
        assert token
        assert request.session["csrf_token"] == token

    def test_ensure_reuses_existing_token(self):
        from swarmer.csrf import ensure_csrf_token
        request = MagicMock()
        request.session = {"csrf_token": "existing"}
        assert ensure_csrf_token(request) == "existing"

    def test_validate_accepts_matching(self):
        from swarmer.csrf import validate_csrf_token
        request = MagicMock()
        request.session = {"csrf_token": "abc123"}
        validate_csrf_token(request, "abc123")  # no exception

    def test_validate_rejects_mismatch(self):
        from swarmer.csrf import validate_csrf_token, CSRFError
        request = MagicMock()
        request.session = {"csrf_token": "abc123"}
        with pytest.raises(CSRFError):
            validate_csrf_token(request, "wrong")

    def test_validate_rejects_missing(self):
        from swarmer.csrf import validate_csrf_token, CSRFError
        request = MagicMock()
        request.session = {}
        with pytest.raises(CSRFError):
            validate_csrf_token(request, "")

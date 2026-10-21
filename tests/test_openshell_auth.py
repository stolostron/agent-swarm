"""Tests for verified OIDC authentication in OpenShell runtime mode."""

import os
import secrets
import sys
import time

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import Response

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.config import settings
from swarmer.k8s_auth import (
    _OIDC_JWKS_CACHE,
    validate_openshell_oidc_token,
    validate_token,
)

ISSUER = "https://identity.example.com"
AUDIENCE = "swarmer"
JWKS_URI = f"{ISSUER}/keys"


@pytest.fixture
def oidc_signing_material(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": "key-1", "use": "sig", "alg": "RS256"})
    monkeypatch.setattr(settings, "openshell_oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "openshell_oidc_audience", AUDIENCE)
    monkeypatch.setattr(settings, "openshell_oidc_groups_claim", "groups")
    _OIDC_JWKS_CACHE.clear()
    from swarmer.k8s_auth import _OIDC_JWKS_LAST_REFRESH

    _OIDC_JWKS_LAST_REFRESH.clear()
    return private_key, {"keys": [jwk]}


def _token(private_key, *, audience=AUDIENCE, issuer=ISSUER, claims=None):
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-123",
        "preferred_username": "engineer",
        "groups": ["developers", "reviewers"],
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims or {})
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": "key-1"})


def _mock_discovery(jwks):
    router = respx.mock
    router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=Response(
            200,
            json={"issuer": ISSUER, "jwks_uri": JWKS_URI},
        )
    )
    router.get(JWKS_URI).mock(return_value=Response(200, json=jwks))


@pytest.mark.asyncio
async def test_accepts_valid_signature_and_maps_stable_identity(oidc_signing_material):
    private_key, jwks = oidc_signing_material
    token = _token(private_key)

    with respx.mock:
        _mock_discovery(jwks)
        identity = await validate_openshell_oidc_token(token)

    assert identity is not None
    assert identity.username == "user-123"
    assert identity.uid == "user-123"
    assert identity.groups == ["developers", "reviewers"]


@pytest.mark.asyncio
async def test_oidc_cannot_claim_reserved_bootstrap_admin_group(oidc_signing_material):
    private_key, jwks = oidc_signing_material
    token = _token(private_key, claims={"groups": ["swarmer-bootstrap-admin"]})

    with respx.mock:
        _mock_discovery(jwks)
        identity = await validate_openshell_oidc_token(token)

    assert identity is not None
    assert identity.groups == []
    assert identity.is_bootstrap_admin is False


@pytest.mark.asyncio
async def test_oidc_supports_a_configured_nested_groups_claim(oidc_signing_material, monkeypatch):
    private_key, jwks = oidc_signing_material
    monkeypatch.setattr(settings, "openshell_oidc_groups_claim", "realm_access.roles")
    token = _token(private_key, claims={"realm_access": {"roles": ["workspace-users"]}})

    with respx.mock:
        _mock_discovery(jwks)
        identity = await validate_openshell_oidc_token(token)

    assert identity is not None
    assert identity.username == "user-123"
    assert identity.groups == ["workspace-users"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"audience": "another-service"},
        {"issuer": "https://another-issuer.example.com"},
        {"claims": {"exp": int(time.time()) - 3600}},
    ],
)
async def test_rejects_invalid_claims(oidc_signing_material, token_kwargs):
    private_key, jwks = oidc_signing_material
    token = _token(private_key, **token_kwargs)

    with respx.mock:
        _mock_discovery(jwks)
        assert await validate_openshell_oidc_token(token) is None


@pytest.mark.asyncio
async def test_requires_explicit_issuer_and_audience(monkeypatch):
    monkeypatch.setattr(settings, "openshell_oidc_issuer", "")
    monkeypatch.setattr(settings, "openshell_oidc_audience", "")
    with respx.mock:
        assert await validate_openshell_oidc_token("not-a-jwt") is None
        assert not respx.calls


@pytest.mark.asyncio
async def test_bootstrap_admin_token_uses_constant_time_file_comparison(tmp_path, monkeypatch):
    token_file = tmp_path / "admin.token"
    configured_token = secrets.token_urlsafe(32)
    token_file.write_text(f"{configured_token}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "swarmer_runtime_mode", "openshell")
    monkeypatch.setattr(settings, "swarmer_admin_token_file", str(token_file))
    monkeypatch.setattr(settings, "swarmer_admin_username", "bootstrap-owner")

    identity = await validate_token(f" {configured_token} ", "unused", False)
    invalid_token = configured_token[:-1] + ("A" if configured_token[-1] != "A" else "B")
    invalid = await validate_token(invalid_token, "unused", False)

    assert identity is not None
    assert identity.username == "bootstrap-owner"
    assert identity.groups == ["swarmer-bootstrap-admin"]
    assert identity.is_bootstrap_admin is True
    assert invalid is None


@pytest.mark.asyncio
async def test_rejects_discovery_issuer_mismatch(oidc_signing_material):
    private_key, _jwks = oidc_signing_material
    token = _token(private_key)

    with respx.mock:
        respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=Response(200, json={"issuer": "https://wrong.example.com", "jwks_uri": JWKS_URI})
        )
        assert await validate_openshell_oidc_token(token) is None


@pytest.mark.asyncio
async def test_rejects_jwks_from_untrusted_origin(oidc_signing_material):
    private_key, _jwks = oidc_signing_material
    token = _token(private_key)

    with respx.mock:
        respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": "https://keys.example.net/jwks"},
            )
        )
        assert await validate_openshell_oidc_token(token) is None
        assert len(respx.calls) == 1

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from swarmer.openshell_command_parser import parse_gateway_command_or_json
from swarmer.openshell_token_parser import parse_token_input
from swarmer.crypto import init_crypto
from swarmer.models.workspace_gateway import WorkspaceGateway


@pytest.fixture(autouse=True)
def init_test_crypto(tmp_path):
    key_file = tmp_path / "secret.key"
    init_crypto(str(key_file))


def test_parse_cli_command_standard_oidc():
    cmd = (
        "openshell gateway add https://gw-openshell-7eda780a294c87c6.openshell.stage.devshift.net:443 "
        "--name swarm "
        "--oidc-issuer https://keycloak-ambient.apps.example.com/realms/ambient-code "
        "--oidc-client-id swarm-3I3WmAEA1KfD4RD0MJTQJWXw7QV "
        "--oidc-audience swarm-3I3WmAEA1KfD4RD0MJTQJWXw7QV"
    )
    res = parse_gateway_command_or_json(cmd)
    assert not res.errors
    assert res.gateway_url == "https://gw-openshell-7eda780a294c87c6.openshell.stage.devshift.net:443"
    assert res.auth_mode == "oidc"
    assert res.suggested_name == "swarm"
    assert res.oidc_issuer == "https://keycloak-ambient.apps.example.com/realms/ambient-code"
    assert res.oidc_client_id == "swarm-3I3WmAEA1KfD4RD0MJTQJWXw7QV"
    assert res.oidc_audience == "swarm-3I3WmAEA1KfD4RD0MJTQJWXw7QV"
    assert res.tls_verify is True


def test_parse_cli_command_multiline_with_bearer():
    cmd = """
    openshell gw add https://gateway.internal:8080 \\
      --name my-gateway \\
      --bearer-token "secret-token-123" \\
      --gateway-insecure
    """
    res = parse_gateway_command_or_json(cmd)
    assert not res.errors
    assert res.gateway_url == "https://gateway.internal:8080"
    assert res.auth_mode == "bearer"
    assert res.suggested_name == "my-gateway"
    assert res.bearer_token == "secret-token-123"
    assert res.tls_verify is False


def test_parse_cli_command_plaintext():
    cmd = "openshell gateway add http://127.0.0.1:8080 --local"
    res = parse_gateway_command_or_json(cmd)
    assert not res.errors
    assert res.gateway_url == "http://127.0.0.1:8080"
    assert res.auth_mode == "none"


def test_parse_json_metadata():
    metadata = {
        "name": "swarm-test",
        "gateway_endpoint": "https://gw-test.example.com:443",
        "auth_mode": "oidc",
        "oidc_issuer": "https://idp.example.com/realm",
        "oidc_client_id": "client-1",
        "oidc_audience": "aud-1",
        "gateway_insecure": True,
    }
    res = parse_gateway_command_or_json(json.dumps(metadata))
    assert not res.errors
    assert res.gateway_url == "https://gw-test.example.com:443"
    assert res.auth_mode == "oidc"
    assert res.suggested_name == "swarm-test"
    assert res.oidc_issuer == "https://idp.example.com/realm"
    assert res.tls_verify is False


def test_parse_json_metadata_non_string_values_are_safely_coerced_or_dropped():
    metadata = {
        "name": ["not", "a", "string"],
        "gateway_endpoint": 443,
        "auth_mode": "oidc",
        "oidc_issuer": {"bad": "shape"},
        "oidc_client_id": 123,
        "oidc_audience": True,
        "bearer_token": ["bad"],
    }
    res = parse_gateway_command_or_json(json.dumps(metadata))
    assert not res.errors
    assert res.gateway_url == "443"
    assert res.oidc_issuer is None
    assert res.oidc_client_id == "123"
    assert res.oidc_audience == "True"
    assert res.bearer_token is None
    assert res.suggested_name is None


def test_parse_token_json_bundle():
    bundle = {
        "refresh_token": "rt-1234567890abcdef",
        "access_token": "at-1234567890abcdef",
        "expires_at": 1755600000,
        "issuer": "https://issuer.example.com",
    }
    res = parse_token_input(json.dumps(bundle))
    assert res.status == "valid"
    assert res.format_detected == "json_bundle"
    assert res.refresh_token == "rt-1234567890abcdef"
    assert res.access_token == "at-1234567890abcdef"
    assert res.expires_at == 1755600000


def test_parse_token_key_value():
    res = parse_token_input("export REFRESH_TOKEN=\"rt-keyvalue-1234567890\"")
    assert res.status == "valid"
    assert res.format_detected == "key_value"
    assert res.refresh_token == "rt-keyvalue-1234567890"


def test_parse_token_raw_string():
    res = parse_token_input("  rt-rawtoken-1234567890  ")
    assert res.status == "valid"
    assert res.format_detected == "raw"
    assert res.refresh_token == "rt-rawtoken-1234567890"


def test_parse_token_json_bundle_non_string_tokens_are_ignored():
    bundle = {
        "refresh_token": {"bad": "shape"},
        "access_token": ["bad"],
        "expires_at": "1755600000",
        "issuer": ["bad"],
        "client_id": 123,
    }
    res = parse_token_input(json.dumps(bundle))
    assert res.status == "malformed"


def test_parse_token_input_non_string_returns_malformed():
    res = parse_token_input(12345)  # type: ignore[arg-type]
    assert res.status == "malformed"


def test_workspace_gateway_encryption():
    gw = WorkspaceGateway(
        workspace_id=1,
        gateway_url="https://gw.example.com",
        auth_mode="oidc",
    )
    # Use obviously-fake, non-secret-shaped placeholders so this fixture does
    # not trip automated secret scanners (no PEM headers / real token prefixes).
    fake_tls_key = "test-placeholder-tls-key-not-a-real-key"
    gw.refresh_token = "my-secret-refresh-token"
    gw.bearer_token = "my-secret-bearer-token"
    gw.tls_key = fake_tls_key

    assert gw.refresh_token_enc is not None
    assert gw.refresh_token_enc != "my-secret-refresh-token"
    assert gw.refresh_token == "my-secret-refresh-token"

    assert gw.bearer_token_enc is not None
    assert gw.bearer_token_enc != "my-secret-bearer-token"
    assert gw.bearer_token == "my-secret-bearer-token"

    assert gw.tls_key_enc is not None
    assert gw.tls_key_enc != fake_tls_key
    assert gw.tls_key == fake_tls_key

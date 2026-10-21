"""Unit tests for local/remote OpenShell sandbox deployment planning."""

import importlib.util
import os
import secrets
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "openshell_deploy.py"
_spec = importlib.util.spec_from_file_location("openshell_deploy", SCRIPT_PATH)
assert _spec and _spec.loader
openshell_deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(openshell_deploy)


def test_policy_is_default_deny_and_scoped_to_required_hosts(monkeypatch):
    monkeypatch.setenv("SWARMER_OIDC_ISSUER", "https://identity.example.com/realms/test")
    monkeypatch.setenv("SWARMER_GATEWAY_OIDC_ISSUER", "https://gateway-id.example.com/realm")
    monkeypatch.setenv("SWARMER_OIDC_JWKS_ALLOWED_ORIGINS", "https://keys.example.com")

    policy = openshell_deploy.build_policy("openshell.openshell.svc.cluster.local", 8080)
    host_ports = {
        (endpoint["host"], endpoint["port"])
        for rule in policy["network_policies"].values()
        for endpoint in rule["endpoints"]
    }

    assert policy["version"] == 1
    assert policy["filesystem_policy"]["read_write"] == ["/sandbox", "/tmp", "/dev/null"]
    assert ("api.github.com", 443) in host_ports
    assert ("github.com", 443) in host_ports
    assert ("openshell.openshell.svc.cluster.local", 8080) in host_ports
    assert {"identity.example.com", "gateway-id.example.com", "keys.example.com"} <= {
        host for host, _ in host_ports
    }
    assert not any(host == "0.0.0.0" or host == "*" for host, _ in host_ports)
    assert all(
        set(binary) == {"path"}
        for rule in policy["network_policies"].values()
        for binary in rule["binaries"]
    )
    assert all(
        rule["name"] == key for key, rule in policy["network_policies"].items()
    )


def test_local_gateway_address_uses_kind_service_dns(monkeypatch):
    monkeypatch.delenv("OPENSHELL_GATEWAY_RUNTIME_HOST", raising=False)
    monkeypatch.delenv("OPENSHELL_GATEWAY_RUNTIME_PORT", raising=False)
    monkeypatch.setenv("OPENSHELL_NAMESPACE", "openshell")

    host, port, scheme = openshell_deploy.runtime_gateway_address(
        {"gateway_endpoint": "https://localhost:17671", "is_remote": False}
    )

    assert (host, port, scheme) == ("openshell.openshell.svc.cluster.local", 8080, "https")


def test_gateway_runtime_address_allows_explicit_endpoint_override(monkeypatch):
    monkeypatch.setenv("OPENSHELL_GATEWAY_RUNTIME_HOST", "gateway.internal.example.com")
    monkeypatch.setenv("OPENSHELL_GATEWAY_RUNTIME_PORT", "9443")
    assert openshell_deploy.runtime_gateway_address({}) == (
        "gateway.internal.example.com",
        9443,
        "https",
    )


def test_gateway_runtime_address_rejects_plaintext_remote_endpoint():
    with pytest.raises(openshell_deploy.DeploymentError, match="secure HTTPS"):
        openshell_deploy.runtime_gateway_address(
            {"gateway_endpoint": "http://gateway.example.com:8080", "is_remote": True}
        )


def test_local_gateway_bundle_contains_only_generated_mtls_files(tmp_path):
    gateway_dir = tmp_path / "gateways" / "kind-cluster"
    mtls_dir = gateway_dir / "mtls"
    mtls_dir.mkdir(parents=True)
    (gateway_dir / "metadata.json").write_text("{}", encoding="utf-8")
    for filename in ("ca.crt", "tls.crt", "tls.key"):
        (mtls_dir / filename).touch()
    os.chmod(mtls_dir / "tls.key", 0o600)
    (gateway_dir / "oidc_token.json").write_text("ignored", encoding="utf-8")

    bundle = openshell_deploy.local_gateway_mtls_bundle("kind-cluster", tmp_path)

    assert bundle is not None
    assert [path.name for path in bundle] == ["ca.crt", "tls.crt", "tls.key"]
    assert all(path.parent == mtls_dir for path in bundle)


def test_empty_admin_token_source_uses_private_default_without_chmodding_worktree(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    worktree = home / "repo"
    worktree.mkdir(parents=True)
    os.chmod(worktree, 0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SWARMER_ADMIN_TOKEN_SOURCE", "")
    monkeypatch.chdir(worktree)

    token_file = openshell_deploy._admin_token_source()

    assert token_file == home / ".config" / "swarmer" / "admin.token"
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert worktree.stat().st_mode & 0o700 == 0o700


def test_app_environment_uses_runtime_secret_paths_not_secret_values(monkeypatch):
    monkeypatch.setenv("SWARMER_GATEWAY_OIDC_ISSUER", "https://identity.example.com")
    monkeypatch.setenv("SWARMER_GATEWAY_OIDC_CLIENT_ID", "swarmer-service")

    env = openshell_deploy.app_environment(
        "https://gateway.example.com:443",
        "/sandbox/auth/gateway-ca.crt",
        has_gateway_client_secret=True,
    )

    assert env["SWARMER_RUNTIME_MODE"] == "openshell"
    assert env["K8S_IN_CLUSTER"] == "false"
    assert env["SWARMER_GATEWAY_TLS_CA"] == "/sandbox/auth/gateway-ca.crt"
    assert env["SWARMER_GATEWAY_OIDC_CLIENT_SECRET_FILE"] == "/sandbox/auth/gateway-client-secret"
    assert "swarmer-service" == env["SWARMER_GATEWAY_OIDC_CLIENT_ID"]
    assert not any(name.startswith("OPENSHELL_") for name in env)
    assert all("secret-value" not in value for value in env.values())


def test_remote_gateway_requires_machine_credentials():
    with pytest.raises(openshell_deploy.DeploymentError, match="Remote gateway deployment requires"):
        openshell_deploy.validate_gateway_auth(
            is_remote=True,
            client_secret_source=None,
            client_cert_source=None,
            client_key_source=None,
        )


def test_upload_falls_back_to_stdin_for_older_supervisors(tmp_path, monkeypatch):
    source = tmp_path / "gateway-secret"
    secret_material = secrets.token_urlsafe(32)
    source.write_text(secret_material, encoding="utf-8")
    os.chmod(source, 0o600)
    invocations = []

    def fake_cli(args):
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr="this sandbox supervisor does not support openshell-upload-v1",
        )

    def fake_exec(args, **kwargs):
        invocations.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(openshell_deploy, "_run", fake_cli)
    monkeypatch.setattr(openshell_deploy.subprocess, "run", fake_exec)

    openshell_deploy._upload_file(
        "remote-gateway", "team-workspace", "swarmer", source,
        "/sandbox/auth/gateway-client-secret",
    )

    args, kwargs = invocations[0]
    assert args[args.index("--") + 1 : args.index("--") + 4] == [
        "/bin/sh", "-c", 'umask 077; cat > "$1" && chmod "$2" "$1"'
    ]
    assert args[-2:] == ["/sandbox/auth/gateway-client-secret", "600"]
    assert kwargs["stdin"].name == str(source)
    assert secret_material not in " ".join(args)

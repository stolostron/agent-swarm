#!/usr/bin/env python3
"""Deploy Swarmer to an existing OpenShell gateway without deleting state."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml


class DeploymentError(RuntimeError):
    """Raised when the requested OpenShell deployment cannot be reconciled."""


_LOCAL_GATEWAY_FORWARD: subprocess.Popen | None = None


_GITHUB_BINARIES = [
    {"path": path}
    for path in ("/usr/bin/python3.12",)
]
_PYTHON_BINARIES = [
    {"path": path}
    for path in ("/usr/bin/python3.12",)
]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise DeploymentError(f"Set {name} before running make openshell-deploy.")
    return value


def _valid_hostname(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise DeploymentError(f"{label} must be a DNS hostname or IPv4 address.")
    return value


def _valid_port(raw: str, label: str) -> int:
    try:
        port = int(raw)
    except ValueError as exc:
        raise DeploymentError(f"{label} must be an integer port.") from exc
    if not 1 <= port <= 65535:
        raise DeploymentError(f"{label} must be between 1 and 65535.")
    return port


def resolve_gateway_name(explicit: str = "", config_root: Path | None = None) -> str:
    """Resolve an explicit gateway or the OpenShell CLI active selection."""
    if explicit.strip():
        name = explicit.strip()
    else:
        roots = [config_root or Path.home() / ".config" / "openshell"]
        system_root = Path(_env("OPENSHELL_SYSTEM_GATEWAY_DIR") or "/etc/openshell")
        if system_root not in roots:
            roots.append(system_root)
        name = ""
        for root in roots:
            try:
                name = (root / "active_gateway").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if name:
                break
        if not name:
            raise DeploymentError(
                "No OpenShell gateway selected; set OPENSHELL_GATEWAY or run "
                "'openshell gateway select <gateway>'."
            )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise DeploymentError("The selected OpenShell gateway name is invalid.")
    return name


def read_gateway_metadata(gateway: str, config_root: Path | None = None) -> dict:
    """Read only the non-secret metadata required for routing and policy."""
    roots = [config_root or Path.home() / ".config" / "openshell"]
    system_root = Path(_env("OPENSHELL_SYSTEM_GATEWAY_DIR") or "/etc/openshell")
    if system_root not in roots:
        roots.append(system_root)
    for root in roots:
        metadata_file = root / "gateways" / gateway / "metadata.json"
        try:
            metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError("OpenShell gateway metadata could not be read.") from exc
        if not isinstance(metadata, dict) or not metadata.get("gateway_endpoint"):
            raise DeploymentError("OpenShell gateway metadata has no endpoint.")
        return metadata
    raise DeploymentError("OpenShell gateway metadata was not found for the selected gateway.")


def gateway_config_directory(gateway: str, config_root: Path | None = None) -> Path:
    """Find the selected gateway's user metadata directory, never its token bundle."""
    roots = [config_root or Path.home() / ".config" / "openshell"]
    system_root = Path(_env("OPENSHELL_SYSTEM_GATEWAY_DIR", "/etc/openshell"))
    if system_root not in roots:
        roots.append(system_root)
    for root in roots:
        gateway_dir = root / "gateways" / gateway
        if (gateway_dir / "metadata.json").is_file():
            return gateway_dir
    raise DeploymentError("OpenShell gateway metadata was not found for the selected gateway.")


def local_gateway_mtls_bundle(gateway: str, config_root: Path | None = None) -> tuple[Path, Path, Path] | None:
    """Return the local gateway's generated client bundle, not a user's token bundle."""
    gateway_dir = gateway_config_directory(gateway, config_root)
    bundle = tuple(gateway_dir / "mtls" / name for name in ("ca.crt", "tls.crt", "tls.key"))
    if all(path.is_file() and os.access(path, os.R_OK) for path in bundle):
        if stat.S_IMODE(bundle[2].stat().st_mode) & 0o077:
            raise DeploymentError(
                "The local gateway client key must be owner-only; rerun make openshell-register to refresh it securely."
            )
        return bundle  # type: ignore[return-value]
    return None


def runtime_gateway_address(metadata: dict) -> tuple[str, int, str]:
    """Resolve the gateway address reachable from inside the Swarmer sandbox."""
    host_override = _env("OPENSHELL_GATEWAY_RUNTIME_HOST")
    port_override = _env("OPENSHELL_GATEWAY_RUNTIME_PORT")
    if host_override:
        host = _valid_hostname(host_override, "OPENSHELL_GATEWAY_RUNTIME_HOST")
        port = _valid_port(port_override or "8080", "OPENSHELL_GATEWAY_RUNTIME_PORT")
        return host, port, "https"

    endpoint = str(metadata["gateway_endpoint"]).strip()
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"https", "grpcs"} or not parsed.hostname:
        raise DeploymentError("The selected gateway must use a secure HTTPS/gRPCs endpoint.")
    try:
        endpoint_port = parsed.port or 443
    except ValueError as exc:
        raise DeploymentError("The gateway endpoint has an invalid port.") from exc

    is_remote = metadata.get("is_remote", False) is True or str(
        metadata.get("is_remote", "")
    ).strip().lower() == "true"
    if is_remote:
        host = _valid_hostname(parsed.hostname, "gateway endpoint host")
        port = _valid_port(port_override or str(endpoint_port), "gateway port")
        return host, port, "https"

    namespace = _env("OPENSHELL_NAMESPACE", "openshell")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
        raise DeploymentError("OPENSHELL_NAMESPACE is not a valid Kubernetes namespace.")
    host = f"openshell.{namespace}.svc.cluster.local"
    port = _valid_port(port_override or "8080", "OPENSHELL_GATEWAY_RUNTIME_PORT")
    return host, port, "https"


def _issuer_endpoint(raw_url: str) -> dict | None:
    if not raw_url:
        return None
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise DeploymentError("OpenShell OIDC issuers must be HTTPS URLs without embedded credentials.")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise DeploymentError("An OpenShell OIDC issuer has an invalid port.") from exc
    return {
        "host": _valid_hostname(parsed.hostname, "OIDC issuer host"),
        "port": port,
        "protocol": "rest",
        "enforcement": "enforce",
        "access": "full",
    }


def build_policy(gateway_host: str, gateway_port: int) -> dict:
    """Build a least-privilege policy for the Swarmer control-plane sandbox."""
    github_endpoints = [
        {"host": "api.github.com", "port": 443, "protocol": "rest", "enforcement": "enforce", "access": "full"},
        {"host": "github.com", "port": 443, "protocol": "rest", "enforcement": "enforce", "access": "full"},
        {"host": "raw.githubusercontent.com", "port": 443, "protocol": "rest", "enforcement": "enforce", "access": "read-only"},
        {"host": "objects.githubusercontent.com", "port": 443, "protocol": "rest", "enforcement": "enforce", "access": "read-only"},
        {"host": "codeload.github.com", "port": 443, "protocol": "rest", "enforcement": "enforce", "access": "read-only"},
    ]
    gateway_endpoint = {"host": gateway_host, "port": gateway_port}
    policy = {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": ["/app", "/usr", "/opt", "/lib", "/etc", "/proc", "/var/log"],
            "read_write": ["/sandbox", "/tmp", "/dev/null"],
        },
        "network_policies": {
            "github": {
                "name": "github",
                "endpoints": github_endpoints,
                "binaries": _GITHUB_BINARIES,
            },
            "gateway": {
                "name": "gateway",
                "endpoints": [gateway_endpoint],
                "binaries": _PYTHON_BINARIES,
            },
        },
    }
    issuer_urls = [
        _env("SWARMER_OIDC_ISSUER"),
        _env("SWARMER_GATEWAY_OIDC_ISSUER"),
        *_env("SWARMER_OIDC_JWKS_ALLOWED_ORIGINS").split(","),
        *_env("SWARMER_GATEWAY_OIDC_ALLOWED_ORIGINS").split(","),
    ]
    oidc_endpoints = []
    for value in issuer_urls:
        endpoint = _issuer_endpoint(value.strip())
        if endpoint and endpoint not in oidc_endpoints:
            oidc_endpoints.append(endpoint)
    if oidc_endpoints:
        policy["network_policies"]["oidc"] = {
            "name": "oidc",
            "endpoints": oidc_endpoints,
            "binaries": _PYTHON_BINARIES,
        }
    return policy


def app_environment(
    gateway_url: str,
    gateway_ca_file: str | None,
    gateway_client_cert_file: str | None = None,
    gateway_client_key_file: str | None = None,
    has_gateway_client_secret: bool = False,
) -> dict[str, str]:
    """Return non-secret environment configuration for the sandbox process."""
    env = {
        "K8S_IN_CLUSTER": "false",
        "SWARMER_RUNTIME_MODE": "openshell",
        "DATABASE_URL": "sqlite+aiosqlite:////sandbox/swarmer.db",
        "SECRET_KEY_FILE": "/sandbox/auth/secret.key",
        "SWARMER_ADMIN_TOKEN_FILE": "/sandbox/auth/admin.token",
        "SWARMER_GATEWAY_URL": gateway_url,
        "SWARMER_GATEWAY_TLS_CA": gateway_ca_file or "",
        "SWARMER_GATEWAY_TLS_CERT": gateway_client_cert_file or "",
        "SWARMER_GATEWAY_TLS_KEY": gateway_client_key_file or "",
        "SWARMER_GATEWAY_OIDC_ISSUER": _env("SWARMER_GATEWAY_OIDC_ISSUER"),
        "SWARMER_GATEWAY_OIDC_CLIENT_ID": _env("SWARMER_GATEWAY_OIDC_CLIENT_ID"),
        "SWARMER_GATEWAY_OIDC_AUDIENCE": _env("SWARMER_GATEWAY_OIDC_AUDIENCE"),
        "SWARMER_GATEWAY_OIDC_SCOPES": _env("SWARMER_GATEWAY_OIDC_SCOPES"),
        "SWARMER_GATEWAY_OIDC_CLIENT_SECRET_FILE": (
            "/sandbox/auth/gateway-client-secret"
            if has_gateway_client_secret
            else ""
        ),
        "SWARMER_OIDC_ISSUER": _env("SWARMER_OIDC_ISSUER"),
        "SWARMER_OIDC_AUDIENCE": _env("SWARMER_OIDC_AUDIENCE"),
        "SWARMER_OIDC_JWKS_ALLOWED_ORIGINS": _env("SWARMER_OIDC_JWKS_ALLOWED_ORIGINS"),
        "SWARMER_OIDC_GROUPS_CLAIM": _env("SWARMER_OIDC_GROUPS_CLAIM", "groups"),
        "HOST": "0.0.0.0",
        "PORT": _env("SWARM_PORT", "8080"),
    }
    internal_url = _env("AGENT_SWARM_INTERNAL_URL")
    if internal_url:
        env["AGENT_SWARM_INTERNAL_URL"] = internal_url
    return env


def _admin_token_source() -> Path:
    configured = _env("SWARMER_ADMIN_TOKEN_SOURCE")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config" / "swarmer" / "admin.token"
    )
    if not configured:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        if configured:
            raise DeploymentError("The configured bootstrap token source file does not exist.")
        path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        os.chmod(path, 0o600)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DeploymentError("The bootstrap token source file must be accessible only to its owner.")
    return path


def _source_file(env_name: str) -> Path | None:
    raw = _env(env_name)
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise DeploymentError(f"Configured source file for {env_name} is not readable.")
    if ("SECRET" in env_name or "TLS_KEY" in env_name) and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DeploymentError(f"Configured secret source file for {env_name} must be owner-only.")
    return path


def validate_gateway_auth(
    *,
    is_remote: bool,
    client_secret_source: Path | None,
    client_cert_source: Path | None,
    client_key_source: Path | None,
) -> None:
    """Require a durable machine identity; reject workstation credentials by default."""
    if (client_cert_source is None) != (client_key_source is None):
        raise DeploymentError("Configure both the Gateway client certificate and key source files.")
    if client_secret_source and client_cert_source:
        raise DeploymentError("Choose either OIDC machine credentials or a client TLS certificate, not both.")
    if client_secret_source and (
        not _env("SWARMER_GATEWAY_OIDC_ISSUER")
        or not _env("SWARMER_GATEWAY_OIDC_CLIENT_ID")
    ):
        raise DeploymentError("Gateway client credentials require an issuer and client ID.")
    if is_remote and client_secret_source is None and client_cert_source is None:
        raise DeploymentError(
            "Remote gateway deployment requires a dedicated OIDC client secret file "
            "or explicitly supplied machine TLS certificate and key. Do not inject "
            "workstation CLI tokens or personal mTLS credentials."
        )


def _cli_base(gateway: str, workspace: str) -> list[str]:
    return ["openshell", "-g", gateway, "--workspace", workspace]


def _start_local_gateway_forward(gateway: str, metadata: dict) -> None:
    """Bring up the registered localhost tunnel for a local Kind gateway."""
    global _LOCAL_GATEWAY_FORWARD
    is_remote = metadata.get("is_remote", False) is True or str(
        metadata.get("is_remote", "")
    ).strip().lower() == "true"
    if is_remote:
        return
    parsed = urlparse(str(metadata.get("gateway_endpoint", "")))
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise DeploymentError("The registered local gateway endpoint has an invalid port.") from exc
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return
    except OSError:
        pass

    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise DeploymentError(
            "The selected local gateway needs a kubectl port-forward; install kubectl or "
            "use an already reachable remote gateway."
        )
    gateway_dir = gateway_config_directory(gateway)
    context_file = gateway_dir / "kubectl_context"
    try:
        context = context_file.read_text(encoding="utf-8").strip()
    except OSError:
        context = ""
    args = [kubectl]
    if context:
        args.extend(["--context", context])
    args.extend(
        [
            "port-forward",
            "-n",
            _env("OPENSHELL_NAMESPACE", "openshell"),
            "svc/openshell",
            f"{port}:8080",
        ]
    )
    try:
        _LOCAL_GATEWAY_FORWARD = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise DeploymentError("Could not start the local OpenShell Gateway port-forward.") from exc
    for _ in range(60):
        if _LOCAL_GATEWAY_FORWARD.poll() is not None:
            _stop_local_gateway_forward()
            raise DeploymentError("The local OpenShell Gateway port-forward exited unexpectedly.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.25)
    _stop_local_gateway_forward()
    raise DeploymentError("Timed out waiting for the local OpenShell Gateway port-forward.")


def _stop_local_gateway_forward() -> None:
    global _LOCAL_GATEWAY_FORWARD
    process = _LOCAL_GATEWAY_FORWARD
    _LOCAL_GATEWAY_FORWARD = None
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run(args: list[str], *, capture: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            check=False,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        raise DeploymentError("The OpenShell CLI is required; install the pinned OpenShell 0.1.4 CLI.") from exc


def _safe_cli_diagnostic(result: subprocess.CompletedProcess) -> str:
    """Return a short CLI diagnostic with credentials and local paths removed."""
    detail = (result.stderr or result.stdout or "").strip()
    detail = re.sub(r"(?i)\bBearer\s+[^\s,]+", "Bearer <redacted>", detail)
    detail = re.sub(
        r"(?i)\b(token|secret|password|api[_-]?key)(\s*[:=]\s*|\s+)[^\s,]+",
        r"\1\2<redacted>",
        detail,
    )
    detail = re.sub(r"/home/[^/\s]+", "<HOME>", detail)
    return detail[:600]


def _run_checked(args: list[str], *, help_text: str) -> subprocess.CompletedProcess:
    result = _run(args)
    if result.returncode != 0:
        diagnostic = _safe_cli_diagnostic(result)
        suffix = f" CLI diagnostic: {diagnostic}" if diagnostic else ""
        raise DeploymentError(help_text + suffix)
    return result


def _sandbox_list(gateway: str, workspace: str) -> list[dict]:
    result = _run_checked(
        [*_cli_base(gateway, workspace), "sandbox", "list", "--output", "json"],
        help_text="Could not list sandboxes; verify the selected gateway and workspace access.",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DeploymentError("The OpenShell CLI returned invalid sandbox-list JSON.") from exc
    sandboxes = payload.get("sandboxes", []) if isinstance(payload, dict) else []
    return [sandbox for sandbox in sandboxes if isinstance(sandbox, dict)]


def _find_sandbox(sandboxes: list[dict], name: str) -> dict | None:
    for sandbox in sandboxes:
        sandbox_name = sandbox.get("name")
        if sandbox_name is None and isinstance(sandbox.get("sandbox"), dict):
            sandbox_name = sandbox["sandbox"].get("name")
        if sandbox_name == name:
            return sandbox
    return None


def _sandbox_phase(sandbox: dict) -> str:
    phase = sandbox.get("phase") or sandbox.get("status") or ""
    if isinstance(phase, dict):
        phase = phase.get("phase") or phase.get("name") or ""
    return str(phase).strip().lower()


def _wait_for_sandbox_ready(gateway: str, workspace: str, name: str, timeout: int = 300) -> None:
    deadline = time.monotonic() + timeout
    last_phase = "not-found"
    while time.monotonic() < deadline:
        sandbox = _find_sandbox(_sandbox_list(gateway, workspace), name)
        if sandbox is None:
            last_phase = "not-found"
        else:
            last_phase = _sandbox_phase(sandbox)
            if last_phase in {"ready", "running"}:
                return
            if last_phase in {"error", "failed", "finished"}:
                raise DeploymentError(
                    f"Swarmer sandbox entered terminal phase '{last_phase}'; it was not deleted."
                )
        time.sleep(2)
    raise DeploymentError(
        f"Timed out waiting for Swarmer sandbox readiness (last phase: {last_phase})."
    )


def _make_sandbox(gateway: str, workspace: str, name: str, image: str, policy: Path, env: dict[str, str]) -> None:
    args = [
        *_cli_base(gateway, workspace),
        "sandbox", "create",
        "--name", name,
        "--from", image,
        "--policy", str(policy),
        "--detach",
        "--no-tty",
        "--restart-policy", "always",
        "--no-credential-warnings",
    ]
    for key, value in env.items():
        if value:
            args.extend(["--env", f"{key}={value}"])
    app_port = int(env["PORT"])
    wait_for_credentials = (
        "umask 077; mkdir -p /sandbox/auth; chmod 700 /sandbox/auth; "
        "while [ ! -f /sandbox/auth/ready ]; do sleep 1; done; "
        "exec /opt/app-root/bin/uvicorn swarmer.main:app "
        f"--host 0.0.0.0 --port {app_port} --proxy-headers --forwarded-allow-ips='*'"
    )
    args.extend(["--", "/bin/sh", "-c", wait_for_credentials])
    result = _run(args)
    if result.returncode != 0:
        diagnostic = _safe_cli_diagnostic(result)
        suffix = f" CLI diagnostic: {diagnostic}" if diagnostic else ""
        raise DeploymentError(
            "OpenShell sandbox creation failed; no existing sandbox was deleted." + suffix
        )


def _write_file_via_exec(
    gateway: str,
    workspace: str,
    name: str,
    source: Path,
    destination: str,
    mode: str,
) -> None:
    """Stream a secret over exec stdin when an older supervisor lacks upload v1."""
    command = (
        'umask 077; cat > "$1" && chmod "$2" "$1"'
    )
    args = [
        *_cli_base(gateway, workspace),
        "sandbox", "exec", "--name", name, "--no-tty", "--",
        "/bin/sh", "-c", command, "sh", destination, mode,
    ]
    try:
        with source.open("rb") as source_stream:
            result = subprocess.run(args, stdin=source_stream, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise DeploymentError("Could not stream the configured credential file to the sandbox.") from exc
    if result.returncode != 0:
        diagnostic = _safe_cli_diagnostic(result)
        suffix = f" CLI diagnostic: {diagnostic}" if diagnostic else ""
        raise DeploymentError("Could not securely write a credential file in the sandbox." + suffix)


def _upload_file(
    gateway: str,
    workspace: str,
    name: str,
    source: Path,
    destination: str,
    mode: str = "600",
) -> None:
    result = _run([*_cli_base(gateway, workspace), "sandbox", "upload", name, str(source), destination])
    if result.returncode == 0:
        return
    if "does not support openshell-upload-v1" in (result.stderr or "").lower():
        _write_file_via_exec(gateway, workspace, name, source, destination, mode)
        return
    diagnostic = _safe_cli_diagnostic(result)
    suffix = f" CLI diagnostic: {diagnostic}" if diagnostic else ""
    raise DeploymentError(
        f"Could not securely upload the configured file to the sandbox destination {destination}." + suffix
    )


def _exec(gateway: str, workspace: str, name: str, command: list[str]) -> None:
    _run_checked(
        [*_cli_base(gateway, workspace), "sandbox", "exec", "--name", name, "--no-tty", "--", *command],
        help_text="Could not prepare the Swarmer sandbox runtime.",
    )


def deploy() -> None:
    if shutil.which("openshell") is None:
        raise DeploymentError("The OpenShell CLI is required; install the pinned OpenShell 0.1.4 CLI.")

    gateway = resolve_gateway_name(_env("OPENSHELL_GATEWAY"))
    workspace = _env("OPENSHELL_WORKSPACE", "default")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", workspace):
        raise DeploymentError("OPENSHELL_WORKSPACE is invalid.")
    sandbox_name = _env("SWARM_SANDBOX_NAME", "swarmer")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,17}[a-z0-9])?", sandbox_name):
        raise DeploymentError("SWARM_SANDBOX_NAME must be a lowercase OpenShell sandbox name (max 19 characters).")
    image = _required("SWARM_IMAGE")
    swarm_port = _valid_port(_env("SWARM_PORT", "8080"), "SWARM_PORT")
    metadata = read_gateway_metadata(gateway)
    _start_local_gateway_forward(gateway, metadata)
    gateway_host, gateway_port, gateway_scheme = runtime_gateway_address(metadata)
    gateway_url = f"{gateway_scheme}://{gateway_host}:{gateway_port}"

    client_secret_source = _source_file("SWARMER_GATEWAY_OIDC_CLIENT_SECRET_SOURCE")
    is_remote = metadata.get("is_remote", False) is True or str(
        metadata.get("is_remote", "")
    ).strip().lower() == "true"
    explicit_ca = _source_file("SWARMER_GATEWAY_CA_SOURCE")
    explicit_cert = _source_file("SWARMER_GATEWAY_TLS_CERT_SOURCE")
    explicit_key = _source_file("SWARMER_GATEWAY_TLS_KEY_SOURCE")
    validate_gateway_auth(
        is_remote=is_remote,
        client_secret_source=client_secret_source,
        client_cert_source=explicit_cert,
        client_key_source=explicit_key,
    )
    local_bundle = None
    if not is_remote and client_secret_source is None and explicit_cert is None and explicit_key is None:
        local_bundle = local_gateway_mtls_bundle(gateway)
        if local_bundle is None:
            raise DeploymentError(
                "Local gateway deployment needs its generated gateway client TLS bundle "
                "or an OIDC machine client secret file."
            )
    if local_bundle:
        ca_source, cert_source, key_source = local_bundle
    else:
        ca_source = explicit_ca
        cert_source = explicit_cert
        key_source = explicit_key
    if explicit_ca:
        ca_source = explicit_ca
    ca_file = "/sandbox/auth/gateway-ca.crt" if ca_source else None
    cert_file = "/sandbox/auth/gateway-tls.crt" if cert_source else None
    key_file = "/sandbox/auth/gateway-tls.key" if key_source else None

    admin_source = _admin_token_source()
    env = app_environment(
        gateway_url,
        ca_file,
        cert_file,
        key_file,
        has_gateway_client_secret=client_secret_source is not None,
    )
    policy = build_policy(gateway_host, gateway_port)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", encoding="utf-8") as policy_file:
        yaml.safe_dump(policy, policy_file, sort_keys=False)
        policy_file.flush()
        _run_checked(
            [*_cli_base(gateway, workspace), "status"],
            help_text=(
                f"OpenShell gateway '{gateway}' is not authenticated. Run "
                f"'openshell gateway login {gateway}' and retry."
            ),
        )
        existing = _find_sandbox(_sandbox_list(gateway, workspace), sandbox_name)
        if existing is None:
            _make_sandbox(gateway, workspace, sandbox_name, image, Path(policy_file.name), env)
            _wait_for_sandbox_ready(gateway, workspace, sandbox_name)
        elif _sandbox_phase(existing) in {"stopped", "completed", "error", "finished"}:
            _run_checked(
                [*_cli_base(gateway, workspace), "sandbox", "start", sandbox_name],
                help_text="Could not start the retained Swarmer sandbox; its workspace was not deleted.",
            )
            _wait_for_sandbox_ready(gateway, workspace, sandbox_name)
        else:
            _wait_for_sandbox_ready(gateway, workspace, sandbox_name)
        _run_checked(
            [
                *_cli_base(gateway, workspace),
                "policy", "set", sandbox_name,
                "--policy", policy_file.name,
                "--wait",
            ],
            help_text="Could not apply the generated policy to the retained Swarmer sandbox.",
        )

    _exec(gateway, workspace, sandbox_name, ["/bin/sh", "-c", "umask 077; mkdir -p /sandbox/auth; chmod 700 /sandbox/auth"])
    _upload_file(gateway, workspace, sandbox_name, admin_source, "/sandbox/auth/admin.token")
    if client_secret_source:
        _upload_file(
            gateway,
            workspace,
            sandbox_name,
            client_secret_source,
            "/sandbox/auth/gateway-client-secret",
        )
    if ca_source:
        _upload_file(gateway, workspace, sandbox_name, ca_source, "/sandbox/auth/gateway-ca.crt", "644")
    if cert_source:
        _upload_file(gateway, workspace, sandbox_name, cert_source, "/sandbox/auth/gateway-tls.crt")
    if key_source:
        _upload_file(gateway, workspace, sandbox_name, key_source, "/sandbox/auth/gateway-tls.key")

    chmod_command = (
        "chmod 600 /sandbox/auth/admin.token; "
        "if [ -f /sandbox/auth/gateway-client-secret ]; then "
        "chmod 600 /sandbox/auth/gateway-client-secret; fi; "
        "if [ -f /sandbox/auth/gateway-tls.crt ]; then chmod 600 /sandbox/auth/gateway-tls.crt; fi; "
        "if [ -f /sandbox/auth/gateway-tls.key ]; then chmod 600 /sandbox/auth/gateway-tls.key; fi; "
        "if [ -f /sandbox/auth/gateway-ca.crt ]; then "
        "chmod 644 /sandbox/auth/gateway-ca.crt; fi; "
        "touch /sandbox/auth/ready"
    )
    _exec(gateway, workspace, sandbox_name, ["/bin/sh", "-c", chmod_command])
    _run_checked(
        [*_cli_base(gateway, workspace), "service", "expose", sandbox_name, str(swarm_port), "web"],
        help_text="Could not expose the Swarmer web service through OpenShell.",
    )
    service_result = _run_checked(
        [*_cli_base(gateway, workspace), "service", "list", sandbox_name, "--output", "json"],
        help_text="Swarmer's OpenShell service was not available after exposure.",
    )
    try:
        service_payload = json.loads(service_result.stdout)
        services = service_payload.get("services", [])
        service_url = str(
            next(
                (service.get("url", "") for service in services if service.get("service") == "web"),
                "",
            )
        )
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise DeploymentError("OpenShell returned invalid service endpoint metadata.") from exc
    if not service_url:
        raise DeploymentError("OpenShell did not return a URL for the Swarmer web service.")

    print(f"Swarmer sandbox: {sandbox_name}")
    print(f"OpenShell gateway/workspace: {gateway}/{workspace}")
    print(f"Swarmer service URL: {service_url}")
    print("The bootstrap admin token is stored in the configured local token file; it is not printed.")
    print("The existing sandbox was started or reconciled without deleting its persistent workspace.")


def main() -> int:
    try:
        deploy()
    except DeploymentError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        _stop_local_gateway_forward()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

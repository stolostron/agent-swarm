"""
OpenShell sandbox client wrapper for AgentSwarm session lifecycle.

Wraps the synchronous OpenShell gRPC SDK (pip install openshell) and exposes
async helpers by running blocking calls via asyncio.to_thread().

Sandbox isolation model:
    Each session runs inside an OpenShell sandbox — a container managed by the
    OpenShell Gateway via gRPC.  The sandbox is the primary security boundary:
    it enforces filesystem restrictions (Landlock LSM), network egress policy
    (OPA rules via SandboxPolicy proto), and process isolation (run_as_user /
    run_as_group).  Commands execute inside the container, not on the Swarmer
    host.

Credential injection:
    All credentials (AI keys, GitHub PAT, Jira/Slack tokens) are stored
    server-side on the Gateway via the Provider API (ensure_provider /
    attach_sandbox_provider) and injected as env vars into the sandbox at exec
    time via GetSandboxProviderEnvironment.  Raw credential values are never
    placed in SandboxSpec.environment or logged by Swarmer.  The Gateway
    returns them as REDACTED on subsequent reads.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import pathlib

# Avoid gRPC c-ares resolver failures on Kubernetes pods with ndots:5
os.environ.setdefault("GRPC_DNS_RESOLVER", "native")
import queue
import shlex
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# ── provider_exists TTL cache ─────────────────────────────────────────────────
# Avoids a gRPC round-trip on every page-load that renders model options or the
# secrets status badge.  Cache entries expire after _PROVIDER_CACHE_TTL seconds.
# Invalidated explicitly by create_google_cloud_provider / delete_provider.

_PROVIDER_CACHE_TTL: float = 30.0
_provider_cache: dict[tuple[str, str], tuple[bool, float]] = {}  # (gateway endpoint, name) → result
_OPENSHELL_WORKSPACE = "default"
_OPENCODE_PROVIDER_BINARIES = [
    {"path": "/usr/local/share/npm-global/lib/node_modules/@opencode/cli-linux-x64/bin/opencode"},
    {"path": "/usr/local/share/npm-global/lib/node_modules/@opencode/cli/bin/opencode.exe"},
    {"path": "/usr/local/share/npm-global/lib/node_modules/opencode-linux-x64/bin/opencode"},
    {"path": "/usr/local/share/npm-global/bin/opencode"},
    {"path": "/usr/local/share/npm-global/lib/node_modules/opencode-ai/bin/opencode.exe"},
]


def _agent_swarm_profile() -> dict:
    from urllib.parse import urlparse

    from swarmer.config import get_agent_swarm_internal_url

    parsed = urlparse(get_agent_swarm_internal_url())
    return {
        "id": "agent-swarm",
        "display_name": "Agent Swarm",
        "inference_capable": False,
        "credentials": [{
            "name": "AGENT_SWARM_API_TOKEN",
            "env_vars": ["AGENT_SWARM_API_TOKEN"],
            "required": True,
            "auth_style": "bearer",
            "header_name": "authorization",
        }],
        "endpoints": [{
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "protocol": "rest",
            "access": "read-write",
            "enforcement": "enforce",
        }],
        "binaries": [
            {"path": "/usr/local/bin/agent-swarm-mcp-server"},
            {"path": "/usr/local/bin/python3.14"},
            {"path": "/usr/local/bin/python3"},
            {"path": "/usr/bin/python3"},
            {"path": "/sandbox/.venv/bin/python*"},
        ],
    }

# Custom provider profiles swarmer registers in OpenShell gateways.
# Ensures static credentials (e.g. OPENAI_API_KEY, GOOGLE_API_KEY) are bound to
# their endpoints so OpenShell does not withhold them as unbound credentials.
# Note: "agent-swarm" and the per-workspace "enmaas-ws-{id}" profiles are built
# dynamically (see _agent_swarm_profile and _enmaas_profile) because their
# endpoints derive from runtime configuration.
CUSTOM_PROVIDER_PROFILES: list[dict] = [
    _agent_swarm_profile(),
    {
        "id": "openai",
        "display_name": "OpenAI",
        "inference_capable": True,
        "credentials": [
            {
                "name": "OPENAI_API_KEY",
                "env_vars": ["OPENAI_API_KEY"],
                "required": True,
                "auth_style": "bearer",
                "header_name": "authorization",
            }
        ],
        "endpoints": [
            {
                "host": "api.openai.com",
                "port": 443,
                "protocol": "rest",
                "access": "read-write",
                "enforcement": "enforce",
            }
        ],
        "binaries": list(_OPENCODE_PROVIDER_BINARIES),
    },
    {
        "id": "google-ai-studio",
        "display_name": "Google AI Studio",
        "inference_capable": True,
        "credentials": [
            {
                # Credential name IS the env var injected into the sandbox.
                # env_vars is used by the gateway proxy for HTTP request rewriting.
                # OpenCode and various Google AI SDKs look for GOOGLE_API_KEY,
                # GOOGLE_GENERATIVE_AI_API_KEY, or GEMINI_API_KEY. All three are
                # registered with endpoint bindings to avoid being withheld by OpenShell.
                "name": "GOOGLE_API_KEY",
                "env_vars": ["GOOGLE_API_KEY"],
                "required": False,
                "auth_style": "header",
                "header_name": "x-goog-api-key",
            },
            {
                "name": "GOOGLE_GENERATIVE_AI_API_KEY",
                "env_vars": ["GOOGLE_GENERATIVE_AI_API_KEY"],
                "required": False,
                "auth_style": "header",
                "header_name": "x-goog-api-key",
            },
            {
                "name": "GEMINI_API_KEY",
                "env_vars": ["GEMINI_API_KEY"],
                "required": False,
                "auth_style": "header",
                "header_name": "x-goog-api-key",
            },
        ],
        "endpoints": [
            {
                "host": "generativelanguage.googleapis.com",
                "port": 443,
                "protocol": "rest",
                "access": "read-write",
                "enforcement": "enforce",
            }
        ],
        "binaries": list(_OPENCODE_PROVIDER_BINARIES),
    },
    {
        "id": "jira",
        "display_name": "Jira",
        "inference_capable": False,
        "credentials": [
            # JIRA_ACCESS_TOKEN is a secret credential — the gateway stores it securely
            # and injects it as an opaque reference token (openshell:resolve:...) into
            # the sandbox via GetSandboxProviderEnvironment.
            # JIRA_SERVER_URL and JIRA_EMAIL are non-secret; they go into provider config
            # (not credentials) and the gateway injects them as plain env vars alongside
            # the credential reference tokens.
            {"name": "JIRA_ACCESS_TOKEN", "env_vars": ["JIRA_ACCESS_TOKEN"], "required": True},
        ],
        "endpoints": [
            {
                "host": "*.atlassian.net",
                "port": 443,
                "protocol": "rest",
                "access": "read-write",
                "enforcement": "enforce",
            },
            {
                "host": "redhat.atlassian.net",
                "port": 443,
                "protocol": "rest",
                "access": "read-write",
                "enforcement": "enforce",
            },
        ],
        "binaries": [
            {"path": "/usr/local/bin/jira-mcp-server"},
            {"path": "/usr/local/bin/python3.14"},
            {"path": "/usr/local/bin/python3"},
            {"path": "/usr/bin/python3"},
            {"path": "/sandbox/.venv/bin/python*"},
        ],
    },
]


def enmaas_profile_type(ws_id: int) -> str:
    """Return the workspace-scoped EnMaaS provider profile type id.

    Each workspace gets its own profile type so workspaces on a shared
    gateway can point at different EnMaaS gateway URLs without their
    ENMAAS_KEY credentials being endpoint-bound to another workspace's host.
    """
    return f"enmaas-ws-{ws_id}"


def _enmaas_profile(profile_type: str, base_url: str = "") -> dict | None:
    """Build a workspace-scoped profile bound to its configured gateway URL.

    *profile_type* is the workspace-scoped type id (``enmaas-ws-{id}``,
    see ``enmaas_profile_type``). Returns None when *base_url* is unset or
    invalid — callers surface a clear error instead of registering a profile
    with no endpoint. Sandbox network policy uses this same validated URL.
    """
    from swarmer.config import enmaas_endpoint_from_url

    endpoint = enmaas_endpoint_from_url(base_url)
    if endpoint is None:
        return None
    host, port = endpoint
    return {
        "id": profile_type,
        "display_name": "EnMaaS",
        "inference_capable": True,
        "credentials": [
            {
                # Bearer auth, mirroring the OpenAI profile — EnMaaS is a
                # distinct OpenAI-compatible gateway, not the OpenAI provider.
                "name": "ENMAAS_KEY",
                "env_vars": ["ENMAAS_KEY"],
                "required": True,
                "auth_style": "bearer",
                "header_name": "authorization",
            }
        ],
        "endpoints": [
            {
                "host": host,
                "port": port,
                "protocol": "rest",
                "access": "read-write",
                "enforcement": "enforce",
            }
        ],
        "binaries": list(_OPENCODE_PROVIDER_BINARIES),
    }


def _load_platform_provider_profiles() -> list[dict]:
    """Load gateway profiles shipped for OpenShell 0.1.4, which has no built-ins."""
    import yaml

    profile_dir = pathlib.Path(__file__).with_name("provider_profiles")
    return [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(profile_dir.glob("*.yaml"))
    ]


PLATFORM_PROVIDER_PROFILES = _load_platform_provider_profiles()


def all_provider_profiles() -> list[dict]:
    """Return every static profile Swarmer must import on a fresh gateway."""
    return [*PLATFORM_PROVIDER_PROFILES, *CUSTOM_PROVIDER_PROFILES]


def _custom_profile_for(
    profile_type: str, _config: dict | None = None, enmaas_base_url: str = ""
) -> dict | None:
    """Resolve a custom provider profile by type id, or None if unknown.

    Dynamic profiles (agent-swarm, enmaas-ws-*) are built from live
    configuration; static profiles come from CUSTOM_PROVIDER_PROFILES. The
    per-workspace EnMaaS types bind to that workspace's stored URL. Provider
    config fields cannot override the endpoint.
    """
    if profile_type == "agent-swarm":
        return _agent_swarm_profile()
    if profile_type.startswith("enmaas-ws-"):
        return _enmaas_profile(profile_type, enmaas_base_url)
    return next(
        (
            p
            for p in (*PLATFORM_PROVIDER_PROFILES, *CUSTOM_PROVIDER_PROFILES)
            if p["id"] == profile_type
        ),
        None,
    )


async def import_enmaas_profile(
    ws_id: int, client=None, base_url: str = ""
) -> bool:
    """Import (or reconcile) a workspace profile on the selected gateway.

    Endpoint bindings always come from the workspace's validated gateway
    URL (*base_url*). Returns False when it is unset or invalid.
    """
    profile = _enmaas_profile(enmaas_profile_type(ws_id), base_url)
    if profile is None:
        return False
    # URL-only workspace updates must not be treated as successful when the
    # gateway cannot reconcile an existing profile. Other profile imports can
    # retain their best-effort behavior during startup.
    await import_provider_profiles([profile], client=client, strict=True)
    return True


def _workspace_selector(workspace: str = _OPENSHELL_WORKSPACE):
    """Build a typed OpenShell 0.1.x workspace selector."""
    from openshell._proto import datamodel_pb2

    return datamodel_pb2.WorkspaceSelector(workspace=workspace)


def _set_workspace(request, workspace: str = _OPENSHELL_WORKSPACE) -> None:
    """Set the required OpenShell 0.1.x workspace selector."""
    request.workspace_scope.CopyFrom(_workspace_selector(workspace))


def _set_sandbox(request, sandbox_name: str) -> None:
    """Set the canonical OpenShell sandbox resource name."""
    request.sandbox = sandbox_name


def _require_deletion_complete(outcome: int, resource_type: str, resource_name: str) -> None:
    """Reject deletion responses that do not establish completed removal."""
    from openshell.mutations import DeletionOutcome

    result = DeletionOutcome(outcome)
    if result not in (DeletionOutcome.COMPLETED, DeletionOutcome.ALREADY_ABSENT):
        raise RuntimeError(
            f"OpenShell {resource_type} deletion for {resource_name} was not complete "
            f"(outcome={result.name})"
        )


def _provider_cache_key(client, name: str) -> tuple[str, str]:
    """Isolate provider existence state between local and remote gateways."""
    return (client._endpoint, name)


def _gateway_rpc_code(error):
    """Read status from the SDK's typed GatewayError response."""
    from openshell.errors import GatewayError

    if isinstance(error, GatewayError):
        return error.code()
    # Raw gRPC errors can still arise from a caller-supplied transport stub;
    # the SDK's own channel maps them to GatewayError before they reach here.
    import grpc

    return error.code() if isinstance(error, grpc.RpcError) else None

# Sandbox pod ephemeral-storage compute resource (container writable layer /
# unsized emptyDirs) applied to every sandbox (ACM-39804). Previously a
# per-session UI setting (ACM-38184), removed because it only bounded this
# compute resource, not the `/sandbox` working directory PVC users actually
# cared about (a separate, gateway-wide setting — see
# docs/OPENSHELL_LOCAL_SETUP.md `workspaceDefaultStorageSize`, `10Gi` by
# Makefile default but overridable per-cluster via OPENSHELL_WORKSPACE_STORAGE).
# Hardcoded to the max of the old dropdown range; cost delta vs. smaller
# values is negligible (~$0.40/mo per long-lived sandbox on AWS gp3).
SANDBOX_EPHEMERAL_STORAGE = "10Gi"


@dataclass(frozen=True)
class GatewayConfig:
    """Descriptor for connecting to an OpenShell gateway."""
    gateway_url: str
    auth_mode: str = "default"  # "default", "oidc", "bearer", "mtls", "none"
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_key: str | None = None
    tls_verify: bool = True
    bearer_token: str | None = None
    bearer_callable: Callable[[], str] | None = None
    workspace_id: int | None = None
    client_secret: str | None = None
    service_account_subject: str | None = None


def default_gateway_config() -> GatewayConfig:
    """Build GatewayConfig from global settings."""
    from swarmer.config import settings

    auth_mode = (
        "mtls"
        if settings.openshell_tls_cert
        else ("bearer" if settings.openshell_bearer_token else "default")
    )
    return GatewayConfig(
        gateway_url=settings.openshell_gateway_url,
        auth_mode=auth_mode,
        tls_ca=settings.openshell_tls_ca or None,
        tls_cert=settings.openshell_tls_cert or None,
        tls_key=settings.openshell_tls_key or None,
        tls_verify=settings.openshell_tls_verify,
        bearer_token=settings.openshell_bearer_token or None,
    )


def _tls_material_path(value: str) -> tuple[pathlib.Path, bool]:
    """Resolve a TLS material value to a filesystem path the SDK can read.

    `GatewayConfig.tls_ca/tls_cert/tls_key` carry two different shapes
    depending on the source: the global env-var settings
    (`default_gateway_config()`) already point at real files on disk, but
    per-workspace `WorkspaceGateway.tls_ca/tls_cert/tls_key` store raw PEM
    *content* in the encrypted database (ACM-41655/41656) — there is no
    filesystem path to give the openshell SDK's path-only `TlsConfig`.

    If *value* names an existing file, use it as-is. Otherwise treat it as
    inline PEM content and spool it to a private (0600) temp file so
    `TlsConfig`/`SandboxClient` can read it; the caller deletes the temp
    file once the (synchronous, constructor-time) read is done.

    Returns (path, is_temp_file).
    """
    try:
        existing = pathlib.Path(value)
        if existing.exists():
            return existing, False
    except OSError:
        pass  # e.g. ENAMETOOLONG for inline PEM content — fall through

    fd, tmp_path = tempfile.mkstemp(prefix="swarmer-tls-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
        os.chmod(tmp_path, 0o600)
    except Exception:
        os.unlink(tmp_path)
        raise
    return pathlib.Path(tmp_path), True


def get_client_for_config(config: GatewayConfig):
    """Factory — builds a SandboxClient from a GatewayConfig.

    The OpenShell SDK exposes TLS material but no certificate-verification
    toggle. ``tls_verify`` is therefore intentionally consumed only by the
    HTTP/WebSocket proxy; SDK connections always use gRPC's verified TLS.
    """
    from openshell import SandboxClient, TlsConfig  # noqa: F401 (optional dep)

    endpoint = _normalize_gateway_endpoint(config.gateway_url)
    gateway_scheme = urlparse(config.gateway_url.strip()).scheme.lower()
    tls = None
    temp_paths: list[pathlib.Path] = []
    try:
        if config.tls_cert and config.tls_key:
            ca_path = None
            if config.tls_ca:
                ca_path, ca_is_tmp = _tls_material_path(config.tls_ca)
                if ca_is_tmp:
                    temp_paths.append(ca_path)
            cert_path, cert_is_tmp = _tls_material_path(config.tls_cert)
            if cert_is_tmp:
                temp_paths.append(cert_path)
            key_path, key_is_tmp = _tls_material_path(config.tls_key)
            if key_is_tmp:
                temp_paths.append(key_path)
            tls = TlsConfig(ca_path=ca_path, cert_path=cert_path, key_path=key_path)
        elif config.tls_ca:
            ca_path, ca_is_tmp = _tls_material_path(config.tls_ca)
            if ca_is_tmp:
                temp_paths.append(ca_path)
            tls = TlsConfig(ca_path=ca_path)
        elif config.auth_mode == "oidc" or gateway_scheme in ("https", "grpcs"):
            tls = TlsConfig()

        bearer = config.bearer_callable or (config.bearer_token if config.auth_mode == "bearer" else None)
        endpoint_host = urlparse(
            config.gateway_url.strip()
            if "://" in config.gateway_url
            else f"//{config.gateway_url.strip()}"
        ).hostname
        if bearer is not None and tls is None and not _is_loopback_host(endpoint_host):
            raise ValueError("bearer-authenticated remote gateways require TLS configuration")
        # SandboxClient reads tls.*_path files synchronously in its
        # constructor (grpc.ssl_channel_credentials(...read_bytes())), so it
        # is safe to delete any temp files in `finally` below once this
        # returns — the credential bytes are already copied into the gRPC
        # channel by then.
        return SandboxClient(
            endpoint,
            tls=tls,
            bearer_token=bearer,
        )
    finally:
        for p in temp_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


async def resolve_gateway_config(
    ws_or_id: Any | None,
    db: AsyncSession | None = None,
) -> GatewayConfig:
    """Resolve GatewayConfig for a workspace or ID, falling back to default."""
    if ws_or_id is None:
        return default_gateway_config()

    from swarmer.models.workspace import Workspace
    from swarmer.models.workspace_gateway import WorkspaceGateway
    from swarmer.openshell_oidc import oidc_manager

    ws_id: int | None = None
    gw: WorkspaceGateway | None = None

    if isinstance(ws_or_id, Workspace):
        ws_id = ws_or_id.id
        if "gateway" in ws_or_id.__dict__ and ws_or_id.__dict__["gateway"] is not None:
            gw = ws_or_id.__dict__["gateway"]
    elif isinstance(ws_or_id, int):
        ws_id = ws_or_id

    if ws_id is None:
        return default_gateway_config()

    if gw is None:
        if db is not None:
            from sqlalchemy import select
            gw = (
                await db.execute(
                    select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == ws_id)
                )
            ).scalar_one_or_none()
        else:
            from swarmer.database import _AsyncSessionLocal, get_db
            if _AsyncSessionLocal is not None:
                async for s in get_db():
                    from sqlalchemy import select
                    gw = (
                        await s.execute(
                            select(WorkspaceGateway).where(WorkspaceGateway.workspace_id == ws_id)
                        )
                    ).scalar_one_or_none()
                    break

    if gw is None or not gw.gateway_url:
        return default_gateway_config()

    bearer_callable = None
    if gw.auth_mode == "oidc" and gw.oidc_issuer and gw.oidc_client_id:
        auth = oidc_manager.get_or_create(
            workspace_id=gw.workspace_id,
            issuer=gw.oidc_issuer,
            client_id=gw.oidc_client_id,
            audience=gw.oidc_audience or "",
            refresh_token=gw.refresh_token,
            access_token=gw.access_token,
            expires_at=(
                int(gw.access_token_expires_at.replace(tzinfo=timezone.utc).timestamp())
                if gw.access_token_expires_at
                else None
            ),
            client_secret=gw.client_secret,
            service_account_subject=gw.service_account_subject,
            tls_ca=gw.tls_ca,
            tls_verify=gw.tls_verify,
        )
        bearer_callable = auth.current_access_token

    return GatewayConfig(
        gateway_url=gw.gateway_url,
        auth_mode=gw.auth_mode,
        tls_ca=gw.tls_ca,
        tls_cert=gw.tls_cert,
        tls_key=gw.tls_key,
        tls_verify=gw.tls_verify,
        bearer_token=gw.bearer_token if gw.auth_mode == "bearer" else None,
        bearer_callable=bearer_callable,
        workspace_id=gw.workspace_id,
        client_secret=gw.client_secret,
        service_account_subject=gw.service_account_subject,
    )


_client_version_observed: dict[str, float] = {}


async def get_client_for_workspace(
    ws_or_id: Any | None,
    db: AsyncSession | None = None,
):
    """Build or retrieve a configured SandboxClient for the given workspace, or None for default."""
    config = await resolve_gateway_config(ws_or_id, db)
    if config.workspace_id is None:
        return None
    client = get_client_for_config(config)
    if db is not None:
        now = time.monotonic()
        if now - _client_version_observed.get(config.gateway_url, 0) >= 60:
            try:
                from swarmer.gateway_version import observe_gateway_version

                await observe_gateway_version(config, client, db)
                _client_version_observed[config.gateway_url] = now
            except Exception:
                # Version tracking must never make an otherwise healthy gateway
                # unusable, especially while upgrading mixed SDK/gateway versions.
                log.warning("OpenShell gateway version observation failed", exc_info=True)
    return client


def _get_client(gateway: GatewayConfig | None = None):
    """Internal factory — reads config or settings and returns a configured SandboxClient."""
    if gateway is not None:
        return get_client_for_config(gateway)
    return get_client_for_config(default_gateway_config())


async def probe_gateway_connectivity(config: GatewayConfig) -> dict:
    """Test connection and auth to an OpenShell gateway."""
    def _do_test() -> dict:
        client = get_client_for_config(config)
        try:
            sandboxes = client.list_all(workspace=_OPENSHELL_WORKSPACE)
            from openshell._proto import openshell_pb2

            gateway_version = ""
            try:
                info = client._stub.GetGatewayInfo(
                    openshell_pb2.GetGatewayInfoRequest(), timeout=10
                )
                gateway_version = info.gateway_version
            except Exception:
                log.debug("GetGatewayInfo failed for %s", config.gateway_url, exc_info=True)
            return {
                "status": "ok",
                "gateway_url": config.gateway_url,
                "auth_mode": config.auth_mode,
                "sandboxes_count": len(sandboxes),
                "gateway_version": gateway_version,
            }
        finally:
            client.close()
    return await asyncio.to_thread(_do_test)


def get_client(
    gateway_url: str,
    tls_ca_path: str | None = None,
    tls_cert_path: str | None = None,
    tls_key_path: str | None = None,
):
    """Public factory for e2e tests and direct usage."""
    from openshell import SandboxClient, TlsConfig  # noqa: F401 (optional dep)

    endpoint = _normalize_gateway_endpoint(gateway_url)
    tls = None
    if tls_ca_path or (tls_cert_path and tls_key_path):
        tls = TlsConfig(
            ca_path=pathlib.Path(tls_ca_path) if tls_ca_path else None,
            cert_path=pathlib.Path(tls_cert_path) if tls_cert_path else None,
            key_path=pathlib.Path(tls_key_path) if tls_key_path else None,
        )
    return SandboxClient(endpoint, tls=tls)


def _normalize_gateway_endpoint(gateway_url: str) -> str:
    """Normalize a gateway URL into gRPC endpoint form `host:port`.

    SandboxClient expects a gRPC target endpoint, not a full URL. We keep the
    original gateway URL elsewhere for HTTP proxying, but strip scheme and
    validate format here before constructing the client.
    """
    raw = (gateway_url or "").strip()
    if not raw:
        raise ValueError("Gateway URL is required.")

    if "://" in raw:
        parsed = urlparse(raw)
        if not parsed.hostname:
            raise ValueError("Gateway URL must include a hostname.")
        if parsed.path not in ("", "/") or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("Gateway URL must not include a path, query, or fragment.")
        if parsed.username or parsed.password:
            raise ValueError("Gateway URL must not include user info.")
        if parsed.port is not None:
            return f"{parsed.hostname}:{parsed.port}"
        return parsed.hostname

    if any(c in raw for c in ("/", "?", "#")):
        raise ValueError("Gateway URL must be host[:port] without path, query, or fragment.")
    return raw


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def create_provider(
    session,
    workspace_secret,
    github_pat,
    mcp_servers: list,
    extra_env: dict[str, str] | None = None,
    client=None,
) -> dict[str, str]:
    """Return workspace extra env vars to inject into the sandbox agent process.

    All credentials (AI keys, GitHub PAT, Jira token) are injected by the
    OpenShell gateway via the Provider API (ensure_provider /
    attach_sandbox_provider) — never as raw env vars here.

    The only env vars returned here are the workspace extra env vars supplied
    by the caller (arbitrary key-value pairs stored in the SQLite DB and
    decrypted before this call). These are passed to exec_command(env=) so
    they reach the agent process via ExecSandboxRequest.environment.
    """
    env_vars: dict[str, str] = {}
    if extra_env:
        env_vars.update(extra_env)
    return env_vars


async def ensure_provider(
    name: str,
    profile_type: str,
    config: dict[str, str],
    credentials: dict[str, str] | None = None,
    client=None,
    enmaas_base_url: str = "",
    strict_profile_reconciliation: bool = True,
) -> None:
    """Create or update a named provider on the gateway with its credentials.

    For static API keys the gateway stores credentials securely and injects
    them as env vars into the sandbox via GetSandboxProviderEnvironment.
    Credentials are stored server-side (returned as REDACTED); use UpdateProvider
    on subsequent launches to rotate keys.

    *enmaas_base_url* supplies the validated workspace URL for ``enmaas-ws-*``
    profile types. ``strict_profile_reconciliation`` propagates profile import
    failures for URL changes that must not be reported as successful otherwise.
    """
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    # Custom Swarmer-owned profiles are imported on the resolved client so
    # dedicated remote gateways and fresh setups behave consistently. EnMaaS
    # profiles use only the explicitly resolved workspace URL.
    custom_profile = _custom_profile_for(profile_type, config, enmaas_base_url)
    if custom_profile is None and profile_type.startswith("enmaas-ws-"):
        # Never register an endpoint-less EnMaaS profile — the workspace must
        # have a gateway URL before EnMaaS credentials can be configured
        # (ACM-48198).
        raise ValueError(
            "EnMaaS gateway URL is missing or invalid — set it in Settings > Tokens."
        )
    if custom_profile is not None:
        await import_provider_profiles(
            [custom_profile], client=client, strict=strict_profile_reconciliation
        )

    def _build_provider(req_provider):
        req_provider.metadata.name = name
        req_provider.type = profile_type
        for k, v in (config or {}).items():
            req_provider.config[k] = v
        for k, v in (credentials or {}).items():
            req_provider.credentials[k] = v

    def _do_ensure():
        create_req = openshell_pb2.CreateProviderRequest()
        _set_workspace(create_req)
        _build_provider(create_req.provider)
        try:
            client._stub.CreateProvider(create_req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) == grpc.StatusCode.ALREADY_EXISTS:
                update_req = openshell_pb2.UpdateProviderRequest()
                _set_workspace(update_req)
                _build_provider(update_req.provider)
                client._stub.UpdateProvider(update_req, timeout=client._timeout)
            else:
                raise

    await asyncio.to_thread(_do_ensure)
    # A successful create/update means the provider now definitely exists —
    # refresh the provider_exists() cache immediately instead of waiting out
    # _PROVIDER_CACHE_TTL. Without this, a page load or session launch that
    # happens right after a credential save (e.g. Gemini/Google AI Studio via
    # the secrets UI) could see a stale cached "False" and skip attaching the
    # provider that was just configured.
    _provider_cache[_provider_cache_key(client, name)] = (
        True, time.monotonic() + _PROVIDER_CACHE_TTL
    )


async def delete_provider(name: str, client=None) -> None:
    """Delete a named provider from the gateway, ignoring NOT_FOUND errors."""
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()
    cache_key = _provider_cache_key(client, name)

    def _do_delete():
        req = openshell_pb2.DeleteProviderRequest()
        req.name = name
        req.allow_missing = True
        _set_workspace(req)
        try:
            response = client._stub.DeleteProvider(req, timeout=client._timeout)
            from openshell.mutations import DeletionOutcome

            outcome = DeletionOutcome(response.outcome)
            if outcome == DeletionOutcome.ACCEPTED:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        client._stub.GetProvider(
                            openshell_pb2.GetProviderRequest(
                                name=name,
                                workspace_scope=_workspace_selector(),
                            ),
                            timeout=client._timeout,
                        )
                    except grpc.RpcError as check_exc:
                        if _gateway_rpc_code(check_exc) == grpc.StatusCode.NOT_FOUND:
                            return
                        raise
                    time.sleep(1)
                raise TimeoutError(f"OpenShell provider {name} was not deleted within 60s")
            _require_deletion_complete(outcome, "provider", name)
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                return
            raise

    await asyncio.to_thread(_do_delete)
    _provider_cache.pop(cache_key, None)  # invalidate cached existence check


async def provider_exists(name: str, client=None) -> bool:
    """Return True if a named provider exists on the gateway.

    Results are cached for _PROVIDER_CACHE_TTL seconds to avoid a gRPC round-trip
    on every page render.  The cache is invalidated by create_google_cloud_provider
    and delete_provider.
    """
    if client is None:
        client = _get_client()

    cache_key = _provider_cache_key(client, name)
    now = time.monotonic()
    cached = _provider_cache.get(cache_key)
    if cached is not None:
        result, expires_at = cached
        if now < expires_at:
            return result

    from openshell._proto import openshell_pb2
    import grpc

    def _do_check() -> bool:
        req = openshell_pb2.GetProviderRequest()
        req.name = name
        _set_workspace(req)
        try:
            client._stub.GetProvider(req, timeout=client._timeout)
            return True
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                return False
            raise

    result = await asyncio.to_thread(_do_check)
    _provider_cache[cache_key] = (result, now + _PROVIDER_CACHE_TTL)
    return result


async def provider_profile_endpoint_matches(
    profile_id: str,
    endpoint: tuple[str, int] | None,
    client=None,
) -> bool:
    """Check that a gateway provider profile has exactly the configured endpoint.

    Unlike provider existence, this check is deliberately not cached: startup
    resume must not trust a stale workspace profile after reconciliation failed.
    """
    if not endpoint:
        return False

    import grpc
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_check() -> bool:
        # Provider profiles are platform-scoped in OpenShell 0.1.x: the
        # workspace selector must be omitted for platform profiles. Sending
        # all_workspaces here fails with INVALID_ARGUMENT.
        req = openshell_pb2.GetProviderProfileRequest(id=profile_id)
        try:
            response = client._stub.GetProviderProfile(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                return False
            raise

        profile = getattr(response, "profile", None)
        endpoints = getattr(profile, "endpoints", ()) if profile is not None else ()
        actual = [
            (str(item.host).lower().rstrip("."), int(item.port))
            for item in endpoints
        ]
        expected = [(endpoint[0].lower().rstrip("."), int(endpoint[1]))]
        return actual == expected

    return await asyncio.to_thread(_do_check)


async def create_google_cloud_provider(
    name: str,
    project: str,
    location: str,
    client=None,
) -> None:
    """Delete any existing provider with this name and create a fresh google-cloud one.

    The google-cloud provider type runs a GCE metadata emulator
    inside the sandbox (127.0.0.1:8174) so GCP SDKs that bypass HTTP_PROXY can still
    obtain credentials. Config keys and credential key differ from google-vertex-ai:
      config:      project_id, region  (lowercase)
      credentials: GCP_ADC_ACCESS_TOKEN  (placeholder; refreshed by gateway)
    """
    if client is None:
        client = _get_client()
    await delete_provider(name, client=client)
    await ensure_provider(
        name, "google-cloud",
        config={"project_id": project, "region": location},
        credentials={
            "GCP_ADC_ACCESS_TOKEN": "__placeholder__",
            "GCP_SA_ACCESS_TOKEN": "__placeholder__",
        },
        client=client,
    )
    # Provider now exists — update cache so the next page load doesn't need an RPC.
    # (ensure_provider() also refreshes this cache on success; this explicit set
    # keeps the contract in place even when ensure_provider() is mocked out, as
    # in test_create_google_cloud_provider_populates_cache.)
    _provider_cache[_provider_cache_key(client, name)] = (
        True, time.monotonic() + _PROVIDER_CACHE_TTL
    )


async def configure_google_cloud_provider(
    provider_name: str,
    adc_json: str,
    client=None,
) -> None:
    """Configure a google-cloud provider with auto-refreshing credentials.

    Parses the ADC JSON to choose the appropriate gateway refresh strategy:
    - service_account → GOOGLE_SERVICE_ACCOUNT_JWT (gateway signs JWTs from SA key)
    - authorized_user → OAUTH2_REFRESH_TOKEN (gateway exchanges refresh token)

    Refresh credential keys use the names declared by the imported google-cloud
    profile: service-account credentials use ``service_account_token`` and user
    ADC uses ``adc_token``. Their profile env vars are resolved by the GCE metadata
    emulator inside the sandbox, including for GCP SDKs that bypass HTTP_PROXY.
    """
    import json as _json
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    adc = _json.loads(adc_json)
    adc_type = adc.get("type", "")

    def _do_configure():
        req = openshell_pb2.ConfigureProviderRefreshRequest()
        _set_workspace(req)
        req.provider = provider_name

        if adc_type == "service_account":
            req.credential_key = "service_account_token"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
            req.material["client_email"] = adc.get("client_email", "")
            req.material["private_key"] = adc.get("private_key", "")
            req.secret_material_keys.append("private_key")
        elif adc_type == "authorized_user":
            req.credential_key = "adc_token"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN
            req.material["client_id"] = adc.get("client_id", "")
            req.material["client_secret"] = adc.get("client_secret", "")
            req.material["refresh_token"] = adc.get("refresh_token", "")
            req.secret_material_keys.extend(["client_secret", "refresh_token"])
        else:
            raise ValueError(f"Unsupported ADC type for google-cloud provider: {adc_type!r}")

        client._stub.ConfigureProviderRefresh(req, timeout=client._timeout)

    await asyncio.to_thread(_do_configure)


async def create_vertex_provider(
    name: str,
    project: str,
    location: str,
    client=None,
) -> None:
    """Delete and recreate a google-vertex-ai provider from the imported profile."""
    await delete_provider(name, client=client)
    await ensure_provider(
        name, "google-vertex-ai",
        config={"VERTEX_AI_PROJECT_ID": project, "VERTEX_AI_REGION": location},
        credentials={
            "GOOGLE_VERTEX_AI_TOKEN": "__placeholder__",
            "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN": "__placeholder__",
            "GOOGLE_SERVICE_ACCOUNT_KEY": "__placeholder__",
        },
        client=client,
    )


async def configure_provider_credential(
    provider_name: str,
    credential_key: str,
    credential_value: str,
    client=None,
) -> None:
    """Store a static credential on a gateway-managed provider."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_configure():
        req = openshell_pb2.ConfigureProviderRefreshRequest()
        _set_workspace(req)
        req.provider = provider_name
        req.credential_key = credential_key
        req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_STATIC
        req.material["value"] = credential_value
        req.secret_material_keys.append("value")
        client._stub.ConfigureProviderRefresh(req, timeout=client._timeout)

    await asyncio.to_thread(_do_configure)


async def configure_vertex_provider(
    provider_name: str,
    adc_json: str,
    project: str,
    location: str,
    client=None,
) -> None:
    """Configure a google-vertex-ai provider with auto-refreshing credentials.

    Parses the ADC JSON to choose the appropriate gateway refresh strategy:
    - service_account → GOOGLE_SERVICE_ACCOUNT_JWT (gateway signs JWTs from SA key)
    - authorized_user → OAUTH2_REFRESH_TOKEN (gateway exchanges refresh token)

    Also stores project/location as non-secret config on the provider so the
    gateway injects them as env vars (GOOGLE_CLOUD_PROJECT, VERTEXAI_PROJECT,
    VERTEX_LOCATION, VERTEXAI_LOCATION) alongside the access token.
    """
    import json as _json
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    adc = _json.loads(adc_json)
    adc_type = adc.get("type", "")

    def _do_configure():
        req = openshell_pb2.ConfigureProviderRefreshRequest()
        _set_workspace(req)
        req.provider = provider_name

        if adc_type == "service_account":
            # The google-vertex-ai profile's service-account credential is distinct
            # from its user ADC credential.
            # Gateway refreshes via GOOGLE_SERVICE_ACCOUNT_JWT and injects the
            # profile's service_account_token as the Vertex service-account env var.
            req.credential_key = "service_account_token"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
            req.material["client_email"] = adc.get("client_email", "")
            req.material["private_key"] = adc.get("private_key", "")
            req.secret_material_keys.append("private_key")
        elif adc_type == "authorized_user":
            # The google-vertex-ai profile's ADC credential key is gcloud_adc_token.
            # Gateway refreshes via OAUTH2_REFRESH_TOKEN (token_url and scopes are
            # defined in the built-in profile, not in the material).
            req.credential_key = "gcloud_adc_token"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN
            req.material["client_id"] = adc.get("client_id", "")
            req.material["client_secret"] = adc.get("client_secret", "")
            req.material["refresh_token"] = adc.get("refresh_token", "")
            req.secret_material_keys.extend(["client_secret", "refresh_token"])
        else:
            raise ValueError(f"Unsupported ADC type for VertexAI provider: {adc_type!r}")

        client._stub.ConfigureProviderRefresh(req, timeout=client._timeout)

    await asyncio.to_thread(_do_configure)


async def attach_sandbox_provider(sandbox_name: str, provider_name: str, client=None) -> None:
    """Attach a pre-configured gateway provider to a sandbox."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_attach():
        req = openshell_pb2.AttachSandboxProviderRequest(
            workspace_scope=_workspace_selector(),
            sandbox=sandbox_name,
            provider=provider_name,
        )
        client._stub.AttachSandboxProvider(req, timeout=client._timeout)

    await asyncio.to_thread(_do_attach)


async def detach_sandbox_provider(sandbox_name: str, provider_name: str, client=None) -> None:
    """Detach a provider from a sandbox, ignoring NOT_FOUND errors."""
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _do_detach():
        req = openshell_pb2.DetachSandboxProviderRequest(
            workspace_scope=_workspace_selector(),
            sandbox=sandbox_name,
            provider=provider_name,
        )
        try:
            client._stub.DetachSandboxProvider(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) in (
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.FAILED_PRECONDITION,
            ):
                return
            raise

    await asyncio.to_thread(_do_detach)


async def approve_draft_policy_chunks(
    sandbox_name: str,
    expected_hosts: set[str] | None = None,
    client=None,
) -> list[str]:
    """Approve pending draft policy chunks for known/expected endpoints only.

    The supervisor observes denied network connections and proposes policy rules
    (draft chunks). This function approves only chunks whose endpoints match
    ``expected_hosts`` — arbitrary/unexpected endpoints are left pending and
    logged as warnings for human review.

    Returns a list of unexpected host names that were NOT approved.
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _host_matches(host: str, expected: set[str]) -> bool:
        """True if ``host`` matches any pattern in ``expected`` (supports leading *)."""
        for pattern in expected:
            if pattern.startswith("*."):
                if host.endswith(pattern[1:]) or host == pattern[2:]:
                    return True
            elif host == pattern:
                return True
        return False

    def _do_approve():
        try:
            req = openshell_pb2.GetDraftPolicyRequest()
            _set_sandbox(req, sandbox_name)
            _set_workspace(req)
            dp = client._stub.GetDraftPolicy(req, timeout=10)
            pending = [c for c in dp.chunks if c.status == "pending"]
            if not pending:
                return 0, []

            if expected_hosts is None:
                # No filter: approve all (fallback/permissive mode)
                to_approve = pending
                unexpected = []
            else:
                to_approve = []
                unexpected = []
                for chunk in pending:
                    chunk_hosts = [ep.host for ep in chunk.proposed_rule.endpoints]
                    if all(_host_matches(h, expected_hosts) for h in chunk_hosts):
                        to_approve.append(chunk)
                    else:
                        unexpected.extend(
                            h for h in chunk_hosts if not _host_matches(h, expected_hosts)
                        )

            if unexpected:
                log.warning(
                    "sandbox %s: %d unexpected endpoint(s) not approved: %s",
                    sandbox_name, len(unexpected), unexpected
                )

            if not to_approve:
                return 0, unexpected

            # Approve individual chunks that match expectations
            approved_count = 0
            for chunk in to_approve:
                try:
                    req = openshell_pb2.ApproveDraftChunkRequest(chunk_id=chunk.id)
                    _set_sandbox(req, sandbox_name)
                    _set_workspace(req)
                    client._stub.ApproveDraftChunk(
                        req, timeout=10
                    )
                    approved_count += 1
                except Exception as exc:
                    log.warning("Failed to approve chunk %s: %s", chunk.rule_name, exc)

            return approved_count, unexpected
        except Exception as exc:
            log.warning("approve_draft_policy_chunks failed for %s: %s", sandbox_name, exc)
            return 0, []

    approved, unexpected = await asyncio.to_thread(_do_approve)
    if approved:
        log.info("sandbox %s: approved %d draft policy chunk(s)", sandbox_name, approved)
        await asyncio.sleep(2)  # let supervisor apply the new policy
    return unexpected


async def get_draft_chunks(sandbox_name: str, client=None) -> list[dict]:
    """Fetch all draft policy chunks for a sandbox and return them as serializable dicts.

    Each dict has the shape:
      {
        "id": str,
        "status": "pending" | "approved" | "rejected",
        "rule_name": str,
        "endpoints": [{"host": str, "port": int, "protocol": str}, ...],
        "binaries": [{"path": str}, ...],
      }

    Returns [] if the sandbox does not exist, the gateway is unreachable, or
    any other error occurs — callers should treat an empty list as "no chunks".
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_fetch() -> list[dict]:
        try:
            req = openshell_pb2.GetDraftPolicyRequest()
            _set_sandbox(req, sandbox_name)
            _set_workspace(req)
            dp = client._stub.GetDraftPolicy(req, timeout=10)
            result = []
            for chunk in dp.chunks:
                endpoints = [
                    {
                        "host": ep.host,
                        "port": ep.port,
                        "protocol": ep.protocol or "rest",
                    }
                    for ep in chunk.proposed_rule.endpoints
                ]
                binaries = [
                    {"path": b.path}
                    for b in chunk.proposed_rule.binaries
                ]
                result.append({
                    "id": chunk.id,
                    "status": chunk.status or "pending",
                    "rule_name": chunk.rule_name,
                    "endpoints": endpoints,
                    "binaries": binaries,
                })
            return result
        except Exception as exc:
            import grpc

            # NOT_FOUND is expected for TUI/server sessions with no policy chunks.
            if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                log.debug("get_draft_chunks: sandbox %s not found (no chunks yet)", sandbox_name)
            else:
                log.warning("get_draft_chunks failed for %s: %s", sandbox_name, exc)
            return []

    return await asyncio.to_thread(_do_fetch)


async def approve_chunks_by_id(
    sandbox_name: str,
    chunk_ids: list[str],
    client=None,
) -> int:
    """Approve specific draft chunks by their IDs immediately on a running sandbox.

    Used when a user promotes a chunk via the Policy tab while the sandbox is
    active. Calls ApproveDraftChunk for each supplied chunk ID, merging the rule
    into the sandbox's active policy without a restart.

    Returns the count of chunks successfully approved. Logs warnings on
    individual failures but does not raise — the rule is already persisted in
    the DB and will apply on next launch regardless.
    """
    from openshell._proto import openshell_pb2

    if not chunk_ids:
        return 0

    if client is None:
        client = _get_client()

    def _do_approve() -> int:
        approved = 0
        for chunk_id in chunk_ids:
            try:
                client._stub.ApproveDraftChunk(
                    openshell_pb2.ApproveDraftChunkRequest(
                        workspace_scope=_workspace_selector(),
                        sandbox=sandbox_name,
                        chunk_id=chunk_id,
                    ),
                    timeout=10,
                )
                approved += 1
            except Exception as exc:
                log.warning(
                    "approve_chunks_by_id: failed to approve chunk %s on sandbox %s: %s",
                    chunk_id, sandbox_name, exc,
                )
        return approved

    approved = await asyncio.to_thread(_do_approve)
    if approved:
        log.info(
            "sandbox %s: live-applied %d/%d chunk(s) from Policy tab",
            sandbox_name, approved, len(chunk_ids),
        )
        await asyncio.sleep(1)  # let supervisor apply the new policy
    return approved


async def undo_chunks_by_rule_name(
    sandbox_name: str,
    rule_names: list[str],
    chunk_ids: list[str] | None = None,
    client=None,
) -> int:
    """Undo (revoke) approved draft chunks for the given rule names on a running sandbox.

    When a user deletes a Net Rule via the Policy tab while the sandbox is
    active, this removes the rule from the sandbox's active policy immediately.

    Strategy:
    1. If ``chunk_ids`` are provided (stored at promote-time), call UndoDraftChunk
       directly by ID — fast and precise.
    2. Otherwise, call GetDraftHistory to find approved chunk IDs by rule_name,
       then call UndoDraftChunk on each. This handles rules promoted in earlier
       sessions or before chunk_id tracking was added.
    3. If no chunk is found via either path, the rule was baked into the
       startup policy (not approved via draft mechanism) and cannot be revoked
       live — caller receives 0 and should inform the user it takes effect on
       next launch.

    Returns the count of chunks successfully undone.
    """
    from openshell._proto import openshell_pb2

    if not rule_names:
        return 0

    if client is None:
        client = _get_client()

    def _do_undo() -> int:
        ids_to_undo: list[str] = list(chunk_ids or [])

        # If no IDs supplied, look them up from draft history by rule_name.
        if not ids_to_undo:
            rule_name_set = set(rule_names)
            try:
                req = openshell_pb2.GetDraftHistoryRequest()
                _set_sandbox(req, sandbox_name)
                _set_workspace(req)
                history = client._stub.GetDraftHistory(req, timeout=10)
                for entry in history.chunks:
                    if entry.rule_name in rule_name_set and entry.status == "approved":
                        ids_to_undo.append(entry.id)
            except Exception as exc:
                log.warning(
                    "undo_chunks_by_rule_name: GetDraftHistory failed for %s: %s",
                    sandbox_name, exc,
                )
                return 0

        if not ids_to_undo:
            # Rule was part of startup policy — cannot revoke live.
            log.info(
                "sandbox %s: rule(s) %s were baked into startup policy; "
                "revocation will take effect on next launch",
                sandbox_name, rule_names,
            )
            return 0

        undone = 0
        for chunk_id in ids_to_undo:
            try:
                req = openshell_pb2.UndoDraftChunkRequest(chunk_id=chunk_id)
                _set_sandbox(req, sandbox_name)
                _set_workspace(req)
                client._stub.UndoDraftChunk(
                    req,
                    timeout=10,
                )
                undone += 1
            except Exception as exc:
                log.warning(
                    "undo_chunks_by_rule_name: failed to undo chunk %s on sandbox %s: %s",
                    chunk_id, sandbox_name, exc,
                )
        return undone

    undone = await asyncio.to_thread(_do_undo)
    if undone:
        log.info(
            "sandbox %s: live-revoked %d/%d chunk(s) from Policy tab delete",
            sandbox_name, undone, len(rule_names),
        )
        await asyncio.sleep(1)  # let supervisor apply the policy change
    return undone


def _build_provider_profile(p: dict, resource_version: int = 0):
    """Translate a provider profile dict to an openshell_pb2.ProviderProfile protobuf."""
    from google.protobuf.json_format import ParseDict
    from openshell._proto import openshell_pb2

    categories = {
        "other": "PROVIDER_PROFILE_CATEGORY_OTHER",
        "inference": "PROVIDER_PROFILE_CATEGORY_INFERENCE",
        "agent": "PROVIDER_PROFILE_CATEGORY_AGENT",
        "source_control": "PROVIDER_PROFILE_CATEGORY_SOURCE_CONTROL",
        "messaging": "PROVIDER_PROFILE_CATEGORY_MESSAGING",
        "data": "PROVIDER_PROFILE_CATEGORY_DATA",
        "knowledge": "PROVIDER_PROFILE_CATEGORY_KNOWLEDGE",
    }
    category = p.get("category", "inference")
    category = categories.get(category, category)
    profile = openshell_pb2.ProviderProfile(
        id=p["id"],
        display_name=p.get("display_name", p["id"]),
        description=p.get("description", ""),
        category=category,
        inference_capable=p.get("inference_capable", True),
    )
    if resource_version:
        profile.resource_version = int(resource_version)
    for cred in p.get("credentials", []):
        c = openshell_pb2.ProviderProfileCredential(
            name=cred["name"],
            description=cred.get("description", ""),
            required=cred.get("required", True),
            auth_style=cred.get("auth_style", ""),
            header_name=cred.get("header_name", ""),
            query_param=cred.get("query_param", ""),
            stable_placeholder=cred.get("stable_placeholder", False),
        )
        for ev in cred.get("env_vars", []):
            c.env_vars.append(ev)
        refresh = cred.get("refresh")
        if refresh:
            strategies = {
                "google_service_account_jwt": "PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT",
                "oauth2_refresh_token": "PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN",
                "oauth2_client_credentials": "PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_CLIENT_CREDENTIALS",
                "aws_sts_assume_role": "PROVIDER_CREDENTIAL_REFRESH_STRATEGY_AWS_STS_ASSUME_ROLE",
            }
            strategy = refresh.get("strategy", "")
            c.refresh.strategy = strategies.get(strategy, strategy)
            c.refresh.token_url = refresh.get("token_url", "")
            for sc in refresh.get("scopes", []):
                c.refresh.scopes.append(sc)
            if refresh.get("refresh_before_seconds") is not None:
                c.refresh.refresh_before.FromSeconds(int(refresh["refresh_before_seconds"]))
            if refresh.get("max_lifetime_seconds") is not None:
                c.refresh.max_lifetime.FromSeconds(int(refresh["max_lifetime_seconds"]))
            for mat in refresh.get("material", []):
                m = c.refresh.material.add()
                m.name = mat["name"]
                m.description = mat.get("description", "")
                m.required = mat.get("required", True)
                m.secret = mat.get("secret", False)
        profile.credentials.append(c)
    enforcement_modes = {
        "enforce": "NETWORK_ENFORCEMENT_MODE_ENFORCE",
        "audit": "NETWORK_ENFORCEMENT_MODE_AUDIT",
    }
    access_presets = {
        "read-only": "NETWORK_ACCESS_PRESET_READ_ONLY",
        "read-write": "NETWORK_ACCESS_PRESET_READ_WRITE",
        "full": "NETWORK_ACCESS_PRESET_FULL",
    }
    for endpoint in p.get("endpoints", []):
        typed_endpoint = dict(endpoint)
        typed_endpoint.pop("tls", None)
        if typed_endpoint.get("enforcement") in enforcement_modes:
            typed_endpoint["enforcement"] = enforcement_modes[typed_endpoint["enforcement"]]
        if typed_endpoint.get("access") in access_presets:
            typed_endpoint["access"] = access_presets[typed_endpoint["access"]]
        ParseDict(typed_endpoint, profile.endpoints.add())
    for binary in p.get("binaries", []):
        binary_path = binary if isinstance(binary, str) else binary["path"]
        profile.binaries.add(path=binary_path)
    for credential_name in p.get("discovery", {}).get("credentials", []):
        profile.discovery.credentials.append(credential_name)
    return profile


async def import_provider_profiles(
    profiles: list[dict], client=None, strict: bool = True
) -> None:
    """Import custom provider type profiles into the gateway (idempotent).

    If profiles already exist, updates each profile via UpdateProviderProfiles
    so new or modified endpoints/credentials take effect on running gateways.
    In strict mode, reconciliation errors are propagated instead of logged so
    callers do not persist configuration that the gateway did not apply.
    """
    import grpc
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_import():
        # Provider profiles are platform-scoped in OpenShell 0.1.x: the
        # workspace selector must be omitted for platform profiles. Sending
        # all_workspaces fails with INVALID_ARGUMENT ("all_workspaces is not
        # supported by this request"), which broke every credential save.
        req = openshell_pb2.ImportProviderProfilesRequest()
        for p in profiles:
            profile = _build_provider_profile(p)
            req.profiles.append(openshell_pb2.ProviderProfileImportItem(profile=profile, source="swarmer"))
        try:
            client._stub.ImportProviderProfiles(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) != grpc.StatusCode.ALREADY_EXISTS:
                raise

        # An import can be a no-op when a profile already exists. Reconcile every
        # imported profile using its current resource version.
        for p in profiles:
            pid = p["id"]
            try:
                get_req = openshell_pb2.GetProviderProfileRequest(id=pid)
                existing = client._stub.GetProviderProfile(
                    get_req, timeout=client._timeout
                )
            except grpc.RpcError as exc:
                if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                    if strict:
                        raise RuntimeError(
                            f"OpenShell did not register imported provider profile {pid}"
                        ) from exc
                    log.warning(
                        "import_provider_profiles: imported profile %s is not visible",
                        pid,
                    )
                    continue
                if strict:
                    raise
                log.warning("import_provider_profiles: failed to read profile %s: %s", pid, exc)
                continue

            try:
                rv = existing.profile.resource_version
                updated_profile = _build_provider_profile(p, resource_version=rv)
                item = openshell_pb2.ProviderProfileImportItem(profile=updated_profile, source="swarmer")
                up_req = openshell_pb2.UpdateProviderProfilesRequest(
                    id=pid,
                    profile=item,
                    expected_resource_version=rv,
                )
                client._stub.UpdateProviderProfiles(up_req, timeout=client._timeout)
            except Exception as exc:
                if strict:
                    raise
                log.warning("import_provider_profiles: failed to update profile %s: %s", pid, exc)

    await asyncio.to_thread(_do_import)


async def create_provider_from_env(
    google_api_key: str,
    github_pat: str,
    client=None,
) -> dict[str, str]:
    """Build env-var dict from explicit credential values (used by e2e tests)."""
    env_vars: dict[str, str] = {}
    if google_api_key:
        env_vars["GOOGLE_API_KEY"] = google_api_key
    if github_pat:
        env_vars["GITHUB_PAT"] = github_pat
    return env_vars


async def create_sandbox(
    image: str,
    env_vars: dict[str, str] | None,
    policy,
    provider_names: list[str] | None = None,
    client=None,
):
    """Create an OpenShell sandbox and wait for it to be ready.

    provider_names lists pre-configured gateway providers to attach at creation
    time so the supervisor can call GetSandboxProviderEnvironment at startup
    and receive injected reference tokens before any exec commands run.

    Every sandbox's ephemeral-storage compute resource (container writable layer /
    unsized emptyDirs) is set to SANDBOX_EPHEMERAL_STORAGE (ACM-39804). This is
    distinct from the `/sandbox` working directory, which is a separate PVC sized
    gateway-wide (see docs/OPENSHELL_LOCAL_SETUP.md).

    Returns the SandboxRef; caller stores ref.name as session.sandbox_name.
    """
    from openshell._proto import openshell_pb2  # noqa: F401 (optional dep)

    if client is None:
        client = _get_client()

    spec = openshell_pb2.SandboxSpec()
    if image:
        spec.template.image = image
    for k, v in (env_vars or {}).items():
        spec.environment[k] = v
    for pname in (provider_names or []):
        spec.providers.append(pname)
    if policy is not None:
        spec.policy.CopyFrom(policy)

    from google.protobuf import struct_pb2

    resources = struct_pb2.Struct()
    resources.update({
        "requests": {"ephemeral-storage": SANDBOX_EPHEMERAL_STORAGE},
        "limits": {"ephemeral-storage": SANDBOX_EPHEMERAL_STORAGE},
    })
    spec.template.resources.CopyFrom(resources)

    def _do_create():
        return client.create(workspace=_OPENSHELL_WORKSPACE, spec=spec)

    ref = await asyncio.to_thread(_do_create)
    await _wait_sandbox_ready(ref.name, client=client)
    return ref


async def _exec_with_supervisor_retry(fn, *, max_attempts: int = 8, base_delay: float = 2.0) -> Any:
    """Run a blocking exec callable, retrying on transient sandbox-not-ready errors.

    The OpenShell gateway reports a sandbox as Ready before the supervisor process
    has fully established its relay session.  Provider types that start extra
    processes inside the sandbox (e.g. the GCE metadata emulator started by the
    google-cloud provider can widen this window. Exec
    RPCs issued in this window fail with one of two transient errors:

        StatusCode.UNAVAILABLE   — "supervisor relay failed: supervisor session not connected"
        StatusCode.FAILED_PRECONDITION — "sandbox is not ready"

    Both are retried with exponential back-off (2 s → 4 s → 8 s … capped at 30 s).
    """
    import grpc

    attempt = 0
    delay = base_delay
    while True:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            attempt += 1
            is_transient = (
                isinstance(exc, grpc.RpcError)
                and (
                    (
                        _gateway_rpc_code(exc) == grpc.StatusCode.UNAVAILABLE
                        and "supervisor session not connected" in (exc.details() or "")
                    )
                    or (
                        _gateway_rpc_code(exc) == grpc.StatusCode.FAILED_PRECONDITION
                        and "sandbox is not ready" in (exc.details() or "")
                    )
                )
            )
            if is_transient and attempt < max_attempts:
                log.warning(
                    "_exec_with_supervisor_retry: sandbox not ready (attempt %d/%d, %s), "
                    "retrying in %.1fs",
                    attempt, max_attempts, exc.details(), delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            else:
                raise


async def _wait_sandbox_ready(
    sandbox_name: str,
    client=None,
    timeout: float = 300.0,
    poll_interval: float = 2.0,
) -> None:
    """Wait for OpenShell 0.1.x to report the sandbox ready."""

    if client is None:
        client = _get_client()

    del poll_interval  # readiness polling cadence is owned by the 0.1.x SDK
    await asyncio.to_thread(
        client.wait_ready,
        sandbox_name,
        workspace=_OPENSHELL_WORKSPACE,
        timeout_seconds=timeout,
    )


async def delete_sandbox(sandbox_name: str, client=None) -> None:
    """Delete an OpenShell sandbox."""
    if client is None:
        client = _get_client()

    def _do_delete():
        from openshell.mutations import DeletionOutcome

        result = client.delete(
            sandbox_name,
            workspace=_OPENSHELL_WORKSPACE,
            allow_missing=True,
        )
        if result.outcome == DeletionOutcome.ACCEPTED:
            client.wait_deleted(
                sandbox_name,
                workspace=_OPENSHELL_WORKSPACE,
                expected_sandbox_id=result.sandbox_id,
            )
        else:
            _require_deletion_complete(result.outcome, "sandbox", sandbox_name)

    await asyncio.to_thread(_do_delete)


async def list_sandboxes(client=None) -> list[str]:
    """Return names of all live sandboxes from the OpenShell gateway."""
    if client is None:
        client = _get_client()

    def _do_list():
        return client.list_all(workspace=_OPENSHELL_WORKSPACE)

    refs = await asyncio.to_thread(_do_list)
    return [ref.name for ref in refs]


async def _sandbox_id(sandbox_name: str, client=None) -> str:
    """Resolve sandbox name → id (needed by the exec RPC)."""
    if client is None:
        client = _get_client()
    ref = await asyncio.to_thread(
        client.get, sandbox_name, workspace=_OPENSHELL_WORKSPACE
    )
    return ref.id



async def write_agent_config(
    sandbox_name: str,
    tool_name: str,
    config_json: str,
    client=None,
) -> None:
    """Write agent config JSON to /sandbox/opencode.json, loaded via the
    OPENCODE_CONFIG environment variable at startup (OpenCode has no --config
    CLI flag).

    Uses stdin to deliver file content so the gateway's no-newline-in-args
    restriction is never hit.

    tool_name is currently unused (OpenCode is the only supported agent tool)
    but kept in the signature for call-site stability.
    """
    if client is None:
        client = _get_client()
    
    # Write to /sandbox/opencode.json — loaded via the OPENCODE_CONFIG env var
    # (set at launch time) so OpenCode picks it up regardless of HOME or
    # working directory.
    dest = shlex.quote("/sandbox/opencode.json")
    script = f"cat > {dest}"

    def _do_write():
        client.exec(
            sandbox_name,
            ["sh", "-c", script],
            workspace=_OPENSHELL_WORKSPACE,
            stdin=config_json.encode(),
        )

    await _exec_with_supervisor_retry(_do_write)


async def write_agents_md(sandbox_name: str, content: str, client=None) -> None:
    """Write content to /sandbox/AGENTS.md via stdin (avoids newline-in-arg restriction)."""
    if client is None:
        client = _get_client()

    def _do_write():
        client.exec(
            sandbox_name,
            ["sh", "-c", "cat > /sandbox/AGENTS.md"],
            workspace=_OPENSHELL_WORKSPACE,
            stdin=content.encode(),
        )

    await _exec_with_supervisor_retry(_do_write)


async def write_file(sandbox_name: str, path: str, content: str, client=None) -> None:
    """Write arbitrary content to a file inside the sandbox via stdin."""
    if client is None:
        client = _get_client()
    parent = shlex.quote(str(pathlib.Path(path).parent))
    dest = shlex.quote(path)
    script = f"mkdir -p {parent} && cat > {dest}"

    def _do_write():
        client.exec(
            sandbox_name,
            ["sh", "-c", script],
            workspace=_OPENSHELL_WORKSPACE,
            stdin=content.encode(),
        )

    await _exec_with_supervisor_retry(_do_write)


async def start_agent(
    sandbox_name: str,
    cmd: list[str],
    env: dict[str, str] | None = None,
    client=None,
) -> None:
    """Start the agent as a detached background process so exec() returns immediately.

    Uses workdir="/sandbox" on the ExecSandboxRequest — identical to how
    exec_interactive sets workdir for TUI sessions — so the agent process starts
    in the correct directory without relying on a shell-level cd.
    """
    if client is None:
        client = _get_client()

    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    bg_cmd = ["sh", "-c", f"nohup {shell_cmd} >/sandbox/.agent.log 2>&1 &"]

    def _do_start():
        client.exec(
            sandbox_name,
            bg_cmd,
            workspace=_OPENSHELL_WORKSPACE,
            env=env or {},
            workdir="/sandbox",
        )

    await asyncio.to_thread(_do_start)


async def read_opencode_response(sandbox_name: str, client=None) -> str:
    """Read the last assistant message from OpenCode's SQLite DB.

    OpenCode V2 stores messages in the ``session_message`` table with the full
    payload (including the ``content`` list of text parts) in ``data``. V1
    stored parts in ``part`` joined to ``message``; that query is kept as a
    fallback for sandboxes still running a V1 image. Both read
    /sandbox/.opencode/opencode.db — the same file V2 writes through the
    ~/.local/share/opencode symlink.
    """

    if client is None:
        client = _get_client()

    # Write the reader script via stdin (no newlines in command arg) then execute it.
    reader = b"""
import sqlite3, json
def read_v2(conn):
    rows = conn.execute('''
        SELECT data FROM session_message
        WHERE type = 'assistant'
        ORDER BY time_created ASC, seq ASC
    ''').fetchall()
    texts = []
    for (data,) in rows:
        try:
            msg = json.loads(data)
        except Exception:
            continue
        for part in (msg.get('content') or []):
            if isinstance(part, dict) and part.get('type') == 'text':
                texts.append(part.get('text', ''))
    return texts
def read_v1(conn):
    rows = conn.execute('''
        SELECT p.data FROM part p
        JOIN message m ON p.message_id = m.id
        WHERE json_extract(m.data, '$.role') = 'assistant'
          AND json_extract(p.data, '$.type') = 'text'
        ORDER BY p.time_created ASC
    ''').fetchall()
    return [json.loads(r[0]).get('text', '') for r in rows if r[0]]
try:
    conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
    conn.execute('PRAGMA wal_checkpoint(FULL)')
    texts = []
    try:
        texts = read_v2(conn)
    except Exception:
        texts = []
    if not texts:
        try:
            texts = read_v1(conn)
        except Exception:
            texts = []
    out = '\\n'.join(t for t in texts if t.strip())
    print(out[:64000] if out else '', end='')
    conn.close()
except Exception as exc:
    import sys; print(f'DB_ERR:{exc}', file=sys.stderr, end='')
"""

    def _do_read():
        try:
            client.exec(
                sandbox_name,
                ["sh", "-c", "cat > /tmp/_oc_read.py"],
                workspace=_OPENSHELL_WORKSPACE,
                stdin=reader,
                timeout_seconds=10,
            )
            result = client.exec(
                sandbox_name,
                ["python3", "/tmp/_oc_read.py"],
                workspace=_OPENSHELL_WORKSPACE,
                timeout_seconds=15,
            )
            if result.stderr and "DB_ERR:" in result.stderr:
                log.warning("read_opencode_response: %s  sandbox=%s", result.stderr.strip(), sandbox_name)
            return (result.stdout or "").strip()
        except Exception as exc:
            log.warning("read_opencode_response failed for sandbox %s: %s", sandbox_name, exc)
            return ""

    return await asyncio.to_thread(_do_read)


async def exec_command(
    sandbox_name: str,
    cmd: list[str],
    client=None,
    stdin: bytes | None = None,
    timeout_seconds: int | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    """Execute a command inside the sandbox; returns ExecResult (.stdout, .stderr, .exit_code).

    env: extra environment variables to inject into this exec process.
    Unlike spec.environment (which is stored on the sandbox but NOT forwarded to
    exec calls by the OpenShell gateway), env vars passed here are sent as part of
    ExecSandboxRequest.environment and ARE visible to the spawned process.
    """
    if client is None:
        client = _get_client()

    def _do_exec():
        return client.exec(
            sandbox_name,
            cmd,
            workspace=_OPENSHELL_WORKSPACE,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            env=env or {},
        )

    return await _exec_with_supervisor_retry(_do_exec)


async def exec_command_streaming(
    sandbox_name: str,
    cmd: list[str],
    on_output: Callable[[str], Awaitable[None]] | None = None,
    poll_interval: float = 5.0,
    env: dict[str, str] | None = None,
    client=None,
) -> Any:
    """Execute a command with incremental output updates via an async callback.

    Uses the SDK's exec_stream() (gRPC unary-stream) so chunks arrive as they are
    produced rather than waiting for the command to complete.  Every *poll_interval*
    seconds the accumulated stdout+stderr is passed to *on_output* so callers can
    persist partial output to the database while the agent is still running.

    Returns the final ExecResult (.stdout, .stderr, .exit_code) once the command exits.

    env: extra env vars forwarded via ExecSandboxRequest.environment (same semantics
         as exec_command — NOT the sandbox spec environment).
    """
    import threading

    if client is None:
        client = _get_client()

    # Shared state between the reader thread and the asyncio polling task.
    _buf_lock = threading.Lock()
    _buf: list[str] = []          # accumulated output chunks
    _done_event = asyncio.Event() # set by reader thread when exec_stream exhausted
    _result_holder: list[Any] = []

    loop = asyncio.get_event_loop()

    def _stream_reader():
        """Blocking thread: consume exec_stream() and accumulate output."""
        try:
            # timeout_seconds=0 (the SDK default when omitted) lets the server use its
            # own session limit (typically 5 min).  Pass a large value so the server
            # allows the agent to run for a full task, and so the SDK computes
            # grpc_deadline = timeout_seconds + 10 instead of the 30 s client default.
            _NO_TIMEOUT = 7200  # 2 h — generous ceiling for complex agent runs
            for item in client.exec_stream(
                sandbox_name,
                cmd,
                workspace=_OPENSHELL_WORKSPACE,
                env=env or {},
                timeout_seconds=_NO_TIMEOUT,
            ):
                # ExecChunk has .stream ("stdout"/"stderr") and .data (bytes)
                chunk_data = getattr(item, "data", None)
                if chunk_data is not None:
                    text = chunk_data.decode("utf-8", errors="replace")
                    with _buf_lock:
                        _buf.append(text)
                else:
                    # ExecResult — final item; capture it
                    _result_holder.append(item)
        except Exception as exc:
            log.warning("exec_command_streaming: stream error for %s: %s", sandbox_name, exc)
        finally:
            loop.call_soon_threadsafe(_done_event.set)

    reader_thread = threading.Thread(target=_stream_reader, daemon=True)
    reader_thread.start()

    # Asyncio polling task: call on_output every poll_interval seconds.
    async def _poll_output():
        if on_output is None:
            return
        while not _done_event.is_set():
            await asyncio.sleep(poll_interval)
            with _buf_lock:
                accumulated = "".join(_buf)
            if accumulated:
                try:
                    await on_output(accumulated)
                except Exception:
                    log.warning("exec_command_streaming: on_output callback failed", exc_info=True)
        # Final callback after the stream ends (picks up any last chunks).
        with _buf_lock:
            accumulated = "".join(_buf)
        if accumulated:
            try:
                await on_output(accumulated)
            except Exception:
                log.warning("exec_command_streaming: final on_output callback failed", exc_info=True)

    poll_task = asyncio.create_task(_poll_output())

    # Wait for the stream reader thread to finish.
    await _done_event.wait()
    await poll_task  # let the final callback fire

    reader_thread.join(timeout=5)

    return _result_holder[0] if _result_holder else None


async def get_sandbox_provider_environment(sandbox_id: str, client=None) -> dict[str, str]:
    """Fetch the provider-injected environment for a sandbox from the gateway.

    Returns a dict of env var name → value (opaque reference tokens for credentials).
    Returns {} when the gateway reports that the sandbox has no provider environment;
    transport and authentication errors propagate to the caller.
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_get():
        req = openshell_pb2.GetSandboxProviderEnvironmentRequest(sandbox_id=sandbox_id)
        resp = client._stub.GetSandboxProviderEnvironment(req, timeout=10)
        return dict(resp.environment)

    import grpc
    from openshell.errors import GatewayError

    try:
        return await asyncio.to_thread(_do_get)
    except GatewayError as exc:
        if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
            return {}
        raise


def exec_interactive(
    sandbox_name: str,
    command: list[str],
    cols: int,
    rows: int,
    env: dict[str, str] | None = None,
    client=None,
):
    """Open an interactive PTY exec stream for a sandbox (for TUI WebSocket bridge).

    The sandbox identifier is its canonical resource name, not its internal ID.

    Returns (response_iterator, input_queue) where:
    - response_iterator: gRPC stream of ExecSandboxEvent messages
    - input_queue: thread-safe queue.Queue — put ExecSandboxInput messages, or None to close

    The caller runs a background thread that drains response_iterator and a write loop
    that puts input messages. This function is synchronous; call from a background thread.
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    input_q: queue.Queue = queue.Queue()

    def _request_generator():
        req = openshell_pb2.ExecSandboxRequest(
            command=command,
            workdir="/sandbox",
            tty=True,
            cols=cols,
            rows=rows,
        )
        req.workspace_scope.CopyFrom(_workspace_selector())
        req.sandbox = sandbox_name
        for k, v in (env or {}).items():
            req.environment[k] = v
        start_msg = openshell_pb2.ExecSandboxInput(start=req)
        yield start_msg
        while True:
            item = input_q.get()
            if item is None:
                return
            yield item

    response_stream = client._stub.ExecSandboxInteractive(_request_generator())
    return response_stream, input_q


async def expose_service(
    sandbox_name: str,
    service_name: str,
    target_port: int,
    client=None,
    gateway_config: GatewayConfig | None = None,
) -> str:
    """Expose a sandbox's port via the OpenShell gateway and return the routable URL."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client(gateway_config)

    if gateway_config is None:
        gateway_config = default_gateway_config()

    def _do():
        req = openshell_pb2.ExposeServiceRequest(
            workspace_scope=_workspace_selector(),
            name=service_name,
            target_port=target_port,
            domain=True,
            sandbox=sandbox_name,
        )
        resp = client._stub.ExposeService(req, timeout=client._timeout)
        url = resp.url
        # The returned URL embeds the gateway hostname; it is stored on the
        # session row but must never be written to log output.
        log.info("expose_service %s/%s", sandbox_name, service_name)
        # The gateway returns the internal cluster port (e.g. 8080), but a
        # workspace gateway may be reachable on a different port. Rewrite to
        # the port from the same gateway config used for this exposure.
        gw = gateway_config.gateway_url or ""
        gw_with_scheme = gw if "://" in gw else f"//{gw}"
        gateway_url = urlparse(gw_with_scheme)
        gw_port = gateway_url.port
        if gw_port is None:
            # A hosted gateway URL commonly omits the default HTTPS port.
            # The service URL returned by OpenShell still carries its internal
            # port, so use the effective gateway port when rewriting it.
            scheme = gateway_url.scheme.lower()
            if scheme in ("https", "grpcs", "wss"):
                gw_port = 443
            elif scheme in ("http", "grpc", "ws"):
                gw_port = 80
        if gw_port:
            from urllib.parse import urlunparse
            p = urlparse(url)
            if p.port and p.port != gw_port:
                host = p.hostname or ""
                if ":" in host:
                    host = f"[{host}]"
                url = urlunparse(p._replace(netloc=f"{host}:{gw_port}"))
                log.info("expose_service: rewrote port %s → %s", p.port, gw_port)
        return url

    return await asyncio.to_thread(_do)


async def delete_service(
    sandbox_name: str,
    service_name: str,
    client=None,
) -> None:
    """Delete an exposed sandbox service endpoint."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do():
        req = openshell_pb2.DeleteServiceRequest(
            workspace_scope=_workspace_selector(),
            name=service_name,
            sandbox=sandbox_name,
            allow_missing=True,
        )
        import grpc
        from openshell.mutations import DeletionOutcome

        try:
            response = client._stub.DeleteService(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                return
            raise

        outcome = DeletionOutcome(response.outcome)
        if outcome == DeletionOutcome.ACCEPTED:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    client._stub.GetService(
                        openshell_pb2.GetServiceRequest(
                            workspace_scope=_workspace_selector(),
                            name=service_name,
                            sandbox=sandbox_name,
                        ),
                        timeout=client._timeout,
                    )
                except grpc.RpcError as exc:
                    if _gateway_rpc_code(exc) == grpc.StatusCode.NOT_FOUND:
                        return
                    raise
                time.sleep(1)
            raise TimeoutError(f"OpenShell service {service_name} was not deleted within 60s")
        _require_deletion_complete(outcome, "service", service_name)

    await asyncio.to_thread(_do)

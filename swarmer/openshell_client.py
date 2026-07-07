"""
OpenShell sandbox client wrapper for AgentSwarm session lifecycle.

Wraps the synchronous OpenShell gRPC SDK (pip install openshell) and exposes
async helpers by running blocking calls via asyncio.to_thread().

Credential injection: env vars go directly into SandboxSpec.environment —
there is no provider_create RPC in this SDK version.
"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import queue
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

# ── provider_exists TTL cache ─────────────────────────────────────────────────
# Avoids a gRPC round-trip on every page-load that renders model options or the
# secrets status badge.  Cache entries expire after _PROVIDER_CACHE_TTL seconds.
# Invalidated explicitly by create_google_cloud_provider / delete_provider.

_PROVIDER_CACHE_TTL: float = 30.0
_provider_cache: dict[str, tuple[bool, float]] = {}  # name → (exists, expires_at)


def _get_client():
    """Internal factory — reads settings and returns a configured SandboxClient."""
    from openshell import SandboxClient, TlsConfig  # noqa: F401 (optional dep)
    from swarmer.config import settings

    tls = None
    if settings.openshell_tls_ca:
        tls = TlsConfig(
            ca_path=pathlib.Path(settings.openshell_tls_ca),
            cert_path=pathlib.Path(settings.openshell_tls_cert),
            key_path=pathlib.Path(settings.openshell_tls_key),
        )
    # When mTLS is configured, use certificate auth for gateway admin operations
    # (provider create/update, sandbox create). Bearer tokens in 0.0.55+ use a
    # sandbox-scoped format that doesn't authorize admin RPCs.
    bearer = None if tls else (settings.openshell_bearer_token or None)
    return SandboxClient(
        settings.openshell_gateway_url,
        tls=tls,
        bearer_token=bearer,
    )


def get_client(
    gateway_url: str,
    tls_ca_path: str | None = None,
    tls_cert_path: str | None = None,
    tls_key_path: str | None = None,
):
    """Public factory for e2e tests and direct usage."""
    from openshell import SandboxClient, TlsConfig  # noqa: F401 (optional dep)

    tls = None
    if tls_ca_path:
        tls = TlsConfig(
            ca_path=pathlib.Path(tls_ca_path),
            cert_path=pathlib.Path(tls_cert_path),
            key_path=pathlib.Path(tls_key_path),
        )
    return SandboxClient(gateway_url, tls=tls)


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
) -> None:
    """Create or update a named provider on the gateway with its credentials.

    For static API keys the gateway stores credentials securely and injects
    them as env vars into the sandbox via GetSandboxProviderEnvironment.
    Credentials are stored server-side (returned as REDACTED); use UpdateProvider
    on subsequent launches to rotate keys.
    """
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _build_provider(req_provider):
        req_provider.metadata.name = name
        req_provider.type = profile_type
        for k, v in (config or {}).items():
            req_provider.config[k] = v
        for k, v in (credentials or {}).items():
            req_provider.credentials[k] = v

    def _do_ensure():
        create_req = openshell_pb2.CreateProviderRequest()
        _build_provider(create_req.provider)
        try:
            client._stub.CreateProvider(create_req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() == grpc.StatusCode.ALREADY_EXISTS:
                update_req = openshell_pb2.UpdateProviderRequest()
                _build_provider(update_req.provider)
                client._stub.UpdateProvider(update_req, timeout=client._timeout)
            else:
                raise

    await asyncio.to_thread(_do_ensure)


async def delete_provider(name: str, client=None) -> None:
    """Delete a named provider from the gateway, ignoring NOT_FOUND errors."""
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _do_delete():
        req = openshell_pb2.DeleteProviderRequest()
        req.name = name
        try:
            client._stub.DeleteProvider(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() == grpc.StatusCode.NOT_FOUND:
                return
            raise

    await asyncio.to_thread(_do_delete)
    _provider_cache.pop(name, None)  # invalidate cached existence check


async def provider_exists(name: str, client=None) -> bool:
    """Return True if a named provider exists on the gateway.

    Results are cached for _PROVIDER_CACHE_TTL seconds to avoid a gRPC round-trip
    on every page render.  The cache is invalidated by create_google_cloud_provider
    and delete_provider.
    """
    now = time.monotonic()
    cached = _provider_cache.get(name)
    if cached is not None:
        result, expires_at = cached
        if now < expires_at:
            return result

    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _do_check() -> bool:
        req = openshell_pb2.GetProviderRequest()
        req.name = name
        try:
            client._stub.GetProvider(req, timeout=client._timeout)
            return True
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() == grpc.StatusCode.NOT_FOUND:
                return False
            raise

    result = await asyncio.to_thread(_do_check)
    _provider_cache[name] = (result, now + _PROVIDER_CACHE_TTL)
    return result


async def create_google_cloud_provider(
    name: str,
    project: str,
    location: str,
    client=None,
) -> None:
    """Delete any existing provider with this name and create a fresh google-cloud one.

    The google-cloud provider type (OpenShell >= 0.0.69) runs a GCE metadata emulator
    inside the sandbox (127.0.0.1:8174) so GCP SDKs that bypass HTTP_PROXY can still
    obtain credentials. Config keys and credential key differ from google-vertex-ai:
      config:      project_id, region  (lowercase)
      credentials: GCP_ADC_ACCESS_TOKEN  (placeholder; refreshed by gateway)
    """
    await delete_provider(name, client=client)
    await ensure_provider(
        name, "google-cloud",
        config={"project_id": project, "region": location},
        credentials={"GCP_ADC_ACCESS_TOKEN": "__placeholder__"},
        client=client,
    )
    # Provider now exists — update cache so the next page load doesn't need an RPC.
    _provider_cache[name] = (True, time.monotonic() + _PROVIDER_CACHE_TTL)


async def configure_google_cloud_provider(
    provider_name: str,
    adc_json: str,
    client=None,
) -> None:
    """Configure a google-cloud provider with auto-refreshing credentials.

    Parses the ADC JSON to choose the appropriate gateway refresh strategy:
    - service_account → GOOGLE_SERVICE_ACCOUNT_JWT (gateway signs JWTs from SA key)
    - authorized_user → OAUTH2_REFRESH_TOKEN (gateway exchanges refresh token)

    The credential key for google-cloud is GCP_ADC_ACCESS_TOKEN (not GOOGLE_VERTEX_AI_TOKEN).
    The GCE metadata emulator inside the sandbox resolves this placeholder so GCP SDKs
    that bypass HTTP_PROXY still receive a valid access token.
    """
    import json as _json
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    adc = _json.loads(adc_json)
    adc_type = adc.get("type", "")

    def _do_configure():
        req = openshell_pb2.ConfigureProviderRefreshRequest()
        req.provider = provider_name

        if adc_type == "service_account":
            req.credential_key = "GCP_ADC_ACCESS_TOKEN"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
            req.material["client_email"] = adc.get("client_email", "")
            req.material["private_key"] = adc.get("private_key", "")
            req.secret_material_keys.append("private_key")
        elif adc_type == "authorized_user":
            req.credential_key = "GCP_ADC_ACCESS_TOKEN"
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
    """Delete any existing provider with this name and create a fresh one with
    exactly the structure the google-vertex-ai profile requires:
      config:      VERTEX_AI_PROJECT_ID, VERTEX_AI_REGION
      credentials: GOOGLE_VERTEX_AI_TOKEN  (placeholder; refreshed by gateway)
    """
    await delete_provider(name, client=client)
    await ensure_provider(
        name, "google-vertex-ai",
        config={"VERTEX_AI_PROJECT_ID": project, "VERTEX_AI_REGION": location},
        credentials={"GOOGLE_VERTEX_AI_TOKEN": "__placeholder__"},
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
        req.provider = provider_name

        if adc_type == "service_account":
            # The google-vertex-ai profile's credential key is GOOGLE_VERTEX_AI_TOKEN.
            # Gateway refreshes via GOOGLE_SERVICE_ACCOUNT_JWT and injects the resulting
            # token as the GOOGLE_VERTEX_AI_TOKEN env var inside the sandbox.
            req.credential_key = "GOOGLE_VERTEX_AI_TOKEN"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
            req.material["client_email"] = adc.get("client_email", "")
            req.material["private_key"] = adc.get("private_key", "")
            req.secret_material_keys.append("private_key")
        elif adc_type == "authorized_user":
            # The google-vertex-ai profile's credential key is GOOGLE_VERTEX_AI_TOKEN.
            # Gateway refreshes via OAUTH2_REFRESH_TOKEN (token_url and scopes are
            # defined in the built-in profile, not in the material).
            req.credential_key = "GOOGLE_VERTEX_AI_TOKEN"
            req.strategy = openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN
            req.material["client_id"] = adc.get("client_id", "")
            req.material["client_secret"] = adc.get("client_secret", "")
            req.material["refresh_token"] = adc.get("refresh_token", "")
            req.secret_material_keys.extend(["client_secret", "refresh_token"])
        else:
            raise ValueError(f"Unsupported ADC type for VertexAI provider: {adc_type!r}")

        client._stub.ConfigureProviderRefresh(req, timeout=client._timeout)

    await asyncio.to_thread(_do_configure)


async def set_cluster_inference(
    provider_name: str,
    model_id: str,
    *,
    no_verify: bool = False,
    client=None,
) -> None:
    """Configure the cluster-level inference proxy (inference.local) to use a provider.

    Required before agents can use ANTHROPIC_BASE_URL=https://inference.local/v1.
    The gateway routes requests through the provider's credentials (VertexAI tokens).
    Use no_verify=True when the 'global' region causes endpoint verification to fail.
    """
    from openshell._proto import inference_pb2, inference_pb2_grpc

    if client is None:
        client = _get_client()

    def _do_set():
        stub = inference_pb2_grpc.InferenceStub(client._channel)
        req = inference_pb2.SetClusterInferenceRequest(
            provider_name=provider_name,
            model_id=model_id,
            no_verify=no_verify,
        )
        stub.SetClusterInference(req, timeout=client._timeout)

    await asyncio.to_thread(_do_set)


async def enable_providers_v2(client=None) -> None:
    """Enable the providers_v2 feature flag on the gateway (required for google-vertex-ai inference).

    Equivalent to: openshell settings set --global --key providers_v2_enabled --value true
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_enable():
        req = openshell_pb2.UpdateConfigRequest()
        req.setting_key = "providers_v2_enabled"
        req.setting_value.bool_value = True
        setattr(req, "global", True)
        client._stub.UpdateConfig(req, timeout=client._timeout)

    await asyncio.to_thread(_do_enable)


async def attach_sandbox_provider(sandbox_name: str, provider_name: str, client=None) -> None:
    """Attach a pre-configured gateway provider to a sandbox."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_attach():
        req = openshell_pb2.AttachSandboxProviderRequest()
        req.sandbox_name = sandbox_name
        req.provider_name = provider_name
        client._stub.AttachSandboxProvider(req, timeout=client._timeout)

    await asyncio.to_thread(_do_attach)


async def detach_sandbox_provider(sandbox_name: str, provider_name: str, client=None) -> None:
    """Detach a provider from a sandbox, ignoring NOT_FOUND errors."""
    from openshell._proto import openshell_pb2
    import grpc

    if client is None:
        client = _get_client()

    def _do_detach():
        req = openshell_pb2.DetachSandboxProviderRequest()
        req.sandbox_name = sandbox_name
        req.provider_name = provider_name
        try:
            client._stub.DetachSandboxProvider(req, timeout=client._timeout)
        except grpc.RpcError as exc:
            if isinstance(exc, grpc.Call) and exc.code() in (
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
            dp = client._stub.GetDraftPolicy(
                openshell_pb2.GetDraftPolicyRequest(name=sandbox_name), timeout=10
            )
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
                    client._stub.ApproveDraftChunk(
                        openshell_pb2.ApproveDraftChunkRequest(
                            name=sandbox_name, chunk_id=chunk.id
                        ), timeout=10
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
        "binaries": [{"path": str, "harness": bool}, ...],
      }

    Returns [] if the sandbox does not exist, the gateway is unreachable, or
    any other error occurs — callers should treat an empty list as "no chunks".
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_fetch() -> list[dict]:
        try:
            dp = client._stub.GetDraftPolicy(
                openshell_pb2.GetDraftPolicyRequest(name=sandbox_name), timeout=10
            )
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
                    {"path": b.path, "harness": bool(b.harness)}
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
            # NOT_FOUND is expected for TUI/server sessions whose sandbox has
            # not yet recorded any policy chunks — log at debug, not warning.
            code = getattr(getattr(exc, "code", None), "value", None) or str(exc)
            is_not_found = "NOT_FOUND" in str(exc) or code == 5  # grpc StatusCode.NOT_FOUND = 5
            if is_not_found:
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
                        name=sandbox_name, chunk_id=chunk_id
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
                history = client._stub.GetDraftHistory(
                    openshell_pb2.GetDraftHistoryRequest(name=sandbox_name),
                    timeout=10,
                )
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
                client._stub.UndoDraftChunk(
                    openshell_pb2.UndoDraftChunkRequest(
                        name=sandbox_name, chunk_id=chunk_id
                    ),
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


async def import_provider_profiles(profiles: list[dict], client=None) -> None:
    """Import custom provider type profiles into the gateway (idempotent)."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_import():
        req = openshell_pb2.ImportProviderProfilesRequest()
        for p in profiles:
            profile = openshell_pb2.ProviderProfile(
                id=p["id"],
                display_name=p.get("display_name", p["id"]),
                category=p.get("category", openshell_pb2.PROVIDER_PROFILE_CATEGORY_INFERENCE),
                inference_capable=p.get("inference_capable", True),
            )
            for cred in p.get("credentials", []):
                c = openshell_pb2.ProviderProfileCredential(
                    name=cred["name"],
                    required=cred.get("required", True),
                    auth_style=cred.get("auth_style", ""),
                    header_name=cred.get("header_name", ""),
                    query_param=cred.get("query_param", ""),
                )
                for ev in cred.get("env_vars", []):
                    c.env_vars.append(ev)
                refresh = cred.get("refresh")
                if refresh:
                    c.refresh.token_url = refresh.get("token_url", "")
                    for sc in refresh.get("scopes", []):
                        c.refresh.scopes.append(sc)
                    for mat in refresh.get("material", []):
                        m = c.refresh.material.add()
                        m.name = mat["name"]
                        m.required = mat.get("required", True)
                        m.secret = mat.get("secret", False)
                profile.credentials.append(c)
            req.profiles.append(openshell_pb2.ProviderProfileImportItem(profile=profile, source="swarmer"))
        client._stub.ImportProviderProfiles(req, timeout=client._timeout)

    await asyncio.to_thread(_do_import)


async def create_provider_from_env(
    google_api_key: str,
    anthropic_api_key: str,
    github_pat: str,
    client=None,
) -> dict[str, str]:
    """Build env-var dict from explicit credential values (used by e2e tests)."""
    env_vars: dict[str, str] = {}
    if google_api_key:
        env_vars["GOOGLE_API_KEY"] = google_api_key
    if anthropic_api_key:
        env_vars["ANTHROPIC_API_KEY"] = anthropic_api_key
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

    def _do_create():
        return client.create(spec=spec)

    ref = await asyncio.to_thread(_do_create)
    await _wait_sandbox_ready(ref.name, client=client)
    return ref


async def _exec_with_supervisor_retry(fn, *, max_attempts: int = 8, base_delay: float = 2.0) -> Any:
    """Run a blocking exec callable, retrying on transient sandbox-not-ready errors.

    The OpenShell gateway reports a sandbox as Ready before the supervisor process
    has fully established its relay session.  Provider types that start extra
    processes inside the sandbox (e.g. the GCE metadata emulator started by the
    google-cloud provider in OpenShell >= 0.0.69) can widen this window.  Exec
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
                and isinstance(exc, grpc.Call)
                and (
                    (
                        exc.code() == grpc.StatusCode.UNAVAILABLE
                        and "supervisor session not connected" in (exc.details() or "")
                    )
                    or (
                        exc.code() == grpc.StatusCode.FAILED_PRECONDITION
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
    """Wait until the sandbox Ready condition is True.

    The gateway reports readiness via status.conditions[type=Ready, status=True]
    rather than the phase field (which stays UNSPECIFIED=0 in current versions).
    We also wait for the supervisor to become attached so provider env vars are
    available to exec commands.
    """
    import time
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    deadline = time.time() + timeout

    def _poll():
        while time.time() < deadline:
            resp = client._stub.GetSandbox(
                openshell_pb2.GetSandboxRequest(name=sandbox_name), timeout=10
            )
            status = resp.sandbox.status
            if status.phase == openshell_pb2.SANDBOX_PHASE_ERROR:
                raise RuntimeError(f"Sandbox {sandbox_name} entered error phase")
            for cond in status.conditions:
                if cond.type == "Ready" and cond.status == "True":
                    return resp.sandbox
            time.sleep(poll_interval)
        raise RuntimeError(
            f"Sandbox {sandbox_name} not ready after {timeout}s "
            f"(last conditions: {[(c.type, c.status) for c in resp.sandbox.status.conditions]})"
        )

    await asyncio.to_thread(_poll)


async def delete_sandbox(sandbox_name: str, client=None) -> None:
    """Delete an OpenShell sandbox."""
    if client is None:
        client = _get_client()

    def _do_delete():
        client.delete(sandbox_name)

    await asyncio.to_thread(_do_delete)


async def list_sandboxes(client=None) -> list[str]:
    """Return names of all live sandboxes from the OpenShell gateway."""
    if client is None:
        client = _get_client()

    def _do_list():
        return client.list()

    refs = await asyncio.to_thread(_do_list)
    return [ref.name for ref in refs]


async def _sandbox_id(sandbox_name: str, client) -> str:
    """Resolve sandbox name → id (needed by the exec RPC)."""
    ref = await asyncio.to_thread(client.get, sandbox_name)
    return ref.id



async def write_agent_config(
    sandbox_name: str,
    tool_name: str,
    config_json: str,
    client=None,
) -> None:
    """Write agent config JSON to /sandbox/{tool_name}.json, passed via --config at startup.

    Uses stdin to deliver file content so the gateway's no-newline-in-args
    restriction is never hit.
    """
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    
    if tool_name == "crush":
        dest = shlex.quote("/sandbox/.config/crush/crush.json")
        script = f"mkdir -p /sandbox/.config/crush && cat > {dest}"
    else:
        # Write to /sandbox/opencode.json — passed explicitly via --config so
        # OpenCode loads it regardless of HOME or working directory.
        dest = shlex.quote("/sandbox/opencode.json")
        script = f"cat > {dest}"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script], stdin=config_json.encode())

    await _exec_with_supervisor_retry(_do_write)


async def write_agents_md(sandbox_name: str, content: str, client=None) -> None:
    """Write content to /sandbox/AGENTS.md via stdin (avoids newline-in-arg restriction)."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", "cat > /sandbox/AGENTS.md"], stdin=content.encode())

    await _exec_with_supervisor_retry(_do_write)


async def write_file(sandbox_name: str, path: str, content: str, client=None) -> None:
    """Write arbitrary content to a file inside the sandbox via stdin."""
    if client is None:
        client = _get_client()
    sid = await _sandbox_id(sandbox_name, client)
    parent = shlex.quote(str(pathlib.Path(path).parent))
    dest = shlex.quote(path)
    script = f"mkdir -p {parent} && cat > {dest}"

    def _do_write(s=sid):
        client.exec(s, ["sh", "-c", script], stdin=content.encode())

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
    sid = await _sandbox_id(sandbox_name, client)

    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    bg_cmd = ["sh", "-c", f"nohup {shell_cmd} >/sandbox/.agent.log 2>&1 &"]

    def _do_start(s=sid):
        client.exec(s, bg_cmd, env=env or {}, workdir="/sandbox")

    await asyncio.to_thread(_do_start)


async def read_opencode_response(sandbox_name: str, client=None) -> str:
    """Read the last assistant message from OpenCode's SQLite DB.

    OpenCode stores conversation history in /sandbox/.opencode/opencode.db rather
    than writing to stdout. This extracts the most recent assistant text parts.
    """

    if client is None:
        client = _get_client()

    # Write the reader script via stdin (no newlines in command arg) then execute it.
    reader = b"""
import sqlite3, json
try:
    conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
    conn.execute('PRAGMA wal_checkpoint(FULL)')
    rows = conn.execute('''
        SELECT p.data FROM part p
        JOIN message m ON p.message_id = m.id
        WHERE json_extract(m.data, '$.role') = 'assistant'
          AND json_extract(p.data, '$.type') = 'text'
        ORDER BY p.time_created ASC
    ''').fetchall()
    texts = [json.loads(r[0]).get('text', '') for r in rows if r[0]]
    out = '\\n'.join(t for t in texts if t.strip())
    print(out[:64000] if out else '', end='')
    conn.close()
except Exception as exc:
    import sys; print(f'DB_ERR:{exc}', file=sys.stderr, end='')
"""

    def _do_read():
        try:
            sid = client.get(sandbox_name).id
            client.exec(sid, ["sh", "-c", "cat > /tmp/_oc_read.py"], stdin=reader, timeout_seconds=10)
            result = client.exec(sid, ["python3", "/tmp/_oc_read.py"], timeout_seconds=15)
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
    client,
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
    sid = await _sandbox_id(sandbox_name, client)

    def _do_exec(s=sid):
        return client.exec(s, cmd, stdin=stdin, timeout_seconds=timeout_seconds,
                           env=env or {})

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
    sid = await _sandbox_id(sandbox_name, client)

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
            for item in client.exec_stream(sid, cmd, env=env or {}, timeout_seconds=_NO_TIMEOUT):
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
    Returns {} if the call fails (e.g. no providers attached).
    """
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do_get():
        req = openshell_pb2.GetSandboxProviderEnvironmentRequest(sandbox_id=sandbox_id)
        resp = client._stub.GetSandboxProviderEnvironment(req, timeout=10)
        return dict(resp.environment)

    try:
        return await asyncio.to_thread(_do_get)
    except Exception:
        return {}


def exec_interactive(
    sandbox_name: str,
    sandbox_id: str,
    command: list[str],
    cols: int,
    rows: int,
    env: dict[str, str] | None = None,
    client=None,
):
    """Open an interactive PTY exec stream for a sandbox (for TUI WebSocket bridge).

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
            sandbox_id=sandbox_id,
            command=command,
            workdir="/sandbox",
            tty=True,
            cols=cols,
            rows=rows,
        )
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
) -> str:
    """Expose a sandbox's port via the OpenShell gateway and return the routable URL."""
    from openshell._proto import openshell_pb2

    if client is None:
        client = _get_client()

    def _do():
        req = openshell_pb2.ExposeServiceRequest(
            sandbox=sandbox_name,
            service=service_name,
            target_port=target_port,
            domain=True,
        )
        resp = client._stub.ExposeService(req, timeout=client._timeout)
        url = resp.url
        log.info("expose_service %s/%s → %s", sandbox_name, service_name, url)
        # The gateway returns the internal cluster port (e.g. 8080) but in local
        # dev with kubectl port-forward the gateway is accessible on a different
        # port (e.g. 17670). Rewrite to the configured gateway port so Swarmer
        # can reach it from the host.
        from swarmer.config import settings
        gw = settings.openshell_gateway_url or ""
        gw_port = gw.rsplit(":", 1)[-1] if ":" in gw else ""
        if gw_port and gw_port.isdigit():
            from urllib.parse import urlparse, urlunparse
            p = urlparse(url)
            if p.port and str(p.port) != gw_port:
                url = urlunparse(p._replace(netloc=f"{p.hostname}:{gw_port}"))
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
            sandbox=sandbox_name,
            service=service_name,
        )
        client._stub.DeleteService(req, timeout=client._timeout)

    await asyncio.to_thread(_do)

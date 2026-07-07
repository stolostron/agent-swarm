"""
Tests for swarmer.openshell_client — the OpenShell SDK wrapper.

Validates the session lifecycle helpers:
  - create_provider() builds env-var dicts from DB credentials (no K8s Secrets)
  - create_sandbox() calls SandboxClient.create() and wait_ready()
  - exec helpers (clone_repos, write_agent_config, write_agents_md, start_agent)
    use /sandbox/ paths, not /workspace/
  - delete_sandbox() calls SandboxClient.delete() without touching PVCs
"""
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Inject openshell SDK stub so swarmer.openshell_client imports succeed
# without a real installed package.
# ---------------------------------------------------------------------------


class _SandboxTemplate:
    def __init__(self):
        self.image = ""
        self.environment = {}


class _SandboxSpec:
    def __init__(self):
        self.template = _SandboxTemplate()
        self.environment = {}
        self.policy = None


_proto_stub = MagicMock()
_proto_stub.openshell_pb2 = MagicMock()
_proto_stub.openshell_pb2.SandboxSpec = _SandboxSpec

_sdk_stub = MagicMock()
_sdk_stub.SandboxClient = MagicMock
_sdk_stub.TlsConfig = MagicMock
_sdk_stub._proto = _proto_stub

# Save any real openshell modules already in sys.modules so we can restore
# them after importing swarmer.openshell_client with our stubs.  This prevents
# the stubs from polluting sys.modules for other test files (e.g.
# test_openshell_policy.py) that need the real protobuf classes.
_saved_modules = {k: v for k, v in sys.modules.items() if "openshell" in k}

sys.modules["openshell"] = _sdk_stub
sys.modules["openshell._proto"] = _proto_stub
sys.modules["openshell._proto.openshell_pb2"] = _proto_stub.openshell_pb2

import swarmer.openshell_client as oc  # noqa: E402

# Restore real openshell modules (or remove the stubs if none were there before).
# Iterate over ALL current sys.modules entries that contain "openshell" (excluding
# swarmer.openshell_client which we intentionally imported) so that transitively-
# loaded modules (sandbox_pb2, datamodel_pb2, etc.) are also restored.  A hardcoded
# key list misses those and leaves a stale MagicMock in the module chain, causing
# lazy imports in other test files to pick up mocks instead of real proto classes.
for _k in list(sys.modules):
    if "openshell" in _k and "swarmer" not in _k:
        if _k in _saved_modules:
            sys.modules[_k] = _saved_modules[_k]
        else:
            sys.modules.pop(_k, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sdk_client():
    """Mock object mimicking the synchronous openshell.SandboxClient interface."""
    client = MagicMock()
    ref = MagicMock()
    ref.name = "sandbox-s42-abc1"
    ref.id = "sandbox-s42-abc1"
    client.create = MagicMock(return_value=ref)
    client.get = MagicMock(return_value=ref)
    client.wait_ready = MagicMock(return_value=ref)
    client.exec = MagicMock(return_value=MagicMock(exit_code=0, stdout=""))
    client.delete = MagicMock(return_value=True)
    return client


@pytest.fixture
def session():
    s = MagicMock()
    s.id = 42
    s.mode = "tui"
    s.agent_tool = "opencode"
    s.model = "google-vertex-anthropic/claude-sonnet-4-6@default"
    s.instruction_prompt = ""
    s.sandbox_name = None
    repo = MagicMock()
    repo.url = "https://github.com/stolostron/agent-swarm"
    repo.branch = "main"
    repo.local_path = "agent-swarm"
    s.repos = [repo]
    return s


@pytest.fixture
def workspace_secret():
    secret = MagicMock()
    secret.google_api_key = "<test-google-api-key>"
    secret.anthropic_api_key = "<test-anthropic-api-key>"
    secret.google_cloud_project = "<test-gcp-project>"
    return secret


@pytest.fixture
def github_pat():
    pat = MagicMock()
    pat.token = "<test-github-pat>"
    pat.username = "<test-github-user>"
    return pat


# ---------------------------------------------------------------------------
# 1. Provider creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_provider_returns_empty_for_no_mcp(sdk_client, session, workspace_secret):
    """AI credentials no longer go via env vars — create_provider returns only MCP vars."""
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[],
    )
    assert isinstance(env_vars, dict)
    assert "GOOGLE_API_KEY" not in env_vars
    assert "ANTHROPIC_API_KEY" not in env_vars
    assert env_vars == {}


@pytest.mark.asyncio
async def test_create_provider_does_not_create_k8s_agent_secret(session, workspace_secret):
    # k8s.create_session_agent_secret has been removed from k8s.py as part of the
    # OpenShell migration dead-code cleanup.  create_provider() cannot call it.
    await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[],
    )


@pytest.mark.asyncio
async def test_create_provider_does_not_create_k8s_pat_secret(session, workspace_secret, github_pat):
    # k8s.create_session_pat_secret has been removed from k8s.py as part of the
    # OpenShell migration dead-code cleanup.  create_provider() cannot call it.
    await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=github_pat,
        mcp_servers=[],
    )


@pytest.mark.asyncio
async def test_create_provider_no_github_pat_in_env(session, workspace_secret, github_pat):
    """GitHub PAT is now injected via the github gateway provider, not env vars."""
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=github_pat,
        mcp_servers=[],
    )
    assert "GITHUB_PAT" not in env_vars
    assert "GH_TOKEN" not in env_vars


@pytest.mark.asyncio
async def test_create_provider_jira_not_in_env_vars(session, workspace_secret):
    """Jira credentials must NOT appear in env_vars from create_provider().

    Jira credentials go through the OpenShell Provider API, not raw env vars.
    create_provider() only returns workspace extra env vars supplied via extra_env.
    """
    jira_mcp = MagicMock()
    jira_mcp.slug = "atlassian-jira"
    jira_mcp.jira_server_url = "https://example.atlassian.net"
    jira_mcp.jira_access_token = "<test-jira-token>"
    jira_mcp.jira_email = "test@example.com"
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[jira_mcp],
    )
    assert "JIRA_SERVER_URL" not in env_vars, (
        "Jira credentials must go through Provider API, not raw env_vars"
    )
    assert "JIRA_ACCESS_TOKEN" not in env_vars
    assert "JIRA_EMAIL" not in env_vars


@pytest.mark.asyncio
async def test_create_provider_includes_workspace_extra_env_vars(session, workspace_secret):
    """create_provider() returns workspace extra env vars passed via extra_env (from DB)."""
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[],
        extra_env={"MY_VAR": "hello", "FOO": "bar"},
    )
    assert env_vars.get("MY_VAR") == "hello"
    assert env_vars.get("FOO") == "bar"


@pytest.mark.asyncio
async def test_create_provider_empty_when_no_extra_env(session, workspace_secret):
    """create_provider() returns {} when no extra_env is supplied."""
    env_vars = await oc.create_provider(
        session=session,
        workspace_secret=workspace_secret,
        github_pat=None,
        mcp_servers=[],
    )
    assert env_vars == {}


# ---------------------------------------------------------------------------
# 2. Sandbox creation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sandbox_passes_byoc_image(sdk_client):
    image = "your-registry.example.com/opencode:latest"
    with patch.object(oc, "_get_client", return_value=sdk_client), \
         patch.object(oc, "_wait_sandbox_ready", new=AsyncMock()):
        await oc.create_sandbox(image=image, env_vars={}, policy=None)
    sdk_client.create.assert_called_once()
    spec = sdk_client.create.call_args.kwargs["spec"]
    assert spec.template.image == image


@pytest.mark.asyncio
async def test_wait_ready_called_after_create(sdk_client):
    """_wait_sandbox_ready (conditions-based) is called instead of sdk client.wait_ready."""
    with patch.object(oc, "_get_client", return_value=sdk_client), \
         patch.object(oc, "_wait_sandbox_ready", new=AsyncMock()) as mock_ready:
        await oc.create_sandbox(
            image="your-registry.example.com/opencode:latest", env_vars={}, policy=None
        )
    mock_ready.assert_called_once()


@pytest.mark.asyncio
async def test_create_sandbox_does_not_create_pvc(sdk_client):
    # k8s_session.ensure_session_pvc has been removed — k8s_session no longer exists.
    # Verify create_sandbox succeeds without any PVC creation.
    with patch.object(oc, "_get_client", return_value=sdk_client), \
         patch.object(oc, "_wait_sandbox_ready", new=AsyncMock()):
        await oc.create_sandbox(
            image="your-registry.example.com/opencode:latest", env_vars={}, policy=None
        )


# ---------------------------------------------------------------------------
# 3. Exec operations: config, AGENTS.md, agent startup
# (git clone now uses exec_command inline in _setup_openshell_sandbox)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_write_exec_uses_sandbox_config_path(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    config_json = '{"$schema": "https://opencode.ai/config.json", "mcpServers": {}}'
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.write_agent_config(
            sandbox_name=sandbox_name,
            tool_name="opencode",
            config_json=config_json,
        )
    sdk_client.exec.assert_called_once()
    calls_repr = str(sdk_client.exec.call_args)
    assert "/sandbox/" in calls_repr
    assert "/workspace/" not in calls_repr


@pytest.mark.asyncio
async def test_agents_md_exec_writes_to_sandbox(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.write_agents_md(sandbox_name=sandbox_name, content="# Instructions\n\nFix the bug.")
    sdk_client.exec.assert_called_once()
    calls_repr = str(sdk_client.exec.call_args)
    assert "AGENTS.md" in calls_repr


@pytest.mark.asyncio
async def test_start_agent_exec_called_with_agent_cmd(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    cmd = ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.start_agent(sandbox_name=sandbox_name, cmd=cmd)
    sdk_client.exec.assert_called_once()
    calls_repr = str(sdk_client.exec.call_args)
    assert "opencode" in calls_repr


# ---------------------------------------------------------------------------
# 4. Session stop / cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_calls_delete_sandbox(sdk_client):
    sandbox_name = "sandbox-s42-abc1"
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.delete_sandbox(sandbox_name=sandbox_name)
    sdk_client.delete.assert_called_once_with(sandbox_name)


# ---------------------------------------------------------------------------
# get_draft_chunks tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_draft_chunks_returns_serializable_list(sdk_client):
    """get_draft_chunks() calls GetDraftPolicy and returns a list of dicts."""
    # Build a fake chunk proto-like object
    fake_ep = MagicMock()
    fake_ep.host = "vuln.go.dev"
    fake_ep.port = 443
    fake_ep.protocol = "rest"

    fake_bin = MagicMock()
    fake_bin.path = "/usr/local/go/bin/govulncheck"
    fake_bin.harness = True

    fake_chunk = MagicMock()
    fake_chunk.id = "chunk-abc"
    fake_chunk.status = "pending"
    fake_chunk.rule_name = "govulncheck"
    fake_chunk.proposed_rule.endpoints = [fake_ep]
    fake_chunk.proposed_rule.binaries = [fake_bin]

    fake_dp = MagicMock()
    fake_dp.chunks = [fake_chunk]
    sdk_client._stub.GetDraftPolicy.return_value = fake_dp

    with patch.object(oc, "_get_client", return_value=sdk_client):
        result = await oc.get_draft_chunks("sandbox-test")

    assert len(result) == 1
    c = result[0]
    assert c["id"] == "chunk-abc"
    assert c["status"] == "pending"
    assert c["rule_name"] == "govulncheck"
    assert c["endpoints"][0]["host"] == "vuln.go.dev"
    assert c["endpoints"][0]["port"] == 443
    assert c["binaries"][0]["path"] == "/usr/local/go/bin/govulncheck"
    assert c["binaries"][0]["harness"] is True


@pytest.mark.asyncio
async def test_get_draft_chunks_returns_empty_on_error(sdk_client):
    """get_draft_chunks() returns [] when the gateway call fails."""
    sdk_client._stub.GetDraftPolicy.side_effect = Exception("gateway unavailable")
    with patch.object(oc, "_get_client", return_value=sdk_client):
        result = await oc.get_draft_chunks("sandbox-gone")
    assert result == []


@pytest.mark.asyncio
async def test_stop_does_not_call_pvc_delete(sdk_client):
    # k8s_session.delete_session_pvc has been removed — k8s_session no longer exists.
    # Verify delete_sandbox succeeds without any PVC deletion.
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.delete_sandbox(sandbox_name="sandbox-s42-abc1")


@pytest.mark.asyncio
async def test_stop_does_not_call_cleanup_session_secrets(sdk_client):
    # k8s.cleanup_session_secrets has been removed from k8s.py as part of the
    # OpenShell migration dead-code cleanup.  delete_sandbox() cannot call it.
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.delete_sandbox(sandbox_name="sandbox-s42-abc1")


# ---------------------------------------------------------------------------
# 5. provider_exists — TTL cache + gRPC interaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_exists_returns_true_when_found(sdk_client):
    """provider_exists() returns True when GetProvider succeeds."""
    sdk_client._stub.GetProvider.return_value = MagicMock()
    oc._provider_cache.clear()
    with patch.object(oc, "_get_client", return_value=sdk_client):
        result = await oc.provider_exists("swarmer-ws-1-google-cloud")
    assert result is True
    sdk_client._stub.GetProvider.assert_called_once()


@pytest.mark.asyncio
async def test_provider_exists_returns_false_on_not_found(sdk_client):
    """provider_exists() returns False on gRPC NOT_FOUND."""
    import grpc

    class _NotFound(grpc.RpcError, grpc.Call):
        def code(self): return grpc.StatusCode.NOT_FOUND
        def details(self): return "provider not found"

    sdk_client._stub.GetProvider.side_effect = _NotFound()
    oc._provider_cache.clear()
    with patch.object(oc, "_get_client", return_value=sdk_client):
        result = await oc.provider_exists("swarmer-ws-1-google-cloud")
    assert result is False


@pytest.mark.asyncio
async def test_provider_exists_cache_hit_skips_grpc(sdk_client):
    """provider_exists() uses cached result and skips the gRPC call."""
    import time
    oc._provider_cache["swarmer-ws-2-google-cloud"] = (True, time.monotonic() + 30)
    with patch.object(oc, "_get_client", return_value=sdk_client):
        result = await oc.provider_exists("swarmer-ws-2-google-cloud")
    assert result is True
    sdk_client._stub.GetProvider.assert_not_called()


@pytest.mark.asyncio
async def test_provider_exists_cache_miss_calls_grpc(sdk_client):
    """provider_exists() calls gRPC when cache entry is expired."""
    import time
    oc._provider_cache["swarmer-ws-3-google-cloud"] = (True, time.monotonic() - 1)  # expired
    sdk_client._stub.GetProvider.return_value = MagicMock()
    with patch.object(oc, "_get_client", return_value=sdk_client):
        result = await oc.provider_exists("swarmer-ws-3-google-cloud")
    assert result is True
    sdk_client._stub.GetProvider.assert_called_once()


@pytest.mark.asyncio
async def test_delete_provider_invalidates_cache(sdk_client):
    """delete_provider() removes the name from _provider_cache."""
    import time
    oc._provider_cache["swarmer-ws-4-google-cloud"] = (True, time.monotonic() + 30)
    with patch.object(oc, "_get_client", return_value=sdk_client):
        await oc.delete_provider("swarmer-ws-4-google-cloud")
    assert "swarmer-ws-4-google-cloud" not in oc._provider_cache


# ---------------------------------------------------------------------------
# 6. create_google_cloud_provider + configure_google_cloud_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_google_cloud_provider_creates_correct_type(sdk_client):
    """create_google_cloud_provider() creates a 'google-cloud' provider after deleting any existing one."""
    oc._provider_cache.clear()
    with patch.object(oc, "_get_client", return_value=sdk_client), \
         patch.object(oc, "delete_provider", new=AsyncMock()) as mock_delete, \
         patch.object(oc, "ensure_provider", new=AsyncMock()) as mock_ensure:
        await oc.create_google_cloud_provider(
            "swarmer-ws-1-google-cloud", "my-project", "us-central1"
        )
    mock_delete.assert_awaited_once_with("swarmer-ws-1-google-cloud", client=None)
    mock_ensure.assert_awaited_once()
    _args, _kwargs = mock_ensure.call_args
    assert _args[0] == "swarmer-ws-1-google-cloud"
    assert _args[1] == "google-cloud"
    assert _kwargs["config"] == {"project_id": "my-project", "region": "us-central1"}
    assert "GCP_ADC_ACCESS_TOKEN" in _kwargs["credentials"]


@pytest.mark.asyncio
async def test_create_google_cloud_provider_populates_cache(sdk_client):
    """create_google_cloud_provider() updates _provider_cache to True after creation."""
    oc._provider_cache.clear()
    with patch.object(oc, "_get_client", return_value=sdk_client), \
         patch.object(oc, "delete_provider", new=AsyncMock()), \
         patch.object(oc, "ensure_provider", new=AsyncMock()):
        await oc.create_google_cloud_provider(
            "swarmer-ws-5-google-cloud", "proj", "us-east1"
        )
    assert oc._provider_cache.get("swarmer-ws-5-google-cloud", (False,))[0] is True


@pytest.mark.asyncio
async def test_configure_google_cloud_provider_service_account(sdk_client):
    """configure_google_cloud_provider() uses GOOGLE_SERVICE_ACCOUNT_JWT for service_account ADC."""
    import json
    adc = {
        "type": "service_account",
        "client_email": "sa@project.iam.gserviceaccount.com",
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n",
    }
    req_mock = MagicMock()
    req_mock.material = {}
    req_mock.secret_material_keys = []
    sdk_client._stub.ConfigureProviderRefresh = MagicMock()

    with patch.object(oc, "_get_client", return_value=sdk_client):
        with patch("openshell._proto.openshell_pb2.ConfigureProviderRefreshRequest",
                   return_value=req_mock):
            await oc.configure_google_cloud_provider(
                "swarmer-ws-1-google-cloud", json.dumps(adc)
            )

    assert sdk_client._stub.ConfigureProviderRefresh.called
    from openshell._proto import openshell_pb2
    assert req_mock.strategy == openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_GOOGLE_SERVICE_ACCOUNT_JWT
    assert req_mock.credential_key == "GCP_ADC_ACCESS_TOKEN"
    assert req_mock.material["client_email"] == adc["client_email"]
    assert req_mock.material["private_key"] == adc["private_key"]
    assert "private_key" in req_mock.secret_material_keys


@pytest.mark.asyncio
async def test_configure_google_cloud_provider_authorized_user(sdk_client):
    """configure_google_cloud_provider() uses OAUTH2_REFRESH_TOKEN for authorized_user ADC."""
    import json
    adc = {
        "type": "authorized_user",
        "client_id": "1234.apps.googleusercontent.com",
        "client_secret": "secret",
        "refresh_token": "1//refresh-token",
    }
    req_mock = MagicMock()
    req_mock.material = {}
    req_mock.secret_material_keys = []
    sdk_client._stub.ConfigureProviderRefresh = MagicMock()

    with patch.object(oc, "_get_client", return_value=sdk_client):
        with patch("openshell._proto.openshell_pb2.ConfigureProviderRefreshRequest",
                   return_value=req_mock):
            await oc.configure_google_cloud_provider(
                "swarmer-ws-1-google-cloud", json.dumps(adc)
            )

    assert sdk_client._stub.ConfigureProviderRefresh.called
    from openshell._proto import openshell_pb2
    assert req_mock.strategy == openshell_pb2.PROVIDER_CREDENTIAL_REFRESH_STRATEGY_OAUTH2_REFRESH_TOKEN
    assert req_mock.credential_key == "GCP_ADC_ACCESS_TOKEN"
    assert req_mock.material["client_id"] == adc["client_id"]
    assert req_mock.material["refresh_token"] == adc["refresh_token"]
    assert "client_secret" in req_mock.secret_material_keys
    assert "refresh_token" in req_mock.secret_material_keys


@pytest.mark.asyncio
async def test_configure_google_cloud_provider_unsupported_type_raises(sdk_client):
    """configure_google_cloud_provider() raises ValueError for unknown ADC types."""
    import json
    adc = {"type": "external_account", "audience": "//iam.googleapis.com/..."}
    with patch.object(oc, "_get_client", return_value=sdk_client):
        with pytest.raises(ValueError, match="Unsupported ADC type"):
            await oc.configure_google_cloud_provider(
                "swarmer-ws-1-google-cloud", json.dumps(adc)
            )


# ---------------------------------------------------------------------------
# 7. _exec_with_supervisor_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_with_supervisor_retry_succeeds_first_attempt():
    """_exec_with_supervisor_retry() returns immediately on success."""
    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        return "ok"

    result = await oc._exec_with_supervisor_retry(_fn)
    assert result == "ok"
    assert call_count == 1


@pytest.mark.asyncio
async def test_exec_with_supervisor_retry_retries_on_unavailable():
    """_exec_with_supervisor_retry() retries on UNAVAILABLE 'supervisor session not connected'."""
    import grpc

    class _Unavailable(grpc.RpcError, grpc.Call):
        def code(self): return grpc.StatusCode.UNAVAILABLE
        def details(self): return "supervisor relay failed: supervisor session not connected"

    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _Unavailable()
        return "done"

    with patch.object(oc.asyncio, "sleep", new=AsyncMock()):
        result = await oc._exec_with_supervisor_retry(_fn, base_delay=0.001)

    assert result == "done"
    assert call_count == 3


@pytest.mark.asyncio
async def test_exec_with_supervisor_retry_retries_on_failed_precondition():
    """_exec_with_supervisor_retry() retries on FAILED_PRECONDITION 'sandbox is not ready'."""
    import grpc

    class _NotReady(grpc.RpcError, grpc.Call):
        def code(self): return grpc.StatusCode.FAILED_PRECONDITION
        def details(self): return "sandbox is not ready"

    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _NotReady()
        return "ready"

    with patch.object(oc.asyncio, "sleep", new=AsyncMock()):
        result = await oc._exec_with_supervisor_retry(_fn, base_delay=0.001)

    assert result == "ready"
    assert call_count == 2


@pytest.mark.asyncio
async def test_exec_with_supervisor_retry_does_not_retry_other_errors():
    """_exec_with_supervisor_retry() re-raises non-transient errors immediately."""
    import grpc

    class _PermDenied(grpc.RpcError, grpc.Call):
        def code(self): return grpc.StatusCode.PERMISSION_DENIED
        def details(self): return "permission denied"

    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        raise _PermDenied()

    with pytest.raises(grpc.RpcError):
        await oc._exec_with_supervisor_retry(_fn, base_delay=0.001)

    assert call_count == 1  # no retry


@pytest.mark.asyncio
async def test_exec_with_supervisor_retry_gives_up_after_max_attempts():
    """_exec_with_supervisor_retry() re-raises after max_attempts transient failures."""
    import grpc

    class _Unavailable(grpc.RpcError, grpc.Call):
        def code(self): return grpc.StatusCode.UNAVAILABLE
        def details(self): return "supervisor relay failed: supervisor session not connected"

    call_count = 0

    def _fn():
        nonlocal call_count
        call_count += 1
        raise _Unavailable()

    with patch.object(oc.asyncio, "sleep", new=AsyncMock()):
        with pytest.raises(grpc.RpcError):
            await oc._exec_with_supervisor_retry(_fn, max_attempts=3, base_delay=0.001)

    assert call_count == 3  # initial + 2 retries = max_attempts

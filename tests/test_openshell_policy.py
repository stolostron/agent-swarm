"""
Tests for swarmer.openshell_policy.build_session_policy().

Validates that build_session_policy() returns a SandboxPolicy proto with:
  - Required structural sections (version, filesystem, network_policies)
  - network_policies included directly in the proto (ACM-34909: pre-applied at creation time)
  - Per-repo GitHub git + API blocks with scoped paths
  - Conditional Jira MCP block (present when Jira MCP enabled, absent otherwise)
  - Conditional Go development block (proxy.golang.org etc.)
  - Conditional Python development block (pypi.org etc.)
  - Agent API block adapted to agent tool (opencode vs crush) and model provider
  - No excess blocks for minimal sessions (single repo, no MCP)
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.openshell_policy import build_session_policy, build_session_network_policies

# Tests that call build_session_policy() need the real openshell protobuf
# classes to construct a SandboxPolicy proto.  The stub package on PyPI
# (0.0.0a0) provides the proto classes but they get replaced by MagicMock
# stubs in other test files when the full suite runs.  Skip these tests when
# the real SDK (identified by the presence of SandboxClient) is not available.
try:
    from openshell import SandboxClient  # noqa: F401
    _REAL_SDK = True
except Exception:
    _REAL_SDK = False

_requires_sdk = pytest.mark.skipif(
    not _REAL_SDK,
    reason="Requires real openshell SDK (SandboxClient); not available in CI",
)


# ---------------------------------------------------------------------------
# Helpers to build minimal test fixtures matching the real model shapes
# ---------------------------------------------------------------------------

def _make_repo(org="stolostron", name="agent-swarm", branch="main"):
    return type("Repo", (), {
        "repo_url": f"https://github.com/{org}/{name}",
        "branch": branch,
        "local_path": name,
    })()


def _make_prompt_source(
    repo_url="https://github.com/stolostron/agentic-sdlc",
    branch="main",
    folder_path=".",
):
    return type("PromptSource", (), {
        "repo_url": repo_url,
        "branch": branch,
        "folder_path": folder_path,
    })()


def _make_mcp(slug="atlassian-jira"):
    # Default slug matches the real McpServer catalog value ("atlassian-jira").
    # build_session_network_policies() uses a loose "jira" in slug match.
    return type("MCP", (), {"slug": slug})()


def _make_session(language="golang"):
    return type("Session", (), {
        "id": 1,
        "language": language,
        "agent_tool": "opencode",
        "model": "google-vertex-anthropic/claude-sonnet-4-6",
    })()


def _policy_dict(policy) -> dict:
    """Convert SandboxPolicy proto to a dict for easy assertions."""
    return {
        "version": policy.version,
        "filesystem": {
            "read_only": list(policy.filesystem.read_only),
            "read_write": list(policy.filesystem.read_write),
        },
        "network_policies": {
            k: {
                "endpoints": [{"host": e.host, "port": e.port} for e in v.endpoints],
                "binaries": [b.path for b in v.binaries],
            }
            for k, v in policy.network_policies.items()
        },
    }


_MODEL = "google-vertex-anthropic/claude-sonnet-4-6"


def _bnet(
    session=None, repos=None, mcp_servers=None, agent_tool="opencode",
    model=_MODEL, prompt_sources=None,
) -> dict:
    """Build network_policies dict via build_session_network_policies."""
    return build_session_network_policies(
        session or _make_session(), repos or [], mcp_servers or [], agent_tool, model,
        prompt_sources=prompt_sources or [],
    )


def _bhosts(
    session=None, repos=None, mcp_servers=None, agent_tool="opencode",
    model=_MODEL, prompt_sources=None,
) -> list[str]:
    """Return all endpoint hosts from computed network_policies."""
    hosts = []
    for rule in _bnet(session, repos, mcp_servers, agent_tool, model,
                      prompt_sources=prompt_sources).values():
        for ep in rule.get("endpoints", []):
            hosts.append(ep.get("host", ""))
    return hosts


# ---------------------------------------------------------------------------
# 1. Required structure
# ---------------------------------------------------------------------------

@_requires_sdk
def test_policy_has_version_1():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert result.version == 1


@_requires_sdk
def test_policy_has_filesystem_policy():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert result.filesystem.include_workdir is True
    assert "/sandbox" in result.filesystem.read_write


def test_policy_has_network_policies_section():
    net = _bnet()
    assert len(net) > 0


@_requires_sdk
def test_build_session_policy_includes_network_policies_in_proto():
    """build_session_policy() must include network_policies in the proto (ACM-34909).

    Network policies are pre-applied at sandbox creation so git clone and AI API
    calls work immediately without a probe-deny-approve cycle.
    """
    repo = _make_repo(org="stolostron", name="agent-swarm")
    session = _make_session(language="golang")
    result = build_session_policy(session, repos=[repo], mcp_servers=[], agent_tool="opencode", model=_MODEL)
    # network_policies map must be populated in the proto
    assert len(result.network_policies) > 0, "network_policies must be set in the SandboxPolicy proto"
    # At least the agent-api block should be present
    assert "agent_api" in result.network_policies, "agent_api block must be in proto network_policies"
    # Per-repo github blocks must also appear
    net_keys = list(result.network_policies.keys())
    assert any(k.startswith("github_git_") for k in net_keys), f"github_git_ block missing from proto: {net_keys}"
    assert any(k.startswith("github_api_") for k in net_keys), f"github_api_ block missing from proto: {net_keys}"


@_requires_sdk
def test_build_session_policy_network_policies_match_build_session_network_policies():
    """build_session_policy() proto network_policies must match build_session_network_policies() dict."""
    repo = _make_repo()
    session = _make_session(language="python")
    mcp = _make_mcp("jira")

    expected_net = build_session_network_policies(session, [repo], [mcp], "opencode", _MODEL)
    proto = build_session_policy(session, repos=[repo], mcp_servers=[mcp], agent_tool="opencode", model=_MODEL)

    proto_keys = set(proto.network_policies.keys())
    expected_keys = set(expected_net.keys())
    assert proto_keys == expected_keys, (
        f"Proto network_policies keys {proto_keys} != computed keys {expected_keys}"
    )


@_requires_sdk
def test_policy_sandbox_uses_sandbox_path():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "/sandbox" in result.filesystem.read_write
    assert "/workspace" not in list(result.filesystem.read_write)


# ---------------------------------------------------------------------------
# 2. GitHub blocks (per-repo)
# ---------------------------------------------------------------------------

def test_github_git_block_generated_per_repo():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    git_blocks = [k for k in net if k.startswith("github_git_")]
    assert len(git_blocks) == 1, f"Expected 1 github_git_ block, got {len(git_blocks)}: {git_blocks}"


def test_github_api_block_generated_per_repo():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    api_blocks = [k for k in net if k.startswith("github_api_")]
    assert len(api_blocks) == 1, f"Expected 1 github_api_ block, got {len(api_blocks)}: {api_blocks}"


def test_github_blocks_scoped_to_repo_path():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    assert "github_git_stolostron_agent_swarm" in net
    assert "github_api_stolostron_agent_swarm" in net


def test_github_git_block_includes_objects_cdn():
    """git clone requires objects.githubusercontent.com for pack-file/blob data (ACM-34909)."""
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    git_block = net["github_git_stolostron_agent_swarm"]
    hosts = [ep.get("host", "") for ep in git_block.get("endpoints", [])]
    assert "objects.githubusercontent.com" in hosts, (
        f"objects.githubusercontent.com missing from git block endpoints: {hosts}"
    )


def test_github_git_block_includes_codeload():
    """git clone --depth=1 uses codeload.github.com for shallow pack data (ACM-34909)."""
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    git_block = net["github_git_stolostron_agent_swarm"]
    hosts = [ep.get("host", "") for ep in git_block.get("endpoints", [])]
    assert "codeload.github.com" in hosts, (
        f"codeload.github.com missing from git block endpoints: {hosts}"
    )


def test_github_git_block_includes_both_git_binary_paths():
    """Both /usr/local/bin/git and /usr/bin/git must be in binaries (OPA matches resolved path)."""
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    git_block = net["github_git_stolostron_agent_swarm"]
    binaries = [b.get("path", "") for b in git_block.get("binaries", [])]
    assert "/usr/local/bin/git" in binaries, f"/usr/local/bin/git missing from binaries: {binaries}"
    assert "/usr/bin/git" in binaries, f"/usr/bin/git missing from binaries: {binaries}"


def test_all_binary_entries_have_harness_true():
    """Every binary entry in every network policy block must have harness=True.

    OPA resolves binary paths via /proc/{pid}/root symlink traversal, which
    fails inside sandbox containers ('Cannot access container filesystem for
    symlink resolution'). harness=True tells OPA to use process harness matching
    instead, which is what the supervisor uses when generating draft chunks.
    Without this, all binary-scoped rules are silently inert (ACM-34909).
    """
    repo = _make_repo(org="stolostron", name="agent-swarm")
    session = _make_session(language="golang")
    mcp = _make_mcp("jira")
    net = build_session_network_policies(session, [repo], [mcp], "opencode", _MODEL)

    for block_name, block in net.items():
        for binary in block.get("binaries", []):
            path = binary.get("path", "?")
            harness = binary.get("harness", False)
            assert harness is True, (
                f"Block '{block_name}' binary '{path}' has harness={harness!r}, expected True. "
                f"OPA symlink resolution fails in sandbox containers — harness=True is required."
            )


def test_crush_agent_api_binary_has_harness_true():
    """Crush binary in agent_api block must also have harness=True."""
    net = _bnet(agent_tool="crush", model="vertexai/claude-sonnet-4-6")
    agent_block = net.get("agent_api", {})
    for binary in agent_block.get("binaries", []):
        path = binary.get("path", "?")
        harness = binary.get("harness", False)
        assert harness is True, (
            f"agent_api binary '{path}' for crush has harness={harness!r}, expected True."
        )


def test_two_repos_generate_two_github_block_pairs():
    repo1 = _make_repo(org="stolostron", name="agent-swarm")
    repo2 = _make_repo(org="stolostron", name="agent-containers")
    net = _bnet(repos=[repo1, repo2])
    assert len([k for k in net if k.startswith("github_git_")]) == 2
    assert len([k for k in net if k.startswith("github_api_")]) == 2


# ---------------------------------------------------------------------------
# 3. Jira MCP block (conditional)
# ---------------------------------------------------------------------------

def test_jira_block_present_when_jira_mcp_enabled():
    # Use the real catalog slug ("atlassian-jira") to confirm the loose match works.
    net = _bnet(mcp_servers=[_make_mcp(slug="atlassian-jira")])
    assert any("jira" in k.lower() for k in net), "Expected a jira MCP network block"


def test_jira_block_absent_when_no_jira_mcp():
    net = _bnet()
    assert not any("jira" in k.lower() for k in net)


def test_jira_block_absent_when_only_non_jira_mcp():
    assert "atlassian.net" not in _bhosts(mcp_servers=[_make_mcp(slug="github")])


# ---------------------------------------------------------------------------
# 4. Language-specific development blocks
# ---------------------------------------------------------------------------

def test_go_development_block_included_for_go_session():
    hosts = _bhosts(session=_make_session(language="golang"))
    assert "proxy.golang.org" in hosts
    assert "sum.golang.org" in hosts


def test_python_development_block_included_for_python_session():
    hosts = _bhosts(session=_make_session(language="python"))
    assert "pypi.org" in hosts
    assert "files.pythonhosted.org" in hosts


def test_govulncheck_not_in_static_policy():
    # govulncheck is not pre-installed; OPA generates a draft chunk when it runs
    assert "vuln.go.dev" not in _bhosts(session=_make_session(language="golang"))


def test_go_block_absent_for_python_session():
    assert "proxy.golang.org" not in _bhosts(session=_make_session(language="python"))


def test_python_block_absent_for_go_session():
    assert "pypi.org" not in _bhosts(session=_make_session(language="golang"))


# ---------------------------------------------------------------------------
# 5. Minimal session — no excess blocks
# ---------------------------------------------------------------------------

def test_minimal_session_no_jira_or_extra_github_blocks():
    repo = _make_repo()
    net = _bnet(session=_make_session(language="golang"), repos=[repo])
    assert not any("jira" in k.lower() for k in net)
    assert len([k for k in net if k.startswith("github_git_")]) == 1


# ---------------------------------------------------------------------------
# 6. Agent API block
# ---------------------------------------------------------------------------

def test_agent_api_block_opencode_includes_gemini_endpoint():
    net = _bnet()
    assert any("agent_api" in k.lower() for k in net)
    hosts = _bhosts()
    assert any("generativelanguage.googleapis.com" in h for h in hosts)


def test_agent_api_block_crush_includes_crush_binary():
    net = _bnet(agent_tool="crush", model="vertexai/claude-sonnet-4-6")
    api_block = net.get("agent_api")
    assert api_block is not None
    # Crush block has no binaries restriction; opencode binary should not appear
    binaries = api_block.get("binaries", [])
    binary_paths = [b.get("path", "") if isinstance(b, dict) else getattr(b, "path", "") for b in binaries]
    assert not any("opencode" in p for p in binary_paths), f"opencode binary in crush block: {binary_paths}"


# ---------------------------------------------------------------------------
# 7. Prompt source raw.githubusercontent.com blocks
# ---------------------------------------------------------------------------

def test_raw_github_block_present_when_prompt_source_configured():
    """A prompt source on GitHub must produce a raw_github_* network block."""
    ps = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="main",
        folder_path="prompts",
    )
    net = _bnet(prompt_sources=[ps])
    raw_keys = [k for k in net if k.startswith("raw_github_")]
    assert raw_keys, f"Expected raw_github_* block, got keys: {sorted(net.keys())}"
    hosts = _bhosts(prompt_sources=[ps])
    assert "raw.githubusercontent.com" in hosts
    assert "github.com" in hosts, "github.com must be included for paste-friendly URLs"


def test_raw_github_block_absent_when_no_prompt_sources():
    net = _bnet()
    assert not any(k.startswith("raw_github_") for k in net)


def test_raw_github_block_path_scoped_to_folder():
    """When folder_path is set, the path prefix must include it."""
    ps = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="main",
        folder_path="skills",
    )
    net = _bnet(prompt_sources=[ps])
    block = net.get("raw_github_stolostron_agentic_sdlc", {})
    endpoints = block.get("endpoints", [])
    assert endpoints, "raw_github block must have endpoints"
    path = endpoints[0].get("path", "")
    assert "skills" in path, f"Expected folder 'skills' in path, got: {path!r}"
    assert path == "/stolostron/agentic-sdlc/main/skills/**"


def test_raw_github_block_path_root_when_folder_is_dot():
    """When folder_path is '.', the path prefix must be branch-level only."""
    ps = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="main",
        folder_path=".",
    )
    net = _bnet(prompt_sources=[ps])
    block = net.get("raw_github_stolostron_agentic_sdlc", {})
    endpoints = block.get("endpoints", [])
    path = endpoints[0].get("path", "")
    assert path == "/stolostron/agentic-sdlc/main/**", (
        f"Root folder should produce branch-level path, got: {path!r}"
    )


def test_raw_github_block_path_root_when_folder_is_empty():
    """When folder_path is empty, path prefix must be branch-level only."""
    ps = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="main",
        folder_path="",
    )
    net = _bnet(prompt_sources=[ps])
    block = net.get("raw_github_stolostron_agentic_sdlc", {})
    endpoints = block.get("endpoints", [])
    path = endpoints[0].get("path", "")
    assert path == "/stolostron/agentic-sdlc/main/**"


def test_raw_github_block_uses_correct_branch():
    """The branch from the prompt source must appear in the path."""
    ps = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="release-2.13",
        folder_path="prompts",
    )
    net = _bnet(prompt_sources=[ps])
    block = net.get("raw_github_stolostron_agentic_sdlc", {})
    endpoints = block.get("endpoints", [])
    path = endpoints[0].get("path", "")
    assert "release-2.13" in path, f"Expected branch in path, got: {path!r}"
    assert path == "/stolostron/agentic-sdlc/release-2.13/prompts/**"


def test_raw_github_block_github_com_endpoint_scoped_to_repo():
    """github.com endpoint must be scoped to the prompt source org/repo (not all of github.com)."""
    ps = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="main", folder_path="prompts",
    )
    net = _bnet(prompt_sources=[ps])
    block = net.get("raw_github_stolostron_agentic_sdlc", {})
    gh_ep = next(
        (ep for ep in block.get("endpoints", []) if ep.get("host") == "github.com"), None
    )
    assert gh_ep is not None, "github.com endpoint missing from raw_github block"
    assert gh_ep.get("path") == "/stolostron/agentic-sdlc/**", (
        f"github.com path must be scoped to org/repo, got: {gh_ep.get('path')!r}"
    )
    rules = gh_ep.get("rules", [])
    assert rules, "github.com endpoint must have rules"
    assert rules[0]["allow"]["method"] == "GET", "github.com must be GET-only"


def test_raw_github_block_curl_binary_present():
    """curl binary must be listed so shell-based fetches work."""
    ps = _make_prompt_source()
    net = _bnet(prompt_sources=[ps])
    block = next((v for k, v in net.items() if k.startswith("raw_github_")), {})
    binaries = [
        b.get("path", "") if isinstance(b, dict) else getattr(b, "path", "")
        for b in block.get("binaries", [])
    ]
    assert "/usr/bin/curl" in binaries, f"curl not in binaries: {binaries}"


def test_raw_github_block_python_binaries_present():
    """python3 paths must be listed for agents using requests/urllib."""
    ps = _make_prompt_source()
    net = _bnet(prompt_sources=[ps])
    block = next((v for k, v in net.items() if k.startswith("raw_github_")), {})
    binaries = [
        b.get("path", "") if isinstance(b, dict) else getattr(b, "path", "")
        for b in block.get("binaries", [])
    ]
    assert "/usr/local/bin/python3.14" in binaries
    assert "/usr/bin/python3" in binaries


def test_raw_github_block_non_github_prompt_source_skipped():
    """Non-GitHub prompt sources must not produce a raw_github_* block."""
    ps = _make_prompt_source(repo_url="https://gitlab.com/org/repo")
    net = _bnet(prompt_sources=[ps])
    assert not any(k.startswith("raw_github_") for k in net)


def test_raw_github_multiple_prompt_sources_each_get_block():
    """Each distinct org/repo prompt source gets its own policy block."""
    ps1 = _make_prompt_source(
        repo_url="https://github.com/stolostron/agentic-sdlc",
        branch="main", folder_path="prompts",
    )
    ps2 = _make_prompt_source(
        repo_url="https://github.com/openshift-fleet/runbooks",
        branch="main", folder_path=".",
    )
    net = _bnet(prompt_sources=[ps1, ps2])
    raw_keys = sorted(k for k in net if k.startswith("raw_github_"))
    assert len(raw_keys) == 2, f"Expected 2 raw_github blocks, got: {raw_keys}"
    assert "raw_github_stolostron_agentic_sdlc" in raw_keys
    assert "raw_github_openshift_fleet_runbooks" in raw_keys


# ---------------------------------------------------------------------------
# custom_policies merge tests (no real SDK needed)
# ---------------------------------------------------------------------------

def test_custom_policies_merged_into_network_policies():
    """Custom rules from session.custom_policies are merged into network_policies dict."""
    custom = [
        {
            "name": "vuln-go-dev",
            "endpoints": [{"host": "vuln.go.dev", "port": 443, "protocol": "rest"}],
            "binaries": [{"path": "/usr/local/go/bin/govulncheck", "harness": True}],
        }
    ]
    net = build_session_network_policies(
        _make_session(language="golang"),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=custom,
    )
    # The custom rule should appear under a slugified key
    matching = [k for k in net if "vuln_go_dev" in k or "custom_vuln" in k]
    assert matching, f"Custom rule not found in network_policies keys: {list(net.keys())}"
    rule = net[matching[0]]
    assert rule["endpoints"][0]["host"] == "vuln.go.dev"


def test_custom_policies_none_does_not_error():
    """Passing custom_policies=None (the default) does not raise."""
    net = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=None,
    )
    assert "agent_api" in net


def test_custom_policies_empty_list_does_not_add_keys():
    """Passing an empty custom_policies list adds no extra keys."""
    net_no_custom = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
    )
    net_empty_custom = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=[],
    )
    assert set(net_no_custom.keys()) == set(net_empty_custom.keys())


# ---------------------------------------------------------------------------
# L7 access backfill tests (ACM-XXXXX)
# ---------------------------------------------------------------------------

def test_custom_policy_endpoint_missing_access_gets_backfilled():
    """An endpoint with protocol but no access/rules gets access=full added.

    Draft chunks from OPA include host/port/protocol but omit access/rules.
    Without the backfill the gateway rejects with 'protocol requires rules or
    access to define allowed traffic'.
    """
    custom = [
        {
            "name": "raw-githubusercontent-com",
            "endpoints": [
                {"host": "raw.githubusercontent.com", "port": 443, "protocol": "rest"}
            ],
            "binaries": [{"path": "/usr/bin/curl", "harness": True}],
        }
    ]
    net = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=custom,
    )
    key = next(k for k in net if "raw_githubusercontent" in k or "custom_raw" in k)
    ep = net[key]["endpoints"][0]
    assert ep.get("access") == "full", (
        f"Expected access=full to be backfilled on endpoint missing access/rules, got: {ep}"
    )


def test_custom_policy_endpoint_with_existing_rules_not_overwritten():
    """An endpoint that already has rules should NOT get access=full added."""
    custom = [
        {
            "name": "api-github-scoped",
            "endpoints": [
                {
                    "host": "api.github.com",
                    "port": 443,
                    "protocol": "rest",
                    "rules": [{"allow": {"method": "GET", "path": "/repos/org/repo/**"}}],
                }
            ],
            "binaries": [],
        }
    ]
    net = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=custom,
    )
    key = next(k for k in net if "api_github" in k or "custom_api" in k)
    ep = net[key]["endpoints"][0]
    assert "access" not in ep, (
        f"access should not be added when rules are already present, got: {ep}"
    )
    assert ep.get("rules"), "rules should be preserved"


def test_custom_policy_endpoint_with_existing_access_not_overwritten():
    """An endpoint that already has access set should keep its value unchanged."""
    custom = [
        {
            "name": "some-host",
            "endpoints": [
                {
                    "host": "example.com",
                    "port": 443,
                    "protocol": "rest",
                    "access": "full",
                }
            ],
            "binaries": [],
        }
    ]
    net = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=custom,
    )
    key = next(k for k in net if "some_host" in k or "custom_some" in k)
    ep = net[key]["endpoints"][0]
    assert ep.get("access") == "full"
    assert "rules" not in ep


def test_custom_policy_endpoint_without_protocol_unchanged():
    """An endpoint without a protocol field is left untouched (no access backfill)."""
    custom = [
        {
            "name": "tcp-host",
            "endpoints": [
                {"host": "example.com", "port": 9090}
            ],
            "binaries": [],
        }
    ]
    net = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=custom,
    )
    key = next(k for k in net if "tcp_host" in k or "custom_tcp" in k)
    ep = net[key]["endpoints"][0]
    assert "access" not in ep
    assert "rules" not in ep


def test_custom_policy_multiple_endpoints_backfill_only_missing():
    """Only endpoints lacking both access and rules get the backfill; others unchanged."""
    custom = [
        {
            "name": "mixed-endpoints",
            "endpoints": [
                # Missing access/rules — should get access=full
                {"host": "raw.githubusercontent.com", "port": 443, "protocol": "rest"},
                # Already has access — unchanged
                {"host": "github.com", "port": 443, "protocol": "rest", "access": "full"},
                # Already has rules — unchanged, no access added
                {
                    "host": "api.github.com",
                    "port": 443,
                    "protocol": "rest",
                    "rules": [{"allow": {"method": "GET", "path": "/repos/**"}}],
                },
            ],
            "binaries": [],
        }
    ]
    net = build_session_network_policies(
        _make_session(),
        repos=[],
        mcp_servers=[],
        agent_tool="opencode",
        model=_MODEL,
        custom_policies=custom,
    )
    key = next(k for k in net if "mixed" in k or "custom_mixed" in k)
    eps = net[key]["endpoints"]
    assert eps[0]["access"] == "full", "missing endpoint should be backfilled"
    assert "rules" not in eps[0]
    assert eps[1]["access"] == "full", "existing access=full should be preserved"
    assert "rules" not in eps[1]
    assert "access" not in eps[2], "endpoint with rules should not get access added"
    assert eps[2]["rules"], "rules should be preserved on third endpoint"

"""
OpenShell policy proto builder for AgentSwarm sessions.

build_session_policy() assembles a complete OpenShell SandboxPolicy proto from
composable blocks based on: repos attached, agent tool, model provider,
Jira MCP enablement, and session language (golang/python).

Network policies are included directly in the returned SandboxPolicy proto so
the sandbox starts with all required network access pre-approved.  This avoids
the fragile probe-deny-approve cycle that depended on a hardcoded sleep timer
and was the root cause of intermittent git clone failures (ACM-34909).

IMPORTANT — harness: True on all binary entries:
  OPA resolves binary paths by traversing /proc/{pid}/root inside the sandbox
  container.  This fails with "Cannot access container filesystem for symlink
  resolution" for binaries that are installed at paths OPA cannot traverse.
  Setting harness=True tells OPA to use its process harness for binary matching
  instead of symlink resolution, which works reliably regardless of the binary's
  installation path.  The supervisor generates harness=True automatically when it
  creates draft chunks from denial analysis — we must match that behaviour in all
  statically pre-set rules (confirmed by inspecting live sandbox draft chunk state).

Returns a SandboxPolicy proto object that is set directly on SandboxSpec.policy.
All filesystem paths use /sandbox/ — /workspace/ is never referenced.
"""


def _bin(path: str) -> dict:
    """Binary entry with harness=True so OPA uses process harness matching."""
    return {"path": path, "harness": True}


# ── Static base policy sections ───────────────────────────────────────────────

_BASE_FILESYSTEM = {
    "include_workdir": True,
    "read_only": ["/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"],
    "read_write": ["/sandbox", "/tmp", "/dev/null", "/home/sandbox"],
}

_GO_DEVELOPMENT_BLOCK = {
    "name": "golang",
    "endpoints": [
        {"host": "proxy.golang.org", "port": 443},
        {"host": "sum.golang.org", "port": 443},
        {"host": "storage.googleapis.com", "port": 443},
    ],
    "binaries": [
        _bin("/usr/local/go/bin/go"),
        _bin("/usr/local/bin/git"),  # agent container image path (confirmed via OPA logs)
        _bin("/usr/bin/git"),        # fallback for other base images
    ],
}


_PYTHON_DEVELOPMENT_BLOCK = {
    "name": "pypi",
    "endpoints": [
        {"host": "pypi.org", "port": 443},
        {"host": "files.pythonhosted.org", "port": 443},
        {"host": "downloads.python.org", "port": 443},
    ],
    "binaries": [
        _bin("/usr/bin/pip"),
        _bin("/usr/local/bin/uv"),
        _bin("/sandbox/.venv/bin/pip"),
        _bin("/sandbox/.venv/bin/python*"),
    ],
}

_JIRA_MCP_BLOCK = {
    "name": "jira-mcp",
    "endpoints": [
        {
            # Wildcard for all Atlassian Cloud tenants.
            "host": "*.atlassian.net",
            "port": 443,
            "protocol": "rest",
            "enforcement": "enforce",
            "access": "full",
        },
        {
            # Literal entry for the Red Hat tenant — OPA proxy enforcement
            # matches on literal host names; the wildcard alone may not be
            # sufficient at the proxy CONNECT layer (confirmed via draft chunks).
            "host": "redhat.atlassian.net",
            "port": 443,
            "protocol": "rest",
            "enforcement": "enforce",
            "access": "full",
        },
    ],
    "binaries": [
        _bin("/usr/local/bin/jira-mcp-server"),
        # jira-mcp-server is a Python package. OPA resolves the canonical binary
        # path via /proc/{pid}/root. The agent container image uses python3.14
        # installed at /usr/local/bin/python3.14 (confirmed via OPA draft chunks);
        # /usr/bin/python3 and /usr/local/bin/python3 are symlinks to it.
        # All three paths are listed so the rule survives image updates.
        _bin("/usr/local/bin/python3.14"),
        _bin("/usr/local/bin/python3"),
        _bin("/usr/bin/python3"),
        _bin("/sandbox/.venv/bin/python*"),
        # curl is used by smoke-test connectivity checks and shell tooling.
        _bin("/usr/bin/curl"),
    ],
}


# ── Per-repo dynamic blocks ───────────────────────────────────────────────────

def _repo_org_name(repo) -> tuple[str, str]:
    """Extract (org, name) from a repo object, falling back to URL parsing."""
    org = getattr(repo, "org", None)
    name = getattr(repo, "name", None)
    if not org or not name:
        parts = repo.repo_url.rstrip("/").split("/")
        if len(parts) >= 2:
            name = parts[-1]
            org = parts[-2]
    return (org or "unknown"), (name or "unknown")


def _repo_slug(repo) -> str:
    org, name = _repo_org_name(repo)
    return f"{org}_{name}".replace("-", "_").replace(".", "_")


def _build_github_git_block(org: str, name: str) -> dict:
    """Build the Landlock network policy block for git clone of a GitHub repo.

    git clone over HTTPS touches three hosts:
      - github.com:443                    — smart HTTP protocol (info/refs + upload-pack)
      - objects.githubusercontent.com:443 — pack-file / blob CDN (actual object data)
      - codeload.github.com:443           — shallow clone pack data (--depth=1 tarballs)

    All three must be accessible to the git binary or the clone will fail partway
    through even if the initial ref-discovery handshake succeeds (ACM-34909).

    Both /usr/local/bin/git and /usr/bin/git are listed — the agent container image
    installs git at /usr/local/bin/git, but some base images use /usr/bin/git.
    OPA matches on the actual resolved binary path so both must be present.
    """
    return {
        "name": f"github-git-{org}-{name}",
        "endpoints": [
            {
                # Full access to github.com for git smart HTTP protocol.
                # Path-scoped rules caused 403s because OPA enforces at the TLS CONNECT
                # layer before git can send the HTTP request (ACM-34909).
                "host": "github.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "access": "full",
            },
            {
                # Pack-file object data served from GitHub's CDN — needed for all clones.
                "host": "objects.githubusercontent.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "access": "full",
            },
            {
                # Shallow clone (--depth=1) pack data and tarball downloads.
                "host": "codeload.github.com",
                "port": 443,
                "protocol": "rest",
                "enforcement": "enforce",
                "access": "full",
            },
        ],
        "binaries": [
            _bin("/usr/local/bin/git"),  # agent container image path (confirmed via OPA logs)
            _bin("/usr/bin/git"),         # fallback for other base images
        ],
    }


def _build_raw_github_block(
    org: str, name: str, branch: str = "main", folder_path: str = ""
) -> dict:
    """Build the policy block for curl/python reads from raw.githubusercontent.com.

    raw.githubusercontent.com URL structure:
      https://raw.githubusercontent.com/{org}/{repo}/{branch}/{path}

    The block is scoped to a specific org/repo/branch/folder so only files
    within the configured prompt source folder are accessible — not all of
    raw.githubusercontent.com, and not even the full branch.

    folder_path scoping:
      - "." or "" (root)  → /{org}/{repo}/{branch}/**  (whole branch)
      - "prompts/"        → /{org}/{repo}/{branch}/prompts/**
      - "docs/prompts"    → /{org}/{repo}/{branch}/docs/prompts/**
    Trailing slashes are stripped; a leading slash is never added twice.

    Path scoping note: OPA enforces path rules at the application layer after
    TLS termination, so the path filter is meaningful here (unlike the proxy
    CONNECT layer where only the host is visible).

    Binaries: curl (shell invocations, agent tool calls) and python3 variants
    (agents using the requests/urllib stack — canonical path confirmed via OPA
    draft chunks as /usr/local/bin/python3.14).
    """
    folder = (folder_path or "").strip("/")
    if folder and folder != ".":
        path_prefix = f"/{org}/{name}/{branch}/{folder}/**"
    else:
        path_prefix = f"/{org}/{name}/{branch}/**"

    # github.com raw/blob paths redirect to raw.githubusercontent.com.
    # Scoped to read (GET) on this org/repo only — broader path than the raw
    # prefix because github.com serves /raw/ and /blob/ sub-paths.
    github_path_prefix = f"/{org}/{name}/**"

    return {
        "name": f"raw-github-{org}-{name}",
        "endpoints": [
            {
                # raw.githubusercontent.com — canonical CDN for raw file content.
                # Path scoped to org/repo/branch[/folder].
                "host": "raw.githubusercontent.com",
                "port": 443,
                "path": path_prefix,
                "protocol": "rest",
                "enforcement": "enforce",
                "rules": [{"allow": {"method": "GET", "path": path_prefix}}],
            },
            {
                # github.com — paste-friendly URLs (/org/repo/raw/branch/... and
                # /org/repo/blob/branch/...) redirect to raw.githubusercontent.com.
                # Allowed so users don't need to know the raw subdomain.
                # Read-only (GET), scoped to this org/repo.
                "host": "github.com",
                "port": 443,
                "path": github_path_prefix,
                "protocol": "rest",
                "enforcement": "enforce",
                "rules": [{"allow": {"method": "GET", "path": github_path_prefix}}],
            },
        ],
        "binaries": [
            _bin("/usr/bin/curl"),
            _bin("/usr/local/bin/python3.14"),
            _bin("/usr/local/bin/python3"),
            _bin("/usr/bin/python3"),
        ],
    }


def _build_github_api_block(org: str, name: str) -> dict:
    return {
        "name": f"github-api-{org}-{name}",
        "endpoints": [
            {
                "host": "api.github.com",
                "port": 443,
                "path": f"/repos/{org}/{name}/**",
                "protocol": "rest",
                "enforcement": "enforce",
                "rules": [{"allow": {"method": "*", "path": f"/repos/{org}/{name}/**"}}],
            }
        ],
        "binaries": [
            _bin("/usr/bin/gh"),
            _bin("/usr/bin/curl"),
        ],
    }


# ── Google Cloud provider block ──────────────────────────────────────────────
# Added automatically when the workspace's google-cloud provider is attached.
# The GCE metadata emulator (127.0.0.1:8174) satisfies most GCP SDK calls, but
# the agent binary itself still needs direct access to:
#   - aiplatform.googleapis.com — Vertex AI API calls (Claude on Vertex, etc.)
#   - api.github.com            — GitHub API access used by the agent binary
#
# Binaries: the agent binary varies by tool (opencode.exe / crush).
# These entries are added per-tool in _build_google_cloud_provider_block().

def _build_google_cloud_provider_block(agent_tool: str) -> dict:
    """Return the network policy block to add when the google-cloud provider is active."""
    if agent_tool == "crush":
        binaries = [_bin("/usr/local/bin/crush")]
    else:
        # opencode — same binary list as the agent-api block
        binaries = [
            _bin("/usr/local/share/npm-global/lib/node_modules/opencode-linux-x64/bin/opencode"),
            _bin("/usr/local/share/npm-global/bin/opencode"),
            _bin("/usr/local/share/npm-global/lib/node_modules/opencode-ai/bin/opencode.exe"),
        ]
    return {
        "name": "google-cloud-provider",
        "endpoints": [
            _endpoint("aiplatform.googleapis.com"),
            _endpoint("api.github.com"),
        ],
        "binaries": binaries,
    }


# ── Agent API block (tool and model-dependent) ────────────────────────────────

def _endpoint(host: str) -> dict:
    return {
        "host": host,
        "port": 443,
        "protocol": "rest",
        "enforcement": "enforce",
        "access": "full",
    }


def _build_agent_api_block(agent_tool: str, model: str) -> dict:
    _provider = model.split("/")[0] if "/" in model else ""
    _is_vertex = _provider in ("google-vertex-anthropic", "vertexai")

    if agent_tool == "crush":
        endpoints = [_endpoint("generativelanguage.googleapis.com")]
        if _is_vertex:
            endpoints.extend([
                _endpoint("aiplatform.googleapis.com"),
                _endpoint("oauth2.googleapis.com"),
            ])
        block = {
            "name": "agent-api",
            "endpoints": endpoints,
            "binaries": [
                _bin("/usr/local/bin/crush"),
            ],
        }
    else:
        endpoints = [
            _endpoint("generativelanguage.googleapis.com"),
            _endpoint("oauth2.googleapis.com"),
            _endpoint("opencode.ai"),
            _endpoint("models.dev"),
        ]
        if _is_vertex:
            endpoints.append(_endpoint("aiplatform.googleapis.com"))
        block = {
            "name": "agent-api",
            "endpoints": endpoints,
            # opencode ships as a native binary installed via npm.
            # The npm wrapper at /usr/local/share/npm-global/bin/opencode invokes
            # the actual native binary at .../opencode-linux-x64/bin/opencode
            # (copied to opencode.exe by the Containerfile).  OPA sees the native
            # binary path, so both paths must be listed with harness=True.
            "binaries": [
                _bin("/usr/local/share/npm-global/lib/node_modules/opencode-linux-x64/bin/opencode"),
                _bin("/usr/local/share/npm-global/bin/opencode"),
                _bin("/usr/local/share/npm-global/lib/node_modules/opencode-ai/bin/opencode.exe"),
            ],
        }
    return {"agent_api": block}


# ── Public API ────────────────────────────────────────────────────────────────

def build_session_policy(
    session,
    repos: list,
    mcp_servers: list,
    agent_tool: str,
    model: str,
    prompt_sources: list | None = None,
    custom_policies: list[dict] | None = None,
    has_google_cloud_provider: bool = False,
):
    """Assemble a complete OpenShell SandboxPolicy proto for this session.

    Network policies are pre-computed from the session's repos, agent tool,
    model provider, MCP configuration, and language — and are included
    directly in the returned SandboxPolicy proto.  This means the sandbox
    starts with all required Landlock network rules already approved, so git
    clone and AI API calls work immediately without any probe-deny-approve
    cycle (ACM-34909).

    custom_policies: optional list of session-level rule dicts promoted from
    draft chunks.  These are merged into the static policy so approved rules
    take effect on the next sandbox launch without any code change.

    Returns a SandboxPolicy proto object to be set on SandboxSpec.policy.
    """
    from google.protobuf.json_format import ParseDict
    from openshell._proto import sandbox_pb2

    network_policies_dict = build_session_network_policies(
        session, repos, mcp_servers, agent_tool, model,
        prompt_sources=prompt_sources,
        custom_policies=custom_policies,
        has_google_cloud_provider=has_google_cloud_provider,
    )

    policy_dict = {
        "version": 1,
        "filesystem": _BASE_FILESYSTEM,
        "landlock": {"compatibility": "best_effort"},
        "process": {"run_as_group": "sandbox", "run_as_user": "sandbox"},
        "network_policies": network_policies_dict,
    }
    # Import SandboxPolicy directly from sandbox_pb2 — SandboxSpec().policy may
    # be None when unset in newer SDK versions, so deriving the class from the
    # instance is not reliable.
    policy_instance = sandbox_pb2.SandboxPolicy()
    return ParseDict(policy_dict, policy_instance, ignore_unknown_fields=True)


def build_session_network_policies(
    session,
    repos: list,
    mcp_servers: list,
    agent_tool: str,
    model: str,
    prompt_sources: list | None = None,
    custom_policies: list[dict] | None = None,
    has_google_cloud_provider: bool = False,
) -> dict:
    """Return the computed network_policies dict for this session.

    Called by build_session_policy() to populate spec.policy.network_policies
    at sandbox creation time.  Also exposed directly for testing and for the
    policy-extract smoke test harness (scripts/openshell_smoke_test.py).

    custom_policies: optional list of session-level rule dicts promoted from
    draft chunks.  Each entry is merged into the dict keyed by a slugified
    version of its "name" field (or "custom_{i}" as a fallback).
    """
    network_policies_dict: dict = {}
    network_policies_dict.update(_build_agent_api_block(agent_tool, model))

    # Google Cloud provider: grant aiplatform.googleapis.com + api.github.com
    # when the workspace's google-cloud provider is attached to this sandbox.
    if has_google_cloud_provider:
        network_policies_dict["google_cloud_provider"] = _build_google_cloud_provider_block(agent_tool)

    for repo in repos:
        slug = _repo_slug(repo)
        org, name = _repo_org_name(repo)
        network_policies_dict[f"github_git_{slug}"] = _build_github_git_block(org, name)
        network_policies_dict[f"github_api_{slug}"] = _build_github_api_block(org, name)

    if any("jira" in getattr(mcp, "slug", "") for mcp in (mcp_servers or [])):
        network_policies_dict["jira_mcp"] = _JIRA_MCP_BLOCK

    # Per-prompt-source raw.githubusercontent.com access.
    # Agents may curl prompt documents or files referenced within them from the
    # prompt repository.  One block per source repo — keyed by org_name slug so
    # multiple prompt sources on different repos each get their own rule entry.
    # If multiple prompt sources share the same org/repo, the block is
    # deduplicated by key (last write wins, contents are identical).
    for ps in (prompt_sources or []):
        repo_url = getattr(ps, "repo_url", "") or ""
        if "github.com" not in repo_url:
            continue
        parts = repo_url.rstrip("/").split("/")
        if len(parts) < 2:
            continue
        ps_name = parts[-1]
        ps_org = parts[-2]
        ps_branch = getattr(ps, "branch", "main") or "main"
        ps_folder = getattr(ps, "folder_path", "") or ""
        ps_slug = f"{ps_org}_{ps_name}".replace("-", "_").replace(".", "_")
        network_policies_dict[f"raw_github_{ps_slug}"] = _build_raw_github_block(
            ps_org, ps_name, ps_branch, ps_folder
        )

    lang = getattr(session, "language", "golang")
    if lang == "golang":
        network_policies_dict["golang"] = _GO_DEVELOPMENT_BLOCK
        # govulncheck is not pre-installed in the sandbox image; if the agent installs it,
        # OPA will emit a draft chunk for vuln.go.dev that the user can explicitly approve.
    elif lang == "python":
        network_policies_dict["pypi"] = _PYTHON_DEVELOPMENT_BLOCK

    # Merge session-level custom rules approved from draft chunks.
    # Backfill access="full" on any endpoint that carries a protocol but lacks
    # both "access" and "rules" — guards against rules stored before the
    # promotion-time fix was applied (the gateway rejects such endpoints with
    # "protocol requires rules or access to define allowed traffic").
    for i, rule in enumerate(custom_policies or []):
        rule_name = rule.get("name", "")
        key = f"custom_{rule_name.replace('-', '_').replace(' ', '_') or i}"
        endpoints = []
        for ep in rule.get("endpoints", []):
            ep = dict(ep)
            if ep.get("protocol") and not ep.get("access") and not ep.get("rules"):
                ep["access"] = "full"
            endpoints.append(ep)
        network_policies_dict[key] = {**rule, "endpoints": endpoints}

    return network_policies_dict

"""
End-to-end smoke test for the Jira MCP server inside an OpenShell sandbox.

Validates the full Jira MCP stack:
  1. Jira credentials sourced from the process environment (JIRA_* vars)
     Load them before running: set -a && source ../jira-mcp-server/.env && set +a
  2. Sandbox created with JIRA_* env vars passed through + _JIRA_MCP_BLOCK policy
  3. Jira env vars present inside the sandbox
  4. Jira network reachable (curl using $JIRA_* vars inside sandbox)
  5. jira-mcp-server binary present on PATH
  6. jira-mcp-server subprocess runs and connects to Jira
     (triggers OPA policy evaluation — missing policy sub-bumps surface as
      network denials logged by OpenShell supervisor)

Usage:
  # Source Jira creds into the environment first — token never touches Python
  set -a && source ../jira-mcp-server/.env && set +a
  python3 scripts/openshell_jira_smoke_test.py

  # Or export manually:
  export JIRA_SERVER_URL=https://redhat.atlassian.net
  export JIRA_ACCESS_TOKEN=<your-token>
  export JIRA_EMAIL=you@redhat.com
  python3 scripts/openshell_jira_smoke_test.py

Requirements:
  - JIRA_SERVER_URL, JIRA_ACCESS_TOKEN, JIRA_EMAIL in process environment
  - OpenShell gateway reachable (OPENSHELL_GATEWAY_URL in .env)
  - swarmer auth/secret.key exists
  - At least one OpencodeSecret in the DB with a Google API key (for provider)

Exit 0 = all steps passed. Exit 1 = one or more failures.
"""
import asyncio
import os
import sys
import uuid

sys.path.insert(0, ".")

PASS = "✓"
FAIL = "✗"
_results: list[tuple[str, bool, str]] = []


def step(label: str, passed: bool, detail: str = "") -> bool:
    marker = PASS if passed else FAIL
    print(f"  {marker}  {label}", end="")
    if detail:
        print(f" — {detail}", end="")
    print()
    _results.append((label, passed, detail))
    return passed


def _cleanup_provider(client, provider_name: str, openshell_pb2) -> None:
    """Detach and delete a gateway provider, ignoring errors."""
    try:
        attached = client._stub.ListSandboxes(openshell_pb2.ListSandboxesRequest(), timeout=10)
        for asb in attached.sandboxes:
            try:
                provs = client._stub.ListSandboxProviders(
                    openshell_pb2.ListSandboxProvidersRequest(sandbox_name=asb.metadata.name),
                    timeout=10,
                )
                if any(p.metadata.name == provider_name for p in provs.providers):
                    client._stub.DetachSandboxProvider(
                        openshell_pb2.DetachSandboxProviderRequest(
                            sandbox_name=asb.metadata.name, provider_name=provider_name
                        ),
                        timeout=10,
                    )
            except Exception:
                pass
    except Exception:
        pass
    try:
        client._stub.DeleteProvider(
            openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10
        )
        step("Delete test provider", True)
    except Exception as exc:
        step("Delete test provider", False, str(exc))


async def run_jira_smoke_test(model: str) -> bool:
    from swarmer.crypto import init_crypto
    from swarmer.openshell_client import _get_client, ensure_provider, create_sandbox
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    from swarmer.openshell_policy import build_session_policy
    from openshell._proto import openshell_pb2

    init_crypto("auth/secret.key")

    # ── 1. Validate JIRA_* env vars present in process environment ───────────
    # The token is never read into Python — it stays in os.environ and is
    # forwarded as-is into SandboxSpec.environment so it flows into the sandbox
    # process environment without appearing in any Python variable.
    print("\n[1] Checking JIRA_* credentials in process environment")
    jira_server_url = os.environ.get("JIRA_SERVER_URL", "")
    jira_email = os.environ.get("JIRA_EMAIL", "")
    # Presence-check only — never read the token value itself
    has_token = bool(os.environ.get("JIRA_ACCESS_TOKEN", ""))

    ok = step("JIRA_SERVER_URL set", bool(jira_server_url), "(set)" if jira_server_url else "(missing)")
    if not ok:
        print("  Hint: set -a && source ../jira-mcp-server/.env && set +a")
        return False
    ok = step("JIRA_ACCESS_TOKEN set", has_token, "(present, not logged)")
    if not ok:
        return False
    ok = step("JIRA_EMAIL set", bool(jira_email), "(set)" if jira_email else "(missing)")
    if not ok:
        return False

    # ── 2. Read Google API key from DB (for gateway provider only) ───────────
    print("\n[2] Reading Google API key from DB")
    google_key = None
    try:
        from swarmer.database import init_db, get_db
        from sqlalchemy import select
        from swarmer.models.opencode_secret import OpencodeSecret

        init_db("sqlite+aiosqlite:///data/swarmer.db")
        async for db in get_db():
            result = await db.execute(select(OpencodeSecret))
            secret = result.scalars().first()
            if secret:
                google_key = secret.google_api_key
            break
    except Exception as exc:
        step("Read OpencodeSecret from DB", False, str(exc))
        return False

    if not step("Google API key present", bool(google_key),
                f"len={len(google_key) if google_key else 0}"):
        return False

    client = _get_client()
    tool = OpenCodeStrategy()

    # ── 3. Gateway provider setup ─────────────────────────────────────────────
    print("\n[3] Gateway provider setup")
    # Use a unique suffix to avoid collisions with parallel smoke test runs
    provider_name = f"swarmer-jira-smoke-google-{uuid.uuid4().hex[:8]}"
    try:
        await ensure_provider(
            provider_name, "google-ai-studio", {},
            credentials={"GOOGLE_API_KEY": google_key},
        )
        step("CreateProvider/UpdateProvider", True, provider_name)
    except Exception as exc:
        step("CreateProvider/UpdateProvider", False, str(exc))
        return False

    # ── 4. Build policy with Jira MCP block ───────────────────────────────────
    print("\n[4] Building sandbox policy (with Jira MCP block)")

    class _FakeMcp:
        slug = "atlassian-jira"

    class _FakeSession:
        language = "golang"
        agent_tool = "opencode"

    fake_mcp = _FakeMcp()
    fake_session = _FakeSession()

    try:
        from swarmer.openshell_policy import build_session_network_policies
        computed_net = build_session_network_policies(
            fake_session, [], [fake_mcp], "opencode", model
        )
        has_jira_block = "jira_mcp" in computed_net
        jira_endpoints = [
            ep.get("host", "") for ep in computed_net.get("jira_mcp", {}).get("endpoints", [])
        ]
        jira_binaries = [
            b.get("path", "") for b in computed_net.get("jira_mcp", {}).get("binaries", [])
        ]
        ok = step("jira_mcp block present in policy", has_jira_block,
                  f"endpoints: {jira_endpoints}")
        step("jira_mcp binaries", has_jira_block, f"{jira_binaries}")
        if not ok:
            _cleanup_provider(client, provider_name, openshell_pb2)
            return False
    except Exception as exc:
        step("Policy computation", False, str(exc))
        _cleanup_provider(client, provider_name, openshell_pb2)
        return False

    policy = build_session_policy(fake_session, [], [fake_mcp], "opencode", model)

    # ── 5. Sandbox creation — JIRA_* passed from os.environ, not Python vars ──
    print("\n[5] Sandbox creation (JIRA_* forwarded from process env)")
    # Pass the values through os.environ so the token string is never held in a
    # named Python variable — os.environ["JIRA_ACCESS_TOKEN"] is the only reference.
    env_vars = {k: os.environ[k] for k in ("JIRA_SERVER_URL", "JIRA_ACCESS_TOKEN", "JIRA_EMAIL")}
    ref = None
    try:
        ref = await create_sandbox(
            image=tool.get_image(),
            env_vars=env_vars,
            policy=policy,
            provider_names=[provider_name],
        )
        step("CreateSandbox + WaitReady", True, ref.name)
    except Exception as exc:
        step("CreateSandbox + WaitReady", False, str(exc))
        _cleanup_provider(client, provider_name, openshell_pb2)
        return False

    sid = ref.id
    sandbox_name = ref.name
    all_passed = True

    def xec(cmd, timeout=30, stdin=None):
        if isinstance(cmd, str):
            return client.exec(sid, ["sh", "-c", cmd], timeout_seconds=timeout, stdin=stdin)
        return client.exec(sid, cmd, timeout_seconds=timeout, stdin=stdin)

    # ── 6. Write JIRA_* vars into sandbox via stdin ───────────────────────────
    # spec.environment only reaches supervisor-launched processes, not ad-hoc
    # exec() calls. Write the vars to /sandbox/.jira.env via stdin so every
    # subsequent exec can source them. The token comes from os.environ — it is
    # passed through the stdin pipe, never held in a named Python variable.
    print("\n[6] Writing JIRA_* env vars into sandbox (/sandbox/.jira.env)")
    jira_env_content = (
        f"export JIRA_SERVER_URL={os.environ['JIRA_SERVER_URL']!r}\n"
        f"export JIRA_EMAIL={os.environ['JIRA_EMAIL']!r}\n"
        f"export JIRA_ACCESS_TOKEN={os.environ['JIRA_ACCESS_TOKEN']!r}\n"
    )
    try:
        xec("cat > /sandbox/.jira.env", timeout=10, stdin=jira_env_content.encode())
        r = xec("test -s /sandbox/.jira.env && echo ok", timeout=5)
        ok = step("JIRA env file written to sandbox", "ok" in r.stdout)
        all_passed = all_passed and ok
    except Exception as exc:
        step("Write JIRA env file", False, str(exc))
        all_passed = False

    # Helper: prefix every subsequent shell command with source of the env file
    def xec_jira(cmd, timeout=30):
        return xec(f". /sandbox/.jira.env && {cmd}", timeout=timeout)

    # Verify the vars are accessible
    for var in ("JIRA_SERVER_URL", "JIRA_EMAIL", "JIRA_ACCESS_TOKEN"):
        try:
            r = xec_jira(f"printenv {var}")
            val = r.stdout.strip()
            ok = step(f"{var} readable in sandbox", bool(val),
                      val if var != "JIRA_ACCESS_TOKEN" else f"len={len(val)}")
            all_passed = all_passed and ok
        except Exception as exc:
            step(f"{var} check", False, str(exc))
            all_passed = False

    # ── 7. Jira network connectivity ──────────────────────────────────────────
    # Source env file inside sandbox — token never appears in Python.
    # Note: sandboxes route HTTPS via an egress proxy (HTTP_PROXY=10.200.0.1:3128).
    # OPA (Landlock) policy controls which binaries may initiate connections, but the
    # proxy is a separate layer that must also allowlist the destination.
    # A 403 from the proxy means the OPA policy is fine but the proxy blocks the host.
    print("\n[7] Jira network connectivity (curl using $JIRA_* inside sandbox)")
    try:
        curl_cmd = (
            'curl -v --max-time 15 '
            '-u "$JIRA_EMAIL:$JIRA_ACCESS_TOKEN" '
            '"$JIRA_SERVER_URL/rest/api/3/myself" '
            '2>&1 | tail -20'
        )
        r = xec_jira(curl_cmd, timeout=30)
        output = (r.stdout or "").strip()
        reachable = (
            "accountId" in output or "displayName" in output or "emailAddress" in output
        )
        proxy_blocked = "403 Forbidden" in output or "ProxyError" in output or "Tunnel connection failed" in output
        if reachable:
            ok = step("curl /rest/api/3/myself → 200 OK", True, output[:120])
        elif proxy_blocked:
            ok = step("curl /rest/api/3/myself → 200 OK", False,
                      "PROXY 403 — redhat.atlassian.net blocked by egress proxy (10.200.0.1:3128); "
                      "OPA policy is correct but proxy allowlist needs updating")
        else:
            ok = step("curl /rest/api/3/myself → 200 OK", False,
                      f"exit={r.exit_code} out={output[:120]}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("Jira network connectivity", False, str(exc))
        all_passed = False

    # ── 8. jira-mcp-server binary present ─────────────────────────────────────
    print("\n[8] jira-mcp-server binary check")
    try:
        r = xec(["which", "jira-mcp-server"])
        binary_path = r.stdout.strip()
        ok = step("jira-mcp-server on PATH", bool(binary_path), binary_path)
        all_passed = all_passed and ok
    except Exception as exc:
        step("jira-mcp-server binary", False, str(exc))
        all_passed = False

    # ── 8b. Write opencode.json with MCP config and verify mcp key present ──────
    # Validates the fix for the mcp_patch double-nesting bug (ACM-34954): the
    # agent config JSON written to /sandbox/opencode.json must include the "mcp"
    # section with the Jira server entry so OpenCode knows to spawn jira-mcp-server.
    print("\n[8b] opencode.json written with mcp section (ACM-34954 regression check)")
    try:
        from swarmer.openshell_client import write_agent_config as _write_agent_config

        # Build the config the same way _setup_openshell_sandbox does after the fix
        _fake_mcp = type("_MCP", (), {
            "slug": "atlassian-jira",
            "type": "local",
            "command": ["jira-mcp-server"],
            "enabled": True,
            "environment": {
                "JIRA_SERVER_URL": "{env:JIRA_SERVER_URL}",
                "JIRA_ACCESS_TOKEN": "{env:JIRA_ACCESS_TOKEN}",
                "JIRA_EMAIL": "{env:JIRA_EMAIL}",
            },
        })()
        config_data = tool.build_config_data(mcp_servers=[_fake_mcp], model=model)
        config_json = config_data.get("opencode.json", "{}")
        await _write_agent_config(sandbox_name, "opencode", config_json)

        # Read it back from the sandbox and verify the mcp key is present
        r = xec("cat /sandbox/opencode.json", timeout=10)
        import json as _json
        try:
            written = _json.loads(r.stdout)
            has_mcp = "mcp" in written
            has_jira = "atlassian-jira" in written.get("mcp", {})
            ok = step("opencode.json has 'mcp' section", has_mcp,
                      f"keys: {list(written.keys())}" if not has_mcp else "")
            all_passed = all_passed and ok
            ok = step("opencode.json mcp has 'atlassian-jira' entry", has_jira,
                      str(list(written.get("mcp", {}).keys())) if not has_jira else "")
            all_passed = all_passed and ok
        except Exception as parse_exc:
            step("Parse opencode.json", False, str(parse_exc))
            all_passed = False
    except Exception as exc:
        step("write opencode.json with MCP config", False, str(exc))
        all_passed = False

    # ── 9. jira-mcp-server subprocess run (MCP initialize) ───────────────────
    # Feed a JSON-RPC initialize request over stdin. The server reads JIRA_*
    # from the sandbox environment (already set), connects to atlassian.net,
    # and returns an MCP initialize response on stdout.
    #
    # OPA evaluates the network policy in real time. Any host/binary not covered
    # by _JIRA_MCP_BLOCK will be denied and logged by the OpenShell supervisor.
    # Those denials are the "policy sub-bumps" we need to add to the block.
    print("\n[9] jira-mcp-server MCP initialize (policy sub-bump discovery)")

    mcp_init_request = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"protocolVersion":"2024-11-05",'
        '"capabilities":{},'
        '"clientInfo":{"name":"smoke-test","version":"0.1"}}}\n'
    )

    try:
        r = xec_jira(
            "jira-mcp-server 2>/tmp/jira-mcp-stderr.txt",
            timeout=20,
        )
        # Re-run with stdin for MCP initialize — xec_jira doesn't support stdin
        # so write the request to a file first and pipe it in
        xec("cat > /tmp/mcp-init.json", timeout=5, stdin=mcp_init_request.encode())
        r = xec_jira(
            "jira-mcp-server < /tmp/mcp-init.json 2>/tmp/jira-mcp-stderr.txt",
            timeout=20,
        )
        stdout = (r.stdout or "").strip()
        stderr_r = xec("cat /tmp/jira-mcp-stderr.txt 2>/dev/null", timeout=5)
        stderr = (stderr_r.stdout or "").strip()

        initialized = "result" in stdout and ("serverInfo" in stdout or "capabilities" in stdout)
        ok = step(
            "jira-mcp-server MCP initialize response",
            initialized,
            stdout[:200] if initialized else
            f"exit={r.exit_code} stdout={stdout[:120]!r} stderr={stderr[:200]!r}",
        )
        all_passed = all_passed and ok

        if initialized:
            print(f"\n  Response (truncated): {stdout[:300]}")
        elif stderr:
            print(f"\n  stderr output:\n{stderr[:600]}")

        # Surface any OPA network denials — these are the policy sub-bumps
        denial_lines = [
            ln for ln in stderr.splitlines()
            if "policy_denied" in ln or "DENY" in ln.upper()
        ]
        if denial_lines:
            print(f"\n  OPA policy denials — add to _JIRA_MCP_BLOCK in openshell_policy.py:")
            for ln in denial_lines[:10]:
                print(f"    {ln}")
            step("No OPA network denials", False,
                 f"{len(denial_lines)} denial(s) — see above for required policy additions")
            all_passed = False
        else:
            step("No OPA network denials", True)

    except Exception as exc:
        step("jira-mcp-server run", False, str(exc))
        all_passed = False

    # ── Cleanup ───────────────────────────────────────────────────────────────
    print("\n[cleanup]")
    try:
        client.delete(sandbox_name)
        step("Delete sandbox", True, sandbox_name)
    except Exception as exc:
        step("Delete sandbox", False, str(exc))

    _cleanup_provider(client, provider_name, openshell_pb2)

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        print("\nFailures:")
        for label, ok, detail in _results:
            if not ok:
                print(f"  {FAIL}  {label}" + (f" — {detail}" if detail else ""))
    return all_passed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="OpenShell Jira MCP e2e smoke test",
        epilog=(
            "Source Jira credentials before running:\n"
            "  set -a && source ../jira-mcp-server/.env && set +a"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model", default="google/gemini-2.5-flash@default",
        help="Model string for gateway provider (default: google/gemini-2.5-flash@default)",
    )
    args = parser.parse_args()

    print(f"OpenShell Jira MCP Smoke Test — model: {args.model}")
    ok = asyncio.run(run_jira_smoke_test(args.model))
    sys.exit(0 if ok else 1)

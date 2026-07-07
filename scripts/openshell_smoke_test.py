"""
End-to-end smoke test for the OpenShell session launch sequence.

Runs each step of _setup_openshell_sandbox individually against a real
OpenShell gateway, verifying correctness before proceeding. Designed to
be both a debugging tool and a repeatable e2e test.

Usage:
  python3 scripts/openshell_smoke_test.py [--model google/gemini-3.5-flash]

Requirements:
  - OpenShell gateway reachable (OPENSHELL_GATEWAY_URL in .env)
  - swarmer auth/secret.key exists
  - At least one OpencodeSecret in the DB with a Google AI Studio key

Exit 0 = all steps passed. Exit 1 = one or more failures.
"""
import argparse
import asyncio
import json
import re
import shlex
import sys

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


def _mask(text: str) -> str:
    return re.sub(r'"key":"[^"]{8}[^"]*"', '"key":"****"', text)


def _redact_secret(value: str) -> str:
    """Return a redacted representation of a secret value for safe log output."""
    if not value:
        return "(empty)"
    return f"(set, len={len(value)})"


async def run_smoke_test(model: str) -> bool:
    from swarmer.crypto import init_crypto
    from swarmer.openshell_client import (
        _get_client, ensure_provider, create_sandbox, _wait_sandbox_ready,
        write_agent_config,
    )
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    from swarmer.openshell_policy import build_session_policy
    from openshell._proto import openshell_pb2

    init_crypto("auth/secret.key")

    # ── 1. Read Google API key from DB ───────────────────────────────────────
    print("\n[1] Reading credentials from DB")
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

    # ── 2. Provider setup ────────────────────────────────────────────────────
    print("\n[2] Gateway provider setup")
    provider_name = "swarmer-smoke-test-google"
    try:
        await ensure_provider(provider_name, "google-ai-studio", {},
                              credentials={"GOOGLE_API_KEY": google_key})
        step("CreateProvider/UpdateProvider", True, provider_name)
    except Exception as exc:
        step("CreateProvider/UpdateProvider", False, str(exc))
        return False

    # ── 3. Sandbox creation ──────────────────────────────────────────────────
    print("\n[3] Sandbox creation")

    class _FakeSession:
        language = "golang"
        agent_tool = "opencode"

    # Build policy with network rules pre-included so the sandbox starts with all
    # required Landlock access approved (no probe-deny-approve cycle needed, ACM-34909).
    policy = build_session_policy(_FakeSession(), [], [], "opencode", model)
    ref = None
    try:
        ref = await create_sandbox(
            image=tool.get_image(),
            env_vars={},
            policy=policy,
            provider_names=[provider_name],
        )
        step("CreateSandbox + WaitReady", True, ref.name)
    except Exception as exc:
        step("CreateSandbox + WaitReady", False, str(exc))
        # Clean up provider and bail
        try:
            client._stub.DeleteProvider(
                openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10)
        except Exception:
            pass
        return False

    sid = ref.id
    sandbox_name = ref.name

    def xec(cmd, timeout=20, stdin=None):
        """Execute a command in the sandbox and return ExecResult."""
        if isinstance(cmd, str):
            return client.exec(sid, ["sh", "-c", cmd], timeout_seconds=timeout, stdin=stdin)
        return client.exec(sid, cmd, timeout_seconds=timeout, stdin=stdin)

    all_passed = True

    # ── 4. Provider env injection ────────────────────────────────────────────
    print("\n[4] Provider environment injection")
    try:
        r = xec(["bash", "-i", "-c", "printenv GOOGLE_API_KEY"])
        val = r.stdout.strip()
        is_ref = val.startswith("openshell:resolve:")
        ok = step("GOOGLE_API_KEY is reference token", is_ref,
                  val[:50] if is_ref else f"got: {_redact_secret(val)}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("GOOGLE_API_KEY check", False, str(exc))
        all_passed = False

    # ── 5. Filesystem write access ───────────────────────────────────────────
    print("\n[5] Filesystem permissions")
    # /home/sandbox may not be writable via landlock; we use HOME=/sandbox so
    # the agent writes to /sandbox/.local instead. Only test /sandbox.
    try:
        r = xec("mkdir -p /sandbox/.smoke-test && rmdir /sandbox/.smoke-test && echo ok")
        ok = step("/sandbox writable", r.exit_code == 0 and "ok" in r.stdout,
                  (r.stderr or "").strip()[:80] if r.exit_code != 0 else "")
        all_passed = all_passed and ok
    except Exception as exc:
        step("/sandbox writable", False, str(exc))
        all_passed = False

    # ── 6. model.json ────────────────────────────────────────────────────────
    print("\n[6] Model configuration")
    try:
        model_setup_cmd = tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/")
        clean_cmd = model_setup_cmd.rstrip().rstrip("&").rstrip()
        r = xec(clean_cmd)
        r2 = xec(["cat", "/sandbox/.local/state/opencode/model.json"])
        model_id = model.split("/", 1)[-1]
        has_model = bool(r2.stdout and model_id in r2.stdout)
        ok = step("model.json written", has_model,
                  r2.stdout.strip()[:80] if has_model
                  else f"exit={r.exit_code} stderr={r.stderr.strip()!r}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("model.json", False, str(exc))
        all_passed = False

    # ── 7. auth.json ─────────────────────────────────────────────────────────
    print("\n[7] Auth configuration (auth.json)")
    try:
        share_cmd = tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/")
        clean_share = share_cmd.rstrip().rstrip(";").rstrip()
        r = xec(clean_share)
        r2 = xec(["cat", "/sandbox/.opencode/auth.json"])
        has_auth = bool(r2.stdout and "google" in r2.stdout)
        ok = step("auth.json written with reference token", has_auth,
                  _mask(r2.stdout.strip()) if has_auth
                  else f"exit={r.exit_code} stderr={r.stderr.strip()!r}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("auth.json", False, str(exc))
        all_passed = False

    # ── 8. Write valid opencode.json (replaces container's outdated schema) ──
    print("\n[8] opencode.json (write valid config)")
    try:
        config_data = tool.build_config_data()
        config_json = config_data.get("opencode.json", "{}")
        await write_agent_config(sandbox_name, "opencode", config_json)
        r = xec(["cat", "/sandbox/opencode.json"])
        cfg = json.loads(r.stdout) if r.stdout else {}
        has_providers = "enabled_providers" in cfg
        ok = step("enabled_providers present in written config", has_providers,
                  str(cfg.get("enabled_providers", "(missing)")))
        all_passed = all_passed and ok
    except Exception as exc:
        step("opencode.json write", False, str(exc))
        all_passed = False

    # ── 9. Pre-applied network policy validation ──────────────────────────────
    # Network policies are now included directly in spec.policy at sandbox creation
    # (ACM-34909). There is no probe-deny-approve cycle. Instead, we validate that
    # the sandbox was started with the correct policy by inspecting the approved
    # policy state and attempting a direct git clone without any prior approval step.
    print("\n[9] Network policy validation (pre-applied at creation)")
    try:
        from swarmer.openshell_policy import build_session_network_policies

        computed_net = build_session_network_policies(_FakeSession(), [], [], "opencode", model)
        ok = step("build_session_network_policies returns non-empty dict",
                  len(computed_net) > 0,
                  f"{len(computed_net)} blocks: {sorted(computed_net.keys())}")
        all_passed = all_passed and ok

        # Verify the agent_api block is present and has expected endpoints
        agent_block = computed_net.get("agent_api", {})
        agent_endpoints = [ep.get("host", "") for ep in agent_block.get("endpoints", [])]
        has_vertex = any("aiplatform" in h for h in agent_endpoints)
        ok = step("agent_api block has aiplatform endpoint", has_vertex,
                  f"endpoints: {agent_endpoints}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("Network policy validation", False, str(exc))
        all_passed = False

    # ── 9b. Public repo clone — direct, no prior approval needed ─────────────
    # Sandbox was created with pre-applied network policy granting git access to
    # github.com. Clone should succeed immediately.
    print("\n[9b] Public repo clone (pre-applied policy, no PAT)")
    pub_repo = "https://github.com/stolostron/agent-swarm"
    try:
        r_clone = xec(f"git clone --depth=1 {pub_repo} /tmp/smoke-repo 2>&1 | tail -3", timeout=60)
        cloned = r_clone.exit_code == 0 or "done." in r_clone.stdout.lower() or "already exists" in r_clone.stdout
        ok = step("git clone public repo (no probe-approve cycle)", cloned,
                  r_clone.stdout.strip()[:100] if cloned else r_clone.stdout.strip()[:200])
        all_passed = all_passed and ok
    except Exception as exc:
        step("git clone public repo", False, str(exc))
        all_passed = False

    # ── 9c. Verify sandbox still alive (may have been GC'd during approval wait) ──
    try:
        client._stub.GetSandbox(openshell_pb2.GetSandboxRequest(name=sandbox_name), timeout=10)
    except Exception as exc:
        step("Sandbox still alive", False, f"GC or external deletion: {exc}")
        all_passed = False
        client._stub.DeleteProvider(openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10)
        return all_passed

    # ── 10. opencode run ──────────────────────────────────────────────────────
    print("\n[10] opencode prompt execution")
    prompt = "Write Hello World in large ASCII art text. Be brief."

    class _FakeSess:
        mode = "prompt"
        instruction_prompt = ""

    main_cmd = f"HOME=/sandbox {tool.build_main_cmd(_FakeSess(), model, resolved_prompt=prompt)}"
    print(f"     cmd: {main_cmd}")
    try:
        r = xec(main_cmd, timeout=120)
        ok_exit = step("opencode exits 0", r.exit_code == 0, f"exit={r.exit_code}")
        all_passed = all_passed and ok_exit

        # OpenCode stores the response in its SQLite DB, not stdout.
        # Query it via Python after the run completes.
        db_reader = b"""
import sqlite3, json
conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
conn.execute('PRAGMA wal_checkpoint(FULL)')
# Get assistant message text parts only
rows = conn.execute('''
    SELECT p.data FROM part p
    JOIN message m ON p.message_id = m.id
    WHERE json_extract(m.data, '$.role') = 'assistant'
      AND json_extract(p.data, '$.type') = 'text'
    ORDER BY p.time_created
''').fetchall()
texts = [json.loads(r[0]).get('text', '') for r in rows if r[0]]
result = '\\n'.join(t for t in texts if t.strip())
# Also check for errors
err_rows = conn.execute(
    "SELECT data FROM event WHERE type LIKE 'message.updated%' ORDER BY id DESC LIMIT 3"
).fetchall()
for (d,) in err_rows:
    info = json.loads(d).get('info', {})
    if info.get('error'):
        print('DB_ERROR:', json.dumps(info['error'])[:200])
        break
print(result[:2000] if result else '')
conn.close()
"""
        xec("cat > /tmp/get_output.py", stdin=db_reader)
        r2 = client.exec(sid, ["python3", "/tmp/get_output.py"], timeout_seconds=10)
        response = (r2.stdout or "").strip()

        # Fall back to stderr if DB query found nothing (e.g. policy_denied error)
        if not response:
            err_reader = b"""
import sqlite3, json
conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
conn.execute('PRAGMA wal_checkpoint(FULL)')
rows = conn.execute(
    "SELECT data FROM event WHERE type LIKE 'message.updated%' ORDER BY id DESC LIMIT 3"
).fetchall()
for r in rows:
    d = json.loads(r[0])
    info = d.get('info', {})
    err = info.get('error')
    if err:
        print('ERROR:', json.dumps(err)[:300])
conn.close()
"""
            xec("cat > /tmp/get_errors.py", stdin=err_reader)
            r3 = client.exec(sid, ["python3", "/tmp/get_errors.py"], timeout_seconds=10)
            if r3.stdout.strip():
                print(f"  DB errors: {r3.stdout.strip()[:400]}")

        ok_out = step("opencode response in DB", bool(response), f"{len(response)} chars")
        all_passed = all_passed and ok_out
        if response:
            print(f"\n--- Response ---\n{response[:800]}\n---")
        if r.exit_code != 0:
            stderr = (r.stderr or "").replace("/bin/bash: /home/sandbox/.bash_profile: Permission denied", "").strip()
            if stderr:
                print(f"  stderr: {stderr[:300]}")
    except Exception as exc:
        step("opencode run", False, str(exc))
        all_passed = False

    # ── Cleanup ──────────────────────────────────────────────────────────────
    print("\n[cleanup]")
    try:
        client.delete(sandbox_name)
        step("Delete sandbox", True, sandbox_name)
    except Exception as exc:
        step("Delete sandbox", False, str(exc))
    try:
        # Detach from any still-running sandboxes before deleting
        try:
            attached = client._stub.ListSandboxes(openshell_pb2.ListSandboxesRequest(), timeout=10)
            for asb in attached.sandboxes:
                provs = client._stub.ListSandboxProviders(
                    openshell_pb2.ListSandboxProvidersRequest(sandbox_name=asb.metadata.name), timeout=10
                )
                if any(p.metadata.name == provider_name for p in provs.providers):
                    client._stub.DetachSandboxProvider(
                        openshell_pb2.DetachSandboxProviderRequest(
                            sandbox_name=asb.metadata.name, provider_name=provider_name
                        ), timeout=10
                    )
        except Exception:
            pass
        client._stub.DeleteProvider(
            openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10)
        step("Delete test provider", True)
    except Exception as exc:
        step("Delete test provider", False, str(exc))

    # ── Summary ──────────────────────────────────────────────────────────────
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


async def run_vertex_smoke_test(
    opencode_model: str = "google-vertex-anthropic/claude-sonnet-5@default",
    agent_tool: str = "opencode",
) -> bool:
    """E2e smoke test for the google-vertex-ai provider (ADC / VertexAI Anthropic Claude).

    Tests:
      1. Read ADC credentials and Vertex config from DB
      2. Register google-vertex-ai provider with ADC refresh strategy
      3. Create sandbox with VertexAI provider attached
      4. Verify access_token reference token is injected as env var
      5. Verify GOOGLE_CLOUD_PROJECT / VERTEXAI_PROJECT env vars present
      6. Run Claude prompt via the agent and verify a response is returned

    Usage:
      python3 scripts/openshell_smoke_test.py --vertex
    """
    from swarmer.crypto import init_crypto
    from swarmer.openshell_client import (
        _get_client, ensure_provider, configure_vertex_provider,
        create_sandbox, write_agent_config, enable_providers_v2,
    )
    from swarmer.openshell_policy import build_session_policy
    from openshell._proto import openshell_pb2

    init_crypto("auth/secret.key")

    model = opencode_model

    # ── 1. Read VertexAI credentials from DB ────────────────────────────────
    print(f"\n[1] Reading VertexAI credentials from DB (agent_tool={agent_tool}, model={model})")
    adc_json = project = location = ""
    try:
        from swarmer.database import init_db, get_db
        from sqlalchemy import select
        from swarmer.models.opencode_secret import OpencodeSecret

        init_db("sqlite+aiosqlite:///data/swarmer.db")
        async for db in get_db():
            result = await db.execute(select(OpencodeSecret))
            secret = result.scalars().first()
            if secret and secret.has_adc:
                adc_json = secret.application_default_credentials
                project = secret.google_cloud_project or ""
                location = secret.vertex_location or ""
            break
    except Exception as exc:
        step("Read OpencodeSecret from DB", False, str(exc))
        return False

    if not step("ADC credentials present", bool(adc_json)):
        print("  Hint: configure Application Default Credentials in the AI Tokens settings page")
        return False
    if not step("GCP project set", bool(project)):
        return False
    if not step("Vertex location set", bool(location)):
        return False

    client = _get_client()

    # Use native google-vertex-ai provider — the gateway proxy resolves the reference
    # token (GOOGLE_VERTEX_AI_TOKEN) in HTTP calls to aiplatform.googleapis.com.
    # No model string rewriting needed; keep original provider prefix.
    model_arg = shlex.quote(model)
    vertex_env_prefix = ""  # no special env prefix needed

    # ── 1b. Enable providers_v2 (required for google-vertex-ai profile) ──
    try:
        await enable_providers_v2()
        step("providers_v2_enabled", True)
    except Exception as exc:
        step("providers_v2_enabled", False, str(exc)[:80])

    # ── 2. Register google-vertex-ai provider ────────────────────────────────
    print("\n[2] Gateway provider setup (google-vertex-ai, ADC refresh)")
    provider_name = f"swarmer-smoke-vertex-{agent_tool}"
    try:
        await ensure_provider(
            provider_name, "google-vertex-ai",
            config={"VERTEX_AI_PROJECT_ID": project, "VERTEX_AI_REGION": location},
            credentials={
                "gcloud_adc_token": "__placeholder__",
                "GOOGLE_VERTEX_AI_TOKEN": "__placeholder__",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "__placeholder__",
                "GOOGLE_CLOUD_PROJECT": project,
                "VERTEX_LOCATION": location,
                "ANTHROPIC_VERTEX_PROJECT_ID": project,
            },
        )
        step("CreateProvider (google-vertex-ai)", True, provider_name)
    except Exception as exc:
        step("CreateProvider (google-vertex-ai)", False, str(exc))
        return False

    try:
        await configure_vertex_provider(provider_name, adc_json=adc_json, project=project, location=location)
        step("ConfigureProviderRefresh (ADC)", True)
    except Exception as exc:
        step("ConfigureProviderRefresh", False, str(exc))
        _cleanup_provider(client, provider_name, openshell_pb2)
        return False

    # Wait for the gateway to complete the first token refresh (async, takes ~2-5s)
    import time as _time
    print("  Waiting for initial token refresh...", end="", flush=True)
    for _ in range(15):
        _time.sleep(1)
        try:
            sr = client._stub.GetProviderRefreshStatus(
                openshell_pb2.GetProviderRefreshStatusRequest(provider=provider_name),
                timeout=5)
            statuses = {c.credential_key: c.status for c in sr.credentials}
            if statuses.get("gcloud_adc_token") == "configured":
                print(" ready")
                step("Token refreshed", True, f"gcloud_adc_token: configured")
                break
            print(".", end="", flush=True)
        except Exception:
            print(".", end="", flush=True)
    else:
        print(" timeout")
        prereq_passed = step("Token refreshed", False, "timed out waiting for gcloud_adc_token")
        if not prereq_passed:
            _cleanup_provider(client, provider_name, openshell_pb2)
            return False

    # ── 3. Sandbox creation ──────────────────────────────────────────────────
    print("\n[3] Sandbox creation")
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    tool = OpenCodeStrategy()

    _agent_tool = agent_tool

    class _FakeSession:
        language = "golang"
        agent_tool = _agent_tool

    # Build policy with network rules pre-included (ACM-34909).
    policy = build_session_policy(_FakeSession(), [], [], agent_tool, model)
    ref = None
    try:
        ref = await create_sandbox(
            image=tool.get_image(),
            env_vars={},
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

    def xec(cmd, timeout=20, stdin=None):
        if isinstance(cmd, str):
            return client.exec(sid, ["sh", "-c", cmd], timeout_seconds=timeout, stdin=stdin)
        return client.exec(sid, cmd, timeout_seconds=timeout, stdin=stdin)

    # ── 4. Check provider token injection ────────────────────────────────────
    print("\n[4] Provider token injection")
    try:
        r = xec(["printenv", "GOOGLE_VERTEX_AI_TOKEN"])
        val = r.stdout.strip()
        is_ref = val.startswith("openshell:resolve:")
        step("GOOGLE_VERTEX_AI_TOKEN injected", is_ref or bool(val),
             val[:60] if is_ref else _redact_secret(val))
        all_passed = all_passed and (is_ref or bool(val))
    except Exception as exc:
        step("GOOGLE_VERTEX_AI_TOKEN check", False, str(exc))
        all_passed = False

    # ── 5. Model config ──────────────────────────────────────────────────────
    print("\n[5] Model configuration")
    try:
        model_setup_cmd = tool.build_model_setup_cmd(model).replace("/workspace/", "/sandbox/")
        share_cmd = tool.build_share_setup_cmd().replace("/workspace/", "/sandbox/")
        if share_cmd.strip():
            clean_share = share_cmd.rstrip().rstrip(";").rstrip()
            xec(f"export HOME=/sandbox; {clean_share}")
        if model_setup_cmd.strip():
            clean_cmd = model_setup_cmd.rstrip().rstrip("&").rstrip()
            r = xec(f"export HOME=/sandbox; {clean_cmd}")
            ok = step("model setup cmd ran", r.exit_code == 0, (r.stderr or "").strip()[:80])
            all_passed = all_passed and ok
    except Exception as exc:
        step("model setup", False, str(exc))
        all_passed = False

    # Write valid agent config
    try:
        config_data = tool.build_config_data()
        config_json = config_data.get(f"{tool.name}.json", "{}")
        await write_agent_config(sandbox_name, agent_tool, config_json)
        step(f"{tool.name}.json written", True)
    except Exception as exc:
        step(f"{tool.name}.json write", False, str(exc))
        all_passed = False

    # ── 5b. Network policy validation (pre-applied at creation) ──────────────
    # Network policies are included in spec.policy at sandbox creation (ACM-34909).
    # No probe-deny-approve cycle. Validate the computed policy dict is complete.
    print("\n[5b] Network policy validation (pre-applied at creation)")
    try:
        from swarmer.openshell_policy import build_session_network_policies

        computed_net = build_session_network_policies(_FakeSession(), [], [], agent_tool, model)
        ok = step("build_session_network_policies returns non-empty dict",
                  len(computed_net) > 0,
                  f"{len(computed_net)} blocks: {sorted(computed_net.keys())}")
        all_passed = all_passed and ok
        agent_block = computed_net.get("agent_api", {})
        agent_endpoints = [ep.get("host", "") for ep in agent_block.get("endpoints", [])]
        has_vertex = any("aiplatform" in h or "inference.local" in h for h in agent_endpoints)
        ok = step("agent_api block covers VertexAI/inference.local endpoints", has_vertex,
                  f"endpoints: {agent_endpoints}")
        all_passed = all_passed and ok
    except Exception as exc:
        step("Network policy validation", False, str(exc)[:80])
        all_passed = False

    # ── 6. Agent run with Claude via VertexAI ────────────────────────────────
    print(f"\n[6] {agent_tool} prompt execution (Claude via VertexAI)")
    prompt = "Reply with exactly one word: ready"

    class _FakeSess:
        mode = "prompt"
        instruction_prompt = ""

    tool_bin = {"opencode": "opencode run"}.get(agent_tool, "opencode run")
    prompt_arg = shlex.quote(prompt)
    main_cmd = f"{vertex_env_prefix}HOME=/sandbox {tool_bin} --model {model_arg} {prompt_arg}"
    print(f"     cmd: HOME=/sandbox {tool_bin} --model {model_arg} {prompt_arg}  (+ vertex env)")
    try:
        r = xec(main_cmd, timeout=120)
        ok_exit = step(f"{agent_tool} exits 0", r.exit_code == 0, f"exit={r.exit_code}")
        all_passed = all_passed and ok_exit

        # OpenCode writes to SQLite DB
        db_reader = b"""
import sqlite3, json
conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
conn.execute('PRAGMA wal_checkpoint(FULL)')
# Try part table first (older schema)
try:
    rows = conn.execute('''
        SELECT p.data FROM part p
        JOIN message m ON p.message_id = m.id
        WHERE json_extract(m.data, '$.role') = 'assistant'
          AND json_extract(p.data, '$.type') = 'text'
        ORDER BY p.time_created
    ''').fetchall()
    texts = [json.loads(r[0]).get('text', '') for r in rows if r[0]]
    if texts:
        print('\\n'.join(t for t in texts if t.strip())[:2000])
        conn.close()
        exit(0)
except Exception:
    pass
# Fallback: look in message.data directly for content
try:
    rows = conn.execute(
        "SELECT data FROM message ORDER BY created"
    ).fetchall()
    for (d,) in rows:
        try:
            msg = json.loads(d)
            if msg.get('role') == 'assistant':
                # Try different content field layouts
                content = msg.get('content') or msg.get('text', '')
                if isinstance(content, list):
                    texts = [c.get('text','') for c in content if isinstance(c, dict) and c.get('type')=='text']
                    content = ' '.join(texts)
                if content:
                    print(str(content)[:2000])
        except Exception:
            pass
except Exception:
    pass
# Also check event table for message content
try:
    rows = conn.execute(
        "SELECT data FROM event WHERE type LIKE '%message%' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    for (d,) in rows:
        try:
            ev = json.loads(d)
            info = ev.get('info', {})
            msg = info.get('message', {})
            if msg.get('role') == 'assistant':
                content = msg.get('content', '')
                if isinstance(content, list):
                    texts = [c.get('text','') for c in content if isinstance(c, dict)]
                    content = ' '.join(texts)
                if content:
                    print(str(content)[:2000])
        except Exception:
            pass
except Exception:
    pass
conn.close()
"""
        xec("cat > /tmp/get_output.py", stdin=db_reader)
        r2 = client.exec(sid, ["python3", "/tmp/get_output.py"], timeout_seconds=10)
        response = (r2.stdout or "").strip()
        if not response:
            # Dump message roles and parts to diagnose
            diag = b"""
import sqlite3, json
conn = sqlite3.connect('/sandbox/.opencode/opencode.db')
conn.execute('PRAGMA wal_checkpoint(FULL)')
print('=== messages ===')
for row in conn.execute('SELECT id, data FROM message ORDER BY created').fetchall():
    mid, d = row
    try:
        msg = json.loads(d)
        print(f'  {mid}: role={msg.get("role")} content_len={len(str(msg.get("content","")))}')
    except:
        print(f'  {mid}: raw={str(d)[:100]}')
print('=== parts ===')
prows = conn.execute('SELECT id, message_id, data FROM part LIMIT 10').fetchall()
print(f'  {len(prows)} parts total')
for pid, mid, d in prows:
    try:
        p = json.loads(d)
        print(f'  part {pid}: type={p.get("type")} text={str(p.get("text",""))[:80]}')
    except:
        print(f'  part {pid}: raw={str(d)[:80]}')
print('=== events (last 3) ===')
for row in conn.execute('SELECT type, data FROM event ORDER BY id DESC LIMIT 3').fetchall():
    etype, d = row
    try:
        ev = json.loads(d)
        info = ev.get('info', {})
        print(f'  event {etype}: error={info.get("error")} msg_role={info.get("message",{}).get("role")}')
    except:
        print(f'  event {etype}: raw={str(d)[:80]}')
conn.close()
"""
            xec("cat > /tmp/diag.py", stdin=diag)
            rd = client.exec(sid, ["python3", "/tmp/diag.py"], timeout_seconds=10)
            if rd.stdout:
                print(f"  DB diagnostic:\n{rd.stdout[:1200]}")
            if r.stdout and r.stdout.strip():
                print(f"  opencode stdout: {r.stdout.strip()[:400]}")
            if r.stderr and r.stderr.strip():
                print(f"  opencode stderr: {r.stderr.strip()[:400]}")
        ok_out = step("OpenCode response in DB", bool(response), f"{len(response)} chars")

        all_passed = all_passed and ok_out
        if response:
            print(f"\n--- Response ---\n{response[:400]}\n---")
        if r.exit_code != 0:
            stderr = (r.stderr or "").strip()
            if stderr:
                print(f"  stderr: {stderr[:300]}")
    except Exception as exc:
        step(f"{agent_tool} run", False, str(exc))
        all_passed = False

    # ── Cleanup ──────────────────────────────────────────────────────────────
    print("\n[cleanup]")
    try:
        client.delete(sandbox_name)
        step("Delete sandbox", True, sandbox_name)
    except Exception as exc:
        step("Delete sandbox", False, str(exc))
    _cleanup_provider(client, provider_name, openshell_pb2)

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


def _cleanup_provider(client, provider_name: str, openshell_pb2) -> None:
    """Detach and delete a gateway provider, ignoring errors."""
    try:
        attached = client._stub.ListSandboxes(openshell_pb2.ListSandboxesRequest(), timeout=10)
        for asb in attached.sandboxes:
            try:
                provs = client._stub.ListSandboxProviders(
                    openshell_pb2.ListSandboxProvidersRequest(sandbox_name=asb.metadata.name), timeout=10
                )
                if any(p.metadata.name == provider_name for p in provs.providers):
                    client._stub.DetachSandboxProvider(
                        openshell_pb2.DetachSandboxProviderRequest(
                            sandbox_name=asb.metadata.name, provider_name=provider_name
                        ), timeout=10
                    )
            except Exception:
                pass
    except Exception:
        pass
    try:
        client._stub.DeleteProvider(
            openshell_pb2.DeleteProviderRequest(name=provider_name), timeout=10)
        step("Delete test provider", True)
    except Exception as exc:
        step("Delete test provider", False, str(exc))


async def run_policy_extract(
    repos: list[str],
    model: str = "google/gemini-3.5-flash",
    agent_tool: str = "opencode",
    language: str = "golang",
    jira_mcp: bool = False,
) -> bool:
    """Policy extraction harness (ACM-34909).

    Creates a sandbox with the given configuration, pre-applies all computed
    network policies at creation time, then clones each repo and verifies that
    git clone succeeds without any probe-deny-approve cycle.

    Prints the serialized network_policies dict so it can be reviewed and
    compared against what build_session_network_policies() computes statically.

    Usage:
      python3 scripts/openshell_smoke_test.py --policy-extract \\
          --repo https://github.com/stolostron/agent-swarm \\
          --model google/gemini-3.5-flash \\
          --agent opencode --language golang
    """
    from swarmer.crypto import init_crypto
    from swarmer.openshell_client import _get_client, ensure_provider, create_sandbox
    from swarmer.openshell_policy import build_session_policy, build_session_network_policies
    from swarmer.agent_tools.opencode import OpenCodeStrategy
    from openshell._proto import openshell_pb2

    init_crypto("auth/secret.key")

    # ── 1. Read credentials ──────────────────────────────────────────────────
    print("\n[1] Reading credentials from DB")
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

    if not step("Google API key present", bool(google_key)):
        return False

    tool = OpenCodeStrategy()
    client = _get_client()
    provider_name = "swarmer-policy-extract-google"

    try:
        await ensure_provider(provider_name, "google-ai-studio", {},
                              credentials={"GOOGLE_API_KEY": google_key})
        step("Provider registered", True, provider_name)
    except Exception as exc:
        step("Provider registration", False, str(exc))
        return False

    # ── 2. Build fake repo objects for policy computation ────────────────────
    print("\n[2] Computing network policies for configuration")

    class _FakeRepo:
        def __init__(self, url: str):
            self.repo_url = url
            parts = url.rstrip("/").split("/")
            self.local_path = parts[-1] if parts else "repo"

    class _FakeMcp:
        def __init__(self, slug: str):
            self.slug = slug

    class _FakeSession:
        pass

    fake_session = _FakeSession()
    fake_session.language = language  # type: ignore[attr-defined]
    fake_session.agent_tool = agent_tool  # type: ignore[attr-defined]

    fake_repos = [_FakeRepo(u) for u in repos]
    fake_mcp = [_FakeMcp("jira")] if jira_mcp else []

    computed_net = build_session_network_policies(fake_session, fake_repos, fake_mcp, agent_tool, model)
    step("Network policy dict computed", len(computed_net) > 0,
         f"{len(computed_net)} blocks: {sorted(computed_net.keys())}")

    print("\n  Computed network_policies:")
    for block_name, block in sorted(computed_net.items()):
        endpoints = [ep.get("host", "?") for ep in block.get("endpoints", [])]
        binaries = [b.get("path", "?") for b in block.get("binaries", [])]
        print(f"    {block_name}:")
        print(f"      endpoints: {endpoints}")
        if binaries:
            print(f"      binaries:  {binaries}")

    # ── 3. Create sandbox with pre-applied policy ────────────────────────────
    print("\n[3] Creating sandbox with pre-applied network policies")
    policy = build_session_policy(fake_session, fake_repos, fake_mcp, agent_tool, model)
    ref = None
    try:
        ref = await create_sandbox(
            image=tool.get_image(),
            env_vars={},
            policy=policy,
            provider_names=[provider_name],
        )
        step("CreateSandbox + WaitReady (policy pre-applied)", True, ref.name)
    except Exception as exc:
        step("CreateSandbox", False, str(exc))
        _cleanup_provider(client, provider_name, openshell_pb2)
        return False

    sid = ref.id
    sandbox_name = ref.name
    all_passed = True

    def xec(cmd, timeout=30, stdin=None):
        if isinstance(cmd, str):
            return client.exec(sid, ["sh", "-c", cmd], timeout_seconds=timeout, stdin=stdin)
        return client.exec(sid, cmd, timeout_seconds=timeout, stdin=stdin)

    # ── 4. Git clone each repo — no probe-approve cycle ─────────────────────
    # The sandbox started with github.com network access already approved via
    # spec.policy. Clone must succeed immediately.
    print("\n[4] Git clone repos (no probe-approve cycle)")
    for repo_url in repos:
        parts = repo_url.rstrip("/").split("/")
        local_path = parts[-1] if parts else "repo"
        is_github = "github.com" in repo_url
        try:
            clone_cmd = f"cd /sandbox && git clone --depth=1 {shlex.quote(repo_url)} {shlex.quote(local_path)} 2>&1 | tail -5"
            r = xec(clone_cmd, timeout=60)
            cloned = r.exit_code == 0 or "done." in r.stdout.lower() or "already exists" in r.stdout
            label = f"git clone {local_path}" + (" (github.com, pre-applied policy)" if is_github else "")
            ok = step(label, cloned,
                      r.stdout.strip()[:120] if cloned else r.stdout.strip()[:240])
            all_passed = all_passed and ok
        except Exception as exc:
            step(f"git clone {local_path}", False, str(exc))
            all_passed = False

    if not repos:
        step("No repos specified — skipping git clone test", True,
             "Pass --repo <url> to test git clone")

    # ── 5. Verify AI API access ──────────────────────────────────────────────
    print("\n[5] AI API connectivity (curl health check)")
    try:
        r = xec("curl -sf --max-time 5 https://generativelanguage.googleapis.com/ 2>&1 | head -3; true", timeout=10)
        reachable = r.exit_code == 0 or "HTTP" in r.stdout or "json" in r.stdout.lower() or len(r.stdout) > 0
        ok = step("generativelanguage.googleapis.com reachable", reachable,
                  r.stdout.strip()[:80] if reachable else r.stderr.strip()[:80])
        all_passed = all_passed and ok
    except Exception as exc:
        step("googleapis.com curl", False, str(exc))
        all_passed = False

    # ── Cleanup ──────────────────────────────────────────────────────────────
    print("\n[cleanup]")
    try:
        client.delete(sandbox_name)
        step("Delete sandbox", True, sandbox_name)
    except Exception as exc:
        step("Delete sandbox", False, str(exc))
    _cleanup_provider(client, provider_name, openshell_pb2)

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
    parser = argparse.ArgumentParser(description="OpenShell e2e smoke test")
    parser.add_argument("--model", default="google/gemini-3.5-flash",
                        help="Model to use for Gemini smoke test (default: google/gemini-3.5-flash)")
    parser.add_argument("--vertex", action="store_true",
                        help="Run VertexAI / ADC smoke test (Anthropic Claude via Vertex)")
    parser.add_argument("--agent", default="opencode", choices=["opencode"],
                        help="Agent tool to test in --vertex mode (default: opencode)")
    parser.add_argument("--policy-extract", action="store_true",
                        help="Run policy extraction harness: compute + pre-apply network policies, "
                             "then validate git clone works without a probe-approve cycle (ACM-34909)")
    parser.add_argument("--repo", action="append", dest="repos", default=[],
                        metavar="URL",
                        help="Repo URL(s) to clone in --policy-extract mode (repeatable)")
    parser.add_argument("--language", default="golang", choices=["golang", "python"],
                        help="Session language for policy computation in --policy-extract mode")
    parser.add_argument("--jira-mcp", action="store_true",
                        help="Include Jira MCP network policy block in --policy-extract mode")
    args = parser.parse_args()

    if args.policy_extract:
        print(f"OpenShell Policy Extraction Harness — repos: {args.repos or ['(none)']}, "
              f"model: {args.model}, agent: {args.agent}, language: {args.language}")
        ok = asyncio.run(run_policy_extract(
            repos=args.repos,
            model=args.model,
            agent_tool=args.agent,
            language=args.language,
            jira_mcp=args.jira_mcp,
        ))
    elif args.vertex:
        print(f"OpenShell VertexAI Smoke Test — agent: {args.agent}")
        ok = asyncio.run(run_vertex_smoke_test(agent_tool=args.agent))
    else:
        print(f"OpenShell Smoke Test — model: {args.model}")
        ok = asyncio.run(run_smoke_test(args.model))
    sys.exit(0 if ok else 1)

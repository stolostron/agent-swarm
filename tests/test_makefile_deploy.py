"""Tests for Makefile OpenShell and Agent Sandbox deployment logic."""

import os
import re
import subprocess
import importlib.util

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
MAKEFILE_PATH = os.path.join(REPO_ROOT, "Makefile")
E2E_SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "e2e_kind_deploy.py")


def _load_e2e_script():
    spec = importlib.util.spec_from_file_location("e2e_kind_deploy", E2E_SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_makefile_agent_sandbox_version_default():
    """Ensure default AGENT_SANDBOX_VERSION is v1.0.1+ and OPENSHELL_VERSION is 0.0.116+."""
    with open(MAKEFILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    as_match = re.search(r"^AGENT_SANDBOX_VERSION\s*\?=\s*(v[0-9]+\.[0-9]+\.[0-9]+)", content, re.MULTILINE)
    assert as_match, "AGENT_SANDBOX_VERSION not found in Makefile"
    assert as_match.group(1) == "v1.0.1"

    os_match = re.search(r"^OPENSHELL_VERSION\s*\?=\s*([0-9]+\.[0-9]+\.[0-9]+)", content, re.MULTILINE)
    assert os_match, "OPENSHELL_VERSION not found in Makefile"
    assert os_match.group(1) == "0.0.116"


def test_makefile_manifest_fallback_present():
    """Verify that Makefile checks sandbox.yaml before manifest.yaml."""
    with open(MAKEFILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    assert "sandbox.yaml" in content
    assert "manifest.yaml" in content
    # Check the fallback structure in the deploy recipe
    pattern = re.compile(
        r'MANIFEST_URL=.*releases/download/\$\(AGENT_SANDBOX_VERSION\)/sandbox\.yaml.*'
        r'if ! curl -fsI "\$\$MANIFEST_URL".*'
        r'MANIFEST_URL=.*releases/download/\$\(AGENT_SANDBOX_VERSION\)/manifest\.yaml',
        re.DOTALL,
    )
    assert pattern.search(content), "Makefile deploy target missing sandbox.yaml -> manifest.yaml fallback"


def test_makefile_kind_destroy_alias_and_e2e_target():
    """The lifecycle smoke test has a stable destroy alias and Make target."""
    with open(MAKEFILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    assert "kind-destroy: kind-delete" in content
    assert "test-e2e-kind:" in content
    assert "scripts/e2e_kind_deploy.py" in content
    assert "--namespace $(NAMESPACE)" in content


def test_e2e_existing_cluster_is_not_destroyed(monkeypatch, capsys):
    """A pre-existing cluster must not be treated as test-owned cleanup."""
    e2e = _load_e2e_script()
    commands = []
    monkeypatch.setattr(e2e, "require_commands", lambda commands: None)
    monkeypatch.setattr(e2e, "assert_port_available", lambda port: None)
    monkeypatch.setattr(e2e, "cluster_exists", lambda cluster: True)
    monkeypatch.setattr(e2e, "run", lambda command, timeout: commands.append(command))
    monkeypatch.setattr(e2e.sys, "argv", ["e2e_kind_deploy.py"])

    assert e2e.main() == 1
    assert not any("kind-destroy" in command for command in commands)
    assert "already exists" in capsys.readouterr().out


def test_e2e_uses_namespace_for_deploy_and_rollout(monkeypatch):
    """The configured namespace must be passed to Make and kubectl."""
    e2e = _load_e2e_script()
    commands = []
    cluster_checks = iter([False, False])

    def fake_run(command, timeout):
        commands.append(command)
        if "kind-deploy" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["kubectl", "rollout"]:
            return subprocess.CompletedProcess(command, 1, "", "rollout failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(e2e, "require_commands", lambda commands: None)
    monkeypatch.setattr(e2e, "assert_port_available", lambda port: None)
    monkeypatch.setattr(e2e, "cluster_exists", lambda cluster: next(cluster_checks))
    monkeypatch.setattr(e2e, "run", fake_run)
    monkeypatch.setattr(e2e.sys, "argv", ["e2e_kind_deploy.py", "--namespace", "custom-ns"])

    assert e2e.main() == 1
    assert ["KIND_CLUSTER=swarmer", "NAMESPACE=custom-ns"] == commands[0][1:3]
    rollout = next(command for command in commands if command[:2] == ["kubectl", "rollout"])
    assert rollout[rollout.index("-n") + 1] == "custom-ns"
    assert any("kind-destroy" in command for command in commands)


def test_makefile_openshift_scc_includes_agent_sandbox_and_openshell():
    """Verify OpenShift SCC grants include agent-sandbox-controller, openshell, and openshell-sandbox."""
    with open(MAKEFILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    assert "oc adm policy add-scc-to-user anyuid    -z agent-sandbox-controller -n agent-sandbox-system" in content
    assert "oc adm policy add-scc-to-user anyuid    -z openshell         -n $(OPENSHELL_NAMESPACE)" in content
    assert "oc adm policy add-scc-to-user anyuid    -z openshell-sandbox -n $(OPENSHELL_NAMESPACE)" in content
    assert "oc adm policy add-scc-to-user privileged -z openshell         -n $(OPENSHELL_NAMESPACE)" in content
    assert "oc adm policy add-scc-to-user privileged -z openshell-sandbox -n $(OPENSHELL_NAMESPACE)" in content


def test_agent_sandbox_manifest_resolution():
    """Verify curl-based resolution chooses sandbox.yaml for v1.0.1 and manifest.yaml for v0.4.6."""
    # Test resolution logic directly using sh with mocked curl
    sh_script = """
    curl() {
        case "$*" in
            *v1.0.1/sandbox.yaml*) return 0 ;;
            *) return 1 ;;
        esac
    }
    check_version() {
        VERSION="$1"
        MANIFEST_URL="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox.yaml"
        if ! curl -fsI "$MANIFEST_URL" > /dev/null 2>&1; then
            MANIFEST_URL="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/manifest.yaml"
        fi
        echo "$MANIFEST_URL"
    }
    check_version "$1"
    """

    res_101 = subprocess.run(["sh", "-c", sh_script, "sh", "v1.0.1"], capture_output=True, text=True, check=True, timeout=10)
    assert res_101.stdout.strip().endswith("/v1.0.1/sandbox.yaml")

    res_046 = subprocess.run(["sh", "-c", sh_script, "sh", "v0.4.6"], capture_output=True, text=True, check=True, timeout=10)
    assert res_046.stdout.strip().endswith("/v0.4.6/manifest.yaml")

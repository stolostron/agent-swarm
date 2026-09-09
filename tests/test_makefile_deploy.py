"""Tests for Makefile OpenShell and Agent Sandbox deployment logic."""

import os
import re
import subprocess
import pytest

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
MAKEFILE_PATH = os.path.join(REPO_ROOT, "Makefile")


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

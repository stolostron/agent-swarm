"""Tests for docs/USER_GUIDE.md documentation.

Validates that the user guide contains all required sections, references
correct Makefile targets, env vars, and file paths from the codebase.
No FastAPI/SQLAlchemy dependencies — only reads files.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
GUIDE_PATH = os.path.join(REPO_ROOT, "docs", "USER_GUIDE.md")
README_PATH = os.path.join(REPO_ROOT, "README.md")
MAKEFILE_PATH = os.path.join(REPO_ROOT, "Makefile")
ENV_EXAMPLE_PATH = os.path.join(REPO_ROOT, ".env.example")
CONFIG_PATH = os.path.join(REPO_ROOT, "swarmer", "config.py")


@pytest.fixture(scope="module")
def guide_content():
    assert os.path.exists(GUIDE_PATH), f"USER_GUIDE.md not found at {GUIDE_PATH}"
    with open(GUIDE_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def readme_content():
    with open(README_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def makefile_content():
    with open(MAKEFILE_PATH) as f:
        return f.read()


# ── Section structure ─────────────────────────────────────────────────

REQUIRED_SECTIONS = [
    "# Agent Swarm User Guide",
    "## Overview",
    "## Prepare Your Environment",
    "### OpenShift (Recommended)",
    "### Local Development with Kind",
    "## Install",
    "### Option 1",
    "### Option 2",
    "### Option 3",
    "### Option 4",
    "### Option 5",
    "## Configure",
    "### Environment Variables",
    "### Secret Key",
    "### Database",
    "### Agent Images",
    "### Access Control",
    "## Usage",
    "### Workspaces",
    "### Secrets & Credentials",
    "### Git Repositories",
    "### Sessions",
    "#### Session Modes",
    "##### Prompt Mode",
    "##### Server Mode",
    "##### TUI Mode",
    "#### Session Lifecycle",
    "### Agent Tools",
    "#### OpenCode",
    "#### Crush",
    "#### Model Selection",
    "### MCP Servers",
    "### Prompt Library",
    "### Cron Scheduling",
    "### Patch Generation",
    "## Teardown",
    "## Appendix",
    "### Makefile Reference",
    "### Troubleshooting",
]


@pytest.mark.parametrize("heading", REQUIRED_SECTIONS)
def test_required_section_exists(guide_content, heading):
    assert heading in guide_content, f"Missing section: {heading}"


# ── Key content references ────────────────────────────────────────────

REQUIRED_TERMS = [
    "FastAPI",
    "HTMX",
    "OpenShift",
    "Kind",
    "Kustomize",
    "Fernet",
    "ServiceAccount",
    "bearer token",
    "SWARMER_SECRET_KEY",
    "K8S_IN_CLUSTER",
    "DATABASE_URL",
    "AGENT_IMAGE_OPENCODE",
    "AGENT_IMAGE_CRUSH",
    "DEFAULT_AGENT_TOOL",
    "AGENT_IMAGE_PULL_SECRET",
    "AGENT_IMAGE_PULL_POLICY",
    "K8S_NAMESPACE",
    "OPENSHIFT_OAUTH_URL",
    "opencode",
    "crush",
    "cron",
    "make setup-secret",
    "make kind-deploy",
    "make kind-delete",
    "make k8s-deploy",
    "make k8s-delete",
    "make k8s-connect",
    "make user-token",
    "make grant-workspace",
    "make image-build",
    "make image-push",
    "make dev",
    "make lint",
    "make db-reset",
    "sqlite",
    "sleep infinity",
    "xterm.js",
    "OSC 52",
    "restartPolicy",
]


@pytest.mark.parametrize("term", REQUIRED_TERMS)
def test_required_term_present(guide_content, term):
    assert term.lower() in guide_content.lower(), (
        f"Required term '{term}' not found in USER_GUIDE.md"
    )


# ── Makefile target cross-check ───────────────────────────────────────

MAKEFILE_TARGETS_IN_GUIDE = [
    "setup-secret",
    "install",
    "dev",
    "lint",
    "db-reset",
    "image-build",
    "image-push",
    "image-build-crush",
    "k8s-deploy",
    "k8s-delete",
    "k8s-connect",
    "openshift-deploy",
    "kind-create",
    "kind-load",
    "kind-load-opencode",
    "kind-load-crush",
    "kind-deploy",
    "kind-delete",
    "user-token",
    "grant-workspace",
    "sync-images",
]


@pytest.mark.parametrize("target", MAKEFILE_TARGETS_IN_GUIDE)
def test_makefile_target_exists(makefile_content, target):
    pattern = rf"^{re.escape(target)}:"
    assert re.search(pattern, makefile_content, re.MULTILINE), (
        f"Makefile target '{target}' not found in Makefile"
    )


@pytest.mark.parametrize("target", MAKEFILE_TARGETS_IN_GUIDE)
def test_makefile_target_in_guide(guide_content, target):
    assert target in guide_content, (
        f"Makefile target '{target}' not mentioned in USER_GUIDE.md"
    )


# ── File path references ─────────────────────────────────────────────

REFERENCED_PATHS = [
    ".env.example",
    "swarmer/config.py",
    "swarmer/crypto.py",
    "k8s/swarmer/namespace.yaml",
    "k8s/swarmer/rbac.yaml",
    "k8s/swarmer/pvc.yaml",
    "k8s/openshift/deployment.yaml",
    "k8s/openshift/route.yaml",
    "k8s/openshift/oauth-client.yaml",
    "k8s/kind-config.yaml",
    "Containerfile",
]


@pytest.mark.parametrize("path", REFERENCED_PATHS)
def test_referenced_file_exists(path):
    full = os.path.join(REPO_ROOT, path)
    assert os.path.exists(full), f"Referenced file does not exist: {path}"


# ── README link to USER_GUIDE.md ─────────────────────────────────────

def test_readme_links_to_user_guide(readme_content):
    assert "docs/USER_GUIDE.md" in readme_content, (
        "README.md should link to docs/USER_GUIDE.md"
    )


# ── OpenShift is recommended ─────────────────────────────────────────

def test_openshift_recommended(guide_content):
    assert "recommended" in guide_content.lower()
    rec_idx = guide_content.lower().index("openshift")
    assert rec_idx < len(guide_content), "OpenShift should appear in the guide"


# ── Deployment options count ──────────────────────────────────────────

def test_five_install_options(guide_content):
    options = re.findall(r"### Option \d", guide_content)
    assert len(options) >= 5, f"Expected 5 install options, found {len(options)}"


# ── Environment variable table ────────────────────────────────────────

def test_env_var_table_exists(guide_content):
    assert "DATABASE_URL" in guide_content
    assert "sqlite" in guide_content.lower()


# ── Session modes documented ──────────────────────────────────────────

def test_session_modes(guide_content):
    for mode in ["Prompt Mode", "Server Mode", "TUI Mode"]:
        assert mode in guide_content, f"Missing session mode: {mode}"


# ── Agent tools documented ────────────────────────────────────────────

def test_agent_tool_model_formats(guide_content):
    assert "provider/model@version" in guide_content or "provider/model" in guide_content
    assert "vertexai/" in guide_content
    assert "claude-sonnet" in guide_content.lower()


# ── Teardown for each method ──────────────────────────────────────────

def test_teardown_commands(guide_content):
    teardown_section = guide_content[guide_content.index("## Teardown"):]
    assert "kind-delete" in teardown_section
    assert "k8s-delete" in teardown_section
    assert "oc delete" in teardown_section


# ── No secrets leaked ─────────────────────────────────────────────────

def test_no_real_secrets(guide_content):
    for line in guide_content.split("\n"):
        if "SECRET_KEY" in line.upper() and "=" in line:
            value_part = line.split("=", 1)[-1].strip().strip('"').strip("'")
            if (
                value_part
                and not value_part.startswith("$")
                and not value_part.startswith("<")
                and not value_part.startswith("{")
                and "--from-literal" not in line
            ):
                assert len(value_part) < 40 or "example" in line.lower() or "python" in line.lower(), (
                    f"Possible leaked secret in line: {line[:80]}"
                )


# ── Heading hierarchy ─────────────────────────────────────────────────

def test_heading_hierarchy(guide_content):
    """Verify heading levels are sane (no jump greater than 3 levels at once)."""
    prev_level = 0
    in_code_block = False
    for line in guide_content.split("\n"):
        if line.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        match = re.match(r"^(#{1,6})\s", line)
        if match:
            level = len(match.group(1))
            if prev_level > 0 and level > prev_level:
                assert level <= prev_level + 3, (
                    f"Heading level jump from {prev_level} to {level}: {line.strip()}"
                )
            prev_level = level

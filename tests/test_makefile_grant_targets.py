"""Tests for the `grant-workspace-access` / `grant-workspace-create` Makefile
targets, specifically the SA_USER (ServiceAccount) vs OIDC_USER (OpenShift
OAuth / OIDC User) selection logic.

These targets shell out to `kubectl`. To exercise the *actual* shell logic
(the `if [ -n "$(SA_USER)" ]; then ... else ... fi` branch selection) rather
than just the unevaluated recipe text that `make -n` would print, we run the
real (non-dry-run) target with a fake `kubectl` shim placed first on `PATH`.
The shim only ever records/echoes its argv and exits 0 — no real cluster is
touched. Validation/error paths that exit before ever calling `kubectl` are
tested by running the real target and asserting on the exit code.

Regression coverage for a real-world bug: `USER` collides with the ambient
shell environment variable (nearly every shell exports `$USER` as the OS
login name), which would cause OIDC-user logic keyed off a variable
literally named `USER` to silently pick up the invoking operator's shell
username instead of requiring an explicit value. The Makefile therefore
uses `OIDC_USER`, never bare `USER`.
"""

import os
import stat
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
MAKEFILE_PATH = os.path.join(REPO_ROOT, "Makefile")

FAKE_KUBECTL = """#!/bin/sh
# Fake kubectl shim for tests: record invocation, never touch a real cluster.
# Logged to stderr (not stdout) because the Makefile pipes the `create
# ... --dry-run=client -o yaml` invocation's stdout into `kubectl apply -f -`
# — anything this shim writes to stdout would corrupt that pipe.
echo "KUBECTL_CALL: $@" >&2
if [ "$1" = "apply" ]; then
    exit 0
fi
# Support `kubectl create ... --dry-run=client -o yaml | kubectl apply -f -`
echo "kind: Fake"
exit 0
"""


@pytest.fixture()
def fake_kubectl_path(tmp_path):
    """Create a fake `kubectl` executable in an isolated dir and return that
    dir so it can be prepended to PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(bin_dir)


def _run_make(args, extra_env=None, path_prefix=None, dry_run=False):
    """Invoke `make [-n] <args>` in REPO_ROOT and return the CompletedProcess.

    `extra_env` is merged on top of a *copy* of the current environment so
    ambient variables (notably $USER, exported by virtually every shell)
    can be controlled explicitly per-test. `path_prefix` is prepended to
    PATH (used to inject the fake kubectl shim).
    """
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    cmd = ["make"]
    if dry_run:
        cmd.append("-n")
    cmd.extend(args)
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Makefile source-level checks
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def makefile_content():
    with open(MAKEFILE_PATH) as f:
        return f.read()


def test_grant_targets_never_reference_bare_user_variable(makefile_content):
    """`$(USER)` (no OIDC_ prefix) must never appear — it collides with the
    ambient $USER environment variable that virtually every shell exports
    for the invoking operator, and would silently substitute their login
    name instead of requiring an explicit grant target."""
    assert "$(USER)" not in makefile_content, (
        "Makefile must not reference the bare $(USER) variable — it collides "
        "with the shell's ambient $USER env var. Use OIDC_USER instead."
    )


def test_oidc_user_variable_present(makefile_content):
    assert "OIDC_USER" in makefile_content


def _extract_target_body(makefile_content: str, target: str) -> str:
    """Return all lines associated with a given Makefile target, including
    target-specific variable assignments (e.g. `target: export VAR := ...`)
    that precede the recipe, up to the next unrelated top-level line or EOF.
    """
    prefix = f"{target}:"
    collected = []
    started = False
    for line in makefile_content.split("\n"):
        if line.startswith(prefix):
            started = True
            collected.append(line[len(prefix):])
            continue
        if started:
            if line.startswith("\t") or line.strip() == "":
                collected.append(line)
                continue
            break
    assert collected, f"Could not locate target '{target}' in Makefile"
    return "\n".join(collected)


def test_grant_workspace_create_supports_sa_user_and_oidc_user(makefile_content):
    target_body = _extract_target_body(makefile_content, "grant-workspace-create")
    assert "SA_USER" in target_body
    assert "OIDC_USER" in target_body
    assert "--serviceaccount=" in target_body
    assert '--user="$$_OIDC_USER"' in target_body


def test_grant_workspace_access_supports_sa_user_and_oidc_user(makefile_content):
    target_body = _extract_target_body(makefile_content, "grant-workspace-access")
    assert "SA_USER" in target_body
    assert "OIDC_USER" in target_body
    assert "--serviceaccount=" in target_body
    assert '--user="$$_OIDC_USER"' in target_body


def test_exported_shell_vars_use_distinct_names_from_source_vars(makefile_content):
    """Regression test for a GNU Make quirk: `target: export SA_USER :=
    $(value SA_USER)` (reusing the same name) creates a self-referential
    target-specific variable that silently truncates any value containing
    '$(...)' sequences before it ever reaches the allow-list check. The
    exported shell variables must use distinct names (e.g. _SA_USER)."""
    for target in ("grant-workspace-access", "grant-workspace-create"):
        body = _extract_target_body(makefile_content, target)
        assert "export SA_USER := $(value SA_USER)" not in body
        assert "export OIDC_USER := $(value OIDC_USER)" not in body
        assert "export _SA_USER := $(value SA_USER)" in body
        assert "export _OIDC_USER := $(value OIDC_USER)" in body


# ---------------------------------------------------------------------------
# Real execution against a fake kubectl — verify correct branch is taken
# ---------------------------------------------------------------------------


class TestGrantWorkspaceCreateExecution:
    def test_sa_user_generates_serviceaccount_binding(self, fake_kubectl_path):
        result = _run_make(
            ["grant-workspace-create", "SA_USER=alice"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--serviceaccount=swarmer:alice" in combined
        assert "clusterrolebinding" in combined
        assert "swarmer-workspace-creator-alice" in combined
        assert "--user=" not in combined
        assert "(ServiceAccount)" in result.stdout

    def test_oidc_user_generates_user_binding(self, fake_kubectl_path):
        result = _run_make(
            ["grant-workspace-create", "OIDC_USER=mikeshng"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--user=mikeshng" in combined
        assert "swarmer-workspace-creator-mikeshng" in combined
        assert "--serviceaccount=" not in combined
        assert "(OpenShift/OIDC User)" in result.stdout

    def test_sa_user_unaffected_by_ambient_user_env_var(self, fake_kubectl_path):
        """Regression test: passing only SA_USER must not trip the mutual
        exclusivity check, and must not silently bind to whatever the
        invoking shell's $USER happens to be (true for virtually all
        interactive shells)."""
        result = _run_make(
            ["grant-workspace-create", "SA_USER=alice"],
            extra_env={"USER": "some-operator"},
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "specify only one" not in combined
        assert "--serviceaccount=swarmer:alice" in combined
        assert "some-operator" not in combined

    def test_no_user_specified_is_an_error(self):
        result = _run_make(["grant-workspace-create"])
        assert result.returncode != 0
        assert "Usage" in result.stdout or "Usage" in result.stderr

    def test_both_sa_user_and_oidc_user_is_an_error(self):
        result = _run_make(["grant-workspace-create", "SA_USER=alice", "OIDC_USER=bob"])
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "specify only one" in combined.lower()

    @pytest.mark.parametrize(
        "malicious_sa_user_template",
        [
            "alice; rm -rf {marker}",
            "alice`touch {marker}`",
            "alice$(touch {marker})",
            "alice' && touch {marker} && echo '",
            'alice" && touch {marker} && echo "',
            "alice\ntouch {marker}",
            "alice|touch {marker}",
            "alice && touch {marker}",
        ],
    )
    def test_rejects_shell_metacharacters_in_sa_user_without_invoking_kubectl(
        self, fake_kubectl_path, tmp_path, malicious_sa_user_template
    ):
        """Security regression: a value containing shell metacharacters must
        be rejected by the allow-list check before any kubectl invocation,
        never executed as shell syntax."""
        marker = tmp_path / "pwned"
        result = _run_make(
            ["grant-workspace-create", f"SA_USER={malicious_sa_user_template.format(marker=marker)}"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined
        assert not marker.exists()

    def test_rejects_shell_metacharacters_in_oidc_user_without_invoking_kubectl(
        self, fake_kubectl_path, tmp_path
    ):
        marker = tmp_path / "pwned"
        result = _run_make(
            ["grant-workspace-create", f"OIDC_USER=bob; touch {marker}"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined
        assert not marker.exists()


class TestGrantWorkspaceAccessExecution:
    def test_sa_user_generates_serviceaccount_rolebinding(self, fake_kubectl_path):
        result = _run_make(
            ["grant-workspace-access", "SA_USER=alice", "WORKSPACE_NS=team-a"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--serviceaccount=swarmer:alice" in combined
        assert "--namespace=team-a" in combined
        assert "swarmer-user-alice" in combined
        assert "--user=" not in combined
        assert "(ServiceAccount)" in result.stdout

    def test_oidc_user_generates_user_rolebinding(self, fake_kubectl_path):
        result = _run_make(
            ["grant-workspace-access", "OIDC_USER=mikeshng", "WORKSPACE_NS=team-a"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--user=mikeshng" in combined
        assert "--namespace=team-a" in combined
        assert "swarmer-user-mikeshng" in combined
        assert "--serviceaccount=" not in combined
        assert "(OpenShift/OIDC User)" in result.stdout

    def test_sa_user_unaffected_by_ambient_user_env_var(self, fake_kubectl_path):
        result = _run_make(
            ["grant-workspace-access", "SA_USER=alice", "WORKSPACE_NS=team-a"],
            extra_env={"USER": "some-operator"},
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "specify only one" not in combined
        assert "--serviceaccount=swarmer:alice" in combined
        assert "some-operator" not in combined

    def test_missing_workspace_ns_is_an_error(self):
        result = _run_make(["grant-workspace-access", "SA_USER=alice"])
        assert result.returncode != 0
        assert "Usage" in result.stdout or "Usage" in result.stderr

    def test_no_user_specified_is_an_error(self):
        result = _run_make(["grant-workspace-access", "WORKSPACE_NS=team-a"])
        assert result.returncode != 0
        assert "Usage" in result.stdout or "Usage" in result.stderr

    def test_both_sa_user_and_oidc_user_is_an_error(self):
        result = _run_make(
            [
                "grant-workspace-access",
                "SA_USER=alice",
                "OIDC_USER=bob",
                "WORKSPACE_NS=team-a",
            ]
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "specify only one" in combined.lower()

    @pytest.mark.parametrize(
        "malicious_sa_user_template",
        [
            "alice; rm -rf {marker}",
            "alice`touch {marker}`",
            "alice$(touch {marker})",
            "alice' && touch {marker} && echo '",
            'alice" && touch {marker} && echo "',
        ],
    )
    def test_rejects_shell_metacharacters_in_sa_user_without_invoking_kubectl(
        self, fake_kubectl_path, tmp_path, malicious_sa_user_template
    ):
        marker = tmp_path / "pwned"
        result = _run_make(
            [
                "grant-workspace-access",
                f"SA_USER={malicious_sa_user_template.format(marker=marker)}",
                "WORKSPACE_NS=team-a",
            ],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined
        assert not marker.exists()

    def test_rejects_shell_metacharacters_in_workspace_ns_without_invoking_kubectl(
        self, fake_kubectl_path, tmp_path
    ):
        """Security regression: WORKSPACE_NS is also allow-list validated —
        a value containing shell metacharacters must be rejected before any
        kubectl invocation."""
        marker = tmp_path / "pwned"
        result = _run_make(
            [
                "grant-workspace-access",
                "SA_USER=alice",
                f"WORKSPACE_NS=team-a; touch {marker}",
            ],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined
        assert not marker.exists()


class TestNamespaceDnsLabelValidation:
    """Regression tests: a character-set-only check on WORKSPACE_NS/NAMESPACE
    still lets through values with leading/trailing hyphens or over-length
    values, which the Kubernetes API would reject anyway but only after
    kubectl is invoked. Both must be validated as a full DNS-1123 label
    (lowercase alphanumeric or '-', start/end alphanumeric, max 63 chars)
    before any kubectl invocation."""

    @pytest.mark.parametrize(
        "invalid_workspace_ns",
        ["-team-a", "team-a-", "a" * 64],
        ids=["leading-hyphen", "trailing-hyphen", "too-long"],
    )
    def test_rejects_invalid_workspace_ns_without_invoking_kubectl(
        self, fake_kubectl_path, invalid_workspace_ns
    ):
        result = _run_make(
            [
                "grant-workspace-access",
                "SA_USER=alice",
                f"WORKSPACE_NS={invalid_workspace_ns}",
            ],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined

    @pytest.mark.parametrize(
        "invalid_namespace",
        ["-swarmer", "swarmer-", "a" * 64, ""],
        ids=["leading-hyphen", "trailing-hyphen", "too-long", "empty"],
    )
    def test_rejects_invalid_namespace_on_grant_workspace_access(
        self, fake_kubectl_path, invalid_namespace
    ):
        result = _run_make(
            [
                "grant-workspace-access",
                "SA_USER=alice",
                "WORKSPACE_NS=team-a",
                f"NAMESPACE={invalid_namespace}",
            ],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined

    @pytest.mark.parametrize(
        "invalid_namespace",
        ["-swarmer", "swarmer-", "a" * 64, ""],
        ids=["leading-hyphen", "trailing-hyphen", "too-long", "empty"],
    )
    def test_rejects_invalid_namespace_on_grant_workspace_create(
        self, fake_kubectl_path, invalid_namespace
    ):
        result = _run_make(
            ["grant-workspace-create", "SA_USER=alice", f"NAMESPACE={invalid_namespace}"],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, combined
        assert "KUBECTL_CALL" not in combined

    def test_accepts_valid_hyphenated_namespace_and_workspace_ns(self, fake_kubectl_path):
        """Sanity check that the tightened validation still accepts
        legitimate internally-hyphenated values."""
        result = _run_make(
            [
                "grant-workspace-access",
                "SA_USER=alice",
                "WORKSPACE_NS=team-a",
                "NAMESPACE=my-namespace",
            ],
            path_prefix=fake_kubectl_path,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, combined
        assert "--serviceaccount=my-namespace:alice" in combined

"""Tests for resolving the default OpenCode agent image reference."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "resolve_agent_image.py"
SPEC = importlib.util.spec_from_file_location("resolve_agent_image", SCRIPT_PATH)
assert SPEC and SPEC.loader
resolver = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = resolver
SPEC.loader.exec_module(resolver)


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.value).encode()


def test_latest_stable_release_produces_published_semver_image(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "urlopen",
        lambda request, timeout: _Response(
            {"tag_name": "v1.18.34", "draft": False, "prerelease": False}
        ),
    )

    assert resolver.resolve_image() == "ghcr.io/anomalyco/opencode:1.18.34"


@pytest.mark.parametrize(
    "metadata",
    [
        [],
        {"tag_name": "not-a-version"},
        {"tag_name": "v1.2.3-rc.01"},
        {"tag_name": "v1.2.3+build.5"},
        {"tag_name": "v1.2.3", "draft": True},
        {"tag_name": "v1.2.3", "prerelease": True},
    ],
)
def test_invalid_release_metadata_fails_without_fallback(monkeypatch, metadata):
    monkeypatch.setattr(resolver, "urlopen", lambda request, timeout: _Response(metadata))

    with pytest.raises(resolver.ResolutionError):
        resolver.resolve_image()


def test_unavailable_release_metadata_fails_closed(monkeypatch):
    def unavailable(request, timeout):
        raise OSError("offline")

    monkeypatch.setattr(resolver, "urlopen", unavailable)

    with pytest.raises(resolver.ResolutionError, match="could not fetch"):
        resolver.resolve_image()


def test_explicit_full_image_override_is_offline_and_wins(monkeypatch):
    def unexpected_network(request, timeout):
        pytest.fail("explicit override should not request GitHub metadata")

    monkeypatch.setattr(resolver, "urlopen", unexpected_network)

    assert resolver.resolve_image("registry.example.com/team/opencode:1.2.3") == (
        "registry.example.com/team/opencode:1.2.3"
    )


def test_stale_env_override_is_used_as_explicit_value_without_mutation(tmp_path):
    stale_ref = "registry.example.com/team/opencode:0.9.0"
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=tmp_path,
        env={**os.environ, "AGENT_IMAGE_OPENCODE": stale_ref},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == stale_ref
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("make_override", "process_override", "expected"),
    [
        (
            None,
            "registry.example.com/process/opencode:2.0.0",
            "registry.example.com/team/opencode:0.9.0",
        ),
        (
            "registry.example.com/cli/opencode:1.2.3",
            "registry.example.com/process/opencode:2.0.0",
            "registry.example.com/cli/opencode:1.2.3",
        ),
    ],
)
def test_image_build_honors_env_file_and_cli_precedence_without_mutating_it(
    tmp_path, make_override, process_override, expected
):
    """A stale .env value is an explicit pin; a CLI value takes precedence."""
    shutil.copy2(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    (tmp_path / "scripts").mkdir()
    shutil.copy2(SCRIPT_PATH, tmp_path / "scripts" / SCRIPT_PATH.name)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENT_IMAGE_OPENCODE=registry.example.com/team/opencode:0.9.0\n",
        encoding="utf-8",
    )
    original_env = env_file.read_text(encoding="utf-8")
    defaults_file = tmp_path / ".push-defaults"
    shutil.copy2(REPO_ROOT / ".push-defaults", defaults_file)
    original_defaults = defaults_file.read_text(encoding="utf-8")
    capture = tmp_path / "container-args.json"
    fake_container = tmp_path / "fake-container"
    fake_container.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "open(os.environ['CAPTURE_ARGS'], 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_container.chmod(0o755)
    command = [
        "make",
        "--no-print-directory",
        "image-build",
        f"CONTAINER_CMD={fake_container}",
    ]
    if make_override:
        command.append(f"AGENT_IMAGE_OPENCODE={make_override}")

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **os.environ,
            "AGENT_IMAGE_OPENCODE": process_override,
            "CAPTURE_ARGS": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    build_args = json.loads(capture.read_text(encoding="utf-8"))
    assert f"AGENT_IMAGE_OPENCODE={expected}" in build_args
    assert env_file.read_text(encoding="utf-8") == original_env
    assert defaults_file.read_text(encoding="utf-8") == original_defaults


def test_kind_deploy_passes_one_offline_resolution_to_build_and_deploy(tmp_path):
    """The composite kind workflow resolves once and propagates the same image ref."""
    shutil.copy2(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("image_release.py", "resolve_agent_image.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, scripts / name)
    image_ref = "registry.example.com/team/opencode:1.2.3"
    result = subprocess.run(
        ["make", "--no-print-directory", "-n", "kind-deploy", f"AGENT_IMAGE_OPENCODE={image_ref}"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert 'AGENT_IMAGE_OPENCODE="$AGENT_IMAGE_REF"' in result.stdout
    assert f'--build-arg "AGENT_IMAGE_OPENCODE={image_ref}"' in result.stdout
    assert f's|AGENT_IMAGE_OPENCODE_VALUE|{image_ref}|g' in result.stdout


def test_invalid_explicit_override_does_not_fall_back_to_latest(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "urlopen",
        lambda request, timeout: pytest.fail("invalid override must not fall back to GitHub"),
    )

    with pytest.raises(resolver.ResolutionError, match="full image reference"):
        resolver.resolve_image("opencode:latest")


def test_make_resolves_only_from_target_recipes_and_build_consumes_result():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    containerfile = (REPO_ROOT / "Containerfile").read_text(encoding="utf-8")

    assert "$(shell python3 scripts/resolve_agent_image.py" not in makefile
    assert "python3 scripts/resolve_agent_image.py" in makefile
    assert '--build-arg "AGENT_IMAGE_OPENCODE=$$AGENT_IMAGE_REF"' in makefile
    assert "ARG AGENT_IMAGE_OPENCODE" in containerfile
    assert "ENV AGENT_IMAGE_OPENCODE=${AGENT_IMAGE_OPENCODE}" in containerfile
    assert "$(MAKE) --no-print-directory _deploy-resolved" in makefile
    assert "$(MAKE) --no-print-directory _kind-deploy-resolved" in makefile
    assert '--build-arg "AGENT_IMAGE_OPENCODE=$(AGENT_IMAGE_OPENCODE)"' in makefile

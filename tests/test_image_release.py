"""Image publishing metadata and Make target contracts."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("image_release", ROOT / "scripts/image_release.py")
assert SPEC and SPEC.loader
image_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(image_release)

DIGEST = "sha256:" + "a" * 64
REFERENCE = "quay.io/example/swarmer@" + DIGEST


@pytest.mark.parametrize("current,expected", [("1.4.9", "1.4.10"), ("1.5.0", "1.5.1")])
def test_next_version(current, expected):
    assert image_release.next_version(current) == expected


@pytest.mark.parametrize("invalid", ["", "1.4", "1.4.8-rc1", "01.4.8", "1.4.8\nnext"])
def test_invalid_semver(invalid):
    with pytest.raises(ValueError):
        image_release.next_version(invalid)


def test_digest_is_only_updated_after_validation(tmp_path):
    digest = tmp_path / "IMAGE_DIGEST"
    image_release.write_digest(digest, DIGEST, "quay.io/example")
    assert image_release.read_digest(digest) == REFERENCE
    with pytest.raises(ValueError):
        image_release.write_digest(digest, "sha256:invalid", "quay.io/example")
    assert digest.read_text() == REFERENCE + "\n"
    with pytest.raises(ValueError, match="belongs to"):
        image_release.read_digest(digest, "quay.io/elsewhere")


def test_empty_digest_blocks_deployment(tmp_path):
    with pytest.raises(ValueError, match="run make image-push first"):
        image_release.read_digest(tmp_path / "IMAGE_DIGEST")


def test_deploy_refuses_missing_digest_and_renders_valid_digest(tmp_path):
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/image_release.py", scripts / "image_release.py")
    (tmp_path / "IMAGE_DIGEST").write_text("")
    command = ["make", "deploy", "REGISTRY=quay.io/example", "SILENT=1"]
    failed = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert failed.returncode != 0
    assert "run make image-push first" in failed.stderr
    image_release.write_digest(tmp_path / "IMAGE_DIGEST", DIGEST, "quay.io/example")
    rendered = subprocess.run(["make", "-n", *command[1:]], cwd=tmp_path, capture_output=True, text=True)
    assert rendered.returncode == 0, rendered.stderr
    assert REFERENCE in rendered.stdout
    mismatch = subprocess.run(["make", "deploy", "REGISTRY=quay.io/elsewhere"], cwd=tmp_path, capture_output=True, text=True)
    assert mismatch.returncode != 0
    assert "belongs to" in mismatch.stderr
    (tmp_path / "IMAGE_DIGEST").write_text("")
    for config in ("command", "env"):
        if config == "env":
            (tmp_path / ".env").write_text("IMAGE_REF=quay.io/example/swarmer:manual\n")
            override = []
        else:
            override = ["IMAGE_REF=quay.io/example/swarmer:manual"]
        result = subprocess.run(["make", "deploy", *override], cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode != 0  # no auth/secret.key in this isolated checkout
        assert "Run 'make setup-secret' first" in result.stdout
        assert "IMAGE_DIGEST" not in result.stderr


def test_make_build_and_push_use_same_default_tag():
    build = subprocess.check_output(["make", "-n", "image-build", "REGISTRY=quay.io/example"], cwd=ROOT, text=True)
    push = subprocess.check_output(["make", "-n", "image-push", "REGISTRY=quay.io/example"], cwd=ROOT, text=True)
    assert 'podman build -f Containerfile -t "swarmer:local"' in build
    assert 'podman tag "swarmer:local" "quay.io/example/swarmer:local"' in push
    assert 'podman push --digestfile "$DIGEST_FILE" "quay.io/example/swarmer:local"' in push
    assert "record-push IMAGE_DIGEST" in push


def test_workflow_tag_override_and_kind_tag():
    build = subprocess.check_output(
        ["make", "-n", "image-build", "REGISTRY=quay.io/example", "IMAGE_TAG=1.4.10"], cwd=ROOT, text=True
    )
    push = subprocess.check_output(
        ["make", "-n", "image-push", "REGISTRY=quay.io/example", "IMAGE_TAG=1.4.10"], cwd=ROOT, text=True
    )
    assert '"swarmer:1.4.10"' in build
    assert '"swarmer:1.4.10" "quay.io/example/swarmer:1.4.10"' in push
    makefile = (ROOT / "Makefile").read_text()
    assert '$(MAKE) deploy SILENT=1 IMAGE_REF="$(if $(KIND_IMAGE_REF),$(KIND_IMAGE_REF),$(LOCAL_IMAGE_REF))"' in makefile


def test_push_records_digest_only_after_success(tmp_path):
    shutil.copy(ROOT / "Makefile", tmp_path / "Makefile")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/image_release.py", scripts / "image_release.py")
    (tmp_path / "VERSION").write_text("1.4.8\n")
    (tmp_path / "IMAGE_DIGEST").write_text(REFERENCE + "\n")
    fake_podman = tmp_path / "podman"
    fake_podman.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = tag ]; then exit 0; fi\n"
        "if [ \"$1\" = push ]; then\n"
        "  [ -z \"$FAIL_PUSH\" ] || exit 1\n"
        "  [ \"$2\" = --digestfile ] || exit 1\n"
        "  [ \"$4\" = quay.io/example/swarmer:local ] || exit 1\n"
        "  printf 'sha256:%064d\\n' 0 > \"$3\"\n"
        "else exit 1; fi\n"
    )
    fake_podman.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    failed = subprocess.run(
        ["make", "image-push", "REGISTRY=quay.io/example"],
        cwd=tmp_path, env={**env, "FAIL_PUSH": "1"}, capture_output=True, text=True,
    )
    assert failed.returncode != 0
    assert (tmp_path / "IMAGE_DIGEST").read_text() == REFERENCE + "\n"
    result = subprocess.run(["make", "image-push", "REGISTRY=quay.io/example"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "IMAGE_DIGEST").read_text() == "quay.io/example/swarmer@sha256:" + "0" * 64 + "\n"
    assert (tmp_path / "VERSION").read_text() == "1.4.8\n"


def test_pending_merges_uses_pr_association_not_commit_message(monkeypatch):
    baseline, source, publisher, next_source = [str(i) * 40 for i in range(1, 5)]

    def fake_run(*args, **kwargs):
        return None

    def fake_output(command, text, env):
        if command[1] == "rev-list":
            return "\n".join([source, publisher, next_source]) + "\n"
        if command[0] == "gh":
            sha = command[2].split("/")[-2]
            if sha == publisher:
                return "[[]]"
            return '[[{"merged_at":"now","base":{"ref":"main"},"merge_commit_sha":"' + sha + '"}]]'
        raise AssertionError(command)

    monkeypatch.setattr(image_release.subprocess, "run", fake_run)
    monkeypatch.setattr(image_release.subprocess, "check_output", fake_output)
    assert image_release.pending_sources("example/repo", baseline, next_source) == [source, next_source]


def test_publisher_catches_up_two_merges_without_rebuilding(tmp_path, monkeypatch):
    scripts = str(ROOT / "scripts")
    monkeypatch.syspath_prepend(scripts)
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.8\n")
    (tmp_path / "IMAGE_DIGEST").write_text("")
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    sources = ["2" * 40, "3" * 40]
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_ACTOR", "example-bot")
    monkeypatch.setenv("QUAY_REPOSITORY_PATH", "quay.io/example")
    monkeypatch.setenv("QUAY_ROBOT_USERNAME", "example-bot")
    monkeypatch.setenv("QUAY_PUSH_TOKEN", "mock-token")
    monkeypatch.setenv("GH_TOKEN", "mock-github-token")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("CONTAINER_CMD", "docker")
    monkeypatch.setattr(publish_image, "pending_sources", lambda repo, baseline, tip, env: sources)
    calls = []
    verified_metadata = []

    def fake_output(*command, env=None):
        if command[-1] == "HEAD":
            commits = sum("commit" in call for call, _ in calls)
            return str(commits + 3) * 40 if commits else "1" * 40
        return "1" * 40

    def fake_run(*command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ("make", "image-push"):
            image_release.write_digest(Path("IMAGE_DIGEST"), DIGEST, "quay.io/example")
        elif command[:2] == ("git", "restore"):
            Path("IMAGE_DIGEST").write_text("")
        elif command[:2] == ("python3", "scripts/e2e_kind_deploy.py"):
            verified_metadata.append(
                (Path("VERSION").read_text(), Path("IMAGE_DIGEST").read_text(), Path("IMAGE_PUBLISH_STATE").read_text())
            )

    monkeypatch.setattr(publish_image, "output", fake_output)
    monkeypatch.setattr(publish_image, "run", fake_run)
    publish_image.publish()
    assert (tmp_path / "VERSION").read_text() == "1.4.10\n"
    assert (tmp_path / "IMAGE_PUBLISH_STATE").read_text() == sources[-1] + "\n"
    assert (tmp_path / "IMAGE_DIGEST").read_text() == REFERENCE + "\n"
    assert verified_metadata == [
        ("1.4.9\n", REFERENCE + "\n", sources[0] + "\n"),
        ("1.4.10\n", REFERENCE + "\n", sources[1] + "\n"),
    ]
    commands = [command for command, _ in calls]
    builds = [call for call in commands if call[:2] == ("make", "image-build")]
    assert len(builds) == 2
    assert "IMAGE_TAG=1.4.9" in builds[0]
    assert "IMAGE_TAG=1.4.10" in builds[1]
    for version, metadata_sha in (("1.4.9", "4" * 40), ("1.4.10", "5" * 40)):
        build = next(i for i, call in enumerate(commands) if call[:2] == ("make", "image-build") and f"IMAGE_TAG={version}" in call)
        login = next(i for i in range(build + 1, len(commands)) if commands[i][:2] == ("podman", "login"))
        semver_push = next(i for i, call in enumerate(commands) if call[:2] == ("make", "image-push") and f"IMAGE_TAG={version}" in call)
        latest_tag = next(i for i, call in enumerate(commands) if call[:2] == ("podman", "tag") and call[2] == f"swarmer:{version}")
        latest_push = next(i for i in range(latest_tag + 1, len(commands)) if commands[i][:2] == ("make", "image-push") and "IMAGE_TAG=latest" in commands[i])
        e2e = next(i for i in range(latest_push + 1, len(commands)) if commands[i][:2] == ("python3", "scripts/e2e_kind_deploy.py"))
        metadata_commit = next(i for i in range(e2e + 1, len(commands)) if "commit" in commands[i])
        metadata_push = next(i for i in range(metadata_commit + 1, len(commands)) if commands[i][:2] == ("git", "push"))
        status = next(i for i in range(metadata_push + 1, len(commands)) if commands[i][:2] == ("gh", "api"))
        assert build < login < semver_push < latest_tag < latest_push < e2e < metadata_commit < metadata_push < status
        assert "--image-ref" in commands[e2e] and REFERENCE in commands[e2e]
        e2e_env = calls[e2e][1]["env"]
        assert e2e_env["CONTAINER_CMD"] == "docker"
        assert all(key not in e2e_env for key in ("GH_TOKEN", "QUAY_PUSH_TOKEN", "REGISTRY_AUTH_FILE", "GIT_ASKPASS"))
        assert f"statuses/{metadata_sha}" in " ".join(commands[status])
        assert "state=success" in commands[status]
        assert "context=KinD E2E / published image" in commands[status]
        assert "target_url=https://github.com/example/repo/actions/runs/123" in commands[status]
        assert calls[status][1]["env"]["GH_TOKEN"] == "mock-github-token"
        build_env = calls[build][1]["env"]
        assert all(key not in build_env for key in ("GH_TOKEN", "QUAY_PUSH_TOKEN", "REGISTRY_AUTH_FILE", "GIT_ASKPASS"))
    assert len([call for call in commands if call[:2] == ("git", "push")]) == 2


def test_publisher_keeps_verified_release_when_later_publish_fails(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.8\n")
    (tmp_path / "IMAGE_DIGEST").write_text("")
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    for key, value in {
        "GITHUB_REPOSITORY": "example/repo", "GITHUB_ACTOR": "example-bot",
        "QUAY_REPOSITORY_PATH": "quay.io/example", "QUAY_ROBOT_USERNAME": "example-bot",
        "QUAY_PUSH_TOKEN": "mock-token", "GH_TOKEN": "mock-github-token",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "123",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(publish_image, "pending_sources", lambda *args: ["2" * 40, "3" * 40])
    calls = []

    def fake_output(*command, env=None):
        if command[-1] == "HEAD":
            return "4" * 40 if any("commit" in call for call in calls) else "1" * 40
        return "1" * 40

    def fake_run(*command, **kwargs):
        calls.append(command)
        if command[:2] == ("make", "image-push"):
            if "IMAGE_TAG=1.4.10" in command:
                raise subprocess.CalledProcessError(1, command)
            image_release.write_digest(Path("IMAGE_DIGEST"), DIGEST, "quay.io/example")
        elif command[:2] == ("git", "restore"):
            Path("IMAGE_DIGEST").write_text("")

    monkeypatch.setattr(publish_image, "output", fake_output)
    monkeypatch.setattr(publish_image, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        publish_image.publish()
    assert len([call for call in calls if call[:2] == ("git", "push")]) == 1
    assert len([call for call in calls if call[:2] == ("gh", "api")]) == 1


def test_publisher_with_no_pending_merges_skips_release_steps(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    for key, value in {
        "GITHUB_REPOSITORY": "example/repo", "QUAY_REPOSITORY_PATH": "quay.io/example",
        "QUAY_ROBOT_USERNAME": "example-bot", "QUAY_PUSH_TOKEN": "mock-token",
        "GH_TOKEN": "mock-github-token",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(publish_image, "pending_sources", lambda *args: [])
    monkeypatch.setattr(publish_image, "output", lambda *args, **kwargs: "1" * 40)
    calls = []
    monkeypatch.setattr(publish_image, "run", lambda *args, **kwargs: calls.append(args))
    publish_image.publish()
    assert calls == [("git", "fetch", "origin", "main")]


def test_publisher_e2e_failure_restores_local_metadata_and_does_not_push(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.8\n")
    (tmp_path / "IMAGE_DIGEST").write_text("")
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    for key, value in {
        "GITHUB_REPOSITORY": "example/repo", "GITHUB_ACTOR": "example-bot",
        "QUAY_REPOSITORY_PATH": "quay.io/example", "QUAY_ROBOT_USERNAME": "example-bot",
        "QUAY_PUSH_TOKEN": "mock-token", "GH_TOKEN": "mock-github-token",
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_RUN_ID": "123",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(publish_image, "pending_sources", lambda *args: ["2" * 40])
    monkeypatch.setattr(publish_image, "output", lambda *args, **kwargs: "1" * 40)
    calls = []

    def fake_run(*command, **kwargs):
        calls.append(command)
        if command[:2] == ("make", "image-push"):
            image_release.write_digest(Path("IMAGE_DIGEST"), DIGEST, "quay.io/example")
        elif command[:2] == ("git", "restore"):
            if "--source=HEAD" in command:
                (tmp_path / "VERSION").write_text("1.4.8\n")
                (tmp_path / "IMAGE_DIGEST").write_text("")
                (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
            else:
                (tmp_path / "IMAGE_DIGEST").write_text("")
        elif command[:2] == ("python3", "scripts/e2e_kind_deploy.py"):
            assert (tmp_path / "VERSION").read_text() == "1.4.9\n"
            assert (tmp_path / "IMAGE_DIGEST").read_text() == REFERENCE + "\n"
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(publish_image, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        publish_image.publish()

    assert (tmp_path / "VERSION").read_text() == "1.4.8\n"
    assert (tmp_path / "IMAGE_DIGEST").read_text() == ""
    assert (tmp_path / "IMAGE_PUBLISH_STATE").read_text() == "1" * 40 + "\n"
    assert not any("commit" in command for command in calls)
    assert not any(command[:2] in (("git", "push"), ("gh", "api")) for command in calls)


def test_publisher_workflow_runs_kind_prerequisites_inline():
    workflow = yaml.load((ROOT / ".github/workflows/publish-image.yml").read_text(), Loader=yaml.BaseLoader)
    publish = workflow["jobs"]["publish"]
    names = [step.get("name") for step in publish["steps"]]
    assert names.index("Install KinD") < names.index("Prepare KinD E2E dependencies") < names.index("Publish all unprocessed squash merges")
    assert publish["env"]["CONTAINER_CMD"] == "docker"
    assert not any("Dispatch KinD E2E" in str(name) for name in names)

    e2e = yaml.load((ROOT / ".github/workflows/e2e-kind.yml").read_text(), Loader=yaml.BaseLoader)
    assert "metadata_sha" not in e2e["on"]["workflow_dispatch"]["inputs"]
    assert "report-published-image" not in e2e["jobs"]


def test_failed_latest_push_does_not_advance_release(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.9\n")
    (tmp_path / "IMAGE_DIGEST").write_text("")
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_ACTOR", "example-bot")
    monkeypatch.setenv("QUAY_REPOSITORY_PATH", "quay.io/example")
    monkeypatch.setenv("QUAY_ROBOT_USERNAME", "example-bot")
    monkeypatch.setenv("QUAY_PUSH_TOKEN", "mock-token")
    monkeypatch.setenv("GH_TOKEN", "mock-github-token")
    monkeypatch.setattr(publish_image, "pending_sources", lambda repo, baseline, tip, env: ["2" * 40])
    monkeypatch.setattr(publish_image, "output", lambda *command, **kwargs: "1" * 40)
    calls = []

    def fake_run(*command, **kwargs):
        calls.append(command)
        if command[:2] == ("make", "image-push"):
            if "IMAGE_TAG=latest" in command:
                raise subprocess.CalledProcessError(1, command)
            image_release.write_digest(Path("IMAGE_DIGEST"), DIGEST, "quay.io/example")

    monkeypatch.setattr(publish_image, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        publish_image.publish()
    assert (tmp_path / "VERSION").read_text() == "1.4.9\n"
    assert (tmp_path / "IMAGE_PUBLISH_STATE").read_text() == "1" * 40 + "\n"
    assert not any("commit" in call for call in calls)
    assert any("IMAGE_TAG=1.4.10" in call for call in calls)

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


def _set_publish_environment(monkeypatch, github_output):
    for key, value in {
        "GITHUB_REPOSITORY": "example/repo", "GITHUB_ACTOR": "example-bot",
        "QUAY_REPOSITORY_PATH": "quay.io/example", "QUAY_ROBOT_USERNAME": "example-bot",
        "QUAY_PUSH_TOKEN": "mock-token", "GH_TOKEN": "mock-github-token",
        "GITHUB_OUTPUT": str(github_output), "CONTAINER_CMD": "docker",
    }.items():
        monkeypatch.setenv(key, value)


def test_publish_stage_exports_candidates_for_separate_e2e(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.8\n")
    (tmp_path / "IMAGE_DIGEST").write_text("")
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    github_output = tmp_path / "github-output"
    _set_publish_environment(monkeypatch, github_output)
    sources = ["2" * 40, "3" * 40]
    monkeypatch.setattr(publish_image, "pending_sources", lambda repo, baseline, tip, env: sources)
    calls = []

    def fake_output(*command, env=None):
        return "1" * 40

    def fake_run(*command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ("make", "image-push"):
            image_release.write_digest(Path("IMAGE_DIGEST"), DIGEST, "quay.io/example")
        elif command[:2] == ("git", "restore"):
            Path("IMAGE_DIGEST").write_text("")

    monkeypatch.setattr(publish_image, "output", fake_output)
    monkeypatch.setattr(publish_image, "run", fake_run)
    publish_image.publish_candidates()

    commands = [command for command, _ in calls]
    assert (tmp_path / "VERSION").read_text() == "1.4.10\n"
    assert (tmp_path / "IMAGE_PUBLISH_STATE").read_text() == sources[-1] + "\n"
    assert (tmp_path / "IMAGE_DIGEST").read_text() == REFERENCE + "\n"
    assert len([c for c in commands if c[:2] == ("make", "image-build")]) == 2
    assert [next(a for a in c if a.startswith("IMAGE_TAG=")) for c in commands if c[:2] == ("make", "image-build")] == [
        "IMAGE_TAG=1.4.9", "IMAGE_TAG=1.4.10",
    ]
    assert sum("commit" in c for c in commands) == 2
    assert not any(c[:2] == ("python3", "scripts/e2e_kind_deploy.py") for c in commands)
    assert not any(c[:2] == ("git", "push") for c in commands)
    assert not any("IMAGE_TAG=latest" in c for c in commands)
    assert github_output.read_text() == (
        "published=true\nimage_refs<<IMAGE_REFS_EOF\n"
        f"{REFERENCE}\n{REFERENCE}\nIMAGE_REFS_EOF\n"
    )
    logs = capsys.readouterr().out
    assert logs.count("::group::Candidate ") == 2
    assert logs.count("::endgroup::") == 2
    assert "Build candidate image" in logs
    assert "Push SemVer candidate image" in logs
    for command, kwargs in calls:
        if command[:2] in (("make", "image-build"), ("make", "image-push")):
            assert "GH_TOKEN" not in kwargs["env"]


def test_publish_stage_with_no_pending_merges_marks_not_published(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "IMAGE_PUBLISH_STATE").write_text("1" * 40 + "\n")
    github_output = tmp_path / "github-output"
    _set_publish_environment(monkeypatch, github_output)
    monkeypatch.setattr(publish_image, "pending_sources", lambda *args: [])
    monkeypatch.setattr(publish_image, "output", lambda *args, **kwargs: "1" * 40)
    calls = []
    monkeypatch.setattr(publish_image, "run", lambda *args, **kwargs: calls.append(args))

    publish_image.publish_candidates()

    assert calls == [("git", "fetch", "origin", "main")]
    assert github_output.read_text() == "published=false\n"


def test_promote_stage_pushes_latest_then_local_metadata(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import publish_image

    monkeypatch.chdir(tmp_path)
    (tmp_path / "VERSION").write_text("1.4.9\n")
    (tmp_path / "IMAGE_DIGEST").write_text(REFERENCE + "\n")
    _set_publish_environment(monkeypatch, tmp_path / "github-output")
    calls = []

    def fake_output(*command, env=None):
        if command[:3] == ("git", "branch", "--show-current"):
            return "image-publisher"
        if command[:3] == ("git", "rev-list", "--count"):
            return "1"
        return "1" * 40

    def fake_run(*command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ("make", "image-push"):
            image_release.write_digest(Path("IMAGE_DIGEST"), DIGEST, "quay.io/example")

    monkeypatch.setattr(publish_image, "output", fake_output)
    monkeypatch.setattr(publish_image, "run", fake_run)
    publish_image.promote_and_push()

    commands = [command for command, _ in calls]
    latest_tag = next(i for i, c in enumerate(commands) if c[:2] == ("podman", "tag"))
    latest_push = next(i for i, c in enumerate(commands) if c[:2] == ("make", "image-push"))
    git_push = next(i for i, c in enumerate(commands) if c[:2] == ("git", "push"))
    assert commands[latest_tag][2:] == ("swarmer:1.4.9", "swarmer:latest")
    assert "IMAGE_TAG=latest" in commands[latest_push]
    assert latest_tag < latest_push < git_push
    assert commands[git_push] == ("git", "push", "origin", "HEAD:main")
    logs = capsys.readouterr().out
    assert "::group::Promote tested candidate swarmer:1.4.9 to latest" in logs
    assert "::group::Push release metadata commits to main" in logs
    assert logs.count("::endgroup::") == 2


def test_publish_workflow_has_visible_e2e_before_promotion_and_preserves_other_caller():
    workflow = yaml.load((ROOT / ".github/workflows/publish-image.yml").read_text(), Loader=yaml.BaseLoader)
    publish = workflow["jobs"]["publish"]
    steps = publish["steps"]
    names = [step.get("name") for step in steps]
    publish_index = names.index("Publish all unprocessed squash merges")
    install_kind_index = names.index("Install KinD")
    dependencies_index = names.index("Prepare KinD E2E dependencies")
    e2e_index = names.index("Run KinD deployment lifecycle e2e")
    promote_index = names.index("Promote latest and push release metadata to main")
    assert publish_index < install_kind_index < dependencies_index < e2e_index < promote_index
    assert steps[publish_index]["run"] == "python3 scripts/publish_image.py --stage publish"
    assert steps[publish_index]["id"] == "publish"
    assert steps[install_kind_index]["if"] == "${{ success() && steps.publish.outputs.published == 'true' }}"
    assert steps[dependencies_index]["if"] == "${{ success() && steps.publish.outputs.published == 'true' }}"
    assert steps[e2e_index]["if"] == "${{ success() && steps.publish.outputs.published == 'true' }}"
    assert "python3 scripts/e2e_kind_deploy.py" in steps[e2e_index]["run"]
    assert "--image-ref \"$image_ref\"" in steps[e2e_index]["run"]
    assert '[[ -n "$IMAGE_REFS" ]]' in steps[e2e_index]["run"]
    assert "::group::KinD E2E candidate" in steps[e2e_index]["run"]
    assert "::endgroup::" in steps[e2e_index]["run"]
    assert steps[promote_index]["if"] == "${{ success() && steps.publish.outputs.published == 'true' }}"
    assert steps[promote_index]["run"] == "python3 scripts/publish_image.py --stage promote-and-push"

    # The standalone PR/workflow_dispatch caller keeps using the same E2E CLI.
    e2e_workflow = yaml.load((ROOT / ".github/workflows/e2e-kind.yml").read_text(), Loader=yaml.BaseLoader)
    e2e_steps = e2e_workflow["jobs"]["kind-lifecycle"]["steps"]
    caller = next(step for step in e2e_steps if step.get("name") == "Run KinD deployment lifecycle e2e")
    assert "python3 scripts/e2e_kind_deploy.py" in caller["run"]
    assert "--cluster-name \"$KIND_CLUSTER\"" in caller["run"]

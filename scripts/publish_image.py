"""Catch up all squash-merged PR images and commit publication metadata."""

import os
from pathlib import Path
import re
import subprocess
import tempfile

from image_release import atomic_write, next_version, pending_sources, read_digest, read_semver, write_digest

SHA = re.compile(r"[0-9a-f]{40}\Z")
REGISTRY = re.compile(r"quay\.io/[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")


def run(*command: str, env: dict[str, str] | None = None, input: str | None = None, text: bool = False) -> None:
    subprocess.run(command, check=True, env=env, input=input, text=text)


def output(*command: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(command, text=True, env=env).strip()


def build_environment() -> dict[str, str]:
    """Allowlist runner settings needed by Podman, without registry or GitHub auth."""
    allowed = (
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME", "LANG", "LC_ALL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "CONTAINER_CMD",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def publish() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    registry = os.environ["QUAY_REPOSITORY_PATH"]
    username = os.environ["QUAY_ROBOT_USERNAME"]
    token = os.environ["QUAY_PUSH_TOKEN"]
    if not REGISTRY.fullmatch(registry) or not username or not token:
        raise ValueError("Valid Quay repository path, robot username and token are required")
    clean_env = build_environment()
    github_env = {**clean_env, "GH_TOKEN": os.environ["GH_TOKEN"]}
    if not SHA.fullmatch(output("git", "rev-parse", "HEAD", env=clean_env)):
        raise ValueError("Invalid checkout commit")

    with tempfile.TemporaryDirectory() as directory:
        askpass = Path(directory) / "git-askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in\n'
            '  *sername*) printf "%s\\n" x-access-token ;;\n'
            '  *assword*) printf "%s\\n" "$GH_TOKEN" ;;\n'
            '  *) exit 1 ;;\nesac\n'
        )
        askpass.chmod(0o700)
        git_env = {**github_env, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"}
        run("git", "fetch", "origin", "main", env=git_env)
        tip = output("git", "rev-parse", "origin/main", env=clean_env)
        baseline = Path("IMAGE_PUBLISH_STATE").read_text().strip()
        sources = pending_sources(repo, baseline, tip, github_env)
        if not sources:
            print("No unprocessed squash merges")
            return

        authfile = str(Path(directory) / "quay-auth.json")
        push_env = {**clean_env, "REGISTRY_AUTH_FILE": authfile}
        for source in sources:
            run("git", "fetch", "origin", "main", env=git_env)
            remote_tip = output("git", "rev-parse", "origin/main", env=clean_env)
            # A concurrent human merge is handled by a later workflow run.
            # A non-fast-forward metadata push below fails rather than losing it.
            run("git", "switch", "-C", "image-publisher", remote_tip, env=clean_env)
            version = next_version(read_semver(Path("VERSION")))
            run("git", "switch", "--detach", source, env=clean_env)
            print(f"Publishing squash merge {source} as {version}")
            make_args = (f"REGISTRY={registry}", "IMAGE=swarmer", f"IMAGE_TAG={version}", "CONTAINER_CMD=podman")
            run("make", "image-build", *make_args, env=clean_env)
            run(
                "podman", "login", "--authfile", authfile, "--username", username,
                "--password-stdin", "quay.io", input=token, text=True, env=push_env,
            )
            run("make", "image-push", *make_args, env=push_env)
            reference = read_digest(Path("IMAGE_DIGEST"), registry)
            run("podman", "tag", f"swarmer:{version}", "swarmer:latest", env=push_env)
            run("make", "image-push", f"REGISTRY={registry}", "IMAGE=swarmer", "IMAGE_TAG=latest", "CONTAINER_CMD=podman", env=push_env)
            if read_digest(Path("IMAGE_DIGEST"), registry) != reference:
                raise ValueError("Latest and SemVer pushes produced different registry digests")

            run("git", "restore", "IMAGE_DIGEST", env=clean_env)
            run("git", "switch", "image-publisher", env=clean_env)
            atomic_write(Path("VERSION"), version)
            write_digest(Path("IMAGE_DIGEST"), reference.rsplit("@", 1)[1], registry)
            atomic_write(Path("IMAGE_PUBLISH_STATE"), source)

            # Verify the digest while it is present in the local publisher
            # checkout; main is updated only after this deployment succeeds.
            try:
                run(
                    "python3", "scripts/e2e_kind_deploy.py",
                    "--cluster-name", "swarmer", "--namespace", "swarmer",
                    "--image-ref", reference,
                    env=clean_env,
                )
            except Exception:
                run(
                    "git", "restore", "--source=HEAD", "--staged", "--worktree",
                    "VERSION", "IMAGE_DIGEST", "IMAGE_PUBLISH_STATE", env=clean_env,
                )
                raise

            run("git", "add", "VERSION", "IMAGE_DIGEST", "IMAGE_PUBLISH_STATE", env=clean_env)
            message = f"Publish swarmer {version}\n\nImage-Publish-Source-SHA: {source}"
            author = os.environ["GITHUB_ACTOR"]
            run(
                "git", "-c", f"user.name={author}",
                "-c", f"user.email={author}@users.noreply.github.com",
                "commit", "-m", message, env=clean_env,
            )
            metadata_sha = output("git", "rev-parse", "HEAD", env=clean_env)
            if not SHA.fullmatch(metadata_sha):
                raise ValueError("Invalid metadata commit SHA")
            run("git", "push", "origin", "HEAD:main", env=git_env)

            run(
                "gh", "api", "--method", "POST", f"repos/{repo}/statuses/{metadata_sha}",
                "-f", "state=success", "-f", "context=KinD E2E / published image",
                "-f", "description=Published-image E2E passed",
                "-f", f"target_url={os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
                env=github_env,
            )
            Path(authfile).unlink(missing_ok=True)


if __name__ == "__main__":
    publish()

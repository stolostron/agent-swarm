"""Publish candidate images, then promote and push release metadata."""

import argparse
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


def _github_environment(clean_env: dict[str, str]) -> dict[str, str]:
    return {**clean_env, "GH_TOKEN": os.environ["GH_TOKEN"]}


def _git_environment(directory: str, github_env: dict[str, str]) -> dict[str, str]:
    askpass = Path(directory) / "git-askpass"
    askpass.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        '  *sername*) printf "%s\\n" x-access-token ;;\n'
        '  *assword*) printf "%s\\n" "$GH_TOKEN" ;;\n'
        '  *) exit 1 ;;\nesac\n'
    )
    askpass.chmod(0o700)
    return {**github_env, "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"}


def _validate_registry_credentials() -> tuple[str, str, str]:
    registry = os.environ["QUAY_REPOSITORY_PATH"]
    username = os.environ["QUAY_ROBOT_USERNAME"]
    token = os.environ["QUAY_PUSH_TOKEN"]
    if not REGISTRY.fullmatch(registry) or not username or not token:
        raise ValueError("Valid Quay repository path, robot username and token are required")
    return registry, username, token


def _write_github_output(published: bool, references: list[str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        output_file.write(f"published={'true' if published else 'false'}\n")
        if references:
            output_file.write("image_refs<<IMAGE_REFS_EOF\n")
            output_file.write("\n".join(references))
            output_file.write("\nIMAGE_REFS_EOF\n")


def publish_candidates() -> None:
    """Push SemVer candidates and prepare local metadata commits for E2E."""
    repo = os.environ["GITHUB_REPOSITORY"]
    registry, username, token = _validate_registry_credentials()
    clean_env = build_environment()
    github_env = _github_environment(clean_env)
    if not SHA.fullmatch(output("git", "rev-parse", "HEAD", env=clean_env)):
        raise ValueError("Invalid checkout commit")

    references: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        git_env = _git_environment(directory, github_env)
        run("git", "fetch", "origin", "main", env=git_env)
        remote_tip = output("git", "rev-parse", "origin/main", env=clean_env)
        baseline = Path("IMAGE_PUBLISH_STATE").read_text().strip()
        sources = pending_sources(repo, baseline, remote_tip, github_env)
        if not sources:
            print("No unprocessed squash merges")
            _write_github_output(False, references)
            return

        if not SHA.fullmatch(remote_tip):
            raise ValueError("Invalid origin/main commit")
        run("git", "switch", "-C", "image-publisher", remote_tip, env=clean_env)
        authfile = str(Path(directory) / "quay-auth.json")
        push_env = {**clean_env, "REGISTRY_AUTH_FILE": authfile}
        run(
            "podman", "login", "--authfile", authfile, "--username", username,
            "--password-stdin", "quay.io", input=token, text=True, env=push_env,
        )

        print(f"Found {len(sources)} unprocessed squash merge(s)")
        for index, source in enumerate(sources, start=1):
            version = next_version(read_semver(Path("VERSION")))
            run("git", "switch", "--detach", source, env=clean_env)
            print(f"::group::Candidate {index}/{len(sources)} — swarmer:{version} ({source})")
            try:
                make_args = (f"REGISTRY={registry}", "IMAGE=swarmer", f"IMAGE_TAG={version}", "CONTAINER_CMD=podman")
                print("Build candidate image")
                run("make", "image-build", *make_args, env=clean_env)
                print("Push SemVer candidate image")
                run("make", "image-push", *make_args, env=push_env)
                reference = read_digest(Path("IMAGE_DIGEST"), registry)
                references.append(reference)

                print(f"Prepare local metadata for digest {reference}")
                run("git", "restore", "IMAGE_DIGEST", env=clean_env)
                run("git", "switch", "image-publisher", env=clean_env)
                atomic_write(Path("VERSION"), version)
                write_digest(Path("IMAGE_DIGEST"), reference.rsplit("@", 1)[1], registry)
                atomic_write(Path("IMAGE_PUBLISH_STATE"), source)
                run("git", "add", "VERSION", "IMAGE_DIGEST", "IMAGE_PUBLISH_STATE", env=clean_env)
                message = f"Publish swarmer {version}\n\nImage-Publish-Source-SHA: {source}"
                author = os.environ["GITHUB_ACTOR"]
                run(
                    "git", "-c", f"user.name={author}",
                    "-c", f"user.email={author}@users.noreply.github.com",
                    "commit", "-m", message, env=clean_env,
                )
            finally:
                print("::endgroup::")

    _write_github_output(True, references)


def promote_and_push() -> None:
    """Promote the tested final candidate and push the prepared metadata commits."""
    registry, username, token = _validate_registry_credentials()
    clean_env = build_environment()
    github_env = _github_environment(clean_env)
    version = read_semver(Path("VERSION"))
    reference = read_digest(Path("IMAGE_DIGEST"), registry)

    with tempfile.TemporaryDirectory() as directory:
        git_env = _git_environment(directory, github_env)
        authfile = str(Path(directory) / "quay-auth.json")
        push_env = {**clean_env, "REGISTRY_AUTH_FILE": authfile}
        if output("git", "branch", "--show-current", env=clean_env) != "image-publisher":
            raise ValueError("Prepared release metadata is not checked out on image-publisher")
        run("git", "fetch", "origin", "main", env=git_env)
        run("git", "merge-base", "--is-ancestor", "origin/main", "HEAD", env=clean_env)
        metadata_commits = output("git", "rev-list", "--count", "origin/main..HEAD", env=clean_env)
        if not metadata_commits.isdigit() or metadata_commits == "0":
            raise ValueError("No prepared release metadata commits to push")

        print(f"::group::Promote tested candidate swarmer:{version} to latest")
        try:
            run(
                "podman", "login", "--authfile", authfile, "--username", username,
                "--password-stdin", "quay.io", input=token, text=True, env=push_env,
            )
            run("podman", "tag", f"swarmer:{version}", "swarmer:latest", env=push_env)
            run(
                "make", "image-push", f"REGISTRY={registry}", "IMAGE=swarmer",
                "IMAGE_TAG=latest", "CONTAINER_CMD=podman", env=push_env,
            )
            if read_digest(Path("IMAGE_DIGEST"), registry) != reference:
                raise ValueError("Latest and SemVer pushes produced different registry digests")
        finally:
            print("::endgroup::")
        print("::group::Push release metadata commits to main")
        try:
            run("git", "push", "origin", "HEAD:main", env=git_env)
        finally:
            print("::endgroup::")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("publish", "promote-and-push"))
    args = parser.parse_args()
    if args.stage == "publish":
        publish_candidates()
    else:
        promote_and_push()


if __name__ == "__main__":
    main()

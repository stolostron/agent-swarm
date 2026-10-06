"""Resolve Swarmer's default OpenCode image from the latest stable release."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

RELEASE_URL = "https://api.github.com/repos/anomalyco/opencode/releases/latest"
IMAGE_REPOSITORY = "ghcr.io/anomalyco/opencode"
SEMVER_RE = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
IMAGE_REF_RE = re.compile(
    r"^[A-Za-z0-9.-]+(?::[0-9]+)?/"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})$"
)


class ResolutionError(ValueError):
    """Raised when a configured or published image reference is unusable."""


def validate_image_ref(image_ref: str) -> str:
    """Require a complete registry-qualified image reference."""
    if not IMAGE_REF_RE.fullmatch(image_ref):
        raise ResolutionError(
            "AGENT_IMAGE_OPENCODE must be a full image reference with a registry "
            "and tag or sha256 digest"
        )
    return image_ref


def fetch_latest_version() -> str:
    """Read and validate the latest stable OpenCode GitHub release tag."""
    request = Request(
        RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "agent-swarm-image-resolver",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            metadata = json.load(response)
    except (OSError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ResolutionError(f"could not fetch OpenCode release metadata: {exc}") from exc

    if not isinstance(metadata, dict):
        raise ResolutionError("OpenCode release metadata is not a JSON object")
    if metadata.get("draft") is True or metadata.get("prerelease") is True:
        raise ResolutionError("GitHub returned a draft or prerelease instead of a stable release")

    tag = metadata.get("tag_name")
    if not isinstance(tag, str):
        raise ResolutionError(f"OpenCode release has an invalid SemVer tag: {tag!r}")
    match = SEMVER_RE.fullmatch(tag)
    if not match:
        raise ResolutionError(f"OpenCode release has an invalid SemVer tag: {tag!r}")
    prerelease = match.group(4)
    if prerelease:
        raise ResolutionError(f"OpenCode release is not a stable version: {tag!r}")
    if match.group(5):
        raise ResolutionError(
            f"OpenCode release tag cannot be represented as an OCI SemVer tag: {tag!r}"
        )

    # OpenCode release tags use a leading v; container tags use bare SemVer.
    return tag.removeprefix("v")


def resolve_image(override: str | None = None) -> str:
    """Return a validated explicit override or resolve the current stable image."""
    if override:
        return validate_image_ref(override)
    version = fetch_latest_version()
    return f"{IMAGE_REPOSITORY}:{version}"


def main() -> int:
    try:
        print(resolve_image(os.environ.get("AGENT_IMAGE_OPENCODE", "")))
    except ResolutionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

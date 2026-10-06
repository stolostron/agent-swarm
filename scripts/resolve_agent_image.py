"""Resolve Swarmer's default OpenCode image from agent-containers' VERSION."""

from __future__ import annotations

import os
import re
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen

VERSION_URL = "https://raw.githubusercontent.com/stolostron/agent-containers/main/VERSION"
IMAGE_REPOSITORY = "quay.io/jpacker/opencode"
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
    """Read and validate the published agent-containers image version."""
    request = Request(
        VERSION_URL,
        headers={
            "Accept": "text/plain",
            "User-Agent": "agent-swarm-image-resolver",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            version = response.read().decode("utf-8").strip()
    except (OSError, URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise ResolutionError(f"could not fetch agent-containers VERSION: {exc}") from exc

    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ResolutionError(f"agent-containers VERSION is not valid SemVer: {version!r}")
    prerelease = match.group(4)
    if prerelease:
        raise ResolutionError(f"agent-containers VERSION is not a stable version: {version!r}")
    if match.group(5):
        raise ResolutionError(
            f"agent-containers VERSION cannot be represented as an OCI SemVer tag: {version!r}"
        )

    # VERSION is the image tag; normalize an optional leading v for OCI tagging.
    return version.removeprefix("v")


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

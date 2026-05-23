"""Unit tests for _build_repo_context() in swarmer.k8s_session.

Tests the pure function that generates the markdown repository context
block appended to AGENTS.md and prompt text.  No K8s or DB dependencies.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.k8s_session import _build_repo_context  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal stub for SessionRepo
# ---------------------------------------------------------------------------

class _FakeRepo:
    """Lightweight stand-in for SessionRepo used only in these tests."""
    def __init__(self, repo_url: str, branch: str = "main", local_path: str = ""):
        self.repo_url = repo_url
        self.branch = branch
        self.local_path = local_path or repo_url.rstrip("/").split("/")[-1].removesuffix(".git")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_repos_returns_empty_string():
    assert _build_repo_context([]) == ""


def test_none_repos_returns_empty_string():
    assert _build_repo_context(None) == ""


def test_single_repo():
    repos = [_FakeRepo("https://github.com/stolostron/agent-swarm.git")]
    result = _build_repo_context(repos)

    assert "## Workspace Repositories" in result
    assert "| `stolostron/agent-swarm` |" in result
    assert "| `main` |" in result
    assert "| `/workspace/agent-swarm` |" in result


def test_multiple_repos():
    repos = [
        _FakeRepo("https://github.com/stolostron/agent-swarm.git", "main"),
        _FakeRepo("https://github.com/stolostron/agent-containers", "release-2.11", "containers"),
    ]
    result = _build_repo_context(repos)

    # Both repos present
    assert "| `stolostron/agent-swarm` |" in result
    assert "| `stolostron/agent-containers` |" in result
    # Branch and path correct for second repo
    assert "| `release-2.11` |" in result
    assert "| `/workspace/containers` |" in result


def test_org_repo_extraction_without_git_suffix():
    repos = [_FakeRepo("https://github.com/org/repo-name")]
    result = _build_repo_context(repos)
    assert "| `org/repo-name` |" in result


def test_org_repo_extraction_with_trailing_slash():
    repos = [_FakeRepo("https://github.com/org/repo-name/")]
    result = _build_repo_context(repos)
    assert "| `org/repo-name` |" in result


def test_custom_local_path():
    repos = [_FakeRepo("https://github.com/org/repo.git", local_path="my-custom-path")]
    result = _build_repo_context(repos)
    assert "| `/workspace/my-custom-path` |" in result


def test_output_is_valid_markdown_table():
    repos = [_FakeRepo("https://github.com/org/repo.git")]
    result = _build_repo_context(repos)

    lines = [l for l in result.strip().split("\n") if l.startswith("|")]
    # Header row + separator + at least one data row
    assert len(lines) >= 3
    # Header
    assert "Repository" in lines[0]
    assert "Branch" in lines[0]
    assert "Path" in lines[0]
    # Separator
    assert lines[1].replace("|", "").replace("-", "").strip() == ""


def test_result_starts_with_newlines():
    """Ensure the block starts with blank lines so it separates from prior content."""
    repos = [_FakeRepo("https://github.com/org/repo.git")]
    result = _build_repo_context(repos)
    assert result.startswith("\n\n")

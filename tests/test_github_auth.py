"""Tests for GitHub App-first auth with PAT fallback in session pods."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarmer.github_auth import (  # noqa: E402
    build_git_credential_setup_shell,
    build_git_user_setup_shell,
    pat_injects_gh_token,
)


class TestPatInjectsGhToken:
    def test_pat_only_session_sets_gh_token(self):
        assert pat_injects_gh_token(has_github_app=False) is True

    def test_app_session_leaves_gh_token_unset(self):
        assert pat_injects_gh_token(has_github_app=True) is False


class TestGitCredentialSetup:
    def test_app_mode_adds_pat_as_secondary_helper(self):
        script = build_git_credential_setup_shell(has_github_app=True)
        assert "--add credential.helper store" in script
        assert "user.name" not in script

    def test_pat_only_mode_sets_user_identity(self):
        script = build_git_credential_setup_shell(has_github_app=False)
        assert "credential.helper store" in script
        assert "user.name" in script
        assert "--add" not in script

    def test_app_user_setup_is_separate(self):
        script = build_git_user_setup_shell(has_github_app=True)
        assert "user.name" in script
        assert "user.email" in script

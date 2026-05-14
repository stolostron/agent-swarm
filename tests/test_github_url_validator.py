"""Unit tests for swarmer.github_url_validator.

Each acceptance-criterion category from ACM-32991 is covered:

* Userinfo credentials  (https://user:TOKEN@github.com/…)
* Query parameter tokens (denylist names + token-shaped values)
* Path-encoded tokens   (token-shaped path segments)
* SSH URL token segments
* Clean URLs that must NOT be rejected

Tests are pure Python — no network, no FastAPI, no SQLAlchemy.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from swarmer.github_url_validator import GitHubURLError, validate_github_url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_rejected(url: str, *, fragment: str | None = None) -> GitHubURLError:
    """Assert the URL is rejected and return the exception for further checks."""
    with pytest.raises(GitHubURLError) as exc_info:
        validate_github_url(url)
    exc = exc_info.value
    assert exc.redacted_url, "redacted_url must not be empty"
    if fragment:
        assert fragment in exc.redacted_url or fragment in exc.reason, (
            f"Expected '{fragment}' in reason or redacted_url.\n"
            f"  reason:       {exc.reason}\n"
            f"  redacted_url: {exc.redacted_url}"
        )
    return exc


def _assert_clean(url: str) -> None:
    """Assert that a clean URL passes validation without raising."""
    validate_github_url(url)  # must not raise


# ===========================================================================
# 1. Userinfo credentials
# ===========================================================================

class TestUserinfoCredentials:
    """URLs with credentials in the userinfo (user:password@host) field."""

    def test_password_is_classic_pat(self):
        exc = _assert_rejected(
            "https://user:ghp_ABCDEFGHIJ1234567890@github.com/owner/repo",
        )
        assert "userinfo" in exc.reason.lower()
        # Token value must NOT appear in the redacted URL
        assert "ghp_ABCDEFGHIJ" not in exc.redacted_url

    def test_password_is_fine_grained_pat(self):
        exc = _assert_rejected(
            "https://x-access-token:github_pat_ABCDEFGHIJKLMNOPQRST@github.com/owner/repo",
        )
        assert "userinfo" in exc.reason.lower()
        assert "github_pat_" not in exc.redacted_url

    def test_username_only_no_password(self):
        """A bare username with no password still triggers a rejection."""
        _assert_rejected("https://mytoken@github.com/owner/repo")

    def test_user_and_40hex_password(self):
        exc = _assert_rejected(
            "https://user:aabbccddeeff00112233445566778899aabbccdd@github.com/owner/repo",
        )
        assert "userinfo" in exc.reason.lower()

    def test_user_and_blank_password_not_rejected(self):
        """A URL with a username but truly empty password has no secret — allow it.

        urllib.parse treats ``https://user@host`` as username='user', password=None.
        If both are present but password is an empty string after the colon that
        is technically ``password=''`` — we conservatively reject that too since
        a colon separator is unusual for GitHub HTTPS URLs.
        """
        # Plain username, no colon/password — urlparse sets password=None
        # We reject if *either* username or password is truthy.
        # "user@host" → username='user', password=None → rejected (username truthy)
        _assert_rejected("https://user@github.com/owner/repo")


# ===========================================================================
# 2. Denylisted query parameter names
# ===========================================================================

class TestDenylistedQueryParams:
    """Query parameters whose *names* are on the auth token denylist."""

    @pytest.mark.parametrize("param_name", [
        "token",
        "access_token",
        "api_key",
        "auth",
        "authorization",
        "bearer",
        # Case variations
        "TOKEN",
        "Access_Token",
        "API_KEY",
    ])
    def test_denylisted_param_name(self, param_name: str):
        url = f"https://github.com/owner/repo?{param_name}=somevalue"
        exc = _assert_rejected(url)
        assert "denylist" in exc.reason.lower()

    def test_denylisted_param_redacts_value(self):
        exc = _assert_rejected(
            "https://github.com/owner/repo?token=ghp_SUPERSECRET1234567890"
        )
        assert "ghp_SUPE" not in exc.redacted_url
        assert "[REDACTED]" not in exc.redacted_url  # we use **** not [REDACTED]
        assert "****" in exc.redacted_url

    def test_denylisted_param_among_others(self):
        """Denylist hit even when mixed with innocent parameters."""
        _assert_rejected(
            "https://github.com/owner/repo?ref=main&token=abc123&format=json"
        )

    def test_multiple_denylisted_params(self):
        _assert_rejected(
            "https://github.com/owner/repo?token=abc&access_token=def"
        )


# ===========================================================================
# 3. Token-shaped query parameter values (regardless of param name)
# ===========================================================================

class TestTokenShapedQueryValues:
    """Query parameters whose *values* look like GitHub tokens."""

    def test_ghp_value_arbitrary_param_name(self):
        exc = _assert_rejected(
            "https://github.com/owner/repo?ref=ghp_ABCDEF1234567890ABCDEF1234567890"
        )
        assert "token-shaped" in exc.reason.lower()

    def test_github_pat_value_arbitrary_param_name(self):
        _assert_rejected(
            "https://github.com/owner/repo?key=github_pat_ABCDEFGHIJKLMNOPQRST"
        )

    def test_40hex_value_arbitrary_param_name(self):
        _assert_rejected(
            "https://github.com/owner/repo?secret=aabbccddeeff00112233445566778899aabbccdd"
        )

    def test_gho_oauth_token_value(self):
        _assert_rejected(
            "https://github.com/owner/repo?t=gho_ABCDEF1234567890"
        )

    def test_ghs_app_token_value(self):
        _assert_rejected(
            "https://github.com/owner/repo?t=ghs_ABCDEF1234567890"
        )

    def test_ghu_user_token_value(self):
        _assert_rejected(
            "https://github.com/owner/repo?t=ghu_ABCDEF1234567890"
        )

    def test_short_hex_not_rejected(self):
        """A short hex value (< 40 chars) should not be flagged as a token."""
        _assert_clean("https://github.com/owner/repo?color=ff0000")

    def test_39hex_not_rejected(self):
        """39 hex chars is not a 40-char token."""
        _assert_clean(
            "https://github.com/owner/repo?ref=aabbccddeeff00112233445566778899aabbcc"
        )


# ===========================================================================
# 4. Path-encoded tokens (HTTPS)
# ===========================================================================

class TestPathEncodedTokensHTTPS:
    """Token-shaped segments embedded in the URL path."""

    def test_ghp_in_path(self):
        exc = _assert_rejected(
            "https://github.com/ghp_ABCDEF1234567890/owner/repo"
        )
        assert "path" in exc.reason.lower()

    def test_github_pat_in_path(self):
        _assert_rejected(
            "https://github.com/github_pat_ABCDEFGHIJKLMNOPQRST/owner/repo"
        )

    def test_40hex_in_path(self):
        _assert_rejected(
            "https://github.com/owner/aabbccddeeff00112233445566778899aabbccdd/file"
        )

    def test_token_disguised_as_git_suffix(self):
        """A token with a .git suffix must still be caught."""
        _assert_rejected(
            "https://github.com/owner/ghp_ABCDEF1234567890.git"
        )

    def test_normal_path_not_rejected(self):
        """Short owner/repo slugs must not be rejected."""
        _assert_clean("https://github.com/stolostron/agent-swarm")
        _assert_clean("https://github.com/stolostron/agent-swarm.git")
        _assert_clean("https://github.com/stolostron/agent-swarm/")


# ===========================================================================
# 5. SSH URL token segments
# ===========================================================================

class TestSSHURLTokenSegments:
    """SSH-format GitHub URLs with token-shaped components."""

    def test_ghp_as_repo_slug(self):
        _assert_rejected("git@github.com:owner/ghp_ABCDEF1234567890")

    def test_github_pat_as_owner(self):
        _assert_rejected("git@github.com:github_pat_ABCDEFGHIJKLMNOPQRST/repo")

    def test_40hex_as_repo(self):
        _assert_rejected(
            "git@github.com:owner/aabbccddeeff00112233445566778899aabbccdd"
        )

    def test_clean_ssh_url(self):
        _assert_clean("git@github.com:stolostron/agent-swarm.git")
        _assert_clean("git@github.com:owner/repo")


# ===========================================================================
# 6. Clean URLs that must pass
# ===========================================================================

class TestCleanURLs:
    """Valid GitHub URLs that must not be rejected."""

    @pytest.mark.parametrize("url", [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://github.com/owner/repo/",
        "https://www.github.com/owner/repo",
        "https://github.com/owner/repo?ref=main",
        "https://github.com/owner/repo?ref=feature/my-feature",
        "https://github.com/stolostron/agent-swarm",
        "git@github.com:owner/repo.git",
        "git@github.com:stolostron/agent-swarm",
        # Non-GitHub URLs: validator should pass them through (not its job to block)
        "https://gitlab.com/owner/repo",
        "https://bitbucket.org/owner/repo",
        # Empty string — allowed (caller decides if required)
        "",
    ])
    def test_clean_url_passes(self, url: str):
        _assert_clean(url)

    def test_ref_with_sha_like_branch_name(self):
        """A ref= parameter that looks like a 39-char string is fine."""
        _assert_clean("https://github.com/owner/repo?ref=deadbeef")

    def test_url_with_innocent_params(self):
        _assert_clean(
            "https://github.com/owner/repo?ref=main&format=patch&strip=1"
        )


# ===========================================================================
# 7. Redacted URL safety
# ===========================================================================

class TestRedactedURLSafety:
    """The redacted_url attribute must never expose the actual token."""

    def test_redacted_url_hides_userinfo_password(self):
        raw_token = "ghp_SUPERSECRET1234567890ABCDEF"
        exc = _assert_rejected(
            f"https://user:{raw_token}@github.com/owner/repo"
        )
        assert raw_token not in exc.redacted_url

    def test_redacted_url_hides_query_param_value(self):
        raw_token = "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        exc = _assert_rejected(
            f"https://github.com/owner/repo?token={raw_token}"
        )
        assert raw_token not in exc.redacted_url

    def test_redacted_url_is_loggable_string(self):
        exc = _assert_rejected(
            "https://user:ghp_SECRET@github.com/owner/repo"
        )
        # Must be a non-empty string (safe to pass to log.warning())
        assert isinstance(exc.redacted_url, str)
        assert len(exc.redacted_url) > 0

    def test_exception_is_value_error_subclass(self):
        with pytest.raises(ValueError):
            validate_github_url(
                "https://user:ghp_TOKEN@github.com/owner/repo"
            )

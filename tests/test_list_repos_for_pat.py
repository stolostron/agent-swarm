"""Unit tests for list_repos_for_pat() in swarmer.github.

Uses respx to mock httpx calls so no network access is required.
The function lives in swarmer/github.py which only imports httpx — no
FastAPI or SQLAlchemy transitive dependencies, so tests run in isolation.
"""

import pytest
import respx
import httpx

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.github import list_repos_for_pat  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal stub for GitHubPAT — avoids pulling in the full SQLAlchemy stack
# ---------------------------------------------------------------------------

class _FakePAT:
    """Lightweight stand-in for GitHubPAT used only in these tests."""
    def __init__(self, *, pat: str, github_username: str = "octocat", github_org: str = ""):
        self.pat = pat
        self.github_username = github_username
        self.github_org = github_org


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo(n: int, private: bool = False) -> dict:
    return {
        "full_name": f"octocat/repo-{n}",
        "private": private,
        "updated_at": f"2026-01-{n:02d}T00:00:00Z",
        "description": f"Repository {n}",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_repos_single_page():
    """Returns repos from /user/repos when github_org is blank."""
    pat = _FakePAT(pat="ghp_test", github_org="")
    repos = [_repo(i) for i in range(1, 4)]

    with respx.mock:
        respx.get("https://api.github.com/user/repos").mock(
            return_value=httpx.Response(200, json=repos)
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0]["full_name"] == "octocat/repo-1"


@pytest.mark.asyncio
async def test_org_repos_used_when_github_org_set():
    """Calls /orgs/{org}/repos when github_org is populated."""
    pat = _FakePAT(pat="ghp_test", github_org="my-enterprise")
    repos = [_repo(i) for i in range(1, 6)]

    with respx.mock:
        respx.get("https://api.github.com/orgs/my-enterprise/repos").mock(
            return_value=httpx.Response(200, json=repos)
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, list)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_pagination_followed():
    """Follows the Link: <next> header to fetch multiple pages."""
    pat = _FakePAT(pat="ghp_test", github_org="")
    page1 = [_repo(i) for i in range(1, 4)]
    page2 = [_repo(i) for i in range(4, 7)]

    # The next-page URL the function will follow verbatim from the Link header.
    page2_url = "https://api.github.com/user/repos?page=2&per_page=100"

    call_count = 0

    def _handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json=page1,
                headers={"link": f'<{page2_url}>; rel="next"'},
            )
        return httpx.Response(200, json=page2)

    with respx.mock:
        respx.get(url__startswith="https://api.github.com/user/repos").mock(
            side_effect=_handler
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, list)
    assert len(result) == 6
    assert result[3]["full_name"] == "octocat/repo-4"
    assert call_count == 2


@pytest.mark.asyncio
async def test_api_error_returns_string():
    """Returns a descriptive string on a non-200 GitHub API response."""
    pat = _FakePAT(pat="ghp_bad")

    with respx.mock:
        respx.get("https://api.github.com/user/repos").mock(
            return_value=httpx.Response(
                401,
                json={"message": "Bad credentials"},
                headers={"content-type": "application/json"},
            )
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, str)
    assert "401" in result
    assert "Bad credentials" in result


@pytest.mark.asyncio
async def test_network_error_returns_string():
    """Returns a descriptive string on a network-level exception."""
    pat = _FakePAT(pat="ghp_test")

    with respx.mock:
        respx.get("https://api.github.com/user/repos").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, str)
    assert "Failed to contact GitHub API" in result


@pytest.mark.asyncio
async def test_empty_repo_list():
    """Handles an empty repo list without error."""
    pat = _FakePAT(pat="ghp_test", github_org="empty-org")

    with respx.mock:
        respx.get("https://api.github.com/orgs/empty-org/repos").mock(
            return_value=httpx.Response(200, json=[])
        )
        result = await list_repos_for_pat(pat)

    assert result == []


@pytest.mark.asyncio
async def test_pagination_cap_at_500():
    """Stops fetching after 500 repos even if more pages exist."""
    pat = _FakePAT(pat="ghp_test", github_org="big-org")

    # 6 pages of 100 repos each; only the first 5 (= 500 repos) should be fetched.
    pages = []
    for p in range(1, 7):
        repos = [_repo(p * 100 + i) for i in range(100)]
        nxt = f"https://api.github.com/orgs/big-org/repos?page={p + 1}"
        link_hdr = f'<{nxt}>; rel="next"' if p < 6 else ""
        pages.append((repos, link_hdr))

    call_count = 0

    def _handler(request):
        nonlocal call_count
        call_count += 1
        repos, link_hdr = pages[call_count - 1]
        headers = {"link": link_hdr} if link_hdr else {}
        return httpx.Response(200, json=repos, headers=headers)

    with respx.mock:
        respx.get(url__startswith="https://api.github.com/orgs/big-org/repos").mock(
            side_effect=_handler
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, list)
    assert len(result) == 500
    assert call_count == 5  # exactly 5 pages fetched; 6th skipped


@pytest.mark.asyncio
async def test_authorization_header_sent():
    """Verifies the PAT is included in the Authorization header."""
    pat = _FakePAT(pat="ghp_supersecret")
    captured_headers: dict = {}

    def _handler(request):
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json=[])

    with respx.mock:
        respx.get("https://api.github.com/user/repos").mock(side_effect=_handler)
        await list_repos_for_pat(pat)

    assert captured_headers.get("authorization") == "token ghp_supersecret"


@pytest.mark.asyncio
async def test_org_404_falls_back_to_user_repos():
    """Falls back to /users/{name}/repos when /orgs/{name}/repos returns 404.

    This covers the case where github_org is set to a personal GitHub username
    rather than a GitHub organization.
    """
    pat = _FakePAT(pat="ghp_test", github_org="jnpacker")
    repos = [_repo(i) for i in range(1, 4)]

    with respx.mock:
        respx.get("https://api.github.com/orgs/jnpacker/repos").mock(
            return_value=httpx.Response(
                404,
                json={"message": "Not Found"},
                headers={"content-type": "application/json"},
            )
        )
        respx.get("https://api.github.com/users/jnpacker/repos").mock(
            return_value=httpx.Response(200, json=repos)
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, list)
    assert len(result) == 3
    assert result[0]["full_name"] == "octocat/repo-1"


@pytest.mark.asyncio
async def test_org_404_fallback_non_404_error_not_retried():
    """A non-404 error from /orgs/{name}/repos is returned as-is, not retried."""
    pat = _FakePAT(pat="ghp_test", github_org="my-enterprise")

    with respx.mock:
        respx.get("https://api.github.com/orgs/my-enterprise/repos").mock(
            return_value=httpx.Response(
                403,
                json={"message": "Forbidden"},
                headers={"content-type": "application/json"},
            )
        )
        result = await list_repos_for_pat(pat)

    assert isinstance(result, str)
    assert "403" in result
    assert "Forbidden" in result

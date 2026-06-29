"""Unit tests for fetch_repo_info() in swarmer.github.

Logic under test:
  - No token (pat=None) → do nothing, return all-None for every repo.
  - Token present (PAT or App IAT) → call GitHub API; assume no write access
    unless push=True is confirmed. Any non-200 or missing/false push → can_push=False.
  - 401 (invalid token) → retry unauthenticated for public/private visibility,
    can_push=False regardless.
"""

import pytest
import respx
import httpx
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.github import fetch_repo_info  # noqa: E402


class _FakeRepo:
    def __init__(self, repo_id: int, url: str):
        self.id = repo_id
        self.repo_url = url


REPO_URL = "https://github.com/myorg/myrepo"
REPO_API = "https://api.github.com/repos/myorg/myrepo"


def _repos(url=REPO_URL):
    return [_FakeRepo(1, url)]


# ---------------------------------------------------------------------------
# No token — do nothing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_token_returns_all_none():
    """With no token, skip all API calls and return None for all repos."""
    with respx.mock:
        # No HTTP calls should be made — respx will raise if any are attempted.
        result = await fetch_repo_info(_repos(), pat=None)

    assert result[1] == {"is_public": None, "can_push": None}


# ---------------------------------------------------------------------------
# Classic PAT — push confirmed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pat_push_true():
    """PAT with push=True in permissions → can_push=True."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": True, "pull": True}}
        ))
        result = await fetch_repo_info(_repos(), pat="ghp_valid")

    assert result[1]["can_push"] is True
    assert result[1]["is_public"] is True


# ---------------------------------------------------------------------------
# PAT — no permissions object (repo not in PAT scope, public repo)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pat_no_permissions_object():
    """PAT authenticated but no permissions object → can_push=False.

    GitHub omits the permissions field when the token has no collaborator access,
    even for public repos. Absence → no write access.
    """
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False}
        ))
        result = await fetch_repo_info(_repos(), pat="ghp_valid")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is True


# ---------------------------------------------------------------------------
# PAT — push=False explicitly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pat_push_false():
    """PAT with push=False → can_push=False."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": False, "pull": True}}
        ))
        result = await fetch_repo_info(_repos(), pat="ghp_valid")

    assert result[1]["can_push"] is False


# ---------------------------------------------------------------------------
# Fine-grained PAT — public repo, push=True from permissions but refs 403
# (GitHub reports user collaborator status, not token scope)
# ---------------------------------------------------------------------------

REFS_API = "https://api.github.com/repos/myorg/myrepo/git/refs"

@pytest.mark.asyncio
async def test_fine_grained_pat_public_repo_not_in_scope():
    """Fine-grained PAT: public repo shows push=True (user is collaborator) but
    refs endpoint returns 403 because the repo is not in the token's scope.
    → can_push=False.
    """
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": True, "pull": True}}
        ))
        respx.get(REFS_API).mock(return_value=httpx.Response(403))
        result = await fetch_repo_info(_repos(), pat="github_pat_AAAA")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is True


@pytest.mark.asyncio
async def test_fine_grained_pat_public_repo_in_scope():
    """Fine-grained PAT: public repo in token scope — refs returns 200 → can_push=True."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": True, "pull": True}}
        ))
        respx.get(REFS_API).mock(return_value=httpx.Response(200, json=[]))
        result = await fetch_repo_info(_repos(), pat="github_pat_AAAA")

    assert result[1]["can_push"] is True
    assert result[1]["is_public"] is True


@pytest.mark.asyncio
async def test_fine_grained_pat_push_false_skips_refs_probe():
    """Fine-grained PAT: push=False → no refs probe needed, immediately can_push=False."""
    refs_called = False

    def _refs_handler(request):
        nonlocal refs_called
        refs_called = True
        return httpx.Response(200, json=[])

    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": False}}
        ))
        respx.get(REFS_API).mock(side_effect=_refs_handler)
        result = await fetch_repo_info(_repos(), pat="github_pat_AAAA")

    assert result[1]["can_push"] is False
    assert refs_called is False  # probe not made when push already False


@pytest.mark.asyncio
async def test_classic_pat_does_not_probe_refs():
    """Classic PAT (ghp_...): no refs probe — trust permissions.push directly."""
    refs_called = False

    def _refs_handler(request):
        nonlocal refs_called
        refs_called = True
        return httpx.Response(403)

    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": True}}
        ))
        respx.get(REFS_API).mock(side_effect=_refs_handler)
        result = await fetch_repo_info(_repos(), pat="ghp_classic")

    assert result[1]["can_push"] is True
    assert refs_called is False  # classic PAT never probes refs


@pytest.mark.asyncio
async def test_pat_403_repo_not_in_scope():
    """Fine-grained PAT: initial /repos call returns 403 (private repo outside scope) → can_push=False."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(403))
        result = await fetch_repo_info(_repos(), pat="github_pat_AAAA")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is None


# ---------------------------------------------------------------------------
# PAT — 404 (private repo token can't see, or doesn't exist)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pat_404():
    """404 → can_push=False, is_public=None."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(404))
        result = await fetch_repo_info(_repos(), pat="ghp_noaccess")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is None


# ---------------------------------------------------------------------------
# PAT — 401 invalid, retries unauthenticated for visibility
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pat_401_retries_unauthenticated_public():
    """401 → retry unauthenticated; public repo gets is_public=True, can_push=False."""
    call_count = 0

    def _handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"private": False})

    with respx.mock:
        respx.get(REPO_API).mock(side_effect=_handler)
        result = await fetch_repo_info(_repos(), pat="ghp_expired")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is True
    assert call_count == 2


@pytest.mark.asyncio
async def test_pat_401_retry_also_fails():
    """401 + unauthenticated retry also fails → is_public=None, can_push=False."""
    call_count = 0

    def _handler(request):
        nonlocal call_count
        call_count += 1
        return httpx.Response(401)

    with respx.mock:
        respx.get(REPO_API).mock(side_effect=_handler)
        result = await fetch_repo_info(_repos(), pat="ghp_expired")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is None


# ---------------------------------------------------------------------------
# GitHub App IAT — push confirmed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_iat_push_true():
    """App IAT with push=True → can_push=True."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": True}}
        ))
        result = await fetch_repo_info(_repos(), pat="ghs_apptoken")

    assert result[1]["can_push"] is True
    assert result[1]["is_public"] is True


# ---------------------------------------------------------------------------
# GitHub App IAT — push=False or absent → no write access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_iat_push_false():
    """App IAT with push=False → can_push=None (indeterminate, not False).

    permissions.push is unreliable for App IATs — actual write access is
    determined by installation permissions. False must not trigger the badge.
    """
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False, "permissions": {"push": False}}
        ))
        result = await fetch_repo_info(_repos(), pat="ghs_apptoken")

    assert result[1]["can_push"] is None


@pytest.mark.asyncio
async def test_app_iat_no_permissions_object():
    """App IAT, no permissions object → can_push=None (indeterminate, not False)."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(
            200, json={"private": False}
        ))
        result = await fetch_repo_info(_repos(), pat="ghs_apptoken")

    assert result[1]["can_push"] is None


# ---------------------------------------------------------------------------
# App IAT — 404 (App not installed on org)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_app_iat_404_not_installed():
    """App IAT 404 (org not installed) → can_push=False."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(404))
        result = await fetch_repo_info(_repos(), pat="ghs_apptoken")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is None


# ---------------------------------------------------------------------------
# Rate-limit / 5xx → no write access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limit_returns_false():
    """429 rate-limit → can_push=False (assume no access, token is present)."""
    with respx.mock:
        respx.get(REPO_API).mock(return_value=httpx.Response(429))
        result = await fetch_repo_info(_repos(), pat="ghp_test")

    assert result[1]["can_push"] is False


# ---------------------------------------------------------------------------
# Network exception → no write access
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_exception_returns_false():
    """Network exception → can_push=False, no crash."""
    with respx.mock:
        respx.get(REPO_API).mock(side_effect=httpx.ConnectError("connection refused"))
        result = await fetch_repo_info(_repos(), pat="ghp_test")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is None


# ---------------------------------------------------------------------------
# Non-GitHub URL — slug extraction fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_github_url_skipped():
    """Non-GitHub URLs make no API call and return can_push=False (no verified access)."""
    repos = [_FakeRepo(1, "https://gitlab.com/myorg/myrepo")]
    with respx.mock:
        result = await fetch_repo_info(repos, pat="ghp_test")

    assert result[1]["can_push"] is False
    assert result[1]["is_public"] is None


# ---------------------------------------------------------------------------
# Multiple repos — results keyed by repo id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multiple_repos_keyed_by_id():
    """Results dict is keyed by repo.id; each repo gets its own result."""
    repos = [
        _FakeRepo(10, "https://github.com/org/repo-a"),
        _FakeRepo(20, "https://github.com/org/repo-b"),
    ]
    with respx.mock:
        respx.get("https://api.github.com/repos/org/repo-a").mock(
            return_value=httpx.Response(200, json={"private": False, "permissions": {"push": True}})
        )
        respx.get("https://api.github.com/repos/org/repo-b").mock(
            return_value=httpx.Response(200, json={"private": True, "permissions": {"push": False}})
        )
        result = await fetch_repo_info(repos, pat="ghp_test")

    assert result[10]["can_push"] is True
    assert result[10]["is_public"] is True
    assert result[20]["can_push"] is False
    assert result[20]["is_public"] is False

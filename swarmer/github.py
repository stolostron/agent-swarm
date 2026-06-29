"""GitHub API helpers used by the sessions router.

Kept in a standalone module (no FastAPI / SQLAlchemy imports) so the logic
can be unit-tested without standing up the full application stack.
"""

import asyncio
import base64
import logging
import re
from urllib.parse import urlparse

import httpx

from swarmer.github_url_validator import GitHubURLError, validate_github_url

log = logging.getLogger(__name__)


def github_slug(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, or None if not a GitHub URL.

    Handles both HTTPS (https://github.com/owner/repo) and SSH
    (git@github.com:owner/repo) formats.  Only the exact host ``github.com``
    (or ``www.github.com``) is accepted to prevent false matches on hosts like
    ``notgithub.com``.

    Raises :class:`~swarmer.github_url_validator.GitHubURLError` if the URL
    contains an embedded authentication token.
    """
    try:
        validate_github_url(url)
    except GitHubURLError as exc:
        log.warning("Rejected GitHub URL with embedded token: %s", exc.redacted_url)
        raise

    # SSH format: git@github.com:owner/repo[.git]
    ssh_match = re.match(r"^git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?$", url)
    if ssh_match:
        return ssh_match.group("slug")

    # HTTPS format: https://github.com/owner/repo[.git]
    parsed = urlparse(url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    m = re.match(r"^/(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$", parsed.path)
    return m.group("slug") if m else None


async def fetch_repo_info(repos: list, pat: str | None) -> dict:
    """Return per-repo visibility and push-access info via the GitHub API.

    Rules (caller is responsible for selecting the right token):
      - No token (pat=None): do nothing — return all-None for every repo.
      - Token present: determine public/private and whether the token can push.

    Token-scope caveat for fine-grained PATs (github_pat_...):
      GitHub's permissions.push field reflects the *user's* collaborator status,
      not whether the token's repository scope includes the repo.  A fine-grained
      PAT held by an admin will show push=True on a public repo even if that repo
      is not listed in the token's allowed repositories.  To detect this we make a
      second call to GET /repos/{slug}/git/refs which requires actual token-level
      repo access — a 403 there means the token cannot access the repo regardless
      of what permissions.push says.

    Result shape: {repo_id: {"is_public": bool|None, "can_push": bool|None}}
      is_public: True=public, False=private, None=could not determine
      can_push:  True=confirmed write access, False=no write access, None=skipped (no token)
    """
    # No credential — nothing to check.
    if not pat:
        return {r.id: {"is_public": None, "can_push": None} for r in repos}

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {pat}",
    }
    _is_fine_grained = pat.startswith("github_pat_")
    _is_app_token = pat.startswith("ghs_")

    async def _check(client: httpx.AsyncClient, repo) -> tuple[int, dict]:
        slug = github_slug(repo.repo_url)
        if not slug:
            return repo.id, {"is_public": None, "can_push": False}
        try:
            r = await client.get(f"https://api.github.com/repos/{slug}", headers=headers)

            if r.status_code == 200:
                data = r.json()
                is_public = not data.get("private", True)
                perms = data.get("permissions", {})
                push_from_perms = bool(perms.get("push"))

                if not push_from_perms:
                    if _is_app_token:
                        # App IAT: permissions.push=False is unreliable — the App's actual
                        # write access is determined by its installation permissions, not
                        # this field. Treat as indeterminate so no false "No write access".
                        return repo.id, {"is_public": is_public, "can_push": None}
                    # PAT: push=False is definitive.
                    return repo.id, {"is_public": is_public, "can_push": False}

                if _is_fine_grained:
                    # permissions.push reflects user collaborator status, not token scope.
                    # Probe with a token-scoped read endpoint to confirm actual access.
                    r2 = await client.get(
                        f"https://api.github.com/repos/{slug}/git/refs",
                        headers=headers,
                    )
                    if r2.status_code == 403:
                        # Token does not include this repo in its scope.
                        log.debug(
                            "fetch_repo_info: fine-grained PAT excluded from %s (refs 403)", slug
                        )
                        return repo.id, {"is_public": is_public, "can_push": False}

                return repo.id, {"is_public": is_public, "can_push": True}

            if r.status_code == 401:
                # Token invalid/expired — retry unauthenticated for public/private visibility.
                log.warning("fetch_repo_info: 401 for %s — retrying unauthenticated", slug)
                r2 = await client.get(
                    f"https://api.github.com/repos/{slug}",
                    headers={"Accept": "application/vnd.github+json"},
                )
                is_public = not r2.json().get("private", True) if r2.status_code == 200 else None
                return repo.id, {"is_public": is_public, "can_push": False}

            # 404, 403, or anything else → no write access.
            return repo.id, {"is_public": None, "can_push": False}

        except Exception:
            return repo.id, {"is_public": None, "can_push": False}

    async with httpx.AsyncClient(timeout=5) as client:
        results = await asyncio.gather(*[_check(client, r) for r in repos])
    result_dict = dict(results)
    log.debug("fetch_repo_info: fine_grained=%s results=%s", _is_fine_grained, result_dict)
    return result_dict


async def list_repos_for_pat(pat) -> list[dict] | str:
    """Fetch all repos accessible via a GitHubPAT, paginated up to 500.

    If pat.github_org is set, tries GET /orgs/{org}/repos first. If that returns
    404 (i.e. the name is a personal account, not an org), falls back to
    GET /users/{org}/repos. Otherwise lists the authenticated user's repos via
    GET /user/repos.

    Returns a list of repo dicts (keys: full_name, private, updated_at, description)
    or a string error message on failure.

    The ``pat`` argument must expose:
      - pat.pat       (str)  — the raw token value
      - pat.github_org (str) — org/username name or empty string
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {pat.pat}",
    }
    if pat.github_org:
        url: str | None = f"https://api.github.com/orgs/{pat.github_org}/repos"
        params: dict = {"per_page": 100, "sort": "updated"}
    else:
        url = "https://api.github.com/user/repos"
        params = {"per_page": 100, "sort": "updated", "affiliation": "owner,collaborator"}

    repos: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            while url and len(repos) < 500:
                r = await client.get(url, headers=headers, params=params)
                params = {}  # pagination: subsequent URLs already carry params
                if r.status_code == 404 and pat.github_org and url.startswith(
                    f"https://api.github.com/orgs/{pat.github_org}/repos"
                ):
                    # The name is a personal account, not an org — retry as user
                    url = f"https://api.github.com/users/{pat.github_org}/repos"
                    params = {"per_page": 100, "sort": "updated"}
                    continue
                if r.status_code != 200:
                    ct = r.headers.get("content-type", "")
                    msg = (
                        r.json().get("message", "unknown error")
                        if ct.startswith("application/json")
                        else r.text
                    )
                    return f"GitHub API error {r.status_code}: {msg}"
                repos.extend(r.json())
                # Follow Link header for next page
                next_url: str | None = None
                for part in r.headers.get("link", "").split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                url = next_url
    except Exception as exc:
        return f"Failed to contact GitHub API: {exc}"

    return repos


async def list_repos_for_github_app(token: str) -> list[dict] | str:
    """Fetch all repos accessible to a GitHub App installation via an IAT.

    Uses GET /installation/repositories (paginated, up to 500).
    Returns same shape as list_repos_for_pat: list of dicts with keys
    full_name, private, updated_at, description.
    Returns a string error message on failure.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url: str | None = "https://api.github.com/installation/repositories"
    params: dict = {"per_page": 100}
    repos: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            while url and len(repos) < 500:
                r = await client.get(url, headers=headers, params=params)
                params = {}
                if r.status_code != 200:
                    ct = r.headers.get("content-type", "")
                    msg = (
                        r.json().get("message", "unknown error")
                        if ct.startswith("application/json")
                        else r.text
                    )
                    return f"GitHub API error {r.status_code}: {msg}"
                data = r.json()
                repos.extend(data.get("repositories", []))
                next_url: str | None = None
                for part in r.headers.get("link", "").split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                url = next_url
    except Exception as exc:
        return f"Failed to contact GitHub API: {exc}"

    return repos


async def list_folder_contents(
    owner: str, repo: str, path: str, branch: str, pat: str | None
) -> list[dict] | str:
    """List contents of a folder in a GitHub repo.

    Returns a list of dicts with keys: name, path, type ('file' or 'dir'), size, sha.
    Returns a string error message on failure.
    Uses the GitHub Contents API: GET /repos/{owner}/{repo}/contents/{path}?ref={branch}
    """
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"token {pat}"

    clean_path = path.strip("/")
    if clean_path == ".":
        clean_path = ""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{clean_path}"
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers=headers, params={"ref": branch})
            if r.status_code != 200:
                ct = r.headers.get("content-type", "")
                msg = (
                    r.json().get("message", "unknown error")
                    if ct.startswith("application/json")
                    else r.text
                )
                return f"GitHub API error {r.status_code}: {msg}"
            return r.json()
    except Exception as exc:
        return f"Failed to contact GitHub API: {exc}"


async def fetch_folder_prompts(
    owner: str, repo: str, folder_path: str, branch: str, pat: str | None
) -> list[dict] | str:
    """Recursively fetch all .md files from a folder and sub-folders.

    Uses the Git Trees API (GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1)
    for efficient recursive listing, then fetches content for each .md file
    via the Contents API.

    Returns a list of dicts: {filename: str, content: str, sha: str}
    or a string error message on failure.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"token {pat}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Resolve branch to a SHA (required for Trees API)
            r = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}",
                headers=headers,
            )
            if r.status_code != 200:
                return f"Failed to resolve branch {branch}: {r.status_code}"
            
            data = r.json()
            if not isinstance(data, dict) or "commit" not in data or "sha" not in data["commit"]:
                return f"Failed to resolve branch {branch}: unexpected response"
                
            head_sha = data["commit"]["sha"]

            # 2. Get recursive tree
            r = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/trees/{head_sha}?recursive=1",
                headers=headers,
            )
            if r.status_code != 200:
                return f"Failed to fetch tree: {r.status_code}"
            
            tree_resp = r.json()
            if tree_resp.get("truncated"):
                return "Repository tree too large (truncated by GitHub). Use a more specific folder path."

            tree_data = tree_resp.get("tree", [])
            prefix = folder_path.strip("/")
            if prefix == ".":
                prefix = ""
            if prefix and not prefix.endswith("/"):
                prefix += "/"

            # Filter for .md files inside the folder_path
            md_files = [
                item for item in tree_data
                if item["type"] == "blob"
                and item["path"].endswith(".md")
                and (not prefix or item["path"].startswith(prefix))
            ]

            if len(md_files) > 100:
                return f"Too many .md files ({len(md_files)}). Use a more specific folder path (max 100)."

            results = []
            for item in md_files:
                # Fetch content for each file
                # Use contents API to get base64 encoded content
                r = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo}/contents/{item['path']}?ref={head_sha}",
                    headers=headers,
                )
                if r.status_code == 200:
                    data = r.json()
                    content_b64 = data.get("content", "")
                    try:
                        content = base64.b64decode(content_b64).decode("utf-8")
                    except Exception:
                        content = "(Error decoding content)"
                    
                    # Store filename relative to folder_path
                    rel_path = item["path"]
                    if prefix and rel_path.startswith(prefix):
                        rel_path = rel_path[len(prefix):]
                    
                    results.append({
                        "filename": rel_path,
                        "content": content,
                        "sha": item["sha"]
                    })
            
            return results

    except Exception as exc:
        return f"Failed to fetch prompts: {exc}"

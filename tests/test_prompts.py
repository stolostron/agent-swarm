"""Unit tests for prompt-related GitHub API helpers in swarmer.github.

Uses respx to mock httpx calls.
"""

import pytest
import respx
import httpx
import base64

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.github import list_folder_contents, fetch_folder_prompts  # noqa: E402

@pytest.mark.asyncio
async def test_list_folder_contents_success():
    """Successfully lists contents of a folder."""
    owner, repo, path, branch = "octocat", "hello-world", "prompts", "main"
    pat = "ghp_test"
    
    mock_response = [
        {"name": "cve-analysis.md", "path": "prompts/cve-analysis.md", "type": "file", "size": 100, "sha": "sha1"},
        {"name": "subfolder", "path": "prompts/subfolder", "type": "dir", "size": 0, "sha": "sha2"},
    ]

    with respx.mock:
        respx.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}").mock(
            return_value=httpx.Response(200, json=mock_response)
        )
        result = await list_folder_contents(owner, repo, path, branch, pat)

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["name"] == "cve-analysis.md"
    assert result[1]["type"] == "dir"

@pytest.mark.asyncio
async def test_list_folder_contents_error():
    """Returns error string on API failure."""
    owner, repo, path, branch = "octocat", "hello-world", "missing", "main"
    
    with respx.mock:
        respx.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        result = await list_folder_contents(owner, repo, path, branch, None)

    assert isinstance(result, str)
    assert "404" in result
    assert "Not Found" in result

@pytest.mark.asyncio
async def test_fetch_folder_prompts_recursive():
    """Recursively fetches .md files from a folder using Git Trees API."""
    owner, repo, folder_path, branch = "octocat", "hello-world", "prompts", "main"
    pat = "ghp_test"
    
    # 1. Mock branch resolution to get HEAD SHA
    respx.get(f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}").mock(
        return_value=httpx.Response(200, json={"commit": {"sha": "head_sha"}})
    )
    
    # 2. Mock recursive tree fetch
    mock_tree = {
        "tree": [
            {"path": "prompts/cve-analysis.md", "type": "blob", "sha": "sha1"},
            {"path": "prompts/sub/deep.md", "type": "blob", "sha": "sha2"},
            {"path": "prompts/README.txt", "type": "blob", "sha": "sha3"}, # Ignored (not .md)
            {"path": "other/ignore.md", "type": "blob", "sha": "sha4"},    # Ignored (wrong path)
        ]
    }
    respx.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/head_sha?recursive=1").mock(
        return_value=httpx.Response(200, json=mock_tree)
    )
    
    # 3. Mock content fetch for each .md file
    content1 = base64.b64encode(b"Content 1").decode()
    respx.get(f"https://api.github.com/repos/{owner}/{repo}/contents/prompts/cve-analysis.md?ref=head_sha").mock(
        return_value=httpx.Response(200, json={"content": content1, "encoding": "base64"})
    )
    content2 = base64.b64encode(b"Content 2").decode()
    respx.get(f"https://api.github.com/repos/{owner}/{repo}/contents/prompts/sub/deep.md?ref=head_sha").mock(
        return_value=httpx.Response(200, json={"content": content2, "encoding": "base64"})
    )

    with respx.mock:
        result = await fetch_folder_prompts(owner, repo, folder_path, branch, pat)

    assert isinstance(result, list)
    assert len(result) == 2
    paths = [r["filename"] for r in result]
    assert "cve-analysis.md" in paths
    assert "sub/deep.md" in paths
    assert result[0]["content"] == "Content 1"

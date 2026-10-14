"""Output-capture checks for the mcp_setup print-only display snippet.

Regression (CodeQL clear-text logging of sensitive information): the bearer
token must never reach printed output. The display snippet is built from
non-secret inputs with a literal placeholder, while the written opencode.json
still receives the real token.
"""

import json
import os
import secrets
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import mcp_setup


def _snippet_from_output(output: str) -> dict:
    block = output.split("opencode.json configuration snippet:")[1]
    block = block.split("\nRun 'make mcp-setup'")[0]
    return json.loads(block)


def _run_print_only(monkeypatch, capsys, *, token: str | None, insecure: bool = False) -> str:
    argv = ["mcp_setup.py", "--print-only", "--url", "https://swarmer.example.com"]
    if token is not None:
        argv += ["--token", token]
    if insecure:
        argv += ["--insecure"]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delenv("AGENT_SWARM_API_TOKEN", raising=False)
    monkeypatch.setattr(mcp_setup, "_auto_detect_token", lambda: "")
    mcp_setup.main()
    return capsys.readouterr().out


def test_print_only_snippet_never_contains_bearer_token(monkeypatch, capsys):
    token = "tok_" + secrets.token_hex(16)
    output = _run_print_only(monkeypatch, capsys, token=token)

    assert token not in output
    assert "<YOUR_TOKEN>" in output

    server = _snippet_from_output(output)["mcp"]["servers"]["agent-swarm"]
    assert server["type"] == "local"
    assert server["command"] == ["agent-swarm-mcp-server"]
    assert server["environment"]["AGENT_SWARM_API_URL"] == "https://swarmer.example.com"
    assert server["environment"]["AGENT_SWARM_VERIFY_SSL"] == "true"
    assert server["environment"]["AGENT_SWARM_API_TOKEN"] == "<YOUR_TOKEN>"


def test_print_only_snippet_omits_token_entry_when_unconfigured(monkeypatch, capsys):
    output = _run_print_only(monkeypatch, capsys, token=None)

    assert "(No active token found)" in output
    assert "<YOUR_TOKEN>" not in output

    environment = _snippet_from_output(output)["mcp"]["servers"]["agent-swarm"]["environment"]
    assert "AGENT_SWARM_API_TOKEN" not in environment


def test_write_path_still_persists_real_token(tmp_path, monkeypatch, capsys):
    """The redaction must be display-only: the written opencode.json keeps
    the real token so the MCP server can authenticate."""
    token = "tok_" + secrets.token_hex(16)
    config = tmp_path / "opencode.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mcp_setup.py",
            "--token", token,
            "--url", "https://swarmer.example.com",
            "--config", str(config),
        ],
    )
    monkeypatch.setattr(mcp_setup, "_auto_detect_token", lambda: "")

    mcp_setup.main()

    written = json.loads(config.read_text())
    assert written["mcp"]["servers"]["agent-swarm"]["environment"]["AGENT_SWARM_API_TOKEN"] == token
    # The console summary never echoes the token either.
    assert token not in capsys.readouterr().out

"""Unit tests for build_main_cmd() in OpenCodeStrategy.

Verifies that prompt mode runs a clean direct invocation (no --continue, no ||
fallback), server mode returns the correct serve subcommand, and TUI mode
returns 'sleep infinity'.  No K8s or DB dependencies required.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.agent_tools.opencode import OpenCodeStrategy  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal stub for Session domain object
# ---------------------------------------------------------------------------

class _FakeSession:
    """Lightweight stand-in for the Session ORM model."""
    def __init__(self, *, mode: str, instruction_prompt: str = ""):
        self.mode = mode
        self.instruction_prompt = instruction_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_opencode = OpenCodeStrategy()


# ---------------------------------------------------------------------------
# OpenCode: prompt mode
# ---------------------------------------------------------------------------

def test_opencode_prompt_no_continue_flag():
    session = _FakeSession(mode="prompt")
    cmd = _opencode.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert "--continue" not in cmd


def test_opencode_prompt_no_fallback_shell():
    session = _FakeSession(mode="prompt")
    cmd = _opencode.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert "||" not in cmd


def test_opencode_prompt_with_prompt_text():
    session = _FakeSession(mode="prompt", instruction_prompt="Fix the bug")
    cmd = _opencode.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert "--continue" not in cmd
    assert "||" not in cmd
    assert "Fix the bug" in cmd


def test_opencode_prompt_resolved_prompt_overrides_instruction():
    session = _FakeSession(mode="prompt", instruction_prompt="session prompt")
    cmd = _opencode.build_main_cmd(
        session,
        model="google-vertex-anthropic/claude-sonnet-5@default",
        resolved_prompt="resolved prompt",
    )
    assert "resolved prompt" in cmd
    assert "session prompt" not in cmd
    assert "--continue" not in cmd


def test_opencode_prompt_is_single_command():
    """Result must be a single invocation, not a chain."""
    session = _FakeSession(mode="prompt", instruction_prompt="do something")
    cmd = _opencode.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert cmd.startswith("opencode")
    # No shell operators that indicate a fallback chain
    assert "||" not in cmd
    assert "&&" not in cmd


# ---------------------------------------------------------------------------
# OpenCode: server mode
# ---------------------------------------------------------------------------

def test_opencode_server_mode():
    session = _FakeSession(mode="server")
    cmd = _opencode.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert cmd.startswith("opencode serve")
    assert "--continue" not in cmd
    assert "||" not in cmd


# ---------------------------------------------------------------------------
# OpenCode: TUI mode
# ---------------------------------------------------------------------------

def test_opencode_tui_mode():
    session = _FakeSession(mode="tui")
    cmd = _opencode.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert cmd == "sleep infinity"

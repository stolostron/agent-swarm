"""Unit tests for build_main_cmd() in OpenCodeStrategy.

Verifies that prompt mode runs a clean direct invocation (no --continue, no ||
fallback) with a private V2 server (--standalone), and that both interactive
modes (server + TUI) return the same persistent shared-service command.
No K8s or DB dependencies required.
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

# The shared V2 service command used for both interactive modes.
_SERVE_CMD = "opencode serve --service --hostname 0.0.0.0 --port 4096"


# ---------------------------------------------------------------------------
# OpenCode: prompt mode
# ---------------------------------------------------------------------------

def test_opencode_prompt_no_continue_flag():
    session = _FakeSession(mode="prompt")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert "--continue" not in cmd


def test_opencode_prompt_no_fallback_shell():
    session = _FakeSession(mode="prompt")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert "||" not in cmd


def test_opencode_prompt_runs_standalone():
    """One-shot prompt runs get a private V2 server, not a lingering daemon."""
    session = _FakeSession(mode="prompt", instruction_prompt="Fix the bug")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert cmd.startswith("opencode run --standalone")


def test_opencode_prompt_with_prompt_text():
    session = _FakeSession(mode="prompt", instruction_prompt="Fix the bug")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert "--continue" not in cmd
    assert "||" not in cmd
    assert "Fix the bug" in cmd


def test_opencode_prompt_resolved_prompt_overrides_instruction():
    session = _FakeSession(mode="prompt", instruction_prompt="session prompt")
    cmd = _opencode.build_main_cmd(
        session,
        model="google-vertex/claude-sonnet-5@default",
        resolved_prompt="resolved prompt",
    )
    assert "resolved prompt" in cmd
    assert "session prompt" not in cmd
    assert "--continue" not in cmd


def test_opencode_prompt_explicit_blank_override_does_not_use_saved_prompt():
    session = _FakeSession(mode="prompt", instruction_prompt="saved prompt")
    cmd = _opencode.build_main_cmd(
        session,
        model="google-vertex/claude-sonnet-5@default",
        resolved_prompt="",
    )
    assert cmd == "opencode run --standalone --model google-vertex/claude-sonnet-5@default"
    assert "saved prompt" not in cmd


def test_opencode_prompt_legacy_vertex_id_normalized():
    """Legacy V1 google-vertex-anthropic model IDs are normalized for V2."""
    session = _FakeSession(mode="prompt", instruction_prompt="hi")
    cmd = _opencode.build_main_cmd(
        session,
        model="google-vertex-anthropic/claude-sonnet-5@default",
    )
    assert "--model google-vertex/claude-sonnet-5@default" in cmd
    assert "google-vertex-anthropic" not in cmd


def test_opencode_prompt_is_single_command():
    """Result must be a single invocation, not a chain."""
    session = _FakeSession(mode="prompt", instruction_prompt="do something")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert cmd.startswith("opencode")
    # No shell operators that indicate a fallback chain
    assert "||" not in cmd
    assert "&&" not in cmd


# ---------------------------------------------------------------------------
# OpenCode: server mode
# ---------------------------------------------------------------------------

def test_opencode_server_mode():
    session = _FakeSession(mode="server")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert cmd == _SERVE_CMD
    assert "--continue" not in cmd
    assert "||" not in cmd


# ---------------------------------------------------------------------------
# OpenCode: TUI mode
# ---------------------------------------------------------------------------

def test_opencode_tui_mode():
    """TUI mode runs the same persistent shared service as server mode.

    OpenCode V2 unification: the TUI client launched by the WebSocket handler
    discovers the service registration and attaches to this server, so a
    dropped connection reconnects to the same live session.
    """
    session = _FakeSession(mode="tui")
    cmd = _opencode.build_main_cmd(session, model="google-vertex/claude-sonnet-5@default")
    assert cmd == _SERVE_CMD

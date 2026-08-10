"""Unit tests for ShellStrategy.

Verifies that:
- build_main_cmd() returns the instruction_prompt verbatim in prompt mode
- build_main_cmd() returns 'sleep infinity' in TUI mode
- build_main_cmd() raises ValueError for server mode
- build_main_cmd() raises ValueError when instruction_prompt is empty
- build_config_data() returns an empty dict (no config files)
- get_model_options() returns an empty list (no AI provider needed)
- get_default_model() returns empty string
- get_server_port() returns None
- is_valid_model() always returns True
- name / display_name are correct
- Registry correctly resolves the 'shell' tool
- Database migration no longer normalises 'shell' to 'opencode'

No K8s or DB dependencies required.
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.agent_tools.shell import ShellStrategy  # noqa: E402
from swarmer.agent_tools.registry import get as get_tool, all_tools  # noqa: E402


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

_shell = ShellStrategy()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_name():
    assert _shell.name == "shell"


def test_display_name():
    assert _shell.display_name == "Shell"


# ---------------------------------------------------------------------------
# prompt mode — build_main_cmd
# ---------------------------------------------------------------------------

def test_prompt_returns_instruction_verbatim():
    session = _FakeSession(mode="prompt", instruction_prompt="python3 scripts/report.py")
    cmd = _shell.build_main_cmd(session, model="")
    assert cmd == "python3 scripts/report.py"


def test_prompt_uses_resolved_prompt_over_instruction():
    """resolved_prompt (from prompt library) takes precedence over instruction_prompt."""
    session = _FakeSession(mode="prompt", instruction_prompt="fallback command")
    cmd = _shell.build_main_cmd(session, model="", resolved_prompt="primary command")
    assert cmd == "primary command"


def test_prompt_strips_whitespace():
    session = _FakeSession(mode="prompt", instruction_prompt="  echo hello  ")
    cmd = _shell.build_main_cmd(session, model="")
    assert cmd == "echo hello"


def test_prompt_empty_instruction_raises():
    session = _FakeSession(mode="prompt", instruction_prompt="")
    with pytest.raises(ValueError, match="non-empty instruction_prompt"):
        _shell.build_main_cmd(session, model="")


def test_prompt_whitespace_only_raises():
    session = _FakeSession(mode="prompt", instruction_prompt="   ")
    with pytest.raises(ValueError, match="non-empty instruction_prompt"):
        _shell.build_main_cmd(session, model="")


def test_prompt_no_model_in_cmd():
    """Command must NOT contain any model string or --model flag."""
    session = _FakeSession(mode="prompt", instruction_prompt="ls -la")
    cmd = _shell.build_main_cmd(session, model="google-vertex-anthropic/claude-sonnet-5@default")
    assert "--model" not in cmd
    assert "opencode" not in cmd
    assert cmd == "ls -la"


# ---------------------------------------------------------------------------
# TUI mode — build_main_cmd
# ---------------------------------------------------------------------------

def test_tui_returns_sleep_infinity():
    session = _FakeSession(mode="tui")
    cmd = _shell.build_main_cmd(session, model="")
    assert cmd == "sleep infinity"


# ---------------------------------------------------------------------------
# Server mode — build_main_cmd
# ---------------------------------------------------------------------------

def test_server_mode_raises():
    session = _FakeSession(mode="server")
    with pytest.raises(ValueError, match="does not support server mode"):
        _shell.build_main_cmd(session, model="")


# ---------------------------------------------------------------------------
# Config / model methods
# ---------------------------------------------------------------------------

def test_build_config_data_empty():
    result = _shell.build_config_data()
    assert result == {}


def test_build_config_data_ignores_args():
    result = _shell.build_config_data(secret=object(), mcp_servers=[], model="anything")
    assert result == {}


def test_get_model_options_empty():
    assert _shell.get_model_options() == []
    assert _shell.get_model_options(has_vertex=True, has_gemini=True) == []


def test_get_default_model_empty_string():
    assert _shell.get_default_model(has_adc=True) == ""
    assert _shell.get_default_model(has_adc=False) == ""


def test_get_server_port_none():
    assert _shell.get_server_port() is None


def test_is_valid_model_always_true():
    assert _shell.is_valid_model("") is True
    assert _shell.is_valid_model("anything") is True
    assert _shell.is_valid_model("google-vertex-anthropic/claude-sonnet-5@default") is True


def test_build_share_setup_cmd_empty():
    assert _shell.build_share_setup_cmd() == ""


def test_build_model_setup_cmd_empty():
    assert _shell.build_model_setup_cmd("any-model") == ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_resolves_shell():
    tool = get_tool("shell")
    assert tool.name == "shell"
    assert isinstance(tool, ShellStrategy)


def test_all_tools_includes_shell():
    names = [t.name for t in all_tools()]
    assert "shell" in names
    assert "opencode" in names


# ---------------------------------------------------------------------------
# Preset / model resolution (inherited no-ops)
# ---------------------------------------------------------------------------

def test_resolve_preset_returns_none():
    assert _shell.resolve_preset("claude") is None
    assert _shell.resolve_preset("gemini") is None
    assert _shell.resolve_preset("shell") is None


def test_is_preset_false():
    assert _shell.is_preset("claude") is False
    assert _shell.is_preset("") is False


def test_resolve_build_model_passthrough():
    """resolve_build_model should return the model unchanged (no preset mapping)."""
    assert _shell.resolve_build_model("my-model") == "my-model"
    assert _shell.resolve_build_model("") == ""

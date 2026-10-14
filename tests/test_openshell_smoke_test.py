"""Focused checks for model validation in the OpenShell smoke test."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.openshell_smoke_test import _config_model_matches
from swarmer.agent_tools.opencode import OpenCodeStrategy


def test_smoke_model_validation_resolves_preset_before_comparing():
    tool = OpenCodeStrategy()
    resolved_model = tool.resolve_build_model("claude")

    assert _config_model_matches(tool, "claude", {"model": resolved_model})
    assert not _config_model_matches(tool, "claude", {"model": "claude"})


def test_smoke_model_validation_uses_normalized_legacy_model():
    tool = OpenCodeStrategy()
    model = "google-vertex-anthropic/claude-sonnet-5@default"

    assert _config_model_matches(
        tool,
        model,
        {"model": "google-vertex/claude-sonnet-5@default"},
    )

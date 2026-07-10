"""Unit tests for the Claude/Gemini model presets feature (ACM-37232).

Covers:
  - OpenCodeStrategy.resolve_preset() maps preset names to configured model IDs
  - OpenCodeStrategy.resolve_build_model() resolves presets to a concrete BUILD
    model, and passes through raw model IDs unchanged
  - OpenCodeStrategy.is_valid_model() accepts preset names
  - OpenCodeStrategy.get_default_model() returns preset names
  - OpenCodeStrategy.get_model_options() always lists both presets with an
    "available" flag + human-readable "reason", instead of omitting options
    when a provider isn't configured
  - OpenCodeStrategy.get_preset_options() filters to just the preset entries
  - OpenCodeStrategy.build_config_data() resolves a preset to build/small/plan
    models, derives enabled_providers correctly, and emits the agent.plan.model
    stanza only when OPENCODE_EXPERIMENTAL_PLAN_MODE is enabled

No K8s or DB dependencies required — these are pure unit tests against the
OpenCodeStrategy class and the Settings singleton.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.agent_tools.opencode import OpenCodeStrategy  # noqa: E402
from swarmer.config import settings  # noqa: E402

_opencode = OpenCodeStrategy()


# ---------------------------------------------------------------------------
# resolve_preset() / resolve_build_model() / is_preset()
# ---------------------------------------------------------------------------

class TestResolvePreset:
    def test_resolve_claude_preset(self):
        mapping = _opencode.resolve_preset("claude")
        assert mapping == {
            "plan": settings.claude_preset_plan_model,
            "build": settings.claude_preset_build_model,
            "small": settings.claude_preset_small_model,
        }

    def test_resolve_gemini_preset(self):
        mapping = _opencode.resolve_preset("gemini")
        assert mapping == {
            "plan": settings.gemini_preset_plan_model,
            "build": settings.gemini_preset_build_model,
            "small": settings.gemini_preset_small_model,
        }

    def test_resolve_unknown_preset_returns_none(self):
        assert _opencode.resolve_preset("google/gemini-3.5-flash") is None
        assert _opencode.resolve_preset("") is None
        assert _opencode.resolve_preset("opus") is None

    def test_is_preset(self):
        assert _opencode.is_preset("claude") is True
        assert _opencode.is_preset("gemini") is True
        assert _opencode.is_preset("google-vertex-anthropic/claude-sonnet-5@default") is False


class TestResolveBuildModel:
    def test_preset_resolves_to_build_model(self):
        assert _opencode.resolve_build_model("claude") == settings.claude_preset_build_model
        assert _opencode.resolve_build_model("gemini") == settings.gemini_preset_build_model

    def test_raw_model_passes_through_unchanged(self):
        raw = "google-vertex-anthropic/claude-opus-4-6@default"
        assert _opencode.resolve_build_model(raw) == raw

    def test_empty_string_passes_through(self):
        assert _opencode.resolve_build_model("") == ""


# ---------------------------------------------------------------------------
# is_valid_model() / get_default_model()
# ---------------------------------------------------------------------------

class TestIsValidModel:
    def test_preset_names_are_valid(self):
        assert _opencode.is_valid_model("claude") is True
        assert _opencode.is_valid_model("gemini") is True

    def test_raw_model_ids_are_valid(self):
        assert _opencode.is_valid_model("google-vertex-anthropic/claude-sonnet-5@default") is True
        assert _opencode.is_valid_model("google/gemini-3.5-flash") is True

    def test_garbage_is_invalid(self):
        assert _opencode.is_valid_model("not-a-model") is False
        assert _opencode.is_valid_model("") is False


class TestGetDefaultModel:
    def test_has_adc_true_returns_claude_preset(self):
        assert _opencode.get_default_model(True) == "claude"

    def test_has_adc_false_returns_gemini_preset(self):
        assert _opencode.get_default_model(False) == "gemini"


# ---------------------------------------------------------------------------
# get_model_options() / get_preset_options() — always-listed + error states
# ---------------------------------------------------------------------------

class TestGetModelOptions:
    def test_presets_always_present_regardless_of_availability(self):
        """Both presets are listed even when neither provider is configured —
        an unavailable provider must surface as a visible error, not vanish."""
        options = _opencode.get_model_options(has_vertex=False, has_gemini=False)
        presets = [o for o in options if o["type"] == "preset"]
        assert {p["value"] for p in presets} == {"claude", "gemini"}
        for p in presets:
            assert p["available"] is False
            assert p["reason"]  # non-empty human-readable reason

    def test_presets_marked_available_when_provider_configured(self):
        options = _opencode.get_model_options(has_vertex=True, has_gemini=True)
        presets = {o["value"]: o for o in options if o["type"] == "preset"}
        assert presets["claude"]["available"] is True
        assert presets["claude"]["reason"] == ""
        assert presets["gemini"]["available"] is True
        assert presets["gemini"]["reason"] == ""

    def test_partial_availability(self):
        options = _opencode.get_model_options(has_vertex=True, has_gemini=False)
        presets = {o["value"]: o for o in options if o["type"] == "preset"}
        assert presets["claude"]["available"] is True
        assert presets["gemini"]["available"] is False
        assert "Google AI Studio" in presets["gemini"]["reason"]

    def test_only_presets_are_returned(self):
        """The Advanced individual-model picker has been removed — only the
        two family-level presets are ever listed."""
        options = _opencode.get_model_options(has_vertex=True, has_gemini=True)
        assert len(options) == 2
        assert all(o["type"] == "preset" for o in options)

    def test_has_gemini_from_legacy_secret_fallback(self):
        """Callers that only pass the OpencodeSecret row (not an explicit
        has_gemini bool) still detect Gemini availability via the encrypted
        key column, pending the ACM-37263 provider migration."""
        class _FakeSecret:
            google_api_key_enc = "encrypted-value"

        options = _opencode.get_model_options(secret=_FakeSecret(), has_vertex=False)
        gemini_preset = next(o for o in options if o["value"] == "gemini")
        assert gemini_preset["available"] is True

    def test_explicit_has_gemini_overrides_missing_secret(self):
        options = _opencode.get_model_options(secret=None, has_vertex=False, has_gemini=True)
        gemini_preset = next(o for o in options if o["value"] == "gemini")
        assert gemini_preset["available"] is True

    def test_get_preset_options_filters_to_presets_only(self):
        presets = _opencode.get_preset_options(has_vertex=True, has_gemini=False)
        assert len(presets) == 2
        assert all(p["type"] == "preset" for p in presets)


# ---------------------------------------------------------------------------
# build_config_data() — preset resolution, enabled_providers, plan mode
# ---------------------------------------------------------------------------

class TestBuildConfigDataPresets:
    def test_claude_preset_resolves_all_three_roles(self, monkeypatch):
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="claude")
        config = json.loads(data["opencode.json"])
        assert config["model"] == settings.claude_preset_build_model
        assert config["small_model"] == settings.claude_preset_small_model
        assert config["agent"]["plan"]["model"] == settings.claude_preset_plan_model

    def test_gemini_preset_resolves_all_three_roles(self, monkeypatch):
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="gemini")
        config = json.loads(data["opencode.json"])
        assert config["model"] == settings.gemini_preset_build_model
        assert config["small_model"] == settings.gemini_preset_small_model
        assert config["agent"]["plan"]["model"] == settings.gemini_preset_plan_model

    def test_plan_stanza_omitted_when_plan_mode_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", False)
        data = _opencode.build_config_data(model="claude")
        config = json.loads(data["opencode.json"])
        assert "agent" not in config
        # model/small_model are still resolved from the preset regardless.
        assert config["model"] == settings.claude_preset_build_model

    def test_claude_preset_enables_vertex_anthropic_provider(self, monkeypatch):
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="claude")
        config = json.loads(data["opencode.json"])
        assert "google-vertex-anthropic" in config["enabled_providers"]
        assert "google" in config["enabled_providers"]

    def test_gemini_preset_does_not_enable_vertex_anthropic(self, monkeypatch):
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="gemini")
        config = json.loads(data["opencode.json"])
        assert config["enabled_providers"] == ["google"]

    def test_non_preset_model_unaffected(self):
        """A raw provider/model string (e.g. from a session created before
        presets existed) keeps the pre-existing pro->flash / opus-sonnet->haiku
        small-model derivation logic."""
        data = _opencode.build_config_data(model="google-vertex-anthropic/claude-sonnet-5@default")
        config = json.loads(data["opencode.json"])
        assert config["model"] == "google-vertex-anthropic/claude-sonnet-5@default"
        assert config["small_model"] == "google-vertex-anthropic/claude-haiku-4-5@20251001"
        assert "agent" not in config

    def test_empty_model_falls_back_to_gemini_default(self):
        data = _opencode.build_config_data(model="")
        config = json.loads(data["opencode.json"])
        assert config["model"] == "google/gemini-3.1-pro-preview"
        assert config["enabled_providers"] == ["google"]

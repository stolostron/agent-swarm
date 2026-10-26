"""Unit tests for the Claude/Gemini/OpenAI/EnMaaS model presets feature (ACM-37232, ACM-48198).

Covers:
  - OpenCodeStrategy.resolve_preset() maps preset names to configured model IDs
  - OpenCodeStrategy.resolve_build_model() resolves presets to a concrete BUILD
    model, and passes through raw model IDs unchanged
  - OpenCodeStrategy.is_valid_model() accepts preset names
  - OpenCodeStrategy.get_default_model() returns preset names
  - OpenCodeStrategy.get_model_options() always lists all presets with an
    "available" flag + human-readable "reason", instead of omitting options
    when a provider isn't configured
  - OpenCodeStrategy.get_preset_options() filters to just the preset entries
  - OpenCodeStrategy.build_config_data() resolves a preset to build/small/plan
    models, derives enabled_providers correctly, and emits the agent.plan.model
    stanza only when OPENCODE_EXPERIMENTAL_PLAN_MODE is enabled
  - The EnMaaS preset (ACM-48198): all roles stay within EnMaaS, generated
    config defines providers.enmaas natively, only enmaas is enabled, and the
    workspace-supplied gateway URL is validated/normalized

No K8s or DB dependencies required — these are pure unit tests against the
OpenCodeStrategy class and the Settings singleton.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.agent_tools.opencode import OpenCodeStrategy, normalize_model_id  # noqa: E402
from swarmer.config import settings  # noqa: E402

_opencode = OpenCodeStrategy()


# ---------------------------------------------------------------------------
# resolve_preset() / resolve_build_model() / is_preset()
# ---------------------------------------------------------------------------

class TestResolvePreset:
    def test_resolve_claude_preset(self):
        mapping = _opencode.resolve_preset("claude")
        assert mapping == {
            "plan": normalize_model_id(settings.claude_preset_plan_model),
            "build": normalize_model_id(settings.claude_preset_build_model),
            "small": normalize_model_id(settings.claude_preset_small_model),
        }

    def test_resolve_gemini_preset(self):
        mapping = _opencode.resolve_preset("gemini")
        assert mapping == {
            "plan": settings.gemini_preset_plan_model,
            "build": settings.gemini_preset_build_model,
            "small": settings.gemini_preset_small_model,
        }

    def test_resolve_openai_preset(self):
        mapping = _opencode.resolve_preset("openai")
        assert mapping == {
            "plan": settings.openai_preset_plan_model,
            "build": settings.openai_preset_build_model,
            "small": settings.openai_preset_small_model,
        }

    def test_openai_preset_defaults(self):
        assert _opencode.resolve_preset("openai") == {
            "plan": "openai/gpt-6.1-terra",
            "build": "openai/gpt-6-luna",
            "small": "openai/gpt-6-luna",
        }

    def test_resolve_enmaas_preset(self):
        """Resolve EnMaaS plan, build, and small roles from configuration."""
        mapping = _opencode.resolve_preset("enmaas")
        assert mapping == {
            "plan": settings.enmaas_preset_plan_model,
            "build": settings.enmaas_preset_build_model,
            "small": settings.enmaas_preset_small_model,
        }

    def test_enmaas_preset_defaults_all_roles_within_enmaas(self):
        """ACM-48198 cost-safety: every default EnMaaS role must be an
        enmaas/ model — no role may silently resolve to a paid provider."""
        mapping = _opencode.resolve_preset("enmaas")
        for role, model in mapping.items():
            assert model.startswith("enmaas/"), f"{role} role {model!r} is not EnMaaS"
        assert mapping == {
            "plan": "enmaas/rits/zai-org/glm-5-3",
            "build": "enmaas/rits/zai-org/glm-5-3",
            "small": "enmaas/rits/zai-org/glm-5-3",
        }

    def test_claude_preset_defaults_use_v2_vertex_provider(self):
        """OpenCode V2 uses the canonical google-vertex provider ID."""
        assert _opencode.resolve_preset("claude") == {
            "plan": "google-vertex/claude-opus-4-6@default",
            "build": "google-vertex/claude-sonnet-5@default",
            "small": "google-vertex/claude-haiku-4-5@20251001",
        }

    def test_resolve_unknown_preset_returns_none(self):
        assert _opencode.resolve_preset("google/gemini-3.5-flash") is None
        assert _opencode.resolve_preset("") is None
        assert _opencode.resolve_preset("opus") is None

    def test_is_preset(self):
        """Recognize preset labels while rejecting a raw model identifier."""
        assert _opencode.is_preset("claude") is True
        assert _opencode.is_preset("gemini") is True
        assert _opencode.is_preset("openai") is True
        assert _opencode.is_preset("enmaas") is True
        assert _opencode.is_preset("google-vertex/claude-sonnet-5@default") is False


class TestResolveBuildModel:
    def test_preset_resolves_to_build_model(self):
        """Resolve each provider preset to its configured build model."""
        assert _opencode.resolve_build_model("claude") == normalize_model_id(settings.claude_preset_build_model)
        assert _opencode.resolve_build_model("gemini") == settings.gemini_preset_build_model
        assert _opencode.resolve_build_model("openai") == settings.openai_preset_build_model

    def test_enmaas_preset_resolves_to_build_model_with_embedded_slashes(self):
        """The upstream EnMaaS model ID contains slashes — the reference must
        split at the FIRST "/" only, so the full ID survives resolution."""
        assert _opencode.resolve_build_model("enmaas") == "enmaas/rits/zai-org/glm-5-3"
        assert _opencode.resolve_build_model("enmaas/rits/zai-org/glm-5-3") == "enmaas/rits/zai-org/glm-5-3"

    def test_raw_model_passes_through_unchanged(self):
        raw = "google-vertex/claude-opus-4-6@default"
        assert _opencode.resolve_build_model(raw) == raw

    def test_legacy_vertex_model_is_normalized(self):
        """V1 google-vertex-anthropic IDs are normalized to the V2 canonical ID."""
        assert (
            _opencode.resolve_build_model("google-vertex-anthropic/claude-opus-4-6@default")
            == "google-vertex/claude-opus-4-6@default"
        )

    def test_empty_string_passes_through(self):
        assert _opencode.resolve_build_model("") == ""


# ---------------------------------------------------------------------------
# is_valid_model() / get_default_model()
# ---------------------------------------------------------------------------

class TestIsValidModel:
    def test_preset_names_are_valid(self):
        """Accept every supported family-level provider preset."""
        assert _opencode.is_valid_model("claude") is True
        assert _opencode.is_valid_model("gemini") is True
        assert _opencode.is_valid_model("openai") is True
        assert _opencode.is_valid_model("enmaas") is True

    def test_raw_model_ids_are_valid(self):
        """Accept raw model identifiers for supported provider families."""
        assert _opencode.is_valid_model("google-vertex-anthropic/claude-sonnet-5@default") is True
        assert _opencode.is_valid_model("google/gemini-3.5-flash") is True
        assert _opencode.is_valid_model("openai/gpt-5.3-codex@default") is True

    def test_raw_enmaas_model_with_embedded_slashes_is_valid(self):
        """Accept an EnMaaS model ID whose upstream name contains slashes."""
        assert _opencode.is_valid_model("enmaas/rits/zai-org/glm-5-3") is True

    def test_garbage_is_invalid(self):
        assert _opencode.is_valid_model("not-a-model") is False
        assert _opencode.is_valid_model("") is False


class TestGetDefaultModel:
    def test_has_adc_true_returns_claude_preset(self):
        assert _opencode.get_default_model(True) == "claude"

    def test_has_adc_false_returns_gemini_preset(self):
        """Choose Gemini as the default when ADC is not configured."""
        assert _opencode.get_default_model(False) == "gemini"

    def test_openai_is_selected_when_gemini_is_unavailable(self):
        """Select OpenAI when Gemini is unavailable and OpenAI is configured."""
        assert _opencode.get_default_model(False, has_gemini=False, has_openai=True) == "openai"

    def test_enmaas_selected_only_in_enmaas_only_workspace(self):
        """An EnMaaS-only workspace (no ADC/gemini/openai) selects EnMaaS;
        mixed workspaces keep their existing defaults (ACM-48198)."""
        assert (
            _opencode.get_default_model(False, has_gemini=False, has_openai=False, has_enmaas=True)
            == "enmaas"
        )
        # Existing defaults are preserved when other providers are available.
        assert _opencode.get_default_model(False, has_gemini=True, has_enmaas=True) == "gemini"
        assert _opencode.get_default_model(False, has_gemini=False, has_openai=True, has_enmaas=True) == "openai"

    def test_no_provider_returns_empty_selection(self):
        """Leave the default unset when no supported provider is available."""
        assert _opencode.get_default_model(False, has_gemini=False, has_openai=False) == ""
        assert (
            _opencode.get_default_model(False, has_gemini=False, has_openai=False, has_enmaas=False)
            == ""
        )


# ---------------------------------------------------------------------------
# get_model_options() / get_preset_options() — always-listed + error states
# ---------------------------------------------------------------------------

class TestGetModelOptions:
    def test_presets_always_present_regardless_of_availability(self):
        """All presets are listed even when no provider is configured —
        an unavailable provider must surface as a visible error, not vanish."""
        options = _opencode.get_model_options(
            has_vertex=False,
            has_gemini=False,
            has_openai=False,
            has_enmaas=False,
        )
        presets = [o for o in options if o["type"] == "preset"]
        assert {p["value"] for p in presets} == {"claude", "gemini", "openai", "enmaas"}
        for p in presets:
            assert p["available"] is False
            assert p["reason"]  # non-empty human-readable reason

    def test_presets_marked_available_when_provider_configured(self):
        """Mark each preset available when its gateway credential is present."""
        options = _opencode.get_model_options(
            has_vertex=True, has_gemini=True, has_openai=True, has_enmaas=True,
            enmaas_base_url=_ENMAAS_TEST_BASE_URL,
        )
        presets = {o["value"]: o for o in options if o["type"] == "preset"}
        assert presets["claude"]["available"] is True
        assert presets["claude"]["reason"] == ""
        assert presets["gemini"]["available"] is True
        assert presets["gemini"]["reason"] == ""
        assert presets["openai"]["available"] is True
        assert presets["openai"]["reason"] == ""
        assert presets["enmaas"]["available"] is True
        assert presets["enmaas"]["reason"] == ""

    def test_partial_availability(self):
        """Show each provider's independent availability and setup guidance."""
        options = _opencode.get_model_options(
            has_vertex=True, has_gemini=False, has_openai=False, has_enmaas=False
        )
        presets = {o["value"]: o for o in options if o["type"] == "preset"}
        assert presets["claude"]["available"] is True
        assert presets["gemini"]["available"] is False
        assert "Google AI Studio" in presets["gemini"]["reason"]
        assert presets["openai"]["available"] is False
        assert "OpenAI" in presets["openai"]["reason"]
        assert presets["enmaas"]["available"] is False

    def test_only_presets_are_returned(self):
        """The Advanced individual-model picker has been removed — only
        family-level presets are listed."""
        options = _opencode.get_model_options(
            has_vertex=True, has_gemini=True, has_openai=True, has_enmaas=True
        )
        assert len(options) == 4
        assert all(o["type"] == "preset" for o in options)

    def test_secret_alone_does_not_grant_gemini_availability(self):
        """Since ACM-37263, the Gemini key lives only on the OpenShell gateway
        provider — passing an OpencodeSecret row with a stale encrypted key
        column (e.g. a workspace that hasn't rotated its key since the
        migration) must NOT make Gemini available. Callers must explicitly
        pass has_gemini sourced from a provider_exists() check, exactly like
        has_vertex."""
        class _FakeSecret:
            google_api_key_enc = "encrypted-value"

        options = _opencode.get_model_options(
            secret=_FakeSecret(),
            has_vertex=False,
            has_gemini=False,
            has_openai=False,
        )
        gemini_preset = next(o for o in options if o["value"] == "gemini")
        assert gemini_preset["available"] is False

    def test_explicit_has_gemini_overrides_missing_secret(self):
        """Trust gateway availability even when no database secret row exists."""
        options = _opencode.get_model_options(secret=None, has_vertex=False, has_gemini=True, has_openai=False)
        gemini_preset = next(o for o in options if o["value"] == "gemini")
        assert gemini_preset["available"] is True

    def test_explicit_has_openai_overrides_missing_secret(self):
        """Trust gateway availability even when no database secret row exists."""
        options = _opencode.get_model_options(secret=None, has_vertex=False, has_gemini=False, has_openai=True)
        openai_preset = next(o for o in options if o["value"] == "openai")
        assert openai_preset["available"] is True

    def test_get_preset_options_filters_to_presets_only(self):
        """Return the complete preset list without non-preset model options."""
        presets = _opencode.get_preset_options(
            has_vertex=True, has_gemini=False, has_openai=False, has_enmaas=False
        )
        assert len(presets) == 4
        assert all(p["type"] == "preset" for p in presets)


# ---------------------------------------------------------------------------
# build_config_data() — preset resolution, enabled_providers, plan mode
# ---------------------------------------------------------------------------

class TestBuildConfigDataPresets:
    def test_claude_preset_resolves_all_three_roles(self, monkeypatch):
        """Render build, title, and plan models from the Claude preset."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="claude")
        config = json.loads(data["opencode.json"])
        assert config["model"] == normalize_model_id(settings.claude_preset_build_model)
        # V2 native shape: the V1 small_model becomes agents.title.model.
        assert config["agents"]["title"]["model"] == normalize_model_id(settings.claude_preset_small_model)
        assert config["agents"]["plan"]["model"] == normalize_model_id(settings.claude_preset_plan_model)

    def test_gemini_preset_resolves_all_three_roles(self, monkeypatch):
        """Render build, title, and plan models from the Gemini preset."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="gemini")
        config = json.loads(data["opencode.json"])
        assert config["model"] == settings.gemini_preset_build_model
        assert config["agents"]["title"]["model"] == settings.gemini_preset_small_model
        assert config["agents"]["plan"]["model"] == settings.gemini_preset_plan_model

    def test_plan_stanza_omitted_when_plan_mode_disabled(self, monkeypatch):
        """Omit plan-agent config when experimental plan mode is disabled."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", False)
        data = _opencode.build_config_data(model="claude")
        config = json.loads(data["opencode.json"])
        assert "plan" not in config["agents"]
        # model/title are still resolved from the preset regardless.
        assert config["model"] == normalize_model_id(settings.claude_preset_build_model)

    def test_claude_preset_enables_vertex_provider(self, monkeypatch):
        """Enable Vertex model access for the Claude preset configuration."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="claude")
        config = json.loads(data["opencode.json"])
        # V2 canonical provider ID (V1 google-vertex-anthropic is rejected by V2).
        assert "google-vertex" in config["enabled_providers"]
        assert "google" in config["enabled_providers"]

    def test_gemini_preset_does_not_enable_vertex(self, monkeypatch):
        """Avoid enabling Vertex credentials for a Gemini-only preset."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="gemini")
        config = json.loads(data["opencode.json"])
        assert config["enabled_providers"] == ["google"]

    def test_openai_preset_resolves_and_sets_plan_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Configure OpenAI role models and the expected agent modes."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="openai")
        config = json.loads(data["opencode.json"])
        assert config["model"] == settings.openai_preset_build_model
        assert config["agents"]["title"]["model"] == settings.openai_preset_small_model
        assert config["enabled_providers"] == ["openai"]
        assert config["agents"]["plan"]["model"] == settings.openai_preset_plan_model
        assert config["agents"]["plan"]["mode"] == "primary"
        assert config["agents"]["build"]["mode"] == "primary"

    def test_non_preset_model_unaffected(self):
        """A raw provider/model string (e.g. from a session created before
        presets existed) keeps the pre-existing pro->flash / opus-sonnet->haiku
        small-model derivation logic."""
        data = _opencode.build_config_data(model="google-vertex/claude-sonnet-5@default")
        config = json.loads(data["opencode.json"])
        assert config["model"] == "google-vertex/claude-sonnet-5@default"
        assert config["agents"]["title"]["model"] == "google-vertex/claude-haiku-4-5@20251001"
        assert "plan" not in config["agents"]

    def test_legacy_vertex_model_normalized_in_config(self):
        """Legacy V1 IDs stored on pre-upgrade sessions are normalized."""
        data = _opencode.build_config_data(model="google-vertex-anthropic/claude-sonnet-5@default")
        config = json.loads(data["opencode.json"])
        assert config["model"] == "google-vertex/claude-sonnet-5@default"
        assert config["agents"]["title"]["model"] == "google-vertex/claude-haiku-4-5@20251001"
        assert "google-vertex-anthropic" not in data["opencode.json"]

    def test_empty_model_falls_back_to_gemini_default(self):
        """Use the configured Gemini default when no model was selected."""
        data = _opencode.build_config_data(model="")
        config = json.loads(data["opencode.json"])
        assert config["model"] == "google/gemini-3.8-flash"
        assert config["enabled_providers"] == ["google"]

    def test_raw_openai_model_enables_only_openai_provider(self):
        """Enable OpenAI for a raw model without enabling other providers."""
        data = _opencode.build_config_data(model="openai/gpt-5.3-codex@default")
        config = json.loads(data["opencode.json"])
        assert config["agents"]["title"]["model"] == settings.openai_preset_small_model
        assert config["enabled_providers"] == ["openai"]


# ---------------------------------------------------------------------------
# EnMaaS preset (ACM-48198) — config generation, provider enablement, URL config
# ---------------------------------------------------------------------------

_ENMAAS_TEST_BASE_URL = "https://enmaas-gateway.example.com/v1"


class TestEnmaasModelOptions:
    def test_enmaas_unavailable_reason_when_workspace_url_unset(self):
        """Without a workspace gateway URL the preset is unavailable."""
        options = _opencode.get_model_options(has_enmaas=True, enmaas_base_url="")
        enmaas = next(o for o in options if o["value"] == "enmaas")
        assert enmaas["available"] is False
        assert "gateway" in enmaas["reason"]
        assert "Tokens" in enmaas["reason"]

    def test_enmaas_invalid_workspace_url_degrades_to_unavailable(self):
        """An invalid endpoint degrades to not-configured, never a 500."""
        options = _opencode.get_model_options(
            has_enmaas=True, enmaas_base_url="http://invalid.example/v1"
        )
        enmaas = next(o for o in options if o["value"] == "enmaas")
        assert enmaas["available"] is False
        assert "gateway" in enmaas["reason"]
        assert "Tokens" in enmaas["reason"]

    def test_enmaas_unavailable_reason_when_key_missing(self):
        """Explain that a configured EnMaaS endpoint still needs a key."""
        options = _opencode.get_model_options(
            has_enmaas=False, enmaas_base_url=_ENMAAS_TEST_BASE_URL
        )
        enmaas = next(o for o in options if o["value"] == "enmaas")
        assert enmaas["available"] is False
        assert "EnMaaS key not set" in enmaas["reason"]

    def test_enmaas_available_when_workspace_url_and_key_present(self):
        """Expose the EnMaaS preset when its key and gateway URL are available."""
        options = _opencode.get_model_options(
            has_enmaas=True, enmaas_base_url=_ENMAAS_TEST_BASE_URL
        )
        enmaas = next(o for o in options if o["value"] == "enmaas")
        assert enmaas["available"] is True
        assert enmaas["reason"] == ""
        assert enmaas["label"] == "GLM-5.3 (EnMaaS)"


class TestEnmaasConfigGeneration:
    def test_enmaas_pconfig_all_roles_enmaas_and_only_enmaas_enabled(self, monkeypatch):
        """Keep all EnMaaS roles on the zero-cost provider with no paid fallback."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="enmaas", enmaas_base_url=_ENMAAS_TEST_BASE_URL)
        config = json.loads(data["opencode.json"])
        assert config["model"] == "enmaas/rits/zai-org/glm-5-3"
        assert config["agents"]["title"]["model"] == "enmaas/rits/zai-org/glm-5-3"
        assert config["agents"]["plan"]["model"] == "enmaas/rits/zai-org/glm-5-3"
        # Only enmaas is enabled — EnMaaS sessions must not inherit the
        # unconditional Google enablement (no paid model selectable).
        assert config["enabled_providers"] == ["enmaas"]
        # No mode overrides are invented for EnMaaS (unlike the OpenAI preset).
        assert "mode" not in config["agents"].get("plan", {})
        assert "build" not in config["agents"]

    def test_enmaas_pconfig_defines_native_provider(self, monkeypatch):
        """Emit OpenCode V2 model metadata for the native EnMaaS provider."""
        monkeypatch.setattr(settings, "opencode_experimental_plan_mode", True)
        data = _opencode.build_config_data(model="enmaas", enmaas_base_url=_ENMAAS_TEST_BASE_URL)
        config = json.loads(data["opencode.json"])
        provider = config["providers"]["enmaas"]
        # OpenCode V2 uses package/settings (not the legacy npm/options keys).
        assert provider["name"] == "EnMaaS"
        assert provider["package"] == "@opencode/ai/providers/openai-compatible"
        assert provider["settings"] == {"baseURL": _ENMAAS_TEST_BASE_URL}
        assert "npm" not in provider
        assert "options" not in provider
        assert provider["env"] == ["ENMAAS_KEY"]
        # Explicit GLM-5.3 model entry; embedded slashes in the model ID are
        # preserved (split at the FIRST "/" only).
        model = provider["models"]["rits/zai-org/glm-5-3"]
        assert model["name"] == "GLM 5.3"
        assert model["modalities"] == {"input": ["text"], "output": ["text"]}
        assert model["limit"] == {"context": 1_000_000, "output": 65_536}
        assert model["cost"] == {"input": 0, "output": 0}

    def test_workspace_supplied_url_drives_generated_config(self):
        """The caller-resolved workspace URL is the generated baseURL."""

        class _FakeSecret:
            enmaas_base_url = "https://workspace-gateway.example.net/v1"

        data = _opencode.build_config_data(
            secret=_FakeSecret(),
            model="enmaas",
            enmaas_base_url="https://workspace-gateway.example.net/v1",
        )
        config = json.loads(data["opencode.json"])
        assert (
            config["providers"]["enmaas"]["settings"]["baseURL"]
            == "https://workspace-gateway.example.net/v1"
        )

    def test_enmaas_pconfig_contains_no_secret_values(self):
        """Keep the EnMaaS key value out of generated OpenCode configuration."""
        data = _opencode.build_config_data(model="enmaas", enmaas_base_url=_ENMAAS_TEST_BASE_URL)
        raw = data["opencode.json"]
        assert "ENMAAS_KEY" in raw  # env var NAME only
        # The key VALUE never appears — only the env var reference.
        assert raw.count("ENMAAS_KEY") == 1
        assert "api_key" not in raw.lower().replace("enmaas_key", "")
        assert "bearer" not in raw.lower()

    def test_raw_enmaas_model_uses_enmaas_housekeeping_model(self):
        """Keep the housekeeping model and enabled provider within EnMaaS."""
        data = _opencode.build_config_data(
            model="enmaas/rits/zai-org/glm-5-3", enmaas_base_url=_ENMAAS_TEST_BASE_URL
        )
        config = json.loads(data["opencode.json"])
        assert config["model"] == "enmaas/rits/zai-org/glm-5-3"
        assert config["agents"]["title"]["model"] == settings.enmaas_preset_small_model
        assert config["enabled_providers"] == ["enmaas"]
        assert "enmaas" in config["providers"]

    def test_enmaas_config_raises_without_workspace_url(self):
        """A broken config must never be written without a workspace URL."""
        with pytest.raises(ValueError, match="gateway URL"):
            _opencode.build_config_data(model="enmaas", enmaas_base_url="")

    def test_non_enmaas_configs_have_no_enmaas_provider(self):
        """Exclude EnMaaS provider configuration from other model families."""
        for model in ("claude", "gemini", "openai", ""):
            data = _opencode.build_config_data(model=model)
            config = json.loads(data["opencode.json"])
            assert "providers" not in config, f"unexpected providers block for {model!r}"
            assert "enmaas" not in config["enabled_providers"]


class TestEnmaasBaseUrlValidation:
    def test_unset_url_returns_empty(self):
        """Allow an unset endpoint while rejecting whitespace-only values."""
        from swarmer.config import validate_enmaas_base_url

        assert validate_enmaas_base_url("") == ""
        assert validate_enmaas_base_url("   ") == ""

    def test_valid_url_normalized_trailing_slash_stripped(self):
        """Strip trailing slashes without changing the endpoint path."""
        from swarmer.config import validate_enmaas_base_url

        assert (
            validate_enmaas_base_url("https://enmaas-gateway.example.com/v1/")
            == "https://enmaas-gateway.example.com/v1"
        )
        # The /v1 path is retained verbatim.
        assert (
            validate_enmaas_base_url("https://enmaas-gateway.example.com/v1")
            == "https://enmaas-gateway.example.com/v1"
        )

    def test_custom_port_preserved(self):
        """Preserve an explicit HTTPS port during endpoint normalization."""
        from swarmer.config import validate_enmaas_base_url

        assert (
            validate_enmaas_base_url("https://enmaas-gateway.example.com:8443/v1")
            == "https://enmaas-gateway.example.com:8443/v1"
        )

    def test_http_scheme_rejected(self):
        """Reject non-HTTPS endpoint configuration."""
        from swarmer.config import validate_enmaas_base_url

        with pytest.raises(ValueError, match="https"):
            validate_enmaas_base_url("http://enmaas-gateway.example.com/v1")

    def test_embedded_credentials_rejected(self):
        """Reject user information embedded in the configured URL."""
        from swarmer.config import validate_enmaas_base_url

        with pytest.raises(ValueError, match="credentials"):
            validate_enmaas_base_url("https://user:pass@enmaas-gateway.example.com/v1")

    def test_query_and_fragment_rejected(self):
        """Reject endpoint URLs containing query strings or fragments."""
        from swarmer.config import validate_enmaas_base_url

        with pytest.raises(ValueError, match="query"):
            validate_enmaas_base_url("https://enmaas-gateway.example.com/v1?token=x")
        with pytest.raises(ValueError, match="fragment"):
            validate_enmaas_base_url("https://enmaas-gateway.example.com/v1#sec")

    def test_endpoint_derivation(self):
        """Derive the HTTPS host and default or explicit port from a URL."""
        from swarmer.config import enmaas_endpoint_from_url

        assert enmaas_endpoint_from_url("https://enmaas-gateway.example.com/v1") == (
            "enmaas-gateway.example.com", 443,
        )
        assert enmaas_endpoint_from_url("https://gw.example.com:8443/v1") == (
            "gw.example.com", 8443,
        )
        assert enmaas_endpoint_from_url("") is None
        assert enmaas_endpoint_from_url("http://not-https.example.com/v1") is None

    def test_invalid_configured_url_degrades_to_not_configured(self):
        """Treat invalid configured URLs as absent while normalizing valid ones."""
        from swarmer.config import sanitize_enmaas_base_url

        assert sanitize_enmaas_base_url("http://not-https.example.com/v1") == ""
        assert sanitize_enmaas_base_url("") == ""
        assert (
            sanitize_enmaas_base_url("https://enmaas-gateway.example.com/v1/")
            == "https://enmaas-gateway.example.com/v1"
        )

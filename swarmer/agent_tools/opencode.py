import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings

# Family-level model presets (ACM-37232). Preset names are stored directly in
# Session.provider (e.g. "claude"/"gemini") in place of a raw provider/model@version
# string. resolve_preset() maps a preset name to its {plan, build, small} model
# IDs, sourced from Settings so they can be reconfigured without code changes.
_PRESET_NAMES = ("claude", "gemini")


class OpenCodeStrategy(AgentToolStrategy):

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "OpenCode"

    def get_image(self) -> str:
        return settings.agent_image_opencode

    def resolve_preset(self, preset: str) -> dict[str, str] | None:
        if preset == "claude":
            return {
                "plan": settings.claude_preset_plan_model,
                "build": settings.claude_preset_build_model,
                "small": settings.claude_preset_small_model,
            }
        if preset == "gemini":
            return {
                "plan": settings.gemini_preset_plan_model,
                "build": settings.gemini_preset_build_model,
                "small": settings.gemini_preset_small_model,
            }
        return None

    def build_config_data(self, secret=None, mcp_servers=None, use_inference_local: bool = False, model: str = "") -> dict[str, str]:  # noqa: ARG002 (use_inference_local retained for interface compat)
        preset = self.resolve_preset(model)
        _plan_model = ""
        if preset:
            # Preset selected — plan/build/small all come from the configured mapping.
            _model = preset["build"]
            _small_model = preset["small"]
            _plan_model = preset["plan"]
        else:
            # Raw provider/model string (not a preset) — derive small_model from the
            # chosen model: swap pro→flash / opus/sonnet→haiku within same provider.
            # Fall back to fixed defaults if the model is unrecognised. Kept for
            # backward compatibility with sessions created before presets existed.
            _model = model or "google/gemini-3.1-pro-preview"
            _small_model = "google/gemini-3.5-flash-lite"
            if "/" in _model:
                _provider, _mid = _model.split("/", 1)
                # Strip @version suffix for comparison
                _mid_base = _mid.split("@")[0]
                if _provider == "google-vertex-anthropic":
                    # Claude on Vertex: use haiku as the small model
                    _small_model = "google-vertex-anthropic/claude-haiku-4-5@20251001"
                elif "pro" in _mid_base:
                    _small_model = f"{_provider}/{_mid.replace('pro', 'flash')}"
                elif "flash" in _mid_base:
                    _small_model = _model  # already the small model

        _enabled_providers = ["google"]
        for _candidate in (_model, _small_model, _plan_model):
            if (
                _candidate
                and "/" in _candidate
                and _candidate.split("/")[0] == "google-vertex-anthropic"
                and "google-vertex-anthropic" not in _enabled_providers
            ):
                _enabled_providers.append("google-vertex-anthropic")

        config: dict = {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": _enabled_providers,
            "model": _model,
            "small_model": _small_model,
            "lsp": {
                "go": {"command": ["gopls"], "extensions": []},
                "python": {"command": ["pyright-langserver", "--stdio"], "extensions": []},
            },
            "server": {
                "hostname": "0.0.0.0",
                "port": 4096,
            },
        }

        # Plan mode (ACM-37232): presets define a stronger-reasoning PLAN model.
        # Only takes effect at runtime when OPENCODE_EXPERIMENTAL_PLAN_MODE=true
        # is also set in the sandbox environment (see routers/sessions.py).
        if _plan_model and settings.opencode_experimental_plan_mode:
            config["agent"] = {"plan": {"model": _plan_model}}

        if mcp_servers:
            mcp_config = {}
            for srv in mcp_servers:
                mcp_config[srv.slug] = {
                    "type": "local",
                    "command": ["jira-mcp-server"],
                    "enabled": True,
                    "environment": {
                        "JIRA_SERVER_URL": "{env:JIRA_SERVER_URL}",
                        "JIRA_ACCESS_TOKEN": "{env:JIRA_ACCESS_TOKEN}",
                        "JIRA_EMAIL": "{env:JIRA_EMAIL}",
                    },
                }
            if mcp_config:
                config["mcp"] = mcp_config

        return {
            "opencode.json": json.dumps(config, indent=2),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def get_container_name(self) -> str:
        return "opencode"

    def get_tui_binary(self) -> str:
        return "opencode"

    def get_server_port(self) -> int | None:
        return 4096

    def get_share_dir(self) -> str:
        return "/workspace/.local/share/opencode"

    def build_share_setup_cmd(self) -> str:
        return (
            "mkdir -p /workspace/.opencode /workspace/.local/share && "
            "rm -rf /workspace/.local/share/opencode && "
            "ln -sf /workspace/.opencode /workspace/.local/share/opencode && "
            "find /workspace/.opencode -name '*.db-wal' -o -name '*.db-shm' | xargs rm -f 2>/dev/null; "
        )

    def build_model_setup_cmd(self, model: str) -> str:
        if "/" not in model:
            return ""
        provider_id, model_id = model.split("/", 1)
        model_json = json.dumps({
            "recent": [{"providerID": provider_id, "modelID": model_id}],
            "favorite": [],
            "variant": {f"{provider_id}/{model_id}": "default"},
        })
        return (
            "mkdir -p /workspace/.local/state/opencode && "
            f"printf '%s' {shlex.quote(model_json)} "
            "> /workspace/.local/state/opencode/model.json && "
        )

    def build_main_cmd(self, session, model: str, resolved_prompt: str = "") -> str:
        if session.mode == "server":
            return "opencode serve --hostname 0.0.0.0 --port 4096"
        elif session.mode == "tui":
            return "sleep infinity"
        else:
            prompt_text = resolved_prompt or session.instruction_prompt or ""
            base_parts = ["opencode", "run", "--model", model]
            prompt_parts = [prompt_text] if prompt_text else []
            return " ".join(shlex.quote(p) for p in base_parts + prompt_parts)

    def is_valid_model(self, model: str) -> bool:
        return model in _PRESET_NAMES or model.startswith(("google/", "google-vertex-anthropic/"))

    def get_model_options(self, secret=None, has_vertex: bool = False, has_gemini: bool = False) -> list[dict]:
        # has_gemini can be passed explicitly by callers that already checked the
        # OpenShell gateway/provider; fall back to the legacy DB-encrypted-key
        # check for callers that only have the OpencodeSecret row (ACM-37263 will
        # migrate this to a provider_exists() check like has_vertex).
        _has_gemini = has_gemini or bool(secret and getattr(secret, "google_api_key_enc", ""))

        _vertex_reason = "" if has_vertex else "Vertex AI not configured — add credentials in Secrets."
        _gemini_reason = "" if _has_gemini else "Google AI Studio API key not set — add it in Secrets."

        # Family-level presets (ACM-37232) — the only UX. Always listed, even
        # when the backing provider isn't configured, so missing credentials show
        # up as a visible error in the dropdown instead of silently disappearing.
        return [
            {
                "value": "claude", "label": "Claude", "group": "Presets", "type": "preset",
                "available": has_vertex, "reason": _vertex_reason,
            },
            {
                "value": "gemini", "label": "Gemini", "group": "Presets", "type": "preset",
                "available": _has_gemini, "reason": _gemini_reason,
            },
        ]

    def get_preset_options(self, has_vertex: bool = False, has_gemini: bool = False) -> list[dict]:
        return [
            opt for opt in self.get_model_options(has_vertex=has_vertex, has_gemini=has_gemini)
            if opt.get("type") == "preset"
        ]

    def get_default_model(self, has_adc: bool) -> str:
        if has_adc:
            return "claude"
        return "gemini"

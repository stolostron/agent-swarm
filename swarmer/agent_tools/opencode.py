import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings


class OpenCodeStrategy(AgentToolStrategy):

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "OpenCode"

    def get_image(self) -> str:
        return settings.agent_image_opencode

    def build_config_data(self, secret=None, mcp_servers=None, use_inference_local: bool = False, model: str = "") -> dict[str, str]:  # noqa: ARG002 (use_inference_local retained for interface compat)
        # Derive small_model from the chosen model: swap pro→flash / opus/sonnet→haiku
        # within same provider. Fall back to fixed defaults if the model is unrecognised.
        _model = model or "google/gemini-3.1-pro-preview"
        _small_model = "google/gemini-3.5-flash"
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
        if "/" in _model and _model.split("/")[0] == "google-vertex-anthropic":
            _enabled_providers = ["google", "google-vertex-anthropic"]

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
        return model.startswith(("google/", "google-vertex-anthropic/"))

    def get_model_options(self, secret=None, has_vertex: bool = False) -> list[dict]:
        options = []
        if has_vertex:
            options.extend([
                {"value": "google-vertex-anthropic/claude-opus-4-6@default", "label": "Claude Opus 4.6 (most capable)", "group": "Claude (Vertex AI)"},
                {"value": "google-vertex-anthropic/claude-sonnet-5@default", "label": "Claude Sonnet 5 (balanced)", "group": "Claude (Vertex AI)"},
                {"value": "google-vertex-anthropic/claude-haiku-4-5@20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Claude (Vertex AI)"},
            ])
        if secret and getattr(secret, "google_api_key_enc", ""):
            options.extend([
                {"value": "google/gemini-3.5-flash", "label": "Gemini 3.5 Flash (fast)", "group": "Gemini"},
                {"value": "google/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "group": "Gemini"},
            ])
        return options

    def get_default_model(self, has_adc: bool) -> str:
        if has_adc:
            return "google-vertex-anthropic/claude-sonnet-5@default"
        return "google/gemini-3.1-pro-preview"

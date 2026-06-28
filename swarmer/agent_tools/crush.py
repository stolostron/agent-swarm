import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings


_SMALL_MODEL = "gemini/gemini-3.5-flash"


_SMALL_MODEL_VERTEX = "vertexai/claude-haiku-4-5-20251001"


def _derive_small_model(model: str) -> str | None:
    """Return the small model paired with the given large model, or None if same."""
    if model == _SMALL_MODEL:
        return None  # already the small model — no separate small needed
    if model.startswith("vertexai/claude-"):
        if model == _SMALL_MODEL_VERTEX:
            return None
        return _SMALL_MODEL_VERTEX
    return _SMALL_MODEL


class CrushStrategy(AgentToolStrategy):

    @property
    def name(self) -> str:
        return "crush"

    @property
    def display_name(self) -> str:
        return "Crush"

    def get_image(self) -> str:
        return settings.agent_image_crush

    def require_image(self) -> str:
        """Return the image, raising ValueError if AGENT_IMAGE_CRUSH is not configured.

        Call this at launch time (not at list/render time) so that missing config
        fails fast only when an actual launch is attempted, not on every page render.
        """
        if not settings.agent_image_crush:
            raise ValueError(
                "AGENT_IMAGE_CRUSH is not set. "
                "Set it in .env or as an environment variable to the Crush container image "
                "(e.g. quay.io/jpacker/crush:0.2.1)."
            )
        return settings.agent_image_crush

    def build_config_data(self, secret=None, mcp_servers=None, use_inference_local: bool = False, model: str = "") -> dict[str, str]:
        # Crush requires explicit provider entries in the config — it does not
        # auto-detect from env vars alone.  The value is a map[string]ProviderConfig
        # (keyed by provider ID), NOT an array.  Use $VAR references so values are
        # resolved at runtime from whatever the sandbox environment provides
        # (injected by the OpenShell provider mechanism).
        providers = {
            "gemini": {
                "name": "Google Gemini",
                "type": "gemini",
                "api_key": "$GOOGLE_API_KEY",
            },
        }
        # Add Vertex AI provider if the selected model uses it.
        # The google-cloud OpenShell provider sets GCP_ADC_ACCESS_TOKEN; the GCE metadata
        # emulator (127.0.0.1:8174) exposes it so the vertexai SDK obtains credentials
        # without needing an explicit API key in the config.
        if model.startswith("vertexai/"):
            providers["vertexai"] = {
                "name": "Vertex AI",
                "type": "vertexai",
            }

        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
                "auto_lsp": True,
            },
            "providers": providers,
            "lsp": {
                "go": {"command": "gopls"},
                "python": {"command": "pyright-langserver", "args": ["--stdio"]},
            },
        }

        if mcp_servers:
            mcp_config = {}
            for srv in mcp_servers:
                mcp_config[srv.slug] = {
                    "type": "stdio",
                    "command": "jira-mcp-server",
                    "env": {
                        "JIRA_SERVER_URL": "$JIRA_SERVER_URL",
                        "JIRA_ACCESS_TOKEN": "$JIRA_ACCESS_TOKEN",
                        "JIRA_EMAIL": "$JIRA_EMAIL",
                    },
                }
            if mcp_config:
                config["mcp"] = mcp_config

        return {
            "crush.json": json.dumps(config, indent=2),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def get_container_name(self) -> str:
        return "crush"

    def get_server_port(self) -> int | None:
        return settings.crush_server_port

    def get_share_dir(self) -> str:
        return "$HOME/.local/share/crush"

    def build_share_setup_cmd(self) -> str:
        return (
            "mkdir -p /workspace/.crush $HOME/.local/share && "
            "rm -rf $HOME/.local/share/crush && "
            "ln -sf /workspace/.crush $HOME/.local/share/crush && "
        )

    def build_model_setup_cmd(self, model: str) -> str:
        if not model:
            return ""
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            provider_id, model_id = "", model
        # think: false disables Gemini reasoning/thinking tokens.  The OpenShell
        # egress proxy performs TLS inspection and corrupts the opaque signatures
        # embedded in reasoning blocks, causing "Corrupted thought signature"
        # errors after the first few turns.
        large = {"model": model_id, "provider": provider_id, "think": False}
        models_cfg: dict = {"large": large}

        small = _derive_small_model(model)
        if small:
            sp, sm = small.split("/", 1)
            models_cfg["small"] = {"model": sm, "provider": sp, "think": False}

        config_data = json.dumps({"models": models_cfg})
        return (
            "mkdir -p $HOME/.local/share/crush && "
            f"printf '%s' {shlex.quote(config_data)} "
            "> $HOME/.local/share/crush/crush.json && "
        )

    def build_main_cmd(self, session, model: str, resolved_prompt: str = "") -> str:
        if session.mode == "server":
            port = settings.crush_server_port
            return f"crush server --host tcp://0.0.0.0:{port}"
        elif session.mode == "tui":
            return "sleep infinity"
        else:
            base_parts = ["crush", "run"]
            # Do not pass --model on the CLI — Crush validates it against its
            # built-in catalogue and rejects unknown IDs like gemini-3.1-pro-preview.
            # The model is already written to crush.json by build_model_setup_cmd.
            prompt_text = resolved_prompt or session.instruction_prompt
            prompt_parts = [prompt_text] if prompt_text else []
            return " ".join(shlex.quote(p) for p in base_parts + prompt_parts)

    def is_valid_model(self, model: str) -> bool:
        return model.startswith(("gemini/", "vertexai/"))

    def get_model_options(self, secret=None, has_vertex: bool = False) -> list[dict]:
        options = []
        if has_vertex:
            options.extend([
                {"value": "vertexai/claude-opus-4-6", "label": "Claude Opus 4.6 (most capable)", "group": "Claude (Vertex AI)"},
                {"value": "vertexai/claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "group": "Claude (Vertex AI)"},
                {"value": "vertexai/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Claude (Vertex AI)"},
            ])
        if secret and getattr(secret, "google_api_key_enc", ""):
            options.extend([
                {"value": "gemini/gemini-3.5-flash", "label": "Gemini 3.5 Flash (fast)", "group": "Gemini"},
                {"value": "gemini/gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "group": "Gemini"},
            ])
        return options

    def get_default_model(self, has_adc: bool) -> str:
        if has_adc:
            return "vertexai/claude-sonnet-4-6"
        return "gemini/gemini-3.1-pro-preview"

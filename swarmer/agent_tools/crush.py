import base64
import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _mcp_token_env_var(slug: str) -> str:
    """Derive an env var name from an MCP server slug, e.g. 'atlassian-jira' -> 'MCP_TOKEN_ATLASSIAN_JIRA'."""
    import re
    clean = re.sub(r"[^a-zA-Z0-9]+", "_", slug).strip("_").upper()
    return f"MCP_TOKEN_{clean}"


_HAIKU_BY_PROVIDER = {
    "vertexai": "claude-haiku-4-5-20251001",
    "anthropic": "claude-haiku-3.5",
}


def _derive_small_model(model: str) -> str | None:
    if "/" not in model:
        return None
    provider_id, model_id = model.split("/", 1)

    if provider_id in {"vertexai", "anthropic"} and "opus" in model_id:
        return f"{provider_id}/claude-sonnet-4-6"
    if provider_id in {"vertexai", "anthropic"} and "sonnet" in model_id:
        haiku_id = _HAIKU_BY_PROVIDER.get(provider_id)
        return f"{provider_id}/{haiku_id}" if haiku_id else None
    if provider_id in {"vertexai", "gemini"} and "gemini" in model_id and "pro" in model_id:
        return f"{provider_id}/{model_id.replace('pro', 'flash')}"
    return None


class CrushStrategy(AgentToolStrategy):

    @property
    def name(self) -> str:
        return "crush"

    @property
    def display_name(self) -> str:
        return "Crush"

    def get_image(self) -> str:
        return settings.agent_image_crush

    def get_config_map_name(self) -> str:
        return "crush-config"

    def build_config_data(self, secret=None, mcp_servers=None) -> dict[str, str]:
        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
            },
        }

        if mcp_servers:
            mcp_config = {}
            for srv in mcp_servers:
                env_var_name = _mcp_token_env_var(srv.slug)
                mcp_config[srv.slug] = {
                    "type": srv.server_type or "http",
                    "url": srv.server_url,
                    "headers": {
                        "Authorization": f"Bearer ${env_var_name}",
                    },
                }
            if mcp_config:
                config["mcp"] = mcp_config

        return {
            "crush.json": json.dumps(config, indent=2),
            "gitconfig": "[safe]\n\tdirectory = *\n",
        }

    def get_config_mount_path(self) -> str:
        return "/workspace/.config/crush"

    def get_secret_name(self) -> str:
        return "crush-secret"

    def get_container_name(self) -> str:
        return "crush"

    def get_server_port(self) -> int | None:
        return settings.crush_server_port

    def get_share_dir(self) -> str:
        return "$HOME/.local/share/crush"

    def build_share_setup_cmd(self) -> str:
        crush_version = getattr(settings, "crush_version", "0.57.0")
        return (
            "mkdir -p /workspace/.crush $HOME/.local/share $HOME/.local/bin && "
            "rm -rf $HOME/.local/share/crush && "
            "ln -sf /workspace/.crush $HOME/.local/share/crush && "
            "export PATH=\"$HOME/.local/bin:$PATH\" && "
            "if ! command -v crush >/dev/null 2>&1; then "
            f"echo 'Downloading Crush v{crush_version}...' && "
            f"curl -fsSL 'https://github.com/charmbracelet/crush/releases/download/v{crush_version}"
            f"/crush_{crush_version}_Linux_x86_64.tar.gz' "
            "| tar -xz --strip-components=1 -C $HOME/.local/bin "
            f"crush_{crush_version}_Linux_x86_64/crush && "
            "chmod +x $HOME/.local/bin/crush; "
            "fi && "
        )

    def build_model_setup_cmd(self, model: str) -> str:
        if not model:
            return ""
        if "/" in model:
            provider_id, model_id = model.split("/", 1)
        else:
            provider_id, model_id = "", model
        large = {"model": model_id, "provider": provider_id}
        models_cfg: dict = {"large": large}

        small = _derive_small_model(model)
        if small:
            sp, sm = small.split("/", 1)
            models_cfg["small"] = {"model": sm, "provider": sp}

        config_data = json.dumps({"models": models_cfg})
        return (
            "mkdir -p $HOME/.local/share/crush && "
            f"printf '%s' {shlex.quote(config_data)} "
            "> $HOME/.local/share/crush/crush.json && "
        )

    def build_main_cmd(self, session, model: str) -> str:
        if session.mode == "server":
            port = settings.crush_server_port
            return f"crush server --host tcp://0.0.0.0:{port}"
        elif session.mode == "tui":
            return "sleep infinity"
        else:
            cmd_parts = ["crush", "run"]
            if model:
                cmd_parts.extend(["--model", model])
            if session.resume:
                cmd_parts.append("--continue")
            if session.instruction_prompt:
                cmd_parts.append(session.instruction_prompt)
            return " ".join(shlex.quote(p) for p in cmd_parts)

    def get_server_mode_ports(self) -> list:
        from kubernetes import client
        port = settings.crush_server_port
        return [client.V1ContainerPort(container_port=port, name="crush")]

    def is_valid_model(self, model: str) -> bool:
        return model.startswith(("vertexai/", "anthropic/", "gemini/", "openai/"))

    def get_model_options(self, secret=None) -> list[dict]:
        options = []
        if secret and secret.has_adc:
            options.extend([
                {"value": "vertexai/claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-opus-4-6", "label": "Claude Opus 4.6 (most capable)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/gemini-3-pro", "label": "Gemini 3 Pro", "group": "Vertex AI — Gemini"},
                {"value": "vertexai/gemini-3-flash", "label": "Gemini 3 Flash (fast)", "group": "Vertex AI — Gemini"},
            ])
        elif secret and getattr(secret, "has_vertex", False):
            options.extend([
                {"value": "vertexai/claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (balanced)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-opus-4-6", "label": "Claude Opus 4.6 (most capable)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5 (fast)", "group": "Vertex AI — Claude"},
                {"value": "vertexai/gemini-3-pro", "label": "Gemini 3 Pro", "group": "Vertex AI — Gemini"},
                {"value": "vertexai/gemini-3-flash", "label": "Gemini 3 Flash (fast)", "group": "Vertex AI — Gemini"},
            ])
        if secret and getattr(secret, "anthropic_api_key_enc", ""):
            options.extend([
                {"value": "anthropic/claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "group": "Anthropic (direct)"},
                {"value": "anthropic/claude-opus-4", "label": "Claude Opus 4", "group": "Anthropic (direct)"},
                {"value": "anthropic/claude-haiku-3.5", "label": "Claude Haiku 3.5 (fast)", "group": "Anthropic (direct)"},
            ])
        if secret and getattr(secret, "openai_api_key_enc", ""):
            options.extend([
                {"value": "openai/gpt-4o", "label": "GPT-4o", "group": "OpenAI"},
                {"value": "openai/o3", "label": "o3 (reasoning)", "group": "OpenAI"},
            ])
        if secret and getattr(secret, "google_api_key_enc", ""):
            options.extend([
                {"value": "gemini/gemini-3-flash", "label": "Gemini 2.5 Flash", "group": "Gemini (AI Studio)"},
                {"value": "gemini/gemini-3-pro", "label": "Gemini 2.5 Pro", "group": "Gemini (AI Studio)"},
            ])
        return options

    def get_default_model(self, has_adc: bool, has_gemini: bool) -> str:
        if has_adc:
            return "vertexai/claude-sonnet-4-6"
        if has_gemini:
            return "gemini/gemini-3-flash"
        return ""

    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        pass

    def get_env_from_sources(self) -> list:
        from kubernetes import client
        return [
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name="crush-secret", optional=True
                )
            )
        ]

    def get_extra_env(self, has_adc: bool) -> list:
        from kubernetes import client
        env = []
        if has_adc:
            env.append(client.V1EnvVar(
                name="GOOGLE_APPLICATION_CREDENTIALS",
                value="/app/gcloud/credentials.json",
            ))
        return env

    def get_extra_volumes(self, has_adc: bool) -> list:
        from kubernetes import client
        volumes = []
        if has_adc:
            volumes.append(
                client.V1Volume(
                    name="gcloud-creds",
                    secret=client.V1SecretVolumeSource(
                        secret_name="crush-secret",
                        items=[
                            client.V1KeyToPath(
                                key="application_default_credentials.json",
                                path="credentials.json",
                            )
                        ],
                    ),
                )
            )
        return volumes

    def get_extra_volume_mounts(self, has_adc: bool) -> list:
        from kubernetes import client
        mounts = []
        if has_adc:
            mounts.append(
                client.V1VolumeMount(
                    name="gcloud-creds",
                    mount_path="/app/gcloud",
                    read_only=True,
                )
            )
        return mounts

    def build_k8s_secret_data(self, secret) -> dict[str, str]:
        data = {}
        if secret.google_api_key_enc:
            data["GEMINI_API_KEY"] = _b64(secret.google_api_key)
        if getattr(secret, "anthropic_api_key_enc", "") and secret.anthropic_api_key_enc:
            data["ANTHROPIC_API_KEY"] = _b64(secret.anthropic_api_key)
        if getattr(secret, "openai_api_key_enc", "") and secret.openai_api_key_enc:
            data["OPENAI_API_KEY"] = _b64(secret.openai_api_key)
        if secret.google_cloud_project:
            data["VERTEXAI_PROJECT"] = _b64(secret.google_cloud_project)
        if secret.vertex_location:
            data["VERTEXAI_LOCATION"] = _b64(secret.vertex_location)
        if secret.has_adc:
            data["application_default_credentials.json"] = _b64(
                secret.application_default_credentials
            )
        return data

    def build_mcp_config_cmd(self, mcp_servers) -> str:
        config = {
            "$schema": "https://charm.land/crush.json",
            "options": {
                "disable_metrics": True,
                "disable_notifications": True,
                "data_directory": ".crush",
            },
        }
        if mcp_servers:
            mcp_config = {}
            for srv in mcp_servers:
                env_var_name = _mcp_token_env_var(srv.slug)
                mcp_config[srv.slug] = {
                    "type": srv.server_type or "http",
                    "url": srv.server_url,
                    "headers": {
                        "Authorization": f"Bearer ${env_var_name}",
                    },
                }
            config["mcp"] = mcp_config
        config_json = json.dumps(config)
        config_path = self.get_config_mount_path()
        return (
            f"printf '%s' {shlex.quote(config_json)} "
            f"> {config_path}/crush.json && "
        )

import base64
import json
import shlex

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class OpenCodeStrategy(AgentToolStrategy):

    @property
    def name(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "OpenCode"

    def get_image(self) -> str:
        return settings.agent_image_opencode

    def get_config_map_name(self) -> str:
        return "opencode-config"

    def build_config_data(self, secret=None, mcp_servers=None) -> dict[str, str]:
        config: dict = {
            "$schema": "https://opencode.ai/config.json",
            "disabled_providers": ["opencode"],
            "lsp": True,
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

    def get_config_mount_path(self) -> str:
        return "/workspace/.config/opencode"

    def get_secret_name(self) -> str:
        return "opencode-secret"

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
            "[ -n \"$GOOGLE_API_KEY\" ] && "
            "printf '{\"google\":{\"type\":\"api\",\"key\":\"%s\"}}' \"$GOOGLE_API_KEY\" "
            "> /workspace/.opencode/auth.json; "
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
            cmd_parts = ["opencode", "run", "--model", model]
            if session.resume:
                cmd_parts.append("--continue")
            if prompt_text:
                cmd_parts.append(prompt_text)
            return " ".join(shlex.quote(p) for p in cmd_parts)

    def get_server_mode_ports(self) -> list:
        from kubernetes import client
        return [client.V1ContainerPort(container_port=4096, name="opencode")]

    def is_valid_model(self, model: str) -> bool:
        return model.startswith("google-vertex-anthropic/") or model.startswith("google/")

    def get_model_options(self, secret=None) -> list[dict]:
        _GEMINI_MODELS = [
            ("google/gemini-3.5-flash-preview", "Gemini 3.5 Flash (fast)"),
            ("google/gemini-3.5-pro-preview", "Gemini 3.5 Pro"),
        ]
        _CLAUDE_MODELS = [
            ("google-vertex-anthropic/claude-haiku-4-5@20251001", "Claude Haiku 4.5 (fast)"),
            ("google-vertex-anthropic/claude-sonnet-4-6@default", "Claude Sonnet 4.6 (balanced)"),
            ("google-vertex-anthropic/claude-opus-4-6@default", "Claude Opus 4.6 (most capable)"),
        ]
        options = []
        if secret and getattr(secret, "google_api_key_enc", ""):
            for value, label in _GEMINI_MODELS:
                options.append({"value": value, "label": label, "group": "Gemini"})
        if secret and getattr(secret, "has_adc", False):
            for value, label in _CLAUDE_MODELS:
                options.append({"value": value, "label": label, "group": "Claude (Vertex)"})
        return options

    def get_default_model(self, has_adc: bool, has_gemini: bool) -> str:
        if has_adc:
            return "google-vertex-anthropic/claude-sonnet-4-6@default"
        elif has_gemini:
            return "google/gemini-3.5-flash-preview"
        else:
            return "google/gemini-3.5-flash-preview"

    def exec_model_update(self, pod_name: str, namespace: str, model: str) -> None:
        if "/" not in model:
            return
        from kubernetes import client
        from kubernetes.stream import stream

        provider_id, model_id = model.split("/", 1)
        model_data = {
            "recent": [{"providerID": provider_id, "modelID": model_id}],
            "favorite": [],
            "variant": {f"{provider_id}/{model_id}": "default"},
        }
        model_json = json.dumps(model_data)
        cmd = [
            "sh", "-c",
            "mkdir -p /workspace/.local/state/opencode && "
            f"printf '%s' {shlex.quote(model_json)} > /workspace/.local/state/opencode/model.json",
        ]
        v1 = client.CoreV1Api()
        stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name, namespace,
            command=cmd,
            stderr=True, stdin=False, stdout=True, tty=False,
        )

    def get_env_from_sources(self, secret_name: str = "") -> list:
        from kubernetes import client
        return [
            client.V1EnvFromSource(
                secret_ref=client.V1SecretEnvSource(
                    name=secret_name or "opencode-secret", optional=True
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

    def get_extra_volumes(self, has_adc: bool, secret_name: str = "") -> list:
        from kubernetes import client
        volumes = []
        if has_adc:
            volumes.append(
                client.V1Volume(
                    name="gcloud-creds",
                    secret=client.V1SecretVolumeSource(
                        secret_name=secret_name or "opencode-secret",
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
        data = {
            "GOOGLE_CLOUD_PROJECT": _b64(secret.google_cloud_project),
            "VERTEX_LOCATION": _b64(secret.vertex_location),
            "GOOGLE_API_KEY": _b64(secret.google_api_key),
        }
        if secret.has_adc:
            data["application_default_credentials.json"] = _b64(
                secret.application_default_credentials
            )
        return data

    def build_mcp_config_cmd(self, mcp_servers) -> str:
        config: dict = {
            "$schema": "https://opencode.ai/config.json",
            "disabled_providers": ["opencode"],
            "lsp": True,
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
            config["mcp"] = mcp_config
        config_json = json.dumps(config)
        config_path = self.get_config_mount_path()
        return (
            f"printf '%s' {shlex.quote(config_json)} "
            f"> {config_path}/opencode.json && "
        )

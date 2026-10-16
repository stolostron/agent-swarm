import json
import shlex
from typing import TYPE_CHECKING

from swarmer.agent_tools import AgentToolStrategy
from swarmer.config import settings
from swarmer.mcp_catalog import get_catalog_entry

if TYPE_CHECKING:
    from swarmer.models.mcp_server import McpServer
    from swarmer.models.opencode_secret import OpencodeSecret
    from swarmer.models.session import Session

# Family-level model presets (ACM-37232). Preset names are stored directly in
# Session.provider (e.g. "claude"/"gemini"/"openai"/"enmaas") in place of a raw
# provider/model@version string. resolve_preset() maps a preset name to its
# {plan, build, small} model IDs, sourced from Settings so they can be
# reconfigured without code changes.
_PRESET_NAMES = ("claude", "gemini", "openai", "enmaas")

# EnMaaS model gateway (ACM-48198). The business-provided OpenAI-compatible
# gateway serves GLM-5.3 at zero model cost. The upstream model ID contains
# slashes — model references are always split at the FIRST "/" only, so the
# provider prefix is "enmaas" and the model ID is "rits/zai-org/glm-5-3".
_ENMAAS_PROVIDER = "enmaas"
_ENMAAS_DEFAULT_MODEL_ID = "rits/zai-org/glm-5-3"
_ENMAAS_DEFAULT_MODEL = f"{_ENMAAS_PROVIDER}/{_ENMAAS_DEFAULT_MODEL_ID}"
# Gateway contract: 1,000,000-token context, 65,536-token output, zero cost
# (business-provided access — cost is a gateway property, not config).
_ENMAAS_LIMITS = {"context": 1_000_000, "output": 65_536}

# OpenCode V2 retired the V1 "google-vertex-anthropic" provider ID in favour of
# the canonical "google-vertex" ID (V2 rejects the legacy ID outright). Sessions
# and schedules created before the V2 upgrade may still carry legacy IDs, so
# they are normalized at resolve time instead of requiring a DB migration.
_LEGACY_VERTEX_PREFIX = "google-vertex-anthropic/"


def normalize_model_id(model: str) -> str:
    """Map legacy V1 provider IDs to their V2 canonical equivalents."""
    if model.startswith(_LEGACY_VERTEX_PREFIX):
        return "google-vertex/" + model[len(_LEGACY_VERTEX_PREFIX):]
    return model


def _enmaas_provider_config(model_ids: list[str], base_url: str) -> dict:
    """Build the native OpenCode V2 ``providers.enmaas`` config block.

    *model_ids* is the list of upstream model IDs (the part after the first
    "/" of each reference — embedded slashes are significant and preserved).
    *base_url* is retained for compatibility; the active endpoint is always
    read from operator-managed configuration (ACM-48198).
    OpenCode V2 uses ``package`` for the provider runtime and ``settings``
    for package-specific settings such as ``baseURL``. The ``env`` list is a
    name reference: OpenCode reads the credential from that environment
    variable at runtime (the OpenShell gateway injects it as an opaque
    reference token).
    Raises ``ValueError`` when no gateway URL is configured so a broken
    config is never written to a sandbox.
    """
    if not base_url:
        raise ValueError(
            "EnMaaS gateway URL is not configured by the operator."
        )
    models: dict[str, dict] = {}
    for mid in model_ids:
        models[mid] = {
            # "GLM 5.3" is the display name of the default gateway model;
            # overridden models get a readable name derived from their ID.
            "name": "GLM 5.3" if mid == _ENMAAS_DEFAULT_MODEL_ID else mid.rsplit("/", 1)[-1],
            "modalities": {"input": ["text"], "output": ["text"]},
            "limit": dict(_ENMAAS_LIMITS),
            "cost": {"input": 0, "output": 0},
        }
    return {
        "name": "EnMaaS",
        "package": "@opencode/ai/providers/openai-compatible",
        "settings": {"baseURL": base_url},
        "env": ["ENMAAS_KEY"],
        "models": models,
    }


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
                "plan": normalize_model_id(settings.claude_preset_plan_model),
                "build": normalize_model_id(settings.claude_preset_build_model),
                "small": normalize_model_id(settings.claude_preset_small_model),
            }
        if preset == "gemini":
            return {
                "plan": settings.gemini_preset_plan_model,
                "build": settings.gemini_preset_build_model,
                "small": settings.gemini_preset_small_model,
            }
        if preset == "openai":
            return {
                "plan": settings.openai_preset_plan_model,
                "build": settings.openai_preset_build_model,
                "small": settings.openai_preset_small_model,
            }
        if preset == "enmaas":
            # All roles stay within EnMaaS — an EnMaaS session must never
            # resolve a role to a paid-provider model (ACM-48198 cost-safety).
            return {
                "plan": settings.enmaas_preset_plan_model,
                "build": settings.enmaas_preset_build_model,
                "small": settings.enmaas_preset_small_model,
            }
        return None

    def resolve_build_model(self, model: str) -> str:
        """Resolve presets to their BUILD model and normalize legacy provider IDs."""
        return normalize_model_id(super().resolve_build_model(model))

    def build_config_data(
        self,
        secret: "OpencodeSecret | None" = None,
        mcp_servers: "list[McpServer] | None" = None,
        use_inference_local: bool = False,
        model: str = "",
        enmaas_base_url: str = "",
    ) -> dict[str, str]:
        # Never allow a workspace-derived argument or stored value to choose
        # the destination for ENMAAS_KEY.
        from swarmer.config import configured_enmaas_base_url

        _enmaas_base_url = configured_enmaas_base_url()
        preset = self.resolve_preset(model)
        _plan_model = ""
        _plan_mode = ""
        _build_mode = ""
        if preset:
            # Preset selected — plan/build/small all come from the configured mapping.
            _model = preset["build"]
            _small_model = preset["small"]
            _plan_model = preset["plan"]
            if model == "openai":
                _plan_mode = "primary"
                _build_mode = "primary"
        else:
            # Raw provider/model string (not a preset) — derive small_model from the
            # chosen model: swap pro→flash / opus/sonnet→haiku within same provider.
            # Fall back to fixed defaults if the model is unrecognised. Kept for
            # backward compatibility with sessions created before presets existed.
            # Legacy "google-vertex-anthropic/..." IDs are normalized to the V2
            # canonical "google-vertex" provider ID.
            _model = normalize_model_id(model or "google/gemini-3.8-flash")
            _small_model = "google/gemini-3.5-flash-lite"
            if "/" in _model:
                _provider, _mid = _model.split("/", 1)
                # Strip @version suffix for comparison
                _mid_base = _mid.split("@")[0]
                if _provider == "google-vertex":
                    # Claude on Vertex: use haiku as the small model
                    _small_model = "google-vertex/claude-haiku-4-5@20251001"
                elif _provider == "openai":
                    _small_model = settings.openai_preset_small_model
                elif _provider == _ENMAAS_PROVIDER:
                    # Raw EnMaaS models use the EnMaaS housekeeping model —
                    # never a paid-provider default (ACM-48198 cost-safety).
                    _small_model = settings.enmaas_preset_small_model
                elif "pro" in _mid_base:
                    _small_model = f"{_provider}/{_mid.replace('pro', 'flash')}"
                elif "flash" in _mid_base:
                    _small_model = _model  # already the small model

        # Enabled providers (ACM-48198): EnMaaS sessions enable ONLY enmaas —
        # they must not inherit the unconditional Google enablement, so no
        # Gemini/Claude model can be selected for any role of an EnMaaS run.
        if _model.startswith(f"{_ENMAAS_PROVIDER}/"):
            _enabled_providers: list[str] = [_ENMAAS_PROVIDER]
        elif _model.startswith("openai/"):
            _enabled_providers = []
        else:
            _enabled_providers = ["google"]
        for _candidate in (_model, _small_model, _plan_model):
            if not (_candidate and "/" in _candidate):
                continue
            _provider = _candidate.split("/")[0]
            if _provider in ("google", "google-vertex", "openai", _ENMAAS_PROVIDER) and _provider not in _enabled_providers:
                _enabled_providers.append(_provider)

        config: dict = {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": _enabled_providers,
            "model": _model,
            "agents": {
                # V2 native shape: the V1 top-level "small_model" becomes the
                # model selection for the built-in (hidden) "title" agent.
                "title": {"model": _small_model},
            },
        }

        # Plan agent (ACM-37232): V2 ships a built-in "plan" primary agent, so
        # a preset's stronger-reasoning PLAN model only needs to be assigned to
        # it (the V1 OPENCODE_EXPERIMENTAL_PLAN_MODE env-var toggle is gone).
        if _plan_model and settings.opencode_experimental_plan_mode:
            _plan_cfg: dict[str, str] = {"model": _plan_model}
            if _plan_mode:
                _plan_cfg["mode"] = _plan_mode
            config["agents"]["plan"] = _plan_cfg
        if _build_mode:
            config["agents"]["build"] = {"mode": _build_mode}

        # Native OpenCode V2 provider definition for EnMaaS (ACM-48198). Unlike
        # google/google-vertex/openai, "enmaas" is not a built-in OpenCode
        # provider, so the generated config must define it: the
        # openai-compatible runtime package, the operator-managed gateway
        # baseURL, the ENMAAS_KEY credential env var, and an explicit model
        # entry per referenced model. No secret values ever appear here — the
        # key is injected by the OpenShell gateway provider at runtime.
        if _model.startswith(f"{_ENMAAS_PROVIDER}/"):
            _enmaas_models: list[str] = []
            for _candidate in (_model, _small_model, _plan_model):
                if _candidate and _candidate.startswith(f"{_ENMAAS_PROVIDER}/"):
                    _mid = _candidate.split("/", 1)[1]
                    if _mid and _mid not in _enmaas_models:
                        _enmaas_models.append(_mid)
            config["providers"] = {
                _ENMAAS_PROVIDER: _enmaas_provider_config(_enmaas_models, _enmaas_base_url),
            }

        if mcp_servers:
            # V2 native MCP shape: servers are grouped under "mcp.servers" and
            # connected by default (V2 inverts V1's "enabled" into "disabled").
            mcp_servers_cfg = {}
            for srv in mcp_servers:
                entry = get_catalog_entry(srv.slug) or {}
                command = getattr(srv, "command", None) or entry.get("command", srv.slug)
                environment = getattr(srv, "environment", None) or entry.get("environment", {})
                mcp_servers_cfg[srv.slug] = {
                    "type": "local",
                    "command": command if isinstance(command, list) else [command],
                    "environment": environment or {
                        "JIRA_SERVER_URL": "{env:JIRA_SERVER_URL}",
                        "JIRA_ACCESS_TOKEN": "{env:JIRA_ACCESS_TOKEN}",
                        "JIRA_EMAIL": "{env:JIRA_EMAIL}",
                    },
                }
            if mcp_servers_cfg:
                config["mcp"] = {"servers": mcp_servers_cfg}

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
            "mkdir -p /workspace/.opencode /workspace/.local/share /workspace/.config/opencode && "
            "rm -rf /workspace/.local/share/opencode && "
            "ln -sf /workspace/.opencode /workspace/.local/share/opencode && "
            "find /workspace/.opencode -name '*.db-wal' -o -name '*.db-shm' | xargs rm -f 2>/dev/null; "
        )

    def build_model_setup_cmd(self, model: str) -> str:
        # OpenCode V2 stores model selection in opencode.json (the "model"
        # field, set by build_config_data) and per-session in its own database.
        # The V1 ~/.local/state/opencode/model.json preselection file has no
        # V2 equivalent, so no model setup command is needed.
        return ""

    def build_main_cmd(
        self,
        session: "Session",
        model: str,
        resolved_prompt: str | None = None,
    ) -> str:
        if session.mode in ("server", "tui"):
            # OpenCode V2 unified backend: one persistent shared service powers
            # both the web console (proxied by Swarmer's Chat tab) and the
            # reconnecting TUI (which auto-discovers the service registration
            # written to ~/.local/state/opencode/service.json).
            #
            # The server password comes from /sandbox/.config/opencode/service.json,
            # pre-written by Swarmer at sandbox setup — service mode ignores the
            # OPENCODE_PASSWORD env var. The port must be passed explicitly:
            # --service would otherwise default to the channel port (49374).
            return "opencode serve --service --hostname 0.0.0.0 --port 4096"
        else:
            prompt_text = session.instruction_prompt if resolved_prompt is None else resolved_prompt
            prompt_text = prompt_text or ""
            # --standalone gives the one-shot run a private server instead of
            # leaving a background service daemon behind in a prompt sandbox.
            base_parts = ["opencode", "run", "--standalone", "--model", normalize_model_id(model)]
            prompt_parts = [prompt_text] if prompt_text else []
            return " ".join(shlex.quote(p) for p in base_parts + prompt_parts)

    def is_valid_model(self, model: str) -> bool:
        # The legacy google-vertex-anthropic prefix stays valid input: it is
        # normalized to google-vertex at resolve time.
        return model in _PRESET_NAMES or model.startswith((
            "google/", "google-vertex/", "google-vertex-anthropic/", "openai/",
            f"{_ENMAAS_PROVIDER}/",
        ))

    def get_model_options(
        self,
        secret: "OpencodeSecret | None" = None,
        has_vertex: bool = False,
        has_gemini: bool = False,
        has_openai: bool = False,
        has_enmaas: bool = False,
        enmaas_base_url: str = "",
    ) -> list[dict]:
        # has_gemini/has_openai/has_enmaas are sourced by the caller from an
        # OpenShell gateway provider_exists() check (ACM-37263) — the keys are
        # never stored in the Swarmer DB, so there is no DB fallback here
        # (mirrors has_vertex).
        _vertex_reason = "" if has_vertex else "Vertex AI not configured — add credentials in Secrets."
        _gemini_reason = "" if has_gemini else "Google AI Studio API key not set — add it in Secrets."
        _openai_reason = "" if has_openai else "OpenAI API key not set — add it in Secrets."
        # The endpoint is operator-controlled deployment configuration,
        # validated from the configured value supplied by the caller.
        from swarmer.config import sanitize_enmaas_base_url

        _enmaas_endpoint_ok = bool(sanitize_enmaas_base_url(enmaas_base_url))
        _enmaas_available = has_enmaas and _enmaas_endpoint_ok
        if not _enmaas_endpoint_ok:
            _enmaas_reason = (
                "EnMaaS gateway is not configured or valid — contact your operator "
                "to configure ENMAAS_BASE_URL."
            )
        else:
            _enmaas_reason = "" if has_enmaas else "EnMaaS key not set — add it in Secrets."

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
                "available": has_gemini, "reason": _gemini_reason,
            },
            {
                "value": "openai", "label": "OpenAI", "group": "Presets", "type": "preset",
                "available": has_openai, "reason": _openai_reason,
            },
            {
                # Business-provided zero-cost gateway (ACM-48198).
                "value": _ENMAAS_PROVIDER, "label": "GLM-5.3 (EnMaaS)", "group": "Presets",
                "type": "preset", "available": _enmaas_available, "reason": _enmaas_reason,
            },
        ]

    def get_preset_options(
        self,
        has_vertex: bool = False,
        has_gemini: bool = False,
        has_openai: bool = False,
        has_enmaas: bool = False,
        enmaas_base_url: str = "",
    ) -> list[dict]:
        return [
            opt
            for opt in self.get_model_options(
                has_vertex=has_vertex,
                has_gemini=has_gemini,
                has_openai=has_openai,
                has_enmaas=has_enmaas,
                enmaas_base_url=enmaas_base_url,
            )
            if opt.get("type") == "preset"
        ]

    def get_default_model(
        self,
        has_adc: bool,
        has_gemini: bool | None = None,
        has_openai: bool | None = None,
        has_enmaas: bool | None = None,
    ) -> str:
        # Existing defaults are preserved for mixed-provider workspaces;
        # EnMaaS is only selected when nothing else is available, so an
        # EnMaaS-only workspace gets EnMaaS and no workspace silently
        # switches to it (ACM-48198 cost-safety).
        if has_adc:
            return "claude"
        if has_gemini is not False:
            return "gemini"
        if has_openai:
            return "openai"
        if has_enmaas:
            return _ENMAAS_PROVIDER
        return ""

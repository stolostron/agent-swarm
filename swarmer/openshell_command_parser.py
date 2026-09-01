"""
Parser for OpenShell CLI commands and metadata JSON snippets.

Extracts gateway configuration (endpoint, auth mode, OIDC parameters,
TLS verification, credentials) from:
  1. `openshell gateway add <ENDPOINT> [flags]` CLI commands
  2. JSON metadata blobs (such as ~/.config/openshell/gateways/<name>/metadata.json)
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass
class ParsedGatewayCommand:
    """Structured gateway configuration parsed from CLI command or JSON."""
    gateway_url: str = ""
    auth_mode: str = "oidc"  # "oidc", "bearer", "mtls", "none"
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_audience: str | None = None
    bearer_token: str | None = None
    tls_verify: bool = True
    suggested_name: str | None = None
    raw_input: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "gateway_url": self.gateway_url,
            "auth_mode": self.auth_mode,
            "oidc_issuer": self.oidc_issuer,
            "oidc_client_id": self.oidc_client_id,
            "oidc_audience": self.oidc_audience,
            "bearer_token": self.bearer_token,
            "tls_verify": self.tls_verify,
            "suggested_name": self.suggested_name,
            "errors": self.errors,
        }


def parse_gateway_command_or_json(text: str) -> ParsedGatewayCommand:
    """Parse a gateway CLI command or JSON snippet into a ParsedGatewayCommand."""
    if not text or not text.strip():
        return ParsedGatewayCommand(errors=["Input is empty."])

    cleaned = text.strip()

    # Check if input is a JSON snippet
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return _parse_json_metadata(cleaned)

    # Otherwise parse as shell command / flags
    return _parse_cli_command(cleaned)


def _parse_json_metadata(json_str: str) -> ParsedGatewayCommand:
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        return ParsedGatewayCommand(errors=[f"Invalid JSON: {exc}"])

    if not isinstance(data, dict):
        return ParsedGatewayCommand(errors=["JSON payload must be an object."])

    def _opt_str(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        return None

    endpoint = data.get("gateway_endpoint") or data.get("endpoint") or data.get("gateway_url") or data.get("url") or ""
    # Coerce to str: JSON values for these keys may be non-string (e.g. a number
    # or null), and downstream .startswith()/DB storage assume a string.
    if not isinstance(endpoint, str):
        endpoint = str(endpoint) if endpoint is not None else ""
    auth_mode = str(data.get("auth_mode") or "oidc").lower()
    if endpoint.startswith("http://") and auth_mode not in ("bearer", "mtls"):
        auth_mode = "none"

    return ParsedGatewayCommand(
        gateway_url=endpoint,
        auth_mode=auth_mode,
        oidc_issuer=_opt_str(data.get("oidc_issuer")),
        oidc_client_id=_opt_str(data.get("oidc_client_id")),
        oidc_audience=_opt_str(data.get("oidc_audience")),
        bearer_token=_opt_str(data.get("bearer_token") or data.get("token")),
        tls_verify=not bool(data.get("insecure") or data.get("gateway_insecure")),
        suggested_name=_opt_str(data.get("name")),
        raw_input=json_str,
    )


def _parse_cli_command(cmd_str: str) -> ParsedGatewayCommand:
    # Normalize line continuations (e.g. `\` at end of line) and multiple spaces
    normalized = re.sub(r"\\\s*\n", " ", cmd_str)
    normalized = normalized.strip()

    # If user pasted just a bare URL
    if (normalized.startswith("http://") or normalized.startswith("https://") or normalized.startswith("grpc://")) and " " not in normalized:
        auth_mode = "none" if normalized.startswith("http://") else "oidc"
        return ParsedGatewayCommand(
            gateway_url=normalized,
            auth_mode=auth_mode,
            raw_input=cmd_str,
        )

    try:
        tokens = shlex.split(normalized)
    except ValueError as exc:
        return ParsedGatewayCommand(errors=[f"Failed to parse shell command: {exc}"])

    if not tokens:
        return ParsedGatewayCommand(errors=["Command contained no tokens."])

    # Strip leading 'openshell', 'gateway', 'gw', 'add' if present
    idx = 0
    if idx < len(tokens) and tokens[idx] in ("openshell", "./openshell"):
        idx += 1
    if idx < len(tokens) and tokens[idx] in ("gateway", "gw"):
        idx += 1
    if idx < len(tokens) and tokens[idx] == "add":
        idx += 1

    remaining = tokens[idx:]

    gateway_url = ""
    name = None
    oidc_issuer = None
    oidc_client_id = None
    oidc_audience = None
    bearer_token = None
    tls_verify = True
    auth_mode = "oidc"

    i = 0
    while i < len(remaining):
        token = remaining[i]

        if token == "--name" and i + 1 < len(remaining):
            name = remaining[i + 1]
            i += 2
        elif token.startswith("--name="):
            name = token.split("=", 1)[1]
            i += 1
        elif token == "--oidc-issuer" and i + 1 < len(remaining):
            oidc_issuer = remaining[i + 1]
            auth_mode = "oidc"
            i += 2
        elif token.startswith("--oidc-issuer="):
            oidc_issuer = token.split("=", 1)[1]
            auth_mode = "oidc"
            i += 1
        elif token == "--oidc-client-id" and i + 1 < len(remaining):
            oidc_client_id = remaining[i + 1]
            i += 2
        elif token.startswith("--oidc-client-id="):
            oidc_client_id = token.split("=", 1)[1]
            i += 1
        elif token == "--oidc-audience" and i + 1 < len(remaining):
            oidc_audience = remaining[i + 1]
            i += 2
        elif token.startswith("--oidc-audience="):
            oidc_audience = token.split("=", 1)[1]
            i += 1
        elif token in ("--bearer-token", "--token") and i + 1 < len(remaining):
            bearer_token = remaining[i + 1]
            auth_mode = "bearer"
            i += 2
        elif token.startswith("--bearer-token=") or token.startswith("--token="):
            bearer_token = token.split("=", 1)[1]
            auth_mode = "bearer"
            i += 1
        elif token in ("--gateway-insecure", "-k", "--insecure"):
            tls_verify = False
            i += 1
        elif token == "--gateway-endpoint" and i + 1 < len(remaining):
            gateway_url = remaining[i + 1]
            i += 2
        elif token.startswith("--gateway-endpoint="):
            gateway_url = token.split("=", 1)[1]
            i += 1
        elif token in ("--local", "--remote"):
            # Informational flags in OpenShell CLI
            i += 1
        elif not token.startswith("-") and not gateway_url:
            # Positional endpoint URL
            gateway_url = token
            i += 1
        else:
            i += 1

    # Infer auth mode if not explicitly set
    if gateway_url.startswith("http://") and auth_mode not in ("bearer", "mtls"):
        auth_mode = "none"
    elif oidc_issuer or oidc_client_id:
        auth_mode = "oidc"
    elif bearer_token:
        auth_mode = "bearer"

    # Derive suggested name from host if not provided
    if not name and gateway_url:
        try:
            parsed = urlparse(gateway_url if "://" in gateway_url else f"https://{gateway_url}")
            host = parsed.hostname or ""
            if host and host not in ("localhost", "127.0.0.1"):
                # e.g. gw-openshell-7eda780a294c87c6.openshell.stage... -> swarm/dedicated
                name = host.split(".")[0]
        except Exception:
            pass

    return ParsedGatewayCommand(
        gateway_url=gateway_url,
        auth_mode=auth_mode,
        oidc_issuer=oidc_issuer,
        oidc_client_id=oidc_client_id,
        oidc_audience=oidc_audience,
        bearer_token=bearer_token,
        tls_verify=tls_verify,
        suggested_name=name,
        raw_input=cmd_str,
    )

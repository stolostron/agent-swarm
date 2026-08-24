#!/usr/bin/env python3
"""Configure or display Agent Swarm MCP settings in opencode.json.

Accepts a bearer token, a pasted opencode.json snippet, or interactively
prompts for credentials, auto-detects the cluster's Swarmer route, and
updates opencode.json.

Usage:
    python3 scripts/mcp_setup.py [--token TOKEN] [--url URL] [--config PATH] [--print-only]
    make mcp-setup TOKEN="..."
    make api-info
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _decode_jwt_sub(token: str) -> str:
    """Extract 'sub' claim from a JWT payload without signature verification."""
    try:
        parts = token.strip().split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload_b64))
            return data.get("sub", "")
    except Exception:
        pass
    return ""


def _parse_pasted_token_or_json(raw: str) -> tuple[str, str | None]:
    """Parse raw input which could be a plain token or a pasted JSON snippet.

    Returns (token, url_or_None).
    """
    raw = raw.strip()
    if not raw:
        return "", None

    # Check if raw input is JSON
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            # Check for nested opencode.json format
            mcp = data.get("mcp", {})
            aswarm = mcp.get("agent-swarm", {})
            env = aswarm.get("environment", {})
            if "AGENT_SWARM_API_TOKEN" in env:
                return env["AGENT_SWARM_API_TOKEN"].strip(), env.get("AGENT_SWARM_API_URL")

            # Check for top-level token fields
            if "AGENT_SWARM_API_TOKEN" in data:
                return str(data["AGENT_SWARM_API_TOKEN"]).strip(), data.get("AGENT_SWARM_API_URL")
            if "token" in data:
                return str(data["token"]).strip(), data.get("url")
        except json.JSONDecodeError:
            pass

    # Check for key=value format or JSON-like fragments
    token_match = re.search(r'AGENT_SWARM_API_TOKEN["\']?\s*[:=]\s*["\']([^"\'\s]+)["\']', raw)
    url_match = re.search(r'AGENT_SWARM_API_URL["\']?\s*[:=]\s*["\']([^"\'\s]+)["\']', raw)
    if token_match:
        token = token_match.group(1).strip()
        url = url_match.group(1).strip() if url_match else None
        return token, url

    # Otherwise treat the entire stripped string as a raw token
    return raw, None


def _auto_detect_url(namespace: str, config_path: Path) -> str:
    """Detect Swarmer route URL from OpenShift route, existing config, or default."""
    if os.environ.get("AGENT_SWARM_API_URL"):
        return os.environ["AGENT_SWARM_API_URL"].rstrip("/")

    # Try OpenShift route lookup via kubectl
    try:
        res = subprocess.run(
            ["kubectl", "get", "route", "swarmer", "-n", namespace, "-o", "jsonpath={.spec.host}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return f"https://{res.stdout.strip()}"
    except Exception:
        pass

    # Try existing opencode.json
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            existing_url = (
                cfg.get("mcp", {})
                .get("agent-swarm", {})
                .get("environment", {})
                .get("AGENT_SWARM_API_URL")
            )
            if existing_url:
                return existing_url.rstrip("/")
        except Exception:
            pass

    return "http://localhost:8080"


def _auto_detect_token() -> str:
    """Try to detect bearer token from env or oc whoami -t."""
    if os.environ.get("AGENT_SWARM_API_TOKEN"):
        return os.environ["AGENT_SWARM_API_TOKEN"].strip()

    try:
        res = subprocess.run(
            ["oc", "whoami", "-t"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Agent Swarm MCP in opencode.json")
    parser.add_argument("--token", help="Bearer token or pasted JSON snippet")
    parser.add_argument("--url", help="Swarmer API URL override")
    parser.add_argument("--config", default="opencode.json", help="Path to opencode.json")
    parser.add_argument("--namespace", default=os.environ.get("NAMESPACE", "swarmer"), help="Swarmer namespace")
    parser.add_argument("--insecure", action="store_true", help="Disable SSL verification for self-signed certificates")
    parser.add_argument("--print-only", action="store_true", help="Print MCP config without modifying files")
    args = parser.parse_args()

    config_path = Path(args.config)
    token = ""
    url_override = args.url

    # 1. Resolve token from CLI argument, stdin, env, or oc
    if args.token:
        parsed_token, parsed_url = _parse_pasted_token_or_json(args.token)
        token = parsed_token
        if not url_override and parsed_url:
            url_override = parsed_url
    elif not sys.stdin.isatty() and not args.print_only:
        # Piped input
        stdin_data = sys.stdin.read().strip()
        if stdin_data:
            parsed_token, parsed_url = _parse_pasted_token_or_json(stdin_data)
            token = parsed_token
            if not url_override and parsed_url:
                url_override = parsed_url

    if not token:
        token = _auto_detect_token()

    if not token and not args.print_only and sys.stdin.isatty():
        try:
            prompt_input = input("Paste your Swarmer bearer token or opencode.json snippet: ").strip()
            if prompt_input:
                parsed_token, parsed_url = _parse_pasted_token_or_json(prompt_input)
                token = parsed_token
                if not url_override and parsed_url:
                    url_override = parsed_url
        except (KeyboardInterrupt, EOFError):
            print("\nOperation cancelled.", file=sys.stderr)
            sys.exit(1)

    # 2. Resolve URL
    api_url = (url_override or _auto_detect_url(args.namespace, config_path)).rstrip("/")

    # 3. Build MCP configuration block
    verify_ssl = "false" if args.insecure else "true"
    mcp_agent_swarm = {
        "type": "local",
        "command": ["agent-swarm-mcp-server"],
        "enabled": True,
        "environment": {
            "AGENT_SWARM_API_URL": api_url,
            "AGENT_SWARM_VERIFY_SSL": verify_ssl,
        },
    }
    if token:
        mcp_agent_swarm["environment"]["AGENT_SWARM_API_TOKEN"] = token

    jwt_user = _decode_jwt_sub(token) if token else ""

    # If print-only mode requested:
    if args.print_only:
        print("\nSwarmer API Details:")
        print("──────────────────────────────────────────────────")
        print(f"API URL:   {api_url}")
        if token:
            print(f"User:      {jwt_user or '(Bearer Token)'}")
            print("Token:     [configured]")
        else:
            print("Token:     (No active token found)")
        print("──────────────────────────────────────────────────")
        print("\nopencode.json configuration snippet:")
        print_snippet = json.loads(json.dumps(mcp_agent_swarm))
        if "AGENT_SWARM_API_TOKEN" in print_snippet.get("environment", {}):
            print_snippet["environment"]["AGENT_SWARM_API_TOKEN"] = "<YOUR_TOKEN>"
        print(json.dumps({"mcp": {"agent-swarm": print_snippet}}, indent=2))
        print("\nRun 'make mcp-setup' to apply this configuration to opencode.json.")
        return

    if not token:
        print("Error: No bearer token provided or found.", file=sys.stderr)
        print("Obtain your token from the Swarmer Web UI (/token) or run 'make mcp-setup'", file=sys.stderr)
        sys.exit(1)

    # 4. Update opencode.json
    config_data: dict[str, Any] = {}
    if config_path.exists():
        try:
            config_data = json.loads(config_path.read_text())
            if not isinstance(config_data, dict):
                config_data = {}
        except Exception as e:
            print(f"Warning: Failed to parse existing {config_path}: {e}", file=sys.stderr)
            config_data = {}

    if "$schema" not in config_data:
        config_data["$schema"] = "https://opencode.ai/config.json"

    if "mcp" not in config_data or not isinstance(config_data["mcp"], dict):
        config_data["mcp"] = {}

    config_data["mcp"]["agent-swarm"] = mcp_agent_swarm

    config_path.write_text(json.dumps(config_data, indent=2) + "\n")

    print(f"✓ Updated {config_path} with Agent Swarm MCP configuration.")
    print(f"  API URL:  {api_url}")
    if jwt_user:
        print(f"  Identity: {jwt_user}")
    print("  Token:    [configured]")


if __name__ == "__main__":
    main()

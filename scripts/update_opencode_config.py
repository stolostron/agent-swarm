#!/usr/bin/env python3
"""Update or create opencode.json with the resolved Agent Swarm API URL."""

import json
import os
import sys


def update_opencode_json(api_url: str, config_path: str = "opencode.json") -> None:
    data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    if not isinstance(data, dict):
        data = {}

    if "$schema" not in data:
        data["$schema"] = "https://opencode.ai/config.json"

    mcp_key = "mcpServers" if "mcpServers" in data else "mcp"
    if mcp_key not in data or not isinstance(data[mcp_key], dict):
        data[mcp_key] = {}

    mcp = data[mcp_key]
    if "agent-swarm" not in mcp or not isinstance(mcp["agent-swarm"], dict):
        mcp["agent-swarm"] = {
            "type": "local",
            "command": ["agent-swarm-mcp-server"],
            "enabled": True,
            "environment": {
                "AGENT_SWARM_API_URL": api_url,
                "AGENT_SWARM_VERIFY_SSL": "false",
            },
        }
    else:
        as_m = mcp["agent-swarm"]
        as_m["enabled"] = True
        if "environment" not in as_m or not isinstance(as_m["environment"], dict):
            as_m["environment"] = {}
        as_m["environment"]["AGENT_SWARM_API_URL"] = api_url
        if "AGENT_SWARM_VERIFY_SSL" not in as_m["environment"]:
            as_m["environment"]["AGENT_SWARM_VERIFY_SSL"] = "false"

    parent_dir = os.path.dirname(config_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    sys.stderr.write(f"Updated {config_path} with AGENT_SWARM_API_URL={api_url}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: update_opencode_config.py <API_URL> [config_path]\n")
        sys.exit(1)
    target_url = sys.argv[1]
    cfg = sys.argv[2] if len(sys.argv) > 2 else "opencode.json"
    update_opencode_json(target_url, cfg)

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.update_opencode_config import update_opencode_json


def test_update_opencode_json_creates_new_file(tmp_path):
    target_file = tmp_path / "opencode.json"
    url = "https://swarmer.test.internal:8080"
    update_opencode_json(url, str(target_file))

    assert os.path.exists(target_file)
    with open(target_file) as f:
        data = json.load(f)

    assert data["$schema"] == "https://opencode.ai/config.json"
    # OpenCode V2 native shape: servers nest under "mcp.servers".
    assert "agent-swarm" in data["mcp"]["servers"]
    as_mcp = data["mcp"]["servers"]["agent-swarm"]
    assert "enabled" not in as_mcp  # V2 drops the V1 enabled flag
    assert as_mcp["environment"]["AGENT_SWARM_API_URL"] == url
    assert as_mcp["environment"]["AGENT_SWARM_VERIFY_SSL"] == "true"


def test_update_opencode_json_updates_existing_file(tmp_path):
    target_file = tmp_path / "opencode.json"
    # Existing V1 flat map — must be normalized to mcp.servers on write.
    initial_data = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "jira": {"type": "local", "command": ["jira-server"], "enabled": True},
            "agent-swarm": {
                "type": "local",
                "command": ["agent-swarm-mcp-server"],
                "enabled": False,
                "environment": {"AGENT_SWARM_API_URL": "http://old-url"},
            },
        },
    }
    with open(target_file, "w") as f:
        json.dump(initial_data, f)

    new_url = "https://new-swarmer.test.internal"
    update_opencode_json(new_url, str(target_file))

    with open(target_file) as f:
        data = json.load(f)

    servers = data["mcp"]["servers"]
    assert "jira" in servers
    assert servers["agent-swarm"]["environment"]["AGENT_SWARM_API_URL"] == new_url
    assert servers["agent-swarm"]["environment"]["AGENT_SWARM_VERIFY_SSL"] == "true"
    # The V1 "enabled" flags are dropped during V2 normalization.
    assert "enabled" not in servers["agent-swarm"]


def test_update_opencode_json_preserves_v2_nested_servers(tmp_path):
    target_file = tmp_path / "opencode.json"
    initial_data = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
            "servers": {
                "jira": {"type": "local", "command": ["jira-server"]},
            }
        },
    }
    with open(target_file, "w") as f:
        json.dump(initial_data, f)

    url = "https://swarmer.test.internal:8080"
    update_opencode_json(url, str(target_file))

    with open(target_file) as f:
        data = json.load(f)

    servers = data["mcp"]["servers"]
    assert "jira" in servers
    assert servers["jira"]["command"] == ["jira-server"]
    assert servers["agent-swarm"]["environment"]["AGENT_SWARM_API_URL"] == url


def test_update_opencode_json_migrates_mcp_servers_key(tmp_path):
    target_file = tmp_path / "opencode.json"
    initial_data = {
        "mcpServers": {
            "agent-swarm": {
                "type": "local",
                "command": ["agent-swarm-mcp-server"],
                "environment": {"AGENT_SWARM_API_URL": "http://old-url"},
            },
        },
    }
    with open(target_file, "w") as f:
        json.dump(initial_data, f)

    url = "https://swarmer.test.internal:8080"
    update_opencode_json(url, str(target_file))

    with open(target_file) as f:
        data = json.load(f)

    assert "mcpServers" not in data
    assert data["mcp"]["servers"]["agent-swarm"]["environment"]["AGENT_SWARM_API_URL"] == url


def test_update_opencode_json_creates_parent_directory(tmp_path):
    target_file = tmp_path / "nested" / "subdir" / "opencode.json"
    url = "https://swarmer.test.internal:8080"
    update_opencode_json(url, str(target_file))

    assert os.path.exists(target_file)
    with open(target_file) as f:
        data = json.load(f)

    assert data["mcp"]["servers"]["agent-swarm"]["environment"]["AGENT_SWARM_API_URL"] == url

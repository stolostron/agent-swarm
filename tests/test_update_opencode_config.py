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
    assert "agent-swarm" in data["mcp"]
    as_mcp = data["mcp"]["agent-swarm"]
    assert as_mcp["enabled"] is True
    assert as_mcp["environment"]["AGENT_SWARM_API_URL"] == url
    assert as_mcp["environment"]["AGENT_SWARM_VERIFY_SSL"] == "false"


def test_update_opencode_json_updates_existing_file(tmp_path):
    target_file = tmp_path / "opencode.json"
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

    assert "jira" in data["mcp"]
    assert data["mcp"]["jira"]["enabled"] is True
    assert data["mcp"]["agent-swarm"]["enabled"] is True
    assert data["mcp"]["agent-swarm"]["environment"]["AGENT_SWARM_API_URL"] == new_url
    assert data["mcp"]["agent-swarm"]["environment"]["AGENT_SWARM_VERIFY_SSL"] == "false"


def test_update_opencode_json_creates_parent_directory(tmp_path):
    target_file = tmp_path / "nested" / "subdir" / "opencode.json"
    url = "https://swarmer.test.internal:8080"
    update_opencode_json(url, str(target_file))

    assert os.path.exists(target_file)
    with open(target_file) as f:
        data = json.load(f)

    assert data["mcp"]["agent-swarm"]["environment"]["AGENT_SWARM_API_URL"] == url

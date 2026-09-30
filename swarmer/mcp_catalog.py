"""
Registry of well-known MCP servers available in the catalog.

Each entry contains the defaults needed to add the server to a workspace.
"""

MCP_SERVER_CATALOG: list[dict] = [
    {
        "slug": "atlassian-jira",
        "display_name": "Jira Atlassian",
        "description": (
            "Jira MCP server using a personal API token. "
            "Read/write issues, search projects, manage boards, and more."
        ),
        "server_url": "",
        "server_type": "stdio",
        "command": "jira-mcp-server",
        "default_jira_server_url": "https://redhat.atlassian.net/",
        "icon": "fas fa-bug",
        "color": "blue",
    },
    {
        "slug": "agent-swarm",
        "display_name": "Agent Swarm",
        "description": "Manage sessions within this workspace from an agent.",
        "server_url": "",
        "server_type": "stdio",
        "command": "agent-swarm-mcp-server",
        "internal": True,
        "environment": {
            "AGENT_SWARM_API_URL": "{env:AGENT_SWARM_API_URL}",
            "AGENT_SWARM_API_TOKEN": "{env:AGENT_SWARM_API_TOKEN}",
            "AGENT_SWARM_WORKSPACE": "{env:AGENT_SWARM_WORKSPACE}",
            "AGENT_SWARM_VERIFY_SSL": "false",
        },
        "icon": "fas fa-robot",
        "color": "purple",
    },
]


def get_catalog_entry(slug: str) -> dict | None:
    for entry in MCP_SERVER_CATALOG:
        if entry["slug"] == slug:
            return entry
    return None

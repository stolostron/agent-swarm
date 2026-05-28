"""Entry point for the Agent Swarm MCP server."""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Swarm MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for SSE transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port for SSE transport (default: 8080)",
    )
    args = parser.parse_args()

    try:
        from .server import AgentSwarmMCPServer
        server = AgentSwarmMCPServer()
        server.run(transport=args.transport, host=args.host, port=args.port)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

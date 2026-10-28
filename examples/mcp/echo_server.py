"""Minimal stdio MCP server used by examples/mcp_servers.yaml.

Speaks the MCP JSON-RPC protocol over stdin/stdout with no dependencies, so
it doubles as the fixture for the harness's MCP integration tests.
"""

from __future__ import annotations

import json
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle_call(name: str, arguments: dict) -> dict:
    if name == "echo":
        return {
            "content": [{"type": "text", "text": str(arguments.get("text", ""))}]
        }
    if name == "add":
        return {
            "content": [
                {"type": "text",
                 "text": str(int(arguments.get("a", 0)) + int(arguments.get("b", 0)))}
            ]
        }
    return {"content": [{"type": "text", "text": f"unknown tool: {name}"}],
            "isError": True}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "example-echo", "version": "1.0"},
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Return the supplied text unchanged",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        },
                        {
                            "name": "add",
                            "description": "Add two integers",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"a": {"type": "integer"},
                                               "b": {"type": "integer"}},
                                "required": ["a", "b"],
                            },
                        },
                    ]
                },
            })
        elif method == "tools/call":
            params = message.get("params") or {}
            send({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": handle_call(
                    str(params.get("name")), params.get("arguments") or {}
                ),
            })


if __name__ == "__main__":
    main()

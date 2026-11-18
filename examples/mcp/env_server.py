"""Minimal stdio MCP fixture that reports its own environment.

Used by the MCP environment hardening tests: every ``env_report`` call returns
the value (or ``null``) the spawned child process sees for each requested
variable, so the test can prove which variables were inherited.
"""

from __future__ import annotations

import json
import os
import sys


def send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


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
                    "serverInfo": {"name": "env-probe", "version": "1.0"},
                },
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "tools": [{
                        "name": "env_report",
                        "description": "Report the child's environment values",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "names": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["names"],
                        },
                    }],
                },
            })
        elif method == "tools/call":
            params = message.get("params") or {}
            arguments = params.get("arguments") or {}
            names = [
                str(name) for name in (arguments.get("names") or [])
                if str(name)
            ]
            report = {name: os.environ.get(name) for name in names}
            send({
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "content": [{
                        "type": "text",
                        "text": json.dumps(report, sort_keys=True),
                    }],
                },
            })


if __name__ == "__main__":
    main()

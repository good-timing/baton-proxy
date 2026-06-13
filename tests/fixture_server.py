"""Minimal stdio MCP server used by the proxy tests as the upstream fixture.

Hand-rolls JSON-RPC over newline-delimited stdio so there are no test
dependencies and the upstream behavior is fully under our control.
"""

from __future__ import annotations

import json
import sys


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = req.get("method")
        req_id = req.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "fixture-server", "version": "0.1.0"},
                        "instructions": "Fixture MCP server. Use echo to echo text.",
                    },
                }
            )
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo back the input text verbatim.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            },
                            {
                                "name": "boom",
                                "description": "Always errors. Used to test tool_call_error emission.",
                                "inputSchema": {"type": "object", "properties": {}, "required": []},
                            },
                        ]
                    },
                }
            )
        elif method == "tools/call":
            params = req.get("params", {})
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            if tool_name == "echo":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": f"Echo: {tool_args.get('text', '')}"}
                            ]
                        },
                    }
                )
            elif tool_name == "boom":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32000, "message": "boom"},
                    }
                )
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                    }
                )
        else:
            if req_id is not None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )


if __name__ == "__main__":
    main()

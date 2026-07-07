"""Canonical JSON-RPC responses shared by both MCP test fixtures.

``fixture_server.py`` (stdio) and ``fixture_http_server.py`` (Streamable HTTP)
must produce the *same* friction event stream so a test can hold both transports
to one expected result. They used to carry separate copies of these responses;
this module is the single source. Each fixture handles only its own framing
(stdio newline-delimited vs HTTP JSON/SSE); the payloads live here.

``result_for(req)`` returns the JSON-RPC response dict for a request, or ``None``
for a notification (no ``id``) — the caller turns ``None`` into "send nothing"
(stdio) or a 202 (HTTP).
"""

from __future__ import annotations

from typing import Any

# The two tools every fixture exposes: `echo` (happy path) and `boom` (always
# errors, for tool_call_error coverage).


def result_for(req: dict[str, Any]) -> dict[str, Any] | None:
    """Build the JSON-RPC response for a request, or None for a notification."""
    method = req.get("method")
    req_id = req.get("id")

    # Notification (no id) — no response.
    if req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "fixture-mcp-server", "version": "0.1.0"},
                "instructions": "Fixture MCP server. Use echo to echo text.",
            },
        }
    if method == "tools/list":
        return {
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
                    {
                        "name": "argkeys",
                        "description": (
                            "Return the sorted argument keys received. Lets a test "
                            "prove exactly which arguments reached the upstream "
                            "(e.g., that an injected param was stripped)."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": [],
                        },
                    },
                ]
            },
        }
    if method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "resources": [
                    {
                        "uri": "fixture://notes.txt",
                        "name": "notes",
                        "description": "A test note resource.",
                        "mimeType": "text/plain",
                    },
                    {
                        "uri": "fixture://secret.txt",
                        "name": "secret",
                        "description": "Always returns an error when read.",
                        "mimeType": "text/plain",
                    },
                ]
            },
        }
    if method == "resources/read":
        uri = (req.get("params") or {}).get("uri", "")
        if uri == "fixture://notes.txt":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {"uri": uri, "mimeType": "text/plain", "text": "Hello from fixture notes."}
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32002, "message": f"Resource not found: {uri}"},
        }
    if method == "prompts/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "prompts": [
                    {"name": "summarize", "description": "Summarize some text."},
                    {"name": "boom_prompt", "description": "Always errors when fetched."},
                ]
            },
        }
    if method == "prompts/get":
        name = (req.get("params") or {}).get("name", "")
        if name == "summarize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "description": "Summarize some text.",
                    "messages": [
                        {
                            "role": "user",
                            "content": {"type": "text", "text": "Please summarize: {{text}}"},
                        }
                    ],
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32002, "message": f"Prompt not found: {name}"},
        }
    if method == "tools/call":
        params = req.get("params") or {}
        tool_name = params.get("name")
        tool_args = params.get("arguments", {})
        if tool_name == "echo":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Echo: {tool_args.get('text', '')}"}]
                },
            }
        if tool_name == "boom":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32000, "message": "boom"},
            }
        if tool_name == "argkeys":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": "keys: " + ",".join(sorted(tool_args or {}))}
                    ]
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
        }
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }

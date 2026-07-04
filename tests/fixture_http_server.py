"""Minimal Streamable HTTP MCP server used by the HTTP-transport proxy tests.

Hand-rolls the MCP Streamable HTTP transport (spec 2025-03-26) over stdlib
``http.server`` so there are no test dependencies and the upstream behavior is
fully under our control. Mirrors ``fixture_server.py`` (the stdio fixture) tool
for tool — same ``echo`` / ``boom`` tools, same resources/prompts — so the two
transports can be asserted against the same expected event stream.

What it exercises, deliberately:

* **Both response framings.** A request whose JSON-RPC ``method`` is in
  ``_SSE_METHODS`` gets its response back as ``text/event-stream`` (a single
  ``data:`` frame on the POST response); everything else gets a plain
  ``application/json`` body. This is the one behavioral fork the proxy's HTTP
  receive path has to handle, so the fixture forces both on every run.
* **Bearer auth.** When ``serve(require_auth=<token>)`` is set, every POST must
  carry ``Authorization: Bearer <token>`` or the server returns 401. Lets the
  auth test assert the header is threaded from ``BATON_UPSTREAM_AUTH_TOKEN``.
* **Session id.** The ``initialize`` response carries an ``Mcp-Session-Id``
  header; the fixture then *requires* that header to be echoed on every
  subsequent request (409 otherwise), so a test can prove the proxy captures
  and re-sends it.
* **Notifications.** A JSON-RPC message with no ``id`` (e.g.
  ``notifications/initialized``) gets a bare ``202 Accepted`` with no body — the
  proxy must not block waiting for a response to these.

Run standalone for manual poking:  ``python fixture_http_server.py [port]``
(prints the bound ``http://127.0.0.1:<port>/mcp`` URL to stdout, then serves).
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Methods whose response the fixture returns as an SSE stream rather than a
# plain JSON body. Picking `resources/read` + `prompts/get` means the A1 e2e
# run drives *both* framings through the proxy in a single session.
_SSE_METHODS = frozenset({"resources/read", "prompts/get"})

# A tools/call for this tool name makes the server stall *before* responding,
# so a client with a short read timeout hits a live-but-silent upstream (as
# opposed to the immediate connection-refused of a dead port). Lets a test
# prove the proxy's socket timeout actually fires and bounds the wait.
_SLOW_TOOL = "slow"
_SLOW_SLEEP_S = 3.0

_SESSION_HEADER = "Mcp-Session-Id"


def _result_for(req: dict[str, Any]) -> dict[str, Any] | None:
    """Build the JSON-RPC response dict for a request, or None for notifications.

    Behaviourally identical to ``fixture_server.py`` so both transports produce
    the same friction event stream.
    """
    method = req.get("method")
    req_id = req.get("id")

    # Notification (no id) — the caller turns this into a 202/no-body.
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
                "serverInfo": {"name": "fixture-http-server", "version": "0.1.0"},
                "instructions": "Fixture Streamable HTTP MCP server. Use echo to echo text.",
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


class _Handler(BaseHTTPRequestHandler):
    # Per-request auth + session state live on the *server instance*
    # (``self.server``), not this handler class — a class attribute would leak
    # across server instances in a single test process (the session id from one
    # test's server would 409 the next test's requests).

    def log_message(self, *args: Any) -> None:  # noqa: D401 - silence stderr spam
        pass

    def _send_json(
        self, status: int, body: dict[str, Any], *, session_id: str | None = None
    ) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if session_id is not None:
            self.send_header(_SESSION_HEADER, session_id)
        self.end_headers()
        self.wfile.write(raw)

    def _send_sse(
        self, status: int, body: dict[str, Any], *, session_id: str | None = None
    ) -> None:
        """Send a single JSON-RPC message as one SSE ``data:`` frame.

        The session id (when present) rides an HTTP *header* — independent of the
        SSE body framing — so a client capturing it works the same whether the
        handshake response is JSON or SSE.
        """
        frame = f"event: message\ndata: {json.dumps(body)}\n\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        if session_id is not None:
            self.send_header(_SESSION_HEADER, session_id)
        self.end_headers()
        self.wfile.write(frame)

    def _send_status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        # Record identifying headers so tests can prove the proxy sends a named
        # UA (the urllib default is Cloudflare-banned in the wild) and announces
        # itself as an intermediary via Via.
        self.server.last_user_agent = self.headers.get("User-Agent")  # type: ignore[attr-defined]
        self.server.last_via = self.headers.get("Via")  # type: ignore[attr-defined]

        # --- Auth gate -----------------------------------------------------
        required = getattr(self.server, "require_auth", None)
        if required:
            got = self.headers.get("Authorization", "")
            if got != f"Bearer {required}":
                self._send_status(401)
                return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_status(400)
            return

        method = req.get("method")

        # --- Session id handshake -----------------------------------------
        # A ``stateless`` server (mirrors DeepWiki's real behavior) issues no
        # Mcp-Session-Id and requires none — exercises the client's "only echo
        # the session id if the server actually issued one" path.
        stateless = getattr(self.server, "stateless", False)
        session_id_issued = getattr(self.server, "session_id", None)
        if method == "initialize" and not stateless:
            session_id_issued = uuid.uuid4().hex
            self.server.session_id = session_id_issued  # type: ignore[attr-defined]
        elif session_id_issued is not None:
            # Every post-init request must echo the session id we issued.
            if self.headers.get(_SESSION_HEADER) != session_id_issued:
                self._send_status(409)
                return

        # Live-but-silent upstream: accept the request, then stall before
        # sending anything. A client whose read timeout is shorter than the
        # sleep will time out mid-request — the case the connection-refused
        # test can't cover.
        if method == "tools/call" and (req.get("params") or {}).get("name") == _SLOW_TOOL:
            time.sleep(_SLOW_SLEEP_S)

        result = _result_for(req)

        # Notification — no response body.
        if result is None:
            self._send_status(202)
            return

        session_id = session_id_issued if method == "initialize" else None
        # ``initialize_sse`` mirrors a real server (DeepWiki) that returns the
        # handshake itself as SSE, not JSON — the one framing my JSON-initialize
        # fixture didn't cover until a live probe found it.
        initialize_sse = getattr(self.server, "initialize_sse", False)
        if method in _SSE_METHODS or (method == "initialize" and initialize_sse):
            self._send_sse(200, result, session_id=session_id)
        else:
            self._send_json(200, result, session_id=session_id)


def serve(
    port: int = 0,
    require_auth: str | None = None,
    *,
    stateless: bool = False,
    initialize_sse: bool = False,
) -> ThreadingHTTPServer:
    """Bind a fixture server on ``127.0.0.1:port`` (0 = ephemeral) and return it.

    ``require_auth`` (a bearer token) makes every POST demand
    ``Authorization: Bearer <token>`` or return 401. ``stateless`` +
    ``initialize_sse`` reproduce a real server observed in the wild (DeepWiki):
    no ``Mcp-Session-Id`` issued, and the ``initialize`` response returned as SSE
    rather than JSON. Auth + session state live on the returned server instance,
    so multiple fixtures can coexist in one process without leaking session ids.

    Caller is responsible for ``server.serve_forever()`` (typically on a thread)
    and ``server.shutdown()``. The bound URL is ``http://{host}:{port}/mcp``.
    """
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.require_auth = require_auth  # type: ignore[attr-defined]
    httpd.session_id = None  # type: ignore[attr-defined]
    httpd.stateless = stateless  # type: ignore[attr-defined]
    httpd.initialize_sse = initialize_sse  # type: ignore[attr-defined]
    httpd.last_user_agent = None  # type: ignore[attr-defined]
    httpd.last_via = None  # type: ignore[attr-defined]
    return httpd


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    httpd = serve(port)
    host, bound_port = httpd.server_address[:2]
    sys.stdout.write(f"http://{host}:{bound_port}/mcp\n")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()

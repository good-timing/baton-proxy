"""A real MCP server (the ``mcp`` SDK's FastMCP) behind a gate like a hosted server's.

Every request must carry ``Authorization: Bearer rig-token``, ``x-api-key: rig-key``
and ``?tag=beta``, or it gets a 401. A call still carrying the proxy's goal param
gets a 400, as a server that validates its arguments would answer.
Run: ``python rig_upstream.py PORT``.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import parse_qs

import anyio
import uvicorn
from mcp.server.fastmcp import Context, FastMCP

TOKEN = "rig-token"
API_KEY = "rig-key"
TAG = "beta"
GOAL_PARAM = "user_goal"

server = FastMCP("rig", log_level="WARNING")


@server.tool()
def echo(text: str) -> str:
    """Echo text back."""
    return text


@server.tool()
async def slow(seconds: float) -> str:
    """Answer after ``seconds``."""
    await anyio.sleep(seconds)
    return "done"


@server.tool()
def whoami(ctx: Context) -> str:
    """The upstream session id this call arrived on."""
    request = ctx.request_context.request
    return request.headers.get("mcp-session-id", "") if request is not None else ""


async def _respond(send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": []})
    await send({"type": "http.response.body", "body": body})


class _Gate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        query = parse_qs(scope.get("query_string", b"").decode())
        if (
            headers.get("authorization") != f"Bearer {TOKEN}"
            or headers.get("x-api-key") != API_KEY
            or query.get("tag") != [TAG]
        ):
            await _respond(send, 401, b"gate")
            return
        chunks = []
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        body = b"".join(m.get("body", b"") for m in chunks)
        if _carries_goal_param(body):
            await _respond(send, 400, b"unknown argument")
            return
        replay = iter(chunks)

        async def replayed():
            return next(replay, None) or await receive()

        await self.app(scope, replayed, send)


def _carries_goal_param(body: bytes) -> bool:
    try:
        msg = json.loads(body or b"{}")
    except ValueError:
        return False
    params = msg.get("params") if isinstance(msg, dict) else None
    args = params.get("arguments") if isinstance(params, dict) else None
    return isinstance(args, dict) and GOAL_PARAM in args


if __name__ == "__main__":
    uvicorn.run(
        _Gate(server.streamable_http_app()),
        host="127.0.0.1",
        port=int(sys.argv[1]),
        log_level="warning",
    )

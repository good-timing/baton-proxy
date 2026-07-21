#!/usr/bin/env python3
"""Minimal stdio MCP backend for the ExtMCP sandbox — a couple of trivial tools.

This exists ONLY so the all-in-one sandbox (Dockerfile.sandbox) is self-contained:
gateway + Baton processor + a backend to capture against, in one `docker run`.
Swap it for your real MCP backend once capture is confirmed — the processor and
gateway config don't change.
"""

from fastmcp import FastMCP

mcp = FastMCP("toy")


@mcp.tool()
def echo(message: str) -> str:
    """Echo the message back unchanged."""
    return message


@mcp.tool()
def get_status(system: str) -> dict:
    """Return a coarse status for a named system."""
    return {"system": system, "status": "ok"}


if __name__ == "__main__":
    mcp.run()

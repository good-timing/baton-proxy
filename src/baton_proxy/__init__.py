"""baton-proxy — transparent MCP proxy.

Wraps a stdio MCP server as a subprocess, or bridges to a remote
Streamable-HTTP MCP server (`--url`); injects an annotation tool into the
handshake, and emits friction events to a baton-console.

See README.md for usage.
"""

__version__ = "0.5.4"

# The product/version token, single-sourced here so the emitter's `sdk_version`
# field and the HTTP bridge's outbound `User-Agent` header can never drift.
USER_AGENT = f"baton-proxy/{__version__}"

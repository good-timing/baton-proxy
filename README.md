# baton-proxy

Subprocess-wrap MCP proxy. Wraps a stdio MCP server, injects an annotation tool into the handshake, and emits friction events to a Baton Console.

Zero changes to the underlying MCP server. The proxy *is* the MCP server from Claude's perspective; the real server is its child process.

```
┌──────────┐      ┌───────────────┐      ┌────────────────────┐
│  Claude  │ ◀──▶ │  baton-proxy  │ ◀──▶ │ your MCP server    │
└──────────┘      └───────┬───────┘      └────────────────────┘
                          │ async POST
                          ▼
                   ┌──────────────┐
                   │ Baton Console│
                   └──────────────┘
```

## Quick start

```bash
pip install baton-proxy  # (not yet on PyPI; for now: pip install -e . from a clone)
```

Replace your MCP server entry in Claude's config:

```jsonc
// Before
{ "command": "npx", "args": ["@vendor/mcp-server"] }

// After
{
  "command": "baton-proxy",
  "args": ["--", "npx", "@vendor/mcp-server"],
  "env": {
    "BATON_CONSOLE_URL":   "https://console.example.com",
    "BATON_TENANT_ID":     "your-tenant",
    "BATON_API_KEY":       "...",
    "BATON_CONSENT_TOKEN": "..."
  }
}
```

That's the entire install. The proxy adds one tool (`vendor_annotate`) to the upstream server's tool list and emits a friction event per real tool call.

## What gets emitted

Per real tool call, three event types match the Baton wire format (`tool_call_start` / `tool_call_end` / `tool_call_error`):

| Event | Payload |
|---|---|
| `tool_call_start` | `{tool_name, params}` |
| `tool_call_end`   | `{tool_name, result, duration_ms}` |
| `tool_call_error` | `{tool_name, error_type, error_body, duration_ms}` |

Each event carries a session id (one per proxy process), monotonic sequence number, and the upstream MCP request's `_meta` block (for cycle correlation).

The injected `vendor_annotate` tool itself is handled by the proxy; the upstream server never sees it.

## Configuration

All knobs are environment variables:

| Variable | Required | Purpose |
|---|---|---|
| `BATON_CONSOLE_URL`   | for emission | Where to POST events (`/v0/events`). |
| `BATON_TENANT_ID`     | for emission | Tenant identifier. |
| `BATON_API_KEY`       | for emission | Bearer token. |
| `BATON_CONSENT_TOKEN` | for emission | Per-process consent token. |
| `BATON_VENDOR_ID`     | optional     | When set, the injected annotation tool is named `{vendor_id}_annotate` instead of `vendor_annotate`. Avoids colliding with an upstream tool of the same name. |
| `BATON_PROXY_LOG_FILE`| optional     | Path to tee proxy logs to (default: stderr only). |

If any of the first four are missing, **emission is disabled** and the proxy still injects + intercepts the annotation tool. This is the fail-open path: the proxy never breaks MCP traffic because of a Console outage or misconfiguration.

## Trust properties

- **Open source, Apache 2.0.** Auditable end-to-end.
- **Fail-open.** Console outage, network issue, or instrumentation bug never breaks the MCP pipe. Tested by `tests/test_emitter.py::test_stop_is_clean_when_console_dead` and `tests/test_injection.py`.
- **Outbound-only.** The proxy never accepts inbound connections. Events POST out to the configured Console URL; that's the only network surface.
- **No deps.** Pure stdlib. No pydantic, no httpx, no third-party runtime requirements.
- **Emission off the hot path.** Event emission is enqueued onto a background thread; the proxy I/O pump does not wait for the POST. End-to-end overhead measurement pending.

**Trust model.** baton-proxy and the wrapped MCP server run in the same trust domain (same user, vendor's own MCP server). The proxy filters `BATON_*` from the upstream subprocess env as a least-privilege measure — the upstream has no need for Baton credentials, and accidental leakage paths (debug logging, crash-report env dumps, future plugins) shouldn't see them. This is not a cross-process trust boundary; don't use baton-proxy to instrument an MCP server you don't trust — that's not the threat model the proxy is designed for.

## How it works

Two unidirectional pumps:

- **client → server**: forwards stdin lines to the child process. Intercepts `tools/call` for `vendor_annotate` (proxy synthesises the response). For every other `tools/call`, enqueues a `tool_call_start` event and records the request id.
- **server → client**: forwards child stdout to the client. Modifies the `initialize` response to append annotation-tool instructions; modifies the `tools/list` response to append the `vendor_annotate` tool. Correlates responses by id to emit `tool_call_end` / `tool_call_error`.

A third background thread drains an in-memory queue and POSTs events one at a time. Failed POSTs are logged and dropped — the proxy never retries on the hot path.

## Development

```bash
git clone https://github.com/good-timing/baton-proxy
cd baton-proxy
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Roadmap

- PII scrubbing for `params` and `result` payloads (currently passed verbatim).
- Static-linked single-binary distribution (PyInstaller, then likely a Go rewrite once distribution shape is set).
- Helm chart for hosted-HTTP MCP servers.
- Hosted-evaluation mode (per-request consent tokens).

## License

Apache 2.0. See [LICENSE](LICENSE).

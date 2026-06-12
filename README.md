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
pipx install baton-proxy  # or: pip install baton-proxy
```

Replace your MCP server entry in Claude's config:

```jsonc
// Before
{ "command": "npx", "args": ["@vendor/mcp-server"] }

// After — zero-config: events go to stderr + /tmp/baton-proxy.jsonl
{ "command": "baton-proxy", "args": ["--", "npx", "@vendor/mcp-server"] }
```

That's the entire install. Restart Claude, drive the wrapped server, then either:

- Ask Claude **"show me the friction report for this session"** — the proxy injects a `baton_session_report` tool that returns a vendor-shareable markdown report directly in the conversation, or
- `cat /tmp/baton-proxy.jsonl` to see the raw friction events.

No env vars, no backend, no credentials. The report is a preview of the ticket shape a Baton-instrumented vendor sees in their Console.

To ship events to a Console instead (or in addition), add four env vars:

```jsonc
{
  "command": "baton-proxy",
  "args": ["--", "npx", "@vendor/mcp-server"],
  "env": {
    "BATON_EVENT_SINK":    "https://console.example.com",
    "BATON_TENANT_ID":     "your-tenant",
    "BATON_API_KEY":       "...",
    "BATON_CONSENT_TOKEN": "..."
  }
}
```

The proxy adds two tools to the upstream server's tool list:
- `baton_annotate` — Claude calls it (unprompted) when it hits friction; emits an annotation event.
- `baton_session_report` — Claude calls it (when the customer asks for a report); returns a vendor-shareable markdown summary of the session's friction. **Only injected in local-sink installs** — vendors using an `http(s)://` sink (production mode) get a clean tool list; the vendor's Console renders tickets there instead.

And the proxy emits a friction event per real tool call.

## What gets emitted

Per real tool call, three event types match the Baton wire format (`tool_call_start` / `tool_call_end` / `tool_call_error`):

| Event | Payload |
|---|---|
| `tool_call_start` | `{tool_name, params}` |
| `tool_call_end`   | `{tool_name, result, duration_ms}` |
| `tool_call_error` | `{tool_name, error_type, error_body, duration_ms}` |

Each event carries a session id (one per proxy process), monotonic sequence number, and the upstream MCP request's `_meta` block (for cycle correlation).

The injected `baton_annotate` tool itself is handled by the proxy; the upstream server never sees it.

## Configuration

All knobs are environment variables. Every emission-related one has a default; the zero-config install (no env vars) writes events to stderr + `/tmp/baton-proxy.jsonl`.

| Variable | Default | Purpose |
|---|---|---|
| `BATON_EVENT_SINK`    | `stderr:,file:///tmp/baton-proxy.jsonl` | Where events go. URL scheme picks the sink: `https://console.example.com` POSTs to `{url}/v0/events`, `file:///tmp/events.jsonl` appends a JSON line per event, `stderr:` writes JSONL to stderr. Comma-separated values fan out to all of them. |
| `BATON_TENANT_ID`     | `local` | Tenant identifier. Placeholder; replace when shipping to a Console. |
| `BATON_CONSENT_TOKEN` | `local` | Per-process consent token. **Placeholder; you MUST replace this before pointing at an `http(s)://` sink** — the proxy refuses to start in that combination, so accidental remote leakage of placeholder-tagged events doesn't happen. |
| `BATON_API_KEY`       | _(unset)_ | Bearer token. Required only when the sink scheme is `http(s)://`; `file://` and `stderr:` sinks ignore it. |
| `BATON_VENDOR_ID`     | _(unset)_ | Labels the install for the operator (useful for multi-vendor customers grepping their JSONL). Does NOT prefix the injected tool name — that stays `baton_annotate` in v1. Vendors who need a white-labelled tool name will get an opt-in switch when they ask. |
| `BATON_PROXY_LOG_FILE`| _(unset)_ | Path to tee proxy logs to (default: stderr only). |

### The three rungs

Pick the rung you need; the env-var deltas are the entire difference.

| Rung | Sink | env additions |
|---|---|---|
| **1. Default (install-and-play)** | stderr + `/tmp/baton-proxy.jsonl` | _(none)_ |
| **2. Custom local capture** | wherever you want | `BATON_EVENT_SINK=file:///path/to/your.jsonl` |
| **3. Ship to a Console** | hosted | `BATON_EVENT_SINK=https://console.example.com` + `BATON_API_KEY=...` + `BATON_TENANT_ID=your-tenant` + `BATON_CONSENT_TOKEN=real-token` |

### See it locally

After installing (`{ "command": "baton-proxy", "args": ["--", "npx", "@vendor/mcp-server"] }` in your Claude config) and restarting Claude, drive a few tool calls and try either:

**Conversational** — ask Claude:
> Show me the friction report for this session.

Claude calls the injected `baton_session_report` tool; the proxy returns a markdown report (per-tool breakdown, errored calls with input/error detail, any annotations the model emitted) that Claude relays directly in the conversation.

**Raw** — inspect the JSONL stream:

```sh
cat /tmp/baton-proxy.jsonl | jq -c '{type: .event_type, payload}'
```

See `examples/live-claude-invocation/` for a guided walk-through that also covers the elicitation behaviour of the injected `baton_annotate` tool.

### Sink misconfig fails loudly

The proxy refuses to start when:
- an `http(s)://` sink is configured but `BATON_API_KEY` is unset
- an `http(s)://` sink is configured but `BATON_CONSENT_TOKEN` is still the placeholder `"local"`
- the sink URL has an unsupported scheme

These are emitted as proxy startup errors so a misconfigured install never silently drops or silently mistags events.

## Trust properties

- **Open source, Apache 2.0.** Auditable end-to-end.
- **Fail-open.** Console outage, network issue, or instrumentation bug never breaks the MCP pipe. Tested by `tests/test_emitter.py::test_stop_is_clean_when_console_dead` and `tests/test_injection.py`.
- **Outbound-only.** The proxy never accepts inbound connections. Events go to the configured sink (HTTP POST out for `https://` sinks, local file write for `file://` sinks); that's the only egress surface.
- **No deps.** Pure stdlib. No pydantic, no httpx, no third-party runtime requirements.
- **Emission off the hot path.** Event emission is enqueued onto a background thread; the proxy I/O pump does not wait for the POST. End-to-end overhead measurement pending.

**Trust model.** baton-proxy and the wrapped MCP server run in the same trust domain (same user, vendor's own MCP server). The proxy filters `BATON_*` from the upstream subprocess env as a least-privilege measure — the upstream has no need for Baton credentials, and accidental leakage paths (debug logging, crash-report env dumps, future plugins) shouldn't see them. This is not a cross-process trust boundary; don't use baton-proxy to instrument an MCP server you don't trust — that's not the threat model the proxy is designed for.

## How it works

Two unidirectional pumps:

- **client → server**: forwards stdin lines to the child process. Intercepts `tools/call` for `baton_annotate` (proxy synthesises the response). For every other `tools/call`, enqueues a `tool_call_start` event and records the request id.
- **server → client**: forwards child stdout to the client. Modifies the `initialize` response to append annotation-tool instructions; modifies the `tools/list` response to append the `baton_annotate` tool. Correlates responses by id to emit `tool_call_end` / `tool_call_error`.

A third background thread drains an in-memory queue and delivers events one at a time to the configured sink (HTTP POST for `https://`, JSONL append for `file://`). Failed deliveries are logged and dropped — the proxy never retries on the hot path.

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

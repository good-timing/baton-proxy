# baton-proxy

Transparent MCP proxy. Wraps a stdio MCP server as a subprocess, **or** bridges to a remote Streamable-HTTP MCP server (`--url`); injects an annotation tool and three intent parameters into the handshake, and emits friction events to one or more sinks (stderr, a JSONL file, or a Baton Console).

Zero changes to the underlying MCP server. The proxy *is* the MCP server from Claude's perspective; the real server is either its child process (stdio) or the endpoint it forwards to (`--url`).

```
┌──────────┐      ┌───────────────┐      ┌────────────────────┐
│  Claude  │ ◀──▶ │  baton-proxy  │ ◀──▶ │ your MCP server    │
└──────────┘      └───────┬───────┘      └────────────────────┘
                          │
                          │ async fan-out, pick any subset
                          ▼
   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
   │  stderr:        │  │  file://        │  │  Baton Console  │
   │  JSONL stream   │  │  JSONL file     │  │  (HTTPS POST)   │
   └─────────────────┘  └─────────────────┘  └─────────────────┘
```

**The docs are at [goodtiming.ai/docs.html#proxy](https://goodtiming.ai/docs.html#proxy)**: the full configuration reference, what gets emitted, the intent parameters and the sink ladder. This page is the short version.

## Quick start

```sh
pipx install baton-proxy  # or: pip install baton-proxy
```

`pipx` installs the CLI into its own isolated venv and puts `baton-proxy` on your PATH, so Claude's config can invoke it directly without env activation. Plain `pip install` works if you already manage your own Python env. Python 3.11+, pure stdlib, no third-party runtime dependencies.

Replace your MCP server entry in Claude's config:

```jsonc
// Before
{ "command": "npx", "args": ["@vendor/mcp-server"] }

// After. Zero-config: events go to stderr + /tmp/baton-proxy.jsonl
{ "command": "baton-proxy", "args": ["--", "npx", "@vendor/mcp-server"] }
```

That's the entire install. Start a new Claude session and drive the wrapped server.

For a remote server, name it with `--url` instead of a command after `--`. The two forms are mutually exclusive, and `BATON_UPSTREAM_AUTH_TOKEN` is sent upstream as a bearer token:

```jsonc
{
  "command": "baton-proxy",
  "args": ["--url", "https://mcp.example.com/mcp"],
  "env": { "BATON_UPSTREAM_AUTH_TOKEN": "..." }
}
```

The token goes in the entry's `env`, not your shell: the MCP client starts the proxy with a fixed six-name environment allowlist, so an exported value never arrives. The upstream then rejects the call and the proxy degrades to a synthetic empty tool list: a server that looks connected and offers nothing.

Either form is started by your MCP client, not by you: the proxy speaks JSON-RPC on stdin, so running it straight from a shell just waits for input.

## Where events go

`BATON_EVENT_SINK` takes a comma-separated list, and the URL scheme picks the sink: `stderr:` writes JSON Lines to stderr, `file:///tmp/events.jsonl` appends one JSON object per event, and `https://console.example.com` POSTs to `{url}/v0/events`. The default is `stderr:,file:///tmp/baton-proxy.jsonl`, so a bare install writes only to your own machine.

A misconfigured sink fails loudly at startup rather than silently dropping events. The full variable list, covering timeouts, tenant shape, the upstream token and the intent-parameter mode, is in the [configuration reference](https://goodtiming.ai/docs.html#configuration).

## Payload scrubbing

**Always on. Nothing turns it off**, not an environment variable and not a flag: the proxy constructs its scrubber unconditionally. Tool params, results and error bodies run through the same ruleset the Baton SDK ships: email, `Bearer` values, `sk-*` and `AKIA*` keys, JWTs, North-American-format phone numbers, Luhn-checked card numbers, plus force-redaction on sensitive field names.

**It is pattern matching, not a guarantee.** A name and a street address pass through untouched. Decide what your server puts in tool params and results on that basis. [What it does and does not catch](https://goodtiming.ai/docs.html#pii).

## Trust properties

- **Open source, Apache 2.0.** Auditable end-to-end.
- **Fail-open.** A Console outage, a network issue or an instrumentation bug never breaks the MCP pipe. If injection or stripping raises, the message is forwarded unmodified.
- **Outbound-only.** The proxy never accepts inbound connections. Events go to the configured sink, either an HTTPS POST out or a local file write, and that is the only egress surface.
- **Source-side scrubbing, on by default**, with the limits stated above.
- **Emission off the hot path.** Events are enqueued onto a background thread; the I/O pump does not wait for the POST.

**Trust model.** baton-proxy and the wrapped MCP server run in the same trust domain (same user, vendor's own MCP server). The proxy filters `BATON_*` out of the upstream subprocess env as a least-privilege measure: the upstream has no need for Baton credentials, and accidental leakage paths such as debug logging or crash-report env dumps should not see them. This is **not** a cross-process trust boundary: do not use baton-proxy to instrument an MCP server you do not trust.

## More

| | |
|---|---|
| [goodtiming.ai/docs.html#proxy](https://goodtiming.ai/docs.html#proxy) | The docs: configuration, what it captures, the intent parameters, the Console |
| [`docs/SPEC.md`](https://github.com/good-timing/baton/blob/main/docs/SPEC.md) | The wire protocol, in the `baton` repo. The contract a collector consumes |
| [baton-sdk](https://pypi.org/project/baton-sdk/) | The in-process alternative: capture from inside a server you own, with no proxy hop |
| [`CHANGELOG.md`](https://github.com/good-timing/baton-proxy/blob/main/CHANGELOG.md) | What has shipped |

Apache-2.0.

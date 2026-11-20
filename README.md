# baton-proxy

Transparent MCP proxy. Wraps a stdio MCP server as a subprocess, **or** bridges to a remote Streamable-HTTP MCP server (`--url`); injects a `user_goal` intent parameter into every tool, and a `baton_annotate` report tool with `BATON_ANNOTATE=on`, and emits friction events to one or more sinks (stderr, a JSONL file, or a Baton Console).

> **"Console" vs "collector".** The receiver is called the **Console** in places
> throughout this repo's docstrings, for brevity. It is just whatever HTTP
> collector you point `BATON_EVENT_SINK` at — a self-hosted ingest service, a
> hosted one, or a third-party-built one. Nothing here requires a particular
> receiver, and vendors who only need local capture can use the stderr or file
> sinks and ignore it entirely. Same disclaimer the SDK's `SPEC.md` carries.

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

### Or let the proxy edit the config

```sh
baton-proxy wrap my-server                                   # an entry already in Claude Desktop's config
baton-proxy wrap linear --url https://mcp.linear.app/mcp     # a new server that signs in with OAuth
baton-proxy wrap my-server --client code                     # ~/.claude.json; close Claude Code first
baton-proxy wrap my-server --dsn 'https://baton_pk_…'        # send events to the Console
baton-proxy unwrap my-server                                 # put the original entry back
```

Any other client (Cursor, VS Code, Windsurf, Codex and so on): `wrap-entry` reads one server entry as JSON on stdin and prints the wrapped entry, and edits no file. Write the result into that client's own config yourself, or let your agent do it, and keep the original to undo:

```sh
echo '{"command": "npx", "args": ["-y", "@vendor/server"]}' | baton-proxy wrap-entry --dsn '<dsn>'
baton-proxy wrap-entry --url https://mcp.linear.app/mcp --dsn '<dsn>' < /dev/null   # a new remote server
```

The printed entry holds the DSN's key and any bearer token in full, because it is what goes into the config.

`wrap` writes absolute paths, because Claude Desktop starts servers with a short `PATH`. A server that signs in with OAuth runs through `npx -y mcp-remote`, which opens a browser on first start and keeps the token itself, so Node.js must be installed. A connector added in the Claude app is not in any config file: `--url` adds a separate local entry, so turn the app's connector off. To send events to a Console, add a server on its Settings page and pass its DSN: `--dsn 'https://baton_pk_…@baton.goodtiming.ai/ten_…/<server>'`. The DSN carries the address, the key and both ids. Without `--dsn`, events stay on this machine. `--dry-run` shows the entry first.

## Serve a URL in front of a remote server

For a harness that wants a URL and cannot start a process, `baton-proxy listen` serves Streamable HTTP in front of a remote MCP server and captures every session.

```sh
baton-proxy listen --listen 127.0.0.1:8710 --url https://mcp.example.com/mcp
```

`--listen` takes an IPv4 address or a host name, and a port.

To send events to a Console, add a server on its Settings page and pass its DSN with `--dsn`. Events then go to the Console and to the local sinks `BATON_EVENT_SINK` names, which are stderr and `/tmp/baton-proxy.jsonl` unless you set it. A remote sink next to a DSN is refused, because the DSN's key and consent would apply to it too. Without `--dsn`, events go only where `BATON_EVENT_SINK` says.

Every header the caller sends is forwarded unchanged, except the connection-level ones, `Host`, `Content-Length`, `Accept`, `Content-Type`, `Accept-Encoding` and the session id. The listener adds itself to `Via`. Every request goes to the `--url` address itself, whatever path the client was registered with, and the client's query is added to it. So `http://127.0.0.1:8710/mcp?tag=beta` reaches `https://mcp.example.com/mcp?tag=beta`. Header values are never written to an event. So the listener needs no credential of its own. `BATON_UPSTREAM_AUTH_TOKEN` fills in for callers that send none, and the listener refuses it on a non-loopback address because anyone who reached the port would act as you.

The server's answer goes back with its own headers, such as `Set-Cookie`, minus the ones that describe its original body and its CORS grants. A refusal such as a 401 goes back to the caller with its status, its body and the headers above. A redirect is answered with a 502, because following it would leave the overlay and the capture behind. A request with an `Origin` header is refused unless `BATON_LISTEN_ALLOWED_ORIGINS` lists it. The listener sends no CORS headers, so a browser page cannot call it even from an allowed origin.

Each client gets its own `Mcp-Session-Id`, so clients stay apart even when the upstream has no sessions. Each client session is one Baton session, and its open calls are closed in that session when it ends. An answer the server streams is passed on as it arrives, so a question the server asks during a call reaches the client in time. The `--url` form without `listen` waits for the whole answer, so use `listen` for servers that ask questions mid-call. The proxy gives up on a server that sends nothing for 60 seconds, which includes a server waiting for the answer to its question. Set `BATON_UPSTREAM_TIMEOUT` to more seconds if those answers can take longer, knowing a dead server then takes that long to notice too. The listener does not offer the standing server-to-client stream.

Even with no overlay the agent does not see the server exactly as it is. Every tool gains a `user_goal` parameter, which the proxy records and strips before the call goes on. It is listed as required unless `BATON_INTENT_PARAM=optional`, and a tool that already declares its own `user_goal` is left as it is. The server's instructions also gain a short paragraph asking for it. Compare two runs through the listener, with and without an overlay, to see what the overlay alone changes.

At startup the listener prints one line naming the overlay (or `none`), what the proxy adds and where events go, and it warns if the server does not answer, answers with a server error or redirects. The first complete tool listing logs how many tools the overlay changed, and any action it could not apply is logged as an error. Every answer carries a `Baton-Overlay` header holding the start of the overlay's hash, as the startup line shows it, or `none`. So a client can tell which run it is in.

To confirm a running listener shows what it should, compare it with the server using the same headers. Pass `--overlay` with the file the listener was started with, if any.

```sh
baton-proxy check --listener 'http://127.0.0.1:8710/mcp?tag=beta' \
  --url 'https://mcp.example.com/mcp?tag=beta' --header 'x-api-key: ...'
```

`BATON_UPSTREAM_AUTH_TOKEN` fills `Authorization` when no `--header` sets it. The check applies the overlay and the proxy's own parameter to the server's list, the way the listener does, and prints `same` when the result matches. Otherwise it names the tools that are changed, missing or added. It exits 1 for any difference, including a listener running a different overlay, and 2 when either side cannot be listed.

The listener does not pass OAuth sign-in through. A client that discovers how to sign in by asking the server cannot do it through the listener, so send a fixed token in a header instead, or set `BATON_UPSTREAM_AUTH_TOKEN`.

## Overlays

An overlay changes what the agent sees without touching the server. The file holds one overlay in the same shape the Console stores. It can add to or replace a tool's description or a parameter's description, hide a tool, hide a parameter the tool does not require, list a parameter as required, and add to the server's instructions. Actions apply in order, and an added text starts a new paragraph. Pass the file with `--overlay PATH`, which is the only way to load one. `BATON_OVERLAY` is ignored with a warning, so a leftover variable cannot change a run.

```json
{
  "actions": [
    { "target": { "tool": "create_fields" }, "append": "Get the record type id from get_workspace first." },
    { "target": { "tool": "create_fields", "param": "record_type_id" }, "replace": "The id get_workspace returns, unchanged." },
    { "target": { "tool": "create_fields", "param": "legacy_mode" }, "remove": true },
    { "target": { "tool": "debug_dump" }, "remove": true },
    { "target": { "instructions": true }, "append": "..." }
  ]
}
```

JSON always works. YAML needs `pip install 'baton-proxy[overlay]'`. A malformed file stops the proxy at startup. An action the tool list cannot take, such as a tool or parameter the server does not list, is logged as an error once the server lists its tools. A required parameter is listed as required but never enforced. An overlay file cannot change Baton's own text (`agent_text`). An overlay saved in the Console can. A call to a hidden tool is answered by the proxy and never reaches the server. Events still record the server's own surface, and the surface snapshot carries the overlay's hash.

## Console settings

With `--console-settings` the proxy follows the settings and overlays saved in the Console, so a change reaches a running proxy without a restart. It reads them from the Console the events go to, so it needs `--dsn`, or one `https://` event sink with `BATON_API_KEY`, `BATON_TENANT_ID` and `BATON_VENDOR_ID`. It works with `listen`, `--url` and a stdio wrap, and `baton-proxy wrap --dsn ... --console-settings` writes it into a client's config. It cannot be used with `--overlay`.

```sh
baton-proxy listen --listen 127.0.0.1:8710 --url https://mcp.example.com/mcp \
  --dsn 'https://baton_pk_...@baton.goodtiming.ai/ten_.../srv-...' --console-settings
```

- The proxy reads the settings at startup and then every minute. A stdio or `--url` proxy waits up to 5 seconds for the first read, because its client lists tools once. `listen` does not wait. Until the first read, and whenever the Console holds none, it runs on its own settings. If a read fails it keeps the last settings it read, and a refused read is logged.
- Each session is matched to an audience by the name its client declares in the handshake. An audience that matches by label or share never matches here, because the proxy knows no person behind a session.
- A session takes its audience's goal, reports and overlay each time it lists tools. Its calls follow the list it was given until it lists again. The server's instructions and the server overlay are set when a session starts.
- Under Console settings the report tool is on unless `BATON_ANNOTATE=off`. The Console can take it away from some callers but cannot add it.
- With results off for an audience, its calls' events carry no result, and an error that came from the server's answer carries no text. They say so with `result_capture`. Errors the proxy writes itself keep their text. The messages of `tool_list_error` and the resource and prompt error events are still sent in full.
- Every event a session makes records the settings it ran under in `capture`. Every answer from `listen` carries `Baton-Overlay: console`, and `baton-proxy check` does not compare such a listener.

## What the listener records

Each client session is one Baton session with its own `session_id`, and `agent_runtime` names the client from its handshake. Every event is one JSON object with `event_type`, `session_id`, `sequence_number`, `captured_at` and a `payload`.

- `surface_snapshot` holds the tools and instructions the server offered. With an overlay loaded, its full hash is at `seam_augmentations.overlay.hash`, and the `Baton-Overlay` header shows the start of it.
- `tool_call_start` holds `tool_name`, the `params` the server received and the agent's `user_goal` as `call_intent`.
- `tool_call_end` holds the `result` and `duration_ms`. `tool_call_error` holds an `error_type`, and the `result` too when the tool itself returned the error. Each pairs with its start by the `call_id` beside `session_id`.

A call still open when its session ends, or when the listener stops, is closed with an error, so every start has an outcome. Header values and query strings are never recorded. To join events to a harness's own results, match each case's time window to `captured_at` and its calls in order.

## Where events go

`BATON_EVENT_SINK` takes a comma-separated list, and the URL scheme picks the sink: `stderr:` writes JSON Lines to stderr, `file:///tmp/events.jsonl` appends one JSON object per event, and `https://collector.example.com` POSTs to `{url}/v0/events`. The default is `stderr:,file:///tmp/baton-proxy.jsonl`, so a bare install writes only to your own machine.

A misconfigured sink fails loudly at startup rather than silently dropping events. The full variable list, covering timeouts, tenant shape, the upstream token and the intent-parameter mode, is in the [configuration reference](https://goodtiming.ai/docs.html#configuration).

## Payload scrubbing

**Always on. Nothing turns it off**, not an environment variable and not a flag: the proxy constructs its scrubber unconditionally. Tool params, results and error bodies run through the same ruleset the Baton SDK ships: email, `Bearer` values, `sk-*` and `AKIA*` keys, JWTs, North-American-format phone numbers, Luhn-checked card numbers, plus force-redaction on sensitive field names.

**It is pattern matching, not a guarantee.** A name and a street address pass through untouched. Decide what your server puts in tool params and results on that basis. [What it does and does not catch](https://goodtiming.ai/docs.html#pii).

## Trust properties

- **Open source, Apache 2.0.** Auditable end-to-end.
- **Fail-open.** A Console outage, a network issue or an instrumentation bug never breaks the MCP pipe. If injection or stripping raises, the message is forwarded unmodified.
- **Outbound-only, except `listen`.** A wrapped server's proxy never accepts inbound connections. `baton-proxy listen` accepts them on the one address you pass it. Events go to the configured sink, either an HTTPS POST out or a local file write. With `--console-settings` the proxy also reads its settings from that same Console. Those are the only egress surfaces.
- **Source-side scrubbing, on by default**, with the limits stated above.
- **Emission off the hot path.** Events are enqueued onto a background thread; the I/O pump does not wait for the POST.

**Trust model.** baton-proxy and the wrapped MCP server run in the same trust domain (same user, vendor's own MCP server). The proxy filters `BATON_*` out of the upstream subprocess env as a least-privilege measure: the upstream has no need for Baton credentials, and accidental leakage paths such as debug logging or crash-report env dumps should not see them. This is **not** a cross-process trust boundary: do not use baton-proxy to instrument an MCP server you do not trust.

## More

| | |
|---|---|
| [goodtiming.ai/docs.html#proxy](https://goodtiming.ai/docs.html#proxy) | The docs: configuration, what it captures, the intent parameters, the Console |
| [`SPEC.md`](https://github.com/good-timing/baton-spec/blob/main/SPEC.md) | The wire protocol, in the `baton-spec` repo. The contract a collector consumes |
| [baton-sdk](https://pypi.org/project/baton-sdk/) | The in-process alternative: capture from inside a server you own, with no proxy hop |
| [`CHANGELOG.md`](https://github.com/good-timing/baton-proxy/blob/main/CHANGELOG.md) | What has shipped |

Apache-2.0.

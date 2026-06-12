# Live Claude elicitation test

**Goal:** prove a real Claude session — not a scripted JSON-RPC client —
drives the proxy correctly, **and calls the injected annotation tool on its
own** when an upstream tool errors.

This is the production-equivalent of the scripted test in `tests/`: the
scripted test forces the annotation call; this manual procedure measures
whether a real Claude session calls it organically after seeing an error
from the upstream `boom` tool.

## What the proxy injects

When you point Claude Code at `baton-proxy --` wrapping any MCP server, the
proxy advertises one extra tool — `<vendor_id>_annotate` — to Claude, and
appends a `MUST call this tool on friction` clause to the server's
`instructions`. This procedure measures whether that nudge works in
practice.

## Run

### 1. Register

```sh
bash examples/live-claude-invocation/setup.sh
```

The proxy wraps `tests/fixture_server.py`, which exposes three tools:
- `echo` — returns its input (success path)
- `boom` — always errors (error path)
- `e2eproxy_annotate` — the proxy-injected annotation tool

### 2. Restart Claude Code

`claude mcp add` only takes effect in new sessions. Quit and relaunch.

### 3. In the new session — paste these one at a time

**a. enumeration**
> What MCP tools are available from the `baton-proxy-example` server?

Expect: `echo`, `boom`, `e2eproxy_annotate`.

**b. forward success**
> Call the echo tool from baton-proxy-example with text="hello".

Expect: response contains `Echo: hello`.

**c. forward error + elicitation**
> Call the boom tool from baton-proxy-example with no arguments.

Expect: error response. **Then watch what Claude does next.** Don't prompt
for annotation — the point is to see whether it calls `e2eproxy_annotate`
on its own.

### 4. Capture session_id (optional — only if you wired up ingest)

The proxy writes a `session=<uuid>` line every time it's spawned (including
`claude mcp list` health checks). Grab the **most recent**:

```sh
grep -oE 'session=[0-9a-f-]{36}' /tmp/baton-proxy-example.log | tail -1 | cut -d= -f2
```

If you want to be sure you're reading the post-validation session, truncate
the log after restarting Claude Code but before step 3:

```sh
: > /tmp/baton-proxy-example.log
```

### 5. Verify ingest round-trip (optional — only if BATON_CONSOLE_URL is set)

```sh
SESSION_ID=<paste from step 4>
curl -sS -X POST "$BATON_CONSOLE_URL/v0/escalate" \
  -H "Authorization: Bearer $BATON_API_KEY" -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\"}"
```

Expect: HTTP 201, body `{"ticket_id": ..., "ticket_url": ...}`.

### 6. Unregister

```sh
bash examples/live-claude-invocation/unregister.sh
```

## What to record

Mark ✓ / ✗ + a one-line excerpt of Claude's actual output for each:

- **enumeration** — did Claude see `e2eproxy_annotate` in tools/list?
- **forward success** — did `echo` round-trip?
- **forward error** — did Claude see the upstream `boom` error?
- **elicitation** — did Claude call `e2eproxy_annotate` without being asked? *(the key data point)*
- **session stitch** (if you ran step 5) — did `/v0/escalate` return a `ticket_id`?

Also note any prompts you had to add beyond steps a/b/c (e.g. "did you mean
to annotate?"). Manual prompting to get elicitation = elicitation failed.

See `result.md` for an example of one such run.

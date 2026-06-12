# Live Claude elicitation test

**Goal:** prove a real Claude session — not a scripted JSON-RPC client —
drives the proxy correctly, **and calls the injected annotation tool on its
own** when an upstream tool errors. As a bonus, see the friction events
the proxy emits land in a local JSONL file you can `cat`.

This is the production-equivalent of the scripted test in `tests/`: the
scripted test forces the annotation call; this manual procedure measures
whether a real Claude session calls it organically after seeing an error
from the upstream `boom` tool.

## What the proxy does

When you point Claude Code at `baton-proxy --` wrapping any MCP server,
the proxy:

1. Injects an extra tool `<vendor_id>_annotate` into the upstream's
   tools/list, advertised to Claude with a "MUST call on friction" nudge.
2. Forwards every other tool call upstream verbatim.
3. Emits structured friction events (`tool_call_start` /
   `tool_call_end` / `tool_call_error` / `annotation`) to the configured
   sink — by default here, a local JSONL file.

This run measures whether the elicitation nudge works (does Claude call
the annotate tool unprompted after an error?) **and** lets you see the
event stream directly.

## Run

### 1. Register

```sh
bash examples/live-claude-invocation/setup.sh
```

Defaults to emitting events to `file:///tmp/baton-proxy-example.jsonl`.
To tee to stderr or POST to a real Console instead, pre-set
`BATON_EVENT_SINK` (see comments in `setup.sh`).

The proxy wraps `tests/fixture_server.py`, which exposes:
- `echo` — returns its input (success path)
- `boom` — always errors (error path)
- `baton_annotate` — the proxy-injected annotation tool

### 2. Restart Claude Code

`claude mcp add` only takes effect in new sessions. Quit and relaunch.

### 3. In the new session — paste these one at a time

**a. enumeration**
> What MCP tools are available from the `baton-proxy-example` server?

Expect: `echo`, `boom`, `baton_annotate`.

**b. forward success**
> Call the echo tool from baton-proxy-example with text="hello".

Expect: response contains `Echo: hello`.

**c. forward error + elicitation**
> Call the boom tool from baton-proxy-example with no arguments.

Expect: error response. **Then watch what Claude does next.** Don't prompt
for annotation — the point is to see whether it calls `baton_annotate`
on its own.

### 4. Inspect the event stream

```sh
cat /tmp/baton-proxy-example.jsonl
```

Each line is one event envelope. You should see:
- `tool_call_start` + `tool_call_end` for the `echo` call (3b)
- `tool_call_start` + `tool_call_error` for the `boom` call (3c)
- `tool_call_start` + `annotation` for `baton_annotate` *if and only
  if* Claude elicited it (3c — the key data point)

Pretty-print with `jq`:

```sh
jq -c '{type: .event_type, payload}' /tmp/baton-proxy-example.jsonl
```

If you set a tee sink (`BATON_EVENT_SINK="stderr:,file://..."`) you'll
have also seen these stream in real time on the proxy's stderr — handy
for live debugging.

### 5. (Optional) Verify ingest round-trip against a Console

If you set `BATON_EVENT_SINK=https://your-console/` instead of (or
alongside) the file sink, you can also stitch a session id back to a
ticket via the Console's escalation endpoint:

```sh
SESSION_ID=$(grep -oE 'session=[0-9a-f-]{36}' /tmp/baton-proxy-example.log | tail -1 | cut -d= -f2)
curl -sS -X POST "$BATON_EVENT_SINK/v0/escalate" \
  -H "Authorization: Bearer $BATON_API_KEY" -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\"}"
```

### 6. Unregister

```sh
bash examples/live-claude-invocation/unregister.sh
```

## What to record

Mark ✓ / ✗ + a one-line excerpt of Claude's actual output for each:

- **enumeration** — did Claude see `baton_annotate` in tools/list?
- **forward success** — did `echo` round-trip?
- **forward error** — did Claude see the upstream `boom` error?
- **elicitation** — did Claude call `baton_annotate` without being asked? *(the key data point)*
- **event stream** — does the JSONL file show the expected event sequence?
- **session stitch** (if you ran step 5) — did `/v0/escalate` return a `ticket_id`?

Manual prompting to get elicitation = elicitation failed. Note any prompts
beyond steps a/b/c.

See `result.md` for an example of one such run.

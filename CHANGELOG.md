# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.5] — 2026-09-05

### Changed
- **`kit.py upload` is gated on the credential file rather than on who types it.** In 0.5.4 the agent was told never to run it and the person typed it themselves; it now refuses without `upload.json`, which we email and only the person can save, and the agent runs the command once they say it is in place.
- **The upload tail says where to sign in and what will arrive there.** It prints the `console_url` from the credential file with `/auth/email` appended, because the console's front page offers only Google sign-in, and it names the address the workspace was set up with.
- **The `delivered` row is now labelled `sent`.** A 201 says the console accepted the request, not that a row exists, and the old word said the second thing.
- **`receipt` offers upload first with the email path under it**, instead of two send paths of equal weight, and states the offer only when something was captured.
- **The note `setup` prints about how the trial ends no longer names a send path.** Which path applies is not known at setup time, so `receipt` states it when there is something to send.
- **`try/PROMPT.md`, `try/CLAUDE.md` and `try/SECURITY.md` were rewritten.** The security detail is opt-in and asked once by the paste, the paste checks out the latest release tag, and the trial is stated as one to run against a non-production server.
- **`try/SECURITY.md` §4 names `console_url`** as the destination of the POST, so a reviewer can read the host off the file they were sent.
- **`try/CLAUDE.md` tells the agent to open the capture with `less`** and to relay the `tool calls` and `tool definitions` rows apart. macOS has no handler for `.jsonl`, and an agent that joined the two rows reported calls to tools that were never called.

### Removed
- **The `secrets redacted` row and its Luhn note left `receipt`.** The count was card-shaped numbers rather than findings. `try/SECURITY.md` §6 still carries the measurement, for the reader it was written for.
- **The paragraph telling a person their file had not left the machine left `receipt`.**
- **"Delivered is not the same as stored" left the upload output and `try/upload.py`'s docstring.** The same fact is stated as "a 201 is not a row" where a reviewer looks for it.
- **The upload output no longer names an identity provider.** What reaches the person is a six-digit code either way, and the console names its own sign-in method.

### Fixed
- **`__version__` was 0.5.4 in a checkout tagged v0.5.5**, so captured events carried `sdk_version: baton-proxy/0.5.4` under that tag. `pyproject.toml` reads the version from that file, so the bump is the whole change.

## [0.5.4] — 2026-09-03

### Added
- **`BATON_PROACTIVE=on|off`, defaulting to `off`** — ported from baton-sdk's `VendorConfig.proactive_mode` (SPEC §13 2026-08-10b, §14 parity). It selects four legs together: the instructions head, the pre-call "BEFORE invoking any tool … you MUST call" paragraph, the annotation tool's description lead, and a handler that refuses an agent-filed pre-call annotation. It never removes the annotation tool — suppressing that also loses the reactive `feature_gap`, which is the product signal — and never touches the proxy's own synthesised proactive, so one turn-opener per session survives in both modes. Reactive clauses are byte-identical across modes. **The default ships `off`, matching the SDK**, on a live verification run: the pre-call paragraph does not ADD intent, it MOVES it — with the paragraph rendered the agent stated its expectation once in the annotation and omitted `expected_result` from all 5 subsequent calls, where a session without it carried 3/3, while `user_goal` held 8/8 in both on the schema advertisement alone. It also buys 277 of 1,236 chars against Claude Code's ~2,087-char `instructions` cap on a field the proxy APPENDS to, removes one approval prompt from inside the user's work window, and bought no friction — three real sessions with the paragraph rendered filed zero friction signals. **Consumer consequence, and it is the cost of this release:** agent-authored proactive annotations supply ~54% of turn boundaries on real traffic, and no time-gap threshold substitutes. Under the new default only the proxy's own synthesised proactive remains, so consumers SHOULD derive units from `call_workflow` transitions rather than from proactive annotations. That field carried on 8 of 8 calls in the verification run, so the replacement signal is flowing before the boundary it replaces was removed.
- **`overall_task`, a third injected param, riding every `tool_call_start` as `call_workflow`.** The proxy injected two params where baton-sdk injects three; the missing one is the task-label grouping key, so proxy-captured sessions emitted no label at all and a consumer grouping by task fell back to per-call intent text, which rewords freely and shatters one task into several. Mirrors the SDK exactly: the description is byte-identical to the SDK's (verified against source), the value is stripped before the start event is built so `params` stays the vendor-visible arguments, it rides EVERY start event rather than only the first — exact-string continuity across calls is the mechanism — and on the synthesised proactive it lands on `workflow`. `surface_hash` is unaffected: the snapshot records the vendor-true surface before injection. `seam_augmentations.intent_param.names` was already a plural list, so the third name extends it without a shape change.
- **`agent_runtime` now names the client, not the transport.** Every event carried `agent_runtime: "mcp-proxy"` unconditionally, so a console reading the field back learned that Baton was in the path and nothing about the app the person was working in. Two signals now outrank the constant, which stays as the fallback: the MCP handshake's `clientInfo.name`, latched on the emitter at `initialize`, and per-event `_meta` via `detect_agent_runtime`, whose rules match the SDK's so both sensors store the same token. The latch is the load-bearing one — `surface_snapshot` is sequence 1 and carries no `_meta`, and console consumers that ask a session what it ran in read its FIRST event. Per-event outranks the latch, which degrades correctly for a hosted adapter multiplexing clients. An unrecognised client passes through as sent rather than being mapped to a guess.

### Changed
- **`intent_param_mode` defaults to `required`** (was `optional`), safe precisely because of what the word means here: `required` appends `user_goal` to a tool's ADVERTISED required list and validates nothing, and the param is stripped before forwarding, so no call can fail for omitting it and the wrapped server never learns the difference. A wrapper that refused a customer's call to collect a telemetry string would be changing how the wrapped server behaves — the one thing it promises not to do. The number this exists to move is 89%: 1,465 of 1,644 real customer calls carried `user_goal` under `optional` on 0.5.2 (Aug 11–14). Asserted end to end: a call omitting `user_goal` entirely is driven through the real proxy subprocess on both transports and is served, with `call_intent: null` on the start event.
- **`BATON_INTENT_PARAM=off` is retired.** The injected params are the intent channel and they are stripped before forwarding, so a deployment that turns them off is a passthrough that records what happened with no record of why. The way to stop the injection is to stop wrapping the server. **The value is ignored with a warning rather than rejected**, because of who is likely to set it: `try/SECURITY.md` documented it as the way to disable injection, so raising would stop an MCP server from starting because its operator did exactly what our own security page told them to. It coerces to the current default (`required`); a value that was never valid still raises. `seam_augmentations.intent_param.mode` therefore never reports `off` from the proxy. **This is a deliberate producer divergence** — baton-sdk and baton-ts keep `off`, because their operator owns the server being instrumented.
- **`BATON_VENDOR_ID` is no longer required at startup; it defaults to the `local` placeholder, and the hard failure moves to the remote sink.** `Config.from_env()` raised without it, which meant the zero-config install did not degrade to an unlabelled event stream — it did not start. The proxy IS the MCP server from the client's perspective, so the wrapped server vanished from the client entirely, and the published quick start (wrap the command, set nothing) was the exact shape that triggered it, on five documentation surfaces. Nothing reading the label locally needs it to be true; it exists so an operator can grep their own JSONL, and `local` is honest there, matching `BATON_TENANT_ID` and `BATON_CONSENT_TOKEN` which have defaulted that way all along. The Console is the case that actually costs something — it buckets friction BY vendor, so a stream labelled `local` files rows under a vendor nobody owns — so `Emitter.start()` now refuses an http/https/s3 sink while the placeholder is in place, mirroring the existing consent guard exactly. **Not breaking for any install that already sets the variable**, which is every real path: `scan` sets it from the config entry name, and the Console's onboarding recipes and local-setup page all emit it.
- **The annotation tool's agent-facing params are renamed to match the injected ones**: `intent`/`expected_outcome`/`workflow` become `user_goal`/`expected_result`/`overall_task`. The same things were named differently depending on which surface the agent happened to be looking at, and the two task-label descriptions asked for opposite things while feeding one slot. **Agent-facing only — the wire keys are unchanged** (`intent`, `expected_outcome`, `workflow`), so stored events, the Console and the kit's receipt are untouched. `overall_task` gains the wording a scored experiment selected, including the repeat-verbatim clause the annotation field never carried; task grouping is exact-string, so that clause is the mechanism rather than advice.

### Fixed
- **The injected `user_goal` described itself as `OPTIONAL.` while the schema advertised it as required.** `intent_param_mode="required"` appends `user_goal` to each tool's advertised `required` list; the description shipping inside that same schema still opened `OPTIONAL.`, so the model was handed both claims and neither was reliably the one it read. Since `required` is now the default (0.5.3 shipped `optional`), this would have gone out as the default behaviour — on the one lever the mode has for moving the 89% fill rate it exists to move. The leading label now tracks the mode; the sentence after it is byte-identical across modes, because that text is measured and the mode is not a licence to reword it. `expected_result` and `overall_task` are never escalated in either mode, so their `OPTIONAL.` was true and is unchanged. No wire change: the description is advertisement only, nothing validates the param, and it is still stripped before forwarding.
- **A client disconnect never reached the upstream, so shutdown never ran.** `_pump_client_to_server` returned on stdin EOF without closing the UPSTREAM's stdin, so a healthy server sat on a pipe nobody would write to again and `child.wait()` never returned. Everything in `run_proxy`'s shutdown hangs off that wait — `drain_pending`, which gives every in-flight `*_start` a matching end, and `emitter.stop()`, which flushes the queue — so the client's SIGTERM landed seconds later with the last events unwritten. Silent no-emit at the one moment nobody is watching. The original code assumed shutdown always begins upstream-side; the commoner direction by far is the client quitting. Measured: the test suite runs in 13s with this and 192s without.
- **The upstream that ignores its own stdin EOF re-created the same hang one hop down.** `child.wait()` is now bounded, but only from the moment the CLIENT is gone — a grace clock running from startup would SIGTERM a healthy upstream seconds into a session meant to last hours. The grace is 1s and small on purpose: everything after the wait must finish before the client's own SIGTERM arrives. Escalation is TERM then KILL, armed by the client pump on its way out, so a live session arms no clock at all and the main thread does one blocking wait rather than polling. The earlier polling shape cost ~20 wakeups/sec per wrapped server, continuously.
- **The escalation signalled the direct child only, and reported our own SIGTERM as the upstream's verdict.** Wrapped servers are routinely launched through something that is not the server (`sh -c`, `npx`, `docker run`); a wrapper that does not forward SIGTERM dies while the real server keeps running and holds the stdout pipe open, leaking a process per session — measured at 3.24s to shut down against 1.22s for a direct child. The child now gets its own session and the whole group is signalled. `result` masks the signal we sent, and only that: a negative rc we did not cause, and every non-zero exit the upstream reached on its own, still travel. Previously a signalled child reported -15 and the shell reported 241, so for any server that ignores stdin EOF the client logged a crash on every clean exit. **Trade to know about:** `start_new_session=True` means the upstream no longer shares our process group, so a Ctrl-C in an interactive terminal reaches the proxy and not the server. MCP clients do not signal the group — they close stdin, then SIGTERM our pid — so the path that matters is unaffected.
- **Signalling the upstream's process group could signal our own.** `os.killpg` was called on `os.getpgid(child.pid)` with nothing checking whose group that is. `start_new_session=True` is what makes the child's group its own, so the code read as safe by construction — but the two live in different places and only one is load-bearing at the moment of the signal. Any state where the child ends up in our group turns shutdown into a SIGKILL of the proxy, the MCP client that launched it, and whatever else shares the terminal. The group is now signalled only once it is confirmed to be a different group, and declining the group still signals the direct child.
- **The shutdown close only ran on the ordinary way out of the pump loop.** It sat after `for line in sys.stdin:`, so it ran on EOF and on nothing else. Two other exits are reachable from the client side: `sys.stdin` decodes strict UTF-8, so a single non-UTF-8 byte raises `UnicodeDecodeError` out of the `for` itself, and `json.loads` on deeply nested input raises `RecursionError`, which `except json.JSONDecodeError` does not catch. Either kills the pump with the upstream's stdin still open, reaching the same unflushed-events shutdown through a different door — and an abrupt exit is when it matters most, because that is when there is something in the queue. A `finally` around the loop covers every exit.
- **Under `proactive_mode="off"` the agent's first instruction was to call `baton_annotate` "again"** with nothing to refer back to, while the tool description one field over said "Do NOT call it before a tool call". The word is now a token rendered only alongside the BEFORE paragraph it points at.
- **The `BATON_INTENT_PARAM='off'` coercion warning was emitted before logging was configured.** `_bootstrap` calls `Config.from_env()` and only then `_configure_logging`, so the warning went out through `logging.lastResort` — stderr only, unformatted, never teed to `BATON_PROXY_LOG_FILE`. Coercing instead of raising is defensible only because the operator is told, and an operator who checks the configured log file was told nothing. `from_env` now collects into `Config.startup_warnings` and the bootstrap drains it after logging is up.
- **Two ways past `proactive_mode="off"`.** `signal_type: ""` passed a gate keyed on `is None` and was enqueued AND confirmed as recorded proactive intent — the exact annotation the mode exists to refuse, filed anyway. And the refusal text ended "— and set signal_type", which reads as a repair instruction: an agent whose narration was refused satisfies it by re-sending with `other`. Nothing validates the field, so one invented word becomes a friction count someone acts on. The gate now takes the advertised enum in BOTH modes, and the refusal names the enum without proposing a substitute.
- **The `FileSink` hardening raised the one exception its handler could not catch.** `os.O_CLOEXEC` and `os.fchmod` are Unix-only; on Windows both are `AttributeError` and the handler beside them caught `OSError`, so a sink that promises availability-first raised out of `FileSink.__init__`, through an unguarded `emitter.start()`, and killed the wrapped MCP server at bootstrap. Windows now ends up unhardened and warned, which is the posture the docstring already states, instead of dead.

### Security
- **The events file was world-readable.** `state.json` was 0600 by a deliberate hardening because it holds a copy of an env block; `events.jsonl` was left at the default umask — and that is the file holding every tool call's full arguments and full results, the business data `SECURITY.md` §6 says plainly is NOT scrubbed. On a shared box the credential was protected and the corpus was published. Fixed in `FileSink` rather than in the trial kit, so every file-sink user gets it. `os.open` sets the mode only when it CREATES, so the chmod after it is not redundant: a file an earlier version left at 0644 stays there without it, and appending is exactly when it starts holding payloads.
- **`FileSink` sets 0600 with `fchmod` on the descriptor rather than `chmod` on the path.** The default sink is a fixed name in a world-writable directory, so on the shared box this hardening is FOR, the path can be a symlink another account planted between the open and the chmod; the descriptor names the file we actually opened. `O_CLOEXEC` stops the handle riding into the wrapped server. A failure to harden logs rather than raises, since `emitter.start()` is unguarded and raising would kill the proxy at bootstrap.
- **`scan`'s remote refusal printed the entry's URL verbatim, and for a large share of remote MCP servers that URL IS the credential.** Zapier and Composio put the token in the path, `?key=` is just as common, and userinfo is the third vector. The message goes to a terminal and is the kind of thing someone pastes into a support thread. It is now reduced to scheme and host, so the host still identifies which entry was refused and the secret does not travel.

### Notes
- No wire-format break. `call_workflow` is additive and already part of the shared `baton-spec` schema; `agent_runtime` changes values, not shape. The two default flips (`intent_param_mode` → `required`, `proactive_mode` → `off`) change captured behaviour on the next restart of an existing wrap — see the `BATON_PROACTIVE` entry for the turn-boundary consequence.


## [0.5.3] — 2026-08-13

### Added
- **`call_expected` on every `tool_call_start`.** `expected_result` is injected into every tool's schema and stripped from every call, but the value only ever reached a consumer through the session's first synthesised proactive annotation — making a per-call param into a per-session fact, and attaching whatever expectation the session happened to *open* with. Sessions commonly open with a docs or list read that states no expectation, so the calls doing the real work contributed nothing and inherited nothing. `enqueue_tool_call_start` now takes `call_expected` and writes it as a sibling of `params`, matching `call_intent`. The key is **omitted** when the param was not filled — "stated no expectation" and "stated an empty one" are different claims. The once-per-session annotation gate is unchanged; it governs annotations, not values.

### Changed
- **`intent_source` keys on either injected param, not on `user_goal` alone.** An agent may fill one without the other, and an expectation arriving with no goal is still injected-param capture. Matches baton-sdk, which keys on any injected param.

### Notes
- Additive on the wire; `call_expected` is already part of the shared `baton-spec` schema. The vendored submodule pin advanced to the schema revision carrying it — the conformance test failed loudly on the new key beforehand, which is the intended behaviour of `additionalProperties: false`.

## [0.5.2] — 2026-08-11

### Changed
- **Goal-param injection renamed to `user_goal` / `expected_result`.** The proxy previously injected a single namespaced `baton_intent` param; it now injects the same two vendor-neutral params as baton-sdk (SPEC §13), with per-param dispositions tracked independently in the registry and the shared `seam_augmentations.intent_param` shape (plural `names: list[str]`). `expected_result` additionally feeds the synthesised proactive annotation's `expected_outcome`, previously only reachable via a real annotate call. Straight rename, not additive — no consumers depended on the old param name; the wire fields (`call_intent`, `intent_source`) are unchanged.

### Added
- Cross-repo envelope conformance test against the shared `baton-spec` schema (vendored as a git submodule; dev/test only, no runtime effect).

## [0.5.1] — 2026-08-05

### Added
- **`session_id` / `principal` on `tool_call_end` and `tool_call_error`.** `Emitter.enqueue_tool_call_end` / `enqueue_tool_call_error` accept optional `session_id` and `principal` kwargs, so downstream sensors that pair results out-of-process (e.g. a gateway seam) can stamp the session and end-user identity on response-side events. Additive; omitted kwargs produce byte-identical output to 0.5.0.

## [0.5.0] — 2026-07-27

### Added
- **End-user identity capture (`user_id`).** New `baton_proxy.identity` module: `hash_user_id()` (HMAC-SHA256, keyed per tenant with the `tenant_id` folded into the message so the same principal never collides across tenants; `h1:` scheme prefix as the key-rotation seam) plus a `Principal` / `IdentityResolver` seam so each capture modality resolves a raw principal that the core hashes. Hashing happens **at the edge** in `Emitter._enqueue` — the raw principal never survives the method, so a console-bound sink only ever sees the hash.
- **`user_id` on the event envelope** — additive and nullable: emitted only when a principal resolves and an HMAC key is set, so output is byte-identical to 0.4.x when unused.
- **`BATON_USER_ID_HMAC_KEY`** config (per-tenant secret). Unset → fail-open: `user_id` is dropped, events still emit (it is additive analytics, never a consent/authz gate); logged once.

### Changed
- Scrubber `REDACT_FIELD_NAMES` now includes `user_name` (defence-in-depth for the identity PII half). `name` is deliberately excluded — it collides with legitimate payload keys (prompt names, tool names in surface snapshots).

## [0.4.1] — 2026-07-21

### Changed
- **HTTP bridge graceful degradation**: when the upstream is unreachable (connection failure, non-2xx, timeout, or an accepted-but-empty reply), the `--url` bridge now degrades the two handshake methods — `initialize` and `tools/list` — to a synthetic healthy response instead of a JSON-RPC error. Erroring `initialize` put some clients (notably Claude Cowork) into a permanent failed-connection state, wedging the entire session including the proxy's own injected tools; degrading it lets the client attach and keeps `baton_annotate` usable. `tools/list` returns just the injected baton tools (no phantom vendor tools). Every other method still degrades per-call (a JSON-RPC error for that id, which clients tolerate), so real tool calls against a dead upstream still surface as errors rather than a wedge. Fail-open throughout.

## [0.4.0] — 2026-07-21

### Added
- **S3 event sink** (`pip install baton-proxy[s3]`): `s3://bucket/prefix` is now a valid `BATON_EVENT_SINK` scheme, usable on its own or in the comma-separated `MultiSink` fan-out. `boto3` is lazy-imported and gated behind the `[s3]` extra, so the base package stays zero-dependency. The placeholder-consent guard treats `s3://` as a remote sink (refuses to ship under `BATON_CONSENT_TOKEN='local'`).

### Changed
- Emitter `enqueue_*` methods accept an optional per-call `session_id`, so a single processor serving many sessions stamps each event with the session read from that request rather than one process-wide id. Omitting it preserves the previous one-session-per-process behavior — backward compatible for the stdio and `--url` transports. (Enables out-of-tree processors, e.g. `baton-extmcp`, to reuse the emitter.)

## [0.3.1] — 2026-07-11

### Added
- **Surface snapshot capture** (`surface_snapshot` event): on each session's first complete `tools/list`, the proxy emits one snapshot of the upstream server's surface — serverInfo, capabilities, instructions, and the full tool list (names, descriptions, input schemas, annotations) — hashed over the **vendor-true** (pre-injection) surface, with a `seam_augmentations` block recording what the proxy adds (injected tools, the intent param, the instructions suffix). Emission is suppressed when the hash matches the last-emitted hash for the session, so a stable surface costs one event per session at most. Pagination-safe: partial `tools/list` pages are never snapshot. Consumers can materialize surface history from these events (a new hash = the surface changed) and pin proposed changes to the exact surface version they were authored against.

## [0.3.0] — 2026-07-07

### Added
- **Per-tool intent param injection** (`baton_intent`): the proxy adds an optional string parameter to every upstream tool's schema at `tools/list`, strips it at `tools/call` before forwarding, and captures the value as user intent. The parameter description reaches the model at call-compose time on every client — including clients that drop `InitializeResult.instructions` entirely (observed on Claude Desktop) — so intent capture no longer depends on instructions compliance. The session's first param intent also emits a proactive annotation (sequenced before its `tool_call_start`, suppressed once a real `baton_annotate` proactive has fired); every call's intent rides `tool_call_start.payload.call_intent` with `intent_source` provenance. Tools that already define a `baton_intent` parameter are left untouched (never stripped, never read). Modes via `BATON_INTENT_PARAM`: `optional` (default) | `required` | `off`. Works on both transports (stdio subprocess and `--url` HTTP bridge); fail-open throughout — an injection or strip error forwards the message unmodified.

## [0.2.2] — 2026-07-04

### Added
- **HTTPS bridge** (`baton-proxy --url <url>`): wrap a remote Streamable HTTP MCP server (spec 2025-03-26), not just a local stdio subprocess. The proxy stays stdio-facing to the client and forwards each message as an HTTP POST, streaming the JSON or SSE response back. Bearer auth via `BATON_UPSTREAM_AUTH_TOKEN`; read timeout via `BATON_UPSTREAM_TIMEOUT` (default 60s); captures and echoes `Mcp-Session-Id` and pins `MCP-Protocol-Version` after the handshake; sends a named `User-Agent` + `Via` header (urllib's default UA is Cloudflare-banned on many hosted endpoints). Stdlib only — no new dependencies.
- **Resource & prompt capture (A1)**: the proxy now emits lifecycle events for `resources/read`, `resources/list`, `prompts/get`, and `prompts/list`, alongside the existing `tools/call` capture.

### Fixed
- HTTP bridge fail-open: a 2xx upstream reply that answers nothing (empty body, `202`, or an SSE stream with no matching frame) no longer leaves the client blocked on that request; a malformed or non-object client message no longer crashes the bridge. Both now emit a synthetic error event and hand the client a JSON-RPC error rather than hanging.
- SSE responses: only `event: message` frames are parsed as JSON-RPC — a server interleaving a keepalive/ping/custom event with a data payload no longer injects a bogus message to the client.
- stdio: the two pump threads are serialized on stdout, so a synthesized `baton_annotate` response can no longer interleave with a real upstream response and corrupt the wire.
- `BATON_UPSTREAM_TIMEOUT=inf`/`nan` falls back to the default instead of silently disabling the read timeout.
- Calls still in flight at shutdown are resolved with a synthetic error, so a mid-call upstream exit no longer leaves a dangling `*_start` with no end/error.

### Packaging
- `pyproject.toml` references the `LICENSE` file instead of inline SPDX text.

## [0.2.1] — 2026-06-23

### Changed
- `baton-proxy scan` now drives the agent to record each friction through `baton_annotate` (intent + `signal_type` + `suggested_improvement`) the moment it hits it, instead of only summarizing at the end. Scan reports are synthesized from captured annotation events, so mechanical-only error findings that used to render thin (generic intent, no fix) now carry the agent's restated intent and a concrete suggested fix. (A live `scan --config github` went from 1 thin finding to 7 with verbatim fixes.)

### Fixed
- `synthesize_scan` now folds a model-filed reactive into the mechanical error finding for the same tool even when the reactive names that tool only in its text (not a structured `tool` field), matching only when exactly one errored tool name appears. Previously a tool that both errored and got annotated could surface as two near-duplicate findings, inflating the headline friction count.

### Removed
- Pinned per-server task plans (`scan_tasks.py`). They existed only to make the cold-visitor homepage-demo finding reproducible; the config-only scan flow retired that demo path, leaving no consumer. Every scan now uses the adversarial generic driver plan.

## [0.2.0] — 2026-06-23

### Added
- `baton-proxy scan --config <name>`: one-command preflight friction report. Resolves an MCP server you've already configured in Claude (from `./.mcp.json` or `~/.claude.json`, reusing its saved credentials), wraps it, drives a headless `claude` agent through it, and renders a local `baton-report.md` from captured events — no permanent install or Claude-config change. The report anchors on mechanical tool errors plus model-flagged friction signals, and is labeled preflight/inferred. Warns when `ANTHROPIC_API_KEY` is set (it bills the API account over a Claude login session).

## [0.1.2] — 2026-06-12

### Fixed
- `sdk_version` field in emitted events was hardcoded to `"baton-proxy/0.0.1"` and never picked up version bumps. Now derived from `baton_proxy.__version__` at module load. Caught while dogfooding the 0.1.1 install — events from a `pipx`-installed proxy were reporting the stale version.

## [0.1.1] — 2026-06-12

Docs-only release to refresh the PyPI project description. No code changes.

### Changed
- README diagram now shows the full sink fan-out (`stderr:` / `file://` / Baton Console) instead of just the Console.
- Intro broadens "emits to a Baton Console" to "emits to one or more sinks", matching what `BATON_EVENT_SINK` actually accepts.
- New "Related" section links [`baton-sdk`](https://github.com/good-timing/baton) (the in-process integration alternative) and the [Baton wire-protocol spec](https://github.com/good-timing/baton/blob/main/docs/SPEC.md).
- Quick-start install line gains a one-line rationale for `pipx` vs `pip`.

## [0.1.0] — 2026-06-12

Initial public release on PyPI.

### Added
- Subprocess-wrap MCP proxy: wraps a stdio MCP server, intercepts the handshake, injects friction-capture tools into the upstream server's `tools/list`.
- `baton_annotate` tool: lets Claude emit a per-call annotation event when it hits friction (unprompted).
- `baton_session_report` tool: returns a vendor-shareable markdown report of the session's friction (errors, slow calls, annotations). Local-sink installs only.
- Friction event emission per real tool call (`tool_call_start` / `tool_call_end` / `tool_call_error`) carrying session id, monotonic sequence, and the upstream MCP request's `_meta` block.
- Multi-sink fan-out via `BATON_EVENT_SINK`: `stderr:`, `file://`, and `http(s)://` schemes, comma-separated. Zero-config default writes to `stderr:` + `file:///tmp/baton-proxy.jsonl`.
- Consent guard: refuses to start when an `http(s)://` sink is paired with the placeholder `BATON_CONSENT_TOKEN=local`, or when an `http(s)://` sink is configured without `BATON_API_KEY`.
- Fail-open delivery: emission runs on a background thread; Console outage never blocks the MCP pipe.

[Unreleased]: https://github.com/good-timing/baton-proxy/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/good-timing/baton-proxy/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/good-timing/baton-proxy/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/good-timing/baton-proxy/releases/tag/v0.1.0

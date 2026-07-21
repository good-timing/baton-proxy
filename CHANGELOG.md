# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

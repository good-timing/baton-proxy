# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

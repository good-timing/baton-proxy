# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/good-timing/baton-proxy/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/good-timing/baton-proxy/releases/tag/v0.1.0

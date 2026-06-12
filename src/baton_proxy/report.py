"""Friction-report synthesis from the proxy's local JSONL event stream.

The proxy already captures everything needed for a vendor-shareable
friction report (tool calls, errors, model annotations). This module
templates that into markdown — the customer sees the "ticket" surface
firsthand without leaving the Claude session.

Pre-rendered markdown only (v1). The injected `baton_session_report` tool
calls ``synthesize()``, the result becomes the tool response, and Claude
relays it to the customer. Zero token cost beyond the relay. A future
"synthesized" mode could return structured data + a synthesis preamble
and pay Claude tokens for a polished narrative — defer until raw mode
proves the surface.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections import Counter, defaultdict
from typing import Any

logger = logging.getLogger(__name__)


def find_file_sink_path(event_sink_url: str | None) -> str | None:
    """Return the first ``file://`` path in a (possibly comma-separated)
    sink URL spec, or None if no file sink is present. Used by the
    proxy to decide whether to inject the report tool — only file
    sinks support the report (stderr can't be read back, http(s) sinks
    indicate production mode where the Console renders tickets)."""
    if not event_sink_url:
        return None
    for part in (p.strip() for p in event_sink_url.split(",") if p.strip()):
        parsed = urllib.parse.urlparse(part)
        if parsed.scheme == "file" and parsed.path:
            return parsed.path
    return None


def has_http_sink(event_sink_url: str | None) -> bool:
    """True if any leg of the sink spec is http(s)://. The report tool is
    NOT injected in that case — an http sink signals 'vendor production
    mode' (white-label later, no Baton-branded customer-facing surface)."""
    if not event_sink_url:
        return False
    for part in (p.strip() for p in event_sink_url.split(",") if p.strip()):
        if part.startswith(("http://", "https://")):
            return True
    return False


def should_inject_report_tool(event_sink_url: str | None) -> bool:
    """Inject the report tool only for purely local installs with a file
    sink to read from. The gate maps to product mode:
      - Default install (stderr + file)           -> inject (gateway demo)
      - Custom local install (file://)            -> inject (gateway demo)
      - stderr: only                              -> skip (nothing to read)
      - any http(s):// (incl. multi-sink with it) -> skip (vendor production)
    """
    return find_file_sink_path(event_sink_url) is not None and not has_http_sink(
        event_sink_url
    )


def synthesize(sink_path: str, session_id: str) -> str:
    """Read the proxy's JSONL sink, filter to this session, return markdown.

    The output is a vendor-shareable friction report — same shape as what
    a Baton-instrumented vendor would see in their Console, but rendered
    from the local stream so customers can see the ticket surface firsthand
    before adopting the SDK + Console.
    """
    events = _read_session_events(sink_path, session_id)
    if not events:
        return _no_events_template(session_id, sink_path)
    return _render_markdown(events, session_id)


def _read_session_events(sink_path: str, session_id: str) -> list[dict[str, Any]]:
    """Best-effort JSONL read. Malformed lines are skipped (logged), not
    fatal — the report should degrade gracefully on a corrupt file."""
    events: list[dict[str, Any]] = []
    try:
        with open(sink_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("session_id") == session_id:
                    events.append(ev)
    except OSError as e:
        logger.warning("baton-proxy report: cannot read sink %r: %s", sink_path, e)
        return []
    events.sort(key=lambda e: e.get("sequence_number", 0))
    return events


def _render_markdown(events: list[dict[str, Any]], session_id: str) -> str:
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        by_type[ev.get("event_type", "unknown")].append(ev)

    starts = by_type.get("tool_call_start", [])
    ends = by_type.get("tool_call_end", [])
    errors = by_type.get("tool_call_error", [])
    annotations = by_type.get("annotation", [])

    total_calls = len(starts)
    error_count = len(errors)
    success_count = len(ends)
    success_rate = (
        f"{success_count}/{total_calls} ({success_count * 100 // total_calls}%)"
        if total_calls
        else "0/0"
    )

    tool_counts: Counter[str] = Counter()
    tool_errors: Counter[str] = Counter()
    for s in starts:
        tool_counts[s.get("payload", {}).get("tool_name", "<unknown>")] += 1
    for e in errors:
        tool_errors[e.get("payload", {}).get("tool_name", "<unknown>")] += 1

    first_ts = events[0].get("captured_at", "")
    last_ts = events[-1].get("captured_at", "")

    lines: list[str] = []
    lines.append("# Baton friction report")
    lines.append("")
    lines.append(f"**Session** `{session_id}`  ")
    lines.append(f"**Window** {first_ts} → {last_ts}  ")
    lines.append(f"**Tool calls** {total_calls} ({success_rate} succeeded)  ")
    lines.append(f"**Errors** {error_count}  ")
    lines.append(f"**Model annotations** {len(annotations)}  ")
    lines.append("")

    if tool_counts:
        lines.append("## Per-tool breakdown")
        lines.append("")
        lines.append("| Tool | Calls | Errors |")
        lines.append("|---|---:|---:|")
        for tool, count in sorted(
            tool_counts.items(), key=lambda x: (-x[1], x[0])
        ):
            lines.append(f"| `{tool}` | {count} | {tool_errors.get(tool, 0)} |")
        lines.append("")

    if errors:
        lines.append("## Errors")
        lines.append("")
        for err in errors:
            payload = err.get("payload", {})
            tool = payload.get("tool_name", "<unknown>")
            etype = payload.get("error_type", "")
            ebody = payload.get("error_body", "")
            duration = payload.get("duration_ms", 0)
            lines.append(f"### `{tool}` — error `{etype}` after {duration}ms")
            lines.append("")
            lines.append("```")
            lines.append(_truncate(ebody, 500))
            lines.append("```")
            lines.append("")

    if annotations:
        lines.append("## Model-emitted annotations")
        lines.append("")
        lines.append(
            "These are signals Claude flagged during this session via the "
            "`baton_annotate` tool — they're what the SDK captures from "
            "every customer agent in real time when a vendor instruments "
            "their MCP server."
        )
        lines.append("")
        for ann in annotations:
            payload = ann.get("payload", {})
            signal = payload.get("signal_type", "")
            intent = payload.get("intent", "")
            improvement = payload.get("suggested_improvement", "")
            lines.append(f"- **`{signal}`** — intent: _{intent}_")
            if improvement:
                lines.append(f"  - **suggested improvement:** {improvement}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "_This is a preview of the ticket shape a Baton-instrumented vendor "
        "sees in their Console for every customer session. The proxy renders "
        "it locally from the JSONL stream; in production, the SDK + Console "
        "deliver the same surface as a real-time vendor ticket._"
    )
    return "\n".join(lines)


def _no_events_template(session_id: str, sink_path: str) -> str:
    return (
        f"# Baton friction report\n\n"
        f"**Session** `{session_id}`  \n"
        f"**Sink** `{sink_path}`\n\n"
        f"No events captured yet for this session. Drive a few tool calls "
        f"through the wrapped server and re-run this tool to see the "
        f"friction report.\n"
    )


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"... [truncated {len(s) - n} chars]"

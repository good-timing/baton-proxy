"""Friction-report synthesis from the proxy's local JSONL event stream.

The proxy captures the events needed for a friction report (tool calls,
errors, model annotations). This module templates that stream into markdown
so the customer sees the report surface firsthand without leaving their
Claude session.

Each reactive annotation in the session (an ``annotation`` event with
``signal_type`` set) becomes one signal block. A signal's cycle is bounded
by **inter-reactive slices** — each reactive owns the events strictly
after the previous reactive (or session start) up to and including itself —
so trails stay tight in long sessions, which can accumulate many unrelated
frictions over hours.

Section ordering per signal block:

  1. What the agent was trying to do  (intent + workflow + expected outcome)
  2. Reasoning trail                   (ordered (proactive, tool calls) steps)
  3. What's missing                    (missing-capability surfaced by the agent)
  4. Where the friction surfaced /     (adaptive heading: error vs ok)
     Last successful tool call before escalation
  5. Why the agent escalated           (context + alternatives ruled out)
  6. Suggested improvement             (verbatim from the agent)
  7. Reproducer                        (session_id, agent_runtime, sdk_version, tool)

Pre-rendered markdown only (v1). The injected ``baton_session_report`` tool
calls ``synthesize()``, the result becomes the tool response, and Claude
relays it to the customer. Zero token cost beyond the relay. A future
"synthesized" mode could return structured data + a synthesis preamble and
pay Claude tokens for a polished narrative — defer until raw mode proves
the surface.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

logger = logging.getLogger(__name__)


# Maps reactive ``signal_type`` to a priority label that the report header
# surfaces alongside each signal. Bumped from ``medium`` to ``high`` when
# the reactive's ``context.downstream_blocked`` flag is set.
_PRIORITY_BY_SIGNAL: dict[str, str] = {
    "failure": "urgent",
    "dead_end": "high",
    "feature_gap": "high",
    "retry_loop": "high",
    "parameter_confusion": "medium",
    "slow_performance": "medium",
    "abandonment": "medium",
    "other": "medium",
}


# =============================================================================
# Gating helpers — decide whether the report tool is injected at all.
# =============================================================================


def find_file_sink_path(event_sink_url: str | None) -> str | None:
    """Return the first ``file://`` path in a (possibly comma-separated) sink
    URL spec, or None if no file sink is present. Used by the proxy to decide
    whether to inject the report tool — only file sinks support the report
    (stderr can't be read back; http(s) sinks indicate production mode where
    upstream renders the report instead of the proxy)."""
    if not event_sink_url:
        return None
    for part in (p.strip() for p in event_sink_url.split(",") if p.strip()):
        parsed = urllib.parse.urlparse(part)
        if parsed.scheme == "file" and parsed.path:
            return parsed.path
    return None


def has_http_sink(event_sink_url: str | None) -> bool:
    """True if any leg of the sink spec is http(s)://. The report tool is NOT
    injected in that case — an http sink signals 'vendor production mode'
    (white-label later, no Baton-branded customer-facing surface)."""
    if not event_sink_url:
        return False
    for part in (p.strip() for p in event_sink_url.split(",") if p.strip()):
        if part.startswith(("http://", "https://")):
            return True
    return False


def should_inject_report_tool(
    event_sink_url: str | None,
    *,
    tenant_type: str = "vendor",
) -> bool:
    """Inject the report tool when there's a file sink to read from AND
    HTTP-sink suppression doesn't apply. The gate maps to product mode:

      - Default install (stderr + file), vendor    -> inject (gateway demo)
      - Custom local install (file://), vendor     -> inject (gateway demo)
      - stderr: only                                -> skip (no file to read)
      - http(s):// only (any tenant_type)           -> skip (no file to read)
      - file + http(s)://, tenant_type=vendor       -> skip (vendor prod)
      - file + http(s)://, tenant_type=customer     -> inject (customer mode)

    Customer mode keeps the in-Claude report tool because the customer
    owns the same Console the events ship to — they want fast access to
    the same report shape without leaving their Claude session.
    """
    if find_file_sink_path(event_sink_url) is None:
        return False
    if has_http_sink(event_sink_url) and tenant_type != "customer":
        return False
    return True


# =============================================================================
# Entry point + JSONL reader.
# =============================================================================


def synthesize(
    sink_path: str,
    session_id: str,
    *,
    scrub_counts: dict[str, int] | None = None,
) -> str:
    """Read the proxy's JSONL sink, filter to this session, return markdown.

    The output is a friction report rendered locally from the JSONL stream
    so customers can see the report surface firsthand.

    ``scrub_counts`` is an optional snapshot from ``Emitter.scrub_counts()``
    (counts of PII fields redacted this session). When provided, the
    header renders a "Scrubbed fields" line as a visible trust signal.
    Computed live from the emitter rather than re-parsed from the JSONL
    file because the file already only contains scrubbed payloads.
    """
    events = _read_session_events(sink_path, session_id)
    if not events:
        return _no_events_template(session_id, sink_path)
    return _render_markdown(events, session_id, scrub_counts=scrub_counts)


def _read_session_events(sink_path: str, session_id: str) -> list[dict[str, Any]]:
    """Best-effort JSONL read. Malformed lines are skipped (logged), not fatal
    — the report should degrade gracefully on a corrupt file."""
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


# =============================================================================
# Rendering.
# =============================================================================


def _render_markdown(
    events: list[dict[str, Any]],
    session_id: str,
    *,
    scrub_counts: dict[str, int] | None = None,
) -> str:
    reactives = [
        e
        for e in events
        if e.get("event_type") == "annotation" and (e.get("payload") or {}).get("signal_type")
    ]

    lines: list[str] = []
    lines.extend(_render_header(events, session_id, reactives, scrub_counts=scrub_counts))

    if not reactives:
        lines.extend(_render_no_reactive_stub())
    else:
        if len(reactives) >= 2:
            lines.extend(_render_toc(reactives))
        prev_seq = -1
        for i, reactive in enumerate(reactives, start=1):
            lines.extend(_render_signal_block(events, reactive, i, prev_reactive_seq=prev_seq))
            prev_seq = int(reactive.get("sequence_number", 0))

    lines.extend(_render_footer())
    return "\n".join(lines)


def _render_header(
    events: list[dict[str, Any]],
    session_id: str,
    reactives: list[dict[str, Any]],
    *,
    scrub_counts: dict[str, int] | None = None,
) -> list[str]:
    first_ts = events[0].get("captured_at", "")
    last_ts = events[-1].get("captured_at", "")
    lines = [
        "# Baton friction report",
        "",
        f"**Session** `{session_id}`  ",
        f"**Window** {first_ts} → {last_ts}  ",
        f"**Events** {len(events)} captured · **Signals filed** {len(reactives)}",
    ]
    scrub_line = _format_scrub_counts(scrub_counts)
    if scrub_line:
        lines.append(f"**Scrubbed fields** {scrub_line}  ")
    lines.append("")
    return lines


def _format_scrub_counts(counts: dict[str, int] | None) -> str:
    """Render the scrub-counts dict as a human-readable comma-separated
    string. Returns empty string when there's nothing to show — caller
    suppresses the header line entirely in that case so a zero-PII
    session doesn't read as "we wanted to redact but failed"."""
    if not counts:
        return ""
    nonzero = {k: v for k, v in counts.items() if v > 0}
    if not nonzero:
        return ""
    # Stable ordering: alphabetical by category. Makes the line readable
    # and keeps test assertions reproducible without depending on dict
    # insertion order.
    labels = {
        "email": "emails",
        "bearer": "bearer tokens",
        "sk_key": "sk-* keys",
        "aws_key": "AWS keys",
        "jwt": "JWTs",
        "phone": "phone numbers",
        "cc": "credit cards",
    }
    parts: list[str] = []
    for category in sorted(nonzero):
        count = nonzero[category]
        # field:* counts are surfaced under a "field-name matches" bucket
        # because most users don't need per-field-name detail in the
        # report header; the dashboard can break it down further.
        if category.startswith("field:"):
            continue
        label = labels.get(category, category)
        parts.append(f"{count} {label}")
    field_total = sum(v for k, v in nonzero.items() if k.startswith("field:"))
    if field_total:
        parts.append(f"{field_total} field-name matches")
    return ", ".join(parts) if parts else ""


def _render_toc(reactives: list[dict[str, Any]]) -> list[str]:
    """Mini-index of all reactives at the top — only emitted for 2+ signals."""
    lines = ["## Signals filed in this session", ""]
    for i, r in enumerate(reactives, start=1):
        p = r.get("payload") or {}
        signal = p.get("signal_type", "")
        ctx = p.get("context") or {}
        intent_text = ctx.get("requested_capability") or p.get("intent") or "agent-filed signal"
        ts = str(r.get("captured_at", ""))
        ts_short = ts.split("T")[1].rstrip("Z") if "T" in ts else ts
        lines.append(f"{i}. `{signal}` — {_short_intent(intent_text)} ({ts_short})")
    lines.append("")
    return lines


def _render_no_reactive_stub() -> list[str]:
    return [
        "## No friction signal filed yet",
        "",
        (
            "Events were captured, but the agent didn't file a reactive "
            "`signal_type` annotation in this session. The report templates "
            "a signal block per filed signal; without one there's nothing "
            "to render. Drive a flow that hits real friction (a missing "
            "capability, a failed tool, a dead end) and the agent will "
            "file a signal that this report can synthesize."
        ),
        "",
    ]


def _render_footer() -> list[str]:
    return [
        "---",
        "",
        (
            "_This report is rendered locally from the proxy's JSONL stream "
            "and previews the friction-signal surface a Baton-instrumented "
            "MCP server delivers to its support pipeline for every customer "
            "session._"
        ),
    ]


# =============================================================================
# Per-signal block — the seven report sections, in order.
# =============================================================================


def _render_signal_block(
    all_events: list[dict[str, Any]],
    reactive: dict[str, Any],
    index: int,
    *,
    prev_reactive_seq: int,
) -> list[str]:
    """One signal's ticket block. Cycle is inter-reactive-bounded:
    ``prev_reactive_seq < seq <= reactive.sequence_number``."""
    reactive_seq = int(reactive.get("sequence_number", 0))
    cycle_events = [
        e
        for e in all_events
        if prev_reactive_seq < int(e.get("sequence_number", 0)) <= reactive_seq
    ]

    rpayload: dict[str, Any] = reactive.get("payload") or {}
    signal_type = str(rpayload.get("signal_type", ""))
    rcontext: dict[str, Any] = rpayload.get("context") or {}
    suggested = rpayload.get("suggested_improvement") or ""

    # Cycle proactives: annotations with `intent` set but no `signal_type`.
    proactives = [
        e
        for e in cycle_events
        if e.get("event_type") == "annotation"
        and int(e.get("sequence_number", 0)) < reactive_seq
        and (e.get("payload") or {}).get("intent")
        and not (e.get("payload") or {}).get("signal_type")
    ]
    first_proactive = proactives[0] if proactives else None
    last_proactive = proactives[-1] if proactives else None
    fp_payload: dict[str, Any] = (first_proactive or {}).get("payload") or {}
    lp_payload: dict[str, Any] = (last_proactive or {}).get("payload") or {}

    # Title/intent fallback chain: prefer the reactive's structured
    # ``context.requested_capability``, then its raw ``intent``, then the
    # first proactive's ``intent``.
    intent_text = (
        rcontext.get("requested_capability")
        or rpayload.get("intent")
        or fp_payload.get("intent")
        or ""
    )
    expected = (
        lp_payload.get("expected_outcome")
        or fp_payload.get("expected_outcome")
        or rpayload.get("expected_outcome")
        or ""
    )
    workflow = (
        rpayload.get("workflow") or lp_payload.get("workflow") or fp_payload.get("workflow") or ""
    )

    # Primary tool: prefer the most-recent tool_call_error within the cycle;
    # fall back to the most-recent tool_call_end only if no error happened.
    # The signal_type already tells us the agent hit friction — when an error
    # is present in the cycle, that's overwhelmingly what the agent reacted
    # to, even if it wasn't the literal last tool call (the agent may have
    # tried a follow-up tool that succeeded but didn't unblock them). Picking
    # an end first would silently bury the failure in mixed success+error
    # cycles.
    tool_ends = [e for e in cycle_events if e.get("event_type") == "tool_call_end"]
    tool_errors = [e for e in cycle_events if e.get("event_type") == "tool_call_error"]
    tool_starts = [e for e in cycle_events if e.get("event_type") == "tool_call_start"]
    primary_tool_call = _latest_before(tool_errors, reactive_seq) or _latest_before(
        tool_ends, reactive_seq
    )
    # primary_start must MATCH the primary_tool_call's tool_name + sequence,
    # not just be the literal latest start before the reactive. In mixed
    # cycles like A → B-error → A, the latest start is the trailing A, but
    # the primary_tool_call is the B error — params from that latest start
    # would mislabel B's invocation with A's args.
    if primary_tool_call is not None:
        primary_tn = (primary_tool_call.get("payload") or {}).get("tool_name")
        primary_seq = int(primary_tool_call.get("sequence_number", 0))
        primary_start = next(
            (
                s
                for s in reversed(tool_starts)
                if int(s.get("sequence_number", 0)) < primary_seq
                and (s.get("payload") or {}).get("tool_name") == primary_tn
            ),
            None,
        )
        tool_name = primary_tn
    else:
        primary_start = _latest_before(tool_starts, reactive_seq)
        tool_name = (
            (primary_start.get("payload") or {}).get("tool_name")
            if primary_start is not None
            else None
        )

    priority = _PRIORITY_BY_SIGNAL.get(signal_type, "medium")
    if priority == "medium" and rcontext.get("downstream_blocked"):
        priority = "high"

    agent_runtime = _first_meta(all_events, "agent_runtime") or "agent"
    tag_candidates = ["baton", signal_type, agent_runtime]
    if tool_name:
        tag_candidates.append(str(tool_name))
    tags: list[str] = []
    for t in tag_candidates:
        if t and t not in tags:
            tags.append(t)

    # ----- Header for this signal block -----
    lines: list[str] = [
        f"## Signal {index} — `{signal_type}`: {_short_intent(intent_text)}",
        "",
        f"**Priority:** `{priority}`  ",
        f"**Tool:** `{tool_name or '—'}`  ",
        f"**Tags:** {' · '.join(f'`{t}`' for t in tags)}",
        "",
    ]

    # ----- §1 What the agent was trying to do -----
    lines.append("### What the agent was trying to do")
    lines.append("")
    lines.append(intent_text if intent_text else "*not captured*")
    lines.append("")
    if workflow:
        lines.append(f"**Workflow:** {workflow}")
    if expected:
        lines.append(f"**Expected from the tool:** {expected}")
    if workflow or expected:
        lines.append("")

    # ----- §2 Reasoning trail -----
    trail = _build_reasoning_trail(cycle_events, reactive_seq)
    if trail:
        lines.append("### Reasoning trail (what the agent tried, in order)")
        lines.append("")
        lines.extend(_render_trail(trail))
        lines.append("")

    # ----- §3 What's missing -----
    missing = rcontext.get("missing_capability_field") or rcontext.get("missing_capability")
    if missing:
        lines.append("### What's missing")
        lines.append("")
        lines.append(f"**{missing}**")
        lines.append("")

    # ----- §4 Where the friction surfaced / Last successful tool call -----
    if primary_tool_call is not None or primary_start is not None:
        if (
            primary_tool_call is not None
            and primary_tool_call.get("event_type") == "tool_call_error"
        ):
            lines.append("### Where the friction surfaced (final tool errored)")
        else:
            lines.append("### Last successful tool call before escalation")
        lines.append("")
        details: list[tuple[str, Any]] = []
        if tool_name:
            details.append(("tool", f"`{tool_name}`"))
        if primary_start is not None:
            params = (primary_start.get("payload") or {}).get("params")
            if params is not None:
                details.append(("params", f"`{json.dumps(params, sort_keys=True)}`"))
        if primary_tool_call is not None and primary_tool_call.get("event_type") == "tool_call_end":
            details.append(("status", "`ok`"))
            d = (primary_tool_call.get("payload") or {}).get("duration_ms")
            if d is not None:
                details.append(("duration_ms", f"`{d}`"))
        if (
            primary_tool_call is not None
            and primary_tool_call.get("event_type") == "tool_call_error"
        ):
            details.append(("status", "`error`"))
            etype = (primary_tool_call.get("payload") or {}).get("error_type")
            if etype:
                details.append(("error_type", f"`{etype}`"))
            ebody = str((primary_tool_call.get("payload") or {}).get("error_body") or "")
            if ebody:
                details.append(("error_body", f"\n  ```\n  {_truncate(ebody, 500)}\n  ```"))
        for k, v in details:
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    # ----- §5 Why the agent escalated -----
    diag_items = {
        k: v
        for k, v in rcontext.items()
        if k not in {"alternatives_considered"} and not isinstance(v, (list, dict))
    }
    alts = rcontext.get("alternatives_considered")
    if diag_items or (isinstance(alts, list) and alts):
        lines.append("### Why the agent escalated")
        lines.append("")
        for k, v in diag_items.items():
            lines.append(f"- **{k}:** {v}")
        if isinstance(alts, list) and alts:
            lines.append("")
            lines.append("**Alternatives the agent ruled out**")
            for a in alts:
                lines.append(f"- {a}")
        lines.append("")

    # ----- §6 Suggested improvement -----
    if suggested:
        lines.append("### Suggested improvement (verbatim from the agent)")
        lines.append("")
        lines.append(f"> {suggested}")
        lines.append("")

    # ----- §7 Reproducer -----
    first_meta = cycle_events[0] if cycle_events else (all_events[0] if all_events else {})
    repro: list[tuple[str, Any]] = []
    sid = first_meta.get("session_id")
    if sid:
        repro.append(("session_id", sid))
    ar = first_meta.get("agent_runtime")
    if ar:
        repro.append(("agent_runtime", ar))
    sv = first_meta.get("sdk_version")
    if sv:
        repro.append(("sdk_version", sv))
    repro.append(("events_in_cycle", len(cycle_events)))
    if tool_name:
        repro.append(("tool", tool_name))
    lines.append("### Reproducer")
    lines.append("")
    for k, v in repro:
        lines.append(f"- **{k}:** `{v}`")
    lines.append("")

    return lines


def _build_reasoning_trail(
    cycle_events: list[dict[str, Any]], reactive_seq: int
) -> list[dict[str, Any]]:
    """Group a cycle's events into ordered (proactive, tool calls) steps.

    Each step starts at a proactive annotation; its ``tool_calls`` list
    collects every ``tool_call_end`` / ``tool_call_error`` event up to the
    next proactive (or the reactive). Tool starts are skipped — the
    end/error events are the informative ones (they carry results /
    errors). Per SPEC §11.5.2 a signal carries the entire reasoning chain,
    not just one (proactive, tool, reactive) triple.
    """
    steps: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for ev in cycle_events:
        if int(ev.get("sequence_number", 0)) >= reactive_seq:
            break
        p = ev.get("payload") or {}
        etype = ev.get("event_type")
        if etype == "annotation":
            if not p.get("signal_type") and p.get("intent"):
                if current is not None:
                    steps.append(current)
                current = {"intent": p.get("intent", ""), "tool_calls": []}
        elif etype == "tool_call_end" and current is not None:
            current["tool_calls"].append(
                {
                    "tool": p.get("tool_name"),
                    "status": "ok",
                    "duration_ms": p.get("duration_ms"),
                }
            )
        elif etype == "tool_call_error" and current is not None:
            current["tool_calls"].append(
                {
                    "tool": p.get("tool_name"),
                    "status": "error",
                    "error_type": p.get("error_type"),
                    "error_body": p.get("error_body"),
                }
            )
    if current is not None:
        steps.append(current)
    return steps


def _render_trail(steps: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for i, step in enumerate(steps, start=1):
        intent = step.get("intent") or "*no intent captured*"
        out.append(f"{i}. **{intent}**")
        for tc in step.get("tool_calls", []):
            tool = tc.get("tool") or "?"
            if tc.get("status") == "error":
                etype = tc.get("error_type") or "Error"
                ebody = _truncate(str(tc.get("error_body") or ""), 200)
                out.append(f"   - `{tool}` → **{etype}**: {ebody}")
            else:
                d = tc.get("duration_ms")
                suffix = f" ({d}ms)" if d is not None else ""
                out.append(f"   - `{tool}` → ok{suffix}")
    return out


# =============================================================================
# Small helpers.
# =============================================================================


def _latest_before(events: list[dict[str, Any]], before_seq: int) -> dict[str, Any] | None:
    return next(
        (e for e in reversed(events) if int(e.get("sequence_number", 0)) < before_seq),
        None,
    )


def _first_meta(events: list[dict[str, Any]], key: str) -> Any:
    for e in events:
        v = e.get(key)
        if v:
            return v
    return None


def _short_intent(intent: str | None, max_chars: int = 80) -> str:
    if not intent:
        return "agent-filed signal"
    s = intent.strip().rstrip(".")
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"... [truncated {len(s) - n} chars]"


def _no_events_template(session_id: str, sink_path: str) -> str:
    return (
        f"# Baton friction report\n\n"
        f"**Session** `{session_id}`  \n"
        f"**Sink** `{sink_path}`\n\n"
        f"No events captured yet for this session. Drive a few tool calls "
        f"through the wrapped server and re-run this tool to see the "
        f"friction report.\n"
    )

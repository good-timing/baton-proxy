"""Tests for the friction-report synthesis (report.py).

Three surfaces:
  - Gating helpers (find_file_sink_path, has_http_sink, should_inject_report_tool)
    decide whether the report tool is injected. Mapped to product mode:
    local-sink demo -> yes, http sink (production) -> no.
  - synthesize() reads the JSONL sink and templates markdown. Each reactive
    annotation becomes one signal block, with inter-reactive cycle bounding.
  - The no-reactive / no-events stubs.
"""

from __future__ import annotations

import json
from pathlib import Path

from baton_proxy.report import (
    find_file_sink_path,
    has_http_sink,
    should_inject_report_tool,
    synthesize,
    synthesize_scan,
)

# =============================================================================
# Gating helpers
# =============================================================================


def test_find_file_sink_path_returns_first_file_url() -> None:
    assert find_file_sink_path("file:///tmp/a.jsonl") == "/tmp/a.jsonl"


def test_find_file_sink_path_skips_non_file_urls() -> None:
    assert find_file_sink_path("stderr:,file:///tmp/a.jsonl") == "/tmp/a.jsonl"


def test_find_file_sink_path_returns_none_when_no_file() -> None:
    assert find_file_sink_path("stderr:") is None
    assert find_file_sink_path("https://example.com") is None
    assert find_file_sink_path(None) is None
    assert find_file_sink_path("") is None


def test_has_http_sink_detects_http_and_https() -> None:
    assert has_http_sink("http://example.com") is True
    assert has_http_sink("https://example.com") is True
    assert has_http_sink("file:///tmp/a.jsonl,https://x") is True


def test_has_http_sink_false_for_local_only() -> None:
    assert has_http_sink("file:///tmp/a.jsonl") is False
    assert has_http_sink("stderr:,file:///tmp/a.jsonl") is False
    assert has_http_sink(None) is False


def test_should_inject_report_tool_for_default_install() -> None:
    """The proxy's zero-config default — stderr + file — IS gateway demo mode.
    Report tool MUST be injected here; this is the whole point."""
    assert should_inject_report_tool("stderr:,file:///tmp/baton-proxy.jsonl") is True


def test_should_inject_report_tool_for_file_only() -> None:
    assert should_inject_report_tool("file:///tmp/events.jsonl") is True


def test_should_NOT_inject_report_tool_for_stderr_only() -> None:
    """Pure stderr has nothing to read back from — no file means no report."""
    assert should_inject_report_tool("stderr:") is False


def test_should_NOT_inject_report_tool_for_http_only() -> None:
    """HTTP sink = vendor production mode. Upstream renders the report, not
    the proxy. No Baton-branded customer-facing surface in production."""
    assert should_inject_report_tool("https://collector.example.com") is False


def test_should_NOT_inject_report_tool_when_any_http_present() -> None:
    """In vendor mode (default), the presence of any http leg means the
    customer is in production mode and the report tool must be suppressed.
    Customer mode flips this — see the customer-mode tests below."""
    assert should_inject_report_tool("file:///tmp/a.jsonl,https://collector.example.com") is False


def test_should_inject_report_tool_for_customer_mode_with_file_and_http() -> None:
    """Customer mode: the same person owns the proxy and the Console, so
    keep the in-Claude report tool injected even when shipping to a
    remote http sink. Requires a file sink alongside so synth has
    something to read."""
    assert (
        should_inject_report_tool(
            "file:///tmp/a.jsonl,https://console.baton.dev",
            tenant_type="customer",
        )
        is True
    )


def test_should_NOT_inject_report_tool_for_customer_mode_http_only() -> None:
    """Even in customer mode, an http-only sink leaves the report tool
    with no file to synthesize from — suppress the tool rather than
    inject something that always returns 'no local file sink'."""
    assert (
        should_inject_report_tool(
            "https://console.baton.dev",
            tenant_type="customer",
        )
        is False
    )


def test_should_inject_report_tool_for_customer_mode_file_only() -> None:
    """File-only is the same as default install — customer mode doesn't
    change it."""
    assert (
        should_inject_report_tool("file:///tmp/a.jsonl", tenant_type="customer") is True
    )


# =============================================================================
# synthesize() — fixtures
# =============================================================================


def _write_events(path: Path, events: list[dict]) -> None:
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _event(seq: int, etype: str, payload: dict, *, session: str = "s1") -> dict:
    """Minimal event envelope for synthesis tests. Top-level metadata fields
    (sdk_version, agent_runtime) added by helpers below when needed."""
    return {
        "event_id": f"ev{seq}",
        "event_type": etype,
        "session_id": session,
        "sequence_number": seq,
        "captured_at": f"2026-06-12T00:00:{seq:02d}Z",
        "payload": payload,
        "sdk_version": "0.2.7",
        "agent_runtime": "claude_code",
    }


def _reactive(seq: int, signal_type: str, **payload_extras) -> dict:
    payload = {"signal_type": signal_type, **payload_extras}
    return _event(seq, "annotation", payload)


def _proactive(seq: int, intent: str, **payload_extras) -> dict:
    payload = {"intent": intent, **payload_extras}
    return _event(seq, "annotation", payload)


# =============================================================================
# Stubs (no events / no reactive)
# =============================================================================


def test_synthesize_empty_session_renders_placeholder(tmp_path: Path) -> None:
    """A fresh session before any tool calls hits this path — should return
    a friendly placeholder, not crash."""
    path = tmp_path / "events.jsonl"
    path.touch()
    out = synthesize(str(path), "s1")
    assert "No events captured yet" in out
    assert "s1" in out


def test_synthesize_missing_file_does_not_crash(tmp_path: Path) -> None:
    out = synthesize(str(tmp_path / "nonexistent.jsonl"), "s1")
    assert "No events" in out  # falls through the empty-events template


def test_synthesize_renders_scrub_counts_in_header(tmp_path: Path) -> None:
    """When scrub_counts is passed, the header surfaces a 'Scrubbed fields'
    line so customers see a visible trust signal in the report tool's output."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_event(0, "tool_call_start", {"tool_name": "x", "params": {}})) + "\n"
    )
    out = synthesize(
        str(path),
        "s1",
        scrub_counts={"email": 2, "bearer": 1, "field:password": 3, "cc": 0},
    )
    assert "Scrubbed fields" in out
    assert "2 emails" in out
    assert "1 bearer tokens" in out
    assert "3 field-name matches" in out
    # Zero-count categories are suppressed.
    assert "0 credit cards" not in out


def test_synthesize_omits_scrub_line_when_no_counts(tmp_path: Path) -> None:
    """A clean session with nothing scrubbed must not render a 'Scrubbed
    fields: ' line — empty / zero counts read as 'we tried and failed'."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_event(0, "tool_call_start", {"tool_name": "x", "params": {}})) + "\n"
    )
    out_none = synthesize(str(path), "s1", scrub_counts=None)
    out_empty = synthesize(str(path), "s1", scrub_counts={})
    out_zero = synthesize(str(path), "s1", scrub_counts={"email": 0})
    for out in (out_none, out_empty, out_zero):
        assert "Scrubbed fields" not in out


def test_synthesize_events_but_no_reactive_renders_no_signal_stub(
    tmp_path: Path,
) -> None:
    """A session that captured tool calls but never had the agent file a
    reactive annotation — falls into the 'no signal filed yet' stub. The
    session rollup from the old report shape is NOT shown."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "echo"}),
            _event(1, "tool_call_end", {"tool_name": "echo", "duration_ms": 5}),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "No friction signal filed yet" in out
    assert "Signals filed** 0" in out
    # Per-tool table from the OLD format must not appear.
    assert "Per-tool breakdown" not in out
    # Drive-friction hint must be present.
    assert "missing capability" in out or "failed tool" in out


def test_synthesize_filters_to_current_session(tmp_path: Path) -> None:
    """Multi-proxy installs may share one JSONL — the report MUST only show
    events for the calling session."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "echo"}, session="s1"),
            _reactive(1, "failure", intent="thing for s1") | {"session_id": "s1"},
            _event(2, "tool_call_start", {"tool_name": "other"}, session="s2"),
            _reactive(3, "failure", intent="thing for s2") | {"session_id": "s2"},
        ],
    )
    out = synthesize(str(path), "s1")
    assert "thing for s1" in out
    assert "thing for s2" not in out
    assert "`other`" not in out


def test_synthesize_skips_malformed_jsonl_lines(tmp_path: Path) -> None:
    """A corrupt line shouldn't break the report — degrade gracefully."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_event(0, "tool_call_start", {"tool_name": "echo"}))
        + "\n"
        + "{not valid json\n"
        + json.dumps(_reactive(1, "failure", intent="find a missing thing"))
        + "\n"
    )
    out = synthesize(str(path), "s1")
    # The two good lines made it through and rendered a signal block.
    assert "find a missing thing" in out
    assert "Signal 1" in out


# =============================================================================
# Single-signal rendering — the seven report sections
# =============================================================================


def test_synthesize_single_reactive_renders_signal_block_with_all_sections(
    tmp_path: Path,
) -> None:
    """The headline test: a single reactive with a rich payload + context
    renders every section."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _proactive(0, "find user's last order", workflow="customer-support"),
            _event(
                1,
                "tool_call_start",
                {"tool_name": "search", "params": {"q": "last order"}},
            ),
            _event(
                2,
                "tool_call_error",
                {
                    "tool_name": "search",
                    "error_type": "EmptyResult",
                    "error_body": "no rows matched",
                    "duration_ms": 12,
                },
            ),
            _reactive(
                3,
                "failure",
                intent="find user's last order",
                expected_outcome="one or more orders",
                workflow="customer-support",
                suggested_improvement="return available filters on empty result",
                context={
                    "downstream_blocked": True,
                    "missing_capability": "filter-by-status",
                    "alternatives_considered": ["scan", "manual lookup"],
                },
            ),
        ],
    )
    out = synthesize(str(path), "s1")

    # Header
    assert "# Baton friction report" in out
    assert "Signals filed** 1" in out

    # Signal block header
    assert "Signal 1" in out
    assert "`failure`" in out
    assert "find user's last order" in out
    # Priority bumped from urgent (failure already urgent — stays urgent).
    assert "**Priority:** `urgent`" in out
    assert "**Tool:** `search`" in out

    # §1 What the agent was trying to do
    assert "### What the agent was trying to do" in out
    assert "**Workflow:** customer-support" in out
    assert "**Expected from the tool:** one or more orders" in out

    # §3 What's missing
    assert "### What's missing" in out
    assert "**filter-by-status**" in out

    # §4 Adaptive heading — final tool errored
    assert "### Where the friction surfaced (final tool errored)" in out
    assert "EmptyResult" in out
    assert "no rows matched" in out

    # §5 Why the agent escalated + alternatives
    assert "### Why the agent escalated" in out
    assert "downstream_blocked" in out
    assert "**Alternatives the agent ruled out**" in out
    assert "scan" in out
    assert "manual lookup" in out

    # §6 Suggested improvement (blockquote)
    assert "### Suggested improvement (verbatim from the agent)" in out
    assert "> return available filters on empty result" in out

    # §7 Reproducer
    assert "### Reproducer" in out
    assert "agent_runtime" in out
    assert "sdk_version" in out


def test_synthesize_primary_start_matches_primary_tool_call(tmp_path: Path) -> None:
    """In an echo(args_A) → boom-error → echo(args_B) cycle, the primary tool
    is boom (the error). Its params section must show boom's args (or be
    empty), NOT args_B from the trailing echo start. Regression test for a
    bug where primary_start was 'latest start before reactive' regardless of
    whether it matched primary_tool_call.
    """
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "echo", "params": {"args": "A"}}),
            _event(1, "tool_call_end", {"tool_name": "echo", "duration_ms": 5}),
            _event(2, "tool_call_start", {"tool_name": "boom", "params": {"args": "BOOM_PARAMS"}}),
            _event(
                3,
                "tool_call_error",
                {"tool_name": "boom", "error_type": "-32000", "error_body": "x"},
            ),
            _event(
                4, "tool_call_start", {"tool_name": "echo", "params": {"args": "TRAILING_ECHO"}}
            ),
            _event(5, "tool_call_end", {"tool_name": "echo", "duration_ms": 7}),
            _reactive(6, "failure", intent="x"),
        ],
    )
    out = synthesize(str(path), "s1")
    # The boom invocation's params should be reported, not the trailing echo's.
    assert "BOOM_PARAMS" in out
    assert "TRAILING_ECHO" not in out


def test_synthesize_error_in_cycle_wins_over_later_success(tmp_path: Path) -> None:
    """If the cycle contains BOTH a tool_call_error and a later tool_call_end,
    the friction section must highlight the ERROR — that's what the agent
    reacted to, even if a follow-up tool succeeded.

    Regression test for a bug where the code preferred the latest end over
    the latest error in the cycle, even when a later tool succeeded.
    Surfaced by eyeballing synthesize() output against a real JSONL stream.
    """
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "echo"}),
            _event(1, "tool_call_end", {"tool_name": "echo", "duration_ms": 5}),
            _event(2, "tool_call_start", {"tool_name": "boom"}),
            _event(
                3,
                "tool_call_error",
                {
                    "tool_name": "boom",
                    "error_type": "-32000",
                    "error_body": "boom message",
                },
            ),
            _event(4, "tool_call_start", {"tool_name": "echo"}),
            _event(5, "tool_call_end", {"tool_name": "echo", "duration_ms": 7}),
            _reactive(6, "failure", intent="trigger a tool_call_error"),
        ],
    )
    out = synthesize(str(path), "s1")
    # Friction section must surface the boom error, NOT the later echo end.
    assert "### Where the friction surfaced (final tool errored)" in out
    assert "### Last successful tool call before escalation" not in out
    assert "boom message" in out
    assert "-32000" in out
    # The signal header must report the failing tool, not the trailing success.
    assert "**Tool:** `boom`" in out


def test_synthesize_adaptive_heading_last_successful_when_final_tool_ok(
    tmp_path: Path,
) -> None:
    """If the final tool call before the reactive SUCCEEDED, the section title
    is 'Last successful tool call before escalation' (the friction is in the
    NEXT step the agent didn't call). Don't lie by calling a successful call
    'where the friction surfaced.'"""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "list_tables"}),
            _event(1, "tool_call_end", {"tool_name": "list_tables", "duration_ms": 8}),
            _reactive(
                2,
                "feature_gap",
                intent="schedule a notebook",
                suggested_improvement="add a scheduling API",
            ),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "### Last successful tool call before escalation" in out
    assert "### Where the friction surfaced" not in out


def test_synthesize_priority_from_signal_type(tmp_path: Path) -> None:
    """signal_type maps to priority per the report's taxonomy. Spot-check
    the less-obvious ones; the full map is in ``_PRIORITY_BY_SIGNAL``."""
    path = tmp_path / "events.jsonl"
    _write_events(path, [_reactive(0, "dead_end", intent="x")])
    assert "**Priority:** `high`" in synthesize(str(path), "s1")

    _write_events(path, [_reactive(0, "parameter_confusion", intent="x")])
    assert "**Priority:** `medium`" in synthesize(str(path), "s1")

    _write_events(path, [_reactive(0, "failure", intent="x")])
    assert "**Priority:** `urgent`" in synthesize(str(path), "s1")


def test_synthesize_priority_bumped_by_downstream_blocked(tmp_path: Path) -> None:
    """medium + context.downstream_blocked=True -> high."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _reactive(
                0,
                "parameter_confusion",  # medium by default
                intent="x",
                context={"downstream_blocked": True},
            )
        ],
    )
    out = synthesize(str(path), "s1")
    assert "**Priority:** `high`" in out


# =============================================================================
# Multi-reactive rendering — TOC + inter-reactive cycle bounding
# =============================================================================


def test_synthesize_two_reactives_render_toc_and_two_blocks(tmp_path: Path) -> None:
    """Two reactives -> TOC at top + two ordered signal blocks."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _proactive(0, "investigate slow query"),
            _event(1, "tool_call_start", {"tool_name": "query"}),
            _event(2, "tool_call_end", {"tool_name": "query", "duration_ms": 4000}),
            _reactive(3, "slow_performance", intent="investigate slow query"),
            _proactive(4, "schedule a notebook"),
            _event(5, "tool_call_start", {"tool_name": "list_jobs"}),
            _event(6, "tool_call_end", {"tool_name": "list_jobs", "duration_ms": 8}),
            _reactive(7, "feature_gap", intent="schedule a notebook"),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "## Signals filed in this session" in out
    assert "1. `slow_performance`" in out
    assert "2. `feature_gap`" in out
    assert "## Signal 1 — `slow_performance`" in out
    assert "## Signal 2 — `feature_gap`" in out


def test_synthesize_single_reactive_suppresses_toc(tmp_path: Path) -> None:
    """For 1-signal sessions the TOC is redundant and is suppressed."""
    path = tmp_path / "events.jsonl"
    _write_events(path, [_reactive(0, "failure", intent="x")])
    out = synthesize(str(path), "s1")
    assert "## Signals filed in this session" not in out
    assert "## Signal 1" in out


def test_synthesize_inter_reactive_cycle_bounding(tmp_path: Path) -> None:
    """Reactive #2's reasoning trail must NOT include reactive #1's proactive
    or tool calls. The cycle is bounded to (prev_reactive_seq, this_seq].

    This is the long-Claude-session fix: without inter-reactive bounding, a
    late-session reactive's trail would replay every earlier friction's
    steps.
    """
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _proactive(0, "PROACTIVE_FROM_CYCLE_ONE"),
            _event(1, "tool_call_start", {"tool_name": "TOOL_FROM_CYCLE_ONE"}),
            _event(
                2,
                "tool_call_end",
                {"tool_name": "TOOL_FROM_CYCLE_ONE", "duration_ms": 3},
            ),
            _reactive(3, "failure", intent="cycle one reactive"),
            _proactive(4, "PROACTIVE_FROM_CYCLE_TWO"),
            _event(5, "tool_call_start", {"tool_name": "TOOL_FROM_CYCLE_TWO"}),
            _event(
                6,
                "tool_call_end",
                {"tool_name": "TOOL_FROM_CYCLE_TWO", "duration_ms": 9},
            ),
            _reactive(7, "feature_gap", intent="cycle two reactive"),
        ],
    )
    out = synthesize(str(path), "s1")

    # Split on Signal 2's heading. Cycle-one stuff should appear ONLY before
    # the split; cycle-two stuff should appear after.
    signal_2_idx = out.index("## Signal 2 — `feature_gap`")
    signal_1_section = out[:signal_2_idx]
    signal_2_section = out[signal_2_idx:]

    # Signal 1 sees cycle one's stuff.
    assert "PROACTIVE_FROM_CYCLE_ONE" in signal_1_section
    assert "TOOL_FROM_CYCLE_ONE" in signal_1_section

    # Signal 2 sees cycle two's stuff.
    assert "PROACTIVE_FROM_CYCLE_TWO" in signal_2_section
    assert "TOOL_FROM_CYCLE_TWO" in signal_2_section

    # Signal 2 must NOT carry cycle one's proactive or tool (inter-reactive
    # bounding). If this assertion fails, the bug is back.
    assert "PROACTIVE_FROM_CYCLE_ONE" not in signal_2_section
    assert "TOOL_FROM_CYCLE_ONE" not in signal_2_section


def test_synthesize_reasoning_trail_renders_multi_step_chain(tmp_path: Path) -> None:
    """A cycle with multiple (proactive, tool calls) sub-tasks renders as an
    ordered list, one step per item, with the tool calls under each step."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _proactive(0, "STEP_ONE_INTENT"),
            _event(1, "tool_call_start", {"tool_name": "step_one_tool"}),
            _event(
                2,
                "tool_call_end",
                {"tool_name": "step_one_tool", "duration_ms": 11},
            ),
            _proactive(3, "STEP_TWO_INTENT"),
            _event(4, "tool_call_start", {"tool_name": "step_two_tool"}),
            _event(
                5,
                "tool_call_error",
                {
                    "tool_name": "step_two_tool",
                    "error_type": "NotFound",
                    "error_body": "no such schedule",
                },
            ),
            _reactive(6, "feature_gap", intent="schedule a thing"),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "### Reasoning trail (what the agent tried, in order)" in out
    assert "STEP_ONE_INTENT" in out
    assert "STEP_TWO_INTENT" in out
    # Both step's tool calls render, with the error decorated.
    assert "step_one_tool" in out
    assert "step_two_tool" in out
    assert "NotFound" in out


# =============================================================================
# synthesize_scan() — mechanical + reactive merge
# =============================================================================


def test_synthesize_scan_folds_reactive_into_error_when_text_names_tool(
    tmp_path: Path,
) -> None:
    """A reactive that names the failing tool in its text but does NOT set an
    explicit tool field still folds into that tool's mechanical error finding,
    so the errored call renders as ONE rich finding (error evidence + the
    agent's suggested fix), not two."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _proactive(1, "check release hygiene"),
            _event(2, "tool_call_start", {"tool_name": "get_latest_release"}),
            _event(
                3,
                "tool_call_error",
                {
                    "tool_name": "get_latest_release",
                    "error_type": "0",
                    "error_body": "404 Not Found",
                },
            ),
            # No context.tool / tool_name — only the prose names the tool.
            _reactive(
                4,
                "dead_end",
                intent="Get the latest release to find the published version",
                suggested_improvement=(
                    "get_latest_release returns 404 even though the repo has tags; "
                    "surface tags when releases is empty."
                ),
            ),
        ],
    )
    out = synthesize_scan(str(path), "s1", server_label="github")
    # One finding, not two: header reports a single friction point.
    assert "**Friction points found** 1 " in out
    assert out.count("## Friction ") == 1
    # The mechanical error evidence and the model's suggested fix both render.
    assert "404 Not Found" in out
    assert "surface tags when releases is empty" in out


def test_synthesize_scan_keeps_unrelated_reactive_standalone(tmp_path: Path) -> None:
    """A reactive whose text does NOT name the errored tool stays a separate
    finding — inference must not over-merge."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(1, "tool_call_start", {"tool_name": "get_latest_release"}),
            _event(
                2,
                "tool_call_error",
                {"tool_name": "get_latest_release", "error_type": "0", "error_body": "404"},
            ),
            # Silent-success gap on a DIFFERENT tool that never errored.
            _reactive(
                3,
                "feature_gap",
                intent="search code for a string that exists",
                suggested_improvement="search_code should warn when the index is stale.",
            ),
        ],
    )
    out = synthesize_scan(str(path), "s1", server_label="github")
    assert "**Friction points found** 2 " in out
    assert out.count("## Friction ") == 2

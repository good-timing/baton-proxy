"""Tests for the friction-report synthesis (report.py).

Two surfaces:
  - Gating helpers (find_file_sink_path, has_http_sink, should_inject_report_tool)
    decide whether the report tool is injected. Mapped to product mode:
    gateway demo (local sink) -> yes, vendor production (http sink) -> no.
  - synthesize() reads the JSONL sink and templates markdown.
"""

from __future__ import annotations

import json
from pathlib import Path

from baton_proxy.report import (
    find_file_sink_path,
    has_http_sink,
    should_inject_report_tool,
    synthesize,
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
    """The proxy's zero-config default — stderr + file — IS gateway demo
    mode. Report tool MUST be injected here; this is the whole point."""
    assert should_inject_report_tool("stderr:,file:///tmp/baton-proxy.jsonl") is True


def test_should_inject_report_tool_for_file_only() -> None:
    assert should_inject_report_tool("file:///tmp/events.jsonl") is True


def test_should_NOT_inject_report_tool_for_stderr_only() -> None:
    """Pure stderr has nothing to read back from — no file means no report."""
    assert should_inject_report_tool("stderr:") is False


def test_should_NOT_inject_report_tool_for_http_only() -> None:
    """HTTP sink = vendor production mode. Vendor's Console renders tickets,
    not the proxy. No Baton-branded customer-facing surface in production."""
    assert should_inject_report_tool("https://console.example.com") is False


def test_should_NOT_inject_report_tool_when_any_http_present() -> None:
    """Even with a local file sink alongside, the presence of any http leg
    means the customer is in production mode and the report tool must be
    suppressed."""
    assert (
        should_inject_report_tool("file:///tmp/a.jsonl,https://console.example.com")
        is False
    )


# =============================================================================
# synthesize() — markdown rendering
# =============================================================================


def _write_events(path: Path, events: list[dict]) -> None:
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _event(seq: int, etype: str, payload: dict, *, session: str = "s1") -> dict:
    return {
        "event_id": f"ev{seq}",
        "event_type": etype,
        "session_id": session,
        "sequence_number": seq,
        "captured_at": f"2026-06-12T00:00:0{seq}Z",
        "payload": payload,
    }


def test_synthesize_empty_session_renders_placeholder(tmp_path: Path) -> None:
    """A fresh session before any tool calls hits this path — should
    return a friendly placeholder, not crash."""
    path = tmp_path / "events.jsonl"
    path.touch()
    out = synthesize(str(path), "s1")
    assert "No events captured yet" in out
    assert "s1" in out


def test_synthesize_missing_file_does_not_crash(tmp_path: Path) -> None:
    out = synthesize(str(tmp_path / "nonexistent.jsonl"), "s1")
    assert "No events" in out  # falls through the empty-events template


def test_synthesize_filters_to_current_session(tmp_path: Path) -> None:
    """Multi-proxy installs may share one JSONL — the report MUST only
    show events for the calling session."""
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "echo"}, session="s1"),
            _event(1, "tool_call_start", {"tool_name": "other"}, session="s2"),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "echo" in out
    assert "other" not in out


def test_synthesize_renders_tool_breakdown_and_errors(tmp_path: Path) -> None:
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
                    "duration_ms": 12,
                },
            ),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "Baton friction report" in out
    assert "Tool calls** 2" in out
    assert "Errors** 1" in out
    # Per-tool breakdown
    assert "`echo`" in out
    assert "`boom`" in out
    # Error detail surfaced
    assert "boom message" in out
    assert "-32000" in out


def test_synthesize_includes_annotations(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            _event(0, "tool_call_start", {"tool_name": "search"}),
            _event(
                1,
                "annotation",
                {
                    "signal_type": "failure",
                    "intent": "find the user's last order",
                    "suggested_improvement": "return available filters on empty result",
                },
            ),
        ],
    )
    out = synthesize(str(path), "s1")
    assert "Model-emitted annotations" in out
    assert "failure" in out
    assert "find the user's last order" in out
    assert "return available filters" in out


def test_synthesize_skips_malformed_jsonl_lines(tmp_path: Path) -> None:
    """A corrupt line shouldn't break the report — degrade gracefully."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(_event(0, "tool_call_start", {"tool_name": "echo"}))
        + "\n"
        + "{not valid json\n"
        + json.dumps(_event(1, "tool_call_end", {"tool_name": "echo", "duration_ms": 5}))
        + "\n"
    )
    out = synthesize(str(path), "s1")
    assert "echo" in out  # the two good lines made it through

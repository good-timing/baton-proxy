"""Unit tests for sinks.py — Sink implementations + URL-driven factory.

End-to-end integration (Emitter -> Sink -> destination) is covered in
test_emitter.py; these tests pin sink-local behavior so a regression
surfaces with a precise failure.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from baton_proxy.sinks import (
    FileSink,
    HttpSink,
    MultiSink,
    Sink,
    StderrSink,
    make_sink,
)


def _evt(event_type: str = "tool_call_start") -> dict[str, Any]:
    return {"event_type": event_type, "payload": {"tool_name": "t"}}


class _RecordingSink(Sink):
    def __init__(self, *, raise_on_write: Exception | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False
        self._raise = raise_on_write

    def write(self, event: dict[str, Any]) -> None:
        if self._raise is not None:
            raise self._raise
        self.events.append(event)

    def close(self) -> None:
        self.closed = True


# =============================================================================
# StderrSink
# =============================================================================


def test_stderr_sink_writes_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    """StderrSink writes one JSON line per event and flushes."""
    captured = io.StringIO()
    monkeypatch.setattr("sys.stderr", captured)
    s = StderrSink()
    s.write(_evt("tool_call_start"))
    s.write(_evt("tool_call_end"))
    s.close()
    lines = captured.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "tool_call_start"
    assert json.loads(lines[1])["event_type"] == "tool_call_end"


# =============================================================================
# FileSink
# =============================================================================


def test_file_sink_appends_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    s = FileSink(str(p))
    s.write(_evt("tool_call_start"))
    s.write(_evt("tool_call_end"))
    s.close()
    lines = p.read_text().splitlines()
    assert [json.loads(line)["event_type"] for line in lines] == [
        "tool_call_start",
        "tool_call_end",
    ]


def test_file_sink_flushes_per_line(tmp_path: Path) -> None:
    """Demo-critical: events visible to `cat` before close()."""
    p = tmp_path / "events.jsonl"
    s = FileSink(str(p))
    s.write(_evt("tool_call_start"))
    assert "tool_call_start" in p.read_text()  # no close needed
    s.close()


def test_file_sink_empty_path_raises() -> None:
    with pytest.raises(ValueError, match="non-empty path"):
        FileSink("")


# =============================================================================
# HttpSink — just the constructor; e2e wire test lives in test_emitter.py
# =============================================================================


def test_http_sink_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        HttpSink("https://example.com", api_key="")


# =============================================================================
# MultiSink — fan-out + failure isolation
# =============================================================================


def test_multi_sink_fans_out_to_each() -> None:
    a, b = _RecordingSink(), _RecordingSink()
    m = MultiSink([a, b])
    m.write(_evt("e"))
    assert len(a.events) == 1
    assert len(b.events) == 1


def test_multi_sink_continues_through_failure() -> None:
    """First sink raises; second sink still receives the event."""
    bad = _RecordingSink(raise_on_write=RuntimeError("dead"))
    good = _RecordingSink()
    m = MultiSink([bad, good])
    with pytest.raises(RuntimeError, match="dead"):
        m.write(_evt("e"))
    # The point: good still got called even though bad raised first.
    assert len(good.events) == 1


def test_multi_sink_closes_all_even_if_one_throws() -> None:
    class _BrokenClose(Sink):
        def write(self, event: dict[str, Any]) -> None:
            return

        def close(self) -> None:
            raise RuntimeError("close failed")

    good = _RecordingSink()
    m = MultiSink([_BrokenClose(), good])
    m.close()
    assert good.closed is True


def test_multi_sink_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        MultiSink([])


# =============================================================================
# make_sink — URL parsing
# =============================================================================


def test_make_sink_file(tmp_path: Path) -> None:
    p = tmp_path / "x.jsonl"
    s = make_sink(f"file://{p}", api_key=None)
    assert isinstance(s, FileSink)
    s.close()


def test_make_sink_stderr() -> None:
    assert isinstance(make_sink("stderr:", api_key=None), StderrSink)


def test_make_sink_http_requires_api_key() -> None:
    with pytest.raises(ValueError, match="BATON_API_KEY"):
        make_sink("https://example.com", api_key=None)


def test_make_sink_https_with_api_key() -> None:
    assert isinstance(make_sink("https://example.com", api_key="k"), HttpSink)


def test_make_sink_unsupported_scheme_raises() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        make_sink("kafka://broker:9092/topic", api_key="k")


def test_make_sink_comma_separated_builds_multi(tmp_path: Path) -> None:
    """`stderr:,file:///tmp/x.jsonl` is the canonical dev pattern: tee
    events to stderr (live view) and a file (post-hoc inspection)."""
    p = tmp_path / "events.jsonl"
    s = make_sink(f"stderr:,file://{p}", api_key=None)
    assert isinstance(s, MultiSink)
    s.close()


def test_make_sink_single_url_is_unwrapped() -> None:
    """A single URL doesn't get wrapped in MultiSink — avoids needless
    indirection on the common path."""
    s = make_sink("stderr:", api_key=None)
    assert not isinstance(s, MultiSink)


def test_make_sink_whitespace_tolerated(tmp_path: Path) -> None:
    """Trailing spaces around comma-separated URLs shouldn't break."""
    p = tmp_path / "events.jsonl"
    s = make_sink(f"stderr: ,  file://{p} ", api_key=None)
    assert isinstance(s, MultiSink)
    s.close()


def test_make_sink_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        make_sink(",,,", api_key=None)

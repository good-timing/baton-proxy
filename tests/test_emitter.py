"""Emitter tests — verify queued events reach the configured sink with the
IncomingEvent-compatible envelope. HTTP sink uses an in-process http.server
as the Console stand-in; file sink writes JSONL to a tmp path.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from baton_proxy.config import Config
from baton_proxy.emitter import Emitter


class _StubConsole(BaseHTTPRequestHandler):
    received: list[dict] = []
    auth_headers: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            self.received.append(json.loads(body))
        except json.JSONDecodeError:
            self.received.append({"_raw": body})
        self.auth_headers.append(self.headers.get("Authorization", ""))
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def log_message(self, *_args, **_kwargs) -> None:
        # Silence stub-server logging during tests.
        return


def _start_stub() -> tuple[HTTPServer, str]:
    _StubConsole.received = []
    _StubConsole.auth_headers = []
    server = HTTPServer(("127.0.0.1", 0), _StubConsole)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, url


def _config_http(url: str | None) -> Config:
    return Config(
        session_id="test-session",
        event_sink=url,
        tenant_id="t",
        api_key="k",
        consent_token="c",
        vendor_id="v",
        log_file=None,
    )


def _config_file(path: str, *, api_key: str | None = None) -> Config:
    """File-sink config — api_key defaults to None to exercise the path where
    it's optional (HTTP sinks require it; file sinks ignore it)."""
    return Config(
        session_id="test-session",
        event_sink=f"file://{path}",
        tenant_id="t",
        api_key=api_key,
        consent_token="c",
        vendor_id="v",
        log_file=None,
    )


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_emission_disabled_when_config_incomplete() -> None:
    """No event_sink -> emitter is a no-op; start() doesn't spin a thread."""
    e = Emitter(_config_http(None))
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={"text": "x"})
    e.stop()
    # No exception, no thread, no POST attempted. Asserting on internal _thread
    # is the cheapest way to verify start() was a no-op.
    assert e._thread is None  # noqa: SLF001


def test_emits_tool_call_start_end_error() -> None:
    server, url = _start_stub()
    try:
        e = Emitter(_config_http(url))
        e.start()
        e.enqueue_tool_call_start(tool_name="echo", params={"text": "hi"})
        e.enqueue_tool_call_end(tool_name="echo", result={"ok": True}, duration_ms=42)
        e.enqueue_tool_call_error(
            tool_name="boom", error_type="-32000", error_body="boom", duration_ms=11
        )
        assert _wait_for(lambda: len(_StubConsole.received) >= 3)
        e.stop()
    finally:
        server.shutdown()

    events_by_type = {ev["event_type"]: ev for ev in _StubConsole.received}
    assert set(events_by_type) == {"tool_call_start", "tool_call_end", "tool_call_error"}

    start = events_by_type["tool_call_start"]
    assert start["session_id"] == "test-session"
    assert start["tenant_id"] == "t"
    assert start["consent_token"] == "c"
    assert start["agent_runtime"] == "mcp-proxy"
    assert start["sdk_version"].startswith("baton-proxy/")
    assert start["payload"] == {"tool_name": "echo", "params": {"text": "hi"}}

    end = events_by_type["tool_call_end"]
    assert end["payload"]["duration_ms"] == 42
    assert end["payload"]["result"] == {"ok": True}

    err = events_by_type["tool_call_error"]
    assert err["payload"]["error_type"] == "-32000"

    # Auth header on every POST.
    assert all(h == "Bearer k" for h in _StubConsole.auth_headers)


def test_emits_annotation() -> None:
    server, url = _start_stub()
    try:
        e = Emitter(_config_http(url))
        e.start()
        e.enqueue_annotation(
            signal_type="failure",
            intent="search for X",
            suggested_improvement="distinguish 404 from transport error",
            # expected_outcome and workflow left as None — must be omitted.
        )
        assert _wait_for(lambda: len(_StubConsole.received) >= 1)
        e.stop()
    finally:
        server.shutdown()

    ann = _StubConsole.received[0]
    assert ann["event_type"] == "annotation"
    assert ann["payload"] == {
        "signal_type": "failure",
        "intent": "search for X",
        "suggested_improvement": "distinguish 404 from transport error",
    }
    # No None-valued keys leak into the wire payload.
    assert all(v is not None for v in ann["payload"].values())
    assert "expected_outcome" not in ann["payload"]
    assert "workflow" not in ann["payload"]
    assert "context" not in ann["payload"]


def test_sequence_numbers_are_monotonic() -> None:
    server, url = _start_stub()
    try:
        e = Emitter(_config_http(url))
        e.start()
        for i in range(5):
            e.enqueue_tool_call_start(tool_name=f"t{i}", params={})
        assert _wait_for(lambda: len(_StubConsole.received) >= 5)
        e.stop()
    finally:
        server.shutdown()

    seqs = [ev["sequence_number"] for ev in _StubConsole.received]
    assert seqs == sorted(seqs)
    assert seqs[0] == 0
    assert len(set(seqs)) == len(seqs)  # all distinct


def test_stop_is_clean_when_console_dead() -> None:
    """If the console URL is unreachable, the background thread still
    drains and exits on stop() without blocking proxy shutdown."""
    e = Emitter(_config_http("http://127.0.0.1:1"))  # nothing listening
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={})
    e.stop(timeout=3.0)
    assert e._thread is None  # noqa: SLF001


def test_emits_to_file_sink(tmp_path: Path) -> None:
    """End-to-end coverage that the Emitter routes events through whatever
    sink make_sink() returns — FileSink in this case. Per-sink unit
    coverage lives in test_sinks.py; this test is here to catch a
    regression in the Emitter -> Sink wiring (e.g. forgetting to call
    start() or stop() on the sink)."""
    sink_path = tmp_path / "events.jsonl"
    e = Emitter(_config_file(str(sink_path)))
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={"text": "hi"})
    e.enqueue_tool_call_end(tool_name="echo", result={"ok": True}, duration_ms=42)
    e.enqueue_tool_call_error(
        tool_name="boom", error_type="-32000", error_body="boom", duration_ms=11
    )
    assert _wait_for(
        lambda: sink_path.exists() and len(sink_path.read_text().splitlines()) >= 3
    )
    e.stop()

    events = [json.loads(line) for line in sink_path.read_text().splitlines()]
    assert [ev["event_type"] for ev in events] == [
        "tool_call_start",
        "tool_call_end",
        "tool_call_error",
    ]
    # Envelope is the same one the HTTP sink ships — Emitter is sink-agnostic.
    start = events[0]
    assert start["session_id"] == "test-session"
    assert start["tenant_id"] == "t"
    assert start["consent_token"] == "c"
    assert start["payload"] == {"tool_name": "echo", "params": {"text": "hi"}}


def test_misconfigured_sink_raises_at_start() -> None:
    """A bad BATON_EVENT_SINK (here: http without api_key) fails loudly at
    Emitter.start() rather than silently no-emitting events. The exact
    error catalogue is exercised in test_sinks.py — this test pins the
    propagation contract from sink construction up through start()."""
    config = Config(
        session_id="test-session",
        event_sink="https://example.com",
        tenant_id="t",
        api_key=None,
        consent_token="c",
        vendor_id="v",
        log_file=None,
    )
    e = Emitter(config)
    with pytest.raises(ValueError, match="BATON_API_KEY"):
        e.start()


def test_stop_drains_when_queue_was_full() -> None:
    """stop() must succeed even if the queue was full when stop was called.
    Previously put_nowait(None) would silently drop the sentinel and the
    drain thread would loop until daemon-killed at process exit."""
    server, url = _start_stub()
    try:
        e = Emitter(_config_http(url))
        # Shrink the queue so we can saturate it deterministically.
        e._queue = __import__("queue").Queue(maxsize=4)  # noqa: SLF001
        e.start()
        # Fill well past capacity to guarantee the queue stays at maxsize
        # during the stop() call (drop-oldest keeps room, but stop racing
        # with drain is what we're after).
        for i in range(50):
            e.enqueue_tool_call_start(tool_name=f"t{i}", params={})
        # Don't wait for drain — stop should still terminate cleanly.
        e.stop(timeout=5.0)
        assert e._thread is None  # noqa: SLF001
    finally:
        server.shutdown()

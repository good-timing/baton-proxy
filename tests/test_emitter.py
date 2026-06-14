"""Emitter tests — verify queued events reach the configured sink with the
IncomingEvent-compatible envelope. HTTP sink uses an in-process http.server
as the upstream HTTP stand-in; file sink writes JSONL to a tmp path.
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


class _StubReceiver(BaseHTTPRequestHandler):
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
    _StubReceiver.received = []
    _StubReceiver.auth_headers = []
    server = HTTPServer(("127.0.0.1", 0), _StubReceiver)
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
        assert _wait_for(lambda: len(_StubReceiver.received) >= 3)
        e.stop()
    finally:
        server.shutdown()

    events_by_type = {ev["event_type"]: ev for ev in _StubReceiver.received}
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
    assert all(h == "Bearer k" for h in _StubReceiver.auth_headers)


def test_scrubs_pii_before_payload_reaches_sink() -> None:
    """Source-side scrubbing: emails / tokens / API keys in tool params and
    results are redacted before the event lands at the sink. Both the file
    sink and HTTP sink see only [REDACTED:*] markers — that's the load-bearing
    trust contract for Persona B (Baton-hosted console)."""
    server, url = _start_stub()
    try:
        e = Emitter(_config_http(url))
        e.start()
        e.enqueue_tool_call_start(
            tool_name="search",
            params={"query": "find ujwal@goodtiming.ai please", "api_key": "should-be-redacted"},
        )
        e.enqueue_tool_call_end(
            tool_name="search",
            result={"matches": [{"email": "x@y.co"}]},
            duration_ms=10,
        )
        assert _wait_for(lambda: len(_StubReceiver.received) >= 2)
        e.stop()
    finally:
        server.shutdown()

    by_type = {ev["event_type"]: ev for ev in _StubReceiver.received}

    start_params = by_type["tool_call_start"]["payload"]["params"]
    assert "ujwal@goodtiming.ai" not in start_params["query"]
    assert "[REDACTED:email]" in start_params["query"]
    # api_key field-name override — entire value masked regardless of pattern.
    assert start_params["api_key"] == "[REDACTED:field-api_key]"

    end_result = by_type["tool_call_end"]["payload"]["result"]
    # Recursive walk into the nested list + dict reaches the email leaf
    # via the field-name override on "email".
    assert end_result["matches"][0]["email"] == "[REDACTED:field-email]"

    # Counter exposed for the report tool to consume.
    counts = e.scrub_counts()
    assert counts["email"] >= 1
    assert counts["field:api_key"] == 1
    assert counts["field:email"] == 1


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
        assert _wait_for(lambda: len(_StubReceiver.received) >= 1)
        e.stop()
    finally:
        server.shutdown()

    ann = _StubReceiver.received[0]
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
        assert _wait_for(lambda: len(_StubReceiver.received) >= 5)
        e.stop()
    finally:
        server.shutdown()

    seqs = [ev["sequence_number"] for ev in _StubReceiver.received]
    assert seqs == sorted(seqs)
    assert seqs[0] == 0
    assert len(set(seqs)) == len(seqs)  # all distinct


def test_stop_is_clean_when_remote_dead() -> None:
    """If the remote URL is unreachable, the background thread still
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
    assert _wait_for(lambda: sink_path.exists() and len(sink_path.read_text().splitlines()) >= 3)
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


def test_http_sink_with_placeholder_consent_refuses_at_start() -> None:
    """The zero-config install ships with consent_token='local'. Pointing
    that install at a remote sink without first replacing the consent token
    would leak placeholder-tagged events to the remote endpoint — refuse at
    start() so the operator sees an actionable error rather than silently-
    mistagged events later."""
    config = Config(
        session_id="test-session",
        event_sink="https://collector.example.com",
        tenant_id="t",
        api_key="k",
        consent_token="local",  # the placeholder
        vendor_id="v",
        log_file=None,
    )
    e = Emitter(config)
    with pytest.raises(ValueError, match="placeholder BATON_CONSENT_TOKEN"):
        e.start()


def test_local_sinks_with_placeholder_consent_are_fine(tmp_path: Path) -> None:
    """file:// and stderr: sinks NEVER trigger the consent guard — the
    placeholder is fine for purely local capture (the whole install-and-play
    flow runs in this mode)."""
    sink_path = tmp_path / "events.jsonl"
    config = Config(
        session_id="test-session",
        event_sink=f"stderr:,file://{sink_path}",
        tenant_id="local",
        api_key=None,
        consent_token="local",  # placeholder is OK for local sinks
        vendor_id="v",
        log_file=None,
    )
    e = Emitter(config)
    e.start()  # no raise
    e.enqueue_tool_call_start(tool_name="echo", params={})
    assert _wait_for(lambda: sink_path.exists() and sink_path.stat().st_size > 0)
    e.stop()


def test_http_sink_with_real_consent_starts_normally() -> None:
    """Smoke test for the inverse: once the operator sets a real
    BATON_CONSENT_TOKEN, the http sink starts cleanly."""
    config = Config(
        session_id="test-session",
        event_sink="http://127.0.0.1:1",  # unreachable; just need start() to succeed
        tenant_id="acme",
        api_key="k",
        consent_token="real-uuid-not-the-placeholder",
        vendor_id="v",
        log_file=None,
    )
    e = Emitter(config)
    e.start()  # no raise
    e.stop(timeout=3.0)


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

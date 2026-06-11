"""Emitter tests — verify queued events POST to the configured Console with
the IncomingEvent-compatible envelope. Uses an in-process http.server as the
Console stand-in.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

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


def _config_with(url: str | None) -> Config:
    return Config(
        session_id="test-session",
        console_url=url,
        tenant_id="t",
        api_key="k",
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
    """No console_url -> emitter is a no-op; start() doesn't spin a thread."""
    e = Emitter(_config_with(None))
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={"text": "x"})
    e.stop()
    # No exception, no thread, no POST attempted. Asserting on internal _thread
    # is the cheapest way to verify start() was a no-op.
    assert e._thread is None  # noqa: SLF001


def test_emits_tool_call_start_end_error() -> None:
    server, url = _start_stub()
    try:
        e = Emitter(_config_with(url))
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


def test_sequence_numbers_are_monotonic() -> None:
    server, url = _start_stub()
    try:
        e = Emitter(_config_with(url))
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
    e = Emitter(_config_with("http://127.0.0.1:1"))  # nothing listening
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={})
    e.stop(timeout=3.0)
    assert e._thread is None  # noqa: SLF001

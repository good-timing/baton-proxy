"""Tests for A1 proxy capture — resource/prompt event dispatch.

Covers:
- _emit_call_end and _emit_call_error dispatch by kind
- Eviction of resource/prompt pending calls emits the right error method
- report._derive_mechanical_findings picks up resource_read_error + prompt_get_error
"""

from __future__ import annotations

import sys
from collections import OrderedDict
from typing import Any

import pytest

from baton_proxy.proxy import (
    EVICTED_ERROR_TYPE,
    _ClientAction,
    _emit_call_end,
    _emit_call_error,
    _evict_overflow,
    _PendingCall,
    _pump_client_to_server,
)


class _CapturingEmitter:
    """Records all *_start, *_end, *_error calls by event type."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event_type: str, **kwargs: Any) -> None:
        self.calls.append((event_type, kwargs))

    def enqueue_tool_call_end(self, **kw: Any) -> None:
        self._record("tool_call_end", **kw)

    def enqueue_tool_call_error(self, **kw: Any) -> None:
        self._record("tool_call_error", **kw)

    def enqueue_resource_read_end(self, **kw: Any) -> None:
        self._record("resource_read_end", **kw)

    def enqueue_resource_read_error(self, **kw: Any) -> None:
        self._record("resource_read_error", **kw)

    def enqueue_resource_list_end(self, **kw: Any) -> None:
        self._record("resource_list_end", **kw)

    def enqueue_resource_list_error(self, **kw: Any) -> None:
        self._record("resource_list_error", **kw)

    def enqueue_prompt_get_end(self, **kw: Any) -> None:
        self._record("prompt_get_end", **kw)

    def enqueue_prompt_get_error(self, **kw: Any) -> None:
        self._record("prompt_get_error", **kw)

    def enqueue_prompt_list_end(self, **kw: Any) -> None:
        self._record("prompt_list_end", **kw)

    def enqueue_prompt_list_error(self, **kw: Any) -> None:
        self._record("prompt_list_error", **kw)


# --------------------------------------------------------------------------- #
# _emit_call_end dispatch
# --------------------------------------------------------------------------- #


def test_emit_call_end_tool() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="tool", subject="my_tool", started_ms=0, runtime_meta=None)
    _emit_call_end(em, call, {"out": 1}, 50)
    assert em.calls == [
        (
            "tool_call_end",
            {"tool_name": "my_tool", "result": {"out": 1}, "duration_ms": 50, "runtime_meta": None},
        )
    ]


def test_emit_call_end_resource_read() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(
        kind="resource_read", subject="file:///notes.txt", started_ms=0, runtime_meta=None
    )
    _emit_call_end(em, call, "...content...", 120)
    assert em.calls == [
        (
            "resource_read_end",
            {"uri": "file:///notes.txt", "duration_ms": 120, "runtime_meta": None},
        )
    ]


def test_emit_call_end_resource_list_extracts_count() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="resource_list", subject="", started_ms=0, runtime_meta=None)
    result = {"resources": [{"uri": "a"}, {"uri": "b"}, {"uri": "c"}]}
    _emit_call_end(em, call, result, 30)
    assert em.calls == [
        ("resource_list_end", {"count": 3, "duration_ms": 30, "runtime_meta": None})
    ]


def test_emit_call_end_resource_list_empty_result() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="resource_list", subject="", started_ms=0, runtime_meta=None)
    _emit_call_end(em, call, None, 10)
    assert em.calls == [
        ("resource_list_end", {"count": 0, "duration_ms": 10, "runtime_meta": None})
    ]


def test_emit_call_end_prompt_get() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="prompt_get", subject="summarize", started_ms=0, runtime_meta=None)
    _emit_call_end(em, call, {"messages": []}, 40)
    assert em.calls == [
        ("prompt_get_end", {"name": "summarize", "duration_ms": 40, "runtime_meta": None})
    ]


def test_emit_call_end_prompt_list_extracts_count() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="prompt_list", subject="", started_ms=0, runtime_meta=None)
    result = {"prompts": [{"name": "p1"}, {"name": "p2"}]}
    _emit_call_end(em, call, result, 20)
    assert em.calls == [("prompt_list_end", {"count": 2, "duration_ms": 20, "runtime_meta": None})]


# --------------------------------------------------------------------------- #
# _emit_call_error dispatch
# --------------------------------------------------------------------------- #


def test_emit_call_error_tool() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="tool", subject="my_tool", started_ms=0, runtime_meta=None)
    _emit_call_error(em, call, "timeout", "upstream did not respond", 5000)
    assert em.calls == [
        (
            "tool_call_error",
            {
                "tool_name": "my_tool",
                "error_type": "timeout",
                "error_body": "upstream did not respond",
                "duration_ms": 5000,
                "runtime_meta": None,
            },
        )
    ]


def test_emit_call_error_resource_read() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(
        kind="resource_read", subject="file:///secret.txt", started_ms=0, runtime_meta=None
    )
    _emit_call_error(em, call, "-32601", "Resource not found", 10)
    assert em.calls[0][0] == "resource_read_error"
    assert em.calls[0][1]["uri"] == "file:///secret.txt"
    assert em.calls[0][1]["error_type"] == "-32601"


def test_emit_call_error_resource_list() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="resource_list", subject="", started_ms=0, runtime_meta=None)
    _emit_call_error(em, call, "-32600", "Not supported", 5)
    assert em.calls[0][0] == "resource_list_error"
    assert "uri" not in em.calls[0][1]
    assert em.calls[0][1]["error_type"] == "-32600"


def test_emit_call_error_prompt_get() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(
        kind="prompt_get", subject="generate_email", started_ms=0, runtime_meta=None
    )
    _emit_call_error(em, call, "-32001", "Unknown prompt", 8)
    assert em.calls[0][0] == "prompt_get_error"
    assert em.calls[0][1]["name"] == "generate_email"


def test_emit_call_error_prompt_list() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="prompt_list", subject="", started_ms=0, runtime_meta=None)
    _emit_call_error(em, call, "unknown", "No prompts available", 3)
    assert em.calls[0][0] == "prompt_list_error"
    assert "name" not in em.calls[0][1]


# --------------------------------------------------------------------------- #
# Eviction dispatches correct error method for each kind
# --------------------------------------------------------------------------- #


def _make_single(kind: str, subject: str) -> OrderedDict[Any, _PendingCall]:
    from baton_proxy.proxy import MAX_PENDING

    pending: OrderedDict[Any, _PendingCall] = OrderedDict()
    for i in range(MAX_PENDING + 1):
        pending[i] = _PendingCall(kind=kind, subject=subject, started_ms=1000, runtime_meta=None)
    return pending


def test_eviction_resource_read() -> None:
    em = _CapturingEmitter()
    pending = _make_single("resource_read", "file:///doc.txt")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "resource_read_error"
    assert em.calls[0][1]["error_type"] == EVICTED_ERROR_TYPE
    assert em.calls[0][1]["uri"] == "file:///doc.txt"


def test_eviction_resource_list() -> None:
    em = _CapturingEmitter()
    pending = _make_single("resource_list", "")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "resource_list_error"
    assert em.calls[0][1]["error_type"] == EVICTED_ERROR_TYPE


def test_eviction_prompt_get() -> None:
    em = _CapturingEmitter()
    pending = _make_single("prompt_get", "my_prompt")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "prompt_get_error"
    assert em.calls[0][1]["name"] == "my_prompt"


def test_eviction_prompt_list() -> None:
    em = _CapturingEmitter()
    pending = _make_single("prompt_list", "")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "prompt_list_error"


# --------------------------------------------------------------------------- #
# _derive_mechanical_findings picks up resource/prompt errors
# --------------------------------------------------------------------------- #


def test_derive_mechanical_finds_resource_read_error() -> None:
    from baton_proxy.report import _derive_mechanical_findings

    events = [
        {
            "event_type": "resource_read_start",
            "payload": {"uri": "file:///notes.txt", "params": {}},
        },
        {
            "event_type": "resource_read_error",
            "payload": {
                "uri": "file:///notes.txt",
                "error_type": "403",
                "error_body": "Forbidden",
                "duration_ms": 50,
            },
        },
    ]
    findings = _derive_mechanical_findings(events)
    assert len(findings) == 1
    assert findings[0]["tool"] == "file:///notes.txt"
    assert findings[0]["error_type"] == "403"
    assert findings[0]["signal"] == "failure"


def test_derive_mechanical_finds_prompt_get_error() -> None:
    from baton_proxy.report import _derive_mechanical_findings

    events = [
        {"event_type": "prompt_get_start", "payload": {"name": "summarize", "params": {}}},
        {
            "event_type": "prompt_get_error",
            "payload": {
                "name": "summarize",
                "error_type": "-32601",
                "error_body": "Unknown prompt",
                "duration_ms": 10,
            },
        },
        {"event_type": "prompt_get_start", "payload": {"name": "summarize", "params": {}}},
        {
            "event_type": "prompt_get_error",
            "payload": {
                "name": "summarize",
                "error_type": "-32601",
                "error_body": "Unknown prompt",
                "duration_ms": 10,
            },
        },
    ]
    findings = _derive_mechanical_findings(events)
    assert len(findings) == 1
    assert findings[0]["tool"] == "summarize"
    assert findings[0]["count"] == 2
    assert findings[0]["signal"] == "retry_loop"


def test_derive_mechanical_mixed_tool_and_resource() -> None:
    from baton_proxy.report import _derive_mechanical_findings

    events = [
        {"event_type": "tool_call_start", "payload": {"tool_name": "search", "params": {}}},
        {
            "event_type": "tool_call_error",
            "payload": {
                "tool_name": "search",
                "error_type": "500",
                "error_body": "Internal error",
                "duration_ms": 100,
            },
        },
        {
            "event_type": "resource_read_start",
            "payload": {"uri": "file:///data.json", "params": {}},
        },
        {
            "event_type": "resource_read_error",
            "payload": {
                "uri": "file:///data.json",
                "error_type": "404",
                "error_body": "Not found",
                "duration_ms": 20,
            },
        },
    ]
    findings = _derive_mechanical_findings(events)
    assert len(findings) == 2
    subjects = {f["tool"] for f in findings}
    assert subjects == {"search", "file:///data.json"}


# ---------------------------------------------------------------------------
# Shutdown: closing the upstream's stdin is what lets run_proxy's shutdown run.
# ---------------------------------------------------------------------------


class _StubProcessor:
    """Forwards everything; the pump's shutdown is what is under test."""

    def handle_client_message(self, req: dict[str, Any]) -> _ClientAction:
        return _ClientAction(forward=req)


class _RecordingStdin:
    """Stands in for the upstream's stdin pipe."""

    def __init__(self) -> None:
        self.closed = False
        self.written: list[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _ExplodingStdin:
    """A client stream that yields one line, then dies mid-iteration.

    Not hypothetical on either count. `sys.stdin` decodes strict UTF-8, so one
    non-UTF-8 byte from a client raises `UnicodeDecodeError` out of the `for`
    itself; and `json.loads` on deeply nested input raises `RecursionError`,
    which `except json.JSONDecodeError` does not catch (measured: escapes at
    depth 200k on 3.14, far shallower on the 3.11 CI runs)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self._sent = False

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> str:
        if self._sent:
            raise self._exc
        self._sent = True
        return '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'


@pytest.mark.parametrize(
    "exc",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RecursionError("maximum recursion depth exceeded"),
    ],
    ids=["non-utf8-byte", "deeply-nested-json"],
)
def test_the_upstreams_stdin_is_closed_even_when_the_pump_dies(monkeypatch, exc):
    """The EOF path closes it; every other way out of that loop skipped it.

    An abrupt exit is exactly when the close matters most. `run_proxy` hangs
    its whole shutdown off `child.wait()` — `drain_pending`, then
    `emitter.stop()` — so an upstream left holding an open pipe means the last
    events are still in the queue when the client's SIGTERM lands. Silent
    no-emit, at the one moment nobody is watching."""
    child_stdin = _RecordingStdin()
    processor = _StubProcessor()
    monkeypatch.setattr(sys, "stdin", _ExplodingStdin(exc))

    with pytest.raises(type(exc)):
        _pump_client_to_server(child_stdin, processor)

    assert child_stdin.closed, "the upstream was left holding an open stdin"

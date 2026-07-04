"""Tests for the pending-call eviction logic.

The pending dict bounds memory at MAX_PENDING entries; once exceeded, the
oldest entry is evicted and a synthetic error event is emitted so the
worker can pair the dangling start with an end/error event.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

import pytest

from baton_proxy.proxy import (
    EVICTED_ERROR_TYPE,
    MAX_PENDING,
    MessageProcessor,
    _ClientAction,
    _evict_overflow,
    _Injection,
    _PendingCall,
)


class _RecordingEmitter:
    """Drop-in for Emitter that collects all *_error calls keyed by kind."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []
        self.starts: list[dict[str, Any]] = []

    def enqueue_tool_call_start(self, **kwargs: Any) -> None:
        self.starts.append(kwargs)

    def enqueue_tool_call_error(self, **kwargs: Any) -> None:
        self.errors.append({"kind": "tool", **kwargs})

    def enqueue_resource_read_error(self, **kwargs: Any) -> None:
        self.errors.append({"kind": "resource_read", **kwargs})

    def enqueue_resource_list_error(self, **kwargs: Any) -> None:
        self.errors.append({"kind": "resource_list", **kwargs})

    def enqueue_prompt_get_error(self, **kwargs: Any) -> None:
        self.errors.append({"kind": "prompt_get", **kwargs})

    def enqueue_prompt_list_error(self, **kwargs: Any) -> None:
        self.errors.append({"kind": "prompt_list", **kwargs})


def _make_pending(n: int) -> OrderedDict[Any, _PendingCall]:
    pending: OrderedDict[Any, _PendingCall] = OrderedDict()
    for i in range(n):
        pending[i] = _PendingCall(
            kind="tool", subject=f"t{i}", started_ms=1000 + i, runtime_meta=None
        )
    return pending


def test_no_eviction_under_cap() -> None:
    pending = _make_pending(MAX_PENDING)
    emitter = _RecordingEmitter()
    _evict_overflow(pending, emitter)  # type: ignore[arg-type]
    assert len(pending) == MAX_PENDING
    assert emitter.errors == []


def test_eviction_drops_oldest_and_emits_error() -> None:
    pending = _make_pending(MAX_PENDING + 3)
    emitter = _RecordingEmitter()
    _evict_overflow(pending, emitter)  # type: ignore[arg-type]
    assert len(pending) == MAX_PENDING

    # Three oldest evicted (ids 0, 1, 2), in order.
    assert [e["tool_name"] for e in emitter.errors] == [
        "t0",
        "t1",
        "t2",
    ]  # tool_name kwarg from enqueue_tool_call_error
    assert all(e["error_type"] == EVICTED_ERROR_TYPE for e in emitter.errors)
    assert all(e["duration_ms"] >= 0 for e in emitter.errors)
    # The evicted entries are gone; newer ones remain.
    assert 0 not in pending
    assert MAX_PENDING + 2 in pending


def test_eviction_survives_emitter_failure() -> None:
    """If the emitter throws, the eviction loop still completes."""

    class _BrokenEmitter:
        def enqueue_tool_call_error(self, **_kwargs: Any) -> None:
            raise RuntimeError("emit dead")

        def enqueue_resource_read_error(self, **_kwargs: Any) -> None:
            raise RuntimeError("emit dead")

        def enqueue_resource_list_error(self, **_kwargs: Any) -> None:
            raise RuntimeError("emit dead")

        def enqueue_prompt_get_error(self, **_kwargs: Any) -> None:
            raise RuntimeError("emit dead")

        def enqueue_prompt_list_error(self, **_kwargs: Any) -> None:
            raise RuntimeError("emit dead")

    pending = _make_pending(MAX_PENDING + 2)
    _evict_overflow(pending, _BrokenEmitter())  # type: ignore[arg-type]
    assert len(pending) == MAX_PENDING


def _bare_processor(emitter: Any) -> MessageProcessor:
    injection = _Injection(tools=[], instructions_suffix="", sink_path=None)
    return MessageProcessor(emitter, injection, "sess-test")  # type: ignore[arg-type]


def _track_tool_call(proc: MessageProcessor, req_id: int, name: str) -> None:
    proc.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        }
    )


def test_drain_pending_emits_error_for_each_outstanding() -> None:
    """Shutdown drain resolves every dangling *_start with a synthetic error."""
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    _track_tool_call(proc, 1, "t1")
    _track_tool_call(proc, 2, "t2")
    assert len(emitter.starts) == 2

    proc.drain_pending("proxy_upstream_closed", "gone")
    assert [e["tool_name"] for e in emitter.errors] == ["t1", "t2"]
    assert all(e["error_type"] == "proxy_upstream_closed" for e in emitter.errors)

    # Draining again is a no-op — pending was cleared.
    proc.drain_pending("x", "y")
    assert len(emitter.errors) == 2


def test_client_action_requires_exactly_one_field() -> None:
    """The respond/forward invariant is structural, so misuse fails loudly."""
    _ClientAction(respond={"a": 1})  # ok
    _ClientAction(forward={"a": 1})  # ok
    with pytest.raises(ValueError):
        _ClientAction()  # neither set
    with pytest.raises(ValueError):
        _ClientAction(respond={"a": 1}, forward={"b": 2})  # both set

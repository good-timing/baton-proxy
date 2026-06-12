"""Tests for the pending-call eviction logic.

The pending dict bounds memory at MAX_PENDING entries; once exceeded, the
oldest entry is evicted and a synthetic tool_call_error is emitted so the
worker can pair the dangling tool_call_start with an end/error event.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from baton_proxy.proxy import (
    EVICTED_ERROR_TYPE,
    MAX_PENDING,
    _evict_overflow,
    _PendingCall,
)


class _RecordingEmitter:
    """Drop-in for Emitter that just collects enqueue_tool_call_error calls."""

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    def enqueue_tool_call_error(self, **kwargs: Any) -> None:
        self.errors.append(kwargs)


def _make_pending(n: int) -> OrderedDict[Any, _PendingCall]:
    pending: OrderedDict[Any, _PendingCall] = OrderedDict()
    for i in range(n):
        pending[i] = _PendingCall(tool_name=f"t{i}", started_ms=1000 + i, runtime_meta=None)
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
    assert [e["tool_name"] for e in emitter.errors] == ["t0", "t1", "t2"]
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

    pending = _make_pending(MAX_PENDING + 2)
    _evict_overflow(pending, _BrokenEmitter())  # type: ignore[arg-type]
    assert len(pending) == MAX_PENDING

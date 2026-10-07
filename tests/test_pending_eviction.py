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
    INVALID_ID_ERROR_TYPE,
    MAX_PENDING,
    REUSED_ID_ERROR_TYPE,
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

    def enqueue_resource_read_start(self, **kwargs: Any) -> None:
        self.starts.append(kwargs)

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
            kind="tool",
            subject=f"t{i}",
            started_ms=1000 + i,
            runtime_meta=None,
            call_id=f"c{i}",
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
    assert [e["call_id"] for e in emitter.errors] == ["c0", "c1", "c2"]
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
    injection = _Injection(tools=[], instructions_suffix="")
    return MessageProcessor(emitter, injection, "sess-test")  # type: ignore[arg-type]


def _tools_call(name: str, **envelope: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
        **envelope,
    }


def test_drain_pending_emits_error_for_each_outstanding() -> None:
    """Shutdown drain resolves every dangling *_start with a synthetic error."""
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    proc.handle_client_message(_tools_call("t1", id=1))
    proc.handle_client_message(_tools_call("t2", id=2))
    assert len(emitter.starts) == 2

    proc.drain_pending("proxy_upstream_closed", "gone")
    assert [e["tool_name"] for e in emitter.errors] == ["t1", "t2"]
    assert all(e["error_type"] == "proxy_upstream_closed" for e in emitter.errors)
    start_ids = [s["call_id"] for s in emitter.starts]
    assert len(set(start_ids)) == 2 and all(start_ids)
    assert [e["call_id"] for e in emitter.errors] == start_ids

    # Draining again is a no-op — pending was cleared.
    proc.drain_pending("x", "y")
    assert len(emitter.errors) == 2


def test_a_reused_request_id_closes_the_call_it_displaces() -> None:
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    proc.handle_client_message(_tools_call("first", id=5))
    proc.handle_client_message(_tools_call("second", id=5))
    first, second = emitter.starts

    assert [(e["tool_name"], e["error_type"], e["call_id"]) for e in emitter.errors] == [
        ("first", REUSED_ID_ERROR_TYPE, first["call_id"])
    ]

    proc.drain_pending("proxy_upstream_closed", "gone")
    assert [(e["tool_name"], e["call_id"]) for e in emitter.errors[1:]] == [
        ("second", second["call_id"])
    ]


def test_a_reused_request_id_closes_a_displaced_resource_read() -> None:
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    for uri in ("file:///a", "file:///b"):
        proc.handle_client_message(
            {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": uri}}
        )

    assert [(e["kind"], e["uri"], e["error_type"]) for e in emitter.errors] == [
        ("resource_read", "file:///a", REUSED_ID_ERROR_TYPE)
    ]


def test_a_boolean_id_does_not_displace_the_call_pending_under_1() -> None:
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    proc.handle_client_message(_tools_call("one", id=1))
    proc.handle_client_message(_tools_call("bool", id=True))

    assert [(e["tool_name"], e["error_type"]) for e in emitter.errors] == [
        ("bool", INVALID_ID_ERROR_TYPE)
    ]
    proc.handle_server_message({"jsonrpc": "2.0", "id": True, "result": {}})
    proc.drain_pending("proxy_upstream_closed", "gone")
    assert [e["tool_name"] for e in emitter.errors[1:]] == ["one"]


@pytest.mark.parametrize("envelope", [{"id": [1]}, {"id": {"a": 1}}, {"id": None}, {}])
def test_a_request_id_no_response_can_match_closes_the_call_at_once(
    envelope: dict[str, Any],
) -> None:
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    proc.handle_client_message(_tools_call("t", **envelope))
    (start,) = emitter.starts

    assert [(e["tool_name"], e["error_type"], e["call_id"]) for e in emitter.errors] == [
        ("t", INVALID_ID_ERROR_TYPE, start["call_id"])
    ]
    proc.drain_pending("proxy_upstream_closed", "gone")
    assert len(emitter.errors) == 1


@pytest.mark.parametrize("bad_id", [[1], {"a": 1}])
def test_an_unhashable_id_from_the_upstream_or_transport_is_ignored(bad_id: Any) -> None:
    emitter = _RecordingEmitter()
    proc = _bare_processor(emitter)
    proc.handle_client_message(_tools_call("t", id=1))

    proc.handle_server_message({"jsonrpc": "2.0", "id": bad_id, "result": {}})
    proc.synthesize_pending_error(bad_id, "proxy_upstream_unreachable", "down")
    assert emitter.errors == []

    proc.drain_pending("proxy_upstream_closed", "gone")
    assert [e["tool_name"] for e in emitter.errors] == ["t"]


def test_client_action_requires_exactly_one_field() -> None:
    """The respond/forward invariant is structural, so misuse fails loudly."""
    _ClientAction(respond={"a": 1})  # ok
    _ClientAction(forward={"a": 1})  # ok
    with pytest.raises(ValueError):
        _ClientAction()  # neither set
    with pytest.raises(ValueError):
        _ClientAction(respond={"a": 1}, forward={"b": 2})  # both set

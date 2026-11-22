"""The tool list events (SPEC §11.4.5): one start and one terminal event for
each ``tools/list`` request the proxy forwards."""

from __future__ import annotations

import io
import sys
import threading
import time
from typing import Any

import pytest

from baton_proxy import proxy
from baton_proxy.proxy import ANNOTATE_TOOL_NAME, MessageProcessor, _Injection


class _RecordingEmitter:
    """Records every ``enqueue_<event_type>`` call as ``(event_type, kwargs)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("enqueue_"):
            raise AttributeError(name)
        return lambda **kwargs: self.calls.append((name.removeprefix("enqueue_"), kwargs))

    def listings(self) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if call[0].startswith("tool_list")]

    def of(self, *event_types: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name in event_types]


def _processor() -> tuple[MessageProcessor, _RecordingEmitter]:
    emitter = _RecordingEmitter()
    injection = _Injection.create(None, intent_param_mode="required")
    return MessageProcessor(emitter, injection, "test-session"), emitter  # type: ignore[arg-type]


def _list_request(msg_id: int, **params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/list", "params": params}


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"name": name, "inputSchema": {"type": "object", "properties": {}}} for name in names]


def test_a_listing_sends_a_start_then_an_end_counting_what_the_client_received() -> None:
    proc, emitter = _processor()
    meta = {"progressToken": 7}

    proc.handle_client_message(_list_request(2, _meta=meta))
    assert emitter.listings() == [("tool_list_start", {"runtime_meta": meta, "capture": None})]

    out = proc.handle_server_message(
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": _tools("search", "fetch")}}
    )

    received = [tool["name"] for tool in out["result"]["tools"]]
    assert received == ["search", "fetch", ANNOTATE_TOOL_NAME]
    (_start, (event_type, end)) = emitter.listings()
    assert event_type == "tool_list_end"
    assert end["count"] == len(received)
    assert end["duration_ms"] >= 0
    assert end["runtime_meta"] == meta
    assert set(end) == {"count", "duration_ms", "surface_hash", "runtime_meta", "capture"}


def test_each_page_of_a_listing_gets_its_own_pair() -> None:
    proc, emitter = _processor()

    proc.handle_client_message(_list_request(2))
    proc.handle_server_message(
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": _tools("a", "b"), "nextCursor": "p2"}}
    )
    proc.handle_client_message(_list_request(3, cursor="p2"))
    proc.handle_server_message({"jsonrpc": "2.0", "id": 3, "result": {"tools": _tools("c")}})

    assert [event_type for event_type, _ in emitter.listings()] == [
        "tool_list_start",
        "tool_list_end",
        "tool_list_start",
        "tool_list_end",
    ]
    assert [kwargs["count"] for _, kwargs in emitter.listings()[1::2]] == [2, 2]


def _list(proc: MessageProcessor, msg_id: int, result: dict[str, Any], **params: Any) -> None:
    proc.handle_client_message(_list_request(msg_id, **params))
    proc.handle_server_message({"jsonrpc": "2.0", "id": msg_id, "result": result})


def test_a_listing_names_the_snapshot_sent_before_its_end() -> None:
    proc, emitter = _processor()

    _list(proc, 2, {"tools": _tools("search")})

    snapshot, end = emitter.of("surface_snapshot", "tool_list_end")
    assert snapshot["surface_hash"].startswith("sha256:")
    assert end["surface_hash"] == snapshot["surface_hash"]


def test_a_relisting_names_the_snapshot_already_sent() -> None:
    proc, emitter = _processor()

    _list(proc, 2, {"tools": _tools("search")})
    _list(proc, 3, {"tools": _tools("search")})
    _list(proc, 4, {"tools": _tools("search", "fetch")})

    snapshots = [snapshot["surface_hash"] for snapshot in emitter.of("surface_snapshot")]
    assert len(snapshots) == 2
    assert [end["surface_hash"] for end in emitter.of("tool_list_end")] == [
        snapshots[0],
        snapshots[0],
        snapshots[1],
    ]


def test_a_paged_listing_names_no_snapshot() -> None:
    proc, emitter = _processor()

    _list(proc, 2, {"tools": _tools("a"), "nextCursor": "p2"})
    _list(proc, 3, {"tools": _tools("b")}, cursor="p2")

    assert emitter.of("surface_snapshot") == []
    assert [end["surface_hash"] for end in emitter.of("tool_list_end")] == [None, None]


def test_a_failed_snapshot_names_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_surface: Any) -> str:
        raise RuntimeError("hash failed")

    monkeypatch.setattr(proxy, "_surface_hash", boom)
    proc, emitter = _processor()

    _list(proc, 2, {"tools": _tools("search")})

    (end,) = emitter.of("tool_list_end")
    assert end["surface_hash"] is None
    assert end["count"] == 2


def test_a_snapshot_that_failed_to_send_is_sent_again_before_it_is_named() -> None:
    proc, emitter = _processor()
    sent = emitter.enqueue_surface_snapshot
    failures = iter([RuntimeError("queue closed")])

    def fails_once(**kwargs: Any) -> None:
        for failure in failures:
            raise failure
        sent(**kwargs)

    emitter.enqueue_surface_snapshot = fails_once  # type: ignore[attr-defined]

    _list(proc, 2, {"tools": _tools("search")})
    _list(proc, 3, {"tools": _tools("search")})

    first_end, snapshot, second_end = emitter.of("tool_list_end", "surface_snapshot")
    assert first_end["surface_hash"] is None
    assert second_end["surface_hash"] == snapshot["surface_hash"]


def test_two_listings_at_once_send_one_snapshot_before_either_end() -> None:
    proc, emitter = _processor()
    sent = emitter.enqueue_surface_snapshot
    sending, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def slow_send(**kwargs: Any) -> None:
        sending.set()
        release.wait(5)
        sent(**kwargs)

    def list_tools(msg_id: int) -> None:
        try:
            _list(proc, msg_id, {"tools": _tools("search")})
        except BaseException as error:
            errors.append(error)

    emitter.enqueue_surface_snapshot = slow_send  # type: ignore[attr-defined]
    first = threading.Thread(target=list_tools, args=(2,))
    second = threading.Thread(target=list_tools, args=(3,))
    first.start()
    assert sending.wait(5)
    second.start()
    second.join(0.2)
    assert second.is_alive()
    started = time.monotonic()
    proc.handle_client_message(_list_request(4))
    assert time.monotonic() - started < 1, "a client request waited on the snapshot"
    release.set()
    for thread in (first, second):
        thread.join(5)
        assert not thread.is_alive()

    assert errors == []
    order = [name for name, _ in emitter.calls if name in ("surface_snapshot", "tool_list_end")]
    assert order == ["surface_snapshot", "tool_list_end", "tool_list_end"]
    snapshot, *ends = emitter.of("surface_snapshot", "tool_list_end")
    assert [end["surface_hash"] for end in ends] == [snapshot["surface_hash"]] * 2


def test_a_listing_the_upstream_refuses_sends_an_error() -> None:
    proc, emitter = _processor()

    proc.handle_client_message(_list_request(2))
    proc.handle_server_message(
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "registry down"}}
    )

    (_start, (event_type, error)) = emitter.listings()
    assert event_type == "tool_list_error"
    assert error["error_type"] == "-32603"
    assert error["error_body"] == "registry down"
    assert error["duration_ms"] >= 0


def test_a_listing_never_answered_is_closed_with_an_error() -> None:
    proc, emitter = _processor()

    proc.handle_client_message(_list_request(2))
    proc.synthesize_pending_error(2, "proxy_upstream_unreachable", "connection refused")

    (_start, (event_type, error)) = emitter.listings()
    assert event_type == "tool_list_error"
    assert error["error_type"] == "proxy_upstream_unreachable"


def test_no_listing_event_carries_the_list() -> None:
    proc, emitter = _processor()

    proc.handle_client_message(_list_request(2))
    proc.handle_server_message({"jsonrpc": "2.0", "id": 2, "result": {"tools": _tools("search")}})

    assert len(emitter.listings()) == 2
    for _event_type, kwargs in emitter.listings():
        assert "search" not in repr(kwargs)


@pytest.mark.parametrize(
    "result", [["not", "an", "object"], "text", {"tools": None}, {"tools": "abc"}, None]
)
def test_a_malformed_listing_result_still_closes_the_listing(result: Any) -> None:
    proc, emitter = _processor()

    proc.handle_client_message(_list_request(2))
    proc.handle_server_message({"jsonrpc": "2.0", "id": 2, "result": result})

    (_start, (event_type, end)) = emitter.listings()
    assert event_type == "tool_list_end"
    assert end["count"] == 0


def test_a_listing_error_that_is_not_an_object_still_closes_the_listing() -> None:
    proc, emitter = _processor()

    proc.handle_client_message(_list_request(2))
    proc.handle_server_message({"jsonrpc": "2.0", "id": 2, "error": "boom"})

    (_start, (event_type, error)) = emitter.listings()
    assert event_type == "tool_list_error"
    assert error["error_type"] == "unknown"


def test_a_listing_the_http_upstream_accepts_and_never_answers_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AnswersNothing:
        def post(self, message: dict[str, Any]) -> list[dict[str, Any]]:
            return []

    proc, emitter = _processor()
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    )
    monkeypatch.setattr(proxy, "_write_stdout", written.append)

    proxy._run_http_loop(proc, AnswersNothing(), _Injection.create(None))

    (answer,) = written
    assert [tool["name"] for tool in answer["result"]["tools"]] == [ANNOTATE_TOOL_NAME]
    (_start, (event_type, error)) = emitter.listings()
    assert event_type == "tool_list_error"
    assert error["error_type"] == "proxy_no_response"

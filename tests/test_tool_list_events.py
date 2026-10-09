"""The tool list events (SPEC §11.4.5): one start and one terminal event for
each ``tools/list`` request the proxy forwards."""

from __future__ import annotations

import io
import sys
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
    assert emitter.listings() == [("tool_list_start", {"runtime_meta": meta})]

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
    assert set(end) == {"count", "duration_ms", "runtime_meta"}


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
    assert [kwargs["count"] for _, kwargs in emitter.listings()[1::2]] == [3, 2]


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

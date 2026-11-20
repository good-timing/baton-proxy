"""Withholding results for an audience with ``results: "off"`` (SPEC §2.6,
§11.2 item 6, §11.4.3): what each lane keeps and what it withholds."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from baton_proxy.config import Config
from baton_proxy.emitter import WITHHELD, Emitter
from baton_proxy.mcp_error import RETURNED_ERROR_TYPE
from baton_proxy.proxy import (
    HIDDEN_TOOL_ERROR_TYPE,
    MessageProcessor,
    _Injection,
)
from baton_proxy.settings import (
    RESULTS_OFF_WARNING,
    SettingsDocument,
    SettingsFetcher,
    _Answer,
    parse_document,
)
from baton_proxy.transport_http import UpstreamHTTPError


class _FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name == "set_agent_runtime":
            return lambda _name: None
        if not name.startswith("enqueue_"):
            raise AttributeError(name)
        kind = name.removeprefix("enqueue_")
        return lambda **kw: self.calls.append((kind, kw))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [kw for k, kw in self.calls if k == kind]


class _Held:
    vendor_id = "srv"

    def __init__(self, document: SettingsDocument | None) -> None:
        self.document = document


def _document(results: str = "off", **overlay: Any) -> SettingsDocument:
    body = {
        "version": 3,
        "audiences": [
            {"id": "quiet", "match": {"client": ["quiet"]}, "capture": {"results": results}},
            {"id": "everyone", "match": {}, "capture": {"results": "full"}},
        ],
        **overlay,
    }
    document = parse_document(json.dumps(body).encode())
    assert document is not None
    return document


def _processor(held: _Held, client: str = "quiet") -> tuple[MessageProcessor, _FakeEmitter]:
    emitter = _FakeEmitter()
    processor = MessageProcessor(emitter, _Injection.create(None), "s", settings=held)  # type: ignore[arg-type]
    processor.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"clientInfo": {"name": client}},
        }
    )
    return processor, emitter


def _call(processor: MessageProcessor, name: str = "lookup", req_id: int = 7) -> None:
    processor.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": {"q": "x"}},
        }
    )


def _answer(processor: MessageProcessor, req_id: int = 7, **body: Any) -> None:
    processor.handle_server_message({"jsonrpc": "2.0", "id": req_id, **body})


SECRET = "the account balance is 1,204"
RESULT = {"content": [{"type": "text", "text": SECRET}]}
ERROR_RESULT = {"content": [{"type": "text", "text": SECRET}], "isError": True}


def test_a_result_is_withheld_and_the_end_says_so() -> None:
    processor, emitter = _processor(_Held(_document()))
    _call(processor)
    _answer(processor, result=RESULT)
    (end,) = emitter.of("tool_call_end")
    assert end["withheld"] is True
    assert emitter.of("tool_call_start")[0]["params"] == {"q": "x"}


def test_a_returned_error_keeps_its_type_and_loses_its_text_and_result() -> None:
    processor, emitter = _processor(_Held(_document()))
    _call(processor)
    _answer(processor, result=ERROR_RESULT)
    (error,) = emitter.of("tool_call_error")
    assert error["error_type"] == RETURNED_ERROR_TYPE
    assert error["error_body"] == ""
    assert error["withheld"] is True


def test_an_upstream_json_rpc_error_loses_its_message() -> None:
    processor, emitter = _processor(_Held(_document()))
    _call(processor)
    _answer(processor, error={"code": -32000, "message": SECRET})
    (error,) = emitter.of("tool_call_error")
    assert error["error_type"] == "-32000"
    assert error["error_body"] == ""
    assert error["withheld"] is True


@pytest.mark.parametrize("results", ["full", "unknown-value"])
def test_an_audience_that_keeps_results_sends_them(results: str) -> None:
    processor, emitter = _processor(_Held(_document(results)))
    _call(processor)
    _answer(processor, result=ERROR_RESULT)
    (error,) = emitter.of("tool_call_error")
    assert error["error_body"] == SECRET
    assert error["result"] == ERROR_RESULT
    assert error["withheld"] is False


def test_another_audience_and_no_document_send_results() -> None:
    for held, client in ((_Held(_document()), "other"), (_Held(None), "quiet")):
        processor, emitter = _processor(held, client)
        _call(processor)
        _answer(processor, result=RESULT)
        (end,) = emitter.of("tool_call_end")
        assert end["withheld"] is False


def test_results_take_effect_at_the_next_call_without_a_new_listing() -> None:
    held = _Held(None)
    processor, emitter = _processor(held)
    processor.handle_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    _answer(processor, req_id=1, result={"tools": []})
    _call(processor, req_id=7)
    held.document = _document()
    _call(processor, req_id=8)
    _answer(processor, req_id=7, result=RESULT)
    _answer(processor, req_id=8, result=RESULT)
    assert [e["withheld"] for e in emitter.of("tool_call_end")] == [False, True]


def test_a_failure_the_proxy_wrote_keeps_its_text() -> None:
    processor, emitter = _processor(_Held(_document()))
    _call(processor, req_id=7)
    _call(processor, req_id=8)
    processor.synthesize_pending_error(7, "proxy_upstream_unreachable", "connection refused")
    processor.drain_pending("proxy_session_closed", "the client went away")
    bodies = [(e["error_body"], e["withheld"]) for e in emitter.of("tool_call_error")]
    assert bodies == [("connection refused", False), ("the client went away", False)]


def test_an_upstream_body_quoted_by_the_proxy_is_replaced() -> None:
    processor, emitter = _processor(_Held(_document()))
    _call(processor)
    processor.synthesize_pending_error(
        7, "proxy_upstream_unreachable", f"upstream returned HTTP 500: {SECRET}", "HTTP 500"
    )
    (error,) = emitter.of("tool_call_error")
    assert error["error_body"] == "HTTP 500"
    assert error["withheld"] is True


def test_a_hidden_tool_keeps_its_unknown_tool_text() -> None:
    body = {"overlays": {"ov": {"actions": [{"target": {"tool": "gone"}, "remove": True}]}}}
    document = parse_document(
        json.dumps(
            {
                "version": 3,
                "audiences": [
                    {"id": "quiet", "match": {}, "capture": {"results": "off"}, "overlay": "ov"}
                ],
                **body,
            }
        ).encode()
    )
    processor, emitter = _processor(_Held(document))
    _call(processor, "gone")
    (error,) = emitter.of("tool_call_error")
    assert error["error_type"] == HIDDEN_TOOL_ERROR_TYPE
    assert error["error_body"] == "Unknown tool: gone"
    assert error["withheld"] is False


def test_other_requests_keep_their_messages() -> None:
    processor, emitter = _processor(_Held(_document()))
    processor.handle_client_message(
        {"jsonrpc": "2.0", "id": 9, "method": "resources/read", "params": {"uri": "r://a"}}
    )
    _answer(processor, req_id=9, error={"code": -32002, "message": "not found: r://a"})
    (error,) = emitter.of("resource_read_error")
    assert error["error_body"] == "not found: r://a"


# --------------------------------------------------------------------------- #
# The emitter and the warning                                                 #
# --------------------------------------------------------------------------- #


def _captured_emitter() -> tuple[Emitter, list[dict[str, Any]]]:
    config = Config(
        session_id="s",
        event_sink=None,
        tenant_id="t",
        api_key="k",
        consent_token="c",
        vendor_id="v",
        log_file=None,
    )
    emitter = Emitter(config)
    captured: list[dict[str, Any]] = []
    emitter._enqueue = lambda **kw: captured.append(kw)  # type: ignore[method-assign]
    return emitter, captured


def test_a_withheld_result_never_enters_the_event() -> None:
    emitter, captured = _captured_emitter()
    emitter.enqueue_tool_call_end(
        tool_name="t", result=RESULT, duration_ms=1, call_id="c", withheld=True
    )
    (event,) = captured
    assert event["payload"] == {"tool_name": "t", "duration_ms": 1, "result_capture": "off"}


def test_the_warning_names_the_legs_that_keep_their_messages_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = json.dumps(
        {"version": 3, "audiences": [{"id": "a", "match": {}, "capture": {"results": "off"}}]}
    ).encode()
    answers = iter([_Answer(200, "e1", body), _Answer(200, "e2", body + b" ")])
    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=lambda *_: next(answers)
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        fetcher.read_once()
        fetcher.read_once()
    assert [r.getMessage() for r in caplog.records] == [RESULTS_OFF_WARNING]
    assert "tool_list_error" in RESULTS_OFF_WARNING


def test_a_string_error_code_is_not_sent_when_withheld() -> None:
    processor, emitter = _processor(_Held(_document()))
    _call(processor)
    _answer(processor, error={"code": SECRET, "message": "m"})
    (error,) = emitter.of("tool_call_error")
    assert error["error_type"] == "unknown"


def test_a_settings_lookup_that_fails_withholds(monkeypatch: pytest.MonkeyPatch) -> None:
    processor, emitter = _processor(_Held(_document()))

    def broken(*_: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(processor, "_call_settings", broken)
    _call(processor)
    _answer(processor, result=RESULT)
    assert emitter.of("tool_call_end")[0]["withheld"] is True


def test_the_emitter_leaves_a_withheld_error_result_out() -> None:
    emitter, captured = _captured_emitter()
    emitter.enqueue_tool_call_error(
        tool_name="t",
        error_type="tool_error",
        error_body="",
        duration_ms=1,
        result=ERROR_RESULT,
        call_id="c",
        withheld=True,
    )
    emitter.enqueue_tool_call_error(
        tool_name="t",
        error_type="x",
        error_body="b",
        duration_ms=1,
        call_id="c",
        withheld=False,
    )
    assert "result" not in captured[0]["payload"]
    assert captured[0]["payload"]["result_capture"] == WITHHELD
    assert "result_capture" not in captured[1]["payload"]


def test_the_warning_comes_again_each_time_results_go_off(caplog: pytest.LogCaptureFixture) -> None:
    def body(results: str) -> bytes:
        audience = {"id": "a", "match": {}, "capture": {"results": results}}
        return json.dumps({"version": 3, "audiences": [audience]}).encode()

    answers = iter(
        [
            _Answer(200, "1", body("off")),
            _Answer(200, "2", body("full")),
            _Answer(200, "3", body("off")),
        ]
    )
    fetcher = SettingsFetcher(
        origin="https://c", api_key="k", tenant_id="t", vendor_id="v", get=lambda *_: next(answers)
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        for _ in range(3):
            fetcher.read_once()
    assert [r.getMessage() for r in caplog.records] == [RESULTS_OFF_WARNING] * 2


@pytest.mark.parametrize(
    ("raised", "body"),
    [
        (lambda: UpstreamHTTPError(500, SECRET), "HTTP 500"),
        (lambda: ValueError(SECRET), "ValueError"),
    ],
    ids=["http-error", "other-exception"],
)
def test_the_http_bridge_sends_no_upstream_text_when_withheld(
    monkeypatch: pytest.MonkeyPatch, raised: Any, body: str
) -> None:
    from baton_proxy import proxy

    processor, emitter = _processor(_Held(_document()))
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "lookup", "arguments": {}},
    }

    class _Client:
        def post(self, _msg: Any) -> Any:
            raise raised()

    monkeypatch.setattr(proxy.sys, "stdin", io.StringIO(json.dumps(call) + "\n"))
    monkeypatch.setattr(proxy, "_write_client", lambda _payload: None)
    proxy._run_http_loop(processor, _Client(), _Injection.create(None))
    (error,) = emitter.of("tool_call_error")
    assert error["error_body"] == body
    assert error["withheld"] is True


def test_a_withheld_returned_error_reaches_the_event_with_no_upstream_text() -> None:
    emitter, captured = _captured_emitter()
    processor = MessageProcessor(emitter, _Injection.create(None), "s", settings=_Held(_document()))
    processor.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"clientInfo": {"name": "quiet"}},
        }
    )
    _call(processor)
    _answer(processor, result=ERROR_RESULT)
    (error,) = [e["payload"] for e in captured if e["event_type"] == "tool_call_error"]
    assert SECRET not in json.dumps(error)
    assert error["result_capture"] == WITHHELD


def test_a_failed_connection_keeps_the_text_the_proxy_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from baton_proxy import proxy

    processor, emitter = _processor(_Held(_document()))
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "lookup"}}

    class _Client:
        def post(self, _msg: Any) -> Any:
            raise RuntimeError("upstream connection failed: refused")

    monkeypatch.setattr(proxy.sys, "stdin", io.StringIO(json.dumps(call) + "\n"))
    monkeypatch.setattr(proxy, "_write_client", lambda _payload: None)
    proxy._run_http_loop(processor, _Client(), _Injection.create(None))
    (error,) = emitter.of("tool_call_error")
    assert error["error_body"] == "upstream connection failed: refused"
    assert error["withheld"] is False

"""Console settings on each connection (SPEC §2.6): the tool list, the
instructions, calls and the snapshot. Names follow baton-sdk's
``test_overlays.py`` and ``test_per_caller_surface.py`` where a test is a port."""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import pytest

from baton_proxy._llm_text import INTENT_INSTRUCTIONS, build_user_goal_param_description
from baton_proxy.overlay import parse_overlay
from baton_proxy.proxy import (
    ANNOTATE_TOOL_NAME,
    HIDDEN_TOOL_ERROR_TYPE,
    REFUSAL_TEXT,
    USER_GOAL_PARAM_NAME,
    MessageProcessor,
    _Injection,
)
from baton_proxy.settings import SettingsDocument, parse_document


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


def _tool(name: str, required: tuple[str, ...] = (), **props: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} original",
        "inputSchema": {
            "type": "object",
            "properties": {k: {"type": "string", "description": v} for k, v in props.items()},
            "required": list(required),
        },
    }


TOOLS = [
    _tool("book_slot", ("day",), day="The day.", timezone="A zone.", notes="Free text."),
    _tool("find_slots", (), day="The day.", duration="Minutes."),
    _tool("legacy_book"),
]

BETA_OVERLAY = {
    "actions": [
        {"target": {"tool": "book_slot"}, "replace": "Books one slot."},
        {"target": {"tool": "legacy_book"}, "remove": True},
        {"target": {"tool": "book_slot", "param": "notes"}, "remove": True},
        {"target": {"tool": "find_slots", "param": "duration"}, "required": True},
        {"target": {"agent_text": "goal_param.description"}, "replace": "What the user wants."},
        {"target": {"agent_text": "report_tool.description"}, "append": "Say which slot."},
        {"target": {"agent_text": "report_tool.refusal"}, "replace": "Only after a failure."},
    ]
}
SERVER_OVERLAY = {
    "actions": [
        {"target": {"instructions": True}, "append": "Times are local."},
        {"target": {"agent_text": "instructions.subagent"}, "append": "Pass the number on."},
    ]
}


def _document(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "version": 9,
        "audiences": [
            {
                "id": "beta",
                "match": {"client": ["beta-client"]},
                "capture": {"goal": "required", "reports": "on"},
                "overlay": "ov_beta_1",
            },
            {"id": "everyone", "match": {}, "capture": {"goal": "optional", "reports": "on"}},
        ],
        "overlays": {"ov_beta_1": BETA_OVERLAY, "ov_server_1": SERVER_OVERLAY},
        "server_overlay": "ov_server_1",
    }
    document.update(changes)
    return document


def _parsed(body: dict[str, Any]) -> SettingsDocument:
    document = parse_document(json.dumps(body).encode())
    assert document is not None
    return document


def _processor(
    held: _Held | None, client: str = "beta-client", **injection: Any
) -> tuple[MessageProcessor, _FakeEmitter]:
    emitter = _FakeEmitter()
    processor = MessageProcessor(
        emitter,  # type: ignore[arg-type]
        _Injection.create(None, **injection),
        "s",
        settings=held,
    )
    processor.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {"clientInfo": {"name": client}},
        }
    )
    return processor, emitter


def _initialize(processor: MessageProcessor, instructions: str = "Vendor text.") -> str:
    result = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "v"}}
    if instructions:
        result["instructions"] = instructions
    out = processor.handle_server_message({"jsonrpc": "2.0", "id": 0, "result": result})
    return out["result"]["instructions"]


def _list(
    processor: MessageProcessor, req_id: int = 1, cursor: str | None = None, **result: Any
) -> list[dict[str, Any]]:
    params = {"cursor": cursor} if cursor else {}
    processor.handle_client_message(
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/list", "params": params}
    )
    body = {"tools": copy.deepcopy(TOOLS), **result}
    out = processor.handle_server_message({"jsonrpc": "2.0", "id": req_id, "result": body})
    return out["result"]["tools"]


def _by_name(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {t["name"]: t for t in tools}


def _call(processor: MessageProcessor, name: str, args: dict[str, Any], req_id: int = 7) -> Any:
    return processor.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )


def _code_list(**injection: Any) -> list[dict[str, Any]]:
    processor, _ = _processor(None, **injection)
    return _list(processor)


# --------------------------------------------------------------------------- #
# The tool list                                                               #
# --------------------------------------------------------------------------- #


def test_two_callers_in_different_audiences_see_different_tool_lists() -> None:
    held = _Held(_parsed(_document()))
    beta = _by_name(_list(_processor(held, "beta-client")[0]))
    other = _by_name(_list(_processor(held, "other-client")[0]))

    assert "legacy_book" not in beta and "legacy_book" in other
    assert beta["book_slot"]["description"] == "Books one slot."
    assert other["book_slot"]["description"] == "book_slot original"
    assert "notes" not in beta["book_slot"]["inputSchema"]["properties"]
    assert beta["find_slots"]["inputSchema"]["required"] == ["duration", USER_GOAL_PARAM_NAME]
    assert other["find_slots"]["inputSchema"]["required"] == []


def test_with_no_settings_held_the_list_is_the_codes() -> None:
    processor, _ = _processor(_Held(None))
    assert _list(processor) == _code_list()


def test_a_caller_on_the_codes_settings_gets_the_result_unchanged() -> None:
    body = _document(audiences=[{"id": "everyone", "match": {}, "capture": {}}])
    processor, _ = _processor(_Held(_parsed(body)))
    assert _list(processor) == _code_list()


def test_the_audiences_goal_mode_and_text_reach_its_user_goal() -> None:
    held = _Held(_parsed(_document()))
    beta = _by_name(_list(_processor(held, "beta-client")[0]))
    other = _by_name(_list(_processor(held, "other-client")[0]))

    goal = beta["find_slots"]["inputSchema"]
    assert goal["properties"][USER_GOAL_PARAM_NAME]["description"] == "What the user wants."
    assert USER_GOAL_PARAM_NAME in goal["required"]
    served = other["find_slots"]["inputSchema"]["properties"][USER_GOAL_PARAM_NAME]
    assert served["description"] == build_user_goal_param_description(intent_param_mode="optional")


def test_a_tools_own_user_goal_is_left_as_it_is() -> None:
    processor, _ = _processor(_Held(_parsed(_document())))
    processor.handle_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    own = _tool("own_goal", (), user_goal="The vendor's own.")
    out = processor.handle_server_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": [own]}})
    (tool, _) = out["result"]["tools"]
    assert tool["inputSchema"]["properties"][USER_GOAL_PARAM_NAME]["description"] == (
        "The vendor's own."
    )


def test_the_report_tool_takes_the_audiences_description() -> None:
    held = _Held(_parsed(_document()))
    beta = _by_name(_list(_processor(held, "beta-client")[0]))
    other = _by_name(_list(_processor(held, "other-client")[0]))
    assert beta[ANNOTATE_TOOL_NAME]["description"].endswith("\n\nSay which slot.")
    assert not other[ANNOTATE_TOOL_NAME]["description"].endswith("Say which slot.")


def test_an_audience_with_reports_off_gets_no_report_tool() -> None:
    body = _document()
    body["audiences"][0]["capture"]["reports"] = "off"
    held = _Held(_parsed(body))
    assert ANNOTATE_TOOL_NAME not in _by_name(_list(_processor(held, "beta-client")[0]))
    assert ANNOTATE_TOOL_NAME in _by_name(_list(_processor(held, "other-client")[0]))


def test_a_vendor_who_turns_reports_off_keeps_them_off() -> None:
    processor, _ = _processor(_Held(_parsed(_document())), report_mode="off")
    assert ANNOTATE_TOOL_NAME not in _by_name(_list(processor))
    assert "Use `" not in _initialize(processor)


def test_a_new_document_reaches_the_next_listing_of_the_same_connection() -> None:
    held = _Held(None)
    processor, _ = _processor(held)
    assert "legacy_book" in _by_name(_list(processor, req_id=1))
    held.document = _parsed(_document())
    assert "legacy_book" not in _by_name(_list(processor, req_id=2))
    held.document = None
    assert _list(processor, req_id=3) == _code_list()


def test_a_later_page_keeps_the_settings_of_its_first_page() -> None:
    held = _Held(_parsed(_document()))
    processor, _ = _processor(held)
    assert "legacy_book" not in _by_name(_list(processor, req_id=1, nextCursor="p2"))
    held.document = None
    assert "legacy_book" not in _by_name(_list(processor, req_id=2, cursor="p2"))


def test_an_action_that_cannot_apply_is_skipped_alone(caplog: pytest.LogCaptureFixture) -> None:
    body = _document()
    body["overlays"]["ov_beta_1"] = {
        "actions": [
            {"target": {"tool": "nope"}, "append": "x"},
            {"target": {"tool": "book_slot", "param": "day"}, "remove": True},
            {"target": {"tool": "book_slot"}, "append": "Applied."},
        ]
    }
    processor, _ = _processor(_Held(_parsed(body)))
    with caplog.at_level(logging.ERROR, logger="baton_proxy"):
        tools = _by_name(_list(processor))
    assert tools["book_slot"]["description"].endswith("\n\nApplied.")
    assert "day" in tools["book_slot"]["inputSchema"]["properties"]
    assert "2 action(s) did not apply" in caplog.text


# --------------------------------------------------------------------------- #
# The instructions                                                            #
# --------------------------------------------------------------------------- #


def test_the_server_overlay_is_in_the_instructions() -> None:
    processor, _ = _processor(_Held(_parsed(_document())))
    instructions = _initialize(processor)
    assert instructions.startswith("Vendor text.\n\nTimes are local. Use `baton_annotate`")
    assert instructions.endswith(f"{INTENT_INSTRUCTIONS}\n\nPass the number on.")


def test_with_no_document_the_instructions_are_the_codes() -> None:
    held, code = _processor(_Held(None))[0], _processor(None)[0]
    assert _initialize(held) == _initialize(code)


def test_the_pointer_leaves_the_instructions_when_nobody_has_reports() -> None:
    body = _document()
    for audience in body["audiences"]:
        audience["capture"]["reports"] = "off"
    instructions = _initialize(_processor(_Held(_parsed(body)))[0])
    assert "Use `" not in instructions
    assert INTENT_INSTRUCTIONS in instructions


def test_a_connection_keeps_the_instructions_it_was_given() -> None:
    held = _Held(_parsed(_document()))
    processor, _ = _processor(held)
    first = _initialize(processor)
    held.document = None
    _list(processor)
    assert "Times are local." in first
    assert _initialize(_processor(held)[0]) == _initialize(_processor(None)[0])


def test_instruction_text_is_trimmed_and_an_empty_replace_removes_it() -> None:
    body = _document()
    body["overlays"]["ov_server_1"] = {
        "actions": [
            {"target": {"instructions": True}, "append": "Times are local.\n"},
            {"target": {"agent_text": "instructions.subagent"}, "replace": ""},
        ]
    }
    instructions = _initialize(_processor(_Held(_parsed(body)))[0], instructions="")
    assert instructions.startswith("Times are local. Use `")
    assert INTENT_INSTRUCTIONS not in instructions
    assert "\n\n\n" not in instructions and not instructions.endswith("\n")


def test_a_pointer_replaced_past_the_length_cap_is_still_served() -> None:
    long = "Report problems. " * 120
    body = _document()
    body["overlays"]["ov_server_1"] = {
        "actions": [{"target": {"agent_text": "instructions.report_pointer"}, "replace": long}]
    }
    instructions = _initialize(_processor(_Held(_parsed(body)))[0])
    assert long.rstrip() in instructions
    assert INTENT_INSTRUCTIONS in instructions


# --------------------------------------------------------------------------- #
# Calls                                                                       #
# --------------------------------------------------------------------------- #


def test_a_hidden_tool_answers_as_a_missing_one() -> None:
    processor, emitter = _processor(_Held(_parsed(_document())))
    _list(processor)
    action = _call(processor, "legacy_book", {})

    assert action.forward is None
    assert action.respond["error"]["message"] == "Unknown tool: legacy_book"
    (error,) = emitter.of("tool_call_error")
    assert error["error_type"] == HIDDEN_TOOL_ERROR_TYPE
    assert error["error_body"] == "Unknown tool: legacy_book"
    assert error["failure_kind"] == "tool_hidden"


def test_a_call_follows_the_list_the_agent_holds_until_it_lists_again() -> None:
    held = _Held(None)
    processor, _ = _processor(held)
    _list(processor, req_id=1)
    held.document = _parsed(_document())
    assert _call(processor, "legacy_book", {}, req_id=7).forward is not None
    action = _call(processor, "book_slot", {"day": "d", "notes": "n"}, req_id=8)
    assert action.forward["params"]["arguments"] == {"day": "d", "notes": "n"}
    assert (
        _call(processor, ANNOTATE_TOOL_NAME, {"user_goal": "1: x"}).respond["result"]["content"][0][
            "text"
        ]
        == REFUSAL_TEXT
    )
    _list(processor, req_id=2)
    assert _call(processor, "legacy_book", {}, req_id=9).forward is None


def test_a_call_before_any_listing_drops_no_param() -> None:
    action = _call(_processor(_Held(_parsed(_document())))[0], "book_slot", {"notes": "n"})
    assert action.forward["params"]["arguments"] == {"notes": "n"}


def test_a_degraded_listing_follows_the_callers_reports() -> None:
    body = _document()
    body["audiences"][0]["capture"]["reports"] = "off"
    processor, _ = _processor(_Held(_parsed(body)))
    degraded = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    assert processor.inject(processor.overlay_degraded(degraded, True))["result"]["tools"] == []


def test_a_degraded_later_page_keeps_the_settings_of_its_first_page() -> None:
    held = _Held(_parsed(_document()))
    processor, _ = _processor(held)
    _list(processor, req_id=1, nextCursor="p2")
    held.document = None
    processor.overlay_degraded({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}, False)
    assert _call(processor, "legacy_book", {}).forward is None


def test_a_server_overlay_sent_before_is_sent_again_when_it_comes_back() -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    for req_id, server_overlay in enumerate(("ov_server_1", None, "ov_server_1"), start=1):
        held.document = _parsed(_document(server_overlay=server_overlay))
        _initialize(processor)
        _list(processor, req_id=req_id)
    assert [s["server_overlay"] for s in _seams(emitter)] == ["ov_server_1", None, "ov_server_1"]


def test_a_call_before_any_listing_follows_the_callers_audience() -> None:
    held = _Held(_parsed(_document()))
    assert _call(_processor(held, "beta-client")[0], "legacy_book", {}).forward is None
    assert _call(_processor(held, "other-client")[0], "legacy_book", {}).forward is not None


def test_a_param_listed_as_required_is_not_enforced() -> None:
    processor, _ = _processor(_Held(_parsed(_document())))
    _list(processor)
    action = _call(processor, "find_slots", {"day": "2026-01-01"})
    assert action.forward["params"]["arguments"] == {"day": "2026-01-01"}


def test_a_hidden_param_is_dropped_from_the_call() -> None:
    processor, emitter = _processor(_Held(_parsed(_document())))
    _list(processor)
    action = _call(processor, "book_slot", {"day": "d", "notes": "n"})
    assert action.forward["params"]["arguments"] == {"day": "d"}
    assert emitter.of("tool_call_start")[0]["params"] == {"day": "d"}


def test_a_required_param_is_never_dropped_from_a_call() -> None:
    body = _document()
    body["overlays"]["ov_beta_1"] = {
        "actions": [{"target": {"tool": "book_slot", "param": "day"}, "remove": True}]
    }
    processor, _ = _processor(_Held(_parsed(body)))
    _list(processor)
    action = _call(processor, "book_slot", {"day": "d"})
    assert action.forward["params"]["arguments"] == {"day": "d"}


def test_the_refusal_follows_the_callers_overlay() -> None:
    held = _Held(_parsed(_document()))
    for client, text in (("beta-client", "Only after a failure."), ("other-client", REFUSAL_TEXT)):
        action = _call(_processor(held, client)[0], ANNOTATE_TOOL_NAME, {"user_goal": "1: x"})
        assert action.respond["result"]["content"][0]["text"] == text


def test_a_caller_without_reports_can_still_file_one() -> None:
    body = _document()
    body["audiences"][0]["capture"]["reports"] = "off"
    processor, emitter = _processor(_Held(_parsed(body)))
    _list(processor)
    args = {"user_goal": "1: x", "what_happened": "It failed.", "tool_name": "book_slot"}
    action = _call(processor, ANNOTATE_TOOL_NAME, args)
    assert action.respond is not None
    assert len(emitter.of("annotation")) == 1


# --------------------------------------------------------------------------- #
# The snapshot                                                                #
# --------------------------------------------------------------------------- #


def _seams(emitter: _FakeEmitter) -> list[dict[str, Any]]:
    return [s["seam_augmentations"] for s in emitter.of("surface_snapshot")]


def test_the_snapshot_names_the_server_overlay_and_keeps_the_codes_values() -> None:
    processor, emitter = _processor(_Held(_parsed(_document())), "beta-client")
    _initialize(processor)
    _list(processor)
    (seam,) = _seams(emitter)
    assert seam["server_overlay"] == "ov_server_1"
    assert seam["intent_param"]["mode"] == "optional"
    assert seam["injected_tools"] == [ANNOTATE_TOOL_NAME]
    assert [t["name"] for t in emitter.of("surface_snapshot")[0]["tools"]] == [
        t["name"] for t in TOOLS
    ]


def test_with_no_server_overlay_the_snapshot_sends_null() -> None:
    processor, emitter = _processor(_Held(_parsed(_document(server_overlay=None))))
    _initialize(processor)
    _list(processor)
    assert _seams(emitter)[0]["server_overlay"] is None


def test_without_console_settings_the_snapshot_leaves_server_overlay_out() -> None:
    processor, emitter = _processor(None)
    _initialize(processor)
    _list(processor)
    assert "server_overlay" not in _seams(emitter)[0]


def test_a_new_server_overlay_sends_a_new_snapshot_and_the_same_one_does_not() -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    _initialize(processor)
    _list(processor, req_id=1)
    _list(processor, req_id=2)
    held.document = _parsed(_document(server_overlay=None))
    _initialize(processor)
    _list(processor, req_id=3)
    assert [s["server_overlay"] for s in _seams(emitter)] == ["ov_server_1", None]


# --------------------------------------------------------------------------- #
# Setup                                                                       #
# --------------------------------------------------------------------------- #


def test_an_overlay_file_and_console_settings_cannot_both_apply() -> None:
    with pytest.raises(ValueError, match="cannot both apply"):
        MessageProcessor(
            _FakeEmitter(),  # type: ignore[arg-type]
            _Injection.create(None),
            "s",
            overlay=parse_overlay({"actions": []}),
            settings=_Held(None),
        )


def test_an_overlay_file_keeps_the_white_space_its_instructions_end_with() -> None:
    emitter = _FakeEmitter()
    overlay = parse_overlay({"actions": [{"target": {"instructions": True}, "append": "Local.\n"}]})
    processor = MessageProcessor(emitter, _Injection.create(None), "s", overlay=overlay)  # type: ignore[arg-type]
    assert "Local.\n Use `" in _initialize(processor)


# --------------------------------------------------------------------------- #
# capture on events (SPEC §11.4)                                              #
# --------------------------------------------------------------------------- #

BETA_CAPTURE = {"settings_version": 9, "audience": "beta", "overlay": "ov_beta_1"}


def test_a_call_records_the_settings_it_ran_under() -> None:
    processor, emitter = _processor(_Held(_parsed(_document())))
    _list(processor)
    _call(processor, "book_slot", {"day": "d"})
    processor.handle_server_message({"jsonrpc": "2.0", "id": 7, "result": {"content": []}})
    assert emitter.of("tool_call_start")[0]["capture"] == BETA_CAPTURE
    assert emitter.of("tool_call_end")[0]["capture"] == BETA_CAPTURE
    assert emitter.of("tool_list_start")[0]["capture"] == BETA_CAPTURE
    assert emitter.of("tool_list_end")[0]["capture"] == BETA_CAPTURE


def test_a_caller_no_audience_matches_records_the_version_alone() -> None:
    body = _document(audiences=[{"id": "beta", "match": {"client": ["x"]}, "capture": {}}])
    processor, emitter = _processor(_Held(_parsed(body)))
    _call(processor, "book_slot", {})
    assert emitter.of("tool_call_start")[0]["capture"] == {"settings_version": 9}


def test_without_a_document_or_with_an_overlay_file_no_event_records_capture() -> None:
    for processor, emitter in (_processor(_Held(None)), _processor(None)):
        _list(processor)
        _call(processor, "book_slot", {})
        assert {kw["capture"] for k, kw in emitter.calls if "capture" in kw} == {None}


def test_a_calls_end_records_what_its_start_did() -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    _list(processor, req_id=1)
    _call(processor, "book_slot", {})
    held.document = None
    _list(processor, req_id=2)
    processor.handle_server_message({"jsonrpc": "2.0", "id": 7, "result": {"content": []}})
    assert emitter.of("tool_call_end")[0]["capture"] == BETA_CAPTURE


def test_every_event_a_caller_makes_carries_capture_on_the_wire() -> None:
    from baton_proxy.config import Config
    from baton_proxy.emitter import Emitter

    config = Config(
        session_id="s",
        event_sink="stderr:",
        tenant_id="t",
        api_key="k",
        consent_token="c",
        vendor_id="v",
        log_file=None,
    )
    emitter = Emitter(config)
    emitter._thread = object()  # type: ignore[assignment]
    processor = MessageProcessor(
        emitter, _Injection.create(None), "s", settings=_Held(_parsed(_document()))
    )
    hello = {"clientInfo": {"name": "beta-client"}}
    processor.handle_client_message(
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": hello}
    )
    _initialize(processor)
    _list(processor)
    report = {"user_goal": "1: x", "what_happened": "It failed.", "tool_name": "book_slot"}
    _call(processor, ANNOTATE_TOOL_NAME, report, req_id=6)
    _call(processor, "legacy_book", {}, req_id=7)
    _call(processor, "book_slot", {}, req_id=8)
    processor.handle_server_message({"jsonrpc": "2.0", "id": 8, "result": {"content": []}})
    _call(processor, "book_slot", {}, req_id=9)
    failed = {"content": [], "isError": True}
    processor.handle_server_message({"jsonrpc": "2.0", "id": 9, "result": failed})
    methods = ("resources/read", "resources/list", "prompts/get", "prompts/list")
    for req_id, method in enumerate(methods, start=20):
        for answer in ({"result": {}}, {"error": {"code": 1}}):
            params = {"uri": "r", "name": "p"}
            processor.handle_client_message(
                {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
            )
            processor.handle_server_message({"jsonrpc": "2.0", "id": req_id, **answer})
    processor.handle_client_message({"jsonrpc": "2.0", "id": 40, "method": "tools/list"})
    processor.handle_server_message({"jsonrpc": "2.0", "id": 40, "error": {"code": 1}})
    _call(processor, "book_slot", {}, req_id=41)
    processor.drain_pending("proxy_session_closed", "gone")

    events = []
    while not emitter._queue.empty():
        event = emitter._queue.get_nowait()
        assert event is not None
        events.append(event.to_json())
    lanes = (
        "tool_call",
        "tool_list",
        "resource_read",
        "resource_list",
        "prompt_get",
        "prompt_list",
    )
    assert {e["event_type"] for e in events} == {
        "surface_snapshot",
        "annotation",
        *(f"{lane}_{leg}" for lane in lanes for leg in ("start", "end", "error")),
    }
    for event in events:
        if event["event_type"] == "surface_snapshot":
            assert "capture" not in event
        else:
            assert event["capture"] == BETA_CAPTURE, event["event_type"]


def test_one_call_records_the_document_its_results_followed_and_the_overlay_it_was_served() -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    _list(processor)
    body = _document(version=10)
    body["audiences"][0]["capture"]["results"] = "off"
    body["overlays"]["ov_beta_2"] = {"actions": []}
    body["audiences"][0]["overlay"] = "ov_beta_2"
    held.document = _parsed(body)
    _call(processor, "book_slot", {})
    start = emitter.of("tool_call_start")[0]
    assert start["capture"] == {"settings_version": 10, "audience": "beta", "overlay": "ov_beta_1"}
    processor.handle_server_message({"jsonrpc": "2.0", "id": 7, "result": {"content": []}})
    assert emitter.of("tool_call_end")[0]["withheld"] is True


def test_a_later_page_records_its_first_pages_settings() -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    _list(processor, req_id=1, nextCursor="p2")
    held.document = _parsed(_document(version=10))
    _list(processor, req_id=2, cursor="p2")
    starts = [kw["capture"] for kw in emitter.of("tool_list_start")]
    assert starts == [BETA_CAPTURE, BETA_CAPTURE]


def test_a_listings_end_records_the_settings_it_was_answered_under() -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    processor.handle_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    held.document = _parsed(_document(version=10))
    processor.handle_server_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    assert emitter.of("tool_list_start")[0]["capture"] == BETA_CAPTURE
    assert emitter.of("tool_list_end")[0]["capture"] == {**BETA_CAPTURE, "settings_version": 10}


def test_a_listing_that_could_not_take_new_settings_keeps_its_starts_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = _Held(_parsed(_document()))
    processor, emitter = _processor(held)
    processor.handle_client_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    def broken() -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(processor, "_call_settings", broken)
    processor.handle_server_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    assert emitter.of("tool_list_end")[0]["capture"] == BETA_CAPTURE


def test_a_refusal_overlay_that_fails_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from baton_proxy import proxy

    processor, _ = _processor(_Held(_parsed(_document())))
    _list(processor)

    def broken(*_: Any) -> Any:
        raise RuntimeError("broken")

    monkeypatch.setattr(proxy, "overlaid", broken)
    action = _call(processor, ANNOTATE_TOOL_NAME, {"user_goal": "1: x"})
    assert action.respond["result"]["content"][0]["text"] == REFUSAL_TEXT

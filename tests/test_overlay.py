from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from baton_proxy.overlay import OverlayError, _apply_one, load_overlay, parse_overlay
from baton_proxy.proxy import (
    HIDDEN_TOOL_ERROR_TYPE,
    USER_GOAL_PARAM_NAME,
    MessageProcessor,
    _Injection,
)


class _FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("enqueue_"):
            raise AttributeError(name)
        kind = name.removeprefix("enqueue_")
        return lambda **kw: self.calls.append((kind, kw))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [kw for k, kw in self.calls if k == kind]


def _tool(name: str, **props: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} original",
        "inputSchema": {
            "type": "object",
            "properties": {k: dict(v) for k, v in props.items()},
            "required": list(props),
        },
    }


def _with_optional(tool: dict[str, Any], **props: dict[str, Any]) -> dict[str, Any]:
    tool["inputSchema"]["properties"].update(props)
    return tool


TOOLS = [
    _tool("get_workspace", workspace_id={"type": "string"}),
    _with_optional(
        _tool("create_fields", record_type_id={"type": "string", "description": "orig"}),
        note={"type": "string"},
        note_kind={"type": "string"},
    ),
    _tool("debug_dump"),
]

OVERLAY = {
    "actions": [
        {"target": {"instructions": True}, "append": "Write tools exist; use them."},
        {"target": {"tool": "create_fields"}, "replace": "Adds fields."},
        {
            "target": {"tool": "create_fields", "param": "record_type_id"},
            "replace": "The id from get_workspace, verbatim.",
        },
        {"target": {"tool": "create_fields", "param": "note"}, "remove": True},
        {"target": {"tool": "create_fields", "param": "note_kind"}, "required": True},
        {"target": {"tool": "debug_dump"}, "remove": True},
    ]
}


def _processor(overlay: dict[str, Any] | None = OVERLAY):
    emitter = _FakeEmitter()
    injection = _Injection.create(None)
    parsed = parse_overlay(overlay) if overlay is not None else None
    return MessageProcessor(emitter, injection, "s", overlay=parsed), emitter  # type: ignore[arg-type]


def _list(processor: MessageProcessor, tools: list[dict[str, Any]], req_id: int = 2):
    processor.handle_client_message({"jsonrpc": "2.0", "id": req_id, "method": "tools/list"})
    msg = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": json.loads(json.dumps(tools))}}
    return processor.handle_server_message(msg)["result"]["tools"]


def _call(processor: MessageProcessor, name: str, args: dict[str, Any], req_id: int = 3):
    req = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    return processor.handle_client_message(req)


def test_the_agent_sees_the_overlaid_surface():
    processor, _ = _processor()
    tools = {t["name"]: t for t in _list(processor, TOOLS)}

    assert "debug_dump" not in tools
    create = tools["create_fields"]
    assert create["description"] == "Adds fields."
    props = create["inputSchema"]["properties"]
    assert props["record_type_id"]["description"] == "The id from get_workspace, verbatim."
    assert "note" not in props
    assert "note_kind" in create["inputSchema"]["required"]
    assert tools["get_workspace"]["description"] == "get_workspace original"


def test_appends_apply_in_order_as_new_paragraphs():
    overlay = parse_overlay(
        {
            "actions": [
                {"target": {"tool": "t"}, "append": "Second."},
                {"target": {"tool": "t"}, "append": "Third."},
                {"target": {"tool": "t", "param": "p"}, "append": "Use an id."},
            ]
        }
    )
    tool = {
        "name": "t",
        "description": "First.\n \u3000",
        "inputSchema": {"properties": {"p": {"type": "string"}}},
    }
    (out,) = overlay.apply_to_tools([tool])
    assert out["description"] == "First.\n\nSecond.\n\nThird."
    assert out["inputSchema"]["properties"]["p"]["description"] == "Use an id."
    assert tool["description"] == "First.\n \u3000"


def test_append_trims_unicode_white_space_only():
    overlay = parse_overlay({"actions": [{"target": {"tool": "t"}, "append": "More."}]})
    (out,) = overlay.apply_to_tools([{"name": "t", "description": "Sep\x1f"}])
    assert out["description"] == "Sep\x1f\n\nMore."


def test_an_empty_replace_removes_the_text():
    overlay = parse_overlay({"actions": [{"target": {"tool": "t"}, "replace": ""}]})
    (out,) = overlay.apply_to_tools([{"name": "t", "description": "old"}])
    assert out["description"] == ""


def test_a_required_param_is_never_hidden_and_is_reported():
    overlay = parse_overlay(
        {
            "actions": [
                {"target": {"tool": "get_workspace", "param": "workspace_id"}, "remove": True}
            ]
        }
    )
    assert overlay.hidden_params("get_workspace") == frozenset({"workspace_id"})
    (out,) = overlay.apply_to_tools([TOOLS[0]])
    assert "workspace_id" in out["inputSchema"]["properties"]
    assert overlay.hidden_params("get_workspace") == frozenset()
    assert overlay.unmatched() == ["actions[0] remove get_workspace.workspace_id"]


def test_required_is_only_advertised_and_survives_the_goal_param():
    processor, _ = _processor()
    tools = {t["name"]: t for t in _list(processor, TOOLS)}
    assert "note_kind" in tools["create_fields"]["inputSchema"]["required"]
    action = _call(processor, "create_fields", {"record_type_id": "r1"})
    assert action.forward is not None


def test_a_change_to_a_hidden_tool_is_reported_not_counted():
    overlay = parse_overlay(
        {
            "actions": [
                {"target": {"tool": "debug_dump"}, "remove": True},
                {"target": {"tool": "debug_dump", "param": "nope"}, "replace": "x"},
            ]
        }
    )
    assert overlay.apply_to_tools([TOOLS[2]]) == []
    assert overlay.unmatched() == ["actions[1] replace debug_dump.nope"]


def test_a_boolean_param_schema_is_left_alone_and_reported():
    overlay = parse_overlay({"actions": [{"target": {"tool": "t", "param": "p"}, "append": "x"}]})
    tool = {"name": "t", "inputSchema": {"properties": {"p": False}}}
    (out,) = overlay.apply_to_tools([tool])
    assert out["inputSchema"]["properties"]["p"] is False
    assert overlay.unmatched() == ["actions[0] append t.p"]


def test_an_instruction_append_is_never_listed_as_unmatched():
    overlay = parse_overlay({"actions": [{"target": {"instructions": True}, "append": "x"}]})
    assert overlay.unmatched() == []


def test_the_goal_param_is_injected_after_the_overlay():
    processor, _ = _processor()
    tools = {t["name"]: t for t in _list(processor, TOOLS)}
    assert USER_GOAL_PARAM_NAME in tools["create_fields"]["inputSchema"]["properties"]


def test_the_snapshot_keeps_the_vendor_true_surface_and_names_the_overlay():
    processor, emitter = _processor()
    _list(processor, TOOLS)

    (snapshot,) = emitter.of("surface_snapshot")
    assert [t["name"] for t in snapshot["tools"]] == [t["name"] for t in TOOLS]
    assert snapshot["tools"][1]["description"] == "create_fields original"
    overlay = snapshot["seam_augmentations"]["overlay"]
    assert overlay["hash"].startswith("sha256:")
    assert overlay["unmatched_targets"] == []


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {"protocolVersion": "2025-03-26", "instructions": "vendor text  \n"},
}


def test_instructions_are_appended_after_the_vendors_and_before_the_suffix():
    processor, _ = _processor()
    out = processor.handle_server_message(json.loads(json.dumps(_INIT)))
    instructions = out["result"]["instructions"]
    assert instructions.startswith("vendor text\n\nWrite tools exist; use them.")


def test_without_an_overlay_the_surface_is_untouched():
    processor, emitter = _processor(None)
    tools = _list(processor, TOOLS)
    assert [t["name"] for t in tools][:3] == [t["name"] for t in TOOLS]
    assert "overlay" not in emitter.of("surface_snapshot")[0]["seam_augmentations"]


def test_a_hidden_param_is_dropped_before_the_vendor_and_the_start_event():
    processor, emitter = _processor()
    action = _call(processor, "create_fields", {"record_type_id": "r1", "note": "x"})

    assert action.forward["params"]["arguments"] == {"record_type_id": "r1"}
    assert emitter.of("tool_call_start")[0]["params"] == {"record_type_id": "r1"}


def test_a_hidden_tool_is_refused_here_and_recorded():
    processor, emitter = _processor()
    action = _call(processor, "debug_dump", {})

    assert action.forward is None
    assert action.respond["error"]["message"] == "Unknown tool: debug_dump"
    (error,) = emitter.of("tool_call_error")
    assert error["error_type"] == HIDDEN_TOOL_ERROR_TYPE


def test_an_action_the_server_cannot_take_is_logged_once(caplog):
    overlay = {"actions": [{"target": {"tool": "nope"}, "append": "x"}]}
    processor, emitter = _processor(overlay)
    with caplog.at_level(logging.ERROR, logger="baton_proxy"):
        _list(processor, TOOLS, req_id=2)
        _list(processor, TOOLS, req_id=4)

    errors = [r for r in caplog.records if "did not apply" in r.getMessage()]
    assert len(errors) == 1
    assert "actions[0] append nope" in errors[0].getMessage()
    snapshot = emitter.of("surface_snapshot")[0]
    assert snapshot["seam_augmentations"]["overlay"]["unmatched_targets"] == [
        "actions[0] append nope"
    ]


def test_dotted_tool_names_resolve():
    overlay = parse_overlay({"actions": [{"target": {"tool": "ns.get"}, "replace": "d"}]})
    (out,) = overlay.apply_to_tools([{"name": "ns.get", "description": "x"}])
    assert out["description"] == "d"


def test_the_hash_is_the_content_not_the_file(tmp_path: Path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(OVERLAY))
    b.write_text(json.dumps(OVERLAY, indent=4))
    assert load_overlay(str(a)).hash == load_overlay(str(b)).hash
    changed = json.loads(json.dumps(OVERLAY))
    changed["actions"][1]["replace"] = "Adds fields!"
    assert parse_overlay(changed).hash != load_overlay(str(a)).hash


def _one(action: dict[str, Any]) -> dict[str, Any]:
    return {"actions": [action]}


@pytest.mark.parametrize(
    "doc, message",
    [
        ({"overlay": 1, "actions": []}, "one key"),
        ({"actions": {}}, "must be a list"),
        (_one({"target": "tools.a", "remove": True}), "needs a `target` object"),
        (_one({"target": {"tool": "a"}}), "exactly one"),
        (_one({"target": {"tool": "a"}, "append": "x", "remove": True}), "exactly one"),
        (_one({"target": {"tool": "a"}, "remove": True, "note": "x"}), "exactly one"),
        (_one({"target": {"tool": "a"}, "remove": False}), "must be true"),
        (_one({"target": {"tool": "a"}, "append": 3}), "needs text"),
        (_one({"target": {"tool": "a"}, "required": True}), "only a param"),
        (_one({"target": {"instructions": True}, "replace": "x"}), "only `append`"),
        (_one({"target": {"agent_text": "report_tool.description"}, "append": "x"}), "agent_text"),
        (_one({"target": {"server": "name"}, "append": "x"}), "unknown target"),
        (_one({"target": {"tool": ""}, "remove": True}), "cannot be empty"),
        (_one({"target": {"tool": "t", "param": ""}, "remove": True}), "cannot be empty"),
    ],
)
def test_a_bad_overlay_is_refused_at_load(doc: dict[str, Any], message: str):
    with pytest.raises(OverlayError, match=message):
        parse_overlay(doc)


def test_yaml_reads_the_same_overlay(tmp_path: Path):
    pytest.importorskip("yaml")
    path = tmp_path / "fix.yaml"
    path.write_text("actions:\n  - target: {tool: create_fields}\n    append: Get the id first.\n")
    assert (
        load_overlay(str(path)).hash
        == parse_overlay(
            {"actions": [{"target": {"tool": "create_fields"}, "append": "Get the id first."}]}
        ).hash
    )


def test_yaml_without_pyyaml_names_the_fix(tmp_path: Path, monkeypatch):
    path = tmp_path / "fix.yaml"
    path.write_text("actions: []\n")
    monkeypatch.setitem(__import__("sys").modules, "yaml", None)
    with pytest.raises(OverlayError, match=r"baton-proxy\[overlay\]"):
        load_overlay(str(path))


def test_a_paginated_listing_reports_unmatched_actions_on_its_last_page(caplog):
    overlay = {"actions": [{"target": {"tool": "nope"}, "append": "x"}]}
    processor, _ = _processor(overlay)
    pages = [
        ({}, {"tools": TOOLS[:1], "nextCursor": "c"}),
        ({"cursor": "c"}, {"tools": TOOLS[1:]}),
    ]
    with caplog.at_level(logging.ERROR, logger="baton_proxy"):
        for i, (params, result) in enumerate(pages, start=10):
            processor.handle_client_message(
                {"jsonrpc": "2.0", "id": i, "method": "tools/list", "params": params}
            )
            processor.handle_server_message({"jsonrpc": "2.0", "id": i, "result": result})
    assert [r for r in caplog.records if "did not apply" in r.getMessage()]


def test_a_tool_result_that_looks_like_a_listing_is_left_alone():
    processor, _ = _processor()
    _call(processor, "get_workspace", {"workspace_id": "w"}, req_id=7)
    body = {"tools": [{"name": "debug_dump"}], "content": []}
    out = processor.handle_server_message({"jsonrpc": "2.0", "id": 7, "result": body})
    assert "debug_dump" in [t["name"] for t in out["result"]["tools"]]


def test_a_degraded_initialize_still_gets_the_overlay_instructions():
    processor, _ = _processor()
    degraded = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x", "instructions": ""}}
    assert processor.overlay_degraded(degraded)["result"]["instructions"] == (
        "Write tools exist; use them."
    )


def test_a_bad_overlay_stops_startup_with_a_logged_reason(tmp_path: Path, monkeypatch, caplog):
    from baton_proxy.proxy import _bootstrap

    path = tmp_path / "o.json"
    path.write_text('{"actions": [{"target": {"tool": "a"}}]}')
    with caplog.at_level(logging.ERROR, logger="baton_proxy"), pytest.raises(SystemExit) as exc:
        _bootstrap(str(path))
    assert exc.value.code == 2
    assert any("--overlay refused" in r.getMessage() for r in caplog.records)


def test_baton_overlay_alone_loads_nothing_and_warns(tmp_path: Path, monkeypatch, caplog):
    from baton_proxy.proxy import _bootstrap

    path = tmp_path / "o.json"
    path.write_text(json.dumps(_one({"target": {"instructions": True}, "append": "X"})))
    monkeypatch.setenv("BATON_OVERLAY", str(path))
    monkeypatch.setenv("BATON_EVENT_SINK", "stderr:")
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        _, _, emitter, processor = _bootstrap()
    emitter.stop()
    assert processor.overlay is None
    assert any("BATON_OVERLAY is ignored" in r.getMessage() for r in caplog.records)


def test_the_overlay_holds_when_listing_telemetry_fails():
    processor, emitter = _processor()

    def broken(**_: Any) -> None:
        raise RuntimeError("emit dead")

    emitter.enqueue_tool_list_start = broken  # type: ignore[method-assign]
    names = [t["name"] for t in _list(processor, TOOLS)]
    assert "debug_dump" not in names


def test_two_sessions_sharing_an_overlay_log_its_unmatched_actions_once(caplog):
    overlay = parse_overlay({"actions": [{"target": {"tool": "nope"}, "append": "x"}]})
    injection = _Injection.create(None)
    sessions = [
        MessageProcessor(_FakeEmitter(), injection, sid, overlay=overlay)  # type: ignore[arg-type]
        for sid in ("a", "b")
    ]
    with caplog.at_level(logging.ERROR, logger="baton_proxy"):
        for processor in sessions:
            _list(processor, TOOLS)
    assert len([r for r in caplog.records if "did not apply" in r.getMessage()]) == 1


def test_a_param_required_but_not_described_is_never_dropped_from_calls():
    overlay = parse_overlay({"actions": [{"target": {"tool": "t", "param": "x"}, "remove": True}]})
    overlay.apply_to_tools([{"name": "t", "inputSchema": {"properties": {}, "required": ["x"]}}])
    assert overlay.hidden_params("t") == frozenset()


def test_a_float_listing_id_still_gets_the_overlay():
    processor, _ = _processor()
    names = [t["name"] for t in _list(processor, TOOLS, req_id=1.5)]  # type: ignore[arg-type]
    assert "debug_dump" not in names


def test_a_listing_answered_by_the_proxy_frees_its_id():
    processor, _ = _processor()
    processor.handle_client_message({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    processor.synthesize_pending_error(9, "proxy_upstream_unreachable", "down")
    assert processor._toollist_ids == {}


def test_a_server_request_sharing_a_listing_id_leaves_the_listing_overlaid():
    processor, _ = _processor()
    processor.handle_client_message({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
    processor.handle_server_message({"jsonrpc": "2.0", "id": 5, "method": "sampling/createMessage"})
    msg = {"jsonrpc": "2.0", "id": 5, "result": {"tools": json.loads(json.dumps(TOOLS))}}
    names = [t["name"] for t in processor.handle_server_message(msg)["result"]["tools"]]
    assert "debug_dump" not in names


def test_a_description_that_is_not_text_skips_only_that_edit():
    overlay = parse_overlay(
        {
            "actions": [
                {"target": {"tool": "a"}, "append": "More."},
                {"target": {"tool": "b"}, "remove": True},
            ]
        }
    )
    odd = {"name": "a", "description": {"text": "structured"}}
    tools = overlay.apply_to_tools([odd, {"name": "b"}])
    assert tools == [odd]
    assert overlay.unmatched() == ["actions[0] append a"]


def test_removing_a_required_param_is_refused_where_it_is_deleted():
    overlay = parse_overlay({"actions": [{"target": {"tool": "t", "param": "x"}, "remove": True}]})
    tool = {"name": "t", "inputSchema": {"properties": {"x": {}}, "required": ["x"]}}
    assert _apply_one(tool, overlay.actions[0]) is not None
    assert "x" in tool["inputSchema"]["properties"]


def test_a_replace_applies_over_a_description_that_is_not_text():
    overlay = parse_overlay({"actions": [{"target": {"tool": "a"}, "replace": "New."}]})
    (tool,) = overlay.apply_to_tools([{"name": "a", "description": {"text": "structured"}}])
    assert tool["description"] == "New."
    assert overlay.unmatched() == []


def test_the_first_listing_report_counts_tools_changed_not_actions():
    overlay = parse_overlay(
        {
            "actions": [
                {"target": {"tool": "a"}, "append": "One."},
                {"target": {"tool": "a"}, "append": "Two."},
                {"target": {"tool": "b"}, "remove": True},
                {"target": {"tool": "gone"}, "append": "Never listed."},
            ]
        }
    )
    overlay.apply_to_tools([{"name": "a", "description": "x"}, {"name": "b"}, {"name": "c"}])
    assert overlay.report_once() == (2, ["actions[3] append gone"])
    assert overlay.report_once() is None

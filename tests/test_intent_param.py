"""Per-tool goal-param injection (`user_goal`/`expected_result`) — unit + e2e
on both transports.

Covers the design contract:
schema injection at tools/list (skip-if-exists per param independently, never
on the proxy's own tools, required-mode appends only user_goal), the upserted
per-param registry (pagination / re-list safe), strip-before-forward
exactness at tools/call (upstream sees only vendor args), the once-per-session
proactive annotation synthesised from the FIRST param intent (carrying
expected_result too, if present; enqueued before its tool_call_start;
suppressed after a real annotate proactive), cold-registry strip-by-reserved-
name, and the BATON_INTENT_PARAM knob.

Param names + mechanics match baton-sdk's ``baton.integrations._llm_text`` /
``_tool_wrap.py`` (ported 2026-08-08 — this file used to test a single
namespaced `baton_intent` param, the SPEC §13 divergence closed alongside
wiring the ``baton-spec`` conformance submodule).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fixture_http_server  # noqa: E402

from baton_proxy.config import Config
from baton_proxy.proxy import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    USER_GOAL_PARAM_NAME,
    MessageProcessor,
    _inject_goal_params,
    _Injection,
)

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"


# --------------------------------------------------------------------------- #
# Unit — _inject_goal_params                                                   #
# --------------------------------------------------------------------------- #


def _tool(name: str = "t", schema: Any = "default") -> dict[str, Any]:
    t: dict[str, Any] = {"name": name, "description": "d"}
    if schema == "default":
        t["inputSchema"] = {"type": "object", "properties": {"x": {"type": "string"}}}
    elif schema is not None:
        t["inputSchema"] = schema
    return t


def test_inject_optional_adds_both_params_without_touching_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_goal_params(tool, "optional") == {
        USER_GOAL_PARAM_NAME: "injected",
        EXPECTED_RESULT_PARAM_NAME: "injected",
    }
    props = tool["inputSchema"]["properties"]
    assert props[USER_GOAL_PARAM_NAME]["type"] == "string"
    assert props[USER_GOAL_PARAM_NAME]["description"]
    assert props[EXPECTED_RESULT_PARAM_NAME]["type"] == "string"
    assert props[EXPECTED_RESULT_PARAM_NAME]["description"]
    assert tool["inputSchema"]["required"] == ["x"]


def test_inject_required_appends_only_user_goal_to_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_goal_params(tool, "required") == {
        USER_GOAL_PARAM_NAME: "injected",
        EXPECTED_RESULT_PARAM_NAME: "injected",
    }
    # Only user_goal is forced required — expected_result stays optional even
    # in "required" mode, matching baton-sdk's _inject_goal_params.
    assert tool["inputSchema"]["required"] == ["x", USER_GOAL_PARAM_NAME]


def test_inject_required_creates_required_list_when_absent() -> None:
    tool = _tool()
    _inject_goal_params(tool, "required")
    assert tool["inputSchema"]["required"] == [USER_GOAL_PARAM_NAME]


def test_inject_handles_schemaless_tool() -> None:
    tool = _tool(schema=None)
    _inject_goal_params(tool, "optional")
    assert USER_GOAL_PARAM_NAME in tool["inputSchema"]["properties"]
    assert EXPECTED_RESULT_PARAM_NAME in tool["inputSchema"]["properties"]


def test_inject_skips_native_param_untouched_independently() -> None:
    """A tool that already declares ONE of the two names keeps it untouched
    while the other still gets injected — dispositions are independent."""
    native_def = {"type": "string", "description": "the vendor's own"}
    tool = _tool(schema={"type": "object", "properties": {USER_GOAL_PARAM_NAME: native_def}})
    assert _inject_goal_params(tool, "optional") == {
        USER_GOAL_PARAM_NAME: "native",
        EXPECTED_RESULT_PARAM_NAME: "injected",
    }
    # Skip-if-exists means UNTOUCHED — same object, no description rewrite.
    assert tool["inputSchema"]["properties"][USER_GOAL_PARAM_NAME] is native_def
    assert EXPECTED_RESULT_PARAM_NAME in tool["inputSchema"]["properties"]


def test_inject_is_idempotent_across_relists() -> None:
    """Desktop lazily re-lists; the second pass must see its own injection as
    'native' for both params and not duplicate the required entry."""
    tool = _tool(schema={"type": "object", "properties": {}, "required": []})
    assert _inject_goal_params(tool, "required") == {
        USER_GOAL_PARAM_NAME: "injected",
        EXPECTED_RESULT_PARAM_NAME: "injected",
    }
    assert _inject_goal_params(tool, "required") == {
        USER_GOAL_PARAM_NAME: "native",
        EXPECTED_RESULT_PARAM_NAME: "native",
    }
    assert tool["inputSchema"]["required"].count(USER_GOAL_PARAM_NAME) == 1


def test_inject_rejects_non_tool_objects() -> None:
    assert _inject_goal_params("not a dict", "optional") == {}
    assert _inject_goal_params({"no": "name"}, "optional") == {}
    assert _inject_goal_params({"name": 42}, "optional") == {}


# --------------------------------------------------------------------------- #
# Unit — MessageProcessor registry + strip + annotation synthesis              #
# --------------------------------------------------------------------------- #


class _FakeEmitter:
    """Records enqueue calls in order; only the methods these paths use."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def enqueue_annotation(self, **kwargs: Any) -> None:
        self.calls.append(("annotation", kwargs))

    def enqueue_tool_call_start(self, **kwargs: Any) -> None:
        self.calls.append(("tool_call_start", kwargs))


def _processor(mode: str = "optional") -> tuple[MessageProcessor, _FakeEmitter]:
    emitter = _FakeEmitter()
    injection = _Injection.create(None, intent_param_mode=mode)
    return MessageProcessor(emitter, injection, "test-session"), emitter  # type: ignore[arg-type]


def _tools_list_response(tools: list[dict[str, Any]], msg_id: int = 2) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}


def _call(name: str, arguments: dict[str, Any], msg_id: int = 10) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


INTENT_TEXT = "User is testing intent capture end to end."
EXPECTED_TEXT = "A confirmation that the value was captured."


def test_registry_upserts_across_paginated_lists() -> None:
    proc, _ = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    proc.handle_server_message(_tools_list_response([_tool("beta")], msg_id=3))
    with proc._registry_lock:
        assert proc._param_registry == {
            "alpha": {USER_GOAL_PARAM_NAME: "injected", EXPECTED_RESULT_PARAM_NAME: "injected"},
            "beta": {USER_GOAL_PARAM_NAME: "injected", EXPECTED_RESULT_PARAM_NAME: "injected"},
        }


def test_registry_skips_proxy_own_tools() -> None:
    proc, _ = _processor()
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    # _inject_into_response appended the proxy's tools AFTER param injection —
    # they must carry no goal params and no registry entry.
    injected_names = {"baton_annotate", "baton_session_report"}
    for tool in out["result"]["tools"]:
        if tool["name"] in injected_names:
            assert USER_GOAL_PARAM_NAME not in tool["inputSchema"]["properties"]
            assert EXPECTED_RESULT_PARAM_NAME not in tool["inputSchema"]["properties"]
    with proc._registry_lock:
        assert not (injected_names & set(proc._param_registry))


def test_off_mode_injects_and_strips_nothing() -> None:
    proc, emitter = _processor(mode="off")
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    upstream = [t for t in out["result"]["tools"] if t["name"] == "alpha"][0]
    assert USER_GOAL_PARAM_NAME not in upstream["inputSchema"]["properties"]
    assert EXPECTED_RESULT_PARAM_NAME not in upstream["inputSchema"]["properties"]

    action = proc.handle_client_message(
        _call("alpha", {"x": "1", USER_GOAL_PARAM_NAME: INTENT_TEXT})
    )
    # Off means fully off: no strip, param forwards as-is.
    assert action.forward["params"]["arguments"][USER_GOAL_PARAM_NAME] == INTENT_TEXT
    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert starts[0][1]["call_intent"] is None


def test_strip_and_capture_with_annotation_first() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    action = proc.handle_client_message(
        _call(
            "alpha",
            {
                "x": "1",
                USER_GOAL_PARAM_NAME: INTENT_TEXT,
                EXPECTED_RESULT_PARAM_NAME: EXPECTED_TEXT,
            },
        )
    )

    # Upstream-bound arguments: both params gone, vendor args intact.
    assert action.forward["params"]["arguments"] == {"x": "1"}

    kinds = [k for k, _ in emitter.calls]
    assert kinds == ["annotation", "tool_call_start"], "annotation must precede the start"
    ann = emitter.calls[0][1]
    assert ann["intent"] == INTENT_TEXT
    assert ann["expected_outcome"] == EXPECTED_TEXT
    assert ann["signal_type"] is None
    assert ann["intent_source"] == INTENT_SOURCE_PARAM
    assert ann["tool_name"] == "alpha"
    start = emitter.calls[1][1]
    assert start["call_intent"] == INTENT_TEXT
    assert start["intent_source"] == INTENT_SOURCE_PARAM
    assert USER_GOAL_PARAM_NAME not in start["params"]
    assert EXPECTED_RESULT_PARAM_NAME not in start["params"]


def test_only_first_param_intent_becomes_annotation() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha"), _tool("beta")]))

    proc.handle_client_message(_call("alpha", {USER_GOAL_PARAM_NAME: "first goal"}, msg_id=10))
    proc.handle_client_message(_call("beta", {USER_GOAL_PARAM_NAME: "second goal"}, msg_id=11))

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0][1]["intent"] == "first goal"
    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert [s[1]["call_intent"] for s in starts] == ["first goal", "second goal"]


def test_real_annotate_proactive_suppresses_param_annotation() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    # A real proactive via the injected annotate tool claims the slot...
    proc.handle_client_message(_call("baton_annotate", {"intent": "the user's goal"}, msg_id=9))
    # ...so the param intent must NOT synthesise a second proactive.
    proc.handle_client_message(_call("alpha", {USER_GOAL_PARAM_NAME: INTENT_TEXT}, msg_id=10))

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0][1]["intent"] == "the user's goal"
    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert starts[0][1]["call_intent"] == INTENT_TEXT  # per-call capture continues


def test_reactive_annotate_does_not_claim_the_proactive_slot() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(
        _call(
            "baton_annotate",
            {"intent": "goal", "signal_type": "failure", "suggested_improvement": "s"},
            msg_id=9,
        )
    )
    proc.handle_client_message(_call("alpha", {USER_GOAL_PARAM_NAME: INTENT_TEXT}, msg_id=10))

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    # Reactive + the synthesised proactive: the reactive carried signal_type,
    # so the param intent still opens the session's proactive slot.
    assert len(annotations) == 2
    assert annotations[1][1]["intent"] == INTENT_TEXT


def test_native_param_forwards_untouched_and_captures_nothing() -> None:
    proc, emitter = _processor()
    native = _tool(
        "alpha",
        schema={"type": "object", "properties": {USER_GOAL_PARAM_NAME: {"type": "string"}}},
    )
    proc.handle_server_message(_tools_list_response([native]))

    action = proc.handle_client_message(_call("alpha", {USER_GOAL_PARAM_NAME: "vendor's value"}))
    assert action.forward["params"]["arguments"][USER_GOAL_PARAM_NAME] == "vendor's value"
    assert [k for k, _ in emitter.calls] == ["tool_call_start"]
    assert emitter.calls[0][1]["call_intent"] is None
    # The vendor's param is a REAL argument — it stays in captured params.
    assert emitter.calls[0][1]["params"][USER_GOAL_PARAM_NAME] == "vendor's value"


def test_cold_registry_strips_by_reserved_name() -> None:
    """No tools/list seen (proxy respawned mid-session): the reserved name
    makes strip-by-default safe."""
    proc, emitter = _processor()
    action = proc.handle_client_message(_call("never_listed", {USER_GOAL_PARAM_NAME: INTENT_TEXT}))
    assert USER_GOAL_PARAM_NAME not in action.forward["params"]["arguments"]
    ann = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(ann) == 1 and ann[0][1]["intent"] == INTENT_TEXT


def test_blank_param_value_strips_but_captures_nothing() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    action = proc.handle_client_message(_call("alpha", {"x": "1", USER_GOAL_PARAM_NAME: "  "}))
    assert action.forward["params"]["arguments"] == {"x": "1"}
    assert [k for k, _ in emitter.calls] == ["tool_call_start"]
    assert emitter.calls[0][1]["call_intent"] is None


# --------------------------------------------------------------------------- #
# Config knob                                                                  #
# --------------------------------------------------------------------------- #


def test_config_defaults_to_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BATON_VENDOR_ID", "v")
    monkeypatch.delenv("BATON_INTENT_PARAM", raising=False)
    assert Config.from_env().intent_param_mode == "optional"


@pytest.mark.parametrize("mode", ["optional", "required", "off"])
def test_config_accepts_valid_modes(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("BATON_VENDOR_ID", "v")
    monkeypatch.setenv("BATON_INTENT_PARAM", mode)
    assert Config.from_env().intent_param_mode == mode


def test_config_rejects_invalid_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BATON_VENDOR_ID", "v")
    monkeypatch.setenv("BATON_INTENT_PARAM", "always")
    with pytest.raises(ValueError, match="BATON_INTENT_PARAM"):
        Config.from_env()


# --------------------------------------------------------------------------- #
# E2E — both transports, shared request script + assertions                    #
# --------------------------------------------------------------------------- #

E2E_REQUESTS: list[dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "argkeys",
            "arguments": {
                "text": "x",
                USER_GOAL_PARAM_NAME: INTENT_TEXT,
                EXPECTED_RESULT_PARAM_NAME: EXPECTED_TEXT,
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "echo",
            "arguments": {"text": "hi", USER_GOAL_PARAM_NAME: "second call goal"},
        },
    },
]


def _parse_streams(stdout: str, stderr: str) -> tuple[dict[int, dict], list[dict]]:
    by_id: dict[int, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            by_id[msg["id"]] = msg
    events: list[dict] = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "event_type" in msg:
            events.append(msg)
    return by_id, events


def _run_stdio(env_extra: dict[str, str] | None = None) -> tuple[dict[int, dict], list[dict]]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "BATON_VENDOR_ID": "v",
            "BATON_EVENT_SINK": "stderr:",
        }
    )
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--", sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    input_data = "".join(json.dumps(req) + "\n" for req in E2E_REQUESTS)
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return _parse_streams(stdout, stderr)


def _run_http(url: str) -> tuple[dict[int, dict], list[dict]]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "BATON_VENDOR_ID": "v",
            "BATON_EVENT_SINK": "stderr:",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--url", url],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    input_data = "".join(json.dumps(req) + "\n" for req in E2E_REQUESTS)
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    return _parse_streams(stdout, stderr)


def _assert_intent_session(by_id: dict[int, dict], events: list[dict]) -> None:
    """Shared assertions — both transports must produce this exact contract."""
    # Injection: every upstream tool grew both params; the proxy's own didn't.
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    for name in ("echo", "boom", "argkeys"):
        assert USER_GOAL_PARAM_NAME in tools[name]["inputSchema"]["properties"], name
        assert EXPECTED_RESULT_PARAM_NAME in tools[name]["inputSchema"]["properties"], name
        # optional mode: required untouched
        assert USER_GOAL_PARAM_NAME not in (tools[name]["inputSchema"].get("required") or [])
    assert USER_GOAL_PARAM_NAME not in tools["baton_annotate"]["inputSchema"]["properties"]

    # Strip exactness: the upstream reports exactly which keys it received.
    assert by_id[3]["result"]["content"][0]["text"] == "keys: text"
    assert "Echo: hi" in by_id[4]["result"]["content"][0]["text"]

    # Events: one synthesised proactive (first call only), before its start,
    # carrying both intent and expected_outcome.
    annotations = [e for e in events if e["event_type"] == "annotation"]
    assert len(annotations) == 1
    ann = annotations[0]["payload"]
    assert ann["intent"] == INTENT_TEXT
    assert ann["expected_outcome"] == EXPECTED_TEXT
    assert ann["intent_source"] == INTENT_SOURCE_PARAM
    assert ann["tool_name"] == "argkeys"
    assert "signal_type" not in ann  # proactive

    starts = [e for e in events if e["event_type"] == "tool_call_start"]
    assert [s["payload"]["tool_name"] for s in starts] == ["argkeys", "echo"]
    assert [s["payload"]["call_intent"] for s in starts] == [INTENT_TEXT, "second call goal"]
    # The expectation rides the start event, per call. Call 2 sends only the
    # goal, so its key is ABSENT rather than null — "stated no expectation" and
    # "stated an empty one" must stay distinguishable downstream.
    assert starts[0]["payload"]["call_expected"] == EXPECTED_TEXT
    assert "call_expected" not in starts[1]["payload"]
    for s in starts:
        assert USER_GOAL_PARAM_NAME not in s["payload"]["params"]
        assert EXPECTED_RESULT_PARAM_NAME not in s["payload"]["params"]
    ann_seq = annotations[0]["sequence_number"]
    assert ann_seq < starts[0]["sequence_number"]


def test_intent_param_e2e_stdio() -> None:
    by_id, events = _run_stdio()
    _assert_intent_session(by_id, events)


def test_intent_param_e2e_http_bridge() -> None:
    httpd = fixture_http_server.serve(0)
    host, port = httpd.server_address[:2]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        by_id, events = _run_http(f"http://{host}:{port}/mcp")
    finally:
        httpd.shutdown()
        httpd.server_close()
    _assert_intent_session(by_id, events)


def test_intent_param_e2e_required_mode() -> None:
    by_id, _events = _run_stdio({"BATON_INTENT_PARAM": "required"})
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    assert USER_GOAL_PARAM_NAME in tools["echo"]["inputSchema"]["required"]
    # expected_result stays optional even in required mode.
    assert EXPECTED_RESULT_PARAM_NAME not in tools["echo"]["inputSchema"]["required"]
    # The annotate tool's required list is its own contract — untouched.
    assert tools["baton_annotate"]["inputSchema"]["required"] == ["intent"]


def test_intent_param_e2e_off_mode() -> None:
    by_id, events = _run_stdio({"BATON_INTENT_PARAM": "off"})
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    assert USER_GOAL_PARAM_NAME not in tools["echo"]["inputSchema"]["properties"]
    assert EXPECTED_RESULT_PARAM_NAME not in tools["echo"]["inputSchema"]["properties"]
    # Params forwarded untouched -> upstream reports them among its keys.
    expected_keys = ",".join(sorted([EXPECTED_RESULT_PARAM_NAME, "text", USER_GOAL_PARAM_NAME]))
    assert by_id[3]["result"]["content"][0]["text"] == f"keys: {expected_keys}"
    assert not [e for e in events if e["event_type"] == "annotation"]


def test_expectation_rides_every_start_not_just_the_first() -> None:
    """``expected_result`` is injected on EVERY tool, so its value must reach a
    consumer on every call — not only the one that opened the session.

    The once-per-session gate governs proactive ANNOTATIONS (a per-call
    proactive would open one downstream turn per tool call). It must not
    govern the values: the second call's expectation belongs to the second
    call, and the session's opening call is frequently a read that states no
    expectation at all, so lifting its value would attach the wrong one — or
    none — to everything that followed.
    """
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha"), _tool("beta")]))

    proc.handle_client_message(
        _call(
            "alpha",
            {USER_GOAL_PARAM_NAME: "first goal", EXPECTED_RESULT_PARAM_NAME: "first expectation"},
            msg_id=10,
        )
    )
    proc.handle_client_message(
        _call(
            "beta",
            {USER_GOAL_PARAM_NAME: "second goal", EXPECTED_RESULT_PARAM_NAME: "second expectation"},
            msg_id=11,
        )
    )

    # One annotation, as before — this change does not touch that gate.
    assert len([c for c in emitter.calls if c[0] == "annotation"]) == 1
    starts = [c[1] for c in emitter.calls if c[0] == "tool_call_start"]
    assert [s["call_expected"] for s in starts] == ["first expectation", "second expectation"]
    # And the params still reach the vendor clean.
    assert all(EXPECTED_RESULT_PARAM_NAME not in s["params"] for s in starts)


def test_expectation_alone_still_records_its_provenance() -> None:
    """Either param may be filled without the other. An expectation arriving
    with no goal is still injected-param capture, so ``intent_source`` must be
    stamped — matching what baton-sdk already does (keyed on any injected
    param, not on the goal alone)."""
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    proc.handle_client_message(
        _call("alpha", {EXPECTED_RESULT_PARAM_NAME: "just the expectation"}, msg_id=10)
    )

    start = [c[1] for c in emitter.calls if c[0] == "tool_call_start"][0]
    assert start["call_expected"] == "just the expectation"
    assert start["call_intent"] is None
    assert start["intent_source"] == INTENT_SOURCE_PARAM
    # No goal means no proactive annotation — that gate is intent-keyed.
    assert [c for c in emitter.calls if c[0] == "annotation"] == []


def test_the_agent_facing_task_label_lands_on_the_wire_key() -> None:
    """The annotation tool's param is ``overall_task``; the event key is
    ``workflow``.

    The two names are deliberately different, and the seam between them is one
    line in ``proxy.py``. It mirrors the injected params (``overall_task`` ->
    ``call_workflow``): "task" is what the agent is told, ``workflow`` is what
    rung 3b groups on. Collapsing them in either direction is a silent data
    loss — pointing the parser back at ``workflow`` drops every label an agent
    files under the name the schema actually advertises, and it would drop it
    quietly, because a missing task label reads downstream as "this agent never
    supplied one" rather than as an error.
    """
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(
        _call(
            "baton_annotate",
            {"intent": "the user's goal", "overall_task": "morning meeting prep"},
            msg_id=9,
        )
    )

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0][1]["workflow"] == "morning meeting prep"
    assert "overall_task" not in annotations[0][1]


def test_the_retired_param_name_is_not_still_accepted() -> None:
    """``workflow`` was the agent-facing name and is no longer offered.

    Worth pinning rather than assuming: the proxy serves the tool schema and
    parses the call in one process, so no agent can be holding a stale schema
    and there is no skew to absorb. Quietly accepting the old name anyway would
    leave two spellings live with only one of them documented, which is how the
    next reader concludes the rename never happened.
    """
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(
        _call(
            "baton_annotate",
            {"intent": "the user's goal", "workflow": "morning meeting prep"},
            msg_id=9,
        )
    )

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0][1]["workflow"] is None

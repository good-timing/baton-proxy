"""Per-tool goal-param injection (`user_goal`) — unit + e2e on both
transports.

Covers the design contract:
schema injection at tools/list (skip-if-exists, never on the proxy's own
tools, required-mode appends user_goal), a stale client's retired names
stripped and recorded, the upserted
per-param registry (pagination / re-list safe), strip-before-forward
exactness at tools/call (upstream sees only vendor args), no annotation built
from an injected intent (SPEC §11.4: the intent rides only its
tool_call_start), cold-registry strip-by-reserved-name, and the
BATON_INTENT_PARAM knob.

Param names + mechanics match baton-sdk's ``baton.integrations._llm_text`` /
``_tool_wrap.py``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fixture_http_server  # noqa: E402

from baton_proxy._llm_text import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_INSTRUCTIONS,
    OVERALL_TASK_PARAM_NAME,
    build_user_goal_param_description,
)
from baton_proxy.config import (
    _INTENT_PARAM_MODES,
    DEFAULT_INTENT_PARAM_MODE,
    DEFAULT_PROACTIVE_MODE,
    Config,
)
from baton_proxy.proxy import (
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


def test_inject_optional_adds_user_goal_without_touching_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_goal_params(tool, "optional") == {USER_GOAL_PARAM_NAME: "injected"}
    props = tool["inputSchema"]["properties"]
    assert props[USER_GOAL_PARAM_NAME]["type"] == "string"
    assert props[USER_GOAL_PARAM_NAME]["description"]
    assert set(props) == {USER_GOAL_PARAM_NAME}
    assert tool["inputSchema"]["required"] == ["x"]


def test_inject_required_appends_user_goal_to_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_goal_params(tool, "required") == {USER_GOAL_PARAM_NAME: "injected"}
    assert tool["inputSchema"]["required"] == ["x", USER_GOAL_PARAM_NAME]


def test_inject_records_a_retired_name_the_tool_declares_as_native() -> None:
    tool = _tool(
        schema={"type": "object", "properties": {EXPECTED_RESULT_PARAM_NAME: {"type": "string"}}}
    )
    assert _inject_goal_params(tool, "required") == {
        EXPECTED_RESULT_PARAM_NAME: "native",
        USER_GOAL_PARAM_NAME: "injected",
    }
    assert tool["inputSchema"]["required"] == [USER_GOAL_PARAM_NAME]


@pytest.mark.parametrize(
    ("mode", "expected_lead"), [("optional", "OPTIONAL."), ("required", "REQUIRED.")]
)
def test_inject_description_label_tracks_the_mode(mode: str, expected_lead: str) -> None:
    """Under ``required`` the injector appends ``user_goal`` to the tool's
    ``required`` list, so a description still opening "OPTIONAL." would
    contradict the schema it ships inside. The ``optional`` leg is the control
    that proves the mode moves the label, not the injection."""
    tool = _tool()
    _inject_goal_params(tool, mode)
    desc = tool["inputSchema"]["properties"][USER_GOAL_PARAM_NAME]["description"]
    assert desc.startswith(expected_lead), f"{mode} mode: {desc[:12]!r}"
    assert desc == build_user_goal_param_description(intent_param_mode=mode)
    # Only the label moved — the measured sentence after it is identical.
    assert desc[len(expected_lead) :] == build_user_goal_param_description()[len("OPTIONAL.") :]


def test_inject_required_creates_required_list_when_absent() -> None:
    tool = _tool()
    _inject_goal_params(tool, "required")
    assert tool["inputSchema"]["required"] == [USER_GOAL_PARAM_NAME]


def test_inject_handles_schemaless_tool() -> None:
    tool = _tool(schema=None)
    _inject_goal_params(tool, "optional")
    assert USER_GOAL_PARAM_NAME in tool["inputSchema"]["properties"]


def test_inject_skips_a_native_user_goal_untouched() -> None:
    native_def = {"type": "string", "description": "the vendor's own"}
    tool = _tool(schema={"type": "object", "properties": {USER_GOAL_PARAM_NAME: native_def}})
    assert _inject_goal_params(tool, "required") == {USER_GOAL_PARAM_NAME: "native"}
    # Skip-if-exists means UNTOUCHED — same object, no description rewrite.
    assert tool["inputSchema"]["properties"][USER_GOAL_PARAM_NAME] is native_def
    assert "required" not in tool["inputSchema"]


def test_inject_is_idempotent_across_relists() -> None:
    """Desktop lazily re-lists; the second pass must see its own injection as
    'native' and not duplicate the required entry."""
    tool = _tool(schema={"type": "object", "properties": {}, "required": []})
    assert _inject_goal_params(tool, "required") == {USER_GOAL_PARAM_NAME: "injected"}
    assert _inject_goal_params(tool, "required") == {USER_GOAL_PARAM_NAME: "native"}
    assert tool["inputSchema"]["required"].count(USER_GOAL_PARAM_NAME) == 1


def test_inject_rejects_non_tool_objects() -> None:
    assert _inject_goal_params("not a dict", "optional") == {}
    assert _inject_goal_params({"no": "name"}, "optional") == {}
    assert _inject_goal_params({"name": 42}, "optional") == {}


# --------------------------------------------------------------------------- #
# Unit — MessageProcessor registry + strip + annotate tool                    #
# --------------------------------------------------------------------------- #


class _FakeEmitter:
    """Records enqueue calls in order; only the methods these paths use."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def enqueue_annotation(self, **kwargs: Any) -> None:
        self.calls.append(("annotation", kwargs))

    def enqueue_tool_call_start(self, **kwargs: Any) -> None:
        self.calls.append(("tool_call_start", kwargs))


def _processor(
    mode: str = "optional", proactive: str = DEFAULT_PROACTIVE_MODE, report: str = "off"
) -> tuple[MessageProcessor, _FakeEmitter]:
    """``proactive`` and ``report`` default to the shipped defaults so most
    tests exercise what an operator actually runs. Tests that call
    ``baton_annotate`` ask for ``report="on"``, and those that drive an
    AGENT-authored pre-call annotation also for ``proactive="on"``."""
    emitter = _FakeEmitter()
    injection = _Injection.create(
        None, intent_param_mode=mode, proactive_mode=proactive, report_mode=report
    )
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
            "alpha": {USER_GOAL_PARAM_NAME: "injected"},
            "beta": {USER_GOAL_PARAM_NAME: "injected"},
        }


def test_registry_skips_proxy_own_tools() -> None:
    """The proxy's own tools are never injected into.

    Asserted through the REGISTRY, not through param names. The annotation tool
    natively declares `user_goal` — the same name injection adds — so "does
    this tool have a goal param" does not distinguish a tool that was injected
    into from one that always had it, and a name-absence assertion would pass
    for the wrong reason if injection DID run over it.

    The registry is the property that still separates them, and it is the one
    that matters: an entry is what makes `_extract_goal_params` strip those
    names from a call. An annotation tool in the registry would have its own
    arguments stripped out from under it — the agent's goal text silently
    removed from the very call that exists to record it.
    """
    proc, _ = _processor(report="on")
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    own = {"baton_annotate"}

    served = {t["name"] for t in out["result"]["tools"]}
    # `baton_annotate` is served under BATON_ANNOTATE=on, and its presence is
    # what makes the registry assertion below meaningful. It is the only tool the proxy adds
    # since the report tool was retired (2026-09-27); `own` is kept as a SET
    # rather than collapsed to the one name, because the property under test is
    # "no tool of OURS enters the registry" and a second one would have to be
    # added here to be checked.
    assert "baton_annotate" in served

    with proc._registry_lock:
        registry = set(proc._param_registry)
    assert not (own & registry)
    # ...and the upstream tool IS in it, so the assertion above is not passing
    # because injection did nothing at all.
    assert "alpha" in registry


def test_off_mode_injects_and_strips_nothing() -> None:
    proc, emitter = _processor(mode="off")
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    upstream = [t for t in out["result"]["tools"] if t["name"] == "alpha"][0]
    assert USER_GOAL_PARAM_NAME not in upstream["inputSchema"]["properties"]

    action = proc.handle_client_message(
        _call("alpha", {"x": "1", USER_GOAL_PARAM_NAME: INTENT_TEXT})
    )
    # Off means fully off: no strip, param forwards as-is.
    assert action.forward["params"]["arguments"][USER_GOAL_PARAM_NAME] == INTENT_TEXT
    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert starts[0][1]["call_intent"] is None


def test_strip_and_capture_sends_no_annotation() -> None:
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

    # Upstream-bound arguments: user_goal and a stale client's retired
    # expected_result both gone, vendor args intact.
    assert action.forward["params"]["arguments"] == {"x": "1"}

    assert [k for k, _ in emitter.calls] == ["tool_call_start"]
    start = emitter.calls[0][1]
    assert start["call_intent"] == INTENT_TEXT
    assert start["call_expected"] == EXPECTED_TEXT
    assert start["intent_source"] == INTENT_SOURCE_PARAM
    assert USER_GOAL_PARAM_NAME not in start["params"]
    assert EXPECTED_RESULT_PARAM_NAME not in start["params"]


@pytest.mark.parametrize("proactive", ["on", "off"])
def test_an_injected_intent_sends_no_annotation(proactive: str) -> None:
    proc, emitter = _processor(proactive=proactive, report="on")
    proc.handle_server_message(_tools_list_response([_tool("alpha"), _tool("beta")]))

    for msg_id, tool, goal in ((10, "alpha", "first goal"), (11, "beta", "second goal")):
        proc.handle_client_message(
            _call(
                tool,
                {
                    USER_GOAL_PARAM_NAME: goal,
                    EXPECTED_RESULT_PARAM_NAME: EXPECTED_TEXT,
                    OVERALL_TASK_PARAM_NAME: "one task",
                },
                msg_id=msg_id,
            )
        )

    assert [k for k, _ in emitter.calls] == ["tool_call_start", "tool_call_start"]
    starts = [c[1] for c in emitter.calls]
    assert [s["call_intent"] for s in starts] == ["first goal", "second goal"]
    assert all(s["intent_source"] == INTENT_SOURCE_PARAM for s in starts)


def test_an_agent_note_is_the_only_annotation_in_the_session() -> None:
    proc, emitter = _processor(proactive="on", report="on")
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(_call("alpha", {USER_GOAL_PARAM_NAME: INTENT_TEXT}, msg_id=9))
    proc.handle_client_message(_call("baton_annotate", {"user_goal": "the user's goal"}, msg_id=10))

    annotations = [c[1] for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0]["intent"] == "the user's goal"
    assert "intent_source" not in annotations[0]
    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert starts[0][1]["call_intent"] == INTENT_TEXT


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
    """No tools/list seen (proxy respawned mid-session): a stale client's
    retired names are stripped too, since a strict upstream refuses them."""
    proc, emitter = _processor()
    action = proc.handle_client_message(
        _call(
            "never_listed",
            {"x": "1", USER_GOAL_PARAM_NAME: INTENT_TEXT, OVERALL_TASK_PARAM_NAME: "2: t"},
        )
    )
    assert action.forward["params"]["arguments"] == {"x": "1"}
    assert [k for k, _ in emitter.calls] == ["tool_call_start"]
    assert emitter.calls[0][1]["call_intent"] == INTENT_TEXT
    assert emitter.calls[0][1]["call_workflow"] == "2: t"


def test_a_stale_clients_retired_names_are_stripped_and_recorded() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    action = proc.handle_client_message(
        _call(
            "alpha",
            {"x": "1", EXPECTED_RESULT_PARAM_NAME: EXPECTED_TEXT, OVERALL_TASK_PARAM_NAME: "2: t"},
        )
    )
    assert action.forward["params"]["arguments"] == {"x": "1"}
    start = emitter.calls[0][1]
    assert (start["call_expected"], start["call_workflow"]) == (EXPECTED_TEXT, "2: t")
    assert start["intent_source"] == INTENT_SOURCE_PARAM


def test_a_retired_name_the_tool_declares_is_forwarded_and_not_recorded() -> None:
    proc, emitter = _processor()
    own = _tool(
        "alpha",
        schema={"type": "object", "properties": {OVERALL_TASK_PARAM_NAME: {"type": "string"}}},
    )
    proc.handle_server_message(_tools_list_response([own]))
    action = proc.handle_client_message(_call("alpha", {OVERALL_TASK_PARAM_NAME: "vendor's"}))
    assert action.forward["params"]["arguments"] == {OVERALL_TASK_PARAM_NAME: "vendor's"}
    assert emitter.calls[0][1]["call_workflow"] is None


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [(None, INTENT_INSTRUCTIONS), ("Vendor text.", f"Vendor text.\n\n{INTENT_INSTRUCTIONS}")],
)
def test_reports_off_instructions_are_the_sentence_below_the_vendors(
    upstream: str | None, expected: str
) -> None:
    proc, _ = _processor()
    result: dict[str, Any] = {"protocolVersion": "2025-03-26", "serverInfo": {"name": "s"}}
    if upstream is not None:
        result["instructions"] = upstream
    out = proc.handle_server_message({"jsonrpc": "2.0", "id": 1, "result": result})
    assert out["result"]["instructions"] == expected


def test_reports_off_serves_no_annotate_tool_and_forwards_a_call_to_it() -> None:
    proc, emitter = _processor()
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    assert [t["name"] for t in out["result"]["tools"]] == ["alpha"]

    action = proc.handle_client_message(
        _call("baton_annotate", {"user_goal": "g", "what_happened": "it failed"})
    )
    assert action.respond is None
    assert action.forward is not None
    assert [k for k, _ in emitter.calls] == ["tool_call_start"]


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


def test_config_defaults_to_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flipped 2026-09-01 (D7). Safe because `required` here is an
    ADVERTISEMENT: nothing validates it and the param is stripped before the
    call is forwarded, so a call that omits `user_goal` is served exactly as
    it was. See `test_required_is_advertised_and_never_enforced` below."""
    monkeypatch.setenv("BATON_VENDOR_ID", "v")
    monkeypatch.delenv("BATON_INTENT_PARAM", raising=False)
    assert Config.from_env().intent_param_mode == "required"


def test_required_is_advertised_and_never_enforced() -> None:
    """`required` appends `user_goal` to the schema's advertised list and
    validates NOTHING — a wrapper that refused a customer's call to collect a
    telemetry string would be changing how the wrapped server behaves, which is
    the one thing it promises not to do.

    Both halves asserted: the schema says required, AND the omitting call is
    served and forwarded untouched."""
    proc, emitter = _processor("required")
    tool = _tool("alpha")
    proc.handle_server_message(_tools_list_response([tool]))
    assert tool["inputSchema"]["required"] == [USER_GOAL_PARAM_NAME]

    action = proc.handle_client_message(_call("alpha", {"text": "hi"}))
    assert action.respond is None, "an omitting call must not be answered by us"
    assert action.forward is not None, "an omitting call must reach the vendor's server"
    assert action.forward["params"]["arguments"] == {"text": "hi"}
    kinds = [kind for kind, _ in emitter.calls]
    assert kinds == ["tool_call_start"], kinds
    assert emitter.calls[0][1]["call_intent"] is None


@pytest.mark.parametrize("mode", ["optional", "required"])
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
            "arguments": {"text": "x", USER_GOAL_PARAM_NAME: INTENT_TEXT},
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
    # Added 2026-09-01 with the `required` default: a call that OMITS
    # `user_goal` entirely, against the real proxy over the real transport.
    # This is the assertion the flip rests on — `required` is an
    # advertisement, so this call must be served, not refused.
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "argkeys", "arguments": {"text": "no goal here"}},
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
    # The default mode reaches the instructions the client receives.
    assert "When you hand work to a subagent" in by_id[1]["result"]["instructions"]
    # Injection: every upstream tool grew `user_goal`, and nothing else.
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    for name in ("echo", "boom", "softfail", "argkeys"):
        assert USER_GOAL_PARAM_NAME in tools[name]["inputSchema"]["properties"], name
        assert EXPECTED_RESULT_PARAM_NAME not in tools[name]["inputSchema"]["properties"], name
        # The default is `required`: the ADVERTISED list gains it. The
        # vendor's own required entries are left alone.
        advertised = tools[name]["inputSchema"].get("required") or []
        assert USER_GOAL_PARAM_NAME in advertised, name
    # `echo` is the fixture tool that declares a required field of its own
    # (`argkeys` ships `required: []`), so it is the one that can catch a
    # replace-instead-of-append.
    assert "text" in (tools["echo"]["inputSchema"].get("required") or []), (
        "appending to the advertised list clobbered the vendor's own required field"
    )
    # BATON_ANNOTATE defaults to off, so the proxy adds no tool of its own.
    assert "baton_annotate" not in tools

    # Strip exactness: the upstream reports exactly which keys it received.
    assert by_id[3]["result"]["content"][0]["text"] == "keys: text"
    # ...and the omitting call. Advertised required, and SERVED anyway — no
    # error, no refusal, the vendor's own argument arriving alone. This is the
    # whole meaning of `required` in this producer, checked end to end rather
    # than at the injection function.
    assert "error" not in by_id[5], f"an omitting call was refused: {by_id[5]}"
    assert by_id[5]["result"]["content"][0]["text"] == "keys: text"
    assert "Echo: hi" in by_id[4]["result"]["content"][0]["text"]

    assert [e for e in events if e["event_type"] == "annotation"] == []

    starts = [e for e in events if e["event_type"] == "tool_call_start"]
    # Three calls now: the third omits `user_goal` under a `required` default
    # and is served anyway, so it produces a start event like any other — with
    # no intent on it. A capture that simply lost the call would be the far
    # worse outcome and would look identical in the fill-rate number.
    assert [s["payload"]["tool_name"] for s in starts] == ["argkeys", "echo", "argkeys"]
    assert [s["payload"].get("call_intent") for s in starts] == [
        INTENT_TEXT,
        "second call goal",
        None,
    ]
    # No call sends a retired name, so the key is ABSENT rather than null —
    # "stated no expectation" and "stated an empty one" must stay
    # distinguishable downstream.
    assert all("call_expected" not in s["payload"] for s in starts)
    for s in starts:
        assert USER_GOAL_PARAM_NAME not in s["payload"]["params"]


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
    by_id, _events = _run_stdio({"BATON_INTENT_PARAM": "required", "BATON_ANNOTATE": "on"})
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    assert USER_GOAL_PARAM_NAME in tools["echo"]["inputSchema"]["required"]
    # The annotate tool's required list is its own contract — untouched.
    assert tools["baton_annotate"]["inputSchema"]["required"] == ["user_goal", "tool_name"]


def test_intent_param_off_is_retired_and_cannot_stop_the_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-01: the params ALWAYS ride. They are the intent channel, and they
    are stripped before the call is forwarded, so the vendor's server cannot
    tell either way — the way to stop the injection is to stop wrapping.

    Coerced with a warning rather than refused, on purpose. `off` was
    DOCUMENTED in `try/SECURITY.md` as the way to disable injection, so the
    people most likely to set it are reviewers following an older copy of our
    own security page; raising would stop their MCP server from starting
    because they did what we told them to."""

    monkeypatch.setenv("BATON_VENDOR_ID", "v")
    monkeypatch.setenv("BATON_INTENT_PARAM", "off")
    with pytest.raises(ValueError):
        # The control: a value that was never valid still raises, so the
        # coercion below is a retirement and not a general softening.
        monkeypatch.setenv("BATON_INTENT_PARAM", "nope")
        Config.from_env()
    monkeypatch.setenv("BATON_INTENT_PARAM", "off")
    cfg = Config.from_env()
    assert cfg.intent_param_mode == DEFAULT_INTENT_PARAM_MODE
    assert "off" not in _INTENT_PARAM_MODES


def test_a_stale_expectation_rides_its_own_start_event() -> None:
    """A stale client's ``expected_result`` lands on that call's own start
    event, not only the first."""
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

    starts = [c[1] for c in emitter.calls if c[0] == "tool_call_start"]
    assert [s["call_expected"] for s in starts] == ["first expectation", "second expectation"]
    # And the params still reach the vendor clean.
    assert all(EXPECTED_RESULT_PARAM_NAME not in s["params"] for s in starts)


def test_expectation_alone_still_records_its_provenance() -> None:
    """A stale client's expectation arriving with no goal is still
    injected-param capture, so ``intent_source`` must be stamped — matching
    baton-sdk, which keys on any of the three values."""
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    proc.handle_client_message(
        _call("alpha", {EXPECTED_RESULT_PARAM_NAME: "just the expectation"}, msg_id=10)
    )

    start = [c[1] for c in emitter.calls if c[0] == "tool_call_start"][0]
    assert start["call_expected"] == "just the expectation"
    assert start["call_intent"] is None
    assert start["intent_source"] == INTENT_SOURCE_PARAM


@pytest.mark.parametrize(
    "retired", [{"expected_result": "one PDF"}, {"overall_task": "2: prep"}, {"workflow": "prep"}]
)
def test_the_annotation_records_no_retired_field(retired: dict[str, str]) -> None:
    """The annotate tool takes only `user_goal` of the intent fields. A stale
    client's report that still sends a retired one is taken, and the field is
    dropped."""
    proc, emitter = _processor(proactive="on", report="on")
    proc.handle_client_message(_call("baton_annotate", {"user_goal": "the goal", **retired}))

    annotations = [c[1] for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0]["intent"] == "the goal"
    assert annotations[0].get("expected_outcome") is None
    assert annotations[0].get("workflow") is None


def test_a_stale_task_label_rides_its_start_event() -> None:
    """A stale client's ``overall_task`` carries the turn number the Console
    falls back to, so it lands as ``call_workflow``. The upstream must never
    see it, or the vendor's own handler receives an argument its schema never
    declared."""
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    call = _call(
        "alpha",
        {
            USER_GOAL_PARAM_NAME: INTENT_TEXT,
            OVERALL_TASK_PARAM_NAME: "prepare campaign approval",
            "real_arg": "kept",
        },
        msg_id=10,
    )
    proc.handle_client_message(call)

    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert len(starts) == 1
    assert starts[0][1]["call_workflow"] == "prepare campaign approval"
    # The vendor sees its own arguments and nothing else.
    assert call["params"]["arguments"] == {"real_arg": "kept"}


def test_the_label_repeats_across_calls() -> None:
    """The label is only useful if it is the SAME string on each call of a
    task — that exact-string continuity is what the consumer groups on."""
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    for msg_id in (10, 11):
        proc.handle_client_message(
            _call(
                "alpha",
                {USER_GOAL_PARAM_NAME: INTENT_TEXT, OVERALL_TASK_PARAM_NAME: "one task"},
                msg_id=msg_id,
            )
        )

    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert [s[1]["call_workflow"] for s in starts] == ["one task", "one task"]


def test_a_call_without_the_label_passes_none_to_the_emitter() -> None:
    """No label supplied means the emitter is handed ``None``.

    The emitter is what turns that into an ABSENT wire key rather than a null
    one (pinned in ``test_emitter``); the distinction matters because a
    consumer grouping by exact string would otherwise see every label-less call
    share one null "group".
    """
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    proc.handle_client_message(_call("alpha", {USER_GOAL_PARAM_NAME: INTENT_TEXT}, msg_id=10))

    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert starts[0][1]["call_workflow"] is None


# --------------------------------------------------------------------------- #
# proactive_mode — the knob ported from the SDK (SPEC §14                      #
# "proactive_mode parity").                                                    #
#                                                                              #
# It governs TWO legs and not a third. On/off changes the annotation tool's    #
# description lead and whether the handler accepts an agent-filed pre-call     #
# annotation. It does NOT govern the instructions' BEFORE paragraph, which D7  #
# drops in both modes — the proxy's `on` therefore differs from the SDK's `on` #
# on purpose, and that divergence is what these tests hold still.              #
# --------------------------------------------------------------------------- #


def _proactive_processor(mode: str) -> tuple[MessageProcessor, _FakeEmitter]:
    emitter = _FakeEmitter()
    injection = _Injection.create(None, proactive_mode=mode)
    return MessageProcessor(emitter, injection, "test-session"), emitter  # type: ignore[arg-type]


def _annotate(arguments: dict[str, Any], msg_id: int = 20) -> dict[str, Any]:
    return _call("baton_annotate", arguments, msg_id=msg_id)


def test_proactive_off_refuses_an_agent_filed_pre_call_annotation() -> None:
    """The SDK refuses at the handler rather than by instruction text alone,
    because text is only a request."""
    proc, emitter = _proactive_processor("off")
    action = proc.handle_client_message(_annotate({"user_goal": INTENT_TEXT}))

    assert action.respond is not None, "a refused proactive must still be answered"
    assert action.forward is None, "the annotation tool is never forwarded upstream"
    text = action.respond["result"]["content"][0]["text"]
    # The same text as baton-sdk and baton-ts, word for word.
    assert text == (
        "baton_annotate is reactive-only on this server. Call it only AFTER a "
        "tool call returns an unhelpful, empty, failed or contradictory result, "
        "or when no tool covers what the user asked for. What the user is "
        "trying to do is already recorded on each tool call, so no pre-call "
        "annotation is needed — nothing was lost. If a tool call really went "
        "wrong, or no tool covers the request, call this again and say "
        "what_happened. If neither is true, do NOT re-send: that would file a "
        "report for a call that did not go wrong."
    )
    assert "signal_type" not in text
    # NO annotation event.
    assert emitter.calls == []


@pytest.mark.parametrize("account", ["", "   ", None, 7])
def test_proactive_off_refuses_a_blank_account_rather_than_recording_it(account: Any) -> None:
    proc, emitter = _proactive_processor("off")
    action = proc.handle_client_message(
        _annotate({"user_goal": INTENT_TEXT, "what_happened": account})
    )

    assert action.respond is not None
    assert action.forward is None
    assert "reactive-only" in action.respond["result"]["content"][0]["text"]
    assert emitter.calls == [], "a blank what_happened must not become an annotation"


def test_a_category_word_alone_is_not_a_report() -> None:
    """An agent holding an older tool listing may still send ``signal_type``.
    It is not what makes a report, and it is never passed on."""
    proc, emitter = _proactive_processor("off")
    refused = proc.handle_client_message(
        _annotate({"user_goal": INTENT_TEXT, "signal_type": "failure"})
    )
    assert refused.respond is not None
    assert "reactive-only" in refused.respond["result"]["content"][0]["text"]
    assert emitter.calls == []

    proc.handle_client_message(
        _annotate({"user_goal": INTENT_TEXT, "signal_type": "failure", "what_happened": "it 500d"})
    )
    assert "signal_type" not in emitter.calls[0][1]


def test_proactive_off_still_takes_the_report() -> None:
    """`off` costs the agent's pre-call narration and nothing else."""
    proc, emitter = _proactive_processor("off")
    action = proc.handle_client_message(
        _annotate(
            {
                "user_goal": INTENT_TEXT,
                "what_happened": "nothing removes an item",
                "tool_name": "none",
            }
        )
    )

    assert action.respond is not None
    assert "reactive-only" not in action.respond["result"]["content"][0]["text"]
    assert [kind for kind, _ in emitter.calls] == ["annotation"]
    assert emitter.calls[0][1]["what_happened"] == "nothing removes an item"
    assert emitter.calls[0][1]["tool_name"] == "none"


@pytest.mark.parametrize(
    ("sent", "recorded"),
    [
        ({"tool_name": "search"}, "search"),
        ({"tool_name": ""}, ""),
        ({}, None),
        ({"tool_name": 7}, None),
    ],
)
def test_the_reported_tool_is_recorded_as_given(sent: dict[str, Any], recorded: Any) -> None:
    """``""`` says "no tool exists" and must not turn into "not stated"."""
    proc, emitter = _proactive_processor("off")
    proc.handle_client_message(
        _annotate({"user_goal": INTENT_TEXT, "what_happened": "it failed", **sent})
    )

    assert emitter.calls[0][1]["tool_name"] == recorded


def test_proactive_on_accepts_the_agents_pre_call_annotation() -> None:
    """`on` is not the default, but it is a supported mode, and it is the one an operator picks
    to keep proactive turn boundaries until the `call_workflow` correlator
    lands."""
    from baton_proxy.config import DEFAULT_PROACTIVE_MODE

    assert DEFAULT_PROACTIVE_MODE == "off"
    proc, emitter = _proactive_processor("on")
    action = proc.handle_client_message(_annotate({"user_goal": INTENT_TEXT}))
    assert action.respond is not None
    assert "reactive-only" not in action.respond["result"]["content"][0]["text"]
    assert [kind for kind, _ in emitter.calls] == ["annotation"]


def test_the_tool_description_changes_with_the_mode_but_the_fields_do_not() -> None:
    """The other leg. `off` reframes the tool as reactive-only; both modes
    list the same fields, because the fields themselves do not change — only
    whether the agent may open with them."""
    from baton_proxy.proxy import _build_injected_tool

    on = _build_injected_tool("baton_annotate", "on")["description"]
    off = _build_injected_tool("baton_annotate", "off")["description"]

    assert "Populate proactively" in on
    assert "Do not call it before a tool call" in off
    assert "Populate proactively" not in off
    for field in ("user_goal", "what_happened", "tool_name"):
        assert field in on and field in off
    for retired in ("expected_result", "overall_task"):
        assert retired not in on and retired not in off
    # `user_goal` stays required in BOTH: a friction report still has to say
    # what was being attempted.
    assert "user_goal" in _build_injected_tool("baton_annotate", "off")["inputSchema"]["required"]


def test_the_injection_carries_the_mode_into_the_rendered_suffix() -> None:
    """The wiring, not the text — `test_llm_text.py` owns what each mode says.
    What is pinned here is that `_Injection` actually passes the mode through,
    because a knob that renders correctly and is never handed the value is the
    silent-no-op shape this repo keeps finding."""
    from baton_proxy._llm_text import build_instructions_suffix

    on = _Injection.create(None, proactive_mode="on", intent_param_mode="required")
    off = _Injection.create(None, proactive_mode="off", intent_param_mode="required")
    no_params = _Injection.create(None, intent_param_mode="off")
    no_reports = _Injection.create(None, proactive_mode="on", report_mode="off")
    assert on.instructions_suffix == build_instructions_suffix(
        "baton_annotate", "on", intent_param_mode="required"
    )
    assert off.instructions_suffix == build_instructions_suffix(
        "baton_annotate", "off", intent_param_mode="required"
    )
    assert "BEFORE" in on.instructions_suffix
    assert "BEFORE" not in off.instructions_suffix
    assert "subagent" in off.instructions_suffix
    assert "subagent" not in no_params.instructions_suffix
    assert no_reports.instructions_suffix == build_instructions_suffix(
        "baton_annotate", "on", intent_param_mode="optional", report_mode="off"
    )
    assert "baton_annotate" not in no_reports.instructions_suffix
    assert no_reports.tools == []


def test_a_tool_with_its_own_user_goal_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    proc, _emitter = _processor("required")
    native = _tool(
        "native",
        schema={"type": "object", "properties": {USER_GOAL_PARAM_NAME: {"type": "string"}}},
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        proc.handle_server_message(_tools_list_response([_tool("plain"), native]))
        # A client re-lists; the warning is not repeated.
        proc.handle_server_message(_tools_list_response([_tool("plain"), native], msg_id=3))
    warned = [r.getMessage() for r in caplog.records if "declares its own" in r.getMessage()]
    assert len(warned) == 1
    assert "'native'" in warned[0]

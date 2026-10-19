"""Per-tool goal-param injection (`user_goal`/`expected_result`) — unit + e2e
on both transports.

Covers the design contract:
schema injection at tools/list (skip-if-exists per param independently, never
on the proxy's own tools, required-mode appends only user_goal), the upserted
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
    build_expected_result_param_description,
    build_overall_task_param_description,
    build_user_goal_param_description,
)
from baton_proxy.config import (
    _INTENT_PARAM_MODES,
    DEFAULT_INTENT_PARAM_MODE,
    DEFAULT_PROACTIVE_MODE,
    Config,
)
from baton_proxy.proxy import (
    EXPECTED_RESULT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    OVERALL_TASK_PARAM_NAME,
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


ALL_THREE = (USER_GOAL_PARAM_NAME, EXPECTED_RESULT_PARAM_NAME, OVERALL_TASK_PARAM_NAME)


def _tool(name: str = "t", schema: Any = "default") -> dict[str, Any]:
    t: dict[str, Any] = {"name": name, "description": "d"}
    if schema == "default":
        t["inputSchema"] = {"type": "object", "properties": {"x": {"type": "string"}}}
    elif schema is not None:
        t["inputSchema"] = schema
    return t


def test_inject_optional_adds_all_three_params_without_touching_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_goal_params(tool, "optional") == {
        USER_GOAL_PARAM_NAME: "injected",
        EXPECTED_RESULT_PARAM_NAME: "injected",
        OVERALL_TASK_PARAM_NAME: "injected",
    }
    props = tool["inputSchema"]["properties"]
    assert props[USER_GOAL_PARAM_NAME]["type"] == "string"
    assert props[USER_GOAL_PARAM_NAME]["description"]
    assert props[EXPECTED_RESULT_PARAM_NAME]["type"] == "string"
    assert props[EXPECTED_RESULT_PARAM_NAME]["description"]
    assert props[OVERALL_TASK_PARAM_NAME]["type"] == "string"
    assert props[OVERALL_TASK_PARAM_NAME]["description"]
    assert tool["inputSchema"]["required"] == ["x"]


def test_inject_required_appends_all_three_to_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_goal_params(tool, "required") == {
        USER_GOAL_PARAM_NAME: "injected",
        EXPECTED_RESULT_PARAM_NAME: "injected",
        OVERALL_TASK_PARAM_NAME: "injected",
    }
    assert tool["inputSchema"]["required"] == ["x", *ALL_THREE]


def test_inject_required_leaves_a_native_param_out_of_required() -> None:
    tool = _tool(
        schema={"type": "object", "properties": {EXPECTED_RESULT_PARAM_NAME: {"type": "string"}}}
    )
    _inject_goal_params(tool, "required")
    assert tool["inputSchema"]["required"] == [USER_GOAL_PARAM_NAME, OVERALL_TASK_PARAM_NAME]


@pytest.mark.parametrize(
    ("mode", "expected_lead"), [("optional", "OPTIONAL."), ("required", "REQUIRED.")]
)
def test_inject_description_labels_track_the_mode(mode: str, expected_lead: str) -> None:
    """Under ``required`` the injector appends all three to the tool's
    ``required`` list, so a description still opening "OPTIONAL." would
    contradict the schema it ships inside. The ``optional`` leg is the control
    that proves the mode moves the label, not the injection."""
    tool = _tool()
    _inject_goal_params(tool, mode)
    props = tool["inputSchema"]["properties"]

    for name, build in (
        (USER_GOAL_PARAM_NAME, build_user_goal_param_description),
        (EXPECTED_RESULT_PARAM_NAME, build_expected_result_param_description),
        (OVERALL_TASK_PARAM_NAME, build_overall_task_param_description),
    ):
        desc = props[name]["description"]
        assert desc.startswith(expected_lead), f"{name} in {mode} mode: {desc[:12]!r}"
        assert desc == build(intent_param_mode=mode)
        # Only the label moved — the measured sentence after it is identical.
        assert desc[len(expected_lead) :] == build()[len("OPTIONAL.") :]


def test_inject_required_creates_required_list_when_absent() -> None:
    tool = _tool()
    _inject_goal_params(tool, "required")
    assert tool["inputSchema"]["required"] == list(ALL_THREE)


def test_inject_handles_schemaless_tool() -> None:
    tool = _tool(schema=None)
    _inject_goal_params(tool, "optional")
    assert USER_GOAL_PARAM_NAME in tool["inputSchema"]["properties"]
    assert EXPECTED_RESULT_PARAM_NAME in tool["inputSchema"]["properties"]
    assert OVERALL_TASK_PARAM_NAME in tool["inputSchema"]["properties"]


def test_inject_skips_native_param_untouched_independently() -> None:
    """A tool that already declares ONE of the names keeps it untouched while
    the others still get injected — dispositions are independent."""
    native_def = {"type": "string", "description": "the vendor's own"}
    tool = _tool(schema={"type": "object", "properties": {USER_GOAL_PARAM_NAME: native_def}})
    assert _inject_goal_params(tool, "optional") == {
        USER_GOAL_PARAM_NAME: "native",
        EXPECTED_RESULT_PARAM_NAME: "injected",
        OVERALL_TASK_PARAM_NAME: "injected",
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
        OVERALL_TASK_PARAM_NAME: "injected",
    }
    assert _inject_goal_params(tool, "required") == {
        USER_GOAL_PARAM_NAME: "native",
        EXPECTED_RESULT_PARAM_NAME: "native",
        OVERALL_TASK_PARAM_NAME: "native",
    }
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
    mode: str = "optional", proactive: str = DEFAULT_PROACTIVE_MODE
) -> tuple[MessageProcessor, _FakeEmitter]:
    """``proactive`` defaults to the shipped default so most tests exercise what
    an operator actually runs. Tests that drive an AGENT-authored pre-call
    annotation must ask for ``"on"`` — under ``off`` the handler refuses it,
    which is the mode's whole point."""
    emitter = _FakeEmitter()
    injection = _Injection.create(None, intent_param_mode=mode, proactive_mode=proactive)
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
            "alpha": {
                USER_GOAL_PARAM_NAME: "injected",
                EXPECTED_RESULT_PARAM_NAME: "injected",
                OVERALL_TASK_PARAM_NAME: "injected",
            },
            "beta": {
                USER_GOAL_PARAM_NAME: "injected",
                EXPECTED_RESULT_PARAM_NAME: "injected",
                OVERALL_TASK_PARAM_NAME: "injected",
            },
        }


def test_registry_skips_proxy_own_tools() -> None:
    """The proxy's own tools are never injected into.

    Asserted through the REGISTRY, not through param names. The annotation tool
    natively declares `user_goal`/`expected_result`/`overall_task` — the same
    three names injection adds — so "does this tool have a goal param" no
    longer distinguishes a tool that was injected into from one that always had
    them, and the old name-absence assertion would now pass for the wrong
    reason if injection DID run over it.

    The registry is the property that still separates them, and it is the one
    that matters: an entry is what makes `_extract_goal_params` strip those
    names from a call. An annotation tool in the registry would have its own
    arguments stripped out from under it — the agent's goal text silently
    removed from the very call that exists to record it.
    """
    proc, _ = _processor()
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    own = {"baton_annotate"}

    served = {t["name"] for t in out["result"]["tools"]}
    # `baton_annotate` is always served, and its presence is what makes the
    # registry assertion below meaningful. It is the only tool the proxy adds
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
    assert EXPECTED_RESULT_PARAM_NAME not in upstream["inputSchema"]["properties"]

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

    # Upstream-bound arguments: both params gone, vendor args intact.
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
    proc, emitter = _processor(proactive=proactive)
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
    proc, emitter = _processor(proactive="on")
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
    """No tools/list seen (proxy respawned mid-session): the reserved name
    makes strip-by-default safe."""
    proc, emitter = _processor()
    action = proc.handle_client_message(_call("never_listed", {USER_GOAL_PARAM_NAME: INTENT_TEXT}))
    assert USER_GOAL_PARAM_NAME not in action.forward["params"]["arguments"]
    assert [k for k, _ in emitter.calls] == ["tool_call_start"]
    assert emitter.calls[0][1]["call_intent"] == INTENT_TEXT


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
    """`required` appends the three params to the schema's advertised list and
    validates NOTHING — a wrapper that refused a customer's call to collect a
    telemetry string would be changing how the wrapped server behaves, which is
    the one thing it promises not to do.

    Both halves asserted: the schema says required, AND the omitting call is
    served and forwarded untouched."""
    proc, emitter = _processor("required")
    tool = _tool("alpha")
    proc.handle_server_message(_tools_list_response([tool]))
    assert tool["inputSchema"]["required"] == list(ALL_THREE)

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
    # Injection: every upstream tool grew both params; the proxy's own didn't.
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    for name in ("echo", "boom", "softfail", "argkeys"):
        assert USER_GOAL_PARAM_NAME in tools[name]["inputSchema"]["properties"], name
        assert EXPECTED_RESULT_PARAM_NAME in tools[name]["inputSchema"]["properties"], name
        # The default is `required`: the ADVERTISED list gains all three. The
        # vendor's own required entries are left alone.
        advertised = tools[name]["inputSchema"].get("required") or []
        assert set(ALL_THREE) <= set(advertised), name
    # `echo` is the fixture tool that declares a required field of its own
    # (`argkeys` ships `required: []`), so it is the one that can catch a
    # replace-instead-of-append.
    assert "text" in (tools["echo"]["inputSchema"].get("required") or []), (
        "appending to the advertised list clobbered the vendor's own required field"
    )
    # The annotate tool DECLARES these names itself now, so absence no longer
    # proves injection skipped it — see test_registry_skips_proxy_own_tools,
    # which asserts that structurally. What is still checkable here is that
    # its own contract was not rewritten by injection's "required" pass.
    annotate_schema = tools["baton_annotate"]["inputSchema"]
    assert annotate_schema["required"] == ["user_goal", "tool_name"]

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
    # The expectation rides the start event, per call. Call 2 sends only the
    # goal, so its key is ABSENT rather than null — "stated no expectation" and
    # "stated an empty one" must stay distinguishable downstream.
    assert starts[0]["payload"]["call_expected"] == EXPECTED_TEXT
    assert "call_expected" not in starts[1]["payload"]
    for s in starts:
        assert USER_GOAL_PARAM_NAME not in s["payload"]["params"]
        assert EXPECTED_RESULT_PARAM_NAME not in s["payload"]["params"]


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
    assert set(ALL_THREE) <= set(tools["echo"]["inputSchema"]["required"])
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


def test_expectation_rides_every_start_not_just_the_first() -> None:
    """``expected_result`` is injected on every tool, so each call's start
    event carries that call's own value."""
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
    proc, emitter = _processor(proactive="on")
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(
        _call(
            "baton_annotate",
            {"user_goal": "the user's goal", "overall_task": "morning meeting prep"},
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
    proc, emitter = _processor(proactive="on")
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(
        _call(
            "baton_annotate",
            {"user_goal": "the user's goal", "workflow": "morning meeting prep"},
            msg_id=9,
        )
    )

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    assert annotations[0][1]["workflow"] is None


def test_the_injected_task_label_rides_every_start_event() -> None:
    """``overall_task`` is stripped like the other two and lands on the start
    event as ``call_workflow``.

    This is the param the whole grouping rung keys on, and it is the one the
    proxy did not inject: before this, proxy-captured traffic emitted no
    ``call_workflow`` at all, so a consumer grouping by task label had nothing
    to group proxy sessions by and fell back to per-call intent text, which
    rewords freely and therefore shatters. Stripping matters as much as
    emitting — the upstream must never see the param, or the vendor's own
    handler receives an argument its schema never declared.
    """
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


def test_the_annotation_goal_and_expectation_land_on_their_wire_keys() -> None:
    """`user_goal` -> `intent`, `expected_result` -> `expected_outcome`.

    Same split as the task label: the agent is asked in the vocabulary the
    injected params use, and the event keeps the key the console and every
    stored event already carry. Both directions are pinned in one test because
    they share a seam — the three `args.get` calls sit on consecutive lines,
    and a careless edit takes them together.
    """
    proc, emitter = _processor(proactive="on")
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    proc.handle_client_message(
        _call(
            "baton_annotate",
            {"user_goal": "find the invoice", "expected_result": "one PDF"},
            msg_id=9,
        )
    )

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(annotations) == 1
    payload = annotations[0][1]
    assert payload["intent"] == "find the invoice"
    assert payload["expected_outcome"] == "one PDF"


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
    # NO annotation event. A refused proactive that still emits is the merge
    # hazard the mode exists to remove: one stray umbrella `overall_task` label
    # is enough to fuse two distinct tasks in a consumer that groups on it.
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
    assert "Do NOT call it before a tool call" in off
    assert "Populate proactively" not in off
    for field in ("user_goal", "expected_result", "overall_task", "what_happened", "tool_name"):
        assert field in on and field in off
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


def test_a_tool_with_its_own_overall_task_is_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    proc, _emitter = _processor("required")
    native = _tool(
        "native",
        schema={"type": "object", "properties": {OVERALL_TASK_PARAM_NAME: {"type": "string"}}},
    )
    with caplog.at_level(logging.WARNING, logger="baton_proxy"):
        proc.handle_server_message(_tools_list_response([_tool("plain"), native]))
    warned = [r.getMessage() for r in caplog.records if "overall_task" in r.getMessage()]
    assert len(warned) == 1
    assert "'native'" in warned[0]

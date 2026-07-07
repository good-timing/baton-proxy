"""Per-tool intent-param injection (`baton_intent`) — unit + e2e on both transports.

Covers the design contract:
schema injection at tools/list (skip-if-exists, never on the proxy's own tools,
required-mode appends), the upserted param registry (pagination / re-list safe),
strip-before-forward exactness at tools/call (upstream sees only vendor args),
the once-per-session proactive annotation synthesised from the FIRST param intent
(enqueued before its tool_call_start; suppressed after a real annotate proactive),
cold-registry strip-by-reserved-name, and the BATON_INTENT_PARAM knob.
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
    INTENT_PARAM_NAME,
    INTENT_SOURCE_PARAM,
    MessageProcessor,
    _inject_intent_param_into_tool,
    _Injection,
)

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"


# --------------------------------------------------------------------------- #
# Unit — _inject_intent_param_into_tool                                        #
# --------------------------------------------------------------------------- #


def _tool(name: str = "t", schema: Any = "default") -> dict[str, Any]:
    t: dict[str, Any] = {"name": name, "description": "d"}
    if schema == "default":
        t["inputSchema"] = {"type": "object", "properties": {"x": {"type": "string"}}}
    elif schema is not None:
        t["inputSchema"] = schema
    return t


def test_inject_optional_adds_param_without_touching_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_intent_param_into_tool(tool, "optional") == "injected"
    props = tool["inputSchema"]["properties"]
    assert props[INTENT_PARAM_NAME]["type"] == "string"
    assert props[INTENT_PARAM_NAME]["description"]
    assert tool["inputSchema"]["required"] == ["x"]


def test_inject_required_appends_to_required() -> None:
    tool = _tool(schema={"type": "object", "properties": {}, "required": ["x"]})
    assert _inject_intent_param_into_tool(tool, "required") == "injected"
    assert tool["inputSchema"]["required"] == ["x", INTENT_PARAM_NAME]


def test_inject_required_creates_required_list_when_absent() -> None:
    tool = _tool()
    assert _inject_intent_param_into_tool(tool, "required") == "injected"
    assert tool["inputSchema"]["required"] == [INTENT_PARAM_NAME]


def test_inject_handles_schemaless_tool() -> None:
    tool = _tool(schema=None)
    assert _inject_intent_param_into_tool(tool, "optional") == "injected"
    assert INTENT_PARAM_NAME in tool["inputSchema"]["properties"]


def test_inject_skips_native_param_untouched() -> None:
    native_def = {"type": "string", "description": "the vendor's own"}
    tool = _tool(schema={"type": "object", "properties": {INTENT_PARAM_NAME: native_def}})
    assert _inject_intent_param_into_tool(tool, "optional") == "native"
    # Skip-if-exists means UNTOUCHED — same object, no description rewrite.
    assert tool["inputSchema"]["properties"][INTENT_PARAM_NAME] is native_def


def test_inject_is_idempotent_across_relists() -> None:
    """Desktop lazily re-lists; the second pass must see its own injection as
    'native' and not duplicate the required entry or rewrite the schema."""
    tool = _tool(schema={"type": "object", "properties": {}, "required": []})
    assert _inject_intent_param_into_tool(tool, "required") == "injected"
    assert _inject_intent_param_into_tool(tool, "required") == "native"
    assert tool["inputSchema"]["required"].count(INTENT_PARAM_NAME) == 1


def test_inject_rejects_non_tool_objects() -> None:
    assert _inject_intent_param_into_tool("not a dict", "optional") is None
    assert _inject_intent_param_into_tool({"no": "name"}, "optional") is None
    assert _inject_intent_param_into_tool({"name": 42}, "optional") is None


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


def test_registry_upserts_across_paginated_lists() -> None:
    proc, _ = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    proc.handle_server_message(_tools_list_response([_tool("beta")], msg_id=3))
    with proc._registry_lock:
        assert proc._param_registry == {"alpha": "injected", "beta": "injected"}


def test_registry_skips_proxy_own_tools() -> None:
    proc, _ = _processor()
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    # _inject_into_response appended the proxy's tools AFTER param injection —
    # they must carry no intent param and no registry entry.
    injected_names = {"baton_annotate", "baton_session_report"}
    for tool in out["result"]["tools"]:
        if tool["name"] in injected_names:
            assert INTENT_PARAM_NAME not in tool["inputSchema"]["properties"]
    with proc._registry_lock:
        assert not (injected_names & set(proc._param_registry))


def test_off_mode_injects_and_strips_nothing() -> None:
    proc, emitter = _processor(mode="off")
    out = proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    upstream = [t for t in out["result"]["tools"] if t["name"] == "alpha"][0]
    assert INTENT_PARAM_NAME not in upstream["inputSchema"]["properties"]

    action = proc.handle_client_message(_call("alpha", {"x": "1", INTENT_PARAM_NAME: INTENT_TEXT}))
    # Off means fully off: no strip, param forwards as-is.
    assert action.forward["params"]["arguments"][INTENT_PARAM_NAME] == INTENT_TEXT
    starts = [c for c in emitter.calls if c[0] == "tool_call_start"]
    assert starts[0][1]["call_intent"] is None


def test_strip_and_capture_with_annotation_first() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))

    action = proc.handle_client_message(_call("alpha", {"x": "1", INTENT_PARAM_NAME: INTENT_TEXT}))

    # Upstream-bound arguments: param gone, vendor args intact.
    assert action.forward["params"]["arguments"] == {"x": "1"}

    kinds = [k for k, _ in emitter.calls]
    assert kinds == ["annotation", "tool_call_start"], "annotation must precede the start"
    ann = emitter.calls[0][1]
    assert ann["intent"] == INTENT_TEXT
    assert ann["signal_type"] is None
    assert ann["intent_source"] == INTENT_SOURCE_PARAM
    assert ann["tool_name"] == "alpha"
    start = emitter.calls[1][1]
    assert start["call_intent"] == INTENT_TEXT
    assert start["intent_source"] == INTENT_SOURCE_PARAM
    assert INTENT_PARAM_NAME not in start["params"]


def test_only_first_param_intent_becomes_annotation() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha"), _tool("beta")]))

    proc.handle_client_message(_call("alpha", {INTENT_PARAM_NAME: "first goal"}, msg_id=10))
    proc.handle_client_message(_call("beta", {INTENT_PARAM_NAME: "second goal"}, msg_id=11))

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
    proc.handle_client_message(_call("alpha", {INTENT_PARAM_NAME: INTENT_TEXT}, msg_id=10))

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
    proc.handle_client_message(_call("alpha", {INTENT_PARAM_NAME: INTENT_TEXT}, msg_id=10))

    annotations = [c for c in emitter.calls if c[0] == "annotation"]
    # Reactive + the synthesised proactive: the reactive carried signal_type,
    # so the param intent still opens the session's proactive slot.
    assert len(annotations) == 2
    assert annotations[1][1]["intent"] == INTENT_TEXT


def test_native_param_forwards_untouched_and_captures_nothing() -> None:
    proc, emitter = _processor()
    native = _tool(
        "alpha", schema={"type": "object", "properties": {INTENT_PARAM_NAME: {"type": "string"}}}
    )
    proc.handle_server_message(_tools_list_response([native]))

    action = proc.handle_client_message(_call("alpha", {INTENT_PARAM_NAME: "vendor's value"}))
    assert action.forward["params"]["arguments"][INTENT_PARAM_NAME] == "vendor's value"
    assert [k for k, _ in emitter.calls] == ["tool_call_start"]
    assert emitter.calls[0][1]["call_intent"] is None
    # The vendor's param is a REAL argument — it stays in captured params.
    assert emitter.calls[0][1]["params"][INTENT_PARAM_NAME] == "vendor's value"


def test_cold_registry_strips_by_reserved_name() -> None:
    """No tools/list seen (proxy respawned mid-session): the reserved name
    makes strip-by-default safe."""
    proc, emitter = _processor()
    action = proc.handle_client_message(_call("never_listed", {INTENT_PARAM_NAME: INTENT_TEXT}))
    assert INTENT_PARAM_NAME not in action.forward["params"]["arguments"]
    ann = [c for c in emitter.calls if c[0] == "annotation"]
    assert len(ann) == 1 and ann[0][1]["intent"] == INTENT_TEXT


def test_blank_param_value_strips_but_captures_nothing() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_tools_list_response([_tool("alpha")]))
    action = proc.handle_client_message(_call("alpha", {"x": "1", INTENT_PARAM_NAME: "  "}))
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
            "arguments": {"text": "x", INTENT_PARAM_NAME: INTENT_TEXT},
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "echo",
            "arguments": {"text": "hi", INTENT_PARAM_NAME: "second call goal"},
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
    # Injection: every upstream tool grew the param; the proxy's own didn't.
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    for name in ("echo", "boom", "argkeys"):
        assert INTENT_PARAM_NAME in tools[name]["inputSchema"]["properties"], name
        # optional mode: required untouched
        assert INTENT_PARAM_NAME not in (tools[name]["inputSchema"].get("required") or [])
    assert INTENT_PARAM_NAME not in tools["baton_annotate"]["inputSchema"]["properties"]

    # Strip exactness: the upstream reports exactly which keys it received.
    assert by_id[3]["result"]["content"][0]["text"] == "keys: text"
    assert "Echo: hi" in by_id[4]["result"]["content"][0]["text"]

    # Events: one synthesised proactive (first call only), before its start.
    annotations = [e for e in events if e["event_type"] == "annotation"]
    assert len(annotations) == 1
    ann = annotations[0]["payload"]
    assert ann["intent"] == INTENT_TEXT
    assert ann["intent_source"] == INTENT_SOURCE_PARAM
    assert ann["tool_name"] == "argkeys"
    assert "signal_type" not in ann  # proactive

    starts = [e for e in events if e["event_type"] == "tool_call_start"]
    assert [s["payload"]["tool_name"] for s in starts] == ["argkeys", "echo"]
    assert [s["payload"]["call_intent"] for s in starts] == [INTENT_TEXT, "second call goal"]
    for s in starts:
        assert INTENT_PARAM_NAME not in s["payload"]["params"]
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
    assert INTENT_PARAM_NAME in tools["echo"]["inputSchema"]["required"]
    # The annotate tool's required list is its own contract — untouched.
    assert tools["baton_annotate"]["inputSchema"]["required"] == ["intent"]


def test_intent_param_e2e_off_mode() -> None:
    by_id, events = _run_stdio({"BATON_INTENT_PARAM": "off"})
    tools = {t["name"]: t for t in by_id[2]["result"]["tools"]}
    assert INTENT_PARAM_NAME not in tools["echo"]["inputSchema"]["properties"]
    # Param forwarded untouched -> upstream reports it among its keys.
    assert by_id[3]["result"]["content"][0]["text"] == f"keys: {INTENT_PARAM_NAME},text"
    assert not [e for e in events if e["event_type"] == "annotation"]

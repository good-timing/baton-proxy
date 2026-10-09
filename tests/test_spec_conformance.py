"""Every event baton-proxy emits over stdio, against the fixture upstream,
validates against the shared wire schema in the ``baton-spec`` submodule
(SPEC §11.4), and the scenario produces every event type that schema declares.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from baton_proxy.config import Config
from baton_proxy.emitter import Emitter
from baton_proxy.identity import Principal
from tests.fixture_responses import BAD_CURSOR

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"
SCHEMA_PATH = REPO / "baton-spec" / "events.schema.json"

# Declared by the schema and never sent by the proxy. ``failure_kind`` names a
# failure a producer makes above the vendor's handler, and the proxy has no such
# layer. ``result_capture`` marks withheld results, which the proxy cannot do.
NEVER_SENT = {
    "tool_call_end": {"result_capture"},
    "tool_call_error": {"failure_kind", "result_capture"},
}

E2E_REQUESTS: list[dict] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "conformance-client", "version": "0.1.0"},
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
                "user_goal": "verify conformance",
                "expected_result": "a valid envelope",
                "overall_task": "verify conformance",
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "boom", "arguments": {}},
    },
    # The returned-flag failure shape. `boom` above takes the JSON-RPC `error`
    # lane and structurally cannot carry a `result`, so no existing request can
    # produce this payload — see the coverage assertion below.
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "softfail", "arguments": {}},
    },
    # A reactive annotation, for the four `AnnotationPayload` members no other
    # request reaches. `baton_annotate` is proxy-owned and never forwarded
    # upstream (`proxy.py:74`), so this touches no fixture. ⚠ `context` must be
    # an OBJECT — `proxy.py:821` drops a non-dict, and a string here silently
    # leaves the member unexercised.
    {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "signal_type": "failure",
                "user_goal": "exercise every declared annotation member",
                "expected_result": "all declared properties present",
                "suggested_improvement": "return a structured empty result",
                "workflow": "conformance coverage",
                "context": {"tool": "softfail", "outcome": "isError"},
            },
        },
    },
    # A second call to a tool already called, so a call_id is shown to belong
    # to the call and not to the tool name.
    {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "argkeys", "arguments": {"text": "y"}},
    },
    {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {"cursor": BAD_CURSOR}},
    {"jsonrpc": "2.0", "id": 9, "method": "resources/list", "params": {}},
    {"jsonrpc": "2.0", "id": 10, "method": "resources/list", "params": {"cursor": BAD_CURSOR}},
    {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "resources/read",
        "params": {"uri": "fixture://notes.txt"},
    },
    {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "resources/read",
        "params": {"uri": "fixture://secret.txt"},
    },
    {"jsonrpc": "2.0", "id": 13, "method": "prompts/list", "params": {}},
    {"jsonrpc": "2.0", "id": 14, "method": "prompts/list", "params": {"cursor": BAD_CURSOR}},
    {"jsonrpc": "2.0", "id": 15, "method": "prompts/get", "params": {"name": "summarize"}},
    {"jsonrpc": "2.0", "id": 16, "method": "prompts/get", "params": {"name": "boom_prompt"}},
]


def _payload_def_name(event_type: str) -> str:
    """``tool_call_error`` -> ``ToolCallErrorPayload``, the schema's $defs key."""
    return "".join(part.title() for part in event_type.split("_")) + "Payload"


def _run_stdio() -> list[dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "BATON_VENDOR_ID": "v",
            "BATON_EVENT_SINK": "stderr:",
        }
    )
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
        _stdout, stderr = proc.communicate(input=input_data, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()

    events = []
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
    return events


@pytest.fixture(scope="module")
def event_schema() -> dict:
    if not SCHEMA_PATH.exists():
        pytest.skip(f"baton-spec submodule not checked out ({SCHEMA_PATH} missing)")
    return json.loads(SCHEMA_PATH.read_text())


def test_emitted_events_conform_to_shared_schema(event_schema: dict, tmp_path: Path) -> None:
    events = _run_stdio()
    for event in events:
        jsonschema.validate(event, event_schema)

    declared_types = set(event_schema["discriminator"]["mapping"])
    seen_types = {e["event_type"] for e in events}
    assert seen_types == declared_types, (
        f"the scenario did not produce: {declared_types - seen_types}; "
        f"the schema does not declare: {seen_types - declared_types}"
    )

    # A property the schema declares and no event carries is a shape this test
    # validates without ever producing it.
    unexercised: dict[str, list[str]] = {}
    for event_type in sorted(declared_types):
        declared = set(event_schema["$defs"][_payload_def_name(event_type)].get("properties", {}))
        produced: set[str] = set()
        for event in events:
            if event["event_type"] == event_type:
                produced |= set(event["payload"])
        missing = declared - produced - NEVER_SENT.get(event_type, set())
        if missing:
            unexercised[event_type] = sorted(missing)
        assert not produced & NEVER_SENT.get(event_type, set()), event_type
    assert not unexercised, f"schema properties no emitted payload carried: {unexercised}"

    # The stdio scenario resolves no principal, so ``principal`` never
    # reaches the loop above. Drive the emitter with one, as baton-extmcp
    # does, and validate the event it writes. Kept in this
    # test rather than a third one: try/SECURITY.md §8 counts the two tests
    # that skip without the submodule, and test_try_kit.py pins that count.
    sink = tmp_path / "events.jsonl"
    config = Config(
        session_id="conformance-session",
        event_sink=f"file://{sink}",
        tenant_id="conformance",
        api_key=None,
        consent_token="ct_conformance",
        vendor_id="conformance-vendor",
        log_file=None,
    )
    emitter = Emitter(config)
    emitter.start()
    emitter.enqueue_tool_call_start(
        tool_name="echo", params={}, principal=Principal(principal_id="u123"), call_id="c1"
    )
    emitter.stop()
    event = json.loads(sink.read_text().splitlines()[-1])
    # All three members, not just the id — the schema's ``required`` would catch
    # a missing one, but not a flat ``principal_id`` surviving BESIDE the object
    # (``additionalProperties: false`` catches that) nor a ``source`` quietly
    # reading "attested". Assert the shape and both values here, where the SPEC
    # §11.4 contract is being validated rather than inferred.
    assert "principal_id" not in event, "the flat field is retired (SPEC §13)"
    principal = event["principal"]
    assert set(principal) == {"id", "source", "form"}
    assert principal["id"] == "u123"
    assert principal["source"] == "asserted", "the proxy verifies nothing"
    assert principal["form"] == "raw"
    jsonschema.validate(event, event_schema)


def test_vectors_still_conform_to_the_schema_shipped_alongside_them(event_schema: dict) -> None:
    vectors_dir = REPO / "baton-spec" / "vectors"
    vectors = sorted(vectors_dir.glob("*.json"))
    assert vectors, f"no vectors found in {vectors_dir}"

    for vector_path in vectors:
        event = json.loads(vector_path.read_text())
        jsonschema.validate(event, event_schema)


def test_a_tool_call_carries_one_call_id_on_both_legs() -> None:
    events = _run_stdio()
    starts = [e for e in events if e["event_type"] == "tool_call_start"]
    closes = [e for e in events if e["event_type"] in ("tool_call_end", "tool_call_error")]
    assert [e["payload"]["tool_name"] for e in starts] == ["argkeys", "boom", "softfail", "argkeys"]

    start_ids = [e["call_id"] for e in starts]
    assert all(isinstance(i, str) and i for i in start_ids)
    assert len(set(start_ids)) == len(starts)
    assert sorted((e["call_id"], e["payload"]["tool_name"]) for e in closes) == sorted(
        (e["call_id"], e["payload"]["tool_name"]) for e in starts
    )

    others = [e for e in events if not e["event_type"].startswith("tool_call_")]
    assert others
    assert all("call_id" not in e for e in others)

"""Every event baton-proxy actually emits (over stdio, against the fixture
upstream) validates against the shared wire schema in the ``baton-spec``
submodule (SPEC §11.4) — the cross-repo counterpart to baton-sdk's own
``tests/functional/test_spec_conformance.py``. This is what would have
caught the SPEC §13 `name`/`names` divergence between the SDK and
baton-proxy before it shipped.

Scope note: baton-proxy also emits resource_read_*/resource_list_*/
prompt_get_*/prompt_list_* events (see ``emitter.py``) that baton-sdk does
not emit yet — that gap is tracked separately (sdk-hardening thread,
"resource/prompt capture parity"). ``events.schema.json`` only covers the
five event types the SDK also emits today, so those are the only ones
validated here; the others are explicitly excluded below rather than
silently skipped, so this test doesn't quietly stop covering them once the
SDK gap closes and they need adding to the schema too.
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
from baton_proxy.identity import HASH_SCHEME, Principal

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"
SCHEMA_PATH = REPO / "baton-spec" / "events.schema.json"

# event_types covered by events.schema.json today — see module docstring.
SCHEMA_COVERED_EVENT_TYPES = {
    "tool_call_start",
    "tool_call_end",
    "tool_call_error",
    "annotation",
    "surface_snapshot",
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
    covered = [e for e in events if e["event_type"] in SCHEMA_COVERED_EVENT_TYPES]
    assert covered, "scenario produced no schema-covered events — check the fixture/scenario"

    for event in covered:
        jsonschema.validate(event, event_schema)

    seen_types = {e["event_type"] for e in covered}
    assert seen_types == SCHEMA_COVERED_EVENT_TYPES, (
        f"scenario didn't exercise every schema-covered type, missing: "
        f"{SCHEMA_COVERED_EVENT_TYPES - seen_types}"
    )

    # ⚠ Property coverage, not one hand-picked property — the same idiom
    # `seen_types` applies one level up, applied one level down. The gap that
    # prompted this was exactly that shape: `result` on `tool_call_error` was
    # declared by the schema and produced by nothing, so the pin bump that
    # legalised it went unexercised and nothing reddened. Enumerating properties
    # closes the NEXT such gap too; a hand-written assert closes only this one.
    #
    # Measured at ZERO allowlist — 31 of 31 declared properties across the five
    # covered types. If a future property genuinely cannot be driven from here,
    # add it to an explicit allowlist rather than deleting the loop: an empty
    # one is what makes this worth having.
    unexercised: dict[str, list[str]] = {}
    for event_type in sorted(SCHEMA_COVERED_EVENT_TYPES):
        declared = set(event_schema["$defs"][_payload_def_name(event_type)].get("properties", {}))
        produced: set[str] = set()
        for event in covered:
            if event["event_type"] == event_type:
                produced |= set(event["payload"])
        if declared - produced:
            unexercised[event_type] = sorted(declared - produced)
    assert not unexercised, (
        f"schema properties that no emitted payload exercised: {unexercised} — the "
        "gate validates shapes this scenario never produces"
    )

    # The stdio scenario resolves no principal, so ``principal`` never
    # reaches the loop above. Drive the emitter with one and an HMAC key, as
    # baton-extmcp does, and validate the hashed event it writes. Kept in this
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
        principal_id_hmac_key=b"conformance-key",
    )
    emitter = Emitter(config)
    emitter.start()
    emitter.enqueue_tool_call_start(
        tool_name="echo", params={}, principal=Principal(principal_id="u123")
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
    assert principal["id"].startswith(f"{HASH_SCHEME}:")
    assert principal["source"] == "asserted", "the proxy verifies nothing"
    assert principal["form"] == "hashed"
    jsonschema.validate(event, event_schema)


def test_vectors_still_conform_to_the_schema_shipped_alongside_them(event_schema: dict) -> None:
    vectors_dir = REPO / "baton-spec" / "vectors"
    vectors = sorted(vectors_dir.glob("*.json"))
    assert vectors, f"no vectors found in {vectors_dir}"

    for vector_path in vectors:
        event = json.loads(vector_path.read_text())
        jsonschema.validate(event, event_schema)

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
            },
        },
    },
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "boom", "arguments": {}}},
]


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


def test_emitted_events_conform_to_shared_schema(event_schema: dict) -> None:
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


def test_vectors_still_conform_to_the_schema_shipped_alongside_them(event_schema: dict) -> None:
    vectors_dir = REPO / "baton-spec" / "vectors"
    vectors = sorted(vectors_dir.glob("*.json"))
    assert vectors, f"no vectors found in {vectors_dir}"

    for vector_path in vectors:
        event = json.loads(vector_path.read_text())
        jsonschema.validate(event, event_schema)

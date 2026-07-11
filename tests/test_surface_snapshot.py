"""Surface-snapshot emission — the vendor-true surface, captured at the seam.

Covers the design contract (design-note: server_surface_and_change_spec):
snapshot built from initialize + the FIRST complete tools/list response,
pre-injection (no baton_* tools, no intent param, pre-suffix instructions);
hash-deduped re-lists (unchanged surface never re-emits, changed surface
does); pagination fragments never snapshotted (cursor request pages and
nextCursor responses both skipped); initialize-less capture still emits with
null server meta; and the outgoing message mutation is unaffected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from baton_proxy.proxy import (
    INTENT_PARAM_NAME,
    MessageProcessor,
    _Injection,
    _surface_hash,
)

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"


class _FakeEmitter:
    """Records enqueue calls in order; only the methods these paths use."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def enqueue_annotation(self, **kwargs: Any) -> None:
        self.calls.append(("annotation", kwargs))

    def enqueue_tool_call_start(self, **kwargs: Any) -> None:
        self.calls.append(("tool_call_start", kwargs))

    def enqueue_surface_snapshot(self, **kwargs: Any) -> None:
        self.calls.append(("surface_snapshot", kwargs))

    def snapshots(self) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == "surface_snapshot"]


def _processor(mode: str = "optional") -> tuple[MessageProcessor, _FakeEmitter]:
    emitter = _FakeEmitter()
    injection = _Injection.create(None, intent_param_mode=mode)
    return MessageProcessor(emitter, injection, "test-session"), emitter  # type: ignore[arg-type]


def _tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} does things",
        "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
    }


def _initialize_response(msg_id: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "protocolVersion": "2025-03-26",
            "serverInfo": {"name": "mock-upstream", "version": "0.9"},
            "capabilities": {"tools": {"listChanged": True}},
            "instructions": "Vendor-authored instructions.",
        },
    }


def _list_request(msg_id: int = 2, cursor: str | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": "tools/list", "params": {}}
    if cursor is not None:
        req["params"]["cursor"] = cursor
    return req


def _list_response(
    tools: list[dict[str, Any]], msg_id: int = 2, next_cursor: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {"tools": tools}
    if next_cursor is not None:
        result["nextCursor"] = next_cursor
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _drive_list(
    proc: MessageProcessor,
    tools: list[dict[str, Any]],
    msg_id: int,
    cursor: str | None = None,
    next_cursor: str | None = None,
) -> dict[str, Any]:
    action = proc.handle_client_message(_list_request(msg_id, cursor=cursor))
    assert action.forward is not None  # list requests always forward unchanged
    return proc.handle_server_message(_list_response(tools, msg_id, next_cursor=next_cursor))


# --------------------------------------------------------------------------- #
# The snapshot payload — vendor-true, pre-injection                            #
# --------------------------------------------------------------------------- #


def test_snapshot_captures_vendor_true_surface() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_initialize_response())
    out = _drive_list(proc, [_tool("enrich_company"), _tool("get_intent_signals")], msg_id=2)

    snaps = emitter.snapshots()
    assert len(snaps) == 1
    snap = snaps[0]

    # Pre-suffix instructions and initialize metadata.
    assert snap["instructions"] == "Vendor-authored instructions."
    assert snap["server_info"] == {"name": "mock-upstream", "version": "0.9"}
    assert snap["capabilities"] == {"tools": {"listChanged": True}}

    # Vendor tools only, and WITHOUT the injected intent param — even though
    # the outgoing message got both injections.
    names = [t["name"] for t in snap["tools"]]
    assert names == ["enrich_company", "get_intent_signals"]
    for tool in snap["tools"]:
        assert INTENT_PARAM_NAME not in tool["inputSchema"]["properties"]

    out_names = {t["name"] for t in out["result"]["tools"]}
    assert "baton_annotate" in out_names  # mutation still happened downstream
    assert snap["surface_hash"].startswith("sha256:")

    aug = snap["seam_augmentations"]
    assert "baton_annotate" in aug["injected_tools"]
    assert aug["intent_param"] == {"name": INTENT_PARAM_NAME, "mode": "optional"}
    assert aug["instructions_suffix"] is True


def test_snapshot_instructions_captured_before_suffix_injection() -> None:
    proc, emitter = _processor()
    out = proc.handle_server_message(_initialize_response())
    _drive_list(proc, [_tool("alpha")], msg_id=2)
    # The message forwarded to the client carries the suffix…
    assert out["result"]["instructions"] != "Vendor-authored instructions."
    assert out["result"]["instructions"].startswith("Vendor-authored instructions.")
    # …the snapshot does not.
    assert emitter.snapshots()[0]["instructions"] == "Vendor-authored instructions."


def test_off_mode_records_null_intent_param() -> None:
    proc, emitter = _processor(mode="off")
    proc.handle_server_message(_initialize_response())
    _drive_list(proc, [_tool("alpha")], msg_id=2)
    assert emitter.snapshots()[0]["seam_augmentations"]["intent_param"] is None


def test_snapshot_without_initialize_has_null_server_meta() -> None:
    # Proxy respawned mid-session: no initialize crossed us. Snapshot still
    # emits; server meta is null rather than fabricated.
    proc, emitter = _processor()
    _drive_list(proc, [_tool("alpha")], msg_id=2)
    snap = emitter.snapshots()[0]
    assert snap["server_info"] is None
    assert snap["capabilities"] is None
    assert snap["instructions"] is None
    assert [t["name"] for t in snap["tools"]] == ["alpha"]


# --------------------------------------------------------------------------- #
# Dedupe + change detection                                                    #
# --------------------------------------------------------------------------- #


def test_unchanged_relist_does_not_reemit() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_initialize_response())
    _drive_list(proc, [_tool("alpha")], msg_id=2)
    _drive_list(proc, [_tool("alpha")], msg_id=3)  # Desktop-style lazy re-list
    assert len(emitter.snapshots()) == 1


def test_changed_surface_reemits_with_new_hash() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_initialize_response())
    _drive_list(proc, [_tool("alpha")], msg_id=2)
    # listChanged refire after an upstream mutation: a tool appeared.
    _drive_list(proc, [_tool("alpha"), _tool("beta")], msg_id=3)
    snaps = emitter.snapshots()
    assert len(snaps) == 2
    assert snaps[0]["surface_hash"] != snaps[1]["surface_hash"]


def test_hash_is_key_order_independent() -> None:
    a = {"server_info": {"name": "s"}, "capabilities": None, "instructions": None, "tools": []}
    b = {"tools": [], "instructions": None, "capabilities": None, "server_info": {"name": "s"}}
    assert _surface_hash(a) == _surface_hash(b)


# --------------------------------------------------------------------------- #
# Pagination — fragments are never surfaces                                    #
# --------------------------------------------------------------------------- #


def test_paginated_first_page_is_skipped() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_initialize_response())
    _drive_list(proc, [_tool("alpha")], msg_id=2, next_cursor="page2")
    assert emitter.snapshots() == []


def test_cursor_continuation_page_is_skipped() -> None:
    proc, emitter = _processor()
    proc.handle_server_message(_initialize_response())
    _drive_list(proc, [_tool("beta")], msg_id=3, cursor="page2")
    assert emitter.snapshots() == []


def test_unsolicited_tools_list_response_is_skipped() -> None:
    # A tools/list-shaped response whose request never crossed us (or whose
    # id we already consumed) is not a snapshot candidate.
    proc, emitter = _processor()
    proc.handle_server_message(_initialize_response())
    proc.handle_server_message(_list_response([_tool("alpha")], msg_id=99))
    assert emitter.snapshots() == []


def test_error_response_forgets_tracked_id() -> None:
    proc, emitter = _processor()
    proc.handle_client_message(_list_request(msg_id=2))
    proc.handle_server_message(
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "boom"}}
    )
    # The id was consumed by the error; a later same-id response can't snapshot.
    proc.handle_server_message(_list_response([_tool("alpha")], msg_id=2))
    assert emitter.snapshots() == []
    with proc._surface_lock:
        assert 2 not in proc._toollist_first_page_ids


def test_tracking_dict_bounded() -> None:
    proc, _ = _processor()
    from baton_proxy.proxy import MAX_PENDING_TOOLLISTS

    for i in range(MAX_PENDING_TOOLLISTS + 10):
        proc.handle_client_message(_list_request(msg_id=1000 + i))
    with proc._surface_lock:
        assert len(proc._toollist_first_page_ids) == MAX_PENDING_TOOLLISTS
        assert 1000 not in proc._toollist_first_page_ids  # oldest evicted


# --------------------------------------------------------------------------- #
# E2E — the snapshot rides the real wire envelope through the subprocess       #
# --------------------------------------------------------------------------- #


def test_e2e_snapshot_on_stderr_sink() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "BATON_VENDOR_ID": "fixture",
            "BATON_EVENT_SINK": "stderr:",
        }
    )
    requests = [
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
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},  # re-list: deduped
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--", sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    input_data = "".join(json.dumps(r) + "\n" for r in requests)
    try:
        _stdout, stderr = proc.communicate(input=input_data, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()

    snaps = []
    for line in stderr.splitlines():
        try:
            msg = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if msg.get("event_type") == "surface_snapshot":
            snaps.append(msg)

    assert len(snaps) == 1, f"expected exactly one snapshot, got {len(snaps)}"
    payload = snaps[0]["payload"]
    assert payload["server_info"]["name"] == "fixture-mcp-server"
    assert payload["instructions"] == "Fixture MCP server. Use echo to echo text."
    names = [t["name"] for t in payload["tools"]]
    assert "echo" in names and "baton_annotate" not in names
    for tool in payload["tools"]:
        assert INTENT_PARAM_NAME not in tool.get("inputSchema", {}).get("properties", {})
    assert payload["surface_hash"].startswith("sha256:")
    # Envelope fields present like any other event.
    assert snaps[0]["vendor_id"] == "fixture"
    assert "sequence_number" in snaps[0]

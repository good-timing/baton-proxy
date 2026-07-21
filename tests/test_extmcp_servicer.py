"""Unit tests for the ExtMcp servicer (baton_proxy.extmcp.servicer).

Drives CheckRequest/CheckResponse with hand-built proto messages — no live
agentgateway — to assert the request-side capture contract: inject on
tools/list, capture+strip on tools/call, dedup, deny, session/identity from
headers. Skipped when the [extmcp] extra (grpc/protobuf) isn't installed.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("grpc", reason="baton-proxy[extmcp] not installed")

from baton_proxy.extmcp import ext_mcp_pb2 as pb  # noqa: E402
from baton_proxy.extmcp.servicer import ExtMcpProcessor  # noqa: E402


class FakeEmitter:
    """Records enqueue_* calls instead of emitting."""

    def __init__(self) -> None:
        self.tool_call_starts: list[dict] = []
        self.surface_snapshots: list[dict] = []
        self.annotations: list[dict] = []

    def enqueue_tool_call_start(self, **kw):
        self.tool_call_starts.append(kw)

    def enqueue_surface_snapshot(self, **kw):
        self.surface_snapshots.append(kw)

    def enqueue_annotation(self, **kw):
        self.annotations.append(kw)


def _tools_list_response(tools):
    resp = pb.McpResponse()
    resp.method = "tools/list"
    resp.mcp_response = json.dumps({"tools": tools}).encode("utf-8")
    return resp


def _tools_call_request(name, arguments, *, session="sess-1", headers=None):
    req = pb.McpRequest()
    req.method = "tools/call"
    req.mcp_request = json.dumps({"name": name, "arguments": arguments}).encode("utf-8")
    hdrs = {"mcp-session-id": session}
    hdrs.update(headers or {})
    for k, v in hdrs.items():
        req.headers.add(key=k, value=v.encode("utf-8"))
    return req


def _proc(**kw):
    return ExtMcpProcessor(FakeEmitter(), **kw)


# ---- tools/list: inject + snapshot -----------------------------------------


def test_tools_list_injects_both_params_on_every_tool():
    proc = _proc()
    tools = [
        {"name": "get_thing", "inputSchema": {"type": "object", "properties": {}}},
        {
            "name": "set_thing",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
    ]
    out = proc.CheckResponse(_tools_list_response(tools), None)
    assert out.WhichOneof("result") == "mutated"
    mutated = json.loads(out.mutated)
    for t in mutated["tools"]:
        props = t["inputSchema"]["properties"]
        assert "user_goal" in props and "expected_result" in props
    # pre-existing prop preserved
    assert "id" in mutated["tools"][1]["inputSchema"]["properties"]


def test_tools_list_emits_surface_snapshot_once_per_hash():
    proc = _proc()
    tools = [{"name": "get_thing", "inputSchema": {"type": "object", "properties": {}}}]
    proc.CheckResponse(_tools_list_response(tools), None)
    proc.CheckResponse(_tools_list_response(tools), None)  # identical → deduped
    assert len(proc._emitter.surface_snapshots) == 1
    snap = proc._emitter.surface_snapshots[0]
    assert snap["seam_augmentations"] == {"injected": ["user_goal", "expected_result"]}
    # snapshot is the vendor-true (pre-injection) surface
    assert "user_goal" not in snap["tools"][0]["inputSchema"]["properties"]


def test_tools_list_skip_if_exists_does_not_clobber():
    proc = _proc()
    tools = [
        {
            "name": "x",
            "inputSchema": {
                "type": "object",
                "properties": {"user_goal": {"type": "string", "description": "custom"}},
            },
        }
    ]
    out = proc.CheckResponse(_tools_list_response(tools), None)
    props = json.loads(out.mutated)["tools"][0]["inputSchema"]["properties"]
    assert props["user_goal"]["description"] == "custom"  # not overwritten
    assert "expected_result" in props  # the missing one still added


# ---- tools/call: capture + strip -------------------------------------------


def test_tools_call_captures_intent_and_strips():
    proc = _proc()
    req = _tools_call_request(
        "echo",
        {"message": "hi", "user_goal": "confirm echo", "expected_result": "hi back"},
    )
    out = proc.CheckRequest(req, None)
    # backend gets clean params
    assert out.WhichOneof("result") == "mutated"
    clean = json.loads(out.mutated)["arguments"]
    assert clean == {"message": "hi"}
    assert out.metadata["baton_captured"] is True
    # capture emitted with clean params + intent + header session
    tcs = proc._emitter.tool_call_starts[-1]
    assert tcs["tool_name"] == "echo"
    assert tcs["params"] == {"message": "hi"}
    assert tcs["call_intent"] == "confirm echo"
    assert tcs["intent_source"] == "injected_param"
    assert tcs["session_id"] == "sess-1"
    # first-per-session proactive annotation carries expected_outcome
    ann = proc._emitter.annotations[-1]
    assert ann["intent"] == "confirm echo"
    assert ann["expected_outcome"] == "hi back"
    assert ann["session_id"] == "sess-1"


def test_tools_call_without_injected_params_passes():
    proc = _proc()
    out = proc.CheckRequest(_tools_call_request("echo", {"message": "hi"}), None)
    assert out.WhichOneof("result") == "pass"
    # still captured (request-side floor), just no intent
    tcs = proc._emitter.tool_call_starts[-1]
    assert tcs["params"] == {"message": "hi"}
    assert tcs["call_intent"] is None
    assert tcs["intent_source"] is None
    # no intent → no proactive annotation
    assert proc._emitter.annotations == []


def test_proactive_annotation_only_once_per_session():
    proc = _proc()
    for _ in range(3):
        proc.CheckRequest(_tools_call_request("echo", {"message": "hi", "user_goal": "g"}), None)
    assert len(proc._emitter.annotations) == 1  # first call only
    assert len(proc._emitter.tool_call_starts) == 3  # every call captured


def test_identity_headers_captured_to_runtime_meta():
    proc = _proc()
    req = _tools_call_request(
        "echo",
        {"message": "hi", "user_goal": "g"},
        headers={"x-gw-ims-user-id": "u123", "x-gw-ims-org-id": "org9"},
    )
    proc.CheckRequest(req, None)
    rm = proc._emitter.tool_call_starts[-1]["runtime_meta"]
    assert rm == {"x-gw-ims-user-id": "u123", "x-gw-ims-org-id": "org9"}


def test_custom_session_header():
    proc = _proc(session_header="x-corp-session")
    req = _tools_call_request(
        "echo",
        {"message": "hi", "user_goal": "g"},
        session="ignored",
        headers={"x-corp-session": "real-sess"},
    )
    proc.CheckRequest(req, None)
    assert proc._emitter.tool_call_starts[-1]["session_id"] == "real-sess"


def test_deny_all_returns_permission_denied():
    proc = _proc(deny_all=True)
    out = proc.CheckRequest(_tools_call_request("echo", {"message": "hi"}), None)
    assert out.WhichOneof("result") == "error"
    assert out.error.code == pb.AuthorizationError.PERMISSION_DENIED
    # still captured before denying
    assert len(proc._emitter.tool_call_starts) == 1


# ---- defensive passes -------------------------------------------------------


def test_non_toolscall_request_passes_without_capture():
    proc = _proc()
    req = pb.McpRequest()
    req.method = "resources/list"
    out = proc.CheckRequest(req, None)
    assert out.WhichOneof("result") == "pass"
    assert proc._emitter.tool_call_starts == []


def test_non_toolslist_response_passes():
    proc = _proc()
    resp = pb.McpResponse()
    resp.method = "tools/call"  # response hook not used in prod; defensive pass
    resp.mcp_response = json.dumps({"content": []}).encode("utf-8")
    out = proc.CheckResponse(resp, None)
    assert out.WhichOneof("result") == "pass"
    assert proc._emitter.surface_snapshots == []

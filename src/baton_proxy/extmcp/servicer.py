"""The ExtMcp gRPC servicer — request-side capture at an agentgateway seam.

Implements the agentgateway ExtMcp contract (ext_mcp.proto) over h2c. Two hooks,
request-side only (the decided prod shape — v1.3.1 gives no response↔request
correlation, so response-side capture is the separate OTLP-later track):

  CheckResponse(tools/list)  -> INJECT user_goal/expected_result into every tool's
                                inputSchema.properties (skip-if-exists); snapshot the
                                vendor surface. Return `mutated`.
  CheckRequest (tools/call)  -> CAPTURE the injected intent + real args + identity/
                                session from headers, then STRIP the injected keys and
                                forward clean params. Return `mutated` (or `pass`).

Everything downstream of capture (async queue, envelope, sink, consent, PII scrub)
is baton-proxy's Emitter — this class only parses the wire and calls enqueue_*.

NOT here (by design): any CheckResponse(tools/call) handling, the global FIFO,
result-body classifiers, the federated report_gap lane. Configure the gateway with
`tools/call: request` so the response hook is never invoked.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from collections.abc import Iterable

from baton_proxy.emitter import Emitter
from baton_proxy.extmcp import ext_mcp_pb2 as pb
from baton_proxy.extmcp import ext_mcp_pb2_grpc as pb_grpc
from baton_proxy.extmcp.intent import INJECT_KEYS, INJECT_PROPS

logger = logging.getLogger(__name__)

# Marks intent captured from the injected optional param (vs a real annotate call).
_INTENT_SOURCE = "injected_param"


def _decode_headers(headers: Iterable) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers:
        try:
            out[h.key] = h.value.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — header decode is best-effort
            out[h.key] = repr(h.value)
    return out


def _surface_hash(tools: list[dict]) -> str:
    blob = json.dumps(tools, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _pass_request() -> pb.McpRequestResult:
    r = pb.McpRequestResult()
    getattr(r, "pass").SetInParent()  # `pass` is a py keyword
    return r


def _pass_response() -> pb.McpResponseResult:
    r = pb.McpResponseResult()
    getattr(r, "pass").SetInParent()
    return r


class ExtMcpProcessor(pb_grpc.ExtMcpServicer):
    """Request-side capture servicer. Construct with a STARTED Emitter."""

    def __init__(
        self,
        emitter: Emitter,
        *,
        session_header: str = "mcp-session-id",
        identity_headers: tuple[str, ...] = ("x-gw-ims-user-id", "x-gw-ims-org-id"),
        fail_slow: float = 0.0,
        deny_all: bool = False,
    ) -> None:
        self._emitter = emitter
        self._session_header = session_header.lower()
        self._identity_headers = tuple(h.lower() for h in identity_headers)
        self._fail_slow = fail_slow
        self._deny_all = deny_all
        self._seen_surfaces: set[str] = set()
        self._seen_sessions: set[str] = set()

    # ---- tools/call: capture the injected intent + strip before the backend ----
    def CheckRequest(self, req: pb.McpRequest, context) -> pb.McpRequestResult:
        if self._fail_slow:
            time.sleep(self._fail_slow)
        if req.method != "tools/call":
            return _pass_request()

        headers = _decode_headers(req.headers)
        session_id = headers.get(self._session_header)
        params = json.loads(bytes(req.mcp_request)) if req.HasField("mcp_request") else {}
        tool = params.get("name")
        args = dict(params.get("arguments") or {})

        captured = {k: args.get(k) for k in INJECT_KEYS if k in args}
        stripped = [k for k in INJECT_KEYS if k in args]
        for k in stripped:
            args.pop(k, None)

        intent = captured.get("user_goal")
        expected = captured.get("expected_result")
        identity = {h: headers[h] for h in self._identity_headers if h in headers}
        runtime_meta = identity or None

        # First prospective intent of the session → a proactive annotation, so the
        # console has the session's intent bound. Per-call intent rides tool_call_start.
        if intent and session_id and session_id not in self._seen_sessions:
            self._seen_sessions.add(session_id)
            self._emitter.enqueue_annotation(
                signal_type=None,
                intent=intent,
                suggested_improvement=None,
                expected_outcome=expected,
                intent_source=_INTENT_SOURCE,
                tool_name=tool,
                session_id=session_id,
                runtime_meta=runtime_meta,
            )

        # The capture: identity+session from headers, intent from the injected
        # param, the real tool + CLEAN args (injected keys already popped).
        self._emitter.enqueue_tool_call_start(
            tool_name=tool or "",
            params=args,
            call_intent=intent,
            intent_source=_INTENT_SOURCE if intent else None,
            session_id=session_id,
            runtime_meta=runtime_meta,
        )

        if self._deny_all:
            r = pb.McpRequestResult()
            r.error.code = pb.AuthorizationError.PERMISSION_DENIED
            r.error.reason = "deny-all fault injection"
            return r

        if not stripped:
            return _pass_request()

        # STRIP: hand the backend clean params — it never sees the injected keys.
        params["arguments"] = args
        r = pb.McpRequestResult()
        r.mutated = json.dumps(params).encode("utf-8")
        r.metadata.update({"baton_captured": bool(captured)})  # CEL extMcp.<key> downstream
        return r

    # ---- tools/list: inject the intent params + snapshot the vendor surface ----
    def CheckResponse(self, resp: pb.McpResponse, context) -> pb.McpResponseResult:
        if self._fail_slow:
            time.sleep(self._fail_slow)
        if resp.method != "tools/list":
            # Not registered in prod config (tools/call: request). Defensive pass.
            return _pass_response()

        result = json.loads(bytes(resp.mcp_response)) if resp.mcp_response else {}
        tools = result.get("tools") or []

        # Snapshot the vendor-true surface (pre-injection), once per hash per process.
        h = _surface_hash(tools)
        if h not in self._seen_surfaces:
            self._seen_surfaces.add(h)
            self._emitter.enqueue_surface_snapshot(
                surface_hash=h,
                server_info=None,  # not available on a tools/list response
                capabilities=None,
                instructions=None,
                # Vendor-TRUE surface: deep-copy BEFORE injection so the snapshot
                # is the pre-injection tools even though we mutate `tools` below.
                tools=copy.deepcopy(tools),
                seam_augmentations={"injected": list(INJECT_KEYS)},
            )

        # Inject the optional params into every tool's inputSchema (skip-if-exists).
        for t in tools:
            schema = t.setdefault("inputSchema", {"type": "object"})
            props = schema.setdefault("properties", {})
            for k in INJECT_KEYS:
                if k not in props:
                    props[k] = dict(INJECT_PROPS[k])

        r = pb.McpResponseResult()
        r.mutated = json.dumps(result).encode("utf-8")
        return r

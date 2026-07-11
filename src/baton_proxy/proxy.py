"""MCP proxy — stdio subprocess wrap or HTTPS bridge.

From Claude's perspective the proxy *is* the MCP server; from the upstream
server's perspective the proxy is the client. This lets the proxy inject an
annotation tool + instructions into the handshake and emit friction events
without the vendor changing a single line of code.

Two upstream transports, one shared message layer:
  - **stdio** (`run_proxy`): wraps a stdio MCP server as a child process; two
    threads pump client<->server byte streams.
  - **HTTPS bridge** (`run_http_proxy`): stays stdio-facing to Claude but
    forwards each client message as an HTTP POST to a Streamable-HTTP upstream
    (spec 2025-03-26), streaming the JSON/SSE response back to stdout.

The interception / emission / correlation logic lives in `MessageProcessor`,
which never touches a pipe — so both transports capture identically and a
change to a signal type or injection rule moves them together. The processor
decides, per client message: intercept-and-synthesise (injected tool) or
forward; and per server message: correlate to a pending start, emit the
matching end/error, and inject into `initialize` / `tools/list` responses.

Errors anywhere in the proxy MUST NOT propagate to either pipe. Fail-open
means: if instrumentation breaks, MCP traffic still flows. For the HTTP bridge
that extends to the network — a timed-out / dropped / rejected POST yields a
synthetic error event + a JSON-RPC error to Claude, never a hang.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from baton_proxy._llm_text import (
    SIGNAL_TYPES,
    build_annotation_tool_description,
    build_instructions_suffix,
    build_intent_param_description,
)
from baton_proxy.config import Config
from baton_proxy.emitter import Emitter, utc_now_ms

logger = logging.getLogger("baton_proxy")

# Cap the in-flight tool-call tracking dict. Well above realistic MCP
# concurrency (1-10 parallel calls); the cap exists to bound memory if the
# upstream stops responding. When exceeded, oldest entries are evicted and
# a synthetic tool_call_error is emitted so the worker sees a well-formed
# start/error pair rather than a dangling start.
MAX_PENDING = 256
EVICTED_ERROR_TYPE = "proxy_pending_evicted"

# Baton-branded tool name for the injected annotation surface. v1 posture:
# the proxy is the gateway demo for customers evaluating Baton, so Baton
# brand visibility is the point. Vendors who eventually need white-label
# tool naming will get an opt-in switch — defer until first vendor asks.
# Underscore form matches the SDK's `derive_annotation_tool_name` rather
# than the dot form in SPEC §5.1.1; the SDK ships underscores today.
ANNOTATE_TOOL_NAME = "baton_annotate"

# Local-only "show me a friction report for this session" tool — only
# injected when the sink is purely local (file:// present, no http(s)://).
# The gate maps to product mode: local-sink demo = report tool present,
# vendor production (http sink) = no report tool, the vendor's own
# pipeline renders the report instead.
REPORT_TOOL_NAME = "baton_session_report"

# Per-tool injected intent param. Namespaced (vs a generic `intent`/`context`)
# because: (1) the fail-open path forwards messages UNTOUCHED, so the param can
# reach the vendor server — a generic name risks silently activating the
# vendor's own semantics, a namespaced one at worst draws a loud unknown-param
# error; (2) generic names collide with real tool params, punching holes in
# capture coverage exactly where the concept is loaded; (3) the reserved name
# makes strip-by-default safe when the registry is cold.
INTENT_PARAM_NAME = "baton_intent"

# Provenance value for intent captured via the injected param (vs a real
# annotate-tool call). Recorded on both the tool_call_start payload and the
# synthesised proactive annotation.
INTENT_SOURCE_PARAM = "injected_param"

# Cap the tools/list request-id correlation dict (see MessageProcessor).
# Same rationale as MAX_PENDING, sized smaller — clients re-list occasionally,
# they don't fan out hundreds of concurrent list requests.
MAX_PENDING_TOOLLISTS = 64


def _surface_hash(surface: Mapping[str, Any]) -> str:
    """Content hash of the vendor-true surface, canonical-JSON keyed.

    This is the identity changes and recipes are authored against
    (``base_surface_hash`` in the change spec) — it must be stable across
    proxy restarts and key ordering, hence sorted keys + compact separators.
    """
    canonical = json.dumps(surface, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_injected_tool(tool_name: str) -> dict[str, Any]:
    return {
        "name": tool_name,
        "description": build_annotation_tool_description(),
        "inputSchema": {
            "type": "object",
            "properties": {
                "signal_type": {
                    "type": "string",
                    "enum": list(SIGNAL_TYPES),
                },
                "intent": {"type": "string"},
                "expected_outcome": {"type": "string"},
                "workflow": {"type": "string"},
                "suggested_improvement": {"type": "string"},
                "context": {"type": "object"},
            },
            # intent is the only required field — proactive annotations
            # (filed BEFORE a tool call to capture the user's goal) carry
            # intent alone. signal_type + suggested_improvement are reactive-
            # only, set AFTER a tool call returned an unhelpful result.
            # Treating signal_type as required forces agents to invent a
            # signal_type='other' for proactives, polluting friction counts
            # downstream.
            "required": ["intent"],
        },
    }


def _build_report_tool() -> dict[str, Any]:
    return {
        "name": REPORT_TOOL_NAME,
        "description": (
            "Show a friction report for the current session — a vendor-shareable "
            "summary of tool calls, errors, and friction signals captured by the "
            "proxy. Use this when the user asks 'show me what went wrong', 'what "
            "would a support ticket for this look like', or wants to see a "
            "rollup of friction in this session. Output is ready-to-paste "
            "markdown."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    }


@dataclass(frozen=True)
class _Injection:
    """Resolved per-process injection state.

    Always carries the annotate tool. Carries the session-report tool too
    when the sink is purely local (so the customer can see the friction
    report surface). HTTP sinks indicate production mode where the
    vendor's own pipeline renders the report, not the proxy — so the
    report tool is suppressed there.
    """

    tools: list[dict[str, Any]]
    instructions_suffix: str
    sink_path: str | None  # path of the file sink to read for the report, if any
    # Intent-param injection mode: "optional" | "required" | "off". Defaulted
    # so tests that build _Injection directly keep their existing shape.
    intent_param_mode: str = "optional"

    @property
    def names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    @classmethod
    def create(
        cls,
        event_sink_url: str | None,
        *,
        tenant_type: str = "vendor",
        intent_param_mode: str = "optional",
    ) -> _Injection:
        from baton_proxy.report import find_file_sink_path, should_inject_report_tool

        tools = [_build_injected_tool(ANNOTATE_TOOL_NAME)]
        sink_path: str | None = None
        if should_inject_report_tool(event_sink_url, tenant_type=tenant_type):
            tools.append(_build_report_tool())
            sink_path = find_file_sink_path(event_sink_url)
        return cls(
            tools=tools,
            instructions_suffix=build_instructions_suffix(ANNOTATE_TOOL_NAME),
            sink_path=sink_path,
            intent_param_mode=intent_param_mode,
        )


class _PendingCall:
    """Tracking state for an in-flight MCP call (start emitted, awaiting end).

    ``kind`` is the discriminator: "tool" | "resource_read" | "resource_list" |
    "prompt_get" | "prompt_list". ``subject`` is the uri/name for keyed kinds,
    empty string for list kinds.
    """

    __slots__ = ("kind", "subject", "started_ms", "runtime_meta")

    def __init__(
        self,
        kind: str,
        subject: str,
        started_ms: int,
        runtime_meta: dict[str, Any] | None,
    ) -> None:
        self.kind = kind
        self.subject = subject
        self.started_ms = started_ms
        self.runtime_meta = runtime_meta


def _emit_call_end(
    emitter: Emitter,
    call: _PendingCall,
    result: Any,
    duration_ms: int,
) -> None:
    """Dispatch the correct *_end event for any pending-call kind."""
    if call.kind == "tool":
        emitter.enqueue_tool_call_end(
            tool_name=call.subject,
            result=result,
            duration_ms=duration_ms,
            runtime_meta=call.runtime_meta,
        )
    elif call.kind == "resource_read":
        emitter.enqueue_resource_read_end(
            uri=call.subject, duration_ms=duration_ms, runtime_meta=call.runtime_meta
        )
    elif call.kind == "resource_list":
        count = len((result or {}).get("resources", []))
        emitter.enqueue_resource_list_end(
            count=count, duration_ms=duration_ms, runtime_meta=call.runtime_meta
        )
    elif call.kind == "prompt_get":
        emitter.enqueue_prompt_get_end(
            name=call.subject, duration_ms=duration_ms, runtime_meta=call.runtime_meta
        )
    elif call.kind == "prompt_list":
        count = len((result or {}).get("prompts", []))
        emitter.enqueue_prompt_list_end(
            count=count, duration_ms=duration_ms, runtime_meta=call.runtime_meta
        )


def _emit_call_error(
    emitter: Emitter,
    call: _PendingCall,
    error_type: str,
    error_body: str,
    duration_ms: int,
) -> None:
    """Dispatch the correct *_error event for any pending-call kind."""
    if call.kind == "tool":
        emitter.enqueue_tool_call_error(
            tool_name=call.subject,
            error_type=error_type,
            error_body=error_body,
            duration_ms=duration_ms,
            runtime_meta=call.runtime_meta,
        )
    elif call.kind == "resource_read":
        emitter.enqueue_resource_read_error(
            uri=call.subject,
            error_type=error_type,
            error_body=error_body,
            duration_ms=duration_ms,
            runtime_meta=call.runtime_meta,
        )
    elif call.kind == "resource_list":
        emitter.enqueue_resource_list_error(
            error_type=error_type,
            error_body=error_body,
            duration_ms=duration_ms,
            runtime_meta=call.runtime_meta,
        )
    elif call.kind == "prompt_get":
        emitter.enqueue_prompt_get_error(
            name=call.subject,
            error_type=error_type,
            error_body=error_body,
            duration_ms=duration_ms,
            runtime_meta=call.runtime_meta,
        )
    elif call.kind == "prompt_list":
        emitter.enqueue_prompt_list_error(
            error_type=error_type,
            error_body=error_body,
            duration_ms=duration_ms,
            runtime_meta=call.runtime_meta,
        )


def _evict_overflow(pending: OrderedDict[Any, _PendingCall], emitter: Emitter) -> None:
    """Evict oldest pending entries down to MAX_PENDING; caller holds pending_lock.

    Each eviction emits a synthetic error event so the wire stream doesn't
    carry a dangling start with no end/error pair.
    """
    while len(pending) > MAX_PENDING:
        _evicted_id, evicted = pending.popitem(last=False)
        try:
            _emit_call_error(
                emitter,
                evicted,
                EVICTED_ERROR_TYPE,
                "proxy pending dict overflowed without upstream response",
                max(0, utc_now_ms() - evicted.started_ms),
            )
        except Exception:
            logger.exception("baton-proxy: enqueue evicted error failed")


def _inject_intent_param_into_tool(tool: Any, mode: str) -> str | None:
    """Add the intent param to one tool's inputSchema; return its disposition.

    Returns "injected" (param added), "native" (the tool already had a
    param with our name — left untouched, per skip-if-exists), or None
    (not a recognisable tool object; nothing recorded). Mutates the tool
    in place. ``mode`` is "optional" or "required" — "off" is gated by
    the caller.
    """
    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
        return None
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
        tool["inputSchema"] = schema
    props = schema.get("properties")
    if not isinstance(props, dict):
        props = {}
        schema["properties"] = props
    if INTENT_PARAM_NAME in props:
        return "native"
    props[INTENT_PARAM_NAME] = {
        "type": "string",
        "description": build_intent_param_description(),
    }
    if mode == "required":
        required = schema.get("required")
        if isinstance(required, list):
            if INTENT_PARAM_NAME not in required:
                required.append(INTENT_PARAM_NAME)
        else:
            schema["required"] = [INTENT_PARAM_NAME]
    return "injected"


def _inject_into_response(msg: dict[str, Any], injection: _Injection) -> dict[str, Any]:
    """Inject into responses to `initialize` and `tools/list`.

    Returns the (possibly-mutated) message. Errors are swallowed — a malformed
    response that looks like a result we want to inject into MUST still be
    forwarded so the upstream server's behavior isn't masked by an injection
    bug.
    """
    try:
        if "result" not in msg or not isinstance(msg["result"], dict):
            return msg
        result = msg["result"]

        if "protocolVersion" in result or "serverInfo" in result:
            original = result.get("instructions", "")
            if not isinstance(original, str):
                original = ""
            result["instructions"] = original + injection.instructions_suffix

        if "tools" in result and isinstance(result["tools"], list):
            result["tools"].extend(injection.tools)
    except Exception:
        logger.exception("baton-proxy: injection failed, forwarding response unmodified")
    return msg


def _handle_injected_call(
    req: dict[str, Any],
    *,
    injection: _Injection,
    session_id: str,
    emitter: Emitter,
) -> dict[str, Any]:
    """Synthesise a response for whichever injected tool was called.

    The annotation event itself is enqueued by the caller before this is
    invoked; this only builds the JSON-RPC envelope to send back. Dispatch
    is by tool name — annotate vs session_report — with a defensive
    fallback that never raises (a bug here MUST NOT break MCP traffic).
    """
    params = req.get("params") or {}
    name = params.get("name") if isinstance(params, dict) else None
    if name == REPORT_TOOL_NAME:
        return _build_report_response(
            req, injection=injection, session_id=session_id, emitter=emitter
        )
    # Default / ANNOTATE_TOOL_NAME path.
    args = params.get("arguments") if isinstance(params, dict) else None
    args = args or {}
    signal = args.get("signal_type")
    # Proactives carry intent alone — absence of signal_type is the
    # semantic marker, not "unknown". Make the confirmation reflect that
    # so downstream telemetry (Console event payloads, log lines) doesn't
    # have to second-guess what mode the annotation was filed in.
    if signal:
        confirmation = f"baton_annotate recorded signal_type={signal}"
    else:
        confirmation = "baton_annotate recorded proactive intent"
    return {
        "jsonrpc": "2.0",
        "id": req.get("id"),
        "result": {"content": [{"type": "text", "text": confirmation}]},
    }


def _build_report_response(
    req: dict[str, Any],
    *,
    injection: _Injection,
    session_id: str,
    emitter: Emitter,
) -> dict[str, Any]:
    from baton_proxy.report import synthesize

    if injection.sink_path is None:
        # Shouldn't happen — report tool is only injected when there IS a
        # file sink — but defend defensively rather than raise into the
        # MCP wire.
        text = "Friction report unavailable — no local file sink configured."
    else:
        try:
            text = synthesize(
                injection.sink_path,
                session_id,
                scrub_counts=emitter.scrub_counts(),
            )
        except Exception:
            logger.exception("baton-proxy: report synthesis failed")
            text = "Friction report synthesis failed — see proxy log for details."
    return {
        "jsonrpc": "2.0",
        "id": req.get("id"),
        "result": {"content": [{"type": "text", "text": text}]},
    }


def _write_line(stream: Any, payload: dict[str, Any] | str) -> None:
    """Newline-terminated JSON-RPC write. Both pipes use the same framing."""
    line = payload if isinstance(payload, str) else json.dumps(payload)
    stream.write(line + "\n")
    stream.flush()


# stdout is the single MCP wire back to the client. The stdio transport has TWO
# pump threads writing to it — the client pump writes synthesised injected-tool
# responses, the server pump writes upstream responses — so their lines can
# interleave without a lock. Route every sys.stdout write through this lock. The
# HTTP path is single-threaded, so the lock is uncontended there.
_stdout_lock = threading.Lock()


def _write_stdout(payload: dict[str, Any] | str) -> None:
    """Write one framed line to sys.stdout under the shared stdout lock."""
    with _stdout_lock:
        _write_line(sys.stdout, payload)


def _write_client_error(req_id: Any, code: int, message: str) -> None:
    """Hand the client a JSON-RPC error for a request id, so it never hangs.

    No-op for notifications (``req_id is None``) — those get no response by
    definition. Swallows write errors (fail-open: a broken stdout must not
    take down the loop).
    """
    if req_id is None:
        return
    try:
        _write_stdout({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})
    except Exception:
        logger.exception("baton-proxy: forward error to client failed")


@dataclass
class _ClientAction:
    """What a transport should do with one client->server message.

    Exactly one field is set. ``respond`` means the proxy owns this message
    (an injected tool call) — synthesise the response back to the client and
    do NOT forward upstream. ``forward`` is the message to hand to the upstream
    server (unchanged from the input; returned so the transport's job is
    uniform whichever branch fired).
    """

    respond: dict[str, Any] | None = None
    forward: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        # Enforce the "exactly one set" invariant structurally, so a future
        # intercept branch that forgets to set forward (or sets both) fails loudly
        # at construction rather than silently forwarding the wrong object. Both
        # call sites are inside handle_client_message, which the transports wrap
        # in try/except — so a violation degrades to a logged error + client
        # error, never a dangling request.
        if (self.respond is None) == (self.forward is None):
            raise ValueError("_ClientAction requires exactly one of respond/forward")


class MessageProcessor:
    """Transport-independent MCP message layer.

    Holds the per-process interception + emission + correlation state and
    exposes two pure-ish handlers that never touch a pipe. Both the stdio and
    HTTP transports feed it parsed JSON-RPC messages, so the logic that decides
    what to intercept, what to emit, and how to pair responses lives in exactly
    ONE place regardless of how bytes reach it. Add a signal_type or change an
    injection rule here and both transports move together.

    The handlers own the same fail-open contract as the old pumps: any
    instrumentation error is logged and swallowed, never raised into a
    transport, so MCP traffic keeps flowing even if capture breaks.
    """

    def __init__(self, emitter: Emitter, injection: _Injection, session_id: str) -> None:
        self._emitter = emitter
        self._injection = injection
        self._session_id = session_id
        self._pending: OrderedDict[Any, _PendingCall] = OrderedDict()
        self._pending_lock = threading.Lock()
        # Intent-param registry: tool_name -> "injected" | "native", UPSERTED
        # per tools/list response (never cleared — tools/list paginates via
        # cursor, Desktop lazily re-lists, listChanged refires; a clear-per-
        # response would forget dispositions for tools on other pages).
        # Written on the server-message path, read on the client-message path —
        # different pump threads on the stdio transport, hence the lock.
        self._param_registry: dict[str, str] = {}
        self._registry_lock = threading.Lock()
        # True once ANY proactive annotation has been emitted this session —
        # from a real annotate-tool call or synthesised from the first param
        # intent. Gates the param->annotation synthesis so a session gets at
        # most one param-sourced proactive (each proactive opens a new turn in
        # the console; one per tool call would splinter the session view).
        # Only touched on the client-message path, which is single-threaded
        # on both transports — no lock needed.
        self._proactive_emitted = False
        # --- Surface-snapshot state (design-note: server_surface_and_change_spec) ---
        # initialize result captured PRE-suffix — the vendor-true instructions,
        # serverInfo and capabilities that belong in the snapshot. Written on
        # the server-message path only.
        self._server_meta: dict[str, Any] | None = None
        # Request ids of first-page tools/list requests (no cursor param) —
        # only their responses are snapshot candidates; a cursor-continuation
        # page is a fragment, never a surface. Insertion-ordered dict so
        # overflow evicts oldest. Written on the client path, popped on the
        # server path — different pump threads on stdio, hence the lock.
        self._toollist_first_page_ids: dict[Any, None] = {}
        # Hashes already emitted this process — a Desktop-style lazy re-list
        # of an unchanged surface must not re-emit. A CHANGED surface (e.g.
        # listChanged refire after an upstream mutation) hashes fresh and
        # emits again by design.
        self._emitted_surface_hashes: set[str] = set()
        self._surface_lock = threading.Lock()

    def _track(self, req_id: Any, call: _PendingCall) -> None:
        """Register an in-flight call + evict overflow, under the pending lock."""
        with self._pending_lock:
            self._pending[req_id] = call
            _evict_overflow(self._pending, self._emitter)

    def handle_client_message(self, req: dict[str, Any]) -> _ClientAction:
        """Intercept/emit for a client->server message; return the transport's action."""
        method = req.get("method")

        if method == "tools/list":
            # Remember first-page requests (no cursor) so the server path can
            # tell a snapshot candidate from a pagination fragment. Forwarded
            # unchanged either way; fail-open on any shape surprise.
            try:
                params = req.get("params") or {}
                cursor = params.get("cursor") if isinstance(params, dict) else None
                req_id = req.get("id")
                if req_id is not None and not cursor:
                    with self._surface_lock:
                        self._toollist_first_page_ids[req_id] = None
                        while len(self._toollist_first_page_ids) > MAX_PENDING_TOOLLISTS:
                            oldest = next(iter(self._toollist_first_page_ids))
                            del self._toollist_first_page_ids[oldest]
            except Exception:
                logger.exception("baton-proxy: tools/list tracking failed")
            return _ClientAction(forward=req)

        if method == "tools/call":
            params = req.get("params", {}) or {}
            tool_name = params.get("name")
            if tool_name in self._injection.names:
                # The proxy owns this tool — don't forward. For annotate
                # calls, emit the annotation event before synthesising the
                # response; that's the whole point of intercepting it.
                if tool_name == ANNOTATE_TOOL_NAME:
                    args = params.get("arguments", {}) or {}
                    ann_meta = (
                        params.get("_meta") if isinstance(params.get("_meta"), dict) else None
                    )
                    ctx = args.get("context") if isinstance(args.get("context"), dict) else None
                    try:
                        self._emitter.enqueue_annotation(
                            signal_type=args.get("signal_type"),
                            intent=args.get("intent"),
                            suggested_improvement=args.get("suggested_improvement"),
                            expected_outcome=args.get("expected_outcome"),
                            workflow=args.get("workflow"),
                            context=ctx,
                            runtime_meta=ann_meta,
                        )
                    except Exception:
                        logger.exception("baton-proxy: enqueue annotation failed")
                    else:
                        # A real proactive (intent, no signal_type) claims the
                        # session's turn-opener slot — the param->annotation
                        # synthesis below must not double-open it.
                        if args.get("intent") and not args.get("signal_type"):
                            self._proactive_emitted = True
                try:
                    response = _handle_injected_call(
                        req,
                        injection=self._injection,
                        session_id=self._session_id,
                        emitter=self._emitter,
                    )
                except Exception:
                    logger.exception("baton-proxy: synthesising injected response failed")
                    # Fail-open: a synthesis bug must still return *something*
                    # to the client rather than dangle the request id.
                    response = {
                        "jsonrpc": "2.0",
                        "id": req.get("id"),
                        "result": {
                            "content": [{"type": "text", "text": "baton_annotate recorded"}]
                        },
                    }
                return _ClientAction(respond=response)

            # Real tool call — emit start FIRST, then track for end/error.
            # Order matters: if enqueue throws we MUST NOT have a pending
            # entry, otherwise the eventual response would emit an orphan
            # tool_call_end the worker can't pair with a start. (Forwarding
            # to the upstream happens after this returns, so there's no risk
            # of the response arriving before we add to pending on success.)
            req_id = req.get("id")
            # TODO: MCP also permits a top-level `_meta` on the request envelope;
            # we currently only capture `params._meta`. Revisit if a vendor runtime
            # surfaces correlation ids at the request level instead.
            runtime_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            safe_tool_name = str(tool_name) if tool_name else ""

            # Strip the injected intent param BEFORE the start event is built,
            # so captured params == the vendor-visible arguments exactly.
            call_intent = self._extract_intent_param(safe_tool_name, params)

            # The session's FIRST param intent also becomes a proactive
            # annotation — enqueued BEFORE the tool_call_start so sequence-
            # order correlation holds downstream ("proactive before the tool
            # calls it covers"). Later param intents ride only the start
            # events; per-call proactives would open one console turn per
            # tool call.
            if call_intent is not None and not self._proactive_emitted:
                try:
                    self._emitter.enqueue_annotation(
                        signal_type=None,
                        intent=call_intent,
                        suggested_improvement=None,
                        intent_source=INTENT_SOURCE_PARAM,
                        tool_name=safe_tool_name,
                    )
                except Exception:
                    logger.exception("baton-proxy: enqueue param-intent annotation failed")
                else:
                    self._proactive_emitted = True

            try:
                self._emitter.enqueue_tool_call_start(
                    tool_name=safe_tool_name,
                    params=params.get("arguments"),
                    call_intent=call_intent,
                    intent_source=INTENT_SOURCE_PARAM if call_intent is not None else None,
                    runtime_meta=runtime_meta,
                )
            except Exception:
                logger.exception("baton-proxy: enqueue tool_call_start failed")
            else:
                self._track(
                    req_id,
                    _PendingCall(
                        kind="tool",
                        subject=safe_tool_name,
                        started_ms=utc_now_ms(),
                        runtime_meta=runtime_meta,
                    ),
                )

        elif method == "resources/read":
            params = req.get("params", {}) or {}
            uri = str(params.get("uri") or "")
            runtime_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            req_id = req.get("id")
            try:
                self._emitter.enqueue_resource_read_start(
                    uri=uri,
                    params={k: v for k, v in params.items() if k != "_meta"} or None,
                    runtime_meta=runtime_meta,
                )
            except Exception:
                logger.exception("baton-proxy: enqueue resource_read_start failed")
            else:
                self._track(
                    req_id,
                    _PendingCall(
                        kind="resource_read",
                        subject=uri,
                        started_ms=utc_now_ms(),
                        runtime_meta=runtime_meta,
                    ),
                )

        elif method == "resources/list":
            params = req.get("params", {}) or {}
            runtime_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            req_id = req.get("id")
            try:
                self._emitter.enqueue_resource_list_start(runtime_meta=runtime_meta)
            except Exception:
                logger.exception("baton-proxy: enqueue resource_list_start failed")
            else:
                self._track(
                    req_id,
                    _PendingCall(
                        kind="resource_list",
                        subject="",
                        started_ms=utc_now_ms(),
                        runtime_meta=runtime_meta,
                    ),
                )

        elif method == "prompts/get":
            params = req.get("params", {}) or {}
            name = str(params.get("name") or "")
            runtime_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            req_id = req.get("id")
            try:
                self._emitter.enqueue_prompt_get_start(
                    name=name,
                    params=params.get("arguments"),
                    runtime_meta=runtime_meta,
                )
            except Exception:
                logger.exception("baton-proxy: enqueue prompt_get_start failed")
            else:
                self._track(
                    req_id,
                    _PendingCall(
                        kind="prompt_get",
                        subject=name,
                        started_ms=utc_now_ms(),
                        runtime_meta=runtime_meta,
                    ),
                )

        elif method == "prompts/list":
            params = req.get("params", {}) or {}
            runtime_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            req_id = req.get("id")
            try:
                self._emitter.enqueue_prompt_list_start(runtime_meta=runtime_meta)
            except Exception:
                logger.exception("baton-proxy: enqueue prompt_list_start failed")
            else:
                self._track(
                    req_id,
                    _PendingCall(
                        kind="prompt_list",
                        subject="",
                        started_ms=utc_now_ms(),
                        runtime_meta=runtime_meta,
                    ),
                )

        return _ClientAction(forward=req)

    def _extract_intent_param(self, tool_name: str, params: dict[str, Any]) -> str | None:
        """Strip the injected intent param from a tools/call; return its value.

        Mutates ``params["arguments"]`` in place (the same object the transport
        forwards, so the upstream never sees the param). Registry dispositions:
        "injected" -> strip + capture; "native" -> the param belongs to the
        vendor's tool, forward untouched and capture nothing; unknown (cold
        registry — proxy respawned mid-session, or a client calling without a
        tools/list through us) -> strip + capture with a log line, safe only
        because the name is namespaced. Never raises; on any error the call
        proceeds with whatever state it has (fail-open).
        """
        if self._injection.intent_param_mode == "off":
            return None
        try:
            args = params.get("arguments")
            if not isinstance(args, dict) or INTENT_PARAM_NAME not in args:
                return None
            with self._registry_lock:
                disposition = self._param_registry.get(tool_name)
            if disposition == "native":
                return None
            if disposition is None:
                logger.warning(
                    "baton-proxy: stripping %s from unlisted tool %r (cold registry)",
                    INTENT_PARAM_NAME,
                    tool_name,
                )
            raw = args.pop(INTENT_PARAM_NAME, None)
            if isinstance(raw, str) and raw.strip():
                return raw
            return None
        except Exception:
            logger.exception("baton-proxy: intent param extraction failed")
            return None

    def handle_server_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Correlate/emit for a server->client message; return the message to write out."""
        # Correlate this response to a pending call (by id) and emit the
        # matching end/error event.
        msg_id = msg.get("id")
        if msg_id is not None:
            with self._pending_lock:
                call = self._pending.pop(msg_id, None)
            if call is not None:
                try:
                    duration_ms = max(0, utc_now_ms() - call.started_ms)
                    if "error" in msg:
                        err = msg["error"] or {}
                        _emit_call_error(
                            self._emitter,
                            call,
                            str(err.get("code", "")) or "unknown",
                            str(err.get("message", "")),
                            duration_ms,
                        )
                    else:
                        _emit_call_end(self._emitter, call, msg.get("result"), duration_ms)
                except Exception:
                    logger.exception("baton-proxy: enqueue end/error failed")

        # Surface capture runs BEFORE both injections below, so the snapshot
        # records the vendor-true surface (no baton_* tools, no intent param,
        # pre-suffix instructions).
        self._capture_surface(msg)

        self._inject_intent_params(msg)
        return _inject_into_response(msg, self._injection)

    def _capture_surface(self, msg: dict[str, Any]) -> None:
        """Snapshot the upstream surface from initialize + tools/list responses.

        initialize responses stash serverInfo/capabilities/instructions;
        a COMPLETE first-page tools/list response (tracked id, no nextCursor)
        emits one ``surface_snapshot`` event, deduped on the surface hash for
        the process lifetime. Multi-page surfaces are skipped in v0 — a
        partial page must never masquerade as the surface. Fail-open: any
        error is logged and the message flows on untouched.
        """
        try:
            msg_id = msg.get("id")
            result = msg.get("result")
            if not isinstance(result, dict):
                # Error (or shapeless) response — forget any tracked list id
                # so the dict doesn't accumulate dead entries.
                if msg_id is not None:
                    with self._surface_lock:
                        self._toollist_first_page_ids.pop(msg_id, None)
                return

            if "protocolVersion" in result or "serverInfo" in result:
                instructions = result.get("instructions")
                self._server_meta = {
                    "server_info": copy.deepcopy(result.get("serverInfo")),
                    "capabilities": copy.deepcopy(result.get("capabilities")),
                    "instructions": instructions if isinstance(instructions, str) else None,
                }
                return

            tools = result.get("tools")
            if not isinstance(tools, list):
                return
            with self._surface_lock:
                first_page = msg_id is not None and (
                    self._toollist_first_page_ids.pop(msg_id, "absent") is None
                )
            if not first_page or result.get("nextCursor"):
                return

            meta = self._server_meta or {}
            surface: dict[str, Any] = {
                # server_info/capabilities/instructions are None when the
                # proxy never saw initialize (respawned mid-session) — the
                # snapshot is still worth having; the hash reflects it.
                "server_info": meta.get("server_info"),
                "capabilities": meta.get("capabilities"),
                "instructions": meta.get("instructions"),
                "tools": copy.deepcopy(tools),
            }
            digest = _surface_hash(surface)
            with self._surface_lock:
                if digest in self._emitted_surface_hashes:
                    return
                self._emitted_surface_hashes.add(digest)

            mode = self._injection.intent_param_mode
            self._emitter.enqueue_surface_snapshot(
                surface_hash=digest,
                server_info=surface["server_info"],
                capabilities=surface["capabilities"],
                instructions=surface["instructions"],
                tools=surface["tools"],
                seam_augmentations={
                    "injected_tools": sorted(self._injection.names),
                    "intent_param": (
                        {"name": INTENT_PARAM_NAME, "mode": mode} if mode != "off" else None
                    ),
                    "instructions_suffix": bool(self._injection.instructions_suffix),
                },
            )
        except Exception:
            logger.exception("baton-proxy: surface snapshot capture failed")

    def _inject_intent_params(self, msg: dict[str, Any]) -> None:
        """Add the intent param to every upstream tool in a tools/list response.

        Runs BEFORE ``_inject_into_response`` appends the proxy's own tools, so
        those never grow the param. Records each tool's disposition in the
        registry (upsert — see __init__). Fail-open: any error is logged and
        the response is forwarded as-is.
        """
        if self._injection.intent_param_mode == "off":
            return
        try:
            result = msg.get("result")
            if not isinstance(result, dict):
                return
            tools = result.get("tools")
            if not isinstance(tools, list):
                return
            dispositions: dict[str, str] = {}
            for tool in tools:
                # Defensive: a pathological upstream could name a tool after
                # one of ours; those calls are intercepted pre-upstream, so
                # param-injecting them would only confuse the schema.
                if isinstance(tool, dict) and tool.get("name") in self._injection.names:
                    continue
                disposition = _inject_intent_param_into_tool(
                    tool, self._injection.intent_param_mode
                )
                if disposition is not None:
                    dispositions[tool["name"]] = disposition
            if dispositions:
                with self._registry_lock:
                    self._param_registry.update(dispositions)
        except Exception:
            logger.exception("baton-proxy: intent param injection failed")

    def synthesize_pending_error(self, req_id: Any, error_type: str, error_body: str) -> None:
        """Emit a synthetic *_error for a tracked call the upstream never answered.

        Used by the HTTP transport's fail-open path: when a POST times out or the
        connection drops mid-request, the pending start would otherwise dangle
        with no end/error pair. Pop it and emit the matching error so the wire
        stream stays well-formed. No-op if the id isn't tracked.
        """
        with self._pending_lock:
            call = self._pending.pop(req_id, None)
        if call is None:
            return
        try:
            _emit_call_error(
                self._emitter,
                call,
                error_type,
                error_body,
                max(0, utc_now_ms() - call.started_ms),
            )
        except Exception:
            logger.exception("baton-proxy: enqueue synthetic error failed")

    def drain_pending(self, error_type: str, error_body: str) -> None:
        """Emit a synthetic *_error for every still-pending call, then clear.

        Called on shutdown so an upstream that died mid-call doesn't leave
        dangling *_start events with no matching end/error pair. Both transports
        call this on their teardown path.
        """
        with self._pending_lock:
            outstanding = list(self._pending.values())
            self._pending.clear()
        for call in outstanding:
            try:
                _emit_call_error(
                    self._emitter,
                    call,
                    error_type,
                    error_body,
                    max(0, utc_now_ms() - call.started_ms),
                )
            except Exception:
                logger.exception("baton-proxy: enqueue drain error failed")


def _pump_client_to_server(child_stdin: Any, processor: MessageProcessor) -> None:
    """stdio client->server I/O loop. Message logic lives in the processor."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            req = None
        # Not a single JSON-RPC object: non-JSON, or valid JSON that is a bare
        # array/scalar (json.loads accepts those without raising). Neither is a
        # message we process — forward the raw line to the upstream (best-effort
        # passthrough, keeps any JSON-RPC batch intact). Guarding here makes
        # every later req.get(...) provably safe.
        if not isinstance(req, dict):
            try:
                child_stdin.write(line + "\n")
                child_stdin.flush()
            except Exception:
                logger.exception("baton-proxy: forward to upstream failed")
            continue

        # Fail-open: a bug processing one message must not kill this pump thread
        # (which would silently stop all capture). Log, error the client so it
        # doesn't hang, move on.
        try:
            action = processor.handle_client_message(req)
        except Exception:
            logger.exception("baton-proxy: handle_client_message failed")
            _write_client_error(
                req.get("id"), -32603, "baton-proxy: internal error processing the request"
            )
            continue

        if action.respond is not None:
            try:
                _write_stdout(action.respond)
            except Exception:
                logger.exception("baton-proxy: forward to client failed")
            continue

        try:
            child_stdin.write(json.dumps(action.forward) + "\n")
            child_stdin.flush()
        except Exception:
            logger.exception("baton-proxy: forward to upstream failed")


def _pump_server_to_client(child_stdout: Any, processor: MessageProcessor) -> None:
    """stdio server->client I/O loop. Message logic lives in the processor."""
    for line in child_stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            try:
                _write_stdout(line)
            except Exception:
                logger.exception("baton-proxy: forward to client failed")
            continue

        out = processor.handle_server_message(msg)
        try:
            _write_stdout(out)
        except Exception:
            logger.exception("baton-proxy: forward to client failed")


def _child_env(parent_env: Mapping[str, str]) -> dict[str, str]:
    """Return parent env with all BATON_* keys filtered out (least privilege)."""
    return {k: v for k, v in parent_env.items() if not k.startswith("BATON_")}


def _configure_logging(log_file: str | None) -> None:
    """Route proxy logs to stderr by default, optionally tee to a file.

    NEVER log to stdout — stdout is the MCP wire.
    """
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    if log_file:
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as e:
            logger.warning("baton-proxy: cannot open log file %r: %s", log_file, e)


def _bootstrap() -> tuple[Config, _Injection, Emitter, MessageProcessor]:
    """Shared per-transport startup.

    Reads config, configures logging, resolves the injection set, starts the
    emitter, and builds the processor that ties them together — the identical
    preamble both transports need. Callers do the transport-specific work (spawn
    a subprocess / open an HTTP client) and log their own startup line.
    """
    config = Config.from_env()
    _configure_logging(config.log_file)
    injection = _Injection.create(
        config.event_sink,
        tenant_type=config.tenant_type,
        intent_param_mode=config.intent_param_mode,
    )
    emitter = Emitter(config)
    emitter.start()
    processor = MessageProcessor(emitter, injection, config.session_id)
    return config, injection, emitter, processor


def run_proxy(argv: list[str]) -> int:
    """Spawn the upstream stdio MCP server and pump bidirectionally.

    `argv` is the upstream command (e.g. `["npx", "@vendor/mcp-server"]`).
    Returns the upstream's exit code.
    """
    config, injection, emitter, processor = _bootstrap()
    logger.info(
        "baton-proxy starting (session=%s, emission=%s, tools=%s, intent_param=%s, upstream=%s)",
        config.session_id,
        "on" if config.emission_enabled else "off",
        sorted(injection.names),
        injection.intent_param_mode,
        " ".join(argv),
    )

    try:
        child = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
            env=_child_env(os.environ),
        )
    except (FileNotFoundError, PermissionError) as e:
        logger.error("baton-proxy: cannot spawn upstream %r: %s", argv, e)
        emitter.stop()
        return 127

    t_in = threading.Thread(
        target=_pump_client_to_server,
        args=(child.stdin, processor),
        name="baton-proxy-in",
        daemon=True,
    )
    t_out = threading.Thread(
        target=_pump_server_to_client,
        args=(child.stdout, processor),
        name="baton-proxy-out",
        daemon=True,
    )
    t_in.start()
    t_out.start()

    rc = child.wait()
    # t_out terminates naturally once child's stdout closes (which happens
    # on child exit). Give it a moment to drain final responses + their
    # tool_call_end events before we stop the emitter. t_in is blocked on
    # sys.stdin and can't be unblocked from Python; daemon=True takes care
    # of it at process exit.
    t_out.join(timeout=2.0)
    # The upstream is gone; any calls still in flight will never get a response.
    # Drain them so each *_start has a matching error rather than dangling.
    processor.drain_pending(
        "proxy_upstream_closed", "upstream connection closed with the request in flight"
    )
    emitter.stop()
    logger.info("baton-proxy exiting (upstream rc=%d)", rc)
    return rc


def run_http_proxy(url: str) -> int:
    """Bridge a stdio-facing Claude client to an HTTPS Streamable-HTTP upstream.

    The proxy stays stdio-facing to Claude (reads JSON-RPC from stdin, writes to
    stdout) but forwards each client message as an HTTP POST to ``url`` per the
    MCP Streamable HTTP transport (spec 2025-03-26), and streams the response
    (JSON body or SSE) back to stdout. The message layer (injection, emission,
    correlation) is shared with the stdio path via ``MessageProcessor``.

    v0 scope (logged at startup): the single POST-driven loop handles the
    client-initiated request/response case only. It does NOT open the standing
    GET SSE channel, so server-*initiated* messages (sampling, elicitation,
    server notifications) are not captured; and requests are serialised (no
    duplex pipelining). Both are irrelevant to the request/response tool-call
    MCP servers this bridge targets first.

    Returns 0 on clean shutdown (stdin EOF), 1 if the upstream is unreachable
    at startup.
    """
    from baton_proxy.transport_http import StreamableHttpClient

    config, injection, emitter, processor = _bootstrap()
    auth_token = os.environ.get("BATON_UPSTREAM_AUTH_TOKEN")
    logger.info(
        "baton-proxy starting (session=%s, emission=%s, tools=%s, intent_param=%s, upstream=%s, transport=http, auth=%s)",
        config.session_id,
        "on" if config.emission_enabled else "off",
        sorted(injection.names),
        injection.intent_param_mode,
        url,
        "bearer" if auth_token else "none",
    )
    logger.info(
        "baton-proxy http bridge v0: server-initiated messages (standing GET SSE) "
        "not captured; requests are serialised"
    )

    client = StreamableHttpClient(url, auth_token=auth_token)

    try:
        rc = _run_http_loop(processor, client)
    finally:
        # Symmetric with the stdio path: resolve any call still pending at
        # teardown (serialisation makes this rare, but a mid-call exit is
        # possible) so no *_start dangles.
        processor.drain_pending(
            "proxy_upstream_closed", "bridge shut down with the request in flight"
        )
        emitter.stop()
    logger.info("baton-proxy exiting (http bridge rc=%d)", rc)
    return rc


def _run_http_loop(processor: MessageProcessor, client: Any) -> int:
    """Read stdin → POST upstream → write responses to stdout. Fail-open throughout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            req = None
        # Drop anything that isn't a single JSON-RPC object: non-JSON, or valid
        # JSON that is a bare array/scalar (json.loads accepts those without
        # raising). There's no raw byte channel to an HTTP endpoint, so we can't
        # forward it; drop with a log. Guarding here makes every later
        # req.get(...) provably safe (so the except path below can't re-raise).
        if not isinstance(req, dict):
            logger.warning("baton-proxy: dropping non-object client line on http bridge")
            continue

        # Fail-open: a bug processing one message must never propagate out and
        # kill the whole bridge process (unlike the stdio path, this loop runs
        # on the main thread). Log, error the client so it doesn't hang, move on.
        try:
            action = processor.handle_client_message(req)
        except Exception:
            logger.exception("baton-proxy: handle_client_message failed")
            _write_client_error(
                req.get("id"), -32603, "baton-proxy: internal error processing the request"
            )
            continue

        if action.respond is not None:
            try:
                _write_stdout(action.respond)
            except Exception:
                logger.exception("baton-proxy: forward to client failed")
            continue

        # Invariant (enforced in _ClientAction.__post_init__): respond is None
        # here, so forward is set. No `or req` fallback — that would silently
        # mask a processor bug by forwarding the unprocessed request.
        forward = action.forward
        req_id = forward.get("id")
        is_notification = req_id is None
        try:
            responses = client.post(forward)
        except Exception as e:
            # Fail-open: a network timeout / drop / non-2xx must not hang Claude
            # or kill the loop. Emit a synthetic error for the dangling start
            # (if this was a tracked call) and hand Claude a JSON-RPC error so
            # it gets a result instead of waiting forever.
            logger.warning("baton-proxy: upstream POST failed: %s", e)
            if not is_notification:
                err = str(e)
                processor.synthesize_pending_error(req_id, "proxy_upstream_unreachable", err)
                _write_client_error(req_id, -32001, f"baton-proxy: upstream request failed: {err}")
            continue

        # Write every returned message; track whether one actually answered this
        # request's id.
        responded = False
        for msg in responses:
            if isinstance(msg, dict) and msg.get("id") == req_id:
                responded = True
            out = processor.handle_server_message(msg)
            try:
                _write_stdout(out)
            except Exception:
                logger.exception("baton-proxy: forward to client failed")

        # The upstream accepted the request (2xx) but returned nothing that
        # answers it — an empty 200 body, a 202, or an SSE stream with no
        # matching frame. Without this, the client blocks forever on this id and
        # the pending start dangles. Resolve both.
        if not is_notification and not responded:
            logger.warning("baton-proxy: upstream returned no response for id=%r", req_id)
            processor.synthesize_pending_error(
                req_id, "proxy_no_response", "upstream returned no response for this request"
            )
            _write_client_error(
                req_id, -32001, "baton-proxy: upstream returned no response for this request"
            )

    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Manual subcommand dispatch: the `scan` activation flow is a distinct
    # entry point. Done before argparse because the proxy path uses
    # REMAINDER for the upstream command, which doesn't compose with
    # subparsers. Everything that isn't `scan` is the proxy wrap, unchanged.
    if argv and argv[0] == "scan":
        from baton_proxy.scan import scan_main

        return scan_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="baton-proxy",
        description=(
            "MCP proxy. Wraps a stdio MCP server (subprocess) OR bridges to an "
            "HTTPS Streamable-HTTP MCP server (--url), injects an annotation "
            "tool into the handshake, and emits friction events to baton-console."
        ),
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Upstream MCP server URL for the HTTPS bridge (Streamable HTTP "
            "transport). Mutually exclusive with the `-- <command>` stdio form. "
            "Auth via BATON_UPSTREAM_AUTH_TOKEN (sent as `Authorization: Bearer`)."
        ),
    )
    parser.add_argument(
        "upstream",
        nargs=argparse.REMAINDER,
        help="Upstream MCP server command, after `--`. Example: -- npx @vendor/mcp-server",
    )
    args = parser.parse_args(argv)

    upstream = list(args.upstream or [])
    if upstream and upstream[0] == "--":
        upstream = upstream[1:]

    if args.url:
        if upstream:
            parser.error("--url and the `-- <command>` stdio form are mutually exclusive")
        return run_http_proxy(args.url)

    if not upstream:
        parser.error(
            "upstream command required (after `--`), or pass --url <url> for the HTTP bridge"
        )

    return run_proxy(upstream)


if __name__ == "__main__":
    raise SystemExit(main())

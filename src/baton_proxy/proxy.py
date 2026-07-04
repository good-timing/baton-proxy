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

    @property
    def names(self) -> set[str]:
        return {t["name"] for t in self.tools}

    @classmethod
    def create(cls, event_sink_url: str | None, *, tenant_type: str = "vendor") -> _Injection:
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


def _write_client_error(req_id: Any, code: int, message: str) -> None:
    """Hand the client a JSON-RPC error for a request id, so it never hangs.

    No-op for notifications (``req_id is None``) — those get no response by
    definition. Swallows write errors (fail-open: a broken stdout must not
    take down the loop).
    """
    if req_id is None:
        return
    try:
        _write_line(
            sys.stdout,
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        )
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

    def _track(self, req_id: Any, call: _PendingCall) -> None:
        """Register an in-flight call + evict overflow, under the pending lock."""
        with self._pending_lock:
            self._pending[req_id] = call
            _evict_overflow(self._pending, self._emitter)

    def handle_client_message(self, req: dict[str, Any]) -> _ClientAction:
        """Intercept/emit for a client->server message; return the transport's action."""
        method = req.get("method")

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
            try:
                self._emitter.enqueue_tool_call_start(
                    tool_name=safe_tool_name,
                    params=params.get("arguments"),
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

        return _inject_into_response(msg, self._injection)

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


def _pump_client_to_server(child_stdin: Any, processor: MessageProcessor) -> None:
    """stdio client->server I/O loop. Message logic lives in the processor."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
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
                _write_line(sys.stdout, action.respond)
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
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                logger.exception("baton-proxy: forward to client failed")
            continue

        out = processor.handle_server_message(msg)
        try:
            _write_line(sys.stdout, out)
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


def run_proxy(argv: list[str]) -> int:
    """Spawn the upstream stdio MCP server and pump bidirectionally.

    `argv` is the upstream command (e.g. `["npx", "@vendor/mcp-server"]`).
    Returns the upstream's exit code.
    """
    config = Config.from_env()
    _configure_logging(config.log_file)
    injection = _Injection.create(config.event_sink, tenant_type=config.tenant_type)
    logger.info(
        "baton-proxy starting (session=%s, emission=%s, tools=%s, upstream=%s)",
        config.session_id,
        "on" if config.emission_enabled else "off",
        sorted(injection.names),
        " ".join(argv),
    )

    emitter = Emitter(config)
    emitter.start()

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

    processor = MessageProcessor(emitter, injection, config.session_id)

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

    config = Config.from_env()
    _configure_logging(config.log_file)
    injection = _Injection.create(config.event_sink, tenant_type=config.tenant_type)

    auth_token = os.environ.get("BATON_UPSTREAM_AUTH_TOKEN")
    logger.info(
        "baton-proxy starting (session=%s, emission=%s, tools=%s, upstream=%s, transport=http, auth=%s)",
        config.session_id,
        "on" if config.emission_enabled else "off",
        sorted(injection.names),
        url,
        "bearer" if auth_token else "none",
    )
    logger.info(
        "baton-proxy http bridge v0: server-initiated messages (standing GET SSE) "
        "not captured; requests are serialised"
    )

    emitter = Emitter(config)
    emitter.start()

    processor = MessageProcessor(emitter, injection, config.session_id)
    client = StreamableHttpClient(url, auth_token=auth_token)

    try:
        rc = _run_http_loop(processor, client)
    finally:
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
            # Non-JSON on the wire is pathological for an HTTP bridge (there is
            # no raw byte channel to an HTTP endpoint). Drop with a log rather
            # than POST garbage; matches the stdio path's best-effort framing.
            logger.warning("baton-proxy: dropping non-JSON client line on http bridge")
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
                _write_line(sys.stdout, action.respond)
            except Exception:
                logger.exception("baton-proxy: forward to client failed")
            continue

        forward = action.forward or req
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
                _write_line(sys.stdout, out)
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

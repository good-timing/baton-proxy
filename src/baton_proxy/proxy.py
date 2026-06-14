"""Subprocess-wrap MCP proxy.

Wraps a stdio MCP server as a child process. From Claude's perspective the
proxy *is* the MCP server; from the upstream server's perspective the proxy
is the client. This lets the proxy inject an annotation tool + instructions
into the handshake and emit friction events without the vendor changing a
single line of code.

The proxy is two unidirectional pumps:
  - client -> server : forwards stdin lines, intercepts `tools/call` for the
                       injected annotation tool, enqueues `tool_call_start`
                       for forwarded calls.
  - server -> client : forwards stdout lines, modifies the `initialize` and
                       `tools/list` responses, enqueues `tool_call_end` /
                       `tool_call_error` based on the response.

Errors anywhere in the proxy MUST NOT propagate to either pipe. Fail-open
means: if instrumentation breaks, MCP traffic still flows.
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
            "required": ["signal_type", "intent", "suggested_improvement"],
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
    """Tracking state for an in-flight tool call (start emitted, awaiting end)."""

    __slots__ = ("tool_name", "started_ms", "runtime_meta")

    def __init__(
        self, tool_name: str, started_ms: int, runtime_meta: dict[str, Any] | None
    ) -> None:
        self.tool_name = tool_name
        self.started_ms = started_ms
        self.runtime_meta = runtime_meta


def _evict_overflow(pending: OrderedDict[Any, _PendingCall], emitter: Emitter) -> None:
    """Evict oldest pending entries down to MAX_PENDING; caller holds pending_lock.

    Each eviction emits a synthetic tool_call_error so the wire stream
    doesn't carry a dangling start with no end/error pair.
    """
    while len(pending) > MAX_PENDING:
        _evicted_id, evicted = pending.popitem(last=False)
        try:
            emitter.enqueue_tool_call_error(
                tool_name=evicted.tool_name,
                error_type=EVICTED_ERROR_TYPE,
                error_body="proxy pending dict overflowed without upstream response",
                duration_ms=max(0, utc_now_ms() - evicted.started_ms),
                runtime_meta=evicted.runtime_meta,
            )
        except Exception:
            logger.exception("baton-proxy: enqueue evicted tool_call_error failed")


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
    signal = args.get("signal_type", "unknown")
    return {
        "jsonrpc": "2.0",
        "id": req.get("id"),
        "result": {
            "content": [{"type": "text", "text": f"baton_annotate recorded signal_type={signal}"}]
        },
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


def _pump_client_to_server(
    child_stdin: Any,
    pending: OrderedDict[Any, _PendingCall],
    pending_lock: threading.Lock,
    emitter: Emitter,
    injection: _Injection,
    session_id: str,
) -> None:
    """Forward client->server, with injection interception + start emission."""
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

        method = req.get("method")

        if method == "tools/call":
            params = req.get("params", {}) or {}
            tool_name = params.get("name")
            if tool_name in injection.names:
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
                        emitter.enqueue_annotation(
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
                    _write_line(
                        sys.stdout,
                        _handle_injected_call(
                            req,
                            injection=injection,
                            session_id=session_id,
                            emitter=emitter,
                        ),
                    )
                except Exception:
                    logger.exception("baton-proxy: synthesising injected response failed")
                continue

            # Real tool call — emit start FIRST, then track for end/error.
            # Order matters: if enqueue throws we MUST NOT have a pending
            # entry, otherwise the eventual response would emit an orphan
            # tool_call_end the worker can't pair with a start. (Forwarding
            # to the upstream happens after this block, so there's no risk
            # of the response arriving before we add to pending on success.)
            req_id = req.get("id")
            # TODO: MCP also permits a top-level `_meta` on the request envelope;
            # we currently only capture `params._meta`. Revisit if a vendor runtime
            # surfaces correlation ids at the request level instead.
            runtime_meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
            safe_tool_name = str(tool_name) if tool_name else ""
            try:
                emitter.enqueue_tool_call_start(
                    tool_name=safe_tool_name,
                    params=params.get("arguments"),
                    runtime_meta=runtime_meta,
                )
            except Exception:
                logger.exception("baton-proxy: enqueue tool_call_start failed")
            else:
                with pending_lock:
                    pending[req_id] = _PendingCall(
                        tool_name=safe_tool_name,
                        started_ms=utc_now_ms(),
                        runtime_meta=runtime_meta,
                    )
                    _evict_overflow(pending, emitter)

        try:
            child_stdin.write(json.dumps(req) + "\n")
            child_stdin.flush()
        except Exception:
            logger.exception("baton-proxy: forward to upstream failed")


def _pump_server_to_client(
    child_stdout: Any,
    pending: OrderedDict[Any, _PendingCall],
    pending_lock: threading.Lock,
    emitter: Emitter,
    injection: _Injection,
) -> None:
    """Forward server->client, with response modification + end/error emission."""
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

        # Correlate this response to a pending tool call (by id) and emit
        # the matching end/error event.
        msg_id = msg.get("id")
        if msg_id is not None:
            call: _PendingCall | None = None
            with pending_lock:
                call = pending.pop(msg_id, None)
            if call is not None:
                try:
                    duration_ms = max(0, utc_now_ms() - call.started_ms)
                    if "error" in msg:
                        err = msg["error"] or {}
                        emitter.enqueue_tool_call_error(
                            tool_name=call.tool_name,
                            error_type=str(err.get("code", "")) or "unknown",
                            error_body=str(err.get("message", "")),
                            duration_ms=duration_ms,
                            runtime_meta=call.runtime_meta,
                        )
                    else:
                        emitter.enqueue_tool_call_end(
                            tool_name=call.tool_name,
                            result=msg.get("result"),
                            duration_ms=duration_ms,
                            runtime_meta=call.runtime_meta,
                        )
                except Exception:
                    logger.exception("baton-proxy: enqueue tool_call_end/error failed")

        modified = _inject_into_response(msg, injection)
        try:
            _write_line(sys.stdout, modified)
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
    """Spawn the upstream MCP server and pump bidirectionally.

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

    pending: OrderedDict[Any, _PendingCall] = OrderedDict()
    pending_lock = threading.Lock()

    t_in = threading.Thread(
        target=_pump_client_to_server,
        args=(child.stdin, pending, pending_lock, emitter, injection, config.session_id),
        name="baton-proxy-in",
        daemon=True,
    )
    t_out = threading.Thread(
        target=_pump_server_to_client,
        args=(child.stdout, pending, pending_lock, emitter, injection),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="baton-proxy",
        description=(
            "Subprocess-wrap MCP proxy. Wraps a stdio MCP server, injects an "
            "annotation tool into the handshake, and emits friction events to "
            "baton-console."
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
    if not upstream:
        parser.error("upstream command required, after `--`")

    return run_proxy(upstream)


if __name__ == "__main__":
    raise SystemExit(main())

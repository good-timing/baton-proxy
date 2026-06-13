"""Async friction-event emitter.

The proxy intercepts MCP traffic on the hot path (every `tools/call`). Doing
a synchronous network call from that thread would add the full ingest
round-trip (~50-200ms) to every tool call. Trust pattern: sub-ms overhead.
So emission is queued and drained on a background thread; the hot path
only pays an `enqueue()`.

Failure mode: the background thread logs and drops on sink failures. A
backed-up or dead emitter must NEVER block proxy I/O — that's the
fail-open contract. Queue is bounded; overflow drops the oldest event
and logs once per 100 drops.

Where events go is the Sink's job (sinks.py). The Emitter just enqueues,
drains, and hands each event to ``self._sink.write(event)``. Sink is built
once at start() from ``BATON_EVENT_SINK`` (URL-driven, comma-separated
list builds a MultiSink); misconfig (unsupported scheme, http without
api_key) raises at start() — never a silent no-emit.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from baton_proxy import __version__ as _PKG_VERSION
from baton_proxy.config import Config
from baton_proxy.sinks import Sink, make_sink

logger = logging.getLogger(__name__)

# Bounded queue — backed-up emitter shouldn't accumulate unbounded memory.
# 1000 events buys a decent buffer for typical 5-10 RPS tool-call workloads.
_QUEUE_MAXSIZE = 1000

_SDK_VERSION = f"baton-proxy/{_PKG_VERSION}"
_AGENT_RUNTIME = "mcp-proxy"


@dataclass(frozen=True)
class _Event:
    """Wire envelope, mirrors baton-console IncomingEvent shape.

    Schemas are mirrored rather than imported so the proxy isn't lock-stepped
    to a baton-console release. The console accepts `spec_version: str = "0.1"`
    with a default and `extra="forbid"` on everything else.
    """

    event_id: str
    event_type: str
    session_id: str
    sequence_number: int
    captured_at: str
    tenant_id: str
    consent_token: str
    sdk_version: str
    agent_runtime: str
    payload: dict[str, Any]
    runtime_meta: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "sequence_number": self.sequence_number,
            "captured_at": self.captured_at,
            "tenant_id": self.tenant_id,
            "consent_token": self.consent_token,
            "sdk_version": self.sdk_version,
            "agent_runtime": self.agent_runtime,
            "payload": self.payload,
        }
        if self.runtime_meta is not None:
            d["runtime_meta"] = self.runtime_meta
        return d


class Emitter:
    """Background-thread emitter. Construct, call .start(), enqueue from any
    thread, and call .stop() at shutdown.

    When `config.emission_enabled` is False, .start() / .enqueue_*() are no-ops
    so callers don't need to branch.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._queue: queue.Queue[_Event | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._seq_lock = threading.Lock()
        # Serialises put_nowait across producers. queue.Queue's internal mutex
        # guards individual operations but not a get+put pair, so an unguarded
        # drop-oldest sequence has a window where another producer can refill
        # the queue between our get and put.
        self._enqueue_lock = threading.Lock()
        self._drop_count = 0
        # Sink set up in start(); None until then.
        self._sink: Sink | None = None

    def start(self) -> None:
        if not self._config.emission_enabled:
            return
        if self._thread is not None:
            return
        assert self._config.event_sink is not None  # emission_enabled gates this
        self._guard_remote_consent()
        self._sink = make_sink(self._config.event_sink, api_key=self._config.api_key)
        self._thread = threading.Thread(target=self._drain, name="baton-proxy-emitter", daemon=True)
        self._thread.start()

    def _guard_remote_consent(self) -> None:
        """Refuse to ship events to a remote sink while the consent token is
        still the install-time placeholder. Local file/stderr sinks are
        always OK — the placeholder just marks "this install hasn't been
        wired to a remote sink yet". The check runs before sink
        construction so a misconfigured install fails loudly at startup
        instead of silently leaking placeholder-tagged events.
        """
        if not self._config.using_placeholder_consent:
            return
        assert self._config.event_sink is not None
        parts = [p.strip() for p in self._config.event_sink.split(",") if p.strip()]
        if any(p.startswith(("http://", "https://")) for p in parts):
            raise ValueError(
                "Refusing to ship events to an http(s) sink with placeholder "
                "BATON_CONSENT_TOKEN='local' — set BATON_CONSENT_TOKEN to the "
                "real per-install consent token before pointing at a remote "
                "endpoint."
            )

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        # Blocking put with timeout — if the queue is full, put_nowait would
        # silently drop the sentinel and the drain thread would loop until
        # daemon-killed at process exit (losing buffered events). put() waits
        # for the drain thread to free a slot, which it does once per second.
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            # Drain thread is dead or wedged; nothing more we can do here.
            pass
        self._thread.join(timeout=timeout)
        self._thread = None
        if self._sink is not None:
            self._sink.close()
            self._sink = None

    def enqueue_tool_call_start(
        self,
        *,
        tool_name: str,
        params: Mapping[str, Any] | None,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="tool_call_start",
            payload={"tool_name": tool_name, "params": dict(params) if params else {}},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_tool_call_end(
        self,
        *,
        tool_name: str,
        result: Any,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="tool_call_end",
            payload={"tool_name": tool_name, "result": result, "duration_ms": duration_ms},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_tool_call_error(
        self,
        *,
        tool_name: str,
        error_type: str,
        error_body: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="tool_call_error",
            payload={
                "tool_name": tool_name,
                "error_type": error_type,
                "error_body": error_body,
                "duration_ms": duration_ms,
            },
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_annotation(
        self,
        *,
        signal_type: str | None,
        intent: str | None,
        suggested_improvement: str | None,
        expected_outcome: str | None = None,
        workflow: str | None = None,
        context: Mapping[str, Any] | None = None,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Annotation event per SPEC §11.4; nullable keys omitted when None."""
        candidates: dict[str, Any] = {
            "signal_type": signal_type,
            "intent": intent,
            "suggested_improvement": suggested_improvement,
            "expected_outcome": expected_outcome,
            "workflow": workflow,
            "context": dict(context) if context is not None else None,
        }
        payload = {k: v for k, v in candidates.items() if v is not None}
        self._enqueue(
            event_type="annotation",
            payload=payload,
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def _enqueue(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        runtime_meta: dict[str, Any] | None,
    ) -> None:
        if not self._config.emission_enabled or self._thread is None:
            return

        with self._seq_lock:
            seq = self._seq
            self._seq += 1

        event = _Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            session_id=self._config.session_id,
            sequence_number=seq,
            captured_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            tenant_id=self._config.tenant_id,  # type: ignore[arg-type]
            consent_token=self._config.consent_token,  # type: ignore[arg-type]
            sdk_version=_SDK_VERSION,
            agent_runtime=_AGENT_RUNTIME,
            payload=payload,
            runtime_meta=runtime_meta,
        )

        with self._enqueue_lock:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                # Drop-oldest. Held under _enqueue_lock so the get+put pair
                # is atomic w.r.t. other producers; without it a concurrent
                # put_nowait could refill the slot between our get and put
                # and silently drop the new event instead of the oldest.
                self._drop_count += 1
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(event)
                except queue.Full:
                    pass
                if self._drop_count % 100 == 1:
                    logger.warning(
                        "baton-proxy emitter queue full, dropped %d events", self._drop_count
                    )

    def _drain(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if event is None:
                return
            self._deliver(event)

    def _deliver(self, event: _Event) -> None:
        """Hand one event to the sink. Any failure is logged and dropped —
        fail-open contract: a broken sink must not stall the drain loop or
        propagate exceptions that would kill the daemon thread."""
        assert self._sink is not None  # start() built it
        try:
            self._sink.write(event.to_json())
        except Exception as e:  # noqa: BLE001 — fail-open at delivery boundary
            logger.warning(
                "baton-proxy emit %s -> %s: %s", event.event_type, type(e).__name__, e
            )


def utc_now_ms() -> int:
    """Monotonic-ish millisecond clock for duration math. time.monotonic()
    gives a relative clock; multiply to ms."""
    return int(time.monotonic() * 1000)

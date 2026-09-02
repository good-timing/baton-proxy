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

from baton_proxy import USER_AGENT as _SDK_VERSION
from baton_proxy.config import Config
from baton_proxy.identity import Principal, hash_user_id
from baton_proxy.scrub import Scrubber
from baton_proxy.sinks import Sink, make_sink

logger = logging.getLogger(__name__)

# Bounded queue — backed-up emitter shouldn't accumulate unbounded memory.
# 1000 events buys a decent buffer for typical 5-10 RPS tool-call workloads.
_QUEUE_MAXSIZE = 1000

# Product/version token, single-sourced in baton_proxy.__init__ (imported above
# as _SDK_VERSION) so it can't drift from the HTTP bridge's User-Agent.

# Fallback for agent_runtime, not the answer: it names the TRANSPORT, which is
# all we can honestly say when nothing observed the client. Every event used to
# carry it unconditionally, so a console reading the field back learned that
# Baton was in the path and nothing about the app the person was working in.
_AGENT_RUNTIME = "mcp-proxy"


def detect_agent_runtime(meta: Mapping[str, Any] | None) -> str | None:
    """The agent runtime one event's MCP ``_meta`` betrays, or None.

    Rules copied from the SDK's
    ``baton/integrations/fastmcp/runtime_adapter.detect_agent_runtime``.
    baton-proxy is zero-dep and cannot import it, so this is a copy across
    repos that no test can pin; what makes the copy worth having is that both
    sensors must store the SAME token for the same client, or every query that
    groups by ``agent_runtime`` splits one client into two. Donor's precedence:

    1. explicit ``_meta.baton.agent_runtime`` — a documented client override
    2. a ``claudecode/*`` key prefix -> ``claude-code``
    3. None, and the caller falls back

    Takes a plain mapping rather than the donor's ``Any``: both proxy call
    sites already ``isinstance``-check ``_meta`` to a dict before it reaches
    the emitter, so the donor's ``meta_to_dict`` normalisation has nothing to
    do here.
    """
    if not meta:
        return None

    baton_meta = meta.get("baton")
    if isinstance(baton_meta, dict):
        runtime = baton_meta.get("agent_runtime")
        if isinstance(runtime, str) and runtime:
            return runtime

    for key in meta:
        if isinstance(key, str) and key.startswith("claudecode/"):
            return "claude-code"

    return None


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
    vendor_id: str
    consent_token: str
    sdk_version: str
    agent_runtime: str
    payload: dict[str, Any]
    runtime_meta: dict[str, Any] | None = None
    # Hashed end-user actor (HMAC-SHA256, per-tenant, hashed at the edge — the
    # raw principal is never emitted). None when no identity resolved or no
    # HMAC key configured. Additive + nullable: omitted from the wire when
    # None, so a v0.4.x console sees byte-identical output.
    user_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "sequence_number": self.sequence_number,
            "captured_at": self.captured_at,
            "tenant_id": self.tenant_id,
            "vendor_id": self.vendor_id,
            "consent_token": self.consent_token,
            "sdk_version": self.sdk_version,
            "agent_runtime": self.agent_runtime,
            "payload": self.payload,
        }
        if self.runtime_meta is not None:
            d["runtime_meta"] = self.runtime_meta
        if self.user_id is not None:
            d["user_id"] = self.user_id
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
        # One-shot guard so a missing HMAC key logs once, not per event.
        self._warned_no_hmac_key = False
        # Sink set up in start(); None until then.
        self._sink: Sink | None = None
        # Source-side PII scrubber. Stateful — accumulates per-category
        # counts across every payload that flows through _enqueue, so the
        # report tool can surface "N emails, M bearer tokens" without
        # re-parsing the JSONL. Applied BEFORE the queue, so file sink
        # and HTTP sink both see scrubbed values.
        self._scrubber = Scrubber()
        # The client the handshake named, latched by set_agent_runtime. A plain
        # attribute: one str assignment, written once on the transport's reader
        # thread before the first event exists, read on every enqueue after.
        self._agent_runtime: str | None = None

    def start(self) -> None:
        if not self._config.emission_enabled:
            return
        if self._thread is not None:
            return
        assert self._config.event_sink is not None  # emission_enabled gates this
        self._guard_remote_consent()
        self._guard_remote_vendor()
        self._sink = make_sink(self._config.event_sink, api_key=self._config.api_key)
        self._thread = threading.Thread(target=self._drain, name="baton-proxy-emitter", daemon=True)
        self._thread.start()

    def set_agent_runtime(self, name: str) -> None:
        """Latch the client the MCP handshake named itself.

        ``clientInfo.name`` arrives on ``initialize``, which precedes every
        event this process emits — including ``surface_snapshot``, which is
        sequence 1 and carries no ``_meta`` at all. That matters because the
        consumers that ask a session what it ran in read its FIRST event
        (baton-console's ``channels.pylon`` and ``worker.correlate``), so the
        per-event ``_meta`` heuristic alone can never reach them: by the time
        any ``_meta`` exists, event 1 is already written.

        Stored as sent, case-folded. Claude Code sends ``claude-code``, which
        is already the token ``detect_agent_runtime`` produces, so the two
        signals agree without a mapping table. An unrecognised client passes
        through rather than being mapped to a guess — the name is something we
        OBSERVED, and inventing one for a client we did not recognise is the
        fabrication this whole field is being fixed to stop.

        Process-wide, which is what the stdio proxy is: one process per client
        session. A hosted adapter multiplexing many sessions through one
        process would need this keyed per session — that is why the per-event
        signal outranks this latch rather than the other way round.
        """
        folded = name.strip().lower()
        if folded:
            self._agent_runtime = folded

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
        # Any sink that leaves the machine is "remote" — http(s) and s3.
        parts = [p.strip() for p in self._config.event_sink.split(",") if p.strip()]
        if any(p.startswith(("http://", "https://", "s3://")) for p in parts):
            raise ValueError(
                "Refusing to ship events to a remote sink (http/https/s3) with "
                "placeholder BATON_CONSENT_TOKEN='local' — set BATON_CONSENT_TOKEN "
                "to the real per-install consent token before pointing at a remote "
                "endpoint."
            )

    def _guard_remote_vendor(self) -> None:
        """Refuse a remote sink while vendor_id is still the placeholder.

        The mirror of the consent guard, and the reason ``vendor_id`` can
        have a default at all. Locally the label is a grep convenience and
        `local` is honest. On a Console it is the key friction is bucketed
        by, so an install that ships `local` does not produce a slightly
        wrong dashboard — it produces rows filed under a vendor nobody owns,
        which is indistinguishable from another install doing the same.
        Refused at start(), before sink construction, so the operator sees
        it once rather than discovering it in the Console days later.
        """
        if not self._config.using_placeholder_vendor:
            return
        assert self._config.event_sink is not None
        parts = [p.strip() for p in self._config.event_sink.split(",") if p.strip()]
        if any(p.startswith(("http://", "https://", "s3://")) for p in parts):
            raise ValueError(
                "Refusing to ship events to a remote sink (http/https/s3) with "
                "the placeholder BATON_VENDOR_ID='local' — set BATON_VENDOR_ID "
                "to the wrapped MCP server's vendor identifier (e.g. 'notion', "
                "'github', 'slack') before pointing at a Console. It is the key "
                "friction is bucketed by."
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

    def scrub_counts(self) -> dict[str, int]:
        """Snapshot of per-category PII redaction counts since session start.
        Read by the report tool to surface "N emails, M tokens" without
        re-parsing the JSONL stream. Returns a copy so callers can't mutate
        the live counter."""
        return dict(self._scrubber.counts)

    def enqueue_tool_call_start(
        self,
        *,
        tool_name: str,
        params: Mapping[str, Any] | None,
        call_intent: str | None = None,
        call_expected: str | None = None,
        call_workflow: str | None = None,
        intent_source: str | None = None,
        runtime_meta: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        principal: Principal | None = None,
    ) -> None:
        # `call_intent` / `call_expected` / `call_workflow` are the values
        # stripped from the injected per-tool params. They ride the payload as
        # SIBLINGS of params — params must stay exactly the vendor-visible
        # arguments. The console ignores unknown payload keys (opaque JSONB),
        # so this is additive on the wire. Each key is OMITTED when the caller
        # left that param off: "the agent said nothing" and "the agent said
        # nothing useful" are different, and a null would flatten them.
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "params": dict(params) if params else {},
        }
        if call_intent is not None:
            payload["call_intent"] = call_intent
        if call_expected is not None:
            payload["call_expected"] = call_expected
        if call_workflow is not None:
            payload["call_workflow"] = call_workflow
        if intent_source is not None:
            payload["intent_source"] = intent_source
        self._enqueue(
            event_type="tool_call_start",
            payload=payload,
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
            session_id=session_id,
            principal=principal,
        )

    def enqueue_surface_snapshot(
        self,
        *,
        surface_hash: str,
        server_info: Any,
        capabilities: Any,
        instructions: str | None,
        tools: list[dict[str, Any]],
        seam_augmentations: dict[str, Any],
        session_id: str | None = None,
    ) -> None:
        # The vendor-true surface (pre-injection), emitted at most once per
        # hash per process — see MessageProcessor._capture_surface for the
        # trigger/dedupe rules. `seam_augmentations` records the as-served
        # delta Baton added, so a consumer can render both layers.
        self._enqueue(
            event_type="surface_snapshot",
            payload={
                "surface_hash": surface_hash,
                "server_info": server_info,
                "capabilities": capabilities,
                "instructions": instructions,
                "tools": tools,
                "seam_augmentations": seam_augmentations,
            },
            runtime_meta=None,
            session_id=session_id,
        )

    def enqueue_tool_call_end(
        self,
        *,
        tool_name: str,
        result: Any,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        principal: Principal | None = None,
    ) -> None:
        # session_id/principal are additive: the stdio proxy omits them (1-process-
        # per-user → _enqueue falls back to the process session). A hosted adapter
        # that serves many sessions from one process MUST pass the per-event session
        # so the end row attributes to the right timeline.
        self._enqueue(
            event_type="tool_call_end",
            payload={"tool_name": tool_name, "result": result, "duration_ms": duration_ms},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
            session_id=session_id,
            principal=principal,
        )

    def enqueue_tool_call_error(
        self,
        *,
        tool_name: str,
        error_type: str,
        error_body: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        principal: Principal | None = None,
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
            session_id=session_id,
            principal=principal,
        )

    def enqueue_resource_read_start(
        self,
        *,
        uri: str,
        params: Mapping[str, Any] | None,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="resource_read_start",
            payload={"uri": uri, "params": dict(params) if params else {}},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_resource_read_end(
        self,
        *,
        uri: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="resource_read_end",
            payload={"uri": uri, "duration_ms": duration_ms},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_resource_read_error(
        self,
        *,
        uri: str,
        error_type: str,
        error_body: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="resource_read_error",
            payload={
                "uri": uri,
                "error_type": error_type,
                "error_body": error_body,
                "duration_ms": duration_ms,
            },
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_resource_list_start(
        self,
        *,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="resource_list_start",
            payload={},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_resource_list_end(
        self,
        *,
        count: int,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="resource_list_end",
            payload={"count": count, "duration_ms": duration_ms},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_resource_list_error(
        self,
        *,
        error_type: str,
        error_body: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="resource_list_error",
            payload={
                "error_type": error_type,
                "error_body": error_body,
                "duration_ms": duration_ms,
            },
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_prompt_get_start(
        self,
        *,
        name: str,
        params: Mapping[str, Any] | None,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="prompt_get_start",
            payload={"name": name, "params": dict(params) if params else {}},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_prompt_get_end(
        self,
        *,
        name: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="prompt_get_end",
            payload={"name": name, "duration_ms": duration_ms},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_prompt_get_error(
        self,
        *,
        name: str,
        error_type: str,
        error_body: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="prompt_get_error",
            payload={
                "name": name,
                "error_type": error_type,
                "error_body": error_body,
                "duration_ms": duration_ms,
            },
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_prompt_list_start(
        self,
        *,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="prompt_list_start",
            payload={},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_prompt_list_end(
        self,
        *,
        count: int,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="prompt_list_end",
            payload={"count": count, "duration_ms": duration_ms},
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
        )

    def enqueue_prompt_list_error(
        self,
        *,
        error_type: str,
        error_body: str,
        duration_ms: int,
        runtime_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._enqueue(
            event_type="prompt_list_error",
            payload={
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
        intent_source: str | None = None,
        tool_name: str | None = None,
        runtime_meta: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        principal: Principal | None = None,
    ) -> None:
        """Annotation event per SPEC §11.4; nullable keys omitted when None.

        ``intent_source``/``tool_name`` mark annotations synthesised from the
        injected per-tool intent param (vs a real annotate-tool call). Extra
        payload keys are safe — the console's annotation payload is opaque.
        """
        candidates: dict[str, Any] = {
            "signal_type": signal_type,
            "intent": intent,
            "suggested_improvement": suggested_improvement,
            "expected_outcome": expected_outcome,
            "workflow": workflow,
            "context": dict(context) if context is not None else None,
            "intent_source": intent_source,
            "tool_name": tool_name,
        }
        payload = {k: v for k, v in candidates.items() if v is not None}
        self._enqueue(
            event_type="annotation",
            payload=payload,
            runtime_meta=dict(runtime_meta) if runtime_meta else None,
            session_id=session_id,
            principal=principal,
        )

    def _enqueue(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        runtime_meta: dict[str, Any] | None,
        session_id: str | None = None,
        principal: Principal | None = None,
    ) -> None:
        # `session_id` overrides the per-process session for callers that
        # carry their own session identity per event (the ExtMCP adapter keys
        # every event on the gateway's mcp-session-id header, not a process
        # uuid). Defaults to the config's process-lifetime session for the
        # stdio-proxy path, which is 1-process-per-user.
        if not self._config.emission_enabled or self._thread is None:
            return

        # Hash the end-user principal AT THE EDGE — the console DB is
        # metadata-only and may only ever see the hash (residency contract). The
        # raw principal never survives this method. No key configured → fail-open:
        # drop user_id, keep emitting, warn once (user_id is additive analytics,
        # never a consent/authz gate).
        user_id: str | None = None
        if principal is not None:
            key = self._config.user_id_hmac_key
            if key:
                user_id = hash_user_id(
                    principal.user_id,
                    tenant_id=self._config.tenant_id or "",
                    key=key,
                )
            elif not self._warned_no_hmac_key:
                self._warned_no_hmac_key = True
                logger.warning(
                    "baton-proxy: identity resolved but BATON_USER_ID_HMAC_KEY "
                    "is unset — dropping user_id (events still emit)"
                )

        # Scrub PII from the payload before anything else touches it. Both
        # the file sink and any HTTP sink will see only the scrubbed copy,
        # so the trust contract holds even for purely local installs.
        payload = self._scrubber(payload)

        with self._seq_lock:
            seq = self._seq
            self._seq += 1

        # Which app the person is working in. The event's own ``_meta`` first,
        # so a process serving several clients still attributes each event to
        # the one that sent it; then the handshake latch, which is the only
        # signal that exists before event 1; then the transport's name, which
        # is the honest answer when nothing named a client.
        agent_runtime = detect_agent_runtime(runtime_meta) or self._agent_runtime or _AGENT_RUNTIME

        event = _Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            session_id=session_id or self._config.session_id,
            sequence_number=seq,
            captured_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            tenant_id=self._config.tenant_id,  # type: ignore[arg-type]
            vendor_id=self._config.vendor_id,
            consent_token=self._config.consent_token,  # type: ignore[arg-type]
            sdk_version=_SDK_VERSION,
            agent_runtime=agent_runtime,
            payload=payload,
            runtime_meta=runtime_meta,
            user_id=user_id,
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
            logger.warning("baton-proxy emit %s -> %s: %s", event.event_type, type(e).__name__, e)


def utc_now_ms() -> int:
    """Monotonic-ish millisecond clock for duration math. time.monotonic()
    gives a relative clock; multiply to ms."""
    return int(time.monotonic() * 1000)

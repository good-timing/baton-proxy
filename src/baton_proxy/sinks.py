"""Event sinks — where the emitter delivers events.

Sink is a small sync interface: ``write(event)`` to deliver one event,
``close()`` to release resources. The proxy's Emitter runs sinks on a single
background drain thread, so write() may block on its destination without
affecting proxy I/O.

Four concrete sinks plus a fan-out:

- ``StderrSink`` — JSONL to stderr. Useful for local dev where you want
  events visible alongside log output. (Why stderr, not stdout: proxy's
  stdout is the JSON-RPC stream back to Claude — writing events there
  would corrupt the protocol. Same constraint as baton-sdk's StdoutSink.)
- ``FileSink`` — JSONL append to a path. Line-buffered so `tail -f` /
  `cat` shows events immediately.
- ``HttpSink`` — POST to ``{url}/v0/events`` with bearer auth. Same
  wire contract as baton-sdk.
- ``MultiSink`` — fan out to a list; a failure in one sink doesn't stop
  the others.

URL-driven construction via ``make_sink(url, api_key)``. A comma-separated
URL list produces a MultiSink (e.g. ``stderr:,file:///tmp/events.jsonl``
for the common dev pattern of tee-to-disk + live stderr).
"""

from __future__ import annotations

import io
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# Per-POST timeout — keep tight so the drain thread doesn't block forever
# on a dead remote endpoint. The proxy I/O path is unaffected either way.
_POST_TIMEOUT_S = 5.0


class Sink(ABC):
    """A destination for emitted events. Implementations choose their own
    delivery semantics; failure isolation is the caller's responsibility
    (see Emitter._deliver and MultiSink._fan_out)."""

    @abstractmethod
    def write(self, event: dict[str, Any]) -> None:
        """Deliver one event envelope. May block; raises on transport failure."""

    @abstractmethod
    def close(self) -> None:
        """Release any held resources. Called from Emitter.stop()."""


class StderrSink(Sink):
    """JSONL to sys.stderr. One event per line; flushes after each write so
    events are visible immediately."""

    def write(self, event: dict[str, Any]) -> None:
        sys.stderr.write(json.dumps(event) + "\n")
        sys.stderr.flush()

    def close(self) -> None:
        # We don't own stderr; nothing to release.
        return


class FileSink(Sink):
    """JSONL append to a filesystem path.

    Line-buffered text mode: each write() flushes on the trailing newline so
    a `tail -f` or `cat` observer sees events immediately. POSIX append +
    one write() per event keeps lines atomic up to PIPE_BUF — multiple
    proxies sharing the same file won't shred each other's lines."""

    def __init__(self, path: str) -> None:
        if not path:
            raise ValueError("FileSink requires a non-empty path")
        self._path = path
        self._handle: io.TextIOBase = open(  # noqa: SIM115 — closed in .close()
            path, "a", buffering=1, encoding="utf-8"
        )

    def write(self, event: dict[str, Any]) -> None:
        self._handle.write(json.dumps(event) + "\n")

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


class HttpSink(Sink):
    """POST events to ``{base_url}/v0/events`` with Authorization: Bearer.

    Same wire contract as baton-sdk's HttpSink and baton-console's
    ``IncomingEvent`` schema. Uses stdlib urllib (no httpx) — the proxy is
    zero-deps by design."""

    def __init__(self, base_url: str, *, api_key: str) -> None:
        if not api_key:
            raise ValueError(f"HttpSink requires an api_key (BATON_API_KEY) for sink {base_url}")
        self._url = base_url.rstrip("/")
        self._api_key = api_key

    def write(self, event: dict[str, Any]) -> None:
        body = json.dumps(event).encode("utf-8")
        url = f"{self._url}/v0/events"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        # Let the caller (Emitter._deliver) see HTTP / URL errors and log
        # them once per failure; don't double-log here.
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as resp:
            if resp.status >= 400:
                raise urllib.error.HTTPError(
                    url, resp.status, f"HTTP {resp.status}", resp.headers, None
                )

    def close(self) -> None:
        return


class S3Sink(Sink):
    """PUT one JSON object per event to a customer-owned S3 bucket.

    Key layout matches the enterprise residency design:
    ``{prefix/}{tenant_id}/{session_id}/{event_id}.json``. This is the
    data-residency fork — payloads land in the customer's bucket, never
    Baton's. boto3 is lazy-imported (optional extra ``[s3]``) so the base
    package stays zero-dep; a real client is built from the ambient AWS
    credential chain unless one is injected (tests pass a fake)."""

    def __init__(self, bucket: str, prefix: str = "", *, client: Any = None) -> None:
        if not bucket:
            raise ValueError("S3Sink requires a bucket")
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3  # noqa: PLC0415 — lazy so base package stays zero-dep
            except ImportError as e:  # pragma: no cover - exercised via message only
                raise ValueError(
                    "s3:// event sink requires boto3 — install 'baton-proxy[s3]'"
                ) from e
            client = boto3.client("s3")
        self._client = client

    def key_for(self, event: dict[str, Any]) -> str:
        parts = [
            p
            for p in (
                self._prefix,
                event.get("tenant_id"),
                event.get("session_id"),
                event.get("event_id"),
            )
            if p
        ]
        return "/".join(parts) + ".json"

    def ref_for(self, event: dict[str, Any]) -> str:
        return f"s3://{self._bucket}/{self.key_for(event)}"

    def write(self, event: dict[str, Any]) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self.key_for(event),
            Body=json.dumps(event).encode("utf-8"),
            ContentType="application/json",
        )

    def close(self) -> None:
        return


class SplitSink(Sink):
    """Residency split: raw PAYLOAD to one sink, METADATA to another.

    The payload leg (typically ``S3Sink`` → customer bucket) gets the full,
    self-describing event. The metadata leg (typically the console ``HttpSink``)
    gets the envelope with ``payload`` removed, marked ``payload_offloaded`` and
    — when the payload sink can name a location (``ref_for``) — a ``payload_ref``
    pointer. So raw text never reaches the console; only metadata + a reference.
    This is "data plane in the customer's perimeter, control plane in Baton's"
    as a wiring choice, not an envelope-schema change."""

    def __init__(self, metadata_sink: Sink, payload_sink: Sink) -> None:
        self._meta = metadata_sink
        self._payload = payload_sink

    def write(self, event: dict[str, Any]) -> None:
        # Payload leg first — if it fails we don't want a dangling metadata row
        # claiming an offload that never happened.
        self._payload.write(event)
        meta = {k: v for k, v in event.items() if k != "payload"}
        meta["payload_offloaded"] = True
        ref_for = getattr(self._payload, "ref_for", None)
        if callable(ref_for):
            meta["payload_ref"] = ref_for(event)
        self._meta.write(meta)

    def close(self) -> None:
        for s in (self._payload, self._meta):
            try:
                s.close()
            except Exception:  # noqa: BLE001 — closing one shouldn't block the other
                logger.exception("baton-proxy split sink close failed")


class MultiSink(Sink):
    """Fan out each event to every sink in the list.

    A failure in one sink doesn't stop the others — each is tried, errors
    are logged, and the first exception is re-raised so the Emitter's
    per-event try/except still counts a failure. Closing closes all in
    order, swallowing per-sink close errors so one bad close doesn't strand
    a later resource."""

    def __init__(self, sinks: list[Sink]) -> None:
        if not sinks:
            raise ValueError("MultiSink requires at least one sink")
        self._sinks = sinks

    def write(self, event: dict[str, Any]) -> None:
        first_error: Exception | None = None
        for s in self._sinks:
            try:
                s.write(event)
            except Exception as e:  # noqa: BLE001 — fan-out isolation
                logger.warning(
                    "baton-proxy %s failed: %s: %s", type(s).__name__, type(e).__name__, e
                )
                if first_error is None:
                    first_error = e
        if first_error is not None:
            raise first_error

    def close(self) -> None:
        for s in self._sinks:
            try:
                s.close()
            except Exception:  # noqa: BLE001 — closing one shouldn't block others
                logger.exception("baton-proxy sink close failed")


def make_sink(url_spec: str, *, api_key: str | None) -> Sink:
    """Build a Sink (or MultiSink) from a comma-separated URL spec.

    Each URL's scheme picks the concrete sink:
      - ``stderr:``                  -> StderrSink
      - ``file:///path/to/x.jsonl``  -> FileSink
      - ``http://``, ``https://``    -> HttpSink (requires api_key)
      - ``s3://bucket/prefix``       -> S3Sink (requires boto3, extra [s3])

    Multiple URLs joined by ``,`` produce a MultiSink. Unsupported schemes
    raise ValueError; HTTP sinks raise if api_key is None — all caught at
    proxy startup, never silently dropped.
    """
    parts = [p.strip() for p in url_spec.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"BATON_EVENT_SINK is empty after parsing: {url_spec!r}")
    sinks = [_make_one(p, api_key=api_key) for p in parts]
    if len(sinks) == 1:
        return sinks[0]
    return MultiSink(sinks)


def _make_one(url: str, *, api_key: str | None) -> Sink:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme
    if scheme == "stderr":
        return StderrSink()
    if scheme == "file":
        if not parsed.path:
            raise ValueError(f"file sink URL is missing a path: {url}")
        return FileSink(parsed.path)
    if scheme in ("http", "https"):
        if api_key is None:
            raise ValueError(f"BATON_API_KEY required for http(s) event sinks (sink: {url})")
        return HttpSink(url, api_key=api_key)
    if scheme == "s3":
        # s3://bucket/prefix -> netloc = bucket, path = /prefix
        if not parsed.netloc:
            raise ValueError(f"s3 sink URL is missing a bucket: {url}")
        return S3Sink(parsed.netloc, parsed.path.lstrip("/"))
    raise ValueError(f"unsupported BATON_EVENT_SINK scheme: {scheme!r} (in {url})")


__all__ = [
    "FileSink",
    "HttpSink",
    "MultiSink",
    "S3Sink",
    "Sink",
    "SplitSink",
    "StderrSink",
    "make_sink",
]

"""Streamable HTTP upstream client for the baton-proxy HTTPS bridge.

Implements the client side of the MCP Streamable HTTP transport (spec
2025-03-26) using only the standard library — baton-proxy ships with zero
runtime dependencies and this preserves that. Each client->server JSON-RPC
message becomes one HTTP POST; the response comes back on that same POST as
either a single JSON body (``application/json``) or a short Server-Sent-Events
stream (``text/event-stream``). We do not open the optional standing GET SSE
channel, so server-initiated messages are out of scope for v0 (see the
``run_http_proxy`` docstring).

Two pieces of transport-level state are threaded automatically:

* **``Mcp-Session-Id``** — captured from the first response that carries it
  (the ``initialize`` response) and echoed on every subsequent request.
* **``MCP-Protocol-Version``** — captured from the negotiated
  ``result.protocolVersion`` in the initialize response and echoed thereafter,
  per the spec's requirement that clients pin the version post-handshake.

Fail-open is the caller's job: ``post`` raises on timeout / connection drop /
non-2xx so ``_run_http_loop`` can emit a synthetic error + a JSON-RPC error to
the client rather than hang. This module never touches stdin/stdout.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("baton_proxy")


# Identify ourselves. urllib's default ``Python-urllib/X.Y`` User-Agent is
# banned outright by Cloudflare's bot filter (HTTP 403, error 1010
# "browser_signature_banned") — and hosted MCP servers commonly sit behind
# Cloudflare (Notion does). Without a real UA the bridge would fail before it
# ever reaches the origin's auth check, EVEN with a valid token. Verified
# against mcp.notion.com: Python-urllib UA → 403, a named UA → 401 (reaches
# origin). Value is filled in at import time from the package version.
def _user_agent() -> str:
    try:
        from baton_proxy import __version__

        return f"baton-proxy/{__version__}"
    except Exception:
        return "baton-proxy"


_USER_AGENT = _user_agent()

# Announce ourselves as an intermediary per RFC 9110 §7.6.3. Unlike a
# transparent forward proxy — which preserves the client's request and adds Via
# — we RE-ORIGINATE: the stdio JSON-RPC client terminates here and we open a
# fresh HTTP POST, so there is no inbound HTTP version to echo. "1.1" is the
# conventional received-protocol placeholder; "baton-proxy" is the intermediary
# pseudonym. Composes with User-Agent (which identifies the originating client)
# rather than replacing it — UA says "who", Via says "an intermediary is here".
_VIA = "1.1 baton-proxy"

# Generous default so a slow-but-healthy upstream (e.g. a database MCP running
# a real query) isn't mistaken for a hang. Overridable via env. The point of an
# explicit timeout is that it is NOT infinite — urllib defaults to no timeout,
# which would hang Claude forever behind a dead upstream.
_DEFAULT_TIMEOUT_S = 60.0

# Fallback protocol version advertised until the initialize response tells us
# what was actually negotiated.
_DEFAULT_PROTOCOL_VERSION = "2025-03-26"

_SESSION_HEADER = "Mcp-Session-Id"
_PROTOCOL_HEADER = "MCP-Protocol-Version"


class StreamableHttpClient:
    """Per-process client to a single Streamable HTTP MCP endpoint.

    Not thread-safe: the HTTP bridge serialises requests (one POST at a time),
    which is what lets the shared ``MessageProcessor`` correlate each response
    to the start it just tracked. Reused across the process lifetime so the
    session id + protocol version persist.
    """

    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._url = url
        self._auth_token = auth_token
        if timeout_s is None:
            timeout_s = _env_timeout()
        self._timeout_s = timeout_s
        self._session_id: str | None = None
        self._protocol_version = _DEFAULT_PROTOCOL_VERSION

    def _headers(self, is_initialize: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Advertise willingness to receive either framing so the server may
            # choose SSE for streamed responses.
            "Accept": "application/json, text/event-stream",
            # Named UA — the urllib default is Cloudflare-banned (see _USER_AGENT).
            "User-Agent": _USER_AGENT,
            # Announce the intermediary hop (see _VIA).
            "Via": _VIA,
        }
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        if self._session_id is not None:
            headers[_SESSION_HEADER] = self._session_id
        # The negotiated protocol version is pinned only after initialize.
        if not is_initialize:
            headers[_PROTOCOL_HEADER] = self._protocol_version
        return headers

    def post(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """POST one JSON-RPC message; return the JSON-RPC responses it produced.

        Returns an empty list for notifications (202 / no body). Raises on
        timeout, connection failure, or a non-2xx status so the caller's
        fail-open path runs.
        """
        method = message.get("method")
        is_initialize = method == "initialize"

        body = json.dumps(message).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=body,
            headers=self._headers(is_initialize),
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=self._timeout_s)
        except urllib.error.HTTPError as e:
            # 4xx/5xx — surface as a failure so the bridge fails open. Read a
            # snippet of the body for the log/error message.
            detail = _safe_read_snippet(e)
            raise RuntimeError(f"upstream returned HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"upstream connection failed: {e.reason}") from e

        with resp:
            # Latch the session id from whichever response first carries it
            # (the initialize response). Header is transport-level, independent
            # of whether the body is JSON or SSE.
            if self._session_id is None:
                sid = resp.headers.get(_SESSION_HEADER)
                if sid:
                    self._session_id = sid

            status = getattr(resp, "status", 200)
            if status == 202:
                # Accepted notification / no response payload.
                return []

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in content_type:
                messages = _parse_sse(resp)
            else:
                messages = _parse_json_body(resp)

        if is_initialize:
            self._capture_protocol_version(messages)
        return messages

    def _capture_protocol_version(self, messages: list[dict[str, Any]]) -> None:
        for msg in messages:
            result = msg.get("result")
            if isinstance(result, dict):
                version = result.get("protocolVersion")
                if isinstance(version, str) and version:
                    self._protocol_version = version
                    return


def _env_timeout() -> float:
    raw = os.environ.get("BATON_UPSTREAM_TIMEOUT")
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        logger.warning("baton-proxy: bad BATON_UPSTREAM_TIMEOUT %r, using default", raw)
        return _DEFAULT_TIMEOUT_S
    # Reject non-finite (inf/nan): urlopen(timeout=inf) blocks forever, which
    # would silently defeat the fail-open guarantee the timeout exists for.
    if not math.isfinite(val) or val <= 0:
        logger.warning(
            "baton-proxy: non-positive/non-finite BATON_UPSTREAM_TIMEOUT %r, using default", raw
        )
        return _DEFAULT_TIMEOUT_S
    return val


def _safe_read_snippet(resp: Any, limit: int = 500) -> str:
    try:
        raw = resp.read(limit)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        return str(raw)
    except Exception:
        return "<unreadable body>"


def _parse_json_body(resp: Any) -> list[dict[str, Any]]:
    """Parse a single ``application/json`` body into a list of JSON-RPC messages.

    A body may be a single object or a JSON-RPC batch (array); normalise to a
    list. A body that isn't a dict/list is dropped with a log (never raised —
    the bridge must not die on a malformed upstream response).
    """
    raw = resp.read()
    if not raw:
        return []
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        logger.warning("baton-proxy: upstream returned non-JSON body, dropping")
        return []
    return _normalize(parsed)


def _parse_sse(resp: Any) -> list[dict[str, Any]]:
    """Collect JSON-RPC messages from an SSE stream on the POST response.

    Reads to end-of-stream (the server closes after delivering the response to
    a client-initiated request). Concatenates multi-line ``data:`` fields per
    the SSE spec; parses each complete frame's data as one JSON-RPC message.
    Malformed frames are skipped, not raised.
    """
    messages: list[dict[str, Any]] = []
    data_lines: list[str] = []
    # SSE default event type when no `event:` field is present is "message".
    event_type = "message"

    def flush() -> None:
        nonlocal event_type
        current = event_type
        event_type = "message"  # reset for the next frame, per the SSE spec
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines.clear()
        # Only `message` events carry JSON-RPC per the MCP Streamable HTTP
        # transport. A server may interleave keepalive/ping/custom event types
        # (with a data payload) on the same stream; parsing those as JSON-RPC
        # would inject a bogus message to the client — skip them.
        if current != "message":
            return
        try:
            parsed = json.loads(payload)
        except ValueError:
            logger.warning("baton-proxy: skipping malformed SSE data frame")
            return
        messages.extend(_normalize(parsed))

    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            # Blank line = end of one event; flush the accumulated data.
            flush()
            continue
        if line.startswith(":"):
            # SSE comment / keep-alive; ignore.
            continue
        field, _, value = line.partition(":")
        # A single leading space after the colon is stripped per the SSE spec.
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event_type = value or "message"
        # Other fields (id, retry) are irrelevant to JSON-RPC payloads.
    # Flush a trailing frame not terminated by a blank line.
    flush()
    return messages


def _normalize(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [m for m in parsed if isinstance(m, dict)]
    logger.warning("baton-proxy: upstream message was not an object/array, dropping")
    return []

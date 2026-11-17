"""``baton-proxy listen``: serve the proxy at a local URL in front of a remote MCP server.

``baton-proxy listen --listen 127.0.0.1:8710 --url https://vendor.example/mcp``
gives an agent harness a URL instead of a command, for setups where the client
cannot start a process (an eval runner, a hosted agent).

Every end-to-end header the caller sends is forwarded unchanged (``_NOT_FORWARDED``
lists the rest), so the listener holds no credential unless
``BATON_UPSTREAM_AUTH_TOKEN`` is set for callers that send no ``Authorization``.
Header values never reach an event. Every request goes to the ``--url`` address
itself, whatever path the client registered, with the client's query added.
A 4xx or 5xx upstream answer goes back to the caller; a redirect is a 502,
since following it would leave the overlay and capture behind.

The listener issues its own ``Mcp-Session-Id`` to each client and maps it to the
upstream's, so clients stay apart even when the upstream is stateless. Each
session gets its own ``MessageProcessor`` (request ids are only unique within a
session) and its own Baton session id.

Requests go out through ``_open`` rather than ``StreamableHttpClient``, which owns
one session and one credential; the listener forwards each caller's own.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import logging
import os
import signal
import sys
import threading
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from baton_proxy import USER_AGENT as _USER_AGENT
from baton_proxy._jsonrpc import answers, owed_answer_id
from baton_proxy._no_redirect import no_redirect_urlopen
from baton_proxy._urls import shown_url
from baton_proxy.emitter import Emitter
from baton_proxy.overlay import Overlay
from baton_proxy.proxy import (
    UPSTREAM_UNREACHABLE_ERROR_TYPE,
    MessageProcessor,
    _bootstrap,
    client_action,
    no_response_error,
    rpc_error,
)
from baton_proxy.sinks import shown_sinks
from baton_proxy.transport_http import (
    POST_HEADERS,
    SESSION_HEADER,
    VIA,
    env_timeout,
    is_event_stream,
    iter_sse,
    read_messages,
)

logger = logging.getLogger("baton_proxy")

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
OVERLAY_HEADER = "Baton-Overlay"
# Each side has its own session id: the listener's to the client, the upstream's to it.
_NEVER_RELAYED = _HOP_BY_HOP | {SESSION_HEADER.lower()}
# Content-Type and Accept are ours because the body is re-serialized and parsed
# here; Accept-Encoding because urllib does not decode a compressed answer.
_NOT_FORWARDED = _NEVER_RELAYED | {"accept", "accept-encoding", "content-type", "host", "via"}
# The body is re-serialized here, so the server's description of its own body
# (type, encoding, digest, validators) no longer fits it. _passed_back also drops
# its CORS grants (access-control-*), since the listener serves no browser.
_NOT_PASSED_BACK = _NEVER_RELAYED | {
    "content-encoding",
    "content-md5",
    "content-range",
    "content-type",
    "date",
    "digest",
    "etag",
    "last-modified",
    "server",
    OVERLAY_HEADER.lower(),
}
_MAX_BODY = 8 * 1024 * 1024
_PROBE_TIMEOUT_S = 5.0
MAX_SESSIONS = 1024
UPSTREAM_REFUSED_ERROR_TYPE = "proxy_upstream_refused"
SESSION_CLOSED_ERROR_TYPE = "proxy_session_closed"
CLIENT_GONE_ERROR_TYPE = "proxy_client_disconnected"


# One outbound call site for SECURITY.md §9's count;
# see test_security_md_section_9_narrow_grep_returns_exactly_its_six.
def _open(req: urllib.request.Request, timeout_s: float) -> Any:
    return no_redirect_urlopen(req, timeout=timeout_s)


class UpstreamUnreachable(Exception):
    """Its text names the failure, never the URL, so it may go into an event."""


class UpstreamRefused(Exception):
    """A non-2xx upstream answer. A 4xx or 5xx goes back to the client with its
    status and body, and its headers filtered, because a 401's ``WWW-Authenticate``
    is how a client finds the authorization server.
    """

    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        super().__init__(f"upstream returned HTTP {status}")
        self.status = status
        self.headers = headers
        self.body = body


def forward_post(
    url: str,
    message: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> Any:
    """POST one message with the caller's headers; return the open 2xx response.

    Raises ``UpstreamRefused`` on a non-2xx and ``UpstreamUnreachable`` when the
    upstream cannot be reached.
    """
    sent = {**POST_HEADERS, **headers}
    req = urllib.request.Request(
        url, data=json.dumps(message).encode("utf-8"), headers=sent, method="POST"
    )
    try:
        resp = _open(req, timeout_s)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        raise UpstreamRefused(e.code, list(e.headers.items()), body) from e
    except urllib.error.URLError as e:
        raise UpstreamUnreachable(f"upstream connection failed: {e.reason}") from e
    return resp


def unreachable_reason(url: str, timeout_s: float = _PROBE_TIMEOUT_S) -> str | None:
    """Why ``url`` looks down, or None if it answered (a 401, 405 or 501 counts)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT}, method="HEAD")
        with _open(req, timeout_s):
            return None
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return f"HTTP {exc.code} to a request with no credential; a redirected call gets a 502"
        # 501 is a server that has no HEAD, which says nothing about its POST.
        return f"HTTP {exc.code}" if exc.code >= 500 and exc.code != 501 else None
    except Exception as exc:
        return type(getattr(exc, "reason", exc)).__name__


def _warn_if_unreachable(url: str) -> None:
    why = unreachable_reason(url)
    if why is not None:
        logger.warning(
            "baton-proxy: the upstream %s did not answer cleanly (%s); calls may fail",
            shown_url(url),
            why,
        )


def forward_delete(url: str, headers: dict[str, str], timeout_s: float) -> None:
    """Ask the upstream to end its session; best effort, never raises."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _USER_AGENT, **headers}, method="DELETE"
        )
        with _open(req, timeout_s):
            pass
    except Exception as exc:
        logger.debug("baton-proxy: upstream DELETE failed: %s", type(exc).__name__)


# A write to a socket the client closed fails with these on macOS, not as a ConnectionError.
_CLIENT_GONE = frozenset({errno.ENOTCONN, errno.EPROTOTYPE})


def _quiet_on_dropped_connections(server: Any) -> None:
    """Log a client dropping its connection at debug, not as a traceback on stderr.

    Patched on the instance: a server subclass would trip
    test_nothing_of_ours_opens_a_listening_port.
    """
    loud = server.handle_error

    def handle_error(request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError) or getattr(exc, "errno", None) in _CLIENT_GONE:
            logger.debug("baton-proxy listen: client connection dropped: %s", type(exc).__name__)
        else:
            loud(request, client_address)

    server.handle_error = handle_error


@dataclass
class _Session:
    processor: Any
    baton_session_id: str
    upstream_id: str | None = None


class _Sessions:
    """Client session id (ours) to session, oldest evicted past ``MAX_SESSIONS``."""

    def __init__(self, make_processor: Any, emitter: Emitter) -> None:
        self._make = make_processor
        self._emitter = emitter
        self._by_id: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.Lock()

    def new(self) -> _Session:
        sid = str(uuid.uuid4())
        return _Session(self._make(sid), sid)

    def add(self, session: _Session) -> None:
        with self._lock:
            self._by_id[session.baton_session_id] = session
            evicted = []
            while len(self._by_id) > MAX_SESSIONS:
                evicted.append(self._by_id.popitem(last=False)[1])
        for old in evicted:
            close(self._emitter, old, "the listener evicted the oldest session")

    def holds(self, session: _Session) -> bool:
        with self._lock:
            return self._by_id.get(session.baton_session_id) is session

    def get(self, sid: str) -> _Session | None:
        with self._lock:
            session = self._by_id.get(sid)
            if session is not None:
                self._by_id.move_to_end(sid)
            return session

    def drop(self, sid: str) -> _Session | None:
        with self._lock:
            return self._by_id.pop(sid, None)

    def all(self) -> list[_Session]:
        with self._lock:
            return list(self._by_id.values())


def close(emitter: Emitter, session: _Session, why: str) -> None:
    with emitter.bound_session(session.baton_session_id):
        session.processor.drain_pending(SESSION_CLOSED_ERROR_TYPE, why)


def _connection_named(headers: Iterable[tuple[str, str]]) -> set[str]:
    """The headers a Connection header names, which are hop-by-hop too."""
    return {
        n.strip().lower() for k, v in headers if k.lower() == "connection" for n in v.split(",")
    }


def _passed_back(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    named = _connection_named(headers)
    return [
        (k, v)
        for k, v in headers
        if (key := k.lower()) not in _NOT_PASSED_BACK
        and key not in named
        and not key.startswith("access-control-")
    ]


def sse_frame(msg: dict[str, Any]) -> bytes:
    return f"event: message\ndata: {json.dumps(msg)}\n\n".encode()


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def upstream_target(base_url: str, request_path: str) -> str:
    """``base_url`` with the request's query joined, unparsed. The request's path is not used."""
    query = request_path.partition("#")[0].partition("?")[2]
    base, _, base_query = base_url.partition("#")[0].partition("?")
    joined = "&".join(q for q in (base_query, query) if q)
    return f"{base}?{joined}" if joined else base


def _has_control_byte(request_path: str) -> bool:
    return any(ord(c) < 0x21 or ord(c) == 0x7F for c in request_path)


def make_server(
    host: str,
    port: int,
    upstream_url: str,
    *,
    emitter: Emitter,
    make_processor: Any,
    fallback_auth_token: str | None = None,
    allowed_origins: frozenset[str] = frozenset(),
    overlay_label: str = "none",
) -> ThreadingHTTPServer:
    if fallback_auth_token and not _is_loopback(host):
        # Anyone who reached the port would act with the operator's credential.
        raise ValueError(
            "BATON_UPSTREAM_AUTH_TOKEN is used for callers that send no Authorization, "
            f"so the listener refuses it on a non-loopback address ({host}). "
            "Have callers send their own token instead."
        )
    sessions = _Sessions(make_processor, emitter)
    timeout_s = env_timeout()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "baton-proxy"

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("baton-proxy listen: " + format, *args)

        def end_headers(self) -> None:
            # Every answer, error and stream says which round it belongs to;
            # see test_every_answer_names_the_overlay_round.
            self.send_header(OVERLAY_HEADER, overlay_label)
            super().end_headers()

        def _upstream_headers(self, session: _Session | None) -> dict[str, str]:
            dropped = _NOT_FORWARDED | _connection_named(self.headers.items())
            out: dict[str, str] = {}
            for name, value in self.headers.items():
                key = name.lower()
                if key in dropped:
                    continue
                sep = "; " if key == "cookie" else ", "
                out[key] = f"{out[key]}{sep}{value}" if key in out else value
            if "authorization" not in out and fallback_auth_token:
                out["authorization"] = f"Bearer {fallback_auth_token}"
            via = ", ".join(self.headers.get_all("Via") or [])
            out["via"] = f"{via}, {VIA}" if via else VIA
            if session is not None and session.upstream_id:
                out[SESSION_HEADER.lower()] = session.upstream_id
            return out

        def _target(self) -> str | None:
            if _has_control_byte(self.path):
                body = b'{"error":"the request target may not hold control bytes"}'
                self._send(400, body, "application/json", close_connection=True)
                return None
            return upstream_target(upstream_url, self.path)

        def _send(
            self,
            status: int,
            body: bytes = b"",
            content_type: str | None = None,
            headers: Sequence[tuple[str, str]] = (),
            close_connection: bool = False,
        ) -> None:
            self.send_response(status)
            if content_type:
                self.send_header("Content-Type", content_type)
            for k, v in headers:
                self.send_header(k, v)
            if close_connection:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _session_headers(self, session: _Session) -> list[tuple[str, str]]:
            # An id we never registered would send the client into a 404 loop.
            held = sessions.holds(session)
            return [(SESSION_HEADER, session.baton_session_id)] if held else []

        def _send_messages(
            self,
            messages: list[dict[str, Any]],
            session: _Session,
            upstream_headers: Sequence[tuple[str, str]] = (),
        ) -> None:
            headers = [*upstream_headers, *self._session_headers(session)]
            if not messages:
                self._send(202, headers=headers)
            elif len(messages) == 1:
                self._send(200, json.dumps(messages[0]).encode(), "application/json", headers)
            else:
                body = b"".join(sse_frame(m) for m in messages)
                self._send(200, body, "text/event-stream", headers)

        def _send_error(self, session: _Session, req_id: Any, message: str) -> None:
            if req_id is None:
                body = json.dumps({"error": f"baton-proxy: {message}"}).encode()
                self._send(502, body, "application/json")
            else:
                error = rpc_error(req_id, -32603, f"baton-proxy: {message}")
                self._send_messages([error], session)

        def _origin_refused(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None or origin in allowed_origins:
                return False
            self._send(403, close_connection=True)
            return True

        def do_GET(self) -> None:
            # No standing server-to-client stream; the spec lets a server refuse it.
            self._send(405, headers=[("Allow", "POST, DELETE")])

        def do_DELETE(self) -> None:
            if self._origin_refused():
                return
            target = self._target()
            if target is None:
                return
            sid = self.headers.get(SESSION_HEADER)
            session = sessions.drop(sid) if sid else None
            if session is None:
                self._send(404)
                return
            # Our session ends whatever the upstream answers, so the client always gets 204.
            try:
                if session.upstream_id:
                    forward_delete(target, self._upstream_headers(session), timeout_s)
            finally:
                close(emitter, session, "the client ended the session")
            self._send(204)

        def do_POST(self) -> None:
            if self._origin_refused():
                return
            target = self._target()
            if target is None:
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > _MAX_BODY:
                self._send(400, close_connection=True)
                return
            try:
                req = json.loads(self.rfile.read(length))
            except ValueError:
                req = None
            if not isinstance(req, dict):
                self._send(400, b'{"error":"one JSON-RPC object per POST"}', "application/json")
                return

            is_initialize = req.get("method") == "initialize"
            sid = self.headers.get(SESSION_HEADER)
            if is_initialize:
                session = sessions.new()
            elif sid is None:
                self._send(400, b'{"error":"Mcp-Session-Id is required"}', "application/json")
                return
            else:
                session = sessions.get(sid)
                if session is None:
                    # The spec's answer to an unknown session id: start a new one.
                    self._send(404)
                    return

            with emitter.bound_session(session.baton_session_id):
                self._handle(req, session, is_initialize, target)

        def _handle(
            self, req: dict[str, Any], session: _Session, is_initialize: bool, target: str
        ) -> None:
            processor = session.processor
            req_id = owed_answer_id(req)
            action = client_action(processor, req)
            if action.forward is None:
                replies = [action.respond] if action.respond is not None else []
                self._send_messages(replies, session)
                return
            forward = action.forward

            try:
                resp = forward_post(target, forward, self._upstream_headers(session), timeout_s)
            except UpstreamRefused as refused:
                processor.synthesize_pending_error(
                    req_id, UPSTREAM_REFUSED_ERROR_TYPE, f"HTTP {refused.status}"
                )
                if refused.status == 404 and session.upstream_id:
                    # The upstream expired its session, so ours is gone too.
                    sessions.drop(session.baton_session_id)
                    close(emitter, session, "the upstream ended it")
                if 300 <= refused.status < 400:
                    logger.warning(
                        "baton-proxy: upstream answered HTTP %d, a redirect; check --url",
                        refused.status,
                    )
                    body = b'{"error":"the upstream redirected, and the listener does not follow"}'
                    self._send(502, body, "application/json")
                    return
                content_type = next(
                    (v for k, v in refused.headers if k.lower() == "content-type"), None
                )
                passed = _passed_back(refused.headers)
                self._send(refused.status, refused.body, content_type, headers=passed)
                return
            except Exception as exc:
                # Other exceptions (urllib's InvalidURL among them) can quote the caller's URL.
                detail = str(exc) if isinstance(exc, UpstreamUnreachable) else type(exc).__name__
                logger.warning("baton-proxy: upstream unreachable: %s", detail)
                processor.synthesize_pending_error(req_id, UPSTREAM_UNREACHABLE_ERROR_TYPE, detail)
                self._send_error(session, req_id, "upstream unreachable")
                return

            with resp:
                upstream_id = resp.headers.get(SESSION_HEADER)
                passed = _passed_back(list(resp.headers.items()))
                status = getattr(resp, "status", 200)
                # An initialize is buffered: its session header depends on its answer.
                if not is_initialize and status != 202 and is_event_stream(resp):
                    self._stream(resp, session, req_id, passed)
                    return
                try:
                    messages = read_messages(resp, req_id)
                except Exception as exc:
                    logger.warning(
                        "baton-proxy: upstream answer unreadable: %s", type(exc).__name__
                    )
                    messages = []

            out = [self._from_upstream(processor, m) for m in messages]
            answer = next((m for m in out if answers(m, req_id)), None)
            if req_id is not None and answer is None:
                out.append(no_response_error(processor, req_id))
            if is_initialize:
                session.upstream_id = upstream_id
                if answer is not None and "result" in answer:
                    sessions.add(session)
                elif upstream_id:
                    # The client gets no session id to end it with, so end it here.
                    forward_delete(target, self._upstream_headers(session), timeout_s)
            self._send_messages(out, session, passed)

        def _from_upstream(self, processor: Any, msg: dict[str, Any]) -> dict[str, Any]:
            try:
                return processor.handle_server_message(msg)
            except Exception:
                logger.exception("baton-proxy: handle_server_message failed")
                return msg

        def _stream(
            self, resp: Any, session: _Session, req_id: Any, passed: list[tuple[str, str]]
        ) -> None:
            """Relay an SSE answer frame by frame, so a server request mid-call reaches
            the client while the call is still open."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            for k, v in [*passed, *self._session_headers(session)]:
                if k.lower() != "cache-control":
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.close_connection = True
            answered = False
            try:
                self.end_headers()
                client_gone = False
            except OSError:
                client_gone = True
            if not client_gone:
                try:
                    for msg in iter_sse(resp):
                        msg = self._from_upstream(session.processor, msg)
                        answered = answers(msg, req_id)
                        if not self._write_event(msg):
                            client_gone = True
                            break
                        if answered:
                            break
                except Exception as exc:
                    logger.warning("baton-proxy: upstream stream broke: %s", type(exc).__name__)
            if req_id is None or answered:
                return
            if client_gone:
                session.processor.synthesize_pending_error(
                    req_id, CLIENT_GONE_ERROR_TYPE, "the client disconnected mid-call"
                )
            else:
                self._write_event(no_response_error(session.processor, req_id))

        def _write_event(self, msg: dict[str, Any]) -> bool:
            """Whether the client still took the frame."""
            try:
                self.wfile.write(sse_frame(msg))
                self.wfile.flush()
                return True
            except OSError:
                return False

    server = ThreadingHTTPServer((host, port), Handler)
    _quiet_on_dropped_connections(server)
    server.daemon_threads = True
    server.baton_sessions = sessions  # type: ignore[attr-defined]
    return server


def parse_listen(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if ":" in host or "[" in host:
        raise ValueError(f"--listen takes an IPv4 address or a host name, got {value!r}")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"--listen wants HOST:PORT with a port from 1 to 65535, got {value!r}")
    return host or "127.0.0.1", int(port)


def run_listener(host: str, port: int, upstream_url: str, overlay_path: str | None = None) -> int:
    config, injection, emitter, template = _bootstrap(overlay_path)
    overlay: Overlay | None = template.overlay

    def make_processor(session_id: str) -> MessageProcessor:
        return MessageProcessor(emitter, injection, session_id, overlay=overlay)

    origins = os.environ.get("BATON_LISTEN_ALLOWED_ORIGINS", "")
    try:
        server = make_server(
            host,
            port,
            upstream_url,
            emitter=emitter,
            make_processor=make_processor,
            fallback_auth_token=os.environ.get("BATON_UPSTREAM_AUTH_TOKEN"),
            allowed_origins=frozenset(o.strip() for o in origins.split(",") if o.strip()),
            overlay_label=overlay.label if overlay else "none",
        )
    except ValueError as exc:
        logger.error("baton-proxy: %s", exc)
        emitter.stop()
        return 2
    except OSError as exc:
        logger.error(
            "baton-proxy: cannot listen on %s:%d: %s. Is another listener running? "
            "Stop it or pass another --listen port.",
            host,
            port,
            exc,
        )
        emitter.stop()
        return 2
    logger.info(
        "baton-proxy listening on http://%s:%d/ (upstream=%s, overlay=%s, tools=%s, "
        "intent_param=%s, events=%s)",
        host,
        port,
        shown_url(upstream_url),
        overlay.label if overlay else "none",
        sorted(injection.names),
        injection.intent_param_mode,
        shown_sinks(config.event_sink),
    )
    signal.signal(signal.SIGTERM, _stop_on_sigterm)
    # Off the main thread, so a slow upstream never holds up serving or Ctrl+C.
    threading.Thread(target=_warn_if_unreachable, args=(upstream_url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # A second stop signal must not cut short closing calls and flushing events.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        server.server_close()
        for session in server.baton_sessions.all():  # type: ignore[attr-defined]
            close(emitter, session, "the listener shut down")
        emitter.stop()
        logger.info("baton-proxy: listener stopped")
    return 0


def _stop_on_sigterm(*_: Any) -> None:
    # A harness or service manager stops with SIGTERM; take the same path as Ctrl+C.
    raise KeyboardInterrupt


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="baton-proxy listen",
        description="Serve a remote MCP server at a local URL, with Baton capture and overlays.",
    )
    parser.add_argument("--listen", required=True, metavar="HOST:PORT")
    parser.add_argument("--url", required=True, help="The MCP server to sit in front of")
    parser.add_argument("--overlay", default=None, metavar="PATH", help="Overlay file to apply")
    args = parser.parse_args(argv)
    try:
        host, port = parse_listen(args.listen)
    except ValueError as exc:
        parser.error(str(exc))
    return run_listener(host, port, args.url, args.overlay)

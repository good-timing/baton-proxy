from __future__ import annotations

import contextlib
import email.message
import errno
import http.client
import http.server
import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fixture_http_server  # noqa: E402

from baton_proxy.listen import (  # noqa: E402
    UpstreamRefused,
    make_server,
    parse_listen,
    run_listener,
    upstream_target,
)
from baton_proxy.overlay import parse_overlay  # noqa: E402
from baton_proxy.proxy import (  # noqa: E402
    NO_RESPONSE_ERROR_TYPE,
    MessageProcessor,
    _Injection,
    shown_url,
)

TOKEN = "tok-123"
OVERLAY = parse_overlay(
    {
        "actions": [
            {"target": {"tool": "echo"}, "replace": "Echo, rewritten."},
            {"target": {"tool": "boom"}, "remove": True},
        ],
    }
)


class _FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, tuple[Any, ...], dict[str, Any]]] = []
        self._bound = threading.local()

    @contextlib.contextmanager
    def bound_session(self, session_id: str):
        self._bound.sid = session_id
        try:
            yield
        finally:
            self._bound.sid = None

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("enqueue_") and name != "set_agent_runtime":
            raise AttributeError(name)
        kind = name.removeprefix("enqueue_")

        def record(*args: Any, **kw: Any) -> None:
            self.calls.append((kind, getattr(self._bound, "sid", None), args, kw))

        return record

    def of(self, kind: str) -> list[tuple[str | None, dict[str, Any]]]:
        return [(sid, kw) for k, sid, _, kw in self.calls if k == kind]


@pytest.fixture(params=["stateful", "stateless"])
def rig(request):
    stateless = request.param == "stateless"
    upstream = fixture_http_server.serve(
        0, require_auth=TOKEN, stateless=stateless, initialize_sse=stateless
    )
    threading.Thread(target=upstream.serve_forever, args=(0.01,), daemon=True).start()
    host, port = upstream.server_address[:2]
    emitter = _FakeEmitter()
    injection = _Injection.create(None)

    def make_processor(sid: str) -> MessageProcessor:
        return MessageProcessor(emitter, injection, sid, overlay=OVERLAY)  # type: ignore[arg-type]

    def start(fallback: str | None = None):
        server = make_server(
            "127.0.0.1",
            0,
            f"http://{host}:{port}/mcp",
            emitter=emitter,  # type: ignore[arg-type]
            make_processor=make_processor,
            fallback_auth_token=fallback,
        )
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        servers.append(server)
        lhost, lport = server.server_address[:2]
        return f"http://{lhost}:{lport}"

    servers: list[Any] = []
    start.servers = servers  # type: ignore[attr-defined]
    yield start, upstream, emitter
    for s in servers:
        s.shutdown()
    upstream.shutdown()


def _post(url: str, msg: dict[str, Any], headers: dict[str, str] | None = None):
    req = urllib.request.Request(
        url,
        data=json.dumps(msg).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers), json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _session(url: str, auth: str | None = f"Bearer {TOKEN}") -> dict[str, str]:
    headers = {"Authorization": auth} if auth else {}
    status, resp_headers, _ = _post(
        url,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
        headers,
    )
    assert status == 200
    headers["Mcp-Session-Id"] = resp_headers["Mcp-Session-Id"]
    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
    return headers


def test_a_harness_sees_the_overlaid_surface_through_the_url(rig):
    start, upstream, emitter = rig
    url = start()
    headers = _session(url)

    status, _, body = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    assert status == 200
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert tools["echo"]["description"] == "Echo, rewritten."
    assert "boom" not in tools and "softfail" in tools
    assert "user_goal" in tools["echo"]["inputSchema"]["properties"]

    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hi", "user_goal": "say hi"}},
    }
    status, _, body = _post(url, call, headers)
    assert status == 200 and "result" in body
    ((call_sid, start_kw),) = emitter.of("tool_call_start")
    assert start_kw["call_intent"] == "say hi"
    assert len(emitter.of("tool_call_end")) == 1
    ((list_sid, _),) = emitter.of("tool_list_start")
    assert call_sid == list_sid
    ((_, snapshot),) = emitter.of("surface_snapshot")
    assert snapshot["server_info"] is not None


def test_the_callers_token_reaches_the_vendor_unchanged(rig):
    start, upstream, _ = rig
    url = start()
    _session(url)
    assert upstream.authorizations and set(upstream.authorizations) == {f"Bearer {TOKEN}"}


def test_a_refusal_goes_back_to_the_caller_whole(rig):
    start, _, emitter = rig
    url = start()
    status, _, _ = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {})
    assert status == 401


def test_the_fallback_token_is_used_only_when_the_caller_sends_none(rig):
    start, upstream, _ = rig
    url = start(fallback=TOKEN)
    _session(url, auth=None)
    status, _, _ = _post(
        url,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"Authorization": "Bearer wrong"},
    )
    assert status == 401
    assert upstream.authorizations[-1] == "Bearer wrong"


def test_a_hidden_tool_never_reaches_the_vendor(rig):
    start, upstream, _ = rig
    url = start()
    headers = _session(url)
    call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "boom"}}
    before = len(upstream.authorizations)
    _, _, body = _post(url, call, headers)
    assert body["error"]["message"] == "Unknown tool: boom"
    assert len(upstream.authorizations) == before


def test_each_session_gets_its_own_baton_session(rig):
    start, upstream, emitter = rig
    url = start()
    for headers in (_session(url), _session(url)):
        _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    sids = {sid for sid, _ in emitter.of("tool_list_start")}
    assert len(sids) == 2 and None not in sids


def test_an_unknown_session_is_told_to_start_again(rig):
    start, upstream, _ = rig
    url = start()
    status, _, _ = _post(
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"Authorization": f"Bearer {TOKEN}", "Mcp-Session-Id": "never-issued"},
    )
    assert status == 404


def test_two_clients_of_a_stateless_upstream_stay_apart(rig):
    start, _, emitter = rig
    url = start()
    a, b = _session(url), _session(url)
    assert a["Mcp-Session-Id"] != b["Mcp-Session-Id"]
    for headers in (a, b, a):
        _post(url, {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, headers)
    sids = [sid for sid, _ in emitter.of("tool_list_start")]
    assert sids[0] == sids[2] != sids[1]


def test_a_call_without_a_session_is_refused(rig):
    start, _, _ = rig
    url = start()
    status, _, _ = _post(
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400


def test_closing_a_session_files_its_open_calls_under_it(rig, monkeypatch):
    monkeypatch.setattr(fixture_http_server, "_SLOW_SLEEP_S", 0.3)
    start, _, emitter = rig
    url = start()
    headers = _session(url)
    slow = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "slow"}}
    t = threading.Thread(target=_post, args=(url, slow, headers))
    t.start()
    deadline = time.monotonic() + 2
    while not emitter.of("tool_call_start") and time.monotonic() < deadline:
        time.sleep(0.02)
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    with urllib.request.urlopen(req, timeout=5):
        pass
    t.join()
    ((start_sid, _),) = emitter.of("tool_call_start")
    closed = [
        sid
        for sid, kw in emitter.of("tool_call_error")
        if kw["error_type"] == "proxy_session_closed"
    ]
    assert closed == [start_sid]


def test_a_browser_origin_is_refused(rig):
    start, _, _ = rig
    url = start()
    status, _, _ = _post(
        url,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"},
    )
    assert status == 403


def test_the_fallback_token_is_refused_off_loopback():
    with pytest.raises(ValueError, match="non-loopback"):
        make_server(
            "0.0.0.0",
            0,
            "http://127.0.0.1:1/mcp",
            emitter=_FakeEmitter(),  # type: ignore[arg-type]
            make_processor=lambda sid: None,
            fallback_auth_token="x",
        )


def test_a_malformed_length_gets_a_400(rig):
    start, _, _ = rig
    url = start()
    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.putrequest("POST", u.path)
    conn.putheader("Content-Length", "abc")
    conn.endheaders()
    assert conn.getresponse().status == 400


SENTINEL = "s3ntinel-7f2c"


def _upstream_headers(upstream) -> dict[str, str]:
    return {k.lower(): v for k, v in upstream.requests[-1][1]}


def test_every_end_to_end_header_reaches_the_server_unchanged(rig):
    start, upstream, _ = rig
    url = start()
    _session(url)
    _raw_request_line(
        url,
        "/",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "Authorization": f"Bearer {TOKEN}",
            "x-api-key": "key-1",
            "X-Tenant-Id": "tenant-9",
            "User-Agent": "harness/2.1",
            "Via": "1.1 harness-gw",
            "Connection": "keep-alive, X-Hop",
            "X-Hop": "drop me",
            "Proxy-Authorization": "Basic drop",
            "Accept-Encoding": "gzip",
        },
    )
    got = _upstream_headers(upstream)
    assert got["x-api-key"] == "key-1"
    assert got["x-tenant-id"] == "tenant-9"
    assert got["user-agent"] == "harness/2.1"
    assert got["via"] == "1.1 harness-gw, 1.1 baton-proxy"
    assert got["host"] == "{}:{}".format(*upstream.server_address[:2])
    assert "x-hop" not in got and "proxy-authorization" not in got
    assert got.get("accept-encoding", "identity") == "identity"


@pytest.mark.parametrize("path", ["", "/", "/mcp", "/beta,stable", "/../admin"])
def test_every_registered_path_reaches_the_server_url_itself(rig, path):
    start, upstream, _ = rig
    _session(start() + path + "?tags=beta,stable&x=%2F")
    assert {p for p, _ in upstream.requests} == {"/mcp?tags=beta,stable&x=%2F"}


@pytest.mark.parametrize(
    ("base", "request_path", "want"),
    [
        ("https://h/mcp", "/", "https://h/mcp"),
        ("https://h/mcp/", "/mcp", "https://h/mcp/"),
        ("https://h/mcp?a=1", "/beta?b=2", "https://h/mcp?a=1&b=2"),
        ("https://h/mcp", "/?b=2", "https://h/mcp?b=2"),
        ("https://h/mcp#x", "/?b=2", "https://h/mcp?b=2"),
        ("https://h/mcp", "/?b=2#x&c=3", "https://h/mcp?b=2"),
    ],
)
def test_upstream_target(base, request_path, want):
    assert upstream_target(base, request_path) == want


def test_no_header_or_query_value_reaches_an_event(rig):
    start, upstream, emitter = rig
    url = start() + f"/?tag={SENTINEL}-q"
    marked = {
        "x-api-key": f"{SENTINEL}-key",
        "X-Tenant-Id": f"{SENTINEL}-tenant",
        "User-Agent": f"{SENTINEL}-ua",
        "Cookie": f"sid={SENTINEL}-cookie",
    }
    headers = {**_session(url), **marked}
    _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hi", "user_goal": "1: say hi"}},
    }
    _post(url, call, headers)

    seen_upstream = json.dumps(upstream.requests[-1])
    for value in [*marked.values(), f"{SENTINEL}-q"]:
        assert value in seen_upstream
    kinds = {kind for kind, *_ in emitter.calls}
    assert {"tool_list_start", "tool_call_start", "tool_call_end"} <= kinds
    assert SENTINEL not in json.dumps(emitter.calls, default=repr)


def test_the_listen_command_serves_only_the_flags_overlay(tmp_path: Path):
    upstream = fixture_http_server.serve(0, require_auth=TOKEN)
    threading.Thread(target=upstream.serve_forever, args=(0.01,), daemon=True).start()
    uhost, uport = upstream.server_address[:2]

    def overlay(text: str) -> str:
        path = tmp_path / f"{text}.json"
        action = {"target": {"tool": "echo"}, "replace": text}
        path.write_text(json.dumps({"actions": [action]}))
        return str(path)

    port = fixture_http_server.free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        PYTHONPATH=str(Path(__file__).parent.parent / "src"),
        BATON_EVENT_SINK="stderr:",
        BATON_OVERLAY=overlay("from-env"),
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "baton_proxy",
            "listen",
            "--listen",
            f"127.0.0.1:{port}",
            "--url",
            f"http://{uhost}:{uport}/mcp",
            "--overlay",
            overlay("from-flag"),
        ],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while True:
            try:
                headers = _session(url)
                break
            except (urllib.error.URLError, ConnectionError):
                assert time.monotonic() < deadline and proc.poll() is None
                time.sleep(0.05)
        _, _, body = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
        tools = {t["name"]: t for t in body["result"]["tools"]}
        assert tools["echo"]["description"] == "from-flag"
    finally:
        proc.terminate()
        _, stderr = proc.communicate(timeout=10)
        upstream.shutdown()
    assert "BATON_OVERLAY is ignored" in stderr
    assert "listener stopped" in stderr


def _raw_request_line(
    url: str,
    target: str,
    msg: dict[str, Any],
    headers: dict[str, str] | list[tuple[str, str]],
) -> int:
    """http.client refuses control bytes in a target; a caller's socket does not."""
    u = urlparse(url)
    body = json.dumps(msg).encode()
    lines = [f"POST {target} HTTP/1.1", f"Host: {u.netloc}", f"Content-Length: {len(body)}"]
    items = headers.items() if isinstance(headers, dict) else headers
    lines += [f"{k}: {v}" for k, v in items]
    with socket.create_connection((u.hostname, u.port), timeout=10) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        return int(s.recv(64).split()[1])


def test_a_query_the_server_url_cannot_carry_never_reaches_an_event(rig):
    start, upstream, emitter = rig
    url = start()
    headers = _session(url)
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hi", "user_goal": "1: hi"}},
    }
    status = _raw_request_line(url, f"/?t={SENTINEL}\x7f", call, headers)
    assert status == 400
    assert SENTINEL not in json.dumps(emitter.calls, default=repr)
    assert all(SENTINEL not in path for path, _ in upstream.requests)


def test_repeated_cookie_headers_are_joined_as_one_cookie_header(rig):
    start, upstream, _ = rig
    url = start()
    headers = list(_session(url).items()) + [("Cookie", "a=1"), ("Cookie", "b=2")]
    _raw_request_line(url, "/", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    assert _upstream_headers(upstream)["cookie"] == "a=1; b=2"


def test_a_request_the_server_never_answers_gets_an_error_and_closes_its_start(rig):
    start, _, emitter = rig
    url = start()
    headers = _session(url)
    call = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "noresponse"}}
    status, _, body = _post(url, call, headers)
    assert status == 200 and body["id"] == 8 and body["error"]["code"] == -32001
    errors = [kw["error_type"] for _, kw in emitter.of("tool_call_error")]
    assert errors == [NO_RESPONSE_ERROR_TYPE]


def test_a_failed_initialize_issues_no_session_id():
    dead = fixture_http_server.free_port()
    emitter = _FakeEmitter()
    injection = _Injection.create(None)
    server = make_server(
        "127.0.0.1",
        0,
        f"http://127.0.0.1:{dead}/mcp",
        emitter=emitter,  # type: ignore[arg-type]
        make_processor=lambda sid: MessageProcessor(emitter, injection, sid),  # type: ignore[arg-type]
    )
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    try:
        host, port = server.server_address[:2]
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        status, headers, body = _post(f"http://{host}:{port}", init)
        assert "error" in body
        assert "Mcp-Session-Id" not in headers
    finally:
        server.shutdown()


def test_a_processor_failure_errors_the_caller_and_forwards_nothing(rig):
    start, upstream, _ = rig
    url = start()
    headers = _session(url)
    (session,) = start.servers[0].baton_sessions.all()
    session.processor.handle_client_message = _raise
    before = len(upstream.requests)
    call = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo"}}
    _, _, body = _post(url, call, headers)
    assert body["error"]["code"] == -32603
    assert len(upstream.requests) == before


class _Answer(io.BytesIO):
    def __init__(self, session_id: str | None, body: bytes, content_type: str) -> None:
        super().__init__(body)
        self.status = 200 if body else 202
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id


def _answer(session_id: str | None, messages: list[dict[str, Any]], sse: bool = False) -> _Answer:
    if sse:
        body = "".join(f"event: message\ndata: {json.dumps(m)}\n\n" for m in messages).encode()
        return _Answer(session_id, body, "text/event-stream")
    body = json.dumps(messages[0]).encode() if messages else b""
    return _Answer(session_id, body, "application/json")


class _HeldOpen(_Answer):
    """An SSE answer whose server keeps the stream open after its last frame."""

    def __iter__(self):
        yield from iter(self.readline, b"")
        raise AssertionError("read past the answer on a stream the server left open")


def _held_open(session_id: str | None, messages: list[dict[str, Any]]) -> _HeldOpen:
    sent = _answer(session_id, messages, sse=True)
    return _HeldOpen(session_id, sent.getvalue(), "text/event-stream")


def _raise(*_: Any) -> None:
    raise RuntimeError("processor bug")


def test_a_busy_port_exits_2_with_a_reason(monkeypatch, caplog):
    monkeypatch.setenv("BATON_EVENT_SINK", "stderr:")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with caplog.at_level(logging.ERROR, logger="baton_proxy"):
            rc = run_listener("127.0.0.1", port, "http://127.0.0.1:1/mcp")
    assert rc == 2
    assert any(f"cannot listen on 127.0.0.1:{port}" in r.getMessage() for r in caplog.records)
    assert any("pass another --listen port" in r.getMessage() for r in caplog.records)


def test_a_client_response_is_forwarded_and_never_answered_or_closed(rig, monkeypatch):
    start, _, emitter = rig
    url = start()
    headers = _session(url)
    pending = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo"}}
    (session,) = start.servers[0].baton_sessions.all()
    session.processor.handle_client_message(pending)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "baton_proxy.listen.forward_post",
        lambda _t, msg, *a: (sent.append(msg), _answer(None, []))[1],
    )
    status, _, body = _post(url, {"jsonrpc": "2.0", "id": 5, "result": {}}, headers)
    assert sent == [{"jsonrpc": "2.0", "id": 5, "result": {}}]
    assert status == 202 and not body
    assert emitter.of("tool_call_error") == []


def test_an_initialize_the_server_never_answers_issues_no_session_id(rig, monkeypatch):
    start, _, _ = rig
    url = start()
    monkeypatch.setattr(
        "baton_proxy.listen.forward_post", lambda *a, **kw: _answer("upstream-sid", [])
    )
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    _, headers, body = _post(url, init, {"Authorization": f"Bearer {TOKEN}"})
    assert body["error"]["code"] == -32001
    assert "Mcp-Session-Id" not in headers
    assert start.servers[0].baton_sessions.all() == []


def test_a_server_request_with_the_same_id_is_not_the_answer(rig, monkeypatch):
    start, _, emitter = rig
    url = start()
    headers = _session(url)
    sampling = {"jsonrpc": "2.0", "id": 4, "method": "sampling/createMessage", "params": {}}
    monkeypatch.setattr(
        "baton_proxy.listen.forward_post", lambda *a, **kw: _answer(None, [sampling], sse=True)
    )
    call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo"}}
    req = urllib.request.Request(url, data=json.dumps(call).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        stream = resp.read().decode()
    assert '"code": -32001' in stream
    errors = [kw["error_type"] for _, kw in emitter.of("tool_call_error")]
    assert errors == [NO_RESPONSE_ERROR_TYPE]


@pytest.mark.parametrize("value", ["[::1]:8710", "::1:8710"])
def test_an_ipv6_listen_address_is_refused(value):
    with pytest.raises(ValueError, match="IPv4"):
        parse_listen(value)


@pytest.mark.parametrize("value", ["127.0.0.1:70000", "127.0.0.1:0", "127.0.0.1:x"])
def test_a_port_out_of_range_is_refused(value):
    with pytest.raises(ValueError, match="1 to 65535"):
        parse_listen(value)


def test_a_rejected_initialize_registers_no_session_and_ends_the_servers(rig, monkeypatch):
    start, _, _ = rig
    url = start()
    rejected = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "no"}}
    monkeypatch.setattr(
        "baton_proxy.listen.forward_post", lambda *a, **kw: _answer("upstream-sid", [rejected])
    )
    ended: list[dict[str, str]] = []
    monkeypatch.setattr(
        "baton_proxy.listen.forward_delete", lambda _t, headers, _s: ended.append(headers)
    )
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    _, headers, body = _post(url, init, {"Authorization": f"Bearer {TOKEN}"})
    assert body["error"]["code"] == -32602
    assert "Mcp-Session-Id" not in headers
    assert start.servers[0].baton_sessions.all() == []
    assert [h["mcp-session-id"] for h in ended] == ["upstream-sid"]


def test_a_server_request_mid_call_reaches_the_client_before_the_call_ends(rig):
    start, upstream, _ = rig
    url = start()
    headers = _session(url)
    call = {"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "elicits"}}
    req = urllib.request.Request(url, data=json.dumps(call).encode(), headers=headers)
    began = time.monotonic()
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert "text/event-stream" in resp.headers["Content-Type"]
        frames = []
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            msg = json.loads(line.removeprefix("data:"))
            frames.append(msg)
            if msg.get("method") == "elicitation/create":
                answer = {"jsonrpc": "2.0", "id": msg["id"], "result": {"action": "accept"}}
                status, _, _ = _post(url, answer, headers)
                assert status == 202
    assert time.monotonic() - began < 4
    assert upstream.elicit_answer == {
        "jsonrpc": "2.0",
        "id": "srv-elicit-1",
        "result": {"action": "accept"},
    }
    assert [m.get("method") or m.get("id") for m in frames] == ["elicitation/create", 21]


def test_the_servers_cookie_reaches_the_client(rig):
    start, _, _ = rig
    url = start()
    headers = _session(url)
    _, got, _ = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    assert got["Set-Cookie"] == "lb=backend-2; Path=/"


def test_a_redirect_is_a_502_and_carries_nothing_on():
    seen: list[str | None] = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_POST = do_GET

    other = http.server.HTTPServer(("127.0.0.1", 0), Elsewhere)
    threading.Thread(target=other.serve_forever, args=(0.01,), daemon=True).start()

    class Redirects(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{other.server_port}/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirects)
    threading.Thread(target=redirector.serve_forever, args=(0.01,), daemon=True).start()
    try:
        server = make_server(
            "127.0.0.1",
            0,
            f"http://127.0.0.1:{redirector.server_port}/mcp",
            emitter=_FakeEmitter(),  # type: ignore[arg-type]
            make_processor=lambda sid: MessageProcessor(
                _FakeEmitter(),
                _Injection.create(None),
                sid,  # type: ignore[arg-type]
            ),
        )
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        conn.request("POST", "/", json.dumps(init), {"Authorization": "Bearer t0k"})
        resp = conn.getresponse()
        status, headers = resp.status, dict(resp.getheaders())
        conn.close()
        server.shutdown()
    finally:
        other.shutdown()
        redirector.shutdown()
    assert status == 502 and "Location" not in headers
    assert seen == []


def test_headers_that_describe_the_servers_own_body_are_not_passed_back():
    from baton_proxy.listen import _passed_back

    kept = _passed_back(
        [
            ("Set-Cookie", "lb=1"),
            ("ETag", '"x"'),
            ("Content-Encoding", "gzip"),
            ("Access-Control-Allow-Origin", "*"),
            ("Connection", "X-Hop"),
            ("X-Hop", "1"),
            ("Retry-After", "2"),
            ("Baton-Overlay", "upstream's own"),
        ]
    )
    assert kept == [("Set-Cookie", "lb=1"), ("Retry-After", "2")]


class _Gone(io.BytesIO):
    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self.writes = 0
        self.fail_after = fail_after

    def write(self, data: Any) -> int:
        self.writes += 1
        if self.writes > self.fail_after:
            raise BrokenPipeError
        return super().write(data)


def _stream_through(
    wfile: io.BytesIO, frames: list[dict[str, Any]], req_id: Any = 7, held_open: bool = False
):
    """Run the listener's SSE relay against ``frames`` with a client writing into ``wfile``."""
    emitter = _FakeEmitter()
    processor = MessageProcessor(emitter, _Injection.create(None), "s")  # type: ignore[arg-type]
    processor.handle_client_message(
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": "echo"}}
    )
    server = make_server(
        "127.0.0.1",
        0,
        "http://127.0.0.1:1/mcp",
        emitter=emitter,  # type: ignore[arg-type]
        make_processor=lambda sid: processor,
    )
    server.server_close()
    handler = object.__new__(server.RequestHandlerClass)
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST / HTTP/1.1"
    handler.command = "POST"
    handler.client_address = ("127.0.0.1", 0)
    handler.wfile = wfile
    session = type("S", (), {"processor": processor, "baton_session_id": "s"})()
    upstream = _held_open(None, frames) if held_open else _answer(None, frames, sse=True)
    passed = [("Cache-Control", "max-age=60"), ("Set-Cookie", "lb=1")]
    handler._stream(upstream, session, req_id, passed)
    return emitter


def test_a_client_gone_before_the_stream_starts_closes_the_call_as_its_doing():
    emitter = _stream_through(_Gone(fail_after=0), [{"jsonrpc": "2.0", "id": 7, "result": {}}])
    assert [kw["error_type"] for _, kw in emitter.of("tool_call_error")] == [
        "proxy_client_disconnected"
    ]


def test_a_client_gone_mid_stream_is_not_blamed_on_the_server():
    frames = [{"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}] * 3
    emitter = _stream_through(_Gone(fail_after=2), frames)
    assert [kw["error_type"] for _, kw in emitter.of("tool_call_error")] == [
        "proxy_client_disconnected"
    ]


def test_a_stream_that_ends_unanswered_gets_the_no_response_error():
    out = io.BytesIO()
    emitter = _stream_through(out, [{"jsonrpc": "2.0", "method": "notifications/progress"}])
    assert b'"code": -32001' in out.getvalue()
    assert [kw["error_type"] for _, kw in emitter.of("tool_call_error")] == [NO_RESPONSE_ERROR_TYPE]


def test_a_streamed_answer_carries_one_cache_control():
    out = io.BytesIO()
    _stream_through(out, [{"jsonrpc": "2.0", "id": 7, "result": {}}])
    head = out.getvalue().split(b"\r\n\r\n")[0].decode().lower()
    assert head.count("cache-control") == 1 and "set-cookie: lb=1" in head


def test_loading_the_event_sinks_does_not_load_the_mcp_http_transport():
    code = "import sys, baton_proxy.sinks; print('baton_proxy.transport_http' in sys.modules)"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent / "src")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.stdout.strip() == "False"


def test_a_message_that_never_reached_the_upstream_is_not_accepted(rig, monkeypatch):
    start, _, _ = rig
    url = start()
    headers = _session(url)

    def unreachable(*_a, **_kw):
        raise ConnectionResetError

    monkeypatch.setattr("baton_proxy.listen.forward_post", unreachable)
    for msg in ({"jsonrpc": "2.0", "id": 7, "result": {}}, {"jsonrpc": "2.0", "method": "n"}):
        status, _, _ = _post(url, msg, headers)
        assert status == 502


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("https://u:p@h:8443/mcp", "<not shown>"),
        ("https://h:8443/mcp/sk-SECRET?api_key=k#f", "https://h:8443"),
        ("https://[::1]:8443/mcp", "https://[::1]:8443"),
        ("https://h:99999/mcp", "<not shown>"),
        ("https://h:0/mcp", "https://h:0"),
        ("https://sk-KEY:a/b@h/mcp", "<not shown>"),
        ("https://[::1/mcp", "<not shown>"),
        ("not a url", "<not shown>"),
    ],
)
def test_shown_url_shows_scheme_host_and_port_or_nothing(url, want):
    assert shown_url(url) == want


@pytest.mark.parametrize("upstream_id", ["upstream-sid", None])
def test_an_upstream_404_ends_only_a_session_the_upstream_issued(rig, monkeypatch, upstream_id):
    start, _, emitter = rig
    url = start()
    headers = _session(url)
    (session,) = start.servers[0].baton_sessions.all()
    session.upstream_id = upstream_id
    in_flight = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo"}}
    session.processor.handle_client_message(in_flight)

    def not_found(*_a, **_kw):
        raise UpstreamRefused(404, [], b"")

    monkeypatch.setattr("baton_proxy.listen.forward_post", not_found)
    status, _, _ = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers)
    closed = [
        kw for _, kw in emitter.of("tool_call_error") if kw["error_type"] == "proxy_session_closed"
    ]
    held = start.servers[0].baton_sessions.all()
    assert status == 404
    if upstream_id:
        assert held == [] and len(closed) == 1
    else:
        assert held == [session] and closed == []


def test_an_sse_initialize_left_open_still_starts_the_session(rig, monkeypatch):
    start, _, _ = rig
    url = start()
    answer = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26"}}
    monkeypatch.setattr(
        "baton_proxy.listen.forward_post", lambda *a, **kw: _held_open("upstream-sid", [answer])
    )
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    _, headers, body = _post(url, init, {"Authorization": f"Bearer {TOKEN}"})
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert "Mcp-Session-Id" in headers


def test_a_streamed_call_ends_at_its_answer():
    out = io.BytesIO()
    frames = [
        {"jsonrpc": "2.0", "id": 7, "result": {}},
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
    ]
    emitter = _stream_through(out, frames, held_open=True)
    assert b'"result": {}' in out.getvalue()
    assert b"notifications/progress" not in out.getvalue()
    assert emitter.of("tool_call_error") == []


def test_a_hidden_tool_called_without_an_id_gets_no_reply_and_never_reaches_the_vendor(rig):
    start, upstream, _ = rig
    url = start()
    headers = _session(url)
    call = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "boom"}}
    before = len(upstream.authorizations)
    status, _, body = _post(url, call, headers)
    assert status == 202 and not body
    assert len(upstream.authorizations) == before


def test_a_dropped_client_connection_prints_no_traceback(capsys):
    server = make_server(
        "127.0.0.1",
        0,
        "http://127.0.0.1:1/mcp",
        emitter=_FakeEmitter(),  # type: ignore[arg-type]
        make_processor=lambda sid: None,
    )
    server.server_close()
    for dropped in (ConnectionResetError(), OSError(errno.EPROTOTYPE, "Protocol wrong type")):
        try:
            raise dropped
        except OSError:
            server.handle_error(None, ("127.0.0.1", 0))
    assert capsys.readouterr().err == ""
    try:
        raise PermissionError("a real bug")
    except PermissionError:
        server.handle_error(None, ("127.0.0.1", 0))
    assert "a real bug" in capsys.readouterr().err


def test_the_url_bridge_stops_at_an_answer_on_a_stream_left_open(monkeypatch):
    from baton_proxy.transport_http import StreamableHttpClient

    answer = {"jsonrpc": "2.0", "id": 3, "result": {}}
    monkeypatch.setattr(
        "baton_proxy.transport_http.no_redirect_urlopen",
        lambda *a, **kw: _held_open(None, [answer]),
    )
    client = StreamableHttpClient("http://127.0.0.1:1/mcp")
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo"}}
    assert client.post(call) == [answer]


def test_the_injected_tool_called_without_an_id_gets_no_reply(rig):
    start, upstream, _ = rig
    url = start()
    headers = _session(url)
    call = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "baton_annotate", "arguments": {"note": "x"}},
    }
    before = len(upstream.authorizations)
    status, _, body = _post(url, call, headers)
    assert status == 202 and not body
    assert len(upstream.authorizations) == before


class _AnswersHead(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_HEAD(self) -> None:
        self.send_response(self.status)
        if 300 <= self.status < 400:
            self.send_header("Location", "http://elsewhere.invalid/")
        self.end_headers()

    def log_message(self, *_: Any) -> None:
        pass


@pytest.mark.parametrize(
    "status, want",
    [(401, None), (405, None), (302, "a redirected call gets a 502"), (503, "HTTP 503")],
)
def test_the_startup_probe_counts_an_answer_and_flags_a_redirect_or_an_outage(status, want):
    from baton_proxy.listen import unreachable_reason

    handler = type("H", (_AnswersHead,), {"status": status})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    try:
        why = unreachable_reason(f"http://127.0.0.1:{server.server_address[1]}/mcp")
    finally:
        server.shutdown()
    assert why == want if want is None else want in (why or "")


@pytest.mark.parametrize(
    "url, want",
    [
        (f"http://127.0.0.1:{fixture_http_server.free_port()}/mcp", "ConnectionRefusedError"),
        ("mcp.example.com/mcp?key=s3cret", "ValueError"),
    ],
)
def test_the_startup_probe_names_a_failure_by_type_only(url, want):
    from baton_proxy.listen import unreachable_reason

    assert unreachable_reason(url) == want

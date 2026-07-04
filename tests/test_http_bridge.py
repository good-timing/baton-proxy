"""End-to-end tests for the baton-proxy HTTPS bridge (Streamable HTTP transport).

Drives a real MCP session through the proxy's ``--url`` mode against the
localhost Streamable HTTP fixture (``fixture_http_server.py``), then asserts:

* the same friction event stream the stdio path produces (A1 lifecycle +
  annotations) — proving the shared ``MessageProcessor`` behaves identically
  whichever transport feeds it;
* both response framings work — ``resources/read`` + ``prompts/get`` come back
  as SSE from the fixture, everything else as a JSON body;
* injection round-trips over HTTP (initialize instructions + injected tools);
* Bearer auth is threaded from ``BATON_UPSTREAM_AUTH_TOKEN``;
* the ``Mcp-Session-Id`` handshake works (the fixture 409s if the proxy fails
  to echo it, which would collapse the whole session into fail-open errors);
* fail-open holds when the upstream is unreachable — Claude gets a JSON-RPC
  error and a synthetic ``tool_call_error`` is emitted, no hang.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import fixture_http_server  # noqa: E402

HERE = Path(__file__).parent
REPO = HERE.parent

# --- Request sequence: mirrors test_a1_emit_e2e so both transports are held to
#     the same expected event stream. resources/read + prompts/get exercise the
#     SSE framing; the rest exercise the JSON-body framing. --------------------

REQUESTS: list[dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    # tools/list — exercises injection of baton_annotate into the tool catalogue
    # over HTTP (rides the same _inject_into_response path as initialize).
    {"jsonrpc": "2.0", "id": 12, "method": "tools/list", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "intent": "Read a resource to verify the HTTP bridge captures resource lifecycle events",
                "expected_outcome": "Resource content returned; proxy emits resource_read_start + resource_read_end",
                "workflow": "HTTP bridge A1 validation",
            },
        },
    },
    {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
    # SSE-framed happy read
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "resources/read",
        "params": {"uri": "fixture://notes.txt"},
    },
    # SSE-framed error read
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "resources/read",
        "params": {"uri": "fixture://secret.txt"},
    },
    {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "signal_type": "failure",
                "intent": "Read a resource to verify the HTTP bridge captures resource lifecycle events",
                "suggested_improvement": "Reading a nonexistent URI returns -32002 with no enumeration hint.",
            },
        },
    },
    {"jsonrpc": "2.0", "id": 7, "method": "prompts/list", "params": {}},
    # SSE-framed happy get
    {"jsonrpc": "2.0", "id": 8, "method": "prompts/get", "params": {"name": "summarize"}},
    # SSE-framed error get
    {"jsonrpc": "2.0", "id": 9, "method": "prompts/get", "params": {"name": "boom_prompt"}},
    # JSON-framed tool calls
    {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "over http"}},
    },
    {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {"name": "boom", "arguments": {}},
    },
]


def _drive_proxy(
    url: str, requests: list[dict[str, Any]], env_extra: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the proxy in --url mode, feed requests on stdin, return (stdout, events).

    ``stdout`` is the list of JSON-RPC messages the proxy wrote back to the
    client; ``events`` is the friction event stream parsed from stderr.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "BATON_VENDOR_ID": "acme",
            "BATON_EVENT_SINK": "stderr:",
        }
    )
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--url", url],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    input_data = "".join(json.dumps(req) + "\n" for req in requests)
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError("proxy did not exit — fail-open likely hung") from None

    stdout_msgs: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            stdout_msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    events: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "event_type" in msg:
            events.append(msg)
    return stdout_msgs, events


def _start_server(
    require_auth: str | None = None,
    *,
    stateless: bool = False,
    initialize_sse: bool = False,
) -> tuple[Any, str]:
    httpd = fixture_http_server.serve(
        0, require_auth=require_auth, stateless=stateless, initialize_sse=initialize_sse
    )
    host, port = httpd.server_address[:2]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, f"http://{host}:{port}/mcp"


@pytest.fixture
def http_server():
    """Start the localhost Streamable HTTP fixture on an ephemeral port."""
    httpd, url = _start_server()
    try:
        yield url
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def http_server_auth():
    """Start a fixture that requires ``Authorization: Bearer s3cr3t`` on every POST."""
    httpd, url = _start_server(require_auth="s3cr3t")
    try:
        yield url
    finally:
        httpd.shutdown()
        httpd.server_close()


def _of(events: list[dict], event_type: str) -> list[dict]:
    return [e for e in events if e.get("event_type") == event_type]


# --------------------------------------------------------------------------- #
# Happy path — full session over HTTP, both framings                          #
# --------------------------------------------------------------------------- #


def test_http_session_end_to_end(http_server: str) -> None:
    stdout_msgs, events = _drive_proxy(http_server, REQUESTS)

    # --- Injection round-trips over HTTP -----------------------------------
    init = next((m for m in stdout_msgs if m.get("id") == 1), None)
    assert init is not None, "no initialize response reached the client"
    assert "baton_annotate" in init["result"]["instructions"], "instructions suffix not injected"

    # baton_annotate is injected into the tools/list response over HTTP.
    tools_list = next((m for m in stdout_msgs if m.get("id") == 12), None)
    assert tools_list is not None, "no tools/list response reached the client"
    tool_names = {t["name"] for t in tools_list["result"]["tools"]}
    assert "baton_annotate" in tool_names, "annotate tool not injected into tools/list"
    assert "echo" in tool_names, "upstream tools missing from injected list"

    # The injected annotate tool is intercepted by the proxy (never forwarded
    # upstream) and its response synthesised — same over HTTP as over stdio.
    annotate_resp = next((m for m in stdout_msgs if m.get("id") == 2), None)
    assert annotate_resp is not None
    assert "baton_annotate recorded" in json.dumps(annotate_resp)

    # --- JSON-framed tool call round-trips ---------------------------------
    echo = next((m for m in stdout_msgs if m.get("id") == 10), None)
    assert echo is not None, "echo response missing"
    assert echo["result"]["content"][0]["text"] == "Echo: over http"

    # --- SSE-framed resource read round-trips ------------------------------
    notes = next((m for m in stdout_msgs if m.get("id") == 4), None)
    assert notes is not None, "SSE resource read response missing"
    assert notes["result"]["contents"][0]["text"] == "Hello from fixture notes."

    # --- A1 lifecycle events emitted (same as stdio path) ------------------
    assert _of(events, "resource_list_start")
    assert _of(events, "resource_list_end")[0]["payload"]["count"] == 2
    assert len(_of(events, "resource_read_start")) == 2
    assert _of(events, "resource_read_end")[0]["payload"]["uri"] == "fixture://notes.txt"
    read_err = _of(events, "resource_read_error")
    assert read_err and read_err[0]["payload"]["error_type"] == "-32002"

    assert _of(events, "prompt_list_start")
    assert _of(events, "prompt_list_end")[0]["payload"]["count"] == 2
    assert len(_of(events, "prompt_get_start")) == 2
    assert _of(events, "prompt_get_end")[0]["payload"]["name"] == "summarize"
    get_err = _of(events, "prompt_get_error")
    assert get_err and get_err[0]["payload"]["name"] == "boom_prompt"

    # --- tool call end + error events --------------------------------------
    assert any(e["payload"].get("tool_name") == "echo" for e in _of(events, "tool_call_end"))
    assert any(e["payload"].get("tool_name") == "boom" for e in _of(events, "tool_call_error"))

    # --- annotations emitted -----------------------------------------------
    annotations = _of(events, "annotation")
    assert len(annotations) >= 2
    assert any(a["payload"].get("signal_type") == "failure" for a in annotations)

    # --- every event shares one session id + the vendor label --------------
    a1_types = {
        "resource_list_start",
        "resource_read_start",
        "prompt_get_start",
        "tool_call_end",
    }
    a1 = [e for e in events if e.get("event_type") in a1_types]
    assert a1
    assert len({e.get("session_id") for e in a1}) == 1
    assert all(e.get("vendor_id") == "acme" for e in a1)


# --------------------------------------------------------------------------- #
# Bearer auth threaded from BATON_UPSTREAM_AUTH_TOKEN                          #
# --------------------------------------------------------------------------- #


# Minimal auth-path sequence: initialize, then one happy read. Built explicitly
# (not sliced from REQUESTS) so inserting requests there can't silently drop the
# read this test asserts on.
_AUTH_REQS: list[dict[str, Any]] = [
    REQUESTS[0],  # initialize
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "resources/read",
        "params": {"uri": "fixture://notes.txt"},
    },
]


def test_bearer_auth_accepted_when_token_matches(http_server_auth: str) -> None:
    stdout_msgs, events = _drive_proxy(
        http_server_auth,
        _AUTH_REQS,
        env_extra={"BATON_UPSTREAM_AUTH_TOKEN": "s3cr3t"},
    )
    # With the right token the read succeeds end to end.
    notes = next((m for m in stdout_msgs if m.get("id") == 4), None)
    assert notes is not None
    assert notes["result"]["contents"][0]["text"] == "Hello from fixture notes."
    assert _of(events, "resource_read_end")


def test_bearer_auth_rejected_fails_open(http_server_auth: str) -> None:
    stdout_msgs, events = _drive_proxy(
        http_server_auth,
        _AUTH_REQS,
        env_extra={"BATON_UPSTREAM_AUTH_TOKEN": "wrong-token"},
    )
    # 401 on every POST → fail-open: the proxy must still exit (not hang) and
    # hand the client JSON-RPC errors rather than nothing.
    errors = [m for m in stdout_msgs if "error" in m]
    assert errors, "expected JSON-RPC errors on the client stream after 401s"
    assert any("HTTP 401" in json.dumps(m) for m in errors)


# --------------------------------------------------------------------------- #
# Real-server shape — SSE-framed initialize + stateless (no Mcp-Session-Id)    #
# Reproduces DeepWiki (mcp.deepwiki.com), validated live 2026-07-04.           #
# --------------------------------------------------------------------------- #


def test_sse_framed_initialize_stateless_upstream() -> None:
    """A stateless server returning ``initialize`` as SSE must still round-trip.

    Locks a real-world shape a live probe found (DeepWiki) that the JSON-
    initialize fixture didn't cover: the handshake response is SSE (so protocol-
    version capture runs off an SSE frame) and no Mcp-Session-Id is issued (so
    the client must NOT invent/echo one). Drives initialize → tools/list → a
    real tool call and asserts injection + capture all hold.
    """
    httpd, url = _start_server(stateless=True, initialize_sse=True)
    reqs = [
        REQUESTS[0],  # initialize
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "sse init"}},
        },
    ]
    try:
        stdout_msgs, events = _drive_proxy(url, reqs)
    finally:
        httpd.shutdown()
        httpd.server_close()

    # initialize parsed from an SSE frame + injection applied
    init = next((m for m in stdout_msgs if m.get("id") == 1), None)
    assert init is not None, "SSE-framed initialize never reached the client"
    assert init["result"]["protocolVersion"] == "2025-03-26"
    assert "baton_annotate" in init["result"]["instructions"]

    # tools/list injection + a real tool call both succeed despite no session id
    tools_list = next((m for m in stdout_msgs if m.get("id") == 3), None)
    assert tools_list is not None
    assert "baton_annotate" in {t["name"] for t in tools_list["result"]["tools"]}

    echo = next((m for m in stdout_msgs if m.get("id") == 10), None)
    assert echo is not None
    assert echo["result"]["content"][0]["text"] == "Echo: sse init"
    assert any(e["payload"].get("tool_name") == "echo" for e in _of(events, "tool_call_end"))


# --------------------------------------------------------------------------- #
# Identifying headers — named UA + Via intermediary announcement              #
# --------------------------------------------------------------------------- #


def test_sends_identifying_headers() -> None:
    """The proxy must send a ``baton-proxy/*`` UA and a Via intermediary header.

    UA: verified against mcp.notion.com — the ``Python-urllib`` default UA gets a
    Cloudflare 403 (browser_signature_banned) before reaching origin auth, so a
    named UA is load-bearing for any Cloudflare-fronted upstream.
    Via: RFC 9110 §7.6.3 — announces the proxy as an intermediary in the chain.
    Both are regression-guarded here.
    """
    httpd, url = _start_server()
    try:
        _drive_proxy(url, [REQUESTS[0]])  # a single initialize is enough
        ua = httpd.last_user_agent
        via = httpd.last_via
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert ua is not None, "no User-Agent header reached the server"
    assert ua.startswith("baton-proxy"), f"expected a baton-proxy UA, got {ua!r}"
    assert via is not None, "no Via header reached the server"
    assert "baton-proxy" in via, f"expected baton-proxy in Via, got {via!r}"


# --------------------------------------------------------------------------- #
# Fail-open — upstream unreachable                                             #
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_unreachable_upstream_fails_open_without_hang() -> None:
    dead_url = f"http://127.0.0.1:{_free_port()}/mcp"
    reqs = [
        REQUESTS[0],  # initialize
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "hi"}},
        },
    ]
    # If this hangs, _drive_proxy raises via the timeout guard.
    stdout_msgs, events = _drive_proxy(dead_url, reqs)

    # Claude gets a JSON-RPC error for the tool call, not silence.
    tool_err = next((m for m in stdout_msgs if m.get("id") == 10 and "error" in m), None)
    assert tool_err is not None, "no JSON-RPC error returned for the failed tool call"

    # A synthetic tool_call_error keeps the wire stream's start/error paired.
    synth = _of(events, "tool_call_error")
    assert any(e["payload"].get("error_type") == "proxy_upstream_unreachable" for e in synth)


def test_slow_upstream_read_timeout_fails_open(http_server: str) -> None:
    """A live-but-silent upstream must be bounded by the read timeout, not hang.

    Distinct from connection-refused (immediate URLError): here the server
    accepts the request and stalls, so only the socket read timeout can unstick
    the proxy. Drives the `slow` tool with a 0.5s timeout against a 3s stall.
    """
    reqs = [
        REQUESTS[0],  # initialize (fast)
        {
            "jsonrpc": "2.0",
            "id": 30,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}},
        },
    ]
    # _drive_proxy's 20s guard would trip long before this returned if the
    # timeout didn't fire (the stall is 3s, but a missing timeout = infinite).
    stdout_msgs, events = _drive_proxy(
        http_server, reqs, env_extra={"BATON_UPSTREAM_TIMEOUT": "0.5"}
    )

    tool_err = next((m for m in stdout_msgs if m.get("id") == 30 and "error" in m), None)
    assert tool_err is not None, "no JSON-RPC error returned for the timed-out call"
    synth = _of(events, "tool_call_error")
    assert any(e["payload"].get("error_type") == "proxy_upstream_unreachable" for e in synth)


def test_accepted_but_no_response_does_not_hang_client(http_server: str) -> None:
    """A 2xx upstream response that carries no answer must not deadlock the client.

    The `noresponse` tool makes the fixture return 202 (no body) for a request
    that carried an id. Without handling, the client blocks forever on that id
    and the pending start dangles. The bridge must instead hand back a JSON-RPC
    error and resolve the pending entry.
    """
    reqs = [
        REQUESTS[0],  # initialize
        {
            "jsonrpc": "2.0",
            "id": 40,
            "method": "tools/call",
            "params": {"name": "noresponse", "arguments": {}},
        },
    ]
    # If the client hung on id=40, _drive_proxy's 20s guard would trip.
    stdout_msgs, events = _drive_proxy(http_server, reqs)

    tool_err = next((m for m in stdout_msgs if m.get("id") == 40 and "error" in m), None)
    assert tool_err is not None, "client got no response for an accepted-but-empty upstream reply"
    # The dangling tool_call_start is resolved with a synthetic error.
    synth = _of(events, "tool_call_error")
    assert any(e["payload"].get("error_type") == "proxy_no_response" for e in synth)


def test_malformed_message_does_not_kill_the_bridge(http_server: str) -> None:
    """A message that makes handle_client_message raise must not down the process.

    Non-dict `params` makes params.get("name") raise inside the processor. The
    HTTP loop runs on the main thread, so an unguarded raise would exit the whole
    bridge. The client must get an error for the bad id AND a following valid
    call must still succeed (proving the loop survived).
    """
    reqs = [
        REQUESTS[0],  # initialize
        {"jsonrpc": "2.0", "id": 50, "method": "tools/call", "params": ["not", "a", "dict"]},
        # A valid call AFTER the bad one — only reaches the server if the loop lived.
        {
            "jsonrpc": "2.0",
            "id": 51,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"text": "still alive"}},
        },
    ]
    stdout_msgs, _events = _drive_proxy(http_server, reqs)

    bad_err = next((m for m in stdout_msgs if m.get("id") == 50 and "error" in m), None)
    assert bad_err is not None, "no error returned for the malformed message"
    echo = next((m for m in stdout_msgs if m.get("id") == 51), None)
    assert echo is not None, "bridge died on the malformed message — later call never answered"
    assert echo["result"]["content"][0]["text"] == "Echo: still alive"

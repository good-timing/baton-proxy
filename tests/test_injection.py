"""End-to-end injection test: drive a scripted JSON-RPC stream through the
proxy and verify it injects the annotation tool + instructions and handles
the injected call without forwarding.

Mirrors the smoke-test spike's checks now run against the production module.
Emission is disabled (env vars unset) so this test is fully offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"

REQUESTS = [
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
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "vendor_annotate",
            "arguments": {
                "signal_type": "failure",
                "intent": "test",
                "suggested_improvement": "none",
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hello"}},
    },
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "boom", "arguments": {}},
    },
]


def _run_proxy() -> dict[int, dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--", sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**env, "PYTHONPATH": str(REPO / "src")},
    )
    assert proc.stdin is not None
    for req in REQUESTS:
        proc.stdin.write(json.dumps(req) + "\n")
        proc.stdin.flush()
    proc.stdin.close()
    try:
        stdout, _stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _stderr = proc.communicate()

    by_id: dict[int, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            by_id[msg["id"]] = msg
    return by_id


def test_initialize_carries_injected_instructions() -> None:
    by_id = _run_proxy()
    init = by_id.get(1)
    assert init is not None, "no initialize response"
    instructions = init.get("result", {}).get("instructions", "")
    assert "vendor_annotate" in instructions
    assert "MUST" in instructions


def test_tools_list_contains_injected_tool() -> None:
    by_id = _run_proxy()
    tools_list = by_id.get(2)
    assert tools_list is not None, "no tools/list response"
    names = [t.get("name") for t in tools_list.get("result", {}).get("tools", [])]
    assert "vendor_annotate" in names
    assert "echo" in names  # upstream tool still there


def test_injected_tool_call_handled_by_proxy() -> None:
    by_id = _run_proxy()
    inj = by_id.get(3)
    assert inj is not None
    text = inj["result"]["content"][0]["text"]
    assert "vendor_annotate recorded" in text


def test_upstream_tool_call_still_works() -> None:
    by_id = _run_proxy()
    echo = by_id.get(4)
    assert echo is not None
    assert "Echo: hello" in echo["result"]["content"][0]["text"]


def test_upstream_tool_error_passes_through() -> None:
    by_id = _run_proxy()
    boom = by_id.get(5)
    assert boom is not None
    assert "error" in boom
    assert boom["error"]["code"] == -32000


class _StubIngest(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            self.received.append(json.loads(body))
        except json.JSONDecodeError:
            self.received.append({"_raw": body})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def log_message(self, *_args, **_kwargs) -> None:
        return


def test_vendor_annotate_emits_annotation_event_end_to_end() -> None:
    """Run the proxy with BATON_* env vars and verify the annotation event
    is POSTed to the console after vendor_annotate is called."""
    _StubIngest.received = []
    server = HTTPServer(("127.0.0.1", 0), _StubIngest)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    console_url = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
        env.update(
            {
                "PYTHONPATH": str(REPO / "src"),
                "BATON_CONSOLE_URL": console_url,
                "BATON_TENANT_ID": "t",
                "BATON_API_KEY": "k",
                "BATON_CONSENT_TOKEN": "c",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "baton_proxy", "--", sys.executable, str(FIXTURE)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert proc.stdin is not None
        for req in REQUESTS:
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
        proc.stdin.close()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

        # Belt-and-suspenders: communicate() returns only after the proxy exits,
        # which drains the queue; still wait briefly in case the OS scheduler
        # hasn't completed the in-flight POSTs.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(ev.get("event_type") == "annotation" for ev in _StubIngest.received):
                break
            time.sleep(0.02)
    finally:
        server.shutdown()

    annotations = [ev for ev in _StubIngest.received if ev.get("event_type") == "annotation"]
    assert len(annotations) == 1, f"expected 1 annotation, got {len(annotations)}"
    ann = annotations[0]
    assert ann["payload"] == {
        "signal_type": "failure",
        "intent": "test",
        "suggested_improvement": "none",
    }
    assert ann["session_id"]
    assert ann["tenant_id"] == "t"
    assert ann["consent_token"] == "c"

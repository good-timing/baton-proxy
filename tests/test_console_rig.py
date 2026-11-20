"""`baton-proxy listen --console-settings` between a real MCP client, a real
server and a stand-in Console that serves a settings document and takes events."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

sys.path.insert(0, str(Path(__file__).parent))

import fixture_http_server  # noqa: E402
from test_listen_rig import (  # noqa: E402
    ACCEPT,
    HEADERS,
    INITIALIZE,
    QUERY,
    _end,
    _env,
    _wait_until_up,
)

pytestmark = pytest.mark.integration

DOCUMENT = {
    "version": 4,
    "audiences": [
        {
            "id": "quiet",
            "match": {"client": ["quiet-runner"]},
            "capture": {"results": "off", "reports": "off"},
            "overlay": "ov_quiet",
        },
        {"id": "everyone", "match": {}, "capture": {}},
    ],
    "overlays": {
        "ov_quiet": {
            "actions": [
                {"target": {"tool": "echo"}, "replace": "Say it back."},
                {"target": {"tool": "whoami"}, "remove": True},
            ]
        },
        "ov_server": {"actions": [{"target": {"instructions": True}, "append": "Be brief."}]},
    },
    "server_overlay": "ov_server",
}


class _Console:
    """Serves ``DOCUMENT`` at /v0/settings and keeps what is posted to /v0/events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.reads: list[dict[str, str]] = []
        console = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:
                console.reads.append({k.lower(): v for k, v in self.headers.items()})
                body = json.dumps(DOCUMENT).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", '"v4"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
                console.events.extend(payload if isinstance(payload, list) else [payload])
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def console_rig(tmp_path: Path):
    console = _Console()
    events = tmp_path / "events.jsonl"
    env = _env(
        BATON_EVENT_SINK=f"file://{events},{console.url}",
        BATON_API_KEY="baton_pk_test",
        BATON_TENANT_ID="ten_test",
        BATON_VENDOR_ID="srv-test",
        BATON_CONSENT_TOKEN="consent",
    )
    uport, lport = fixture_http_server.free_port(), fixture_http_server.free_port()
    procs: list[subprocess.Popen[str]] = []
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "rig_upstream.py"), str(uport)],
                env=_env(),
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )
        _wait_until_up(f"http://127.0.0.1:{uport}/mcp", procs[0])
        listen = [
            *("listen", "--listen", f"127.0.0.1:{lport}"),
            *("--url", f"http://127.0.0.1:{uport}/mcp", "--console-settings"),
        ]
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "baton_proxy", *listen],
                env=env,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
        _wait_until_up(f"http://127.0.0.1:{lport}/mcp", procs[1])
        deadline = time.monotonic() + 10
        while not console.reads:
            assert time.monotonic() < deadline, "the listener never read its settings"
            time.sleep(0.05)
        yield {
            "proxied": f"http://127.0.0.1:{lport}/mcp{QUERY}",
            "listener": procs[1],
            "events": events,
            "console": console,
        }
    finally:
        for proc in reversed(procs):
            _end(proc)
        console.server.shutdown()


async def _session(url: str, client: str) -> dict[str, Any]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            info = Implementation(name=client, version="0")
            async with ClientSession(read, write, client_info=info) as session:
                init = await session.initialize()
                listed = await session.list_tools()
                echoed = await session.call_tool("echo", {"text": f"secret-from-{client}"})
    return {
        "instructions": init.instructions,
        "tools": {t.name: t.description for t in listed.tools},
        "echo": echoed.content[0].text,  # type: ignore[union-attr]
    }


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_two_callers_get_their_audiences_from_the_console(console_rig):
    async def both() -> list[dict[str, Any]]:
        return await asyncio.gather(
            _session(console_rig["proxied"], "quiet-runner"),
            _session(console_rig["proxied"], "other-runner"),
        )

    quiet, other = asyncio.run(both())
    assert quiet["tools"]["echo"] == "Say it back."
    assert "whoami" not in quiet["tools"] and "whoami" in other["tools"]
    assert "baton_annotate" not in quiet["tools"] and "baton_annotate" in other["tools"]
    assert other["tools"]["echo"] != "Say it back."
    for session in (quiet, other):
        assert session["instructions"].startswith("Be brief.")
    assert quiet["echo"] == "secret-from-quiet-runner"


def test_a_quiet_callers_events_carry_capture_and_no_result(console_rig):
    asyncio.run(_session(console_rig["proxied"], "quiet-runner"))
    console_rig["listener"].terminate()
    console_rig["listener"].communicate(timeout=15)
    events = _events(console_rig["events"])
    (end,) = [e for e in events if e["event_type"] == "tool_call_end"]
    assert end["capture"] == {"settings_version": 4, "audience": "quiet", "overlay": "ov_quiet"}
    assert end["payload"]["result_capture"] == "off"
    assert "secret-from-quiet-runner" not in json.dumps(end)
    read = console_rig["console"].reads[0]
    assert read["authorization"] == "Bearer baton_pk_test"
    assert read["x-baton-tenant-id"] == "ten_test"


def test_every_answer_says_the_console_decides(console_rig):
    answered = httpx.post(
        console_rig["proxied"], json=INITIALIZE, headers={**HEADERS, **ACCEPT}, timeout=10
    )
    refused = httpx.post(console_rig["proxied"], json=INITIALIZE, headers=ACCEPT, timeout=10)
    assert {r.headers.get("Baton-Overlay") for r in (answered, refused)} == {"console"}


def test_console_settings_and_an_overlay_file_are_refused_together(tmp_path: Path):
    overlay = tmp_path / "o.json"
    overlay.write_text(json.dumps({"actions": []}))
    env = _env(BATON_EVENT_SINK="https://c.example", BATON_API_KEY="k", BATON_TENANT_ID="t")
    args = ["--console-settings", "--overlay", str(overlay), "--", sys.executable, "-c", "pass"]
    proc = subprocess.run(
        [sys.executable, "-m", "baton_proxy", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 2
    assert "not allowed with argument" in proc.stderr


@pytest.mark.parametrize("missing", ["sink", "tenant", "vendor"])
def test_console_settings_without_a_named_console_is_refused(tmp_path: Path, missing: str):
    named = {
        "BATON_EVENT_SINK": "https://c.example",
        "BATON_API_KEY": "k",
        "BATON_TENANT_ID": "t",
        "BATON_VENDOR_ID": "srv",
    }
    named.pop(
        {"sink": "BATON_EVENT_SINK", "tenant": "BATON_TENANT_ID", "vendor": "BATON_VENDOR_ID"}[
            missing
        ]
    )
    for url in (None, "https://s.example/mcp"):
        target = ["--url", url] if url else ["--", sys.executable, "-c", "pass"]
        proc = subprocess.run(
            [sys.executable, "-m", "baton_proxy", "--console-settings", *target],
            env=_env(**named),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert proc.returncode == 2, proc.stderr
        assert "pass --dsn to listen or wrap" in proc.stderr


def test_a_vendor_who_turns_reports_off_keeps_them_off_under_the_console(tmp_path: Path):
    from baton_proxy import proxy

    script = (
        "import sys\n"
        "from baton_proxy import proxy as p\n"
        "class E:\n"
        "    def __init__(self, c): pass\n"
        "    def start(self): pass\n"
        "p.Emitter = E\n"
        "p.SettingsFetcher.start = lambda self, **_: None\n"
        "_, injection, _, processor = p._bootstrap(console_settings=True)\n"
        "assert processor.settings is not None\n"
        "sys.stdout.write(','.join(sorted(injection.names)))\n"
    )
    base = {
        "BATON_EVENT_SINK": "https://c.example",
        "BATON_API_KEY": "k",
        "BATON_TENANT_ID": "t",
        "BATON_VENDOR_ID": "srv",
        "BATON_CONSENT_TOKEN": "c",
    }
    on = subprocess.run(
        [sys.executable, "-c", script], env=_env(**base), capture_output=True, text=True
    )
    off = subprocess.run(
        [sys.executable, "-c", script],
        env=_env(**base, BATON_ANNOTATE="off"),
        capture_output=True,
        text=True,
    )
    assert (on.stdout, off.stdout) == (proxy.ANNOTATE_TOOL_NAME, ""), (on.stderr, off.stderr)


def test_listen_refuses_console_settings_with_an_overlay_file(tmp_path: Path):
    args = ["listen", "--listen", "127.0.0.1:1", "--url", "http://s/mcp"]
    proc = subprocess.run(
        [sys.executable, "-m", "baton_proxy", *args, "--overlay", "o.json", "--console-settings"],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 2
    assert "not allowed with argument" in proc.stderr

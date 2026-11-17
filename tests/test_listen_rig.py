"""The real `baton-proxy listen` command between an eval runner's MCP client and a real server.

The client is set up the way an eval runner that registers MCP servers by URL sets
it up: an ``httpx.AsyncClient`` carrying the auth headers, handed to the ``mcp``
SDK's ``streamable_http_client``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).parent))

import fixture_http_server  # noqa: E402
import rig_upstream  # noqa: E402

pytestmark = pytest.mark.integration

GOAL = rig_upstream.GOAL_PARAM
HEADERS = {"Authorization": f"Bearer {rig_upstream.TOKEN}", "x-api-key": rig_upstream.API_KEY}
QUERY = f"?tag={rig_upstream.TAG}"
CASES = 5
OVERLAY = {
    "actions": [
        {"target": {"tool": "echo"}, "replace": "Say it back, word for word."},
        {"target": {"tool": "slow"}, "remove": True},
        {"target": {"tool": "whoami"}, "remove": True},
    ]
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "rig", "version": "0"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def _wait_until_up(url: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while True:
        assert proc.poll() is None, "the process exited before it was serving"
        try:
            urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except OSError:
            assert time.monotonic() < deadline, "the process never started serving"
            time.sleep(0.1)


def _end(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


@pytest.fixture
def rig(tmp_path: Path, request):
    overlay_args: list[str] = []
    if getattr(request, "param", None) == "overlay":
        path = tmp_path / "overlay.json"
        path.write_text(json.dumps(OVERLAY))
        overlay_args = ["--overlay", str(path)]
    events = tmp_path / "events.jsonl"
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        PYTHONPATH=str(Path(__file__).parent.parent / "src"),
        BATON_EVENT_SINK=f"file://{events}",
    )
    uport, lport = fixture_http_server.free_port(), fixture_http_server.free_port()
    procs: list[subprocess.Popen[str]] = []
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "rig_upstream.py"), str(uport)],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )
        _wait_until_up(f"http://127.0.0.1:{uport}/mcp", procs[0])
        listen = [
            *("listen", "--listen", f"127.0.0.1:{lport}"),
            *("--url", f"http://127.0.0.1:{uport}/mcp", *overlay_args),
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
        yield {
            "direct": f"http://127.0.0.1:{uport}/mcp{QUERY}",
            "proxied": f"http://127.0.0.1:{lport}/mcp{QUERY}",
            "listener": procs[1],
            "events": events,
        }
    finally:
        for proc in reversed(procs):
            _end(proc)


async def _case(url: str, n: int, call: str, args: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                text = (await session.call_tool("echo", {"text": f"case-{n}", **args})).content
                other = (await session.call_tool(call, args)).content if call else []
    return {
        "tools": [t.model_dump(exclude_none=True) for t in listed.tools],
        "echo": text[0].text,  # type: ignore[union-attr]
        "other": other[0].text if other else None,  # type: ignore[union-attr]
    }


async def _cases(url: str, call: str = "", args: dict[str, Any] | None = None) -> list[Any]:
    return await asyncio.gather(*(_case(url, n, call, args or {}) for n in range(CASES)))


def _without_ours(tool: dict[str, Any]) -> dict[str, Any]:
    """``tool`` minus the param the proxy adds to every tool."""
    tool = json.loads(json.dumps(tool))
    schema = tool.get("inputSchema", {})
    schema.get("properties", {}).pop(GOAL, None)
    if GOAL in schema.get("required", []):
        schema["required"].remove(GOAL)
        if not schema["required"]:
            del schema["required"]
    return tool


def _stop(listener: subprocess.Popen[str]) -> str:
    listener.send_signal(signal.SIGINT)
    _, stderr = listener.communicate(timeout=15)
    return stderr


def test_parallel_cases_see_the_servers_tools_and_answers_on_their_own_sessions(rig):
    direct = asyncio.run(_cases(rig["direct"], "whoami"))
    proxied = asyncio.run(_cases(rig["proxied"], "whoami", {GOAL: "1: rig"}))
    assert [case["echo"] for case in proxied] == [f"case-{n}" for n in range(CASES)]
    for case in proxied:
        assert [_without_ours(t) for t in case["tools"]] == direct[0]["tools"]
        assert all(GOAL in t["inputSchema"]["properties"] for t in case["tools"])
    upstream_sessions = {case["other"] for case in proxied}
    assert len(upstream_sessions) == CASES and "" not in upstream_sessions


def test_parallel_cases_run_at_once_through_the_listener(rig):
    started = time.monotonic()
    asyncio.run(_cases(rig["proxied"], "slow", {"seconds": 1, GOAL: "1: rig"}))
    assert time.monotonic() - started < CASES * 0.6


def test_a_call_in_flight_at_ctrl_c_is_on_disk_with_its_outcome(rig):
    async def interrupted() -> None:
        task = asyncio.ensure_future(_case(rig["proxied"], 0, "slow", {"seconds": 2, GOAL: "1"}))
        await asyncio.sleep(1)
        rig["listener"].send_signal(signal.SIGINT)
        # The mcp client waits on a stream whose server is gone; it is not what is tested.
        await asyncio.wait([task], timeout=5)
        task.cancel()

    asyncio.run(interrupted())
    _, stderr = rig["listener"].communicate(timeout=15)
    assert rig["listener"].returncode == 0, stderr
    assert "Traceback" not in stderr, stderr
    events = [json.loads(line) for line in rig["events"].read_text().splitlines()]
    started = {
        e["call_id"]: e["payload"]["tool_name"]
        for e in events
        if e.get("event_type") == "tool_call_start"
    }
    outcomes = {
        e["call_id"]: e["event_type"]
        for e in events
        if e.get("event_type") in ("tool_call_end", "tool_call_error")
    }
    assert sorted(started.values()) == ["echo", "slow"]
    assert {started[c]: outcomes.get(c) for c in started} == {
        "echo": "tool_call_end",
        "slow": "tool_call_error",
    }


@pytest.mark.parametrize(
    "drop", ["x-api-key", "tag", None], ids=["without-the-api-key", "without-the-tag", "whole"]
)
def test_the_servers_gate_sees_exactly_what_listen_passes_on(rig, drop):
    headers = {k: v for k, v in HEADERS.items() if k != drop}
    url = rig["proxied"].replace(QUERY, "") if drop == "tag" else rig["proxied"]
    resp = httpx.post(url, json=INITIALIZE, headers={**headers, **ACCEPT}, timeout=10)
    assert resp.status_code == (200 if drop is None else 401)


def test_the_startup_line_says_what_round_a_adds_and_where_events_go(rig):
    stderr = _stop(rig["listener"])
    assert "overlay=none" in stderr
    assert f"intent_param=required, events={rig['events']}" in stderr
    assert "did not answer" not in stderr


@pytest.mark.parametrize("rig", ["overlay"], indirect=True)
def test_round_b_shows_the_overlay_and_says_what_it_changed(rig):
    case, *_ = asyncio.run(_cases(rig["proxied"], args={GOAL: "1: rig"}))
    assert case["echo"] == "case-0"
    assert {t["name"]: t.get("description") for t in case["tools"]} == {
        "echo": "Say it back, word for word."
    }
    stderr = _stop(rig["listener"])
    label = next(w for w in stderr.split() if w.startswith("overlay=sha256:"))
    label = label.removeprefix("overlay=").rstrip(",")
    assert f"overlay {label} changed 3 tool(s); every action applied" in stderr


@pytest.mark.parametrize("rig", ["overlay"], indirect=True)
def test_every_answer_names_the_overlay_round(rig):
    answered = httpx.post(
        rig["proxied"], json=INITIALIZE, headers={**HEADERS, **ACCEPT}, timeout=10
    )
    refused = httpx.post(rig["proxied"], json=INITIALIZE, headers=ACCEPT, timeout=10)
    no_stream = httpx.get(rig["proxied"], timeout=10)
    labels = {r.headers.get("Baton-Overlay") for r in (answered, refused, no_stream)}
    assert (answered.status_code, refused.status_code, no_stream.status_code) == (200, 401, 405)
    assert len(labels) == 1 and labels.pop().startswith("sha256:")

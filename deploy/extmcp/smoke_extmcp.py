#!/usr/bin/env python3
"""Smoke test for a running Baton ExtMCP deployment.

Points at a live agentgateway MCP endpoint (your sandbox or a real gateway with
the baton-capture policy attached) and proves the capture contract end to end:

  1. tools/list  -> the injected `user_goal` / `expected_result` params are
     present on every tool's inputSchema.
  2. tools/call  -> a call carrying those params succeeds (backend gets clean
     args — the processor strips them).
  3. (optional, --events-file) the capture landed: a tool_call_start with the
     injected keys STRIPPED from params and the intent captured under the
     header-derived session.
  4. FailOpen: instructions to confirm real work survives the processor going down.

Transport: bridges via `npx mcp-remote <url>` (the same stdio->HTTP path clients
like Claude Desktop use), so this needs Node/npx on PATH. No Python deps.

Usage:
  # against the all-in-one sandbox container (emits to a file you can read):
  docker run -d -p 8080:8080 -e BATON_VENDOR_ID=sandbox \
      -e BATON_EVENT_SINK=file:///tmp/ev.jsonl -v /tmp/ev:/tmp baton-extmcp-sandbox
  python smoke_extmcp.py --url http://127.0.0.1:8080/mcp --events-file /tmp/ev/ev.jsonl

  # against a real gateway (verify injection + strip; check your console for capture):
  python smoke_extmcp.py --url https://<gateway-host>/mcp --tool <a-real-tool>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time


def log(m: str) -> None:
    print(f"[smoke] {m}", flush=True)


class MCPRemote:
    """Minimal JSON-RPC client over `npx mcp-remote <url>` (stdio<->HTTP)."""

    def __init__(self, url: str):
        self.p = subprocess.Popen(
            ["npx", "-y", "mcp-remote", url, "--transport", "http-only", "--allow-http"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        self._id = 0
        self._buf: dict[int, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _drain_stderr(self):
        for line in self.p.stderr:
            if "--" in line or "Error" in line:
                sys.stderr.write(f"  [mcp-remote] {line.rstrip()}\n")

    def _read_stdout(self):
        for line in self.p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self._lock:
                    self._buf[msg["id"]] = msg

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call(self, method, params=None, timeout=40):
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if mid in self._buf:
                    return self._buf.pop(mid)
            time.sleep(0.05)
        raise TimeoutError(f"{method} timed out after {timeout}s")

    def close(self):
        try:
            self.p.terminate()
            self.p.wait(3)
        except Exception:
            self.p.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080/mcp",
                    help="gateway MCP endpoint (Streamable-HTTP)")
    ap.add_argument("--tool", default="echo",
                    help="a tool to call for the capture check (default: echo, the sandbox toy)")
    ap.add_argument("--arg", default="message=hi",
                    help="one real arg as key=value for the tool call")
    ap.add_argument("--events-file", default=None,
                    help="if set, assert the capture landed in this JSONL sink")
    args = ap.parse_args()

    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    c = MCPRemote(args.url)
    try:
        c.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "baton-smoke", "version": "0"}})
        c.notify("notifications/initialized")
        tl = c.call("tools/list", {})
        tools = tl.get("result", {}).get("tools", [])
        injected = [
            t["name"] for t in tools
            if {"user_goal", "expected_result"} <= set(
                (t.get("inputSchema", {}).get("properties", {}) or {}).keys())
        ]
        log(f"tools/list: {len(tools)} tools, injected on {len(injected)}")
        check("injection present on every tool", len(tools) > 0 and len(injected) == len(tools))

        k, _, v = args.arg.partition("=")
        call_args = {k: v, "user_goal": "baton smoke — confirm capture",
                     "expected_result": "the call succeeds and is captured"}
        res = c.call("tools/call", {"name": args.tool, "arguments": call_args})
        check("tools/call succeeded (backend got clean args)",
              not res.get("result", {}).get("isError", False) and "error" not in res)
    finally:
        c.close()

    if args.events_file:
        time.sleep(1.0)
        try:
            evs = [json.loads(line) for line in open(args.events_file)]
        except OSError as e:
            log(f"could not read events file: {e}")
            evs = []
        tcs = [e for e in evs if e.get("event_type") == "tool_call_start"
               and e.get("payload", {}).get("tool_name") == args.tool]
        check("capture landed (tool_call_start emitted)", len(tcs) >= 1)
        if tcs:
            p = tcs[-1]["payload"]
            check("injected keys stripped from captured params",
                  "user_goal" not in p.get("params", {})
                  and "expected_result" not in p.get("params", {}))
            check("intent captured", p.get("call_intent") == "baton smoke — confirm capture")
            check("session id from gateway header",
                  bool(tcs[-1].get("session_id")) and tcs[-1]["session_id"] != "local")
    else:
        log("no --events-file: verify the capture in your console / sink manually.")

    print()
    log("FailOpen check (do manually): stop the baton-extmcp process and re-run — "
        "tools/call must still succeed (capture is best-effort, work never breaks).")
    print("=" * 60)
    print("SMOKE:", "PASS" if ok else "FAIL")
    print("=" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

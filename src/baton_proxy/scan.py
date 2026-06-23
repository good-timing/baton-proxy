"""``baton-proxy scan`` — one-command preflight friction report.

Drives a headless agent through a wrapped MCP server and renders a friction
report, with **no permanent install and no change to the user's Claude config**.
This is the activation CTA: ``uvx baton-proxy scan -- npx -y @vendor/mcp-server``.

Pipeline — all local, nothing leaves the machine:

  1. write an ephemeral MCP config in a temp dir pointing a headless agent at
     ``baton-proxy -- <server>`` with a file event sink (the proxy captures
     friction exactly as in the live wrap);
  2. pick a task plan — pinned for known demo servers, generic otherwise;
  3. drive ``claude -p`` headlessly through that config (the agent is the
     "robot user"; LLM cost lands on the dev's own auth, never Baton's);
  4. render the scan report (``report.synthesize_scan``) → ``./baton-report.md``
     and print a headline.

The report is explicitly labeled preflight/inferred — it previews the friction
an agent is *likely* to hit, it is not real-user data.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from baton_proxy import report
from baton_proxy.scan_tasks import pinned_plan_for

# Wall-clock budget for the agent run. A CTA must always finish in a few
# minutes; on timeout we still render whatever was captured (partial report)
# rather than hang.
DEFAULT_TIMEOUT_S = 300
DEFAULT_REPORT_PATH = "baton-report.md"

# Launcher commands to skip when labelling a server, so `uvx mcp-server-time`
# labels as the package rather than the runner.
_RUNNERS = frozenset(
    {"npx", "uvx", "uv", "npm", "pnpm", "yarn", "bunx", "bun", "node", "deno", "python", "python3"}
)

# Fallback driver prompt for servers without a pinned plan — the "scan YOUR
# server" path. Reliability on an arbitrary server comes from HOW we drive, not
# from a task library: adversarial framing (find friction, don't just use it),
# full-surface coverage, and verify-after-each-action (the move that surfaces
# the silent-success class). The honesty guard ("don't invent friction") keeps
# the report grounded per the value-prop discipline.
GENERIC_PLAN = (
    "You are a QA engineer stress-testing the API design of an unfamiliar MCP "
    "server. Your goal is to surface the real friction a developer's agent would "
    "hit when using it.\n"
    "1. List every tool and read its schema.\n"
    "2. Exercise EVERY tool at least once, and chain them into two or three "
    "realistic multi-step workflows a real user would actually attempt.\n"
    "3. Try the filters, options, and query shapes a real user would expect — "
    "especially ones the tools might not support.\n"
    "4. After each action, VERIFY it did exactly what you intended (read back or "
    "re-query). Watch closely for tools that report success but did not actually "
    "do what you asked.\n"
    "5. Note every friction you actually hit: errors, confusing parameters, "
    "missing capabilities, multi-step detours for simple goals, oversized "
    "responses, and silent successes. Do NOT invent friction — report only what "
    "you genuinely encountered.\n"
    "Finish with a short summary of the roughest edges."
)


def scan_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="baton-proxy scan",
        description="One-command preflight friction report for an MCP server.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_S,
        help=f"Wall-clock budget for the agent run, seconds (default {DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_REPORT_PATH,
        help=f"Where to write the report (default ./{DEFAULT_REPORT_PATH}).",
    )
    parser.add_argument(
        "server",
        nargs=argparse.REMAINDER,
        help="Server command after `--`, e.g. -- npx -y @vendor/mcp-server",
    )
    args = parser.parse_args(argv)

    server_cmd = list(args.server or [])
    if server_cmd and server_cmd[0] == "--":
        server_cmd = server_cmd[1:]
    if not server_cmd:
        parser.error("server command required, after `--`")

    driver = _resolve_driver()
    if driver is None:
        return 2  # guidance already printed

    label = _server_label(server_cmd)
    workdir = tempfile.mkdtemp(prefix="baton-scan-")
    sink_path = os.path.join(workdir, "events.jsonl")
    cfg_path = _write_mcp_config(workdir, server_cmd, label, sink_path)
    plan = pinned_plan_for(" ".join(server_cmd)) or GENERIC_PLAN

    print(f"▸ scanning {label} — preflight (inferred; nothing leaves your machine)")
    print(f"▸ driving agent through the wrapped server (budget {args.timeout}s)…")
    timed_out = _run_agent(driver, plan, cfg_path, workdir, args.timeout)

    sid = _first_session_id(sink_path)
    if sid is None:
        print(_no_events_guidance(label, workdir))
        return 1

    md = report.synthesize_scan(sink_path, sid, server_label=label)
    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    _print_headline(md, out_path, timed_out=timed_out)
    return 0


# =============================================================================
# Steps.
# =============================================================================


def _resolve_driver() -> str | None:
    """Locate the agent driver. v0 drives via the ``claude`` CLI (reuses the
    dev's logged-in session or their ANTHROPIC_API_KEY). Returns the binary
    path, or None after printing actionable guidance."""
    claude = shutil.which("claude")
    if not claude:
        print(
            "✗ baton-proxy scan needs the `claude` CLI to drive the agent.\n"
            "  → install Claude Code (https://docs.claude.com/claude-code), then "
            "`claude login`\n"
            "    (or rely on ANTHROPIC_API_KEY). LLM cost lands on your auth, never Baton's."
        )
        return None
    if os.environ.get("ANTHROPIC_API_KEY") and not _confirm_api_key_billing():
        return None
    return claude


def _confirm_api_key_billing() -> bool:
    """``ANTHROPIC_API_KEY`` takes precedence over a Claude login session, so a
    dev with both set would be billed on their API account without realizing it.
    Warn, and if interactive let them bail to unset it. Non-interactive (e.g.
    run inside another agent), proceed with the warning so the flow doesn't
    hang. Returns True to proceed."""
    print(
        "⚠️  ANTHROPIC_API_KEY is set — scan will drive the agent with it and bill "
        "your API account,\n"
        "    even if you're logged into Claude Code. Unset it to use your Claude "
        "subscription instead."
    )
    if not sys.stdin.isatty():
        print("    (non-interactive — proceeding with the API key.)")
        return True
    try:
        answer = input("    Continue with the API key? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        return True
    print("    Aborted. `unset ANTHROPIC_API_KEY` and re-run to use your Claude login.")
    return False


def _server_label(server_cmd: list[str]) -> str:
    """Pick a recognizable label for the server. Prefer an ``@scope/pkg``
    package name, then a path-like token, then the first non-flag token that
    isn't a runner (``npx``/``uvx``/``node``/…). So ``uvx mcp-server-time``
    labels as ``mcp-server-time`` and ``server-filesystem /tmp/dir`` labels as
    the package, not the runner or the trailing directory. Used as the report
    title and the proxy's required BATON_VENDOR_ID."""
    non_flags = [t for t in server_cmd if not t.startswith("-")]
    for tok in non_flags:
        if tok.startswith("@"):
            return tok
    for tok in non_flags:
        if "/" in tok:
            return tok
    for tok in non_flags:
        if tok not in _RUNNERS:
            return tok
    if non_flags:
        return non_flags[0]
    return " ".join(server_cmd) or "the scanned server"


def _write_mcp_config(workdir: str, server_cmd: list[str], label: str, sink_path: str) -> str:
    """Write an ephemeral MCP config that launches the server wrapped by
    baton-proxy with a file sink. The agent's MCP client merges this env over
    the inherited environment, so the server's own runtime (PATH, Node, creds)
    still flows — we only need to set the proxy's vars."""
    cfg = {
        "mcpServers": {
            "scan_target": {
                "command": sys.executable,
                "args": ["-m", "baton_proxy", "--", *server_cmd],
                "env": {
                    "BATON_VENDOR_ID": label,
                    "BATON_EVENT_SINK": f"file://{sink_path}",
                },
            }
        }
    }
    path = os.path.join(workdir, "mcp.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def _run_agent(driver: str, plan: str, cfg_path: str, workdir: str, timeout: int) -> bool:
    """Drive the headless agent. Returns True if it hit the time budget (the
    caller still renders a partial report). Agent output goes to a log in the
    temp dir so the terminal stays clean for the report."""
    cmd = [
        driver,
        "-p",
        plan,
        "--mcp-config",
        cfg_path,
        "--strict-mcp-config",  # ignore the user's real MCP servers
        "--permission-mode",
        "bypassPermissions",  # headless: never block on a tool prompt
        "--output-format",
        "text",
    ]
    log_path = os.path.join(workdir, "agent.log")
    with open(log_path, "w", encoding="utf-8") as log:
        try:
            subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
            return False
        except subprocess.TimeoutExpired:
            return True


def _first_session_id(sink_path: str) -> str | None:
    """First session_id in the sink, or None if no events were captured."""
    try:
        with open(sink_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = ev.get("session_id")
                if sid:
                    return str(sid)
    except OSError:
        return None
    return None


def _no_events_guidance(label: str, workdir: str) -> str:
    """No events => the wrapped server never produced a tool call. Almost
    always: it needs credentials to boot, or the command failed to start."""
    return (
        f"\n✗ no friction captured for `{label}` — the wrapped server produced no "
        "tool calls.\n"
        "  Most likely it needs credentials to boot, or the command failed to start:\n"
        "    • account-gated server → pass credentials through the environment, e.g.\n"
        "        VENDOR_API_KEY=… baton-proxy scan -- <server command>\n"
        "    • `npx`/`uvx` servers auto-install; a local or private server must be "
        "built first.\n"
        f"  Agent log for debugging: {os.path.join(workdir, 'agent.log')}"
    )


def _print_headline(md: str, out_path: str, *, timed_out: bool) -> None:
    """Print the report header (through the friction count) + pointers."""
    print()
    for line in md.splitlines():
        print(line)
        if line.startswith("**Friction points found**"):
            break
    print()
    if timed_out:
        print("⚠️  agent hit the time budget — report is partial.")
    print(f"Full report   → ./{out_path}")
    print(
        "Keep it on    → `pipx install baton-proxy`, then prepend `baton-proxy --` "
        "to your MCP entry to capture real-user friction continuously."
    )

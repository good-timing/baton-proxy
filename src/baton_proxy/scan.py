"""``baton-proxy scan`` — one-command preflight friction report.

Drives a headless agent through a wrapped MCP server and renders a friction
report, with **no permanent install and no change to the user's Claude config**.
This is the activation CTA: ``uvx baton-proxy scan --config <name>`` — it targets
a server the user has already configured in Claude, reusing that entry's saved
credentials (a friction report only lands on a server they actually run).

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

# baton-proxy's own invocation names — used to detect (and peel) an entry that
# is ALREADY wrapped in the proxy, so `--config` doesn't double-wrap it.
_BATON_PROXY_NAMES = frozenset({"baton-proxy", "baton_proxy"})


class ScanConfigError(Exception):
    """A ``--config`` resolution failure carrying a user-facing message."""

# Fallback driver prompt for servers without a pinned plan — the "scan YOUR
# server" path. Reliability on an arbitrary server comes from HOW we drive, not
# from a task library: adversarial framing (find friction, don't just use it),
# full-surface coverage, and verify-after-each-action (the move that surfaces
# the silent-success class). The honesty guard ("don't invent friction") keeps
# the report grounded per the value-prop discipline.
#
# Step 5 is load-bearing for report CONTENT: the scan report is built from
# captured `baton_annotate` events, not from the agent's prose summary. A
# mechanical error finding stays thin (no intent, no suggested fix) unless the
# agent files a *reactive* annotation on that friction — which the merge folds
# into the error finding. So the plan tells the agent to annotate each friction
# through the tool, with intent + signal_type + suggested_improvement, rather
# than only describing it at the end where the report can't see it.
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
    "5. The moment you hit a friction — an errored or timed-out call, a confusing "
    "parameter, a missing capability, a multi-step detour for a simple goal, an "
    "oversized response, or a silent success (a call that returns ok but didn't "
    "do what you asked) — record it with the `baton_annotate` tool right then: "
    "set signal_type, restate what the user was trying to do (intent), and give a "
    "concrete suggested_improvement. Annotate the friction itself, not just your "
    "final summary — a friction you only describe in prose is not captured. Do "
    "NOT invent friction; annotate only what you genuinely encountered.\n"
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
        "--config",
        metavar="NAME",
        help=(
            "Scan an authed server you already use, by name — e.g. `--config "
            "github` — reusing its saved credentials. Auto-discovers ./.mcp.json "
            "and ~/.claude.json. No secret to type; nothing leaves your machine."
        ),
    )
    parser.add_argument(
        "--config-file",
        metavar="PATH",
        help="Search this MCP config file for --config NAME instead of the standard locations.",
    )
    parser.add_argument(
        # Accepted only so a bare `-- <server>` invocation gets a tailored error
        # pointing at --config, rather than an opaque argparse failure.
        "server",
        nargs=argparse.REMAINDER,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    bare_server = list(args.server or [])
    if bare_server and bare_server[0] == "--":
        bare_server = bare_server[1:]

    # scan targets a server you've configured in Claude (`--config <name>`),
    # reusing that entry's saved credentials. A friction report only delivers
    # its insight on a server you actually run, so the bare `-- <server>` form
    # (idly scanning a stranger's server) is intentionally not supported.
    if bare_server:
        parser.error(
            "the bare `-- <server>` form is not supported; scan targets a server "
            "you've configured in Claude. Add it to your config and run "
            "`baton-proxy scan --config <name>`."
        )
    if args.config_file and not args.config:
        parser.error("--config-file requires --config NAME")
    if not args.config:
        parser.error(
            "scan requires --config NAME — point it at an MCP server you've configured "
            "in Claude (e.g. `--config github`). It reuses that entry's saved "
            "credentials; nothing leaves your machine."
        )

    try:
        server_cmd, entry_env, label = _resolve_config_entry(args.config, args.config_file)
    except ScanConfigError as e:
        print(f"✗ {e}")
        return 2
    creds_note = ", reusing its saved credentials" if entry_env else ""
    source_note = f" (config entry `{args.config}`{creds_note})"

    driver = _resolve_driver()
    if driver is None:
        return 2  # guidance already printed

    workdir = tempfile.mkdtemp(prefix="baton-scan-")
    sink_path = os.path.join(workdir, "events.jsonl")
    cfg_path = _write_mcp_config(workdir, server_cmd, label, sink_path, extra_env=entry_env)
    plan = pinned_plan_for(" ".join(server_cmd)) or GENERIC_PLAN

    print(f"▸ scanning {label}{source_note} — preflight (inferred; nothing leaves your machine)")
    print(f"▸ driving agent through the wrapped server (budget {args.timeout}s)…")
    try:
        timed_out = _run_agent(driver, plan, cfg_path, workdir, args.timeout)
    finally:
        # The generated config may hold resolved credentials (from --config or
        # a credentialed `-- <server>`). Drop it once the agent has booted —
        # don't leave an indefinite plaintext secret in the temp dir. The
        # credential-free debug artifacts (events.jsonl, agent.log) stay.
        _safe_unlink(cfg_path)

    sid = _first_session_id(sink_path)
    if sid is None:
        print(_no_events_guidance(label, workdir))
        return 1

    md = report.synthesize_scan(sink_path, sid, server_label=label)
    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    _print_headline(md, out_path, timed_out=timed_out)
    # Success: the report is written to out_path; nothing else references the
    # temp dir. Drop the whole thing so no captured events (which can include
    # tool-argument secrets) linger on disk — "creds never move." The
    # no-events branch above deliberately keeps the dir for its agent.log hint.
    _safe_rmtree(workdir)
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


def _write_mcp_config(
    workdir: str,
    server_cmd: list[str],
    label: str,
    sink_path: str,
    *,
    extra_env: dict[str, str] | None = None,
) -> str:
    """Write an ephemeral MCP config that launches the server wrapped by
    baton-proxy with a file sink.

    The agent's MCP client merges this ``env`` over the inherited environment
    (verified: a non-empty block layers, it does not replace), so the server's
    own runtime (PATH, Node, ambient creds) still flows. ``extra_env`` carries
    credentials resolved from a ``--config`` entry; ``${VAR}`` references in
    those values are expanded by the MCP client at launch (also verified). The
    proxy's own vars are set LAST so a stray entry value can never shadow them.
    """
    env: dict[str, str] = dict(extra_env or {})
    env["BATON_VENDOR_ID"] = label
    env["BATON_EVENT_SINK"] = f"file://{sink_path}"
    cfg = {
        "mcpServers": {
            "scan_target": {
                "command": sys.executable,
                "args": ["-m", "baton_proxy", "--", *server_cmd],
                "env": env,
            }
        }
    }
    path = os.path.join(workdir, "mcp.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _safe_rmtree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# =============================================================================
# `--config <name>` resolution — wrap an already-authed server entry.
# =============================================================================


def _config_search_paths(explicit_file: str | None) -> list[str]:
    """Where to look for the named entry. An explicit ``--config-file`` short-
    circuits the search; otherwise project-scoped ``./.mcp.json`` then the
    user's ``~/.claude.json`` (project entry beats global within that file)."""
    if explicit_file:
        return [explicit_file]
    return [os.path.join(os.getcwd(), ".mcp.json"), os.path.expanduser("~/.claude.json")]


def _load_mcp_servers(path: str) -> dict[str, dict]:
    """Return ``{name: entry}`` from one config file. Merges the top-level
    ``mcpServers`` with the current project's entries in the ``~/.claude.json``
    shape (``projects.<cwd>.mcpServers``), the more-specific project scope
    winning. Missing/unreadable/malformed file → empty dict (the caller reports
    'not found' across all searched paths)."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers: dict[str, dict] = {}
    top = data.get("mcpServers")
    if isinstance(top, dict):
        servers.update(top)
    projects = data.get("projects")
    if isinstance(projects, dict):
        proj = projects.get(os.getcwd())
        if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
            servers.update(proj["mcpServers"])
    return servers


def _strip_baton_env(env: dict[str, str]) -> dict[str, str]:
    """Drop ``BATON_*`` keys from a resolved entry's env. An entry that is
    already baton-proxy-wrapped carries the vendor's live ``BATON_EVENT_SINK`` /
    ``BATON_API_KEY``; inheriting those would ship the robot scan session to the
    vendor's real Console. scan sets its own (local file sink) instead."""
    return {k: v for k, v in env.items() if not k.startswith("BATON_")}


def _unwrap_baton_proxy(server_cmd: list[str]) -> list[str]:
    """Peel a leading baton-proxy invocation off a resolved command.

    A real vendor's authed entry is commonly ALREADY wrapped (`baton-proxy --
    <upstream>` or `python -m baton_proxy -- <upstream>`). Wrapping that again
    would double-inject the annotation/report tools and nest two proxies. Return
    the bare upstream so scan wraps it exactly once; recurse to handle an
    accidental multi-wrap. A wrapper with no `--` separator is left untouched."""
    if not server_cmd:
        return server_cmd
    head = os.path.basename(server_cmd[0])
    rest = server_cmd[1:]
    is_console = head in _BATON_PROXY_NAMES
    is_module = (
        head.startswith("python")
        and len(rest) >= 2
        and rest[0] == "-m"
        and rest[1] in _BATON_PROXY_NAMES
    )
    if not (is_console or is_module):
        return server_cmd
    try:
        sep = rest.index("--")
    except ValueError:
        return server_cmd
    upstream = rest[sep + 1 :]
    return _unwrap_baton_proxy(upstream) if upstream else server_cmd


def _resolve_config_entry(
    name: str, explicit_file: str | None
) -> tuple[list[str], dict[str, str], str]:
    """Resolve a named MCP server entry to ``(server_cmd, env, label)``.

    Raises ``ScanConfigError`` (user-facing message) when the name is absent,
    ambiguous across configs, a remote/non-stdio server, or malformed.
    """
    paths = _config_search_paths(explicit_file)
    matches: list[tuple[str, dict]] = []
    available: set[str] = set()
    for p in paths:
        servers = _load_mcp_servers(p)
        available.update(servers.keys())
        if name in servers and isinstance(servers[name], dict):
            matches.append((p, servers[name]))

    if not matches:
        searched = ", ".join(paths)
        avail = ", ".join(sorted(available)) or "none found"
        raise ScanConfigError(
            f"no MCP server named `{name}` in {searched}.\n"
            f"  available: {avail}\n"
            "  → check the name, or pass `--config-file <path>` to point at another config."
        )
    if len({json.dumps(e, sort_keys=True) for _p, e in matches}) > 1:
        srcs = ", ".join(p for p, _e in matches)
        raise ScanConfigError(
            f"`{name}` is defined differently in multiple configs ({srcs}).\n"
            "  → pass `--config-file <path>` to choose one."
        )

    entry = matches[0][1]
    etype = entry.get("type")
    if etype in ("http", "sse") or ("command" not in entry and "url" in entry):
        url = entry.get("url", "")
        raise ScanConfigError(
            f"`{name}` is a remote ({etype or 'http'}) MCP server"
            + (f" ({url})" if url else "")
            + ".\n  scan wraps stdio servers today; remote/OAuth servers aren't supported yet."
        )
    command = entry.get("command")
    if not command or not isinstance(command, str):
        raise ScanConfigError(f"`{name}` entry has no usable `command` to launch.")
    raw_args = entry.get("args")
    raw_args = raw_args if isinstance(raw_args, list) else []
    server_cmd = _unwrap_baton_proxy([command, *[str(a) for a in raw_args]])
    raw_env = entry.get("env")
    raw_env = raw_env if isinstance(raw_env, dict) else {}
    env = _strip_baton_env({str(k): str(v) for k, v in raw_env.items()})
    return server_cmd, env, name


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
        "  Most likely the configured server failed to start, or its saved "
        "credentials are missing/expired:\n"
        "    • confirm the entry works in Claude itself (scan runs the same command "
        "+ env).\n"
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

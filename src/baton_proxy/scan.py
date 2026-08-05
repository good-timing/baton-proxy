"""``baton-proxy scan`` — one-command preflight friction report.

Drives a headless agent through a wrapped MCP server and renders a friction
report, with **no permanent install and no change to the user's Claude config**.

Two modes, for two different people, deliberately kept apart:

* ``--config <name>`` — **self-scan** (the activation CTA). The operator scans a
  server *they* configured and run, reusing that entry's saved credentials. The
  aha only lands on a server you own and understand, which is why the bare
  ``-- <server>`` form is a hard error and stays one (2026-06-23 decision).
* ``--url <url>`` — **third-party scan** (outreach). We point the same machinery
  at a stranger's public MCP server to produce a cold-outreach artifact. Not a
  relaxation of the rule above: different operator, different purpose. It is not
  reachable by accident, it runs under the ``guest`` module's read-only + low-
  volume guard, and the report says on its face which mode produced it — because
  a report that reads like the operator's own is a lie to whoever receives it.

Pipeline — all local, nothing but the MCP calls themselves leaves the machine:

  1. write an ephemeral MCP config in a temp dir pointing a headless agent at
     ``baton-proxy -- <server>`` (or ``baton-proxy --url <url>`` for a remote
     target) with a file event sink — the proxy captures friction exactly as in
     the live wrap;
  2. drive ``claude -p`` headlessly through that config (the agent is the
     "robot user"; LLM cost lands on the dev's own auth, never Baton's);
  3. render the scan report (``report.synthesize_scan``) → ``./baton-report.md``
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
from urllib.parse import urlparse

from baton_proxy import guest, report

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


# Step 5 of both plans. Shared verbatim because it is the load-bearing bit for
# report CONTENT (see the GENERIC_PLAN comment) and the two modes must never
# drift on it — a self-scan and a third-party scan should differ in how hard
# they push the server, not in how honestly they record what they found.
_ANNOTATE_STEP = (
    "5. The moment you hit a friction — an errored or timed-out call, a confusing "
    "parameter, a missing capability, a multi-step detour for a simple goal, an "
    "oversized response, or a silent success (a call that returns ok but didn't "
    "do what you asked) — record it with the `baton_annotate` tool right then: "
    "set signal_type, restate what the user was trying to do (intent), and give a "
    "concrete suggested_improvement. Annotate the friction itself, not just your "
    "final summary — a friction you only describe in prose is not captured. Do "
    "NOT invent friction; annotate only what you genuinely encountered.\n"
)

# The driver prompt for every scan — the "scan YOUR server" path. Reliability
# on an arbitrary server comes from HOW we drive, not from a task library:
# adversarial framing (find friction, don't just use it), full-surface
# coverage, and verify-after-each-action (the move that surfaces the
# silent-success class). The honesty guard ("don't invent friction") keeps the
# report grounded per the value-prop discipline.
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
    "do what you asked.\n" + _ANNOTATE_STEP + "Finish with a short summary of the roughest edges."
)

# The driver prompt for `--url` — a stranger's production server. Same detection
# machinery, opposite posture on steps 2-4: the self-scan plan tells the agent to
# exercise EVERY tool and try things that might not be supported, which on
# someone else's server means writing to their data and running up their bill.
# This is the prompt half of the politeness contract; `baton_proxy.guest` is the
# mechanical half that holds when the model gets enthusiastic anyway.
#
# Step 2 draws the line at OTHER PEOPLE'S state rather than at "any write",
# which is a deliberate call. A flat no-writes rule reads stricter but makes the
# mode useless on the exact archetype it targets: plenty of public servers have
# one tool that mints a fresh object for the caller (a route, a diagram, a
# short link), and refusing it means scanning a server without ever calling it.
# Creating something self-scoped that nobody else can see is genuinely different
# from editing, deleting, or sending — so the rule is "touch nothing that
# existed before you arrived, and reuse no identifier you were not just given",
# which is the property that actually keeps a stranger's data safe. The blunt
# name-based guard in `guest` still refuses anything obviously destructive; it
# can't read a schema, so nuance lives here.
#
# The closing line matters — a guard refusal is our restriction, and an agent
# that logged it as a server defect would put a falsehood in a document we hand
# to that server's operator.
GUEST_PLAN = (
    "You are evaluating the API design of a PUBLIC MCP server that belongs to "
    "SOMEONE ELSE. You are a guest on production infrastructure you do not own, "
    "you were not invited, and every call may cost its operator money.\n"
    "1. List every tool and read its schema carefully — most of what a developer "
    "needs to know is visible there, at zero cost to the operator.\n"
    "2. TOUCH NOTHING THAT ALREADY EXISTS. Prefer tools that read, search, list, "
    "fetch or describe. Never update, delete, overwrite, or send anything, and "
    "never pass an id, key, or name you were not handed by a call you just made "
    "— that would edit a stranger's data. If the server's only meaningful tool "
    "creates something, you may create ONE self-scoped throwaway of your own "
    "with plainly fictional inputs, and then only read it back.\n"
    "3. Keep the volume low: a few calls per tool at most. Do not enumerate "
    "exhaustively, do not loop, and do not retry a failing call more than once.\n"
    "4. Within those limits, use the surface the way a real user would: chain "
    "one realistic multi-step task, try the filters and query shapes a user "
    "would expect, and VERIFY each result is actually what you asked for — watch "
    "for calls that report success but return nothing useful.\n"
    + _ANNOTATE_STEP
    + "If a call is refused by baton-proxy's guest guard, that is OUR restriction "
    "and not the server's — never record it as friction.\n"
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
        "--url",
        metavar="URL",
        help=(
            "Scan a REMOTE MCP server by URL (Streamable HTTP) that you do not "
            "operate — the third-party/outreach mode. Runs as a polite guest: "
            "read-only, a capped number of calls, and identified honestly in the "
            "User-Agent. The report is labeled third-party, not a self-scan."
        ),
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=guest.DEFAULT_MAX_UPSTREAM_CALLS,
        metavar="N",
        help=(
            f"--url only: ceiling on upstream tool calls (default "
            f"{guest.DEFAULT_MAX_UPSTREAM_CALLS}). Lower it further on a server you "
            "suspect is expensive to call."
        ),
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

    # The bare `-- <server>` form stays a hard error. It was made one because a
    # self-scan only produces the aha on a server the operator owns and
    # understands, and that reasoning is untouched by `--url` existing: `--url`
    # is not "the bare form with the guard rails off", it's a different mode for
    # a different operator, and it says so in its own report.
    if bare_server:
        parser.error(
            "the bare `-- <server>` form is not supported; scan targets a server "
            "you've configured in Claude. Add it to your config and run "
            "`baton-proxy scan --config <name>`. To scan a remote server you do "
            "NOT operate, use `--url <url>` (third-party mode)."
        )
    if args.url and (args.config or args.config_file):
        parser.error("--url and --config are different scan modes; pass exactly one")
    if args.config_file and not args.config:
        parser.error("--config-file requires --config NAME")
    if not args.config and not args.url:
        parser.error(
            "scan requires --config NAME — point it at an MCP server you've configured "
            "in Claude (e.g. `--config github`). It reuses that entry's saved "
            "credentials; nothing leaves your machine. To scan a remote server you "
            "do NOT operate, use `--url <url>`."
        )

    server_cmd: list[str] = []
    entry_env: dict[str, str] = {}
    if args.url:
        try:
            label, target = _resolve_url_target(args.url)
        except ScanConfigError as e:
            print(f"✗ {e}")
            return 2
        mode = report.SCAN_MODE_THIRD_PARTY
        source_note = " (remote server, not operated by you)"
    else:
        try:
            server_cmd, entry_env, label = _resolve_config_entry(args.config, args.config_file)
        except ScanConfigError as e:
            print(f"✗ {e}")
            return 2
        mode = report.SCAN_MODE_SELF
        target = None
        creds_note = ", reusing its saved credentials" if entry_env else ""
        source_note = f" (config entry `{args.config}`{creds_note})"

    driver = _resolve_driver()
    if driver is None:
        return 2  # guidance already printed

    workdir = tempfile.mkdtemp(prefix="baton-scan-")
    sink_path = os.path.join(workdir, "events.jsonl")
    max_calls = max(1, int(args.max_calls))
    if args.url:
        entry_env = _guest_env(max_calls)
    cfg_path = _write_mcp_config(
        workdir, server_cmd, label, sink_path, extra_env=entry_env, url=args.url
    )
    plan = GUEST_PLAN if args.url else GENERIC_PLAN

    # "nothing leaves your machine" is true of a stdio self-scan and false of a
    # remote one — the agent's calls are exactly what leaves. Say the true thing
    # for each mode rather than the reassuring one for both.
    privacy_note = (
        "the agent's calls reach the server, nothing else leaves"
        if args.url
        else "nothing leaves your machine"
    )
    print(f"▸ scanning {label}{source_note} — preflight (inferred; {privacy_note})")
    if args.url:
        print(
            f"▸ guest mode: read-only, ≤{max_calls} upstream calls, "
            f"identifying as {guest.user_agent({guest.GUEST_MODE_ENV: '1'})}"
        )
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
        print(_no_events_guidance(label, workdir, remote=bool(args.url)))
        return 1

    md = report.synthesize_scan(
        sink_path, sid, server_label=label, mode=mode, target=target, max_calls=max_calls
    )
    out_path = args.out
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    _print_headline(md, out_path, timed_out=timed_out, third_party=bool(args.url))
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
    url: str | None = None,
) -> str:
    """Write an ephemeral MCP config that launches the target wrapped by
    baton-proxy with a file sink.

    The agent's MCP client merges this ``env`` over the inherited environment
    (verified: a non-empty block layers, it does not replace), so the server's
    own runtime (PATH, Node, ambient creds) still flows. ``extra_env`` carries
    credentials resolved from a ``--config`` entry; ``${VAR}`` references in
    those values are expanded by the MCP client at launch (also verified). The
    proxy's own vars are set LAST so a stray entry value can never shadow them.

    With ``url`` set the wrapper runs the proxy's existing HTTPS bridge
    (``baton-proxy --url``) instead of spawning a child process — the agent
    still talks stdio to a local baton-proxy, so everything downstream (capture,
    injection, correlation, rendering) is the same code path as the stdio scan.
    """
    env: dict[str, str] = dict(extra_env or {})
    env["BATON_VENDOR_ID"] = label
    env["BATON_EVENT_SINK"] = f"file://{sink_path}"
    args = ["-m", "baton_proxy", "--url", url] if url else ["-m", "baton_proxy", "--", *server_cmd]
    cfg = {
        "mcpServers": {
            "scan_target": {
                "command": sys.executable,
                "args": args,
                "env": env,
            }
        }
    }
    path = os.path.join(workdir, "mcp.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def _guest_env(max_calls: int) -> dict[str, str]:
    """Environment that switches the wrapped proxy into guest mode.

    Passed through the generated MCP config rather than relying on inheritance,
    so the guard is on by construction for every ``--url`` scan and can't be
    silently lost if the client's env layering ever changes.
    """
    return {
        guest.GUEST_MODE_ENV: "1",
        guest.GUEST_MAX_CALLS_ENV: str(max_calls),
    }


def _resolve_url_target(raw: str) -> tuple[str, str]:
    """Validate a ``--url`` target and return ``(label, url)``.

    ``label`` is the host — it becomes ``BATON_VENDOR_ID`` and the report's
    server name, where the full URL would be noise. Refuses a non-HTTP scheme
    outright, and refuses plain ``http://`` for a non-loopback host: we may be
    sending an auth token, and a scan of a stranger's server should not be the
    thing that puts one on the wire in clear text.
    """
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ScanConfigError(
            f"`{raw}` is not an http(s) URL.\n"
            "  → pass the server's Streamable HTTP endpoint, e.g. "
            "`--url https://example.com/api/mcp`."
        )
    host = parsed.hostname or parsed.netloc
    if parsed.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise ScanConfigError(
            f"`{raw}` is plain http to a remote host.\n"
            "  → use https, so credentials and payloads aren't sent in clear text."
        )
    return host, raw


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


def _no_events_guidance(label: str, workdir: str, *, remote: bool = False) -> str:
    """No events => the wrapped target never produced a tool call. The likely
    causes differ by mode, so the guidance does too: a configured stdio server
    usually failed to launch, while a remote endpoint usually answered but
    wanted auth (or isn't a Streamable HTTP endpoint at all)."""
    log_hint = f"  Agent log for debugging: {os.path.join(workdir, 'agent.log')}"
    if remote:
        return (
            f"\n✗ no friction captured for `{label}` — the remote server produced no "
            "tool calls.\n"
            "  Most likely it needs authentication, exposes no callable tools, or "
            "isn't a Streamable HTTP MCP endpoint:\n"
            "    • check the URL is the MCP endpoint itself (often `/mcp`), not the "
            "docs page.\n"
            "    • if it needs a bearer token, export BATON_UPSTREAM_AUTH_TOKEN "
            "before re-running.\n"
            "    • every tool may have been refused as write-shaped — the guest "
            "guard is read-only by design.\n" + log_hint
        )
    return (
        f"\n✗ no friction captured for `{label}` — the wrapped server produced no "
        "tool calls.\n"
        "  Most likely the configured server failed to start, or its saved "
        "credentials are missing/expired:\n"
        "    • confirm the entry works in Claude itself (scan runs the same command "
        "+ env).\n"
        "    • `npx`/`uvx` servers auto-install; a local or private server must be "
        "built first.\n" + log_hint
    )


def _print_headline(md: str, out_path: str, *, timed_out: bool, third_party: bool = False) -> None:
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
    if third_party:
        # The next step for a third-party scan is a human deciding whether the
        # report is worth sending — not an install prompt aimed at an operator
        # who isn't standing here.
        print(
            "Next          → read it before sending. It's addressed to this server's "
            "operator and says on its face that an outsider ran it."
        )
    else:
        print(
            "Keep it on    → `pipx install baton-proxy`, then prepend `baton-proxy --` "
            "to your MCP entry to capture real-user friction continuously."
        )

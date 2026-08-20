#!/usr/bin/env python3
"""The Baton try kit — setup, receipt, uninstall.

Three commands, and nothing else. Everything they do is described in
``SECURITY.md`` beside this file; if the two ever disagree, the document is the
one that is wrong, because a stranger approved the trial by reading it.

Why these three are code and the rest of the trial is prose: a bad config edit is
silent for days, and a wrong receipt is a claim we repeat to someone else. Those
are the only two steps whose failure nobody witnesses. Everything else in the
flow — choosing a server, explaining what is about to happen, restarting the
client, deciding whether the file may leave the machine — fails loudly and
immediately, so an agent narrating from ``CLAUDE.md`` is the right medium.

The rule this file exists to enforce: **the same code writes the wrap and
reverses it**, so the removal promise in SECURITY.md §7 is keepable rather than
merely stated.

Standard library only. It deliberately does not import ``baton_proxy``: setup
runs from a bare checkout before ``PYTHONPATH`` is set anywhere, and a reviewer
should be able to read one file to know what touches their machine.

Usage — every command is run from this ``try/`` directory, which is where the
trial's ``CLAUDE.md`` puts you (``cd baton-proxy/try && claude``):

    python3 kit.py setup <server-name> [--tenant X] [--vendor Y]
    python3 kit.py receipt
    python3 kit.py uninstall

Exit codes: 0 success, 1 refusal (with a reason and what to do), 2 usage.
"""

from __future__ import annotations

import sys

# Checked BEFORE any other import, and deliberately not inside a command: this
# file is parseable by 3.9 but imports `datetime.UTC`, which is not — so a guard
# placed any lower is dead code that never runs, and the reader gets a raw
# ImportError instead. The interpreter running setup is written into the config
# as the entry's `command`, so "which python am I" is a correctness question
# here, not a nicety.
if sys.version_info < (3, 11):  # noqa: UP036 — this file may be RUN by an older interpreter
    raise SystemExit(
        f"kit.py needs Python 3.11 or newer; this is "
        f"{sys.version_info.major}.{sys.version_info.minor}.\n"
        "Setup writes the interpreter it was run with into your MCP config, so a\n"
        "wrap made now would fail when your client launches it.\n"
        "  -> re-run with a newer interpreter, e.g. `python3.12 kit.py setup ...`"
    )

import argparse
import json
import os
import re
import shutil
import urllib.parse
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# This file lives in <checkout>/try/. Every path the kit writes into a config
# entry is derived from here and made absolute — never from the current working
# directory, which is whatever directory the MCP client happened to launch from.
TRY_DIR = Path(__file__).resolve().parent
CHECKOUT = TRY_DIR.parent
SRC_DIR = CHECKOUT / "src"
EVENTS_PATH = TRY_DIR / "events.jsonl"
STATE_PATH = TRY_DIR / "state.json"

STATE_VERSION = 1

# Names a baton-proxy invocation can appear under in someone's config.
_PROXY_NAMES = frozenset({"baton-proxy", "baton_proxy"})

RESTART_NOTE = (
    "The change is INERT until you fully restart your MCP client — it binds its\n"
    "server set at startup. Nothing is captured before that restart."
)


class Refuse(Exception):
    """A refusal carrying a user-facing message. Raised anywhere the kit would
    otherwise have to guess; caught in main() and printed as-is."""


# =============================================================================
# Pure core — text in, text out. No filesystem, so the round-trip test needs
# no temp home directory and can assert on bytes.
# =============================================================================


def detect_indent(text: str) -> Any:
    """Recover the indent a JSON file was written with, so rewriting it does not
    reformat the whole thing. `~/.claude.json` holds far more than MCP servers
    and belongs to another tool; we touch one entry and leave the shape alone."""
    m = re.search(r'^\{\s*?\n([ \t]+)"', text)
    if not m:
        return 2
    lead = m.group(1)
    return "\t" if "\t" in lead else len(lead)


def dumps_like(data: Any, original_text: str) -> str:
    """Serialize `data` in the same shape `original_text` arrived in."""
    out = json.dumps(data, indent=detect_indent(original_text), ensure_ascii=False)
    return out + "\n" if original_text.endswith("\n") else out


def iter_entries(data: Any) -> list[tuple[str | None, str, dict]]:
    """Every MCP server entry in one parsed config, WITH the scope it lives in.

    Returns ``(scope, name, entry)`` where scope is None for the top-level
    ``mcpServers`` block and the project path for ``projects.<path>.mcpServers``.

    This is where the port of ``scan.py``'s reader deliberately diverges. That
    one merges every scope into a flat ``{name: entry}`` and keys the project
    block on ``os.getcwd()`` — correct for reading, wrong twice for writing:
    a merge forgets which block an entry came from, and this kit runs from
    ``try/``, so a cwd lookup would search a project the user has never opened
    and silently find nothing. Writing must know exactly which block it touched.
    """
    found: list[tuple[str | None, str, dict]] = []
    if not isinstance(data, dict):
        return found
    top = data.get("mcpServers")
    if isinstance(top, dict):
        for name, entry in top.items():
            if isinstance(entry, dict):
                found.append((None, name, entry))
    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj, block in projects.items():
            if not isinstance(block, dict):
                continue
            servers = block.get("mcpServers")
            if isinstance(servers, dict):
                for name, entry in servers.items():
                    if isinstance(entry, dict):
                        found.append((proj, name, entry))
    return found


def entry_at(data: Any, scope: str | None) -> dict:
    """The mutable ``mcpServers`` dict for one scope. Raises if absent — the
    caller found it by enumeration, so absence here is a bug, not user error."""
    if scope is None:
        return data["mcpServers"]
    return data["projects"][scope]["mcpServers"]


def is_stdio(entry: dict) -> bool:
    """A wrappable entry launches a subprocess. Remote servers (http/sse) are
    refused by name rather than skipped, so the user learns why."""
    if entry.get("type") in ("http", "sse"):
        return False
    if "command" not in entry and "url" in entry:
        return False
    return isinstance(entry.get("command"), str) and bool(entry["command"])


def not_wrappable_reason(entry: dict) -> str:
    """Why a single entry ``is_stdio`` rejected cannot be wrapped.

    The rejected shapes fail for different reasons and only one of them is close
    to workable, so collapsing them into "remote (http/sse)" throws away the one
    fact that tells them apart. This reports what the entry *is*. It promises
    nothing about what a later version might wrap, and it never prints a header
    VALUE — the list is shown to someone who may paste it back to us."""
    if entry.get("type") == "sse":
        return "sse transport"
    # Order matters: a malformed stdio entry (`{"command": ""}`) reaches here
    # too, and calling that one "http, no credential" would be a false statement
    # about their config in the one artifact whose premise is being exact.
    if entry.get("type") == "http" or "url" in entry:
        headers = entry.get("headers")
        headers = headers if isinstance(headers, dict) else {}
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
        others = sorted(k for k in headers if k.lower() != "authorization")
        if isinstance(auth, str) and auth.strip().lower().startswith("bearer "):
            what = "http, bearer token in the config"
            if "${" in auth:
                what += " (a ${VAR} reference)"
            return what + (f", plus {', '.join(others)}" if others else "")
        if headers:
            return "http, custom headers (" + ", ".join(sorted(headers)) + ")"
        return "http, no credential in the config"
    return "no usable launch command"


def is_proxy_invocation(cmd: list[str]) -> bool:
    """Does this command LEAD with a baton-proxy launch, in the two head forms?

    Narrow on purpose: this is what ``unwrap_command`` consumes, and unwrap is
    pinned byte-for-byte to ``scan.py``'s donor by a drift test. Widening it
    would move unwrap. The broader question — "is this entry a proxy at all" —
    is ``is_wrapped``, which sweeps every token."""
    if not cmd:
        return False
    head = os.path.basename(cmd[0])
    rest = cmd[1:]
    if head in _PROXY_NAMES:
        return True
    return head.startswith("python") and len(rest) >= 2 and rest[0] == "-m" and rest[1] in _PROXY_NAMES


def safe_endpoint(url: str) -> str:
    """Scheme and host only — never the path, query, or userinfo.

    An MCP endpoint URL is frequently the credential itself: Zapier and Composio
    put the secret in the PATH (``…/api/mcp/s/<token>/sse``), and ``?key=`` is
    just as common. The refusal that prints it is shown to someone who may paste
    it into a support thread with us, so it gets the same treatment as a header
    value — named, never quoted."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "the configured endpoint"
    host = parts.hostname or ""
    if not host:
        return "the configured endpoint"
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}" if parts.scheme else host


def unwrap_command(cmd: list[str]) -> list[str]:
    """Peel any leading baton-proxy invocation off a command.

    Ported from ``scan.py``'s ``_unwrap_baton_proxy`` and pinned to it by a drift
    test — do not change its behaviour here. It recovers the wrapped command;
    it recurses to handle an accidental multi-wrap, and a wrapper with no ``--``
    separator is left alone because there is no upstream command to recover.
    Whether an entry is wrapped is ``is_wrapped``'s question, not this one."""
    if not cmd:
        return cmd
    if not is_proxy_invocation(cmd):
        return cmd
    rest = cmd[1:]
    try:
        sep = rest.index("--")
    except ValueError:
        return cmd
    upstream = rest[sep + 1 :]
    return unwrap_command(upstream) if upstream else cmd


def is_wrapped(entry: dict) -> bool:
    """True for any entry that launches baton-proxy in any form WE CAN DETECT.

    A token-level sweep, not a head check, because the head is only one of the
    ways a proxy gets launched: ``uvx baton-proxy -- …`` and ``uv run
    baton-proxy -- …`` are how our own README tells people to run it,
    ``/usr/bin/env python3 -m baton_proxy`` is ordinary, and a shell wrapper
    puts the whole command inside one argument. A head check sees none of them,
    so each would be wrapped a SECOND time — two nested proxies, the annotation
    tool injected twice, discovered days later in a file nobody is watching.

    It OVER-triggers by design. A path component that merely happens to be
    named ``baton-proxy`` reads as a wrap (see the documented false positive in
    the tests), and the cost of that is one manual step for the user, against a
    silent double-wrap for the miss. Known still-missed: a direct
    ``python3 /path/to/baton_proxy/__main__.py``, where no token's basename is
    the package name."""
    cmd = [entry.get("command", ""), *[str(a) for a in entry.get("args") or []]]
    return any(os.path.basename(piece) in _PROXY_NAMES for token in cmd for piece in token.split())


def file_sink_uri(path: str) -> str:
    """Build the ``file://`` URI for the event sink, in the form the PROXY parses.

    Not ``Path.as_uri()``, which is the obvious choice and is wrong here.
    ``as_uri()`` percent-encodes, and the consumer — ``sinks.py:_make_one`` —
    does ``urlparse(url)`` and hands ``parsed.path`` to ``FileSink`` WITHOUT
    unquoting. So a checkout under ``/Users/x/My Projects/`` yields
    ``file:///Users/x/My%20Projects/...``, ``FileSink`` tries to open a
    directory named ``My%20Projects``, ``open()`` raises inside ``__init__``,
    ``Emitter.start()`` propagates it, and the proxy dies at launch — days after
    setup told the user everything was fine, on the one machine we cannot see.

    Writing the raw path round-trips through the consumer's own parse, which is
    what correctness means here. The equality check below is both the proof and
    the guard: a path containing ``?`` or ``#`` cannot survive any file URI
    (urlparse would split it into a query or fragment), so it is refused by name
    rather than written and discovered later.
    """
    # A comma never reaches urlparse: make_sink splits BATON_EVENT_SINK on ","
    # FIRST and parses each piece, so a checkout under "Proj,old" becomes two
    # bogus sinks and the proxy dies at every launch. Checked before the parse
    # because the parse cannot see it.
    if "," in path:
        raise Refuse(
            f"the events file path contains a comma, which the proxy reads as a\n"
            f"  separator between two sinks:\n    {path}\n"
            "  → move or rename the checkout so its path has no comma, then run\n"
            "    setup again."
        )
    uri = f"file://{path}"
    if urllib.parse.urlparse(uri).path != path:
        raise Refuse(
            f"the events file path cannot be expressed as a URI the proxy will read\n"
            f"  back unchanged:\n    {path}\n"
            "  → a `?` or `#` in the path is the usual cause. Move or rename the\n"
            "    checkout so its path contains neither, then run setup again."
        )
    return uri


def build_wrapped_entry(
    original: dict,
    *,
    tenant_id: str,
    vendor_id: str,
    src_dir: str,
    events_path: str,
    interpreter: str = sys.executable,
) -> dict:
    """The one transformation this kit exists to perform.

    Same key name, same env, the original command demoted to the proxy's
    argument. Rules encoded here rather than in prose because each one is a
    silent failure if a human or a model gets it wrong:

    - The upstream env is preserved verbatim, ``${VAR}`` references included —
      the MCP client expands those when it launches the server; we never resolve
      them and never see a credential.
    - Baton's own vars are written LAST, so a stray value in the user's env
      cannot shadow them.
    - ``BATON_EVENT_SINK`` is the file ONLY. The proxy's default also mirrors
      every event to stderr, which the client may capture into its own logs —
      SECURITY.md §7 promises that does not happen here.
    - ``BATON_TENANT_ID`` is set explicitly. The default is the sentinel
      ``"local"``, and every trial that kept it would land in one merged bucket
      on our side, which is the difference between the file being useful and not.
    - ``PYTHONPATH`` is APPENDED, not prepended: it is inherited by the wrapped
      server too, and a Python server's own modules must keep winning.
    """
    cmd = [original.get("command", ""), *[str(a) for a in original.get("args") or []]]
    upstream = unwrap_command(cmd)

    env = {str(k): str(v) for k, v in (original.get("env") or {}).items()}
    # Append only if absent. Re-wrapping an entry that already carries our path
    # (a repeat setup after the state file was deleted) must not grow it every
    # time — an unbounded PYTHONPATH is the kind of damage nobody looks for.
    parts = [p for p in (env.get("PYTHONPATH") or "").split(os.pathsep) if p]
    if src_dir not in parts:
        parts.append(src_dir)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["BATON_TENANT_ID"] = tenant_id
    env["BATON_VENDOR_ID"] = vendor_id
    env["BATON_EVENT_SINK"] = file_sink_uri(events_path)

    wrapped = dict(original)
    # sys.executable, never the bare name. `python3` is resolved against the MCP
    # CLIENT's PATH, and a GUI-launched client on macOS inherits launchd's
    # minimal PATH where `python3` is /usr/bin/python3 — 3.9, which cannot import
    # baton_proxy (it needs >=3.11). The server would then die at client launch,
    # days after setup printed success. scan.py:268 already writes sys.executable
    # and test_scan.py pins it; this is the same invariant.
    wrapped["command"] = interpreter
    wrapped["args"] = ["-m", "baton_proxy", "--", *upstream]
    wrapped["env"] = env
    return wrapped


def apply_wrap(
    config_text: str,
    *,
    scope: str | None,
    name: str,
    tenant_id: str,
    vendor_id: str,
    src_dir: str,
    events_path: str,
    interpreter: str = sys.executable,
) -> tuple[str, dict]:
    """Wrap one entry. Returns ``(new_config_text, state)``.

    ``state`` carries the original entry as an object, byte-for-byte in the
    sense that matters: it round-trips through json unchanged, so uninstall can
    put back exactly what was there rather than reconstructing it."""
    data = json.loads(config_text)
    block = entry_at(data, scope)
    original = block[name]
    wrapped = build_wrapped_entry(
        original,
        tenant_id=tenant_id,
        vendor_id=vendor_id,
        src_dir=src_dir,
        events_path=events_path,
        interpreter=interpreter,
    )
    block[name] = wrapped
    state = {
        "version": STATE_VERSION,
        "wrapped_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "scope": scope,
        "server_name": name,
        "original_entry": original,
        "wrapped_entry": wrapped,
        "tenant_id": tenant_id,
        "vendor_id": vendor_id,
        "events_path": events_path,
    }
    return dumps_like(data, config_text), state


def apply_unwrap(config_text: str, state: dict) -> tuple[str, dict]:
    """Reverse exactly what apply_wrap did. Returns ``(new_config_text, restored)``.

    Restores INTO THE CURRENT FILE rather than copying the backup over it: days
    pass between setup and uninstall, and the client rewrites this file
    continuously in that time. The backup is evidence of what was there, never
    the source of the restore — putting it back wholesale would silently discard
    every unrelated change made since.

    Refuses if the entry no longer looks like the one we wrote. Someone editing
    it by hand in between is exactly the case where guessing is worst."""
    data = json.loads(config_text)
    scope, name = state["scope"], state["server_name"]
    try:
        block = entry_at(data, scope)
    except (KeyError, TypeError):
        raise Refuse(
            f"the config no longer has the block that held `{name}`"
            + (f" (project {scope})" if scope else " (global mcpServers)")
            + ".\n  → nothing was changed. Restore this entry by hand:\n\n"
            + json.dumps(state["original_entry"], indent=2)
        ) from None
    current = block.get(name)
    if current is None:
        raise Refuse(
            f"`{name}` is no longer in the config; someone removed it after setup.\n"
            "  → nothing was changed. If you want it back, this is what it was:\n\n"
            + json.dumps(state["original_entry"], indent=2)
        )
    if current == state["original_entry"]:
        # Already back to what it was — someone restored it by hand, or the
        # client rewrote it. There is nothing to undo and refusing here would be
        # a trap: setup refuses on the stale state, uninstall refuses on the
        # entry, and CLAUDE.md forbids the agent from deleting the state file to
        # escape. Clearing the state IS the remaining work.
        return config_text, current
    if current != state["wrapped_entry"]:
        raise Refuse(
            f"`{name}` has been edited since setup wrapped it, so this kit will not\n"
            "  silently overwrite it. Both versions, for you to reconcile by hand:\n\n"
            "  --- in your config now ---\n"
            + json.dumps(current, indent=2)
            + "\n\n  --- what setup recorded as the original ---\n"
            + json.dumps(state["original_entry"], indent=2)
        )
    block[name] = state["original_entry"]
    return dumps_like(data, config_text), state["original_entry"]


# =============================================================================
# Receipt — computed from the local JSONL and nothing else.
# =============================================================================


def read_events(path: Path) -> list[dict]:
    """Every well-formed line. A truncated final line (the proxy was killed
    mid-write) is skipped rather than fatal — the receipt must work on whatever
    is on disk, including a session that ended badly."""
    if not path.exists():
        return []
    events = []
    # errors="replace": a proxy killed mid-write can leave a partial UTF-8
    # sequence, and the decode happens during iteration, outside the try below.
    # The receipt must survive a session that ended badly — that is the point.
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                events.append(obj)
    return events


def summarize(events: list[dict], size_bytes: int) -> dict:
    """The receipt's numbers. Counts only — no error counts, and no analysis.

    Analysis is what the file is FOR, and it happens with us in the room. The
    receipt's job is narrower: prove we captured a class of thing you have no
    other way to see."""
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    with_intent: set[str] = set()
    tool_calls = 0
    tools: list[str] = []
    stamps: list[str] = []
    kinds: Counter[str] = Counter()

    for e in events:
        kind = e.get("event_type", "")
        kinds[kind] += 1
        if e.get("captured_at"):
            stamps.append(e["captured_at"])
        payload = e.get("payload") or {}
        if kind == "tool_call_start":
            tool_calls += 1
            if payload.get("call_intent"):
                with_intent.add(e.get("session_id") or "")
        elif kind == "annotation" and payload.get("intent"):
            with_intent.add(e.get("session_id") or "")
        elif kind == "surface_snapshot":
            names = [t.get("name", "") for t in payload.get("tools") or [] if isinstance(t, dict)]
            if names:
                tools = names

    # Redaction counts are read back off the file rather than from the proxy's
    # own counters: those live in a process that exited days ago, and the
    # receipt must answer from a cold session. The marker is what survives.
    redactions: Counter[str] = Counter()
    for e in events:
        for m in re.finditer(r"\[REDACTED:([a-z_\-]+)\]", json.dumps(e.get("payload") or {})):
            redactions[m.group(1)] += 1

    return {
        "sessions": len(sessions),
        "sessions_with_intent": len(with_intent - {""}),
        "tool_calls": tool_calls,
        "tools": tools,
        "first": min(stamps) if stamps else None,
        "last": max(stamps) if stamps else None,
        "size_bytes": size_bytes,
        "events": len(events),
        "kinds": dict(kinds),
        "redactions": dict(redactions),
    }


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


NOT_CAPTURING = """\
No events have been captured yet.

That is a real answer, not an error — and it is worth getting to the bottom of
now rather than at the end of the trial. In order:

  1. Has the MCP client been fully restarted since setup ran? The wrap is inert
     until it is. This is the usual cause.
  2. Has the wrapped server actually been used since that restart? Opening a
     session is not enough; the agent has to call the server at least once.
     (A session that merely starts does record one tool-surface snapshot, so if
     even that is missing, the proxy is not in the path at all.)
  3. Is the entry that got wrapped the one you actually use? Run
     `python3 kit.py receipt` from this folder and check the server name
     and config path printed above.
  4. Does the wrapped command launch? Run exactly what setup wrote into the
     entry (printed above as `launch check`). If that fails, the server is dead
     in your client too — which you would have noticed.
"""


def launch_check(state: dict | None) -> str:
    """The command that reproduces the wrapped entry's own launch.

    Built from what setup actually recorded, not from a fixed string. A hardcoded
    `python3 -m baton_proxy` would fail on precisely the machine this file argues
    about at length — a GUI-launched client under launchd's PATH, where `python3`
    is 3.9 — telling the user their healthy wrap is broken."""
    if not state:
        return f"PYTHONPATH={SRC_DIR} {sys.executable} -m baton_proxy --help"
    entry = state.get("wrapped_entry") or {}
    interp = entry.get("command", sys.executable)
    pp = (entry.get("env") or {}).get("PYTHONPATH", str(SRC_DIR))
    return f"PYTHONPATH={pp} {interp} -m baton_proxy --help"


# =============================================================================
# Commands
# =============================================================================


def load_state() -> dict:
    """Read the state file, or refuse with a way out.

    Unguarded json.loads here would traceback out of all three commands —
    including uninstall, which would leave someone wrapped with no supported
    way to remove it. The exit-code contract in the module docstring is a
    promise to a stranger; a traceback breaks it."""
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise Refuse(
            f"the setup state file is unreadable: {STATE_PATH}\n"
            "  → your config entry is probably still wrapped. Its `args` end with\n"
            "    `-- <your original command>`, which is what the entry was before.\n"
            "    Restore that by hand, then delete the state file."
        ) from None


def search_paths(explicit: str | None) -> list[Path]:
    if explicit:
        # Resolved, not just expanded: this path is stored in the state file and
        # has to survive being read back from a different working directory days
        # later. A relative path here would make receipt and uninstall fail.
        return [Path(explicit).expanduser().resolve()]
    # resolve() follows symlinks: a dotfiles-managed ~/.claude.json must be
    # written THROUGH the link, or os.replace swaps the symlink for a regular
    # file and silently divorces the user's managed config.
    # The cwd .mcp.json entry is deliberately absent: the trial pins the working
    # directory to try/ (see CLAUDE.md), so it could only ever name
    # baton-proxy/try/.mcp.json, which is never a user config — and naming it in
    # the not-found message misdirects. --config-file covers a project config.
    return [(Path.home() / ".claude.json").resolve()]


def discover(explicit: str | None) -> list[tuple[Path, str, str | None, str, dict]]:
    """``(path, text, scope, name, entry)`` for every entry in every config.

    A missing or malformed file is skipped silently when it came from the
    default search list — those paths are speculative. When the user NAMED the
    file, a parse error is their answer and must surface: otherwise a trailing
    comma reports as "no MCP configuration found", which points at the wrong
    problem entirely."""
    out = []
    for p in search_paths(explicit):
        try:
            text = p.read_text(encoding="utf-8")
            data = json.loads(text)
        except json.JSONDecodeError as e:
            if explicit:
                raise Refuse(f"{p} is not valid JSON: {e}\n  → nothing was changed.") from None
            continue
        except OSError:
            if explicit:
                raise Refuse(f"cannot read {p}.\n  → check the path passed to --config-file.") from None
            continue
        for scope, name, entry in iter_entries(data):
            out.append((p, text, scope, name, entry))
    return out


def describe(path: Path, scope: str | None) -> str:
    return f"{path}" + (f" · project {scope}" if scope else " · global mcpServers")


def write_atomically(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    ``write_text`` truncates before it writes, so a crash mid-write leaves the
    user with a truncated ``~/.claude.json``. The backup makes that recoverable,
    but only if they find this document and read it; ``os.replace`` is atomic on
    the same filesystem and makes the window zero instead."""
    mode = path.stat().st_mode & 0o777
    tmp = path.with_name(path.name + ".baton-tmp")
    # Created 0600 BEFORE any content is written, then set to the original
    # file's mode. Writing first and chmod-ing after would leave the whole of
    # `~/.claude.json` — OAuth tokens, every project's env block — readable by
    # every user on the box for the duration of the write. os.replace also keeps
    # the temp file's permissions, so the mode has to be right at both ends.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Never strand a temp file holding the user's config next to the real one.
        tmp.unlink(missing_ok=True)
        raise


def cmd_setup(args: argparse.Namespace) -> int:
    if STATE_PATH.exists():
        state = load_state()
        # Check before claiming. Someone may have restored the entry by hand
        # since setup ran, and "already wrapped, nothing changed" would then be
        # a false statement of exactly the kind this kit exists not to make.
        if not wrap_still_present(state):
            raise Refuse(
                f"a state file says `{state['server_name']}` was wrapped at "
                f"{state['wrapped_at']},\n"
                "  but the entry in your config is not the one setup wrote — it has been\n"
                "  changed or restored by hand since.\n"
                f"  → delete {STATE_PATH} to start over, or run uninstall first."
            )
        print(f"Already wrapped: `{state['server_name']}` since {state['wrapped_at']}.")
        print(f"  config: {describe(Path(state['config_path']), state['scope'])}")
        print("\nNothing changed. Current entry:\n")
        print(json.dumps(state["wrapped_entry"], indent=2))
        print(f"\n{RESTART_NOTE}")
        return 0

    found = discover(args.config_file)
    if not found:
        raise Refuse(
            "no MCP configuration found in "
            + ", ".join(str(p) for p in search_paths(args.config_file))
            + ".\n  → the trial needs one stdio MCP server already configured and working."
            "\n  → if yours lives elsewhere, pass --config-file <path>."
        )

    if not args.server:
        stdio = [(p, s, n) for p, _t, s, n, e in found if is_stdio(e) and not is_wrapped(e)]
        # Already-wrapped stdio entries get their own line rather than falling
        # out of both lists — otherwise a machine whose only stdio server is
        # already wrapped is told "there is no stdio server here to wrap", which
        # is false, and the name it hides is the one needed to reach the real
        # refusal below.
        already = [(p, s, n) for p, _t, s, n, e in found if is_stdio(e) and is_wrapped(e)]
        remote = [(n, not_wrappable_reason(e)) for _p, _t, _s, n, e in found if not is_stdio(e)]
        lines = [f"  {n:<24} {describe(p, s)}" for p, s, n in stdio] or ["  (none)"]
        msg = "which server should the trial wrap? Pass its name.\n\n  " + "\n".join(lines)
        if already:
            msg += "\n\n  Already baton-proxy, so not offered:\n  " + "\n  ".join(
                f"  {n:<24} {describe(p, s)}" for p, s, n in already
            )
        if remote:
            msg += (
                "\n\n  Not wrappable — this kit wraps stdio entries only (v0). What each is:\n  "
                + "\n  ".join(f"  {n:<24} {r}" for n, r in sorted(set(remote)))
            )
        if not stdio:
            msg += (
                "\n\n  There is no UNWRAPPED stdio server here. The trial needs one;\n"
                "  nothing has been changed."
            )
        raise Refuse(msg)

    matches = [(p, t, s, n, e) for p, t, s, n, e in found if n == args.server]
    if not matches:
        names = sorted({n for _p, _t, _s, n, _e in found})
        raise Refuse(
            f"no MCP server named `{args.server}`.\n  available: "
            + (", ".join(names) or "none")
            + "\n  → check the name, or pass --config-file <path>."
        )
    if len(matches) > 1:
        where = "\n".join(f"    {describe(p, s)}" for p, _t, s, _n, _e in matches)
        raise Refuse(
            f"`{args.server}` is defined in more than one place:\n{where}\n"
            "  → this kit will not choose for you.\n"
            "  → if the duplicates are in the SAME file (a global entry plus a project\n"
            "    one, say), rename one of them — there is no scope selector, so\n"
            "    --config-file cannot separate them.\n"
            "  → if they are in different files, pass --config-file <path> to pick one."
        )

    path, text, scope, name, entry = matches[0]
    if not is_stdio(entry):
        raise Refuse(
            f"`{name}` is not wrappable — {not_wrappable_reason(entry)}"
            + (f" ({safe_endpoint(str(entry['url']))})" if entry.get("url") else "")
            + ".\n  This kit wraps stdio entries only (v0): setup replaces the command that\n"
            "  launches the server, and this entry has none.\n"
            "  → if you already reach this server through a stdio entry (a local bridge\n"
            "    command, say), name that one instead. Nothing changed."
        )
    if is_wrapped(entry):
        cmd = [entry.get("command", ""), *[str(a) for a in entry.get("args") or []]]
        if unwrap_command(cmd) == cmd:
            # Nothing to peel: an HTTP-bridge entry (`--url`) has no upstream
            # command inside it, so "unwrap it by hand" would mean deleting the
            # entry's only launch mechanism. Never tell someone to do that.
            raise Refuse(
                f"`{name}` IS baton-proxy — bridging a remote upstream, not wrapping a\n"
                "  local one. There is no original command inside it to restore, so there\n"
                "  is nothing for this kit to wrap and nothing to undo.\n"
                "  → pick a different server. Nothing changed."
            )
        raise Refuse(
            f"`{name}` is already wrapped in baton-proxy, but this kit did not do it\n"
            "  (there is no state file). Refusing to touch someone else's wrap.\n"
            "  → unwrap it by hand first, or pick a different server."
        )

    tenant = args.tenant or f"trial-{uuid.uuid4().hex[:8]}"
    vendor = args.vendor or name

    if not SRC_DIR.is_dir():
        raise Refuse(
            f"expected the proxy source at {SRC_DIR}, which is not there.\n"
            "  → run this from a full checkout: the kit points PYTHONPATH at that folder."
        )

    new_text, state = apply_wrap(
        text,
        scope=scope,
        name=name,
        tenant_id=tenant,
        vendor_id=vendor,
        src_dir=str(SRC_DIR),
        events_path=str(EVENTS_PATH),
    )
    state["config_path"] = str(path)

    # Back up the WHOLE file before touching it. `~/.claude.json` holds far more
    # than MCP servers, and a bad write on a machine we will never see is
    # unrecoverable for us. The backup is evidence; uninstall does not read it.
    backup = TRY_DIR / f"config-backup.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    shutil.copy2(path, backup)

    write_atomically(path, new_text)
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    print(f"Wrapped `{name}` in {describe(path, scope)}")
    print(f"  backup:  {backup}")
    print(f"  events:  {EVENTS_PATH}")
    print(f"  tenant:  {tenant}   vendor: {vendor}")
    print("\nThe entry now reads:\n")
    print(json.dumps(state["wrapped_entry"], indent=2))
    print(f"\n{RESTART_NOTE}")
    print("\nAfter the restart, use the server normally. Run\n"
          "  python3 kit.py receipt\nany time — early is better than late.")
    return 0


def wrap_still_present(state: dict) -> bool:
    """Is the entry setup wrote still exactly what is in the config?"""
    try:
        data = json.loads(Path(state["config_path"]).read_text(encoding="utf-8"))
        return entry_at(data, state["scope"]).get(state["server_name"]) == state["wrapped_entry"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return False


def cmd_receipt(args: argparse.Namespace) -> int:
    state = None
    if STATE_PATH.exists():
        state = load_state()
        events_path = Path(state.get("events_path", EVENTS_PATH))
    else:
        events_path = EVENTS_PATH

    print("Baton trial receipt")
    print("=" * 60)
    if state:
        print(f"wrapped server : {state['server_name']}")
        print(f"config         : {describe(Path(state['config_path']), state['scope'])}")
        print(f"wrapped at     : {state['wrapped_at']}")
        print(f"labels         : tenant={state['tenant_id']} vendor={state['vendor_id']}")
    else:
        print("No setup state found — this receipt is reading the event file directly.")
    print(f"event file     : {events_path}")
    print()

    print(f"launch check   {launch_check(state)}")
    print()

    events = read_events(events_path)
    if not events:
        # The kit can answer the most likely cause for free rather than listing
        # four diagnostics that all miss it. The client rewrites this config
        # continuously and may clobber the entry; someone may have reverted it
        # by hand. Either way "no events" is a symptom, not the finding.
        if state and not wrap_still_present(state):
            print(
                f"THE WRAP IS GONE. `{state['server_name']}` in\n"
                f"  {describe(Path(state['config_path']), state['scope'])}\n"
                "is no longer the entry setup wrote — it has been changed or restored\n"
                "since, so nothing has been passing through the proxy.\n\n"
                "  → run `python3 kit.py uninstall` to clear the stale state, then\n"
                "    setup again if you still want the trial.\n"
            )
            return 0
        print(NOT_CAPTURING)
        return 0

    s = summarize(events, events_path.stat().st_size)
    print(f"sessions             {s['sessions']}")
    print(f"tool calls           {s['tool_calls']}")
    print(f"intent captured      {s['sessions_with_intent']} of {s['sessions']} sessions")
    print(f"tool definitions     {len(s['tools'])} captured exactly as your server served them")
    if s["tools"]:
        print(f"                     {', '.join(s['tools'][:8])}"
              + (f", +{len(s['tools']) - 8} more" if len(s["tools"]) > 8 else ""))
    print(f"span                 {s['first']} → {s['last']}")
    print(f"events               {s['events']}")
    print(f"file size            {human_size(s['size_bytes'])}")
    if s["redactions"]:
        detail = ", ".join(f"{k}×{v}" for k, v in sorted(s["redactions"].items()))
        print(f"secrets redacted     {sum(s['redactions'].values())} ({detail})")
    else:
        print("secrets redacted     0 — no credential or PII patterns matched")
    print()
    print("This file has not left your machine, and nothing here sends it.")
    print("Read it before you decide whether it may: it contains the full arguments")
    print("and full results of every tool call, which the scrubber does not redact")
    print("(see SECURITY.md §6).")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not STATE_PATH.exists():
        raise Refuse(
            f"no setup state at {STATE_PATH}, so there is nothing recorded to reverse.\n"
            "  → if a wrap is in place, remove it by hand: the entry's `args` end with\n"
            "    `-- <your original command>`, which is what it was before."
        )
    state = load_state()
    path = Path(state["config_path"])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise Refuse(f"cannot read {path}: {e}\n  → nothing was changed.") from None

    new_text, restored = apply_unwrap(text, state)
    write_atomically(path, new_text)
    STATE_PATH.unlink()

    print(f"Restored `{state['server_name']}` in {describe(path, state['scope'])}")
    print("\nThe entry now reads:\n")
    print(json.dumps(restored, indent=2))
    print(f"\n{RESTART_NOTE}")
    left = []
    if EVENTS_PATH.exists():
        left.append(f"  {EVENTS_PATH}  ({human_size(EVENTS_PATH.stat().st_size)})")
    for b in sorted(TRY_DIR.glob("config-backup.*.json")):
        left.append(f"  {b}")
    if left:
        print("\nDeliberately left in place, for you to read or delete:")
        print("\n".join(left))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kit.py",
        description="Baton try kit — set up, receipt, remove. See SECURITY.md beside this file.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="wrap one configured stdio MCP server")
    p_setup.add_argument("server", nargs="?", help="name of the server entry to wrap")
    p_setup.add_argument("--config-file", help="config to use instead of searching")
    p_setup.add_argument("--tenant", help="label for this trial (default: a random trial-XXXX)")
    p_setup.add_argument("--vendor", help="label for the wrapped server (default: its name)")
    p_setup.set_defaults(fn=cmd_setup)

    p_receipt = sub.add_parser("receipt", help="what has been captured so far")
    p_receipt.set_defaults(fn=cmd_receipt)

    p_uninstall = sub.add_parser("uninstall", help="restore the original entry")
    p_uninstall.set_defaults(fn=cmd_uninstall)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except Refuse as e:
        print(f"kit.py {args.cmd}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

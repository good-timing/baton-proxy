#!/usr/bin/env python3
"""The Baton try kit: setup, receipt, uninstall.

Three commands, and nothing else. Everything they do is described in
``SECURITY.md`` beside this file; if the two ever disagree, the document is the
one that is wrong, because a stranger approved the trial by reading it.

None of the three opens a network connection. The capture is a file on the
person's own disk, and it moves only when they upload it themselves in a
browser, signed in to Baton. That is a claim a reviewer checks with §9's grep
rather than by believing this docstring.

Why setup and receipt are code and the rest of the trial is prose: a bad config
edit is silent for days, and a wrong receipt is a claim we repeat to someone
else. Those are the only two steps whose failure nobody witnesses. Everything
else in the flow — choosing a server, explaining what is about to happen,
handing over to a second terminal, deciding whether the file may leave — fails
loudly and immediately, so an agent narrating from ``CLAUDE.md`` is the right
medium.

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
import shlex
import shutil
import urllib.parse
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

# The project config this kit writes in project mode. CHECKOUT, not TRY_DIR:
# a project file applies to its exact directory only, so it has to sit beside
# the folder the person is told to start Claude Code in. A `try/.mcp.json` would
# load for sessions started in `try/`, which is where the KIT runs and not where
# the person runs their client.
MCP_PATH = CHECKOUT / ".mcp.json"

MODE_GLOBAL = "global"
MODE_PROJECT = "project"

# Which mode a plain `setup` uses. MODE_PROJECT since K1b: setup writes
# `.mcp.json` in this checkout and only READS their config. `--in-place` is the
# way back to the old behaviour.
#
# ⚠ The triage of the ~79 tests that drive setup's default is NOT in this
# commit. Flipping the constant and triaging its fallout were split so the V1
# hand-drive could run against true text first — a drive against the old
# default tests the mode we are replacing. The suite is red until that triage
# lands; the failure manifest is in the commit body.
DEFAULT_MODE = MODE_PROJECT

# The shape of state.json. 2 adds `mode`, `source_config_path` and
# `source_scope`, and gives `scope: None` a second meaning — the top level of
# the project config this kit writes, rather than of theirs.
#
# ⚠ Nothing reads this field, here or anywhere. It is a record for a person
# looking at the file, not a compatibility gate, so bumping it does not protect
# an older kit from a newer state file. What actually carries compatibility is
# `wrap_mode()` below.
STATE_VERSION = 2


def wrap_mode(state: dict) -> str:
    """Which mode wrote this state file.

    A COMPATIBILITY read, and it is a named function so that it says so. Absent
    means a state file written before `mode` existed, which is a real machine
    mid-trial, and every one of those is global.

    ⚠ This default stays MODE_GLOBAL when K1b flips ``DEFAULT_MODE``. The two
    constants share a value today and answer different questions: DEFAULT_MODE
    is what a fresh `setup` does, this is what an OLD state file meant. Spelled
    out at three call sites it was three things for whoever greps MODE_GLOBAL
    during the flip to individually recognise as not theirs.
    """
    return state.get("mode", MODE_GLOBAL)


# Names a baton-proxy invocation can appear under in someone's config.
_PROXY_NAMES = frozenset({"baton-proxy", "baton_proxy"})

# A redacted dump is still the restore recipe — it keeps the command, the args,
# and the env and header KEY names. Only values are hidden, and this says where
# the exact bytes are, so nobody is left guessing at what was elided.
#
# Worded over the whole record rather than over `env`, because the record is what
# gets redacted: an http entry hides its header values and shortens its url to
# scheme and host, and a pointer that mentioned only env would leave someone
# staring at a truncated URL with no idea it was deliberate.
STATE_POINTER = (
    "\n\n  What is hidden above is hidden, not lost — env and header values shown\n"
    "  as `<literal …>`, and any URL shortened to scheme and host. The entry\n"
    "  exactly as it was is in try/state.json under `original_entry`."
)

# The one place this kit names, and the only one it ever will. Pinned as a
# constant because CLAUDE.md says it too, and a document that names a different
# page than the code prints is wrong in the single place a person acts on it
# — the failure shape §4's injected-param pins were written for.
#
# Naming it costs nothing from the security posture, because THEY do the
# uploading: no network call is added anywhere by knowing where a file may go.
# Nothing in this checkout opens a connection of its own, so "nothing here sends
# it" is literally true and §9.1's grep sees no call site in `try/`.
SETUP_URL = "https://baton.goodtiming.ai/setup/proxy"

# The paste on the Baton Proxy page is a copy of PROMPT.md's text below the rule,
# pinned there to a named kit version rather than fetched when the page renders.
# So the console holds one of these strings and this file holds the other, and
# nothing in either repository can notice them drifting apart.
#
# This pair is what notices. PASTE_SHA256 is the sha256 of everything below the
# `---` in `try/PROMPT.md`, stripped of surrounding blank lines and ending in one
# newline; PASTE_VERSION is the release that paste ships in, and the test holds
# it equal to `baton_proxy.__version__`. Editing the paste breaks the hash;
# re-pinning the hash without bumping the version breaks the pair. The release
# then carries a version the console can be updated to and named by.
PASTE_VERSION = "0.6.11"
PASTE_SHA256 = "6e0dbeb917a2ad6bf66aa3cfde69b516664e484b14c6a163af23114e035ad659"


# Setup is the last thing that speaks before the kit goes quiet. Once they walk
# away from that window there is no agent anywhere that knows this kit exists,
# and nothing in the session they open next mentions Baton — so the ending is
# handed over here or it is never handed over at all.
#
# Future-conditional throughout. Nothing has been captured at setup time and
# nothing may ever be, so this says what they will find, never that there is
# already something to hand over.
def come_back() -> str:
    """Where to run `receipt` from, and when.

    A function rather than a module constant because `TRY_DIR` is read at call
    time everywhere else in this file, and the tests relocate it. A module-level
    f-string freezes the developer's own checkout into the string at import,
    which production never notices — `TRY_DIR` comes off `__file__` — and which
    silently points every assertion about this line at the wrong directory."""
    return (
        "Use the server the way you normally would, then come back and run\n"
        "  python3 kit.py receipt\n"
        # On its own line: this path is interpolated and can be long, and a
        # sentence continuing after it wraps past 80 columns.
        f"from {TRY_DIR}"
    )


# Printed by setup, and the only part of the trial that survives the handoff:
# once they are working in the other terminal, no session there knows this kit
# exists. It says how the trial ends without naming the file or the page: at
# setup time nothing has been captured and nothing may ever be, and the receipt
# states the ending at the moment there is something to state.
ENDING_NOTE = (
    "How the trial ends: use the server, then come back to the window you\n"
    "started from and say you are done. `python3 kit.py receipt` prints what\n"
    "landed and how to see it in Baton. Nothing is switched off until you run\n"
    "`python3 kit.py uninstall`."
)


# The ending, and the only place in the kit that names a destination. Named
# rather than sent: no code here opens a connection, and the person is the one
# who signs in and uploads. Two ways to reach a reader and one sentence behind
# both. The receipt prints it with the path filled in, and CLAUDE.md quotes it
# for the agent to say, so it is written once here and pinned by the tests.
#
# A function and not a module constant for the same reason `come_back()` is one:
# a module-level f-string freezes the developer's own `TRY_DIR` into the string
# at import, and every test that redirects it would then be reading an assertion
# about the wrong directory.
#
# The path is on its own line because it is interpolated and can be long: a
# sentence carried past it wraps past 80 columns in an ordinary terminal and
# puts half a thought under the tail of a path.
def setup_note(events_path: Path) -> str:
    return (
        f"It's at {events_path}\n"
        "(less that path to read it). Upload it on the Baton Proxy page,\n"
        f"{SETUP_URL}, and your session is there."
    )


# Run 6: the upload box takes a drag, and a path in a terminal is not something
# you can drag. `open -R` puts a Finder window in front of the person with the
# file already selected, which turns the last step from "find this path in a
# file dialog" into a drag they can see.
#
# Printed and never run. The receipt is a reporting command and stays one; what
# it can do is say the command, and the agent runs it (CLAUDE.md, *Ending it*).
#
# macOS only, and read at call time rather than at import so the tests can drive
# both platforms. `open -R` is macOS's, and Linux has no one equivalent worth
# guessing at: `xdg-open` on the FILE opens it in an editor rather than showing
# it in a manager, and there is no portable "reveal". So Linux is told nothing
# rather than told something that does the wrong thing.
def reveal_note(events_path: Path) -> str | None:
    if sys.platform != "darwin":
        return None
    # Quoted for the reason `_cd_to` states: `/Users/x/Client Work/app` is an
    # ordinary macOS path, and this line is printed to be pasted. It was the one
    # printed command in this file that did NOT quote — the two `cd` lines and
    # the `--from` rows all do — which is the same shape as a ruleset that
    # models separators for one field and not its neighbour. Milder than the
    # `cd` case, because this fires after a good capture and the path is in the
    # sentence beside it, so the cost is a Finder window that does not open.
    return f"Reveal it in Finder: open -R {shlex.quote(str(events_path))}"


# Not "fully quit and reopen", which was false and was verified false: a second
# terminal picked the wrap up with the original session still running. Each
# client process reads the config when it launches, so there is no daemon to
# flush. The narrower claim is the one that has to survive — a person who keeps
# working in the window they already had open captures nothing and has no way to
# see why.
RESTART_NOTE = (
    "This takes effect in the NEXT session your client starts. A new terminal is\n"
    "enough — nothing needs to be closed, because each client process reads the\n"
    "config when it launches. The session running now keeps the server it already\n"
    "launched, and nothing is captured through it."
)

# The proxy's provenance marker, copied rather than imported: `try/` runs from a
# checkout with no dependencies and no `baton_proxy` on the path (see the header).
# It is `INTENT_SOURCE_PARAM` in `src/baton_proxy/proxy.py`.
INTENT_SOURCE_PARAM = "injected_param"

# Printed under the intent line when it is zero and calls were captured. The
# first human-led run produced an empty intent layer and the agent explained it
# as "the agent never called `baton_annotate`" — a cause that was wrong then and
# is impossible now: intent rides parameters on the tool's own schema, so there
# is no prompt to refuse and no tool to skip. What the file cannot say is WHY the
# model left them off, and that half is stated rather than left to be filled in.
INTENT_IS_ZERO = (
    "                     The goal rides parameters the proxy adds to your own\n"
    "                     tools' schemas, so there is nothing here to refuse and\n"
    "                     nothing to switch off: a zero means the model left them\n"
    "                     off every call, no call failed for it, and your server\n"
    "                     saw the calls exactly as it always does. Why the model\n"
    "                     left them off is not in this file."
)

# Printed under the annotations line when it is zero. This is the number where
# a refusal IS one of the causes — and where a refusal is invisible, because a
# client-side denial never reaches the proxy. Both causes are named for the same
# reason the `cc` note exists: an unexplained zero gets explained downstream.
ANNOTATIONS_ARE_ZERO = (
    "                     Zero is the ordinary case: the tool is for friction —\n"
    "                     a wrong result, a dead end, a missing capability — and\n"
    "                     a session that went smoothly files none. It is also\n"
    "                     what a refusal looks like: if you declined the tool at\n"
    "                     a prompt, that stayed in your client and never reached\n"
    "                     this file. Nothing here can tell the two apart."
)


# Uninstall's version, and deliberately not the constant above. Nothing is
# pending here and nothing is inert: the config already says what it said before
# setup, so the only thing left to say is what the next session will do.
UNINSTALL_NOTE = (
    "New sessions will use your original server again. The session running now\n"
    "keeps the wrapped server it already launched, so that session is the last\n"
    "place the proxy is still in the path."
)

# Only printed where the restore verified. Saying "new sessions will use your
# original server again" directly under a warning that the file does not match
# what was recorded asserts the thing the warning just withdrew — and what a
# session loads is exactly the file that failed the comparison.
UNVERIFIED_NOTE = (
    "What a new session loads is whatever is in that file, which is the thing\n"
    "that did not match — so check it before assuming the trial is off your\n"
    "machine. The session running now keeps the server it already launched\n"
    "either way."
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

    This is where it deliberately diverges from a plain reader. Merging every
    scope into a flat ``{name: entry}`` and keying the project block on
    ``os.getcwd()`` is correct for reading, wrong twice for writing:
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


def is_http(entry: dict) -> bool:
    """Does the CLIENT reach this server over the network rather than launch it?

    Deliberately the LOOSE predicate — ``type: "http"`` or the mere presence of a
    ``url`` — because all it decides is which *explanation* an entry gets.
    Whether an http entry can actually be wrapped is ``http_bridge``'s question,
    and that one is strict."""
    if entry.get("type") == "sse":
        return False
    return entry.get("type") == "http" or "url" in entry


def bearer_header(entry: dict) -> tuple[str | None, list[str]]:
    """``(token, other_header_names)`` — the ONE place a bearer is recognised.

    Two callers depend on this agreeing with itself: ``http_bridge`` decides
    whether an entry can be wrapped, ``not_wrappable_reason`` explains why one
    cannot. A second copy of "is this a bearer" that drifted from the first would
    offer an entry as a candidate and then describe it as unwrappable, or worse
    the reverse — so there is one copy.

    The token comes back WITHOUT the ``Bearer `` prefix, because that is the form
    the bridge wants: ``transport_http`` composes ``Authorization: Bearer
    {token}`` itself, and passing the prefix through would put it on the wire
    twice. The value is never resolved — a ``${VAR}`` reference is carried across
    as a reference, which is the entire reason this move is safe."""
    headers = entry.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    auth = next((v for k, v in headers.items() if str(k).lower() == "authorization"), None)
    others = sorted(str(k) for k in headers if str(k).lower() != "authorization")
    if not isinstance(auth, str) or not auth.strip().lower().startswith("bearer "):
        return None, others
    # strip() again after the slice: `Bearer  ${X}` (two spaces) would otherwise
    # carry a leading space into the env var and onto the wire.
    token = auth.strip()[len("bearer ") :].strip()
    return (token or None), others


def http_bridge(entry: dict) -> tuple[str, str] | None:
    """``(url, token)`` if this entry is the ONE remote shape the bridge carries.

    The narrowness IS the safety property. ``run_http_proxy`` sends exactly one
    header of its own — ``Authorization: Bearer $BATON_UPSTREAM_AUTH_TOKEN`` — so
    an entry is carryable only when its auth is a bearer token *written in the
    config* and it sends no other header.

    Every other shape refuses, and each refusal prevents the same failure: an
    entry whose credential we could not carry would wrap cleanly, print success,
    and surface as a dead server in the next session started — days later, with
    pointing at the cause. That failure is what this whole kit is shaped to
    avoid.

    In particular **an http entry with no credential at all stays refused**,
    which looks over-cautious and is not. That shape is ambiguous: it is either a
    public endpoint, or an OAuth server whose token the CLIENT holds and never
    writes to the file. Wrapping the second kind produces exactly the dead server
    above, and nothing in the config distinguishes them.

    stdio wins ties by construction. An entry carrying both a ``command`` and a
    ``url`` is a hand-made oddity; demoting its command is reversible, dropping
    it is not."""
    if is_stdio(entry) or not is_http(entry):
        return None
    url = entry.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    token, others = bearer_header(entry)
    if token is None or others:
        return None
    return url.strip(), token


# The two words the candidate list marks each offered row with. CLAUDE.md uses
# the same two, and gates two extra warnings on the second — a remote wrap puts
# a process of ours on the machine holding their bearer token, which someone who
# approved the stdio story has not yet approved. Leaving the rows unmarked left
# the agent to infer the kind by going back into the config, and an inference it
# can skip is a warning the person may never hear.
KIND_STDIO = "stdio"
KIND_REMOTE = "remote"


def is_wrappable(entry: dict) -> bool:
    """Can setup replace this entry with a baton-proxy one?

    Two disjoint classes: a stdio server whose command we demote to the proxy's
    argument, and a remote Streamable-HTTP server with a bearer in the config,
    which we bridge. Everything else gets a reason from ``not_wrappable_reason``
    rather than silence."""
    return is_stdio(entry) or http_bridge(entry) is not None


def wrappable_kind(entry: dict) -> str:
    """Which of the two wrappable classes an offered entry is.

    Only meaningful for an entry ``is_wrappable`` accepted: the two classes are
    disjoint there, so "not stdio" is exactly "the bridged remote one"."""
    return KIND_STDIO if is_stdio(entry) else KIND_REMOTE


def not_wrappable_reason(entry: dict) -> str:
    """Why a single entry ``is_wrappable`` rejected cannot be wrapped.

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
    if is_http(entry):
        headers = entry.get("headers")
        headers = headers if isinstance(headers, dict) else {}
        token, others = bearer_header(entry)
        url = entry.get("url")
        if token is not None and not (isinstance(url, str) and url.strip()):
            # Checked BEFORE the bearer line below, which would otherwise print
            # "http, bearer token in the config" — the exact phrase every document
            # now defines as the WRAPPABLE class — under the heading "Not
            # wrappable". This list is how a prospect's own run reports which
            # classes their config holds, so a wrong label there is a wrong
            # measurement, not just a confusing sentence.
            return "http, no endpoint url"
        if token is not None:
            # A SOLE bearer is wrappable now, so it does not reach here through
            # the candidate list. This is the bearer-PLUS-other-headers case: the
            # bridge sends one header of its own and cannot carry the rest.
            what = "http, bearer token in the config"
            if "${" in token:
                what += " (a ${VAR} reference)"
            return what + (f", plus {', '.join(others)}" if others else "")
        if headers:
            return "http, custom headers (" + ", ".join(sorted(str(k) for k in headers)) + ")"
        return "http, no credential in the config"
    return "no usable launch command"


_PROJECT_DIR_REF = re.compile(r"\$\{CLAUDE_PROJECT_DIR\b")


def _path_candidate(value: str) -> str:
    """The part of an argument that could be a path.

    `--config=logs/app.json` is a relative path wearing a flag. Splitting on the
    LAST `=` leaves the value; an argument with no `=` is returned whole."""
    return value.rsplit("=", 1)[-1] if "=" in value else value


def _relative_path_like(value: str, *, exempt_npm_and_url: bool = True) -> bool:
    """Does this string resolve against a working directory?

    Widened 2026-09-17 after review: the first version took only `./` and `../`,
    which misses `dist/index.js` — the argument of every built-TypeScript MCP
    server there is, and a silent break on the move. `node server.js` is missed
    too and cannot be caught here at all; `_names_a_file_in` covers that one
    where a base directory is known.

    Two exemptions, and each is a real entry rather than a hypothetical:

    - **`@scope/name`** is an npm package, not a path.
      `npx -y @modelcontextprotocol/server-filesystem` is the most common MCP
      entry written, and refusing it would refuse most trials.
    - **A URL.** `https://` carries separators and resolves against nothing.

    Everything else with a separator and no absolute root is treated as a path.
    That direction is deliberate: a false positive costs a trial that would have
    worked and says `--in-place` in the same breath, while a false negative is a
    server that dies in their next session with nothing pointing at the cause.

    ``exempt_npm_and_url`` is False for the ``command`` field, where neither
    exemption can apply: a command is the executable, so a separator in it is a
    path by definition, and neither an npm spec nor a URL is launchable. It is a
    parameter rather than a second copy of the rule so the two callers cannot
    drift — the width difference is deliberate, the rule underneath is one rule.

    ⚠ That last sentence is about WIDTHS OF THIS RULE, and does not forbid a
    different rule. ``_explicitly_relative`` below is a different rule: this one
    asks whether a string is SHAPED like a path, which is the right question for
    a launch position and the wrong one for an environment value, where a
    secret has the same shape. A review read the sentence as covering that case
    and reported the second function as an unfixed violation of it, so the scope
    is stated here instead of inferred.
    """
    # The WHOLE value first, before the `=` split. `/opt/my=dir/bin/server` is an
    # absolute path that happens to contain an `=`; splitting it would leave
    # `dir/bin/server`, which has a separator and no root, and the entry would be
    # refused for being relative when it is not. Caught by probing the refactor
    # that introduced it rather than by a test, so here is the test: it is the
    # first row of `test_an_entry_that_travels_is_left_alone`.
    if Path(value).is_absolute():
        return False
    candidate = _path_candidate(value)
    if not candidate:
        return False
    if exempt_npm_and_url and (candidate.startswith("@") or "://" in candidate):
        return False
    if Path(candidate).is_absolute():
        return False
    return "/" in candidate or os.sep in candidate


def _explicitly_relative(value: str) -> bool:
    """Does this value SAY it is a relative path, rather than merely look like one?

    The env-field rule. ``_relative_path_like`` asks a question about shape — a
    separator, no absolute root — which is the right question for an argument,
    where a bare word in a launch position is usually a path. It is the wrong
    question for an environment value, because plenty of values that are not
    paths carry a ``/``: the base64 alphabet has one, so an AWS secret key trips
    it about half the time, and `ghp_aB3/dE5fG7` trips it every time.

    ``./`` and ``../`` are unambiguous. A value carrying one is stating that it
    resolves against a working directory, which is exactly what breaks when the
    entry moves. Everything else in env is left to ``_names_a_file_in``, which
    proves the claim against the filesystem instead of guessing from shape.

    ⚠ Runs on ``_path_candidate(value)``, not on the raw value, and that is not
    tidiness. An env value can carry ARGUMENT grammar: ``NODE_OPTIONS`` and
    ``JAVA_OPTS`` hold flags, so ``NODE_OPTIONS=--require=./instrument.js``
    arrives here as ``--require=./instrument.js``. Checking the head of that
    string finds ``--require`` and misses the path. Measured 2026-09-17: the
    first version of this function returned None for exactly that value while
    the identical string in ``args`` was refused. Splitting first is what keeps
    the two fields answering the same question about the same substring.

    What this gives up, stated rather than glossed: ``CONFIG=config/app.json``
    where that file does NOT exist under the entry's base is no longer refused.
    That case is already outside the guard's reach when ``base`` is None, which
    ``_names_a_file_in``'s own docstring concedes for every top-level entry."""
    candidate = _path_candidate(value.strip())
    return candidate.startswith(("./", "../", ".\\", "..\\"))


def _names_a_file_in(base: Path | None, value: str, *, files_only: bool = False) -> bool:
    """Does this argument name something that exists in the entry's own directory?

    The check `_relative_path_like` cannot do. `node server.js` carries no
    separator, so nothing about its shape says path — but if `server.js` sits in
    the directory the entry is scoped to, it is one, and it stops resolving the
    moment the entry moves.

    Only meaningful for an entry with a directory to resolve against, which is a
    project key in `~/.claude.json`. A top-level entry has no base: the working
    directory a stdio server is launched in is not documented, which is why a
    relative path there is already unreliable.

    ``files_only`` is set for environment values, and it is the difference
    between a guard and a nuisance. `NODE_ENV=production` is an ordinary
    environment value and an ordinary project has a `production/` directory, so
    a directory match there refuses a config with nothing wrong with it —
    measured 2026-09-17, on `production`, `test` and `debug`. A refusal that
    fires on a normal config is the sentence that stops the next prospect, and
    `--in-place` being offered in the same breath does not excuse it. Arguments
    keep the wider check: `node server` resolving to a directory is a real
    launch, and an argument is a position where a bare word is usually a path.
    """
    if base is None:
        return False
    candidate = _path_candidate(value)
    if not candidate or candidate.startswith("-") or Path(candidate).is_absolute():
        return False
    try:
        target = base / candidate
        return target.is_file() if files_only else target.exists()
    except OSError:  # pragma: no cover - an unreadable base is not worth a branch
        return False


def cwd_dependent_reason(entry: dict, base: Path | None = None) -> str | None:
    """Why copying this entry into another directory would break it, or None.

    The guard that project scope makes necessary. ``kit.py``'s wrap has always
    been in place: ``start_where`` says so in its own docstring — "the kit wraps
    in place and never moves an entry between scopes, so whatever directory rule
    they already had is the one that survives the trial." Writing the entry into
    ``baton-proxy/.mcp.json`` moves it, and the client then launches the server
    from somewhere else. A relative path resolves against the new place, the
    server dies, and it dies in their NEXT session rather than in front of us —
    the same delayed failure ``not_wrappable_reason`` exists to prevent.

    Two classes, both verified against ``code.claude.com/docs/en/mcp.md`` on
    2026-09-17 rather than recalled:

    - **A relative path.** There is no ``cwd`` field on a server entry, and the
      working directory a stdio server is launched in is not documented at all.
      So a relative path is already unreliable; moving it makes it wrong.
    - **A ``${CLAUDE_PROJECT_DIR}`` reference.** The docs: *"Claude Code sets
      ``CLAUDE_PROJECT_DIR`` in the spawned server's environment to the project
      root"*, and expansion happens in ``command``, ``args``, ``env``, ``url``
      and ``headers``. The project root IS the directory the entry is scoped to,
      so this one reference means something different in ``baton-proxy/`` than
      it meant where they wrote it. It is the case a slash-hunting check cannot
      see, and the only reason it is here is that the docs were read.

    ``base`` is the directory the entry is scoped to, when it has one. It buys
    the one check shape cannot make — see ``_names_a_file_in``.

    Runs on the ORIGINAL entry, before ``build_wrapped_entry`` demotes the
    command into ``args`` and the paths stop being where a reader expects them.
    """
    # FIRST, and over the whole entry: the reference can sit in any of the five
    # expanded fields, and the reason is the same wherever it is.
    #
    # Ordered ahead of the path rules rather than after them, which is where it
    # sat until review caught it. `${CLAUDE_PROJECT_DIR:-.}/bin/server` has a
    # separator and no absolute root, so the relative-path rule matched it first
    # and reported a `${VAR}` reference as a relative path — telling the person
    # to make a path absolute when what they have is not a path and cannot be
    # made one without hardcoding their project root.
    for field in ("command", "args", "env", "url", "headers"):
        if _PROJECT_DIR_REF.search(json.dumps(entry.get(field), ensure_ascii=False)):
            # A bare fragment, like `not_wrappable_reason`'s. The caller composes
            # the sentence and owns the line breaks; this returned its own
            # indentation for a layout that did not exist yet.
            return (
                f"its `{field}` refers to ${{CLAUDE_PROJECT_DIR}}, which Claude Code sets "
                "to the directory the entry is scoped to — a different directory once "
                "the entry is copied"
            )

    # ⚠ NO FIELD PRINTS ITS VALUE HERE. env, args and command alike.
    #
    # An earlier version of this comment argued the opposite for args, citing
    # `redact_entry`'s documented limit: "there is no way to tell which argument
    # is secret, and blanking args would destroy the restore recipe these dumps
    # exist to be." THAT ARGUMENT DOES NOT REACH THIS FUNCTION. It is about
    # printing a whole entry as a recipe someone restores from. A refusal is not
    # a recipe — it names ONE offending position and stops — so hiding that one
    # value destroys nothing.
    #
    # And the cost of being wrong was measured, on the most common remote-MCP
    # entry there is:
    #
    #   npx -y mcp-remote https://… --header "Authorization: Bearer abc/def"
    #   -> argument 5 is a relative path (`Authorization: Bearer abc/def`)
    #
    # A bearer token on stderr, in a refusal that is itself false. Position is
    # enough to act on: "argument 5" is an index into their own array, and they
    # are looking at the config while they read it.
    command = entry.get("command")
    # The same rule as the arguments get, minus the two exemptions — a command
    # with a slash in it is a path by definition, while `node` and `python3`
    # resolve against PATH and travel fine.
    if isinstance(command, str) and _relative_path_like(command, exempt_npm_and_url=False):
        return f"its launch command is a relative path ({hidden_label(command)})"

    for i, arg in enumerate(str(a) for a in entry.get("args") or []):
        if _relative_path_like(arg):
            return f"argument {i + 1} is a relative path ({hidden_label(arg)})"
        if _names_a_file_in(base, arg):
            return (
                f"argument {i + 1} names a file in the entry's own directory ({hidden_label(arg)})"
            )

    env = entry.get("env")
    for key, value in (env if isinstance(env, dict) else {}).items():
        if not isinstance(value, str):
            continue
        # ⚠ NEVER the raw value. Every other printer in this file goes through
        # `shown_env_value`, and this one did not: it interpolated the value
        # straight into a refusal that `cmd_setup` prints. Measured 2026-09-17 —
        # an `AWS_SECRET_ACCESS_KEY` was printed in full to stderr, which
        # `redact_entry`'s own docstring explains is worse than an ordinary CLI
        # leak because this kit is narrated by an agent and whatever it prints
        # is read into a model's context by design. `try/CLAUDE.md:53` tells the
        # agent never to read out a value the commands hid; here the command hid
        # nothing.
        shown = shown_env_value(key, value)
        # ⚠ NARROWED for env, and only env. `_relative_path_like` treats any
        # value with a separator and no root as a path. The base64 alphabet
        # contains `/`, so a 40-character AWS secret key trips it roughly half
        # the time — measured, along with `ghp_aB3/dE5fG7`. That is not a path
        # and no amount of `--in-place` makes the refusal true.
        #
        # `5e996d3` already found this exact class ("the guard was refusing
        # normal configs") and narrowed the OTHER env check with `files_only`,
        # measuring `production`, `test` and `debug`. It did not look at this
        # one. The rule it should have carried: an env value is a path when it
        # SAYS it is (`./`, `../`) or when it demonstrably names something in
        # the entry's own directory. Shape alone is an argument's evidence, not
        # an environment value's.
        #
        # `_relative_path_like`'s docstring justifies false positives as costing
        # "a trial that would have worked". That was written for `args` and it
        # does not cover this: here a false positive also prints a credential.
        if _explicitly_relative(value):
            return f"its `{key}` environment value is a relative path ({shown})"
        # Applied to env as well as args. Review found it on args only, which
        # left `{"DB": "data.sqlite"}` — a bare filename sitting in the entry's
        # own directory — passing while the identical string in `args` was
        # caught. Each field is a position that can be missed; this one was.
        if _names_a_file_in(base, value, files_only=True):
            return (
                f"its `{key}` environment value names a file in the entry's own directory ({shown})"
            )
    return None


def cwd_dependent_warning(name: str, reason: str) -> str:
    """The copied-entry path warning, as a warning rather than a refusal.

    Says three things in the order they become useful: what may break, that
    nothing of THEIRS is at risk, and what to do if the server does not start.

    ⚠ It does not say "this is broken". The guard reads shape, not truth — a
    header value with a slash in it trips the same rule a real relative path
    does, and that case is ordinary rather than exotic (`mcp-remote --header
    "Authorization: Bearer …"`). Overstating it here would send people to
    `--in-place` on a false alarm, which is the outcome dropping the refusal was
    meant to stop. "May not" is the honest strength.

    The reason fragment arrives already redacted — `cwd_dependent_reason` hides
    every value it names, in all three positions."""
    return (
        f"⚠ One thing to know about `{name}`: {reason}.\n"
        "  The copy in this checkout is launched from a different directory than\n"
        "  your own entry is, so a path like that may not resolve there. Your own\n"
        "  config was not changed, so your normal server is unaffected either way.\n"
        "  If the wrapped server does not start, or the receipt shows nothing was\n"
        "  captured, this is the first thing to check. Running setup again with\n"
        "  --in-place wraps the entry where it already sits, which does not move\n"
        "  it. That edits the config file the entry is in."
    )


def is_proxy_invocation(cmd: list[str]) -> bool:
    """Does this command LEAD with a baton-proxy launch, in the two head forms?

    Narrow on purpose: this is what ``unwrap_command`` consumes. Widening it
    would move unwrap. The broader question — "is this entry a proxy at all" —
    is ``is_wrapped``, which sweeps every token."""
    if not cmd:
        return False
    head = os.path.basename(cmd[0])
    rest = cmd[1:]
    if head in _PROXY_NAMES:
        return True
    return (
        head.startswith("python") and len(rest) >= 2 and rest[0] == "-m" and rest[1] in _PROXY_NAMES
    )


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


# A value that is exactly a ``${VAR}`` reference is a POINTER to a credential,
# not the credential — it is the pattern every MCP client's docs recommend, and
# showing it is what makes a printed entry checkable. Anything else in an env
# value may be the literal secret.
_VAR_REF = re.compile(r"^\$\{[^}]*\}$")

# The same pointer, allowed one leading scheme word — ``Bearer ${ACME_TOKEN}``,
# the shape every remote MCP config uses. Deliberately NOT "contains a ${VAR}
# anywhere": ``Bearer sk-live-abc ${X}`` does hold a literal, and labelling that
# a reference would be the same false claim in the other direction.
#
# The leading word is an ALLOWLIST of auth schemes, not ``\S+``. ``\S+`` reads
# "one token, then a reference", which is true of ``sk-live-abc123 ${SIG}`` —
# a whole credential sitting in the prefix slot, printed to the reader under a
# label saying no literal is present. The two-token fixture above cannot catch
# that, because its literal is the SECOND word. Scheme names are a short closed
# set; a credential is not in it.
_AUTH_SCHEMES = ("bearer", "basic", "digest", "token", "apikey")
_VAR_REF_VALUE = re.compile(
    r"^(?:(?:" + "|".join(_AUTH_SCHEMES) + r")[ \t]+)?\$\{[^}]*\}$", re.IGNORECASE
)

HIDDEN = "<literal value, not shown>"
HIDDEN_VAR_REF = "<${VAR} reference, not shown>"


def hidden_label(value: str) -> str:
    """Which not-shown label is TRUE of this value.

    Neither label shows anything; the choice is only about what we assert. A
    header of ``Bearer ${ACME_TOKEN}`` is not a literal, and saying it is makes
    a false statement about the reader's own config — inside the refusal path
    that exists so they can reconcile it by hand.

    Used for every hidden value, ``env`` and ``headers`` alike, rather than
    fixed on the field that surfaced it: the rule is about the value, and this
    file has already paid twice for scoping a rule to one field."""
    return HIDDEN_VAR_REF if _VAR_REF_VALUE.match(value.strip()) else HIDDEN


# Exactly what build_wrapped_entry writes. Keyed to the literal names rather than
# a ``BATON_`` prefix on purpose: SECURITY.md §7 contemplates a user having their
# OWN variable beginning BATON_, and a prefix test would print its literal value
# on the grounds that we must have written it. We did not.
_OUR_ENV_KEYS = frozenset({"PYTHONPATH", "BATON_TENANT_ID", "BATON_VENDOR_ID", "BATON_EVENT_SINK"})


def shown_env_value(key: str, value: str) -> str:
    """What may be printed for one env value.

    Shown: the variables we wrote ourselves (setup's whole purpose is to show
    what it wrote, and `launch_check` needs `PYTHONPATH` readable), and any value
    that is exactly a ``${VAR}`` reference. Hidden: everything else, which is
    where a literal token lives.

    Deliberately strict at the edge: ``/usr/local/bin:${PATH}`` is a composite,
    so it hides. Losing a little readability on an unusual PATH is the cheaper
    error than printing a token that happens to sit beside one."""
    if key in _OUR_ENV_KEYS:
        return value
    return value if _VAR_REF.match(value.strip()) else hidden_label(value)


def redact_entry(entry: dict) -> dict:
    """A copy of an entry safe to print, with env VALUES collapsed by the rule above.

    Every site that shows an entry goes through this. ``safe_endpoint`` set the
    rule for the candidate list — name it, never quote it — and the entry prints
    predate that rule; this is the same rule applied to the record rather than to
    one field, which is the lesson the URL leak taught on this file already.

    It matters more here than on an ordinary CLI because this kit is narrated by
    an agent: whatever it prints is read into a model's context by design, so
    "it is only the user's own terminal" was never the whole story.

    It covers ``env``, ``headers`` and ``url``, not just env — because one caller
    passes an entry we did NOT write. ``apply_unwrap``'s third refusal prints
    ``current``, whatever the person hand-edited the entry into, and that can be
    an http shape holding a bearer in ``headers`` or a token in the ``url`` path
    (Zapier, Composio). Scoping this to ``env`` would be the same mistake as
    scoping the no-header-values rule to the candidate list: enforced on a field
    instead of on the record.

    Structure is never altered — only values change, so a printed entry is still
    an accurate picture of the shape that is in the config.

    Known limit, stated in SECURITY.md rather than papered over: a credential
    passed as a command-line ARGUMENT (``--api-key sk-…``) still prints. There is
    no way to tell which argument is secret, and blanking args would destroy the
    restore recipe these dumps exist to be."""
    out = dict(entry)
    env = entry.get("env")
    if isinstance(env, dict):
        out["env"] = {k: shown_env_value(str(k), str(v)) for k, v in env.items()}
    headers = entry.get("headers")
    if isinstance(headers, dict):
        # Header names, never header values — the rule safe_endpoint already
        # follows for the candidate list. No ${VAR} exemption: a bearer header is
        # a credential slot whatever shape its value takes. The LABEL still tells
        # the truth about which shape it is; that costs no visibility.
        out["headers"] = {k: hidden_label(str(v)) for k, v in headers.items()}
    url = entry.get("url")
    if isinstance(url, str) and url:
        out["url"] = safe_endpoint(url)
    return out


def entry_json(entry: dict) -> str:
    """The one way an entry reaches a terminal."""
    return json.dumps(redact_entry(entry), indent=2)


def unwrap_command(cmd: list[str]) -> list[str]:
    """Peel any leading baton-proxy invocation off a command.

    It recovers the wrapped command; it recurses to handle an accidental
    multi-wrap, and a wrapper with no ``--`` separator is left alone because
    there is no upstream command to recover.
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

    Two shapes go in and one shape comes out. A **stdio** entry keeps its key
    name and its env, and its command is demoted to the proxy's argument. An
    **http** entry — only the bearer-in-config shape ``http_bridge`` accepts —
    becomes a stdio entry running the proxy's ``--url`` bridge, which is how the
    proxy has reached remote servers since 0.2.2. Either way the key name never
    changes, so every session, script and habit that named this server still
    reaches it.

    Rules encoded here rather than in prose because each one is a silent failure
    if a human or a model gets it wrong:

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

    bridge = http_bridge(original)
    if bridge is not None:
        url, token = bridge
        # The credential moves slot — from a `headers` value the client expands,
        # to an env value the client expands — and is never resolved on the way.
        # A `${VAR}` reference stays a reference and the client resolves it at
        # launch, exactly as it did in the header. That is what keeps this
        # function's central promise ("we never see a credential") true for the
        # http class, which the design note had assumed it would cost.
        #
        # Written after the user's env is copied in, like the other BATON_ vars,
        # so a stray value of the same name cannot shadow it. NOT added to
        # `_OUR_ENV_KEYS`: we write the KEY, but the VALUE is theirs, and a
        # literal token here must stay hidden by the ordinary redaction rule.
        env["BATON_UPSTREAM_AUTH_TOKEN"] = token
        # A fresh dict, not `dict(original)`. Carrying `type`/`url`/`headers`
        # alongside a `command` would leave the client an entry claiming to be
        # both transports at once — ambiguous at best, and it would hand the
        # bearer header to a server that is now local. Unrecognised keys DO
        # survive, same as the stdio path: they are the user's, not ours.
        wrapped = {k: v for k, v in original.items() if k not in ("type", "url", "headers")}
        wrapped["command"] = interpreter
        wrapped["args"] = ["-m", "baton_proxy", "--url", url]
        wrapped["env"] = env
        return wrapped

    cmd = [original.get("command", ""), *[str(a) for a in original.get("args") or []]]
    upstream = unwrap_command(cmd)

    wrapped = dict(original)
    # sys.executable, never the bare name. `python3` is resolved against the MCP
    # CLIENT's PATH, and a GUI-launched client on macOS inherits launchd's
    # minimal PATH where `python3` is /usr/bin/python3 — 3.9, which cannot import
    # baton_proxy (it needs >=3.11). The server would then die at client launch,
    # days after setup printed success.
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


def build_project_config(
    original: dict,
    *,
    name: str,
    source_path: Path,
    source_scope: str | None,
    tenant_id: str,
    vendor_id: str,
    src_dir: str,
    events_path: str,
    interpreter: str = sys.executable,
) -> tuple[str, dict]:
    """The project-mode twin of ``apply_wrap``. Returns ``(file_text, state)``.

    Deliberately NOT routed through ``apply_wrap``. That function's job is
    surgery on a config the person owns: it parses their text, replaces one
    entry inside it, and re-serialises in the shape it arrived in, because
    ``~/.claude.json`` holds far more than MCP servers and belongs to another
    tool. None of that applies here. This file is ours, it is new every time,
    and it holds exactly one entry — so it is built, not edited.

    The state it returns carries BOTH locations, and that is the point of the
    shape. ``config_path``/``scope`` say where the wrap lives, which is what
    ``receipt`` checks and ``uninstall`` removes. ``source_config_path``/
    ``source_scope`` say where the entry was COPIED FROM, which is the only
    record that their own config was read and not touched — and the sentence
    the trial has to be able to say honestly.
    """
    wrapped = build_wrapped_entry(
        original,
        tenant_id=tenant_id,
        vendor_id=vendor_id,
        src_dir=src_dir,
        events_path=events_path,
        interpreter=interpreter,
    )
    # `mcpServers` at the top level: the project-config shape Claude Code reads.
    # Two spaces and a trailing newline, like every other file this kit writes.
    text = json.dumps({"mcpServers": {name: wrapped}}, indent=2, ensure_ascii=False) + "\n"
    state = {
        "version": STATE_VERSION,
        "wrapped_at": datetime.now(UTC).isoformat(timespec="seconds"),
        # None, because the entry sits at the TOP LEVEL of the file we write.
        # `wrap_still_present` and `apply_unwrap` both read the entry back with
        # `entry_at(data, scope)`, and anything else here would send them
        # looking in a `projects` block this file does not have.
        "scope": None,
        "server_name": name,
        "original_entry": original,
        "wrapped_entry": wrapped,
        "tenant_id": tenant_id,
        "vendor_id": vendor_id,
        "events_path": events_path,
        "source_config_path": str(source_path),
        "source_scope": source_scope,
    }
    return text, state


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
            # `describe`, not a second hand-written version of it. This branch
            # said "(global mcpServers)" for any `scope is None`, which is the
            # same false sentence describe() was just fixed for, surviving in
            # the twin position — and here it would name ~/.claude.json to
            # someone whose entry was never in it.
            + f" ({describe(Path(state['config_path']), scope)})"
            + ".\n  → nothing was changed. Restore this entry by hand:\n\n"
            + entry_json(state["original_entry"])
            + STATE_POINTER
        ) from None
    current = block.get(name)
    if current is None:
        raise Refuse(
            f"`{name}` is no longer in the config; someone removed it after setup.\n"
            "  → nothing was changed. If you want it back, this is what it was:\n\n"
            + entry_json(state["original_entry"])
            + STATE_POINTER
        )
    if current == state["original_entry"]:
        # Already back to what it was — someone restored it by hand, or the
        # client rewrote it. There is nothing to undo and refusing here would be
        # a trap: setup refuses on the stale state, uninstall refuses on the
        # entry, and CLAUDE.md forbids the agent from deleting the state file to
        # escape. Clearing the state IS the remaining work.
        return config_text, current
    if not is_still_our_wrap(current, state):
        raise Refuse(
            f"`{name}` has been edited since setup wrapped it, so this kit will not\n"
            "  silently overwrite it. Both versions, for you to reconcile by hand:\n\n"
            "  --- in your config now ---\n"
            + entry_json(current)
            + "\n\n  --- what setup recorded as the original ---\n"
            + entry_json(state["original_entry"])
            + STATE_POINTER
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


# Everything else the proxy records as a request reaching the upstream server
# (`emitter.py`). Lists are counts-only traffic, but they are still traffic.
_OTHER_START_KINDS = frozenset(
    {"resource_read_start", "resource_list_start", "prompt_get_start", "prompt_list_start"}
)


def summarize(events: list[dict], size_bytes: int) -> dict:
    """The receipt's numbers. Counts only — no error counts, and no analysis.

    Analysis is what the file is FOR, and it happens with us in the room. The
    receipt's job is narrower: prove we captured a class of thing you have no
    other way to see."""
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    # Per session, not just the total. `sessions 2 / tool calls 2` is true of a
    # trial where both sessions worked and of one where a second server took
    # every call from the first — and the second is the one that needs saying.
    # Keyed on first timestamp so the rows read in the order they happened.
    calls_by_session: Counter[str] = Counter()
    # Tool calls are not the only way a session reaches the server. Counting
    # only `tool_call_start` reported a session that read resources as dead, and
    # then explained the zero by accusing another server of answering — for
    # traffic this file proves the proxy captured.
    other_by_session: Counter[str] = Counter()
    first_seen: dict[str, str] = {}
    # Two mechanisms, counted apart, because they fail for different reasons.
    # Intent rides the injected params on every tool call; `baton_annotate` is
    # the friction path and is the only one of the two a person can refuse. The
    # old single number merged them at session grain, which is how an empty
    # intent layer came to be explained as a refusal — a cause the file cannot
    # see, and not a cause of this number at all.
    calls_with_intent = 0
    agent_annotations = 0
    tool_calls = 0
    tools: list[str] = []
    stamps: list[str] = []
    kinds: Counter[str] = Counter()

    for e in events:
        kind = e.get("event_type", "")
        kinds[kind] += 1
        sid = e.get("session_id") or ""
        if e.get("captured_at"):
            stamps.append(e["captured_at"])
            if sid and (sid not in first_seen or e["captured_at"] < first_seen[sid]):
                first_seen[sid] = e["captured_at"]
        payload = e.get("payload") or {}
        if kind in _OTHER_START_KINDS and sid:
            other_by_session[sid] += 1
        if kind == "tool_call_start":
            tool_calls += 1
            if sid:
                calls_by_session[sid] += 1
            if payload.get("call_intent"):
                calls_with_intent += 1
        elif kind == "annotation":
            # The proxy synthesises ONE annotation per session out of the first
            # call's injected params, marked `intent_source="injected_param"`
            # (`proxy.py`). Counting those as agent-filed would report an agent
            # that called nothing as one that filed on every session — the same
            # oracle bug that would have failed the 2026-09-01 denial verify.
            if payload.get("intent_source") != INTENT_SOURCE_PARAM:
                agent_annotations += 1
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

    ordered = sorted(sessions - {None}, key=lambda s: (first_seen.get(str(s), ""), str(s)))
    per_session = [(str(s), calls_by_session[str(s)], other_by_session[str(s)]) for s in ordered]

    return {
        "sessions": len(sessions),
        "per_session": per_session,
        # Dead means nothing reached the server at all — not merely no TOOL call.
        "dead_sessions": sum(1 for _s, c, o in per_session if c == 0 and o == 0),
        "other_calls": sum(o for _s, _c, o in per_session),
        "calls_with_intent": calls_with_intent,
        "agent_annotations": agent_annotations,
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


_NOT_CAPTURING_HEAD = """\
No events have been captured yet.

That is a real answer, not an error — and it is worth getting to the bottom of
now rather than at the end of the trial. In order:

"""

# Written as a list rather than one block because one of the steps only exists
# for a project-scoped wrap: "start it from there" is the decisive question when
# the entry is scoped to a directory, and a false instruction when it is global,
# which is the same defect this checklist is here to catch.
_NOT_CAPTURING_STEPS = (
    "Has a NEW client session been started since setup ran? A session that was\n"
    "already running never sees the wrap. This is the usual cause.",
    "Has the wrapped server actually been used in one of those sessions? Opening\n"
    "a session is not enough; the agent has to call the server at least once.\n"
    "(A session that merely starts does record one tool-surface snapshot, so if\n"
    "even that is missing, the proxy is not in the path at all.)",
    "Is the entry that got wrapped the one you actually use? Check the server\n"
    "name and config path printed above.",
    "Does the wrapped command launch? Run exactly what setup wrote into the\n"
    "entry (printed above as `launch check`). If that fails, the server is dead\n"
    "in your client too — which you would have noticed.",
)


# Project mode only, and it goes FIRST, because it is the only cause on this
# list the person may already have produced by answering a question — and the
# only one with a switch they can see.
#
# Verified against `code.claude.com/docs/en/mcp.md` on 2026-09-17, not recalled:
# "For security reasons, Claude Code prompts for approval in interactive
# sessions before using project-scoped servers from `.mcp.json` files." A server
# waiting on that answer shows in `claude mcp list` as "Pending approval", and
# `claude mcp reset-project-choices` clears a previous answer.
#
# Without this step the checklist sends someone who dismissed that prompt off to
# check restarts and directories — none of which is the reason, and all of which
# read as the kit not knowing what it did.
def approval_step(folder: Path) -> str:
    """The project-config approval question, naming BOTH prompts.

    Corrected 2026-09-17 after review, against the same doc page: there are two
    gates, not one, and the step named only the second.

    - **Trusting the workspace.** The person is starting Claude Code in a folder
      they cloned minutes ago and have never opened. The docs tie approvals to
      it: `claude mcp list` and `claude mcp get` read `.mcp.json` approvals "only
      from settings files that aren't checked into the repository until you trust
      the workspace by running `claude` in it and accepting the workspace trust
      dialog."
    - **Approving the server.** "For security reasons, Claude Code prompts for
      approval in interactive sessions before using project-scoped servers from
      `.mcp.json` files."

    `claude mcp reset-project-choices` clears the SECOND only, so a step that
    named one prompt and one command sent someone who declined the first to run
    something that could not help them.

    `claude mcp list` is read in a directory, and this checklist already has a
    step devoted to people being in the wrong one, so the folder is named here
    rather than assumed.

    "declined" and "not answered", not "dismissed": the word "dismissed" was in
    the first version under a comment claiming the step was verified line by
    line against the docs, and it was not in the docs.
    """
    return (
        "Were the two prompts answered? Starting Claude Code in a folder for the\n"
        "first time asks whether you trust it, and a project config's servers are\n"
        "then approved separately before they can be used. A prompt that was\n"
        "declined or not answered leaves the server switched off.\n"
        f"  cd {shlex.quote(str(folder))} && claude mcp list\n"
        "shows whether the server is still pending. To be asked the SECOND one\n"
        "again, run this in the same folder:\n"
        "  claude mcp reset-project-choices\n"
        "The trust answer is given by starting Claude Code in the folder."
    )


def is_still_our_wrap(current: dict | None, state: dict) -> bool:
    """Is the entry on disk the one setup wrote, untouched since?

    One predicate, two callers — `apply_unwrap` before it restores, and
    `remove_project_config` before it deletes. Both had the comparison written
    out, and both use it to decide the same thing: whether a person has been in
    there by hand, which is the case where guessing is worst. A definition that
    changed in one copy and not the other would make one path refuse when it
    should not, and the other delete something it should not have touched."""
    return current == state["wrapped_entry"]


def entry_home(scope: str | None, config_path: str | Path) -> Path | None:
    """The one directory this entry loads for, or None if it loads everywhere.

    Three cases, and the kit had this logic written out by hand in each place
    that needed it — `not_capturing` and `start_where` — with a third copy due
    when the K8 guard needed a base to resolve paths against. One answer, three
    readers:

    - A **project scope** inside `~/.claude.json`: the scope key IS the path.
    - The **top level of a project config**, reached with `--src-config`: the
      file's own directory.
    - The **global config**: None. `~/.claude.json`'s top level loads wherever a
      session starts, which is the whole distinction `is_global_config` draws.
    """
    if scope is not None:
        return Path(scope)
    if is_global_config(config_path):
        return None
    return Path(config_path).parent


def scope_selector(scope: str | None) -> str:
    """The value `--from` takes for one scope, and the value the refusal prints.

    `global` for the top-level block, the project path otherwise. One function,
    so the string a person is shown is produced by the same code that matches
    what they then type back. A printed value that does not round-trip is worse
    than having no selector at all: it reads as the kit refusing their answer."""
    return "global" if scope is None else scope


def needs_approval(scope: str | None, config_path: str | Path) -> bool:
    """Does Claude Code gate this entry behind the project-config prompts?

    The rule is about the FILE, not about which mode this kit ran in — the
    version that asked `mode == MODE_PROJECT` got both edges wrong. It missed
    `--in-place --src-config <repo>/.mcp.json`, which wraps a project-scoped
    server in a real `.mcp.json` and is gated exactly the same way. And its
    justification for the other edge — that an entry the person already had "was
    approved long ago, if it ever needed to be" — assumed they had used that
    server in that folder, which this kit never checks.

    True for the top level of a config that is not `~/.claude.json`, which is
    what a project `.mcp.json` is. A project KEY inside `~/.claude.json` is a
    different thing: it is their own user-level file, and the docs gate
    `.mcp.json` files, not those.
    """
    return scope is None and not is_global_config(config_path)


def not_capturing(scope: str | None, config_path: str | Path) -> str:
    """The empty-file checklist, with the questions that apply to this wrap.

    The directory question applies whenever the entry is not in the global
    config — a project scope inside `~/.claude.json`, or the top level of a
    project config reached with `--src-config`. Both load for one directory;
    only `~/.claude.json` loads for all of them.

    The approval question applies to a project `.mcp.json`; see
    ``needs_approval``. It used to be decided by which mode this kit ran in,
    and that parameter is gone rather than left inert: keying this on the mode
    instead of on the file is the bug
    ``test_a_global_wrap_into_a_project_mcp_json_still_asks_about_approval``
    exists to catch, and an unused `mode` argument in the signature is an
    invitation to wire it back up."""
    steps = list(_NOT_CAPTURING_STEPS)
    home = entry_home(scope, config_path)
    where = None if home is None else str(home)
    if where is not None:
        steps.insert(
            1,
            "Was that session started from the directory this entry loads in?\n"
            f"  {where}\n"
            "A session started anywhere else loads your global servers only — the\n"
            "wrap never runs, and nothing is captured.",
        )
    if needs_approval(scope, config_path) and home is not None:
        steps.insert(0, approval_step(home))
    body = "".join(
        f"  {n}. {step}\n".replace("\n", "\n     ", step.count("\n"))
        for n, step in enumerate(steps, start=1)
    )
    return _NOT_CAPTURING_HEAD + body


# Dave's run: `toybox-baton`, a near-duplicate in global scope, answered all four
# tool calls while the wrapped `toybox` sat idle. Two causes rather than his one,
# because the kit cannot tell them apart and the other is at least as common on
# day one — a server that has simply not been used yet looks identical from here.
NOTHING_CALLED = """\
CONNECTED, BUT NOTHING CALLED IT. Your server started and we captured its tool
list, but no tool call ever reached it. Two things do this:

  1. The server has not been called yet. Opening a session is not enough — the
     agent has to use the server at least once. Ask for that, then run this
     again.
  2. Another server in your client is answering these tools, so the wrapped one
     sits idle. Run /mcp and look for a second entry with a similar tool list —
     a near-duplicate name is the usual shape.
"""

# The same finding inside a working trial, where it is a note and not a banner:
# calls landed, so the trial is not broken, and a session with no calls is
# ordinary if they simply did not use the server in it.
DEAD_SESSION_NOTE = """\
One or more sessions above recorded no calls. That is normal for a session where
you did not use the server. If you DID use it in one of them, another server in
your client is probably answering these tools — run /mcp and look for a second
entry with a similar tool list.
"""


def wrap_is_gone(state: dict, *, had_events: bool) -> str:
    """Row 3. The entry in the config is not the one setup wrote.

    Two readings, and the difference matters to the person: an empty file means
    nothing was ever captured, while a file with events in it means capture
    STOPPED — a distinction invisible in a total that only ever grows."""
    since = (
        "Anything counted above was captured before that; nothing has been\n"
        "captured since, and nothing will be."
        if had_events
        else "Nothing has been passing through the proxy."
    )
    # The CAUSE differs by mode, and naming the wrong one sends someone looking
    # in the wrong file. In global mode the usual cause is the client itself:
    # it rewrites `~/.claude.json` continuously and may put the entry back. In
    # project mode the file is ours, in our own checkout, and no client
    # maintains it — so it changed because a person changed or deleted it, and
    # saying "restored" would point them at a config that was never touched.
    cause = (
        "is no longer the entry setup wrote. That file is this kit's own, so it\n"
        "was edited or deleted by hand — your own config was never part of this."
        if wrap_mode(state) == MODE_PROJECT
        else "is no longer the entry setup wrote — it has been changed or restored\nsince."
    )
    return (
        f"THE WRAP IS GONE. `{state['server_name']}` in\n"
        f"  {describe(Path(state['config_path']), state['scope'])}\n"
        f"{cause} {since}\n\n"
        "  → run `python3 kit.py uninstall` to clear the stale state, then\n"
        "    setup again if you still want the trial.\n"
    )


STATE_CLEARED = (
    "Setup state has been cleared — this receipt is reading the event file left\n"
    "behind by a trial that has already been ended."
)

NOT_SET_UP = """\
No events, and no setup state — this folder has no record of a wrap.

The likely answer is that setup has not run here yet:

  python3 kit.py setup

If it DID run, then try/state.json is gone since: `uninstall` deletes it, and so
does deleting it by hand. This command reads only try/events.jsonl and
try/state.json, so it cannot tell you whether your MCP config still holds a
wrapped entry — that one you have to open and look at. Nothing here changed
anything.
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
            "  → your config entry is probably still wrapped, and the whole config as\n"
            "    it was before setup is in the newest `config-backup.*.json` beside\n"
            "    this file. Copy the entry back from there, then delete the state file.\n"
            "  → failing that, the wrapped entry says which shape it was. `args` of\n"
            "    `-m baton_proxy -- <command>` means it was a stdio entry running\n"
            "    <command>. `args` of `-m baton_proxy --url <endpoint>` means it was an\n"
            "    http entry for <endpoint>, whose `Authorization: Bearer` header value\n"
            "    is the entry's `BATON_UPSTREAM_AUTH_TOKEN`."
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
    # the not-found message misdirects. --src-config covers a project config.
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
                raise Refuse(
                    f"cannot read {p}.\n  → check the path passed to --src-config."
                ) from None
            continue
        for scope, name, entry in iter_entries(data):
            out.append((p, text, scope, name, entry))
    return out


def describe(path: Path, scope: str | None) -> str:
    """Where an entry lives, in one line, for a person reading it.

    The third branch is not new cosmetics for the project file this kit now
    writes — it is the same false sentence `is_global_config` was added to
    stop, still being printed by this function. `scope is None` means "the top
    level of whatever file was read", and for a project `.mcp.json` reached with
    `--src-config` that top level loads for ONE directory. Calling it "global
    mcpServers" told the person the opposite, in the line that names the file
    the kit is about to change.

    Branches on `entry_home`, not on `is_global_config` directly. That helper
    was extracted to retire this exact three-way fork from the places that had
    it written out by hand, and this function — 250 lines below it — was a
    fourth copy of the same decision. Two copies of "which directory does this
    entry load for" is how the receipt ends up describing a location the rest
    of the kit reasons about differently."""
    if scope is not None:
        return f"{path} · project {scope}"
    home = entry_home(scope, path)
    if home is None:
        return f"{path} · global mcpServers"
    return f"{path} · project config, loaded for sessions started in {home}"


def is_global_config(config_path: str | Path) -> bool:
    """Is this the file a client loads no matter where a session starts?

    Only `~/.claude.json` is. `scope is None` means the entry sits at the top
    level of whatever file was read, and `--src-config` exists so a project
    `.mcp.json` can be reached — whose top level loads for sessions started in
    its own directory and nowhere else. Reading None as "global" put a false
    sentence in front of exactly the person the directory fix was written for."""
    try:
        return Path(config_path).expanduser().resolve() == (Path.home() / ".claude.json").resolve()
    except OSError:  # pragma: no cover - an unresolvable home is not worth a branch
        return False


def _cd_to(where: str) -> str:
    """A `cd` a person can paste. Quoted, because `/Users/x/Client Work/app` is
    an ordinary macOS path and an unquoted one silently cds to `/Users/x/Client`,
    which loads global scope and captures nothing: the failure this whole line
    exists to prevent."""
    return f"    cd {shlex.quote(where)} && claude"


def start_where(scope: str | None, config_path: str | Path) -> str:
    """Where to start the client so the wrapped entry actually loads.

    Chosen from the scope setup already holds, never hardcoded — a fixed string
    is the defect this replaces. In the 2026-08-28 run the kit wrote the entry
    to `projects["/Users/davideyler/workplace"]` and then named its own `try/`
    directory as the place to restart from: a project-scoped server only loads
    for a session started from its own directory, so that instruction loads
    global scope, the wrap never starts, and the file stays empty for reasons
    the person cannot see.

    Two directories, and conflating them is the whole bug. `try/` is ours and
    the three commands run there; the project key is theirs and is where the
    client starts. Only their config decides the second one — the kit wraps in
    place and never moves an entry between scopes, so whatever directory rule
    they already had is the one that survives the trial."""
    # `entry_home(...) is None` is exactly "loads wherever you start from", which
    # is what this branch is about. Same condition as before, read off the one
    # helper now rather than spelled out here for the third time.
    if entry_home(scope, config_path) is None:
        return (
            "Open a second terminal and start Claude Code the way you normally do.\n"
            "This entry is registered globally, so it loads wherever you start from."
        )
    if scope is None:
        # Not global, and the kit does not get to say how their client loads an
        # arbitrary file. It can say where the file sits and what that means for
        # the shape it is nearly always in.
        holder = Path(config_path).parent
        return (
            "Open a second terminal and start Claude Code where this entry\n"
            "applies. It is at the top level of\n"
            f"  {config_path}\n"
            "which is not your global config — a project config is loaded for\n"
            "sessions started in the directory it sits in:\n\n"
            f"{_cd_to(str(holder))}"
        )
    try:
        already_there = Path(scope).expanduser().resolve() == Path.cwd().resolve()
    except OSError:  # pragma: no cover - an unreadable cwd is not worth a branch
        already_there = False
    if already_there:
        return (
            "Open a second terminal in this same directory and start Claude Code\n"
            f"there. The entry is scoped to {scope}, and it only loads for a session\n"
            "started from there."
        )
    return (
        "Open a second terminal and start Claude Code where this MCP server is\n"
        "registered:\n\n"
        f"{_cd_to(scope)}\n\n"
        "The entry is scoped to that directory. A session started anywhere else\n"
        "loads your global servers only, and captures nothing."
    )


def write_atomically(path: Path, text: str, *, default_mode: int = 0o600) -> None:
    """Write via a temp file in the same directory, then ``os.replace``.

    ``write_text`` truncates before it writes, so a crash mid-write leaves the
    user with a truncated ``~/.claude.json``. The backup makes that recoverable,
    but only if they find this document and read it; ``os.replace`` is atomic on
    the same filesystem and makes the window zero instead.

    ``default_mode`` is used when the file does not exist yet, which happens for
    the project config this kit writes in project mode — every other caller is
    rewriting a file that is already there. 0600 rather than the umask default,
    for the same reason ``write_state_file`` and ``write_backup`` are 0600: the
    entry is copied verbatim, so if their original carried a literal token
    rather than a ``${VAR}`` reference, that token is now in a file this kit
    created. A new 0644 file would publish it to every account on the box.
    """
    mode = path.stat().st_mode & 0o777 if path.exists() else default_mode
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


def write_state_file(state: dict) -> None:
    """Write state.json at 0600, because of what is IN it.

    It stores ``original_entry`` and ``wrapped_entry`` verbatim — including the
    env block, copied out of a ``~/.claude.json`` that is very often 0600. A
    plain ``write_text`` creates 0644 under the usual umask, which quietly
    republishes a protected credential to every account on the box. That is the
    same mode slip ``write_atomically`` documents at length for the config file;
    this is the file the kit authors itself, and it had no such guard.

    ``os.open`` sets the mode only when it CREATES, so the chmod is not
    redundant — a state file left behind by an earlier version stays 0644
    without it."""
    fd = os.open(STATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(state, indent=2) + "\n")
    os.chmod(STATE_PATH, 0o600)


def write_backup(source: Path, backup: Path) -> None:
    """Copy the whole config to ``backup`` at 0600, whatever the source's mode.

    This was ``shutil.copy2``, which copies the SOURCE's mode: a
    ``~/.claude.json`` at 0644 made a 0644 backup, and the backup is a verbatim
    copy of every server's env block, literal secrets included. SECURITY.md §2
    and §7 promise 0600, and a reviewer reads that promise, not this function.

    Same ordering as ``write_atomically``: the mode is 0600 before the first
    byte goes in. The ``fchmod`` is not redundant: ``os.open``'s mode applies
    only when it creates, and the umask can narrow it."""
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as dst:
        os.fchmod(dst.fileno(), 0o600)
        with open(source, "rb") as src:
            shutil.copyfileobj(src, dst)


def restored_matches_on_disk(path: Path, state: dict) -> bool:
    """Re-read the config and compare the entry to what setup recorded.

    Deliberately reads the FILE rather than trusting the value ``apply_unwrap``
    returned: an in-memory comparison is tautological (it returns the recorded
    original, so it always matches) and proves nothing about the write. This is
    what lets the printed entry hide values and still keep SECURITY.md §2's
    promise — the bytes it does not show are compared here."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return entry_at(data, state["scope"]).get(state["server_name"]) == state["original_entry"]
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
        print(entry_json(state["wrapped_entry"]))
        print(f"\n{RESTART_NOTE}")
        # The handoff again, not only on the first run. Someone re-running setup
        # days later has usually lost the window that carried it — a multi-day
        # trial is what the kit asks for, so cold re-entry is the normal case.
        print(f"\n{start_where(state['scope'], state['config_path'])}")
        print(f"\n{come_back()}")
        print(f"\n{ENDING_NOTE}")
        return 0

    found = discover(args.src_config)
    if not found:
        raise Refuse(
            "no MCP configuration found in "
            + ", ".join(str(p) for p in search_paths(args.src_config))
            + ".\n  → the trial needs one MCP server already configured and working."
            "\n  → if yours lives elsewhere, pass --src-config <path>."
        )

    if not args.server:
        wrappable = [
            (p, s, n, wrappable_kind(e))
            for p, _t, s, n, e in found
            if is_wrappable(e) and not is_wrapped(e)
        ]
        # Already-wrapped entries get their own line rather than falling out of
        # both lists — otherwise a machine whose only server is already wrapped
        # is told "there is nothing here to wrap", which is false, and the name
        # it hides is the one needed to reach the real refusal below.
        already = [(p, s, n) for p, _t, s, n, e in found if is_wrappable(e) and is_wrapped(e)]
        remote = [(n, not_wrappable_reason(e)) for _p, _t, _s, n, e in found if not is_wrappable(e)]
        # Four spaces on EVERY row, matching the two sections below. The old
        # form put the indent in the join prefix as well as in the elements, so
        # the first row sat at four and every other at two.
        lines = [f"    {n:<24} {k:<6} {describe(p, s)}" for p, s, n, k in wrappable] or [
            "    (none)"
        ]
        msg = "which server should the trial wrap? Pass its name.\n\n" + "\n".join(lines)
        if already:
            msg += "\n\n  Already baton-proxy, so not offered:\n  " + "\n  ".join(
                f"  {n:<24} {describe(p, s)}" for p, s, n in already
            )
        if remote:
            msg += "\n\n  Not wrappable. What each is:\n  " + "\n  ".join(
                f"  {n:<24} {r}" for n, r in sorted(set(remote))
            )
        if not wrappable:
            msg += (
                "\n\n  There is no UNWRAPPED server here that this kit can wrap. It wraps a\n"
                "  stdio server, or a remote one whose `Authorization: Bearer` token is\n"
                "  written in the config and which sends no other header. Nothing has\n"
                "  been changed."
            )
        raise Refuse(msg)

    matches = [(p, t, s, n, e) for p, t, s, n, e in found if n == args.server]
    if not matches:
        names = sorted({n for _p, _t, _s, n, _e in found})
        raise Refuse(
            f"no MCP server named `{args.server}`.\n  available: "
            + (", ".join(names) or "none")
            + "\n  → check the name, or pass --src-config <path>."
        )
    # Filtered whenever `--from` was given, NOT only when there is more than one
    # match. Gating it on the duplicate meant a `--from` that matched nothing was
    # silently ignored the moment the duplicate went away: someone reuses the
    # command the refusal taught them, on another machine or after tidying their
    # config, and the kit wraps a definition they did not pick. That is the
    # outcome the "matched nothing" refusal exists to prevent, and the gate meant
    # it could not fire in the one case that reaches real people.
    if args.from_scope:
        matches = [m for m in matches if scope_selector(m[2]) == args.from_scope]
    if len(matches) > 1:
        # A CHOICE, not a dead end. This refusal used to end with "rename one of
        # them" — by hand, in `~/.claude.json` — which is the exact act Bharath
        # had just told us he would not perform, offered to him as the only way
        # forward. He had three `playwright` entries under three project keys,
        # `--src-config` cannot separate entries inside one file, and so he
        # trialled nothing.
        #
        # Every row now prints the flag that picks it. The kit still does not
        # choose; it just stops being the only thing standing in the way.
        # shlex.quote, for the reason `_cd_to` states: `/Users/x/Client Work/app`
        # is an ordinary macOS path, and an unquoted row pasted back gives
        # "unrecognized arguments: Work/app". The promise this refusal makes is
        # that the printed string is the string that works.
        where = "\n".join(
            f"    --from {shlex.quote(scope_selector(s)):<30} {describe(p, s)}"
            for p, _t, s, _n, _e in matches
        )
        raise Refuse(
            f"`{args.server}` is defined in more than one place:\n{where}\n"
            "  → run setup again with one of the --from lines above. Nothing has been\n"
            "    changed, and nothing needs renaming."
        )
    if not matches:
        raise Refuse(
            f"no `{args.server}` at --from {args.from_scope}.\n"
            "  → run setup with the server name alone to see where it is defined."
        )

    path, text, scope, name, entry = matches[0]
    if not is_wrappable(entry):
        raise Refuse(
            f"`{name}` is not wrappable — {not_wrappable_reason(entry)}"
            + (f" ({safe_endpoint(str(entry['url']))})" if entry.get("url") else "")
            + ".\n  Setup can replace a stdio entry's launch command, or bridge a remote\n"
            "  entry whose `Authorization: Bearer` token is written in the config and\n"
            "  which sends no other header. This entry is neither, and wrapping it\n"
            "  anyway would drop something the server needs — which shows up as a dead\n"
            "  server in the next session they start, not now.\n"
            "  → name a different server, or reach this one through an entry of either\n"
            "    shape. Nothing changed."
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

    # The server's own name, not a question. A stranger running a local,
    # no-account trial has no tenant and being asked to name one invites the
    # exact "am I signing up for something?" thought the kit exists to
    # prevent. They already picked this name; it is how they refer to the
    # server. Both labels reading the same is fine — nothing authenticates
    # either. `--tenant` stays as an override for our own rigs.
    tenant = args.tenant or name
    vendor = args.vendor or name

    if not SRC_DIR.is_dir():
        raise Refuse(
            f"expected the proxy source at {SRC_DIR}, which is not there.\n"
            "  → run this from a full checkout: the kit points PYTHONPATH at that folder."
        )

    mode = MODE_GLOBAL if args.in_place else DEFAULT_MODE
    backup: Path | None = None

    cwd_warning: str | None = None

    if mode == MODE_PROJECT:
        # BEFORE build_wrapped_entry, which is the ordering the guard's own test
        # pins: the wrap demotes the command into `args`, and a guard run after
        # it names a position the person's entry does not have.
        #
        # ⚠ A WARNING, NOT A REFUSAL — changed 2026-09-17, by the operator, after
        # the refusal was measured against a real entry. It was a `Refuse` and
        # the reasoning for that is recorded in `_relative_path_like`: "a false
        # positive costs a trial that would have worked and says `--in-place` in
        # the same breath, while a false negative is a server that dies in their
        # next session with nothing pointing at the cause."
        #
        # BOTH HALVES OF THAT SENTENCE WERE WRITTEN BEFORE PROJECT MODE AND THIS
        # MODE BREAKS BOTH:
        #
        # - A false positive is NOT cheap. `--in-place` is the only way out it
        #   offers, and that edits the config the person came here to keep
        #   untouched. It is the sentence that stopped Bharath. Measured: the
        #   ordinary `mcp-remote` entry, `--header "Authorization: Bearer
        #   abc/def"`, is refused — a header value carrying a slash, which is
        #   not a path and could never be one.
        # - A false negative is NOT a dead server. In this mode their own entry
        #   is never written, so a path that stops resolving breaks OUR COPY in
        #   the checkout and nothing else. Their server keeps working everywhere
        #   they already use it. The cost is a trial that captures nothing —
        #   which `receipt`'s own "connected, but nothing called it" checklist
        #   exists to diagnose, and which this warning now names in advance.
        #
        # So the guard is kept for its diagnosis and stripped of its veto. It
        # still runs BEFORE the wrap, for the position-naming reason above.
        # `--in-place` does not need it at all: that mode never moves the entry,
        # which is why this whole check is gated on the mode rather than folded
        # into `not_wrappable_reason`.
        cwd_warning = cwd_dependent_reason(entry, base=entry_home(scope, path))
        # Ours, in our own checkout, and the kit is the only thing that writes
        # it — so an existing one with no state file is a leftover we cannot
        # reason about rather than a config someone owns. Named as ours, with
        # the one deletion CLAUDE.md can permit.
        if MCP_PATH.exists():
            raise Refuse(
                f"{MCP_PATH} is already here, and there is no state file saying this kit\n"
                "  wrote it. That is a leftover from an earlier trial whose state was\n"
                "  cleared.\n"
                "  → this file belongs to the kit, inside the kit's own checkout, and\n"
                "    deleting it is safe. Delete it and run setup again. Nothing in your\n"
                "    own config has been changed."
            )
        new_text, state = build_project_config(
            entry,
            name=name,
            source_path=path,
            source_scope=scope,
            tenant_id=tenant,
            vendor_id=vendor,
            src_dir=str(SRC_DIR),
            events_path=str(EVENTS_PATH),
        )
        state["config_path"] = str(MCP_PATH)
        # No backup: nothing of theirs is being overwritten. The backup exists
        # because the global path rewrites a file holding every credential they
        # own, and not writing that file is the point of this mode.
        state["mode"] = mode
        write_atomically(MCP_PATH, new_text)
        write_state_file(state)
    else:
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

        # Back up the WHOLE file before touching it. `~/.claude.json` holds far
        # more than MCP servers, and a bad write on a machine we will never see
        # is unrecoverable for us. The backup is evidence; uninstall does not
        # read it.
        backup = TRY_DIR / f"config-backup.{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
        write_backup(path, backup)

        state["mode"] = mode
        write_atomically(path, new_text)
        write_state_file(state)

    # Read back off the state rather than from rebound locals, which is how
    # the already-wrapped branch at the top of this function reads them too.
    # The project branch used to reassign `path, scope = MCP_PATH, None` just
    # to feed these two lines.
    wrote_to, wrote_scope = Path(state["config_path"]), state["scope"]
    print(f"Wrapped `{name}` in {describe(wrote_to, wrote_scope)}")
    if mode == MODE_PROJECT:
        print(
            f"  copied from: {describe(Path(state['source_config_path']), state['source_scope'])}"
        )
        print("  your own config was read, not changed.")
    if backup is not None:
        print(f"  backup:  {backup}")
    print(f"  events:  {EVENTS_PATH}")
    print(f"  tenant:  {tenant}   vendor: {vendor}")
    print("\nThe entry now reads:\n")
    print(entry_json(state["wrapped_entry"]))
    # AFTER the entry and BEFORE the restart note, which is where it is
    # actionable: they have just seen what was written, and the next thing they
    # do is start the server this may affect. Above the entry it would be read
    # before there is anything to attach it to; below `start_where` it would sit
    # under the line the whole message builds to.
    if cwd_warning is not None:
        print(f"\n{cwd_dependent_warning(name, cwd_warning)}")
    print(f"\n{RESTART_NOTE}")
    # This window is the only place the security detail and the config diff
    # exist, and after the handoff no agent anywhere else knows the kit is here.
    print("\nLeave this window open — it holds the security detail and the diff above.")
    print(f"\n{start_where(wrote_scope, wrote_to)}")
    print(f"\n{come_back()}")
    print(f"\n{ENDING_NOTE}")
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
    # Read BEFORE the header: without state, the header line depends on whether
    # there are events, and getting that wrong is what made two of the doc's
    # rows match one output.
    events = read_events(events_path)

    print("Baton trial receipt")
    print("=" * 60)
    if state:
        print(f"wrapped server : {state['server_name']}")
        print(f"config         : {describe(Path(state['config_path']), state['scope'])}")
        print(f"wrapped at     : {state['wrapped_at']}")
        print(f"labels         : tenant={state['tenant_id']} vendor={state['vendor_id']}")
    elif events:
        # `uninstall` unlinks state.json and LEAVES events.jsonl — it prints it
        # under "left behind" — so every receipt after a finished trial lands
        # here. Saying "No setup state found" fired the doc's first row and its
        # last from one output: the agent is told nothing is wrapped yet AND
        # that the trial is running, and the person has already ended it.
        print(STATE_CLEARED)
    else:
        print("No setup state found — this receipt is reading the event file directly.")
    print(f"event file     : {events_path}")
    print()

    print(f"launch check   {launch_check(state)}")
    print()

    # Asked once, and asked whether or not the file is empty. Gating it on "no
    # events" was one case too few: a session records its tool-surface snapshot,
    # the client then rewrites the entry back — which is why this row exists —
    # and from then on the file is not empty while nothing more is captured.
    # Row 5 fired there instead and named two causes, neither of them true.
    wrap_gone = bool(state) and not wrap_still_present(state)

    if not events:
        # The kit can answer the most likely cause for free rather than listing
        # four diagnostics that all miss it. The client rewrites this config
        # continuously and may clobber the entry; someone may have reverted it
        # by hand. Either way "no events" is a symptom, not the finding.
        if wrap_gone:
            assert state is not None
            print(wrap_is_gone(state, had_events=False))
            return 0
        # NOT_CAPTURING is written for someone whose setup DID run: its first
        # step asks about the restart since setup, and its third points at a
        # server name and config path printed above — neither of which exists
        # without state. Serving it here fired two of the doc's branches at once
        # and sent the reader back to the command they had just run.
        print(not_capturing(state["scope"], state["config_path"]) if state else NOT_SET_UP)
        return 0

    s = summarize(events, events_path.stat().st_size)
    print(f"sessions             {s['sessions']}")
    # The rows are the whole point of blocker 3: an aggregate cannot say that one
    # of two sessions captured nothing. Bounded at ten, and the count of what is
    # not shown is printed rather than dropped silently.
    shown = s["per_session"][-10:]
    for sid, calls, other in shown:
        extra = f", {other} resource/prompt calls" if other else ""
        print(f"                     {sid}  {calls} calls{extra}")
    if len(s["per_session"]) > len(shown):
        hidden = s["per_session"][: -len(shown)]
        print(
            f"                     +{len(hidden)} earlier sessions "
            f"({sum(1 for _s, c, o in hidden if c == 0 and o == 0)} with nothing in them)"
        )
    print(f"tool calls           {s['tool_calls']}")
    # Per CALL, and per mechanism. Sessions was the wrong grain — one call with a
    # goal in a session of twenty made the row read as a covered session — and
    # merging the two mechanisms made the number unanswerable when it was zero.
    # Both rows are gated on there being calls to have carried a goal. `0 of 0`
    # with six lines under it explaining the zero argues with the banner below,
    # which has already said nothing came down the pipe.
    if s["tool_calls"]:
        print(f"intent captured      {s['calls_with_intent']} of {s['tool_calls']} tool calls")
        if not s["calls_with_intent"]:
            print(INTENT_IS_ZERO)
    if s["tool_calls"] or s["agent_annotations"]:
        print(f"annotations filed    {s['agent_annotations']} by your agent")
        if not s["agent_annotations"]:
            print(ANNOTATIONS_ARE_ZERO)
    print(f"tool definitions     {len(s['tools'])} captured exactly as your server served them")
    if s["tools"]:
        print(
            f"                     {', '.join(s['tools'][:8])}"
            + (f", +{len(s['tools']) - 8} more" if len(s["tools"]) > 8 else "")
        )
    print(f"span                 {s['first']} → {s['last']}")
    print(f"events               {s['events']}")
    print(f"file size            {human_size(s['size_bytes'])}")
    # Both of these need state: on a trial that has already ended the header
    # says so, and telling someone to go fix a wrap they removed is dead advice.
    # One banner per output is what makes CLAUDE.md's table a table.
    if wrap_gone:
        assert state is not None
        print()
        print(wrap_is_gone(state, had_events=True), end="")
    elif state and s["tool_calls"] == 0 and s["other_calls"] == 0:
        print()
        print(NOTHING_CALLED, end="")
    elif state and s["dead_sessions"]:
        print()
        print(DEAD_SESSION_NOTE, end="")

    # Gated on whether anything actually reached the server, and gated on the
    # same count the diagnosis above uses — a resource read is a call, so a
    # session that only read resources produced a capture worth uploading.
    #
    # Handing someone a handshake-only file to upload wastes the one trip most
    # people will make, and it argues with the banner printed a few lines up,
    # which just told them nothing came down the pipe.
    if not (s["tool_calls"] or s["other_calls"]):
        return 0

    print()
    print(setup_note(events_path))
    reveal = reveal_note(events_path)
    if reveal:
        print(reveal)
    return 0


def checkout_note(verified: bool) -> str:
    """The last thing `uninstall` says, and the question the person running it
    actually has: how do I get this off my machine.

    Built here rather than as a module constant because it interpolates
    ``STATE_PATH``, which the tests monkeypatch — a module-level f-string would
    freeze the real path into both branches at import.

    The two branches are not phrasings of one sentence. On the verified path the
    checkout is disposable and saying so finishes the job. On the unverified one
    ``state.json`` was deliberately KEPT as the only record of the original
    entry, so "delete the folder and you are done" would talk someone into
    destroying their own recovery record one line under a warning that the
    restore did not match.
    """
    if verified:
        return (
            f"Nothing was installed. Everything this kit put on your machine is inside\n"
            f"  {CHECKOUT}\n"
            "and deleting that folder removes all of it, including the files listed above\n"
            "and the kit itself. There is no package to uninstall, no service to stop and\n"
            "no account to close."
        )
    return (
        f"Nothing was installed — everything this kit put on your machine is inside\n"
        f"  {CHECKOUT}\n"
        "so removing it is deleting that folder. Not yet, though: the restore above did\n"
        f"not verify, and {STATE_PATH} is the only record of what your entry\n"
        "said before setup. Settle the config first, then delete the folder."
    )


def _print_left_behind() -> None:
    """What uninstall leaves on the machine, named rather than left to be found.

    The sentence about `config-backup.*` is the reason this section exists: it
    is a full copy of the config, every server's credentials included. In
    project mode no backup is written — there was no write to their file to
    make recoverable — so the glob finds none and the sentence must not claim
    one. It is built from what is actually there.
    """
    left = []
    if EVENTS_PATH.exists():
        left.append(f"  {EVENTS_PATH}  ({human_size(EVENTS_PATH.stat().st_size)})")
    backups = sorted(TRY_DIR.glob("config-backup.*.json"))
    left.extend(f"  {b}" for b in backups)
    if not left:
        return
    note = "\nDeliberately left in place"
    if backups:
        note += (
            ". `config-backup.*` is a full copy of your config, every server's credentials included"
        )
    print(note + ":")
    print("\n".join(left))


def _finish_uninstall(*, verified: bool) -> int:
    """The ending both uninstall paths share.

    Project mode always passes True: `remove_project_config` either does the
    removal or raises `Refuse`, so there is no unverified state to report. What
    differs between the two modes is said before this is called."""
    print(f"\n{UNINSTALL_NOTE if verified else UNVERIFIED_NOTE}")
    _print_left_behind()
    print()
    print(checkout_note(verified))
    return 0


def remove_project_config(state: dict) -> str:
    """Take our entry out of the project config, and say what happened.

    The project-mode counterpart to ``apply_unwrap``, and deliberately not a
    restore. There is nothing to put back: in this mode their own config was
    never written, so "undo" means removing the file this kit added rather than
    reversing an edit to a file they own.

    Three outcomes, because the file can be in three states by the time someone
    runs uninstall, and two of them are ways a person can get stuck:

    - **Gone already.** Not an error. Setup's own refusal for a leftover file
      tells them deleting it is safe, so someone who followed that advice and
      then ran uninstall must not be met with "cannot read". The wrap is off,
      which is what they asked for.
    - **Holding our entry and nothing else.** The file is deleted outright. It
      exists only because this kit wrote it.
    - **Holding something else too.** Only our key is removed and the file is
      rewritten. Deleting it whole would throw away servers someone added by
      hand after setup — their work, in a file we introduced but do not own the
      whole of.

    An entry that is no longer the one setup wrote is left alone and reported,
    on the same principle ``apply_unwrap`` refuses an edited entry: the case
    where guessing is worst is the case where someone has been in there.
    """
    path = Path(state["config_path"])
    name = state["server_name"]
    if not path.exists():
        return f"{path} was already gone; nothing to remove."
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        servers = data.get("mcpServers") or {}
    except (OSError, json.JSONDecodeError) as e:
        raise Refuse(
            f"cannot read {path}: {e}\n"
            "  → this file belongs to the kit and holds no original of yours; your own\n"
            "    config was never changed. Deleting it by hand finishes the uninstall."
        ) from None

    current = servers.get(name)
    if current is None:
        others = ", ".join(sorted(servers)) or "nothing"
        return f"`{name}` was not in {path} (it holds {others}); nothing to remove."
    if not is_still_our_wrap(current, state):
        raise Refuse(
            f"`{name}` in {path} is not the entry setup wrote — it has been edited\n"
            "  since. Refusing to delete someone's edit.\n"
            "  → your own config was never changed by this mode, so nothing of yours is\n"
            "    waiting to be restored. Delete the file by hand when you are done with it."
        )

    del servers[name]
    if servers:
        # `dumps_like`, not a hardcoded shape. This is the branch where they
        # ADDED a server to the file by hand, so it is the one file here that
        # someone has edited — rewriting it with our own indent reformats
        # their work on the way past.
        write_atomically(path, dumps_like(data, text))
        return f"Removed `{name}` from {path}, which still holds {', '.join(sorted(servers))}."
    path.unlink()
    return f"Deleted {path}. It held only the wrapped entry, and this kit wrote it."


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not STATE_PATH.exists():
        raise Refuse(
            f"no setup state at {STATE_PATH}, so there is nothing recorded to reverse.\n"
            "  → if a wrap is in place, remove it by hand: the entry's `args` end with\n"
            "    `-- <your original command>`, which is what it was before."
        )
    state = load_state()
    path = Path(state["config_path"])

    # Absent means a state file written before `mode` existed, which is a real
    # machine mid-trial, and those are all global.
    if wrap_mode(state) == MODE_PROJECT:
        what = remove_project_config(state)
        STATE_PATH.unlink()
        print(what)
        print(
            f"\n  Your own config was never changed by this trial — the entry was copied\n"
            f"  out of {state['source_config_path']} and left exactly as it was.\n"
            "  There is nothing to restore."
        )
        # Falls through to the shared tail rather than repeating it. Only the
        # two lines above are mode-specific; UNINSTALL_NOTE, what is left
        # behind, and the checkout note are the same ending either way.
        return _finish_uninstall(verified=True)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise Refuse(f"cannot read {path}: {e}\n  → nothing was changed.") from None

    new_text, restored = apply_unwrap(text, state)
    write_atomically(path, new_text)

    # Verify BEFORE unlinking. state.json is the only record of the original
    # entry; deleting it on an unverified write would destroy the one thing that
    # makes a bad restore recoverable.
    verified = restored_matches_on_disk(path, state)
    if verified:
        STATE_PATH.unlink()

    print(f"Restored `{state['server_name']}` in {describe(path, state['scope'])}")
    print("\nThe entry now reads:\n")
    print(entry_json(restored))
    # The print above hides literal env values, so it is no longer proof on its
    # own. This is, and it is a stronger check than reading a dump by eye: it
    # re-reads the file and compares every byte, including the values it hid.
    if verified:
        print(
            "\n  Verified against the file on disk: byte-identical to the entry\n"
            "  recorded at setup, including the values not shown above."
        )
    else:
        print(
            "\n  WARNING: the entry on disk does not match what setup recorded.\n"
            f"  {STATE_PATH} has been KEPT so the original is not lost. Compare by hand."
        )
    return _finish_uninstall(verified=verified)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kit.py",
        description="Baton try kit: set up, receipt, remove. See SECURITY.md beside this file.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="wrap one configured MCP server")
    p_setup.add_argument("server", nargs="?", help="name of the server entry to wrap")
    # Named for what it DOES. Renamed from `--global` 2026-09-17, which was wrong
    # twice over. An entry can live under a project key in ~/.claude.json, and
    # wrapping it there is not global anything -- the PROJECT_SCOPED fixture
    # does exactly that. And neither that name nor `--config-file` said anything about
    # writing, so the two together edited someone's config while the refusal
    # that recommended the pair said nothing about it. This name states the
    # write, which is the whole point of it.
    #
    # Moved at K1b, as the comment above it required. "(what setup does today)"
    # was true only while the default was global; the flip made `--help` print
    # a false statement about a plain `setup`, which is the same defect the
    # earlier version had in the other direction.
    p_setup.add_argument(
        "--in-place",
        dest="in_place",
        action="store_true",
        help="wrap the entry where it already lives, editing that config file in "
        "place, instead of writing a project config in this checkout",
    )
    # K9, reopened and settled 2026-09-17: the flag stays, under a name that
    # says which direction it points. As `--config-file` it named a file and said
    # nothing about whether that file got written -- and the answer came from a
    # DIFFERENT flag. This one only ever reads, in every mode, so its help needs
    # no caveat about the default changing: the default does not reach it.
    p_setup.add_argument(
        "--src-config",
        dest="src_config",
        help="config to READ the servers from, instead of searching for "
        "~/.claude.json. It is never written unless you also pass --in-place, "
        "which says so.",
    )
    p_setup.add_argument(
        "--from",
        dest="from_scope",
        metavar="SCOPE",
        help="which definition to use when one server name is defined in more than "
        "one place: `global`, or the project path. setup prints the exact value "
        "for each when it finds more than one.",
    )
    p_setup.add_argument("--tenant", help="label for this trial (default: the server's name)")
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

"""Tests for the try kit's config surgery.

The kit's whole claim to being code rather than prose is that the same module
writes the wrap and reverses it, so ``uninstall(setup(x)) == x`` is a property a
test can pin. That is the first test below; everything else guards a rule whose
failure would be silent on a machine we never see.

``try/kit.py`` is a standalone script, not a package module (deliberately — it
runs from a bare checkout before anything is importable), so it is loaded here by
path.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest

from baton_proxy.proxy import _inject_goal_params

KIT_PATH = Path(__file__).resolve().parent.parent / "try" / "kit.py"


def _load_kit():
    spec = importlib.util.spec_from_file_location("try_kit", KIT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kit = _load_kit()

# The real one, off `__file__`, captured before any fixture redirects it. The
# guard below is the only thing that may read it.
_REAL_MCP_PATH = kit.MCP_PATH


@pytest.fixture(scope="session", autouse=True)
def _no_project_config_left_by_the_whole_run():
    """The same rule as the per-test guard, for writers it cannot see.

    A module-scoped fixture is set up BEFORE any function-scoped one, so by the
    time the per-test guard first looks the file already exists and it reads
    that as "not mine". That is exactly how `stdio_run` and `bridge_run` leaked
    past it: the per-test guard was proven against a function-scoped mutant and
    only ever discriminated that class.

    Session scope catches a writer at any scope — but only one that LEAVES THE
    FILE BEHIND. A fixture that ran setup and then uninstall would hold a live
    `.mcp.json` in the checkout for its whole lifetime and remove it on the way
    out, and neither guard would say a word. No such fixture exists today; the
    limit is recorded so the pair is not read as proof that nothing can write
    that path. Note too that after any leak the per-test guard sees
    `existed=True` and is blind for the rest of the run.

    The entry check fails a run that STARTS poisoned, because a stale file also
    changes results — setup refuses on a leftover it cannot account for, so a
    poisoned run reports failures that say nothing about the code.
    """
    if _REAL_MCP_PATH.exists():
        raise AssertionError(
            f"{_REAL_MCP_PATH} exists before the run. A previous run leaked it, or a "
            "real trial is set up in this checkout. Remove it (or finish the trial "
            "with `uninstall`) — results from a poisoned run mean nothing."
        )
    yield
    if _REAL_MCP_PATH.exists():
        _REAL_MCP_PATH.unlink()
        raise AssertionError(
            f"the run wrote {_REAL_MCP_PATH}, a real file in the working tree that "
            "`claude` loads, and no single test owned up to it — so the writer is "
            "scoped above the per-test guard. Redirect kit.MCP_PATH in whichever "
            "module- or session-scoped fixture calls setup. (File removed.)"
        )


@pytest.fixture(autouse=True)
def _no_project_config_in_the_working_tree():
    """Fail the test that writes a `.mcp.json` into this checkout.

    K1b found this the expensive way: the flip sent every setup test down the
    project path, `kit_home` did not redirect MCP_PATH yet, and the suite wrote
    a live wrapped server into the repo root. Three things made it quiet — K7
    git-ignores that exact path so `git status` stays clean, the file only
    changes behaviour for a `claude` started in this directory, and the damage
    showed up as ONE extra failure in the NEXT run, which reads like flakiness
    rather than a leak.

    Autouse and per-test so the failure names the test that did it. It asserts
    on the real path rather than `kit.MCP_PATH`, which by then is whatever the
    fixtures pointed it at.
    """
    existed = _REAL_MCP_PATH.exists()
    yield
    if _REAL_MCP_PATH.exists() and not existed:
        _REAL_MCP_PATH.unlink()
        raise AssertionError(
            f"this test wrote {_REAL_MCP_PATH}, which is a real file in the working "
            "tree that `claude` loads. A fixture is not redirecting kit.MCP_PATH. "
            "(The file has been removed so the rest of the run is not poisoned.)"
        )


WRAP_ARGS = dict(
    tenant_id="trial-abc123",
    vendor_id="notion",
    src_dir="/checkout/src",
    events_path="/checkout/try/events.jsonl",
)

# Canonical form: json.dumps(indent=2) + trailing newline. Whole-file byte
# equality is asserted against these; a config in any other shape is covered by
# the semantic test below.
GLOBAL_ONLY = {
    "mcpServers": {
        "notion": {
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {"NOTION_TOKEN": "${NOTION_TOKEN}"},
        }
    }
}

PROJECT_SCOPED = {
    "numStartups": 41,
    "mcpServers": {"other": {"command": "node", "args": ["other.js"]}},
    "projects": {
        "/Users/someone/work/app": {
            "allowedTools": [],
            "mcpServers": {"notion": {"command": "npx", "args": ["-y", "srv"]}},
        }
    },
}

NO_ENV = {"mcpServers": {"plain": {"command": "./run.sh", "args": []}}}

# Both modes owe the `THE WRAP IS GONE` row, so every clobber test runs twice.
# Pinning them to one mode was the tempting fix at K1b and it is the wrong one:
# `--in-place` is where every K8-refused and OAuth-blocked prospect lands, so
# dropping its coverage would leave the row untested for the people most likely
# to meet it.
#
# ⚠ Defined up here, not beside `_clobber`, because a DECORATOR is evaluated at
# import time: with it next to its helpers the first test to use it sat 700
# lines earlier and the whole module failed to collect. A collection error looks
# nothing like a test failure, and the set-diff is what caught it — the run
# reported "1 red" and it was the file, not a test.
_BOTH_MODES = pytest.mark.parametrize(
    "in_place", [pytest.param(True, id="in-place"), pytest.param(False, id="project")]
)

# The remote shape. SECURITY.md §7's removal GUARANTEE is only ever as wide as
# this corpus, so the http class enters it here rather than in a test of its own.
# Both credential forms, because they take different paths through the redaction
# rule and only one of them is ever printed.
HTTP_VAR_BEARER = {
    "mcpServers": {
        "remote": {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer ${REMOTE_TOKEN}"},
        }
    }
}

HTTP_LITERAL_BEARER = {
    "mcpServers": {
        "remote": {
            "type": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": "Bearer sk-live-LITERAL-abc123"},
        }
    }
}


def canonical(data) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# =============================================================================
# The property the decision rests on.
# =============================================================================


@pytest.mark.parametrize(
    "data,scope,name",
    [
        (GLOBAL_ONLY, None, "notion"),
        (PROJECT_SCOPED, "/Users/someone/work/app", "notion"),
        (PROJECT_SCOPED, None, "other"),
        (NO_ENV, None, "plain"),
        # The rewrite that changes SHAPE, not just the command — `type`, `url`
        # and `headers` all have to come back, and they come back from the state
        # file rather than from anything reconstructible out of the wrap.
        (HTTP_VAR_BEARER, None, "remote"),
        (HTTP_LITERAL_BEARER, None, "remote"),
    ],
)
def test_round_trip_is_byte_identical(data, scope, name):
    """uninstall(setup(x)) == x, on the bytes, for every scope shape."""
    before = canonical(data)
    wrapped_text, state = kit.apply_wrap(before, scope=scope, name=name, **WRAP_ARGS)
    assert wrapped_text != before, "setup must actually change something"
    restored_text, restored_entry = kit.apply_unwrap(wrapped_text, state)
    assert restored_text == before
    expected = (
        data["mcpServers"][name] if scope is None else data["projects"][scope]["mcpServers"][name]
    )
    assert restored_entry == expected


def test_round_trip_preserves_unrelated_content_in_a_non_canonical_file():
    """A file written with 4-space indent and unrelated keys survives
    semantically, and its indent is not reformatted away."""
    before = json.dumps(PROJECT_SCOPED, indent=4) + "\n"
    wrapped_text, state = kit.apply_wrap(
        before, scope="/Users/someone/work/app", name="notion", **WRAP_ARGS
    )
    assert '\n    "' in wrapped_text, "indent style must be preserved"
    restored_text, _ = kit.apply_unwrap(wrapped_text, state)
    assert json.loads(restored_text) == PROJECT_SCOPED
    assert restored_text == before


def test_a_hand_formatted_config_is_normalized_but_never_altered():
    """A config with collapsed inline containers does NOT come back byte-equal —
    rewriting expands them. Pinned deliberately rather than left as a surprise:
    the content is identical, the whitespace is not, and a reviewer diffing their
    own config should find that documented (SECURITY.md §2).

    The file that actually matters is already canonical: `~/.claude.json` as
    Claude Code writes it round-trips through json.dumps(indent=2) byte-for-byte,
    which the byte-equality tests above cover."""
    before = '{\n  "mcpServers": {\n    "s": {"command": "npx", "args": ["-y", "srv"]}\n  }\n}\n'
    wrapped, state = kit.apply_wrap(before, scope=None, name="s", **WRAP_ARGS)
    restored, _ = kit.apply_unwrap(wrapped, state)
    assert json.loads(restored) == json.loads(before)  # content preserved
    assert restored != before  # whitespace normalized


def test_wrap_is_idempotent():
    """Wrapping a wrap yields the same entry — one proxy, not two nested."""
    before = canonical(GLOBAL_ONLY)
    once, _ = kit.apply_wrap(before, scope=None, name="notion", **WRAP_ARGS)
    twice, _ = kit.apply_wrap(once, scope=None, name="notion", **WRAP_ARGS)
    assert json.loads(twice)["mcpServers"]["notion"] == json.loads(once)["mcpServers"]["notion"]


# =============================================================================
# Rules whose failure is silent.
# =============================================================================


def test_env_is_preserved_verbatim_including_var_refs():
    e = kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS)
    assert e["env"]["NOTION_TOKEN"] == "${NOTION_TOKEN}"


def test_baton_vars_are_written_last_so_a_stray_value_cannot_shadow_them():
    original = {
        "command": "npx",
        "args": ["srv"],
        "env": {"BATON_EVENT_SINK": "https://evil.example/v0", "BATON_TENANT_ID": "someone-else"},
    }
    e = kit.build_wrapped_entry(original, **WRAP_ARGS)
    assert e["env"]["BATON_EVENT_SINK"] == "file:///checkout/try/events.jsonl"
    assert e["env"]["BATON_TENANT_ID"] == "trial-abc123"


def test_sink_is_the_file_only_never_stderr():
    """The proxy's default also mirrors to stderr, which the client may capture
    into its own logs. SECURITY.md §7 promises that does not happen here."""
    e = kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS)
    assert "stderr" not in e["env"]["BATON_EVENT_SINK"]
    assert e["env"]["BATON_EVENT_SINK"].startswith("file://")


def test_tenant_id_is_always_set():
    """Default is the sentinel 'local'; every trial that kept it would merge."""
    e = kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS)
    assert e["env"]["BATON_TENANT_ID"] == "trial-abc123"


def test_pythonpath_is_appended_so_the_wrapped_server_keeps_priority():
    original = {"command": "python3", "args": ["-m", "srv"], "env": {"PYTHONPATH": "/their/libs"}}
    e = kit.build_wrapped_entry(original, **WRAP_ARGS)
    assert e["env"]["PYTHONPATH"].split(":") == ["/their/libs", "/checkout/src"]


def test_the_command_is_demoted_not_replaced():
    e = kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS)
    assert e["args"] == ["-m", "baton_proxy", "--", "npx", "-y", "@notionhq/notion-mcp-server"]


def test_the_interpreter_is_absolute_matching_scan():
    """A bare `python3` is resolved against the MCP CLIENT's PATH. A GUI-launched
    client on macOS gets launchd's minimal PATH, where python3 is 3.9 and cannot
    import baton_proxy — the server dies at launch, days after setup succeeded.
    scan.py writes sys.executable for the same reason and test_scan.py pins it."""
    import sys

    e = kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS)
    assert e["command"] == sys.executable
    assert Path(e["command"]).is_absolute()


def test_unrelated_entries_are_untouched():
    before = canonical(PROJECT_SCOPED)
    after, _ = kit.apply_wrap(before, scope="/Users/someone/work/app", name="notion", **WRAP_ARGS)
    assert json.loads(after)["mcpServers"]["other"] == PROJECT_SCOPED["mcpServers"]["other"]
    assert json.loads(after)["numStartups"] == 41


def test_sink_uri_survives_the_proxys_own_parser_on_a_path_with_a_space(tmp_path):
    """The bug this pins: Path.as_uri() percent-encodes, and sinks.py parses with
    urlparse WITHOUT unquoting — so a checkout under "My Projects" produced a
    sink FileSink could not open, and the proxy died at launch days after setup
    said it was fine. Asserted against the real consumer, not a copy of it."""
    import urllib.parse

    from baton_proxy.sinks import make_sink

    tmp_spaced = tmp_path / "My Projects" / "try" / "events.jsonl"
    path = str(tmp_spaced)
    uri = kit.file_sink_uri(path)
    assert urllib.parse.urlparse(uri).path == path

    tmp_spaced.parent.mkdir(parents=True, exist_ok=True)
    sink = make_sink(uri, api_key=None)  # would raise FileNotFoundError before
    sink.write({"probe": 1})
    sink.close()
    assert tmp_spaced.exists()


def test_sink_uri_refuses_a_path_that_cannot_round_trip():
    """`?` and `#` cannot ride a file URI — urlparse splits them off. Refused by
    name at setup rather than written and discovered on day five."""
    with pytest.raises(kit.Refuse) as e:
        kit.file_sink_uri("/tmp/weird?dir/events.jsonl")
    assert "`?` or `#`" in str(e.value)


def test_config_path_is_resolved_so_it_survives_a_different_cwd(monkeypatch, tmp_path):
    """setup stores config_path in the state file; receipt and uninstall read it
    days later from wherever the user happens to be."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "local.json").write_text("{}", encoding="utf-8")
    assert kit.search_paths("local.json")[0].is_absolute()


# =============================================================================
# Refusals — the seam where the kit stops and a human decides.
# =============================================================================


def test_uninstall_refuses_when_the_entry_was_edited_after_setup():
    before = canonical(GLOBAL_ONLY)
    wrapped_text, state = kit.apply_wrap(before, scope=None, name="notion", **WRAP_ARGS)
    tampered = json.loads(wrapped_text)
    tampered["mcpServers"]["notion"]["args"].append("--extra")
    with pytest.raises(kit.Refuse) as e:
        kit.apply_unwrap(json.dumps(tampered, indent=2) + "\n", state)
    assert "edited since setup" in str(e.value)
    assert "what setup recorded as the original" in str(e.value)


def test_uninstall_succeeds_when_the_entry_was_already_restored_by_hand():
    """The deadlock this closes: setup refuses on the stale state file,
    uninstall refused on the entry, and CLAUDE.md forbids the agent from
    deleting state.json to escape — so every documented way out was blocked.
    An already-restored entry means the only work left is clearing the state."""
    before = canonical(GLOBAL_ONLY)
    wrapped_text, state = kit.apply_wrap(before, scope=None, name="notion", **WRAP_ARGS)
    restored_text, entry = kit.apply_unwrap(before, state)  # config already back to original
    assert restored_text == before
    assert entry == GLOBAL_ONLY["mcpServers"]["notion"]


def test_sink_uri_refuses_a_comma_which_the_proxy_reads_as_a_sink_separator():
    """make_sink splits BATON_EVENT_SINK on "," BEFORE urlparse, so the
    round-trip check cannot see this one — a checkout under "Proj,old" becomes
    two bogus sinks and the proxy dies at every client launch."""
    with pytest.raises(kit.Refuse) as e:
        kit.file_sink_uri("/tmp/Proj,old/try/events.jsonl")
    assert "comma" in str(e.value)


def test_write_never_exposes_content_at_a_wider_mode_than_the_target(tmp_path, monkeypatch):
    """Not just the final mode — the temp file holds the whole config (OAuth
    tokens, every project's env block) while it is being written."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    cfg.chmod(0o600)

    # Observed at chmod time, which is AFTER the content is written — the
    # window that matters. Checking at os.replace would see the corrected mode
    # and pass against a temp file that was world-readable while holding the
    # config: the assertion has to sit inside the exposure, not after it.
    seen = []
    real_chmod = kit.os.chmod

    def spy(target, mode):
        seen.append(kit.os.stat(target).st_mode & 0o777)
        return real_chmod(target, mode)

    monkeypatch.setattr(kit.os, "chmod", spy)
    kit.write_atomically(cfg, '{"secret": "x"}')
    assert seen and seen[0] <= 0o600, f"temp file held the config at {oct(seen[0])} before chmod"


def test_write_leaves_no_temp_file_when_the_write_fails(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kit.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        kit.write_atomically(cfg, "{}")
    assert not list(tmp_path.glob("*.baton-tmp"))


def test_launch_check_uses_the_interpreter_setup_actually_recorded(tmp_path):
    """A hardcoded `python3` would fail on exactly the machine this kit worries
    about, telling the user a healthy wrap is broken."""
    _, state = kit.apply_wrap(
        canonical(GLOBAL_ONLY),
        scope=None,
        name="notion",
        interpreter="/opt/py312/bin/python3.12",
        **WRAP_ARGS,
    )
    cmd = kit.launch_check(state)
    assert "/opt/py312/bin/python3.12" in cmd
    assert "/checkout/src" in cmd


def test_default_search_does_not_name_the_try_directory(monkeypatch, tmp_path):
    """cwd is try/ during the trial, so a cwd-relative .mcp.json could only ever
    name baton-proxy/try/.mcp.json — never a user config, and misleading in the
    not-found message."""
    monkeypatch.chdir(tmp_path)
    assert all(".mcp.json" not in str(p) for p in kit.search_paths(None))


def test_uninstall_refuses_when_the_entry_is_gone_and_shows_what_it_was():
    before = canonical(GLOBAL_ONLY)
    wrapped_text, state = kit.apply_wrap(before, scope=None, name="notion", **WRAP_ARGS)
    gutted = json.loads(wrapped_text)
    del gutted["mcpServers"]["notion"]
    with pytest.raises(kit.Refuse) as e:
        kit.apply_unwrap(json.dumps(gutted, indent=2) + "\n", state)
    assert "@notionhq/notion-mcp-server" in str(e.value)


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "http", "url": "https://mcp.example.com"},
        {"type": "sse", "url": "https://mcp.example.com/sse"},
        {"url": "https://mcp.example.com"},
        {"command": ""},
    ],
)
def test_remote_and_malformed_entries_are_not_stdio(entry):
    assert not kit.is_stdio(entry)


@pytest.mark.parametrize(
    "entry,wrapped",
    [
        # The `--` form -- what this kit writes, and what already worked.
        ({"command": "python3", "args": ["-m", "baton_proxy", "--", "npx", "srv"]}, True),
        ({"command": "baton-proxy", "args": ["--", "npx", "srv"]}, True),
        # THE BUG: a hand-built HTTPS-bridge wrap carries no `--`, so the old
        # "unwrap changed nothing" test called it unwrapped and setup would have
        # wrapped it a SECOND time -- two nested proxies, baton_annotate injected
        # twice, discovered days later in a file nobody is watching.
        ({"command": "python3", "args": ["-m", "baton_proxy", "--url", "https://x/mcp"]}, True),
        # Same hole, other separator-less shapes.
        ({"command": "baton-proxy", "args": []}, True),
        ({"command": "baton-proxy", "args": ["--verbose"]}, True),
        ({"command": "/opt/venv/bin/baton-proxy", "args": ["--url", "https://x"]}, True),
        # Launch forms a HEAD-only check misses -- `uvx`/`uv run` are how our own
        # README tells people to run baton-proxy, so these are not exotic.
        ({"command": "uvx", "args": ["baton-proxy", "--", "npx", "srv"]}, True),
        ({"command": "uv", "args": ["run", "baton-proxy", "--", "npx", "srv"]}, True),
        ({"command": "/usr/bin/env", "args": ["python3", "-m", "baton_proxy", "--", "s"]}, True),
        ({"command": "bash", "args": ["-lc", "baton-proxy -- npx srv"]}, True),
        # DOCUMENTED FALSE POSITIVE, chosen not missed: a path component that
        # merely happens to be named baton-proxy reads as a wrap. The cost is one
        # manual step for the user; the cost of the opposite miss is a silent
        # double-wrap nobody sees for days.
        ({"command": "npx", "args": ["--prefix", "/opt/baton-proxy", "srv"]}, True),
        # Untouched: a server that is nobody's wrap.
        ({"command": "npx", "args": ["-y", "srv"]}, False),
        ({"command": "python3", "args": ["-m", "some_other_server"]}, False),
        ({"command": "npx", "args": ["-y", "@scope/baton-proxy-lookalike"]}, False),
    ],
)
def test_is_wrapped_catches_every_proxy_invocation(entry, wrapped):
    assert kit.is_wrapped(entry) is wrapped


@pytest.mark.parametrize(
    "url,secret",
    [
        # Zapier/Composio put the token in the PATH; ?key= is just as common;
        # userinfo is the third vector. All three are the credential itself.
        ("https://mcp.zapier.com/api/mcp/s/SUPERSECRET/sse", "SUPERSECRET"),
        ("https://api.example.com/mcp?key=SUPERSECRET", "SUPERSECRET"),
        ("https://user:SUPERSECRET@api.example.com/mcp", "SUPERSECRET"),
    ],
)
def test_safe_endpoint_never_leaks_the_credential(url, secret):
    """The refusal that prints an endpoint is shown to someone who may paste it
    into a support thread with us -- the same reason header VALUES are never
    printed. An endpoint gets named, never quoted."""
    out = kit.safe_endpoint(url)
    assert secret not in out
    assert out.startswith("https://")


def test_bridge_entry_is_not_told_to_unwrap_itself():
    """`--url` bridges newly reach the already-wrapped refusal, and they have no
    upstream command inside them -- so "unwrap it by hand" would mean deleting
    the entry's only launch mechanism."""
    cmd = ["python3", "-m", "baton_proxy", "--url", "https://x/mcp"]
    assert kit.is_wrapped({"command": cmd[0], "args": cmd[1:]})
    assert kit.unwrap_command(list(cmd)) == cmd  # nothing to peel -> the branch fires


def test_unwrap_still_leaves_a_separatorless_wrap_alone():
    """The fix moves `is_wrapped`, NOT `unwrap_command`. A `--url` bridge has no
    original stdio command to recover, so unwrap must keep returning it as-is --
    that behaviour is pinned to scan.py's donor by the drift test above."""
    cmd = ["python3", "-m", "baton_proxy", "--url", "https://x/mcp"]
    assert kit.unwrap_command(list(cmd)) == cmd


@pytest.mark.parametrize(
    "entry,expected",
    [
        # Every shape is_stdio rejects gets its own reason. The point of the
        # split is that only ONE of these is close to workable (bearer-in-the-
        # config), so the list has to tell them apart rather than say "remote".
        ({"type": "sse", "url": "https://x/sse"}, "sse transport"),
        ({"type": "http", "url": "https://x"}, "http, no credential in the config"),
        ({"url": "https://x"}, "http, no credential in the config"),
        (
            {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer abc"}},
            "http, bearer token in the config",
        ),
        (
            {"type": "http", "url": "https://x", "headers": {"authorization": "bearer abc"}},
            "http, bearer token in the config",
        ),
        (
            {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer ${TOK}"}},
            "http, bearer token in the config (a ${VAR} reference)",
        ),
        (
            {
                "type": "http",
                "url": "https://x",
                "headers": {"Authorization": "Bearer abc", "X-Tenant": "acme"},
            },
            "http, bearer token in the config, plus X-Tenant",
        ),
        (
            {"type": "http", "url": "https://x", "headers": {"Authorization": "Basic abc"}},
            "http, custom headers (Authorization)",
        ),
        # A bearer with nowhere to send it must NOT report the wrappable class's
        # own phrase under the "Not wrappable" heading — the list is the
        # instrument that tells us which classes prospects actually have.
        (
            {"type": "http", "headers": {"Authorization": "Bearer abc"}},
            "http, no endpoint url",
        ),
        (
            {"type": "http", "url": "  ", "headers": {"Authorization": "Bearer abc"}},
            "http, no endpoint url",
        ),
        (
            {"type": "http", "url": "https://x", "headers": {"X-Key": "abc"}},
            "http, custom headers (X-Key)",
        ),
        # A BROKEN STDIO entry lands here too. Calling it "http, no credential"
        # would be a false claim about their config, so it gets its own reason.
        ({"command": ""}, "no usable launch command"),
        ({"command": 123}, "no usable launch command"),
        ({}, "no usable launch command"),
    ],
)
def test_not_wrappable_reason_is_per_entry(entry, expected):
    assert not kit.is_stdio(entry)
    assert kit.not_wrappable_reason(entry) == expected


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer s3cret"}},
        {"type": "http", "url": "https://x", "headers": {"X-Key": "s3cret"}},
        {"type": "http", "url": "https://x", "headers": {"Authorization": "Basic s3cret"}},
    ],
)
def test_not_wrappable_reason_never_prints_a_header_value(entry):
    """The list is shown to someone who may paste it back to us, and a header
    value is a credential. Header NAMES are the diagnostic; values never are."""
    assert "s3cret" not in kit.not_wrappable_reason(entry)


def test_write_preserves_the_config_files_permissions(tmp_path):
    """`~/.claude.json` is 0600 and holds OAuth tokens and every project's env
    block. os.replace carries the TEMP file's mode, and a fresh file is created
    at the umask (0644) — so the atomic-write fix, added to make the config
    safer, would have published those credentials to every user on the box, and
    uninstall would not have put the mode back."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"mcpServers": {}}', encoding="utf-8")
    cfg.chmod(0o600)
    kit.write_atomically(cfg, '{"mcpServers": {"x": {}}}')
    assert cfg.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.baton-tmp")), "temp file must not be left behind"


def test_write_preserves_a_permissive_mode_too(tmp_path):
    """Preserve, not clamp — the kit's job is to leave the file as it found it."""
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    cfg.chmod(0o644)
    kit.write_atomically(cfg, '{"a": 1}')
    assert cfg.stat().st_mode & 0o777 == 0o644


# =============================================================================
# Discovery — the port's two divergences from scan.py's reader.
# =============================================================================


def test_discovery_finds_every_scope_and_keeps_the_location():
    got = {(scope, name) for scope, name, _e in kit.iter_entries(PROJECT_SCOPED)}
    assert got == {(None, "other"), ("/Users/someone/work/app", "notion")}


def test_discovery_does_not_key_projects_on_cwd(monkeypatch, tmp_path):
    """scan.py reads projects[os.getcwd()]. The kit runs from try/, so a cwd
    lookup would search a project the user has never opened. Entries must be
    found from anywhere."""
    monkeypatch.chdir(tmp_path)
    names = {n for _s, n, _e in kit.iter_entries(PROJECT_SCOPED)}
    assert "notion" in names


# =============================================================================
# Drift pin — the copied helper against its donor in scan.py.
# =============================================================================


@pytest.mark.parametrize(
    "cmd",
    [
        ["npx", "-y", "srv"],
        ["baton-proxy", "--", "npx", "-y", "srv"],
        ["python3", "-m", "baton_proxy", "--", "npx", "srv"],
        ["python3", "-m", "baton_proxy", "--", "baton-proxy", "--", "npx", "srv"],
        ["baton-proxy"],
        ["baton-proxy", "--verbose"],
        ["baton-proxy", "--"],
        ["python3", "-m", "baton_proxy", "--url", "https://x/mcp"],
        [],
    ],
)
def test_unwrap_matches_scan_helper(cmd):
    """kit.py copies scan.py's unwrap rather than importing it (setup runs
    before anything is importable). Copies drift; this is the pin."""
    from baton_proxy.scan import _unwrap_baton_proxy

    assert kit.unwrap_command(list(cmd)) == _unwrap_baton_proxy(list(cmd))


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "npx", "args": ["-y", "srv"]},
        {"command": "baton-proxy", "args": ["--", "npx", "srv"]},
        {"command": "python3", "args": ["-m", "baton_proxy", "--url", "https://x/mcp"]},
        {"command": "uvx", "args": ["baton-proxy", "--verbose"]},
        {"command": "uv", "args": ["run", "baton-proxy"]},
        {"command": "bash", "args": ["-lc", "baton-proxy -- npx srv"]},
        {"command": "npx", "args": ["--prefix", "/opt/baton-proxy", "srv"]},
        {"command": "", "args": []},
    ],
)
def test_is_wrapped_matches_scans_proxy_detector(entry):
    """The second copied helper. `is_wrapped` and scan's `_launches_baton_proxy`
    are the same token sweep in two files, and they guard the same thing from
    opposite sides — the kit refuses to wrap a proxy, scan refuses to scan one.
    Copies drift; this is the pin, same as the unwrap one above."""
    from baton_proxy.scan import _launches_baton_proxy

    cmd = [entry.get("command", ""), *[str(a) for a in entry.get("args") or []]]
    assert kit.is_wrapped(entry) is _launches_baton_proxy(cmd)


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.zapier.com/api/mcp/s/SUPERSECRET/sse",
        "https://api.example.com/mcp?key=SUPERSECRET",
        "https://user:SUPERSECRET@api.example.com/mcp",
        "http://127.0.0.1:8789/mcp",
        "not a url at all",
        "",
    ],
)
def test_safe_endpoint_matches_scans_copy(url):
    """The third copied helper. Both files print endpoints in refusals, and both
    have to hide the same three credential vectors — a copy that drifted would
    leak on whichever side fell behind."""
    from baton_proxy.scan import _safe_endpoint

    assert kit.safe_endpoint(url) == _safe_endpoint(url)


# =============================================================================
# Receipt.
# =============================================================================


def _ev(**kw):
    base = {
        "event_id": "e",
        "session_id": "s1",
        "event_type": "tool_call_start",
        "captured_at": "2026-08-19T10:00:00Z",
        "payload": {},
    }
    base.update(kw)
    return base


def test_receipt_counts_sessions_calls_and_intent_coverage():
    """Intent is counted per CALL, not per session. One goal in a session of
    twenty is one covered call, and the session grain reported it as full
    coverage of that session."""
    events = [
        _ev(session_id="s1", payload={"tool_name": "search", "call_intent": "find the doc"}),
        _ev(session_id="s1", payload={"tool_name": "search"}),
        _ev(session_id="s2", payload={"tool_name": "search"}),
    ]
    s = kit.summarize(events, 1234)
    assert s["sessions"] == 2
    assert s["tool_calls"] == 3
    assert s["calls_with_intent"] == 1


def test_a_synthesised_annotation_is_not_counted_as_one_the_agent_filed():
    """The proxy makes one annotation per session out of the first call's
    injected params (`intent_source="injected_param"`). Counting those as
    agent-filed reports an agent that called nothing as one that filed on every
    session — which is what the first denial-verify oracle did, and it would
    have failed a passing run."""
    events = [
        _ev(
            session_id="s1",
            event_type="annotation",
            payload={"intent": "find the doc", "intent_source": "injected_param"},
        ),
        _ev(
            session_id="s1",
            event_type="annotation",
            payload={"intent": "find the doc", "signal_type": "failure"},
        ),
    ]
    s = kit.summarize(events, 1234)
    assert s["agent_annotations"] == 1


def test_the_two_intent_mechanisms_are_reported_apart(tmp_path, kit_home, capsys):
    """Params and the tool fail for different reasons, and only one of them can
    be refused. Merging them at session grain is what left the first human-led
    run's empty intent layer open to being explained as a refusal."""
    out = _receipt_with_events(
        tmp_path,
        kit_home,
        capsys,
        [
            {
                "event_type": "tool_call_start",
                "session_id": "s1",
                "captured_at": "2026-08-30T10:00:00Z",
                "payload": {"tool_name": "echo", "call_intent": "check the wrap"},
            },
            {
                "event_type": "tool_call_start",
                "session_id": "s1",
                "captured_at": "2026-08-30T10:00:02Z",
                "payload": {"tool_name": "echo"},
            },
        ],
    )
    assert "intent captured      1 of 2 tool calls" in out
    assert "annotations filed    0 by your agent" in out
    # The zero that CAN be a refusal says so, and says the refusal is invisible.
    assert "declined the tool at" in out
    # The zero that cannot is not on this receipt at all.
    assert "nothing here to refuse" not in out


def test_the_four_forks_are_asked_the_same_way_and_the_text_form_is_complete():
    """Track 2's chooser rule. Two halves, and the second is the load-bearing
    one: a client without the tool is a real prospect, so the text form has to
    carry the same facts. Pinned because the failure is silent — a fact that
    only ever appears inside an option label reads as disclosed to whoever
    wrote it and as never said to whoever skimmed it."""
    assert "## How to ask" in _claude_md(), "the chooser rule has no section"
    section = _flat(_doc_section("## How to ask", "## Start by finding out"))
    assert "AskUserQuestion" in section, "the section never names the client's chooser"
    # The rule itself: prose above, choice below. A fact that only ever appears
    # inside an option label reads as disclosed to whoever wrote it.
    assert "facts in prose above it" in section
    # Absorbed 2026-09-04 from `test_the_ask_has_to_name_what_it_is_asking_about`,
    # which pinned this in the paste as well. The paste no longer says how to
    # ask anything, so the doc is the only sink left and the rule is checked
    # where it now lives rather than deleted with the file that lost it.
    assert "says what it is about and what happens next" in section
    # The fallback, and that it is the complete one rather than the reduced
    # one. The last-line ordering comes here from
    # `test_the_ask_is_the_last_line_in_both_files` for the same reason.
    assert "Without a chooser" in section
    assert "the question alone on the last line" in section
    # Absorbed 2026-09-04 from the ending-fork test, which was deleted with the
    # three-option fork it pinned. The general rule outlived that fork and is
    # where the next surface will look for it: an option that exists in prose
    # and not in the chooser has not been offered.
    assert "every option a section names even if you expect it to be false" in section
    # V1, 2026-09-17. The rule named three slots — prose above, labels, the
    # question — and the chooser has a fourth. With nothing said about the row
    # under each option, the agent filled it with the prose again, expanded, and
    # the operator's words were "these options and their verbose descriptions
    # are really confusing to make a decision". The facts were disclosed twice
    # and the choice got harder, which is the opposite of what a chooser is for.
    assert "The row under each option is one line" in section, (
        "the per-option row is unspecified again, so the facts go in twice"
    )
    assert "It is not a second copy of the prose" in section, (
        "the row no longer forbids restating the prose, which is the failure V1 saw"
    )
    assert "belongs above, once" in section, (
        "the rule no longer says where a fact common to every option goes"
    )


def test_claude_md_routes_on_both_intent_rows_by_name():
    """Track 2.1. The doc's branch for these two numbers is only reachable if it
    names the rows the receipt prints, so the labels are read out of the doc —
    a rename on either side fails here rather than leaving the agent to explain
    a row the doc never mentions. The prose itself is NOT pinned: whether the
    agent relays it is a followability property and is graded by the harness."""
    doc = _claude_md()
    for row in ("intent captured", "annotations filed"):
        assert f"`{row}`" in doc, f"CLAUDE.md no longer routes on the `{row}` row"


def test_a_zero_intent_layer_is_not_left_open_to_a_refusal(tmp_path, kit_home, capsys):
    """The first human-led run captured four calls with no intent and the agent
    reported it as "the agent never called `baton_annotate`". Nothing was
    called: intent rides the tool schema, so a refusal cannot produce this
    number, and the receipt says which causes remain rather than leaving the
    space for one to be invented."""
    out = _receipt_with_events(
        tmp_path,
        kit_home,
        capsys,
        [
            {
                "event_type": "tool_call_start",
                "session_id": "s1",
                "captured_at": "2026-08-30T10:00:00Z",
                "payload": {"tool_name": "echo"},
            }
        ],
    )
    assert "intent captured      0 of 1 tool calls" in out
    assert "nothing here to refuse" in out
    assert "Why the model" in out and "not in this file" in out


def test_neither_zero_note_fires_when_nothing_reached_the_server(tmp_path, kit_home, capsys):
    """`0 of 0 tool calls` plus six lines explaining the zero argues with the
    CONNECTED, BUT NOTHING CALLED IT banner printed underneath it."""
    out = _receipt_with_events(
        tmp_path,
        kit_home,
        capsys,
        [
            {
                "event_type": "surface_snapshot",
                "session_id": "s1",
                "captured_at": "2026-08-30T10:00:00Z",
                "payload": {"tools": [{"name": "echo"}]},
            }
        ],
    )
    assert "CONNECTED, BUT NOTHING CALLED IT" in out
    assert "intent captured" not in out
    assert "annotations filed" not in out


def test_receipt_reads_redaction_markers_off_the_file_not_process_state():
    """Scrub counters live in a process that exited days ago; the receipt must
    answer from a cold session, so it counts the markers that survived."""
    events = [
        _ev(payload={"result": "mail [REDACTED:email] and [REDACTED:email], key [REDACTED:sk_key]"})
    ]
    s = kit.summarize(events, 10)
    assert s["redactions"] == {"email": 2, "sk_key": 1}


def _run_receipt(tmp_path, monkeypatch, capsys, events_lines):
    events = tmp_path / "events.jsonl"
    events.write_text("".join(json.dumps(e) + "\n" for e in events_lines))
    monkeypatch.setattr(kit, "STATE_PATH", tmp_path / "no-state.json")
    monkeypatch.setattr(kit, "EVENTS_PATH", events)
    kit.cmd_receipt(argparse.Namespace())
    return events, capsys.readouterr().out


def test_the_receipt_ends_on_the_page_the_person_uploads_it_on(tmp_path, monkeypatch, capsys):
    """This test has been reversed twice and is now back where it started, which
    is worth recording rather than rewriting away.

    It first read: the receipt "must not name, offer, or imply a place to send
    it", on the reasoning that a destination costs the one sentence the kit
    sells. Then `kit.py upload` shipped and it became "an address, yes; an
    endpoint, no", because the kit itself had gained a way to send. 0.6.0 takes
    that away again: there is no command that sends and no credential in the
    kit, so naming the page the person signs in to costs nothing: no call site,
    no key, nothing the kit would talk to.

    So what is pinned is the ending itself. The receipt hands over the file's
    real path and the one page it goes to, and it is still the person who
    uploads it."""
    events, out = _run_receipt(tmp_path, monkeypatch, capsys, [_ev(payload={"tool_name": "s"})])
    assert kit.setup_note(events) in out, f"the receipt does not end with the Setup line:\n{out}"
    assert str(events) in out, "the ending never names the file it is about"
    assert kit.SETUP_URL in out, "the ending never names the page the capture goes to"
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", out), (
        f"the receipt names an address to send a capture to:\n{out}"
    )


def test_receipt_reports_no_error_counts():
    """By design: the receipt proves capture, it does not preview analysis.
    Error counts are something the user can already get for themselves."""
    events = [_ev(event_type="tool_call_error", payload={"error_type": "boom"})]
    s = kit.summarize(events, 10)
    assert "errors" not in s


def test_receipt_takes_the_tool_surface_from_the_snapshot():
    events = [
        _ev(
            event_type="surface_snapshot",
            payload={"tools": [{"name": "search"}, {"name": "create"}]},
        )
    ]
    assert kit.summarize(events, 10)["tools"] == ["search", "create"]


def test_read_events_skips_a_truncated_final_line(tmp_path):
    """The proxy killed mid-write must not make the receipt unavailable."""
    p = tmp_path / "events.jsonl"
    p.write_text(
        '{"event_type":"tool_call_start","session_id":"s1","payload":{}}\n{"trunc', encoding="utf-8"
    )
    assert len(kit.read_events(p)) == 1


# =============================================================================
# Credential-at-rest and the printed record (2026-08-20).
#
# The kit already had a rule — name it, never quote it — but it was enforced on
# ONE FIELD, the candidate list's endpoint. state.json and every entry print
# walked around it. These pin the rule on the record instead.
# =============================================================================


def test_state_file_is_not_world_readable(tmp_path, monkeypatch):
    """It holds original_entry verbatim, env included, copied out of a config
    that is usually 0600. write_text would create it 0644 under the usual umask."""
    monkeypatch.setattr(kit, "STATE_PATH", tmp_path / "state.json")
    kit.write_state_file({"version": 1, "original_entry": {"env": {"TOKEN": "s3cret"}}})
    assert kit.STATE_PATH.stat().st_mode & 0o077 == 0, "group/other can read the token"


def test_state_file_mode_is_fixed_even_when_it_already_exists(tmp_path, monkeypatch):
    """os.open sets the mode only on CREATE, so a 0644 file left by an earlier
    version would keep its mode through every subsequent setup."""
    stale = tmp_path / "state.json"
    stale.write_text("{}", encoding="utf-8")
    stale.chmod(0o644)
    monkeypatch.setattr(kit, "STATE_PATH", stale)
    kit.write_state_file({"version": 1})
    assert stale.stat().st_mode & 0o077 == 0


def test_backup_is_not_world_readable_when_the_config_is(tmp_path):
    """The backup is the whole config verbatim, every env block included. It was
    `shutil.copy2`, which copies the SOURCE's mode, so a 0644 config made a 0644
    backup while SECURITY.md §2 and §7 said 0600."""
    source = tmp_path / "claude.json"
    source.write_text('{"mcpServers": {"s": {"env": {"TOKEN": "s3cret"}}}}', encoding="utf-8")
    source.chmod(0o644)
    backup = tmp_path / "config-backup.json"
    kit.write_backup(source, backup)
    assert backup.stat().st_mode & 0o777 == 0o600, "group/other can read the token"
    assert backup.read_bytes() == source.read_bytes(), "the backup is no longer a verbatim copy"


def test_backup_mode_is_fixed_even_when_the_file_already_exists(tmp_path):
    """os.open sets the mode only on CREATE. The name is stamped to the second,
    so a second setup inside that second writes into the first one's file."""
    source = tmp_path / "claude.json"
    source.write_text("{}", encoding="utf-8")
    stale = tmp_path / "config-backup.json"
    stale.write_text("old", encoding="utf-8")
    stale.chmod(0o644)
    kit.write_backup(source, stale)
    assert stale.stat().st_mode & 0o777 == 0o600
    assert stale.read_text(encoding="utf-8") == "{}"


def test_setup_writes_its_backup_0600_from_a_0644_config(tmp_path, kit_home, capsys):
    """The two above prove nothing if setup stops calling the helper. This is
    the path SECURITY.md describes, driven from a config other users can read.

    `--in-place` since K1b: there is no backup on the default path, because
    project mode never writes their config and a backup of a file nothing
    touches would be the reassurance without the risk. This pins the mode that
    does write, which is the only mode SECURITY.md's backup paragraph is
    about."""
    path = _config(tmp_path, GLOBAL_ONLY)
    path.chmod(0o644)
    before = path.read_bytes()
    assert kit.main(["setup", "notion", "--src-config", str(path), "--in-place"]) == 0
    (backup,) = kit_home.glob("config-backup.*.json")
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o644, "§2: the config keeps its own mode"


def test_a_literal_env_value_is_never_printed():
    entry = {"command": "npx", "args": ["-y", "srv"], "env": {"SNOWFLAKE_PAT": "xoxb-REAL-TOKEN"}}
    out = kit.entry_json(entry)
    assert "xoxb-REAL-TOKEN" not in out
    assert "SNOWFLAKE_PAT" in out, "the KEY must survive — the print is a restore recipe"
    assert "npx" in out and "srv" in out, "structure must survive too"


def test_a_var_reference_is_shown_because_it_is_a_pointer_not_a_secret():
    """`${VAR}` is the recommended pattern and is what makes a printed entry
    checkable. Redacting it would cost readability and buy nothing."""
    entry = {"command": "npx", "env": {"SNOWFLAKE_PAT": "${SNOWFLAKE_PAT}"}}
    assert "${SNOWFLAKE_PAT}" in kit.entry_json(entry)


def test_our_own_variables_stay_visible():
    """Setup's whole purpose is showing what it wrote, and launch_check needs
    PYTHONPATH readable."""
    out = kit.entry_json(kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS))
    assert kit.HIDDEN not in out
    for k in ("PYTHONPATH", "BATON_TENANT_ID", "BATON_VENDOR_ID", "BATON_EVENT_SINK"):
        assert k in out


def test_redaction_never_mutates_the_entry_it_was_given():
    """It runs on state["original_entry"], which uninstall then writes back to
    the config. Mutating it would restore the marker string as a real value."""
    entry = {"command": "npx", "env": {"TOKEN": "literal"}}
    kit.entry_json(entry)
    assert entry["env"]["TOKEN"] == "literal"


def test_refuse_paths_hide_the_value_but_keep_the_recipe_and_say_where_it_is():
    """These dumps exist so someone can restore by hand. Redacting them without
    saying where the exact bytes are would strand a person mid-uninstall."""
    src = json.loads(canonical(GLOBAL_ONLY))
    src["mcpServers"]["notion"]["env"] = {"NOTION_TOKEN": "secret-literal-value"}
    before = json.dumps(src, indent=2) + "\n"
    wrapped_text, state = kit.apply_wrap(before, scope=None, name="notion", **WRAP_ARGS)
    gutted = json.loads(wrapped_text)
    del gutted["mcpServers"]["notion"]
    with pytest.raises(kit.Refuse) as e:
        kit.apply_unwrap(json.dumps(gutted, indent=2) + "\n", state)
    msg = str(e.value)
    assert "secret-literal-value" not in msg
    assert "@notionhq/notion-mcp-server" in msg, "the recipe must still be readable"
    assert "state.json" in msg, "and must say where the hidden values are"


def test_the_pointer_to_state_covers_every_field_the_redaction_touches():
    """The pointer is prose, and prose scoped to one field is the same defect as
    code scoped to one field. An http entry's refusal hides header values AND
    shortens the url, so a pointer that only mentioned `env` would leave someone
    staring at a truncated endpoint with no idea it was deliberate."""
    for field in ("env", "header", "URL"):
        assert field in kit.STATE_POINTER
    assert "state.json" in kit.STATE_POINTER


def test_uninstall_verification_reads_the_file_not_the_return_value(tmp_path):
    """apply_unwrap returns the recorded original, so comparing in memory always
    passes and proves nothing. The check has to cover the write."""
    before = canonical(GLOBAL_ONLY)
    _, state = kit.apply_wrap(before, scope=None, name="notion", **WRAP_ARGS)
    cfg = tmp_path / "cfg.json"

    cfg.write_text(before, encoding="utf-8")
    assert kit.restored_matches_on_disk(cfg, state) is True

    tampered = json.loads(before)
    tampered["mcpServers"]["notion"]["args"].append("--drifted")
    cfg.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    assert kit.restored_matches_on_disk(cfg, state) is False

    cfg.write_text("{ not json", encoding="utf-8")
    assert kit.restored_matches_on_disk(cfg, state) is False


def test_redaction_covers_an_entry_shape_we_never_wrote():
    """`apply_unwrap`'s third refusal prints `current` — whatever the person
    hand-edited the entry into, which need not be the stdio shape setup wrote.
    An http shape puts the credential in `headers`, or in the URL path itself
    (Zapier, Composio). Redacting only `env` would enforce the rule on a field
    instead of on the record — the same mistake, one layer in."""
    current = {
        "type": "http",
        "url": "https://hooks.zapier.com/api/mcp/s/SECRET-IN-THE-PATH/sse",
        "headers": {"Authorization": "Bearer xoxb-LITERAL", "X-Api-Key": "k-LITERAL"},
    }
    out = kit.entry_json(current)
    assert "SECRET-IN-THE-PATH" not in out
    assert "xoxb-LITERAL" not in out
    assert "k-LITERAL" not in out
    assert "Authorization" in out and "X-Api-Key" in out, "header NAMES still identify it"
    assert "hooks.zapier.com" in out, "scheme+host stays, so the entry is recognisable"


def test_a_hidden_var_reference_is_not_called_a_literal():
    """The value stays hidden either way; what changes is the claim. A header of
    `Bearer ${ACME_TOKEN}` labelled `<literal value, not shown>` tells the reader
    something false about their own config, inside the refusal path that exists
    so they can reconcile it by hand."""
    out = kit.entry_json(
        {
            "type": "http",
            "url": "https://x/mcp",
            "headers": {"Authorization": "Bearer ${ACME_TOKEN}"},
        }
    )
    assert "ACME_TOKEN" not in out, "still hidden — this changes the label, not the visibility"
    assert kit.HIDDEN_VAR_REF in out
    assert kit.HIDDEN not in out, "nothing here is a literal"


def test_a_literal_beside_a_var_reference_is_still_called_a_literal():
    """The label must not fail in the other direction either. `Bearer sk-live
    ${X}` does hold a literal, so the strict end of the rule is what keeps the
    new label true — a contains-a-${VAR}-anywhere test would call it a pointer.

    The one-token case is the one that matters and the one a two-token fixture
    misses: `sk-live-abc123 ${SIG}` is a whole credential in the prefix slot,
    and it passes any rule that allows an arbitrary leading token. Only a scheme
    KEYWORD may precede the reference."""
    out = kit.entry_json(
        {
            "type": "http",
            "url": "https://x",
            "headers": {"Authorization": "Bearer sk-live-abc ${X}"},
            # The env rule's own documented strict edge: a composite hides, and
            # it hides as a LITERAL, because part of it may be one.
            "env": {"OTHER": "/usr/local/bin:${PATH}"},
        }
    )
    assert "sk-live-abc" not in out
    assert kit.HIDDEN in out
    assert kit.HIDDEN_VAR_REF not in out

    for literal_prefix in ("sk-live-abc123 ${SIG}", "xoxb-REAL-TOKEN ${N}"):
        assert kit.hidden_label(literal_prefix) == kit.HIDDEN, (
            f"{literal_prefix!r} carries a literal in the prefix slot"
        )
    for scheme in ("Bearer ${T}", "bearer ${T}", "Basic ${T}", "Token ${T}", "${T}"):
        assert kit.hidden_label(scheme) == kit.HIDDEN_VAR_REF, f"{scheme!r} is a pointer"


def test_a_users_own_BATON_prefixed_variable_is_not_treated_as_ours():
    """SECURITY.md §7 explicitly contemplates a user owning a BATON_-prefixed
    variable. A prefix test would print its literal value on the grounds that we
    must have written it — we did not."""
    entry = {"command": "npx", "env": {"BATON_CUSTOM_TOKEN": "not-ours-literal"}}
    assert "not-ours-literal" not in kit.entry_json(entry)


# =============================================================================
# The http bridge. baton-proxy has spoken Streamable HTTP since 0.2.2; until now
# the KIT refused every remote entry, which made the trial's boundary narrower
# than the product's.
# =============================================================================


def _bridge(entry):
    return kit.build_wrapped_entry(entry, interpreter="/usr/bin/python3.13", **WRAP_ARGS)


def test_the_bridge_rewrite_is_a_shape_change_not_a_demotion():
    """stdio wrapping keeps the entry and demotes its command. Here the entry
    stops being an http entry at all: the client must launch a subprocess, and
    the url it used to dial moves into that subprocess's ARGV."""
    wrapped = _bridge(HTTP_VAR_BEARER["mcpServers"]["remote"])
    assert wrapped["command"] == "/usr/bin/python3.13"
    assert wrapped["args"] == ["-m", "baton_proxy", "--url", "https://mcp.example.com/mcp"]
    # An entry claiming both transports is ambiguous to the client, and would
    # hand a bearer header to a server that is now local.
    for gone in ("type", "url", "headers"):
        assert gone not in wrapped, f"`{gone}` must not survive into a stdio-shaped entry"


def test_the_bearer_moves_slot_without_ever_being_resolved():
    """The invariant the design note assumed the http class would cost, and did
    not: a `${VAR}` reference is carried across as a REFERENCE. The client
    expanded it in `headers` before and expands it in `env` after, and the kit
    never learns the token. Client behaviour, verified on 2.1.223 — not a
    protocol guarantee, which is why no user-facing line promises it."""
    wrapped = _bridge(HTTP_VAR_BEARER["mcpServers"]["remote"])
    assert wrapped["env"]["BATON_UPSTREAM_AUTH_TOKEN"] == "${REMOTE_TOKEN}"


def test_the_bearer_prefix_is_stripped_because_the_bridge_re_adds_it():
    """`transport_http` composes `Authorization: Bearer {token}` itself. Passing
    the prefix through would put `Bearer Bearer …` on the wire — a 401 days after
    setup, on a machine we cannot see."""
    entry = {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer tok"}}
    assert _bridge(entry)["env"]["BATON_UPSTREAM_AUTH_TOKEN"] == "tok"


def test_an_odd_bearer_spelling_still_yields_a_clean_token():
    """Header names are case-insensitive per RFC, the scheme is too, and a human
    hand-editing a config types a second space. All three reach the wire."""
    entry = {
        "type": "http",
        "url": "https://x/mcp",
        "headers": {"authorization": "bearer   ${TOK}  "},
    }
    assert _bridge(entry)["env"]["BATON_UPSTREAM_AUTH_TOKEN"] == "${TOK}"


def test_baton_vars_are_written_last_so_the_users_env_cannot_shadow_them():
    """Same rule the stdio path has, now with one more variable under it — and
    this one carries a credential, so a shadowing entry would silently swap the
    token the bridge presents."""
    entry = {
        "type": "http",
        "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer ${REAL}"},
        "env": {"BATON_UPSTREAM_AUTH_TOKEN": "${DECOY}", "BATON_TENANT_ID": "someone-else"},
    }
    env = _bridge(entry)["env"]
    assert env["BATON_UPSTREAM_AUTH_TOKEN"] == "${REAL}"
    assert env["BATON_TENANT_ID"] == "trial-abc123"


def test_keys_we_do_not_recognise_survive_the_rewrite():
    """`type`/`url`/`headers` are dropped because they are ours to translate.
    Everything else in the entry belongs to the user — dropping a `disabled` flag
    or a client-specific key would be damage they did not ask for."""
    entry = {
        "type": "http",
        "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer t"},
        "disabled": False,
        "someClientKey": {"a": 1},
    }
    wrapped = _bridge(entry)
    assert wrapped["disabled"] is False
    assert wrapped["someClientKey"] == {"a": 1}


def test_a_literal_upstream_token_is_hidden_but_a_reference_is_shown():
    """BATON_UPSTREAM_AUTH_TOKEN is deliberately NOT in `_OUR_ENV_KEYS`. We write
    the KEY, but the VALUE is the user's credential — putting it on the show-list
    because "we wrote it" is the invariant-scoped-to-one-field mistake wearing a
    different hat, and it would print a live token into an agent's context."""
    assert "BATON_UPSTREAM_AUTH_TOKEN" not in kit._OUR_ENV_KEYS
    literal = _bridge(HTTP_LITERAL_BEARER["mcpServers"]["remote"])
    out = kit.entry_json(literal)
    assert "sk-live-LITERAL-abc123" not in out
    assert "BATON_UPSTREAM_AUTH_TOKEN" in out, "the key name still shows what is set"
    assert "--url" in out and "mcp.example.com" in out, "still the restore recipe"

    ref = kit.entry_json(_bridge(HTTP_VAR_BEARER["mcpServers"]["remote"]))
    assert "${REMOTE_TOKEN}" in ref, "a pointer is not a credential, and is the useful half"


def test_our_own_bridge_output_reads_as_wrapped():
    """Otherwise setup would wrap it a second time — `--url` bridges carry no
    `--`, which is the exact hole `is_proxy_invocation` was widened to close.
    The guard has to hold against what we now WRITE, not just hand-made shapes."""
    wrapped = _bridge(HTTP_VAR_BEARER["mcpServers"]["remote"])
    assert kit.is_wrapped(wrapped) is True
    assert kit.is_stdio(wrapped) is True, "it is a stdio entry now, by construction"


@pytest.mark.parametrize(
    "entry,why",
    [
        ({"type": "sse", "url": "https://x/sse"}, "a transport the bridge does not speak"),
        (
            {
                "type": "http",
                "url": "https://x",
                "headers": {"Authorization": "Bearer a", "X-T": "b"},
            },
            "the bridge sends one header of its own and cannot carry the rest",
        ),
        (
            {"type": "http", "url": "https://x", "headers": {"Authorization": "Basic a"}},
            "not a bearer; the bridge has no way to present it",
        ),
        (
            {"type": "http", "url": "https://x"},
            "ambiguous: public endpoint, or OAuth whose token the CLIENT holds. "
            "Wrapping the second kind is a dead server found days later.",
        ),
        ({"type": "http", "url": "", "headers": {"Authorization": "Bearer a"}}, "no endpoint"),
        ({"type": "http", "headers": {"Authorization": "Bearer a"}}, "no endpoint"),
    ],
)
def test_the_wrappable_remote_class_stays_narrow(entry, why):
    """Each refusal here prevents the same failure: a wrap that looks successful
    and produces a server that cannot authenticate, discovered after the restart
    with nothing pointing at the cause."""
    assert kit.http_bridge(entry) is None, why
    assert kit.is_wrappable(entry) is False


def test_stdio_wins_when_an_entry_somehow_claims_both():
    """A hand-made entry with a command AND a url. Demoting the command is
    reversible; dropping it is not, so the ambiguous case takes the safe branch."""
    entry = {
        "command": "npx",
        "args": ["-y", "srv"],
        "url": "https://x/mcp",
        "headers": {"Authorization": "Bearer t"},
    }
    assert kit.http_bridge(entry) is None
    assert _bridge(entry)["args"] == ["-m", "baton_proxy", "--", "npx", "-y", "srv"]


def test_one_bearer_normalization_serves_both_callers():
    """`http_bridge` decides wrappability and `not_wrappable_reason` explains a
    refusal. Two copies of "is this a bearer" drifting apart would let an entry
    be offered as a candidate and then described as unwrappable."""
    sole = {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer ${T}"}}
    assert kit.is_wrappable(sole)
    token, others = kit.bearer_header(sole)
    assert (token, others) == ("${T}", [])
    plus = {**sole, "headers": {**sole["headers"], "X-T": "b"}}
    assert not kit.is_wrappable(plus)
    assert kit.bearer_header(plus) == ("${T}", ["X-T"])


# ---------------------------------------------------------------------------
# The CLI contract.
#
# Everything above calls apply_wrap/apply_unwrap/cmd_* directly. `main` — the
# argparse wiring and the exit codes it produces — had no test at all, which
# means the surface an agent actually drives was the one surface nothing
# pinned. kit.py:30 states the contract as "0 success, 1 refusal, 2 usage", and
# try/CLAUDE.md is written against it: "a refusal is an answer" (CLAUDE.md:31)
# tells the agent to relay a 1 verbatim rather than retry, and CLAUDE.md:84-86
# promises that `receipt` and `uninstall` "will reject the flag if you pass it".
# ---------------------------------------------------------------------------


@pytest.fixture
def kit_home(tmp_path, monkeypatch):
    """Point every path the kit writes into at a tmp dir.

    Not optional cleanliness: `cmd_setup` drops a `config-backup.*` beside
    TRY_DIR and `write_state_file` writes STATE_PATH, both of which are the
    REAL `try/` directory by default. Without this a test run stomps a live
    trial's state — the hazard `spikes/http_entry_wrap/run_kit_bridge_e2e.sh`
    carries today. SRC_DIR is deliberately left real: `cmd_setup` refuses when
    it is missing, and that refusal is not what these tests are about.

    ⚠ MCP_PATH is a FOURTH such path and it was not covered here until K1b.
    While the default was global, only a test that opted into `project_mode`
    could reach it, and that fixture redirects it. At the flip every setup test
    takes the project path, so the redirect has to be the default rather than
    the opt-in. Without it the suite writes a live `.mcp.json` into this
    working tree, K7's `.gitignore` hides it from `git status`, the next
    `claude` started here loads it, and the following run fails on the leftover
    rather than on anything real.

    ⚠ MCP_PATH goes in a CHECKOUT dir beside the kit home, never inside it.
    In production `MCP_PATH.parent` is the checkout root and `TRY_DIR` is
    `try/` below it, so the two are never the same directory — `kit.py:79-83`
    says a `try/.mcp.json` is wrong, and `start_where` exists to stop the kit
    offering its own directory as the place to start the client. A first cut
    here used `home / ".mcp.json"`, making `MCP_PATH.parent == TRY_DIR`, and
    `test_the_handoff_never_offers_the_kits_own_directory_as_the_place_to_start`
    failed with `cd <tmp>/kit-home && claude` in the output — a red caused by
    the fixture that read as fallout from the K1b flip.
    """
    home = tmp_path / "kit-home"
    home.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    monkeypatch.setattr(kit, "TRY_DIR", home)
    monkeypatch.setattr(kit, "STATE_PATH", home / "state.json")
    monkeypatch.setattr(kit, "EVENTS_PATH", home / "events.jsonl")
    monkeypatch.setattr(kit, "MCP_PATH", checkout / ".mcp.json")
    return home


def _config(tmp_path, data) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(canonical(data), encoding="utf-8")
    return path


# =============================================================================
# Project mode — the trial stops editing their config (K1a, 2026-09-17).
#
# Built with the default still MODE_GLOBAL, so every test above keeps
# describing the kit as it ships and these describe the path K1b will switch to.
# `project_mode` below is the opt-in; at the flip it becomes the default and
# these tests stay exactly as they are.
# =============================================================================


@pytest.fixture
def project_mode(tmp_path, monkeypatch, kit_home):
    """Turn on project mode and hand back the file it writes.

    MCP_PATH is `CHECKOUT/.mcp.json` — a real path in this working tree — so
    without a redirect a test run writes a project config into the repo and
    the next `claude` started here loads it. `kit_home` now redirects it for
    every test, to this same `tmp_path/checkout/`; what this fixture still owns
    is turning the mode on and RETURNING the path, which its callers assert on.

    ⚠ `kit_home` is requested rather than left to signature order. Both
    fixtures set MCP_PATH, and while they now agree on the value, a future
    change to either would be decided by whichever ran last. Declaring the
    dependency makes that order a fact rather than a coincidence of every
    caller happening to list `kit_home` first.

    ⚠ `exist_ok` because `kit_home` creates this directory first, by the same
    dependency. Without it the fixture raises FileExistsError on every test
    that uses it.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    monkeypatch.setattr(kit, "DEFAULT_MODE", kit.MODE_PROJECT)
    monkeypatch.setattr(kit, "MCP_PATH", checkout / ".mcp.json")
    return checkout / ".mcp.json"


def test_project_mode_writes_our_file_and_leaves_theirs_byte_identical(
    tmp_path, kit_home, project_mode, capsys
):
    """The decision, as one assertion: their config is not written.

    Byte equality rather than "the entry is still there" — the promise made to
    three prospects is about the FILE, and a kit that reformatted it or
    reordered a key while preserving the entry would have broken that promise
    while passing a semantic check."""
    path = _config(tmp_path, GLOBAL_ONLY)
    before = path.read_bytes()

    assert kit.main(["setup", "notion", "--src-config", str(path)]) == 0
    capsys.readouterr()

    assert path.read_bytes() == before, "their config was written to in project mode"
    assert project_mode.exists(), "the project config was not written"

    written = json.loads(project_mode.read_text(encoding="utf-8"))
    assert list(written) == ["mcpServers"], "a project config is `mcpServers` at the top level"
    assert list(written["mcpServers"]) == ["notion"], "the key name is theirs and must not change"
    assert kit.is_wrapped(written["mcpServers"]["notion"]), "the entry we wrote is not wrapped"

    # No backup, because nothing of theirs was overwritten. The backup exists to
    # make a bad write recoverable, and the whole point of this mode is that
    # there is no write to their file to recover from.
    assert not list(kit_home.glob("config-backup.*.json"))


def test_the_project_config_is_0600_because_it_can_hold_their_token(
    tmp_path, kit_home, project_mode, capsys
):
    """A new file on their disk, and `build_wrapped_entry` copies `env`
    verbatim — so an entry whose original carried a literal token rather than a
    `${VAR}` reference puts that token in a file this kit created.

    `write_atomically` took its mode from the file it was replacing, which is
    right for every other caller and has nothing to read when the file is new.
    Under the usual umask that is 0644, which republishes a credential that was
    0600 in `~/.claude.json` to every account on the box — the same mode slip
    `write_state_file` and `write_backup` each document having made."""
    path = _config(
        tmp_path,
        {"mcpServers": {"srv": {"command": "npx", "env": {"TOKEN": "xoxb-REAL-SECRET"}}}},
    )

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 0
    capsys.readouterr()

    assert project_mode.stat().st_mode & 0o777 == 0o600, (
        "the project config is world-readable and holds a literal token"
    )
    assert "xoxb-REAL-SECRET" in project_mode.read_text(encoding="utf-8"), (
        "the token must really be in there, or this test passes for the wrong reason"
    )


def test_project_mode_records_both_where_it_wrote_and_where_it_read(
    tmp_path, kit_home, project_mode, capsys
):
    """state.json carries two locations, and each has one reader.

    `config_path`/`scope` are where the wrap LIVES — what receipt checks and
    uninstall removes. `source_config_path`/`source_scope` are where it was
    copied FROM, and that pair is the only record that their config was read
    and not touched.

    `scope` must be None: the entry sits at the top level of the file we write,
    and `wrap_still_present` and `apply_unwrap` both look the entry up with
    `entry_at(data, scope)`. Anything else sends them into a `projects` block
    this file does not have, and the failure reads as "THE WRAP IS GONE"."""
    path = _config(tmp_path, PROJECT_SCOPED)

    assert kit.main(["setup", "notion", "--src-config", str(path)]) == 0
    capsys.readouterr()
    state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))

    assert state["mode"] == kit.MODE_PROJECT
    assert state["config_path"] == str(project_mode)
    assert state["scope"] is None, "the entry is at the top level of the file we wrote"
    assert state["source_config_path"] == str(path)
    assert state["source_scope"] == "/Users/someone/work/app", "copied from a project key"

    assert kit.wrap_still_present(state), (
        "receipt must find the wrap it just wrote; if `scope` is wrong this is "
        "where it reports THE WRAP IS GONE on a trial that is working"
    )


def test_project_mode_warns_about_a_relative_path_and_names_the_way_out(
    tmp_path, kit_home, project_mode, capsys
):
    """The K8 guard at its call site, which is the part no test could reach.

    ⚠ THIS WAS A REFUSAL UNTIL 2026-09-17 and is now a warning — the operator's
    call, after the refusal was measured against an ordinary `mcp-remote` entry
    whose `--header "Authorization: Bearer abc/def"` it rejected. The promises
    below are the refusal's, minus the veto, and the change of the FIRST one is
    the whole diff: the wrap now happens.

    Each fails differently if the wiring is wrong. That the guard is CALLED at
    all — the pure function has always passed its own tests with no caller.
    That it runs BEFORE the wrap: afterwards the relative path has moved into
    `args`, so the message would say "argument 4" of an entry whose fourth
    argument does not exist. And that the way out is `--in-place`, never a
    hand-edit — the kit telling Bharath to rename an entry by hand in the file
    he had just said he would not touch is why this thread exists."""
    path = _config(tmp_path, {"mcpServers": {"srv": {"command": "bin/server"}}})

    rc = kit.main(["setup", "srv", "--src-config", str(path)])
    out, _err = capsys.readouterr()

    assert rc == 0, "the guard no longer vetoes; a shape it cannot verify is a warning"
    assert project_mode.exists(), "the wrap must still happen — that is what changed"
    assert kit.STATE_PATH.exists()
    assert "launch command" in out, f"the guard ran after the wrap: {out}"
    assert "argument" not in out, f"the message names a position they do not have: {out}"
    assert "--in-place" in out, "the way out is still named"
    assert "by hand" not in out, "never the sentence that stopped Bharath"
    assert "edits the config file" in out, (
        "the escape hatch edits their config, and whatever recommends it has to "
        "say so before they run it — true of a warning exactly as it was of the refusal"
    )
    # The honesty constraint. The guard reads SHAPE, so it cannot know this is
    # broken; a header value with a slash trips the same rule. Wording that
    # asserts breakage would send people to `--in-place` on a false alarm, which
    # is the outcome dropping the veto was meant to stop.
    # `_flat`, because both sentences are hard-wrapped and a raw `in` check
    # passes or fails on where the wrap happens to fall — which is not a
    # property of the message. This assertion failed on exactly that first.
    flat = _flat(out).lower()
    assert "may not resolve" in flat, f"the warning overstates what the guard knows:\n{out}"
    assert "your own config was not changed" in flat, (
        "the warning must say whose file is at risk, which is the reason it is survivable at all"
    )


def test_an_ordinary_mcp_remote_entry_is_wrapped_and_not_refused(
    tmp_path, kit_home, project_mode, capsys
):
    """The entry that turned the K8 refusal into a warning, 2026-09-17.

    `npx -y mcp-remote <url> --header "Authorization: Bearer …"` is the most
    common remote-MCP shape there is. A literal bearer token is base64-ish, `/`
    is in that alphabet, so roughly half of them carry one — and the old guard
    read that slash as a relative path and refused the whole entry. The only way
    out it offered was `--in-place`, which edits the config the person came here
    to keep untouched. This thread exists because a prospect refused that step.

    Three things, and the first is the regression:

    - it is WRAPPED, not refused;
    - their own file is byte-identical;
    - the warning still fires, and names the position without the value.

    ⚠ The token DOES still appear once, in the "The entry now reads:" dump, and
    that is `redact_entry`'s documented limit rather than this path's: "there is
    no way to tell which argument is secret, and blanking args would destroy the
    restore recipe these dumps exist to be." It is in SECURITY.md and it
    pre-dates project mode. Asserted here so the two are not confused: a future
    reader seeing the token in setup's output should know which mechanism put it
    there, and that the WARNING is not the one that did."""
    header = "Authorization: Bearer abc/def"
    data = {
        "mcpServers": {
            "srv": {
                "command": "npx",
                "args": ["-y", "mcp-remote", "https://acme.example/sse", "--header", header],
            }
        }
    }
    path = _config(tmp_path, data)
    before = path.read_bytes()

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 0, (
        "an ordinary mcp-remote entry must not be refused"
    )
    out, err = capsys.readouterr()

    assert project_mode.exists(), "the entry was not wrapped"
    assert path.read_bytes() == before, "their own config was modified"
    assert header not in err, f"the token reached stderr:\n{err}"

    warning = out[out.index("One thing to know") :]
    assert "argument 5" in warning, f"the warning does not name the position:\n{warning}"
    assert header not in warning, f"the warning printed the token:\n{warning}"

    # The documented limit, pinned rather than left to surprise. If args ever
    # start being redacted in the dump too, this assertion is the one to delete
    # — deliberately, and with SECURITY.md updated in the same diff.
    dump = out[out.index("The entry now reads:") : out.index("One thing to know")]
    assert header in dump, (
        "the entry dump no longer prints args verbatim. That is an IMPROVEMENT, "
        "but SECURITY.md documents the opposite — update it in this diff"
    )


def test_the_escape_hatch_the_refusal_offers_does_what_its_name_says(
    tmp_path, kit_home, project_mode, capsys
):
    """The chain this rename exists to fix, walked end to end.

    Under the old names it ran: refused for a relative path -> the refusal says
    add `--global` -> they add it, keeping the `--config-file` already on the
    line -> their config is edited. Neither flag's name mentioned writing, and
    the kit itself had recommended the combination. That is this thread's own
    failure reproduced inside the fix for it.

    The write still happens — `--in-place` means edit it where it is, and that
    is a real thing to want. What changed is that the flag causing the write is
    named for the write, and the message says what it will do before they run
    it. So the last assertion here is that their file IS edited: the fix is not
    that the write stopped, it is that nobody arrives at it by surprise.

    ⚠ The first run now SUCCEEDS and warns (2026-09-17). The chain this walks is
    unchanged in the part that matters — the advice is still `--in-place`, and
    taking it still edits their file — so the promise is kept and only the
    entry point moved from a refusal to a warning."""
    path = _config(tmp_path, {"mcpServers": {"srv": {"command": "bin/server"}}})
    before = path.read_bytes()

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 0
    out, _err = capsys.readouterr()
    assert path.read_bytes() == before, "the warned run must still not touch their file"

    # Take the advice exactly as given, keeping the flag already on the line.
    hatch = next(ln for ln in out.splitlines() if "--in-place" in ln)
    assert "edits the config file" in out, f"the advice does not say what it does: {hatch}"

    # The first run wrapped, so clear what it wrote before taking the advice —
    # setup correctly refuses a second wrap over its own leftovers.
    assert kit.main(["uninstall"]) == 0
    capsys.readouterr()

    assert kit.main(["setup", "srv", "--in-place", "--src-config", str(path)]) == 0
    capsys.readouterr()
    assert path.read_bytes() != before, "`--in-place` must do what it says"
    assert not project_mode.exists(), "`--in-place` must not write a project config"


def test_the_read_flag_alone_never_writes_their_file(tmp_path, kit_home, project_mode, capsys):
    """The other half, and the one the old name could not promise. `--src-config`
    reads. It writes nothing, in any mode, and there is no second flag that can
    change that without being typed."""
    path = _config(tmp_path, {"mcpServers": {"srv": {"command": "/abs/server"}}})
    before = path.read_bytes()

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 0
    capsys.readouterr()

    assert path.read_bytes() == before, "`--src-config` wrote the file it was handed"
    assert project_mode.exists(), "the wrap has to have gone somewhere"


def test_project_mode_refuses_a_leftover_file_it_cannot_account_for(
    tmp_path, kit_home, project_mode, capsys
):
    """An `.mcp.json` with no state file is a trial whose state was cleared.

    Refused rather than overwritten: the kit does not silently replace a config
    file, even its own. It is the one deletion `try/CLAUDE.md` can permit, so
    the refusal says the file is ours and in our own checkout — the two facts
    that make deleting it safe to recommend."""
    project_mode.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    path = _config(tmp_path, GLOBAL_ONLY)
    before = path.read_bytes()

    rc = kit.main(["setup", "notion", "--src-config", str(path)])
    err = capsys.readouterr().err

    assert rc == 1
    assert path.read_bytes() == before, "their config must not be touched on this path either"
    assert "deleting it is safe" in err, f"the way out is not stated: {err}"


def test_project_mode_tells_them_where_to_start_the_client(
    tmp_path, kit_home, project_mode, capsys
):
    """A project config loads for its own directory only, so the handover line
    has to name the checkout. Getting this wrong is the 2026-08-28 defect
    `start_where` was written for: the person starts a session somewhere else,
    the wrap never runs, and the file stays empty for a reason they cannot see."""
    _setup_project(tmp_path)
    out = capsys.readouterr().out

    assert str(project_mode.parent) in out, "the second terminal is not pointed at the checkout"
    assert "your own config was read, not changed" in out.lower()
    assert "registered globally" not in out, (
        "a project config does not load wherever you start from, and saying so "
        "is the sentence that makes the trial capture nothing"
    )


def test_the_read_flag_never_writes_and_the_write_flag_says_it_does(
    tmp_path, kit_home, project_mode, capsys
):
    """K9: `--src-config` stays, because it answers a different question.

    `--src-config` says where the entry is READ FROM. The mode says where the
    wrap is WRITTEN. Conflating the two is the reading under which this flag
    looks redundant once project mode exists — and it is also the reading under
    which someone who passes a path precisely because they want it left alone
    gets it edited.

    Two identical configs, the runs differing only by `--in-place`."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    read_only = _config(tmp_path / "a", GLOBAL_ONLY)
    edited = _config(tmp_path / "b", GLOBAL_ONLY)
    before = read_only.read_bytes()

    assert kit.main(["setup", "notion", "--src-config", str(read_only)]) == 0
    # Read BEFORE the state file is cleared. The first version of this test
    # asserted `str(read_only) not in edited.read_text()` after unlinking it,
    # which is true no matter what the kit does — one run's config path would
    # never appear in another run's config file. Deleting source_config_path
    # entirely left it green.
    read_only_state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    kit.STATE_PATH.unlink()
    assert kit.main(["setup", "notion", "--in-place", "--src-config", str(edited)]) == 0
    capsys.readouterr()

    assert read_only.read_bytes() == before, "project mode wrote to the config it was handed"
    assert edited.read_bytes() != before, "--in-place must edit the file it was handed"
    assert read_only_state["source_config_path"] == str(read_only), (
        "the only record that their file was read at all"
    )


THREE_PLAYWRIGHTS = {
    "projects": {
        "/Users/b/work/a": {"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "a"]}}},
        "/Users/b/work/b": {"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "b"]}}},
        "/Users/b/work/c": {"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "c"]}}},
    },
}


def test_a_duplicate_name_is_a_choice_and_never_a_rename(tmp_path, kit_home, capsys):
    """K6, and this is Bharath's config.

    Three servers called `playwright`, one under each of three project keys. The
    kit could not tell them apart and refused, and its advice was to rename one
    BY HAND in `~/.claude.json` — the exact act he had told us, in the same
    conversation, that he would not perform. `--src-config` cannot separate
    entries that live in one file. So the only route the kit offered him was the
    one he had ruled out, and he trialled nothing.

    The refusal now prints the flag that picks each one. What is asserted is
    that it round-trips: the string shown is the string that works, produced by
    the same function on both sides. A selector that prints a value the kit then
    rejects would read as the kit refusing his answer, which is worse than
    having no selector."""
    path = _config(tmp_path, THREE_PLAYWRIGHTS)

    rc = kit.main(["setup", "playwright", "--src-config", str(path)])
    err = capsys.readouterr().err

    assert rc == 1, "the kit still does not choose between them"
    assert "rename" not in err, "the sentence that stopped Bharath is still here"
    assert "by hand" not in err
    # Counted as ROWS, not as occurrences of the string: the closing sentence
    # says "one of the --from lines above", so the substring count is 4 and the
    # first version of this assertion was wrong about what it measured.
    rows = [ln for ln in err.splitlines() if ln.strip().startswith("--from ")]
    assert len(rows) == 3, f"every candidate must print its own selector:\n{err}"

    # Parsed with shlex, the way a shell would. `.split()[0]` was the first
    # version and it is why the suite could not see that the rows were unquoted:
    # the test's parser had the same bug as the code, so a path with a space
    # round-tripped in the test and broke when pasted.
    offered = [shlex.split(ln.split("--from ", 1)[1])[0] for ln in rows]
    assert sorted(offered) == ["/Users/b/work/a", "/Users/b/work/b", "/Users/b/work/c"]

    assert kit.main(["setup", "playwright", "--src-config", str(path), "--from", offered[1]]) == 0
    capsys.readouterr()

    state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    # `source_scope`, not `scope`, since K1b. `scope` says where the WRAP is,
    # and in project mode that is the top level of our own file, so it is None
    # for every `--from`. `source_scope` says which of the three definitions was
    # copied, which is the thing `--from` picked and the thing this test is
    # about. Reading `scope` here compared None to a path and reported it as
    # "a different definition was wrapped" — true of the wrap's location, and
    # nothing to do with the selection.
    assert state["source_scope"] == offered[1], (
        "a different definition was wrapped than the one picked"
    )
    assert state["original_entry"]["args"] == ["-y", "b"], "the picked entry is not the one used"


def test_the_global_definition_can_be_picked_by_name(tmp_path, kit_home, capsys):
    """The top-level block has no path, so it needs a word. `global` is that
    word, and it comes from the same function that prints it."""
    data = {
        "mcpServers": {"srv": {"command": "npx", "args": ["-y", "top"]}},
        "projects": {
            "/Users/b/app": {"mcpServers": {"srv": {"command": "npx", "args": ["-y", "p"]}}}
        },
    }
    path = _config(tmp_path, data)

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 1
    assert "--from global" in capsys.readouterr().err

    assert kit.main(["setup", "srv", "--src-config", str(path), "--from", "global"]) == 0
    capsys.readouterr()
    state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    assert state["scope"] is None
    assert state["original_entry"]["args"] == ["-y", "top"]


def test_a_from_value_that_matches_nothing_says_so(tmp_path, kit_home, capsys):
    """Silently falling back to "wrap something" would be the worst outcome of a
    mistyped path: the person believes they picked one definition and the kit
    wrapped another."""
    path = _config(tmp_path, THREE_PLAYWRIGHTS)

    rc = kit.main(["setup", "playwright", "--src-config", str(path), "--from", "/typo"])
    err = capsys.readouterr().err

    assert rc == 1
    assert not kit.STATE_PATH.exists(), "nothing may be wrapped when the pick matched nothing"
    assert "/typo" in err


def test_a_wrong_from_is_refused_even_when_only_one_server_matches(tmp_path, kit_home, capsys):
    """The dangerous half, and the one the first version could not reach.

    `--from` was filtered only when there was more than one match, so with a
    single candidate it was never read and never checked. The failure is not
    hypothetical: the refusal TEACHES people this flag, they save the command,
    and then they run it against a config where the duplicate has been tidied
    away or on another machine. The kit wraps a definition they did not pick and
    says nothing — which is precisely what the matched-nothing refusal exists to
    prevent, unable to fire in the one case that reaches a person."""
    path = _config(tmp_path, GLOBAL_ONLY)
    before = path.read_bytes()

    rc = kit.main(["setup", "notion", "--src-config", str(path), "--from", "/stale/path"])

    assert rc == 1, "a --from that matches nothing was ignored because there was no duplicate"
    assert "/stale/path" in capsys.readouterr().err
    assert path.read_bytes() == before, "something was wrapped despite the pick not matching"
    assert not kit.STATE_PATH.exists()


def test_a_from_row_with_a_space_in_the_path_can_be_pasted(tmp_path, kit_home, capsys):
    """`/Users/x/Client Work/app` is an ordinary macOS path, and `_cd_to` already
    quotes for exactly this reason. Unquoted, the row the kit prints gives
    `unrecognized arguments: Work/app` when pasted back — so the refusal's whole
    promise, that the printed string is the string that works, fails on the
    machines it was written for.

    Asserted by feeding the printed row through `shlex.split` and handing the
    result straight to the kit, which is what a shell does."""
    data = {
        "projects": {
            "/Users/x/Client Work/app": {"mcpServers": {"srv": {"command": "npx"}}},
            "/Users/x/other": {"mcpServers": {"srv": {"command": "npx"}}},
        }
    }
    path = _config(tmp_path, data)

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 1
    err = capsys.readouterr().err

    row = next(ln for ln in err.splitlines() if "Client Work" in ln)
    picked = shlex.split(row.split("--from ", 1)[1])[0]
    assert picked == "/Users/x/Client Work/app", f"the printed row does not survive a shell: {row}"

    assert kit.main(["setup", "srv", "--src-config", str(path), "--from", picked]) == 0
    capsys.readouterr()
    state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    # `source_scope` since K1b — see the sibling above.
    assert state["source_scope"] == "/Users/x/Client Work/app"


def test_the_approval_step_names_both_prompts(tmp_path, kit_home, project_mode, capsys):
    """There are two gates and the step named one.

    The person is starting Claude Code in a folder they cloned minutes ago, so
    they meet the workspace trust dialog first and the server approval second.
    `claude mcp reset-project-choices` clears only the second — so someone who
    declined the first was being sent to run a command that could not help them.

    `claude mcp list` is read in a directory, and this checklist already carries
    a step for people being in the wrong one, so the folder is named."""
    _setup_project(tmp_path)
    capsys.readouterr()

    assert kit.main(["receipt"]) == 0
    out = capsys.readouterr().out

    # Pinned on the sentence that DESCRIBES the first prompt, not on the word
    # "trust": a later line says "The trust answer is given by...", so looking
    # for the word alone stayed green with the first prompt deleted entirely.
    assert "whether you trust it" in out, "the workspace trust prompt is not described"
    assert "two prompts" in out, "the step does not say there are two"
    assert "SECOND" in out, "which of the two prompts that command clears is not said"
    # Both commands must survive as ONE pasteable line. The first version broke
    # `reset-project-choices` across a line ending, which is how this test caught
    # it: a command a person cannot copy is not an instruction.
    lines = [ln.strip() for ln in out.splitlines()]
    assert "claude mcp reset-project-choices" in lines, (
        f"the command is not on a line of its own: {out}"
    )
    assert any(
        ln.startswith("cd ") and str(project_mode.parent) in ln and "claude mcp list" in ln
        for ln in lines
    ), "`claude mcp list` is cwd-sensitive and the folder to run it in is not given"
    assert "dismissed" not in out, (
        "the docs do not say 'dismissed'; the first version asserted it had been "
        "verified against them line by line"
    )


def test_an_in_place_wrap_of_a_project_mcp_json_still_asks_about_approval(
    tmp_path, kit_home, capsys
):
    """The gate is about the FILE, not about which mode this kit ran in.

    `--in-place --src-config <repo>/.mcp.json` wraps a project-scoped server
    inside a real `.mcp.json`, which Claude Code gates the same way. Keying the
    step on our own mode missed it, and handed someone a four-step checklist
    with the actual cause left out."""
    repo = tmp_path / "repo"
    repo.mkdir()
    their_project_config = repo / ".mcp.json"
    their_project_config.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")

    assert (
        kit.main(["setup", "notion", "--in-place", "--src-config", str(their_project_config)]) == 0
    )
    capsys.readouterr()

    assert kit.main(["receipt"]) == 0
    out = capsys.readouterr().out

    assert "No events" in out, "this test only means something on the empty-file path"
    assert "reset-project-choices" in out, (
        "a project .mcp.json is approval-gated whichever mode this kit used to write it"
    )


def test_receipt_finds_the_wrap_and_asks_about_approval_in_project_mode(
    tmp_path, kit_home, project_mode, capsys
):
    """K5. Two things receipt owes a project-mode trial.

    It has to FIND the wrap — `wrap_still_present` reads the file we wrote, and
    getting that wrong reports THE WRAP IS GONE on a trial that is working.

    And when nothing has been captured, its checklist has to name the one cause
    the person may already have produced by answering a question. A project
    config is not trusted automatically; the prompt can be dismissed, and every
    other step on the list then sends them to look at restarts and directories.
    Verified against the docs on 2026-09-17, including the two commands that let
    them check and undo it."""
    _setup_project(tmp_path)
    capsys.readouterr()

    assert kit.main(["receipt"]) == 0
    out = capsys.readouterr().out

    assert "THE WRAP IS GONE" not in out, "receipt cannot find the wrap it just wrote"
    assert str(project_mode) in out, "the config row does not name the file in use"
    assert "global mcpServers" not in out, "our project file is not the global config"

    assert "approved" in out, "the approval prompt is not on the checklist"
    assert "claude mcp list" in out, "no way given to check whether it is pending"
    assert "reset-project-choices" in out, "no way given to undo a declined prompt"


@pytest.mark.parametrize("data,where", [(GLOBAL_ONLY, None), (PROJECT_SCOPED, "project key")])
def test_the_approval_question_is_not_asked_of_an_entry_in_their_own_config(
    tmp_path, kit_home, monkeypatch, capsys, data, where
):
    """Neither shape inside `~/.claude.json` is approval-gated.

    The docs gate `.mcp.json` FILES. The top level of `~/.claude.json` loads
    everywhere and is never prompted for; a project KEY inside it is their own
    user-level file, not a project config file, and is not prompted for either.
    Asking anyway is the class of defect this checklist was split up to avoid —
    a decisive-sounding step that is simply false for the wrap in front of them.

    Driven through a real `~/.claude.json` rather than `--src-config`, which is
    what the first version of this test got wrong: it pointed `--src-config` at
    a path in a tmp dir and called the result a global wrap, when by the kit's
    own `is_global_config` that file is a project config. The test name said
    global; the fixture was not.

    ⚠ `--in-place` since K1b, and the flag is doing more than keeping a mode
    green. The claim here is about an entry that STAYS in `~/.claude.json`, and
    only `--in-place` leaves it there. The default COPIES it into a `.mcp.json`,
    which is exactly the shape the docs gate — so the answer legitimately flips
    to "yes, approval is asked", and asserting the old answer on the new default
    would pin a sentence that is false in front of a prospect. The project side
    is its own test: `test_receipt_finds_the_wrap_and_asks_about_approval_in_project_mode`
    (:1916), which asserts `reset-project-choices` IS offered. This one is the
    control for the case where their entry never moves."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    (home / ".claude.json").write_text(canonical(data), encoding="utf-8")

    assert kit.main(["setup", "notion", "--in-place"]) == 0
    capsys.readouterr()

    assert kit.main(["receipt"]) == 0
    out = capsys.readouterr().out

    assert "No events" in out, "this test only means something on the empty-file path"
    assert "reset-project-choices" not in out, (
        f"an entry at a {where or 'top level'} of their own config has no project approval to give"
    )


def test_a_vanished_project_file_is_not_blamed_on_their_client(
    tmp_path, kit_home, project_mode, capsys
):
    """THE WRAP IS GONE names a cause, and the cause differs by mode.

    In global mode the usual one is the client itself, which rewrites
    `~/.claude.json` continuously and may put the entry back — so "changed or
    restored" is right there. In project mode the file is ours, in our own
    checkout, and no client maintains it. Saying "restored" would send someone
    to inspect a config that was never part of this."""
    _setup_project(tmp_path)
    kit.EVENTS_PATH.write_text('{"kind": "tool_call_start", "session_id": "s"}\n', encoding="utf-8")
    project_mode.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    capsys.readouterr()

    assert kit.main(["receipt"]) == 0
    out = capsys.readouterr().out

    assert "THE WRAP IS GONE" in out
    assert "this kit's own" in out, "the person is not told whose file changed"
    assert "restored" not in out, (
        "'restored' points at a config this mode never wrote; nothing was restored"
    )


def _setup_project(tmp_path, data=None, server="notion"):
    """A completed project-mode setup, for the uninstall tests below."""
    path = _config(tmp_path, GLOBAL_ONLY if data is None else data)
    assert kit.main(["setup", server, "--src-config", str(path)]) == 0
    return path


def test_uninstall_in_project_mode_deletes_our_file_and_restores_nothing(
    tmp_path, kit_home, project_mode, capsys
):
    """Uninstall is a REMOVAL here, not a restore, and that is the whole
    difference K4 exists for.

    Before this, uninstall read `config_path` — which is our own `.mcp.json` —
    and wrote `original_entry` into it. That "succeeded": the verify compared
    the file against itself and passed, so state.json was deleted and the kit
    reported a restore. What it left was a file the person never had, holding a
    verbatim copy of their server entry including `env`, git-ignored so it never
    appeared in `git status`, absent from the list of things left behind, and
    still registering that server for any session started in the checkout. The
    next `setup` then hit the leftover-file refusal — so every project-mode
    trial ended somewhere the kit refused to work."""
    their_config = _setup_project(tmp_path)
    before = their_config.read_bytes()
    # An events file, so the "Deliberately left in place" section actually
    # prints. Without one it returns early, and the backup assertion below
    # passes because nothing was printed at all rather than because the
    # sentence is right — which is how it first passed.
    kit.EVENTS_PATH.write_text('{"kind": "tool_call"}\n', encoding="utf-8")
    capsys.readouterr()

    assert kit.main(["uninstall"]) == 0
    out = capsys.readouterr().out

    assert "Deliberately left in place" in out, "the section under test did not print"
    assert str(kit.EVENTS_PATH) in out, "the capture is left behind and must be named"

    assert not project_mode.exists(), "the file this kit added is still on their machine"
    assert their_config.read_bytes() == before, "their config was touched on the way out"
    assert not kit.STATE_PATH.exists(), "the trial is over; the state file is cleared"
    assert "nothing to restore" in out.lower(), (
        "a 'restore' here would be a claim about a file of theirs that was never written"
    )
    assert "config-backup" not in out, "project mode writes no backup, so none is left behind"

    # And the kit is usable again, which is the half that was actually broken:
    # the leftover file made the next setup refuse.
    assert kit.main(["setup", "notion", "--src-config", str(their_config)]) == 0


def test_uninstall_after_they_deleted_the_file_themselves_is_not_an_error(
    tmp_path, kit_home, project_mode, capsys
):
    """Setup's leftover refusal tells them deleting this file is safe. Someone
    who does that and then runs uninstall must not be met with `cannot read`
    about the file the kit told them to delete — a dead end the kit walked them
    into itself."""
    _setup_project(tmp_path)
    project_mode.unlink()
    capsys.readouterr()

    assert kit.main(["uninstall"]) == 0
    assert "already gone" in capsys.readouterr().out
    assert not kit.STATE_PATH.exists()


def test_uninstall_keeps_servers_someone_added_to_our_file(
    tmp_path, kit_home, project_mode, capsys
):
    """The file is ours, but not everything in it is. Someone who added a second
    server to it by hand has work in there, and deleting the file whole would
    throw it away — so only our key is removed."""
    _setup_project(tmp_path)
    data = json.loads(project_mode.read_text(encoding="utf-8"))
    data["mcpServers"]["theirs"] = {"command": "node", "args": ["/abs/x.js"]}
    project_mode.write_text(canonical(data), encoding="utf-8")
    capsys.readouterr()

    assert kit.main(["uninstall"]) == 0

    assert project_mode.exists(), "a file holding someone else's server was deleted"
    kept = json.loads(project_mode.read_text(encoding="utf-8"))["mcpServers"]
    assert list(kept) == ["theirs"], "our entry should be the only one removed"


def test_uninstall_refuses_to_delete_an_entry_someone_edited(
    tmp_path, kit_home, project_mode, capsys
):
    """Same principle as `apply_unwrap` refusing an edited entry: the case where
    someone has been in there by hand is the case where guessing is worst. The
    refusal says their own config was never touched, because that is the
    question a person reading it actually has."""
    _setup_project(tmp_path)
    data = json.loads(project_mode.read_text(encoding="utf-8"))
    data["mcpServers"]["notion"]["args"].append("--their-edit")
    project_mode.write_text(canonical(data), encoding="utf-8")
    capsys.readouterr()

    rc = kit.main(["uninstall"])
    err = capsys.readouterr().err

    assert rc == 1
    assert project_mode.exists(), "their edit was deleted"
    assert kit.STATE_PATH.exists(), "state is the only record; it must survive a refusal"
    assert "never changed by this mode" in err


def test_a_state_file_from_before_mode_existed_uninstalls_as_global(
    tmp_path, kit_home, monkeypatch, capsys
):
    """The back-compat guarantee, which was documented and untested.

    `state.get("mode", MODE_GLOBAL)` is the whole mechanism: nothing reads
    STATE_VERSION, so the default on that one `.get` is what carries a trial
    started under an older kit. Someone mid-trial who pulls a newer checkout has
    a state file with no `mode` key and a wrap sitting in their own config.

    Defaulting the other way is the dangerous direction and it is the one no
    test caught: project mode would try to REMOVE an entry from `~/.claude.json`
    treating it as a file the kit had added, and leave the real wrap in place —
    reporting success while the person is still wrapped.

    Driven with DEFAULT_MODE flipped, so the default under test is the `.get`'s
    and not the module's."""
    path = _config(tmp_path, GLOBAL_ONLY)
    before = path.read_bytes()
    assert kit.main(["setup", "notion", "--in-place", "--src-config", str(path)]) == 0

    state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    del state["mode"]
    kit.STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    monkeypatch.setattr(kit, "DEFAULT_MODE", kit.MODE_PROJECT)
    capsys.readouterr()

    assert kit.main(["uninstall"]) == 0
    out = capsys.readouterr().out

    assert path.read_bytes() == before, (
        "a pre-`mode` state file was treated as project mode: the wrap was left in "
        "their config and uninstall reported success"
    )
    assert "Restored" in out, "the global path restores; it does not remove a file"


def test_in_place_still_restores_when_the_default_is_project(
    tmp_path, kit_home, project_mode, capsys
):
    """`--in-place` must keep meaning `--in-place` after the flip.

    The `args.in_place` half of the mode expression had no coverage: with
    DEFAULT_MODE global, no argv could reach project mode, so mutating the line
    to `mode = DEFAULT_MODE` left the whole suite green. After K1b that mutation
    makes `--in-place` write a project config instead — and `--in-place` is the only
    escape the cwd-dependency refusal offers, so it would send someone who was
    correctly refused straight back into the same failure.

    Driven with DEFAULT_MODE already flipped, which is the only arrangement in
    which this can fail."""
    path = _config(tmp_path, GLOBAL_ONLY)
    before = path.read_bytes()

    assert kit.main(["setup", "notion", "--in-place", "--src-config", str(path)]) == 0
    capsys.readouterr()

    assert not project_mode.exists(), "`--in-place` wrote a project config"
    assert path.read_bytes() != before, "`--in-place` must wrap the entry in place"
    state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    assert state["mode"] == kit.MODE_GLOBAL
    assert state["config_path"] == str(path)

    assert kit.main(["uninstall"]) == 0
    assert path.read_bytes() == before, "the global path still restores byte-for-byte"


def test_setup_returns_zero_through_main(tmp_path, kit_home, capsys):
    """The success leg of the 0/1/2 contract, driven the way the agent drives
    it. `cmd_setup` returning 0 is already implied by other tests; that `main`
    hands that 0 back rather than swallowing it is not."""
    path = _config(tmp_path, GLOBAL_ONLY)
    rc = kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "trial-t"])
    assert rc == 0
    assert (kit_home / "state.json").exists()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("cmd", ["receipt", "uninstall"])
def test_receipt_and_uninstall_reject_the_config_file_flag(cmd, tmp_path, kit_home):
    """CLAUDE.md:84-86 tells the agent `--src-config` is setup-only because the
    path is recorded in state.json, so the other two find it themselves. The
    mechanism is that neither subparser declares the flag — so this is argparse
    RAISING SystemExit(2), not a Refuse returning 1. Asserting the code and not
    merely "it failed" is the point: a 1 here would read to the agent as a
    refusal to relay, and a 2 as its own malformed command."""
    with pytest.raises(SystemExit) as exc:
        kit.main([cmd, "--src-config", str(tmp_path / "mcp.json")])
    assert exc.value.code == 2


def test_the_two_setup_routes_now_diverge_and_only_one_touches_their_config(
    tmp_path, kit_home, monkeypatch, capsys
):
    """THE K1b TRIPWIRE, FIRED. This test used to be called
    `test_the_in_place_flag_is_accepted_and_changes_nothing_yet` and asserted
    that both routes landed in the same place, because K3 shipped the flag
    before K1 changed anything. Its own docstring said what would happen:

        "when the default flips to writing a project `.mcp.json`, this test has
        to change, and changing it is the diff saying out loud that
        `--in-place` became the only route to the old behaviour."

    This is that diff. The assertion is inverted rather than deleted: the two
    routes must now DIVERGE, and the divergence is the feature.

    **Still driven through DISCOVERY, not `--src-config`, and that is still the
    whole point.** An earlier version passed `--src-config` on both legs and
    review found it would have survived the flip unchanged: `--src-config` pins
    the write target, so both legs keep editing the file they were handed and
    the tripwire never fires. Reading `state.json` rather than config bytes is
    the other half — `scope` and `config_path` are the artifacts that move."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    their_config = home / ".claude.json"
    their_config.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")

    assert kit.main(["setup", "notion"]) == 0
    plain_state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    plain_config = their_config.read_text(encoding="utf-8")

    # Both legs start from the same untouched config. Without this the second
    # run meets the entry the first one wrapped and refuses it as someone
    # else's wrap, which is a real refusal but not the thing under test.
    kit.STATE_PATH.unlink()
    kit.MCP_PATH.unlink(missing_ok=True)
    their_config.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")

    assert kit.main(["setup", "notion", "--in-place"]) == 0
    flagged_state = json.loads(kit.STATE_PATH.read_text(encoding="utf-8"))
    flagged_config = their_config.read_text(encoding="utf-8")
    capsys.readouterr()

    # The default now writes OUR file and leaves theirs byte-for-byte alone.
    assert plain_state["config_path"] == str(kit.MCP_PATH)
    assert plain_state["source_config_path"] == str(their_config.resolve())
    assert plain_config == canonical(GLOBAL_ONLY), (
        "the default route CHANGED their config; leaving it untouched is the feature"
    )

    # `--in-place` is now the only route to the old behaviour.
    assert flagged_state["config_path"] == str(their_config.resolve())
    assert flagged_state["scope"] is None
    assert flagged_config != canonical(GLOBAL_ONLY), (
        "the --in-place route did not wrap anything; without this the test passes "
        "on a run that did nothing at all"
    )

    # And the two really are different, stated directly rather than inferred
    # from the two halves above.
    assert plain_state["config_path"] != flagged_state["config_path"]


# The help sentence each default owes. Phase C's own note: the pairing was held
# true by a COMMENT ("moves with the default in K1b") doing a test's job, and
# fixing the stale wording in 2f4a244 reddened nothing.
#
# ⚠ Keyed by `kit.MODE_*`, not by the string literals "project"/"global". With
# literals, RENAMING a constant raises KeyError at the lookup below instead of
# failing the assertion this test is about — a green-to-error change that hides
# which promise broke.
_IN_PLACE_HELP_FOR_DEFAULT = {
    kit.MODE_PROJECT: "instead of writing a project config in this checkout",
    kit.MODE_GLOBAL: "(what setup does today)",
}


def test_the_in_place_help_says_what_the_current_default_is(capsys):
    """`--help` must describe the route the person is NOT on, correctly.

    This has been wrong in both directions already. Before K1b the help said
    `--in-place` was "(what setup does today)", which was true; the flip made it
    false and `--help` printed a false statement about a plain `setup` until
    2f4a244 fixed it — and fixing it reddened NOTHING, which is why this exists.

    Keyed off `kit.DEFAULT_MODE`, so flipping the constant without moving the
    sentence is what fails, rather than the sentence being pinned to a literal
    that a future flip would simply be edited to match."""
    expected = _IN_PLACE_HELP_FOR_DEFAULT[kit.DEFAULT_MODE]
    other = next(v for k, v in _IN_PLACE_HELP_FOR_DEFAULT.items() if k != kit.DEFAULT_MODE)

    with pytest.raises(SystemExit) as e:
        kit.main(["setup", "--help"])
    assert e.value.code == 0
    help_text = capsys.readouterr().out
    flat = _flat(help_text)

    assert expected in flat, (
        f"`--in-place`'s help does not describe the {kit.DEFAULT_MODE!r} default:\n{help_text}"
    )
    assert other not in flat, f"`--in-place`'s help still describes the OTHER default:\n{help_text}"


def test_the_in_place_flag_reaches_the_code_under_its_own_name(monkeypatch):
    """`dest="in_place"` is load-bearing and nothing read it.

    `args.global` is a syntax error — `global` is a Python keyword — so the
    `dest` is not style. It is the only way the flag can be read at all.
    Dropping it leaves argparse happy and every other test green while K1's one
    consumer breaks. The comment beside the flag states this; this holds it.

    Asserted on the namespace `main` actually hands the command, because that is
    the contract K1 consumes."""
    seen = {}

    def _capture(args):
        seen["in_place"] = getattr(args, "in_place", "ATTRIBUTE MISSING")
        return 0

    monkeypatch.setattr(kit, "cmd_setup", _capture)

    assert kit.main(["setup", "notion", "--in-place"]) == 0
    assert seen["in_place"] is True, "`--in-place` must arrive as `in_place`"

    assert kit.main(["setup", "notion"]) == 0
    assert seen["in_place"] is False, "and default to False, not to absent"


def test_the_in_place_flag_is_setup_only(tmp_path, kit_home):
    """Same reason `--src-config` is setup-only (see above): `uninstall` and
    `receipt` read the scope out of `state.json` rather than being told it
    again, and a second place to say it is a second place to say it wrongly.
    argparse SystemExit(2), not a Refuse."""
    for cmd in ("receipt", "uninstall"):
        with pytest.raises(SystemExit) as exc:
            kit.main([cmd, "--in-place"])
        assert exc.value.code == 2, f"`{cmd} --in-place` is a usage error, not a refusal"


def test_no_subcommand_is_a_usage_error(kit_home):
    """`add_subparsers(required=True)`. A bare `kit.py` must not print a receipt
    or, worse, do something."""
    with pytest.raises(SystemExit) as exc:
        kit.main([])
    assert exc.value.code == 2


def test_a_refusal_returns_one_and_says_so_only_on_stderr(kit_home, capsys):
    """The refusal leg. `main` catches Refuse, prefixes it with `kit.py <cmd>: `
    and returns 1 — it does not raise, and it does not print to stdout.

    stdout staying empty is load-bearing rather than tidy: CLAUDE.md:63-77 has
    the agent orient by reading `receipt`'s stdout, and a refusal leaking into
    that stream is a refusal an agent can mistake for a report."""
    rc = kit.main(["uninstall"])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert err.startswith("kit.py uninstall: ")
    assert "nothing recorded to reverse" in err


def test_setup_refusal_also_returns_one(tmp_path, kit_home, capsys):
    """The same leg reached through a different command, because `main`'s except
    clause names `args.cmd` — a refusal raised under `setup` must be labelled
    `setup`, not carry whichever command was added to the parser first."""
    path = _config(tmp_path, GLOBAL_ONLY)
    rc = kit.main(["setup", "nosuchserver", "--src-config", str(path)])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert err.startswith("kit.py setup: ")


# ---------------------------------------------------------------------------
# SECURITY.md §9's audit greps.
#
# §9 hands a reviewer four commands and tells them what to expect. Those are
# the most load-bearing sentences in the document — they are the ones a
# skeptical reader runs FIRST, and the whole point of the kit is that its
# claims are mechanical rather than promised. Nothing enforced them, and one of
# the six expected matches is a COMMENT line, so an ordinary reword falsifies a
# published security document with every test still green.
#
# Asserted as a match SET rather than a count: a count survives a deleted call
# site paired with a new one, which is the swap that matters most.
# ---------------------------------------------------------------------------

REPO_ROOT = KIT_PATH.resolve().parent.parent

# SECURITY.md:443, transcribed. The ERE maps to Python's `re` unchanged.
NARROW_AUDIT_RE = r"urlopen\(|Popen\(|subprocess\.run\(|boto3\.client\("
# SECURITY.md:447, the widened form offered to a reviewer who would rather not
# trust our regex.
WIDE_AUDIT_RE = r"urlopen|socket|http\.client|requests\.|boto3|subprocess"

# The five call sites of §4's table, plus the one comment line §9 names. Stored
# as (path, lineno, substring-of-the-line) so a moved line fails loudly instead
# of a bare count quietly absorbing a swap. Nothing under `try/` is in the set,
# and `test_the_kit_contributes_no_audited_call_site` is the assertion of that.
EXPECTED_AUDIT_HITS = {
    ("src/baton_proxy/proxy.py", 1605, "subprocess.Popen("),
    ("src/baton_proxy/transport_http.py", 135, "urllib.request.urlopen(req"),
    ("src/baton_proxy/transport_http.py", 187, "urlopen(timeout=inf) blocks forever"),
    ("src/baton_proxy/sinks.py", 159, "urllib.request.urlopen(req"),
    ("src/baton_proxy/sinks.py", 191, 'boto3.client("s3")'),
    ("src/baton_proxy/scan.py", 510, "subprocess.run(cmd"),
}


# What §9's commands exclude, and what `try/.gitignore` lists: the trial's own
# captured data. events.jsonl holds complete tool arguments and results, so a
# reviewer who wrapped a server that talks about `subprocess.run(` has that text
# sitting inside src|try — and the six-match promise is about OUR CODE, not
# about what their agent happened to say. Pinned against try/.gitignore below.
TRIAL_ARTIFACTS = ("events.jsonl", "state.json", "config-backup.*")


def _audited_files(root: Path = REPO_ROOT):
    """Every file §9's `grep -r src/ try/` would read, in a stable order."""
    for base in ("src", "try"):
        for path in sorted((root / base).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if any(fnmatch(path.name, pat) for pat in TRIAL_ARTIFACTS):
                continue
            yield path


def _grep(pattern: str, root: Path = REPO_ROOT):
    """`grep -rnE <pattern> src/ try/` as (relative_path, lineno, line)."""
    import re

    rx = re.compile(pattern)
    hits = []
    for path in _audited_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # grep skips binaries too
        for n, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append((str(path.relative_to(root)), n, line))
    return hits


def test_security_md_section_9_narrow_grep_returns_exactly_its_six():
    """§9: "Six matches: the five in the §4 table, plus one comment line in
    transport_http.py."

    It was six, went to seven when `kit.py upload` shipped, and is six again now
    that the kit sends nothing. Each time, the count moved in the same commit as
    the code, which is the whole point of pinning it. A reviewer runs the
    printed command and counts; a document that says six over a tree that answers
    seven is the one failure this section cannot survive, because its only claim
    is that its claims are mechanical.

    Note what makes this stable at all: SECURITY.md quotes the regex as
    `urlopen\\(` — escaped — so the document does not match its own grep.
    Unescaping it while editing the doc would add two matches and make the
    sentence wrong, which is exactly the class this pins."""
    hits = _grep(NARROW_AUDIT_RE)
    found = {(p, n, line.strip()) for p, n, line in hits}
    for path, lineno, needle in EXPECTED_AUDIT_HITS:
        assert any(p == path and n == lineno and needle in line for p, n, line in found), (
            f"§9's expected match is gone or moved: {path}:{lineno} ({needle!r})"
        )
    assert len(hits) == 6, (
        "SECURITY.md §9 promises a reviewer SIX matches; this grep now returns "
        f"{len(hits)}:\n" + "\n".join(f"  {p}:{n}: {line.strip()}" for p, n, line in hits)
    )


def test_the_audit_grep_ignores_the_trials_own_captured_data(tmp_path: Path):
    """A reviewer who RAN the kit has try/events.jsonl in the tree it tells them
    to grep, and that file is full of verbatim tool results. One payload quoting
    `subprocess.run(` makes §9's six-match promise read as seven — green in CI,
    red on the machine of the one person who actually used the thing.

    Built in a tmp tree on purpose: writing try/events.jsonl in this checkout
    would overwrite a live trial's captured events."""
    (tmp_path / "src").mkdir()
    (tmp_path / "try").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = subprocess.run(cmd)\n", encoding="utf-8")
    (tmp_path / "try" / "events.jsonl").write_text(
        '{"payload": {"result": "I ran subprocess.run(cmd) for you"}}\n', encoding="utf-8"
    )
    (tmp_path / "try" / "state.json").write_text('{"c": "urlopen("}\n', encoding="utf-8")
    (tmp_path / "try" / "config-backup.20260830T000000Z.json").write_text(
        '{"x": "Popen("}\n', encoding="utf-8"
    )
    assert [h[0] for h in _grep(NARROW_AUDIT_RE, root=tmp_path)] == ["src/real.py"]


def test_section_9_excludes_every_artifact_the_kit_can_leave_behind():
    """The exclusion has to hold in the DOCUMENT, not only in this test — the
    reviewer runs the printed command, not our grep. And the list has to track
    try/.gitignore: anything the kit is allowed to leave behind is something the
    published grep will read on a used checkout."""
    import re

    gitignore = (REPO_ROOT / "try" / ".gitignore").read_text(encoding="utf-8")
    ignored = [
        line.strip() for line in gitignore.splitlines() if line.strip() and not line.startswith("#")
    ]
    assert sorted(ignored) == sorted(TRIAL_ARTIFACTS), (
        "try/.gitignore and the audit exclusion list have drifted"
    )
    security_md = (REPO_ROOT / "try" / "SECURITY.md").read_text(encoding="utf-8")
    section_9 = security_md.split("## 9.")[1]
    grep_lines = [ln for ln in section_9.splitlines() if ln.startswith("grep -")]
    assert len(grep_lines) >= 2, "§9's two audit greps"
    for line in grep_lines[:2]:
        for pattern in TRIAL_ARTIFACTS:
            # A glob is shell-quoted in the document, and must be — unquoted,
            # `config-backup.*` would be expanded by the shell before grep saw it.
            assert re.search(rf"--exclude='?{re.escape(pattern)}'?", line), (
                f"§9's published grep would still read {pattern}:\n  {line}"
            )


def test_the_project_config_the_kit_writes_is_git_ignored():
    """K7. `MCP_PATH` is the checkout root's own `.mcp.json`, and the kit writes
    it in project mode with the person's `env` copied verbatim — so it can hold
    a literal credential. A prospect's clone is a git checkout; an untracked
    file appearing in `git status` after setup is one more thing they did not
    ask for, and one they could commit.

    Anchored with a leading slash, and that is asserted rather than assumed: an
    unanchored `.mcp.json` would also hide one a contributor legitimately adds
    somewhere else in the tree.

    Note this belongs to the ROOT .gitignore, not `try/.gitignore`. That file is
    pinned byte-for-byte against TRIAL_ARTIFACTS, which is the list §9's audit
    grep excludes — and §9 greps `src/ try/`, which this file sits outside of.
    """
    rules = [
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "/.mcp.json" in rules, (
        "the project config setup writes is not ignored; it can carry a literal "
        "token copied out of the person's own config"
    )
    assert ".mcp.json" not in rules, "an unanchored rule would hide a legitimate one elsewhere"

    # The rule as git actually reads it, not as we read it.
    root_file = REPO_ROOT / kit.MCP_PATH.name
    assert kit.MCP_PATH == REPO_ROOT / ".mcp.json", "MCP_PATH moved; this rule no longer covers it"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(root_file)], cwd=REPO_ROOT
    ).returncode
    assert ignored == 0, f"git does not ignore {root_file}"


# The two tests a plain `git clone` cannot run. §9.5's suite prints "2 skipped"
# with no reason attached, and a reviewer reading a security document does not
# get to guess which two. The only skip in the suite is the `event_schema`
# fixture, so this list IS the set of submodule-dependent tests — pinned below
# against the file rather than trusted.
SUBMODULE_SKIPPED_TESTS = (
    "test_emitted_events_conform_to_shared_schema",
    "test_vectors_still_conform_to_the_schema_shipped_alongside_them",
)


def _requested_fixtures(node) -> set[str]:
    """Fixture names a test asks for, by argument OR by `usefixtures`."""
    import ast

    names = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
    for dec in node.decorator_list:
        func = dec.func if isinstance(dec, ast.Call) else None
        if isinstance(func, ast.Attribute) and func.attr == "usefixtures":
            names |= {
                a.value
                for a in dec.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            }
    return names


def _tests_needing_the_submodule() -> set[str]:
    """Every test function that takes the fixture which skips on a plain clone.

    `ast.walk`, not `.body`, and both function nodes, and `usefixtures` as well
    as the argument list: each narrower reading is a way for a third gated test
    to be added and stay invisible here, which is how the "2 skipped" this
    defends goes stale while its own guard is still green."""
    import ast

    src = (REPO_ROOT / "tests" / "test_spec_conformance.py").read_text(encoding="utf-8")
    return {
        node.name
        for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
        and "event_schema" in _requested_fixtures(node)
    }


def _skip_sites() -> dict[str, int]:
    """Every place under `tests/` that can turn a test into a skip.

    Counts `pytest.skip` and the `skip`/`skipif` marks by attribute name, which
    over-detects rather than under-detects — the safe direction for a guard
    whose whole job is to notice a skip nobody told the document about."""
    import ast

    sites: dict[str, int] = {}
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        found = sum(
            isinstance(node, ast.Attribute) and node.attr in ("skip", "skipif")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
        if found:
            sites[path.name] = found
    return sites


def test_the_submodule_fixture_is_the_suites_only_skip():
    """Finding 1. `_tests_needing_the_submodule` reads ONE file, and the "2
    skipped" it defends is a whole-suite count.

    A `pytest.skip` added in any other test file changes that count and leaves
    every assertion below untouched — SECURITY.md:453 would say 2 while the run
    a reviewer does says 3, which is the document being wrong on the one number
    it offers as checkable."""
    assert _skip_sites() == {"test_spec_conformance.py": 1}, (
        f"the suite's skip sites changed; §8 names them and §9.5 counts them: {_skip_sites()}"
    )


def test_section_9_asks_for_the_skip_reasons_it_will_produce():
    """Finding 9. `pytest` reports "2 skipped" and, without `-rs`, no reason.

    The reviewer §9 is written for is one we are not in the room with, running
    the suite to check the claims above it. Two silent skips in the run that is
    supposed to be the proof read as two things unverified — when what they are
    is the wire-format conformance pair, which needs a submodule §8 already says
    the trial does not need."""
    security_md = (REPO_ROOT / "try" / "SECURITY.md").read_text(encoding="utf-8")
    section_9 = security_md.split("## 9.")[1]
    pytest_lines = [ln for ln in section_9.splitlines() if "pytest" in ln and ln.startswith(".")]
    assert len(pytest_lines) == 2, f"§9.5's two pytest invocations: {pytest_lines}"
    for line in pytest_lines:
        assert " -rs" in line, f"§9 runs the suite and hides why it skipped:\n  {line}"


def test_section_8_names_the_two_tests_the_submodule_gates():
    """Finding 9, the other half. `-rs` prints the reason on the day; §8 says it
    in the document, for the reviewer reading before they run anything.

    The names are checked against the suite, not just quoted: §8 naming a test
    that has been renamed is worse than §8 naming none, because a reviewer who
    greps for it and finds nothing has been given a reason to distrust the rest
    of the section."""
    live = _tests_needing_the_submodule()
    assert live == set(SUBMODULE_SKIPPED_TESTS), (
        "the set of tests that skip without the submodule has changed; §8 and "
        f"§9's skip count both describe it: {sorted(live)}"
    )
    section_8 = (REPO_ROOT / "try" / "SECURITY.md").read_text(encoding="utf-8")
    section_8 = _flat(section_8.split("## 8.")[1].split("## 9.")[0])
    # §8 named both tests until the 2026-09-04 rewrite and now states how many
    # there are. That is the same promise one size down and it is still tied to
    # the suite: a third test that needs the submodule leaves §8 saying "two",
    # which is the drift this exists to catch. What it no longer promises is a
    # name a reviewer can grep, which the docstring above says is the weaker of
    # the two failures.
    claim = f"{_COUNT_WORDS[len(SUBMODULE_SKIPPED_TESTS)].lower()} schema-conformance tests skip"
    assert claim in section_8, f"§8 does not account for the tests it makes skip: {claim!r}"


def test_one_of_the_six_is_a_comment_not_a_call_site():
    """§9 distinguishes "the five in the §4 table" from "one comment line". A
    reviewer counting call sites and getting six would conclude the table is
    incomplete — so the comment is part of the claim, not noise around it."""
    hits = _grep(NARROW_AUDIT_RE)
    comments = [(p, n) for p, n, line in hits if line.strip().startswith("#")]
    assert comments == [("src/baton_proxy/transport_http.py", 187)]


def test_the_kit_contributes_no_audited_call_site():
    """§4: "All five are the proxy's; the kit contributes none." True, then made
    false by `kit.py upload`, and true again in 0.6.0. The value of the sentence
    is that it moved with the code every time.

    The assertion is the whole of the kit's egress claim, so it is made over the
    directory rather than over a file list: a network call ANYWHERE under `try/`,
    in `kit.py` or in a helper someone adds beside it, is the regression this
    catches. CLAUDE.md's "there is no command that sends" and §1's "nothing in
    the kit sends" are both this grep, written out in prose."""
    kit_hits = [h for h in _grep(NARROW_AUDIT_RE) if h[0].startswith("try/")]
    assert kit_hits == [], "the kit gained a network- or process-capable call site: " + repr(
        [(p, n, line.strip()) for p, n, line in kit_hits]
    )
    # §3a states the same thing one level earlier, as something a reviewer can
    # read off the import block without trusting a grep: the one `urllib` the
    # kit imports parses strings. `urllib.request` here would be a send path two
    # lines from existing, with no call site yet for §9's grep to find.
    imports = [
        line
        for line in KIT_PATH.read_text(encoding="utf-8").splitlines()
        if re.match(r"^(import|from)\s", line)
    ]
    assert [line for line in imports if "urllib" in line] == ["import urllib.parse"], (
        f"§3a says the kit's only urllib import is urllib.parse: {imports}"
    )


def test_the_widened_grep_introduces_no_new_call_site():
    """§9's second command exists for a reviewer who would rather not trust our
    regex: "this catches every mention, imports and prose included, and there
    are no other call sites."

    So the widened set may grow freely with prose and imports — but any line in
    it that CALLS something network- or process-capable must already be one of
    the six. This is the assertion that catches a `requests.post(` or a
    `socket.socket(` the narrow regex was never written to see."""
    import re

    call_rx = re.compile(
        r"(urlopen|Popen|subprocess\.run|boto3\.client|requests\.\w+|http\.client\.\w+|socket\.socket)\s*\("
    )
    narrow = {(p, n) for p, n, _ in _grep(NARROW_AUDIT_RE)}
    strays = [
        (p, n, line.strip())
        for p, n, line in _grep(WIDE_AUDIT_RE)
        if p.endswith(".py") and call_rx.search(line) and (p, n) not in narrow
    ]
    assert not strays, "call site the narrow §9 grep cannot see: " + repr(strays)


def test_the_dependency_list_is_still_empty():
    """§9 step 2: "The dependency list — expect it to be empty." A reviewer runs
    `grep -n dependencies pyproject.toml` and reads the answer off the line, so
    the claim is about the shipped install, not about `[dev]`."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == []


# ---------------------------------------------------------------------------
# The documented commands parse.
#
# `try/CLAUDE.md` is not documentation the agent might read — it is auto-loaded
# when someone runs `claude` from `try/`, so every invocation in it is a command
# the agent WILL run, verbatim, on a stranger's machine. A flag rename that
# leaves the doc behind teaches a command that exits 2 on first use, and the
# agent has been told (CLAUDE.md:31) not to retry with different flags.
#
# Parse-only. Nothing here executes; `parse_args` is the whole assertion.
# ---------------------------------------------------------------------------

# Placeholders the docs use for a value the person supplies. Substituted rather
# than skipped, because dropping the argument would test a different command
# than the one on the page.
_DOC_PLACEHOLDERS = {
    "<server-name>": "notion",
    "<name>": "notion",
    # `--src-config <path>`, wherever a doc writes it after the command. One
    # token for exactly this reason: `<path to the config>` would shlex-split
    # into four, and the extra three would reach argparse as positionals.
    "<path>": "/tmp/mcp.json",
}


def _documented_kit_commands():
    """Every `python3 kit.py …` invocation in the two docs, as argv."""
    import re
    import shlex

    # Stop at a `#` comment (CLAUDE.md's cheat-sheet annotates each line) or at
    # the closing backtick of an inline-code span.
    rx = re.compile(r"python3 kit\.py ([^`\n#]*)")
    for doc in ("CLAUDE.md", "SECURITY.md"):
        path = REPO_ROOT / "try" / doc
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for m in rx.finditer(line):
                tail = m.group(1).strip().rstrip(".")
                argv = [_DOC_PLACEHOLDERS.get(tok, tok) for tok in shlex.split(tail)]
                yield f"try/{doc}:{n}", argv


def test_the_docs_document_commands_that_actually_exist():
    """Guard against the guard: if the extractor silently matches nothing, this
    file passes while checking zero commands — the green-by-emptiness failure."""
    found = list(_documented_kit_commands())
    assert len(found) >= 6, f"extractor found only {len(found)}; the docs have more"
    assert {tuple(argv[:1]) for _, argv in found} == {
        ("setup",),
        ("receipt",),
        ("uninstall",),
    }, "the docs document a command the kit does not have, or stopped documenting one"


def test_every_kit_command_in_the_docs_parses(monkeypatch, capsys):
    """Driven through `main` with the three handlers stubbed, so the parse is
    real and nothing runs.

    Through `main` rather than a parser built here, because kit.py builds its
    parser inline and the alternative was to refactor shipped, security-reviewed
    code for testability. The stub buys a second assertion for free: not just
    that argparse accepts the argv, but that it dispatches to the handler the
    document's reader would expect."""
    dispatched: list[str] = []
    for name in ("cmd_setup", "cmd_receipt", "cmd_uninstall"):
        monkeypatch.setattr(kit, name, lambda _args, _n=name: dispatched.append(_n) or 0)

    for where, argv in _documented_kit_commands():
        dispatched.clear()
        try:
            rc = kit.main(argv)
        except SystemExit as e:  # pragma: no cover - only on a real regression
            pytest.fail(
                f"{where} documents a command argparse rejects "
                f"(exit {e.code}): python3 kit.py {' '.join(argv)}\n"
                f"{capsys.readouterr().err}"
            )
        assert rc == 0
        assert dispatched == [f"cmd_{argv[0]}"], f"{where}: {argv} reached {dispatched}"


# ---------------------------------------------------------------------------
# TK-F-8 — the composed stdio path: setup writes an entry, a client launches
# that entry, events land.
#
# Everything above this line tests the kit's config surgery against a config
# object. That is the shape of the wrap, and the shape has never been the
# failure mode. The failure mode is that the shape is right, every test above
# is green, the prospect uses their server for five days, and `receipt` reports
# zero — discovered at the END of a trial rather than the start. Nothing in
# this repo composed the three parties (kit writes → client launches → proxy
# emits) until here.
#
# "Exactly as the config says" is the load-bearing phrase: the entry is read
# BACK OFF DISK and its `command`/`args`/`env` are used verbatim, so a wrap
# that only works when a test helpfully supplies a missing PYTHONPATH fails
# here the way it would fail on a stranger's machine.
# ---------------------------------------------------------------------------

TESTS_DIR = Path(__file__).resolve().parent
STDIO_FIXTURE = TESTS_DIR / "fixture_server.py"

# One minimal but complete MCP session. `tools/list` is not decoration: the
# surface snapshot is emitted off the handshake+list, so a run without it
# cannot distinguish "the proxy captured nothing" from "we never asked".
_SESSION = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "tk-f-8-client", "version": "0.1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hello from a wrapped server"}},
    },
]

TK_F_8_TENANT = "trial-tkf8"


_KIT_HOME_GLOBALS = ("TRY_DIR", "STATE_PATH", "EVENTS_PATH", "MCP_PATH")


def _kit_home_at(home: Path):
    """Save/restore the module globals `kit_home` monkeypatches.

    A plain fixture cannot be used here: the composed run is module-scoped (one
    subprocess for several assertions) and `monkeypatch` is function-scoped.
    SRC_DIR stays real deliberately — the wrap must point at the actual proxy
    source, which is the whole thing under test.

    ⚠ MCP_PATH joined this list at K1b, and it is why the names are a tuple
    rather than three assignments. Under the old default these fixtures never
    reached the project path, so the omission cost nothing; after the flip
    their `setup` call wrote a live `.mcp.json` into the real checkout. The
    function-scoped guard could not see it — a module fixture runs before any
    function fixture, so the file already existed by the time the guard looked,
    and the leak read as run-to-run flakiness in the counts.
    """
    # mkdir FIRST. Callers do `saved = _kit_home_at(home)` outside their `try`,
    # so anything that raises after the assignments leaves all four globals
    # pointed at a tmp dir for the rest of the session with no restore to
    # reach. Creating the directory before the first assignment keeps the
    # mutating half of this function free of failure points.
    (home / "checkout").mkdir(exist_ok=True)
    saved = tuple(getattr(kit, n) for n in _KIT_HOME_GLOBALS)
    kit.TRY_DIR = home
    kit.STATE_PATH = home / "state.json"
    kit.EVENTS_PATH = home / "events.jsonl"
    kit.MCP_PATH = home / "checkout" / ".mcp.json"
    return saved


def _restore_kit_home(saved) -> None:
    """The other half of `_kit_home_at`, as a function so the two cannot drift.

    Unpacking the tuple by hand at each call site is what let MCP_PATH be added
    to one end and not the other; with the names in one list an arity mismatch
    is impossible instead of merely unlikely."""
    for name, value in zip(_KIT_HOME_GLOBALS, saved, strict=True):
        setattr(kit, name, value)


def _wrapped_entry(name: str) -> dict:
    """Read the entry `setup` just wrote, out of the file it actually wrote to.

    ⚠ Both composed-run fixtures used to read this back out of the SOURCE config
    they had handed to `--src-config`. Under the old global default that file
    WAS the one setup rewrote, so the read worked by coincidence rather than by
    rule. Project mode copies the entry into `kit.MCP_PATH` and leaves the
    source alone, so the same line returned the ORIGINAL, unwrapped entry: the
    bridge raised `KeyError: 'command'` on a remote entry that has no command,
    and the stdio run launched the fixture directly with no proxy in its path —
    which reported itself as "nothing captured the tool surface", a capture
    failure blamed on the wrap rather than on the fixture.

    Read the file directly rather than through `kit.read_state` and `entry_at`.
    These are the tests that prove the wrap launches and observes, so the read
    that feeds them must not run through the kit code they are grading.
    """
    data = json.loads(kit.MCP_PATH.read_text(encoding="utf-8"))
    return data["mcpServers"][name]


def _drive(entry: dict, messages: list[dict], *, timeout: int = 20) -> tuple[str, str]:
    """Launch `entry` the way an MCP client does and drive one session.

    The env is the parent environment stripped of `BATON_*` (so a developer's
    own exports cannot make a broken wrap look healthy) with the ENTRY's env
    merged on top — which is what a client actually does. Passing only the
    entry's env would be a different, easier test: the wrapped command is a
    Python interpreter, and stripping the inherited environment changes how it
    resolves its own installation."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})
    proc = subprocess.Popen(
        [entry["command"], *[str(a) for a in entry.get("args") or []]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    payload = "".join(json.dumps(m) + "\n" for m in messages)
    try:
        out, err = proc.communicate(input=payload, timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
        proc.kill()
        out, err = proc.communicate()
        pytest.fail(
            f"the wrapped entry did not exit within {timeout}s of the client\n"
            f"closing stdin\nstdout:\n{out}\nstderr:\n{err}"
        )
    return out, err, proc.returncode


@pytest.fixture(scope="module")
def stdio_run(tmp_path_factory):
    """setup → read the entry back off disk → launch it → collect what landed."""
    home = tmp_path_factory.mktemp("kit-home")
    saved = _kit_home_at(home)
    try:
        config_path = home / "mcp.json"
        config_path.write_text(
            canonical(
                {
                    "mcpServers": {
                        "fixture": {
                            "command": sys.executable,
                            "args": [str(STDIO_FIXTURE)],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        rc = kit.main(
            [
                "setup",
                "fixture",
                "--src-config",
                str(config_path),
                "--tenant",
                TK_F_8_TENANT,
                "--vendor",
                "toybox",
            ]
        )
        assert rc == 0
        entry = _wrapped_entry("fixture")
        stdout, stderr, returncode = _drive(entry, _SESSION)
        # `communicate()` returns after the proxy exits, which joins its drain
        # thread — every queued event is on disk by now, so no sleep.
        return {
            "entry": entry,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
            "events": kit.read_events(home / "events.jsonl"),
            "home": home,
        }
    finally:
        _restore_kit_home(saved)


def _replies(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


@pytest.mark.integration
def test_the_session_actually_completed_through_the_wrapped_entry(stdio_run):
    """The guard against green-by-emptiness in the other direction: if the
    wrapped server never answered, "no events" would be a launch failure
    reported as a capture failure, and every diagnosis below would be aimed at
    the wrong half."""
    replies = _replies(stdio_run["stdout"])
    ids = {r.get("id") for r in replies}
    assert {1, 2, 3} <= ids, (
        f"the client did not get answers to initialize/tools/list/tools/call: {ids}\n"
        f"stderr:\n{stdio_run['stderr']}"
    )
    echo = next(r for r in replies if r.get("id") == 3)
    assert "error" not in echo, echo


@pytest.mark.integration
def test_the_upstream_surface_lands(stdio_run):
    """`surface_snapshot` is what proves the handshake completed THROUGH the
    proxy rather than around it. Its absence is the signature of a wrap that
    launches and forwards but never observes."""
    snapshots = [e for e in stdio_run["events"] if e.get("event_type") == "surface_snapshot"]
    assert snapshots, (
        "nothing captured the tool surface — the proxy was not in the path\n"
        f"stderr:\n{stdio_run['stderr']}"
    )
    names = [t.get("name") for t in snapshots[0]["payload"]["tools"]]
    assert "echo" in names, names
    # The double-wrap check, same reasoning as the bridge grader: a snapshot is
    # what the UPSTREAM served, so a `baton_` name here means a second proxy is
    # nested inside the first.
    assert not [n for n in names if str(n).startswith("baton_")], names


@pytest.mark.integration
def test_the_call_lands_as_a_matched_start_end_pair(stdio_run):
    """One `tools/call` in, one start and one end out, same session, same tool.

    A start with no end is the shape of a proxy that observes the request and
    loses the response — which `receipt` would report as a healthy call count
    while the durations and results were never captured."""
    events = stdio_run["events"]
    starts = [
        e
        for e in events
        if e.get("event_type") == "tool_call_start" and e["payload"]["tool_name"] == "echo"
    ]
    ends = [
        e
        for e in events
        if e.get("event_type") == "tool_call_end" and e["payload"]["tool_name"] == "echo"
    ]
    assert len(starts) == 1, f"expected 1 tool_call_start for echo, got {len(starts)}"
    assert len(ends) == 1, f"expected 1 tool_call_end for echo, got {len(ends)}"
    assert starts[0]["session_id"] == ends[0]["session_id"] is not None
    assert starts[0]["sequence_number"] < ends[0]["sequence_number"]


@pytest.mark.integration
def test_every_landed_event_carries_the_tenant_setup_was_given(stdio_run):
    """`--tenant` is how a trial's file is ours rather than everyone's. The
    default is the sentinel `local`, and a wrap that silently kept it puts
    every trial on earth in one merged bucket — a capture that looks complete
    and is unattributable."""
    events = stdio_run["events"]
    assert events
    assert {e.get("tenant_id") for e in events} == {TK_F_8_TENANT}
    assert {e.get("vendor_id") for e in events} == {"toybox"}


@pytest.mark.integration
def test_the_wrapped_entry_exits_when_the_client_disconnects(stdio_run):
    """Closing stdin is how an MCP client shuts a stdio server down; the server
    is expected to exit, and a client that has to escalate to SIGTERM does so
    after a grace period it chooses.

    This was RED when the assertion was first written: `_pump_client_to_server`
    returned on stdin EOF without closing the UPSTREAM's stdin, so the upstream
    sat healthy on a pipe nobody would write to again and `child.wait()` never
    returned. The damage is not a stray process. `run_proxy` hangs its entire
    shutdown off that wait — `drain_pending`, which gives every in-flight
    `*_start` a matching end, and `emitter.stop()`, which flushes the queue —
    so the client's SIGTERM arrived with the last events still unwritten. A
    trial's final call being the one that never lands is not a shape any test
    above this line could see, because they all end at a config object."""
    assert stdio_run["returncode"] == 0, stdio_run["stderr"]


# ---------------------------------------------------------------------------
# TK-F-9 — the composed bridge path.
#
# Same shape as TK-F-8 one transport over: the kit rewrites a remote entry into
# the proxy's `--url` bridge, moving the bearer out of a `headers` slot and into
# `BATON_UPSTREAM_AUTH_TOKEN`. Four parties now, not three — kit writes, client
# launches, bearer travels, events land — and until this test the composed path
# was first exercised on a prospect's machine.
#
# The graders are lifted from `baton-internal/spikes/http_entry_wrap/
# check_kit_bridge.py:29-34,:54-55`, WITH their reasoning, because they were
# spending an LLM to grade something a scripted client can grade for free. The
# design note's residue for the agent tier is narrow and none of it is here:
# `${VAR}` expansion is a CLIENT behaviour, and a session's MCP server set
# binding at client startup is what makes the restart step load-bearing.
#
# Both credential shapes run. The literal arm has no stand-in anywhere: the
# token in the config IS the token on the wire, so every assertion grades the
# product. The reference arm needs the client's half played by this test, which
# is stated on the assertion rather than hidden — it is the shape every remote
# MCP config actually uses, so a chain that only works for literals is a chain
# that works for nobody.
# ---------------------------------------------------------------------------

sys.path.insert(0, str(TESTS_DIR))
import fixture_http_server  # noqa: E402

# Contains the word "bearer" ON PURPOSE, lifted from the spike's sentinel. It is
# what makes the token-split check below a real check: a grader that counted
# occurrences of "bearer" in the header would be grading the fixture's own
# choice of string instead of the wire.
TK_F_9_SENTINEL = "sentinel-bearer-4f2a91"
TK_F_9_TENANT = "trial-tkf9"

# The two wrappable remote shapes. `literal` is graded end to end with nothing
# standing in; `reference` needs this test to expand `${TK_F_9_TOKEN}` the way
# an MCP client does before launch.
_CREDENTIAL_SHAPES = ["literal", "reference"]


@pytest.fixture(scope="module", params=_CREDENTIAL_SHAPES)
def bridge_run(request, tmp_path_factory):
    """setup on a remote entry → launch the bridge → collect both sides."""
    shape = request.param
    httpd = fixture_http_server.serve(0, require_auth=TK_F_9_SENTINEL)
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}/mcp"
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    home = tmp_path_factory.mktemp(f"kit-home-bridge-{shape}")
    saved = _kit_home_at(home)
    try:
        header = f"Bearer {TK_F_9_SENTINEL}" if shape == "literal" else "Bearer ${TK_F_9_TOKEN}"
        config_path = home / "mcp.json"
        config_path.write_text(
            canonical(
                {
                    "mcpServers": {
                        "remote": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": header},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        rc = kit.main(
            [
                "setup",
                "remote",
                "--src-config",
                str(config_path),
                "--tenant",
                TK_F_9_TENANT,
                "--vendor",
                "toybox-remote",
            ]
        )
        assert rc == 0
        entry = _wrapped_entry("remote")

        launch = dict(entry)
        if shape == "reference":
            # The client's half, played here and nowhere else. A real MCP client
            # expands `${VAR}` in an entry's env against its own environment at
            # launch; the kit deliberately never resolves it, which is what
            # keeps "we never see a credential" true for the remote class. That
            # ONE substitution is the only stand-in in this test.
            launch["env"] = {
                k: (TK_F_9_SENTINEL if v == "${TK_F_9_TOKEN}" else v)
                for k, v in entry["env"].items()
            }

        stdout, stderr, returncode = _drive(launch, _SESSION)
        return {
            "shape": shape,
            "entry": entry,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": returncode,
            "events": kit.read_events(home / "events.jsonl"),
            "authorizations": list(httpd.authorizations),
        }
    finally:
        _restore_kit_home(saved)
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.integration
def test_the_bridge_session_completed_and_the_process_exited(bridge_run):
    """Green-by-emptiness guard plus the shutdown property, which the stdio path
    has already had to pay for once.

    `run_http_proxy` is a SEPARATE shutdown implementation from `run_proxy` — a
    main-thread stdin loop with its own drain-and-stop in a `finally` — so the
    stdio fix does not cover it and its clean exit cannot be assumed from the
    other test passing."""
    replies = _replies(bridge_run["stdout"])
    assert {1, 2, 3} <= {r.get("id") for r in replies}, bridge_run["stderr"]
    # Ids alone do not mean the session worked: a JSON-RPC *error* reply carries
    # the id of the request it refused, so a run whose every upstream POST was
    # rejected 401 satisfies the check above. That is the exact state this guard
    # exists to catch, so read the echo reply itself, as the stdio sibling does.
    echo = next(r for r in replies if r.get("id") == 3)
    assert "error" not in echo, echo
    assert bridge_run["returncode"] == 0, bridge_run["stderr"]


@pytest.mark.integration
def test_the_bearer_arrived_at_the_upstream(bridge_run):
    """No header at all is the signature of a bridge that dropped the credential
    on the way — which presents to the prospect as their own remote server
    refusing them, days after setup said everything was fine."""
    assert bridge_run["authorizations"], (
        "no Authorization header ever arrived at the upstream\n" + bridge_run["stderr"]
    )


@pytest.mark.integration
def test_every_authorization_value_is_exactly_two_parts(bridge_run):
    """Token-wise, NOT substring — lifted verbatim from the spike's grader with
    its reason: the sentinel itself contains the word "bearer", so counting
    occurrences grades the fixture instead of the wire.

    Two parts is also what catches the doubled prefix. The kit strips `Bearer `
    when it moves the value into `BATON_UPSTREAM_AUTH_TOKEN` precisely because
    the bridge re-adds it; a regression on either side yields
    `Bearer Bearer <tok>`, which an upstream rejects with a 401 that names
    nothing."""
    values = sorted(set(bridge_run["authorizations"]))
    # Not decoration: a loop over an empty list passes, so under a mutation that
    # drops the header entirely this grader went green while the run had failed
    # completely. Found by mutating, which is the only way that shape is ever
    # found.
    assert values, "nothing to grade — no Authorization header arrived at all"
    for value in values:
        parts = value.split()
        assert len(parts) == 2 and parts[0].lower() == "bearer", (
            f"malformed Authorization: {value!r} (want exactly `Bearer <token>`)"
        )


@pytest.mark.integration
def test_no_unexpanded_variable_reference_reached_the_wire(bridge_run):
    """A literal `${` upstream means the reference was never resolved by anyone.

    On the `literal` arm this grades the product outright: the kit must move the
    value across without mangling it into something reference-shaped. On the
    `reference` arm the expansion is this test's, so what it grades is narrower
    and still worth having — that the kit wrote the reference somewhere a client
    CAN expand, rather than into a slot the client never looks at."""
    values = sorted(set(bridge_run["authorizations"]))
    assert values, "nothing to grade — no Authorization header arrived at all"
    for value in values:
        assert "${" not in value, (
            f"a literal ${{VAR}} reached the wire — nothing expanded it: {value!r}"
        )


@pytest.mark.integration
def test_the_token_on_the_wire_is_the_one_from_the_config(bridge_run):
    """The value, not just the shape. A bridge that sent a well-formed bearer
    carrying the wrong token would pass every assertion above it."""
    tokens = {v.split()[1] for v in bridge_run["authorizations"] if len(v.split()) == 2}
    assert tokens == {TK_F_9_SENTINEL}, tokens


@pytest.mark.integration
def test_the_remote_surface_lands_and_no_proxy_is_nested(bridge_run):
    """`surface_snapshot` records what the UPSTREAM served. Its absence means the
    handshake did not complete through the proxy; a `baton_`-prefixed name in it
    means two proxies are nested, because the outer one would be seeing the
    inner one's injected tools as if they were the vendor's. Both graders come
    from the spike, where the second cost an LLM call to reach."""
    snapshots = [e for e in bridge_run["events"] if e.get("event_type") == "surface_snapshot"]
    assert snapshots, (
        "no surface_snapshot — the handshake did not complete through the proxy\n"
        + bridge_run["stderr"]
    )
    names = [t.get("name") for t in snapshots[0]["payload"]["tools"]]
    assert "echo" in names, names
    assert not [n for n in names if str(n).startswith("baton_")], (
        f"upstream served a baton_ tool — two proxies are nested: {names}"
    )


@pytest.mark.integration
def test_the_bridged_call_lands_with_the_tenant_label(bridge_run):
    """The same end-of-chain assertion as the stdio path: a matched pair, on the
    tenant setup was given. `--tenant` is what makes a trial's file ours rather
    than everyone's."""
    events = bridge_run["events"]
    starts = [
        e
        for e in events
        if e.get("event_type") == "tool_call_start" and e["payload"]["tool_name"] == "echo"
    ]
    ends = [
        e
        for e in events
        if e.get("event_type") == "tool_call_end" and e["payload"]["tool_name"] == "echo"
    ]
    assert len(starts) == 1, f"expected 1 tool_call_start for echo, got {len(starts)}"
    assert len(ends) == 1, f"expected 1 tool_call_end for echo, got {len(ends)}"
    assert events
    assert {e.get("tenant_id") for e in events} == {TK_F_9_TENANT}


@pytest.mark.integration
def test_the_kit_never_wrote_the_credential_into_the_config(bridge_run):
    """The composed proof of the promise the unit tests make about the wrap: on
    the reference arm the entry on disk must still hold `${TK_F_9_TOKEN}` and no
    resolved value, because the kit resolves nothing. Asserted here as well as
    upstairs because this is the entry that was actually LAUNCHED — a
    transformation that only holds for a config object nobody runs is not the
    promise."""
    on_disk = json.dumps(bridge_run["entry"])
    if bridge_run["shape"] == "reference":
        assert "${TK_F_9_TOKEN}" in on_disk
        assert TK_F_9_SENTINEL not in on_disk
    else:
        # The literal was already in the config the user wrote; the kit moved it
        # between slots and must not have copied it into a second one.
        assert on_disk.count(TK_F_9_SENTINEL) == 1


# ---------------------------------------------------------------------------
# TK-F-3/4/5/7 — the surfaces an agent reads, and the refusals it must relay.
#
# `try/CLAUDE.md` is auto-loaded when someone runs `claude` from `try/`, so it
# is not documentation the agent might consult — it is a script the agent WILL
# follow, on a stranger's machine, with nobody in the room. It branches on
# strings this kit prints. Nothing held the two together.
#
# Every test here drives `main()`, because the agent drives `main()`.
# ---------------------------------------------------------------------------

# Five entries, one per class the candidate list has to describe. Two are
# wrappable, one is already ours, two are refused for different reasons — and
# the two refusals must not be collapsed, because the list is how a prospect's
# own run reports which classes their config holds.
FIVE_ENTRIES = {
    "mcpServers": {
        "alpha": {
            "command": "npx",
            "args": ["-y", "alpha-mcp"],
            "env": {"ALPHA_TOKEN": "sk-live-ALPHA-LITERAL-9f2b"},
        },
        "bravo": {
            "command": "/usr/bin/python3",
            "args": ["-m", "baton_proxy", "--", "node", "bravo.js"],
            "env": {},
        },
        "charlie": {
            "type": "http",
            "url": "https://charlie.example.com/mcp",
            "headers": {"Authorization": "Bearer ${CHARLIE_TOKEN}"},
        },
        "delta": {
            "type": "http",
            "url": "https://delta.example.com/mcp",
            "headers": {
                "Authorization": "Bearer sk-live-DELTA-LITERAL-4c81",
                "X-Delta-Workspace": "acme-prod",
            },
        },
        "echo_srv": {"type": "sse", "url": "https://echo.example.com/sse"},
    }
}

_FIVE_LITERALS = ("sk-live-ALPHA-LITERAL-9f2b", "sk-live-DELTA-LITERAL-4c81")


def _setup_listing(tmp_path, capsys) -> str:
    """`setup` with no server name — the refusal that carries the candidate list."""
    path = _config(tmp_path, FIVE_ENTRIES)
    rc = kit.main(["setup", "--src-config", str(path)])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    return err


def test_the_candidate_list_names_every_entry_in_the_config(tmp_path, kit_home, capsys):
    """TK-F-3. All five, not just the wrappable two.

    An entry that appears in neither list is one the person can see in their own
    config and cannot find in our output, which reads as the kit not having
    looked. `CLAUDE.md` tells the agent to show this list and ask which one they
    want — a name missing from it cannot be chosen."""
    err = _setup_listing(tmp_path, capsys)
    for name in FIVE_ENTRIES["mcpServers"]:
        assert name in err, f"`{name}` is in the config and not in the list:\n{err}"


def test_every_refusal_in_the_list_carries_its_own_reason(tmp_path, kit_home, capsys):
    """TK-F-3. A reason exists in the core and must REACH the surface.

    `CLAUDE.md:80-89` forbids exactly one thing: telling the person a server
    cannot be wrapped without saying why. The two refused entries fail for
    different reasons and the difference is the useful part — `delta` is one
    header away from wrappable, `echo_srv` is a transport we do not speak."""
    err = _setup_listing(tmp_path, capsys)
    assert "sse transport" in err
    assert "X-Delta-Workspace" in err, "the extra header is why delta is refused; name it"
    # And the already-ours entry gets its own line rather than vanishing from
    # both lists, which would tell someone their only server does not exist.
    assert "bravo" in err and "Already baton-proxy" in err


def test_the_candidate_list_prints_no_credential(tmp_path, kit_home, capsys):
    """TK-F-3. The list is shown to someone who may paste it back to us.

    Both literals are in the config and neither may be in the output: `delta`'s
    is a header value on a REFUSED entry, which is the path that has to describe
    an entry it is not wrapping, and `alpha`'s is an env value on an offered
    one."""
    err = _setup_listing(tmp_path, capsys)
    for literal in _FIVE_LITERALS:
        assert literal not in err, f"{literal!r} reached the surface"


def test_a_var_reference_is_still_described_as_one(tmp_path, kit_home, capsys):
    """TK-F-3, the other direction. `charlie` is wrappable and offered, so the
    reference never needs printing — but nothing may claim it is a literal
    either. This pins that the two remote entries are told apart at all."""
    err = _setup_listing(tmp_path, capsys)
    # Split the refusal into the half that OFFERS and the half that REFUSES.
    # Asserting `"charlie" in err` alone proves nothing: a `charlie` demoted to
    # unwrappable is still printed, just under the other header, and its
    # reference is absent either way because a reason never quotes a header
    # value. Only the section a name lands in distinguishes the two classes.
    assert "Already baton-proxy" in err and "Not wrappable" in err, err
    offered = err.split("\n\n  Already baton-proxy")[0]
    refused = err.split("\n\n  Not wrappable")[1]
    assert "charlie" in offered, f"the ${{VAR}}-bearer remote is not offered:\n{err}"
    for name in ("delta", "echo_srv"):
        assert name not in offered, f"`{name}` is refusable and was offered:\n{err}"
        assert name in refused, f"`{name}` is missing from the refused list:\n{err}"
    assert "${CHARLIE_TOKEN}" not in err


def _offered_rows(err: str) -> dict[str, str]:
    """The offered half of the candidate list, as {server name: its row}."""
    offered = err.split("\n\n  Already baton-proxy")[0]
    return {
        name: line
        for line in offered.splitlines()
        for name in FIVE_ENTRIES["mcpServers"]
        if name in line
    }


def test_each_offered_row_says_whether_the_server_is_remote(tmp_path, kit_home, capsys):
    """Finding 4. The refused list names every class precisely; the offered one
    named none, so a stdio entry and an http+bearer entry rendered identically.

    That difference is not cosmetic: `CLAUDE.md` describes each offered row by
    its kind. Before this, the doc could only tell the agent to go back into the
    config and infer the kind from the presence of a `url`. The remote warnings
    that word once gated left the main flow in 0.6.4 and live in `SECURITY.md`
    §2, but a row the agent has to infer is still a row it can misdescribe.

    The two words are read off the module, so the doc's vocabulary and the code's
    cannot drift apart while both stay green."""
    rows = _offered_rows(_setup_listing(tmp_path, capsys))
    assert set(rows) == {"alpha", "charlie"}, f"the offered half changed shape: {rows}"
    assert kit.KIND_STDIO in rows["alpha"], f"alpha is a stdio server: {rows['alpha']!r}"
    assert kit.KIND_REMOTE in rows["charlie"], f"charlie is remote: {rows['charlie']!r}"
    # And each row carries ONE kind. A row reading "stdio" that also contains
    # "remote" somewhere would pass both assertions above and tell the reader
    # nothing, which is the state this finding started from.
    assert kit.KIND_REMOTE not in rows["alpha"], f"alpha is not remote: {rows['alpha']!r}"
    assert kit.KIND_STDIO not in rows["charlie"], f"charlie is not stdio: {rows['charlie']!r}"
    # And the doc's vocabulary IS the module's. Reading the constants above only
    # makes this test follow a rename; it does not stop one. `CLAUDE.md` names
    # the rows by the literal word, so a renamed constant with an untouched doc
    # leaves the agent hunting a word no row carries.
    claude_md = (REPO_ROOT / "try" / "CLAUDE.md").read_text(encoding="utf-8")
    for kind in (kit.KIND_STDIO, kit.KIND_REMOTE):
        assert f"`{kind}`" in claude_md, (
            f"the rows are marked {kind!r} and CLAUDE.md never says that word"
        )


def test_the_offered_rows_share_one_indent(tmp_path, kit_home, capsys):
    """Finding 6. `"\n\n  " + "\n".join(lines)` where every element already
    carried its own two spaces, so the first row sat at 4 and the rest at 2.

    Pinned as "all rows agree" rather than as a literal width: the width is a
    layout choice and finding 4 rewrites the row anyway; a first row that does
    not line up with its own list is the defect."""
    rows = _offered_rows(_setup_listing(tmp_path, capsys))
    indents = {name: len(row) - len(row.lstrip(" ")) for name, row in rows.items()}
    assert len(set(indents.values())) == 1, f"the offered rows do not line up: {indents}"


# --- TK-F-4: --src-config is answered, never quietly abandoned -------------


@pytest.fixture
def home_with_a_real_config(tmp_path, monkeypatch):
    """A populated `~/.claude.json` that every TK-F-4 case must NOT touch.

    Without this the tests would pass on a machine that simply has no global
    config — green by absence, on the one assertion whose whole subject is a
    fallback that must not happen."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text(
        canonical({"mcpServers": {"the-real-one": {"command": "npx", "args": ["-y", "real"]}}}),
        encoding="utf-8",
    )
    # Via the environment, not `kit.Path.home`: kit does `from pathlib import
    # Path`, so `kit.Path is pathlib.Path` and patching the attribute would
    # replace `Path.home` for every caller in the process, not just the kit's.
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _bad_config_refusal(argv, capsys) -> str:
    rc = kit.main(argv)
    out, err = capsys.readouterr()
    assert rc == 1, f"expected a refusal, got {rc}"
    assert out == "", "a refusal must not leak into the stream CLAUDE.md reads"
    return err


# Each case pairs with the phrase whose ABSENCE is the interesting failure.
# Naming the path is not enough on its own: `discover` skips an unreadable file
# silently when the path came from the default search list, and the whole reason
# it branches on `explicit` is that a named file's parse error is the person's
# answer. Drop that branch and every case still refuses, still names the path,
# and reports "no MCP configuration found" — which points at the wrong problem
# entirely. That mutation passed until these phrases were pinned.
_BAD_CONFIG_CASES = [
    ("missing", "cannot read"),
    ("directory", "cannot read"),
    ("not-json", "is not valid JSON"),
    ("no-mcpservers", "no MCP configuration found"),
]


@pytest.mark.parametrize("case,expected_phrase", _BAD_CONFIG_CASES)
def test_a_bad_config_file_is_named_and_never_falls_back(
    case, expected_phrase, tmp_path, kit_home, home_with_a_real_config, capsys
):
    """TK-F-4. Four ways to point `--src-config` at something unusable.

    The failure this prevents is not the error message: it is a typo'd path
    falling through to the search list and wrapping an entry in the person's
    REAL global config, which `SECURITY.md` §2 promises cannot happen unasked.
    So each case asserts four things — a 1, the path named, the right problem
    named, and that the entry only reachable through the fallback was neither
    named nor touched."""
    if case == "missing":
        target = tmp_path / "nope" / "mcp.json"
    elif case == "directory":
        target = tmp_path / "a-directory"
        target.mkdir()
    elif case == "not-json":
        target = tmp_path / "mcp.json"
        target.write_text('{"mcpServers": {"x": {"command": "npx"},}}', encoding="utf-8")
    else:
        target = tmp_path / "mcp.json"
        target.write_text(canonical({"projects": {}}), encoding="utf-8")

    err = _bad_config_refusal(["setup", "alpha", "--src-config", str(target)], capsys)
    assert str(target) in err or str(Path(target).resolve()) in err, err
    assert expected_phrase in err, (
        f"{case} was refused for the wrong stated reason — want {expected_phrase!r}:\n{err}"
    )
    assert "the-real-one" not in err, "the fallback config was read"
    # And nothing was written: no state file, and the real config is untouched.
    assert not (kit_home / "state.json").exists()
    assert "baton_proxy" not in (home_with_a_real_config / ".claude.json").read_text()


# --- TK-F-5: the four branches CLAUDE.md:63-77 dispatches on ----------------
#
# The doc quotes the first branch's string verbatim and paraphrases the rest.
# Asserting the QUOTED string against the doc's own text (rather than against a
# copy typed here) is what makes this a drift pin: a reword on either side has
# to move both.


def _receipt_output(capsys) -> str:
    rc = kit.main(["receipt"])
    out, _err = capsys.readouterr()
    assert rc == 0
    return out


def _claude_md() -> str:
    return (REPO_ROOT / "try" / "CLAUDE.md").read_text(encoding="utf-8")


def test_receipt_branch_one_no_state(kit_home, capsys):
    """No state file at all → *Setting up*. The string the doc quotes in bold is
    read OUT OF THE DOC, so a reword in either place fails here."""
    quoted = "No setup state found"
    assert _routed(quoted), "CLAUDE.md no longer names this branch"
    assert quoted in _receipt_output(capsys)


def test_receipt_with_no_state_is_not_served_the_has_state_checklist(kit_home, capsys):
    """Finding 5. With no state AND no events, TWO branches fired at once.

    After "No setup state found" came the four-step checklist written for someone
    whose setup DID run: step 1 asks whether the client has been restarted since
    a setup that never happened, and step 3 says to check the server name and
    config path "printed above" — neither of which is printed when there is no
    state — then sends them back to the command they just ran. `CLAUDE.md` routes
    the agent past all of it on the first line. The person reading their own
    terminal is routed nowhere.

    So the assertion is not that a nicer message exists. It is that the checklist
    for the other case does not appear in this one, and that the way forward is
    named."""
    out = _receipt_output(capsys)
    assert "No setup state found" in out
    assert "No events have been captured yet" not in out, (
        "the has-state checklist is being served to someone with no state:\n" + out
    )
    assert "restarted since setup ran" not in out, "there was no setup to restart since"
    assert "kit.py setup" in out, f"nothing tells the person where to go next:\n{out}"


@_BOTH_MODES
def test_receipt_branch_two_the_wrap_is_gone(tmp_path, kit_home, capsys, in_place):
    """State, but the entry holding the wrap is not the one setup wrote.

    Without this branch the agent sees "no events", walks the restart checklist,
    and lands on a machine where the proxy was never in the path at all.

    ⚠ The CAUSE differs by mode and the docstring used to name only one of them.
    In place: their client rewrites `~/.claude.json` continuously, so a
    hand-restore is common and it happens TO them. In project mode nothing
    rewrites our file — `CLAUDE.md:223` says so and it is true — so it goes
    missing only because a person removed it. Rarer, identical consequence, and
    harder for them to connect to capture stopping. See `_clobber`."""
    path = _wrapped(tmp_path, kit_home, capsys, in_place=in_place)
    _clobber(path)

    out = _receipt_output(capsys)
    assert "THE WRAP IS GONE" in out
    assert "uninstall" in out, "the branch must offer the way out the doc promises"
    assert "wrap is gone" in _claude_md()


def test_receipt_branch_three_state_but_no_events(tmp_path, kit_home, capsys):
    """Wrapped, still wrapped, nothing captured. The usual answer is that the
    client has not been restarted, and the doc promises "a short checklist"."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()

    out = _receipt_output(capsys)
    assert "No events have been captured yet" in out
    assert "THE WRAP IS GONE" not in out, "this is the other no-events branch"
    # The checklist's first item, reworded 08-31: the wrap is not pending on a
    # quit, it is pending on a session that has not started yet.
    assert "NEW client session" in out, f"the usual cause is not named first:\n{out}"


def test_receipt_branch_four_counts(tmp_path, kit_home, capsys):
    """Events → report the numbers. Pinned as the labels the agent reads back,
    not as a rendering: a renamed label is a branch the doc cannot find."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    (kit_home / "events.jsonl").write_text(
        "\n".join(
            json.dumps(e)
            for e in (
                {
                    "event_type": "tool_call_start",
                    "session_id": "s1",
                    "captured_at": "2026-08-30T10:00:00Z",
                    "payload": {"tool_name": "echo", "call_intent": "check the wrap"},
                },
                {
                    "event_type": "tool_call_end",
                    "session_id": "s1",
                    "captured_at": "2026-08-30T10:00:01Z",
                    "payload": {"tool_name": "echo", "result": {}, "duration_ms": 3},
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )
    out = _receipt_output(capsys)
    assert "tool calls           1" in out
    assert "sessions             1" in out
    assert "No events have been captured yet" not in out


def _receipt_with_events(tmp_path, kit_home, capsys, events: list[dict]) -> str:
    """A set-up trial whose event file is exactly these events. Setup runs for
    real so `receipt` reads the state it would read on a live machine — the
    banners below the counts are gated on it."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    (kit_home / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return _receipt_output(capsys)


def test_the_luhn_false_positive_rate_security_md_quotes_is_the_real_one():
    """ "Roughly one in ten" is a measured claim shown to a prospect, so it is
    pinned to the scrubber it describes rather than to a comment. Seeded, so a
    drift in `_luhn_valid` or the candidate regex fails here instead of quietly
    making the sentence false.

    It was the receipt that quoted it until 2026-09-04, when the `secrets
    redacted` row and its note came out; §6 of `SECURITY.md` still does, so the
    claim is still shipped and this still guards a live sentence."""
    import random

    from baton_proxy.scrub import _CC_CANDIDATE, _luhn_valid

    rng = random.Random(20260901)
    # Shaped like the things that actually appear in tool results, not like
    # cards: epoch millis, and 16-digit record ids.
    candidates = [str(rng.randrange(1_700_000_000_000, 1_800_000_000_000)) for _ in range(5000)]
    candidates += [str(rng.randrange(10**15, 10**16)) for _ in range(5000)]
    assert all(_CC_CANDIDATE.fullmatch(c) for c in candidates), "not all are cc candidates"
    hits = sum(1 for c in candidates if _luhn_valid(c))
    assert 0.08 <= hits / len(candidates) <= 0.12, f"Luhn passes {hits / len(candidates):.2%}"


# The phrase `CLAUDE.md` routes the post-uninstall case on. Tied to the module
# below rather than retyped, because a reworded constant with an untouched doc
# is the drift this whole branch exists to stop.
STATE_CLEARED_MARKER = "Setup state has been cleared"


# The banner an agent routes on. Six rows now, five banners: the counts row has
# no banner of its own and is read off its labels by `_counts_shown` below.
#
#   1  no state, no events        "No setup state found"
#   2  no state, events           "Setup state has been cleared"   (+counts)
#   3  state, no events, gone     "THE WRAP IS GONE"
#   4  state, no events           "No events have been captured yet"
#   5  state, events, no calls    "CONNECTED, BUT NOTHING CALLED IT"  (+counts)
#   6  state, events, calls       — counts only
#
# Row 5 requires state deliberately: on a trial that has already ENDED, row 2
# wins. Its remedy is to go look at /mcp and fix a live wrap, which is dead
# advice once the wrap is gone, and two banners in one output is the defect this
# property exists to stop.
NOTHING_CALLED_MARKER = "CONNECTED, BUT NOTHING CALLED IT"


def _fired(out: str) -> list[str]:
    return [
        m
        for m in (
            "No setup state found",
            STATE_CLEARED_MARKER,
            "THE WRAP IS GONE",
            "No events have been captured yet",
            NOTHING_CALLED_MARKER,
        )
        if m in out
    ]


def _routed(marker: str) -> bool:
    """Does CLAUDE.md's routing list name the banner `marker`?

    The doc quoted each banner verbatim until the 2026-09-04 rewrite, which
    bolds them instead and prints one of them in sentence case (`connected but
    nothing called it`). Case, commas and the quoting style are presentation;
    the WORDS are the tie to what `kit.py` prints, and they are what this
    normalises down to. A reworded banner on either side still fails.
    """
    return marker.lower().replace(",", "") in _flat(_claude_md()).lower()


def _counts_shown(out: str) -> bool:
    """Branch four has no banner of its own, so it is read off its labels.

    Without this the exclusivity test cannot see branch four at all, and an
    overlap between a header and the counts — which is exactly what the
    post-uninstall receipt was — stays invisible to it."""
    return all(label in out for label in ("sessions", "tool calls", "events"))


def test_the_no_state_branch_fires_alone_too(kit_home, capsys):
    """The exclusivity property, on the case that had two markers in one output.

    The test below it only ever ran the state-and-no-events case, so the doc's
    table looked like a table while its first row and its third both matched a
    fresh folder. That is the branch an agent meets most often — a `try/` nobody
    has set up yet."""
    assert _fired(_receipt_output(capsys)) == ["No setup state found"]


def test_the_four_receipt_branches_are_mutually_exclusive(tmp_path, kit_home, capsys):
    """The property that makes the doc's table a table. Two branches firing at
    once is how an agent on an already-wrapped machine falls through to
    *Setting up* and wraps a second server on top of the first."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    out = _receipt_output(capsys)
    assert _fired(out) == ["No events have been captured yet"], _fired(out)


def _ended_trial(kit_home):
    """The state `uninstall` leaves: events on disk, state.json gone."""
    (kit_home / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "tool_call_start",
                "session_id": "s1",
                "captured_at": "2026-08-30T10:00:00Z",
                "payload": {"tool_name": "echo", "call_intent": "check the wrap"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert not (kit_home / "state.json").exists()


def test_an_ended_trial_is_its_own_branch_not_branch_one(kit_home, capsys):
    """`uninstall` unlinks state.json and LEAVES events.jsonl — it prints it
    under "left behind" — so EVERY receipt after a finished trial has events and
    no state. That output opened with "No setup state found" and then printed
    the full counts, so the doc's first row and its last both matched it: the
    agent is told nothing is wrapped yet AND that the trial is running, of a
    person who ended it themselves.

    The marker is read out of the module and out of the doc, so a reword in
    either place fails here rather than silently unrouting the branch."""
    _ended_trial(kit_home)
    assert STATE_CLEARED_MARKER in kit.STATE_CLEARED, "the module no longer says this"
    assert _routed(STATE_CLEARED_MARKER), "CLAUDE.md no longer names this branch"
    out = _receipt_output(capsys)
    assert _fired(out) == [STATE_CLEARED_MARKER], _fired(out)
    assert _counts_shown(out), "an ended trial still reports its numbers:\n" + out


def test_the_ended_trial_branch_does_not_read_as_never_set_up(kit_home, capsys):
    """The half that matters to the person rather than the agent: branch one
    sends them to *Setting up*, which on a machine whose trial just ended means
    wrapping a server again to answer a question about one that already ran."""
    _ended_trial(kit_home)
    out = _receipt_output(capsys)
    assert "No setup state found" not in out, "the ended-trial receipt claims nothing ran:\n" + out


# --- TK-F-7: the kit will not wrap its own work ----------------------------


def test_setup_refuses_an_entry_this_kit_already_wrapped(tmp_path, kit_home, capsys):
    """TK-F-7. Nested proxies — the geometry `check_kit_bridge.py:54` grades with
    LLM spend, caught here for free and one layer earlier.

    Reached with no state file, which is the case that matters: with state,
    `setup` reports "already wrapped" and returns 0. Without it the entry is
    someone else's wrap as far as this kit can tell, and wrapping it again would
    put the outer proxy's `surface_snapshot` on the inner proxy's injected
    tools — a capture that looks healthy and describes the wrong server."""
    wrapped = {
        "mcpServers": {
            "notion": kit.build_wrapped_entry(GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS)
        }
    }
    path = _config(tmp_path, wrapped)
    rc = kit.main(["setup", "notion", "--src-config", str(path)])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert "already wrapped in baton-proxy" in err
    assert json.loads(path.read_text(encoding="utf-8")) == wrapped, "the config was touched"


def test_setup_refuses_its_own_bridge_entry_without_telling_anyone_to_delete_it(
    tmp_path, kit_home, capsys
):
    """The bridge half of TK-F-7, which needs a different sentence.

    An `--url` entry has no upstream command inside it, so the stdio branch's
    advice — "unwrap it by hand first" — would mean deleting the entry's only
    launch mechanism. Never tell someone to do that."""
    bridged = {
        "mcpServers": {
            "remote": kit.build_wrapped_entry(HTTP_VAR_BEARER["mcpServers"]["remote"], **WRAP_ARGS)
        }
    }
    path = _config(tmp_path, bridged)
    rc = kit.main(["setup", "remote", "--src-config", str(path)])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out == ""
    assert "IS baton-proxy" in err
    assert "unwrap it by hand" not in err
    assert json.loads(path.read_text(encoding="utf-8")) == bridged


# ---------------------------------------------------------------------------
# SECURITY.md §4's injected-parameter disclosure.
#
# The document tells a prospect exactly what the proxy grafts onto their tools'
# schemas, by count AND by name. That is a disclosure, not prose: a reader
# decides whether to run the kit on it. It went stale the moment a third param
# was injected, and every test stayed green — the same failure shape §9's
# greps were written for, one section up.
# ---------------------------------------------------------------------------

_COUNT_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}


def _injected_param_names() -> list[str]:
    """The names the proxy actually grafts, straight from the injector."""
    tool: dict[str, Any] = {"name": "t", "inputSchema": {"type": "object", "properties": {}}}
    _inject_goal_params(tool, "optional")
    return sorted(tool["inputSchema"]["properties"])


# The disclosure's own words, so the anchor and the count claim below cannot
# drift apart. "grafted onto" became "added to" in the 2026-09-04 rewrite; the
# sentence is the same sentence and this is the one place that spelling lives.
_INJECTION_CLAIM_TAIL = "parameters added to every upstream tool's schema"


def _injection_disclosure() -> str:
    """§4's injection paragraph alone.

    Scoped deliberately: these names appear elsewhere in the document (§8 lists
    them again as recorded content), so asserting against the whole file passes
    while the disclosure itself is missing one.
    """
    doc = (KIT_PATH.parent / "SECURITY.md").read_text()
    start = doc.index(_INJECTION_CLAIM_TAIL)
    return doc[start : doc.index("\n\n", start)]


def test_security_md_discloses_every_injected_param_by_name():
    """§4's injection paragraph must name each grafted param.

    A reviewer greps for the name they saw in their own tool schema; a name the
    disclosure omits reads as one the proxy adds without saying so, which is the
    single worst thing this document can do.
    """
    disclosure = _injection_disclosure()
    for name in _injected_param_names():
        assert f"`{name}`" in disclosure, (
            f"SECURITY.md §4's disclosure never names the injected param {name!r}"
        )


def test_security_md_injected_param_count_matches_the_code():
    """§4 states the count in words; the injector decides it.

    Pinned separately from the names because the two drift apart differently: a
    fourth param added and documented still leaves the sentence saying "Three".
    """
    doc = (KIT_PATH.parent / "SECURITY.md").read_text()
    expected = _COUNT_WORDS[len(_injected_param_names())]
    claim = f"**{expected} {_INJECTION_CLAIM_TAIL}:**"
    assert claim in doc, (
        f"SECURITY.md §4 does not say {expected!r} parameters; the proxy injects "
        f"{_injected_param_names()}"
    )


def test_security_md_says_required_is_advertised_and_not_enforced():
    """The word "optional" left this sentence on 2026-09-01, when the default
    became `required` — a reviewer now sees `user_goal` in their own tools'
    `required` arrays, and a document that did not mention it would be caught
    omitting the one addition they can see with their own eyes.

    Both halves or neither. "Required" alone tells them the wrap can refuse
    their server's traffic, which is false and is the scariest possible false
    claim to make here; silence leaves the retired optional story standing."""
    doc = _flat((KIT_PATH.parent / "SECURITY.md").read_text())
    assert "and nothing enforces it" in doc, (
        "SECURITY.md never says the advertised requirement is not enforced"
    )
    assert "forwarded exactly as it would have been unwrapped" in doc, (
        "the consequence a reviewer actually cares about — their own call still "
        "goes through — is not stated"
    )
    # The escape hatch, because a reviewer who does not want the word in their
    # schemas at all should not have to ask us for it.
    assert "BATON_INTENT_PARAM=optional" in doc


# ---------------------------------------------------------------------------
# TK-D-1 — "fully quit and reopen" is false, and it is a belief rather than a
# string (Dave's run, 2026-08-28, blocker 2).
#
# Verified on that run: a second terminal picked up the wrap with the original
# session still live. Each client process reads `~/.claude.json` at startup;
# there is no daemon to flush, so nothing needs to be quit. The true statement
# is much narrower — the session ALREADY RUNNING keeps the subprocesses it
# launched and will never see the change.
#
# Two costs to the false one, and the second is the one that matters. We charge
# a stranger the price of closing their editor at the most abandonable moment in
# the funnel, for nothing. Then: a reviewer who knows how stdio MCP works can
# see the claim is false, in the same voice as a security document whose entire
# power is that it is verifiably accurate.
#
# So the pin is over every sink the belief was written into, not over the one
# constant — it appeared in kit.py, in both docs and in the README
# ([[feedback_invariant_scoped_to_one_field]]: a rule enforced on one field is
# not enforced on the record).
# ---------------------------------------------------------------------------

# The DEMAND forms, not the words. A document is allowed to say "nothing needs
# to be quit" — that sentence is the fix — and a comment is allowed to name the
# phrase it retired. What may not survive is anything that asks for the act.
_QUIT_BELIEF = re.compile(
    r"(?i)fully (?:quit|restart(?:ed)?)"
    r"|quit (?:and|then) reopen"
    r"|reopen (?:the|your) client"
    r"|needs? (?:a|another) restart"
    r"|restart (?:the|your)(?: MCP)? client"
    r"|(?:the|your) client restarts"
    # Naming the client by product name is the same demand — README:60 said
    # "Restart Claude" and sailed through the first version of this sweep,
    # in a file the sweep claims to cover.
    r"|restart\s+claude"
    # And the noun form, which presupposes the event even when it asks for
    # nothing: "verify it before restarting", "a dead server after the restart".
    r"|(?:after|before) the restart"
    r"|before restarting"
)

# Every file that tells a person or an agent what to do after the config edit.
# The two docs are swept whole — every line of them is shown to someone. kit.py
# is swept as its STRING CONSTANTS only: what it prints is the sink, and a
# comment explaining which phrasing was retired is not a thing anyone is asked
# to do. That scope is the assertion's honest limit, so it is stated rather
# than left to the reader of a passing test.
_RESTART_SINKS = ("try/CLAUDE.md", "try/SECURITY.md", "README.md", "try/PROMPT.md")


def _kit_strings() -> list[tuple[int, str]]:
    """Every string literal in kit.py, with its line — docstrings included,
    since the module docstring is read by the reviewer this file is written for."""
    import ast

    tree = ast.parse((REPO_ROOT / "try" / "kit.py").read_text(encoding="utf-8"))
    return [
        (n.lineno, n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_the_string_sweep_actually_reaches_the_text_the_kit_prints():
    """Guard against the guard, second kind: an extractor that returns nothing
    makes the test below pass while checking no text at all."""
    strings = _kit_strings()
    assert len(strings) > 100, f"only {len(strings)} strings extracted from kit.py"
    assert any("NEXT session your client starts" in s for _n, s in strings), (
        "the extractor missed RESTART_NOTE, which is the string this is all about"
    )


def test_no_text_the_trial_shows_anyone_asks_them_to_quit_their_client():
    """The sweep. A phrase deleted from `RESTART_NOTE` and left in `CLAUDE.md`
    is the same defect, because the agent reads the doc and the person reads the
    terminal — two sinks, one claim."""
    offenders = []
    for rel in _RESTART_SINKS:
        for n, line in enumerate((REPO_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if _QUIT_BELIEF.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    for n, s in _kit_strings():
        if m := _QUIT_BELIEF.search(s):
            offenders.append(f"try/kit.py:{n}: …{m.group(0)}…")
    assert not offenders, "the kit still asks someone to quit their client:\n" + "\n".join(
        offenders
    )


# Every instance the 08-28 run and the greps after it turned up, verbatim. The
# regex is the whole assertion above, and one that matches nothing passes
# forever ([[feedback_control_condition_must_be_able_to_fail]]).
_THE_BELIEF_AS_IT_WAS_WRITTEN = (
    "their MCP client is fully quit and reopened; it binds its server set at startup",
    "> Quit and reopen the client. This session will end with it.",
    "The change is INERT until you fully restart your MCP client",
    "  1. Has the MCP client been fully restarted since setup ran?",
    "- The client needs another restart before the original server is live again.",
    "- **The change is inert until the client restarts.**",
    "verifies the result against the file on disk; restart your client",
    "That's the entire install. **Restart Claude**, drive the wrapped server",
    "and surface as a dead server after the restart — days later",
    "Verify it yourself before restarting:",
)

# Sentences the fix is made of. A sweep that also rejects these has banned the
# vocabulary rather than the instruction, and the next true sentence about
# startup behaviour cannot be written.
_TRUE_REPLACEMENTS = (
    "This takes effect in the NEXT session your client starts.",
    "A new terminal is enough — nothing needs to be closed.",
    "Nothing needs to be quit: a new terminal is enough.",
    "New sessions will use your original server again.",
    "a client binds its server set at startup",
    "starting a new Claude session, drive a few tool calls",
    "surface as a dead server in the next session they start",
)


@pytest.mark.parametrize("line", _THE_BELIEF_AS_IT_WAS_WRITTEN)
def test_the_sweep_would_notice_the_phrasings_it_was_written_for(line):
    assert _QUIT_BELIEF.search(line), f"the sweep would have missed {line!r}"


@pytest.mark.parametrize("line", _TRUE_REPLACEMENTS)
def test_the_sweep_leaves_the_true_sentences_alone(line):
    assert not _QUIT_BELIEF.search(line), f"the sweep rejects its own replacement: {line!r}"


def test_setup_says_what_is_actually_required_of_them(tmp_path, kit_home, capsys):
    """Rendered, not the constant: setup is where the claim is charged, and the
    replacement has to keep the warning the false version carried. Dropping
    "the session running now sees nothing" recreates the empty capture from the
    other direction — someone keeps using the window they already had open."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert not _QUIT_BELIEF.search(out), out
    assert "new" in out.lower(), "nothing says a NEW session is what picks the wrap up"
    assert "running now" in out, f"the already-running session is not warned about:\n{out}"


def test_uninstall_does_not_charge_a_restart_it_does_not_need(tmp_path, kit_home, capsys):
    """Uninstall's true line is gentler still, and it is a different sentence
    from setup's: nothing is pending, nothing is inert, and there is nothing to
    verify afterwards. It said "the change is INERT until you fully restart"."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    assert kit.main(["uninstall"]) == 0
    out, _err = capsys.readouterr()
    assert not _QUIT_BELIEF.search(out), out
    assert "original server again" in out, f"uninstall never says what happens next:\n{out}"
    assert "INERT" not in out, "uninstall is not pending on anything"


# ---------------------------------------------------------------------------
# TK-D-2 — the handoff names the directory the wrap actually loads in (Dave's
# run, 2026-08-28, blocker 1 — the single highest-loss defect in the flow).
#
# In that run setup wrote the wrapped entry to
# `projects["/Users/davideyler/workplace"]` and then told him to start his
# client from `…/baton-proxy/try`. A project-scoped server only loads for a
# session started from its own directory, so following the instruction loads
# global scope, the wrapped entry never starts, and `events.jsonl` stays empty —
# after he has read the security page, approved the commands, and let us rewrite
# his client config. Every cost paid, blank result, and the only conclusion
# available to him is that Baton does not work. He does not file a bug.
#
# The path was never missing: `describe()` prints the project key in setup's own
# "Wrapped" line. The instruction ignored it. So the sentence is CHOSEN from the
# scope setup already holds, and a hardcoded string cannot be the fix — which is
# what these tests pin, one per scope.
#
# Two directories, and conflating them is the bug:
#   try/          — ours. setup, receipt, uninstall run here.
#   project key   — theirs. where they start the client.
# ---------------------------------------------------------------------------


def _project_config(tmp_path, key: str, name: str = "notion") -> Path:
    """A config whose only server is scoped to `key`."""
    return _config(
        tmp_path,
        {
            "mcpServers": {},
            "projects": {key: {"mcpServers": {name: {"command": "npx", "args": ["-y", "srv"]}}}},
        },
    )


@pytest.fixture
def global_config(tmp_path, monkeypatch):
    """`~/.claude.json`, relocated. Two of the tests below assert the sentence
    that is only true for the global config, so the fixture has to make the file
    they write actually be it — a tmp path with a global-shaped entry inside is
    a project config as far as the kit is now concerned, and rightly."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    path = home / ".claude.json"
    path.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")
    return path


@_BOTH_MODES
def test_setup_names_the_project_directory_the_wrapped_entry_loads_in(
    tmp_path, kit_home, capsys, in_place
):
    """Finding 11, at the sink that produced it.

    ⚠ The CLAIM is unchanged by K1b and the ANSWER is not: name the directory
    the wrapped entry actually loads in. In place that is still their own
    project key, because the entry never moves. In project mode the entry was
    copied into our checkout, so the checkout is the only right answer and
    naming their key would send them to a session that loads the ORIGINAL
    server — finding 11 again, pointing the other way."""
    key = "/Users/someone/work/app"
    path = _project_config(tmp_path, key)
    args = ["setup", "notion", "--src-config", str(path), "--tenant", "t"]
    if in_place:
        args.append("--in-place")
    assert kit.main(args) == 0
    out, _err = capsys.readouterr()
    loads_in = key if in_place else str(kit.MCP_PATH.parent)
    assert f"cd {loads_in} && claude" in out, f"the handoff never names {loads_in}:\n{out}"


def test_the_handoff_never_offers_the_kits_own_directory_as_the_place_to_start(
    tmp_path, kit_home, capsys
):
    """The other half, and the one that actually fired: it is not enough that
    the right path appears somewhere — the WRONG one must not be handed over as
    a command. `try/` is where the kit's three commands run; it is never where
    their client starts unless their own config says so."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert f"cd {kit.TRY_DIR} && claude" not in out, out
    assert "baton-proxy/try && claude" not in out, out


def test_a_global_entry_is_not_given_an_invented_directory(global_config, kit_home, capsys):
    """A global entry loads wherever they start from, so naming a directory
    would be a fresh false instruction rather than the same one corrected."""
    path = global_config
    assert (
        kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t", "--in-place"]) == 0
    )
    out, _err = capsys.readouterr()
    assert "cd " not in out, f"a global wrap was told to cd somewhere:\n{out}"
    assert "second terminal" in out


def test_the_cd_is_dropped_when_they_are_already_in_the_project_directory(
    tmp_path, kit_home, capsys, monkeypatch
):
    """Dave: "When the current directory already matches the project key, drop
    the `cd`." Telling someone to cd to where they are reads as a step they got
    wrong.

    ⚠ `--in-place` only, and NOT because project mode cannot reach the state —
    because it does not implement the rule. Measured at K1b: `start_where`
    applies the already-there check inside its `scope is not None` branch, and
    project mode goes through `scope is None`, which emits a `cd` unconditionally.
    Running setup from the checkout root and then reading the handover gets
    `cd <the directory you are standing in> && claude`.

    Left as a finding rather than fixed here, because extending Dave's rule is a
    judgment about what a prospect reads and there is an argument on the other
    side: the handover is for a SECOND terminal, which opens in their default
    directory rather than wherever setup ran, so the `cd` is useful even when
    the setup process was already there. That argument would also retire the
    rule for the in-place case, which Dave asked for — so it is one decision
    about both branches, not a gap to close quietly. → backlog.
    """
    here = (tmp_path / "work").resolve()
    here.mkdir()
    monkeypatch.chdir(here)
    path = _project_config(tmp_path, str(here))
    assert (
        kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t", "--in-place"]) == 0
    )
    out, _err = capsys.readouterr()
    assert "cd " not in out, f"told to cd to the directory they are standing in:\n{out}"
    assert "second terminal" in out


@_BOTH_MODES
def test_the_already_wrapped_path_hands_over_the_same_directory(
    tmp_path, kit_home, capsys, in_place
):
    """Cold re-entry is the normal case on a multi-day trial, not a fallback:
    windows close, laptops sleep. Someone who re-runs setup gets "already
    wrapped" — and used to get no handoff at all, which is the state the person
    is in precisely when they have lost the first window.

    "The SAME directory" is the whole assertion, so it reads the state rather
    than a constant: the re-entry path must reach the same answer the first run
    did, in whichever mode that run used."""
    key = "/Users/someone/work/app"
    path = _project_config(tmp_path, key)
    args = ["setup", "notion", "--src-config", str(path), "--tenant", "t"]
    if in_place:
        args.append("--in-place")
    assert kit.main(args) == 0
    capsys.readouterr()
    assert kit.main(args) == 0, "the re-entry run is the same command, run again"
    out, _err = capsys.readouterr()
    loads_in = key if in_place else str(kit.MCP_PATH.parent)
    assert "Already wrapped" in out
    assert f"cd {loads_in} && claude" in out, f"the second run hands over nothing:\n{out}"


def test_every_scope_hands_over_the_line_the_doc_tells_the_agent_to_relay():
    """The doc tie. `CLAUDE.md` routes the agent to a line by its opening words,
    so all three scopes have to open with them — and the doc has to still say
    so. A reworded helper with an untouched doc leaves the agent looking for a
    line that is not there and composing its own path, which is the defect."""
    marker = "Open a second terminal"
    assert f"`{marker}`" in _claude_md(), "CLAUDE.md no longer routes on this line"
    home_config = Path.home() / ".claude.json"
    for scope, config in (
        (None, home_config),  # global
        (None, "/Users/someone/work/app/.mcp.json"),  # a project config file
        ("/Users/someone/work/app", home_config),  # project scope
        (str(Path.cwd()), home_config),  # project scope, already there
    ):
        line = kit.start_where(scope, config)
        assert line.startswith(marker), (scope, config)
        # The kit is Claude Code only. "Start your client" is followed exactly by
        # someone on another client, whose events file then stays empty.
        assert "start Claude Code" in line and "your client" not in line, (scope, config, line)


# A CONCRETE path — `cd <path>` describing the shape of setup's line is the
# handover working, not the defect. What may not appear is a directory the doc
# picked, which is what `cd baton-proxy/try && claude` was.
_DOC_PICKS_A_DIRECTORY = re.compile(r"cd\s+(?!<)[^\s`]+\s*&&\s*claude")


def test_the_doc_never_names_a_directory_to_start_the_clients_session_in():
    """The other half of finding 11: the wrong path was IN THE DOC, as a command
    to run. Only setup knows the right one, so the doc must hand over rather
    than instruct — including for the kit's own folder, which is right for
    `kit.py` and wrong for their client."""
    assert _DOC_PICKS_A_DIRECTORY.search("run `cd baton-proxy/try && claude` again"), (
        "the check would not have caught the line it was written for"
    )
    assert not _DOC_PICKS_A_DIRECTORY.search("hands over a `cd <path> && claude`")
    offenders = [line for line in _claude_md().splitlines() if _DOC_PICKS_A_DIRECTORY.search(line)]
    assert not offenders, "CLAUDE.md names a start directory itself:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# TK-D-3 — the receipt diagnoses instead of reporting (Dave's run, 2026-08-28,
# blocker 3; findings 13 and 17).
#
# A near-duplicate server in global scope, `toybox-baton`, answered all four
# tool calls while the wrapped `toybox` sat idle. The receipt printed
# `sessions 2 / tool calls 2` — meaning one of the two sessions recorded zero —
# and said nothing about it. It held the exact evidence and reported it as a
# statistic.
#
# This is the only screen a stranger sees after something goes wrong, so it is
# the one chance to turn "it didn't work" into a next step. Zero events and zero
# CALLS are different failures with different causes; the counts have to be per
# session or the difference is not even representable.
# ---------------------------------------------------------------------------


def _session_events(sid: str, calls: int, hour: int = 10) -> list[dict]:
    """One session: its tool-surface snapshot, then `calls` matched pairs.

    Every session records a snapshot even if the agent never calls the server
    (SECURITY.md §5) — which is exactly why a zero-call session is invisible in
    an aggregate and obvious per session."""
    out: list[dict] = [
        {
            "event_type": "surface_snapshot",
            "session_id": sid,
            "captured_at": f"2026-08-30T{hour:02d}:00:00Z",
            "payload": {"tools": [{"name": "search"}, {"name": "fetch"}]},
        }
    ]
    for i in range(calls):
        out.append(
            {
                "event_type": "tool_call_start",
                "session_id": sid,
                "captured_at": f"2026-08-30T{hour:02d}:{i + 1:02d}:00Z",
                "payload": {"tool_name": "search", "call_intent": "look something up"},
            }
        )
        out.append(
            {
                "event_type": "tool_call_end",
                "session_id": sid,
                "captured_at": f"2026-08-30T{hour:02d}:{i + 1:02d}:01Z",
                "payload": {"tool_name": "search", "result": {}, "duration_ms": 3},
            }
        )
    return out


def _write_events(kit_home, *sessions: tuple[str, int]) -> None:
    rows: list[dict] = []
    for n, (sid, calls) in enumerate(sessions):
        rows.extend(_session_events(sid, calls, hour=10 + n))
    (kit_home / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def _wrapped(tmp_path, kit_home, capsys, scope_key: str | None = None, *, in_place: bool = False):
    """Wrap `notion` and return THE FILE THAT HOLDS THE WRAP.

    ⚠ It used to return the source config unconditionally, which was the same
    file under the old default and is not any more. Callers that clobber the
    wrap need the file setup actually wrote, or they edit a file the receipt no
    longer looks at and the row under test never fires.
    """
    path = _project_config(tmp_path, scope_key) if scope_key else _config(tmp_path, GLOBAL_ONLY)
    args = ["setup", "notion", "--src-config", str(path), "--tenant", "t"]
    if in_place:
        args.append("--in-place")
    assert kit.main(args) == 0
    capsys.readouterr()
    return path if in_place else kit.MCP_PATH


# The step's own question mark, then the next non-empty line — which is the
# directory. One wording literal, and it is the distinctive half of the sentence.
_DIRECTORY_STEP = re.compile(r"loads in\?\s*\n\s*(\S.*)")


def _directory_question(checklist: str) -> str:
    """The directory named by the checklist's "…this entry loads in?" step, alone.

    ⚠ Exists because `<dir> in checklist` is a lie here. The checklist can carry
    the same directory on the APPROVAL step, so a search over the whole block
    stays green while this step names somewhere else entirely — proven with a
    mutant that did exactly that. Sliced to the one step, so the assertion can
    only be satisfied by the line it is about
    → [[feedback_string_slicing_a_document_measures_the_wrong_thing]].

    ⚠ Anchored on the question mark, capturing the next non-empty line. The
    first version sliced between two wording literals and then took `lines[1]`,
    which was brittle in three ways worth naming: the terminator
    ("A session started anywhere else") is a SECOND literal that must track
    `kit.py`, and it is not unique in that file; `lines[1]` silently shifts if
    the step's indentation or wrapping changes; and the empty-string fallback
    turned "the question is missing entirely" into a mismatch that read as
    "wrong directory". One literal now, and a missing question fails as itself.
    """
    match = _DIRECTORY_STEP.search(checklist)
    assert match, f"the checklist never asks the directory question:\n{checklist}"
    return match.group(1).strip()


def _clobber(path: Path, name: str = "notion") -> None:
    """Replace the wrapped entry with an unwrapped one, wherever it sits.

    This is the hand-restore the `THE WRAP IS GONE` row exists for, and the two
    modes reach it by different routes — which is why both are parametrized
    rather than one standing in for the other:

    - **in place**: their client rewrites `~/.claude.json` continuously, so this
      happens TO them and is the original reason the row was built.
    - **project**: nothing rewrites our file — `CLAUDE.md:223` says so, and it
      is true. It goes missing because a PERSON removes it: setup's own leftover
      refusal (`kit.py:2045`) tells them deleting it is safe, `git clean -x`
      takes it, or an uninstall half-runs. Rarer, same consequence, and the
      person is even less likely to connect it to capture stopping.

    ⚠ It FINDS the entry rather than being told the shape. An earlier version
    took the scope key and rebuilt the whole file from a literal, which meant
    every caller re-derived `key if in_place else None` — a fact `_wrapped` had
    already settled one line above. Worse, it could not fail usefully: writing
    the wrong shape produces a file with no wrap in it, which is exactly what a
    SUCCESSFUL clobber produces. A broken helper and a working one were
    indistinguishable, and the row would have gone on passing.

    Hence `assert replaced`. And rewriting the entry in place rather than
    replacing the file is closer to the hand-restore being modelled: the rest of
    their config survives, which is the whole reason the in-place row is about a
    file the client owns.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    holders = [data.get("mcpServers")]
    holders += [p.get("mcpServers") for p in (data.get("projects") or {}).values()]

    replaced = False
    for holder in holders:
        if isinstance(holder, dict) and name in holder:
            holder[name] = {"command": "npx", "args": ["-y", "srv"]}
            replaced = True
    assert replaced, f"no `{name}` entry to clobber in {path}:\n{data}"

    path.write_text(canonical(data), encoding="utf-8")


def test_daves_run_no_longer_reports_a_dead_session_as_a_statistic(tmp_path, kit_home, capsys):
    """`sessions 2 / tool calls 2`, one of them dead. The shape of his run."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_events(kit_home, ("d1e2f3a4", 2), ("bee5d1a2", 0))
    out = _receipt_output(capsys)
    assert "bee5d1a2" in out, f"the dead session is not named:\n{out}"
    assert "0 calls" in out, f"its count is not readable per session:\n{out}"
    assert "/mcp" in out, f"the likely cause is not named:\n{out}"


def test_a_mixed_capture_is_the_counts_branch_and_not_a_banner(tmp_path, kit_home, capsys):
    """The diagnostic rides INSIDE the counts row rather than replacing it —
    calls landed, so the headline is what was captured. A banner here would
    route the agent to a failure on a trial that is working."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_events(kit_home, ("d1e2f3a4", 2), ("bee5d1a2", 0))
    out = _receipt_output(capsys)
    assert _fired(out) == [], _fired(out)
    assert _counts_shown(out)


def test_a_session_that_connected_and_called_nothing_is_its_own_branch(tmp_path, kit_home, capsys):
    """Row 5. Handshake events present, zero calls anywhere: the file is not
    empty, so the empty-file checklist does not apply, and the counts alone say
    `sessions 1 / tool calls 0` without saying what to do about it."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_events(kit_home, ("bee5d1a2", 0))
    out = _receipt_output(capsys)
    assert _fired(out) == [NOTHING_CALLED_MARKER], _fired(out)
    assert _counts_shown(out), "row 5 still reports its numbers:\n" + out
    assert "/mcp" in out, "the cause Dave's run hit is not named:\n" + out
    assert _routed(NOTHING_CALLED_MARKER), "CLAUDE.md does not route on this row"


def test_an_ended_trial_that_captured_nothing_is_still_the_ended_trial_branch(kit_home, capsys):
    """Two rows could match this: uninstall leaves events and removes state, and
    those events can be all-handshake. Row 2 wins — row 5's remedy is to go fix
    a live wrap, and there is no wrap left to fix."""
    _write_events(kit_home, ("bee5d1a2", 0))
    (kit_home / "state.json").unlink(missing_ok=True)
    out = _receipt_output(capsys)
    assert _fired(out) == [STATE_CLEARED_MARKER], _fired(out)


@_BOTH_MODES
def test_an_empty_file_under_a_project_scoped_wrap_names_the_directory(
    tmp_path, kit_home, capsys, in_place
):
    """Row 4, carrying finding 11's other half. `receipt` is where someone lands
    when the trial produced nothing, so the checklist has to ask the question
    the wrong-directory bug makes decisive — and it can only ask it when the
    wrap is scoped to a directory at all.

    Project mode makes that ALWAYS true, which is why both modes run here: in
    place it depends on their entry having a project key, and in project mode
    our own file always has one. The directory differs; the question does not."""
    key = "/Users/someone/work/app"
    _wrapped(tmp_path, kit_home, capsys, scope_key=key, in_place=in_place)
    out = _receipt_output(capsys)
    assert _fired(out) == ["No events have been captured yet"], _fired(out)
    # Scoped to the checklist: the header already prints the directory as part
    # of the config location, so asserting over the whole output would pass
    # without the checklist ever asking the question.
    checklist = out[out.index("No events have been captured yet") :]
    loads_in = key if in_place else str(kit.MCP_PATH.parent)
    assert "loads in" in checklist, f"the checklist never asks the question:\n{checklist}"
    # ⚠ Scoped to the STEP, not the checklist. `loads_in in checklist` passed
    # under a mutant that made this step name the wrong directory, because the
    # APPROVAL step above it names the same directory and satisfied the search.
    # An assertion that another line can satisfy is not testing this one.
    assert _directory_question(checklist) == loads_in, (
        f"the directory question names the wrong place:\n{checklist}"
    )


def test_an_empty_file_under_a_global_wrap_invents_no_directory(global_config, kit_home, capsys):
    """The same checklist must not grow a step that is false. A global entry
    loads wherever they start, so "start it from X" would be a new wrong
    instruction replacing the one just fixed."""
    assert (
        kit.main(
            ["setup", "notion", "--src-config", str(global_config), "--tenant", "t", "--in-place"]
        )
        == 0
    )
    capsys.readouterr()
    out = _receipt_output(capsys)
    assert _fired(out) == ["No events have been captured yet"], _fired(out)
    checklist = out[out.index("No events have been captured yet") :]
    assert "started from" not in checklist, f"a global wrap was given a directory:\n{checklist}"
    assert "loads in" not in checklist, checklist


@_BOTH_MODES
def test_the_six_receipt_rows_are_mutually_exclusive(tmp_path, kit_home, capsys, in_place):
    """The property that makes the doc's table a table, over every row at once.

    It has failed twice on this file, both times because a case nobody ran had
    two markers in it ([[feedback_invariant_scoped_to_one_field]]). So the cases
    are enumerated here rather than left one-per-test: the failure was never a
    wrong assertion, it was a row nothing exercised."""
    key = "/Users/someone/work/app"

    def fresh() -> None:
        (kit_home / "events.jsonl").unlink(missing_ok=True)
        (kit_home / "state.json").unlink(missing_ok=True)
        # ⚠ And the wrap file, since K1b. Every case here re-runs setup, and
        # project mode REFUSES when `.mcp.json` is present with no state file —
        # correctly, since that is an unaccountable leftover. Without this line
        # case 4 never wraps, and the row it is checking is asserted against a
        # refusal instead of a wrap.
        kit.MCP_PATH.unlink(missing_ok=True)

    # 1 — nothing here at all.
    fresh()
    assert _fired(_receipt_output(capsys)) == ["No setup state found"]

    # 2 — uninstall's leftovers: events, no state.
    fresh()
    _write_events(kit_home, ("d1e2f3a4", 2))
    assert _fired(_receipt_output(capsys)) == [STATE_CLEARED_MARKER]

    # 3 — wrapped, then the wrap goes (see `_clobber`: how differs by mode).
    fresh()
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key, in_place=in_place)
    _clobber(path)
    assert _fired(_receipt_output(capsys)) == ["THE WRAP IS GONE"]

    # 4 — wrapped, still wrapped, nothing landed.
    fresh()
    _wrapped(tmp_path, kit_home, capsys, scope_key=key, in_place=in_place)
    assert _fired(_receipt_output(capsys)) == ["No events have been captured yet"]

    # 5 — connected, never called.
    _write_events(kit_home, ("bee5d1a2", 0))
    assert _fired(_receipt_output(capsys)) == [NOTHING_CALLED_MARKER]

    # 6 — the working trial.
    _write_events(kit_home, ("d1e2f3a4", 2), ("bee5d1a2", 0))
    out = _receipt_output(capsys)
    assert _fired(out) == [] and _counts_shown(out)


# ---------------------------------------------------------------------------
# TK-D-4 — §6 discloses that annotations restate what the server returned
# (Dave's run, 2026-08-28, blocker 4; finding 18).
#
# The run found this about ITSELF: to explain why a tool was wrong, the agent
# wrote the captured rows into the annotation's `context` field. So the business
# data is in the file twice — once as the tool result, once as model-composed
# prose — and the scrubber reported zero redactions, which it would also have
# reported on the prose, because it matches credentials and personal
# identifiers, not groceries.
#
# §6 said results land verbatim. It did not say annotations do too. This
# document is credible precisely because it volunteers this class of fact
# unprompted — an omission the reader finds themselves retroactively reframes
# every volunteered fact as selective rather than honest, and for Snowflake
# specifically this is the one their reviewer finds.
# ---------------------------------------------------------------------------


def _scrubber_limits() -> str:
    """§6's first limit, where the business-data claim already lives.

    Scoped like `_injection_disclosure` above: annotations are mentioned in §3
    and §5 as well, so a whole-file assertion passes while the LIMIT is silent
    — and the limit is the paragraph a reviewer reads as the honest scope."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text()
    start = doc.index("1. **Business data is not scrubbed.**")
    return doc[start : doc.index("2. **", start)]


def _annotate_branch() -> str:
    """The proxy's `baton_annotate` branch alone.

    Scoped, because the reader below used to run its regex over the whole
    module: every `args.get(...)` in `proxy.py`, from any handler. The pin it
    feeds asserts that one named field is the one the annotation records, and a
    whole-file read satisfies that as soon as anything anywhere happens to read
    a field of the same name — green while the disclosure it guards has gone
    stale, which is the failure shape this file keeps finding."""
    src = (REPO_ROOT / "src" / "baton_proxy" / "proxy.py").read_text(encoding="utf-8")
    start = src.index("if tool_name == ANNOTATE_TOOL_NAME:")
    return src[start : src.index("_handle_injected_call(", start)]


def test_the_annotate_slice_is_the_annotate_branch_and_stops_there():
    """Guard against the guard. A slice that drifted wide would restore exactly
    the looseness the scoping removed, and every assertion below would stay
    green while checking the whole module again."""
    branch = _annotate_branch()
    assert "enqueue_annotation(" in branch, "the slice is not the annotate branch"
    assert "def " not in branch, f"the slice ran on into another function:\n{branch[-400:]}"


def _annotate_argument_names() -> set[str]:
    """The argument names the proxy reads off a `baton_annotate` call — the
    fields whose contents are prose the model composed."""
    return set(re.findall(r'args\.get\("([a-z_]+)"\)', _annotate_branch()))


def test_the_field_the_run_put_business_data_into_is_still_called_context():
    """Guard against the guard, and a drift pin: the disclosure below names a
    field, so the field has to be the one the proxy actually records."""
    names = _annotate_argument_names()
    assert "context" in names, f"the annotate call no longer reads `context`: {sorted(names)}"


def test_security_md_discloses_that_annotations_restate_the_results():
    """The disclosure itself, in the limit that already carries its half of the
    claim. Pinned as prose because prose is what it is: nothing else in this
    repo makes the statement, and a reader decides on it."""
    limit = _scrubber_limits()
    for token in ("annotation", "`context`", "twice"):
        assert token in limit, f"§6's business-data limit never says {token!r}:\n{limit}"
    assert "model" in limit, "the disclosure does not say who wrote the prose"
    # The claim, not the arithmetic: it said the scrubber found "zero" of them
    # until 2026-09-04 and now says why there is nothing to find. Either way the
    # limit has to close on the scrubber not catching this, or naming the second
    # copy reads as naming something handled.
    assert "not a pattern it matches" in _flat(limit), (
        "it no longer says the scrubber does not catch the second copy"
    )


def test_section_5_says_the_same_thing_where_intent_is_listed():
    """The other sink. §5 is the field-by-field list, and someone auditing what
    is recorded reads it rather than §6's limits."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text()
    start = doc.index("- **Intent**:")
    bullet = doc[start : doc.index("\n\n", start)]
    assert "restate" in bullet or "quote" in bullet, (
        f"§5's intent bullet does not say annotations can carry results:\n{bullet}"
    )


@_BOTH_MODES
def test_a_clobbered_wrap_wins_over_the_nothing_called_it_row(tmp_path, kit_home, capsys, in_place):
    """Row 3 is checked only when the file is EMPTY, which is one case too few.

    Setup runs, a session starts and records its tool-surface snapshot, the wrap
    then goes (see `_clobber` for how, per mode), and every call after that goes
    to the unwrapped server. The file is no longer empty, so row 3 was never
    consulted and row 5 fired instead: an affirmative diagnosis naming two
    causes, neither of which is true, sending the person to `/mcp` to hunt a
    duplicate that does not exist. The old code printed bare counts here, so
    this is worse than what it replaced."""
    key = "/Users/someone/work/app"
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key, in_place=in_place)
    _write_events(kit_home, ("bee5d1a2", 0))
    _clobber(path)
    out = _receipt_output(capsys)
    assert _fired(out) == ["THE WRAP IS GONE"], _fired(out)


@_BOTH_MODES
def test_a_wrap_clobbered_after_a_real_capture_still_says_so(tmp_path, kit_home, capsys, in_place):
    """The same row, with calls in the file. Capture STOPPED, which is the fact
    worth saying, and it is invisible in a total that only ever grows."""
    key = "/Users/someone/work/app"
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key, in_place=in_place)
    _write_events(kit_home, ("d1e2f3a4", 2))
    _clobber(path)
    out = _receipt_output(capsys)
    assert _fired(out) == ["THE WRAP IS GONE"], _fired(out)
    assert _counts_shown(out), "what was captured before it broke still counts:\n" + out


# --- Review findings: `scope is None` is not the same claim as "global" ------
#
# `iter_entries` returns scope None for the TOP LEVEL of whatever file was
# read, and `search_paths`' own docstring says `--src-config` is how a project
# config is reached. So a `.mcp.json` passed with `--src-config` produces
# scope None — and "registered globally, so it loads wherever you start from"
# is then false in the one direction that costs a trial: a `.mcp.json` loads for
# sessions started in its own directory and nowhere else. That is the
# empty-capture-with-an-invisible-cause failure, re-entered through the
# --src-config door.


def _mcp_json(tmp_path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    path = project / ".mcp.json"
    path.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")
    return path


def test_a_project_config_file_is_not_described_as_loading_everywhere(tmp_path, kit_home, capsys):
    path = _mcp_json(tmp_path)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "loads wherever you start from" not in out, out
    assert str(path.parent) in out, f"the directory that file belongs to is not named:\n{out}"


@_BOTH_MODES
def test_the_checklist_asks_the_directory_question_for_a_project_config_file(
    tmp_path, kit_home, capsys, in_place
):
    """The receipt's half of the same claim: someone whose file is empty gets
    the checklist, and for a non-global config the directory question is the one
    that resolves it.

    The source here is a `.mcp.json` of THEIRS, reached through `--src-config`.
    In place, the wrap stays in it and the directory is its own folder. In
    project mode the entry is copied out, so the directory is our checkout —
    naming their folder would be the wrong-directory bug with an extra step."""
    path = _mcp_json(tmp_path)
    args = ["setup", "notion", "--src-config", str(path), "--tenant", "t"]
    if in_place:
        args.append("--in-place")
    assert kit.main(args) == 0
    capsys.readouterr()
    out = _receipt_output(capsys)
    checklist = out[out.index("No events have been captured yet") :]
    loads_in = str(path.parent) if in_place else str(kit.MCP_PATH.parent)
    # Scoped to the step — see the sibling above for why the looser form lies.
    assert _directory_question(checklist) == loads_in, (
        f"the directory question names the wrong place:\n{checklist}"
    )


def test_the_global_claim_survives_for_the_config_that_is_actually_global(
    tmp_path, kit_home, capsys, monkeypatch
):
    """The control. `~/.claude.json` IS loaded from everywhere, and the sentence
    saying so is the right one there — a fix that made every wrap directory-bound
    would be the same defect pointing the other way."""
    home = tmp_path / "home"
    home.mkdir()
    path = home / ".claude.json"
    path.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    assert (
        kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t", "--in-place"]) == 0
    )
    out, _err = capsys.readouterr()
    assert "loads wherever you start from" in out, out
    assert "cd " not in out, out


@_BOTH_MODES
def test_a_project_path_with_a_space_is_handed_over_as_a_runnable_command(
    tmp_path, kit_home, capsys, monkeypatch, in_place
):
    """`cd /Users/x/Google Drive/app && claude` cds to `/Users/x/Google` and
    starts the client in the wrong directory — which loads global scope and
    captures nothing, the exact failure this line was added to prevent. Parsed
    with the shell's own rules rather than string-matched, so the assertion is
    that the command WORKS, not that it looks quoted.

    ⚠ Project mode does NOT retire this case, it moves it. The quoted path
    becomes OUR checkout, and a checkout is somewhere the person chose — the
    paste tells them to clone "into the directory I'm in", which on macOS is
    routinely `~/Google Drive/...` or `~/Client Work/...`. So the project row
    redirects `kit.MCP_PATH` into a directory with a space rather than skipping.
    Without that it would assert quoting against a tmp path that has none, and
    pass whether or not the kit quotes anything."""
    import shlex

    key = str(tmp_path / "Google Drive" / "app")
    path = _project_config(tmp_path, key)
    args = ["setup", "notion", "--src-config", str(path), "--tenant", "t"]
    if in_place:
        args.append("--in-place")
    else:
        spaced = tmp_path / "Client Work" / "baton-proxy"
        spaced.mkdir(parents=True)
        monkeypatch.setattr(kit, "MCP_PATH", spaced / ".mcp.json")
    assert kit.main(args) == 0
    out, _err = capsys.readouterr()
    line = next(ln for ln in out.splitlines() if "&& claude" in ln)
    argv = shlex.split(line)
    loads_in = key if in_place else str(kit.MCP_PATH.parent)
    assert argv[:2] == ["cd", loads_in], f"the handed-over command cds elsewhere: {argv}"
    assert " " in loads_in, "the case is only a quoting test if the path has a space"


# --- Review finding: a call is not only a tool call -------------------------
#
# The proxy emits `resource_read_start`, `resource_list_start`,
# `prompt_get_start` and `prompt_list_start` as well. A session that reached the
# server that way had zero `tool_call_start` events, so it was reported as dead
# — and the diagnosis sent the person to hunt a duplicate server for traffic the
# proxy demonstrably captured. Rare, and a wrong answer rather than a missing
# one, which is the kind this receipt is being rebuilt to stop giving.

_OTHER_START = "resource_read_start"


def _resource_session(sid: str, reads: int, hour: int = 12) -> list[dict]:
    out = [
        {
            "event_type": "surface_snapshot",
            "session_id": sid,
            "captured_at": f"2026-08-30T{hour:02d}:00:00Z",
            "payload": {"tools": [{"name": "search"}]},
        }
    ]
    for i in range(reads):
        out.append(
            {
                "event_type": _OTHER_START,
                "session_id": sid,
                "captured_at": f"2026-08-30T{hour:02d}:{i + 1:02d}:00Z",
                "payload": {"uri": "file:///doc.md", "duration_ms": 4},
            }
        )
    return out


def _write_raw(kit_home, rows: list[dict]) -> None:
    (kit_home / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_a_session_that_only_read_resources_is_not_called_dead(tmp_path, kit_home, capsys):
    _wrapped(tmp_path, kit_home, capsys)
    _write_raw(kit_home, _resource_session("c0ffee01", 3))
    out = _receipt_output(capsys)
    assert _fired(out) == [], f"a session that reached the server was called dead:\n{out}"
    # "run /mcp", not "/mcp": the header prints a config path ending mcp.json.
    assert "run /mcp" not in out, "it was sent to hunt a duplicate for traffic we captured"
    assert "3" in out, f"the reads it did make are not reported at all:\n{out}"


def test_a_resource_only_session_beside_a_calling_one_raises_no_note(tmp_path, kit_home, capsys):
    """The note's own claim is that a row reading `0 calls` may mean another
    server took them. A row with resource reads in it means the opposite."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_raw(kit_home, _session_events("d1e2f3a4", 2) + _resource_session("c0ffee01", 1))
    out = _receipt_output(capsys)
    assert "run /mcp" not in out, f"a live session was diagnosed as a dead one:\n{out}"


def test_a_session_with_nothing_in_it_at_all_is_still_diagnosed(tmp_path, kit_home, capsys):
    """The control: widening what counts as activity must not switch the row off."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_events(kit_home, ("bee5d1a2", 0))
    assert _fired(_receipt_output(capsys)) == [NOTHING_CALLED_MARKER]


def test_uninstall_does_not_promise_a_restore_it_could_not_verify(
    tmp_path, kit_home, capsys, monkeypatch
):
    """Review finding. The unverified branch prints "the entry on disk does not
    match what setup recorded… Compare by hand" — and then printed "New sessions
    will use your original server again", which is the claim the line above just
    withdrew. The note it replaced was neutral about what would load, so this
    was introduced by the rewrite, in the one output where being wrong is
    expensive: the person is being asked to check a config by hand.

    `--in-place` since K1b: there is nothing to restore on the default path, so
    `restored_matches_on_disk` is only ever consulted for a wrap that edited
    their config. This is the only mode that can reach the branch."""
    _wrapped(tmp_path, kit_home, capsys, in_place=True)
    monkeypatch.setattr(kit, "restored_matches_on_disk", lambda *_a, **_k: False)
    assert kit.main(["uninstall"]) == 0
    out, _err = capsys.readouterr()
    assert "WARNING" in out
    assert "original server again" not in out, f"it promised what it could not check:\n{out}"
    assert (kit_home / "state.json").exists(), "the unverified branch still keeps the record"


def test_uninstall_names_the_checkout_and_says_nothing_was_installed(tmp_path, kit_home, capsys):
    """First human-led run, P1. `uninstall` listed what it left behind and
    stopped — no line saying the folder is still there, that deleting it
    removes everything, or that nothing was installed. `CLAUDE.md` tells the
    agent to say it; the kit did not print it, so the fact reached the person
    only if the agent happened to remember. The person running `uninstall` is
    usually the one leaving, and "how do I get this off my machine" is the
    question in their head at that moment."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    assert kit.main(["uninstall"]) == 0
    out, _err = capsys.readouterr()
    assert "Nothing was installed" in out, f"uninstall never says it:\n{out}"
    assert str(kit.CHECKOUT) in out, f"the folder is never named:\n{out}"
    assert "deleting that folder removes all of it" in out


def test_uninstall_says_what_the_backups_it_leaves_behind_hold(tmp_path, kit_home, capsys):
    """A security review of our own documents found the leftover list said the
    backups were there and not what was in them: a verbatim copy of the whole
    config, literal credentials included. The decision was to keep them, as
    evidence of a write we cannot see, and make the header honest. So this pins
    the words and the behaviour together: the backup is named under the header
    and is still on disk, and `state.json` is deleted exactly as before.

    `--in-place` since K1b: only that mode leaves a `config-backup.*` behind,
    so only that mode owes the sentence saying what one holds."""
    _wrapped(tmp_path, kit_home, capsys, in_place=True)
    (backup,) = kit_home.glob("config-backup.*.json")
    assert kit.main(["uninstall"]) == 0
    out, _err = capsys.readouterr()
    header = (
        "Deliberately left in place. `config-backup.*` is a full copy of your config, "
        "every server's credentials included:"
    )
    assert header in out, f"the leftover list does not say what the backups hold:\n{out}"
    assert out.index(header) < out.index(str(backup)), "the backup is not listed under the header"
    assert backup.exists(), "uninstall deleted the backup; the decision was to keep it"
    assert not (kit_home / "state.json").exists(), "uninstall stopped deleting state.json"


def test_the_unverified_branch_does_not_tell_you_to_delete_the_record(
    tmp_path, kit_home, capsys, monkeypatch
):
    """The same fact, and the opposite advice. On the unverified path
    `state.json` is deliberately KEPT as the only record of the original entry,
    so "delete the folder and you are done" would talk someone into destroying
    their recovery record one line under a warning that the restore did not
    match. Nothing-was-installed is still true and still said; what changes is
    the instruction.

    `--in-place` since K1b, for the same reason as its twin above: `cmd_uninstall`
    returns through `_finish_uninstall(verified=True)` before
    `restored_matches_on_disk` is ever called in project mode, so the branch this
    monkeypatch reaches for is unreachable there. Project mode has no restore to
    leave unverified — it deletes a file of ours instead of rewriting one of
    theirs."""
    _wrapped(tmp_path, kit_home, capsys, in_place=True)
    monkeypatch.setattr(kit, "restored_matches_on_disk", lambda *_a, **_k: False)
    assert kit.main(["uninstall"]) == 0
    out, _err = capsys.readouterr()
    assert "Nothing was installed" in out
    assert str(kit.CHECKOUT) in out
    assert "deleting that folder removes all of it" not in out, (
        f"it invited deletion of the kept record:\n{out}"
    )
    assert "Not yet" in out
    # Scoped to the note's OWN text. Asserting the path against the whole
    # output passes on the WARNING line printed further up, which names
    # `state.json` too — so a note that stopped interpolating it stayed green.
    note = kit.checkout_note(verified=False)
    assert note in out
    assert str(kit.STATE_PATH) in note, f"the note does not name the record it protects:\n{note}"
    assert (kit_home / "state.json").exists()


# ---------------------------------------------------------------------------
# The ending (email 2026-08-31, upload 2026-09-01, Setup page 2026-09-08).
#
# The route has changed three times and the property under it never has: the
# trial must not end at a file on a stranger's laptop with no named next move,
# and whatever names that move costs nothing from the security posture because
# THEY do the moving. `CLAUDE.md`'s "Never send the file anywhere" is absolute
# again, and §9.1's grep sees no call site under `try/`: the kit names a page
# and opens nothing.
#
# Two halves, and the second is the one the run showed we get wrong: the receipt
# has to hand over the ending, and SETUP has to say there is one, because once
# they walk away from that window no agent anywhere knows this kit exists, and
# the setup output is the last thing that speaks.
# ---------------------------------------------------------------------------


def test_the_offer_is_withheld_from_a_capture_with_no_calls_in_it(tmp_path, kit_home, capsys):
    """Dave: the "captured" line prints only after the file has been read and
    found to contain calls — never optimistically.

    A handshake-only file is not empty (the surface snapshot is in it), so the
    offer's gate cannot be "are there events". Sending someone to the Baton Proxy page
    with a handshake-only file wastes the one trip they will make, and it argues
    with the banner printed just above, which said nothing came down the pipe."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_events(kit_home, ("bee5d1a2", 0))
    out = _receipt_output(capsys)
    assert _fired(out) == [NOTHING_CALLED_MARKER], f"the diagnosis stopped firing:\n{out}"
    assert kit.SETUP_URL not in out, f"offered to upload a capture with nothing in it:\n{out}"
    assert "It's at" not in out, f"handed over a capture with nothing in it:\n{out}"


def test_a_resource_only_capture_is_still_worth_uploading(tmp_path, kit_home, capsys):
    """The gate has to count what `summarize` counts. A session that only read
    resources reached the server and produced real data; gating the offer on
    `tool_calls` alone would withhold it from a capture worth having — the same
    defect as calling that session dead, one branch further on."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_raw(kit_home, _resource_session("c0ffee01", 3))
    out = _receipt_output(capsys)
    assert kit.SETUP_URL in out, f"a real capture was given no way out:\n{out}"


@_BOTH_MODES
def test_the_offer_survives_a_wrap_that_was_clobbered_after_capturing(
    tmp_path, kit_home, capsys, in_place
):
    """Capture STOPPED, but what was captured before it stopped is real and is
    the whole reason to upload anything. The banner says the wrap is gone; the
    closing block still has to hand over the file."""
    key = "/Users/someone/work/app"
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key, in_place=in_place)
    _write_events(kit_home, ("d1e2f3a4", 2))
    _clobber(path)
    out = _receipt_output(capsys)
    assert _fired(out) == ["THE WRAP IS GONE"], _fired(out)
    assert kit.SETUP_URL in out, f"a real capture lost its ending to the banner:\n{out}"


def test_setup_hands_over_the_ending_before_the_window_goes_quiet(tmp_path, kit_home, capsys):
    """The structural half. Once they walk away from this window there is no
    agent left that knows the kit is here and nothing in their new session
    mentions Baton, so the ending is given to them here or not at all."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "kit.py receipt" in out, f"setup never says how to come back:\n{out}"
    # The destination is not in this note, because at setup time there is
    # nothing to hand over and the receipt states the ending at the moment there
    # is. What setup still owes them is the SHAPE of the ending, which is what
    # this asserts.
    assert "How the trial ends" in out, f"setup never says how the trial ends:\n{out}"
    assert "kit.py uninstall" in out, f"setup's ending never says how to switch it off:\n{out}"


def test_the_ending_setup_hands_over_does_not_claim_a_file_exists_yet(tmp_path, kit_home, capsys):
    """At setup time nothing has been captured and nothing may ever be. The line
    is a conditional about what they will find, not a promise that there is
    something to send — the same optimism the receipt's gate exists to stop, one
    step earlier and harder to notice."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    tail = out[out.index("How the trial ends") :]
    # It used to carry the send path under an "if there is something in it"
    # conditional. It now carries no destination at all, which satisfies the
    # same property harder: the ending belongs to `receipt`, which runs when the
    # answer is known. So the pin is the deferral, and the absence of an offer
    # made before there is anything to offer.
    assert "kit.py receipt" in tail, f"setup's ending defers the ending to nothing:\n{tail}"
    assert kit.SETUP_URL not in tail, (
        f"setup names the upload page before there is anything to upload:\n{tail}"
    )
    assert "events.jsonl" not in tail, f"setup names a capture file that may never exist:\n{tail}"


def test_the_already_wrapped_path_hands_over_the_ending_too(tmp_path, kit_home, capsys):
    """Cold re-entry is the normal case on a multi-day trial, and it is exactly
    the person who has lost the window that carried the ending the first time."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    assert kit.main(["setup", "notion", "--src-config", str(path)]) == 0
    out, _err = capsys.readouterr()
    assert "Already wrapped" in out
    assert "How the trial ends" in out, f"the re-entry hands over no ending:\n{out}"


def test_the_doc_and_the_kit_say_the_same_sentence():
    """Same shape as §4's injected-param pin, and the same failure it caught: a
    document naming a different place than the code prints is wrong in the one
    place a person acts on it, and every test stays green.

    The ending is written twice on purpose. `receipt` prints it with the real
    path, and `CLAUDE.md` quotes it with a placeholder so the agent knows what
    to relay, so this is the assertion that they are one sentence. Compared as a
    whole line rather than on the URL alone: a doc that keeps the address and
    loses "(less that path to read it)" has dropped the only reading advice the
    person is given, and no other test would notice."""
    doc = _claude_md()
    quoted = _flat(kit.setup_note(Path("/full/path/to/try/events.jsonl")))
    assert quoted in _flat_unquoted(doc), (
        f"CLAUDE.md does not quote the line the receipt prints:\n  {quoted}"
    )
    urls = set(re.findall(r"https?://[^\s`>,)]+", doc)) - {kit.SETUP_URL}
    assert not urls, f"CLAUDE.md names another destination: {urls}"
    addresses = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", doc))
    assert addresses <= {"security@goodtiming.ai"}, (
        f"CLAUDE.md names an address a capture could be sent to: {addresses}"
    )


def test_uninstall_is_no_longer_the_close():
    """`Ending it` used to finish "Offer `uninstall` and leave it there" — which
    switches off our own sensor at the moment it first produced something worth
    seeing, and reads to the person as though declining to send ended the trial.
    Uninstall is the exit: always available, offered on request, never suggested
    after a good capture.

    Pinned on the section rather than the file, because the doc must still say
    how to remove the kit — one section down, where it belongs."""
    doc = _claude_md()
    ending = doc[doc.index("## Ending it") : doc.index("## Removing it")]
    assert "uninstall" not in ending, f"`Ending it` still closes on uninstall:\n{ending}"
    assert "leave it there" not in ending


def test_the_doc_says_the_trial_can_be_ended_more_than_once():
    """ "I'm done" is a statement about the DATA, not the machine. Nothing is torn
    down when they say it, the wrap is a permanent edit until `uninstall`, and
    saying it again on a longer trial is ordinary rather than a mistake. The doc
    has to say so: an agent reading the old text has no reason to think the loop
    is available."""
    doc = _claude_md()
    ending = doc[doc.index("## Ending it") : doc.index("## Removing it")]
    assert "again" in ending.lower(), f"`Ending it` never says it can be said twice:\n{ending}"


# ---------------------------------------------------------------------------
# Review of the email-ending commit: the doc still denies a capture the receipt
# now offers to send.
#
# `wrap_is_gone` has always had two readings — an empty file means nothing ever
# passed through, a file with counts means capture STOPPED — and both the
# marker row and `Ending it` flattened them into the empty one. That was a
# reporting flaw before this commit and is a contradiction after it: with counts
# in the file the receipt prints the banner AND the gzip command AND the
# address, while an agent narrating from the doc says nothing was captured and
# reaches for uninstall. Which is the close this commit removed, reappearing at
# a site the `Ending it` slice cannot see.
# ---------------------------------------------------------------------------


def _doc_section(start: str, end: str) -> str:
    doc = _claude_md()
    return doc[doc.index(start) : doc.index(end)]


def _marker_row(marker: str) -> str:
    """One bullet of the routing list, from its marker to the next bullet.

    Re-pointed 2026-09-04: the list lost its "exactly one of these six lines"
    preamble and its quotes around each banner, and now runs to the end of the
    section rather than to `## Setting up`. The bullet it returns is the same
    bullet ([[the doc is the spec, so the marker moves, not the check]]).
    """
    table = _doc_section("## Start by finding out where you are", "## If they asked")
    start = table.index(f"**{marker}**")
    nxt = table.find("\n- **", start)
    return table[start : nxt if nxt != -1 else len(table)]


def test_the_wrap_is_gone_row_does_not_deny_a_capture_that_happened():
    """The row asserted the empty reading unconditionally. `wrap_is_gone` does
    not: with events it says what was counted "was captured before that", and
    the receipt goes on to print the send offer underneath it."""
    row = _marker_row("THE WRAP IS GONE")
    assert "nothing has been passing through" not in row, (
        f"the row states the empty reading as though it were the only one:\n{row}"
    )
    assert "counts above the banner are real" in _flat(row).lower(), (
        f"the row never says a capture may predate the clobber:\n{row}"
    )


def test_the_ending_splits_a_clobbered_capture_from_an_empty_one():
    """They print different things and want different answers. Grouping them
    sent a real capture to a checklist that row does not print, and dropped the
    offer the receipt did print."""
    ending = _flat(_doc_section("## Ending it", "## Removing it"))
    # 2026-09-04: the three quiet rows share one branch again, which is fine —
    # they share an ANSWER (relay the banner) and the grouping was never the
    # defect. What was, and what this still pins, is the exception travelling
    # with them: a clobbered capture is real, so it gets the ending rather than
    # a checklist the row does not print.
    assert "when the wrap is gone the counts above the banner are real" in ending, (
        f"the clobbered capture is handled as an empty one again:\n{ending}"
    )
    assert "so end as above" in ending, (
        f"the wrap-gone case keeps the checklist and loses the ending:\n{ending}"
    )


def test_setups_come_back_line_follows_the_kit_directory_under_test(tmp_path, kit_home, capsys):
    """`kit_home` monkeypatches `kit.TRY_DIR`, which a module-level f-string
    freezes past. Production is unaffected — `TRY_DIR` comes off `__file__` —
    but every setup assertion would then be reading the developer's own
    checkout path, and a future pin on "the come-back line names the kit
    directory" would pass while checking the wrong one."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    # Scoped to the come-back line. `str(kit.TRY_DIR) in out` passes on the
    # `backup:` line above it, which reads TRY_DIR at call time and always did —
    # so the whole-output form is green while the line under test is wrong.
    tail = out[out.index("python3 kit.py receipt") :]
    line = next(ln for ln in tail.splitlines() if ln.startswith("from "))
    assert line == f"from {kit.TRY_DIR}", f"the come-back line names another directory:\n{line}"


def test_security_md_says_the_file_can_leave_and_who_makes_it_leave():
    """`kit.py`'s module docstring makes this document authoritative — "if the
    two ever disagree, the document is the one that is wrong, because a stranger
    approved the trial by reading it". Each time the ending has moved, the exit
    it describes has had to move with it or the document became the wrong one.

    The exit is now entirely outside the kit: a person, signed in, choosing a
    file in a browser. That is the easiest kind of exit to leave undescribed,
    because no code in the checkout implements it. Someone who reads this page,
    finds nothing about where the capture goes, and then meets an upload at the
    end re-reads the whole page as a setup for the ask. The document
    works because it volunteers."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text()
    section = doc[doc.index("## 4. What leaves your machine") : doc.index("## 5. What is recorded")]
    flat = _flat(section)
    assert "unless you upload it" in flat, "§4 never names the one way the capture leaves"
    assert "you sign in to Baton in your own browser and choose the file yourself" in flat, (
        "§4 never says who makes the capture leave, or how"
    )
    # The half a reader checks the code against: not "we do not send it" but
    # "there is nothing here that could", which is §9.1's grep in prose.
    assert "there is no upload command" in flat, (
        "§4 stopped saying the kit has no way to send the capture at all"
    )
    assert "the capture stays on your disk" in flat, (
        "§4 never says what happens to the file when they do not upload it"
    )
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", section), (
        "§4 names an address a capture could be sent to"
    )


# ---------------------------------------------------------------------------
# TK-D-5 — nothing asks them to name a tenant (Dave's run, 2026-08-28, item 8).
#
# Setup asked for a `--tenant` label and CLAUDE.md told the agent to offer one.
# He does not have a tenant. He is running a local, no-account trial where
# nothing is authenticated and nothing leaves, and being asked to name a tenant
# invites exactly the thought the kit exists to prevent — "wait, am I signing up
# for something?" — arriving at the moment we are trying to prove otherwise.
#
# The answer was already on hand: he picked a server by name one step earlier,
# and that name is how he refers to it. So the default is the server's name and
# the question is gone. Both labels reading the same is fine; neither is
# authenticated, and `SECURITY.md` §5 already says so.
#
# `--tenant` itself survives as an override — `baton-internal/harness/kit_run.sh`
# and `spikes/http_entry_wrap/run_kit_bridge_e2e.sh` both pass it to tell their
# runs apart. What is banned is ASKING, not the flag, so the sweep is over the
# prose and over the documented commands, not over the argparse declaration.
# ---------------------------------------------------------------------------

# The ASK forms. A document is allowed to say "do not ask them to name a
# tenant" — that sentence is the fix — so a prohibition is not an offender, and
# `_A_PROHIBITION` is the honest statement of that hole rather than a silent
# carve-out ([[feedback_invariant_scoped_to_one_field]]: a guard is scoped too).
_TENANT_ASK = re.compile(
    r"(?i)"
    r"(?:offer|ask\w*|suggest|prompt|invite|request)[^.\n]{0,40}\b(?:tenant|label)\b"
    r"|what (?:tenant|label)"
    r"|\b(?:tenant|label)\b[^.\n]{0,30}(?:do you want|would you like|of their choice)"
)

# Bare `not` is deliberately absent: "it is not offered by default, but you can
# offer them a tenant label" would exempt itself on it.
_A_PROHIBITION = re.compile(r"(?i)\b(?:do not|don't|never)\s+(?:ask|offer|suggest|prompt)")


def _sentences(para: str) -> list[str]:
    """A paragraph's sentences, tolerating the bold markers these docs wrap
    around them (`label.**` ends a sentence as much as `label.` does).

    The unit matters: the exemption below is per SENTENCE, not per paragraph.
    These docs collapse blank-line-free bullet lists into single paragraphs —
    one of them is 1,977 characters — so a paragraph-scoped carve-out would
    blanket everything sharing a paragraph with one prohibition, INCLUDING the
    step the fix sentence lives in, which is the likeliest place for the ask to
    be re-added ([[feedback_invariant_scoped_to_one_field]]: the guard is scoped
    too, and its scope is invisible the moment it passes)."""
    return [x for x in re.split(r"(?<=[.!?])[*_`)\]]*\s+", para) if x.strip()]


# CLAUDE.md is what tells the agent to ask; kit.py's strings are what the person
# reads. SECURITY.md is swept too — it describes the entry the wrap writes, and
# an example there is a claim about what setup does.
_TENANT_SINKS = ("try/CLAUDE.md", "try/SECURITY.md", "try/PROMPT.md")


def _unwrapped(text: str):
    """Paragraphs, each rejoined onto one line, with the line it starts at.

    Not cosmetic. These docs are hard-wrapped at ~79 columns, and the ask this
    sweeps for was FOUR lines long — "Offer a label" on one, "Suggest something
    like their / company or team name" split across two more. A line-based
    sweep sees a fragment of an instruction and matches none of it, which is how
    a guard passes while the thing it names is still on the page.
    """
    para: list[str] = []
    start = 1
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not para:
                start = n
            para.append(line.strip())
            continue
        if para:
            yield start, " ".join(para)
            para = []
    if para:
        yield start, " ".join(para)


def test_no_text_the_trial_shows_asks_them_to_name_a_tenant():
    offenders = []
    for rel in _TENANT_SINKS:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for n, para in _unwrapped(text):
            for sentence in _sentences(para):
                if _TENANT_ASK.search(sentence) and not _A_PROHIBITION.search(sentence):
                    offenders.append(f"{rel}:{n}: {sentence[:120]}")
    for n, s in _kit_strings():
        for sentence in _sentences(s):
            if (m := _TENANT_ASK.search(sentence)) and not _A_PROHIBITION.search(sentence):
                offenders.append(f"try/kit.py:{n}: …{m.group(0)}…")
    assert not offenders, "the kit still asks someone to name a tenant:\n" + "\n".join(offenders)


# Verbatim from the prose this removed, plus the phrasings a rewrite would
# reach for. A regex that matches nothing passes forever
# ([[feedback_control_condition_must_be_able_to_fail]]).
_THE_ASK_AS_IT_WAS_WRITTEN = (
    # The step this removed, verbatim and wrapped exactly as it sat in
    # CLAUDE.md — so the sweep is graded on the shape it actually has to catch.
    """**3. Offer a label.** `--tenant` is a plain string that tags the events so the
file can be told apart from anyone else's later. Suggest something like their
company or team name. Nothing is authenticated by it; it is a label, and the
default is a random one if they would rather not.""",
    "Ask them for a label to tag the events with.",
    "Suggest a tenant name — their company or team.",
    "What label do you want on this trial?",
    "Prompt them for a tenant id before running setup.",
)

# Sentences the fix is made of, and the vocabulary that has to stay writable:
# the flag still exists, SECURITY.md still explains what the labels are, and the
# receipt still prints them.
_TRUE_TENANT_SENTENCES = (
    "**Do not ask them to name a tenant or a label.**",
    "The events are tagged with the server's own name, which they already picked.",
    "labels         : tenant=notion vendor=notion",
    "id, type, session id, sequence number, timestamp, and the tenant/vendor labels",
    "Nothing is authenticated by either label.",
)


@pytest.mark.parametrize("line", _THE_ASK_AS_IT_WAS_WRITTEN)
def test_the_tenant_sweep_would_notice_the_phrasings_it_was_written_for(line):
    # Through `_unwrapped` and `_sentences`, because that is how the sweep sees
    # the page: wrapped lines rejoined, then split at sentence boundaries.
    para = next(text for _n, text in _unwrapped(line))
    caught = [x for x in _sentences(para) if _TENANT_ASK.search(x) and not _A_PROHIBITION.search(x)]
    assert caught, f"the sweep would have missed {line!r}"


@pytest.mark.parametrize("line", _TRUE_TENANT_SENTENCES)
def test_the_tenant_sweep_leaves_the_true_sentences_alone(line):
    unmatched = not _TENANT_ASK.search(line)
    assert unmatched or _A_PROHIBITION.search(line), (
        f"the sweep rejects its own replacement: {line!r}"
    )


def test_the_prohibition_exempts_its_own_sentence_and_not_its_paragraph():
    """The carve-out's own limit, pinned. Step 3 states the rule and then
    explains it, all in one paragraph — so a paragraph-scoped exemption would
    make the step that says "do not ask" the one place an ask could be added
    invisibly."""
    para = (
        "**Do not ask them to name a tenant or a label.** The events are tagged "
        "with the server's own name. If they would rather, offer a label of "
        "their own."
    )
    caught = [x for x in _sentences(para) if _TENANT_ASK.search(x) and not _A_PROHIBITION.search(x)]
    assert len(caught) == 1, f"expected only the re-added ask, got {caught}"
    assert "offer a label" in caught[0]

    # And the fix sentence alone stays exempt, or the sweep bans its own remedy.
    only_the_rule = _sentences("**Do not ask them to name a tenant or a label.**")
    assert not [x for x in only_the_rule if _TENANT_ASK.search(x) and not _A_PROHIBITION.search(x)]


def test_no_documented_command_passes_the_tenant_flag():
    """The mechanical half, and the one that cannot be argued with. `--tenant`
    in a command on the page is an instruction to supply one however the
    surrounding prose is worded — and it is the form the agent copies."""
    offenders = [
        f"{where}: {' '.join(argv)}"
        for where, argv in _documented_kit_commands()
        if "--tenant" in argv
    ]
    assert not offenders, "a documented command still asks for a tenant:\n" + "\n".join(offenders)


def test_setup_with_no_tenant_labels_the_events_with_the_server_name(tmp_path, kit_home, capsys):
    """The behaviour the removed question was paying for. It used to default to
    `trial-<random hex>`, which is unattributable on our side and meaningless on
    theirs — so skipping the question was a real cost, and naming it was the
    reason to ask. The server name settles both."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path)]) == 0
    out, _err = capsys.readouterr()

    # See the sibling below: the labels live on the entry setup wrote, which
    # project mode puts in `kit.MCP_PATH`.
    entry = _wrapped_entry("notion")
    assert entry["env"]["BATON_TENANT_ID"] == "notion"
    assert entry["env"]["BATON_VENDOR_ID"] == "notion"
    assert "trial-" not in out, f"a random trial label is still being minted:\n{out}"


def test_security_md_says_the_labels_authenticate_nothing():
    """The deleted CLAUDE.md step held the ONLY sentence in the shipped kit
    saying so, and dropping it made the docs quieter in the direction that
    matters: the config examples now print the customer's own server name as
    `BATON_TENANT_ID`, which reads more like an identity than `trial-4f2a9c11`
    did, not less. So the disclosure moves to the document a reviewer reads
    rather than disappearing with the step that used to carry it."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8")
    section = doc[doc.index("## 5. What is recorded") : doc.index("## 6. What the scrubber")]
    assert "nothing checks them against anything" in _flat(section), (
        "§5 never says the tenant/vendor labels check nothing — and no other "
        "shipped file does either since the setup step was removed"
    )


def test_the_tenant_flag_still_works_for_the_rigs_that_pass_it(tmp_path, kit_home, capsys):
    """`kit_run.sh` and `run_kit_bridge_e2e.sh` pass `--tenant` to tell their own
    runs apart, and TK-F-8/9 assert every landed event carries it. Removing the
    question must not remove the override."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--src-config", str(path), "--tenant", "t2-kit-run"]) == 0
    # `_wrapped_entry`, not `path`, since K1b: the labels are on the entry setup
    # WROTE, and project mode writes it to `kit.MCP_PATH` and leaves the source
    # untouched. Reading `path` here returned their original, unlabelled entry.
    entry = _wrapped_entry("notion")
    assert entry["env"]["BATON_TENANT_ID"] == "t2-kit-run"
    assert entry["env"]["BATON_VENDOR_ID"] == "notion", "the override must not move vendor too"


# ---------------------------------------------------------------------------
# TK-D-6 — the consent screen that is not ours (Dave's run, 2026-08-28, item 8
# and spec §9).
#
# In that run a wrapped server popped a browser tab mid-session reading "Grant
# localhost:9553 access to Notion". It is legitimate — the server holds its own
# sign-in session and treated its first wrapped start as a new one — and it is
# unexplainable at a glance. To someone watching a Baton trial it reads as our
# tool authorizing itself against a third party, and the timing makes it look
# like ours even though the server was theirs all along. That ends a security
# conversation on the spot, with no opportunity to explain afterward.
#
# It cannot be detected: a stdio server's own OAuth session is invisible in the
# config. So the fix is disclosure in both registers — SECURITY.md §2 for the
# person who reads before approving, CLAUDE.md for the agent who has to say it
# BEFORE it happens rather than explain it after.
#
# And the claim the disclosure rests on is mechanical, so it is pinned as one:
# the port on that screen belongs to their server, because we never open one.
# ---------------------------------------------------------------------------

# Constructs that would open a listening port. `socket` is deliberately absent
# from this list as a bare word — it appears in SECURITY.md §9's own grep
# command, and matching that would make this test fail on the document that
# proves it.
_A_LISTENER = re.compile(
    r"socket\.socket|\.bind\(|\.listen\(|serve_forever|HTTPServer|socketserver"
    # `socket.create_server`, `asyncio.start_server`, `loop.create_server` —
    # none of them contain `socket.socket`, and an OAuth callback helper is
    # likelier to be written with the asyncio pair than with the raw module.
    r"|create_server\(|start_server\(|create_unix_server\("
)


def test_nothing_of_ours_opens_a_listening_port():
    """The load-bearing half of the OAuth disclosure. §2 tells someone the
    `localhost` port on that consent screen is their own server's and not ours,
    which is only true while this holds — and it is the kind of claim that goes
    quietly false the day someone adds a callback helper."""
    offenders = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")) + [KIT_PATH]:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if m := _A_LISTENER.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}: …{m.group(0)}…")
    assert not offenders, "SECURITY.md §2 says we open no listening port; this does:\n" + "\n".join(
        offenders
    )


def test_the_listener_sweep_can_fail():
    """[[feedback_control_condition_must_be_able_to_fail]] — the assertion above
    is an absence, and an absence is what a broken regex also reports."""
    for line in (
        "    srv = socketserver.TCPServer(('127.0.0.1', 0), Handler)",
        "    s.bind(('localhost', 9553))",
        "    httpd = HTTPServer(addr, CallbackHandler)",
        "    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        "    server = await asyncio.start_server(_callback, '127.0.0.1', 0)",
        "    srv = await loop.create_server(factory, '127.0.0.1', 9553)",
        "    s = socket.create_server(('127.0.0.1', 0))",
    ):
        assert _A_LISTENER.search(line), f"the sweep would have missed {line!r}"


def test_security_md_discloses_the_reauthorization_prompt():
    """§2 is "What changes on your machine", and a browser window opening
    unbidden is a change on their machine. The document works because it
    volunteers this class of fact before the reader meets it."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8")
    section = doc[doc.index("## 2. What changes") : doc.index("## 3. What your agent sees")]
    assert "signs you in to a third party" in section, "§2 never mentions re-authorization"
    assert "localhost" in section, "§2 does not name what they will actually see"
    assert "no listening port of its own" in section, (
        "§2 asserts the port is theirs without saying why it cannot be ours"
    )


def test_claude_md_tells_the_agent_to_say_it_at_the_handover():
    """The doc and the terminal are two sinks for one claim. Disclosure that
    only exists in a document nobody opened does not stop the surprise — and by
    the time the tab is open there is no good moment to explain it.

    It used to be a question asked before setup, and every answer to it led to
    the same warning. So it is said once, unconditionally, at the handover."""
    md = _claude_md()
    para = next(
        text for _n, text in _unwrapped(md) if "wrapped start may open a browser tab" in text
    )
    assert "Say this once at the handover" in para, "the warning is not tied to a moment"
    assert md.index("**2. Run it.**") < md.index(para[:40]) < md.index("**3. Hand them"), (
        "the warning is not placed at the handover, between running setup and the handoff"
    )
    assert "without asking first" in para and "Ask;" not in para, (
        "the agent is told to ask a question whose every answer leads to the same warning"
    )
    assert "may open" in para, "an agent told to predict this will overstate it"
    assert "Baton never asks for credentials" in para, (
        "the warning no longer says the sign-in is theirs and not ours"
    )


def test_claude_md_tells_the_agent_to_fill_in_the_real_path_on_a_refusal():
    """The refusal paragraph hands the person a command to run themselves, and
    the doc writes that command with a `<path>` placeholder. Relayed literally it
    cannot run, in the one paragraph a security-minded reader studies hardest.
    So the sentence that introduces the line tells the agent to substitute.

    And to say what to do with it. In the terminal the `!` line renders as
    ordinary prose, and someone who has never used `!` does not know it is a
    command to paste at their prompt; the whole handoff depends on that."""
    paras = [text for _n, text in _unwrapped(_claude_md())]
    i = next(k for k, text in enumerate(paras) if text.startswith("**If a kit command is refused"))
    intro, line, after = paras[i : i + 3]
    assert "Fill in the real path to this checkout" in intro, (
        "the agent is not told to put the real path in the command it relays"
    )
    assert "do not relay the placeholder" in intro, (
        "the agent is not told that `<path>` is a placeholder"
    )
    assert "Tell them to copy the line and paste it at their prompt" in intro, (
        "the person is handed a `!` line with no word on what to do with it"
    )
    assert "They may never have used `!` before" in intro, (
        "the agent is not told the person may not know what a `!` line is"
    )
    assert line == "> ! cd '<path>/baton-proxy/try' && python3 kit.py setup", (
        "the instruction to substitute is not directly above the line it is about"
    )
    # K1b: "touch"/"that edit" became "read"/"it". The paragraph is about a
    # refusal of `setup`, and after the flip setup only READS their config —
    # "edit" was the false word, not the pinned one.
    assert after == (
        "This is not a fallback. Someone deciding whether to let a tool read their "
        "client's config is better served running it themselves."
    ), "the closing sentences of the refusal paragraph changed"


def test_the_handover_line_carries_the_folder_the_wrap_loads_in():
    """The last thing the person reads has to name where to start the client.

    K1b makes this the most likely way a drive captures nothing. A project
    config loads only for a session started in its own directory, so a terminal
    opened anywhere else gets their ordinary server — and the result is
    indistinguishable from a broken install. `kit.py` prints a `cd` line and
    `receipt` carries a whole checklist item for it (`start_where`, and item 3
    of the empty-capture list), but CLAUDE.md told the agent to END its message
    with a block that said "start Claude Code there" and named no folder. Under
    the global default "there" was true, because a global entry loads wherever
    you start. Nothing pinned the block, so the flip falsified it in silence.

    Pinned as meaning, not spelling: the closing block must carry a `cd`, must
    carry a substitution the agent is told to fill in, and must say what
    starting elsewhere costs."""
    md = _claude_md()
    step = _doc_section("**3. Hand them a second terminal", "## While it runs")
    flat = _flat(step)
    assert "cd '<folder>'" in step, (
        "the closing hand-over block no longer tells them to cd anywhere IN QUOTES. The "
        "quotes are the pin: `/Users/x/Client Work/app` is an ordinary folder name and an "
        "unquoted cd stops at the first space, so the terminal opens somewhere the wrap "
        "does not load and captures nothing — silently, which is the whole failure "
        "`_cd_to` quotes to prevent."
    )
    assert "do not relay the placeholder" in flat, (
        "the agent is not told `<folder>` is a placeholder, and it will be relayed literally"
    )
    assert "anywhere else will not record anything" in flat, (
        "the block no longer says what starting in the wrong folder costs, which is "
        "the sentence that makes the cd worth obeying"
    )
    assert "start Claude Code there, and\n> use" not in md, (
        "the pre-K1b block is back: it says `there` with no folder, which was only "
        "true while the wrap was global"
    )


def test_the_agent_does_not_open_a_terminal_for_them():
    """T7 was built, driven once, and reverted the same day.

    `open -a Terminal <dir>` from inside an agent session hands the new window
    that session's environment. Measured: an agent's shell carries seven
    CLAUDE_CODE_* variables, `CLAUDE_CODE_CHILD_SESSION` among them, plus the
    messaging socket, token and session id of the session doing the opening.
    The client started in that window reads the child marker, disables
    transcript saving, and prints a warning the person did not cause.

    Worse than wrong: inconsistent. If their terminal app is already running,
    `open` asks the running app for a window and the shell gets a clean
    environment; if it is not, `open` launches it as a child and it inherits.
    So the same instruction degrades the session for some people and not
    others, which is the hardest kind of report to act on.

    Pinned as a prohibition rather than deleted, because "open a terminal in
    the right folder for them" is an obvious convenience and will be proposed
    again by someone who has not paid for it."""
    step = _flat(_doc_section("**3. Hand them a second terminal", "## While it runs"))
    assert "Do not open a terminal for them" in step, (
        "the prohibition is gone, and the convenience will be re-added"
    )
    assert "CLAUDE_CODE_CHILD_SESSION" in step, (
        "the reason is gone, so the prohibition reads as taste and will be overruled"
    )
    # The INSTRUCTION form, not the mention. A prohibition has to name the
    # command it forbids, so `"open -a Terminal" not in step` fails on the
    # paragraph doing the forbidding — which is what it did, one commit after
    # it was written. Pin the shape that would actually tell an agent to run
    # it: a platform heading followed by `run`.
    assert not re.search(r"\*\*On macOS\*\*,\s*run\s*`open -a Terminal", step), (
        "the agent is instructed to open a terminal again"
    )
    assert "Do not open a terminal" in step, (
        "the paragraph stopped forbidding it, so naming the command is now an example "
        "rather than a prohibition"
    )
    assert "Continue without using this MCP server" in step, (
        "the hand-over no longer warns that the approval prompt's DEFAULT captures "
        "nothing, which is the cheapest way for a trial to produce a silent zero"
    )
    assert "Do not promise either prompt" in step, (
        "the doc promises a trust prompt that a folder under an already-trusted one "
        "never raises — which is the usual shape, since their session is one level up"
    )


def test_claude_md_makes_a_refusal_stick_for_the_config_commands():
    """Try once, then hand over. The model cannot see its own permission mode,
    so a refusal is the only detector it has. Handing over without ever trying
    was rejected: in manual mode the person gets a prompt with a "don't ask
    again" option, and never trying would turn that one keypress into a paste on
    every command. So the first refusal switches the rest of the trial, and auto
    mode shows its denial text once instead of three times.

    ⚠ REWRITTEN AT K1b, and the old premise is worth recording because it was
    load-bearing and is now false. It read: "`receipt` joins them only once a
    wrap is in place. Before that it reads just the kit's own files; after, it
    reads the config to check the wrap is still there, and a read of that path
    is what auto mode refuses." In project mode `receipt` checks the wrap in
    `MCP_PATH`, the kit's own file, so it never opens their config at any point
    of the trial — and `uninstall` deletes that same file rather than writing
    theirs. The set of commands that touch their config went from two-and-a-half
    to exactly one, `setup`, and it is a read.

    So the promise this guards is unchanged — the rule names exactly the
    commands that open their config, and hands over only those — while the
    membership changed. The three `receipt` clauses are gone because the
    condition they turned on no longer exists, not because the caution was
    dropped.

    What replaces them is the OTHER refusal, which is not about the config at
    all: some permission modes refuse to run code from a freshly cloned
    repository whatever it touches. That one can hit `receipt` and `uninstall`,
    so the doc must keep a route for them without claiming they read the
    config."""
    paras = [text for _n, text in _unwrapped(_claude_md())]
    # By leading text, never by offset from the intro. The K1b rewrite split
    # this section into more paragraphs than it had, and a positional index
    # would have moved silently onto the wrong one.
    rule = next(t for t in paras if t.startswith("Once a kit command has been refused"))
    only = next(t for t in paras if t.startswith("`setup` is the only command"))
    other = next(t for t in paras if t.startswith("A refusal can still reach them"))

    assert "do not attempt `setup` again for the rest of the trial" in rule, (
        "a refusal no longer carries over to the command that opens their config"
    )
    assert "with or without a server name" in rule, (
        "the rule does not name setup in both of its forms"
    )
    assert "`uninstall`" not in rule, (
        "uninstall is back in the hand-over rule; in project mode it writes only "
        "the kit's own files, so handing it over costs a paste for nothing"
    )
    assert "without trying it first" in rule, "the remaining hand-overs are still attempted"
    # "The same way" was read as the `!` format alone, and the repeats went out
    # bare. The instruction is named so it cannot be read out of the back-reference.
    assert (
        "Every hand-over carries the instruction to copy the line and paste it, "
        "not only the first one." in rule
    ), "the copy-and-paste instruction is tied to the first hand-over only"

    assert "`setup` is the only command that opens their config" in only, (
        "the doc no longer says which single command reaches their config, which is "
        "what makes the rule above checkable rather than a bare list"
    )
    assert "keep running those yourself" in only, (
        "receipt and uninstall are handed over despite touching no config of theirs"
    )

    assert "refuse to run code from a repository the person has just cloned" in other, (
        "the second, config-independent refusal is gone — it is the one that can still "
        "hit receipt and uninstall, and without it a refusal there reads as a contradiction"
    )
    assert "hand it over the same way" in other, (
        "the second refusal names no route, so the trial stops at it"
    )
    assert "in every mode" not in rule, "the rule still says the agent always keeps receipt"


def _claude_md_paras(heading: str) -> list[str]:
    """The paragraphs under one `##` heading of `try/CLAUDE.md`.

    By SECTION, where the rest of this file selects by leading paragraph text.
    The difference matters for a mode qualification: it is free to move between
    the paragraphs of the section it belongs to, and a per-paragraph selector
    reads that re-merge as a deletion. What must not happen is it leaving the
    section — which is what this returns."""
    out: list[str] = []
    inside = False
    for _n, para in _unwrapped(_claude_md()):
        if para.startswith("## "):
            inside = para == f"## {heading}"
            continue
        if inside:
            out.append(para)
    assert out, f"CLAUDE.md has no '## {heading}' section, or it is empty"
    return out


# Every `CLAUDE.md` claim that holds for the wrap in the checkout and NOT for an
# `--in-place` wrap, with the cost of leaving it unqualified.
#
# ⚠ A SWEEP LIST, deliberately, and not a count. The first version of this test
# opened "Three of CLAUDE.md's promises…", which reads as the complete set. It
# was three of EIGHT: `/code-review` found the other five, and the closed
# framing is what would have stopped the next reader looking. Same failure as
# the "Two refusals name a way forward" count in the doc itself, in the same
# commit → `_RESTART_SINKS`, which exists because one sentence corrected in one
# place stays wrong in three.
#
# Adding a row is the cheap half. Before adding one, check the claim string
# appears in exactly ONE paragraph — the test asserts that, because a claim that
# got duplicated is a second place to correct.
_MODE_BOUND_CLAIMS = [
    (
        "remove the wrap this checkout holds",
        "the comment in the command list is the first thing the agent reads about "
        "`uninstall`, and under `--in-place` that command removes no file here — it "
        "rewrites their own config",
    ),
    (
        "without changing it",
        "the doc's opening description of the kit is the agent's whole frame, and "
        "`--in-place` both changes that file and writes nothing in the checkout",
    ),
    (
        "let a tool read their client's config",
        "the hand-over reassurance says *read* on a path that WRITES. ⚠ That "
        "paragraph is pinned byte-for-byte by "
        "`test_claude_md_tells_the_agent_to_fill_in_the_real_path_on_a_refusal`, so the "
        "qualification lives in the paragraph AFTER it — add, never reword",
    ),
    (
        "is the only command that opens their config",
        "the agent keeps running `receipt` itself on a wrap that made it read their config",
    ),
    (
        "reads their Claude Code config",
        "the reassurance given immediately before they run setup is the wrong one — "
        "`--in-place` writes that file",
    ),
    (
        "loads only for a session started in the directory",
        "this is the sentence an empty capture is diagnosed with, so the agent sends "
        "them to a folder that was never the problem",
    ),
    (
        "will not record anything",
        "the closing block is relayed to the person word for word, and an `--in-place` "
        "wrap records wherever THEIR entry loaded — which may be no folder at all",
    ),
    (
        "nothing rewrites it",
        "an agent that believes this treats `THE WRAP IS GONE` as impossible and hunts "
        "for a kit bug instead of relaying it",
    ),
    (
        "nothing to restore",
        "the agent says their config was never changed just after `--in-place` changed it",
    ),
    # ⚠ ONE ROW, TWO SENTENCES, and the guarantee is weaker here than it looks.
    # "removes everything else" and "get the original server back" sit in the
    # same paragraph of §Removing it, so the check below cannot tell them apart:
    # either sentence's qualification satisfies both. Measured — as two rows, a
    # mutant aimed at the second reported the first. Merged rather than left
    # looking independent. Splitting the paragraph would not fix it either,
    # because the window spans the next paragraph too; the fix, if it is ever
    # worth it, is a per-sentence check → the docstring's "sit WITH the claim".
    (
        "removes everything else",
        "deleting the checkout before `uninstall` leaves an `--in-place` entry that "
        "cannot start, and an `--in-place` wrap comes off wherever their entry loaded "
        "rather than only in this folder — both sentences are in this one paragraph",
    ),
]


def test_every_mode_bound_promise_names_the_in_place_exception(tmp_path, kit_home, capsys):
    """Each `CLAUDE.md` claim that is true of one wrap has to name which.

    The kit has two wraps and this doc is what the agent obeys. On the default
    path their config is never written, so "`setup` is the only command that
    opens their config" and "there is nothing to restore" both hold.
    `--in-place` writes their config, and then `receipt` reads it and
    `uninstall` writes it. Unqualified, the doc has the agent telling someone
    their config was never changed immediately after it was changed — this
    feature's own failure mode, said back to the prospect it was built for.

    The qualification has to sit WITH the claim: in its own paragraph or the one
    straight after. Anywhere else in the section is not good enough, because an
    agent reads the claim and acts, and three claims in one section would all be
    satisfied by a single mention.

    ⚠ The claims are swept from `_MODE_BOUND_CLAIMS`; the numbered parts below
    additionally tie some of them to what the KIT actually produces, because a
    doc test that only greps its own sentences passes on a doc that has drifted
    away from the command it describes. No count here — the list is the owner,
    and it has already grown twice."""
    paras = [para for _n, para in _unwrapped(_claude_md())]

    for claim, cost in _MODE_BOUND_CLAIMS:
        hits = [i for i, p in enumerate(paras) if claim in p]
        assert len(hits) == 1, (
            f"{claim!r} appears in {len(hits)} paragraphs of CLAUDE.md, not 1. "
            "ZERO means the sentence was reworded or deleted — re-point this row at "
            "the new wording rather than dropping it, because a row that matches "
            "nothing guards nothing. MORE THAN ONE means a second place the exception "
            "has to be written, and this sweep would pass on the qualified copy alone."
        )
        # The claim's own paragraph, plus the next. A blockquote carries several
        # claims and cannot hold its own exception, so the following paragraph is
        # where that one has to live.
        window = " ".join(paras[hits[0] : hits[0] + 2])
        assert "--in-place" in window, (
            f"CLAUDE.md says {claim!r} without naming the --in-place case beside it, so {cost}"
        )

    removing = _claude_md_paras("Removing it")
    rules = _claude_md_paras("Rules that do not bend")

    # 1. ⚠ RESTORED, because the sweep is WEAKER than what it replaced. The
    # sweep asks only whether `--in-place` appears beside the claim; the block
    # this commit series deleted asserted the exception names both commands.
    # Measured: rewriting that paragraph to "An `--in-place` wrap puts the wrap
    # in their own config. Hand setup over the same way in that case." — no
    # `receipt`, no `uninstall` — left the WHOLE suite green. The doc could then
    # tell the agent to keep running `receipt` itself on a wrap that made it
    # read their config, which is that row's own cost string
    # → [[feedback_a_fix_can_recreate_its_bug_in_the_untested_half]].
    #
    # Generalising a specific assertion is not free: the general one has to be
    # shown to still catch what the specific one caught, and this one did not.
    setting_up = _claude_md_paras("Setting up")
    exception = [p for p in setting_up if "--in-place" in p and "`receipt`" in p]
    assert exception, (
        "the --in-place exception no longer names `receipt`, which that mode makes read "
        "their config — and the agent is told above to keep running `receipt` itself"
    )
    assert any("`uninstall`" in p for p in exception), (
        "the --in-place exception names `receipt` but not `uninstall`, which writes "
        "their config on that path"
    )

    # 2. The restore claim, tied to the word the command actually prints.
    config = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--in-place", "--src-config", str(config)]) == 0
    capsys.readouterr()
    # Read WHILE wrapped. Taking this after `uninstall` reads the restored entry,
    # which has no `PYTHONPATH` at all — the assertion in part 3 then dies on a
    # KeyError instead of measuring anything.
    wrapped = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["notion"]

    assert kit.main(["uninstall"]) == 0
    printed = capsys.readouterr().out

    assert "Restored" in printed, (
        "the --in-place uninstall no longer prints `Restored`; this test's doc "
        "assertion below would then be pinning a word the kit never says"
    )
    # ⚠ And the OTHER half. The doc tells the agent to "read which of the two the
    # command printed", which only works if one output does not satisfy both
    # rows. Asserting `Restored` appears on the in-place path leaves a reword of
    # the project branch to "Restored … nothing to restore" passing — the
    # one-output-two-rows failure the receipt tests in this file exist to stop.
    # No state to clear first — the uninstall above cleared it, which is the
    # "Setup state has been cleared" row of the receipt's own first line.
    assert kit.main(["setup", "notion", "--src-config", str(config)]) == 0
    capsys.readouterr()
    assert kit.main(["uninstall"]) == 0
    default_printed = capsys.readouterr().out

    assert "Restored" not in default_printed, (
        "the DEFAULT uninstall prints `Restored` too, so the doc's 'read which of the "
        f"two the command printed' cannot discriminate:\n{default_printed}"
    )
    assert "nothing to restore" in default_printed.lower(), (
        "the default uninstall says neither word — the assertion above then passes on "
        f"an uninstall that printed nothing at all:\n{default_printed}"
    )

    restore = [p for p in removing if "--in-place" in p]
    assert restore, (
        "§Removing it says 'there is nothing to restore' with no mode named, so the "
        "agent says it after an --in-place uninstall that restored their config"
    )
    assert any("Restored" in p for p in restore), (
        "the exception does not name `Restored`, the word the command prints — so the "
        "agent has no way to tell the two outcomes apart from the output"
    )

    # 3. The delete order. The doc's reason is that the in-place entry runs the
    # proxy out of this checkout; that is asserted here rather than trusted.
    assert str(kit.SRC_DIR) in wrapped["env"]["PYTHONPATH"], (
        "the in-place wrap no longer points into the checkout, so §Removing it's "
        "reason for uninstalling before deleting the folder is now false"
    )
    order = [p for p in removing if "uninstall" in p and "delet" in p and "--in-place" in p]
    assert order, (
        "§Removing it says 'deleting this checkout removes everything else' without "
        "naming the --in-place case, where the entry left in their config runs the "
        "proxy out of the folder being deleted — a server that cannot start"
    )

    # 4. The warning is not a refusal, and the doc quotes its real first words.
    prefix = "⚠ One thing to know about"
    assert kit.cwd_dependent_warning("srv", "why").startswith(prefix), (
        "the warning's opening changed; the doc quotes it so the agent can tell a "
        "warning from a refusal by reading the output"
    )
    warned = [p for p in rules if prefix in p]
    assert warned, (
        f"§Rules never quotes {prefix!r}, so nothing tells the agent that a successful "
        "setup can still print a warning — and the refusal rules above read as though "
        "every ⚠ line were a failure"
    )
    assert any("exits non-zero" in p for p in warned), (
        "the warning paragraph does not say why the retry rule does not reach it, so "
        "'do not retry with different flags' still reads as covering the --in-place "
        "line the warning itself prints"
    )


def _recording_reads(monkeypatch) -> list[str]:
    """Record every file `Path.read_text` opens, resolved."""
    reads: list[str] = []
    real_read_text = Path.read_text

    def recording_read_text(self, *args, **kwargs):
        reads.append(str(self.resolve()))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)
    return reads


def test_receipt_reads_the_config_only_once_a_wrap_is_in_place(
    tmp_path, kit_home, capsys, monkeypatch
):
    """The refusal rule hands `receipt` over only once a wrap is in place, on the
    grounds that before then it touches no config. That is a claim about the
    code, so it is measured here rather than trusted: every file `receipt` reads
    is recorded, with no wrap and then with one. The second half is also what
    shows the recorder can see a config read at all.

    `--in-place` since K1b. In project mode the answer is not "once a wrap is in
    place" but NEVER, which is a different claim and gets its own test below."""
    config = _config(tmp_path, GLOBAL_ONLY).resolve()
    reads = _recording_reads(monkeypatch)
    _receipt_output(capsys)
    assert str(config) not in reads, "receipt read the config with no wrap in place"
    assert (
        kit.main(["setup", "notion", "--src-config", str(config), "--tenant", "t", "--in-place"])
        == 0
    )
    capsys.readouterr()
    reads.clear()
    _receipt_output(capsys)
    assert str(config) in reads, "receipt no longer reads the config once a wrap is in place"


def test_in_project_mode_receipt_never_opens_their_config(tmp_path, kit_home, capsys, monkeypatch):
    """G1. `CLAUDE.md:135`: "`setup` is the only command that opens their
    config. `receipt` and `uninstall` read and write only the kit's own files."

    That sentence is what makes the `!` hand-over shrink to ONE command in
    project mode, and the agent is told to keep running the other two itself. If
    `receipt` reached for their config, the agent would be refused on a command
    the doc promises is safe — and it would look like a kit fault rather than a
    doc fault.

    ⚠ Say it in the narrow form and no wider. Project mode does NOT mean "we
    never read their config": `setup` reads it, that is K2, and `PROMPT.md:50`
    tells the person so. The claim here is about `receipt` only, and the sibling
    above pins the in-place answer, which is different rather than weaker.

    The wrap file IS read — asserted, because "no reads at all" would pass on a
    receipt that did nothing."""
    config = _config(tmp_path, GLOBAL_ONLY).resolve()
    assert kit.main(["setup", "notion", "--src-config", str(config), "--tenant", "t"]) == 0
    capsys.readouterr()

    reads = _recording_reads(monkeypatch)
    out = _receipt_output(capsys)

    assert str(config) not in reads, (
        f"receipt opened their config in project mode; CLAUDE.md:135 says it does not:\n{reads}"
    )
    assert str(kit.MCP_PATH.resolve()) in reads, (
        "receipt did not read the wrap file either — without this the assertion above "
        f"passes on a receipt that read nothing at all:\n{reads}"
    )
    assert "THE WRAP IS GONE" not in out, "the receipt did not find the wrap it just wrote"


@pytest.mark.parametrize(
    "extra,kept",
    [
        pytest.param(
            ["--in-place"],
            ("backup:", "events:", "tenant:"),
            id="in-place",
        ),
        pytest.param(
            [],
            ("copied from:", "your own config was read, not changed.", "events:", "tenant:"),
            id="project",
        ),
    ],
)
def test_setup_does_not_tell_them_to_check_early(tmp_path, kit_home, capsys, extra, kept):
    """Watched live after 0.6.4: `CLAUDE.md` had lost the day-one receipt nag and
    setup still printed its own copy. It told the person the wrap may well be
    broken before they had used it once, and handed them a check the ending
    already makes: saying they are done runs `receipt`, which states what landed
    or that nothing did. Pinned on both setup paths, together with the rest of
    the printout, which has to survive the cut.

    ⚠ Parametrized by MODE at K1b rather than pinned to `--in-place`, because
    only the `kept` list is mode-bound and the day-one nag is not. `backup:` is
    printed by the mode that writes a backup, and project mode writes none — so
    pinning this whole test to `--in-place` would have stopped checking the nag
    on the path every prospect now takes, to keep one line in the kept list.

    The project row pins two lines nothing else pins, and they are the two the
    feature exists to be able to say: `copied from:` and `your own config was
    read, not changed.`"""
    path = _config(tmp_path, GLOBAL_ONLY)
    args = ["setup", "notion", "--src-config", str(path), "--tenant", "t", *extra]
    assert kit.main(args) == 0
    first, _err = capsys.readouterr()
    assert kit.main(args) == 0
    again, _err = capsys.readouterr()
    assert "Already wrapped" in again, "the second run is not the re-entry path"
    for out in (first, again):
        for nag in ("Run it early", "first day", "day one", "five-minute", "wasted trial"):
            assert nag not in out, f"setup still tells them to check early ({nag!r}):\n{out}"
        assert kit.come_back() in out and kit.ENDING_NOTE in out, (
            f"the cut took more than the day-one lines:\n{out}"
        )
    for line in (*kept, kit.RESTART_NOTE):
        assert line in first, f"the cut took {line!r} with it:\n{first}"


def test_step_2_does_not_reprint_what_the_person_watched_print():
    """Watched live: the person ran setup with `!`, watched it print, and then saw
    the same entry and the same guidance again from the agent. Pasting the entry
    is right when the agent ran it, since tool output is folded and they would
    not see it otherwise; it is noise when they ran it and saw it. What the raw
    output does not give them is what changed in plain words, and that stays."""
    paras = [text for _n, text in _unwrapped(_claude_md())]
    step = next(text for text in paras if text.startswith("**2. Run it.**"))
    assert "If you ran it, paste the printed entry into your reply, in a code block" in step, (
        "the agent that ran setup itself no longer shows the entry it folded away"
    )
    assert "tool output is folded and the person will not see it otherwise" in step, (
        "the reason to paste, which holds only when the agent ran it, is gone"
    )
    assert "If they ran it themselves with `!`, they watched it print" in step, (
        "the instruction is no longer conditional on who ran setup"
    )
    assert "do not reprint the entry or repeat the guidance printed under it" in step, (
        "the agent reprints what the person already has on screen"
    )
    assert "Say briefly, in plain words, what changed" in step, (
        "the plain-words summary, the part the raw output does not give them, is gone"
    )
    assert "Do not ask them to name a tenant or a label" in step, "step 2 lost its tenant rule"


# ---------------------------------------------------------------------------
# TK-D-7 — the kit is Claude Code only, and it says so before the cost is paid
# (Dave's spec §7, second half).
#
# `~/.claude.json` is Claude Code's file and nothing else's, so the kit is
# single-client by construction. That is fine; discovering it at the server
# listing step is not, because by then they have read the security document and
# approved a clone. SECURITY.md §1 named Claude Desktop in its first sentence —
# true of the proxy, and read by someone deciding whether the KIT is for them.
#
# The site the spec names is the pasted prompt, and it now HAS a file
# (`try/PROMPT.md`), so all three shipping surfaces are covered. The prompt is
# checked separately below rather than added to the parametrize: this test
# measures the disclosure against a paragraph only the two long documents have,
# and a file without it would pass on a marker that never matched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["try/CLAUDE.md", "try/SECURITY.md"])
def test_the_single_client_assumption_is_stated_before_it_bites(rel):
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    paras = list(_unwrapped(text))
    hit = [(n, p) for n, p in paras if "works with Claude Code only" in p]
    assert hit, f"{rel} never says which client the kit is for"
    n, para = hit[0]
    assert "~/.claude.json" in para, "the reason is what makes it checkable, not the claim"
    # Before the reader has spent anything: the config-file search is where the
    # single-client assumption first shows, so the disclosure has to precede it.
    # A missing marker must FAIL, not default to the end of the document — with
    # a fallback of len(paras) the assertion is true however late the
    # disclosure sits, and a reworded marker would retire the guard silently
    # ([[feedback_control_condition_must_be_able_to_fail]]).
    costs = [i for i, (_ln, p) in enumerate(paras) if "servers it can wrap" in p or "Before:" in p]
    assert costs, (
        f"{rel}: the paragraph this ordering is measured against is gone or reworded — "
        "re-point the marker rather than deleting the check"
    )
    said_at = next(i for i, (ln, _p) in enumerate(paras) if ln == n)
    assert said_at < costs[0], f"{rel} discloses the client assumption too late"


# ---------------------------------------------------------------------------
# TK-FL-2 — the pre-consent summary names the change they will notice
# (followability run `follow-20260831-163630`, finding 2).
#
# The summary itself is gone: the 2026-09-04 rewrite made the security detail
# opt-in and asked for by the paste, so CLAUDE.md no longer has a step 2 and the
# two tests that read one were deleted with it. What survives here is the half
# that was never about the doc — the code facts a summary of this shape has to
# be true about, wherever it is next written, and which §3 of SECURITY.md still
# states today. Read "step 2" below as "any summary we give before the wrap".
#
# Step 2's list covered the config entry, the credentials, the local file and
# reversibility. It did not cover the two added tools or the three grafted
# parameters — which is the addition a person is most likely to SEE, since it
# shows up in their own agent's tool list. Every `a1` run volunteered it anyway,
# but from `SECURITY.md` §3 rather than from step 2, so an agent that skipped
# the document gave a strictly worse summary and could not tell.
#
# The two claims underneath it are mechanical and pinned as such: the tool names
# come from `proxy.py`, the parameter count from the injector itself, and the
# report tool is GATED — it appears only because the kit writes a file sink, so
# a summary promising it is one config change away from being false.
# ---------------------------------------------------------------------------


def test_the_tool_names_step_2_promises_are_the_ones_the_proxy_grafts():
    """A rename in `proxy.py` would leave the doc naming tools that do not
    exist, in the one paragraph a person reads before approving anything."""
    from baton_proxy.proxy import ANNOTATE_TOOL_NAME, REPORT_TOOL_NAME

    assert ANNOTATE_TOOL_NAME == "baton_annotate"
    assert REPORT_TOOL_NAME == "baton_session_report"


def test_step_2s_parameter_count_is_the_injectors_own():
    """The word "three" is a number in a security summary, so it is read off the
    code that does the grafting rather than copied from `SECURITY.md` §3 — two
    docs agreeing proves only that they were written together."""
    tool: dict[str, Any] = {"name": "t", "inputSchema": {"type": "object", "properties": {}}}
    dispositions = _inject_goal_params(tool, "optional")
    assert len(dispositions) == 3, (
        f"the proxy grafts {len(dispositions)} parameters; step 2 and SECURITY.md §3 say three"
    )
    assert "required" not in tool["inputSchema"], (
        "step 2 calls them optional; the default mode now marks one required"
    )


def test_the_report_tool_step_2_promises_is_one_the_kit_actually_gets(tmp_path):
    """`baton_session_report` is injected only when a file sink is configured
    (`report.should_inject_report_tool`). The kit writes exactly that and no
    HTTP sink, which is what opens the gate — so the promise is true because of
    a line in `kit.py`, not by construction."""
    from baton_proxy.report import should_inject_report_tool

    sink = kit.file_sink_uri(str(tmp_path / "events.jsonl"))
    assert should_inject_report_tool(sink), (
        "step 2 tells the person their agent will see baton_session_report, and "
        "the kit's own sink no longer causes it to be injected"
    )


# ---------------------------------------------------------------------------
# TK-P-1 — the prompt has a file (Dave's spec §7 / polish pass, last open item).
#
# It was quoted in one internal findings doc and shipped from nobody's
# repository, so the one surface a prospect meets FIRST was the one surface no
# guard could see. `try/PROMPT.md` is that file. What it must keep is small and
# each piece is a defect the run actually produced.
# ---------------------------------------------------------------------------


PROMPT_MD = "try/PROMPT.md"


def _prompt_text() -> str:
    return (REPO_ROOT / PROMPT_MD).read_text(encoding="utf-8")


def test_the_prompt_does_not_send_them_hunting_for_a_checkout():
    """Finding 3, the worst of Dave's run: "if it's already on this machine"
    cost four approvals, the fourth of them an agent reading `~/Downloads`,
    before the person knew anything about the product. The clause can only ever
    cost approvals — no first-time user already has the repo — so the fix was to
    clone into the current directory and forbid the search outright.

    This is the one step whose tested wording is recorded verbatim
    (`trykit-findings-2026-08-28.md`), so it is pinned rather than paraphrased.
    """
    text = _prompt_text()
    assert "into the directory I'm in" in text, "the prompt stopped naming where to clone"
    # The ban is now general rather than named: the paste stops the agent doing
    # ANY work before the approval it says is not yet given, which covers the
    # `~/Downloads` read that cost the run its fourth approval.
    assert "don't do anything else" in text, (
        "the prompt no longer forbids the work-before-approval that cost Dave's run four"
    )
    for retired in ("if it's already on this machine", "Ask me where to put it"):
        assert retired.lower() not in text.lower(), (
            f"the prompt re-added the clause finding 3 removed: {retired!r}"
        )


def test_the_prompt_survives_arriving_as_a_file():
    """The paste travels two ways. It is copied off the Baton Proxy page into a
    session, and it is saved or forwarded as a file, which gets opened from a
    downloads folder, where step 1's "the current directory" quietly means
    exactly there.

    We cannot detect which route it took, so the text carries the check itself.
    Pinned because the clone is the first thing it costs them and a kit in
    `~/Downloads` is a kit they will not find again.
    """
    prompt = _flat(_prompt_text())
    assert "ask me where the kit should live before you clone" in prompt, (
        "the paste no longer checks where it is before cloning, so an attachment "
        "route lands the kit in a downloads folder"
    )
    assert "downloads folder" in prompt, "the check stopped naming the case it is for"


def _flat(text: str) -> str:
    """Hard-wrapped text with the newlines collapsed. Every phrase worth
    pinning in these two files is longer than the distance to the next line
    break, so a literal `in` check against the raw text passes or fails on
    where the wrap happens to fall — which is not a property of the doc.

    Also used on `--help` output, which argparse wraps to the terminal width —
    the same problem arriving from a different wrapper."""
    return " ".join(text.split())


def _flat_unquoted(text: str) -> str:
    """`_flat`, with a blockquote's `>` markers taken off first.

    The lines the doc tells the agent to SAY are blockquoted, so `_flat` leaves
    a `>` at every wrap inside them and a verbatim comparison against what the
    kit prints can never match. Stripping the marker is what makes the two
    comparable, and comparing them is the only way a rewrap of either file
    cannot quietly change what the person is told."""
    return _flat(re.sub(r"(?m)^> ?", "", text))


def test_the_main_flow_carries_no_security_readout():
    """Dave's rubric (2026-09-11): every security-flavoured thing belongs in the
    security readout, and nothing of that kind appears anywhere else. A prospect
    who skips the security detail is choosing to skip it.

    The remote paragraph broke that. It said three things "whether or not they
    asked for the security detail", so a reader who had declined got a security
    lecture anyway. It is deleted rather than moved: `SECURITY.md` §2 already
    carries those facts and shows the before-and-after config with the `${VAR}`
    reference kept, where the paragraph could only assert it. This replaces
    `test_the_remote_consent_is_reachable_under_the_order_the_paste_sets`, which
    pinned the paragraph in place.

    Widened in the same release from "bearer token" to "bearer" in any case,
    after step 1's eligibility paragraph turned out to still name the
    `Authorization: Bearer` header without ever saying "bearer token"."""
    md = _claude_md()
    flat = _flat(md)
    for gone in (
        "say three things before `setup` runs",
        "whether or not they asked for the security detail",
        "`receipt` on the first day",
    ):
        assert gone not in flat, f"the three-things paragraph is back: {gone!r}"
    # The main flow is everything from finding out where you are onward, less
    # the one section that IS the readout.
    start = md.index("## Start by finding out where you are")
    readout = md.index("## If they asked for the security detail")
    setting_up = md.index("## Setting up")
    assert start < readout < setting_up, "the doc's sections moved; re-cut the main flow"
    main = _flat(md[start:readout] + md[setting_up:]).lower()
    for fact in ("bearer", "${var}"):
        assert fact not in main, f"the main flow mentions {fact!r}, which belongs in the readout"
    # And the readout still carries what the paragraph pointed to, or deleting it
    # lost them rather than moving them to where they already were.
    sec = (KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8")
    section = sec[sec.index("## 2. What changes") : sec.index("## 3. What your agent sees")]
    assert "bearer token" in _flat(section) and "${" in section, (
        "SECURITY.md §2 no longer carries the facts the deleted paragraph pointed to"
    )


def test_the_prompt_hands_the_agent_the_doc_it_will_run_from():
    """Restores the half of `test_the_prompt_hands_off_to_the_shipped_docs` that
    survived the 2026-09-04 rewrite.

    That test wanted both documents named and the paste now names one, on
    purpose: the security detail is opt-in, so `SECURITY.md` is read only if
    they ask for it. `CLAUDE.md` is different in kind — it is not reading, it is
    the instructions the install runs on. The paste executes in a session
    started OUTSIDE `try/`, so nothing loads that file the way a session started
    inside it would; if the paste stops naming it, the agent drives the whole
    install from these fifty lines and every rule that lives only there — never
    edit a config by hand, never type `upload` — silently stops applying.

    Anchored on the literal path because the path is the load-bearing part: the
    session that just cloned is one level above `try/`, so a bare `CLAUDE.md` is
    a file it will not find.
    """
    prompt = _flat(_prompt_text())
    assert "`baton-proxy/try/CLAUDE.md`" in prompt, (
        "the paste no longer names the document the agent runs the install from"
    )
    assert "read `baton-proxy/try/CLAUDE.md` and follow it" in prompt, (
        "the paste names the document without telling the agent to follow it"
    )


def test_the_prompt_leaves_the_ending_to_the_kit():
    """The destination is pinned across `kit.py` and `CLAUDE.md`, and the ending
    is gated on there being something to hand over. The prompt runs before any
    capture exists, so naming the page here would make the offer at the one
    moment it cannot be true, and would add a third site to a two-site pin by
    accident rather than by decision.

    It also travels furthest: the console renders a copy of this text, and a URL
    inside a paste that a page is itself serving is the shape that goes stale
    without anyone reading it again."""
    text = _prompt_text()
    assert kit.SETUP_URL not in text, (
        "the prompt names the upload page before there is anything to upload"
    )
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "the prompt names an address"
    # The clone is the one URL the paste needs, and it is the repository's.
    urls = set(re.findall(r"https?://[^\s`>,)]+", text))
    assert urls == {"https://github.com/good-timing/baton-proxy"}, (
        f"the paste names a destination that is not the clone: {urls}"
    )


def test_the_two_receipt_rows_are_relayed_apart():
    """Run 5, and the second time this doc has had to say do not explain a
    number: the agent read `tool calls 4` next to a `tool definitions` list of
    four names and reported "4 tool calls across list_items, get_item,
    find_expiring and add_item". Two of those were never called. One row counts
    what landed and the other lists what the server offers, and joining them
    invents a per-tool breakdown the receipt does not have and the file does not
    support.

    Pinned on the words rather than the sentiment: the rule has to name both
    rows, or the next rewrite compresses it into "relay the receipt" and the
    join comes back."""
    doc = _flat(_claude_md())
    assert "`tool calls` is how many landed" in doc, (
        "the doc no longer says what the `tool calls` row counts"
    )
    assert "`tool definitions` is what the server offers" in doc, (
        "the doc no longer says what the `tool definitions` row lists"
    )
    assert "Never join them into one sentence" in doc, (
        "nothing stops the agent merging the two rows into a per-tool breakdown"
    )


# =============================================================================
# Moving the entry — the failure project scope introduces (2026-09-17).
#
# Every other guard in this file protects a wrap that happens IN PLACE. Project
# scope copies the entry into `baton-proxy/.mcp.json`, and the client then
# launches the server from a directory the person never chose. What breaks is
# invisible at setup and shows up in their next session, which is the class the
# kit refuses rather than ships.
#
# The two shapes below are not one rule with two spellings. A relative path is
# visible in the text of the entry. A `${CLAUDE_PROJECT_DIR}` reference is not a
# path at all and would pass any slash-hunting check — it breaks because the
# client resolves it to the scoped directory, which is the thing that changed.
# =============================================================================


@pytest.mark.parametrize(
    "entry,expected_fragment",
    [
        ({"command": "./server.sh"}, "relative path"),
        ({"command": "../bin/server"}, "relative path"),
        ({"command": "bin/server"}, "relative path"),
        ({"command": "node", "args": ["./index.js"]}, "argument 1"),
        ({"command": "node", "args": ["--flag", "../lib/main.js"]}, "argument 2"),
        ({"command": "node", "env": {"DB": "./data.sqlite"}}, "`DB` environment value"),
        # The most common relative path in an MCP entry, and the first version
        # of this guard missed it: every built-TypeScript server is `node
        # dist/index.js`.
        ({"command": "node", "args": ["dist/index.js"]}, "argument 1"),
        ({"command": "node", "args": ["--config=logs/app.json"]}, "argument 1"),
        # ⚠ `{"DB": "data/app.sqlite"}` was HERE and has moved to
        # `test_a_bare_relative_env_path_is_caught_where_the_kit_can_know`, with
        # a base. It is not a deleted promise; it is the same promise proved a
        # way that a secret cannot satisfy. See `_explicitly_relative`.
    ],
)
def test_an_entry_that_resolves_against_a_directory_is_named(entry, expected_fragment):
    """Each of these would wrap cleanly, print success, and die later."""
    reason = kit.cwd_dependent_reason(entry)
    assert reason is not None, f"{entry} moved to another directory would break, unnoticed"
    assert expected_fragment in reason, f"the reason for {entry} does not say which part: {reason}"


def test_a_bare_relative_env_path_is_caught_where_the_kit_can_know(tmp_path):
    """`{"DB": "data/app.sqlite"}` — the row that moved out of the shape list.

    It used to be refused on shape alone, with no base. That rule could not tell
    it from an AWS secret key, so env lost it (`_explicitly_relative`). The
    promise is kept by proving the claim instead of guessing it: with a base,
    the file is either there or it is not.

    ⚠ AND THE GIVE-UP, asserted rather than described, so nobody rediscovers it
    as a surprise: with no base, or with a base where the file does not exist,
    this entry is NOT refused. `_names_a_file_in`'s docstring already concedes
    the first case for every top-level entry — "the working directory a stdio
    server is launched in is not documented, which is why a relative path there
    is already unreliable". The kit does not make that worse; it declines to
    guess about it."""
    entry = {"command": "node", "env": {"DB": "data/app.sqlite"}}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "app.sqlite").write_text("x", encoding="utf-8")

    reason = kit.cwd_dependent_reason(entry, base=tmp_path)
    assert reason is not None, "a real relative env path under the entry's own base was missed"
    assert "`DB` environment value" in reason, reason
    assert "data/app.sqlite" not in reason, f"the reason printed the value: {reason}"

    assert kit.cwd_dependent_reason(entry) is None, "the no-base give-up changed; update the note"
    assert kit.cwd_dependent_reason(entry, base=tmp_path / "elsewhere") is None, (
        "the missing-file give-up changed; update the note"
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "${CLAUDE_PROJECT_DIR:-.}/bin/server"},
        {"command": "node", "args": ["${CLAUDE_PROJECT_DIR}/index.js"]},
        {"command": "node", "env": {"ROOT": "${CLAUDE_PROJECT_DIR:-.}"}},
        {"type": "http", "url": "${CLAUDE_PROJECT_DIR}/sock", "headers": {}},
    ],
)
def test_a_project_dir_reference_is_reported_as_itself_not_as_a_path(entry):
    """Asserted on the BRANCH, not on the refusal.

    The first version of this row only checked that something was refused and
    that the message quoted the entry — which the relative-path rule satisfied
    all by itself, because `${CLAUDE_PROJECT_DIR:-.}/bin/server` has a separator
    and no absolute root. Deleting the entire `${CLAUDE_PROJECT_DIR}` branch left
    the row green. It was pinning the string, not the reason.

    So both halves are asserted here. The person is told what they actually
    have: a reference the client resolves to a directory that is about to
    change, not a path they can be asked to make absolute — because they
    cannot, without hardcoding their own project root."""
    reason = kit.cwd_dependent_reason(entry)
    assert reason is not None, f"{entry} moves meaning when the entry moves"
    assert "CLAUDE_PROJECT_DIR" in reason, f"the reason does not name the reference: {reason}"
    assert "relative path" not in reason, (
        f"a ${{CLAUDE_PROJECT_DIR}} reference is reported as a relative path: {reason}. "
        "The advice that follows — make it absolute — is impossible to act on."
    )


def test_the_command_field_gets_no_npm_exemption():
    """`command` and `args` run the same rule with different widths, and this is
    the only row where the two disagree.

    Without it the width parameter is decoration: a mutation flipping `command`
    to the argument rule passed every other test in this file. The input is
    deliberately unusual — a directory literally named `@scope` — because the
    realistic inputs are exactly the ones that cannot tell the two rules apart.
    What is being pinned is the reason for the difference, not the input: an
    argument may be an npm package name, and `command` is the executable, so a
    separator in it is a path and there is nothing else it could be."""
    as_command = kit.cwd_dependent_reason({"command": "@scope/bin/server"})
    as_argument = kit.cwd_dependent_reason({"command": "npx", "args": ["@scope/bin/server"]})

    assert as_command is not None, "a command with a separator in it is a path"
    assert as_argument is None, "the same string as an argument is an npm spec and travels"


def test_a_file_in_the_entrys_own_directory_is_caught_without_a_separator(tmp_path):
    """`node server.js`, where `server.js` sits in the project the entry is
    scoped to. Nothing about the shape of `server.js` says path, so this is the
    one class the text rules cannot reach, and it breaks on the move like the
    rest. Only available where the entry HAS a directory — a project key in
    `~/.claude.json` — which is why `base` is optional."""
    (tmp_path / "server.js").write_text("// their server\n")
    entry = {"command": "node", "args": ["server.js"]}

    reason = kit.cwd_dependent_reason(entry, base=tmp_path)
    assert reason is not None
    assert "names a file" in reason
    # Same entry, no base to check against: undetectable, and claiming otherwise
    # would be the guard reporting a fact it cannot know.
    assert kit.cwd_dependent_reason(entry) is None
    # A base that holds no such file is not a hit either.
    assert kit.cwd_dependent_reason(entry, base=tmp_path / "elsewhere") is None


def test_the_same_bare_filename_is_caught_in_env_as_in_args(tmp_path):
    """The two fields are checked by the same pair of rules, or the guard has a
    blind position. Review found `_names_a_file_in` wired to `args` only, so
    this exact string was refused as an argument and passed as an environment
    value — a difference with no reason behind it, in a guard whose whole job is
    that a path stops resolving when the entry moves."""
    (tmp_path / "data.sqlite").write_text("")

    as_arg = kit.cwd_dependent_reason({"command": "node", "args": ["data.sqlite"]}, base=tmp_path)
    as_env = kit.cwd_dependent_reason(
        {"command": "node", "env": {"DB": "data.sqlite"}}, base=tmp_path
    )

    assert as_arg is not None, "the argument form is the one that already worked"
    assert as_env is not None, "the same filename in `env` is the same broken path"
    assert "`DB`" in as_env, f"the reason does not say which variable: {as_env}"


@pytest.mark.parametrize("word", ["production", "test", "debug"])
def test_an_ordinary_env_word_is_not_refused_for_matching_a_directory(tmp_path, word):
    """The guard must not fire on a config with nothing wrong with it.

    `NODE_ENV=production` is an ordinary environment value, and an ordinary
    project has a `production/` directory beside it. Checking `exists()` on env
    values refused all three of these — measured before the fix, not imagined.

    A refusal here costs more than a missed one. It lands on someone who has
    done nothing unusual, it is the kit telling them their own config is the
    problem, and offering `--in-place` in the same breath does not repair it:
    three prospects have already stopped at a sentence about their config.
    So env values match on `is_file()` only. An argument keeps the wider check,
    where `node server` resolving to a directory is a real launch."""
    (tmp_path / word).mkdir()
    entry = {"command": "node", "env": {"NODE_ENV": word}}

    assert kit.cwd_dependent_reason(entry, base=tmp_path) is None, (
        f"a directory named `{word}` beside the config made an ordinary env value a refusal"
    )


@pytest.mark.parametrize(
    "entry",
    [
        # An absolute path that contains an `=`. The argument rule splits on the
        # last `=` to see through `--config=logs/app.json`, and once `command`
        # was routed through that same rule this one started being refused for
        # being relative when it is absolute. The whole value is tested first.
        {"command": "/opt/my=dir/bin/server"},
        {"command": "node", "args": ["/abs/path/index.js"]},
        {"command": "/usr/local/bin/server"},
        {"command": "python3", "args": ["-m", "my_server"]},
        # The one that makes the narrow rule necessary rather than merely safe:
        # an npm scope carries a separator and is not a path. Refusing this
        # would refuse the most common MCP entry there is.
        {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
        {"command": "node", "env": {"TOKEN": "${MY_TOKEN}"}},
        {"type": "http", "url": "https://example.com/mcp"},
    ],
)
def test_an_entry_that_travels_is_left_alone(entry):
    """A refusal here costs a trial that would have worked."""
    assert kit.cwd_dependent_reason(entry) is None, f"{entry} travels fine and was refused"


# A secret is not a path, and the base64 alphabet contains `/`. Measured
# 2026-09-17: a 40-character AWS secret key carries one about half the time, so
# the shape rule refused an ordinary config — and then printed the key.
_SECRETS_THAT_ARE_NOT_PATHS = [
    pytest.param("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", id="aws-secret-with-slashes"),
    pytest.param("ghp_aB3/dE5fG7", id="github-pat-with-slash"),
    pytest.param("a2V5L3ZhbHVl/Zm9vL2Jhcg", id="base64-with-slash"),
    pytest.param("user:pa/ssword@host", id="credential-pair"),
]


def test_every_secret_case_actually_contains_a_separator():
    """The guard on the fixture above, not on the kit.

    A value with no `/` could never have been refused by the shape rule, so a
    row like that is a param that cannot fail — it reads as coverage and is
    noise. One was caught here: `dGhpcy9pcy9iYXNlNjQ` is base64 TEXT and carries
    no separator at all, so it passed the widened-rule mutant that reds its
    three siblings."""
    for param in _SECRETS_THAT_ARE_NOT_PATHS:
        (secret,) = param.values
        assert "/" in secret, f"{param.id!r} cannot exercise the shape rule: {secret!r}"


@pytest.mark.parametrize("secret", _SECRETS_THAT_ARE_NOT_PATHS)
def test_a_secret_with_a_slash_in_it_is_not_a_relative_path(secret, tmp_path):
    """`5e996d3` found this class and fixed one of the two env checks.

    Its title is "env values match files only — the guard was refusing normal
    configs", and it measured `production`, `test` and `debug` against
    `_names_a_file_in`. It did not look at `_relative_path_like`, which asks
    only about shape — and shape cannot tell a secret from a path.

    The cost is not only a lost trial. The refusal names the value, so a config
    that was never broken produces a printed credential; see the sibling below.
    """
    entry = {"command": "npx", "args": ["-y", "srv"], "env": {"AWS_SECRET_ACCESS_KEY": secret}}
    assert kit.cwd_dependent_reason(entry, base=tmp_path) is None, (
        f"a secret containing a separator was refused as a relative path: {secret!r}"
    )


@pytest.mark.parametrize(
    "value,refused",
    [
        ("./data.sqlite", True),
        ("../shared/db.sqlite", True),
        ("data/app.sqlite", False),
        ("plain-value", False),
        # ⚠ An env value carrying ARGUMENT grammar. `NODE_OPTIONS` and
        # `JAVA_OPTS` hold flags, so the path sits after an `=` and the head of
        # the string is `--require`. The first version of `_explicitly_relative`
        # checked the raw value and missed this, while the identical string in
        # `args` was refused — the same substring, two answers.
        ("--require=./instrument.js", True),
        ("--config=../shared/app.json", True),
        # The other side of the same split: a flag whose value is not a path.
        ("--max-old-space-size=4096", False),
    ],
)
def test_an_env_value_is_a_path_when_it_says_so(value, refused, tmp_path):
    """The narrowed env rule, both directions in one place.

    `./` and `../` state that the value resolves against a working directory,
    which is exactly what moving the entry breaks. A bare `data/app.sqlite` that
    names nothing on disk is left to `_names_a_file_in`, which proves the claim
    instead of guessing — the give-up is recorded in `_explicitly_relative`.
    """
    entry = {"command": "npx", "args": ["-y", "srv"], "env": {"DB": value}}
    reason = kit.cwd_dependent_reason(entry, base=tmp_path)
    assert (reason is not None) is refused, f"{value!r} -> {reason!r}"


@pytest.mark.parametrize(
    "value", ["--require=./instrument.js", "./plain.js", "../up/one.js", "not-a-path"]
)
def test_env_and_args_agree_about_the_same_string(value, tmp_path):
    """The two fields ask different questions; they must not give the same
    string two answers WHERE BOTH QUESTIONS APPLY.

    `args` is wider on purpose — a bare `dist/index.js` in a launch position is
    a path, and the same characters in an env value are indistinguishable from a
    secret. That difference is deliberate and tested above. What is NOT
    deliberate is disagreeing about a value that explicitly says `./`: measured
    2026-09-17, `--require=./instrument.js` was refused as an argument and
    accepted as an env value, because only one side ran `_path_candidate`.
    """
    as_arg = kit.cwd_dependent_reason({"command": "node", "args": [value]}, base=tmp_path)
    as_env = kit.cwd_dependent_reason({"command": "node", "env": {"X": value}}, base=tmp_path)
    assert (as_arg is not None) == (as_env is not None), (
        f"{value!r} explicitly states a relative path, and the two fields disagree:\n"
        f"  arg -> {as_arg!r}\n  env -> {as_env!r}"
    )


@pytest.mark.parametrize(
    "entry,names",
    [
        pytest.param({"command": "./s3cret-TOKEN"}, "launch command", id="command"),
        pytest.param({"command": "node", "args": ["./s3cret-TOKEN"]}, "argument 1", id="arg"),
        pytest.param(
            {"command": "node", "env": {"K": "./s3cret-TOKEN"}},
            "`K` environment value",
            id="env",
        ),
    ],
)
def test_no_field_of_the_cwd_refusal_prints_its_value(entry, names):
    """One rule across all three positions, because the first fix got one.

    ⚠ Redacting `args` and `command` reddened NOTHING when it was applied — no
    test asserted the raw value was printed, which is exactly why it shipped.
    So the promise is pinned per FIELD rather than per example.

    The refusal must stay actionable: it still names WHICH position, and the
    hidden label still says which shape the value has."""
    reason = kit.cwd_dependent_reason(entry)
    assert reason is not None, f"{entry} should be refused"
    assert "s3cret-TOKEN" not in reason, f"the refusal printed the value: {reason}"
    assert names in reason, f"the refusal no longer says which position: {reason}"
    assert kit.HIDDEN in reason, f"the value is not marked as withheld: {reason}"


def test_the_cwd_refusal_never_prints_the_env_value_it_names(tmp_path, kit_home, capsys):
    """⚠ A CREDENTIAL LEAK, measured end to end 2026-09-17 and fixed here.

    `cwd_dependent_reason` interpolated the raw env value into its reason, and
    `cmd_setup` prints that reason. So `setup` wrote a person's real
    `AWS_SECRET_ACCESS_KEY` to stderr — on a config with nothing wrong with it,
    because of the sibling defect above.

    `redact_entry`'s docstring already says why this is worse than an ordinary
    CLI leak: "this kit is narrated by an agent: whatever it prints is read into
    a model's context by design". And `try/CLAUDE.md:53` tells the agent never
    to read out a value the commands hid — which only holds if the commands hide
    it.

    Driven through `kit.main`, not the pure function. The leak needed BOTH the
    reason and the printer to be wrong, and a unit test on the reason alone
    would not have shown it reaching a terminal.

    ⚠ IT IS A WARNING ON A SUCCESSFUL RUN NOW (2026-09-17), which makes the leak
    WORSE rather than better, and is why this test matters more after that
    change than before it. A refusal is printed once and the run stops. This
    text sits in setup's normal output — the output `try/CLAUDE.md` step 2 tells
    the agent to paste into its reply, in a code block, because tool output is
    folded. So the value would travel from stderr into the transcript."""
    secret = "./s3cret/ToKeN-not-a-real-key"
    data = {
        "mcpServers": {"srv": {"command": "npx", "args": ["-y", "srv"], "env": {"API_KEY": secret}}}
    }
    path = _config(tmp_path, data)

    assert kit.main(["setup", "srv", "--src-config", str(path)]) == 0
    out, err = capsys.readouterr()

    assert secret not in err, f"setup printed the env value verbatim:\n{err}"
    assert secret not in out, f"setup printed the env value verbatim:\n{out}"
    # The warning still has to be USABLE: it names the key, says why, and offers
    # the way out. Without this the assertion above passes on an empty message.
    assert "API_KEY" in out, f"the warning no longer names which value it means:\n{out}"
    assert kit.HIDDEN in out, f"the warning does not mark the value as withheld:\n{out}"
    assert "--in-place" in out, f"the warning no longer offers the way out:\n{out}"


def test_the_guard_reads_the_original_entry_not_the_wrapped_one():
    """The order this runs in is load-bearing, so it is pinned rather than
    described.

    `build_wrapped_entry` demotes the command into `args` and puts
    `sys.executable` — an absolute path — into `command`. Since the arg rule was
    widened, both entries are refused, so the ordering no longer changes the
    VERDICT. It changes what the person is told, and the wrapped one tells them
    something false about their own config: their entry has no fourth argument.
    It has a `command`, which is where the path they need to fix actually is.

    So a refusal that misdirects is the failure this pins, not a missed refusal.
    The kit's refusals are read by someone deciding what to do next, and the
    whole reason this guard exists is that the alternative — finding out in the
    next session — tells them nothing at all."""
    original = {"command": "bin/server"}
    wrapped = kit.build_wrapped_entry(original, interpreter="/usr/bin/python3.13", **WRAP_ARGS)

    before = kit.cwd_dependent_reason(original)
    after = kit.cwd_dependent_reason(wrapped)
    assert before is not None and after is not None, "both shapes carry the same broken path"

    assert "launch command" in before, f"their entry has it in `command`: {before}"
    assert "argument 4" in after, (
        "the wrap is expected to leave the path as the fourth argument; if that "
        "stops being true, the assertion below is no longer measuring anything"
    )
    assert "argument" not in before, (
        f"the guard ran after the wrap and is naming a position that does not exist "
        f"in the person's entry: {before}"
    )


# =============================================================================
# 0.6.0: the kit sends nothing, and the console holds a copy of the paste.
#
# Two facts about this release, and each one is a claim some other repository or
# some other document is relying on.
#
# The kit lost `upload`, `upload.py` and the credential we used to email, so
# "nothing here sends it" is literally true again: §9.1's grep returns no call
# site under `try/`, and the trial ends with a person signing in to Baton and
# choosing a file. What is left to guard is that no surface still tells them
# otherwise, because a removed command that a document still names is worse than
# one that exists.
#
# And the Baton Proxy page renders a COPY of the paste, pinned to a named kit
# version rather than fetched from here when the page renders. Nothing in either
# repository can see the other, so the pin below is the only thing that can
# notice the two drifting apart.
# =============================================================================


PASTE_SEPARATOR = "\n---\n"


def _paste() -> str:
    """The text the console copies: everything below the rule in `PROMPT.md`.

    This IS the definition the hash is over, and `kit.py` states it in the same
    words beside the pin. The preamble above the rule is ours, explaining the
    paste to whoever edits it, and never travels; the blank lines around the
    text are markdown rather than paste, so they are stripped and one newline
    ends it."""
    parts = _prompt_text().split(PASTE_SEPARATOR, 1)
    assert len(parts) == 2, "PROMPT.md no longer separates its preamble from the paste"
    return parts[1].strip() + "\n"


def test_the_paste_is_pinned_to_the_version_that_ships_it():
    """Decision 3 of the Setup-page spec: the console's copy is pinned to a
    named kit version ("try kit v0.6.0"), not fetched at render time. So the
    paste exists twice, in two repositories, and the copy a prospect actually
    pastes is the one this repository cannot see.

    The pin is what notices. Editing the paste breaks the hash; re-pinning the
    hash without moving `__version__` breaks the pair, because `PASTE_VERSION`
    is held equal to it. What comes out is a release whose number says the paste
    changed, which is the thing the console's copy is labelled with and updated
    by hand against.

    The limit, stated rather than papered over: nothing here can check that the
    console was updated, and a paste edited and re-pinned twice inside one
    unreleased version is two edits under one number. What it does guarantee is
    that no paste edit reaches a release without the hash and the version moving
    in the same diff.
    """
    import hashlib

    from baton_proxy import __version__

    digest = hashlib.sha256(_paste().encode("utf-8")).hexdigest()
    assert digest == kit.PASTE_SHA256, (
        "try/PROMPT.md's paste changed and its pin did not. The Baton Proxy page "
        "holds a copy of this text pinned to a kit version, so a paste edit is a "
        "release:\n"
        f"  1. kit.PASTE_SHA256 = {digest!r}\n"
        "  2. bump __version__ and kit.PASTE_VERSION together\n"
        "  3. update the console's copy and the version it is labelled with"
    )
    assert kit.PASTE_VERSION == __version__, (
        f"kit.PASTE_VERSION is {kit.PASTE_VERSION!r} and __version__ is "
        f"{__version__!r}. They name the release the console's copy is pinned "
        "to, so they move together or the label points at the wrong paste."
    )


def test_no_shipped_surface_names_a_way_to_send_the_capture():
    """`upload.json`, `team@goodtiming.ai` and `kit.py upload` were the three
    names the old ending was built out of: the credential we emailed, the
    address to mail the file to, and the command that POSTed it. All three are
    gone from the code, and a document that still names one of them is worse
    than the command that no longer exists: it sends a person hunting for an
    email that was never sent, or types a command that exits 2.

    Swept over the whole directory rather than asserted on the files we happened
    to edit, because these strings come back by being pasted in from an older
    draft. The trial's own artifacts are excluded for the same reason §9's grep
    excludes them: a capture can contain any string, including these."""
    retired = ("upload.json", "team@goodtiming.ai", "kit.py upload")
    checked = []
    for path in _audited_files():
        if path.parent.name != "try":
            continue
        checked.append(path.name)
        text = path.read_text(encoding="utf-8")
        for needle in retired:
            assert needle not in text, (
                f"try/{path.name} still names {needle!r}, which 0.6.0 removed"
            )
    assert set(checked) >= {"kit.py", "CLAUDE.md", "SECURITY.md", "PROMPT.md", ".gitignore"}, (
        f"the sweep did not read the whole kit: {sorted(checked)}"
    )


def test_the_kit_has_three_commands_and_upload_is_not_one():
    """The command is gone from `main`, not merely undocumented. A parser that
    still accepted `upload` would leave the agent one refusal away from a code
    path this release deleted, and `CLAUDE.md`'s "there is no command that
    sends" would be a sentence rather than a fact."""
    parser_commands = set()
    for line in KIT_PATH.read_text(encoding="utf-8").splitlines():
        m = re.search(r'sub\.add_parser\("(\w+)"', line)
        if m:
            parser_commands.add(m.group(1))
    assert parser_commands == {"setup", "receipt", "uninstall"}, parser_commands
    with pytest.raises(SystemExit) as e:
        kit.main(["upload"])
    assert e.value.code == 2, "`kit.py upload` is still a command argparse accepts"


def test_the_doc_forbids_sending_without_naming_an_exception():
    """The rule used to carry its own exception, "never send it EXCEPT
    through `kit.py upload`", and an exception is the part of a rule an agent
    reasons from. There is no command to except now, so the rule is absolute, and what
    replaces the exception is the answer to the question that produced it: if
    someone asks the agent to send the file, the answer is that they upload it
    themselves."""
    doc = _flat(_claude_md())
    assert "**Never send the file anywhere.**" in doc, "the rule lost its absolute form"
    assert "There is no command that sends" in doc, (
        "the rule asserts a promise without the fact that makes it keepable"
    )
    assert "except" not in doc[doc.index("Never send the file anywhere") :][:400].lower(), (
        "the send rule grew an exception again"
    )
    assert "the person uploads it themselves on the Baton Proxy page" in doc, (
        "the doc never says what to answer when someone asks the agent to send it"
    )


# ---------------------------------------------------------------------------
# Run 6: the last step is a drag, and a path in a terminal cannot be dragged.
#
# The upload box on Baton Proxy takes a file. On macOS `open -R` puts a Finder window
# in front of the person with the file already selected, which turns "find this
# path in a file dialog" into something they can see and drag. Linux has no
# portable equivalent worth guessing at, so it is told nothing rather than told
# something that opens the capture in an editor.
#
# Three surfaces have to agree: what `receipt` prints, what `CLAUDE.md` tells
# the agent to run, and what the agent then says. The platform split is the part
# that rots quietly, because CI and the developer are usually on one of the two.
# ---------------------------------------------------------------------------


MACOS_ENDING = (
    "It's at /full/path/to/try/events.jsonl. Finder is showing it. Drag it onto "
    "the upload box on the Baton Proxy page, "
    "https://baton.goodtiming.ai/setup/proxy, and your session is there."
)


def test_the_receipt_reveals_the_capture_on_macos(tmp_path, monkeypatch, capsys):
    """One line under the ending, and only the command: `receipt` is a reporting
    command and does not run things. The agent runs it, and a person reading the
    receipt on their own can type it."""
    monkeypatch.setattr(kit.sys, "platform", "darwin")
    events, out = _run_receipt(tmp_path, monkeypatch, capsys, [_ev(payload={"tool_name": "s"})])
    assert out.rstrip().endswith(f"Reveal it in Finder: open -R {events}"), (
        f"the receipt does not end with the reveal command on macOS:\n{out}"
    )
    # Under the ending, not instead of it: the file's path and the page are what
    # the person acts on, and the reveal is help with the last step of it.
    assert out.index(kit.setup_note(events)) < out.index("Reveal it in Finder"), (
        "the reveal line printed above the line it is helping with"
    )


def test_the_receipt_says_nothing_about_finder_on_linux(tmp_path, monkeypatch, capsys):
    """`open -R` is macOS's. On Linux the ending is exactly what it was, and the
    failure this pins is a receipt that prints a command the person's machine
    does not have at the one moment they are trying to finish."""
    monkeypatch.setattr(kit.sys, "platform", "linux")
    events, out = _run_receipt(tmp_path, monkeypatch, capsys, [_ev(payload={"tool_name": "s"})])
    assert kit.setup_note(events) in out, f"Linux lost the ending it always had:\n{out}"
    for macos_only in ("Finder", "open -R"):
        assert macos_only not in out, f"the receipt offered {macos_only!r} on Linux:\n{out}"
    assert kit.reveal_note(events) is None, "reveal_note answered on a platform without Finder"


def test_the_doc_hands_the_file_over_differently_on_each_platform():
    """Both branches are written out, because the agent has to pick one and the
    two endings are different sentences rather than one sentence with a clause.

    The macOS half is pinned whole for the same reason the shared line is: it is
    said to the person verbatim, and the drag is only possible because the
    command above it ran first."""
    doc = _claude_md()
    flat = _flat_unquoted(doc)
    assert "**On macOS**, run `open -R '/full/path/to/try/events.jsonl'`" in flat, (
        "the doc no longer tells the agent to reveal the file on macOS"
    )
    assert MACOS_ENDING in flat, "the macOS ending is not the sentence the person is told"
    assert kit.SETUP_URL in MACOS_ENDING, "the macOS ending stopped naming the Baton Proxy page"
    assert "**On Linux**, reveal nothing" in flat, (
        "the doc no longer tells the agent to leave the file alone on Linux"
    )
    # The Linux branch is the line `receipt` prints on both platforms, which
    # `test_the_doc_and_the_kit_say_the_same_sentence` holds equal to the code.
    assert _flat(kit.setup_note(Path("/full/path/to/try/events.jsonl"))) in flat, (
        "the Linux branch is no longer the line the receipt prints"
    )


def test_the_doc_and_the_receipt_name_the_same_reveal_command(monkeypatch):
    """Two places say `open -R`, and a person may act on either: the receipt
    prints it for someone reading alone, the doc has the agent run it. A flag
    that drifted between them would reveal the file in one path and open it in
    an editor in the other."""
    monkeypatch.setattr(kit.sys, "platform", "darwin")
    printed = kit.reveal_note(Path("/full/path/to/try/events.jsonl"))
    assert printed == "Reveal it in Finder: open -R /full/path/to/try/events.jsonl"

    # Compared WITHOUT shell quotes, and that is not a weakening — it is the
    # only honest comparison. `shlex.quote` adds quotes when the path needs
    # them, so the kit's line is bare for this space-free example; the doc shows
    # quotes unconditionally, because an agent filling in a placeholder cannot
    # know whether the real path has a space. Same command, two correct quoting
    # strategies. What must not drift is the command and its flag.
    unquoted = _flat_unquoted(_claude_md()).replace("'", "")
    assert "open -R /full/path/to/try/events.jsonl" in unquoted, (
        f"the doc does not run the command the receipt prints:\n  {printed}"
    )

    # ⚠ The case this test did not cover until 2026-09-18, and the reason the
    # doc grew quotes at all: a folder with a space. The kit must quote here or
    # `open -R` receives two arguments and reveals nothing.
    spaced = kit.reveal_note(Path("/Users/x/Client Work/baton-proxy/try/events.jsonl"))
    assert spaced == (
        "Reveal it in Finder: open -R '/Users/x/Client Work/baton-proxy/try/events.jsonl'"
    ), f"the receipt stopped quoting a path with a space: {spaced}"


# ---------------------------------------------------------------------------
# Run 6, the document half: two things the kit does that §5, §6 and §2 did not
# say. Both are checkable against the code, which is the only kind of claim
# this document makes, so both are checked here rather than believed.
# ---------------------------------------------------------------------------


def test_security_md_says_what_the_scrubber_does_not_see(tmp_path):
    """§6 said "every payload passes through `Scrubber`", which was true and
    was read as "everything does". `runtime_meta` does not: `_emit` scrubs
    `payload` and hands the client's `_meta` to the event untouched
    (`emitter.py`).

    Nothing in it is ours. It is whatever the client attached to its own
    request, which for Claude Code is a tool-use id and a progress token. But
    "what is recorded" and "what is redacted" are the two questions this
    document exists to answer, and it answered the second one for one field
    while implying both.

    Driven through a real emitter rather than read off the source: the claim is
    about what lands in the file. If the scrubber is ever extended over
    `runtime_meta`, this fails, and §5 and §6 are what it is telling you to
    change."""
    from baton_proxy.config import Config
    from baton_proxy.emitter import Emitter

    sink = tmp_path / "events.jsonl"
    emitter = Emitter(
        Config(
            session_id="s",
            event_sink=f"file://{sink}",
            tenant_id="t",
            api_key=None,
            consent_token="c",
            vendor_id="v",
            log_file=None,
        )
    )
    emitter.start()
    emitter.enqueue_tool_call_start(
        tool_name="echo",
        params={"note": "mail dave@example.com"},
        runtime_meta={"claudecode/toolUseId": "tu_1", "progressToken": 3},
    )
    emitter.stop(timeout=5.0)

    event = json.loads(sink.read_text(encoding="utf-8").splitlines()[0])
    assert "dave@example.com" not in json.dumps(event["payload"]), (
        "the payload reached the file unscrubbed, which §6 says cannot happen"
    )
    assert event["runtime_meta"] == {"claudecode/toolUseId": "tu_1", "progressToken": 3}, (
        "runtime_meta no longer reaches the file as the client sent it; §5 and §6 "
        f"say it does: {event.get('runtime_meta')!r}"
    )

    doc = _flat((KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8"))
    assert (
        "the `_meta` object your client attached to the request, recorded as it arrived" in doc
    ), "§5 never says the client's `_meta` is recorded"
    assert "the `runtime_meta` object of §5 is written as your client sent it" in doc, (
        "§6 still implies the scrubber sees everything"
    )


def test_security_md_says_the_bridge_does_not_hold_the_server_stream():
    """§2 and §5 both say it now, because it decides what a remote wrap can
    capture: the bridge POSTs each client message and reads the response off
    that same POST, and never opens the standing GET SSE channel the transport
    allows. So sampling, elicitation and server notifications never pass through
    it and are not in the capture.

    Pinned on the request methods rather than on a comment: opening that channel
    is one `method="GET"` away, and it would make a sentence in §2, a paragraph
    in §5 and the scope of the whole remote wrap wrong at once."""
    source = (REPO_ROOT / "src" / "baton_proxy" / "transport_http.py").read_text(encoding="utf-8")
    assert re.findall(r'method="(\w+)"', source) == ["POST"], (
        "transport_http.py issues a request that is not the POST loop; §2 and §5 "
        "tell a reviewer server-initiated messages never reach the proxy"
    )
    doc = _flat((KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8"))
    assert (
        "server-initiated messages (sampling, elicitation, notifications) are not "
        "carried by it and are not captured" in doc
    ), "§2's remote section never says what the bridge leaves behind"
    assert "does not open the standing GET SSE channel" in doc, (
        "§5 stopped saying why server-initiated messages are missing"
    )


def test_security_md_names_the_headers_that_announce_the_proxy():
    """A remote wrap sends the upstream two headers that say a proxy is there,
    and a request the upstream never saw before the wrap is a change on the wire
    that §2 exists to list. Read off the headers the bridge builds, not a copy
    of them: a renamed `Via` or a new identifying header fails here first."""
    from baton_proxy import USER_AGENT, __version__
    from baton_proxy.transport_http import StreamableHttpClient

    headers = StreamableHttpClient("https://example.invalid/mcp")._headers(is_initialize=True)
    named = {k: v for k, v in headers.items() if "baton-proxy" in v}
    assert set(named) == {"User-Agent", "Via"}, f"headers naming the proxy: {named}"
    assert named["User-Agent"] == USER_AGENT

    doc = _flat((KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8"))
    section = doc[
        doc.index("### Remote entries, specifically") : doc.index("## 3. What your agent")
    ]
    shown = {"User-Agent": USER_AGENT.replace(__version__, "<version>"), "Via": named["Via"]}
    for name, value in shown.items():
        assert f"`{name}: {value}`" in section, f"§2's remote section never names {name}"
    assert "A stdio wrap sends neither" in section, "§2 does not say which path sends them"


def test_security_md_scopes_stderr_to_what_actually_goes_there():
    """§7 said events are not mirrored to stderr, which is true and narrower than
    it reads: the proxy's status lines go there on every wrap, unscrubbed, and
    the client may keep them. Pinned on the mechanism each sentence names, so the
    prose moves when the code does."""
    import inspect

    from baton_proxy import proxy as proxy_mod
    from baton_proxy.config import DEFAULT_EVENT_SINK
    from baton_proxy.transport_http import _safe_read_snippet

    limit = inspect.signature(_safe_read_snippet).parameters["limit"].default
    assert "logging.StreamHandler(sys.stderr)" in inspect.getsource(proxy_mod._configure_logging)

    doc = _flat((KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8"))
    section = doc[doc.index("## 7. Where the data lives") : doc.index("## 8. Provenance")]
    assert f"the default is `{DEFAULT_EVENT_SINK}`" in section, "§7 misquotes the proxy's default"
    assert "that is the kit's doing" in section, "§7 credits the proxy with the kit's property"
    assert "Status lines do go to stderr, and the scrubber never sees them" in section
    assert f"the first {limit} bytes of the response body" in section, (
        f"§7's error-body size no longer matches `_safe_read_snippet` ({limit})"
    )


# §3a described the GLOBAL wrap in all three rows for a whole release, and
# nothing went red when it was fixed — which is why these two pins exist. The
# defect was found by an agent reading the doc during a V1 drive, not by the
# suite. See `_MODE_BOUND_CLAIMS` for the same rule applied to `CLAUDE.md`.
#
# The owner is this list, never a count: §3a has three rows today and the sweep
# has to keep working when it has four.
_SECURITY_3A_ROWS = [
    ("`setup <server>`", ("your config is not written",)),
    ("`receipt`", ("never opens your config",)),
    # ⚠ `events.jsonl` survives uninstall in BOTH modes — `_finish_uninstall`
    # is shared and calls `_print_left_behind` on either path. Splitting this
    # row by mode dropped that disclosure from the default column, leaving a
    # security reader told only what gets DELETED, about a file §5 says holds
    # full tool results. Found by /code-review, 2026-09-18.
    ("`uninstall`", ("nothing of yours was changed", "leaves `try/events.jsonl`")),
]

#: Phrases that describe writing THEIR config. Legal in the `--in-place`
#: column, a defect in the default one. `config-backup` is here because the
#: backup exists only because the global path rewrites their file.
_WRITES_THEIR_CONFIG = ("rewrites one entry", "config-backup", "writes the original entry back")


def _security_md() -> str:
    return (REPO_ROOT / "try" / "SECURITY.md").read_text(encoding="utf-8")


def _probe(text: str) -> str:
    """Lowercased, unbolded, one-space text — what a substring probe may match.

    The doc is hard-wrapped and uses `**bold**`, so a probe written as a
    sentence fails on a line break or an asterisk and reads as "the promise is
    gone" when the promise is right there. Matching the rendered words is what
    these pins are actually about."""
    return " ".join(text.replace("*", "").split()).lower()


def _section_3a_rows() -> list[list[str]]:
    """§3a's table, as a list of cell-lists, header included."""
    doc = _security_md()
    start = doc.index("### 3a.")
    table = doc[start : doc.index("\n## ", start)]
    rows = [ln for ln in table.splitlines() if ln.startswith("|")]
    assert rows, "§3a no longer has a table — re-point this test, do not delete it"
    return [[c.strip() for c in ln.strip("|").split("|")] for ln in rows]


def test_security_3a_splits_every_command_by_mode():
    """§3a must say which wrap each row is about, and the default column must
    never claim we write their config.

    This is the defect a prospect would have found first: §3a is the security
    document, and the people who read it are the ones who read code. It told
    them `setup` backs up and rewrites their config — the exact sentence the
    project-mode feature exists to stop saying — while the code had not done
    that by default since K1b.

    Cell-level rather than paragraph-level, deliberately. The whole table is one
    paragraph, so a `_MODE_BOUND_CLAIMS`-style window check would be satisfied
    by the `--in-place` column alone and would pass on a default column that
    still described the global wrap."""
    rows = _section_3a_rows()
    header = rows[0]
    assert len(header) == 3, (
        f"§3a's table has {len(header)} columns, not 3 — the by-mode split is gone, "
        "and an unqualified row is how this broke the first time"
    )
    assert "--in-place" in header[2], "§3a's third column no longer names --in-place"

    body = {r[0]: r for r in rows[2:]}
    for command, default_promises in _SECURITY_3A_ROWS:
        assert command in body, (
            f"§3a has no row for {command}. ZERO means it was reworded or dropped — "
            "re-point this row at the new wording, because a row matching nothing "
            "guards nothing."
        )
        default_cell, in_place_cell = _probe(body[command][1]), _probe(body[command][2])
        for default_promise in default_promises:
            assert default_promise in default_cell, (
                f"§3a's default column for {command} no longer promises {default_promise!r}"
            )
        for phrase in _WRITES_THEIR_CONFIG:
            assert phrase not in default_cell, (
                f"§3a says {phrase!r} in {command}'s DEFAULT column. That describes the "
                "--in-place wrap, and stating it unqualified is what made this document "
                "tell three prospects the opposite of what the kit does."
            )
        assert in_place_cell, f"§3a's --in-place column for {command} is empty"


def test_security_discloses_the_settings_file_the_repo_ships():
    """`.claude/settings.json` pre-approves our own commands, so §3a has to
    name it.

    Driven from the FILE, not from a list written here: if someone adds a
    command to the allow-list, this reds until the disclosure covers it. A doc
    test that only greps its own sentences passes on a doc that has drifted
    away from the thing it describes — the same reason
    `test_every_mode_bound_promise_names_the_in_place_exception` reaches for
    what the kit actually prints.

    Found by an agent reading the repo during a V1 drive, unprompted, in one
    pass. The doc's whole job is disclosure and it did not mention a file we
    ship that grants something."""
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    assert settings_path.is_file(), (
        "`.claude/settings.json` is gone. If that is deliberate, delete this test and "
        "the SECURITY.md paragraph together — a disclosure of a file that no longer "
        "exists is its own defect."
    )
    allow = json.loads(settings_path.read_text(encoding="utf-8"))["permissions"]["allow"]
    doc = _security_md()

    # To the end of the section, not to the first blank line. The wildcard
    # warning is its own paragraph, and stopping at `\n\n` read only the
    # reassuring half — which is how the understatement got written in the
    # first place.
    start = doc.index("`.claude/settings.json`")
    disclosure = _probe(doc[start : doc.index("`try/CLAUDE.md` is a plain-text", start)])

    # ⚠ The token AFTER `kit.py`, not the last token on the line. Taking the
    # last one collapsed `Bash(python3 kit.py setup *)` to the empty string
    # (`"*)".rstrip(")*") == ""`), and `"" in disclosure` is always True — so
    # the wildcard rule, the only one that reaches `--in-place`, was guarded by
    # nothing, and a future `Bash(python3 kit.py doctor *)` would have passed
    # with `doctor` undisclosed. Found by /code-review, 2026-09-18; it is the
    # same silent-pass this file was written to end, one level up.
    verbs = set()
    for rule in allow:
        tokens = rule.rstrip(")").split()
        assert "kit.py" in tokens, f"allow-rule is not a kit command line: {rule!r}"
        verb = tokens[tokens.index("kit.py") + 1]
        assert verb and verb != "*", f"allow-rule names no command: {rule!r}"
        verbs.add(verb)
    for verb in sorted(verbs):
        assert verb in disclosure, (
            f"`.claude/settings.json` pre-approves `{verb}` and §3a's disclosure does not "
            f"name it. The allow-list is the owner; rules today: {allow}"
        )

    # A trailing `*` matches any continuation, so a wildcard rule pre-approves
    # every FLAG of that command too — including `--in-place`, which rewrites
    # the file §3a's table exists to say we leave alone. The disclosure has to
    # say so; "it grants nothing beyond those command lines" was literally true
    # and read as a reassurance.
    if any(rule.rstrip(")").endswith("*") for rule in allow):
        # ⚠ NOT the bare token. `--in-place` appears twice in this paragraph and
        # only one of them is the grant; a mutant that deleted the load-bearing
        # sentence left the other in place and the assertion GREEN. Measured
        # 2026-09-18 — the same shape as a checklist probe matching the same
        # directory on a different step.
        assert "also covers `setup <server> --in-place`" in disclosure, (
            "a rule ends in `*`, so it pre-approves that command with ANY flag. The "
            "disclosure has to say the wildcard REACHES --in-place, not merely mention "
            f"the flag somewhere. Rules today: {allow}"
        )
        assert "delete that one line" in disclosure, (
            "the disclosure names the wildcard grant without telling the reader how to "
            "decline it, which is the only part they can act on"
        )
    assert "one level above the clone" in disclosure, (
        "the disclosure no longer says the first session does not load these rules, "
        "which is the fact that makes the grant small rather than alarming"
    )
    assert "still refuses" in disclosure, (
        "the disclosure no longer says the allow-list cannot override the client's own "
        "refusal — measured behaviour, and the reason the hand-over step exists"
    )


#: Every path placeholder the agent is asked to substitute into a command.
#: The list is the owner — no count is written down, because this doc has had
#: three counts go stale within an hour of being written.
_PATH_PLACEHOLDERS = ("<path>", "<folder>", "/full/path/")


def test_every_command_the_agent_fills_a_path_into_is_quoted():
    """A command in `CLAUDE.md` carrying a path must show that path quoted.

    `kit.py` already knew this: `_cd_to` runs the path through `shlex.quote`
    and its docstring says why — *"`/Users/x/Client Work/app` is an ordinary
    macOS path and an unquoted one silently cds to `/Users/x/Client`, which
    loads global scope and captures nothing: the failure this whole line exists
    to prevent."* The doc then spelled the same commands out again without
    quotes, and `reveal_note` was the one printed command in `kit.py` that had
    missed it too.

    Found on the 2026-09-18 V1 drive. The operator's own path had no spaces, so
    every existing test and the whole drive passed over it — which is exactly
    how a rule that is right in one position and absent in its siblings
    survives.

    ⚠ This sweeps COMMANDS, not prose. `> It's at /full/path/to/try/events.jsonl.`
    is a sentence the agent says, not a line anyone pastes, and quoting it would
    be noise. The discriminator is the backtick span or the `!` line, which is
    what a person copies.
    """
    doc = _claude_md()

    commands: list[str] = []
    fenced = False
    for line in doc.splitlines():
        # Fenced blocks count. The docstring's own discriminator is "what a
        # person copies", and `CLAUDE.md`'s command list is a fence — nothing in
        # it carries a placeholder today, which is exactly why leaving it out
        # would never have shown up as a failing test. Found by /code-review.
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        stripped = line.lstrip("> ").strip()
        if fenced:
            if stripped:
                commands.append(stripped)
            continue
        if stripped.startswith("!"):
            commands.append(stripped)
        commands.extend(re.findall(r"`([^`]+)`", line))

    hits = [c for c in commands if any(ph in c for ph in _PATH_PLACEHOLDERS)]
    assert hits, (
        "no command in CLAUDE.md carries a path placeholder any more. ZERO means the "
        "hand-over lines were reworded or removed — re-point _PATH_PLACEHOLDERS at the "
        "new spelling, because a sweep that matches nothing guards nothing."
    )

    for command in hits:
        for placeholder in _PATH_PLACEHOLDERS:
            # EVERY occurrence, and only the characters immediately bracketing
            # it. The first version asked whether a quote appeared anywhere
            # after the placeholder, which passed on `cd <folder> && echo
            # 'done'` and on the unterminated `cd '<folder>` — measured, both.
            # A guard satisfied by a quote belonging to a different argument is
            # the exact shape this sweep exists to catch.
            spans = [(m.start(), m.end()) for m in re.finditer(r"'[^']*'", command)]
            start = 0
            while (i := command.find(placeholder, start)) != -1:
                start = i + len(placeholder)
                inside = any(a < i and start <= b for a, b in spans)
                assert inside, (
                    f"CLAUDE.md spells out {command!r} with {placeholder} not wrapped in "
                    "single quotes on BOTH sides. A folder name with a space is ordinary, "
                    "and an unquoted path stops at the first one: the command then succeeds "
                    "against the WRONG directory and says nothing. Quote it, as `_cd_to` "
                    "and `reveal_note` do."
                )


def test_the_doc_says_to_keep_the_quotes_and_not_only_shows_them():
    """Showing quotes is not enough; the rule has to be stated.

    An agent filling `<folder>` with a path that has no spaces has every reason
    to drop quotes it reads as noise, and the result looks correct on the
    operator's machine and breaks on the prospect's. The same class as the
    handover block itself: the doc's version of a command silently lost what the
    code put there on purpose."""
    rules = _doc_section("**Keep the quotes when you fill a path", "**A warning is not a refusal")
    flat = _flat(rules)
    assert "stops at the first space" in flat, (
        "the rule no longer says WHAT goes wrong, and a rule without its reason is the "
        "first thing an agent reasons its way around"
    )
    assert "no spaces is not a reason" in flat, (
        "the rule no longer covers the case that actually fires: a path that looks safe, "
        "on a machine that is not the prospect's"
    )
    assert "relay it exactly as printed" in flat, (
        "the rule no longer tells the agent to prefer the kit's printed line over "
        "rebuilding the command, which is the only version guaranteed correct"
    )
    # ⚠ The doc says QUOTE ALWAYS and the kit quotes CONDITIONALLY
    # (`shlex.quote` emits nothing for a space-free path, which is nearly every
    # real machine). An earlier draft of this rule claimed the kit always
    # quotes; that was false, and it contradicted this file's own comment at
    # `test_the_doc_and_the_receipt_name_the_same_reveal_command`. An agent
    # believing it reads a correct receipt as broken and "repairs" it, against
    # the same paragraph's instruction to relay it as printed.
    assert "no spaces has no quotes and is still correct" in flat, (
        "the rule no longer explains that the kit quotes only when the path needs it, "
        "so the doc and the kit's real output look like a contradiction"
    )
    assert "not in disagreement" in flat, (
        "the rule no longer tells the agent the two quoting strategies AGREE, which is "
        "the sentence that stops it from editing the kit's line"
    )

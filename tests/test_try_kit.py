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
import urllib.error
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


def test_the_receipt_hands_over_a_command_that_cannot_destroy_the_file(
    tmp_path, monkeypatch, capsys
):
    """The last step of the trial is "send it, or do not", and the file can be
    large enough that size is the whole obstacle. The receipt used to stop at
    "read it before you decide" and leave the person holding a file with no next
    move.

    `gzip -c … > …` and not `gzip …`: the bare form REPLACES the original, and
    trial data is not reproducible. A command printed in a document a stranger
    pastes without reading is not where that should be discovered."""
    events, out = _run_receipt(tmp_path, monkeypatch, capsys, [_ev(payload={"tool_name": "s"})])
    assert f"gzip -c {events} > {events}.gz" in out
    assert f"gzip {events}" not in out, "the bare form deletes the source"


def test_the_receipt_names_an_address_but_never_a_place_to_upload_to(tmp_path, monkeypatch, capsys):
    """This test used to assert the opposite, and the reversal is the point.

    It read: the receipt "must not name, offer, or imply a place to send it",
    on the reasoning that a destination costs the one sentence the kit sells.
    That reasoning holds against an UPLOAD and not against an address, and the
    difference is who does the sending. `kit.py` still has no network call, so
    "nothing here sends it" stays literally true and §9.1's grep stays at its
    six adjudicated matches; what changes is that the person is no longer left
    to guess where a file they have decided to release should go.

    So the pin moves rather than lifting: an address, yes; a URL, an endpoint,
    or anything the kit itself would talk to, no.

    Moved a second time when `kit.py upload` shipped, and the surviving half is
    the one that was ever load-bearing. "Nothing here sends it" is gone because
    it became false — there is now a command that sends it — and pretending
    otherwise in the one document a reviewer greps is the failure this file
    exists to prevent. What still holds, and holds for every kit whether or not
    it was provisioned: the receipt names an address and never an endpoint, and
    nothing moves without the person."""
    _events, out = _run_receipt(tmp_path, monkeypatch, capsys, [_ev(payload={"tool_name": "s"})])
    assert kit.TEAM_EMAIL in out, f"the receipt still ends with nowhere to send it:\n{out}"
    for scheme in ("http://", "https://"):
        assert scheme not in out, f"the receipt offered an endpoint ({scheme})"
    assert "only if you run it yourself" in out, "the receipt stopped saying who does the sending"
    assert "nothing here sends it" not in out, (
        "a sentence that is no longer true came back: `upload.py` sends it"
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
    """
    home = tmp_path / "kit-home"
    home.mkdir()
    monkeypatch.setattr(kit, "TRY_DIR", home)
    monkeypatch.setattr(kit, "STATE_PATH", home / "state.json")
    monkeypatch.setattr(kit, "EVENTS_PATH", home / "events.jsonl")
    return home


def _config(tmp_path, data) -> Path:
    path = tmp_path / "mcp.json"
    path.write_text(canonical(data), encoding="utf-8")
    return path


def test_setup_returns_zero_through_main(tmp_path, kit_home, capsys):
    """The success leg of the 0/1/2 contract, driven the way the agent drives
    it. `cmd_setup` returning 0 is already implied by other tests; that `main`
    hands that 0 back rather than swallowing it is not."""
    path = _config(tmp_path, GLOBAL_ONLY)
    rc = kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "trial-t"])
    assert rc == 0
    assert (kit_home / "state.json").exists()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("cmd", ["receipt", "uninstall"])
def test_receipt_and_uninstall_reject_the_config_file_flag(cmd, tmp_path, kit_home):
    """CLAUDE.md:84-86 tells the agent `--config-file` is setup-only because the
    path is recorded in state.json, so the other two find it themselves. The
    mechanism is that neither subparser declares the flag — so this is argparse
    RAISING SystemExit(2), not a Refuse returning 1. Asserting the code and not
    merely "it failed" is the point: a 1 here would read to the agent as a
    refusal to relay, and a 2 as its own malformed command."""
    with pytest.raises(SystemExit) as exc:
        kit.main([cmd, "--config-file", str(tmp_path / "mcp.json")])
    assert exc.value.code == 2


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
    rc = kit.main(["setup", "nosuchserver", "--config-file", str(path)])
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
# of a bare count quietly absorbing a swap.
EXPECTED_AUDIT_HITS = {
    ("src/baton_proxy/proxy.py", 1605, "subprocess.Popen("),
    ("src/baton_proxy/transport_http.py", 135, "urllib.request.urlopen(req"),
    ("src/baton_proxy/transport_http.py", 187, "urlopen(timeout=inf) blocks forever"),
    ("src/baton_proxy/sinks.py", 159, "urllib.request.urlopen(req"),
    ("src/baton_proxy/sinks.py", 191, 'boto3.client("s3")'),
    ("src/baton_proxy/scan.py", 510, "subprocess.run(cmd"),
    # The kit's own, and the only one that exists to send the person's data.
    # `upload.py` puts it in a named function rather than inline as a default
    # argument precisely so this grep can see it — written
    # `opener=urllib.request.urlopen` the call has no paren after the name, and
    # the kit would have gained an egress §9's published check could not find.
    ("try/upload.py", 116, "urllib.request.urlopen(req)"),
}


# What §9's commands exclude, and what `try/.gitignore` lists: the trial's own
# captured data. events.jsonl holds complete tool arguments and results, so a
# reviewer who wrapped a server that talks about `subprocess.run(` has that text
# sitting inside src|try — and the six-match promise is about OUR CODE, not
# about what their agent happened to say. Pinned against try/.gitignore below.
TRIAL_ARTIFACTS = ("events.jsonl", "state.json", "config-backup.*", "upload.json")


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


def test_security_md_section_9_narrow_grep_returns_exactly_its_seven():
    """§9: "Seven matches: the six in the §4 table, plus one comment line in
    transport_http.py."

    It was six until `kit.py upload` shipped, and the count moved in the same
    commit as the code — which is the whole point of pinning it. A reviewer runs
    the printed command and counts; a document that says six over a tree that
    answers seven is the one failure this section cannot survive, because its
    only claim is that its claims are mechanical.

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
    assert len(hits) == 7, (
        "SECURITY.md §9 promises a reviewer SEVEN matches; this grep now returns "
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


def test_the_kit_contributes_exactly_one_audited_call_site_and_it_is_the_uploader():
    """§9 said "The kit contributes none" for as long as that was true. `upload`
    made it one, and the value of the sentence is that it moved with the code.

    The assertion is deliberately tighter than a count: it names the file. A
    second network call anywhere in `try/` — in `kit.py`, or a helper someone
    adds beside it — is the regression this catches, and the whole argument for
    putting the sending in its own auditable file dies quietly without it."""
    kit_hits = [h for h in _grep(NARROW_AUDIT_RE) if h[0].startswith("try/")]
    assert [h[0] for h in kit_hits] == ["try/upload.py"], (
        "the kit's egress is no longer confined to try/upload.py: "
        + repr([(p, n, line.strip()) for p, n, line in kit_hits])
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
    # Both docs write the credential path as one token for exactly this reason:
    # `<path to that file>` would shlex-split into four, and the extra three
    # would reach argparse as positionals `upload` does not take.
    "<path>": "/tmp/upload.json",
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
        ("upload",),
        ("uninstall",),
    }


def test_every_kit_command_in_the_docs_parses(monkeypatch, capsys):
    """Driven through `main` with the three handlers stubbed, so the parse is
    real and nothing runs.

    Through `main` rather than a parser built here, because kit.py builds its
    parser inline and the alternative was to refactor shipped, security-reviewed
    code for testability. The stub buys a second assertion for free: not just
    that argparse accepts the argv, but that it dispatches to the handler the
    document's reader would expect."""
    dispatched: list[str] = []
    for name in ("cmd_setup", "cmd_receipt", "cmd_upload", "cmd_uninstall"):
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


def _kit_home_at(home: Path):
    """Save/restore the three module globals `kit_home` monkeypatches.

    A plain fixture cannot be used here: the composed run is module-scoped (one
    subprocess for several assertions) and `monkeypatch` is function-scoped.
    SRC_DIR stays real deliberately — the wrap must point at the actual proxy
    source, which is the whole thing under test."""
    saved = (kit.TRY_DIR, kit.STATE_PATH, kit.EVENTS_PATH)
    kit.TRY_DIR = home
    kit.STATE_PATH = home / "state.json"
    kit.EVENTS_PATH = home / "events.jsonl"
    return saved


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
                "--config-file",
                str(config_path),
                "--tenant",
                TK_F_8_TENANT,
                "--vendor",
                "toybox",
            ]
        )
        assert rc == 0
        entry = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["fixture"]
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
        kit.TRY_DIR, kit.STATE_PATH, kit.EVENTS_PATH = saved


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
                "--config-file",
                str(config_path),
                "--tenant",
                TK_F_9_TENANT,
                "--vendor",
                "toybox-remote",
            ]
        )
        assert rc == 0
        entry = json.loads(config_path.read_text(encoding="utf-8"))["mcpServers"]["remote"]

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
        kit.TRY_DIR, kit.STATE_PATH, kit.EVENTS_PATH = saved
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
    rc = kit.main(["setup", "--config-file", str(path)])
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

    That difference is not cosmetic: `CLAUDE.md` gates two extra warnings on it —
    that a process of ours will hold their bearer token, and that `${VAR}`
    expansion inside `env` is measured behaviour rather than a guarantee. Before
    this, the doc could only tell the agent to go back into the config and infer
    the kind from the presence of a `url`. An inference the agent can skip is a
    warning the person may never hear.

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
    # makes this test follow a rename; it does not stop one. `CLAUDE.md` gates
    # the two warnings named above on the literal word, so a renamed constant
    # with an untouched doc leaves the agent hunting a word no row carries.
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


# --- TK-F-4: --config-file is answered, never quietly abandoned -------------


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
    """TK-F-4. Four ways to point `--config-file` at something unusable.

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

    err = _bad_config_refusal(["setup", "alpha", "--config-file", str(target)], capsys)
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


def test_receipt_branch_two_the_wrap_is_gone(tmp_path, kit_home, capsys):
    """State, but the entry in the config is not the one setup wrote — the
    client rewrites this file continuously, and a hand-restore is common.

    Without this branch the agent sees "no events", walks the restart checklist,
    and lands on a machine where the proxy was never in the path at all."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    path.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")  # restored by hand

    out = _receipt_output(capsys)
    assert "THE WRAP IS GONE" in out
    assert "uninstall" in out, "the branch must offer the way out the doc promises"
    assert "wrap is gone" in _claude_md()


def test_receipt_branch_three_state_but_no_events(tmp_path, kit_home, capsys):
    """Wrapped, still wrapped, nothing captured. The usual answer is that the
    client has not been restarted, and the doc promises "a short checklist"."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    (kit_home / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    return _receipt_output(capsys)


def _receipt_with_redactions(tmp_path, kit_home, capsys, marker_text: str) -> str:
    return _receipt_with_events(
        tmp_path,
        kit_home,
        capsys,
        [
            {
                "event_type": "tool_call_end",
                "session_id": "s1",
                "captured_at": "2026-08-30T10:00:01Z",
                "payload": {"tool_name": "echo", "result": marker_text, "duration_ms": 3},
            }
        ],
    )


def test_a_cc_count_is_printed_with_what_it_cannot_mean(tmp_path, kit_home, capsys):
    """The count is a Luhn checksum over long digit strings, and about 1 in 10
    non-card ids passes it. The first human-led run printed 9 of these and the
    agent explained them as the person's searches returning payment-shaped
    content — a cause nothing in the kit can see. So the number stays and the
    meaning is stated by the kit rather than invented downstream."""
    out = _receipt_with_redactions(
        tmp_path, kit_home, capsys, "order [REDACTED:cc] and [REDACTED:cc]"
    )
    assert "cc×2" in out
    assert "1 in 10 long numeric ids" in out, f"the false-positive rate is not stated:\n{out}"
    assert "not evidence that any card number was in the file" in out
    # The evidence is destroyed at redaction time, so the kit must not offer a
    # way to look: no "check the file" advice can answer this one.
    assert "kept no\n" in out or "kept no copy" in out


def test_the_cc_note_is_not_printed_when_no_cc_matched(tmp_path, kit_home, capsys):
    """It is gated on the category, not on redactions in general. Printing a
    paragraph about card-shaped numbers under a line reporting two emails
    answers a question nobody asked and reads as a hedge on the whole count."""
    out = _receipt_with_redactions(
        tmp_path, kit_home, capsys, "mail [REDACTED:email] and [REDACTED:email]"
    )
    assert "email×2" in out
    assert "Luhn" not in out, f"the cc note fired without a cc match:\n{out}"


def test_the_luhn_false_positive_rate_the_receipt_quotes_is_the_real_one():
    """`1 in 10` is a measured claim printed to a prospect, so it is pinned to
    the scrubber it describes rather than to a comment. Seeded, so a drift in
    `_luhn_valid` or the candidate regex fails here instead of quietly making
    the receipt's sentence false."""
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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
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
    rc = kit.main(["setup", "notion", "--config-file", str(path)])
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
    rc = kit.main(["setup", "remote", "--config-file", str(path)])
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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert not _QUIT_BELIEF.search(out), out
    assert "new" in out.lower(), "nothing says a NEW session is what picks the wrap up"
    assert "running now" in out, f"the already-running session is not warned about:\n{out}"


def test_uninstall_does_not_charge_a_restart_it_does_not_need(tmp_path, kit_home, capsys):
    """Uninstall's true line is gentler still, and it is a different sentence
    from setup's: nothing is pending, nothing is inert, and there is nothing to
    verify afterwards. It said "the change is INERT until you fully restart"."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
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


def test_setup_names_the_project_directory_the_wrapped_entry_loads_in(tmp_path, kit_home, capsys):
    """Finding 11, at the sink that produced it."""
    key = "/Users/someone/work/app"
    path = _project_config(tmp_path, key)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert f"cd {key} && claude" in out, f"the handoff never names the project path:\n{out}"


def test_the_handoff_never_offers_the_kits_own_directory_as_the_place_to_start(
    tmp_path, kit_home, capsys
):
    """The other half, and the one that actually fired: it is not enough that
    the right path appears somewhere — the WRONG one must not be handed over as
    a command. `try/` is where the kit's three commands run; it is never where
    their client starts unless their own config says so."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert f"cd {kit.TRY_DIR} && claude" not in out, out
    assert "baton-proxy/try && claude" not in out, out


def test_a_global_entry_is_not_given_an_invented_directory(global_config, kit_home, capsys):
    """A global entry loads wherever they start from, so naming a directory
    would be a fresh false instruction rather than the same one corrected."""
    path = global_config
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "cd " not in out, f"a global wrap was told to cd somewhere:\n{out}"
    assert "second terminal" in out


def test_the_cd_is_dropped_when_they_are_already_in_the_project_directory(
    tmp_path, kit_home, capsys, monkeypatch
):
    """Dave: "When the current directory already matches the project key, drop
    the `cd`." Telling someone to cd to where they are reads as a step they got
    wrong."""
    here = (tmp_path / "work").resolve()
    here.mkdir()
    monkeypatch.chdir(here)
    path = _project_config(tmp_path, str(here))
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "cd " not in out, f"told to cd to the directory they are standing in:\n{out}"
    assert "second terminal" in out


def test_the_already_wrapped_path_hands_over_the_same_directory(tmp_path, kit_home, capsys):
    """Cold re-entry is the normal case on a multi-day trial, not a fallback:
    windows close, laptops sleep. Someone who re-runs setup gets "already
    wrapped" — and used to get no handoff at all, which is the state the person
    is in precisely when they have lost the first window."""
    key = "/Users/someone/work/app"
    path = _project_config(tmp_path, key)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    assert kit.main(["setup", "notion", "--config-file", str(path)]) == 0
    out, _err = capsys.readouterr()
    assert "Already wrapped" in out
    assert f"cd {key} && claude" in out, f"the second run hands over nothing:\n{out}"


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
        assert kit.start_where(scope, config).startswith(marker), (scope, config)


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


def _wrapped(tmp_path, kit_home, capsys, scope_key: str | None = None):
    path = _project_config(tmp_path, scope_key) if scope_key else _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    return path


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


def test_an_empty_file_under_a_project_scoped_wrap_names_the_directory(tmp_path, kit_home, capsys):
    """Row 4, carrying finding 11's other half. `receipt` is where someone lands
    when the trial produced nothing, so the checklist has to ask the question
    the wrong-directory bug makes decisive — and it can only ask it when the
    entry is project-scoped."""
    key = "/Users/someone/work/app"
    _wrapped(tmp_path, kit_home, capsys, scope_key=key)
    out = _receipt_output(capsys)
    assert _fired(out) == ["No events have been captured yet"], _fired(out)
    # Scoped to the checklist: the header already prints the project key as part
    # of the config location, so asserting over the whole output would pass
    # without the checklist ever asking the question.
    checklist = out[out.index("No events have been captured yet") :]
    assert "loads in" in checklist, f"the checklist never asks the question:\n{checklist}"
    assert key in checklist, f"it asks, but never names the directory:\n{checklist}"


def test_an_empty_file_under_a_global_wrap_invents_no_directory(global_config, kit_home, capsys):
    """The same checklist must not grow a step that is false. A global entry
    loads wherever they start, so "start it from X" would be a new wrong
    instruction replacing the one just fixed."""
    assert kit.main(["setup", "notion", "--config-file", str(global_config), "--tenant", "t"]) == 0
    capsys.readouterr()
    out = _receipt_output(capsys)
    assert _fired(out) == ["No events have been captured yet"], _fired(out)
    checklist = out[out.index("No events have been captured yet") :]
    assert "started from" not in checklist, f"a global wrap was given a directory:\n{checklist}"
    assert "loads in" not in checklist, checklist


def test_the_six_receipt_rows_are_mutually_exclusive(tmp_path, kit_home, capsys):
    """The property that makes the doc's table a table, over every row at once.

    It has failed twice on this file, both times because a case nobody ran had
    two markers in it ([[feedback_invariant_scoped_to_one_field]]). So the cases
    are enumerated here rather than left one-per-test: the failure was never a
    wrong assertion, it was a row nothing exercised."""
    key = "/Users/someone/work/app"

    def fresh() -> None:
        (kit_home / "events.jsonl").unlink(missing_ok=True)
        (kit_home / "state.json").unlink(missing_ok=True)

    # 1 — nothing here at all.
    fresh()
    assert _fired(_receipt_output(capsys)) == ["No setup state found"]

    # 2 — uninstall's leftovers: events, no state.
    fresh()
    _write_events(kit_home, ("d1e2f3a4", 2))
    assert _fired(_receipt_output(capsys)) == [STATE_CLEARED_MARKER]

    # 3 — wrapped, then restored by hand.
    fresh()
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key)
    path.write_text(
        canonical(
            {
                "mcpServers": {},
                "projects": {
                    key: {"mcpServers": {"notion": {"command": "npx", "args": ["-y", "srv"]}}}
                },
            }
        ),
        encoding="utf-8",
    )
    assert _fired(_receipt_output(capsys)) == ["THE WRAP IS GONE"]

    # 4 — wrapped, still wrapped, nothing landed.
    fresh()
    _wrapped(tmp_path, kit_home, capsys, scope_key=key)
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


def test_a_clobbered_wrap_wins_over_the_nothing_called_it_row(tmp_path, kit_home, capsys):
    """Row 3 is checked only when the file is EMPTY, which is one case too few.

    The client rewrites this config continuously — that is why the row exists at
    all. Setup runs, a session starts and records its tool-surface snapshot, the
    client then restores the entry, and every call after that goes to the
    unwrapped server. The file is no longer empty, so row 3 was never consulted
    and row 5 fired instead: an affirmative diagnosis naming two causes, neither
    of which is true, sending the person to `/mcp` to hunt a duplicate that does
    not exist. The old code printed bare counts here, so this is worse than what
    it replaced."""
    key = "/Users/someone/work/app"
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key)
    _write_events(kit_home, ("bee5d1a2", 0))
    path.write_text(
        canonical(
            {
                "mcpServers": {},
                "projects": {
                    key: {"mcpServers": {"notion": {"command": "npx", "args": ["-y", "srv"]}}}
                },
            }
        ),
        encoding="utf-8",
    )
    out = _receipt_output(capsys)
    assert _fired(out) == ["THE WRAP IS GONE"], _fired(out)


def test_a_wrap_clobbered_after_a_real_capture_still_says_so(tmp_path, kit_home, capsys):
    """The same row, with calls in the file. Capture STOPPED, which is the fact
    worth saying, and it is invisible in a total that only ever grows."""
    key = "/Users/someone/work/app"
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key)
    _write_events(kit_home, ("d1e2f3a4", 2))
    path.write_text(
        canonical(
            {
                "mcpServers": {},
                "projects": {
                    key: {"mcpServers": {"notion": {"command": "npx", "args": ["-y", "srv"]}}}
                },
            }
        ),
        encoding="utf-8",
    )
    out = _receipt_output(capsys)
    assert _fired(out) == ["THE WRAP IS GONE"], _fired(out)
    assert _counts_shown(out), "what was captured before it broke still counts:\n" + out


# --- Review findings: `scope is None` is not the same claim as "global" ------
#
# `iter_entries` returns scope None for the TOP LEVEL of whatever file was
# read, and `search_paths`' own docstring says `--config-file` is how a project
# config is reached. So a `.mcp.json` passed with `--config-file` produces
# scope None — and "registered globally, so it loads wherever you start from"
# is then false in the one direction that costs a trial: a `.mcp.json` loads for
# sessions started in its own directory and nowhere else. That is the
# empty-capture-with-an-invisible-cause failure, re-entered through the
# --config-file door.


def _mcp_json(tmp_path) -> Path:
    project = tmp_path / "app"
    project.mkdir()
    path = project / ".mcp.json"
    path.write_text(canonical(GLOBAL_ONLY), encoding="utf-8")
    return path


def test_a_project_config_file_is_not_described_as_loading_everywhere(tmp_path, kit_home, capsys):
    path = _mcp_json(tmp_path)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "loads wherever you start from" not in out, out
    assert str(path.parent) in out, f"the directory that file belongs to is not named:\n{out}"


def test_the_checklist_asks_the_directory_question_for_a_project_config_file(
    tmp_path, kit_home, capsys
):
    """The receipt's half of the same claim: someone whose file is empty gets
    the checklist, and for a non-global config the directory question is the one
    that resolves it."""
    path = _mcp_json(tmp_path)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    out = _receipt_output(capsys)
    checklist = out[out.index("No events have been captured yet") :]
    assert str(path.parent) in checklist, f"the checklist never names it:\n{checklist}"


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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "loads wherever you start from" in out, out
    assert "cd " not in out, out


def test_a_project_path_with_a_space_is_handed_over_as_a_runnable_command(
    tmp_path, kit_home, capsys
):
    """`cd /Users/x/Google Drive/app && claude` cds to `/Users/x/Google` and
    starts the client in the wrong directory — which loads global scope and
    captures nothing, the exact failure this line was added to prevent. Parsed
    with the shell's own rules rather than string-matched, so the assertion is
    that the command WORKS, not that it looks quoted."""
    import shlex

    key = str(tmp_path / "Google Drive" / "app")
    path = _project_config(tmp_path, key)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    line = next(ln for ln in out.splitlines() if "&& claude" in ln)
    argv = shlex.split(line)
    assert argv[:2] == ["cd", key], f"the handed-over command cds elsewhere: {argv}"


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
    expensive: the person is being asked to check a config by hand."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    assert kit.main(["uninstall"]) == 0
    out, _err = capsys.readouterr()
    assert "Nothing was installed" in out, f"uninstall never says it:\n{out}"
    assert str(kit.CHECKOUT) in out, f"the folder is never named:\n{out}"
    assert "deleting that folder removes all of it" in out


def test_the_unverified_branch_does_not_tell_you_to_delete_the_record(
    tmp_path, kit_home, capsys, monkeypatch
):
    """The same fact, and the opposite advice. On the unverified path
    `state.json` is deliberately KEPT as the only record of the original entry,
    so "delete the folder and you are done" would talk someone into destroying
    their recovery record one line under a warning that the restore did not
    match. Nothing-was-installed is still true and still said; what changes is
    the instruction."""
    path = _config(tmp_path, GLOBAL_ONLY)
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
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
# The email ending (plan of record 2026-08-31).
#
# Upload is deferred; email is the route, and it costs nothing from the security
# posture because THEY send the file. `CLAUDE.md`'s "Never send the file
# anywhere" holds verbatim, the receipt's "there is no upload endpoint in this
# kit" stays true, and §9.1's grep contract is untouched — no network call is
# added anywhere.
#
# What changes is that the trial stops ending at a file on a stranger's laptop
# with no named next move. Two halves, and the second is the one the run showed
# we get wrong: the receipt has to name the address, and SETUP has to say the
# ending too — because once they walk away from that window no agent anywhere
# knows this kit exists, and the setup output is the last thing that speaks.
# ---------------------------------------------------------------------------


def test_the_offer_is_withheld_from_a_capture_with_no_calls_in_it(tmp_path, kit_home, capsys):
    """Dave: the "captured" line prints only after the file has been read and
    found to contain calls — never optimistically.

    A handshake-only file is not empty (the surface snapshot is in it), so the
    offer's gate cannot be "are there events". Asking someone to gzip and mail a
    file with nothing in it wastes the one send they will make, and it argues
    with the banner printed just above, which said nothing came down the pipe."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_events(kit_home, ("bee5d1a2", 0))
    out = _receipt_output(capsys)
    assert _fired(out) == [NOTHING_CALLED_MARKER], f"the diagnosis stopped firing:\n{out}"
    assert kit.TEAM_EMAIL not in out, f"offered to send a capture with nothing in it:\n{out}"
    assert "gzip -c" not in out, f"offered to compress a capture with nothing in it:\n{out}"


def test_a_resource_only_capture_is_still_worth_sending(tmp_path, kit_home, capsys):
    """The gate has to count what `summarize` counts. A session that only read
    resources reached the server and produced real data; gating the offer on
    `tool_calls` alone would withhold it from a capture worth having — the same
    defect as calling that session dead, one branch further on."""
    _wrapped(tmp_path, kit_home, capsys)
    _write_raw(kit_home, _resource_session("c0ffee01", 3))
    out = _receipt_output(capsys)
    assert kit.TEAM_EMAIL in out, f"a real capture was given no way out:\n{out}"


def test_the_offer_survives_a_wrap_that_was_clobbered_after_capturing(tmp_path, kit_home, capsys):
    """Capture STOPPED, but what was captured before it stopped is real and is
    the whole reason to send anything. The banner says the wrap is gone; the
    closing block still has to hand over the file."""
    key = "/Users/someone/work/app"
    path = _wrapped(tmp_path, kit_home, capsys, scope_key=key)
    _write_events(kit_home, ("d1e2f3a4", 2))
    path.write_text(
        canonical(
            {
                "mcpServers": {},
                "projects": {
                    key: {"mcpServers": {"notion": {"command": "npx", "args": ["-y", "srv"]}}}
                },
            }
        ),
        encoding="utf-8",
    )
    out = _receipt_output(capsys)
    assert _fired(out) == ["THE WRAP IS GONE"], _fired(out)
    assert kit.TEAM_EMAIL in out, f"a real capture lost its ending to the banner:\n{out}"


def test_setup_hands_over_the_ending_before_the_window_goes_quiet(tmp_path, kit_home, capsys):
    """The structural half. Once they walk away from this window there is no
    agent left that knows the kit is here and nothing in their new session
    mentions Baton, so the ending is given to them here or not at all."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    assert "kit.py receipt" in out, f"setup never says how to come back:\n{out}"
    assert kit.TEAM_EMAIL in out, f"setup never says how the trial ends:\n{out}"


def test_the_ending_setup_hands_over_does_not_claim_a_file_exists_yet(tmp_path, kit_home, capsys):
    """At setup time nothing has been captured and nothing may ever be. The line
    is a conditional about what they will find, not a promise that there is
    something to send — the same optimism the receipt's gate exists to stop, one
    step earlier and harder to notice."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    out, _err = capsys.readouterr()
    tail = out[out.index("kit.py receipt") :]
    assert "if there is something in it" in tail.lower(), (
        f"setup's ending reads as though a capture already exists:\n{tail}"
    )


def test_the_already_wrapped_path_hands_over_the_ending_too(tmp_path, kit_home, capsys):
    """Cold re-entry is the normal case on a multi-day trial, and it is exactly
    the person who has lost the window that carried the ending the first time."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
    capsys.readouterr()
    assert kit.main(["setup", "notion", "--config-file", str(path)]) == 0
    out, _err = capsys.readouterr()
    assert "Already wrapped" in out
    assert kit.TEAM_EMAIL in out, f"the re-entry hands over no ending:\n{out}"


def test_the_doc_and_the_kit_name_the_same_address():
    """Same shape as §4's injected-param pin, and the same failure it caught: a
    document naming a different address than the code prints is wrong in the one
    place a person acts on it, and every test stays green."""
    doc = _claude_md()
    assert kit.TEAM_EMAIL in doc, f"CLAUDE.md never names {kit.TEAM_EMAIL}"
    others = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", doc)) - {kit.TEAM_EMAIL}
    assert others <= {"security@goodtiming.ai"}, f"CLAUDE.md names another address: {others}"


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
    # with them: a clobbered capture is real, so it gets the decision rather
    # than a checklist the row does not print.
    assert "when the wrap is gone the counts above the banner are real" in ending, (
        f"the clobbered capture is handled as an empty one again:\n{ending}"
    )
    assert "hand over the decision" in ending, (
        f"the wrap-gone case keeps the checklist and loses the offer:\n{ending}"
    )


def test_setups_come_back_line_follows_the_kit_directory_under_test(tmp_path, kit_home, capsys):
    """`kit_home` monkeypatches `kit.TRY_DIR`, which a module-level f-string
    freezes past. Production is unaffected — `TRY_DIR` comes off `__file__` —
    but every setup assertion would then be reading the developer's own
    checkout path, and a future pin on "the come-back line names the kit
    directory" would pass while checking the wrong one."""
    path = _project_config(tmp_path, "/Users/someone/work/app")
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t"]) == 0
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
    approved the trial by reading it". The email ending added a user-facing exit
    the document never described.

    Not a contradiction: §4 says nothing Baton records leaves, and nothing does,
    because the person attaches the file themselves. But Dave's own argument for
    disclosing upload applies unchanged — someone who reads the security page
    and then meets an unmentioned address at the end re-reads the whole document
    as a setup for the ask. The document works because it volunteers."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text()
    section = doc[doc.index("## 4. What leaves your machine") : doc.index("## 5. What is recorded")]
    assert kit.TEAM_EMAIL in section, "§4 never mentions the address the receipt prints"
    # Was "no upload endpoint", which the document could say while it was true.
    # `upload` is the second user-facing exit and gets the same treatment the
    # address got, for the same reason: a reviewer who meets it at the end of a
    # trial this page never described re-reads the whole page as a setup.
    assert "kit.py upload" in section, "§4 never mentions the command that sends the file"
    assert "refuses without `try/upload.json`" in section, (
        "§4 does not say what stops `upload` on a kit we handed to nobody"
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
    assert kit.main(["setup", "notion", "--config-file", str(path)]) == 0
    out, _err = capsys.readouterr()

    entry = json.loads(path.read_text())["mcpServers"]["notion"]
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
    assert kit.main(["setup", "notion", "--config-file", str(path), "--tenant", "t2-kit-run"]) == 0
    entry = json.loads(path.read_text())["mcpServers"]["notion"]
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


def test_claude_md_tells_the_agent_to_say_it_before_setup_runs():
    """The doc and the terminal are two sinks for one claim. Disclosure that
    only exists in a document nobody opened does not stop the surprise — and by
    the time the tab is open there is no good moment to explain it."""
    md = _claude_md()
    para = next(text for _n, text in _unwrapped(md) if "signs them in to something" in text)
    assert "say so before setup" in para, "the warning is not tied to a moment"
    assert "Ask; if they do not know" in para, (
        "the agent is not told to ask, so it will infer from the config and be wrong"
    )
    assert "may happen" in para, "an agent told to predict this will overstate it"


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
# TK-FL-1 — one answer for what comes first (followability run
# `follow-20260831-163630`, finding 1).
#
# The opening says read `SECURITY.md` "before you do anything else". Two
# sections later, *Start by finding out where you are* said "begin with
# `python3 kit.py receipt`". Both were "first", and the model resolved it
# differently across samples of the same run — `a2` ran `receipt` before
# `SECURITY.md` in one and after it in the other. Nothing broke either way, but
# an instruction two runs of one model order differently is not an instruction,
# it is a coin flip, and the next reader may resolve it somewhere worse.
#
# The order is now total and stated at the site that created the collision: the
# lead into `receipt` defers to `SECURITY.md` rather than competing with it. So
# the check is structural — the paragraph ABOVE the receipt command, whatever it
# is reworded to — rather than a marker my own sentence supplies, which a
# rewrite would retire silently.
# ---------------------------------------------------------------------------

# The receipt command as `_unwrapped` sees it: a fenced block with no blank
# lines is one paragraph, joined on spaces. Not the cheat-sheet block at the top
# of the file, which carries all three commands and their `#` comments.
_THE_RECEIPT_BLOCK = "``` python3 kit.py receipt ```"


def _first_run_lead(text: str) -> tuple[int, str]:
    """The paragraph immediately above the receipt command — the sentence that
    tells the agent where to start."""
    paras = list(_unwrapped(text))
    at = [i for i, (_n, p) in enumerate(paras) if p == _THE_RECEIPT_BLOCK]
    assert at, (
        "the receipt command this ordering is measured against is gone or "
        "reformatted — re-point the marker rather than deleting the check"
    )
    return paras[at[0] - 1]


def _competing_first_offenders(text: str) -> list[str]:
    n, lead = _first_run_lead(text)
    if "SECURITY.md" not in lead:
        return [f"try/CLAUDE.md:{n}: {lead[:120]}"]
    return []


def test_claude_md_gives_one_answer_for_what_comes_first():
    md = _claude_md()
    paras = list(_unwrapped(md))
    opening = [i for i, (_n, p) in enumerate(paras) if "before you do anything else" in p]
    assert opening, (
        "the opening no longer claims an absolute first — if that claim moved, "
        "re-point this check; if it went away, the deferral below is now dangling"
    )
    assert "SECURITY.md" in paras[opening[0]][1], "the absolute first names no document"

    lead_at = next(i for i, (_n, p) in enumerate(paras) if p == _THE_RECEIPT_BLOCK)
    assert opening[0] < lead_at, "the document tells the agent to run before it tells it to read"
    assert not _competing_first_offenders(md), (
        "two sections claim to be first and neither yields:\n"
        + "\n".join(_competing_first_offenders(md))
    )


# Verbatim as it sat in CLAUDE.md at `cccc383`, wrapped exactly as it was — so
# the check is graded on the shape it actually has to catch
# ([[feedback_control_condition_must_be_able_to_fail]]).
_THE_LEAD_AS_IT_WAS_WRITTEN = """The person may be at any point in the trial — the session that set this up is
probably long gone. So begin with:

```
python3 kit.py receipt
```
"""


def test_the_competing_first_check_would_notice_the_lead_it_was_written_for():
    assert _competing_first_offenders(_THE_LEAD_AS_IT_WAS_WRITTEN), (
        "the check would have passed on the prose it exists to catch"
    )


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
    """The paste now travels two ways. A provisioned handover goes out as two
    attachments — this file and their `upload.json` — so it gets opened from a
    downloads folder, and step 1's "the current directory" quietly means
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


def test_the_prompt_says_which_client_before_it_asks_for_anything():
    """Spec §7's second half names the paste as the site for this, and the paste
    is the only surface that is read before a clone is approved. The reason has
    to travel with the claim — `~/.claude.json` is what makes it checkable
    rather than a thing we assert about ourselves."""
    paras = list(_unwrapped(_prompt_text()))
    said = [i for i, (_n, p) in enumerate(paras) if "works with Claude Code" in p]
    assert said, "the prompt never says which client the kit is for"
    assert "~/.claude.json" in paras[said[0]][1], (
        "the reason is what makes the claim checkable, not the claim"
    )
    # The clone is the first thing it costs them. A missing marker FAILS rather
    # than defaulting to the end of the file, so a reworded step 1 cannot retire
    # this ordering silently ([[feedback_control_condition_must_be_able_to_fail]]).
    costs = [i for i, (_n, p) in enumerate(paras) if "Clone https://" in p]
    assert costs, "the paragraph this ordering is measured against is gone — re-point it"
    assert said[0] < costs[0], "the prompt discloses the client assumption after the clone"


def _flat(text: str) -> str:
    """Hard-wrapped markdown with the newlines collapsed. Every phrase worth
    pinning in these two files is longer than the distance to the next line
    break, so a literal `in` check against the raw text passes or fails on
    where the wrap happens to fall — which is not a property of the doc."""
    return " ".join(text.split())


def test_the_ending_fork_names_its_option_set_rather_than_leaving_it_to_the_agent():
    """The second dogfood run's second finding, and the same failure mode as
    `fc7bf82` one layer up.

    The receipt printed the `upload` offer to a kit that had one; the agent
    relayed a three-option chooser of read-it / email-it / send-nothing, and
    upload survived only as prose. So the one reader the offer was ungated FOR
    could reach it only by rejecting the menu — a gate removed from the
    filesystem and put back in the presentation.

    Two things are pinned: the general rule, which is where the next surface
    will look for it, and this fork's set, because "two options is the usual
    shape" is what let three options look correct.
    """
    doc = _flat(_claude_md())
    assert "every option a section names even if you expect it to be false" in doc, (
        "the presentation-layer rule is gone; a chooser can silently drop an option again"
    )
    assert "Three options, all shown every time" in doc, (
        "the ending fork went back to leaving its option set to the agent"
    )
    for option in (
        "*Send it to my Baton workspace*",
        "*Email it myself*",
        "*Not sending anything*",
    ):
        assert option in doc, f"the ending fork stopped naming one of its three options: {option}"


def test_upload_leads_only_when_the_person_was_handed_the_file():
    """Ordering is conditional (Ujwal, 2026-09-02): provisioned → upload first,
    otherwise email first, both always shown.

    The signal is that someone TOLD the agent where the file is, not a
    filesystem check — the check is what `fc7bf82` removed, and it failed
    because the file lands in a downloads folder and never beside `kit.py`.
    Three things have to survive together or the rule turns into either the old
    gate or a recommendation: the condition, the ban on hunting for the file,
    and the fact that position carries no endorsement.
    """
    doc = _flat(_claude_md())
    assert "Leads if they were handed an `upload.json`" in doc, (
        "the ordering rule lost the signal it keys on"
    )
    assert "Never open the file or hunt for it" in doc, (
        "nothing now stops the agent hunting the filesystem for a credential"
    )
    assert "never call upload easier or recommended" in doc, (
        "ordering upload first now reads as us recommending it"
    )
    # The bullet this replaced said "do not present it as the recommended one",
    # full stop, which directly contradicts an ordering rule that sometimes puts
    # it first. A contradiction left in place is what §13 cost us on 09-01.
    assert "Do not present it as the recommended one" not in doc, (
        "the absolute wording is back and now contradicts the conditional ordering"
    )


def test_the_remote_consent_is_reachable_under_the_order_the_paste_sets():
    """The half of the same finding that is not cosmetic.

    The remote disclosure once hung off a pre-consent summary the paste ran
    BEFORE listing the servers, so the consent was specified for a moment at
    which no row existed and it could never be reached. The summary is gone
    (2026-09-04) and the disclosure now hangs off the pick itself, which is the
    fix rather than a consequence of the rewrite: this pins the window it has to
    land in — after the row exists, before the config is written — and the three
    facts it carries, not the order of the forks around it.
    """
    doc = _flat(_claude_md())
    # One sentence now carries both halves of the window: "if they picked" is
    # after the row exists, "before `setup` runs" is before the edit.
    assert "If they picked a remote server, say three things before `setup` runs" in doc, (
        "the remote consent lost the placement that makes it reachable"
    )
    # The three facts, absorbed 2026-09-04 from the bound test that used to
    # hold them. The bound is gone; the facts are the part that was never
    # about formatting.
    for fact in ("bearer token", "${VAR}", "`receipt` on the first day"):
        assert fact in doc, f"the remote section lost a fact while being reshaped: {fact!r}"


def test_the_prompt_leaves_the_ending_to_the_kit():
    """`TEAM_EMAIL` is pinned across kit.py, CLAUDE.md and SECURITY.md §4, and
    the send offer is gated on there being something to send. The prompt runs
    before any capture exists, so naming the address here would make the offer
    at the one moment it cannot be true — and would add a fourth site to a
    three-site pin by accident rather than by decision."""
    text = _prompt_text()
    assert kit.TEAM_EMAIL not in text, (
        "the prompt offers the ending before there is anything to send"
    )
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "the prompt names an address"


# =============================================================================
# `upload` (2026-09-01) — the kit's first and only egress.
#
# Everything in this section guards one shape: the kit can now send the person's
# data, and the only thing that makes that acceptable is that the sending is
# bounded, visible, and theirs. So these tests are not about whether the POST
# works — that is `replay_events.py`'s job and it has been doing it against prod
# for weeks. They are about the bounds: it refuses without the handed-over file,
# it is invisible to a kit that has no such file, it never prints the key, it
# rewrites one field and not the other, and the agent is told not to type it.
# =============================================================================


def _load_uploader_module():
    """The uploader, loaded the same way `kit.py` loads it — by path."""
    spec = importlib.util.spec_from_file_location(
        "try_kit_upload_under_test", REPO_ROOT / "try" / "upload.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


upload_mod = _load_uploader_module()


CREDS = {
    "console_url": "https://console.example.test",
    "api_key": "bk_live_not_a_real_key",
    "tenant_id": "ten_abc123",
}


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def _recording_opener(codes=None):
    """An opener that records the bodies it was given and replays `codes`.

    Each entry is None (a 201), an int status raised as the HTTPError urllib
    would raise, or an exception instance raised as-is — the last of those is
    how the transport faults are driven, since a blocked network arrives as a
    `URLError` and never as a status. Returns (opener, sent) where `sent` is the
    list of decoded envelopes — which is how the tenant-rewrite property is
    checked without a network.
    """
    sent: list[dict] = []
    codes = list(codes or [])

    def opener(req):
        sent.append(json.loads(req.data.decode()))
        code = codes.pop(0) if codes else None
        if isinstance(code, BaseException):
            raise code
        if code is not None:
            raise urllib.error.HTTPError(req.full_url, code, "no", {"Retry-After": "0"}, None)
        return _FakeResponse()

    return opener, sent


def _a_person_typing(monkeypatch, answer):
    """Put a terminal in front of `upload`, with `answer` waiting in it.

    Returns the list of prompts `input()` was shown. The prompt is worth
    capturing rather than reading off stdout: a patched `input` never writes it,
    so a capsys assertion would pass whatever the question turned out to say —
    and the question is the whole interface of this gate.

    `answer` may be an exception instance, which is how Ctrl-D and Ctrl-C are
    driven: both arrive at `input()` as a raise and neither is an answer the
    keyboard can spell.
    """
    prompts: list[str] = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(kit, "_stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


@pytest.fixture
def at_a_terminal(monkeypatch):
    """The ordinary run, for the tests that are about something else.

    Every test that drives `cmd_upload` all the way to a POST needs a person in
    front of it now, and none of the ones written before the gate existed were
    about the person. They ask for this and go back to what they were pinning.
    """
    _a_person_typing(monkeypatch, "send")


def _ready_to_send(kit_home, monkeypatch, *, events=1, creds=None):
    """A kit with everything `upload` wants except someone typing at it.

    A credential, a capture of `events` lines, and an opener that records
    envelopes instead of opening a socket — so `sent == []` is a real assertion
    that nothing left, not an assertion that the network happened to be down.
    """
    (kit_home / "events.jsonl").write_text(
        "".join(f'{{"event_id":"e{i}","session_id":"s1"}}\n' for i in range(events)),
        encoding="utf-8",
    )
    (kit_home / "upload.json").write_text(json.dumps(creds or CREDS), encoding="utf-8")
    opener, sent = _recording_opener()
    monkeypatch.setattr(upload_mod, "open_request", opener)
    monkeypatch.setattr(kit, "load_uploader", lambda: upload_mod)
    return sent


def test_upload_refuses_without_the_handed_over_file(kit_home, capsys):
    """The ordinary case, and the one most people meet: every kit cloned from
    the repository is a kit with no `upload.json`.

    Two things the refusal has to do, because someone reading it has just been
    told no by a security tool. Name the alternative that actually works —
    email needs nothing from us in advance — and make clear this is a missing
    arrangement rather than a broken kit."""
    (kit_home / "events.jsonl").write_text('{"event_id":"e1"}\n', encoding="utf-8")
    with pytest.raises(kit.Refuse) as e:
        kit.cmd_upload(argparse.Namespace())
    msg = str(e.value)
    assert "upload.json" in msg, "the refusal does not name the file it wants"
    assert "email" in msg.lower(), "the refusal leaves them with no working path"
    assert "receipt" in msg, "the refusal does not say what to run instead"


def test_upload_refuses_before_it_mentions_the_capture(kit_home):
    """Credentials are checked before the event file, and the order is the point.

    A person with no `upload.json` will never be able to run this. Leading with
    "there is nothing to send yet" tells them to come back once they have data
    and earn the same refusal then — the permanent condition goes first."""
    # No events file AND no credentials: the credential refusal is the one that
    # should surface, because it is the one that will not change.
    with pytest.raises(kit.Refuse) as e:
        kit.cmd_upload(argparse.Namespace())
    assert "upload.json" in str(e.value)
    assert "nothing to send yet" not in str(e.value)


def test_a_malformed_credential_file_is_the_same_answer_as_a_missing_one(kit_home):
    """The person did not write this file and cannot fix its schema. Three
    failures — absent, unparseable, incomplete — have one useful answer, and it
    is the one that points at email."""
    (kit_home / "events.jsonl").write_text('{"event_id":"e1"}\n', encoding="utf-8")
    for bad in ("{not json", "[]", '{"console_url": "https://x.test"}'):
        (kit_home / "upload.json").write_text(bad, encoding="utf-8")
        with pytest.raises(kit.Refuse) as e:
            kit.cmd_upload(argparse.Namespace())
        assert "email" in str(e.value).lower(), f"no alternative offered for {bad!r}"


def test_upload_rewrites_the_tenant_and_never_the_vendor(tmp_path):
    """`tenant_id` says which workspace the events land in; `vendor_id` says
    which of their servers each event came from, and the dashboard's server
    picker keys off it. Rewriting both would file a capture from `notion` as
    having come from the workspace itself, which is the kind of wrong that looks
    right on the screen."""
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"event_id":"e1","session_id":"s1","tenant_id":"notion","vendor_id":"notion"}\n'
        '{"event_id":"e2","session_id":"s1","tenant_id":"notion","vendor_id":"notion"}\n',
        encoding="utf-8",
    )
    opener, sent = _recording_opener()
    result = upload_mod.send(events, CREDS, rate=0, emit=lambda *_: None, opener=opener)
    assert [e["tenant_id"] for e in sent] == ["ten_abc123", "ten_abc123"]
    assert [e["vendor_id"] for e in sent] == ["notion", "notion"], "vendor_id was rewritten"
    assert result["delivered"] == 2
    assert result["sessions"] == 1


def test_a_refused_key_stops_the_run_instead_of_repeating_itself(tmp_path):
    """A wrong key 401s identically on every line. Grinding a four-thousand-line
    file through it produces four thousand copies of one fact, and the person
    watching cannot tell that from four thousand different problems.

    403 is in the same branch on purpose: it is also how an over-quota or
    suspended tenant is refused, and both readings mean every remaining line
    fails the same way."""
    events = tmp_path / "events.jsonl"
    events.write_text("".join(f'{{"event_id":"e{i}"}}\n' for i in range(50)), encoding="utf-8")
    for code in (401, 403):
        opener, sent = _recording_opener([code])
        with pytest.raises(upload_mod.Terminal):
            upload_mod.send(events, CREDS, rate=0, emit=lambda *_: None, opener=opener)
        assert len(sent) == 1, f"HTTP {code} kept going after the first refusal"


def test_an_event_too_large_is_skipped_and_the_rest_still_go(tmp_path):
    """413 is per-event and is never fixed by re-sending: the body is over the
    limit and will be over it again. So it is counted and stepped past, and the
    report names the lines rather than telling someone to try again."""
    events = tmp_path / "events.jsonl"
    events.write_text('{"event_id":"e1"}\n{"event_id":"e2"}\n{"event_id":"e3"}\n', encoding="utf-8")
    opener, sent = _recording_opener([None, 413, None])
    result = upload_mod.send(events, CREDS, rate=0, emit=lambda *_: None, opener=opener)
    assert len(sent) == 3, "one oversized event stopped the file"
    assert result["delivered"] == 2
    assert result["oversized_lines"] == [2]


def test_a_throttled_event_waits_and_then_gives_up_saying_nothing_is_wrong(tmp_path):
    """429 is the server asking for a pause, so it is retried on the same event
    rather than counted as a failure. Past the ceiling it stops — and the
    message says the file is fine and a later run will not double-send, because
    the person's next move is to run it again."""
    events = tmp_path / "events.jsonl"
    events.write_text('{"event_id":"e1"}\n', encoding="utf-8")
    slept: list[float] = []
    opener, sent = _recording_opener([429] * (upload_mod.MAX_THROTTLE_RETRIES + 1))
    with pytest.raises(upload_mod.Terminal) as e:
        upload_mod.send(
            events, CREDS, rate=0, emit=lambda *_: None, sleep=slept.append, opener=opener
        )
    assert len(sent) == upload_mod.MAX_THROTTLE_RETRIES + 1
    assert len(slept) == upload_mod.MAX_THROTTLE_RETRIES, "a 429 was not waited on"
    assert "will not land twice" in str(e.value)


def test_every_exit_that_stops_the_run_still_leaves_them_a_way_to_send(tmp_path):
    """Ujwal's call, 2026-09-02: a failed upload falls back to email.

    Each of these exits used to end the conversation — the person is left with a
    capture, a command that will not work, and no next line. The email path
    needs nothing from us and works for anyone, so it belongs on every stop.

    Pinned as a set rather than one message at a time: the failure mode is a
    fourth branch added later that quietly ends without it.

    Driven through `kit.load_uploader()` rather than the module-level import,
    because the address is INJECTED there — reaching for the module directly
    would test a fallback that names no address and pass while the real command
    printed something else.
    """
    mod = kit.load_uploader()
    events = tmp_path / "events.jsonl"
    events.write_text("".join(f'{{"event_id":"e{i}"}}\n' for i in range(3)), encoding="utf-8")

    stops = {}
    for label, codes in (("401", [401]), ("403", [403])):
        opener, _ = _recording_opener(codes)
        with pytest.raises(mod.Terminal) as e:
            mod.send(events, CREDS, rate=0, emit=lambda *_: None, opener=opener)
        stops[label] = str(e.value)

    opener, _ = _recording_opener([429] * (mod.MAX_THROTTLE_RETRIES + 1))
    with pytest.raises(mod.Terminal) as e:
        mod.send(events, CREDS, rate=0, emit=lambda *_: None, sleep=lambda *_: None, opener=opener)
    stops["throttled"] = str(e.value)

    opener, _ = _recording_opener([urllib.error.URLError("blocked")] * 8)
    with pytest.raises(mod.Terminal) as e:
        mod.send(events, CREDS, rate=0, emit=lambda *_: None, sleep=lambda *_: None, opener=opener)
    stops["unreachable"] = str(e.value)

    for label, raw in stops.items():
        # Flattened: these messages are hard-wrapped for a terminal, so a literal
        # `in` check against the raw text passes or fails on where the wrap
        # happens to fall, which is not a property of the message.
        message = _flat(raw)
        assert kit.TEAM_EMAIL in message, (
            f"the {label} exit offers email without naming the address — someone who "
            "has just been stopped should not need another command to find it"
        )
        # `receipt` is still named, and not as a way to look the address up: it
        # prints the `gzip` line, and the raw capture is the thing you do not
        # want mailed.
        assert "kit.py receipt" in message, f"the {label} exit lost the compression step"
        # The sender constraint travels with the offer or the offer is a trap:
        # the console de-duplicates on `event_id` globally, and we resolve the
        # workspace from the sending address, so a forward strands whatever
        # already uploaded in a different workspace.
        assert "from the address we set your" in message, (
            f"the {label} exit offers email without saying which mailbox it has to come from"
        )


def test_the_uploader_is_handed_the_one_address_rather_than_keeping_its_own():
    """`TEAM_EMAIL` is pinned across kit.py, CLAUDE.md and SECURITY.md §4, and
    the fallback messages need it. `upload.py` cannot import it — a module-level
    import back into `kit.py` is the import-graph edge `load_uploader` exists to
    refuse — and a second literal would be a fourth site to keep in step.

    So it is injected at load. This pins both halves: the uploader holds no
    address of its own, and loading it through the kit supplies one.
    """
    source = (Path(kit.__file__).resolve().parent / "upload.py").read_text(encoding="utf-8")
    assert kit.TEAM_EMAIL not in source, (
        "upload.py now carries its own copy of the address, which is the fourth site "
        "the injection exists to avoid"
    )
    fresh = _load_uploader_module()
    assert fresh.TEAM_EMAIL is None, "upload.py defaults to an address it was not given"
    assert kit.TEAM_EMAIL not in fresh.email_fallback(), (
        "an uninjected uploader names an address from somewhere"
    )
    assert "kit.py receipt" in fresh.email_fallback(), (
        "without an address the fallback must still point somewhere that has one"
    )
    assert kit.load_uploader().TEAM_EMAIL == kit.TEAM_EMAIL, (
        "kit.py stopped handing the uploader the address it prints everywhere else"
    )


def test_a_blocked_network_is_not_reported_as_a_credential_we_got_wrong(tmp_path):
    """The message this replaces told them the address in `upload.json` was
    wrong and we should send a new one. In the environments this kit is written
    for, a blocked outbound connection is the expected outcome — and the file is
    verified against the real console before it is handed over, so a replacement
    would change nothing. Sending someone back to us for one, seconds after
    their own network refused them, is the worst version of this moment.
    """
    events = tmp_path / "events.jsonl"
    events.write_text('{"event_id":"e1"}\n', encoding="utf-8")
    opener, _ = _recording_opener([urllib.error.URLError("blocked")] * 8)
    with pytest.raises(upload_mod.Terminal) as e:
        upload_mod.send(
            events, CREDS, rate=0, emit=lambda *_: None, sleep=lambda *_: None, opener=opener
        )
    message = str(e.value)
    assert "your network does not allow the connection" in message, (
        "the blocked-network case stopped naming the cause we actually expect"
    )
    assert "nothing to replace" in message, (
        "the message no longer rules out the credential, so they will ask us for a new one"
    )
    for retired in ("is wrong and we should send you a new one", "Check that you are online"):
        assert retired not in message, f"the misdiagnosis is back: {retired!r}"


def test_upload_names_the_key_and_never_prints_it(kit_home, at_a_terminal, monkeypatch, capsys):
    """The same rule the entry printer follows, on the one file whose whole
    content is a credential. A person may paste this output into a thread with
    us, or into one with their own security team."""
    (kit_home / "events.jsonl").write_text(
        '{"event_id":"e1","session_id":"s1"}\n', encoding="utf-8"
    )
    (kit_home / "upload.json").write_text(json.dumps(CREDS), encoding="utf-8")
    opener, _sent = _recording_opener()
    monkeypatch.setattr(upload_mod, "open_request", opener)
    monkeypatch.setattr(kit, "load_uploader", lambda: upload_mod)
    kit.cmd_upload(argparse.Namespace())
    out = capsys.readouterr().out
    assert CREDS["api_key"] not in out, "the upload printed the key it was handed"
    assert "not shown" in out, "the key was dropped silently rather than named"
    assert CREDS["tenant_id"] in out, "the person cannot see which workspace this went to"
    assert "Delivered is not the same as stored" in out, (
        "the receipt-side honesty about 201 went missing"
    )


def test_the_receipt_offers_upload_to_everyone_with_its_condition_attached(kit_home, capsys):
    """This was gated on the credential file existing, for one day.

    Gating made the option invisible to the one person it was FOR: the file
    arrives by mail and lands in a downloads folder, so the reader who could
    use it was the reader who never saw it offered. Ungated, the load-bearing
    thing is the CONDITION — "if we set up a workspace for you and sent you an
    `upload.json`" — which is false for almost everyone and obviously false to
    them. A bare command with no condition on it reads as a step they missed,
    so the clause is pinned the way the address is."""
    (kit_home / "events.jsonl").write_text(
        json.dumps(_ev(payload={"tool_name": "s"})) + "\n", encoding="utf-8"
    )
    kit.cmd_receipt(argparse.Namespace())
    out = capsys.readouterr().out
    assert "If we set up a workspace for you and sent you an `upload.json`" in out, (
        "the upload offer lost the condition that tells most readers it is not for them"
    )
    assert "python3 kit.py upload --credentials" in out, "the offer names no command"
    assert kit.TEAM_EMAIL in out, "the email path stopped being offered alongside"
    for scheme in ("http://", "https://"):
        assert scheme not in out, "the receipt printed an endpoint"


def test_a_credential_from_anywhere_is_validated_the_same_way(kit_home, tmp_path):
    """A path that is wrong, or points at something that is not a credential,
    gets the same refusal as a missing file — including the line naming the
    email path. Someone who mistyped and someone who was never sent a file both
    need to be told what still works."""
    (kit_home / "events.jsonl").write_text('{"event_id":"e1"}\n', encoding="utf-8")
    bad = tmp_path / "not-a-credential.json"
    bad.write_text('{"console_url": "https://x.test"}', encoding="utf-8")
    with pytest.raises(kit.Refuse) as e:
        kit.cmd_upload(argparse.Namespace(credentials=str(bad)))
    assert "email" in str(e.value).lower(), "a bad path left them with no working path"


def test_upload_reads_the_credential_and_never_makes_a_second_copy(
    kit_home, at_a_terminal, tmp_path, monkeypatch, capsys
):
    """A first version installed the file beside `kit.py` so a second run could
    skip the flag. That optimised the wrong case.

    This is a prospect proving the thing works, and most of them send once — so
    the copy bought a shorter second command that usually never happens, and
    paid for it by leaving a live API key inside a checkout permanently, in a
    place the person did not choose. Their download stays their only copy, which
    also makes the cleanup one sentence: delete the file you were sent.

    Pinned as an absence, because that is how it would regress: someone adds a
    convenience copy back and nothing else fails."""
    (kit_home / "events.jsonl").write_text(
        '{"event_id":"e1","session_id":"s1"}\n', encoding="utf-8"
    )
    downloaded = tmp_path / "upload.json"
    downloaded.write_text(json.dumps(CREDS), encoding="utf-8")

    opener, sent = _recording_opener()
    monkeypatch.setattr(upload_mod, "open_request", opener)
    monkeypatch.setattr(kit, "load_uploader", lambda: upload_mod)
    kit.cmd_upload(argparse.Namespace(credentials=str(downloaded)))

    assert len(sent) == 1, "the capture did not go"
    assert not kit.upload_credentials_path().exists(), (
        "upload copied the credential into the checkout; the download is meant to "
        "stay the only copy"
    )
    strays = [f.name for f in kit_home.rglob("*") if f.is_file() and "upload" in f.name]
    assert strays == [], f"a credential-shaped file appeared in try/: {strays}"
    assert CREDS["api_key"] not in capsys.readouterr().out, "the key was printed"


# ---------------------------------------------------------------------------
# The terminal gate (2026-09-04). SECURITY.md §3a and §4 row 6 both tell a
# reader that nothing runs `upload` on their behalf, and until now that was a
# sentence in `CLAUDE.md` asking the agent not to. Everything else the agent
# does in this trial is reversible or visible; this one is neither, so it is
# checked in the process against the one thing an agent cannot claim to be.
#
# Three properties, and the third is the one that makes the first two mean
# anything: it refuses without a terminal, it does nothing on any answer but
# `send`, and it still sends when someone types it.
# ---------------------------------------------------------------------------


def test_upload_refuses_when_nobody_is_typing_at_it(kit_home, monkeypatch, capsys):
    """A kit that has the credential AND the capture — every condition met but
    the person — still sends nothing.

    The seam is patched to False rather than leant on: under plain `pytest`
    stdin is not a terminal, but under `pytest -s` in a developer's shell it is,
    and a test that passes for that reason is testing the harness."""
    sent = _ready_to_send(kit_home, monkeypatch)
    monkeypatch.setattr(kit, "_stdin_is_a_terminal", lambda: False)

    with pytest.raises(kit.Refuse) as e:
        kit.cmd_upload(argparse.Namespace())

    msg = str(e.value)
    assert "terminal" in msg, "the refusal never names the thing it wants"
    assert "python3 kit.py upload" in msg, "the refusal names no command to run there"
    assert sent == [], "the capture went out on a run with nobody present"
    assert capsys.readouterr().out == "", "a refused upload still printed a report"


def test_the_refusal_hands_back_the_credential_path_they_passed(kit_home, monkeypatch, tmp_path):
    """The half of the command they cannot reconstruct.

    The file is in a downloads folder under a name we chose, and this refusal is
    usually read as text an agent relayed rather than in the terminal that
    produced it — so "run it yourself" has to be a line they can paste. Quoted,
    because a downloads path with a space in it is the ordinary case on the
    machines this kit is written for."""
    downloaded = tmp_path / "my downloads" / "upload.json"
    downloaded.parent.mkdir()
    downloaded.write_text(json.dumps(CREDS), encoding="utf-8")
    _ready_to_send(kit_home, monkeypatch)
    monkeypatch.setattr(kit, "_stdin_is_a_terminal", lambda: False)

    with pytest.raises(kit.Refuse) as e:
        kit.cmd_upload(argparse.Namespace(credentials=str(downloaded)))

    msg = str(e.value)
    assert f"--credentials {shlex.quote(str(downloaded))}" in msg, (
        "the command they are told to run drops the flag that makes it work"
    )


def test_the_capture_check_still_comes_before_the_terminal_check(kit_home, monkeypatch):
    """Order, the second half. `test_upload_refuses_before_it_mentions_the_capture`
    pins credentials ahead of capture; this pins capture ahead of the person.

    Both of those are conditions that will not change by asking, and neither can
    send anything — so making someone open a terminal, type `send`, and only
    then be told there is nothing to send is a round trip for a refusal they
    were always going to get."""
    (kit_home / "upload.json").write_text(json.dumps(CREDS), encoding="utf-8")
    monkeypatch.setattr(kit, "_stdin_is_a_terminal", lambda: False)

    with pytest.raises(kit.Refuse) as e:
        kit.cmd_upload(argparse.Namespace())

    assert "nothing to send yet" in str(e.value)
    assert "terminal" not in str(e.value), "the terminal check jumped the capture check"


@pytest.mark.parametrize(
    "answer",
    ["", "no", "n", "y", "yes", "Send", "SEND", "sent", "send it", EOFError(), KeyboardInterrupt()],
    ids=lambda a: type(a).__name__ if isinstance(a, BaseException) else (a or "empty"),
)
def test_anything_but_send_sends_nothing_and_is_not_an_error(answer, kit_home, monkeypatch, capsys):
    """Case-sensitive and exact, because the question is not "are you sure".

    And a decline is not a refusal: they were asked and they answered, which is
    the gate working. Exit 1 on stderr would have `CLAUDE.md`'s "a refusal is an
    answer" rule make the agent relay a working kit as a broken one."""
    sent = _ready_to_send(kit_home, monkeypatch)
    _a_person_typing(monkeypatch, answer)

    assert kit.cmd_upload(argparse.Namespace()) == 0, "a decline was reported as a failure"

    out, err = capsys.readouterr()
    assert sent == [], f"{answer!r} was read as consent"
    assert "Nothing was sent." in out, "the decline was silent about what it did"
    assert err == "", "a decline was written to stderr"


@pytest.mark.parametrize("answer", ["send", "  send  "])
def test_typing_send_is_what_releases_the_capture(answer, kit_home, monkeypatch, capsys):
    """The other side of the gate, and the reason the seam exists at all: a
    suite that could only ever be the not-a-terminal case would pin the refusal
    and never the thing the refusal is standing in front of."""
    sent = _ready_to_send(kit_home, monkeypatch, events=3)
    prompts = _a_person_typing(monkeypatch, answer)

    assert kit.cmd_upload(argparse.Namespace()) == 0
    assert len(sent) == 3, "typing `send` did not send"
    assert prompts == ["Type send to continue: "], f"the question was asked as {prompts}"
    assert "Nothing was sent." not in capsys.readouterr().out


def test_the_question_names_what_the_answer_is_about_and_still_never_the_key(
    kit_home, monkeypatch, capsys
):
    """Consent to what, exactly: a count, a file, a destination and a workspace,
    before the prompt rather than after it.

    The destination is `safe_endpoint`'d like every other address this kit
    prints. A console URL can carry its own credential in the path, and someone
    deciding whether to send may paste this line into a thread with their
    security team — which is the same reason the key is named and not shown."""
    creds = dict(CREDS, console_url="https://console.example.test/s/tok_in_the_path")
    _ready_to_send(kit_home, monkeypatch, events=2, creds=creds)
    _a_person_typing(monkeypatch, "no")

    kit.cmd_upload(argparse.Namespace())

    line = capsys.readouterr().out.splitlines()[0]
    assert "2 events" in line, "the count of what is about to go is missing"
    assert str(kit_home / "events.jsonl") in line, "the file it would send is not named"
    assert "https://console.example.test" in line, "the destination is not named"
    assert creds["tenant_id"] in line, "the workspace it would land in is not named"
    assert "tok_in_the_path" not in line, "the summary printed the console URL's path"
    assert creds["api_key"] not in line, "the summary printed the key"


def test_the_terminal_check_lives_in_the_kit_and_not_in_the_uploader():
    """§3a hands a reviewer "all the network code is in `upload.py`" as a claim
    they check by reading one short file. Consent is not network code, and a
    gate added there would have given that file a second job — and the reviewer
    a longer read — for nothing. Pinned as an absence on one side and a presence
    on the other, which is the only way this regresses."""
    uploader = (REPO_ROOT / "try" / "upload.py").read_text(encoding="utf-8")
    kit_source = KIT_PATH.read_text(encoding="utf-8")
    for borrowed in ("isatty", "input("):
        assert borrowed not in uploader, f"the consent gate leaked into upload.py: {borrowed}"
    assert kit_source.count("isatty()") == 1, (
        "isatty is called somewhere other than the one seam the tests patch"
    )


def test_the_uploader_has_no_write_path_at_all():
    """The narrower form of the rule above, read off the module. `upload.py`
    reads a capture and sends it; the moment it can write, "your download stays
    your only copy" becomes something a reviewer takes on trust rather than
    checks."""
    source = (REPO_ROOT / "try" / "upload.py").read_text(encoding="utf-8")
    # The one `open(` is the capture, opened for reading.
    assert 'open(events_path, encoding="utf-8")' in source
    for writer in ("write_text(", "write_bytes(", "shutil", "os.open", '"w"', "'w'"):
        assert writer not in source, f"upload.py gained a write path: {writer}"


def test_the_uploader_is_not_on_the_import_graph_of_the_other_commands():
    """SECURITY.md §3a claims the only command that loads the sending code is
    the one that was typed, and §4 row 6 repeats it. A module-level
    `import upload` would falsify both while every test still passed."""
    source = KIT_PATH.read_text(encoding="utf-8")
    module_level = [
        line
        for line in source.splitlines()
        if re.match(r"^(import|from)\s", line) and "upload" in line
    ]
    assert not module_level, f"kit.py imports the uploader at module level: {module_level}"
    assert "def load_uploader" in source, "the by-path loader is gone"


def test_claude_md_forbids_the_agent_from_running_upload():
    """The rule that had to move when the button appeared. "Never send the file
    anywhere" was written when no command in the kit could send it — the agent
    had nothing to send it WITH. It now has a shell and a one-line command, so
    the prohibition has to name it or it is a rule about a thing that no longer
    exists."""
    doc = _flat(_claude_md())
    assert "kit.py upload" in doc, "the doc does not mention the command it must forbid"
    assert "the person runs this, never you" in doc, "the command list does not mark it off-limits"
    assert "if they ask you to run it, say this one is theirs to type" in doc, (
        "the rule does not survive the person asking, which is when it is needed"
    )


def test_security_md_keeps_the_config_entry_check_that_upload_could_have_broken():
    """The one sentence a reviewer is handed as sufficient proof: `file://` plus
    no `BATON_API_KEY` in the entry means the wrap cannot deliver anywhere.

    `upload` is exactly the change that could have falsified it — putting the key
    in the config entry was the easy shape and needed no new code. It reads its
    key from `upload.json` instead, so this pins both the surviving sentence and
    the reason it survived."""
    security = (REPO_ROOT / "try" / "SECURITY.md").read_text(encoding="utf-8")
    assert "absence of `BATON_API_KEY` in the config entry as sufficient" in _flat(security), (
        "§4's closing check is gone; upload must not have taken it with it"
    )
    assert "never in your config entry" in _flat(security), (
        "§4 stopped saying where upload's key does NOT live"
    )


def test_uninstall_names_the_credential_it_leaves_behind(kit_home, monkeypatch, capsys):
    """`uninstall` leaves the events file and the backups on purpose. If a
    credential is also sitting there it names that too, and it is different in
    kind from the other two: a live API key.

    The kit never puts it there — `upload` reads it wherever the person saved it
    — so this fires only when they chose to keep it beside `kit.py`. Named when
    it is, because the moment the trial is declared over is the last moment
    anyone thinks to look in that folder."""
    config = _config(kit_home, {"mcpServers": {"acme": {"command": "acme-server"}}})
    kit.cmd_setup(
        argparse.Namespace(server="acme", config_file=str(config), tenant=None, vendor=None)
    )
    capsys.readouterr()
    (kit_home / "upload.json").write_text(json.dumps(CREDS), encoding="utf-8")
    kit.cmd_uninstall(argparse.Namespace())
    out = capsys.readouterr().out
    assert "upload.json" in out, "uninstall said nothing about the key it left in place"
    assert CREDS["api_key"] not in out, "uninstall printed the key while naming it"


def test_security_md_section_7_accounts_for_the_credential_file():
    """§7 is the removal inventory and its promise is completeness — "that is
    the entire footprint". The credential is the one file in this trial the kit
    does not create, so what §7 owes the reader is the opposite of an entry in
    the cleanup list: that there is nothing here to clean up, because the file
    never moved from wherever they saved it."""
    doc = (KIT_PATH.parent / "SECURITY.md").read_text(encoding="utf-8")
    section = doc[doc.index("## 7. Where the data lives") : doc.index("## 8. Provenance")]
    assert "upload.json" in section, "§7 never accounts for the credential file"
    assert "never moves or copies it" in section, (
        "§7 stopped saying the kit leaves the credential where the person put it"
    )

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
from pathlib import Path

import pytest

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
    events = [
        _ev(session_id="s1", payload={"tool_name": "search", "call_intent": "find the doc"}),
        _ev(session_id="s1", payload={"tool_name": "search"}),
        _ev(session_id="s2", payload={"tool_name": "search"}),
    ]
    s = kit.summarize(events, 1234)
    assert s["sessions"] == 2
    assert s["tool_calls"] == 3
    assert s["sessions_with_intent"] == 1


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


def test_the_receipt_names_no_destination(tmp_path, monkeypatch, capsys):
    """The kit sells one sentence — nothing leaves your machine — and an upload
    endpoint would cost it to save one step at the very end. The file travels by
    whatever channel the person's own company already permits, so the receipt
    must not name, offer, or imply a place to send it."""
    _events, out = _run_receipt(tmp_path, monkeypatch, capsys, [_ev(payload={"tool_name": "s"})])
    for scheme in ("http://", "https://", "@"):
        assert scheme not in out, f"the receipt offered a destination ({scheme})"
    assert "goodtiming" not in out and "baton.ai" not in out


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
    ("src/baton_proxy/proxy.py", 1252, "subprocess.Popen("),
    ("src/baton_proxy/transport_http.py", 135, "urllib.request.urlopen(req"),
    ("src/baton_proxy/transport_http.py", 187, "urlopen(timeout=inf) blocks forever"),
    ("src/baton_proxy/sinks.py", 133, "urllib.request.urlopen(req"),
    ("src/baton_proxy/sinks.py", 165, 'boto3.client("s3")'),
    ("src/baton_proxy/scan.py", 510, "subprocess.run(cmd"),
}


def _audited_files():
    """Every file `grep -r src/ try/` would read, in a stable order."""
    for base in ("src", "try"):
        for path in sorted((REPO_ROOT / base).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                yield path


def _grep(pattern: str):
    """`grep -rnE <pattern> src/ try/` as (relative_path, lineno, line)."""
    import re

    rx = re.compile(pattern)
    hits = []
    for path in _audited_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # grep skips binaries too
        for n, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append((str(path.relative_to(REPO_ROOT)), n, line))
    return hits


def test_security_md_section_9_narrow_grep_returns_exactly_its_six():
    """§9: "Six matches: the five in the §4 table, plus one comment line in
    transport_http.py."

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


def test_one_of_the_six_is_a_comment_not_a_call_site():
    """§9 distinguishes "the five in the §4 table" from "one comment line". A
    reviewer counting call sites and getting six would conclude the table is
    incomplete — so the comment is part of the claim, not noise around it."""
    hits = _grep(NARROW_AUDIT_RE)
    comments = [(p, n) for p, n, line in hits if line.strip().startswith("#")]
    assert comments == [("src/baton_proxy/transport_http.py", 187)]


def test_the_kit_contributes_no_audited_call_site():
    """§9: "The kit contributes none — it only reads and writes local files."
    The narrow grep covers `try/` precisely so a reviewer can see that zero."""
    assert [h for h in _grep(NARROW_AUDIT_RE) if h[0].startswith("try/")] == []


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
    "<label>": "trial-doc",
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
    assert {tuple(argv[:1]) for _, argv in found} == {("setup",), ("receipt",), ("uninstall",)}


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

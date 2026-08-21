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
        data["mcpServers"][name]
        if scope is None
        else data["projects"][scope]["mcpServers"][name]
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
    assert json.loads(restored) == json.loads(before)   # content preserved
    assert restored != before                            # whitespace normalized


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
    sink = make_sink(uri, api_key=None)          # would raise FileNotFoundError before
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
    restored_text, entry = kit.apply_unwrap(before, state)   # config already back to original
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
    assert seen and seen[0] <= 0o600, (
        f"temp file held the config at {oct(seen[0])} before chmod"
    )


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
    _, state = kit.apply_wrap(canonical(GLOBAL_ONLY), scope=None, name="notion",
                              interpreter="/opt/py312/bin/python3.12", **WRAP_ARGS)
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


# =============================================================================
# Receipt.
# =============================================================================


def _ev(**kw):
    base = {"event_id": "e", "session_id": "s1", "event_type": "tool_call_start",
            "captured_at": "2026-08-19T10:00:00Z", "payload": {}}
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
    events = [_ev(payload={"result": "mail [REDACTED:email] and [REDACTED:email], key [REDACTED:sk_key]"})]
    s = kit.summarize(events, 10)
    assert s["redactions"] == {"email": 2, "sk_key": 1}


def test_receipt_reports_no_error_counts():
    """By design: the receipt proves capture, it does not preview analysis.
    Error counts are something the user can already get for themselves."""
    events = [_ev(event_type="tool_call_error", payload={"error_type": "boom"})]
    s = kit.summarize(events, 10)
    assert "errors" not in s


def test_receipt_takes_the_tool_surface_from_the_snapshot():
    events = [_ev(event_type="surface_snapshot",
                  payload={"tools": [{"name": "search"}, {"name": "create"}]})]
    assert kit.summarize(events, 10)["tools"] == ["search", "create"]


def test_read_events_skips_a_truncated_final_line(tmp_path):
    """The proxy killed mid-write must not make the receipt unavailable."""
    p = tmp_path / "events.jsonl"
    p.write_text('{"event_type":"tool_call_start","session_id":"s1","payload":{}}\n{"trunc', encoding="utf-8")
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
    out = kit.entry_json(kit.build_wrapped_entry(
        GLOBAL_ONLY["mcpServers"]["notion"], **WRAP_ARGS))
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

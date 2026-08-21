"""Tests for the scan subcommand's pure helpers (orchestration that shells to
`claude` is covered by manual e2e, not unit tests)."""

from __future__ import annotations

import json
import sys

import pytest

from baton_proxy import scan


def test_scan_main_requires_config() -> None:
    # No --config and no server → hard error (SystemExit from argparse).
    with pytest.raises(SystemExit):
        scan.scan_main([])


def test_scan_main_rejects_bare_server_form() -> None:
    # The bare `-- <server>` form is intentionally unsupported; require --config.
    with pytest.raises(SystemExit):
        scan.scan_main(["--", "npx", "-y", "@vendor/mcp-server"])


def test_write_mcp_config_wraps_server_in_proxy(tmp_path) -> None:
    sink = str(tmp_path / "events.jsonl")
    cfg_path = scan._write_mcp_config(
        str(tmp_path), ["npx", "-y", "@scope/pkg"], "@scope/pkg", sink
    )
    cfg = json.loads(open(cfg_path).read())
    target = cfg["mcpServers"]["scan_target"]
    # Re-invokes baton-proxy as the wrapper, not the server directly.
    assert target["command"] == sys.executable
    assert target["args"] == ["-m", "baton_proxy", "--", "npx", "-y", "@scope/pkg"]
    assert target["env"]["BATON_VENDOR_ID"] == "@scope/pkg"
    assert target["env"]["BATON_EVENT_SINK"] == f"file://{sink}"


def test_first_session_id_reads_first_event(tmp_path) -> None:
    sink = tmp_path / "events.jsonl"
    sink.write_text(
        '{"session_id":"abc","event_type":"tool_call_start"}\n'
        '{"session_id":"abc","event_type":"tool_call_end"}\n'
    )
    assert scan._first_session_id(str(sink)) == "abc"


def test_first_session_id_none_when_empty_or_missing(tmp_path) -> None:
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert scan._first_session_id(str(empty)) is None
    assert scan._first_session_id(str(tmp_path / "nope.jsonl")) is None


def test_confirm_api_key_non_interactive_proceeds(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert scan._confirm_api_key_billing() is True
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY is set" in out and "non-interactive" in out


def test_confirm_api_key_interactive_yes(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    assert scan._confirm_api_key_billing() is True


def test_confirm_api_key_interactive_default_aborts(monkeypatch, capsys) -> None:
    # Bare Enter (empty) defaults to abort so we never silently bill the key.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert scan._confirm_api_key_billing() is False
    assert "Aborted" in capsys.readouterr().out


def test_resolve_driver_no_key_returns_path(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert scan._resolve_driver() == "/usr/local/bin/claude"


def test_resolve_driver_with_key_can_abort(monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-whatever")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    assert scan._resolve_driver() is None


# --- --config resolution -----------------------------------------------------


def _write_cfg(tmp_path, servers: dict) -> str:
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"mcpServers": servers}))
    return str(p)


def test_resolve_config_entry_stdio_returns_cmd_env_label(tmp_path) -> None:
    cfg = _write_cfg(
        tmp_path,
        {
            "vendor": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@vendor/mcp-server"],
                "env": {"VENDOR_API_KEY": "secret-123"},
            }
        },
    )
    server_cmd, env, label = scan._resolve_config_entry("vendor", cfg)
    assert server_cmd == ["npx", "-y", "@vendor/mcp-server"]
    assert env == {"VENDOR_API_KEY": "secret-123"}
    assert label == "vendor"  # label is the config name, not parsed from the command


def test_resolve_config_entry_unwraps_baton_proxy_and_strips_baton_env(tmp_path) -> None:
    # A real vendor's authed entry is commonly already baton-proxy-wrapped, with
    # BATON_* pointing at their live Console. scan must peel the wrapper and drop
    # those so the robot session stays local + single-wrapped.
    cfg = _write_cfg(
        tmp_path,
        {
            "notion": {
                "command": "baton-proxy",
                "args": ["--", "npx", "-y", "@notionhq/notion-mcp-server"],
                "env": {
                    "NOTION_TOKEN": "ntn_x",
                    "BATON_EVENT_SINK": "https://console.example/ingest",
                    "BATON_API_KEY": "live-key",
                },
            }
        },
    )
    server_cmd, env, _label = scan._resolve_config_entry("notion", cfg)
    assert server_cmd == ["npx", "-y", "@notionhq/notion-mcp-server"]
    assert env == {"NOTION_TOKEN": "ntn_x"}  # BATON_* stripped


def test_unwrap_baton_proxy_module_form() -> None:
    assert scan._unwrap_baton_proxy(
        ["python3", "-m", "baton_proxy", "--", "uvx", "mcp-server-time"]
    ) == ["uvx", "mcp-server-time"]
    # Not wrapped → untouched.
    assert scan._unwrap_baton_proxy(["npx", "server"]) == ["npx", "server"]


def test_resolve_config_entry_refuses_a_bridge_entry_rather_than_nesting(tmp_path) -> None:
    """Exactly what `try/kit.py setup` now writes for a remote server. It reads
    as ordinary stdio (it HAS a command) and `_unwrap_baton_proxy` cannot peel it
    (no `--`), so before the guard scan wrapped it a second time: two nested
    proxies, the annotation tool injected twice, in a report that looked normal.

    Two things went wrong at once, which is why refusing is the only right
    answer. The nesting, and `_strip_baton_env` dropping the very variable the
    inner bridge needs — BATON_UPSTREAM_AUTH_TOKEN — so the upstream would have
    401'd even single-wrapped."""
    cfg = _write_cfg(
        tmp_path,
        {
            "acme": {
                "command": "/usr/bin/python3.13",
                "args": ["-m", "baton_proxy", "--url", "https://mcp.acme.com/mcp"],
                "env": {"BATON_UPSTREAM_AUTH_TOKEN": "${ACME_TOKEN}"},
            }
        },
    )
    try:
        scan._resolve_config_entry("acme", cfg)
        raise AssertionError("expected ScanConfigError")
    except scan.ScanConfigError as e:
        assert "IS baton-proxy" in str(e)
        assert "${ACME_TOKEN}" not in str(e), "a refusal must not quote a credential"


@pytest.mark.parametrize(
    "command,args",
    [
        # Separator-less wraps `_unwrap_baton_proxy` returns untouched.
        ("baton-proxy", []),
        ("baton-proxy", ["--verbose"]),
        ("/opt/venv/bin/baton-proxy", ["--url", "https://x"]),
        # Launch forms a head-only check misses. `uvx`/`uv run` are how the
        # README tells people to run baton-proxy, so these are not exotic.
        ("uvx", ["baton-proxy", "--verbose"]),
        ("uv", ["run", "baton-proxy"]),
        ("/usr/bin/env", ["python3", "-m", "baton_proxy", "--url", "https://x"]),
        ("bash", ["-lc", "baton-proxy --url https://x"]),
    ],
)
def test_resolve_config_entry_refuses_every_unpeelable_proxy_shape(tmp_path, command, args) -> None:
    cfg = _write_cfg(tmp_path, {"s": {"command": command, "args": args}})
    try:
        scan._resolve_config_entry("s", cfg)
        raise AssertionError(f"expected ScanConfigError for {command} {args}")
    except scan.ScanConfigError as e:
        assert "baton-proxy" in str(e)


def test_a_peelable_wrap_is_still_peeled_not_refused(tmp_path) -> None:
    """The guard runs AFTER unwrap, so the case scan was built for — a vendor's
    already-wrapped entry — keeps working. Refusing that would break the common
    path to fix the rare one."""
    cfg = _write_cfg(
        tmp_path,
        {"n": {"command": "baton-proxy", "args": ["--", "npx", "-y", "srv"]}},
    )
    assert scan._resolve_config_entry("n", cfg)[0] == ["npx", "-y", "srv"]


def test_resolve_config_entry_rejects_remote(tmp_path) -> None:
    cfg = _write_cfg(tmp_path, {"remote": {"type": "http", "url": "https://x/mcp"}})
    try:
        scan._resolve_config_entry("remote", cfg)
        raise AssertionError("expected ScanConfigError")
    except scan.ScanConfigError as e:
        assert "remote" in str(e) and "stdio" in str(e)


@pytest.mark.parametrize(
    "url,secret",
    [
        # Zapier/Composio put the token in the PATH; `?key=` is just as common;
        # userinfo is the third vector. All three ARE the credential.
        ("https://mcp.zapier.com/api/mcp/s/SUPERSECRET/sse", "SUPERSECRET"),
        ("https://api.example.com/mcp?key=SUPERSECRET", "SUPERSECRET"),
        ("https://user:SUPERSECRET@api.example.com/mcp", "SUPERSECRET"),
    ],
)
def test_the_remote_refusal_names_the_endpoint_without_quoting_it(tmp_path, url, secret) -> None:
    """This message goes to a terminal and gets pasted into support threads. An
    endpoint that IS a credential must be named, never quoted — the same rule
    the try kit's refusals follow."""
    cfg = _write_cfg(tmp_path, {"remote": {"type": "http", "url": url}})
    try:
        scan._resolve_config_entry("remote", cfg)
        raise AssertionError("expected ScanConfigError")
    except scan.ScanConfigError as e:
        assert secret not in str(e)
        assert "api.example.com" in str(e) or "mcp.zapier.com" in str(e), (
            "the host still has to show, or the message stops identifying the entry"
        )


def test_resolve_config_entry_missing_lists_available(tmp_path) -> None:
    cfg = _write_cfg(tmp_path, {"github": {"command": "x"}, "notion": {"command": "y"}})
    try:
        scan._resolve_config_entry("nope", cfg)
        raise AssertionError("expected ScanConfigError")
    except scan.ScanConfigError as e:
        msg = str(e)
        assert "nope" in msg and "github" in msg and "notion" in msg


def test_write_mcp_config_merges_entry_env_and_baton_wins(tmp_path) -> None:
    sink = str(tmp_path / "events.jsonl")
    cfg_path = scan._write_mcp_config(
        str(tmp_path),
        ["npx", "-y", "@scope/pkg"],
        "vendor",
        sink,
        extra_env={"VENDOR_API_KEY": "k", "BATON_EVENT_SINK": "https://stray"},
    )
    env = json.loads(open(cfg_path).read())["mcpServers"]["scan_target"]["env"]
    assert env["VENDOR_API_KEY"] == "k"
    # Proxy vars set last → a stray entry value can't shadow the local sink.
    assert env["BATON_EVENT_SINK"] == f"file://{sink}"
    assert env["BATON_VENDOR_ID"] == "vendor"

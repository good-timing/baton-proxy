"""Tests for the scan subcommand's pure helpers (orchestration that shells to
`claude` is covered by manual e2e, not unit tests)."""

from __future__ import annotations

import json
import sys

from baton_proxy import scan


def test_server_label_prefers_package_token() -> None:
    assert scan._server_label(["npx", "-y", "@scope/pkg"]) == "@scope/pkg"
    assert scan._server_label(["python", "path/to/server.py"]) == "path/to/server.py"


def test_server_label_package_beats_trailing_path_arg() -> None:
    # Servers that take a directory arg must label as the package, not the dir.
    assert (
        scan._server_label(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp/dir"])
        == "@modelcontextprotocol/server-filesystem"
    )


def test_server_label_falls_back_to_first_non_flag() -> None:
    assert scan._server_label(["mcp-server", "--port", "3000"]) == "mcp-server"


def test_server_label_skips_runner() -> None:
    assert scan._server_label(["uvx", "mcp-server-time"]) == "mcp-server-time"
    assert scan._server_label(["npx", "-y", "some-mcp-server"]) == "some-mcp-server"


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


def test_resolve_config_entry_rejects_remote(tmp_path) -> None:
    cfg = _write_cfg(tmp_path, {"remote": {"type": "http", "url": "https://x/mcp"}})
    try:
        scan._resolve_config_entry("remote", cfg)
        raise AssertionError("expected ScanConfigError")
    except scan.ScanConfigError as e:
        assert "remote" in str(e) and "stdio" in str(e)


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

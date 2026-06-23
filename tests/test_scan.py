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

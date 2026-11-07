from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from baton_proxy import wrap
from baton_proxy.proxy import main as proxy_main

PY = "/opt/py/bin/python3"
NPX = "/opt/node/bin/npx"


def _apply(config: dict, name: str, **kw):
    text = json.dumps(config, indent=2) + "\n"
    kw.setdefault("python", PY)
    kw.setdefault("npx", NPX)
    new_text, original, wrapped = wrap.apply_wrap(text, name, **kw)
    return json.loads(new_text), original, wrapped


def test_stdio_entry_becomes_proxy_with_original_command_after_dashes():
    cfg = {
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "@v/fs"], "env": {"ROOT": "${HOME}"}},
            "other": {"command": "uvx", "args": ["x"]},
        },
        "theme": "dark",
    }
    out, original, _ = _apply(cfg, "fs")
    assert out["mcpServers"]["fs"] == {
        "command": PY,
        "args": ["-m", "baton_proxy", "--", "npx", "-y", "@v/fs"],
        "env": {"ROOT": "${HOME}"},
    }
    assert out["mcpServers"]["other"] == cfg["mcpServers"]["other"]
    assert out["theme"] == "dark"
    assert original == cfg["mcpServers"]["fs"]


def test_existing_mcp_remote_entry_wraps_as_plain_stdio():
    cfg = {"mcpServers": {"linear": {"command": "npx", "args": ["mcp-remote", "https://x/mcp"]}}}
    out, _, _ = _apply(cfg, "linear")
    assert out["mcpServers"]["linear"]["args"] == [
        "-m",
        "baton_proxy",
        "--",
        "npx",
        "mcp-remote",
        "https://x/mcp",
    ]


def test_bearer_http_entry_uses_url_bridge_and_moves_token_to_env():
    cfg = {
        "mcpServers": {
            "api": {
                "type": "http",
                "url": "https://api.example/mcp",
                "headers": {"Authorization": "Bearer ${API_TOKEN}"},
            }
        }
    }
    out, _, _ = _apply(cfg, "api")
    assert out["mcpServers"]["api"] == {
        "command": PY,
        "args": ["-m", "baton_proxy", "--url", "https://api.example/mcp"],
        "env": {"BATON_UPSTREAM_AUTH_TOKEN": "${API_TOKEN}"},
    }


def test_http_entry_without_credential_goes_through_mcp_remote():
    cfg = {"mcpServers": {"notion": {"type": "http", "url": "https://mcp.notion.com/mcp"}}}
    out, _, _ = _apply(cfg, "notion")
    entry = out["mcpServers"]["notion"]
    assert entry["command"] == PY
    assert entry["args"] == [
        "-m",
        "baton_proxy",
        "--",
        NPX,
        "-y",
        "mcp-remote",
        "https://mcp.notion.com/mcp",
    ]
    assert entry["env"]["PATH"].split(os.pathsep)[0] == "/opt/node/bin"
    assert "type" not in entry and "url" not in entry


def test_url_with_no_entry_adds_a_new_mcp_remote_entry():
    cfg = {"mcpServers": {}}
    out, original, _ = _apply(cfg, "linear", url="https://mcp.linear.app/mcp")
    assert original is None
    assert out["mcpServers"]["linear"]["args"][-2:] == ["mcp-remote", "https://mcp.linear.app/mcp"]


def test_url_creates_mcp_servers_block_when_config_has_none():
    out, _, _ = _apply({"globalShortcut": "x"}, "linear", url="https://mcp.linear.app/mcp")
    assert "linear" in out["mcpServers"]
    assert out["globalShortcut"] == "x"


def test_url_for_a_name_already_in_the_config_is_refused():
    cfg = {"mcpServers": {"linear": {"command": "npx", "args": ["x"]}}}
    with pytest.raises(wrap.Refused, match="already"):
        _apply(cfg, "linear", url="https://mcp.linear.app/mcp")


def test_an_entry_already_running_the_proxy_is_refused():
    cfg = {"mcpServers": {"fs": {"command": "baton-proxy", "args": ["--", "npx", "fs"]}}}
    with pytest.raises(wrap.Refused, match="already wrapped"):
        _apply(cfg, "fs")


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "sse", "url": "https://x/sse"},
        {"type": "http", "url": "https://x/mcp", "headers": {"X-Key": "abc"}},
    ],
)
def test_shapes_we_cannot_carry_are_refused(entry):
    with pytest.raises(wrap.Refused):
        _apply({"mcpServers": {"s": entry}}, "s")


def test_oauth_shape_without_npx_is_refused():
    cfg = {"mcpServers": {}}
    with pytest.raises(wrap.Refused, match="npx"):
        _apply(cfg, "linear", url="https://mcp.linear.app/mcp", npx=None)


def test_unknown_name_is_refused_and_lists_the_names():
    cfg = {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}
    with pytest.raises(wrap.Refused, match="a, b"):
        _apply(cfg, "c")


def test_baton_env_is_written_after_the_users_env():
    cfg = {"mcpServers": {"fs": {"command": "npx", "env": {"BATON_EVENT_SINK": "stderr:"}}}}
    out, _, _ = _apply(cfg, "fs", baton_env={"BATON_EVENT_SINK": "https://c.example"})
    assert out["mcpServers"]["fs"]["env"]["BATON_EVENT_SINK"] == "https://c.example"


def test_tab_indent_is_kept():
    text = '{\n\t"mcpServers": {\n\t\t"fs": {\n\t\t\t"command": "npx"\n\t\t}\n\t}\n}\n'
    new_text, _, _ = wrap.apply_wrap(text, "fs", python=PY, npx=NPX)
    assert new_text.startswith('{\n\t"mcpServers"')


def test_project_scope_in_claude_json():
    cfg = {"projects": {"/p": {"mcpServers": {"fs": {"command": "npx"}}}}}
    out, _, _ = _apply(cfg, "fs", project="/p")
    assert out["projects"]["/p"]["mcpServers"]["fs"]["command"] == PY


# --- the command, end to end through the proxy's own entry point ---


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(wrap, "_find_npx", lambda: NPX)
    return tmp_path


@pytest.fixture
def cfg(home):
    return home / "cfg.json"


def run(cfg: Path, *argv: str) -> int:
    return proxy_main([*argv, "--config", str(cfg)])


def _write(path: Path, data: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    path.write_text(text)
    return text


def _wrap_then_edit(path: Path, name: str, entry: dict, *argv: str) -> str:
    _write(path, {"mcpServers": {name: entry}})
    assert proxy_main(["wrap", name, *argv]) == 0
    data = json.loads(path.read_text())
    data["mcpServers"][name]["args"].append("--edited")
    return _write(path, data)


def test_wrap_then_unwrap_restores_the_config_byte_for_byte(home, cfg):
    before = _write(cfg, {"mcpServers": {"fs": {"command": "npx", "args": ["@v/fs"]}}})

    assert run(cfg, "wrap", "fs") == 0
    assert json.loads(cfg.read_text())["mcpServers"]["fs"]["args"][:2] == ["-m", "baton_proxy"]
    assert (home / "cfg.json.baton-backup").read_text() == before

    assert run(cfg, "unwrap", "fs") == 0
    assert cfg.read_text() == before


def test_unwrap_of_an_added_entry_removes_it(cfg):
    before = _write(cfg, {"mcpServers": {}})
    assert run(cfg, "wrap", "linear", "--url", "https://mcp.linear.app/mcp") == 0
    assert run(cfg, "unwrap", "linear") == 0
    assert cfg.read_text() == before


def test_unwrap_refuses_an_edited_entry_and_prints_the_original(cfg, capsys):
    edited = _wrap_then_edit(cfg, "fs", {"command": "npx", "args": ["@v/fs"]}, "--config", str(cfg))

    assert run(cfg, "unwrap", "fs") == 1
    assert cfg.read_text() == edited
    err = capsys.readouterr().err
    assert "changed since" in err
    assert '"@v/fs"' in err


def test_unwrap_refusal_hides_the_originals_header_values(cfg, capsys):
    secret = "sk-live-abcdefghijklmnop"
    entry = {
        "type": "http",
        "url": "https://a/mcp",
        "headers": {"Authorization": f"Bearer {secret}"},
    }
    _wrap_then_edit(cfg, "api", entry, "--config", str(cfg))
    assert run(cfg, "unwrap", "api") == 1
    out = capsys.readouterr()
    assert secret not in out.out + out.err


def test_dry_run_writes_nothing(cfg, capsys):
    before = _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    assert run(cfg, "wrap", "fs", "--dry-run") == 0
    assert cfg.read_text() == before
    assert "baton_proxy" in capsys.readouterr().out


def test_refusal_exits_1_and_leaves_the_file(cfg, capsys):
    before = _write(cfg, {"mcpServers": {"s": {"type": "sse", "url": "https://x/sse"}}})
    assert run(cfg, "wrap", "s") == 1
    assert cfg.read_text() == before
    assert capsys.readouterr().err


CONSOLE = ("--sink", "https://c.example", "--vendor", "acme", "--consent", "tok")


def test_api_key_is_never_printed(cfg, capsys):
    _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    key = "baton_sk_secretsecretsecret"
    assert run(cfg, "wrap", "fs", *CONSOLE, "--api-key", key) == 0
    out = capsys.readouterr()
    assert key not in out.out + out.err
    env = json.loads(cfg.read_text())["mcpServers"]["fs"]["env"]
    assert env["BATON_API_KEY"] == key
    assert env["BATON_VENDOR_ID"] == "acme"
    assert env["BATON_CONSENT_TOKEN"] == "tok"


@pytest.mark.parametrize(
    ("argv", "missing"),
    [
        (("--sink", "https://c.example", "--vendor", "v", "--consent", "c"), "--api-key"),
        (("--sink", "https://c.example", "--api-key", "k", "--consent", "c"), "--vendor"),
        (("--sink", "https://c.example", "--api-key", "k", "--vendor", "v"), "--consent"),
        (("--sink", "s3://bucket/x", "--vendor", "v"), "--consent"),
        (("--api-key", "k"), "--api-key"),
    ],
)
def test_a_sink_the_proxy_would_refuse_at_start_is_refused_at_wrap(cfg, capsys, argv, missing):
    before = _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    assert run(cfg, "wrap", "fs", *argv) == 1
    assert cfg.read_text() == before
    assert missing in capsys.readouterr().err


def test_a_local_sink_needs_no_vendor_or_consent(cfg):
    _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    assert run(cfg, "wrap", "fs", "--sink", "file:///tmp/e.jsonl") == 0


def test_desktop_is_the_default_config_on_macos(home, monkeypatch):
    monkeypatch.setattr(wrap.sys, "platform", "darwin")
    assert wrap.client_config_path("desktop") == (
        home / "Library/Application Support/Claude/claude_desktop_config.json"
    )
    assert wrap.client_config_path("code") == home / ".claude.json"


def test_proxy_forms_still_parse_after_the_subcommands():
    with pytest.raises(SystemExit):
        proxy_main([])


@pytest.mark.parametrize(
    "exe",
    [
        "/Users/u/.cache/uv/archive-v0/kDlgO07AupSCIO1I/bin/python",
        "/Users/u/.local/pipx/.cache/abc123/bin/python",
    ],
)
def test_a_cache_interpreter_is_refused(exe):
    with pytest.raises(wrap.Refused, match="cache"):
        wrap.launch_python(exe)


def test_an_installed_interpreter_is_kept():
    exe = "/Users/u/.local/pipx/venvs/baton-proxy/bin/python"
    assert wrap.launch_python(exe) == exe


def test_wrap_from_a_uvx_cache_writes_nothing(cfg, monkeypatch):
    before = _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    monkeypatch.setattr(wrap.sys, "executable", "/u/.cache/uv/archive-v0/x/bin/python")
    assert run(cfg, "wrap", "fs") == 1
    assert cfg.read_text() == before


@pytest.mark.parametrize("auth", ["Basic dXNlcjpw", "sk-bare-token"])
def test_an_authorization_header_that_is_not_bearer_is_refused(auth):
    entry = {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": auth}}
    with pytest.raises(wrap.Refused, match="not a Bearer"):
        _apply({"mcpServers": {"s": entry}}, "s")


def test_string_args_are_refused_not_split_into_characters():
    with pytest.raises(wrap.Refused, match="args"):
        _apply({"mcpServers": {"s": {"command": "npx", "args": "--flag"}}}, "s")


def test_invalid_json_config_is_a_refusal_not_a_traceback(cfg, capsys):
    cfg.write_text('{"mcpServers": {"fs": {"command": "npx"},}}')
    assert run(cfg, "wrap", "fs") == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_a_corrupt_state_file_stops_wrap_before_the_config_is_written(home, cfg):
    before = _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    (home / ".baton-proxy").mkdir()
    (home / ".baton-proxy" / "wraps.json").write_text("{not json")
    assert run(cfg, "wrap", "fs") == 1
    assert cfg.read_text() == before


def test_undo_hint_names_the_client_and_project(home, capsys, monkeypatch):
    claude = home / ".claude.json"
    proj = str(home / "repo")
    _write(claude, {"projects": {proj: {"mcpServers": {"fs": {"command": "npx"}}}}})
    monkeypatch.chdir(home)
    assert proxy_main(["wrap", "fs", "--client", "code", "--project", "repo/"]) == 0
    assert f"baton-proxy unwrap fs --client code --project {proj}" in capsys.readouterr().out
    assert proxy_main(["unwrap", "fs", "--client", "code", "--project", proj]) == 0


def test_project_matches_through_a_symlink(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    real = str(tmp_path / "real")
    cfg = {"projects": {real: {"mcpServers": {"fs": {"command": "npx"}}}}}
    out, _, _ = _apply(cfg, "fs", project=str(tmp_path / "link"))
    assert out["projects"][real]["mcpServers"]["fs"]["command"] == PY


@pytest.mark.parametrize(
    "client_default", [{"type": "stdio"}, {"env": {}}, {"type": "stdio", "env": {}}]
)
def test_unwrap_tolerates_defaults_a_client_writes_back(home, client_default):
    claude = home / ".claude.json"
    before = _write(claude, {"mcpServers": {"fs": {"command": "npx"}}})
    assert proxy_main(["wrap", "fs", "--client", "code"]) == 0
    data = json.loads(claude.read_text())
    data["mcpServers"]["fs"].update(client_default)
    _write(claude, data)
    assert proxy_main(["unwrap", "fs", "--client", "code"]) == 0
    assert claude.read_text() == before


def test_a_macos_pipx_run_cache_is_refused():
    with pytest.raises(wrap.Refused, match="cache"):
        wrap.launch_python("/Users/u/Library/Caches/pipx/abc/bin/python")


def test_mcp_remote_path_extends_the_path_wrap_ran_with(monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/home/u/.nvm/bin", "/usr/bin"]))
    out, _, _ = _apply({"mcpServers": {}}, "l", url="https://x/mcp")
    assert out["mcpServers"]["l"]["env"]["PATH"].split(os.pathsep) == [
        "/opt/node/bin",
        "/home/u/.nvm/bin",
        "/usr/bin",
    ]


@pytest.mark.parametrize("key", ["headersHelper", "oauth"])
def test_http_auth_the_proxy_cannot_carry_is_refused(key):
    entry = {"type": "http", "url": "https://x/mcp", key: {"x": 1}}
    with pytest.raises(wrap.Refused, match=key):
        _apply({"mcpServers": {"s": entry}}, "s")


@pytest.mark.parametrize(
    ("sink", "expect"), [("HTTPS://c.example", "--api-key"), ("htps://c.example", "no sink")]
)
def test_sink_schemes_are_read_as_the_proxy_reads_them(cfg, capsys, sink, expect):
    _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    assert run(cfg, "wrap", "fs", "--sink", sink, "--vendor", "v", "--consent", "c") == 1
    assert expect in capsys.readouterr().err


def test_unwrap_of_an_entry_removed_by_hand_clears_its_record(home, cfg):
    _write(cfg, {"mcpServers": {}})
    run(cfg, "wrap", "linear", "--url", "https://mcp.linear.app/mcp")
    _write(cfg, {"mcpServers": {}})
    assert run(cfg, "unwrap", "linear") == 0
    assert json.loads((home / ".baton-proxy" / "wraps.json").read_text()) == {}


def test_unwrap_with_no_config_file_is_a_message_not_a_traceback(cfg, capsys):
    _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    run(cfg, "wrap", "fs")
    cfg.unlink()
    assert run(cfg, "unwrap", "fs") == 1
    assert "baton-proxy unwrap:" in capsys.readouterr().err


def test_a_token_in_a_url_query_is_not_printed(cfg, capsys):
    _write(cfg, {"mcpServers": {}})
    run(cfg, "wrap", "z", "--url", "https://x/mcp?token=sekrit123", "--dry-run")
    out = capsys.readouterr().out
    assert "sekrit123" not in out
    assert "https://x/mcp?" in out


def test_the_backup_is_the_config_just_before_each_wrap(home, cfg):
    _write(cfg, {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}})
    run(cfg, "wrap", "a")
    before_b = cfg.read_text()
    run(cfg, "wrap", "b")
    assert (home / "cfg.json.baton-backup").read_text() == before_b


def test_a_state_file_that_cannot_be_written_leaves_the_config_alone(home, cfg):
    before = _write(cfg, {"mcpServers": {"fs": {"command": "npx"}}})
    (home / ".baton-proxy").write_text("a file where the directory should be")
    assert run(cfg, "wrap", "fs") == 1
    assert cfg.read_text() == before

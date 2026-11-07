"""`baton-proxy wrap` / `unwrap`: put one MCP server behind the proxy by rewriting the client config."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from baton_proxy.sinks import SINK_SCHEMES, is_remote_spec, spec_schemes

_PROXY_NAMES = frozenset({"baton-proxy", "baton_proxy"})
_SHOWN_ENV = frozenset({"BATON_EVENT_SINK", "BATON_TENANT_ID", "BATON_VENDOR_ID", "PATH"})
_VAR_REF = re.compile(r"^\$\{[^}]*\}$")
# http keys that carry auth the proxy cannot: dropping them would lose it silently.
_UNCARRIED_AUTH = ("headersHelper", "oauth")


class Refused(Exception):
    """A wrap or unwrap that would leave a broken or unrecoverable entry."""


def client_config_path(client: str) -> Path:
    home = Path.home()
    if client == "code":
        return home / ".claude.json"
    if sys.platform == "darwin":
        return home / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return (
            Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
            / "Claude/claude_desktop_config.json"
        )
    return home / ".config/Claude/claude_desktop_config.json"


def _detect_indent(text: str) -> int | str:
    m = re.search(r'^\{\s*?\n([ \t]+)"', text)
    if not m:
        return 2
    return "\t" if "\t" in m.group(1) else len(m.group(1))


def _dumps_like(data: Any, original_text: str) -> str:
    out = json.dumps(data, indent=_detect_indent(original_text), ensure_ascii=False)
    return out + "\n" if original_text.endswith("\n") else out


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise Refused(f"{what} is not valid JSON ({e}); fix it, then run again") from e
    if not isinstance(data, dict):
        raise Refused(f"{what} is not a JSON object")
    return data


def _servers(data: dict[str, Any], project: str | None, *, create: bool) -> dict[str, Any]:
    parent: Any = data
    if project is not None:
        projects = data.get("projects")
        projects = projects if isinstance(projects, dict) else {}
        key = next(
            (k for k in projects if os.path.realpath(k) == os.path.realpath(project)), project
        )
        parent = projects.get(key)
        if not isinstance(parent, dict):
            raise Refused(f"no project {project!r} in this config")
    block = parent.setdefault("mcpServers", {}) if create else parent.get("mcpServers", {})
    if not isinstance(block, dict):
        raise Refused("`mcpServers` in this config is not an object")
    return block


def _args(entry: dict[str, Any]) -> list[str]:
    args = entry.get("args") or []
    if not isinstance(args, list):
        raise Refused("this entry's `args` is not a list")
    return [str(a) for a in args]


def _is_wrapped(entry: dict[str, Any]) -> bool:
    cmd = [str(entry.get("command", "")), *_args(entry)]
    return any(os.path.basename(piece) in _PROXY_NAMES for token in cmd for piece in token.split())


def _is_stdio(entry: dict[str, Any]) -> bool:
    if entry.get("type") in ("http", "sse"):
        return False
    return isinstance(entry.get("command"), str) and bool(entry["command"])


def _bearer(entry: dict[str, Any]) -> tuple[str | None, list[str]]:
    headers = entry.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    auth = next((v for k, v in headers.items() if str(k).lower() == "authorization"), None)
    others = sorted(str(k) for k in headers if str(k).lower() != "authorization")
    if auth is None:
        return None, others
    if not isinstance(auth, str) or not auth.strip().lower().startswith("bearer "):
        return None, [*others, "Authorization (not a Bearer token)"]
    # A ${VAR} stays a reference: the client expands it at launch, as it did in the header.
    token = auth.strip()[len("bearer ") :].strip()
    return (token or None), others


def _proxy_entry(base: dict[str, Any], python: str, args: list[str]) -> dict[str, Any]:
    entry = {k: v for k, v in base.items() if k not in ("type", "url", "headers")}
    entry["command"] = python
    entry["args"] = ["-m", "baton_proxy", *args]
    entry["env"] = {str(k): str(v) for k, v in (base.get("env") or {}).items()}
    return entry


def _mcp_remote_entry(
    url: str, base: dict[str, Any], python: str, npx: str | None
) -> dict[str, Any]:
    if npx is None:
        raise Refused(
            "this server signs in with OAuth, which goes through `npx mcp-remote`,\n"
            "  and npx is not on PATH. Install Node.js, then run wrap again."
        )
    entry = _proxy_entry(base, python, ["--", npx, "-y", "mcp-remote", url])
    env = entry["env"]
    # npx starts with `#!/usr/bin/env node`; a GUI-launched client's PATH may not hold node.
    npx_dir = os.path.dirname(npx)
    # The PATH wrap runs with stands in for one the entry lacks; the client's may be much shorter.
    base_path = env.get("PATH") or os.environ.get("PATH", "")
    parts = [p for p in base_path.split(os.pathsep) if p]
    env["PATH"] = os.pathsep.join([npx_dir, *[p for p in parts if p != npx_dir]])
    return entry


def build_wrapped_entry(
    original: dict[str, Any] | None,
    *,
    url: str | None,
    python: str,
    npx: str | None,
) -> dict[str, Any]:
    if original is None:
        assert url is not None
        return _mcp_remote_entry(url, {}, python, npx)
    if _is_wrapped(original):
        raise Refused("this entry is already wrapped by baton-proxy")
    if _is_stdio(original):
        wrapped = dict(original)
        wrapped["command"] = python
        wrapped["args"] = ["-m", "baton_proxy", "--", original["command"], *_args(original)]
        return wrapped
    if original.get("type") == "sse":
        raise Refused(
            "sse transport is not supported; use the server's Streamable HTTP url with --url"
        )
    entry_url = original.get("url")
    if not isinstance(entry_url, str) or not entry_url.strip():
        raise Refused("this entry has neither a command nor a url")
    entry_url = entry_url.strip()
    uncarried = [k for k in _UNCARRIED_AUTH if k in original]
    if uncarried:
        raise Refused(
            f"this entry signs in with {', '.join(uncarried)}, which the proxy cannot carry"
        )
    token, others = _bearer(original)
    if others:
        raise Refused(f"this entry sends headers the proxy cannot carry: {', '.join(others)}")
    if token is None:
        return _mcp_remote_entry(entry_url, original, python, npx)
    wrapped = _proxy_entry(original, python, ["--url", entry_url])
    wrapped["env"]["BATON_UPSTREAM_AUTH_TOKEN"] = token
    return wrapped


def apply_wrap(
    config_text: str,
    name: str,
    *,
    python: str,
    npx: str | None,
    url: str | None = None,
    project: str | None = None,
    baton_env: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Returns (new config text, the original entry or None if added, the entry written)."""
    data = _parse(config_text, "the config")
    servers = _servers(data, project, create=url is not None)
    original = servers.get(name)
    if url is not None and original is not None:
        raise Refused(f"{name!r} is already in this config; drop --url to wrap the existing entry")
    if url is None and original is None:
        names = ", ".join(sorted(servers)) or "none"
        raise Refused(f"no server named {name!r}. Servers in this config: {names}")
    if original is not None and not isinstance(original, dict):
        raise Refused(f"{name!r} is not an object")
    wrapped = build_wrapped_entry(original, url=url, python=python, npx=npx)
    if baton_env:
        wrapped["env"] = {**(wrapped.get("env") or {}), **baton_env}
    servers[name] = wrapped
    return _dumps_like(data, config_text), original, wrapped


def _normal_form(entry: Any) -> Any:
    """An entry without the defaults a client may add when it writes the config back."""
    if not isinstance(entry, dict):
        return entry
    out = {k: v for k, v in entry.items() if v not in ({}, [])}
    if out.get("type") == "stdio":
        del out["type"]
    return out


def apply_unwrap(
    config_text: str,
    name: str,
    *,
    original: dict[str, Any] | None,
    wrapped: dict[str, Any],
    project: str | None = None,
) -> str | None:
    """The restored config text, or None if the entry is already gone."""
    data = _parse(config_text, "the config")
    servers = _servers(data, project, create=False)
    current = servers.get(name)
    if current is None:
        return None
    if _normal_form(current) != _normal_form(wrapped):
        shown = _shown(original) if original else "(none: wrap added it)"
        raise Refused(
            f"{name!r} has changed since it was wrapped, so restoring it would lose that edit.\n"
            f"  Edit the entry by hand. The original was:\n{shown}"
        )
    if original is None:
        del servers[name]
    else:
        servers[name] = original
    return _dumps_like(data, config_text)


def _state_path() -> Path:
    return Path.home() / ".baton-proxy" / "wraps.json"


def _state_key(config: Path, project: str | None, name: str) -> str:
    return json.dumps([str(config), project, name])


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    return _parse(path.read_text(encoding="utf-8"), str(path))


def _write_atomically(path: Path, text: str, *, mode: int | None = None) -> None:
    if mode is None:
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def _save_state(state: dict[str, Any]) -> None:
    # 0600: the saved entries can hold a literal token from the user's config.
    _write_atomically(_state_path(), json.dumps(state, indent=2) + "\n", mode=0o600)


def _without_query(value: Any) -> Any:
    if not isinstance(value, str) or "://" not in value:
        return value
    parts = urllib.parse.urlsplit(value)
    return parts._replace(query="<not shown>" if parts.query else "").geturl()


def _shown(entry: dict[str, Any]) -> str:
    out = dict(entry)
    if "url" in out:
        out["url"] = _without_query(out["url"])
    if isinstance(out.get("args"), list):
        out["args"] = [_without_query(a) for a in out["args"]]
    env = entry.get("env")
    if isinstance(env, dict):
        out["env"] = {
            k: v if k in _SHOWN_ENV or _VAR_REF.match(str(v)) else "<not shown>"
            for k, v in env.items()
        }
    headers = entry.get("headers")
    if isinstance(headers, dict):
        out["headers"] = {k: "<not shown>" for k in headers}
    return json.dumps(out, indent=2)


def launch_python(executable: str) -> str:
    """The interpreter to write into the config, refusing one a cache clean deletes."""
    parts = [p.lower() for p in Path(executable).parts]
    in_cache = any(p in (".cache", "cache", "caches") for p in parts)
    if "archive-v0" in parts or ("pipx" in parts and in_cache):
        raise Refused(
            f"this baton-proxy runs from a temporary cache ({executable}),\n"
            "  which is deleted on a cache clean, and the server would stop starting.\n"
            "  Install it first (`pipx install baton-proxy`), then run wrap again."
        )
    return executable


def _find_npx() -> str | None:
    return shutil.which("npx")


def _restart_hint(client: str) -> str:
    return (
        "Quit and reopen Claude Desktop."
        if client == "desktop"
        else "Start a new Claude Code session. If one was open during the wrap, check the\n"
        "entry is still there: an open session can write ~/.claude.json back."
    )


def _project_path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="baton-proxy", description="Put an MCP server behind baton-proxy."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("wrap", "unwrap"):
        s = sub.add_parser(cmd)
        s.add_argument("name", help="The server's name in the config.")
        s.add_argument("--client", choices=("desktop", "code"), default="desktop")
        s.add_argument("--config", type=Path, help="A config file to edit instead of the client's.")
        s.add_argument("--project", type=_project_path, help="A project path in ~/.claude.json.")
    w = sub.choices["wrap"]
    w.add_argument("--url", help="Add a new remote server (signed in through mcp-remote).")
    w.add_argument("--sink", help="BATON_EVENT_SINK, e.g. https://baton.goodtiming.ai")
    w.add_argument("--api-key", help="BATON_API_KEY, required with an http(s) sink.")
    w.add_argument("--vendor", help="BATON_VENDOR_ID, required with a remote sink.")
    w.add_argument("--consent", help="BATON_CONSENT_TOKEN, required with a remote sink.")
    w.add_argument("--tenant", help="BATON_TENANT_ID")
    w.add_argument("--dry-run", action="store_true", help="Print the entry; write nothing.")
    return p


def _baton_env(args: argparse.Namespace) -> dict[str, str]:
    """The BATON_* values to write, refusing a set the proxy would refuse at start."""
    schemes = spec_schemes(args.sink)
    unknown = sorted(set(schemes) - SINK_SCHEMES)
    if unknown:
        raise Refused(f"the proxy has no sink for {', '.join(map(repr, unknown))}")
    http_sink = any(s in ("http", "https") for s in schemes)
    if http_sink and not args.api_key:
        raise Refused("an http(s) sink needs --api-key; the proxy will not start without one")
    if args.api_key and not http_sink:
        raise Refused("--api-key is only used by an http(s) --sink")
    if is_remote_spec(args.sink):
        missing = [f for f in ("vendor", "consent") if not getattr(args, f)]
        if missing:
            flags = " and ".join(f"--{f}" for f in missing)
            raise Refused(f"a remote sink needs {flags}; the proxy will not start without them")
    pairs = {
        "BATON_EVENT_SINK": args.sink,
        "BATON_API_KEY": args.api_key,
        "BATON_TENANT_ID": args.tenant,
        "BATON_VENDOR_ID": args.vendor,
        "BATON_CONSENT_TOKEN": args.consent,
    }
    return {k: v for k, v in pairs.items() if v}


def cli(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    config = (args.config or client_config_path(args.client)).expanduser().resolve()
    try:
        return _wrap(args, config) if args.cmd == "wrap" else _unwrap(args, config)
    except (Refused, OSError) as e:
        print(f"baton-proxy {args.cmd}: {e}", file=sys.stderr)
        return 1


def _wrap(args: argparse.Namespace, config: Path) -> int:
    exists = config.exists()
    if exists:
        text = config.read_text(encoding="utf-8")
    elif args.url:
        text = ""
    else:
        raise Refused(f"no config at {config}")
    new_text, original, wrapped = apply_wrap(
        text,
        args.name,
        python=launch_python(sys.executable),
        npx=_find_npx(),
        url=args.url,
        project=args.project,
        baton_env=_baton_env(args),
    )
    print(f"{args.name} in {config}:\n{_shown(wrapped)}")
    if args.dry_run:
        print("Dry run: nothing written.")
        return 0
    # The record is saved first: a config wrapped with no record could never be unwrapped.
    state = _load_state()
    state[_state_key(config, args.project, args.name)] = {"original": original, "wrapped": wrapped}
    _save_state(state)
    if exists:
        shutil.copy2(config, _backup_path(config))
    _write_atomically(config, new_text)
    print(_restart_hint(args.client))
    signs_in = original is None or not _is_stdio(original)
    if signs_in and "mcp-remote" in wrapped["args"]:
        print(
            "The first start opens a browser to sign in. If this server is also\n"
            "added as a connector in the Claude app, turn that one off, or the\n"
            "agent sees both and may use the one Baton cannot see."
        )
    print(f"Undo: {_unwrap_command(args, config)}")
    return 0


def _backup_path(config: Path) -> Path:
    return config.with_name(config.name + ".baton-backup")


def _unwrap_command(args: argparse.Namespace, config: Path) -> str:
    parts = ["baton-proxy", "unwrap", args.name]
    if args.config:
        parts += ["--config", str(config)]
    elif args.client != "desktop":
        parts += ["--client", args.client]
    if args.project:
        parts += ["--project", args.project]
    return shlex.join(parts)


def _unwrap(args: argparse.Namespace, config: Path) -> int:
    state = _load_state()
    key = _state_key(config, args.project, args.name)
    record = state.get(key)
    if record is None:
        raise Refused(
            f"no record of wrapping {args.name!r} in {config}.\n"
            f"  Edit the entry by hand. {_backup_path(config)} is the config as it was\n"
            "  before the last wrap, if one ran."
        )
    new_text = apply_unwrap(
        config.read_text(encoding="utf-8"),
        args.name,
        original=record.get("original"),
        wrapped=record.get("wrapped") or {},
        project=args.project,
    )
    if new_text is None:
        del state[key]
        _save_state(state)
        print(f"{args.name} is no longer in {config}; its wrap record is cleared.")
        return 0
    _write_atomically(config, new_text)
    del state[key]
    _save_state(state)
    print(f"{args.name} restored in {config}. {_restart_hint(args.client)}")
    return 0

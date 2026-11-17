"""`baton-proxy check`: the tool list a running listener shows, against the server's own."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from baton_proxy._jsonrpc import answers
from baton_proxy._llm_text import INTENT_PARAM_NAMES
from baton_proxy.listen import (
    OVERLAY_HEADER,
    UpstreamRefused,
    UpstreamUnreachable,
    forward_delete,
    forward_post,
)
from baton_proxy.proxy import ANNOTATE_TOOL_NAME, shown_url
from baton_proxy.transport_http import SESSION_HEADER, env_timeout, read_messages

_PROTOCOL_VERSION = "2025-06-18"
_MAX_PAGES = 50


class CheckFailed(Exception):
    pass


def list_tools(url: str, headers: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    """Every tool ``url`` lists after a handshake, and its Baton-Overlay header."""
    timeout_s = env_timeout()
    sent = dict(headers)
    overlay: str | None = None

    def call(msg: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal overlay
        try:
            resp = forward_post(url, msg, sent, timeout_s)
        except UpstreamRefused as refused:
            raise CheckFailed(f"{shown_url(url)} answered HTTP {refused.status}") from None
        except UpstreamUnreachable as exc:
            raise CheckFailed(f"{shown_url(url)} is unreachable: {exc}") from None
        with resp:
            overlay = resp.headers.get(OVERLAY_HEADER, overlay)
            session = resp.headers.get(SESSION_HEADER)
            if session and SESSION_HEADER not in sent:
                sent[SESSION_HEADER] = session
            messages = read_messages(resp, msg.get("id"))
        answer = next((m for m in messages if answers(m, msg.get("id"))), None)
        if "id" in msg and (answer is None or "result" not in answer):
            raise CheckFailed(f"{shown_url(url)} did not answer {msg['method']}: {answer}")
        return answer

    init = {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "baton-proxy-check", "version": "1"},
    }
    call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    try:
        call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            msg = {"jsonrpc": "2.0", "id": 2 + page, "method": "tools/list", "params": params}
            result = (call(msg) or {})["result"]
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
    finally:
        if SESSION_HEADER in sent:
            forward_delete(url, sent, timeout_s)
    return tools, overlay


def without_proxy_additions(tool: dict[str, Any]) -> dict[str, Any]:
    """``tool`` as the listener would show it with nothing of the proxy's own."""
    tool = json.loads(json.dumps(tool))
    schema = tool.get("inputSchema")
    if isinstance(schema, dict):
        props = schema.get("properties")
        required = schema.get("required")
        for name in INTENT_PARAM_NAMES:
            if isinstance(props, dict):
                props.pop(name, None)
            if isinstance(required, list) and name in required:
                required.remove(name)
        if required == []:
            del schema["required"]
    return tool


def compare(
    direct: list[dict[str, Any]], proxied: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Tool names changed, missing from the listener, and added by it."""
    theirs = {t.get("name"): t for t in direct}
    ours = {
        t.get("name"): without_proxy_additions(t)
        for t in proxied
        if t.get("name") != ANNOTATE_TOOL_NAME
    }
    changed = sorted(n for n in theirs.keys() & ours.keys() if theirs[n] != ours[n])
    return changed, sorted(theirs.keys() - ours.keys()), sorted(ours.keys() - theirs.keys())


def _headers(pairs: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition(":")
        if not sep or not name.strip():
            raise ValueError(f"--header takes 'Name: value', got {pair!r}")
        headers[name.strip()] = value.strip()
    token = os.environ.get("BATON_UPSTREAM_AUTH_TOKEN")
    if token and not any(k.lower() == "authorization" for k in headers):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="baton-proxy check",
        description=(
            "List tools through a running `baton-proxy listen` and from the server "
            "directly, with the same headers, and say whether they match."
        ),
    )
    parser.add_argument("--listener", required=True, help="The listener's URL, as clients use it")
    parser.add_argument("--url", required=True, help="The server's own URL, query included")
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="'NAME: VALUE'",
        help="Sent to both; repeat for more. BATON_UPSTREAM_AUTH_TOKEN fills Authorization.",
    )
    args = parser.parse_args(argv)
    try:
        headers = _headers(args.header)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        direct, _ = list_tools(args.url, headers)
        proxied, overlay = list_tools(args.listener, headers)
    except CheckFailed as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        return 2
    changed, missing, added = compare(direct, proxied)
    print(f"server:   {len(direct)} tools at {shown_url(args.url)}")
    print(f"listener: {len(proxied)} tools at {shown_url(args.listener)}, overlay {overlay}")
    if overlay is None:
        print("check failed: no Baton-Overlay header; is --listener a baton-proxy listener?")
        return 2
    if not (changed or missing or added):
        print("same: the listener shows the server's tools, plus the proxy's goal param")
        return 0
    for label, names in (("changed", changed), ("hidden", missing), ("added", added)):
        if names:
            print(f"{label}: {', '.join(names)}")
    if overlay == "none":
        print("different, with no overlay loaded")
        return 1
    print("different, as the overlay says")
    return 0

"""`baton-proxy check`: the tool list a running listener shows, against the server's own."""

from __future__ import annotations

import argparse
import copy
import os
import sys
from typing import Any

from baton_proxy._jsonrpc import answers, owed_answer_id
from baton_proxy._urls import shown_url
from baton_proxy.listen import (
    OVERLAY_HEADER,
    UpstreamRefused,
    UpstreamUnreachable,
    forward_delete,
    forward_post,
)
from baton_proxy.overlay import Overlay, OverlayError, load_overlay, overlay_label
from baton_proxy.proxy import ANNOTATE_TOOL_NAME, _inject_goal_params
from baton_proxy.transport_http import SESSION_HEADER, env_timeout, read_messages

# What current MCP clients send, so the check lists the surface they are shown.
PROTOCOL_VERSION = "2025-06-18"
_MAX_PAGES = 50


class CheckFailed(Exception):
    pass


def list_tools(url: str, headers: dict[str, str]) -> tuple[list[dict[str, Any]], str | None]:
    """Every tool ``url`` lists after a handshake, and its Baton-Overlay header."""
    timeout_s = env_timeout()
    sent = dict(headers)
    overlay: str | None = None

    def call(msg: dict[str, Any]) -> Any:
        nonlocal overlay
        try:
            resp = forward_post(url, msg, sent, timeout_s)
            with resp:
                overlay = resp.headers.get(OVERLAY_HEADER, overlay)
                session = resp.headers.get(SESSION_HEADER)
                if session and SESSION_HEADER not in sent:
                    sent[SESSION_HEADER] = session
                messages = read_messages(resp, owed_answer_id(msg))
        except UpstreamRefused as refused:
            raise CheckFailed(f"{shown_url(url)} answered HTTP {refused.status}") from None
        except UpstreamUnreachable as exc:
            raise CheckFailed(f"{shown_url(url)} is unreachable: {exc}") from None
        except Exception as exc:
            # Only the type: urllib's messages can quote the URL and its query.
            raise CheckFailed(f"{shown_url(url)} failed: {type(exc).__name__}") from None
        answer = next((m for m in messages if answers(m, owed_answer_id(msg))), None)
        if "id" in msg and (answer is None or "result" not in answer):
            raise CheckFailed(f"{shown_url(url)} did not answer {msg['method']}: {answer}")
        return answer

    init = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "baton-proxy-check", "version": "1"},
    }
    answer = call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init})
    version = answer["result"].get("protocolVersion")
    if isinstance(version, str):
        sent["MCP-Protocol-Version"] = version
    try:
        call({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for page in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            msg = {"jsonrpc": "2.0", "id": 2 + page, "method": "tools/list", "params": params}
            result = call(msg)["result"]
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise CheckFailed(f"{shown_url(url)} sent a tools/list result with no tool list")
            tools.extend(result["tools"])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        else:
            raise CheckFailed(f"{shown_url(url)} lists more than {_MAX_PAGES} pages of tools")
    finally:
        if SESSION_HEADER in sent:
            forward_delete(url, sent, timeout_s)
    return tools, overlay


def expected(direct: list[dict[str, Any]], overlay: Overlay | None, mode: str) -> list[Any]:
    """What the listener should list: the overlay, then the goal param, as the proxy does."""
    tools = copy.deepcopy(direct)
    if overlay is not None:
        tools = overlay.apply_to_tools(tools)
    for tool in tools:
        _inject_goal_params(tool, mode)
    return tools


def compare(
    want: list[Any], proxied: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Tool names changed, missing from the listener, and added by it."""
    theirs = {t.get("name"): t for t in want if isinstance(t, dict)}
    ours = {t.get("name"): t for t in proxied if t.get("name") != ANNOTATE_TOOL_NAME}
    changed = sorted(n for n in theirs.keys() & ours.keys() if theirs[n] != ours[n])
    return changed, sorted(theirs.keys() - ours.keys()), sorted(ours.keys() - theirs.keys())


def closest(direct: list[dict[str, Any]], proxied: list[dict[str, Any]], overlay: Overlay | None):
    """``compare`` under whichever goal-param mode the listener runs."""
    results = [
        compare(expected(direct, overlay, mode), proxied) for mode in ("required", "optional")
    ]
    return min(results, key=lambda r: sum(map(len, r)))


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
            "directly, with the same headers, and say whether the listener shows "
            "what it should."
        ),
    )
    parser.add_argument("--listener", required=True, help="The listener's URL, as clients use it")
    parser.add_argument("--url", required=True, help="The server's own URL, query included")
    parser.add_argument(
        "--overlay", default=None, metavar="PATH", help="The overlay the listener was started with"
    )
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
        overlay = load_overlay(args.overlay) if args.overlay else None
    except (ValueError, OverlayError) as exc:
        parser.error(str(exc))
    try:
        direct, _ = list_tools(args.url, headers)
        proxied, running = list_tools(args.listener, headers)
    except CheckFailed as exc:
        print(f"check failed: {exc}", file=sys.stderr)
        return 2
    print(f"server:   {len(direct)} tools at {shown_url(args.url)}")
    print(f"listener: {len(proxied)} tools at {shown_url(args.listener)}, overlay {running}")
    if running is None:
        print("check failed: no Baton-Overlay header; is --listener a baton-proxy listener?")
        return 2
    want = overlay_label(overlay)
    if running != want:
        print(f"different: the listener runs overlay {running}, and this check expects {want}")
        return 1
    changed, missing, added = closest(direct, proxied, overlay)
    if not (changed or missing or added):
        print("same: the listener shows what the server and the overlay say it should")
        return 0
    for label, names in (("changed", changed), ("missing", missing), ("added", added)):
        if names:
            print(f"{label}: {', '.join(names)}")
    print("different from what the server and the overlay say")
    return 1

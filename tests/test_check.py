from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from baton_proxy import check
from baton_proxy._llm_text import USER_GOAL_PARAM_NAME as GOAL
from baton_proxy.overlay import parse_overlay
from baton_proxy.proxy import _inject_goal_params

ODD_TOOLS = [
    {"name": "no_args", "inputSchema": {"type": "object"}},
    {"name": "empty_required", "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "no_schema"},
    {
        "name": "own_goal",
        "inputSchema": {"type": "object", "properties": {GOAL: {"type": "integer"}}},
    },
    {
        "name": "plain",
        "description": "d",
        "inputSchema": {"type": "object", "properties": {"x": {}}, "required": ["x"]},
    },
]


def _as_listed(tools: list[dict], mode: str = "required") -> list[dict]:
    """``tools`` as the listener lists them: the proxy's own injector, then its tool."""
    listed = copy.deepcopy(tools)
    for tool in listed:
        _inject_goal_params(tool, mode)
    return [*listed, {"name": "baton_annotate"}]


@pytest.mark.parametrize("mode", ["required", "optional"])
def test_what_the_proxy_adds_is_never_a_difference(mode):
    assert check.closest(ODD_TOOLS, _as_listed(ODD_TOOLS, mode), None) == ([], [], [])


def test_a_changed_missing_or_added_tool_is_named():
    listed = _as_listed(ODD_TOOLS)
    listed[4]["description"] = "other"
    listed = [t for t in listed if t["name"] != "no_args"] + [{"name": "extra"}]
    assert check.closest(ODD_TOOLS, listed, None) == (["plain"], ["no_args"], ["extra"])


OVERLAY = {
    "actions": [
        {"target": {"tool": "plain"}, "replace": "new"},
        {"target": {"tool": "no_args"}, "remove": True},
    ]
}


def test_the_overlays_changes_are_expected_and_nothing_else():
    overlay = parse_overlay(OVERLAY)
    listed = _as_listed(overlay.apply_to_tools(ODD_TOOLS))
    assert check.closest(ODD_TOOLS, listed, overlay) == ([], [], [])
    one_more_gone = [t for t in listed if t["name"] != "no_schema"]
    assert check.closest(ODD_TOOLS, one_more_gone, overlay) == (
        [],
        ["no_schema"],
        [],
    )


@pytest.mark.parametrize(
    "running, overlay_file, code",
    [("none", False, 0), ("sha256:other", False, 1), ("none", True, 1)],
)
def test_the_round_must_be_the_one_the_check_expects(
    monkeypatch, tmp_path: Path, running, overlay_file, code
):
    lists = {"server": (ODD_TOOLS, None), "listener": (_as_listed(ODD_TOOLS), running)}
    monkeypatch.setattr(check, "list_tools", lambda url, headers: lists[url])
    argv = ["--listener", "listener", "--url", "server"]
    if overlay_file:
        path = tmp_path / "o.json"
        path.write_text(json.dumps({"actions": []}))
        argv += ["--overlay", str(path)]
    assert check.main(argv) == code


def test_a_failure_is_named_by_type_and_never_quotes_the_url(capsys):
    assert check.main(["--listener", "l", "--url", "mcp.example.com/mcp?key=s3cret"]) == 2
    err = capsys.readouterr().err
    assert "ValueError" in err and "s3cret" not in err


def test_a_header_without_a_colon_is_refused():
    with pytest.raises(SystemExit):
        check.main(["--listener", "l", "--url", "u", "--header", "x-api-key"])

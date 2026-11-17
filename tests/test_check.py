from __future__ import annotations

import pytest

from baton_proxy import check


def _tool(name: str, description: str = "d", goal: bool = False) -> dict:
    props: dict = {"x": {"type": "string"}}
    required = ["x"]
    if goal:
        props["user_goal"] = {"type": "string"}
        required.append("user_goal")
    schema = {"type": "object", "properties": props, "required": required}
    return {"name": name, "description": description, "inputSchema": schema}


def test_the_proxys_own_param_and_tool_are_not_a_difference():
    direct = [_tool("a")]
    proxied = [_tool("a", goal=True), {"name": "baton_annotate"}]
    assert check.compare(direct, proxied) == ([], [], [])


def test_a_changed_hidden_or_added_tool_is_named():
    direct = [_tool("a"), _tool("b")]
    proxied = [_tool("a", "other", goal=True), _tool("c", goal=True)]
    assert check.compare(direct, proxied) == (["a"], ["b"], ["c"])


@pytest.mark.parametrize("overlay, code", [("none", 1), ("sha256:abc", 0)])
def test_a_difference_fails_only_without_an_overlay(monkeypatch, capsys, overlay, code):
    lists = {"server": ([_tool("a")], None), "listener": ([_tool("a", "x")], overlay)}
    monkeypatch.setattr(check, "list_tools", lambda url, headers: lists[url])
    assert check.main(["--listener", "listener", "--url", "server"]) == code
    assert "changed: a" in capsys.readouterr().out


def test_a_header_without_a_colon_is_refused():
    with pytest.raises(SystemExit):
        check.main(["--listener", "l", "--url", "u", "--header", "x-api-key"])

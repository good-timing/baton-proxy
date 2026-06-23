"""Tests for pinned scan task plans."""

from __future__ import annotations

from baton_proxy.scan_tasks import PINNED_TASK_PLANS, pinned_plan_for


def test_memory_matches_regardless_of_launch_form() -> None:
    # The pin matches on package substring, so any launch form resolves.
    for cmd in (
        "npx -y @modelcontextprotocol/server-memory",
        "npx @modelcontextprotocol/server-memory",
        "@modelcontextprotocol/server-memory",
    ):
        assert pinned_plan_for(cmd) == PINNED_TASK_PLANS["@modelcontextprotocol/server-memory"]


def test_unknown_server_falls_through_to_none() -> None:
    assert pinned_plan_for("npx -y @vendor/some-other-mcp-server") is None
    assert pinned_plan_for("") is None


def test_pinned_plan_is_nonempty_prompt() -> None:
    plan = pinned_plan_for("npx -y @modelcontextprotocol/server-memory")
    assert plan is not None
    # A driver prompt, not a tool list — should read as an instruction.
    assert "knowledge graph" in plan and "confirm" in plan

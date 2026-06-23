"""Pinned task plans for ``scan`` — fixed driver prompts for known servers.

``scan`` drives a headless agent through a wrapped server to surface friction.
For an arbitrary server the task plan is LLM-generated from ``tools/list``,
which varies run-to-run — fine for a one-off scan of the user's own server,
but it makes the *demo* finding flicker. For the DEFAULT demo server(s) behind
the homepage CTA we pin a fixed plan so the surfaced friction is reproducible.
See design-notes ``activation_cta_scan_and_hosted.md`` open questions #2/#8:
render-side anchoring guarantees a floor, the *pinned plan* is what buys
run-to-run determinism.

Each plan is a single driver prompt; the agent figures out the tool calls. The
prompt is engineered to (a) walk the agent into the server's known friction and
(b) verify its own work, so the silent-success class surfaces and gets annotated
rather than passing unnoticed.
"""

from __future__ import annotations

# Memory's friction is a silent success: ``create_relations`` persists a
# relation referencing entities that don't exist instead of erroring — the
# "200 instead of an error" class. Framing the two people as *already in the
# graph* nudges the agent to call ``create_relations`` first (the entities
# aren't actually there), and the read-back-to-confirm step forces it to
# discover the dangling relation and annotate it.
#
# Validated 2026-06-22: 3/3 runs against `npx -y @modelcontextprotocol/
# server-memory` (graph reset each run) surfaced a reactive friction signal
# (dead_end / failure / feature_gap — all the same silent-success trap) with a
# stable headline count of 1 friction point.
PINNED_TASK_PLANS: dict[str, str] = {
    "@modelcontextprotocol/server-memory": (
        "A user wants to link two people who are already in their knowledge "
        "graph: record that 'Ada Lovelace' worked_with 'Charles Babbage'. "
        "Create that relationship, then read the graph back to confirm it saved "
        "correctly, and report whether the stored data is consistent."
    ),
}


def pinned_plan_for(server_command: str) -> str | None:
    """Return a pinned driver prompt if ``server_command`` references a known
    default server, else ``None`` (caller falls back to LLM task generation).

    Matches on substring so the launch command form is irrelevant — e.g.
    ``npx -y @modelcontextprotocol/server-memory`` matches the package key.
    """
    if not server_command:
        return None
    for key, plan in PINNED_TASK_PLANS.items():
        if key in server_command:
            return plan
    return None

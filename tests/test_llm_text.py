"""Regression tests for the proxy's shared LLM-facing text templates.

Mirrors the test surface of the SDK's ``tests/integrations/test_llm_text.py``
so the two modules stay coherent. Load-bearing properties:

- Rendered instructions MUST stay under the safety cap (Claude Code
  truncates ``InitializeResult.instructions`` at ~2087 chars; we cap at
  1500 to leave headroom for the upstream server's own pre-existing
  instructions that the proxy's suffix is appended to).
- ``build_instructions_suffix`` MUST raise if rendered output would
  exceed the cap, rather than silently returning a string Claude Code
  will truncate mid-sentence.
- The AFTER/IF MUST/REQUIRED behavioral framing is load-bearing —
  milder framing under-populates fields, and the IF clause is the
  feature_gap mechanical trigger surfaced by the 2026-06-12 live-Claude
  proxy test on Notion MCP. The BEFORE trigger was DROPPED 2026-09-01
  (D7) and its absence is now itself pinned, below: intent rides the
  injected params, and the suffix asking for it again taught the agent
  otherwise.
- All 8 canonical signal_type enum values must appear in the rendered
  text; downstream taxonomies (priority mapping in the report
  synthesizer, the Console channel adapter) key off these strings.
- The "annotation doesn't replace answering" guardrail is load-bearing —
  without it the agent treats the annotation as proxy-satisfaction and
  stops answering the user.
- The three mechanical triggers for feature_gap (workaround used, no
  tool matched, server can't do it) are the structural fix for the
  error-vs-gap asymmetry; their wording must remain checkable.
"""

from __future__ import annotations

import re

import pytest

from baton_proxy._llm_text import (
    _INSTRUCTIONS_LENGTH_CAP,
    SIGNAL_TYPES,
    build_annotation_tool_description,
    build_instructions_suffix,
    build_overall_task_param_description,
)

# =============================================================================
# Cap discipline
# =============================================================================


def test_instructions_under_truncation_cap() -> None:
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    assert len(rendered) <= _INSTRUCTIONS_LENGTH_CAP


def test_instructions_under_cap_with_long_annotation_tool_name() -> None:
    """A reasonably-long vendor-prefixed annotate name (per the
    `{vendor_id}_annotate` convention) must still fit under the cap."""
    rendered = build_instructions_suffix(
        annotation_tool_name="very_long_vendor_display_name_annotate",
    )
    assert len(rendered) <= _INSTRUCTIONS_LENGTH_CAP


def test_instructions_raises_when_tool_name_exceeds_cap() -> None:
    """build_instructions_suffix raises ValueError if rendered length
    exceeds the cap, rather than silently returning a string Claude Code
    will truncate mid-sentence."""
    with pytest.raises(ValueError, match="exceeds the"):
        build_instructions_suffix(annotation_tool_name="A" * 1500)


# =============================================================================
# Behavioral framing
# =============================================================================


def test_instructions_carry_must_required_framing() -> None:
    """The AFTER/IF MUST/REQUIRED framing drives annotation population —
    milder phrasing empirically under-populates fields. Don't drop it
    accidentally.

    BEFORE is deliberately absent; see the test below."""
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    assert "AFTER" in rendered
    assert "MUST" in rendered
    assert "REQUIRED" in rendered


def test_the_pre_call_request_is_gated_on_proactive_mode() -> None:
    """The whole of D7's separation, and it is a KNOB rather than a deletion —
    leg-for-leg the same choice baton-sdk's ``build_server_instructions``
    makes, so one agent meeting both producers reads one rule.

    Why the paragraph is worth turning off: it names the same three fields the
    injected params carry on every call, so it teaches the agent that intent is
    the annotation tool's job. Measured 2026-09-01 — three real sessions, four
    annotations, every one pre-call with no signal_type, and zero friction
    signals filed. Why it is not deleted: agent-authored proactives supply
    ~54% of turn boundaries, and the proxy fronts servers its operator does not
    own."""
    on = build_instructions_suffix(annotation_tool_name="baton_annotate", proactive_mode="on")
    off = build_instructions_suffix(annotation_tool_name="baton_annotate", proactive_mode="off")

    assert "BEFORE" in on
    assert "BEFORE" not in off
    # Named individually: a suffix that still asks for the three fields under
    # some other heading is the same thing with different spelling.
    for field in ("expected_result", "overall_task"):
        assert field in on
        assert field not in off
    # The head moves with it — "record what the user is trying to do" is a
    # promise the reactive-only mode does not keep.
    assert "record what the user is trying to do" in on
    assert "report when a tool call on this server goes wrong" in off


def test_the_reactive_clauses_are_identical_in_both_modes() -> None:
    """The half that must never be gated. AFTER and IF are the friction
    signal — the product — and they have no param analogue, so nothing about
    the proactive choice may touch them."""
    on = build_instructions_suffix(annotation_tool_name="baton_annotate", proactive_mode="on")
    off = build_instructions_suffix(annotation_tool_name="baton_annotate", proactive_mode="off")
    for clause in ("AFTER any tool", "IF a tool response", "does NOT replace answering"):
        assert clause in on and clause in off
    # And what `off` costs is exactly the paragraph, not a rewrite around it:
    # the reactive tail is byte-identical.
    tail = "AFTER any tool"
    assert on[on.index(tail) :] == off[off.index(tail) :]


def test_turning_proactive_off_is_what_buys_the_truncation_headroom() -> None:
    """The cost side of the knob, and the reason someone would flip it on a
    large server. The proxy APPENDS its suffix, and Claude Code truncates
    `instructions` at ~2,087 chars — workfront's rendered value measured 4,408
    on 2026-09-01 — so on an enterprise server it is our framing, at the end,
    that is silently cut. `off` takes ~280 chars off the suffix."""
    on = build_instructions_suffix(annotation_tool_name="baton_annotate", proactive_mode="on")
    off = build_instructions_suffix(annotation_tool_name="baton_annotate", proactive_mode="off")
    assert len(on) - len(off) > 250, (
        f"the paragraph stopped costing anything: {len(on)} vs {len(off)}"
    )
    assert len(on) <= _INSTRUCTIONS_LENGTH_CAP


def test_the_default_is_todays_behaviour() -> None:
    """The proxy defaults `on` where the SDK defaults `off`, and the asymmetry
    is the point: the SDK wraps a server its vendor owns, the proxy fronts
    servers its operator does not, so a default that changed live capture on
    the next restart would make upgrading a decision."""
    from baton_proxy.config import DEFAULT_PROACTIVE_MODE

    assert DEFAULT_PROACTIVE_MODE == "on"
    assert build_instructions_suffix("baton_annotate") == build_instructions_suffix(
        "baton_annotate", DEFAULT_PROACTIVE_MODE
    )


def test_instructions_carry_full_signal_type_enum() -> None:
    """All 8 canonical signal_type values must appear in the rendered
    text. Downstream priority mapping (report synthesizer, Console
    channel adapter) keys off these strings; a missing value would
    silently break escalation routing."""
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    for value in SIGNAL_TYPES:
        assert value in rendered, f"signal_type value {value!r} missing"


def test_instructions_carry_dont_replace_answering_guardrail() -> None:
    """Without the 'doesn't replace answering' clause the agent treats
    the annotation as proxy-satisfaction and stops answering the user —
    documented failure mode from the SDK's iteration history."""
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    assert "does NOT replace answering" in rendered


# =============================================================================
# Mechanical triggers — the structural fix for the error-vs-gap asymmetry
# =============================================================================


def test_instructions_carry_error_mechanical_trigger() -> None:
    """Errors must remain a mechanical trigger — the strongest clause
    in the original template, and the one Dave's 2026-06-12 live-Claude
    session showed actually firing in practice."""
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    # Phrasing is in the AFTER block: "errors, times out, ...".
    assert "errors" in rendered.lower()


def test_instructions_carry_feature_gap_mechanical_triggers() -> None:
    """Three observable conditions an agent can check against its own
    behavior, on par with 'the call returned an error'. Surfaced by the
    proxy's 2026-06-12 live-Claude test on Notion MCP: the agent
    successfully routed around a missing capability (Neuralink-style
    push-to-user) and never filed a feature_gap because the original
    template lacked a mechanical trigger for the workaround case.
    """
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    # The IF block carries the three concrete gap triggers. Check each.
    assert "lacks a structured field" in rendered
    assert "workaround" in rendered
    assert "asked for something this server can't do" in rendered
    # All three roll up to signal_type='feature_gap'.
    assert "signal_type='feature_gap'" in rendered


# =============================================================================
# Annotation tool description (the field reference)
# =============================================================================


def test_description_carries_all_8_signal_types() -> None:
    """The annotation tool's inputSchema enum and its description must
    reference the same 8-value enum. A drift between them would let
    Claude pass a value the schema rejects (or vice versa)."""
    description = build_annotation_tool_description()
    for value in SIGNAL_TYPES:
        assert value in description, f"signal_type value {value!r} missing"


def test_description_carries_field_reference() -> None:
    """Description is the place loaded at *call* time, so it owns the
    field-level reference. Each field the inputSchema accepts must have
    a one-line entry the agent can consult while filling in the call."""
    description = build_annotation_tool_description()
    for field in (
        "user_goal:",
        "expected_result:",
        "overall_task:",
        "signal_type:",
        "suggested_improvement:",
        "context:",
    ):
        assert field in description, f"field {field!r} missing from description"


def test_description_does_not_duplicate_triggers() -> None:
    """Triggers belong in instructions (loaded once at session init, drives
    the first proactive annotation). Description is read at call time —
    too late to drive 'should I call this at all'. Don't duplicate the
    behavioral framing; it's just per-call context overhead."""
    description = build_annotation_tool_description()
    # The description shouldn't carry the BEFORE/AFTER/IF triggers.
    assert "BEFORE" not in description
    assert "AFTER" not in description
    # It also shouldn't restate the MUST-call conditions.
    assert "MUST call" not in description


# ---------------------------------------------------------------------------
# The task label's wording is a result, not a style choice.
#
# It comes out of a scored experiment (baton-internal `spikes/overall_task_a5/`,
# 40 paired live-agent sessions): one wording misses task boundaries the user
# does not announce, the other splits single tasks, and the trade was decided
# in favour of the text below. Rewording it re-runs that experiment on live
# traffic without scoring it.
#
# All three producers must ask in the SAME words, and this is not hypothetical:
# the revert of the rejected wording landed in baton-sdk and never reached
# baton-ts, which shipped the losing arm undetected because nothing pinned it.
# Each producer now pins the literal in its own suite, beside this reasoning —
# baton-sdk's `_OVERALL_TASK_PARAM_DESCRIPTION` and baton-ts's
# `llmText.test.ts`. Deliberately NOT a cross-repo read: this suite ships in
# the Try Kit, where a prospect's clone has no sibling checkouts, and a test
# that skips for them would break SECURITY.md §8's skip count — a number the
# document offers a reviewer as checkable.
#
# What that costs, stated plainly: three local pins cannot notice the three
# drifting apart on their own. Each makes a change deliberate and visible in
# review, which is what was missing when ts drifted; it is not a mechanical
# guarantee of agreement.
# ---------------------------------------------------------------------------

SELECTED_WORDING = (
    "OPTIONAL. Short stable label for the broader task this call serves "
    "(e.g. 'prepare campaign approval'). REPEAT the exact same string on "
    "every call serving the same task; change it only when the user starts "
    "a different task."
)


def test_the_task_label_wording_is_the_one_the_experiment_selected() -> None:
    assert build_overall_task_param_description() == SELECTED_WORDING


def test_the_wording_does_not_carry_the_rejected_candidate() -> None:
    """Named phrases, so a re-introduction fails by name rather than by diff.

    Both are load-bearing in the rejected text: they scoped the label to the
    current turn rather than the task, which produced the A -> B -> A relabel a
    merge-only consumer resolves as three tasks instead of one.
    """
    text = build_overall_task_param_description()
    assert "not the overall theme" not in text
    assert "working on right now" not in text


def test_the_wording_keeps_the_clause_that_makes_it_groupable() -> None:
    """Grouping is by exact string match, so the repeat clause IS the
    mechanism: without it 80% of adjacent same-task calls reword and every task
    shatters, whichever granularity wording is chosen."""
    assert "REPEAT the exact same string" in build_overall_task_param_description()


# The retired agent-facing names. All three remain WIRE keys, so they appear
# legitimately across the codebase — but never in text an agent reads, where
# they would name a param the schema no longer accepts.
RETIRED_AGENT_FACING_NAMES = ("intent", "expected_outcome", "workflow")


def test_no_agent_facing_text_still_asks_for_a_retired_param_name() -> None:
    """Presence tests are not enough, and the sibling SDK proved it.

    Its lead line still told agents to populate "intent + expected_outcome +
    workflow" long after those params were renamed, with every field-reference
    test green — because each only asked whether the CURRENT names appear
    somewhere, never whether a retired one still does. An agent that follows
    such a line sends params the handler drops: the call succeeds, the
    annotation emits, and the goal text is simply absent.

    Matched only where a param is REFERENCED — `name:`, `name (REQUIRED`, or
    inside the `a + b + c` populate list. A bare word-boundary search
    over-detects and would fail on "you satisfied the user's intent via a
    workaround", which is prose and correct.
    """
    surfaces = {
        "instructions suffix": build_instructions_suffix("baton_annotate"),
        "annotation tool description": build_annotation_tool_description(),
    }
    for where, text in surfaces.items():
        for retired in RETIRED_AGENT_FACING_NAMES:
            referenced = (
                rf"\b{retired}(?=:)|\b{retired} \(REQUIRED"
                rf"|(?<=\+ ){retired}\b|\b{retired}(?= \+)"
            )
            assert not re.search(referenced, text), (
                f"{where} still names the retired param {retired!r}; an agent will send "
                f"it and the value will be dropped"
            )

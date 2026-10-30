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
- Reports only (the default), the suffix is one pointer sentence and the
  report rules live in the tool's description; both texts are pinned whole.
- The agent describes the problem and the collector groups it (SPEC
  §11.5.5): no text offers ``signal_type`` or a category to pick.
- The "annotation doesn't replace answering" guardrail is load-bearing —
  without it the agent treats the annotation as proxy-satisfaction and
  stops answering the user.
- The triggers (an error, no tool or field covering the request, a
  workaround) are states the agent can check against its own behavior.
"""

from __future__ import annotations

import re

import pytest

from baton_proxy._llm_text import (
    _CLAUDE_CODE_TRUNCATION_CAP,
    _EVENT_FILE_LINE,
    _INSTRUCTIONS_LENGTH_CAP,
    build_annotation_tool_description,
    build_instructions_suffix,
    build_user_goal_param_description,
    required_param_names,
)
from baton_proxy.config import DEFAULT_EVENT_SINK
from baton_proxy.sinks import find_file_sink_path

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
        annotation_tool_name="very_long_vendor_display_name_annotate"
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


def test_the_report_rules_live_in_the_tool_description() -> None:
    """Each rule is written once, in the tool's description; the suffix only
    points at it."""
    suffix = build_instructions_suffix("baton_annotate")
    for rule in ("AFTER", "MUST", "once per user request", "wrapped in"):
        assert rule not in suffix, rule
    description = build_annotation_tool_description()
    assert "right after a call errors, times out" in description
    assert "no tool or field covers what they asked for" in description
    assert "or you had to work around one" in description
    assert "and still answer the user" in description


# ``failure`` is left out: the text uses it as an ordinary word.
_OLD_KINDS = (
    "retry_loop",
    "dead_end",
    "parameter_confusion",
    "slow_performance",
    "abandonment",
    "feature_gap",
)


def test_no_agent_facing_text_offers_a_category_to_pick() -> None:
    for text in (
        build_instructions_suffix("baton_annotate"),
        build_annotation_tool_description(),
    ):
        assert "signal_type" not in text
        for kind in _OLD_KINDS:
            assert kind not in text


def test_no_tool_is_asked_for_as_the_word_none() -> None:
    """Shown ``""`` or told "empty string", agents send the quote characters."""
    instructions = build_instructions_suffix("baton_annotate")
    description = build_annotation_tool_description()

    assert "or none if no tool exists" in description
    for text in (instructions, description):
        assert '""' not in text
        assert "mpty string" not in text


# =============================================================================
# Annotation tool description (the field reference)
# =============================================================================


def test_description_carries_field_reference() -> None:
    """Description is the place loaded at *call* time, so it owns the
    field-level reference. Each field the inputSchema accepts must have
    a one-line entry the agent can consult while filling in the call."""
    description = build_annotation_tool_description()
    for field in (
        "user_goal:",
        "what_happened:",
        "tool_name:",
        "suggested_improvement:",
        "context:",
    ):
        assert field in description, f"field {field!r} missing from description"


def test_a_reports_only_tool_does_not_describe_a_note() -> None:
    """It refuses a note, and a field that says how to send one invites it."""
    description = build_annotation_tool_description()
    assert "proactive" not in description


# The SDK's scored reports-only text, with "on this server" for the vendor name:
# rewording it needs a new scored run.
REPORTS_ONLY_SUFFIX = (
    " Use `baton_annotate` to report a tool call on this server that went wrong, or "
    "a request no tool covers; its description says when and how.\n\nWhen you hand "
    "work to a subagent that may call this server's tools, tell it the number of the "
    "user's current message in this conversation, and that it must start user_goal "
    "with that number on every call to this server's tools. This applies only to "
    "this server's tools."
)
REPORTS_ONLY_TOOL_DESCRIPTION = (
    "Report a tool call on this server that went wrong, so the team behind this "
    "server can fix it. Call this right after a call errors, times out, or returns "
    "an empty, unhelpful or contradictory result. Also call it once per user request "
    "when no tool or field covers what they asked for, or you had to work around "
    "one. Do not call it before a tool call or for normal successful work.\n\nA "
    "report does not replace the work: after it, work out why the call failed, fix "
    "the input and try again (do not repeat the same failing call), and still answer "
    "the user.\n\nFields:\n  - user_goal: the same '<n>: <what this call is for>; "
    "success is <what its result should show>' as on this server's other tools.\n  "
    "- what_happened: REQUIRED. 1-2 plain sentences in your own words: what you "
    "asked for, what came back, and why it was unusable. Not a category.\n  - "
    "tool_name: REQUIRED. The tool on this server that went wrong, or none if no tool "
    "exists for the request.\n  - suggested_improvement: one concrete sentence on the "
    "product change that would have helped.\n  - context: supplementary info not "
    "covered above. Common keys: plan, alternatives_considered, likely_cause, "
    "user_impact, error_class, downstream_blocked, confidence_in_intent. When no tool "
    "covers the request also missing_capability_field and requested_capability."
)


def test_reports_only_text_is_the_scored_text() -> None:
    assert build_instructions_suffix("baton_annotate") == REPORTS_ONLY_SUFFIX
    assert build_annotation_tool_description() == REPORTS_ONLY_TOOL_DESCRIPTION


# ---------------------------------------------------------------------------
# The user_goal wording is a result, not a style choice: it was scored on live
# agents. All three producers must ask in the SAME words, so each pins the
# literal in its own suite — baton-sdk's `_USER_GOAL_PARAM_BODY` and baton-ts's
# `llmText.test.ts`. Deliberately NOT a cross-repo read: this suite ships in
# the Try Kit, where a prospect's clone has no sibling checkouts, and a test
# that skips for them would break SECURITY.md §8's skip count — a number the
# document offers a reviewer as checkable.
#
# Three local pins cannot notice the three drifting apart on their own. Each
# makes a change deliberate and visible in review; it is not a mechanical
# guarantee of agreement.
# ---------------------------------------------------------------------------

USER_GOAL_BODY = (
    "Write it as '<n>: <what this call is for>; success is <what its result "
    "should show>'. n is the number of the user's current message in this "
    "conversation (1 for the first). Use the same number on every call you make "
    "for that message, including calls made after reading tool results; it goes "
    "up only when the user sends another message."
)
SUBAGENT_SENTENCE = (
    "When you hand work to a subagent that may call this server's tools, tell it "
    "the number of the user's current message in this conversation, and that it "
    "must start user_goal with that number on every call to this server's "
    "tools. This applies only to this server's tools."
)


def test_the_user_goal_wording_is_the_one_baton_sdk_ships() -> None:
    assert build_user_goal_param_description() == "OPTIONAL. " + USER_GOAL_BODY
    assert build_user_goal_param_description(intent_param_mode="required") == (
        "REQUIRED. " + USER_GOAL_BODY
    )


def test_required_mode_escalates_user_goal_only() -> None:
    assert required_param_names(intent_param_mode="required") == ("user_goal",)
    assert required_param_names(intent_param_mode="optional") == ()


def test_the_subagent_sentence_follows_the_pointer_when_params_ride() -> None:
    """A main agent that never loads the tool schemas reads only this."""
    rendered = build_instructions_suffix("baton_annotate")
    assert rendered.endswith("when and how.\n\n" + SUBAGENT_SENTENCE)


def test_reports_off_leaves_only_the_subagent_sentence_and_the_event_file_line() -> None:
    assert build_instructions_suffix("baton_annotate", report_mode="off") == (
        "\n\n" + SUBAGENT_SENTENCE
    )
    with_file = build_instructions_suffix(
        "baton_annotate", event_file_path="/tmp/e.jsonl", report_mode="off"
    )
    assert with_file.startswith("\n\n" + SUBAGENT_SENTENCE)
    assert "/tmp/e.jsonl" in with_file
    assert "baton_annotate" not in with_file


def test_the_event_file_line_stays_after_the_subagent_sentence() -> None:
    """Truncation eats the end, so the convenience line must be what it takes."""
    rendered = build_instructions_suffix("baton_annotate", event_file_path="/tmp/e.jsonl")
    assert rendered.index(SUBAGENT_SENTENCE) < rendered.index("/tmp/e.jsonl")


# The retired agent-facing names. They still appear across the codebase, as
# wire keys or as names a stale client may send — but never in text an agent
# reads, where they would name a param the schema no longer accepts.
RETIRED_AGENT_FACING_NAMES = (
    "intent",
    "expected_outcome",
    "workflow",
    "expected_result",
    "overall_task",
)


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


# =============================================================================
# The event-file line — the retired report tool's replacement.
#
# `baton_session_report` rendered a fixed template over the local JSONL and
# classified nothing; it was deleted 2026-09-27 and the answer is the agent
# reading that file itself. Which only works if the agent is TOLD where the
# file is.
#
# What these pin is the one property the rest of this module's cap discipline
# does not cover: this is the only piece of the suffix whose length the proxy
# does not choose. The path is the operator's, so the line must be DROPPED when
# it does not fit rather than raised on — a deep directory is not a
# misconfiguration the way a 1,500-character tool name is.
# =============================================================================

# DERIVED, not typed. Seven docstrings here reason about "the 22-char default
# sink path", and a hardcoded copy keeps every one of them green against a path
# the product no longer uses — while the real default silently stops fitting.
# `tests/test_injection.py` already derives it this way; these two modules
# disagreed until 2026-09-27.
_DEFAULT_SINK_PATH = find_file_sink_path(DEFAULT_EVENT_SINK)
assert _DEFAULT_SINK_PATH, f"the default sink has no file leg: {DEFAULT_EVENT_SINK!r}"


def test_the_event_file_line_names_the_default_path() -> None:
    rendered = build_instructions_suffix("baton_annotate", event_file_path=_DEFAULT_SINK_PATH)
    assert _DEFAULT_SINK_PATH in rendered
    assert len(rendered) <= _CLAUDE_CODE_TRUNCATION_CAP


def test_no_path_renders_no_line_and_no_stray_paragraph() -> None:
    """Every vendor-production wrap: an http-only sink has no local file, so
    there is nothing to name. The suffix must be byte-identical to what it was
    before this parameter existed — an empty paragraph would spend the budget
    saying nothing."""
    without = build_instructions_suffix("baton_annotate")
    assert build_instructions_suffix("baton_annotate", event_file_path=None) == without
    assert build_instructions_suffix("baton_annotate", event_file_path="") == without
    assert "captured as JSONL" not in without
    assert "\n\n\n" not in without


def test_the_line_sits_after_the_report_text() -> None:
    """The proxy APPENDS its suffix to the upstream server's own `instructions`,
    and Claude Code truncates the result from the END at ~2,087. Last means the
    convenience line is what a cut takes, never the report pointer."""
    rendered = build_instructions_suffix("baton_annotate", event_file_path=_DEFAULT_SINK_PATH)
    assert rendered.index(_DEFAULT_SINK_PATH) > rendered.index("its description says when and how")


def test_the_line_does_not_run_into_the_clause_before_it() -> None:
    """The subagent clause ends with no break of its own, so the line's
    separator has to lead."""
    rendered = build_instructions_suffix("baton_annotate", event_file_path=_DEFAULT_SINK_PATH)
    assert "this server's tools.\n\nTool calls" in rendered, repr(rendered[-260:])
    assert "tools.Tool" not in rendered


def test_the_line_does_not_claim_the_file_holds_only_this_session() -> None:
    """⚠ The claim the first draft got wrong. It said "This session's tool calls
    and results", and the file it names holds no such thing: the default sink is
    ONE fixed path, so it accumulates across restarts and across every server
    wrapped with the defaults, and the try kit's file grows for as long as the
    wrap is installed.

    The retired tool could say "this session" because it filtered on
    `session_id`. An agent cannot: `session_id` is a per-process uuid4 that never
    reaches the model. An agent told the file is this session's would report an
    earlier session's failures, and another server's arguments, as this one's.
    """
    rendered = build_instructions_suffix("baton_annotate", event_file_path=_DEFAULT_SINK_PATH)
    assert "This session's" not in rendered
    assert "this session" not in rendered.lower()
    # What it says instead has to be the true shape, not merely a hedge. ⚠ The
    # SECOND draft said "appended across sessions", which was true and still
    # understated it: other wrapped SERVERS append to the same path too, so an
    # agent reading the tail can pick up another server's failure. Both halves
    # are asserted, and so is the filter that makes them actionable — every
    # event carries `session_id` and `vendor_id`, so the two things the file
    # mixes are the two things the agent can separate.
    assert "Other sessions and other wrapped servers" in rendered
    assert "session_id" in rendered and "vendor_id" in rendered
    assert "newest last" in rendered


def test_a_path_too_long_to_fit_is_dropped_not_raised() -> None:
    """Raising here would let a deep directory take down a wrap that is otherwise
    correct, on the handshake. A filesystem path is the operator's, not ours, so
    it is not a misconfiguration the way a 500-character tool name is. What must
    survive the drop is everything else: the suffix comes back WHOLE, not
    truncated to make room.
    """
    long_path = "/" + "a" * 1600 + "/events.jsonl"
    rendered = build_instructions_suffix("baton_annotate", event_file_path=long_path)
    assert long_path not in rendered
    assert rendered == build_instructions_suffix("baton_annotate")


def test_a_deep_path_still_fits() -> None:
    """The control for the test above: the drop must be caused by the BUDGET and
    not by the path, so a merely DEEP path — the shape a real operator has — must
    still render. Without this, a bug that dropped the line unconditionally would
    pass the drop test and be invisible."""
    long_path = "/Users/someone/Library/Application Support/baton/deeply/nested/events.jsonl"
    rendered = build_instructions_suffix("baton_annotate", event_file_path=long_path)
    assert long_path in rendered


def test_a_path_with_braces_does_not_raise_through_the_formatter() -> None:
    """The path must never sit inside the suffix's multi-field template.

    That template is formatted with `annotation_tool_name` and `again`, so a `{`
    or `}` in the operator's sink path reaches the formatter as an unknown field
    and raises KeyError — at injection time, on the handshake, over a
    punctuation character in a directory name. Verified by mutation: moving the
    line into that template reds this test with `KeyError: 'b'`. What does NOT
    matter, and what this deliberately does not claim, is `.replace` vs
    `.format` on the line by itself — `.format` does not re-process what it
    substitutes in, so both are safe there.
    """
    for path in ("/tmp/a{b}c.jsonl", "/tmp/{}.jsonl", "/tmp/only{.jsonl"):
        rendered = build_instructions_suffix("baton_annotate", event_file_path=path)
        assert path in rendered, path


def test_the_line_leaves_room_for_a_real_operators_path() -> None:
    """The budget, derived rather than restated, so a reword reds here rather than
    in the drop test — which would keep passing while the line stopped rendering
    for everybody.

    ⚠ What is asserted is a PATH length, not a line length. Writing this line to a
    character count is what produced two false claims about the file it names, so
    what has to hold is that a real path fits — not that the prose is short. 256
    is `NAME_MAX` territory and comfortably past any sink path an operator types.
    """
    fixed = len(_EVENT_FILE_LINE.replace("{event_file_path}", ""))
    room = _CLAUDE_CODE_TRUNCATION_CAP - len(build_instructions_suffix("baton_annotate")) - fixed
    assert room >= 256, (
        f"only {room} chars of path fit (the line is {fixed}); a real operator's sink "
        "path would be dropped"
    )


def test_the_base_keeps_the_reserve_and_the_event_line_is_exempt() -> None:
    """⚠ The two-tier cap, which is the one thing here easy to collapse back by
    accident.

    The 1,500 cap's job is that a server with instructions of its OWN does not
    have our NORMATIVE text cut: it holds 587 back under the client's ~2,087 for
    the upstream string. That reserve belongs to the base — the report text —
    and the base must stay inside it.

    The event-file line is exempt, and being LAST is what earns it: truncation
    takes the end, so this line's bytes can only ever displace this line.
    """
    base = build_instructions_suffix("baton_annotate")
    assert len(base) <= _INSTRUCTIONS_LENGTH_CAP
    reserve = _CLAUDE_CODE_TRUNCATION_CAP - len(base)
    assert reserve >= 500, (
        f"only {reserve} chars left for the upstream server's own instructions; "
        "the BASE has grown into its reserve"
    )
    whole = build_instructions_suffix("baton_annotate", event_file_path=_DEFAULT_SINK_PATH)
    assert len(whole) <= _CLAUDE_CODE_TRUNCATION_CAP


def test_the_exemption_is_real_and_not_just_unexercised() -> None:
    """The control for the test above. If the line happened to fit inside 1,500
    anyway, every assertion there would pass while the exemption did nothing —
    and a later reword back to the single cap would be invisible. A long tool
    name fills the base close to 1,500, so the whole suffix must actually EXCEED
    1,500 to prove the line is being measured against the client's cap instead."""
    tool_name = "t" * 1050
    base = build_instructions_suffix(tool_name)
    assert len(base) <= _INSTRUCTIONS_LENGTH_CAP
    whole = build_instructions_suffix(tool_name, event_file_path=_DEFAULT_SINK_PATH)
    assert _DEFAULT_SINK_PATH in whole
    assert len(whole) > _INSTRUCTIONS_LENGTH_CAP, (
        f"the whole suffix is {len(whole)}, inside the 1,500 base cap — the "
        "two-tier split is not being exercised by this test"
    )


def test_the_try_kits_own_events_path_renders() -> None:
    """⚠ Every other budget test here uses the 22-char default path, so all of
    them stay green while the line silently stops rendering for the TRIAL — the
    surface a prospect actually meets, and the one that used to get the retired
    tool.

    The kit's path is a checkout-relative `try/events.jsonl`, so its length is
    the prospect's directory layout, not ours. A deleted try-kit test used to pin
    that the kit really got the tool it was promised; nothing replaced it until
    this.
    """
    from baton_proxy.sinks import find_file_sink_path

    kit_path = "/Users/someone/code/a-deeply-named-evaluation-checkout/try/events.jsonl"
    assert find_file_sink_path(f"stderr:,file://{kit_path}") == kit_path
    rendered = build_instructions_suffix("baton_annotate", event_file_path=kit_path)
    assert kit_path in rendered

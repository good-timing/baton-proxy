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
- The BEFORE/AFTER/IF MUST/REQUIRED behavioral framing is load-bearing
  — milder framing under-populates fields, and the IF clause is the
  feature_gap mechanical trigger surfaced by the 2026-06-12 live-Claude
  proxy test on Notion MCP.
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

import pytest

from baton_proxy._llm_text import (
    _INSTRUCTIONS_LENGTH_CAP,
    SIGNAL_TYPES,
    build_annotation_tool_description,
    build_instructions_suffix,
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
    """The BEFORE/AFTER MUST/REQUIRED framing drives annotation
    population — milder phrasing empirically under-populates fields.
    Don't drop it accidentally."""
    rendered = build_instructions_suffix(annotation_tool_name="baton_annotate")
    assert "BEFORE" in rendered
    assert "AFTER" in rendered
    assert "MUST" in rendered
    assert "REQUIRED" in rendered


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
        "intent:",
        "expected_outcome:",
        "workflow:",
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

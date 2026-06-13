"""Shared LLM-facing text — server instructions + annotation tool description.

Mirrors the discipline in the baton-sdk's ``baton.integrations._llm_text``
module so the proxy and the SDK present a coherent annotation surface to
the calling agent. Vendors moving from proxy (hosted-eval depth) to SDK
(deep instrumentation depth) get the same field reference and the same
behavioral framing — only the deployment shape differs.

**Split of responsibility** (load-bearing under Claude Code's truncation
cap on ``InitializeResult.instructions``):

- *Server instructions* (this module's ``build_instructions_suffix``)
  carry the MUST/REQUIRED behavioral framing — the BEFORE/AFTER/IF
  triggers, the signal_type enum, and the "annotation doesn't replace
  answering" guardrail. Loaded once at session init, which is the only
  point that can drive the *first* proactive annotation before any tool
  is called.
- *Annotation tool description* (this module's
  ``build_annotation_tool_description``) carries the field-level
  reference — what belongs in intent / expected_outcome / workflow /
  suggested_improvement / context. Loaded by Claude on every call to
  the annotation tool itself, so this is the right place for the
  just-in-time field dictionary.

**Why not put both in instructions:** empirically the truncation cap
drops the tail silently. **Why not put the behavioral framing in the
description:** per-call context overhead, plus the description is read
at *call* time — too late to drive the first proactive annotation.

**Trigger discipline.** A live-Claude proxy test on 2026-06-12 surfaced
an asymmetry the original templates baked in: only the
"if a call returned an error" trigger was mechanical (an observable
state Claude could check at the end of any tool call); the
feature-gap path required vigilance, and vigilance loses to task
completion every time. Three mechanical triggers now sit alongside
each other in the instructions: (1) error after a call, (2) intent
satisfied via workaround because no tool matched, (3) user asked for
something this server can't do. Each is a state Claude can check
deterministically against its own behavior, on par with "the call
returned an error".
"""

from __future__ import annotations

# Proxy variant of the SDK template. Two adaptations vs the SDK shape:
#   1. The proxy ATTACHES its text as a suffix to whatever instructions
#      the upstream server already returns (vs the SDK, which renders the
#      whole instructions field). The leading space lets the suffix
#      concatenate cleanly onto a trailing-stop upstream value.
#   2. There's no per-vendor display name plumbed through the proxy
#      (the proxy is opaque to the wrapped server's identity), so the
#      template uses neutral "any tool on this server" phrasing instead
#      of the SDK's "{vendor_display_name} tool".
_DEFAULT_SERVER_INSTRUCTIONS_SUFFIX_TEMPLATE = (
    " This server is wrapped in the Baton support-signal proxy. Use "
    "`{annotation_tool_name}` to record what the user is trying to do "
    "and how each tool call went. See that tool's description for "
    "field-level detail.\n\n"
    "BEFORE invoking any tool on this server, you MUST call "
    "`{annotation_tool_name}` with intent (REQUIRED), expected_outcome "
    "(REQUIRED), and workflow (REQUIRED when the request fits a "
    "recognizable broader task, e.g., 'morning meeting prep', "
    "'pre-outreach research').\n\n"
    "AFTER any tool on this server errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of "
    "giving up, you MUST call `{annotation_tool_name}` again with "
    "signal_type (REQUIRED) — one of failure, retry_loop, dead_end, "
    "parameter_confusion, slow_performance, abandonment, feature_gap, "
    "other — and suggested_improvement (REQUIRED whenever you can "
    "articulate one).\n\n"
    "IF a tool response lacks a structured field for what the user "
    "asked about, OR you satisfied the user's intent via a workaround "
    "because no tool matched what they asked for, OR the user asked "
    "for something this server can't do — you MUST call "
    "`{annotation_tool_name}` with signal_type='feature_gap' AND still "
    "answer the user with your best inference. Filing the annotation "
    "does NOT replace answering."
)


_DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE = (
    "Record structured signal about a tool call on this server — what "
    "the user is trying to do, and how it went. Populate proactively "
    "before the call (intent + expected_outcome + workflow) and "
    "reactively after if the result was unhelpful (signal_type + "
    "suggested_improvement).\n"
    "\n"
    "Fields:\n"
    "  - intent: one sentence on what the user is trying to accomplish.\n"
    "  - expected_outcome: what you expect the tool to return.\n"
    "  - workflow: the broader task this call is part of, e.g., "
    "'morning meeting prep', 'pre-outreach research', 'personal "
    "scheduling'. Skip when the call doesn't fit a recognizable "
    "broader task.\n"
    "  - signal_type: one of failure, retry_loop, dead_end, "
    "parameter_confusion, slow_performance, abandonment, feature_gap, "
    "other.\n"
    "  - suggested_improvement: a concrete sentence about what product "
    "change would have helped.\n"
    "  - context: supplementary info not covered above. Common keys: "
    "plan, alternatives_considered, likely_cause, user_impact, "
    "error_class, downstream_blocked, confidence_in_intent. For "
    "signal_type='feature_gap' also missing_capability_field and "
    "requested_capability."
)


# Empirically measured Claude Code truncation cap for
# ``InitializeResult.instructions``. Reserve headroom for vendor extensions
# composed on top — and, for the proxy specifically, the upstream server's
# own pre-existing instructions string that the suffix is appended to.
_CLAUDE_CODE_TRUNCATION_CAP = 2087
_INSTRUCTIONS_LENGTH_CAP = 1500


# Canonical signal_type values per SPEC §3.1. Stable and additive-only
# until v1.0 (SPEC §13). The annotation tool's inputSchema enum and the
# instructions text must reference the same eight values; downstream
# escalation taxonomies (e.g., the priority mapping in the report
# synthesizer) key off these strings.
SIGNAL_TYPES: tuple[str, ...] = (
    "failure",
    "retry_loop",
    "dead_end",
    "parameter_confusion",
    "slow_performance",
    "abandonment",
    "feature_gap",
    "other",
)


def build_instructions_suffix(annotation_tool_name: str) -> str:
    """Build the proxy's instructions suffix.

    Appended to the upstream server's existing ``instructions`` field
    (rather than replacing it, as the SDK does). Raises ``ValueError`` if
    the rendered output exceeds the safety cap so a misconfigured
    annotation-tool name fails loudly at injection time, rather than
    silently producing a string Claude Code would truncate mid-sentence.
    """
    rendered = _DEFAULT_SERVER_INSTRUCTIONS_SUFFIX_TEMPLATE.format(
        annotation_tool_name=annotation_tool_name,
    )
    if len(rendered) > _INSTRUCTIONS_LENGTH_CAP:
        raise ValueError(
            f"Rendered instructions suffix is {len(rendered)} chars, which "
            f"exceeds the {_INSTRUCTIONS_LENGTH_CAP}-char safety cap "
            f"(Claude Code truncates at ~{_CLAUDE_CODE_TRUNCATION_CAP}). "
            f"Shorten annotation_tool_name."
        )
    return rendered


def build_annotation_tool_description() -> str:
    """Build the annotation tool's ``description`` field.

    No placeholders — the proxy is opaque to the wrapped server's
    identity, so the field reference uses neutral "this server" phrasing.
    """
    return _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE

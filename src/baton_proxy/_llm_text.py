"""Shared LLM-facing text — server instructions + annotation tool description.

Mirrors the discipline in the baton-sdk's ``baton.integrations._llm_text``
module so the proxy and the SDK present a coherent annotation surface to
the calling agent. Vendors moving from proxy (hosted-eval depth) to SDK
(deep instrumentation depth) get the same field reference and the same
behavioral framing — only the deployment shape differs.

**Split of responsibility** (load-bearing under Claude Code's truncation
cap on ``InitializeResult.instructions``):

- *Server instructions* (this module's ``build_instructions_suffix``)
  carry the MUST/REQUIRED behavioral framing — the AFTER/IF triggers,
  the signal_type enum, and the "annotation doesn't replace answering"
  guardrail. Loaded once at session init.

  **There is no BEFORE trigger any more** (D7, 2026-09-01). It asked for
  the same three fields the injected params carry on every call, so it
  taught the agent that intent is the annotation tool's job; the params
  carry INTENT and this tool carries FRICTION. The suffix is therefore
  no longer the thing that drives the session's first proactive
  annotation — the proxy synthesises that itself from the first call's
  params (``proxy.py``, ``_proactive_emitted``), which is why dropping
  the paragraph costs a turn rather than a record.
- *Annotation tool description* (this module's
  ``build_annotation_tool_description``) carries the field-level
  reference — what belongs in user_goal / expected_result / overall_task /
  suggested_improvement / context. Loaded by Claude on every call to
  the annotation tool itself, so this is the right place for the
  just-in-time field dictionary. Its LEAD varies with ``proactive_mode``;
  the field reference below it does not.

**Why not put both in instructions:** empirically the truncation cap
drops the tail silently, and the proxy APPENDS — on a large enterprise
server it is this text, at the end, that gets cut. **Why not put the
behavioral framing in the description:** per-call context overhead, and
the description is read at *call* time, which is too late for a trigger
that has to fire before the agent decides to call anything.

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
#
# The BEFORE paragraph was DROPPED 2026-09-01 (D7 in baton-internal
# `intent_param_injection.md`), in both proactive modes, and this is the one
# place in the proxy where the two channels were conflated. It asked for
# user_goal / expected_result / overall_task — the same three fields the
# injected params already carry on every single call — and by naming them here
# it taught the agent that intent is the annotation tool's job. Evidence: on
# 2026-09-01 three real sessions filed four annotations, every one of them
# BEFORE-style, carrying intent with no signal_type, and filed ZERO friction
# signals. The tool spent itself restating what the params had already
# recorded. Now the params carry INTENT and this tool carries FRICTION, which
# is the artifact with no param analogue.
#
# It also bought headroom that is not optional on the servers we most want:
# 260 chars off a 1,236-char suffix, against Claude Code's ~2,087-char cap on
# `instructions` — and the proxy APPENDS, so on a large enterprise server
# (workfront rendered 4,408 chars) it is this framing, at the end, that gets
# silently cut.
#
# What did NOT change: AFTER and IF stay in both modes. They produce the
# friction signal, they are the product, and they have no other carrier.
_DEFAULT_SERVER_INSTRUCTIONS_SUFFIX_TEMPLATE = (
    " This server is wrapped in the Baton support-signal proxy. Use "
    "`{annotation_tool_name}` to record how each tool call went. See that "
    "tool's description for field-level detail.\n\n"
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


# The two leads, selected by ``proactive_mode``. Ported from baton-sdk's
# ``_ANNOTATION_LEAD_PROACTIVE`` / ``_ANNOTATION_LEAD_REACTIVE_ONLY``.
#
# `on` is today's behaviour and stays the proxy's default: the tool is for
# intent AND outcomes. `off` reframes it as the friction channel alone, which
# is what the handler enforces in that mode — text alone is only a request,
# and one stray proactive carrying an umbrella `overall_task` label is enough
# to merge two distinct tasks in any consumer that groups on it.
#
# `user_goal` stays required in BOTH leads: a friction report still has to say
# what was being attempted, and by then the params have carried it once
# already. That duplication is cheap; a signal with no subject is not.
_ANNOTATION_LEAD_PROACTIVE = (
    "Record structured signal about a tool call on this server — what "
    "the user is trying to do, and how it went. Populate proactively "
    "before the call (user_goal + expected_result + overall_task) and "
    "reactively after if the result was unhelpful (signal_type + "
    "suggested_improvement).\n"
)

_ANNOTATION_LEAD_REACTIVE_ONLY = (
    "Report a tool call on this server that went wrong — call this AFTER a "
    "call returns an unhelpful, empty, failed or contradictory result, or "
    "when no tool covers what the user asked for. Do NOT call it before a "
    "tool call or to narrate normal successful work. What the user is trying "
    "to do is already recorded on each tool call.\n"
)

_DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE = (
    "{lead}"
    "\n"
    "Fields:\n"
    "  - user_goal: one sentence on what the user is trying to "
    "accomplish.\n"
    "  - expected_result: what a successful result should look like, so a "
    "silent/thin failure can be told apart from success.\n"
    "  - overall_task: short stable label for the broader task this "
    "call serves, e.g., 'morning meeting prep', 'pre-outreach "
    "research', 'personal scheduling'. REPEAT the exact same string on "
    "every call serving the same task; change it only when the user "
    "starts a different task. Skip when the call doesn't fit a "
    "recognizable broader task.\n"
    "  - signal_type: reactive-only — omit on a proactive annotation. "
    "Set only once a tool call has returned an unhelpful result. One "
    "of failure, retry_loop, dead_end, parameter_confusion, "
    "slow_performance, abandonment, feature_gap, other.\n"
    "  - suggested_improvement: reactive-only — omit on a proactive. "
    "A concrete sentence about what product change would have helped.\n"
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


def build_annotation_tool_description(proactive_mode: str = "on") -> str:
    """Build the annotation tool's ``description`` field.

    ``proactive_mode="off"`` swaps the lead for the reactive-only one; the
    field reference below it is identical in both modes, because the fields
    themselves do not change — only whether the agent may open with them.

    No vendor placeholder — the proxy is opaque to the wrapped server's
    identity, so the field reference uses neutral "this server" phrasing.
    """
    lead = _ANNOTATION_LEAD_PROACTIVE if proactive_mode == "on" else _ANNOTATION_LEAD_REACTIVE_ONLY
    return _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(lead=lead)


# Per-tool injected params (`user_goal` / `expected_result`). Unlike
# instructions, this text rides IN each tool's schema, so it is in front of
# the model at call-compose time on every client — including Claude Desktop,
# which ignores initialize-instructions entirely (verified empirically
# 2026-07-07).
#
# Names + copy match baton-sdk's ``baton.integrations._llm_text`` verbatim
# (ported 2026-08-08 — proxy previously used a single namespaced
# `baton_intent` param with its own copy; that was the accepted SPEC §13
# divergence, closed here since neither producer has an external customer
# depending on the old shape). Vendor-neutral names, not `baton_*` — anything
# the customer's agent can see on an instrumented surface must speak the
# vendor's voice, never Baton's (white-label rule); proxy's original
# namespaced choice was a collision-safety call that doesn't apply once the
# names match the SDK's spike-proven neutral choice.
USER_GOAL_PARAM_NAME = "user_goal"
EXPECTED_RESULT_PARAM_NAME = "expected_result"
# The task-label grouping key (wire field ``call_workflow``; console rung 3b).
# Deliberately NOT named ``workflow``: injected params live inside vendor tool
# schemas, where ``workflow`` is a plausible real vendor param (Workfront
# approvals, CI pipelines, Notion automations) — a collision would make the
# strip swallow the vendor's own argument, and the name would invite the model
# to fill in the vendor object it is touching instead of the meta task label.
OVERALL_TASK_PARAM_NAME = "overall_task"

_USER_GOAL_PARAM_DESCRIPTION = (
    "OPTIONAL. One sentence: what the user is actually trying to accomplish "
    "with this call (their goal, not a restatement of the arguments)."
)

_EXPECTED_RESULT_PARAM_DESCRIPTION = (
    "OPTIONAL. One sentence: what a successful result should look like, so a "
    "silent/thin failure can be told apart from success."
)


# The stability contract is the load-bearing design element: user_goal/
# expected_result are call-scoped diagnostics that reword freely, so they
# cannot key grouping; this param works ONLY if the model repeats the label
# verbatim while the task is unchanged (measured 2026-08-10: without the
# contract, 80% of adjacent same-task calls reword their goal text).
#
# Granularity is a KNOWN, MEASURED weakness of this text, kept anyway because
# the obvious fix is worse. Do not reword without scoring against both corpora
# in baton-internal `spikes/overall_task_a5/` (40 paired live-agent sessions,
# 2026-08-11, one build per run): the candidate ("the specific task the user is
# working on right now — not the overall theme") fixes boundary detection
# (0.700 -> 1.000) but relabels *within* one task (0.200 then 0.400 over-split
# on identical scripts), and shattering is the failure mode that destroys
# downstream trust. Byte-identical to baton-sdk's
# ``_OVERALL_TASK_PARAM_DESCRIPTION`` and baton-ts's
# ``OVERALL_TASK_PARAM_DESCRIPTION``. Each producer pins its own copy in its
# own suite (no cross-repo read: this suite ships in the Try Kit, where a
# prospect's clone has no siblings). Three local pins make a change deliberate
# and visible in review; they cannot notice the three drifting apart, so change
# all three together.
_OVERALL_TASK_PARAM_DESCRIPTION = (
    "OPTIONAL. Short stable label for the broader task this call serves "
    "(e.g. 'prepare campaign approval'). REPEAT the exact same string on "
    "every call serving the same task; change it only when the user starts "
    "a different task."
)


def build_overall_task_param_description() -> str:
    """Build the injected ``overall_task`` param's ``description`` field."""
    return _OVERALL_TASK_PARAM_DESCRIPTION


def build_user_goal_param_description() -> str:
    """Build the injected ``user_goal`` param's ``description`` field."""
    return _USER_GOAL_PARAM_DESCRIPTION


def build_expected_result_param_description() -> str:
    """Build the injected ``expected_result`` param's ``description`` field."""
    return _EXPECTED_RESULT_PARAM_DESCRIPTION

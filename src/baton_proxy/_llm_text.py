"""Shared LLM-facing text — server instructions + annotation tool description.

Mirrors the discipline in the baton-sdk's ``baton.integrations._llm_text``
module so the proxy and the SDK present a coherent annotation surface to
the calling agent. Vendors moving from proxy (hosted-eval depth) to SDK
(deep instrumentation depth) get the same field reference and the same
behavioral framing — only the deployment shape differs.

**Reports only** (``proactive_mode="off"``, the default): the instructions
suffix is one sentence pointing at the annotation tool, and the tool's
description holds the triggers, the recovery rule and the fields, so each rule
is written once. The proxy APPENDS to the upstream server's instructions and
Claude Code truncates the field at ~2,087 from the end, so a short suffix is
also the one least likely to be cut.

**Proactive** (``proactive_mode="on"``): the suffix carries the BEFORE/AFTER/IF
triggers, because only text loaded at session start can ask for an annotation
before the first tool call.

Every trigger names a state the agent can check against its own behavior (an
error, a missing field, a workaround), not a judgement it has to remember to
make.
"""

from __future__ import annotations

# Proxy variant of the SDK template. Two adaptations vs the SDK shape:
#   1. The proxy ATTACHES its text as a suffix to whatever instructions
#      the upstream server already returns (vs the SDK, which renders the
#      whole instructions field). The leading space lets the suffix
#      concatenate cleanly onto a trailing-stop upstream value.
#   2. There's no per-vendor display name plumbed through the proxy
#      (the proxy is opaque to the wrapped server's identity), so the
#      template uses neutral "on this server" phrasing instead of the SDK's
#      "{vendor_display_name} tool".
_HEAD_PROACTIVE = (
    " This server is wrapped in the Baton usage and friction proxy. Use "
    "`{annotation_tool_name}` to record what the user is trying to do "
    "and how each tool call went, and to report when a tool you needed "
    "does not exist, so the team behind this server can improve it. See "
    "that tool's description for field-level detail.\n\n"
)

# The whole report suffix under proactive_mode="off". Holds no report rule:
# each is written once, in the tool's description.
_REPORT_POINTER = (
    " Use `{annotation_tool_name}` to report a tool call on this server that went "
    "wrong, or a request no tool covers; its description says when and how."
)

# Rendered only when proactive_mode == "on".
#
# WHY IT IS GATED RATHER THAN DELETED. It names the same field the
# injected param already carries on every call, so it teaches the agent that
# intent is the annotation tool's job, and agents then file pre-call
# annotations instead of friction signals. It also costs 260 of the suffix's
# chars, which is not free: the proxy APPENDS to the upstream server's own
# instructions, and Claude Code truncates the field at ~2,087, so on a large
# server it is the tail — our framing — that is silently cut.
#
# Both of those are arguments for turning it OFF, not for removing the choice.
# The proxy fronts servers its operator does not own, and agent-authored
# proactive annotations supply ~54% of turn boundaries (SPEC §11.5 tier 2), so
# deleting it outright would take that from every existing wrap on the next
# restart with nobody choosing it. It is the knob's job, exactly as in the SDK.
_PROACTIVE_CLAUSE = (
    "BEFORE invoking any tool on this server, you MUST call "
    "`{annotation_tool_name}` with user_goal (REQUIRED).\n\n"
)

_REACTIVE_CLAUSES = (
    "AFTER any tool on this server errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of "
    "giving up, you MUST call `{annotation_tool_name}` again with "
    "what_happened (REQUIRED, your own words, NOT a category), tool_name "
    "(REQUIRED) and suggested_improvement (REQUIRED whenever you can "
    "articulate one).\n\n"
    "IF a tool response lacks a structured field for what the user "
    "asked about, OR you satisfied the user's intent via a workaround "
    "because no tool matched what they asked for, OR the user asked "
    "for something this server can't do — you MUST call "
    "`{annotation_tool_name}` once per user request, not per call, with "
    "what_happened and tool_name (none if no tool) AND still answer the "
    "user with your best inference. Filing the annotation does NOT replace "
    "answering."
)


_ANNOTATION_LEAD_PROACTIVE = (
    "Record structured signal about a tool call on this server — what "
    "the user is trying to do, and how it went. Populate proactively "
    "before the call (user_goal) and reactively after if the result was "
    "unhelpful (what_happened + tool_name + suggested_improvement).\n"
)

# The Console splits user_goal on "; success is" (SPEC §11.5.1 rule 2).
_USER_GOAL_FORMAT = "'<n>: <what this call is for>; success is <what its result should show>'"

# Keep the key list: without it, agents leave context empty.
_CONTEXT_FIELD = (
    "  - context: supplementary info not covered above. Common keys: "
    "plan, alternatives_considered, likely_cause, user_impact, "
    "error_class, downstream_blocked, confidence_in_intent. When no tool "
    "covers the request also missing_capability_field and "
    "requested_capability."
)

_PROACTIVE_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE = (
    _ANNOTATION_LEAD_PROACTIVE + "\n"
    "Fields:\n"
    "  - user_goal: {user_goal}\n"
    "  - what_happened: REQUIRED on a report — omit on a proactive annotation. "
    "1-2 plain sentences in YOUR OWN WORDS: what you asked for, "
    "what came back, and why it was unusable. Do NOT pick a category or "
    "invent a label — describe it.\n"
    "  - tool_name: REQUIRED on a report. The tool on this server that went "
    "wrong. Write none if no tool exists for the request.\n"
    "  - suggested_improvement: reactive-only — omit on a proactive. "
    "A concrete sentence about what product change would have helped.\n" + _CONTEXT_FIELD
)

# The SDK's scored report-tool text, with "on this server" for the vendor name.
_REPORT_TOOL_DESCRIPTION = (
    "Report a tool call on this server that went wrong, so the team behind this "
    "server can fix it. Call this right after a call errors, times out, or returns "
    "an empty, unhelpful or contradictory result. Also call it once per user "
    "request when no tool or field covers what they asked for, or you had to work "
    "around one. Do not call it before a tool call or for normal successful work.\n"
    "\n"
    "A report does not replace the work: after it, work out why the call failed, "
    "fix the input and try again (do not repeat the same failing call), and still "
    "answer the user.\n"
    "\n"
    "Fields:\n"
    "  - user_goal: the same " + _USER_GOAL_FORMAT + " as on this server's other tools.\n"
    "  - what_happened: REQUIRED. 1-2 plain sentences in your own words: what you "
    "asked for, what came back, and why it was unusable. Not a category.\n"
    "  - tool_name: REQUIRED. The tool on this server that went wrong, or none if "
    "no tool exists for the request.\n"
    "  - suggested_improvement: one concrete sentence on the product change that "
    "would have helped.\n" + _CONTEXT_FIELD
)


# Empirically measured Claude Code truncation cap for
# ``InitializeResult.instructions``. Reserve headroom for vendor extensions
# composed on top — and, for the proxy specifically, the upstream server's
# own pre-existing instructions string that the suffix is appended to.
_CLAUDE_CODE_TRUNCATION_CAP = 2087
_INSTRUCTIONS_LENGTH_CAP = 1500


# A main agent that delegates may never load this server's tool schemas, so the
# user_goal description never reaches it and its subagents get no number.
_INSTRUCTIONS_SUBAGENT_CLAUSE = (
    "\n\nWhen you hand work to a subagent that may call this server's tools, tell it "
    "the number of the user's current message in this conversation, and that it "
    "must start user_goal with that number on every call to this server's "
    "tools. This applies only to this server's tools."
)

#: All of the proxy's instructions under ``BATON_ANNOTATE=off``, apart from the
#: event-file line; the same sentence baton-sdk exports.
INTENT_INSTRUCTIONS = _INSTRUCTIONS_SUBAGENT_CLAUSE.strip()


# The line that tells the agent where the session's own events are, rendered
# only when there IS a local file to name. It replaces the retired
# `baton_session_report` tool (2026-09-27): that tool rendered a fixed template
# over this same file and classified nothing, so the agent reading the file
# directly is the better answer AND the smaller surface.
#
# ⚠ The one part of this suffix whose length the proxy does not control. A path
# is whatever the operator's BATON_EVENT_SINK says, so this line is the only
# piece that is DROPPED rather than allowed to breach the cap — see
# build_instructions_suffix.
# ⚠ WRITTEN TO A CLAIM IT CAN KEEP, and no longer to a length.
#
# It used to be written to a length, and the length was distorting it. The first
# version said "This session's tool calls and results", which is false: the
# default sink is ONE fixed path (`config.DEFAULT_EVENT_SINK`), so the file
# accumulates across restarts AND across every server wrapped with the defaults,
# and the try kit's file grows for as long as the wrap is installed. The second
# version said "appended across sessions", which is true and still narrower than
# the truth — other SERVERS append to it too.
#
# The honest sentence did not fit the 1,500-char cap under
# `proactive_mode="on"`, which is what forced the rewording twice. It fits now
# because the cap that applies to this line is the client's ~2,087 rather than
# our self-imposed 1,500 — see `build_instructions_suffix` for why being LAST is
# what earns it that.
#
# And it tells the agent how to actually narrow the file rather than only that it
# cannot be trusted: every event carries `session_id` and `vendor_id`
# (`emitter.py:155-160`), so the two things the file mixes are both filterable
# from the file itself. The retired tool pre-filtered on `session_id`; naming the
# field gets the same result without a renderer.
_EVENT_FILE_LINE = (
    "\n\nTool calls and results are captured as JSONL at {event_file_path}, "
    "newest last. Other sessions and other wrapped servers append to the same "
    "file, so filter on the `session_id` and `vendor_id` that every line "
    "carries, and read from the end. Use it to answer what went wrong."
)


def build_instructions_suffix(
    annotation_tool_name: str,
    proactive_mode: str = "off",
    event_file_path: str | None = None,
    intent_param_mode: str = "required",
    report_mode: str = "on",
) -> str:
    """Build the proxy's instructions suffix.

    ``proactive_mode="off"`` renders only the pointer to the annotation tool,
    whose description holds the report rules; ``"on"`` renders the head, the
    pre-call request and the AFTER/IF clauses. Any ``intent_param_mode`` but ``"off"``
    adds the subagent sentence, since every wrapped tool then carries
    ``user_goal``. ``report_mode="off"`` drops the annotation text, leaving the
    subagent sentence and the event-file line; its default here is ``"on"`` so
    a caller that does not pass it gets the text this function always built.
    Leg-for-leg the same selection baton-sdk's ``build_server_instructions``
    makes — only the DEFAULT differs, and deliberately: the SDK wraps a server
    its vendor owns, the proxy fronts servers its operator does not, so the
    proxy ships at today's behaviour and the operator chooses.

    ``event_file_path`` names the local event file, LAST, after the report
    text. Claude Code truncates ``instructions`` at ~2,087 from the END and the
    proxy APPENDS to whatever the upstream server sent, so on a server with long
    instructions of its own the cut lands inside our suffix. Last means the
    convenience line is what truncation takes, not a report rule.

    None when the sink spec has no ``file://`` leg — every http-only wrap, which
    is the common production shape. ⚠ NOT "every production wrap": a
    ``file+http`` tee-to-disk has a file leg and does get its path named, so an
    absolute path the operator chose (home directory and OS username included)
    enters the model's context on that shape. The retired tool's gate suppressed
    itself there; this line does not, because naming a path the operator
    configured is not the same act as rendering the file's contents.

    Appended to the upstream server's existing ``instructions`` field
    (rather than replacing it, as the SDK does). Raises ``ValueError`` if
    the rendered output exceeds the safety cap so a misconfigured
    annotation-tool name fails loudly at injection time, rather than
    silently producing a string Claude Code would truncate mid-sentence.

    ⚠ TWO CAPS, and the split is the point.

    ``_INSTRUCTIONS_LENGTH_CAP`` (1,500) bounds the report text — the pointer, or
    under ``proactive_mode="on"`` the head, pre-call request and AFTER/IF
    clauses — and overflowing it RAISES. Its margin under the client's ~2,087
    exists so that a server with instructions of its own does not have our
    report text cut.

    The event-file line is measured against ``_CLAUDE_CODE_TRUNCATION_CAP``
    instead, and it earns that by being LAST: truncation eats the end, so this
    line's bytes can only ever displace this line, never the report text the
    1,500 margin protects.

    It is still DROPPED rather than raised on when it does not fit even that,
    because its length comes from the operator's sink URL — a filesystem path is
    not a misconfiguration the way a 500-character tool name is, and raising
    would fail a handshake that is otherwise correct. Rare now rather than
    routine, and ``_bootstrap`` logs a warning when it happens, so an operator
    whose agent was never told the path can find out why.
    """
    if report_mode == "off":
        rendered = ""
    else:
        rendered = _render_report_text(annotation_tool_name, proactive_mode)
    # After `_render_report_text` caps its text: the sentence has a fixed
    # length, so it comes out of the headroom under the truncation point.
    # Before the event-file line, so truncation still takes that line first.
    if intent_param_mode != "off":
        rendered += _INSTRUCTIONS_SUBAGENT_CLAUSE
    if event_file_path:
        # Spliced into the ALREADY-FORMATTED string, so the path never sits
        # inside the multi-field template above. That template is formatted with
        # `annotation_tool_name` and `again`, so a `{` or `}` in the operator's
        # sink path would raise KeyError out of it — at injection time, on the
        # handshake, over a punctuation character in a directory name. Composing
        # the line here rather than adding it to that template is the whole
        # guard; `.replace` vs `.format` on this line alone is not, since
        # `.format` does not re-process what it substitutes in.
        line = _EVENT_FILE_LINE.replace("{event_file_path}", event_file_path)
        if len(rendered) + len(line) <= _CLAUDE_CODE_TRUNCATION_CAP:
            rendered += line
    return rendered


def _render_report_text(annotation_tool_name: str, proactive_mode: str) -> str:
    if proactive_mode == "on":
        template = _HEAD_PROACTIVE + _PROACTIVE_CLAUSE + _REACTIVE_CLAUSES
    else:
        template = _REPORT_POINTER
    rendered = template.format(annotation_tool_name=annotation_tool_name)
    if len(rendered) > _INSTRUCTIONS_LENGTH_CAP:
        raise ValueError(
            f"Rendered instructions suffix is {len(rendered)} chars, which "
            f"exceeds the {_INSTRUCTIONS_LENGTH_CAP}-char safety cap "
            f"(Claude Code truncates at ~{_CLAUDE_CODE_TRUNCATION_CAP}). "
            f"Shorten annotation_tool_name."
        )
    return rendered


def build_annotation_tool_description(proactive_mode: str = "off") -> str:
    """Build the annotation tool's ``description`` field.

    ``proactive_mode="off"`` makes it the report tool, holding the report rules
    the suffix only points at. Its ``user_goal`` line refers to the other tools'
    template, which they always carry: config refuses ``BATON_INTENT_PARAM=off``.
    """
    if proactive_mode == "on":
        return _PROACTIVE_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
            user_goal=_USER_GOAL_PARAM_BODY
        )
    return _REPORT_TOOL_DESCRIPTION


# Per-tool injected params. Unlike instructions, this text rides IN each tool's
# schema, so it is in front of the model at call-compose time on every client —
# including Claude Desktop, which ignores initialize-instructions entirely.
#
# Vendor-neutral names, not `baton_*`: anything the customer's agent can see on
# an instrumented surface must speak the vendor's voice, never Baton's.
USER_GOAL_PARAM_NAME = "user_goal"
INTENT_PARAM_NAMES = (USER_GOAL_PARAM_NAME,)

# No longer injected, but a client holding a tool list from before may still
# send them: the upstream tool would get an argument it never declared, and
# ``overall_task`` carries the turn number the Console falls back to.
EXPECTED_RESULT_PARAM_NAME = "expected_result"
OVERALL_TASK_PARAM_NAME = "overall_task"
RETIRED_PARAM_NAMES = (EXPECTED_RESULT_PARAM_NAME, OVERALL_TASK_PARAM_NAME)

# The text below is byte-identical to baton-sdk's and baton-ts's. Each producer
# pins its own copy in its own suite (this suite ships in the Try Kit, where a
# clone has no siblings to read), so change all three together.
#
# The leading number says which user message the call answers, and the
# Console cuts a turn where it changes (SPEC §11.5.1 rule 2). The Console
# splits the rest on "; success is". Only the leading label tracks
# ``intent_param_mode``, so it agrees with the schema's ``required`` list.
#
# Do not reword without scoring the result on live agents.
_USER_GOAL_PARAM_BODY = (
    f"Write it as {_USER_GOAL_FORMAT}. n is the number of the user's current message in this "
    "conversation (1 for the first). Use the same number on every call you make "
    "for that message, including calls made after reading tool results; it goes "
    "up only when the user sends another message."
)


def build_user_goal_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``user_goal`` param's ``description`` field.

    ``intent_param_mode="required"`` swaps the leading label for "REQUIRED.",
    matching the ``required`` entry the injector adds under that mode. The
    default is ``"optional"`` rather than the proxy's own ``required`` default
    so that omitting the argument cannot silently advertise a constraint the
    caller never asked for — every real call site passes the configured mode.
    """
    label = "REQUIRED. " if intent_param_mode == "required" else "OPTIONAL. "
    return label + _USER_GOAL_PARAM_BODY


def required_param_names(*, intent_param_mode: str) -> tuple[str, ...]:
    """Which injected params ``intent_param_mode`` advertises as required.

    Advertised, never enforced: a call that omits one is forwarded as it would
    be unwrapped. A tool that declares one of these names itself keeps its own
    schema for it.
    """
    return INTENT_PARAM_NAMES if intent_param_mode == "required" else ()

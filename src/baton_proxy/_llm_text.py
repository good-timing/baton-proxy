"""Shared LLM-facing text — server instructions + annotation tool description.

Mirrors the discipline in the baton-sdk's ``baton.integrations._llm_text``
module so the proxy and the SDK present a coherent annotation surface to
the calling agent. Vendors moving from proxy (hosted-eval depth) to SDK
(deep instrumentation depth) get the same field reference and the same
behavioral framing — only the deployment shape differs.

The instructions suffix is one sentence pointing at the annotation tool, and
the tool's description holds the triggers, the recovery rule and the fields, so
each rule is written once. The proxy APPENDS to the upstream server's
instructions and Claude Code truncates the field at ~2,087 from the end, so a
short suffix is also the one least likely to be cut.

Every trigger names a state the agent can check against its own behavior (an
error, a missing field, a workaround), not a judgement it has to remember to
make.
"""

from __future__ import annotations

# The whole report suffix. Holds no report rule: each is written once, in the
# tool's description. The leading space lets it join a trailing-stop upstream
# value.
_REPORT_POINTER = (
    " Use `{annotation_tool_name}` to report a tool call on this server that went "
    "wrong, or a request no tool covers; its description says when and how."
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


# Rendered only when there IS a local file to name. Its length comes from the
# operator's BATON_EVENT_SINK, so it is the one piece DROPPED rather than allowed
# to breach the cap — see build_instructions_suffix.
#
# Do not narrow it to "this session's" calls: the default sink is one fixed path
# (`config.DEFAULT_EVENT_SINK`) that every restart and every wrapped server
# appends to, which is why it names the two fields that tell them apart.
_EVENT_FILE_LINE = (
    "\n\nTool calls and results are captured as JSONL at {event_file_path}, "
    "newest last. Other sessions and other wrapped servers append to the same "
    "file, so filter on the `session_id` and `vendor_id` that every line "
    "carries, and read from the end. Use it to answer what went wrong."
)


def build_instructions_suffix(
    annotation_tool_name: str,
    *,
    event_file_path: str | None = None,
    report_mode: str = "on",
) -> str:
    """Build the proxy's instructions suffix.

    Renders the pointer to the annotation tool, whose description holds the
    report rules, then the subagent sentence, since every wrapped tool carries
    ``user_goal``. ``report_mode="off"`` drops the annotation text, leaving the
    subagent sentence and the event-file line. The same selection baton-sdk's
    ``resolve_install_text`` makes.

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

    ``_INSTRUCTIONS_LENGTH_CAP`` (1,500) bounds the report text, and overflowing
    it RAISES. Its margin under the client's ~2,087 exists so that a server with
    instructions of its own does not have our report text cut.

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
        rendered = _render_report_text(annotation_tool_name)
    # After `_render_report_text` caps its text: the sentence has a fixed
    # length, so it comes out of the headroom under the truncation point.
    # Before the event-file line, so truncation still takes that line first.
    rendered += _INSTRUCTIONS_SUBAGENT_CLAUSE
    if event_file_path:
        # Spliced into the already-formatted report text: a `{` or `}` in the
        # operator's sink path would make `.format` on that template raise.
        line = _EVENT_FILE_LINE.replace("{event_file_path}", event_file_path)
        if len(rendered) + len(line) <= _CLAUDE_CODE_TRUNCATION_CAP:
            rendered += line
    return rendered


def _render_report_text(annotation_tool_name: str) -> str:
    rendered = _REPORT_POINTER.format(annotation_tool_name=annotation_tool_name)
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

    It holds the report rules the suffix only points at. Its ``user_goal`` line
    refers to the other tools' template, which they always carry: config coerces
    ``BATON_INTENT_PARAM=off`` to the default.
    """
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
# Console cuts a turn where it changes (SPEC §11.5.1 rule 2). Only the leading
# label tracks ``intent_param_mode``, so it agrees with the schema's ``required``
# list.
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

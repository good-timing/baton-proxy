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
  the report fields, and the "annotation doesn't replace answering"
  guardrail. Loaded once at session init.

  The BEFORE clause renders only under ``proactive_mode="on"``
  (``_PROACTIVE_CLAUSE`` below). The injected params carry INTENT on every
  call and this tool carries FRICTION, so under ``off`` the session loses
  the agent's turn-opener, not the intent: every tool_call_start carries
  its call's intent.
- *Annotation tool description* (this module's
  ``build_annotation_tool_description``) carries the field-level
  reference — what belongs in user_goal / expected_result / overall_task /
  suggested_improvement / context. Loaded by Claude on every call to
  the annotation tool itself, so this is the right place for the
  just-in-time field dictionary. Its LEAD varies with ``proactive_mode``,
  and the field reference describes a note only in the mode that takes one.

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
# Two heads and one gated clause, selected by ``proactive_mode`` — a
# leg-for-leg port of baton-sdk's ``build_server_instructions``. The head
# differs only in what it promises the tool is for: with proactive on it
# records intent AND outcomes; with it off the injected params carry intent
# and the tool is the friction channel alone.
_HEAD_PROACTIVE = (
    " This server is wrapped in the Baton usage and friction proxy. Use "
    "`{annotation_tool_name}` to record what the user is trying to do "
    "and how each tool call went, and to report when a tool you needed "
    "does not exist, so the team behind this server can improve it. See "
    "that tool's description for field-level detail.\n\n"
)

_HEAD_REACTIVE_ONLY = (
    " This server is wrapped in the Baton usage and friction proxy. Use "
    "`{annotation_tool_name}` to report when a tool call on this server "
    "goes wrong, or when a tool you needed does not exist, so the team "
    "behind this server can improve it. See that tool's description for "
    "field-level detail.\n\n"
)

# Rendered only when proactive_mode == "on".
#
# WHY IT IS GATED RATHER THAN DELETED. It names the same three fields the
# injected params already carry on every call, so it teaches the agent that
# intent is the annotation tool's job — measured 2026-09-01, three real
# sessions filed four annotations, every one of them a pre-call annotation
# carrying intent with no `signal_type`, and ZERO friction signals. It also
# costs 260 of the suffix's chars, which is not free: the proxy APPENDS to the
# upstream server's own instructions, and Claude Code truncates the field at
# ~2,087 (workfront's rendered value measured 4,408 on 2026-09-01), so on a
# large enterprise server it is the tail — our framing — that is silently cut.
#
# Both of those are arguments for turning it OFF, not for removing the choice.
# The proxy fronts servers its operator does not own, and agent-authored
# proactive annotations supply ~54% of turn boundaries (SPEC §11.5 tier 2), so
# deleting it outright would take that from every existing wrap on the next
# restart with nobody choosing it. It is the knob's job, exactly as in the SDK.
_PROACTIVE_CLAUSE = (
    "BEFORE invoking any tool on this server, you MUST call "
    "`{annotation_tool_name}` with user_goal (REQUIRED), expected_result "
    "(REQUIRED), and overall_task (REQUIRED): a short stable label for "
    "the broader task this call serves (e.g., 'morning meeting prep'), "
    "repeated verbatim until the user starts a different task.\n\n"
)

# Always present, in both modes — this is the product signal, and it has no
# param analogue.
#
# ONE WORD VARIES, and it is a back-reference rather than a rewrite: `again`
# points at the pre-call request, so under ``proactive_mode="off"`` — the
# default since 2026-09-01 — it pointed at a paragraph that no longer renders.
# The agent's first instruction was to do a thing "again" it had never been
# asked to do once, while the tool description one field over said "Do NOT call
# it before a tool call". Found in review 2026-09-01, live on every wrap.
_REACTIVE_CLAUSES = (
    "AFTER any tool on this server errors, times out, returns an "
    "unhelpful or contradictory result, or the user shows signs of "
    "giving up, you MUST call `{annotation_tool_name}` {again}with "
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


# The annotation tool's two description leads, selected by the same
# ``proactive_mode``. Ported from baton-sdk's ``_ANNOTATION_LEAD_PROACTIVE`` /
# ``_ANNOTATION_LEAD_REACTIVE_ONLY``.
#
# `off` reframes the tool as the friction channel alone, which is also what the
# handler then enforces — text is only a request, and one stray pre-call
# annotation carrying an umbrella `overall_task` label is enough to merge two
# distinct tasks in a consumer that groups on it.
#
# `user_goal` stays required in BOTH leads: a friction report still has to say
# what was being attempted, and by then the params have carried it once
# already. That duplication is cheap; a signal with no subject is not.
_ANNOTATION_LEAD_PROACTIVE = (
    "Record structured signal about a tool call on this server — what "
    "the user is trying to do, and how it went. Populate proactively "
    "before the call (user_goal + expected_result + overall_task) and "
    "reactively after if the result was unhelpful (what_happened + "
    "tool_name + suggested_improvement).\n"
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
    "research'. REPEAT the exact same string on every call serving the "
    "same task; change it only when the user starts a different task.\n"
    "  - what_happened: REQUIRED on a report{omit_on_a_note}. "
    "1-2 plain sentences in YOUR OWN WORDS: what you asked for, "
    "what came back, and why it was unusable. Do NOT pick a category or "
    "invent a label — describe it.\n"
    "  - tool_name: REQUIRED on a report. The tool on this server that went "
    "wrong. Write none if no tool exists for the request.\n"
    "  - suggested_improvement: {report_only}"
    "A concrete sentence about what product change would have helped.\n"
    "  - context: supplementary info not covered above. Common keys: "
    "plan, alternatives_considered, likely_cause, user_impact, "
    "error_class, downstream_blocked, confidence_in_intent. When no tool "
    "covers the request also missing_capability_field and "
    "requested_capability."
)


# Empirically measured Claude Code truncation cap for
# ``InitializeResult.instructions``. Reserve headroom for vendor extensions
# composed on top — and, for the proxy specifically, the upstream server's
# own pre-existing instructions string that the suffix is appended to.
_CLAUDE_CODE_TRUNCATION_CAP = 2087
_INSTRUCTIONS_LENGTH_CAP = 1500


# A main agent that delegates may never load this server's tool schemas, so the
# overall_task description never reaches it and its subagents get no number.
_INSTRUCTIONS_SUBAGENT_CLAUSE = (
    "\n\nWhen you hand work to a subagent that may call this server's tools, tell it "
    "the number of the user's current message in this conversation, and that it "
    "must start overall_task with that number on every call to this server's "
    "tools. This applies only to this server's tools."
)


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
) -> str:
    """Build the proxy's instructions suffix.

    ``proactive_mode="off"`` reframes the head and drops the pre-call
    annotation request; the reactive clauses are the same in both modes apart
    from the word `again`, which is a back-reference to that request and is
    rendered only when the request is. Any ``intent_param_mode`` but ``"off"``
    adds the subagent sentence, since every wrapped tool then carries
    ``overall_task``.
    Leg-for-leg the same selection baton-sdk's ``build_server_instructions``
    makes — only the DEFAULT differs, and deliberately: the SDK wraps a server
    its vendor owns, the proxy fronts servers its operator does not, so the
    proxy ships at today's behaviour and the operator chooses.

    ``event_file_path`` names the local event file, LAST, after both MUST
    clauses. ⚠ That is deliberate and it is the opposite of where this line first
    went. Claude Code truncates ``instructions`` at ~2,087 from the END and the
    proxy APPENDS to whatever the upstream server sent, so on a server with
    roughly 920-1,040 chars of its own instructions the cut lands inside our
    suffix — and putting this line first moved the cut onto "Filing the
    annotation does NOT replace answering", the clause without which the agent
    treats the annotation as satisfying the user. Last means the convenience
    line is what truncation takes, which is the right thing to lose.

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

    ``_INSTRUCTIONS_LENGTH_CAP`` (1,500) bounds the BASE suffix — head, optional
    pre-call clause, both MUST clauses — and overflowing it RAISES, as it always
    did. Its 587-char margin under the client's ~2,087 exists so that a server
    with instructions of its own does not have OUR normative text cut.

    The event-file line is measured against ``_CLAUDE_CODE_TRUNCATION_CAP``
    instead, and it earns that by being LAST. Truncation eats the end, so this
    line's bytes can only ever displace this line — they cannot push a MUST
    clause over the cliff, which is the whole thing the 1,500 margin protects.
    Holding it to 1,500 as well bought nothing and cost the sentence its
    accuracy: it left 160 chars under ``proactive_mode="on"``, and the honest
    wording does not fit 160.

    It is still DROPPED rather than raised on when it does not fit even that,
    because its length comes from the operator's sink URL — a filesystem path is
    not a misconfiguration the way a 500-character tool name is, and raising
    would fail a handshake that is otherwise correct. Rare now rather than
    routine, and ``_bootstrap`` logs a warning when it happens, so an operator
    whose agent was never told the path can find out why.
    """
    head = _HEAD_PROACTIVE if proactive_mode == "on" else _HEAD_REACTIVE_ONLY
    clause = _PROACTIVE_CLAUSE if proactive_mode == "on" else ""
    rendered = (head + clause + _REACTIVE_CLAUSES).format(
        annotation_tool_name=annotation_tool_name,
        # The back-reference only has a referent when the clause above it
        # rendered. Trailing space lives in the token so `off` leaves no
        # double space behind.
        again="again " if proactive_mode == "on" else "",
    )
    if len(rendered) > _INSTRUCTIONS_LENGTH_CAP:
        raise ValueError(
            f"Rendered instructions suffix is {len(rendered)} chars, which "
            f"exceeds the {_INSTRUCTIONS_LENGTH_CAP}-char safety cap "
            f"(Claude Code truncates at ~{_CLAUDE_CODE_TRUNCATION_CAP}). "
            f"Shorten annotation_tool_name."
        )
    # After the cap check: the sentence has a fixed length, so it comes out of
    # the headroom under the truncation point. Before the event-file line, so
    # truncation still takes that line first.
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


def build_annotation_tool_description(proactive_mode: str = "off") -> str:
    """Build the annotation tool's ``description`` field.

    ``proactive_mode="off"`` swaps the lead for the reactive-only one and
    drops every mention of a note from the field reference: a reports-only
    tool refuses a note, so its fields must not describe one.

    No vendor placeholder — the proxy is opaque to the wrapped server's
    identity, so the field reference uses neutral "this server" phrasing.
    """
    takes_notes = proactive_mode == "on"
    return _DEFAULT_ANNOTATION_TOOL_DESCRIPTION_TEMPLATE.format(
        lead=_ANNOTATION_LEAD_PROACTIVE if takes_notes else _ANNOTATION_LEAD_REACTIVE_ONLY,
        omit_on_a_note=" — omit on a proactive annotation" if takes_notes else "",
        report_only="reactive-only — omit on a proactive. " if takes_notes else "",
    )


# Per-tool injected params. Unlike instructions, this text rides IN each tool's
# schema, so it is in front of the model at call-compose time on every client —
# including Claude Desktop, which ignores initialize-instructions entirely.
#
# Vendor-neutral names, not `baton_*`: anything the customer's agent can see on
# an instrumented surface must speak the vendor's voice, never Baton's.
USER_GOAL_PARAM_NAME = "user_goal"
EXPECTED_RESULT_PARAM_NAME = "expected_result"
# The task label (wire field ``call_workflow``).
# Deliberately NOT named ``workflow``: injected params live inside vendor tool
# schemas, where ``workflow`` is a plausible real vendor param (Workfront
# approvals, CI pipelines, Notion automations) — a collision would make the
# strip swallow the vendor's own argument, and the name would invite the model
# to fill in the vendor object it is touching instead of the meta task label.
OVERALL_TASK_PARAM_NAME = "overall_task"
INTENT_PARAM_NAMES = (USER_GOAL_PARAM_NAME, EXPECTED_RESULT_PARAM_NAME, OVERALL_TASK_PARAM_NAME)

# The text below is byte-identical to baton-sdk's and baton-ts's. Each producer
# pins its own copy in its own suite (this suite ships in the Try Kit, where a
# clone has no siblings to read), so change all three together.
#
# The leading label has to track ``intent_param_mode``: under ``"required"``
# the injector appends the escalated params to the schema's advertised
# ``required`` list, so a description still opening "OPTIONAL." contradicts the
# schema it ships inside — the model reads both. Only the label moves; the body
# is byte-identical across modes, because that sentence is the measured text and
# the mode is not a licence to reword it.
_USER_GOAL_PARAM_BODY = (
    "One sentence: what the user is actually trying to accomplish "
    "with this call (their goal, not a restatement of the arguments)."
)

_EXPECTED_RESULT_PARAM_BODY = (
    "One sentence: what a successful result should look like, so a "
    "silent/thin failure can be told apart from success."
)

# Two parts with two jobs. The leading number says which user message the
# call answers, and the Console cuts a turn where it changes (SPEC §11.5.1
# rule 2). The label after the colon says which task, and it works ONLY if the
# model repeats it verbatim while the task is unchanged: user_goal and
# expected_result reword freely, so they cannot key grouping.
#
# Do not reword without scoring the result on live agents.
_OVERALL_TASK_PARAM_BODY = (
    "The number of the user's current message in this conversation "
    "(1 for the first), a colon, then a short stable label for the broader "
    "task this call serves (e.g. '3: prepare campaign approval'). Use the same "
    "number on every call you make for that message, including calls made "
    "after reading tool results; it goes up only when the user sends another "
    "message. REPEAT the exact same label text after the colon on every call "
    "serving the same task, across messages; change the label only when the "
    "user starts a different task."
)


def _labelled(body: str, intent_param_mode: str) -> str:
    return ("REQUIRED. " if intent_param_mode == "required" else "OPTIONAL. ") + body


def build_user_goal_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``user_goal`` param's ``description`` field.

    ``intent_param_mode="required"`` swaps the leading label for "REQUIRED.",
    matching the ``required`` entry the injector adds under that mode. The
    default is ``"optional"`` rather than the proxy's own ``required`` default
    so that omitting the argument cannot silently advertise a constraint the
    caller never asked for — every real call site passes the configured mode.
    """
    return _labelled(_USER_GOAL_PARAM_BODY, intent_param_mode)


def build_expected_result_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``expected_result`` param's ``description`` field;
    the mode moves the label as for ``user_goal``."""
    return _labelled(_EXPECTED_RESULT_PARAM_BODY, intent_param_mode)


def build_overall_task_param_description(*, intent_param_mode: str = "optional") -> str:
    """Build the injected ``overall_task`` param's ``description`` field;
    the mode moves the label as for ``user_goal``."""
    return _labelled(_OVERALL_TASK_PARAM_BODY, intent_param_mode)


def required_param_names(*, intent_param_mode: str) -> tuple[str, ...]:
    """Which injected params ``intent_param_mode`` advertises as required.

    Advertised, never enforced: a call that omits one is forwarded as it would
    be unwrapped. A tool that declares one of these names itself keeps its own
    schema for it.
    """
    return INTENT_PARAM_NAMES if intent_param_mode == "required" else ()

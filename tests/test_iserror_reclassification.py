"""A failed tools/call is a 200 with ``isError`` — and the proxy sits on the wire.

SPEC §6.1 / §11.4.3: MCP files a failed tool call as a **successful** JSON-RPC
response whose body sets the error flag; a JSON-RPC ``error`` member means a
protocol fault. A producer that classifies on the ``error`` member alone files
every tool failure as a success.

⚠ **For a WIRE sensor that is 100% of failures, not a subset.** Measured on a
real stdio round trip across `mcp` 1.27.2 / 2.2.0 and `fastmcp` 2.14.7 / 4.0.3
(hub: `docs/design-notes/iserror_sensor_probe.md`, the wire addendum): every
library converts a RAISED exception into a 200 carrying ``isError: true``
*before the bytes leave the process*. The in-process SDKs see the exception and
already file it correctly; the proxy never does. So on the wire there is ONE
failure shape, and both of the SPEC's two shapes arrive here identically.

That is why there is no "raise" case in this file with a different expected
payload — `test_a_raise_reaches_the_wire_as_iserror_too` asserts sameness, and
sameness is the finding.

**One spelling, and it is ``isError``.** The same probe measured that the snake
``is_error`` is a PYTHON ATTRIBUTE NAME from mcp 2.x's ``mcp_types`` rewrite,
never a wire field: MCP's schema is camelCase and every server serialises
``model_dump_json(by_alias=True)``. A wire sensor needs one spelling and is
version-proof. (A consumer reading *stored* events still needs both, because
baton-sdk dumps its own objects without ``by_alias`` — a different problem, in
a different repo.)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from baton_proxy.config import Config
from baton_proxy.emitter import Emitter
from baton_proxy.proxy import ANNOTATE_TOOL_NAME, MessageProcessor, _Injection
from baton_proxy.report import _render_trail

REASON = "You do not have sufficient access to delete this Project"
ENVELOPE: dict[str, Any] = {
    "content": [{"type": "text", "text": REASON}],
    "isError": True,
}


class _FakeEmitter:
    """Records enqueue calls in order; only the methods these paths use."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("enqueue_"):
            raise AttributeError(name)

        def record(**kwargs: Any) -> None:
            self.calls.append((name.removeprefix("enqueue_"), kwargs))

        return record

    def types(self) -> list[str]:
        return [t for t, _ in self.calls]

    def one(self, event_type: str) -> dict[str, Any]:
        matches = [kw for t, kw in self.calls if t == event_type]
        assert len(matches) == 1, f"{event_type}: expected 1, got {len(matches)} of {self.types()}"
        return matches[0]


def _processor() -> tuple[MessageProcessor, _FakeEmitter]:
    emitter = _FakeEmitter()
    injection = _Injection.create(None, intent_param_mode="optional", proactive_mode="off")
    return MessageProcessor(emitter, injection, "test-session"), emitter  # type: ignore[arg-type]


def _call(name: str, msg_id: int = 10, method: str = "tools/call") -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": {}}
    if method == "resources/read":
        params = {"uri": name}
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}


def _reply(result: Any, msg_id: int = 10) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


# --- the negative control, first ------------------------------------------


def test_baton_own_refusal_is_never_filed_as_a_vendor_failure() -> None:
    """⚠ The proxy synthesises ``isError: True`` for its OWN refusals.

    ``_refuse_unknown_signal`` and ``_refuse_proactive`` both answer the agent
    with a 200 carrying the flag — deliberately, because a JSON-RPC error reads
    as the server being broken and invites a retry loop. If either ever reached
    ``handle_server_message``, the detector added here would file Baton's own
    refusal as a vendor tool failure: fabricated friction, on the exact lane
    the proactive gate exists to keep clean.

    It cannot today — the injected branch answers with ``respond=`` and returns
    before the call is ever tracked as pending, so there is no server message
    to pair. That is a fact about control flow, which is precisely the kind of
    fact a later refactor breaks silently. Pinned rather than commented.
    """
    proc, emitter = _processor()
    action = proc.handle_client_message(
        {
            "jsonrpc": "2.0",
            "id": 77,
            "method": "tools/call",
            "params": {
                "name": ANNOTATE_TOOL_NAME,
                "arguments": {"signal_type": "not-a-real-signal", "context": "x"},
            },
        }
    )

    # The proxy answered it itself — nothing was forwarded upstream.
    assert action.respond is not None, "the refusal must not be forwarded to the vendor"
    assert action.respond["result"]["isError"] is True, "fixture no longer drives a refusal"
    assert "tool_call_error" not in emitter.types()
    assert "tool_call_start" not in emitter.types()


# --- the reclassification --------------------------------------------------


def test_a_returned_iserror_result_is_filed_as_a_failure() -> None:
    proc, emitter = _processor()
    proc.handle_client_message(_call("delete_project"))
    proc.handle_server_message(_reply(ENVELOPE))

    assert "tool_call_end" not in emitter.types(), "an isError 200 is not a successful call"
    err = emitter.one("tool_call_error")
    assert err["tool_name"] == "delete_project"
    # SPEC §11.4.3: the registered value for the returned shape, matching what
    # baton-extmcp has emitted since 0.1.0.
    assert err["error_type"] == "tool_error"
    # The reason, unwrapped out of `content` — not the JSON of the envelope.
    assert err["error_body"] == REASON
    # ...and the envelope kept WHOLE beside it, so reclassifying does not move
    # a structured body into a flat string.
    assert err["result"] == ENVELOPE


def test_a_raise_reaches_the_wire_as_iserror_too() -> None:
    """⚠ The finding, as a test: on the wire a raise is indistinguishable.

    Every MCP server converts a raised handler exception into a 200 carrying
    ``isError: true`` before serialising. So the proxy cannot tell the SPEC's
    RAISE shape from its RETURN shape, and must not try — it always emits the
    returned form. This is the case the in-process SDKs already handled and the
    proxy did not, and it is the bulk of real failures.
    """
    proc, emitter = _processor()
    raised = {
        "content": [{"type": "text", "text": "Error executing tool delete_project: boom"}],
        "isError": True,
    }
    proc.handle_client_message(_call("delete_project"))
    proc.handle_server_message(_reply(raised))

    err = emitter.one("tool_call_error")
    assert err["error_type"] == "tool_error"
    assert err["error_body"] == "Error executing tool delete_project: boom"
    assert err["result"] == raised


def test_a_jsonrpc_error_member_still_files_its_own_code() -> None:
    """The protocol-fault lane is untouched: its code is not ``tool_error``."""
    proc, emitter = _processor()
    proc.handle_client_message(_call("delete_project"))
    proc.handle_server_message(
        {"jsonrpc": "2.0", "id": 10, "error": {"code": -32602, "message": "bad params"}}
    )

    err = emitter.one("tool_call_error")
    assert err["error_type"] == "-32602"
    assert err["error_body"] == "bad params"
    assert err.get("result") is None, "a protocol fault has no result object"


# --- the guards ------------------------------------------------------------


def test_a_successful_call_is_still_an_end() -> None:
    """The floor. Without this the detector could fire on everything and pass."""
    proc, emitter = _processor()
    ok = {"content": [{"type": "text", "text": "done"}], "isError": False}
    proc.handle_client_message(_call("delete_project"))
    proc.handle_server_message(_reply(ok))

    assert "tool_call_error" not in emitter.types()
    assert emitter.one("tool_call_end")["result"] == ok


def test_detection_requires_a_list_valued_content() -> None:
    """SPEC §11.4.3 makes the ``content`` list a MUST, not a nicety.

    It excludes a caller holding a vendor's own return value unconverted, where
    an object carrying an error flag for its own unrelated reasons would read
    as a failed tool call. ⚠ baton-extmcp does NOT apply this clause
    (``servicer.py:319`` tests the flag alone) — a second way it is not the
    reference implementation the thread calls it.
    """
    proc, emitter = _processor()
    proc.handle_client_message(_call("delete_project"))
    proc.handle_server_message(_reply({"isError": True, "rows": 3}))

    assert "tool_call_error" not in emitter.types()
    assert emitter.one("tool_call_end")["result"] == {"isError": True, "rows": 3}


def test_the_flag_is_only_read_on_a_tool_call() -> None:
    """⚠ The kind gate, as a pin rather than a comment.

    ``_emit_call_end`` is shared by ``tool``, ``resource_read``,
    ``resource_list``, ``prompt_get`` and ``prompt_list``. Only a
    ``CallToolResult`` carries the flag; a resource body that happens to hold
    an ``isError`` key is the vendor's own data, and filing it as a failed tool
    call invents friction that never happened.

    ⚠ The body below is deliberately a tools/call-SHAPED one — ``content`` as a
    list, flag set — so it would satisfy ``is_error_result`` outright and only
    the kind gate can refuse it. The first version of this test used a
    realistic resource body (``contents``, plural), which the content clause
    rejected on its own: the test passed with the kind gate DELETED, proving
    nothing. Found by mutating the gate, not by reading the test.
    """
    proc, emitter = _processor()
    proc.handle_client_message(_call("file:///x", method="resources/read"))
    proc.handle_server_message(_reply(ENVELOPE))

    assert "tool_call_error" not in emitter.types()
    assert "resource_read_end" in emitter.types()


# --- and once through the real emitter, to the file a reader opens ---------


def test_the_envelope_survives_the_emitter_onto_the_wire(tmp_path: Path) -> None:
    """Byte-pinned on what lands in the sink, not on the object handed in.

    The fake above proves the processor calls the right method with the right
    arguments. It cannot prove ``result`` survives ``_enqueue`` — which is
    where the scrubber runs and where the payload is assembled — so a field
    accepted by the emitter and dropped from the payload would pass every test
    above. Read it back off a real ``FileSink`` instead.

    ⚠ **Nothing is truncated on the way, deliberately.** ``baton-extmcp`` cuts
    ``error_body`` at 2000 chars, and the obvious move is to match it. Do not:
    that cut runs BEFORE the scrubber, so a secret straddling the boundary
    reaches the scrubber as a fragment no pattern matches and the surviving
    half ships in the clear. That is the bug ``baton``'s `33581cb` review found
    in the SDK. The proxy caps nothing anywhere — ``tool_call_end`` carries
    whole results today — so passing the text whole is both the safe choice and
    the consistent one.
    """
    sink = tmp_path / "events.jsonl"
    emitter = Emitter(
        Config(
            session_id="iserror-session",
            event_sink=f"file://{sink}",
            tenant_id="t",
            api_key=None,
            consent_token="ct_test",
            vendor_id="v",
            log_file=None,
        )
    )
    emitter.start()
    proc = MessageProcessor(
        emitter, _Injection.create(None, intent_param_mode="optional", proactive_mode="off"), "s"
    )
    proc.handle_client_message(_call("delete_project"))
    proc.handle_server_message(_reply(ENVELOPE))
    emitter.stop()

    events = [json.loads(line) for line in sink.read_text().splitlines()]
    errors = [e for e in events if e["event_type"] == "tool_call_error"]
    assert len(errors) == 1, [e["event_type"] for e in events]
    payload = errors[0]["payload"]
    assert payload["error_type"] == "tool_error"
    assert payload["error_body"] == REASON
    assert payload["result"] == ENVELOPE


def test_the_error_emitter_is_callable_without_a_result(tmp_path: Path) -> None:
    """⚠ `result` DEFAULTS, and making it required broke a downstream repo.

    ``Emitter`` is a shared library, not a private one: ``baton-extmcp`` imports
    it, depends on ``baton-proxy>=0.6.8`` with no upper pin, and calls this
    method with five kwargs and no ``result`` (``servicer.py:320``). A required
    keyword-only argument is a ``TypeError`` there on every tool failure.

    Nothing is deployed and there are no customers, so this is a sibling-repo
    build break, not an incident — the reason to hold the shape is that it
    costs nothing, not that anyone would be hurt. The strictness bought nothing
    in exchange either: ``_emit_call_error`` already defaults it, so no in-repo
    caller was ever forced to declare its shape.

    This test calls it exactly as extmcp does, so a re-tightening reddens here
    rather than in the sibling's suite.
    """
    sink = tmp_path / "events.jsonl"
    emitter = Emitter(
        Config(
            session_id="compat-session",
            event_sink=f"file://{sink}",
            tenant_id="t",
            api_key=None,
            consent_token="ct_test",
            vendor_id="v",
            log_file=None,
        )
    )
    emitter.start()
    emitter.enqueue_tool_call_error(
        tool_name="rm",
        error_type="tool_error",
        error_body="nope",
        duration_ms=3,
        session_id="s",
    )
    emitter.stop()

    event = json.loads(sink.read_text().splitlines()[-1])
    assert event["event_type"] == "tool_call_error"
    # Omitted, not nulled: §11.4.3 makes it optional, and an explicit null
    # asserts there was a body and it was empty about a call that had none.
    assert "result" not in event["payload"]


# --- what the reclassified failure looks like in the proxy's own report ----


def test_a_multiline_reason_stays_inside_its_list_item() -> None:
    """⚠ New exposure from the reclassification, on the line it is most visible.

    A reason used to come from JSON-RPC ``error.message``, effectively
    single-line. It now comes from a result's ``content`` text, which routinely
    is not — a fastmcp traceback, a multi-line validation message. Rendered raw
    into ``   - `tool` → **type**: {body}``, the second line lands at column 0,
    which ENDS the markdown list item and drops the rest of the trail out of
    the list.
    """
    steps = [
        {
            "intent": "delete it",
            "tool_calls": [
                {
                    "tool": "rm",
                    "status": "error",
                    "error_type": "tool_error",
                    "error_body": "Traceback:\n  line one\n  line two",
                }
            ],
        }
    ]
    lines = _render_trail(steps)
    body_lines = [ln for ln in lines if ln.startswith("   - ")]
    assert len(body_lines) == 1
    assert "\n" not in body_lines[0]
    assert "line one line two" in body_lines[0]
    # Every rendered line belongs to the list; none escaped to column 0.
    assert all(ln.startswith(("1.", "   - ")) for ln in lines), lines


def test_a_failure_with_no_message_says_so() -> None:
    """``error_text`` is empty when no content part carries text.

    An empty ``content``, or a server that puts the reason in
    ``structuredContent`` and returns an image or resource part. Rendered bare
    that is a colon with nothing after it, which reads as our rendering bug
    rather than as a vendor who said nothing.
    """
    steps = [
        {
            "intent": "delete it",
            "tool_calls": [
                {"tool": "rm", "status": "error", "error_type": "tool_error", "error_body": ""}
            ],
        }
    ]
    line = next(ln for ln in _render_trail(steps) if ln.startswith("   - "))
    assert not line.rstrip().endswith(":"), line
    assert "no message" in line

"""Reading MCP's error flag off a ``tools/call`` result, on the wire.

**A failed MCP tool call is a 200.** The protocol files it as a successful
JSON-RPC response whose ``CallToolResult`` body sets the error flag; a JSON-RPC
``error`` member means a protocol fault, not a tool failure. A producer that
classifies on the ``error`` member alone files tool failures as successes.
SPEC §6.1 and §11.4.3 (``baton/docs/SPEC.md``) carry the rule.

⚠ **One spelling here, and it is ``isError``.** SPEC §11.4.3's producer rule
asks an *in-process* sensor to duck-type both ``is_error`` and ``isError``,
because it holds a Python object and mcp 2.x's ``mcp_types`` rewrite renamed the
attribute. This module reads the WIRE, where that rename never happened: MCP's
schema is camelCase and every server serialises ``model_dump_json(by_alias=
True)``. Measured across `mcp` 1.27.2 / 2.2.0 and `fastmcp` 2.14.7 / 4.0.3 on a
real stdio round trip — seven observations, all ``isError`` — in the hub's
``docs/design-notes/iserror_sensor_probe.md``. So this is version-proof, not
under-specified. **Do not "fix" it by adding the snake spelling**: a wire body
that carries one did not come from an MCP server.

Lives in its own module rather than inside ``proxy.py`` because ``baton-extmcp``
sits on the same seam and depends on this package for its ``Emitter``. It
implements the check itself (``servicer.py:324``). Since the ``content`` clause
was dropped here on 2026-09-24 they agree on read level, spelling, and the
absence of that clause.

⚠ **They do NOT agree on the flag's form, and the difference is deliberate
here.** ``servicer.py:324`` tests ``result.get("isError")`` truthily; this
module tests ``is True``. So ``{"isError": "false"}`` — a string, which is
truthy — reads as a FAILURE over there and as a success here, and a truthy
read is how a non-conformant server's own word "false" becomes a fabricated
failure. MCP types the field as a boolean, so ``is True`` is the conformant
spelling and the one that cannot be talked into a false positive. ``error_body``
also still differs: extmcp JSON-dumps ``content`` and cuts at 2000 characters
BEFORE scrubbing, which ``error_text`` below explains is a defect.
"""

from __future__ import annotations

from typing import Any

# The registered ``error_type`` for a failure the tool RETURNED rather than
# raised (SPEC §11.4.3). `baton-extmcp` has emitted this value since 0.1.0;
# the console's shared error vocabulary already groups on it.
RETURNED_ERROR_TYPE = "tool_error"


def is_error_result(result: Any) -> bool:
    """True when a ``tools/call`` result body reports the call FAILED.

    ⚠ **No ``content`` clause, and it was REMOVED here on 2026-09-24 rather
    than never written.** SPEC §11.4.3 stated one as a universal MUST, and this
    module obeyed it. The rule was written for an in-process sensor holding a
    library-CONVERTED result, where an object carrying an error attribute for
    its own unrelated reasons can reach the predicate. Nothing of that shape
    exists on the wire: what arrives here is a decoded ``tools/call`` result
    body, so a top-level ``isError`` IS MCP's flag.

    **Measured, which is what settled it:** ``CallToolResult``'s own JSON
    schema lists ``content`` as REQUIRED with no default. A conformant server
    therefore always sends it, so the clause excluded nothing real — and a
    server that omits it had its failures filed as ``tool_call_end``, a success.
    A guard that cannot fire for a conformant peer and silently miscounts a
    non-conformant one is pure cost. SPEC §11.4.3 now scopes the MUST by
    vantage point.

    ⚠ **The kind gate at the call site is now the ONLY thing standing between
    a vendor's own ``isError`` key and a fabricated failure**, and it is enough
    because it is the right check: ``_emit_call_end`` is shared with the
    resource and prompt lanes, whose bodies are vendor data, and this predicate
    is reached only when ``call.kind == "tool"``. Do not delete that gate on
    the grounds that this function looks careful — it no longer is on its own.
    """
    return isinstance(result, dict) and result.get("isError") is True


def error_text(result: Any) -> str:
    """The human-readable reason, unwrapped from the result's ``content``.

    Joins the text parts, which is where a vendor puts the actual reason. An
    error result's ``structuredContent`` is typically null and the text lives
    here. Returns ``""`` when there is nothing to show, never None — the caller
    ships a string field either way, and the envelope rides alongside it, so a
    body this cannot read is not a body that was lost.

    ⚠ **Returns the text WHOLE — never truncate here, and not in the caller
    before the scrubber runs.** ``baton-extmcp`` cuts at 2000 characters, and
    copying that is the obvious move and the wrong one: the cut lands before
    PII scrubbing, so a secret straddling the boundary reaches the scrubber as
    a fragment no pattern matches and the surviving half ships unredacted.
    That is the defect `baton`'s `33581cb` code review found in the SDK. The
    proxy caps no payload anywhere — ``tool_call_end`` carries whole results —
    so whole is both the safe answer and the consistent one.
    """
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    # ⚠ The list check lives HERE, and it moved here on 2026-09-24 when
    # `is_error_result` stopped requiring one. That clause had been doing
    # double duty: classifying, and incidentally keeping a non-list out of
    # this loop. Dropping it left `{"isError": true, "content": 5}` raising
    # `TypeError` inside the caller's emit block, which swallows it — so the
    # call produced a `tool_call_start` and NO terminal event at all. An
    # orphaned start is worse than the miscount the drop removed, and it is
    # the pairing violation `proxy.py` calls a MUST NOT. Reading the reason
    # and deciding the outcome are different jobs; the shape check belongs
    # with the reading.
    if not isinstance(content, list):
        return ""
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
    ]
    return "\n".join(parts)

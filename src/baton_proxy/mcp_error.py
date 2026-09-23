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
sits on the same seam, depends on this package for its ``Emitter``, and
currently implements the check itself without the ``content`` clause below.
"""

from __future__ import annotations

from typing import Any

# The registered ``error_type`` for a failure the tool RETURNED rather than
# raised (SPEC §11.4.3). `baton-extmcp` has emitted this value since 0.1.0;
# the console's shared error vocabulary already groups on it.
RETURNED_ERROR_TYPE = "tool_error"


def is_error_result(result: Any) -> bool:
    """True when a ``tools/call`` result body reports the call FAILED.

    ⚠ The list-valued ``content`` clause is a MUST in SPEC §11.4.3, not a
    nicety. It excludes a caller that receives a vendor's return value
    unconverted, where an object carrying an error flag for its own unrelated
    reasons would otherwise read as a failed tool call.

    It is **not** what excludes a vendor dict spelling ``{"isError": true, …}``:
    under the conversion a server normally applies, that dict becomes a real
    ``CallToolResult`` whose flag is ``false``, and the flag settles it.
    """
    return (
        isinstance(result, dict)
        and result.get("isError") is True
        and isinstance(result.get("content"), list)
    )


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
    parts = [
        part["text"]
        for part in result.get("content") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
    ]
    return "\n".join(parts)

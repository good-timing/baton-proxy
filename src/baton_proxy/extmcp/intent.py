"""The two optional params the processor injects into every tool's inputSchema
on ``tools/list``, then captures + strips on ``tools/call``.

Names are deliberately VENDOR-NEUTRAL (``user_goal`` / ``expected_result``), not
``baton_*`` — anything the customer's agent can see on an instrumented surface
must speak the vendor's voice, never Baton's (white-label rule). Baton branding
lives only on the owner console. So these do NOT reuse baton-proxy's ``baton_intent``
injection surface; they are the neutral spike-proven names.

Clients fill injected optional params even when they ignore initialize
instructions, so this is the reliable prospective-intent channel.
"""

from __future__ import annotations

# The injected properties, keyed by param name → JSON Schema fragment.
INJECT_PROPS: dict[str, dict[str, str]] = {
    "user_goal": {
        "type": "string",
        "description": (
            "OPTIONAL. One sentence: what the user is actually trying to accomplish "
            "with this call (their goal, not a restatement of the arguments)."
        ),
    },
    "expected_result": {
        "type": "string",
        "description": (
            "OPTIONAL. One sentence: what a successful result should look like, so a "
            "silent/thin failure can be told apart from success."
        ),
    },
}

# The keys we capture off the arguments and strip before the backend sees them.
INJECT_KEYS: tuple[str, ...] = tuple(INJECT_PROPS)

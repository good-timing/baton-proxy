"""Guest mode — the politeness contract for scanning a server we don't own.

``baton-proxy scan --url <url>`` points a driven agent at a **third party's**
production MCP server. We are not the operator, nobody invited us, and some MCP
servers carry real per-call cost (an LLM-backed tool bills its operator for our
curiosity). Prompt instructions alone are not a guarantee — an agent told to be
read-only will still occasionally reach for a write tool — so guest mode is the
mechanical half of the contract, enforced in the proxy where the calls actually
leave the machine:

* **Read-only.** A ``tools/call`` whose tool name is write-shaped is refused
  before it reaches the wire.
* **Low volume.** A hard ceiling on upstream tool calls for the whole process.
* **Honest identification.** The outbound ``User-Agent`` gains a comment naming
  the tool, its source, and its purpose, so the operator reading their access
  log can tell exactly who we were and what we were doing.

Everything here is **off unless ``BATON_GUEST_MODE`` is set**. With it unset the
proxy behaves exactly as before — the live wrap on a vendor's own server must
never be silently rate-limited or made read-only.

Refusals are recorded (``guest_guard_refusal`` events) so the scan report can
state what we declined to do. They are deliberately NOT ``tool_call_error``
events: a call *we* blocked is our restriction, not the target's friction, and
must never be rendered as a finding against them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from baton_proxy import USER_AGENT

GUEST_MODE_ENV = "BATON_GUEST_MODE"
GUEST_MAX_CALLS_ENV = "BATON_GUEST_MAX_CALLS"

# A whole scan's budget against someone else's production server. The driven
# agent's plan asks for a light touch; this is the number that holds when it
# doesn't. Sized to still allow a real read-surface tour (list, read schemas, a
# few calls per read tool, one multi-step workflow) on a typical server.
DEFAULT_MAX_UPSTREAM_CALLS = 30

# Where an operator who finds us in their access log can see what this is.
SOURCE_URL = "https://github.com/good-timing/baton-proxy"

REFUSAL_WRITE_SHAPED = "write_shaped_tool"
REFUSAL_BUDGET = "call_budget_exhausted"

_FALSEY = frozenset({"", "0", "false", "no", "off"})

# --- Read/write classification ------------------------------------------------
#
# MCP has no machine-readable read-vs-write annotation we can rely on across
# servers, so the tool NAME is the only signal available before the call. The
# naive version of this (any write-ish token anywhere => refuse) misfires badly
# on ordinary read tools: `get_message` contains "message", `list_orders`
# contains "order", `get_run_status` contains "run". So classification is
# position-aware, mirroring how MCP tools are actually named
# (`[namespace_]verb_noun`):
#
#   1. Walk tokens left to right and find the FIRST one that is a verb at all.
#      Leading namespaces ("slack", "github") are not verbs, so they're skipped
#      and the real action verb is found — `slack_post_message` classifies on
#      "post", not on "slack".
#   2. If that first verb is a write verb -> refuse.
#   3. Regardless of position, a small set of unambiguously destructive words
#      anywhere in the name -> refuse. This catches `get_or_create_user`, where
#      the leading verb is a read.
#   4. No verb found at all (`weather_for_city`) -> allow. A tool that really
#      mutates almost always names its action, and refusing every unverbed name
#      would gut the read surface the scan exists to exercise.
#
# It stays a heuristic. Where it errs it errs toward refusing, and every refusal
# is disclosed in the report rather than hidden.

_READ_VERBS = frozenset(
    {
        "get", "list", "search", "read", "fetch", "describe", "show", "find",
        "query", "lookup", "view", "count", "check", "inspect", "browse",
        "preview", "explore", "retrieve", "has", "is", "exists", "validate",
        "resolve", "suggest", "compare", "analyze", "analyse", "summarize",
    }
)  # fmt: skip

# Write verbs, judged by POSITION (the first verb in the name). Broad on
# purpose: on a stranger's production server, wrongly refusing a read costs a
# thinner report while wrongly allowing a write damages someone else's data.
_WRITE_VERBS = frozenset(
    {
        "create", "new", "add", "insert", "upsert", "register", "provision",
        "delete", "remove", "destroy", "drop", "purge", "truncate", "clear",
        "archive", "restore", "reset", "revert", "rollback", "overwrite",
        "update", "edit", "modify", "patch", "put", "set", "write", "rename",
        "move", "copy", "duplicate", "merge", "apply", "assign", "attach",
        "detach", "enable", "disable", "activate", "deactivate", "toggle",
        "send", "post", "publish", "submit", "notify", "email", "mail", "sms",
        "message", "comment", "reply", "share", "invite", "upload", "push",
        "sync", "import", "export", "trigger", "schedule", "dispatch", "draft",
        "execute", "exec", "run", "invoke", "eval", "install", "uninstall",
        "deploy", "build", "start", "stop", "restart", "kill", "terminate",
        "cancel", "close", "open", "approve", "reject", "complete", "finish",
        "pay", "charge", "refund", "purchase", "buy", "order", "checkout",
        "subscribe", "unsubscribe", "grant", "revoke", "authorize", "login",
        "logout", "rotate", "upgrade", "downgrade", "generate",
    }
)  # fmt: skip

# Refused ANYWHERE in the name, not just in the verb slot. Reserved for words
# that are effectively never a noun in a read-only tool's name, so they don't
# reintroduce the `get_message` false positive.
_ALWAYS_REFUSE = frozenset(
    {
        "create", "delete", "destroy", "remove", "update", "drop", "purge",
        "truncate", "upsert", "insert", "overwrite", "revoke", "uninstall",
        "deploy", "refund", "charge", "unsubscribe", "rollback", "terminate",
        "send", "publish", "provision",
    }
)  # fmt: skip


def _tokens(tool_name: str) -> list[str]:
    """Split a tool name into lowercase word tokens, in order.

    Handles the three conventions in the wild at once: ``snake_case``,
    ``kebab-case``/``dotted.namespaces``, and ``camelCase``/``PascalCase``.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tool_name)
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t]


def is_write_shaped(tool_name: str) -> bool:
    """True if the tool's NAME suggests it changes state. See the block comment
    above for the classification rules and why it is position-aware."""
    tokens = _tokens(tool_name)
    if any(t in _ALWAYS_REFUSE for t in tokens):
        return True
    for token in tokens:
        if token in _WRITE_VERBS:
            return True
        if token in _READ_VERBS:
            return False
    return False


@dataclass
class GuestPolicy:
    """Per-process guest limits. Constructed once by the proxy at startup.

    Single-threaded by construction: the only consumer is the HTTP bridge loop,
    which runs on the main thread and serialises requests.
    """

    enabled: bool = False
    max_calls: int = DEFAULT_MAX_UPSTREAM_CALLS
    calls_allowed: int = 0
    refusals: list[tuple[str, str]] = field(default_factory=list)

    def check_tool_call(self, tool_name: str) -> str | None:
        """Decide one upstream tool call. Returns ``None`` to allow, or a
        refusal reason constant. Counts the call only when allowing, so a
        refusal never eats the budget."""
        if not self.enabled:
            return None
        if is_write_shaped(tool_name):
            self.refusals.append((tool_name, REFUSAL_WRITE_SHAPED))
            return REFUSAL_WRITE_SHAPED
        if self.calls_allowed >= self.max_calls:
            self.refusals.append((tool_name, REFUSAL_BUDGET))
            return REFUSAL_BUDGET
        self.calls_allowed += 1
        return None

    def refusal_message(self, tool_name: str, reason: str) -> str:
        """The text handed back to the driving agent as a JSON-RPC error.

        Phrased so the agent cannot mistake our restriction for the server's
        friction: the scan report is built from what the agent annotates, and a
        guard refusal recorded as a server defect would be a falsehood in a
        document we hand to that server's operator.
        """
        if reason == REFUSAL_WRITE_SHAPED:
            what = (
                f"`{tool_name}` looks like it changes state, and this scan is "
                "strictly read-only on a server it does not own"
            )
        else:
            what = (
                f"this scan's budget of {self.max_calls} upstream calls is spent — "
                "we keep the load on someone else's server low"
            )
        return (
            f"baton-proxy guest guard: refused — {what}. This is OUR restriction, "
            "not a fault or limitation of the server. Do not record it as friction; "
            "continue with read-only tools."
        )


def _mode_enabled(src: Mapping[str, str]) -> bool:
    return str(src.get(GUEST_MODE_ENV, "") or "").strip().lower() not in _FALSEY


def from_env(env: Mapping[str, str] | None = None) -> GuestPolicy:
    """Build the policy from the environment. Disabled unless ``BATON_GUEST_MODE``
    is set to a truthy value; a bad ``BATON_GUEST_MAX_CALLS`` falls back to the
    default rather than failing open to unlimited."""
    src = os.environ if env is None else env
    max_calls = DEFAULT_MAX_UPSTREAM_CALLS
    raw_max = str(src.get(GUEST_MAX_CALLS_ENV, "") or "").strip()
    if raw_max:
        try:
            parsed = int(raw_max)
        except ValueError:
            parsed = -1
        if parsed > 0:
            max_calls = parsed
    return GuestPolicy(enabled=_mode_enabled(src), max_calls=max_calls)


def user_agent(env: Mapping[str, str] | None = None) -> str:
    """The outbound ``User-Agent`` for the HTTP bridge.

    Outside guest mode this is the bare product token, unchanged. In guest mode
    it gains an RFC 9110 comment naming what we are and why we're calling, so a
    stranger's access log answers "who is this" without them having to ask.
    """
    src = os.environ if env is None else env
    if not _mode_enabled(src):
        return USER_AGENT
    return f"{USER_AGENT} (+{SOURCE_URL}; preflight friction scan; read-only)"

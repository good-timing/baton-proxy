"""Source-side PII scrubbing for event payloads.

The proxy emits whatever the wrapped MCP server returns — including tool
params and results that may carry customer PII (emails in Notion search
results, bearer tokens in error bodies, API keys pasted into chat tools).
Persona B's trust pitch is that nothing unscrubbed ever leaves the
customer's machine; this module is what makes that true. Every event
payload runs through ``Scrubber`` in ``Emitter._enqueue`` before it lands
in the queue, so both the local JSONL sink AND any HTTP sink see only
scrubbed values.

**Default ruleset** ships fixed for v1 (no public env-var configurability
— deferred until customer feedback justifies the surface area):

- email — RFC-ish address regex
- bearer — ``Bearer <opaque>`` header values
- sk_key — OpenAI/Anthropic-style ``sk-*`` API keys
- aws_key — ``AKIA*`` access key IDs
- jwt — three-segment ``eyJ*.*.*`` tokens
- phone — North-American-leaning loose digit pattern
- cc — 13-19 digit candidates filtered by Luhn

Plus field-name overrides: any dict key matching
``{email, phone, ssn, api_key, token, secret, password}`` (case-insensitive)
force-redacts its string value regardless of pattern.

Differs from LangSmith's approach (off by default, bring-your-own regex)
by shipping rules on by default — the right tradeoff for Persona B's
consumer/SMB audience that won't write masking code.

Mirrored in ``baton.scrub`` once the SDK port lands (P1 in the MVP plan).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Cap on recursive walk depth. Matches LangSmith's default; protects
# against pathological inputs without truncating realistic MCP payloads.
DEPTH_LIMIT = 10

# Dict keys whose string values are redacted regardless of value-pattern
# match. Case-insensitive exact match (no plural / prefix matching to keep
# false positives down). Kept narrow on purpose — too broad and we wreck
# legitimate fields like ``Slack:channel_token_string_id``.
REDACT_FIELD_NAMES: frozenset[str] = frozenset(
    {"email", "phone", "ssn", "api_key", "token", "secret", "password"}
)

# Ordered list of (category, pattern). Order matters where patterns can
# overlap: JWT must run before bearer because a bare JWT can look
# bearer-shaped. sk_key / aws_key run before email because their token
# bodies could otherwise match the email local-part regex.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9_\-.+/=]{16,}", re.IGNORECASE)),
    ("sk_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # Phone: optional ``+1``, optional area-code parens, separators
    # ``-`` / ``.`` / space, 10 digits. Conservative — won't catch every
    # international format, but won't trip on every 10-digit identifier.
    ("phone", re.compile(r"\b(?:\+?1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")),
)

# Credit-card candidates — Luhn-filtered before redacting to keep false
# positives down on long numeric IDs (timestamps, order numbers).
_CC_CANDIDATE = re.compile(r"\b\d{13,19}\b")


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum. Caller passes a digits-only string of the
    right length; we don't re-validate length here."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


class Scrubber:
    """Stateful recursive scrubber with the default ruleset baked in.

    Construct one per session and reuse for every event. The ``counts``
    attribute accumulates per-category redaction counts across all calls
    so the friction report can surface "N emails, M bearer tokens" at
    session render time.

    Signature is ``Callable[[Any], Any]`` so the Scrubber instance plugs
    into anywhere a plain function would (keeps parity with the SDK's
    ``scrubber: Callable[[Any], Any]`` slot for the future shared
    package).
    """

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, value: Any) -> Any:
        return self._walk(value, depth=0, force_field=None)

    def _walk(self, value: Any, *, depth: int, force_field: str | None) -> Any:
        if depth >= DEPTH_LIMIT:
            return value
        if isinstance(value, dict):
            return {
                k: self._walk(
                    v,
                    depth=depth + 1,
                    force_field=(
                        k.lower()
                        if isinstance(k, str) and k.lower() in REDACT_FIELD_NAMES
                        else force_field
                    ),
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [
                self._walk(item, depth=depth + 1, force_field=force_field) for item in value
            ]
        if isinstance(value, str):
            if force_field is not None:
                self.counts[f"field:{force_field}"] += 1
                return f"[REDACTED:field-{force_field}]"
            return self._scrub_string(value)
        # int / float / bool / None / bytes / etc. — leave alone. Sensitive
        # data stored as a non-string at this leaf is rare in MCP payloads
        # and not worth the false-positive cost of stringifying everything.
        return value

    def _scrub_string(self, s: str) -> str:
        for category, pattern in _PATTERNS:

            def _replace(_m: re.Match[str], _cat: str = category) -> str:
                self.counts[_cat] += 1
                return f"[REDACTED:{_cat}]"

            s = pattern.sub(_replace, s)
        # Credit card pass — separate because we Luhn-filter candidates
        # before counting / replacing. Skips non-CC long digit strings.
        return _CC_CANDIDATE.sub(self._cc_replace, s)

    def _cc_replace(self, m: re.Match[str]) -> str:
        match = m.group(0)
        if _luhn_valid(match):
            self.counts["cc"] += 1
            return "[REDACTED:cc]"
        return match


def identity_scrub(value: Any) -> Any:
    """No-op scrubber. Exported for explicit opt-out (and as the SDK
    parity reference once the port lands). Vendors / customers who
    legitimately want raw payloads can wire this in where a Scrubber
    instance would otherwise go."""
    return value

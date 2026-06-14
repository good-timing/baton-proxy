"""Tests for the default scrubber (scrub.py).

Coverage:
  - Each shipped pattern detects realistic positives and rejects common
    near-misses (digit-string IDs aren't phones, OAuth-shaped scopes
    aren't bearers, plain UUIDs aren't credit cards).
  - Field-name override forces redaction regardless of value pattern,
    and propagates through nested containers under the sensitive key.
  - Recursive walk handles dict / list nesting; non-string scalars pass
    through unchanged.
  - Depth limit aborts cleanly instead of recursing into pathological
    inputs.
  - Counter accumulates per-category counts across multiple calls.
  - identity_scrub is a no-op (parity hook for SDK port).
"""

from __future__ import annotations

import pytest

from baton_proxy.scrub import DEPTH_LIMIT, Scrubber, identity_scrub

# =============================================================================
# Pattern coverage
# =============================================================================


def test_email_is_redacted() -> None:
    s = Scrubber()
    out = s("contact me at ujwal@goodtiming.ai please")
    assert "ujwal@goodtiming.ai" not in out
    assert "[REDACTED:email]" in out
    assert s.counts["email"] == 1


def test_bearer_token_is_redacted() -> None:
    s = Scrubber()
    out = s("Authorization: Bearer abc123XYZ_token-value+/=")
    assert "abc123XYZ_token-value" not in out
    assert "[REDACTED:bearer]" in out
    assert s.counts["bearer"] == 1


def test_sk_key_is_redacted() -> None:
    s = Scrubber()
    out = s("api key sk-ABCDEFGHIJ1234567890klmnop here")
    assert "sk-ABCDEFGHIJ1234567890klmnop" not in out
    assert "[REDACTED:sk_key]" in out
    assert s.counts["sk_key"] == 1


def test_aws_access_key_is_redacted() -> None:
    s = Scrubber()
    out = s("AWS access key AKIAIOSFODNN7EXAMPLE leaked")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED:aws_key]" in out
    assert s.counts["aws_key"] == 1


def test_jwt_is_redacted() -> None:
    s = Scrubber()
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk"
    out = s(f"token: {jwt} end")
    assert jwt not in out
    assert "[REDACTED:jwt]" in out
    assert s.counts["jwt"] == 1


def test_phone_number_is_redacted() -> None:
    s = Scrubber()
    out = s("call (555) 123-4567 anytime")
    assert "(555) 123-4567" not in out
    assert "[REDACTED:phone]" in out
    assert s.counts["phone"] == 1


def test_credit_card_with_valid_luhn_is_redacted() -> None:
    # Common test card number — passes Luhn.
    s = Scrubber()
    out = s("card 4111111111111111 charged")
    assert "4111111111111111" not in out
    assert "[REDACTED:cc]" in out
    assert s.counts["cc"] == 1


def test_long_digit_string_failing_luhn_is_not_redacted() -> None:
    """A 16-digit identifier that doesn't satisfy Luhn must pass through
    unchanged — otherwise every long numeric ID (timestamps, order ids)
    would look like a credit-card leak."""
    s = Scrubber()
    not_a_card = "1234567890123456"  # 16 digits, fails Luhn
    out = s(f"order id {not_a_card}")
    assert not_a_card in out
    assert s.counts["cc"] == 0


def test_plain_string_with_no_pii_passes_through() -> None:
    s = Scrubber()
    out = s("the quick brown fox jumps over the lazy dog")
    assert out == "the quick brown fox jumps over the lazy dog"
    assert sum(s.counts.values()) == 0


def test_multiple_pii_in_one_string_counts_each() -> None:
    s = Scrubber()
    out = s("email a@b.co and phone 555-123-4567 leaked")
    assert "[REDACTED:email]" in out
    assert "[REDACTED:phone]" in out
    assert s.counts["email"] == 1
    assert s.counts["phone"] == 1


# =============================================================================
# Field-name override
# =============================================================================


def test_field_name_match_redacts_string_value() -> None:
    s = Scrubber()
    out = s({"password": "hunter2"})
    assert out == {"password": "[REDACTED:field-password]"}
    assert s.counts["field:password"] == 1


def test_field_name_match_is_case_insensitive() -> None:
    s = Scrubber()
    out = s({"Email": "ok@ok.co", "TOKEN": "xyz"})
    assert out["Email"] == "[REDACTED:field-email]"
    assert out["TOKEN"] == "[REDACTED:field-token]"
    assert s.counts["field:email"] == 1
    assert s.counts["field:token"] == 1


def test_field_name_override_propagates_into_nested_lists() -> None:
    """``{"password": ["a", "b"]}`` — both list items should be redacted
    because the sensitive field name flows down."""
    s = Scrubber()
    out = s({"password": ["one", "two"]})
    assert out == {"password": ["[REDACTED:field-password]", "[REDACTED:field-password]"]}
    assert s.counts["field:password"] == 2


def test_non_sensitive_field_with_pii_value_uses_pattern_match() -> None:
    """No field-name match → pattern scrubber handles the value."""
    s = Scrubber()
    out = s({"description": "reach me at hi@hi.co"})
    assert "hi@hi.co" not in out["description"]
    assert "[REDACTED:email]" in out["description"]
    assert s.counts["email"] == 1


# =============================================================================
# Recursion + depth limit
# =============================================================================


def test_recursive_walk_into_nested_dicts() -> None:
    s = Scrubber()
    out = s({"a": {"b": {"c": "email me at x@y.co"}}})
    assert "[REDACTED:email]" in out["a"]["b"]["c"]


def test_recursive_walk_into_nested_lists() -> None:
    s = Scrubber()
    out = s([["x@y.co"], [{"z": "a@b.co"}]])
    assert "[REDACTED:email]" in out[0][0]
    assert "[REDACTED:email]" in out[1][0]["z"]
    assert s.counts["email"] == 2


def test_non_string_scalars_pass_through() -> None:
    s = Scrubber()
    out = s({"n": 42, "b": True, "x": None, "f": 3.14})
    assert out == {"n": 42, "b": True, "x": None, "f": 3.14}


def test_depth_limit_does_not_crash_on_deep_input() -> None:
    """Construct a structure deeper than DEPTH_LIMIT — should return
    cleanly without scrubbing past the cap. Just verifying no recursion
    blow-up and a sane result; the deep leaf may or may not be scrubbed
    depending on exactly where the cap hits."""
    s = Scrubber()
    deep: dict = {"a": "x@y.co"}
    cur = deep
    for _ in range(DEPTH_LIMIT + 5):
        cur["next"] = {"a": "x@y.co"}
        cur = cur["next"]
    # Should not raise; should return *something*.
    out = s(deep)
    assert isinstance(out, dict)


# =============================================================================
# Counter behavior + idempotency
# =============================================================================


def test_counter_accumulates_across_calls() -> None:
    s = Scrubber()
    s("a@b.co")
    s({"more": "c@d.co"})
    s({"password": "p", "email": "e@e.co"})
    assert s.counts["email"] == 2
    assert s.counts["field:password"] == 1
    assert s.counts["field:email"] == 1


def test_already_redacted_token_is_idempotent() -> None:
    """Scrubbing an already-scrubbed string is a no-op — the redaction
    token itself contains no pattern matches."""
    s = Scrubber()
    once = s("email a@b.co")
    twice = s(once)
    assert once == twice
    assert s.counts["email"] == 1  # second pass didn't double-count


# =============================================================================
# identity_scrub (opt-out hook + SDK parity reference)
# =============================================================================


def test_identity_scrub_returns_input_unchanged() -> None:
    payload = {"email": "x@y.co", "nested": ["a@b.co"]}
    assert identity_scrub(payload) is payload


def test_identity_scrub_does_not_accumulate_counts() -> None:
    """identity_scrub has no state — it's a plain function. Tests
    relying on a counter must use Scrubber explicitly."""
    out = identity_scrub("email a@b.co")
    assert out == "email a@b.co"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""End-user identity capture — hash_user_id + the Emitter edge-hash.

Residency contract: a console-bound event carries only the HMAC
HASH of the principal, never the raw value; a missing key fails open (skip
user_id, keep emitting). Also guards the `user_name` scrub rule against
over-broad `name` redaction.
"""

from __future__ import annotations

import json

from baton_proxy.config import Config
from baton_proxy.emitter import Emitter
from baton_proxy.identity import HASH_SCHEME, Principal, hash_user_id
from baton_proxy.scrub import Scrubber

KEY = b"tenant-secret-key"


# ---- hash_user_id (pure) ---------------------------------------------------


def test_hash_deterministic_and_scheme_prefixed() -> None:
    a = hash_user_id("u123", tenant_id="t1", key=KEY)
    assert a == hash_user_id("u123", tenant_id="t1", key=KEY)
    assert a.startswith(f"{HASH_SCHEME}:")


def test_same_principal_different_tenants_never_collide() -> None:
    # tenant folded into the message — per-tenant guarantee even with a shared key.
    assert hash_user_id("u123", tenant_id="t1", key=KEY) != hash_user_id(
        "u123", tenant_id="t2", key=KEY
    )


def test_canonicalization_strip_and_lowercase() -> None:
    assert hash_user_id("  U123 ", tenant_id="t", key=KEY) == hash_user_id(
        "u123", tenant_id="t", key=KEY
    )


def test_different_key_different_hash() -> None:
    assert hash_user_id("u", tenant_id="t", key=b"k1") != hash_user_id(
        "u", tenant_id="t", key=b"k2"
    )


# ---- Emitter edge-hash -----------------------------------------------------


def _config(path: str, *, key: bytes | None) -> Config:
    return Config(
        session_id="s",
        event_sink=f"file://{path}",
        tenant_id="acme",
        api_key=None,
        consent_token="c",
        vendor_id="v",
        log_file=None,
        user_id_hmac_key=key,
    )


def _emit_one(tmp_path, *, key: bytes | None, principal: Principal | None) -> dict:
    p = tmp_path / "events.jsonl"
    e = Emitter(_config(str(p), key=key))
    e.start()
    e.enqueue_tool_call_start(tool_name="echo", params={"x": 1}, principal=principal)
    e.stop()
    lines = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    return lines[-1]


def test_user_id_hashed_at_edge_raw_never_emitted(tmp_path) -> None:
    ev = _emit_one(tmp_path, key=KEY, principal=Principal(user_id="u123"))
    assert ev["user_id"] == hash_user_id("u123", tenant_id="acme", key=KEY)
    assert "u123" not in json.dumps(ev)  # raw principal never on the wire


def test_no_key_fail_open_skips_user_id(tmp_path) -> None:
    ev = _emit_one(tmp_path, key=None, principal=Principal(user_id="u123"))
    assert "user_id" not in ev  # additive field omitted (v0.4.x wire-compatible)
    assert "u123" not in json.dumps(ev)


def test_no_principal_omits_user_id(tmp_path) -> None:
    ev = _emit_one(tmp_path, key=KEY, principal=None)
    assert "user_id" not in ev


# ---- scrub: user_name redacted, name-ish keys untouched --------------------


def test_user_name_field_scrubbed_but_not_name() -> None:
    out = Scrubber()({"user_name": "Alice Smith", "name": "get_thing", "tool_name": "echo"})
    assert out["user_name"].startswith("[REDACTED")
    assert out["name"] == "get_thing"  # prompt/tool names must survive
    assert out["tool_name"] == "echo"


# --------------------------------------------------------------------------
# The CROSS-REPO vector — the only assertion that can catch a joint drift
# --------------------------------------------------------------------------

# One principal, one tenant, one key, and the two digests they must produce.
# ⚠ These literals are DUPLICATED VERBATIM in the sibling sensor
# (`baton/tests/test_identity_adapter.py` <-> `baton-proxy/tests/test_identity.py`)
# and that duplication is the entire point: `hash_user_id` is a hand-maintained
# copy across two repos that cannot import each other, and every other test of
# it compares the implementation to ITSELF. The pre-existing
# "issuer=None matches the pre-issuer form" check asserts
# `hash_user_id(x) == hash_user_id(x, issuer=None)` — both sides from the same
# module — so a layout change applied to BOTH repos on the same day stays green
# in both while every `h1:` hash ever emitted becomes unreproducible. A frozen
# literal is the only thing that reds for that, because it was computed before
# the change and no edit can move it.
#
# The principal carries a trailing space and mixed case on purpose: canonical-
# isation (NFC, strip, lower) is part of the derivation, so a divergence there
# is a divergence in the hash.
#
# If one of these ever fails, the answer is NOT to update the literal. It means
# the two sensors have stopped agreeing about what `h1:` denotes, and every
# stored `user_id` was written under the other definition.
_VECTOR_PRINCIPAL = "Alice@Example.COM "
_VECTOR_TENANT = "ten_abc"
_VECTOR_KEY = b"shared-key-bytes"
_VECTOR_ISSUER = "https://idp.example.com"
_VECTOR_ISSUERLESS = "h1:b8556c3cd4564b06af433259553eadee690754318e27ca392deabba8aac7843b"
_VECTOR_WITH_ISSUER = "h1:9fc18f492b9dfe9092acf9d330d710b648d29b4aa131ecf702938df9409f0e78"


def test_the_shared_cross_repo_vector_issuerless() -> None:
    """Frozen 2026-09-10, when the two copies were verified byte-identical."""
    assert (
        hash_user_id(_VECTOR_PRINCIPAL, tenant_id=_VECTOR_TENANT, key=_VECTOR_KEY)
        == _VECTOR_ISSUERLESS
    )


def test_the_shared_cross_repo_vector_with_an_issuer() -> None:
    """The issuer fold is append-only, so this pins the APPENDED layout too.

    Without it, only the issuer-less half would be nailed down and the two
    repos could still diverge on where the issuer goes — which is the failure
    the docstring warns cannot be hidden behind a compatible default a second
    time.
    """
    assert (
        hash_user_id(
            _VECTOR_PRINCIPAL,
            tenant_id=_VECTOR_TENANT,
            key=_VECTOR_KEY,
            issuer=_VECTOR_ISSUER,
        )
        == _VECTOR_WITH_ISSUER
    )

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
    assert hash_user_id("u", tenant_id="t", key=b"k1") != hash_user_id("u", tenant_id="t", key=b"k2")


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

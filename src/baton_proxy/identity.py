"""Principal identity — resolve a raw principal, hash it at the edge.

Baton attaches the resolved principal (``principal``) to every event so the
Console can group by ``(tenant_id, vendor_id, principal.id)``, at whatever grain
the resolver named: a person, a service account or an organisation.

Residency contract: the Console DB is metadata-only and may only ever see the
HASH — raw identity must never leave the capture edge. So hashing happens HERE,
before an event reaches any console-bound sink. The raw principal does not
survive past ``Emitter._enqueue``.

Two pieces:

- ``hash_principal_id`` — the per-tenant HMAC. Reachable by both the proxy and the
  gRPC gateway processor (which depends on this package for the shared core).
  Zero new deps (stdlib ``hmac``/``hashlib``/``unicodedata``).
- ``IdentityResolver`` / ``Principal`` — the per-modality seam. Each capture
  modality (gateway headers, host-app callback, stdio env, transport-lib hook)
  ships a resolver that turns its native carrier into a ``Principal``; the
  core only ever receives the raw principal and hashes it. New modalities plug
  in with zero core change.

Mirrored in ``baton.identity`` (SDK) — keep the two copies in lockstep until
the shared package lands (same discipline as ``scrub.py``). ⚠ ``PRINCIPAL_SOURCE``
and ``PRINCIPAL_FORM`` are the EXCEPTION and have no SDK twin: neither is a
constant there, because the SDK has a raw mode and an attested path. Do not
"restore lockstep" by adding them.
"""

from __future__ import annotations

import hmac
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

# Scheme tag prefixed onto every hash. It names the HMAC KEY GENERATION and
# NOTHING ELSE (SPEC §11.4). This is the ROTATION seam: rotate the HMAC key by
# cutting new hashes to ``h2:`` while historical events stay under ``h1:``, so a
# consumer comparing two values knows they are incomparable rather than two
# people. A single principal produces different hashes across the rotation
# boundary — an accepted, documented discontinuity (the raw value was never
# stored, so it can't be re-hashed).
#
# ⚠ **It is not a provenance marker and not a classifier.** Both facts now ride
# ``_PrincipalWire.source`` and ``.form``, which exist in every derivation mode —
# something a tag cannot do, because ``"raw"`` emits none. The SDK's
# ``VENDOR_HASH_SCHEME = "v1"`` was RETIRED for exactly that reason (SPEC §13).
# **Do not reintroduce a provenance tag here** — a second meaning on this prefix
# is the joining that change undid.
HASH_SCHEME = "h1"

# What this package can truthfully say about a principal's provenance, and it is
# one value: **nothing in this stack verifies an identity.** ``baton-proxy``
# resolves none of its own, and its one real caller — ``baton-extmcp`` — reads a
# gateway header (``x-gw-ims-user-id``) that no verifier in the producing stack
# checked. SPEC §13 names both producers and requires ``"asserted"`` here.
#
# ⚠ **This is a CORRECTION, not a downgrade.** Under the retired encoding every
# proxy and extmcp event stamped ``h1:``, which the old rule read as attested —
# a claim that never happened. Principals that read as verified stop reading as
# verified, with no change in the underlying identity.
#
# ⚠ **A constant binds every embedder of ``IdentityResolver``, not just the
# in-house one** — that Protocol is public, so an embedder that genuinely DOES
# verify a token cannot say so, and its principal understates. That is the
# fail-safe direction (SPEC §11.4: trust only where the value is exactly
# ``"attested"``), and it is the trade taken deliberately: a parameter would
# invite every caller to assert an attestation it cannot make, which is the
# mislabel this removes. The day a verifier exists — here or in an embedder that
# can be checked — provenance stops being a constant and becomes something a
# resolver states.
PRINCIPAL_SOURCE = "asserted"

# What this package does to a principal before emitting it, and it is likewise
# one value: it always hashes. There is no ``"raw"`` mode here — the SDK has
# ``principal_id_mode``, the proxy has only ``principal_id_hmac_key``, and with
# no key the principal is dropped WHOLE rather than emitted verbatim. ``form``
# is the only thing a consumer may classify on (SPEC §11.4), so it is derived
# from what actually ran, never passed in.
PRINCIPAL_FORM = "hashed"


@dataclass(frozen=True)
class Principal:
    """A resolved principal, RAW (pre-hash).

    ⚠ **Not the wire shape, and the two are easy to confuse.** This is what a
    resolver HANDS US — a raw subject plus optional PII. What goes on the wire
    is ``emitter._PrincipalWire``: ``{id, source, form}``, built after hashing,
    by which point the raw value is gone. One is the question, the other is the
    answer, and nothing here is ever emitted.

    Only ``principal_id`` becomes the wire value today. ``user_name`` / ``user_data``
    are PII confined to the customer-owned payload tier (S3) — they are NOT
    emitted to the console path today and are force-scrubbed out of payloads
    (see scrub ``REDACT_FIELD_NAMES``). They exist here so a resolver can carry
    them once the split-sink payload tier lands, without a shape change.
    """

    principal_id: str
    user_name: str | None = None
    user_data: dict[str, Any] | None = None


class IdentityResolver(Protocol):
    """Turns a modality-native carrier (gRPC headers / FastMCP context /
    host-app callback / process env) into a ``Principal``. Returns ``None`` when
    no identity is available — the core then omits ``principal`` WHOLE
    (fail-open)."""

    def resolve(self, carrier: Any) -> Principal | None: ...


def _canonicalize(raw_principal: str) -> str:
    """Pin the principal string once, centrally, so every modality hashes an
    identical value — else the same human hashes differently per capture path
    and cross-modality cohorts break. NFC-normalize, strip, lowercase."""
    return unicodedata.normalize("NFC", raw_principal).strip().lower()


def hash_principal_id(
    raw_principal: str, *, tenant_id: str, key: bytes, issuer: str | None = None
) -> str:
    """HMAC-SHA256 a raw principal into a console-safe, per-tenant ``principal.id``.

    ``tenant_id`` is folded into the HMAC MESSAGE (not just the key) so the same
    principal under two tenants can never collide or be cross-tenant-correlated,
    even if an operator misconfigures one shared key — the per-tenant guarantee
    the residency contract requires. Returns ``"<scheme>:<hex>"`` (e.g. ``"h1:9f2c…"``).

    ``issuer`` — the OIDC ``iss`` claim — is folded in the same way when
    supplied, because a ``sub`` is unique only within the provider that minted
    it (RFC 7519 §4.1.2). Two identity providers behind one vendor can hand out
    the same ``sub`` to different people, and without the issuer those two
    people hash to one ``principal.id``.

    ⚠ **``issuer=None`` MUST hash byte-identically to the pre-issuer form**,
    and the append-only message layout below is what guarantees it. Every hash
    this package and the extmcp gateway have produced since 0.5.0 was
    issuer-less, and a format change under the same ``h1:`` tag would leave one
    derivation tag naming two different derivations across the sensor family —
    precisely what the scheme prefix exists to prevent. Do NOT change the
    layout a second time: the append-only shape is what makes ``None``
    compatible, and a second divergence would have no compatible default to
    hide behind. Pinned byte-for-byte by ``test_identity.py``'s shared vector,
    which the SDK asserts on the same values.

    Added 2026-09-10, closing the divergence this module's own header opened:
    ``baton.identity`` grew ``issuer`` on 2026-09-09 while the docstring above
    still promised the two copies were in lockstep.
    """
    message = f"{tenant_id}\x00{_canonicalize(raw_principal)}"
    if issuer is not None:
        message += f"\x00{_canonicalize(issuer)}"
    digest = hmac.new(key, message.encode(), sha256).hexdigest()
    return f"{HASH_SCHEME}:{digest}"

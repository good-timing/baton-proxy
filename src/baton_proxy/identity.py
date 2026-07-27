"""End-user identity — resolve a raw principal, hash it at the edge.

Baton attaches an end-user actor (``user_id``) to every event so the Console
can answer "which *customer* hit this" and group by
``(tenant_id, vendor_id, user_id)``.

Residency contract: the Console DB is metadata-only and may only ever see the
HASH — raw identity must never leave the capture edge. So hashing happens HERE,
before an event reaches any console-bound sink. The raw principal does not
survive past ``Emitter._enqueue``.

Two pieces:

- ``hash_user_id`` — the per-tenant HMAC. Reachable by both the proxy and the
  gRPC gateway processor (which depends on this package for the shared core).
  Zero new deps (stdlib ``hmac``/``hashlib``/``unicodedata``).
- ``IdentityResolver`` / ``Principal`` — the per-modality seam. Each capture
  modality (gateway headers, host-app callback, stdio env, transport-lib hook)
  ships a resolver that turns its native carrier into a ``Principal``; the
  core only ever receives the raw principal and hashes it. New modalities plug
  in with zero core change.

Mirrored in ``baton.identity`` (SDK) — keep the two copies in lockstep until
the shared package lands (same discipline as ``scrub.py``).
"""

from __future__ import annotations

import hmac
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

# Scheme tag prefixed onto every hash. This is the ROTATION seam: rotate the
# HMAC key by cutting new hashes to ``h2:`` while historical events stay under
# ``h1:``. A single principal produces different hashes across the rotation
# boundary — an accepted, documented discontinuity (the raw value was never
# stored, so it can't be re-hashed).
HASH_SCHEME = "h1"


@dataclass(frozen=True)
class Principal:
    """A resolved end-user identity, RAW (pre-hash).

    Only ``user_id`` is hashed onto the wire today. ``user_name`` / ``user_data``
    are PII confined to the customer-owned payload tier (S3) — they are NOT
    emitted to the console path today and are force-scrubbed out of payloads
    (see scrub ``REDACT_FIELD_NAMES``). They exist here so a resolver can carry
    them once the split-sink payload tier lands, without a shape change.
    """

    user_id: str
    user_name: str | None = None
    user_data: dict[str, Any] | None = None


class IdentityResolver(Protocol):
    """Turns a modality-native carrier (gRPC headers / FastMCP context /
    host-app callback / process env) into a ``Principal``. Returns ``None`` when
    no identity is available — the core then skips ``user_id`` (fail-open)."""

    def resolve(self, carrier: Any) -> Principal | None: ...


def _canonicalize(raw_principal: str) -> str:
    """Pin the principal string once, centrally, so every modality hashes an
    identical value — else the same human hashes differently per capture path
    and cross-modality cohorts break. NFC-normalize, strip, lowercase."""
    return unicodedata.normalize("NFC", raw_principal).strip().lower()


def hash_user_id(raw_principal: str, *, tenant_id: str, key: bytes) -> str:
    """HMAC-SHA256 a raw principal into a console-safe, per-tenant ``user_id``.

    ``tenant_id`` is folded into the HMAC MESSAGE (not just the key) so the same
    principal under two tenants can never collide or be cross-tenant-correlated,
    even if an operator misconfigures one shared key — the per-tenant guarantee
    the residency contract requires. Returns ``"<scheme>:<hex>"`` (e.g. ``"h1:9f2c…"``).
    """
    message = f"{tenant_id}\x00{_canonicalize(raw_principal)}".encode()
    digest = hmac.new(key, message, sha256).hexdigest()
    return f"{HASH_SCHEME}:{digest}"

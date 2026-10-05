"""Principal identity — what a resolver hands back.

Baton attaches the resolved principal (``principal``) to every event so the
Console can group by ``(tenant_id, vendor_id, principal.id)``, at whatever grain
the resolver named: a person, a service account or an organisation (SPEC
§11.4). The resolver decides everything about the value, including whether it
is hashed; the emitter sends it on as stated.

``IdentityResolver`` / ``Principal`` are the per-modality seam. Each capture
modality (gateway headers, host-app callback, stdio env, transport-lib hook)
ships a resolver that turns its native carrier into a ``Principal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

# Nothing in this stack verifies an identity, so every principal is the
# resolver's claim. A consumer trusts only exactly ``"attested"`` (SPEC §11.4).
PRINCIPAL_SOURCE = "asserted"

PRINCIPAL_FORM_RAW = "raw"
PRINCIPAL_FORM_HASHED = "hashed"
PRINCIPAL_FORMS = frozenset({PRINCIPAL_FORM_RAW, PRINCIPAL_FORM_HASHED})

# ``principal.id`` is external text copied onto every event of the call.
PRINCIPAL_ID_MAX_LEN = 128


@dataclass(frozen=True)
class Principal:
    """A resolved principal, as a resolver returns it.

    ``principal_id`` and ``form`` reach the wire as given; the wire shape is
    ``emitter._PrincipalWire``. ``user_name`` / ``user_data`` are PII confined
    to the customer-owned payload tier (S3) — they are NOT emitted to the
    console path today and are force-scrubbed out of payloads (see scrub
    ``REDACT_FIELD_NAMES``). They exist here so a resolver can carry them once
    the split-sink payload tier lands, without a shape change.
    """

    principal_id: str
    user_name: str | None = None
    user_data: dict[str, Any] | None = None
    form: Literal["raw", "hashed"] = "raw"
    """What ``principal_id`` is: ``"raw"``, a real identity, or ``"hashed"``,
    a pseudonym the resolver derived itself. A consumer treats anything not
    ``"hashed"`` as personal data, so leave it unset unless the resolver
    hashed the value."""


class IdentityResolver(Protocol):
    """Turns a modality-native carrier (gRPC headers / FastMCP context /
    host-app callback / process env) into a ``Principal``. Returns ``None`` when
    no identity is available — the core then omits ``principal`` WHOLE
    (fail-open)."""

    def resolve(self, carrier: Any) -> Principal | None: ...

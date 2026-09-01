"""Runtime configuration — read from environment variables once at startup.

Subprocess-wrap deployment is 1-process-per-user (Claude Desktop / Claude Code
spawns one proxy per MCP server entry), so a static per-process token model is
fine. Hosted-HTTP deployment will need a per-request resolver; not in scope here.

Zero-config defaults
--------------------

The proxy is meant to be install-and-play: add ``baton-proxy --`` in front of
any MCP server, restart, and you get a stream of friction events in
``/tmp/baton-proxy.jsonl`` (and on stderr). No env vars required. The
defaults are deliberately placeholder-flavoured (``"local"``) so that the
upgrade to a remote sink is forced to be explicit.

When ``BATON_EVENT_SINK`` resolves to an http(s):// sink, the emitter
refuses to start while ``BATON_CONSENT_TOKEN`` is still the placeholder —
placeholder-tagged events must never leak to a remote collector.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

# Zero-config defaults. Multi-sink (stderr + local file) so the events are
# immediately visible both as a live stream and as a persistent log; tenant
# and consent default to a sentinel ``"local"`` to make it obvious in any
# downstream system that the install hasn't been wired to a remote sink yet.
DEFAULT_EVENT_SINK = "stderr:,file:///tmp/baton-proxy.jsonl"
DEFAULT_TENANT_ID = "local"
DEFAULT_CONSENT_TOKEN = "local"
DEFAULT_TENANT_TYPE = "vendor"

# Valid values for BATON_TENANT_TYPE. ``vendor`` = production install
# wrapped on a customer's machine that ships signal to the vendor's
# Console; ``customer`` = end-user install where the same person owns
# both the proxy and the Console tenant ("Sentry for AI agents" shape).
# Different defaults follow: vendor mode hides the in-Claude report tool
# (Console renders reports server-side), customer mode keeps it.
_TENANT_TYPES: frozenset[str] = frozenset({"vendor", "customer"})

# Valid values for BATON_INTENT_PARAM — the per-tool goal-param injection
# mode. ``optional`` injects `user_goal`/`expected_result` as optional params
# on every upstream tool; ``required`` (default since 2026-09-01) additionally
# marks `user_goal` required in the schema (`expected_result` and
# ``overall_task`` stay optional even then); ``off`` disables injection
# entirely. Clients fill the params even when optional (Desktop, verified
# 2026-07-07), while ignoring initialize-instructions — so param injection is
# the reliable intent channel and instructions remain a best-effort extra.
#
# WHAT ``required`` MEANS HERE, because the word is doing unusual work
# (D7, 2026-09-01): **advertised as required, never enforced.** It appends
# `user_goal` to the schema's advertised `required` list and validates
# nothing; the param is stripped before the call is forwarded, so no call can
# fail for omitting it and the wrapped server never learns the difference.
# That is the only meaning compatible with what a wrapper promises — refusing
# a customer's call to collect a telemetry string would change how the wrapped
# server behaves, which is the one thing we say we do not do. baton-ts built a
# non-optional zod field on the vendor's real schema and DID refuse; it drops
# that to match.
#
# Which is why flipping the default is safe: an advertisement cannot break a
# call. The number it should move is 89% — 1,465 of 1,644 real customer calls
# carried `user_goal` under ``optional`` (workfront, proxy 0.5.2, Aug 11-14).
DEFAULT_INTENT_PARAM_MODE = "required"
_INTENT_PARAM_MODES: frozenset[str] = frozenset({"optional", "required", "off"})

# Valid values for BATON_PROACTIVE — whether the agent may FILE its own
# pre-call annotation. Ported from baton-sdk's ``VendorConfig.proactive_mode``
# (SPEC §13, 2026-08-10b) and §14's "proactive_mode parity across producers",
# whose ordering constraint — do not disable proactive before the
# ``overall_task`` port lands — cleared with `96f31db`.
#
# ONE DELIBERATE DIVERGENCE FROM THE SDK, and it is the whole reason to read
# this comment. The SDK's ``on`` renders a BEFORE clause into the instructions
# asking for that pre-call annotation. The proxy's ``on`` does NOT: D7
# (2026-09-01) drops the BEFORE paragraph in BOTH modes, because the injected
# params already carry the same three fields on every call and the paragraph
# taught the agent that intent was the tool's job. So here the knob governs
# only the two remaining legs:
#
#   on  (default, today's behaviour) — the annotation tool is described as
#       proactive-and-reactive, and the handler accepts an annotation with no
#       signal_type.
#   off — the tool is described as reactive-only, and the handler REFUSES an
#       annotation with no signal_type, the way the SDK does. Refusing here
#       rather than requiring signal_type in the schema keeps the agent from
#       fabricating a `failure` to get the call through, which would corrupt
#       the one signal worth protecting.
#
# What the knob never touches: the tool itself (suppressing it also lost the
# reactive `feature_gap`, the product signal), and the proxy's OWN synthesised
# proactive built from the first call's injected params — that one is ours, not
# the agent's, and it is what keeps one turn-opener per session in both modes.
DEFAULT_PROACTIVE_MODE = "on"
_PROACTIVE_MODES: frozenset[str] = frozenset({"on", "off"})


@dataclass(frozen=True)
class Config:
    """All runtime knobs. Created via Config.from_env()."""

    # Process-lifetime session identifier per SPEC §11.4. Every event the proxy
    # emits during this process shares this session_id.
    session_id: str

    # Where emitted events go. A URL whose scheme selects the sink:
    #   https://collector.example.com  -> HTTP POST to {url}/v0/events
    #   file:///tmp/events.jsonl     -> append-JSONL to the local path
    #   stderr:                      -> JSONL to stderr
    # Comma-separated values fan out (MultiSink). Defaults via from_env() to
    # ``DEFAULT_EVENT_SINK`` (stderr + local file). None disables emission —
    # only test code sets this to None directly; ``from_env()`` always
    # returns a populated value.
    event_sink: str | None
    tenant_id: str | None
    # Only required for http(s) sinks; ignored for file/stderr sinks. The
    # HTTP sink raises at startup if event_sink is http(s):// and this is None.
    api_key: str | None
    consent_token: str | None

    # Vendor identifier surfaced in proxy logs and used by the console to
    # bucket friction signal per wrapped MCP server. Required at startup
    # so every event carries a meaningful vendor label — without it the
    # customer-mode dashboard can't render its cross-vendor view.
    vendor_id: str

    # Where the proxy writes its own operational log. Stderr by default;
    # override with BATON_PROXY_LOG_FILE for persistent debugging.
    log_file: str | None

    # Which Baton tenant shape this proxy is wired to: ``vendor`` (default)
    # ships signal to the wrapped MCP server's vendor Console; ``customer``
    # ships to the end-user's own Baton tenant. Controls whether the
    # in-Claude ``baton_session_report`` tool is injected when an HTTP sink
    # is configured — vendor mode hides it (Console renders reports
    # server-side); customer mode keeps it. Defaulted here so tests that
    # construct Config directly don't need to spell it out; from_env()
    # always populates it explicitly from BATON_TENANT_TYPE.
    tenant_type: str = DEFAULT_TENANT_TYPE

    # Per-tool intent-param injection mode: optional | required | off.
    # See DEFAULT_INTENT_PARAM_MODE above.
    intent_param_mode: str = DEFAULT_INTENT_PARAM_MODE

    # Whether the agent may file its own pre-call annotation: on | off.
    # See DEFAULT_PROACTIVE_MODE above.
    proactive_mode: str = DEFAULT_PROACTIVE_MODE

    # Per-tenant secret keying the end-user ``user_id`` HMAC (identity.py).
    # Raw identity is hashed at the edge with this key before an event reaches
    # any console-bound sink (residency contract). None → user_id capture is
    # fail-open-skipped (events still emit, just without the actor field);
    # user_id is additive analytics, never a consent/authz gate. Populated from
    # BATON_USER_ID_HMAC_KEY (raw UTF-8 secret) by from_env().
    user_id_hmac_key: bytes | None = None

    @property
    def emission_enabled(self) -> bool:
        """True when the envelope-essential fields are populated. With
        ``from_env()`` defaults this is always True; only test code that
        passes ``event_sink=None`` etc. directly will see False."""
        return all(v is not None for v in (self.event_sink, self.tenant_id, self.consent_token))

    @property
    def using_placeholder_consent(self) -> bool:
        """True when consent_token is still the install-time placeholder.
        Emitter refuses to start an http(s) sink while this is True — a
        placeholder consent token must never reach a remote collector."""
        return self.consent_token == DEFAULT_CONSENT_TOKEN

    @classmethod
    def from_env(cls) -> Config:
        vendor_id = _env("BATON_VENDOR_ID")
        if not vendor_id:
            raise ValueError(
                "BATON_VENDOR_ID is required — set it to the wrapped MCP "
                "server's vendor identifier (e.g., 'notion', 'github', "
                "'slack'). The console uses this to bucket friction signal "
                "by vendor; it also labels events in the local JSONL stream."
            )
        tenant_type = _env("BATON_TENANT_TYPE") or DEFAULT_TENANT_TYPE
        if tenant_type not in _TENANT_TYPES:
            raise ValueError(
                f"BATON_TENANT_TYPE must be one of {sorted(_TENANT_TYPES)}; got {tenant_type!r}."
            )
        intent_param_mode = _env("BATON_INTENT_PARAM") or DEFAULT_INTENT_PARAM_MODE
        if intent_param_mode not in _INTENT_PARAM_MODES:
            raise ValueError(
                f"BATON_INTENT_PARAM must be one of {sorted(_INTENT_PARAM_MODES)}; "
                f"got {intent_param_mode!r}."
            )
        proactive_mode = _env("BATON_PROACTIVE") or DEFAULT_PROACTIVE_MODE
        if proactive_mode not in _PROACTIVE_MODES:
            raise ValueError(
                f"BATON_PROACTIVE must be one of {sorted(_PROACTIVE_MODES)}; "
                f"got {proactive_mode!r}."
            )
        # Ported from baton-sdk's ``_config.py`` guard. Both channels off is
        # not a quiet configuration, it is capture switched off: the params are
        # the intent channel and the annotation tool is the friction channel,
        # and a proxy with neither is a passthrough that emits tool calls with
        # no reason attached. Refuse at startup rather than run empty.
        if intent_param_mode == "off" and proactive_mode == "off":
            raise ValueError(
                "BATON_INTENT_PARAM='off' with BATON_PROACTIVE='off' captures no "
                "intent at all — the injected params are the intent channel and "
                "the annotation tool is the friction channel. Set one of them: "
                "BATON_INTENT_PARAM=optional|required (per-call intent, no extra "
                "turn) or BATON_PROACTIVE=on (agent-filed pre-call annotations)."
            )
        hmac_key = _env("BATON_USER_ID_HMAC_KEY")
        return cls(
            session_id=str(uuid.uuid4()),
            event_sink=_env("BATON_EVENT_SINK") or DEFAULT_EVENT_SINK,
            tenant_id=_env("BATON_TENANT_ID") or DEFAULT_TENANT_ID,
            api_key=_env("BATON_API_KEY"),
            consent_token=_env("BATON_CONSENT_TOKEN") or DEFAULT_CONSENT_TOKEN,
            vendor_id=vendor_id,
            tenant_type=tenant_type,
            intent_param_mode=intent_param_mode,
            proactive_mode=proactive_mode,
            user_id_hmac_key=hmac_key.encode("utf-8") if hmac_key else None,
            log_file=_env("BATON_PROXY_LOG_FILE"),
        )


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None

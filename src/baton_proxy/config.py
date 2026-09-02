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
# ``overall_task`` stay optional even then). **There is no ``off``** — the
# params ALWAYS ride (2026-09-01): they are the intent channel, they are
# stripped before the call is forwarded, and the way to stop them is to stop
# wrapping the server. Clients fill the params even when optional (Desktop, verified
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
# ``off`` is NOT here. It was accepted until 2026-09-01 and is now coerced away
# in ``from_env`` with a warning rather than rejected — see there for why the
# retirement is not allowed to stop a wrapped server from starting.
_INTENT_PARAM_MODES: frozenset[str] = frozenset({"optional", "required"})

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
#   on  — the instructions ask for a pre-call annotation, the tool is described
#       as proactive-and-reactive, and the handler accepts an annotation with
#       no signal_type.
#   off (default since 2026-09-01) — no pre-call request, the tool is described
#       as reactive-only, and the handler REFUSES an annotation with no
#       signal_type, the way the SDK does. Refusing there rather than requiring
#       signal_type in the schema keeps the agent from fabricating a `failure`
#       to get the call through, which would corrupt the one signal worth
#       protecting.
#
# REACTIVE IS UNCONDITIONAL. The AFTER/IF clauses are byte-identical in both
# modes, the tool is always registered, and an annotation carrying a
# signal_type is always accepted. `off` costs the agent's pre-call narration
# and nothing else — suppressing the tool itself also lost the reactive
# `feature_gap`, which is the product.
#
# What the knob never touches: the tool itself, and the proxy's OWN synthesised
# proactive built from the first call's injected params — that one is ours, not
# the agent's, and it is what keeps one turn-opener per session in both modes.
#
# WHY THE DEFAULT IS `off` (2026-09-01, after the D7 verification run — it
# shipped `on` earlier the same day and flipped on evidence):
#
#   1. The pre-call request COSTS the optional params. Measured live: with the
#      paragraph rendered the agent stated its expectation once in the
#      annotation and then omitted `expected_result` from all 5 calls; without
#      it, 3/3 calls carried it. `user_goal` held at 8/8 either way because the
#      schema advertises it as required. So the paragraph does not add intent,
#      it moves it out of the per-call record.
#   2. It costs 277 of 1,236 chars against Claude Code's ~2,087 cap on a field
#      the proxy APPENDS to — on a 100-tool enterprise server (workfront
#      rendered 4,408) our framing is what gets silently cut.
#   3. It costs an approval INSIDE the work window. The first human-led try-kit
#      run hit a permission prompt for `baton_annotate` mid-task, in the one
#      session where nothing mentions Baton; one denial and the whole intent
#      layer looked empty. Under `off` the tool is only reached on friction.
#   4. It did not buy friction: across three real sessions with the paragraph
#      rendered, every annotation was pre-call and ZERO friction signals were
#      filed.
#
# The cost, and it is real: agent-authored proactives supply ~54% of turn
# boundaries (SPEC §11.5 tier 2), so sessions collapse toward one turn until a
# correlator keys on `call_workflow` instead. That field is now present on
# 8 of 8 real calls, so the replacement signal is flowing before the boundary
# it replaces is removed.
DEFAULT_PROACTIVE_MODE = "off"
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

    # Warnings raised while READING the environment, held here instead of
    # logged. ``from_env`` runs BEFORE ``_configure_logging`` (proxy.py
    # ``_bootstrap``), so anything logged from it goes out through
    # ``logging.lastResort``: stderr only, unformatted, and never teed to
    # BATON_PROXY_LOG_FILE. That silently voided the one guarantee the
    # coerce-don't-raise decision rests on — the operator is told. The
    # bootstrap drains this after logging is configured.
    #
    # A test cannot catch the original bug with ``caplog``, which attaches its
    # own handler and so passes whether or not a real one exists; the pin is on
    # this field being populated and the bootstrap emitting it.
    startup_warnings: tuple[str, ...] = ()

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
        warnings: list[str] = []
        intent_param_mode = _env("BATON_INTENT_PARAM") or DEFAULT_INTENT_PARAM_MODE
        if intent_param_mode == "off":
            # RETIRED 2026-09-01. The injected params are the intent channel,
            # they are the whole point of a wrap, and they are stripped before
            # the call is forwarded — so a deployment that turns them off is a
            # passthrough that records what happened with no record of why.
            # There is no longer a way to switch them off from the environment;
            # the way to stop the injection is to stop wrapping the server.
            #
            # Coerced rather than refused, deliberately, and for the same reason
            # the both-channels case warns instead of raising: this value used to
            # be documented in `try/SECURITY.md`, so the people most likely to
            # set it are reviewers following an older copy of our own security
            # page. Raising would mean their MCP server stops starting because
            # they did what we told them to. Nothing is hidden — the params
            # never reach their server either way, and the warning says so.
            warnings.append(
                "baton-proxy: BATON_INTENT_PARAM='off' is no longer supported and has "
                f"been ignored; the intent params are injected as "
                f"{DEFAULT_INTENT_PARAM_MODE!r}. They are stripped from every call "
                "before it is forwarded, so your server still receives exactly the "
                "arguments it would receive unwrapped. To stop the injection entirely, "
                "remove the proxy from the server's config entry."
            )
            intent_param_mode = DEFAULT_INTENT_PARAM_MODE
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
            startup_warnings=tuple(warnings),
        )


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None

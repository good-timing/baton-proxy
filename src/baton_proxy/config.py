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
upgrade to a real Console is forced to be explicit.

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
# downstream system that the install hasn't been wired to a real Console yet.
DEFAULT_EVENT_SINK = "stderr:,file:///tmp/baton-proxy.jsonl"
DEFAULT_TENANT_ID = "local"
DEFAULT_CONSENT_TOKEN = "local"


@dataclass(frozen=True)
class Config:
    """All runtime knobs. Created via Config.from_env()."""

    # Process-lifetime session identifier per SPEC §11.4. Every event the proxy
    # emits during this process shares this session_id.
    session_id: str

    # Where emitted events go. A URL whose scheme selects the sink:
    #   https://console.example.com  -> HTTP POST to {url}/v0/events
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

    # Vendor identifier surfaced in proxy logs and the annotation tool's
    # namespace prefix (``{vendor_id}_annotate``). Optional; falls back to
    # the generic ``vendor_annotate`` name.
    vendor_id: str | None

    # Where the proxy writes its own operational log. Stderr by default;
    # override with BATON_PROXY_LOG_FILE for persistent debugging.
    log_file: str | None

    @property
    def emission_enabled(self) -> bool:
        """True when the envelope-essential fields are populated. With
        ``from_env()`` defaults this is always True; only test code that
        passes ``event_sink=None`` etc. directly will see False."""
        return all(
            v is not None
            for v in (self.event_sink, self.tenant_id, self.consent_token)
        )

    @property
    def using_placeholder_consent(self) -> bool:
        """True when consent_token is still the install-time placeholder.
        Emitter refuses to start an http(s) sink while this is True — a
        placeholder consent token must never reach a remote collector."""
        return self.consent_token == DEFAULT_CONSENT_TOKEN

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            session_id=str(uuid.uuid4()),
            event_sink=_env("BATON_EVENT_SINK") or DEFAULT_EVENT_SINK,
            tenant_id=_env("BATON_TENANT_ID") or DEFAULT_TENANT_ID,
            api_key=_env("BATON_API_KEY"),
            consent_token=_env("BATON_CONSENT_TOKEN") or DEFAULT_CONSENT_TOKEN,
            vendor_id=_env("BATON_VENDOR_ID"),
            log_file=_env("BATON_PROXY_LOG_FILE"),
        )


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None

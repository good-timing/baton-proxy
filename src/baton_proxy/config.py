"""Runtime configuration — read from environment variables once at startup.

Subprocess-wrap deployment is 1-process-per-user (Claude Desktop / Claude Code
spawns one proxy per MCP server entry), so a static per-process token model is
fine. Hosted-HTTP deployment will need a per-request resolver; not in scope here.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """All runtime knobs. Created via Config.from_env()."""

    # Process-lifetime session identifier per SPEC §11.4. Every event the proxy
    # emits during this process shares this session_id.
    session_id: str

    # Per-event-type emission target. None disables emission (fail-open path:
    # the proxy still injects + intercepts the annotation tool, but no HTTP
    # traffic leaves the process).
    console_url: str | None
    tenant_id: str | None
    api_key: str | None
    consent_token: str | None

    # Vendor identifier surfaced in proxy logs and (eventually) in the
    # annotation tool's namespace prefix. Optional for now.
    vendor_id: str | None

    # Where the proxy writes its own operational log. Stderr by default;
    # override with BATON_PROXY_LOG_FILE for persistent debugging.
    log_file: str | None

    @property
    def emission_enabled(self) -> bool:
        """True when every field the emitter needs is populated."""
        return all(
            v is not None
            for v in (self.console_url, self.tenant_id, self.api_key, self.consent_token)
        )

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            session_id=str(uuid.uuid4()),
            console_url=_env("BATON_CONSOLE_URL"),
            tenant_id=_env("BATON_TENANT_ID"),
            api_key=_env("BATON_API_KEY"),
            consent_token=_env("BATON_CONSENT_TOKEN"),
            vendor_id=_env("BATON_VENDOR_ID"),
            log_file=_env("BATON_PROXY_LOG_FILE"),
        )


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None

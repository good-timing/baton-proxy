"""Tests for Config.from_env() — zero-config defaults + env-var overrides.

The proxy is install-and-play: ``baton-proxy --`` in front of any MCP
server with NO env vars set should produce a working multi-sink install
(stderr + /tmp/baton-proxy.jsonl). These tests pin that contract.
"""

from __future__ import annotations

import pytest

from baton_proxy.config import (
    DEFAULT_CONSENT_TOKEN,
    DEFAULT_EVENT_SINK,
    DEFAULT_TENANT_ID,
    Config,
)


def _scrub_baton_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any BATON_* env vars so from_env() sees a clean environment.

    Note: BATON_VENDOR_ID is required at startup since Phase 2; tests that
    call from_env() must set it explicitly via ``_set_required_env`` (below).
    Leaving it out is what the missing-vendor-id test exercises."""
    for key in (
        "BATON_EVENT_SINK",
        "BATON_TENANT_ID",
        "BATON_API_KEY",
        "BATON_CONSENT_TOKEN",
        "BATON_VENDOR_ID",
        "BATON_PROXY_LOG_FILE",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env vars from_env() requires (just BATON_VENDOR_ID
    today). Tests that exercise the zero-config UX layer this on top of
    ``_scrub_baton_env`` to model 'no optional env vars set'."""
    monkeypatch.setenv("BATON_VENDOR_ID", "v")


def test_from_env_zero_config_uses_multi_sink_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty env -> event_sink defaults to stderr + local file. The whole
    point of the install-and-play UX: zero env vars produce a working sink."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    config = Config.from_env()
    assert config.event_sink == DEFAULT_EVENT_SINK
    assert "stderr:" in config.event_sink
    assert "file://" in config.event_sink
    assert config.tenant_id == DEFAULT_TENANT_ID
    assert config.consent_token == DEFAULT_CONSENT_TOKEN
    assert config.emission_enabled is True


def test_from_env_explicit_event_sink_overrides_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_EVENT_SINK", "https://collector.example.com")
    config = Config.from_env()
    assert config.event_sink == "https://collector.example.com"


def test_from_env_explicit_tenant_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_TENANT_ID", "acme")
    config = Config.from_env()
    assert config.tenant_id == "acme"


def test_from_env_explicit_consent_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_CONSENT_TOKEN", "real-token-uuid")
    config = Config.from_env()
    assert config.consent_token == "real-token-uuid"
    assert config.using_placeholder_consent is False


def test_using_placeholder_consent_is_true_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The placeholder flag drives the emitter's remote-sink consent guard."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    config = Config.from_env()
    assert config.using_placeholder_consent is True


def test_empty_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty BATON_EVENT_SINK="" should fall back to the
    default rather than disabling emission. (Unix shells make accidentally
    setting an empty string easy; treating that as 'disabled' is
    surprising.)"""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_EVENT_SINK", "")
    config = Config.from_env()
    assert config.event_sink == DEFAULT_EVENT_SINK


def test_api_key_remains_optional_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default sinks (stderr + file) don't need api_key, so it stays
    None when unset — the http-sink-needs-api-key guard lives in sinks.py."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    config = Config.from_env()
    assert config.api_key is None


def test_from_env_raises_when_vendor_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """vendor_id is required at startup — the console needs it to bucket
    friction signal per wrapped MCP server, and the local JSONL stream uses
    it to label events. Loud failure beats silent emission tagged with an
    empty vendor."""
    _scrub_baton_env(monkeypatch)
    with pytest.raises(ValueError, match="BATON_VENDOR_ID"):
        Config.from_env()


def test_from_env_raises_when_vendor_id_is_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exported-but-empty BATON_VENDOR_ID="" should fail the same way as
    unset — an empty string is a misconfigured shell export, not a valid
    vendor identifier."""
    _scrub_baton_env(monkeypatch)
    monkeypatch.setenv("BATON_VENDOR_ID", "")
    with pytest.raises(ValueError, match="BATON_VENDOR_ID"):
        Config.from_env()


def test_from_env_tenant_type_defaults_to_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset BATON_TENANT_TYPE = vendor mode. Preserves existing install
    semantics; customer mode is opt-in."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    config = Config.from_env()
    assert config.tenant_type == "vendor"


def test_from_env_tenant_type_customer_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """BATON_TENANT_TYPE=customer flips the report-tool gate so the
    in-Claude tool stays injected even with a remote http sink."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_TENANT_TYPE", "customer")
    config = Config.from_env()
    assert config.tenant_type == "customer"


def test_from_env_tenant_type_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typos / unknown values fail loudly — silently treating
    BATON_TENANT_TYPE=customers (plural) as vendor would surprise the
    user; better to raise."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_TENANT_TYPE", "customers")
    with pytest.raises(ValueError, match="BATON_TENANT_TYPE"):
        Config.from_env()

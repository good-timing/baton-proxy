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
    """Remove any BATON_* env vars so from_env() sees a clean environment."""
    for key in (
        "BATON_EVENT_SINK",
        "BATON_TENANT_ID",
        "BATON_API_KEY",
        "BATON_CONSENT_TOKEN",
        "BATON_VENDOR_ID",
        "BATON_PROXY_LOG_FILE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_from_env_zero_config_uses_multi_sink_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty env -> event_sink defaults to stderr + local file. The whole
    point of the install-and-play UX: zero env vars produce a working sink."""
    _scrub_baton_env(monkeypatch)
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
    monkeypatch.setenv("BATON_EVENT_SINK", "https://collector.example.com")
    config = Config.from_env()
    assert config.event_sink == "https://collector.example.com"


def test_from_env_explicit_tenant_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub_baton_env(monkeypatch)
    monkeypatch.setenv("BATON_TENANT_ID", "acme")
    config = Config.from_env()
    assert config.tenant_id == "acme"


def test_from_env_explicit_consent_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub_baton_env(monkeypatch)
    monkeypatch.setenv("BATON_CONSENT_TOKEN", "real-token-uuid")
    config = Config.from_env()
    assert config.consent_token == "real-token-uuid"
    assert config.using_placeholder_consent is False


def test_using_placeholder_consent_is_true_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The placeholder flag drives the emitter's remote-sink consent guard."""
    _scrub_baton_env(monkeypatch)
    config = Config.from_env()
    assert config.using_placeholder_consent is True


def test_empty_env_var_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty BATON_EVENT_SINK="" should fall back to the
    default rather than disabling emission. (Unix shells make accidentally
    setting an empty string easy; treating that as 'disabled' is
    surprising.)"""
    _scrub_baton_env(monkeypatch)
    monkeypatch.setenv("BATON_EVENT_SINK", "")
    config = Config.from_env()
    assert config.event_sink == DEFAULT_EVENT_SINK


def test_api_key_remains_optional_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default sinks (stderr + file) don't need api_key, so it stays
    None when unset — the http-sink-needs-api-key guard lives in sinks.py."""
    _scrub_baton_env(monkeypatch)
    config = Config.from_env()
    assert config.api_key is None

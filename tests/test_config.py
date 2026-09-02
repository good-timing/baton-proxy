"""Tests for Config.from_env() — zero-config defaults + env-var overrides.

The proxy is install-and-play: ``baton-proxy --`` in front of any MCP
server with NO env vars set should produce a working multi-sink install
(stderr + /tmp/baton-proxy.jsonl). These tests pin that contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

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


def test_from_env_proactive_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shipped `on` on 2026-09-01 and flipped to `off` the same day, on the
    evidence of the D7 verification run — the pre-call request does not add
    intent, it moves it out of the per-call record (`expected_result` went
    3/3 to 0/5 when the paragraph rendered, while `user_goal` held at 8/8
    because the schema advertises it). It also costs 277 chars against a cap
    the proxy appends into, and an approval prompt inside the work window.
    Now matches the SDK's default as well as its legs."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    assert Config.from_env().proactive_mode == "off"


def test_from_env_proactive_off_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_PROACTIVE", "off")
    assert Config.from_env().proactive_mode == "off"


def test_from_env_proactive_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reason as BATON_TENANT_TYPE: silently reading `BATON_PROACTIVE=false`
    as `on` leaves an operator believing they turned something off."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_PROACTIVE", "false")
    with pytest.raises(ValueError, match="BATON_PROACTIVE"):
        Config.from_env()


def test_intent_param_off_is_ignored_with_a_warning_and_the_proxy_starts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """2026-09-01: the injected params always ride. They are the intent
    channel, they are stripped before the call is forwarded, and the way to
    stop them is to stop wrapping the server.

    Ignored with a warning rather than rejected, and the reason is who sets it.
    `try/SECURITY.md` documented `BATON_INTENT_PARAM=off` as the way to disable
    injection, so the people most likely to have it set are security reviewers
    following our own page. Raising would mean their MCP server stops starting
    because they did exactly what we told them to — we would have bricked a
    server we do not own, from our own security document."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_INTENT_PARAM", "off")
    with caplog.at_level("WARNING", logger="baton_proxy"):
        cfg = Config.from_env()
    assert cfg.intent_param_mode == "required"
    # HELD, not logged — `from_env` runs before `_configure_logging`, so a
    # warning emitted here would go out through `logging.lastResort` (stderr
    # only, no formatter, never teed to BATON_PROXY_LOG_FILE) and the operator
    # who checks the log file would find nothing. `_bootstrap` drains it after
    # logging exists.
    #
    # This assertion is the reason the fix is visible at all: `caplog` attaches
    # its own handler, so the ORIGINAL code passed a `caplog.text` check while
    # the real deployment logged into the void. Asserting caplog is EMPTY and
    # the field is populated is the only shape of this test that can tell the
    # two apart.
    assert caplog.text == ""
    text = "\n".join(cfg.startup_warnings)
    # The warning has to say the thing a reviewer actually needs: their server
    # is unaffected, and here is the real way to turn it off.
    assert "no longer supported" in text
    assert "stripped from every call" in text
    assert "remove the proxy from the server's config entry" in text


def test_the_bootstrap_drains_startup_warnings_into_the_configured_log_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The other half, and the half that was broken: a warning is only a
    warning if it reaches where the operator looks.

    Drives the REAL `_bootstrap` rather than re-performing its two steps,
    because the defect was the ORDER of those steps — `from_env()` logged
    before `_configure_logging` existed, so the message went out through
    `logging.lastResort` (stderr, unformatted) and never reached
    BATON_PROXY_LOG_FILE. A test that calls configure-then-drain itself would
    pass against the broken bootstrap, which is exactly how the original
    caplog test passed."""
    from baton_proxy.proxy import _bootstrap, logger

    log_file = tmp_path / "proxy.log"
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_INTENT_PARAM", "off")
    monkeypatch.setenv("BATON_PROXY_LOG_FILE", str(log_file))
    monkeypatch.setenv("BATON_EVENT_SINK", f"file://{tmp_path / 'events.jsonl'}")
    root_handlers = list(logging.getLogger().handlers)
    baton_handlers = list(logger.handlers)
    try:
        _config, _injection, emitter, _processor = _bootstrap()
        emitter.stop()
        for handler in logger.handlers:
            handler.flush()
        contents = log_file.read_text()
    finally:
        for handler in logger.handlers:
            handler.close()
        logging.getLogger().handlers[:] = root_handlers
        logger.handlers[:] = baton_handlers
    assert "no longer supported" in contents
    assert "remove the proxy from the server's config entry" in contents


def test_a_value_that_was_never_valid_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the coercion above. Softening `off` must not soften the
    check — a typo has to keep failing loudly, or an operator who wrote
    `requird` silently gets the default and never learns."""
    _scrub_baton_env(monkeypatch)
    _set_required_env(monkeypatch)
    monkeypatch.setenv("BATON_INTENT_PARAM", "requird")
    with pytest.raises(ValueError, match="BATON_INTENT_PARAM"):
        Config.from_env()

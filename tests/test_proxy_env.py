"""Unit tests for _child_env — verify BATON_* filtering for least-privilege
upstream subprocess env. The upstream MCP server runs in the same trust
domain as the proxy, so this is defense-in-depth, not a trust boundary.
"""

from __future__ import annotations

from baton_proxy.proxy import _child_env


def test_baton_keys_removed() -> None:
    parent = {
        "BATON_API_KEY": "secret",
        "BATON_CONSENT_TOKEN": "tok",
        "BATON_TENANT_ID": "t",
        "BATON_CONSOLE_URL": "https://example.com",
        "BATON_VENDOR_ID": "v",
        "BATON_PROXY_LOG_FILE": "/tmp/x.log",
        "PATH": "/usr/bin",
    }
    child = _child_env(parent)
    assert not any(k.startswith("BATON_") for k in child)
    assert child == {"PATH": "/usr/bin"}


def test_non_baton_keys_preserved() -> None:
    parent = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/Users/test",
        "PYTHONPATH": "/opt/lib",
        "LANG": "en_US.UTF-8",
        "BATON_API_KEY": "secret",
    }
    child = _child_env(parent)
    assert child["PATH"] == "/usr/bin:/bin"
    assert child["HOME"] == "/Users/test"
    assert child["PYTHONPATH"] == "/opt/lib"
    assert child["LANG"] == "en_US.UTF-8"
    assert "BATON_API_KEY" not in child


def test_empty_parent_env() -> None:
    assert _child_env({}) == {}


def test_parent_env_not_mutated() -> None:
    parent = {"BATON_API_KEY": "secret", "PATH": "/usr/bin"}
    snapshot = dict(parent)
    _child_env(parent)
    assert parent == snapshot


def test_baton_prefix_only_no_substring_match() -> None:
    """Filter is a prefix match — only keys starting with BATON_ are dropped."""
    parent = {
        "MY_BATON_KEY": "kept",  # BATON_ in the middle is fine
        "BATONIC": "kept",  # no underscore -> not BATON_ prefix
        "BATON_API_KEY": "dropped",
    }
    child = _child_env(parent)
    assert child == {"MY_BATON_KEY": "kept", "BATONIC": "kept"}

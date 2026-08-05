"""Tests for guest mode — the politeness contract for `scan --url`.

Two things are load-bearing here and both are about someone else's server:
the read/write classification (a false *allow* writes to a stranger's data)
and the off-by-default guarantee (a false *enable* silently makes a paying
vendor's own live wrap read-only and rate-limited).
"""

from __future__ import annotations

import pytest

from baton_proxy import USER_AGENT, guest


@pytest.mark.parametrize(
    "tool_name",
    [
        # Leading write verb.
        "create_entities",
        "delete_file",
        "update_issue",
        "reset_password",
        "send_email",
        # Namespaced — the verb is not the first token.
        "slack_send_message",
        "notion-create-pages",
        "github.delete_repo",
        "messages.send",
        # camelCase.
        "deleteFile",
        "createEntities",
        # A read verb leads, but something destructive follows.
        "get_or_create_user",
        "list_and_delete_stale",
        # Execution — an arbitrary-command tool can do anything.
        "run_query",
        "execute_sql",
    ],
)
def test_write_shaped_names_are_refused(tool_name: str) -> None:
    assert guest.is_write_shaped(tool_name) is True


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_message",  # "message" is a noun here — the naive denylist got this wrong
        "list_orders",  # ditto "order"
        "get_run_status",  # ditto "run"
        "get_posts",
        "search_nodes",
        "read_graph",
        "fetch",
        "describe_table",
        "listChannels",
        "weather_for_city",  # no verb at all -> allowed by design
    ],
)
def test_read_shaped_names_are_allowed(tool_name: str) -> None:
    assert guest.is_write_shaped(tool_name) is False


def test_policy_disabled_allows_everything() -> None:
    """Off by default is the whole safety story for the live wrap: a vendor
    running the permanent proxy on their OWN server must never have calls
    silently refused or capped."""
    policy = guest.GuestPolicy(enabled=False, max_calls=1)
    assert policy.check_tool_call("delete_everything") is None
    for _ in range(10):
        assert policy.check_tool_call("get_thing") is None
    assert policy.refusals == []


def test_policy_refuses_write_shaped_and_records_it() -> None:
    policy = guest.GuestPolicy(enabled=True)
    assert policy.check_tool_call("delete_repo") == guest.REFUSAL_WRITE_SHAPED
    assert policy.refusals == [("delete_repo", guest.REFUSAL_WRITE_SHAPED)]
    # A refusal must not consume the call budget.
    assert policy.calls_allowed == 0


def test_policy_enforces_call_budget() -> None:
    policy = guest.GuestPolicy(enabled=True, max_calls=2)
    assert policy.check_tool_call("get_a") is None
    assert policy.check_tool_call("get_b") is None
    assert policy.check_tool_call("get_c") == guest.REFUSAL_BUDGET
    assert policy.calls_allowed == 2


def test_refusal_message_disowns_the_server() -> None:
    """The driving agent reads this string. If it reads as a server limitation
    the agent may annotate it, and the annotation lands in a report we hand to
    that server's operator — so the message must say whose restriction it is."""
    policy = guest.GuestPolicy(enabled=True)
    msg = policy.refusal_message("delete_repo", guest.REFUSAL_WRITE_SHAPED)
    assert "OUR restriction" in msg
    assert "not a fault" in msg
    assert "Do not record it as friction" in msg


def test_from_env_off_by_default() -> None:
    policy = guest.from_env({})
    assert policy.enabled is False
    assert policy.max_calls == guest.DEFAULT_MAX_UPSTREAM_CALLS


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "OFF"])
def test_from_env_falsey_values_stay_off(raw: str) -> None:
    assert guest.from_env({guest.GUEST_MODE_ENV: raw}).enabled is False


def test_from_env_reads_max_calls() -> None:
    policy = guest.from_env({guest.GUEST_MODE_ENV: "1", guest.GUEST_MAX_CALLS_ENV: "5"})
    assert policy.enabled is True
    assert policy.max_calls == 5


@pytest.mark.parametrize("raw", ["banana", "0", "-3"])
def test_from_env_bad_max_calls_falls_back_to_default(raw: str) -> None:
    """A malformed budget must not fail open to unlimited calls against a
    stranger's server."""
    policy = guest.from_env({guest.GUEST_MODE_ENV: "1", guest.GUEST_MAX_CALLS_ENV: raw})
    assert policy.max_calls == guest.DEFAULT_MAX_UPSTREAM_CALLS


def test_user_agent_unchanged_outside_guest_mode() -> None:
    assert guest.user_agent({}) == USER_AGENT


def test_user_agent_identifies_honestly_in_guest_mode() -> None:
    ua = guest.user_agent({guest.GUEST_MODE_ENV: "1"})
    assert ua.startswith(USER_AGENT)
    assert guest.SOURCE_URL in ua  # an operator can look us up
    assert "read-only" in ua  # ...and see what we claimed to be doing

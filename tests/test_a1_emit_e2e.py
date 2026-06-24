"""End-to-end test: A1 resource/prompt lifecycle events appear on the console.

Drives a realistic MCP session through the proxy using the fixture server,
then parses the stderr JSONL stream to verify that every A1 event (start +
end/error pair) for resources/list, resources/read, prompts/list, and
prompts/get is emitted correctly.

The annotation calls in REQUESTS mirror real intents captured from live
sessions against the github and sentry MCP servers (vendor_id labels preserved
for realism; the fixture ignores them).

Emission is directed to stderr: only (BATON_EVENT_SINK=stderr:) so no file is
created and runs are isolated from one another.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"

# --- Realistic request sequence modeled on real sessions -------------------- #
# Intents are verbatim from baton-events.jsonl (github + sentry sessions).    #
# The sequence mirrors: discover resources → read happy + error → annotate    #
# → discover prompts → get happy + error → annotate.                          #

REQUESTS: list[dict[str, Any]] = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    # Proactive annotation (github) before reading a resource
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "intent": "Read a github UI resource to verify proxy captures resource_read_start/end lifecycle events in A1",
                "expected_outcome": "Resource content returned successfully; proxy emits resource_read_start + resource_read_end to baton cloud",
                "workflow": "A1 lifecycle event validation",
            },
        },
    },
    # resources/list — expect resource_list_start + resource_list_end (count=2)
    {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
    # Proactive annotation (sentry) before reading resources
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "intent": "Inventory existing teams and projects in goodtiming-inc before creating a baton project.",
                "expected_outcome": "Lists of current teams and projects so I avoid creating duplicates.",
                "workflow": "Sentry account/project setup for goodtiming",
            },
        },
    },
    # resources/read happy path — expect resource_read_start + resource_read_end
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "resources/read",
        "params": {"uri": "fixture://notes.txt"},
    },
    # resources/read error path — expect resource_read_start + resource_read_error
    {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "resources/read",
        "params": {"uri": "fixture://secret.txt"},
    },
    # Reactive annotation for the resource error (mirrors real github signal)
    {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "signal_type": "failure",
                "intent": "Read a github UI resource to verify proxy captures resource_read_start/end lifecycle events in A1",
                "suggested_improvement": "Reading a nonexistent UI resource URI returns MCP error -32002 with no enumeration hint. The error should suggest calling resources/list to discover valid URIs.",
            },
        },
    },
    # Proactive annotation (github) before prompt operations
    {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "intent": "Get the latest release for good-timing/baton-proxy to find the current release version",
                "expected_outcome": "Latest release object with tag, date, and notes — repo has v0.2.x tags so a release should exist",
                "workflow": "Repo status check",
            },
        },
    },
    # prompts/list — expect prompt_list_start + prompt_list_end (count=2)
    {"jsonrpc": "2.0", "id": 9, "method": "prompts/list", "params": {}},
    # prompts/get happy path — expect prompt_get_start + prompt_get_end
    {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "prompts/get",
        "params": {"name": "summarize"},
    },
    # prompts/get error path — expect prompt_get_start + prompt_get_error
    {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "prompts/get",
        "params": {"name": "boom_prompt"},
    },
    # Reactive annotation for the prompt error
    {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "signal_type": "failure",
                "intent": "Get the latest release for good-timing/baton-proxy to find the current release version",
                "suggested_improvement": "list_releases returns [] (silent empty) while get_latest_release throws a raw 404 leaking the GitHub API URL. Normalize to a structured empty response with a hint like 'no releases found; the repo has N tags — use get_tag or list_tags instead'.",
            },
        },
    },
]


def _collect_events() -> list[dict[str, Any]]:
    """Run the proxy with stderr: sink and return the JSONL events from stderr."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env.update(
        {
            "PYTHONPATH": str(REPO / "src"),
            "BATON_VENDOR_ID": "github",
            "BATON_EVENT_SINK": "stderr:",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--", sys.executable, str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    input_data = "".join(json.dumps(req) + "\n" for req in REQUESTS)
    try:
        _stdout, stderr = proc.communicate(input=input_data, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()

    # communicate() returns after the proxy exits, which joins the drain thread,
    # so all queued events are on stderr by the time we get here — no sleep needed.
    events: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "event_type" in msg:
            events.append(msg)
    return events


@pytest.fixture(scope="module")
def events() -> list[dict[str, Any]]:
    """Shared proxy run — collect once per module, reused across all tests."""
    return _collect_events()


def _of(events: list[dict], event_type: str) -> list[dict]:
    return [e for e in events if e.get("event_type") == event_type]


# --------------------------------------------------------------------------- #
# resource_list — start + end with count                                       #
# --------------------------------------------------------------------------- #


def test_resource_list_start_emitted(events: list[dict]) -> None:
    assert _of(events, "resource_list_start"), "resource_list_start not in console output"


def test_resource_list_end_emitted_with_count(events: list[dict]) -> None:
    ends = _of(events, "resource_list_end")
    assert ends, "resource_list_end not in console output"
    assert ends[0]["payload"]["count"] == 2


# --------------------------------------------------------------------------- #
# resource_read — start + end (happy) + error (boom)                          #
# --------------------------------------------------------------------------- #


def test_resource_read_start_emitted_twice(events: list[dict]) -> None:
    starts = _of(events, "resource_read_start")
    assert len(starts) == 2, f"expected 2 resource_read_start, got {len(starts)}"


def test_resource_read_end_emitted_for_notes(events: list[dict]) -> None:
    ends = _of(events, "resource_read_end")
    assert ends, "resource_read_end not in console output"
    assert ends[0]["payload"]["uri"] == "fixture://notes.txt"


def test_resource_read_error_emitted_for_secret(events: list[dict]) -> None:
    errors = _of(events, "resource_read_error")
    assert errors, "resource_read_error not in console output"
    payload = errors[0]["payload"]
    assert payload["uri"] == "fixture://secret.txt"
    assert payload["error_type"] == "-32002"


# --------------------------------------------------------------------------- #
# prompt_list — start + end with count                                         #
# --------------------------------------------------------------------------- #


def test_prompt_list_start_emitted(events: list[dict]) -> None:
    assert _of(events, "prompt_list_start"), "prompt_list_start not in console output"


def test_prompt_list_end_emitted_with_count(events: list[dict]) -> None:
    ends = _of(events, "prompt_list_end")
    assert ends, "prompt_list_end not in console output"
    assert ends[0]["payload"]["count"] == 2


# --------------------------------------------------------------------------- #
# prompt_get — start + end (happy) + error (boom_prompt)                      #
# --------------------------------------------------------------------------- #


def test_prompt_get_start_emitted_twice(events: list[dict]) -> None:
    starts = _of(events, "prompt_get_start")
    assert len(starts) == 2, f"expected 2 prompt_get_start, got {len(starts)}"


def test_prompt_get_end_emitted_for_summarize(events: list[dict]) -> None:
    ends = _of(events, "prompt_get_end")
    assert ends, "prompt_get_end not in console output"
    assert ends[0]["payload"]["name"] == "summarize"


def test_prompt_get_error_emitted_for_boom_prompt(events: list[dict]) -> None:
    errors = _of(events, "prompt_get_error")
    assert errors, "prompt_get_error not in console output"
    payload = errors[0]["payload"]
    assert payload["name"] == "boom_prompt"
    assert payload["error_type"] == "-32002"


# --------------------------------------------------------------------------- #
# All A1 events carry a valid session_id and vendor_id                        #
# --------------------------------------------------------------------------- #

_A1_TYPES = {
    "resource_list_start", "resource_list_end",
    "resource_read_start", "resource_read_end", "resource_read_error",
    "prompt_list_start", "prompt_list_end",
    "prompt_get_start", "prompt_get_end", "prompt_get_error",
}


def test_all_a1_events_carry_session_id(events: list[dict]) -> None:
    a1 = [e for e in events if e.get("event_type") in _A1_TYPES]
    assert a1, "no A1 events found at all"
    session_ids = {e.get("session_id") for e in a1}
    assert None not in session_ids, "some A1 events missing session_id"
    assert len(session_ids) == 1, "A1 events have mismatched session_ids"


def test_all_a1_events_carry_vendor_id(events: list[dict]) -> None:
    a1 = [e for e in events if e.get("event_type") in _A1_TYPES]
    assert a1
    assert all(e.get("vendor_id") == "github" for e in a1)


# --------------------------------------------------------------------------- #
# Annotation events also appear (proactive + reactive intents from real data)  #
# --------------------------------------------------------------------------- #


def test_annotations_emitted_with_real_intents(events: list[dict]) -> None:
    annotations = _of(events, "annotation")
    assert len(annotations) >= 4, f"expected >=4 annotations, got {len(annotations)}"
    intents = {a["payload"].get("intent") for a in annotations}
    assert "Read a github UI resource to verify proxy captures resource_read_start/end lifecycle events in A1" in intents
    assert "Inventory existing teams and projects in goodtiming-inc before creating a baton project." in intents


def test_reactive_annotation_signal_types_present(events: list[dict]) -> None:
    reactive = [
        e for e in _of(events, "annotation")
        if e["payload"].get("signal_type") == "failure"
    ]
    assert len(reactive) == 2, f"expected 2 failure annotations, got {len(reactive)}"

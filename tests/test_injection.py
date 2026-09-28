"""End-to-end injection test: drive a scripted JSON-RPC stream through the
proxy and verify it injects the annotation tool + instructions and handles
the injected call without forwarding.

Mirrors the smoke-test spike's checks now run against the production module.
Emission is disabled (env vars unset) so this test is fully offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent
FIXTURE = HERE / "fixture_server.py"

REQUESTS = [
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
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "baton_annotate",
            "arguments": {
                "signal_type": "failure",
                "user_goal": "test",
                "suggested_improvement": "none",
            },
        },
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"text": "hello"}},
    },
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "boom", "arguments": {}},
    },
]


def _instructions(by_id: dict[int, dict]) -> str:
    init = by_id.get(1)
    assert init is not None, "no initialize response"
    return init.get("result", {}).get("instructions", "")


def _served_names(by_id: dict[int, dict]) -> set[str]:
    tools_list = by_id.get(2)
    assert tools_list is not None, "no tools/list response"
    return {t.get("name") for t in tools_list.get("result", {}).get("tools", [])}


def _run_proxy() -> dict[int, dict]:
    """The default install: no `BATON_*` at all. Literally `_run_proxy_with_env({})`
    — it was a second copy of that body until 2026-09-27, and the copies had
    already drifted: only one of them kept the subprocess's stderr, so the tests
    calling this one could not see a startup warning."""
    return _run_proxy_with_env({})


def test_initialize_carries_injected_instructions() -> None:
    by_id = _run_proxy()
    instructions = _instructions(by_id)
    assert "baton_annotate" in instructions
    assert "MUST" in instructions


def test_tools_list_contains_injected_tool() -> None:
    by_id = _run_proxy()
    names = _served_names(by_id)
    assert "baton_annotate" in names
    assert "echo" in names  # upstream tool still there


def test_the_default_installs_handshake_names_its_event_file() -> None:
    """⚠ The retired report tool's replacement, measured on the WIRE rather than
    on the template. `baton_session_report` is gone; what an agent gets instead
    is the path, in `instructions`, so it can read the session back itself.

    The unit tests in test_llm_text.py pin the rendering. This pins the wiring —
    that `find_file_sink_path` runs over the REAL default sink spec and its
    answer reaches `InitializeResult`. Before this, nothing in front of the model
    named the path: the startup log prints session, emission, tools, intent mode,
    proactive mode and the upstream command, and not the sink.
    """
    from baton_proxy.config import DEFAULT_EVENT_SINK
    from baton_proxy.sinks import find_file_sink_path

    expected = find_file_sink_path(DEFAULT_EVENT_SINK)
    assert expected, f"the default sink has no file leg: {DEFAULT_EVENT_SINK!r}"
    instructions = _instructions(_run_proxy())
    assert expected in instructions, f"the default install's handshake never names {expected!r}"


def test_an_http_only_wrap_names_no_event_file() -> None:
    """Vendor production: no local file, so nothing to point at, and the line
    must not render as a dangling sentence. This is also what keeps the suffix
    the same size it was on the surface where size decides whether Claude Code
    defers tool loading at all.
    """
    by_id = _run_proxy_with_env(
        {
            "BATON_EVENT_SINK": "https://collector.example.com",
            "BATON_API_KEY": "k",
            "BATON_TENANT_ID": "acme",
            "BATON_CONSENT_TOKEN": "real-token",
        }
    )
    instructions = _instructions(by_id)
    assert "captured as JSONL" not in instructions
    assert "baton_annotate" in instructions, "the rest of the suffix went with it"


def test_a_custom_file_sink_is_the_path_named(tmp_path: Path) -> None:
    """The control for the test above: a DIFFERENT path has to come through, so
    the first test cannot be passing on a hardcoded default that the code never
    actually looked up."""
    custom = tmp_path / "somewhere-else.jsonl"
    by_id = _run_proxy_with_env({"BATON_EVENT_SINK": f"stderr:,file://{custom}"})
    assert str(custom) in _instructions(by_id)


# The EXACT served set, on each sink shape that used to decide whether a second
# tool appeared. Asserted as a set rather than as `"baton_session_report" not in
# names`, which is the obvious form and passes against a typo, against a rename,
# and against a third tool nobody meant to add
# ([[feedback_a_negative_test_must_be_able_to_fail]]).
#
# The upstream half is READ OFF THE FIXTURE's own tools/list rather than listed
# here: a hardcoded copy reds when the fixture gains a fifth tool, which is a
# maintenance failure dressed as a finding, and it would not have caught the
# thing this set exists to catch. What is hardcoded is the one name the proxy
# ADDS, because that is the number under test — one, not two.
def _expected_served() -> set[str]:
    # PACKAGE-qualified: the bare name `fixture_responses` resolves only inside
    # the fixture SUBPROCESS, whose sys.path starts at its own directory, but
    # `tests` is a package (`tests/__init__.py`) and the repo root is on the path,
    # so this plain import works and a `spec_from_file_location` dance does not
    # need to. (`test_try_kit.py` genuinely needs that dance — it loads
    # `try/kit.py`, which is outside any package.)
    from tests.fixture_responses import result_for

    listed = result_for({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    upstream = {t["name"] for t in listed["result"]["tools"]}
    assert upstream, "the fixture declares no tools; this set would assert nothing"
    return upstream | {"baton_annotate"}


def test_the_default_install_serves_annotate_and_the_upstream_tool_only() -> None:
    """Default install (no env vars) -> sink defaults to stderr + file, which is
    the shape that USED to inject a second tool, `baton_session_report`. It was
    retired 2026-09-27; the file sink now only decides whether the instructions
    can name a path."""
    assert _served_names(_run_proxy()) == _expected_served()


def test_an_http_sink_serves_the_same_set() -> None:
    """Vendor production mode. It never got the second tool, so this shape is
    the control: it was already equal to the set above, and it still is."""
    by_id = _run_proxy_with_env(
        {
            "BATON_EVENT_SINK": "https://collector.example.com",
            "BATON_API_KEY": "k",
            "BATON_TENANT_ID": "acme",
            "BATON_CONSENT_TOKEN": "real-token",
        }
    )
    assert _served_names(by_id) == _expected_served()


def test_the_customer_mode_arm_serves_the_same_set(tmp_path: Path) -> None:
    """⚠ The arm most likely to regress, and the one the retirement plan named
    as its verification. `file + http(s) + BATON_TENANT_TYPE=customer` was the
    ONE combination that kept the report tool when an HTTP sink was present —
    every other http shape was already suppressed, so a deletion that missed
    that branch would leave exactly this shape still injecting.

    ⚠ `BATON_TENANT_TYPE` is an unread name as of 2026-09-27 — the variable was
    deleted with the branch. It is still SET here on purpose, and that is now the
    second thing this test proves: a config still carrying it, as the Console's
    local-setup page wrote them, gets the same tool set and does not fail to
    start. A `file + http` tee is also a shape worth keeping a served-set
    assertion on in its own right.
    """
    by_id = _run_proxy_with_env(
        {
            "BATON_EVENT_SINK": f"file://{tmp_path / 'events.jsonl'},https://collector.example.com",
            "BATON_API_KEY": "k",
            "BATON_TENANT_ID": "acme",
            "BATON_CONSENT_TOKEN": "real-token",
            "BATON_TENANT_TYPE": "customer",
        }
    )
    assert _served_names(by_id) == _expected_served()


def test_injected_tool_call_handled_by_proxy() -> None:
    by_id = _run_proxy()
    inj = by_id.get(3)
    assert inj is not None
    text = inj["result"]["content"][0]["text"]
    assert "baton_annotate recorded" in text


def test_upstream_tool_call_still_works() -> None:
    by_id = _run_proxy()
    echo = by_id.get(4)
    assert echo is not None
    assert "Echo: hello" in echo["result"]["content"][0]["text"]


def test_upstream_tool_error_passes_through() -> None:
    by_id = _run_proxy()
    boom = by_id.get(5)
    assert boom is not None
    assert "error" in boom
    assert boom["error"]["code"] == -32000


def test_annotate_tool_name_is_baton_branded() -> None:
    """v1 posture: the proxy is a gateway demo, Baton brand visibility is the
    point. White-label tool naming was previously per-vendor; that machinery
    is gone until a vendor specifically asks. Regression guard for that
    design decision."""
    from baton_proxy.proxy import ANNOTATE_TOOL_NAME

    assert ANNOTATE_TOOL_NAME == "baton_annotate"


def _run_proxy_with_env(extra_env: dict[str, str]) -> dict[int, dict]:
    """Run the proxy with the REQUESTS script and extra env overrides.

    BATON_VENDOR_ID is required at startup so we set a baseline of ``"v"``;
    callers can override it (or any other BATON_*) via ``extra_env``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
    env["PYTHONPATH"] = str(REPO / "src")
    env["BATON_VENDOR_ID"] = "v"
    env.update(extra_env)
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
        stdout, stderr = proc.communicate(input=input_data, timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
    by_id: dict[int, dict] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "id" in msg:
            by_id[msg["id"]] = msg
    # Stashed on the dict rather than returned as a tuple, so the existing
    # callers keep their shape. The operator-facing log is the only place a
    # DROPPED event-file line is visible, and it goes to the proxy's real
    # stderr — `caplog` attaches its own handler and would pass over a
    # deployment that logged into the void (see test_config.py's note on
    # exactly that).
    by_id["stderr"] = stderr  # type: ignore[index,assignment]
    return by_id


def test_vendor_id_does_not_affect_tool_name() -> None:
    """v1 design decision: BATON_VENDOR_ID labels the install for the operator
    but does NOT prefix the injected tool name. Customers evaluating Baton
    via the proxy always see ``baton_annotate``, regardless of which vendor
    they wrapped. Regression guard — flipping this back to vendor-namespaced
    is a deliberate move (vendor opt-in to white-label), not an accident."""
    by_id = _run_proxy_with_env({"BATON_VENDOR_ID": "acme"})

    instructions = _instructions(by_id)
    assert "baton_annotate" in instructions
    assert "acme_annotate" not in instructions

    names = _served_names(by_id)
    assert "baton_annotate" in names
    assert "acme_annotate" not in names


def test_annotation_schema_requires_only_the_goal() -> None:
    """Proactive annotations carry the goal alone; signal_type +
    suggested_improvement are reactive-only. The schema must reflect
    that — forcing signal_type as required pushes the agent to invent
    `signal_type='other'` for proactives, polluting friction counts.

    Regression guard for the 2026-06-16 schema loosening (proxy.py
    inputSchema.required changed from [signal_type, intent,
    suggested_improvement] to one field; that field is now named
    ``user_goal`` agent-side and still stored as ``intent``).
    """
    from baton_proxy.proxy import _build_injected_tool

    tool = _build_injected_tool("baton_annotate")
    schema = tool["inputSchema"]
    assert schema["required"] == ["user_goal"]
    # signal_type stays a valid PROPERTY — reactives still set it.
    assert "signal_type" in schema["properties"]
    assert "suggested_improvement" in schema["properties"]


def test_proactive_annotation_handled_without_signal_type() -> None:
    """A proactive annotation arrives with only ``user_goal`` (and maybe
    expected_result / overall_task / context). The proxy must accept it,
    emit the event, and not invent a signal_type='unknown' for the
    user-visible confirmation — the absence of signal_type is the
    semantic marker that this was proactive."""
    from baton_proxy.proxy import _handle_injected_call

    resp = _handle_injected_call(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "baton_annotate",
                "arguments": {
                    "user_goal": "user wants the 3 most recent issues",
                    "expected_result": "a list of 3 issues, newest first",
                },
            },
        },
    )
    assert resp["id"] == 99
    # The handler should not fabricate signal_type='unknown' for proactives.
    text = resp["result"]["content"][0]["text"]
    assert "signal_type=unknown" not in text


def test_handle_injected_call_null_params_does_not_crash() -> None:
    """JSON-RPC permits params: null. dict.get's default fires on missing
    keys, not on explicit None, so the chained get pattern must coerce."""
    # An Emitter, a Config and an `_Injection` were built here purely to satisfy
    # the signature's report-tool branch, which read the sink path and the scrub
    # counts. The branch is gone (2026-09-27) and so is the scaffolding — the
    # handler now takes the request and nothing else, which is what these
    # defensive cases were ever about.
    from baton_proxy.proxy import _handle_injected_call

    resp = _handle_injected_call(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
    )
    assert resp["id"] == 1
    # No params at all → no signal_type → confirmation surfaces it as a
    # proactive (preferred to the prior "unknown" sentinel — see the
    # schema-loosening note on the handler).
    assert "proactive intent" in resp["result"]["content"][0]["text"]

    resp = _handle_injected_call(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": None},
    )
    assert resp["id"] == 2

    resp = _handle_injected_call(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "baton_annotate", "arguments": None},
        },
    )
    assert resp["id"] == 3


class _StubIngest(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            self.received.append(json.loads(body))
        except json.JSONDecodeError:
            self.received.append({"_raw": body})
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"accepted"}')

    def log_message(self, *_args, **_kwargs) -> None:
        return


def test_baton_annotate_emits_annotation_event_end_to_end() -> None:
    """Run the proxy with BATON_* env vars and verify the annotation event
    is POSTed to the console after baton_annotate is called."""
    _StubIngest.received = []
    server = HTTPServer(("127.0.0.1", 0), _StubIngest)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    event_sink = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("BATON_")}
        env.update(
            {
                "PYTHONPATH": str(REPO / "src"),
                "BATON_EVENT_SINK": event_sink,
                "BATON_TENANT_ID": "t",
                "BATON_API_KEY": "k",
                "BATON_CONSENT_TOKEN": "c",
                "BATON_VENDOR_ID": "v",
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
            proc.communicate(input=input_data, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()

        # Belt-and-suspenders: communicate() returns only after the proxy exits,
        # which drains the queue; still wait briefly in case the OS scheduler
        # hasn't completed the in-flight POSTs.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(ev.get("event_type") == "annotation" for ev in _StubIngest.received):
                break
            time.sleep(0.02)
    finally:
        server.shutdown()

    annotations = [ev for ev in _StubIngest.received if ev.get("event_type") == "annotation"]
    assert len(annotations) == 1, f"expected 1 annotation, got {len(annotations)}"
    ann = annotations[0]
    assert ann["payload"] == {
        "signal_type": "failure",
        "intent": "test",
        "suggested_improvement": "none",
    }
    assert ann["session_id"]
    assert ann["tenant_id"] == "t"
    assert ann["consent_token"] == "c"


def test_a_dropped_event_file_line_is_logged_to_the_operator(tmp_path: Path) -> None:
    """⚠ The one failure mode with no other witness. The line is DROPPED rather
    than raised when the path does not fit, so an operator whose agent never
    learns where the events are has nothing to tell them why. `BATON_PROACTIVE=on`
    leaves 35 chars for a path, which most real paths exceed.

    Read off the proxy's REAL stderr, not `caplog`: the warning fires in
    `_bootstrap` after `_configure_logging`, and a caplog assertion would pass
    against a handler configuration that never reaches an operator.
    """
    # A REAL directory: `FileSink` opens the path at startup, so a made-up one
    # fails the proxy before it ever renders instructions — which is how the
    # first draft of this test "passed" its warning assertion and then found no
    # initialize response to check against.
    #
    # ⚠ Nested rather than one long name, and built to a MEASURED target rather
    # than eyeballed: the line's cap moved to the client's 2,087 on 2026-09-27,
    # so overflowing now needs ~490 chars of path where it used to need 36, and
    # each single path COMPONENT is capped at 255 by the filesystem.
    deep = tmp_path
    for _ in range(3):
        deep = deep / ("d" * 200)
        deep.mkdir()
    long_path = str(deep / "events.jsonl")
    assert len(long_path) > 490, f"{len(long_path)} chars is no longer enough to overflow"
    by_id = _run_proxy_with_env(
        {"BATON_EVENT_SINK": f"stderr:,file://{long_path}", "BATON_PROACTIVE": "on"}
    )
    stderr = by_id["stderr"]
    assert "did not fit the instructions length cap" in stderr, stderr[-900:]
    assert long_path in stderr
    # ...and it really was dropped, so the warning is not crying wolf.
    assert long_path not in _instructions(by_id)


def test_a_path_that_fits_logs_nothing() -> None:
    """The control. Without it the assertion above passes over a warning that
    fires on every start, which is the same as no warning at all."""
    by_id = _run_proxy_with_env({"BATON_EVENT_SINK": "stderr:,file:///tmp/s.jsonl"})
    assert "did not fit the instructions length cap" not in by_id["stderr"]
    assert "/tmp/s.jsonl" in _instructions(by_id)

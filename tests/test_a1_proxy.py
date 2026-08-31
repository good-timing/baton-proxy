"""Tests for A1 proxy capture — resource/prompt event dispatch.

Covers:
- _emit_call_end and _emit_call_error dispatch by kind
- Eviction of resource/prompt pending calls emits the right error method
- report._derive_mechanical_findings picks up resource_read_error + prompt_get_error
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import Any

import pytest

from baton_proxy import proxy as proxy_mod
from baton_proxy.proxy import (
    EVICTED_ERROR_TYPE,
    _ClientAction,
    _emit_call_end,
    _emit_call_error,
    _evict_overflow,
    _PendingCall,
    _pump_client_to_server,
    run_proxy,
)


class _CapturingEmitter:
    """Records all *_start, *_end, *_error calls by event type."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, event_type: str, **kwargs: Any) -> None:
        self.calls.append((event_type, kwargs))

    def enqueue_tool_call_end(self, **kw: Any) -> None:
        self._record("tool_call_end", **kw)

    def enqueue_tool_call_error(self, **kw: Any) -> None:
        self._record("tool_call_error", **kw)

    def enqueue_resource_read_end(self, **kw: Any) -> None:
        self._record("resource_read_end", **kw)

    def enqueue_resource_read_error(self, **kw: Any) -> None:
        self._record("resource_read_error", **kw)

    def enqueue_resource_list_end(self, **kw: Any) -> None:
        self._record("resource_list_end", **kw)

    def enqueue_resource_list_error(self, **kw: Any) -> None:
        self._record("resource_list_error", **kw)

    def enqueue_prompt_get_end(self, **kw: Any) -> None:
        self._record("prompt_get_end", **kw)

    def enqueue_prompt_get_error(self, **kw: Any) -> None:
        self._record("prompt_get_error", **kw)

    def enqueue_prompt_list_end(self, **kw: Any) -> None:
        self._record("prompt_list_end", **kw)

    def enqueue_prompt_list_error(self, **kw: Any) -> None:
        self._record("prompt_list_error", **kw)


# --------------------------------------------------------------------------- #
# _emit_call_end dispatch
# --------------------------------------------------------------------------- #


def test_emit_call_end_tool() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="tool", subject="my_tool", started_ms=0, runtime_meta=None)
    _emit_call_end(em, call, {"out": 1}, 50)
    assert em.calls == [
        (
            "tool_call_end",
            {"tool_name": "my_tool", "result": {"out": 1}, "duration_ms": 50, "runtime_meta": None},
        )
    ]


def test_emit_call_end_resource_read() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(
        kind="resource_read", subject="file:///notes.txt", started_ms=0, runtime_meta=None
    )
    _emit_call_end(em, call, "...content...", 120)
    assert em.calls == [
        (
            "resource_read_end",
            {"uri": "file:///notes.txt", "duration_ms": 120, "runtime_meta": None},
        )
    ]


def test_emit_call_end_resource_list_extracts_count() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="resource_list", subject="", started_ms=0, runtime_meta=None)
    result = {"resources": [{"uri": "a"}, {"uri": "b"}, {"uri": "c"}]}
    _emit_call_end(em, call, result, 30)
    assert em.calls == [
        ("resource_list_end", {"count": 3, "duration_ms": 30, "runtime_meta": None})
    ]


def test_emit_call_end_resource_list_empty_result() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="resource_list", subject="", started_ms=0, runtime_meta=None)
    _emit_call_end(em, call, None, 10)
    assert em.calls == [
        ("resource_list_end", {"count": 0, "duration_ms": 10, "runtime_meta": None})
    ]


def test_emit_call_end_prompt_get() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="prompt_get", subject="summarize", started_ms=0, runtime_meta=None)
    _emit_call_end(em, call, {"messages": []}, 40)
    assert em.calls == [
        ("prompt_get_end", {"name": "summarize", "duration_ms": 40, "runtime_meta": None})
    ]


def test_emit_call_end_prompt_list_extracts_count() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="prompt_list", subject="", started_ms=0, runtime_meta=None)
    result = {"prompts": [{"name": "p1"}, {"name": "p2"}]}
    _emit_call_end(em, call, result, 20)
    assert em.calls == [("prompt_list_end", {"count": 2, "duration_ms": 20, "runtime_meta": None})]


# --------------------------------------------------------------------------- #
# _emit_call_error dispatch
# --------------------------------------------------------------------------- #


def test_emit_call_error_tool() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="tool", subject="my_tool", started_ms=0, runtime_meta=None)
    _emit_call_error(em, call, "timeout", "upstream did not respond", 5000)
    assert em.calls == [
        (
            "tool_call_error",
            {
                "tool_name": "my_tool",
                "error_type": "timeout",
                "error_body": "upstream did not respond",
                "duration_ms": 5000,
                "runtime_meta": None,
            },
        )
    ]


def test_emit_call_error_resource_read() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(
        kind="resource_read", subject="file:///secret.txt", started_ms=0, runtime_meta=None
    )
    _emit_call_error(em, call, "-32601", "Resource not found", 10)
    assert em.calls[0][0] == "resource_read_error"
    assert em.calls[0][1]["uri"] == "file:///secret.txt"
    assert em.calls[0][1]["error_type"] == "-32601"


def test_emit_call_error_resource_list() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="resource_list", subject="", started_ms=0, runtime_meta=None)
    _emit_call_error(em, call, "-32600", "Not supported", 5)
    assert em.calls[0][0] == "resource_list_error"
    assert "uri" not in em.calls[0][1]
    assert em.calls[0][1]["error_type"] == "-32600"


def test_emit_call_error_prompt_get() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(
        kind="prompt_get", subject="generate_email", started_ms=0, runtime_meta=None
    )
    _emit_call_error(em, call, "-32001", "Unknown prompt", 8)
    assert em.calls[0][0] == "prompt_get_error"
    assert em.calls[0][1]["name"] == "generate_email"


def test_emit_call_error_prompt_list() -> None:
    em = _CapturingEmitter()
    call = _PendingCall(kind="prompt_list", subject="", started_ms=0, runtime_meta=None)
    _emit_call_error(em, call, "unknown", "No prompts available", 3)
    assert em.calls[0][0] == "prompt_list_error"
    assert "name" not in em.calls[0][1]


# --------------------------------------------------------------------------- #
# Eviction dispatches correct error method for each kind
# --------------------------------------------------------------------------- #


def _make_single(kind: str, subject: str) -> OrderedDict[Any, _PendingCall]:
    from baton_proxy.proxy import MAX_PENDING

    pending: OrderedDict[Any, _PendingCall] = OrderedDict()
    for i in range(MAX_PENDING + 1):
        pending[i] = _PendingCall(kind=kind, subject=subject, started_ms=1000, runtime_meta=None)
    return pending


def test_eviction_resource_read() -> None:
    em = _CapturingEmitter()
    pending = _make_single("resource_read", "file:///doc.txt")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "resource_read_error"
    assert em.calls[0][1]["error_type"] == EVICTED_ERROR_TYPE
    assert em.calls[0][1]["uri"] == "file:///doc.txt"


def test_eviction_resource_list() -> None:
    em = _CapturingEmitter()
    pending = _make_single("resource_list", "")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "resource_list_error"
    assert em.calls[0][1]["error_type"] == EVICTED_ERROR_TYPE


def test_eviction_prompt_get() -> None:
    em = _CapturingEmitter()
    pending = _make_single("prompt_get", "my_prompt")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "prompt_get_error"
    assert em.calls[0][1]["name"] == "my_prompt"


def test_eviction_prompt_list() -> None:
    em = _CapturingEmitter()
    pending = _make_single("prompt_list", "")
    _evict_overflow(pending, em)  # type: ignore[arg-type]
    assert em.calls[0][0] == "prompt_list_error"


# --------------------------------------------------------------------------- #
# _derive_mechanical_findings picks up resource/prompt errors
# --------------------------------------------------------------------------- #


def test_derive_mechanical_finds_resource_read_error() -> None:
    from baton_proxy.report import _derive_mechanical_findings

    events = [
        {
            "event_type": "resource_read_start",
            "payload": {"uri": "file:///notes.txt", "params": {}},
        },
        {
            "event_type": "resource_read_error",
            "payload": {
                "uri": "file:///notes.txt",
                "error_type": "403",
                "error_body": "Forbidden",
                "duration_ms": 50,
            },
        },
    ]
    findings = _derive_mechanical_findings(events)
    assert len(findings) == 1
    assert findings[0]["tool"] == "file:///notes.txt"
    assert findings[0]["error_type"] == "403"
    assert findings[0]["signal"] == "failure"


def test_derive_mechanical_finds_prompt_get_error() -> None:
    from baton_proxy.report import _derive_mechanical_findings

    events = [
        {"event_type": "prompt_get_start", "payload": {"name": "summarize", "params": {}}},
        {
            "event_type": "prompt_get_error",
            "payload": {
                "name": "summarize",
                "error_type": "-32601",
                "error_body": "Unknown prompt",
                "duration_ms": 10,
            },
        },
        {"event_type": "prompt_get_start", "payload": {"name": "summarize", "params": {}}},
        {
            "event_type": "prompt_get_error",
            "payload": {
                "name": "summarize",
                "error_type": "-32601",
                "error_body": "Unknown prompt",
                "duration_ms": 10,
            },
        },
    ]
    findings = _derive_mechanical_findings(events)
    assert len(findings) == 1
    assert findings[0]["tool"] == "summarize"
    assert findings[0]["count"] == 2
    assert findings[0]["signal"] == "retry_loop"


def test_derive_mechanical_mixed_tool_and_resource() -> None:
    from baton_proxy.report import _derive_mechanical_findings

    events = [
        {"event_type": "tool_call_start", "payload": {"tool_name": "search", "params": {}}},
        {
            "event_type": "tool_call_error",
            "payload": {
                "tool_name": "search",
                "error_type": "500",
                "error_body": "Internal error",
                "duration_ms": 100,
            },
        },
        {
            "event_type": "resource_read_start",
            "payload": {"uri": "file:///data.json", "params": {}},
        },
        {
            "event_type": "resource_read_error",
            "payload": {
                "uri": "file:///data.json",
                "error_type": "404",
                "error_body": "Not found",
                "duration_ms": 20,
            },
        },
    ]
    findings = _derive_mechanical_findings(events)
    assert len(findings) == 2
    subjects = {f["tool"] for f in findings}
    assert subjects == {"search", "file:///data.json"}


# ---------------------------------------------------------------------------
# Shutdown: closing the upstream's stdin is what lets run_proxy's shutdown run.
# ---------------------------------------------------------------------------


class _StubProcessor:
    """Forwards everything; the pump's shutdown is what is under test."""

    def handle_client_message(self, req: dict[str, Any]) -> _ClientAction:
        return _ClientAction(forward=req)


class _RecordingStdin:
    """Stands in for the upstream's stdin pipe."""

    def __init__(self) -> None:
        self.closed = False
        self.written: list[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.closed = True


class _ExplodingStdin:
    """A client stream that yields one line, then dies mid-iteration.

    Not hypothetical on either count. `sys.stdin` decodes strict UTF-8, so one
    non-UTF-8 byte from a client raises `UnicodeDecodeError` out of the `for`
    itself; and `json.loads` on deeply nested input raises `RecursionError`,
    which `except json.JSONDecodeError` does not catch (measured: escapes at
    depth 200k on 3.14, far shallower on the 3.11 CI runs)."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self._sent = False

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> str:
        if self._sent:
            raise self._exc
        self._sent = True
        return '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'


@pytest.mark.parametrize(
    "exc",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        RecursionError("maximum recursion depth exceeded"),
    ],
    ids=["non-utf8-byte", "deeply-nested-json"],
)
def test_the_upstreams_stdin_is_closed_even_when_the_pump_dies(monkeypatch, exc):
    """The EOF path closes it; every other way out of that loop skipped it.

    An abrupt exit is exactly when the close matters most. `run_proxy` hangs
    its whole shutdown off `child.wait()` — `drain_pending`, then
    `emitter.stop()` — so an upstream left holding an open pipe means the last
    events are still in the queue when the client's SIGTERM lands. Silent
    no-emit, at the one moment nobody is watching."""
    child_stdin = _RecordingStdin()
    processor = _StubProcessor()
    monkeypatch.setattr(sys, "stdin", _ExplodingStdin(exc))

    with pytest.raises(type(exc)):
        _pump_client_to_server(child_stdin, processor)

    assert child_stdin.closed, "the upstream was left holding an open stdin"


# ---------------------------------------------------------------------------
# Shutdown, one hop down: the wait on the upstream is bounded once the client
# is gone. a613b35 made the client's EOF reach the upstream; this covers the
# upstream that receives it and does not care — and the escalation tail that
# review found four defects in.
# ---------------------------------------------------------------------------


class _FakeChild:
    """A `Popen` stand-in whose exit is under the test's control.

    `pid` is -1 deliberately. The implementation signals the process GROUP, so
    a fake carrying a plausible pid would have `os.getpgid` resolve a real
    group and the suite would SIGTERM a stranger; `pid = 0` is worse still,
    because `getpgid(0)` returns the caller's own group and the suite would
    kill itself. -1 raises `ProcessLookupError`, which routes to the
    `send_signal` fallback — the path these fakes are here to grade.

    `stdout` is an exhausted iterator so `_pump_server_to_client` returns at
    once; the thing under test is the shutdown, not the pumps.
    """

    def __init__(self, *, exits_after: float | None, unkillable: bool = False) -> None:
        """`exits_after=None` never exits on its own; a float is seconds until it does.

        The delay is load-bearing, not padding. A child already dead on the
        first look means the wait returns before any escalation logic is
        reached, so the test cannot see whether the clock was gated on the
        client's disconnect — measured: with an immediate exit, deleting the
        gate entirely left both tests green.
        """
        self.stdin = _RecordingStdin()
        self.stdout: Any = iter(())
        self.pid = -1
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None
        self.wait_calls = 0
        self._unkillable = unkillable
        self._exit = threading.Event()
        if exits_after is not None:
            if exits_after <= 0:
                self._settle(0)
            else:
                timer = threading.Timer(exits_after, lambda: self._settle(0))
                timer.daemon = True
                timer.start()

    def _settle(self, rc: int) -> None:
        if self.returncode is None:
            self.returncode = rc
        self._exit.set()

    def wait(self, timeout: float | None = None) -> int:
        # Faithful to `Popen.wait`: with no timeout it BLOCKS until the process
        # dies, it does not raise. A fake that raised would turn the field's
        # hang into a TimeoutExpired and redden for a reason the field never
        # produces.
        self.wait_calls += 1
        if self._exit.wait(timeout):
            assert self.returncode is not None
            return self.returncode
        raise subprocess.TimeoutExpired(cmd="upstream", timeout=timeout or 0)

    def poll(self) -> int | None:
        return self.returncode if self._exit.is_set() else None

    def send_signal(self, sig: int) -> None:
        if sig == signal.SIGTERM:
            self.terminated = True
        elif sig == signal.SIGKILL:
            self.killed = True
        if self._unkillable:
            # Uninterruptible sleep: even SIGKILL does not land.
            return
        # A real child killed by a signal reports -N, and that is the whole
        # subject of the exit-status test below.
        self._settle(-sig)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


class _Worker(threading.Thread):
    """Runs `run_proxy` off the main thread so a hang fails instead of hanging.

    Without this the RED case is not a failing test, it is a stuck suite: the
    defects under test are unbounded waits. Daemon, so a genuine hang cannot
    outlive the run.

    It also keeps whatever `run_proxy` raised. A crash on the way in — a
    missing env var, say — otherwise leaves the thread dead and the fake child
    untouched, which is indistinguishable from a clean exit.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="run-proxy-under-test")
        self.error: BaseException | None = None
        self.rc: int | None = None

    def run(self) -> None:
        try:
            self.rc = run_proxy(["unused"])
        except BaseException as e:  # noqa: BLE001 - re-raised by the caller
            self.error = e


def _run_proxy_in_a_worker(timeout: float = 3.0) -> _Worker:
    worker = _Worker()
    worker.start()
    worker.join(timeout)
    if worker.error is not None:
        raise AssertionError(f"run_proxy raised before it could reach the wait: {worker.error!r}")
    return worker


def _shutdown_env(monkeypatch) -> None:
    """The minimum `Config.from_env` accepts, pointed somewhere harmless."""
    monkeypatch.setenv("BATON_EVENT_SINK", "stderr:")
    monkeypatch.setenv("BATON_VENDOR_ID", "toybox")


def _tight_graces(monkeypatch) -> None:
    monkeypatch.setattr(proxy_mod, "_UPSTREAM_EXIT_GRACE", 0.05)
    monkeypatch.setattr(proxy_mod, "_UPSTREAM_KILL_GRACE", 0.05)
    monkeypatch.setattr(proxy_mod, "_UPSTREAM_ABANDON_GRACE", 0.05)


class _NeverEofStdin:
    """The client is still attached; iterating blocks, as a real pipe does."""

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> str:
        threading.Event().wait()
        raise AssertionError("unreachable")


def _spawning(child: Any, spawned: list[Any]):
    def _popen(*a: Any, **kw: Any) -> Any:
        spawned.append(kw)
        return child

    return _popen


def test_a_stubborn_upstream_does_not_hang_the_shutdown(monkeypatch):
    """An upstream that ignores its own stdin EOF re-creates a613b35's hang.

    That fix closed the upstream's stdin so a *healthy* server would see EOF
    and exit. One that reads stdin and keeps serving anyway never exits, so the
    wait never returns and everything hanging off it is skipped again:
    `drain_pending`, which gives each in-flight `*_start` a matching end, and
    `emitter.stop()`, which flushes the queue. The client's SIGTERM then lands
    with the last events unwritten.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    child = _FakeChild(exits_after=None)
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, []))
    # The client has already gone: stdin is at EOF, so the pump returns at once
    # and the escalation is armed.
    monkeypatch.setattr(sys, "stdin", iter(()))

    worker = _run_proxy_in_a_worker()

    assert not worker.is_alive(), (
        "run_proxy never returned: the wait on the upstream is still unbounded, "
        "so drain_pending and emitter.stop() are unreachable"
    )
    assert child.terminated, "the stubborn upstream was never asked to stop"


def test_terminating_the_upstream_is_not_reported_as_a_crash(monkeypatch):
    """Review finding 2. For any server that ignores stdin EOF, escalation is
    now the ORDINARY shutdown path — so its exit status is what the client sees
    every single time.

    A child killed by a signal reports `-15`, `run_proxy` returns it, `main`
    raises `SystemExit(-15)`, and the shell reports **241**. The client logs a
    crash on every clean exit. The signal was ours, not the upstream's failure,
    so it must not be dressed up as the upstream's verdict.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    child = _FakeChild(exits_after=None)
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, []))
    monkeypatch.setattr(sys, "stdin", iter(()))

    worker = _run_proxy_in_a_worker()

    assert child.terminated, "precondition: this test is about the escalation path"
    assert worker.rc == 0, (
        f"run_proxy returned {worker.rc}; SystemExit({worker.rc}) makes the shell "
        "report 241, so the client logs a crash on every ordinary shutdown"
    )


def test_a_genuine_upstream_crash_is_still_reported(monkeypatch):
    """The other side of finding 2, and the branch that keeps the mask honest.

    Masking the signal we sent must not become masking every non-zero exit. An
    upstream that dies on its own still has to have its returncode reach the
    client — that is the whole reason `run_proxy` returns the child's rc.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    child = _FakeChild(exits_after=None)
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, []))
    monkeypatch.setattr(sys, "stdin", _NeverEofStdin())
    # The upstream falls over by itself, client still attached.
    threading.Timer(0.1, lambda: child._settle(3)).start()

    worker = _run_proxy_in_a_worker()

    assert not child.terminated, "we never signalled it; it died on its own"
    assert worker.rc == 3, f"the upstream's own exit code was lost (got {worker.rc})"


def test_an_unkillable_upstream_still_lets_the_proxy_exit(monkeypatch):
    """Review finding 3. SIGKILL cannot be caught, but it does not land on a
    process wedged in uninterruptible I/O — a hung NFS mount, a stuck device.

    The commit this test belongs to exists to remove an unbounded wait; ending
    the ladder with one puts the same hang back at the last step, with the same
    consequence. So the ladder has a final rung: give up, say so, and let the
    queue flush.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    child = _FakeChild(exits_after=None, unkillable=True)
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, []))
    monkeypatch.setattr(sys, "stdin", iter(()))

    worker = _run_proxy_in_a_worker()

    assert child.killed, "precondition: the ladder should have reached SIGKILL"
    assert not worker.is_alive(), (
        "run_proxy is still waiting on a process that SIGKILL will never reap"
    )


def test_a_healthy_session_is_never_signalled(monkeypatch):
    """The guard, and the branch that runs on every real session.

    The escalation must be armed only once the CLIENT has gone. Armed at
    startup it would SIGTERM a perfectly healthy upstream seconds into a
    session meant to last hours. It also must not be gated on joining the input
    pump: an upstream can crash while the client is still attached, and that
    returncode has to come straight back.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    # Alive across the whole grace with the client attached — a session, not a
    # corpse. An escalation not gated on the disconnect fires first.
    child = _FakeChild(exits_after=0.3)
    spawned: list[Any] = []
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, spawned))
    monkeypatch.setattr(sys, "stdin", _NeverEofStdin())

    worker = _run_proxy_in_a_worker()

    assert spawned, "run_proxy never spawned the upstream, so it never reached the wait"
    assert not worker.is_alive(), "run_proxy did not return on a clean upstream exit"
    assert not child.terminated, (
        "a healthy upstream was SIGTERMed: the escalation is armed at startup "
        "instead of at the client's disconnect"
    )
    assert not child.killed


def test_the_live_session_does_not_poll(monkeypatch):
    """Review finding 4. The first version polled `wait(timeout=…)` in a loop
    for the whole life of the session.

    CPython's own `wait(timeout=…)` polls internally, so each call is worth
    about nine wakeups; at five calls a second, per wrapped server, for as long
    as the session lasts, that is measurable CPU and it keeps a laptop out of
    deep idle. Nothing needs polling: the escalation is a timer, so the wait
    can simply block.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    child = _FakeChild(exits_after=0.3)
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, []))
    monkeypatch.setattr(sys, "stdin", _NeverEofStdin())

    _run_proxy_in_a_worker()

    assert child.wait_calls == 1, (
        f"the upstream was waited on {child.wait_calls} times for one session; "
        "a blocking wait needs exactly one and a polling loop needs many"
    )


def test_the_upstream_is_spawned_in_its_own_process_group(monkeypatch):
    """Review finding 1, at the spawn site.

    `terminate()` signals the direct child only. Wrapped servers are routinely
    launched through something that is not the server — `sh -c`, `npx`,
    `docker run` — and a wrapper that does not forward SIGTERM dies while the
    real server keeps running, holding the stdout pipe open so the output
    thread waits out its full timeout. Signalling the group needs the group to
    exist, which is what this flag creates.
    """
    _shutdown_env(monkeypatch)
    _tight_graces(monkeypatch)
    child = _FakeChild(exits_after=0.1)
    spawned: list[Any] = []
    monkeypatch.setattr(proxy_mod.subprocess, "Popen", _spawning(child, spawned))
    monkeypatch.setattr(sys, "stdin", _NeverEofStdin())

    _run_proxy_in_a_worker()

    assert spawned and spawned[0].get("start_new_session") is True, (
        "the upstream shares our process group, so only the direct child can "
        "be signalled and a non-forwarding wrapper leaks the real server"
    )


@pytest.mark.integration
def test_a_wrapper_that_swallows_sigterm_does_not_leak_the_real_server(tmp_path):
    """Review finding 1, against real processes — the fakes above can only
    grade the flag and the fallback.

    `sh -c 'server & wait'` is the shape: the shell does not exec, so it does
    not forward SIGTERM. Signalling the direct child kills the shell and leaves
    the actual server running, holding the stdout pipe open — so `t_out.join`
    burns its whole timeout and a process is leaked per session. Measured
    before the fix: 3.24s to shut down against 1.22s for a direct child.
    """
    pidfile = tmp_path / "grandchild.pid"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        # Ignores stdin entirely: the upstream this whole ladder is about.
        "time.sleep(60)\n"
    )
    wrapper = f"{sys.executable} {grandchild} & wait"

    proc = subprocess.Popen(
        [sys.executable, "-m", "baton_proxy", "--", "sh", "-c", wrapper],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "BATON_EVENT_SINK": "stderr:", "BATON_VENDOR_ID": "toybox"},
    )
    leaked: int | None = None
    try:
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "the wrapped server never started"
        leaked = int(pidfile.read_text())

        # How an MCP client shuts a stdio server down.
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=15)

        # The grandchild must be gone. A signal to the direct child only would
        # have killed `sh` and left this one running for its full 60s.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(leaked, 0)
            except ProcessLookupError:
                leaked = None
                break
            time.sleep(0.05)
        assert leaked is None, (
            "the real server outlived the proxy: only the wrapper was signalled, "
            "so every session leaks a process and holds the stdout pipe open"
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        if leaked is not None:
            # `os.kill`, never `os.killpg`. Under a mutation that drops
            # `start_new_session`, the leaked pid sits in the TEST RUNNER's
            # group, so a killpg here takes down pytest and the shell that
            # started it — which is exactly what it did, twice, before this
            # comment existed.
            with contextlib.suppress(OSError):
                os.kill(leaked, signal.SIGKILL)


def test_the_proxy_never_signals_its_own_process_group(monkeypatch):
    """Found by a mutation run, and more dangerous than the finding it was
    checking for.

    `_signal_upstream` signals the child's process GROUP. That is only safe
    while the child has a group of its own. If it ever shares OURS — a platform
    where `start_new_session` does nothing, or a future edit dropping the flag —
    then the group holds the proxy, the MCP client that launched it, and
    whatever else shares the terminal, and shutdown SIGKILLs the lot.

    Not hypothetical: deleting `start_new_session=True` and running this suite
    SIGTERMed the test runner itself, which is how this was found. The blast
    radius is why the guard is here and not merely implied by the flag.
    """
    killed: list[tuple[int, int]] = []
    # Read before patching: `proxy_mod.os` is the same module object this file
    # imported, so a lambda calling `os.getpgid` would call itself.
    our_pgid = os.getpgid(0)
    monkeypatch.setattr(proxy_mod.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    # The child is in our group — exactly the state the flag is meant to prevent.
    monkeypatch.setattr(proxy_mod.os, "getpgid", lambda pid: our_pgid)
    child = _FakeChild(exits_after=None)

    proxy_mod._signal_upstream(child, signal.SIGTERM)

    assert not killed, (
        f"the proxy signalled its own process group {killed}: that group holds "
        "the proxy, the client that launched it, and the user's terminal"
    )
    assert child.terminated, (
        "declining the group must not mean declining to signal at all — the "
        "direct child still has to be asked to stop"
    )

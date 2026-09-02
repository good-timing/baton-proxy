"""The one file in this kit that can send anything, and it only runs when you run it.

Everything else in `try/` reads and writes local files. This module is the
seventh match in SECURITY.md §9.1's egress grep, and it is in a file of its own
for that reason: a reviewer who wants to know what can leave the machine reads
one file whose entire purpose is leaving, rather than hunting a network call
inside the 1800 lines that do config surgery.

`kit.py` does not import this at module level. It loads it by path inside
`cmd_upload`, so `setup`, `receipt` and `uninstall` never put the sending code
on their import graph — the only command that loads the uploader is the one you
typed.

**The credential lives here, not in your MCP config entry.** That is not
cosmetic. SECURITY.md §4 tells a reviewer they may treat
`BATON_EVENT_SINK=file://…` plus the ABSENCE of `BATON_API_KEY` in the config
entry as sufficient proof that the wrap cannot deliver anywhere. Putting an
upload key in the entry would falsify the one check that section offers. So the
key is handed over separately, read from `try/upload.json`, and the running
proxy has no access to it and no code path that would use it.

Three things this cannot tell you, all of them worth knowing before trusting a
run, and all of them stated to the person rather than swallowed:

  - **Delivered is not stored.** Ingest answers 201 whether it inserted the row
    or dropped it under `ON CONFLICT (event_id) DO NOTHING`, and `event_id` is a
    global primary key — so a file already loaded under a different tenant lands
    nothing here and still reports a clean run. Knowing takes a row count bound
    to the tenant, which is a database query on our side. This prints what it
    sent, never what is in the console.
  - **A second run is safe and mostly silent.** Same dedup: resending the whole
    file adds only what is new, and reports the same numbers either way.
  - **`vendor_id` is never rewritten.** Only `tenant_id`. The dashboard's server
    picker keys off the per-event `vendor_id`, so a capture keeps reading as the
    server it came from inside whatever workspace it lands in.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# The handed-over file. It is not in this repository and never should be: it
# carries a live key. Anyone reading this in a fresh clone will not have one,
# and `refusal()` below is what they get.
CREDENTIALS_NAME = "upload.json"

# Ingest rate-limits per API key (300/min, fixed window). 4/s = 240/min, which
# leaves headroom for the window boundary without assuming how many instances
# are serving. Matches `baton-internal/scripts/replay_events.py`, which is the
# same drive run from our side.
DEFAULT_RATE = 4.0

# A 429 is the server saying wait, not fail. Past this many waits on one event
# the budget is gone rather than busy, and grinding the rest of the file through
# it would take minutes to say so.
MAX_THROTTLE_RETRIES = 5

# Transport faults and 5xx get a short exponential backoff. Anything past this
# is a real failure of that one event, counted and reported.
TRANSIENT_BACKOFFS = (1.0, 2.0, 4.0)

REQUIRED_FIELDS = ("console_url", "api_key", "tenant_id")


def open_request(req: urllib.request.Request) -> Any:
    """The kit's only network call, on purpose in a function of its own.

    §9.1 tells a reviewer to grep for `urlopen\\(` and promises a fixed count.
    Written inline as a default argument (`opener=urllib.request.urlopen`) the
    call would be invisible to that grep — no paren follows the name — and the
    kit would have gained an egress the document's own check cannot see. That is
    a worse outcome than a seventh match, so the seam for the tests is here, and
    the literal the reviewer greps for is on the line below.
    """
    return urllib.request.urlopen(req)


class Terminal(Exception):
    """A failure every remaining line would repeat — stop, do not grind.

    The distinction matters to the person watching: a wrong key 401s identically
    on all 4000 lines, and printing that 4000 times is noise dressed as effort.
    """


class NoCredentials(Exception):
    """No `upload.json`, or one that is not usable. Not an error — a branch."""


def refusal(where: Path) -> str:
    """What someone without a credential file is told, which is most people.

    Deliberately not phrased as a failure. Upload works only for a trial we
    provisioned in advance; email works for anyone, needs nothing from us first,
    and is what the receipt offers when this file is absent. Someone who typed
    `upload` on a hunch should leave this message knowing the kit is fine and
    the other path is open, not thinking they broke something.
    """
    return (
        f"there is no {where.name} beside kit.py, so there is nowhere for this to go.\n"
        "\n"
        "  `upload` sends your capture straight to a Baton workspace, and it only\n"
        "  works if we created one for you and handed you the file — it holds the\n"
        "  console address, a key, and the workspace it belongs to. Nothing in the\n"
        "  kit can create one, and it will not invent a destination.\n"
        "\n"
        "  If you did not get that file, nothing is wrong: run `receipt` and email\n"
        "  the capture instead. That path works for anyone and needs nothing from\n"
        "  us in advance."
    )


def load_credentials(directory: Path) -> dict[str, str]:
    """Read and validate the handed-over file.

    Every failure here raises `NoCredentials`, including a malformed one — the
    person cannot fix a schema they never wrote, and the useful thing to say is
    the same in all three cases: this is not usable, use the other path.
    """
    path = directory / CREDENTIALS_NAME
    if not path.exists():
        raise NoCredentials(refusal(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise NoCredentials(f"{path.name} could not be read ({e}). {refusal(path)}") from e
    if not isinstance(data, dict):
        raise NoCredentials(f"{path.name} is not a JSON object. {refusal(path)}")
    missing = [f for f in REQUIRED_FIELDS if not data.get(f)]
    if missing:
        # Name the keys, never the values — the same rule the rest of the kit
        # follows for anything credential-shaped.
        raise NoCredentials(f"{path.name} is missing {', '.join(missing)}. {refusal(path)}")
    return {k: str(v) for k, v in data.items()}


def endpoint(console_url: str) -> str:
    return console_url.rstrip("/") + "/v0/events"


def post_event(
    url: str,
    headers: dict[str, str],
    event: dict[str, Any],
    lineno: int,
    *,
    sleep: Any = None,
    opener: Any = None,
) -> None:
    """POST one envelope, retrying only what is worth retrying.

    `sleep` and `opener` are injected so the retry ladder is testable without a
    network or a wall clock. Nothing else in the kit needs that, and a retry
    ladder that has never been exercised is a guess.

    They default to None rather than to the functions themselves: a default
    evaluated at `def` time captures `open_request` permanently, so a test that
    replaces the module attribute would still reach the network — which is a
    test that passes while sending real traffic from someone's machine.
    """
    sleep = sleep or time.sleep
    opener = opener or open_request
    body = json.dumps(event).encode()
    throttled = 0
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener(req) as r:
                r.read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                throttled += 1
                if throttled > MAX_THROTTLE_RETRIES:
                    raise Terminal(
                        f"line {lineno}: still rate-limited after {MAX_THROTTLE_RETRIES} "
                        "waits. Nothing is wrong with the file — wait a few minutes and "
                        "run it again; what already landed will not land twice."
                    ) from e
                # The server always sends Retry-After, whole seconds, floor 1.
                sleep(float(e.headers.get("Retry-After", 1) or 1))
                continue
            if e.code in (401, 403):
                # 403 is also how an over-quota or suspended tenant is refused,
                # which is why this stops the run rather than retrying: both
                # readings mean every remaining line fails identically.
                raise Terminal(
                    f"line {lineno}: the console refused this key (HTTP {e.code}). Every "
                    "line would fail the same way. Send us the message you see here and "
                    f"the {CREDENTIALS_NAME} we gave you may need replacing — do not edit "
                    "it yourself."
                ) from e
            if e.code == 413:
                # Per-event and never fixable by re-sending: this one event's
                # body is over the limit and will be over it every time. Counted
                # and skipped so the other 3999 still go.
                raise
            if e.code >= 500 and attempt < len(TRANSIENT_BACKOFFS):
                sleep(TRANSIENT_BACKOFFS[attempt])
                attempt += 1
                continue
            raise
        except urllib.error.URLError:
            if attempt < len(TRANSIENT_BACKOFFS):
                sleep(TRANSIENT_BACKOFFS[attempt])
                attempt += 1
                continue
            raise


def send(
    events_path: Path,
    creds: dict[str, str],
    *,
    rate: float = DEFAULT_RATE,
    emit: Any = None,
    sleep: Any = None,
    monotonic: Any = None,
    opener: Any = None,
) -> dict[str, Any]:
    """Send every line of the capture. Returns counts; prints progress.

    Rewrites `tenant_id` on each envelope and nothing else. The file sink writes
    the same body `HttpSink` POSTs, so there is no envelope to build here — the
    lines are already the request bodies.
    """
    emit = emit or print
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    opener = opener or open_request
    url = endpoint(creds["console_url"])
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {creds['api_key']}",
    }
    interval = 1.0 / rate if rate and rate > 0 else 0.0

    delivered = failed = skipped = 0
    sessions: set[str] = set()
    oversized: list[int] = []
    # An absolute deadline rather than a sleep after each POST, so pacing absorbs
    # request latency instead of adding to it.
    next_send = monotonic()

    with open(events_path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                # The receipt tolerates a truncated final line (the proxy killed
                # mid-write), so this must too, and for the same reason.
                skipped += 1
                continue

            event["tenant_id"] = creds["tenant_id"]
            sessions.add(str(event.get("session_id", "")))

            if interval:
                wait = next_send - monotonic()
                if wait > 0:
                    sleep(wait)
                next_send = max(next_send + interval, monotonic())

            try:
                post_event(url, headers, event, lineno, sleep=sleep, opener=opener)
            except urllib.error.HTTPError as e:
                failed += 1
                if e.code == 413:
                    oversized.append(lineno)
                continue
            except urllib.error.URLError as e:
                if delivered == 0:
                    # Nothing has ever landed, so this is a wrong address or a
                    # console that is not up — not a blip. Say so now rather
                    # than after four thousand backoffs.
                    raise Terminal(
                        f"cannot reach {url} ({e.reason}) and nothing has landed yet. "
                        "Check that you are online; if you are, the address in "
                        f"{CREDENTIALS_NAME} is wrong and we should send you a new one."
                    ) from e
                failed += 1
                continue

            delivered += 1
            if delivered % 250 == 0:
                emit(f"  sent {delivered}…")

    return {
        "delivered": delivered,
        "failed": failed,
        "skipped": skipped,
        "sessions": len({s for s in sessions if s}),
        "oversized_lines": oversized,
    }

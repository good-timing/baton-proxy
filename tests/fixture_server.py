"""Minimal stdio MCP server used by the proxy tests as the upstream fixture.

Hand-rolls JSON-RPC over newline-delimited stdio so there are no third-party
test dependencies and the upstream behavior is fully under our control. The
response payloads are shared with the Streamable HTTP fixture via
``fixture_responses.result_for`` so both transports produce the same friction
event stream; this file only handles the stdio framing.
"""

from __future__ import annotations

import json
import sys

from fixture_responses import result_for


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = result_for(req)
        # None = a notification (no id); the stdio transport sends nothing back.
        if result is not None:
            send(result)


if __name__ == "__main__":
    main()

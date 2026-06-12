#!/usr/bin/env bash
# Register baton-proxy as a Claude Code MCP server, wrapping the in-repo
# fixture upstream (tests/fixture_server.py). Defaults to a local file
# sink — so after running this and driving a few tool calls in Claude,
# `cat /tmp/baton-proxy-example.jsonl` shows the friction events.
#
# To send events to a real Console instead (or in addition), set
# BATON_EVENT_SINK before invoking. Examples:
#   BATON_EVENT_SINK="https://console.example.com" ./setup.sh
#   BATON_EVENT_SINK="stderr:,file:///tmp/events.jsonl" ./setup.sh
set -euo pipefail

NAME="baton-proxy-example"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
LOG_FILE="/tmp/${NAME}.log"
EVENTS_FILE="/tmp/${NAME}.jsonl"

EVENT_SINK="${BATON_EVENT_SINK:-file://$EVENTS_FILE}"
TENANT_ID="${BATON_TENANT_ID:-example-tenant}"
CONSENT_TOKEN="${BATON_CONSENT_TOKEN:-example-elicitation}"
# api_key only matters if EVENT_SINK is http(s)://; harmless for file/stderr.
API_KEY="${BATON_API_KEY:-dev-key}"

if [ ! -x "$PYTHON" ]; then
  echo "error: $PYTHON not found." >&2
  echo "create it with: cd $REPO && python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

claude mcp remove "$NAME" -s user >/dev/null 2>&1 || true

claude mcp add "$NAME" -s user \
  -e PYTHONPATH="$REPO/src" \
  -e BATON_EVENT_SINK="$EVENT_SINK" \
  -e BATON_API_KEY="$API_KEY" \
  -e BATON_TENANT_ID="$TENANT_ID" \
  -e BATON_CONSENT_TOKEN="$CONSENT_TOKEN" \
  -e BATON_VENDOR_ID="e2eproxy" \
  -e BATON_PROXY_LOG_FILE="$LOG_FILE" \
  -- "$PYTHON" -m baton_proxy -- "$PYTHON" "$REPO/tests/fixture_server.py"

# Reset logs so each run starts clean.
: > "$LOG_FILE"
case "$EVENT_SINK" in
  file://*) : > "${EVENT_SINK#file://}" ;;
esac

cat <<EOF

registered '$NAME' (user scope):
  proxy log:  $LOG_FILE
  event sink: $EVENT_SINK
  tenant:     $TENANT_ID
  vendor:     e2eproxy -> tool 'e2eproxy_annotate'

next:
  1. quit + relaunch Claude Code (mid-session reload won't pick this up)
  2. follow $(dirname "$0")/instructions.md
  3. when done: bash $(dirname "$0")/unregister.sh
EOF

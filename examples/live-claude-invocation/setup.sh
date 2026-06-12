#!/usr/bin/env bash
# Register baton-proxy as a Claude Code MCP server, wrapping the in-repo
# fixture upstream (tests/fixture_server.py). Defaults to a file sink at
# /tmp/baton-proxy-example.jsonl so this example can run alongside a
# main install (which would default to /tmp/baton-proxy.jsonl) without
# the two streams interleaving.
#
# To send events somewhere else (or in addition), set BATON_EVENT_SINK
# before invoking. Examples:
#   BATON_EVENT_SINK="https://console.example.com" \
#     BATON_API_KEY="..." BATON_TENANT_ID="acme" \
#     BATON_CONSENT_TOKEN="real-uuid" ./setup.sh
#   BATON_EVENT_SINK="stderr:,file:///tmp/baton-proxy-example.jsonl" ./setup.sh
set -euo pipefail

NAME="baton-proxy-example"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
LOG_FILE="/tmp/${NAME}.log"
EVENTS_FILE="/tmp/${NAME}.jsonl"

EVENT_SINK="${BATON_EVENT_SINK:-file://$EVENTS_FILE}"

if [ ! -x "$PYTHON" ]; then
  echo "error: $PYTHON not found." >&2
  echo "create it with: cd $REPO && python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

claude mcp remove "$NAME" -s user >/dev/null 2>&1 || true

# Pass through any BATON_* env vars the operator set; otherwise the proxy
# uses its zero-config defaults (tenant_id=local, consent_token=local) —
# which are fine for the local file sink default and refused if the
# operator opts up to an http(s) sink.
ENV_ARGS=(
  -e PYTHONPATH="$REPO/src"
  -e BATON_EVENT_SINK="$EVENT_SINK"
  -e BATON_VENDOR_ID="e2eproxy"
  -e BATON_PROXY_LOG_FILE="$LOG_FILE"
)
for var in BATON_API_KEY BATON_TENANT_ID BATON_CONSENT_TOKEN; do
  if [ -n "${!var:-}" ]; then
    ENV_ARGS+=(-e "$var=${!var}")
  fi
done

claude mcp add "$NAME" -s user \
  "${ENV_ARGS[@]}" \
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
  vendor:     e2eproxy -> tool 'e2eproxy_annotate'

next:
  1. quit + relaunch Claude Code (mid-session reload won't pick this up)
  2. follow $(dirname "$0")/instructions.md
  3. when done: bash $(dirname "$0")/unregister.sh
EOF

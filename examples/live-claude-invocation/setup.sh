#!/usr/bin/env bash
# Register baton-proxy as a Claude Code MCP server pointed at the in-repo
# fixture upstream (tests/fixture_server.py). After running this, restart
# Claude Code and follow instructions.md.
#
# To also exercise the ingest path, set BATON_CONSOLE_URL to a running
# baton-console / baton-ingest endpoint before invoking this script. Left
# unset, the proxy still injects + emits per spec; ingest delivery just
# fails open and the elicitation test (the point of this example) still
# works.
set -euo pipefail

NAME="baton-proxy-example"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
LOG_FILE="/tmp/${NAME}.log"

CONSOLE_URL="${BATON_CONSOLE_URL:-http://localhost:8000}"
TENANT_ID="${BATON_TENANT_ID:-example-tenant}"
API_KEY="${BATON_API_KEY:-dev-key}"

if [ ! -x "$PYTHON" ]; then
  echo "error: $PYTHON not found." >&2
  echo "create it with: cd $REPO && python3 -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

claude mcp remove "$NAME" -s user >/dev/null 2>&1 || true

claude mcp add "$NAME" -s user \
  -e PYTHONPATH="$REPO/src" \
  -e BATON_CONSOLE_URL="$CONSOLE_URL" \
  -e BATON_API_KEY="$API_KEY" \
  -e BATON_TENANT_ID="$TENANT_ID" \
  -e BATON_CONSENT_TOKEN="example-elicitation" \
  -e BATON_VENDOR_ID="e2eproxy" \
  -e BATON_PROXY_LOG_FILE="$LOG_FILE" \
  -- "$PYTHON" -m baton_proxy -- "$PYTHON" "$REPO/tests/fixture_server.py"

# Reset log so each run starts clean.
: > "$LOG_FILE"

cat <<EOF

registered '$NAME' (user scope):
  log:     $LOG_FILE
  console: $CONSOLE_URL
  tenant:  $TENANT_ID
  vendor:  e2eproxy -> tool 'e2eproxy_annotate'

next:
  1. quit + relaunch Claude Code (mid-session reload won't pick this up)
  2. follow $(dirname "$0")/instructions.md
  3. when done: bash $(dirname "$0")/unregister.sh
EOF

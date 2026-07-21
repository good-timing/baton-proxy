#!/usr/bin/env bash
# All-in-one sandbox entrypoint: co-locate the Baton ExtMCP processor (h2c :4445)
# next to agentgateway (:8080). The processor emits captured signal to
# BATON_EVENT_SINK; the default includes stderr: so capture is visible right in
# the container logs, plus a file for inspection.
set -euo pipefail

# Sandbox-friendly defaults (override at `docker run -e ...`). VENDOR_ID is the
# only field baton-extmcp hard-requires.
: "${BATON_VENDOR_ID:=sandbox}"
: "${BATON_TENANT_ID:=sandbox}"
: "${BATON_EVENT_SINK:=stderr:,file:///tmp/baton-events.jsonl}"
export BATON_VENDOR_ID BATON_TENANT_ID BATON_EVENT_SINK

# Baton capture callout — emits to BATON_EVENT_SINK (stderr shows in logs).
baton-extmcp &

# Wait for the processor to accept connections before the gateway dials it.
for _ in $(seq 1 50); do
  (exec 3<>/dev/tcp/127.0.0.1/4445) 2>/dev/null && { exec 3>&-; break; }
  sleep 0.2
done

# agentgateway front — spawns the stdio toy backend, serves MCP on :8080/mcp.
exec /app/agentgateway -f /app/agentgateway.yaml

#!/usr/bin/env bash
set -euo pipefail
NAME="baton-proxy-example"
if claude mcp remove "$NAME" -s user 2>/dev/null; then
  echo "unregistered '$NAME'."
else
  echo "'$NAME' was not registered (nothing to do)."
fi

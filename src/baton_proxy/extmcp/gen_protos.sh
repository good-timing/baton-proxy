#!/bin/bash
# Regenerate ext_mcp_pb2.py + ext_mcp_pb2_grpc.py from the vendored ext_mcp.proto.
#
# The proto is pinned to agentgateway v1.3.1 (git dbaaf7e). Re-run this only when
# re-vendoring a newer agentgateway ext_mcp.proto. Needs grpcio-tools:
#   pip install "grpcio-tools>=1.60"
#
# grpc_tools emits `import ext_mcp_pb2` (top-level), which does NOT resolve inside
# the baton_proxy.extmcp package — so we rewrite it to a package-relative import
# after generation. Keep this fix in lockstep with any regeneration.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-python3}"

"$PY" -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. ext_mcp.proto

# Re-apply the package-relative import fix (see module docstring in __init__.py).
"$PY" - <<'FIX'
import pathlib
p = pathlib.Path("ext_mcp_pb2_grpc.py")
s = p.read_text()
s2 = s.replace("import ext_mcp_pb2 as ext__mcp__pb2",
               "from baton_proxy.extmcp import ext_mcp_pb2 as ext__mcp__pb2")
assert s2 != s, "import line not found — grpc_tools output changed shape"
p.write_text(s2)
print("patched package-relative import in ext_mcp_pb2_grpc.py")
FIX

echo "generated ext_mcp_pb2.py ext_mcp_pb2_grpc.py"

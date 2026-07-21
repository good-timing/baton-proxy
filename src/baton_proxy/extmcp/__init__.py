"""Baton ExtMCP processor — capture at an agentgateway seam via the ExtMcp
gRPC callout, reusing baton-proxy's emitter/sink/config/consent/scrub core.

This subpackage is the ONLY part of baton-proxy that depends on gRPC. Install
it with the optional extra:  ``pip install baton-proxy[extmcp]``. The base
package never imports anything under here, so ``pip install baton-proxy`` stays
zero-dependency.

Entrypoint: ``baton-extmcp`` (see server.main). Contract: ext_mcp.proto
(agentgateway v1.3.1, git dbaaf7e).
"""

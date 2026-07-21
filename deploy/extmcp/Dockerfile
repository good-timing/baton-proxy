# Baton ExtMCP processor — SIDECAR (the production artifact).
#
# Runs ONLY the processor. Co-locate next to agentgateway (same pod / same node);
# the gateway dials it on :4445 over h2c (cleartext HTTP/2). Captured signal goes
# to BATON_EVENT_SINK — the processor is not in the network path and holds no
# backend, so it scales with # gateways, not call volume.
#
# Build from the baton-proxy repo root:
#   docker build -f deploy/extmcp/Dockerfile -t baton-extmcp .
#
# Required runtime env (see src/baton_proxy/extmcp/server.py for the full list):
#   BATON_VENDOR_ID, BATON_EVENT_SINK, BATON_API_KEY, BATON_CONSENT_TOKEN
FROM python:3.11-slim

WORKDIR /app

# Install baton-proxy with the gRPC extra from source (pre-release). Once a
# release with the [extmcp] extra is on PyPI, replace the two lines below with:
#   RUN pip install --no-cache-dir "baton-proxy[extmcp]==<version>"
COPY . /src
RUN pip install --no-cache-dir "/src[extmcp]" && rm -rf /src

# h2c listen port (matches the gateway's mcpGuardrails processor host).
ENV BATON_EXTMCP_PORT=4445
EXPOSE 4445

# baton-extmcp reads all config from env; drains the emitter on SIGTERM.
CMD ["baton-extmcp"]

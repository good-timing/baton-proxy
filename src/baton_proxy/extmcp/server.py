"""``baton-extmcp`` entrypoint — serve the ExtMcp processor over h2c.

Config is env-driven (12-factor / container-friendly). Reuses baton-proxy's
Config.from_env() for the emit envelope + consent guard, plus a few ExtMCP-only
knobs. Deploy co-located (sidecar / same-node) with agentgateway; the gateway
dials this on 127.0.0.1:<port> over cleartext HTTP/2.

Env:
  BATON_VENDOR_ID           (required) vendor label for captured signal
  BATON_TENANT_ID           tenant (default "local")
  BATON_EVENT_SINK          e.g. https://console.baton.cloud  (default stderr+file)
  BATON_API_KEY             bearer for http(s) sinks
  BATON_CONSENT_TOKEN       per-install consent (http sink refuses the "local" placeholder)
  BATON_EXTMCP_PORT         h2c listen port (default 4445)
  BATON_EXTMCP_SESSION_HEADER    header carrying the session id (default mcp-session-id)
  BATON_EXTMCP_IDENTITY_HEADERS  comma-list of identity headers to capture
                                 (default x-gw-ims-user-id,x-gw-ims-org-id)
  BATON_EXTMCP_MAX_WORKERS  gRPC thread pool size (default 8)
  BATON_EXTMCP_FAIL_SLOW    sleep N sec per hook — FailOpen fault-injection (default 0)
  BATON_EXTMCP_DENY_ALL     "1" to deny every tools/call — gating fault-injection
  BATON_EXTMCP_GRACE        shutdown grace seconds (default 5)
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

logger = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v else default


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("BATON_EXTMCP_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        from concurrent import futures

        import grpc

        from baton_proxy.config import Config
        from baton_proxy.emitter import Emitter
        from baton_proxy.extmcp import ext_mcp_pb2_grpc as pb_grpc
        from baton_proxy.extmcp.servicer import ExtMcpProcessor
    except ImportError as e:  # grpc not installed
        sys.stderr.write(
            f"baton-extmcp needs the gRPC extra: pip install 'baton-proxy[extmcp]'  ({e})\n"
        )
        raise SystemExit(2) from e

    config = Config.from_env()
    emitter = Emitter(config)
    emitter.start()  # raises loudly on placeholder-consent + http sink

    port = int(_env("BATON_EXTMCP_PORT", "4445"))
    max_workers = int(_env("BATON_EXTMCP_MAX_WORKERS", "8"))
    grace = float(_env("BATON_EXTMCP_GRACE", "5"))
    identity_headers = tuple(
        h.strip()
        for h in (
            _env("BATON_EXTMCP_IDENTITY_HEADERS", "x-gw-ims-user-id,x-gw-ims-org-id") or ""
        ).split(",")
        if h.strip()
    )
    servicer = ExtMcpProcessor(
        emitter,
        session_header=_env("BATON_EXTMCP_SESSION_HEADER", "mcp-session-id"),
        identity_headers=identity_headers,
        fail_slow=float(_env("BATON_EXTMCP_FAIL_SLOW", "0") or "0"),
        deny_all=_env("BATON_EXTMCP_DENY_ALL", "") == "1",
    )

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb_grpc.add_ExtMcpServicer_to_server(servicer, server)
    server.add_insecure_port(f"[::]:{port}")  # insecure == h2c (what agentgateway dials)
    server.start()
    logger.info(
        "baton-extmcp on h2c :%d  vendor=%s tenant=%s sink=%s session_header=%s",
        port,
        config.vendor_id,
        config.tenant_id,
        config.event_sink,
        servicer._session_header,
    )

    stop = threading.Event()

    def _shutdown(signum, _frame):
        logger.info("baton-extmcp received signal %s — draining", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    stop.wait()
    # Stop accepting new RPCs, let in-flight ones finish, then drain the emitter.
    server.stop(grace).wait(timeout=grace)
    emitter.stop(timeout=grace)
    logger.info("baton-extmcp stopped")


if __name__ == "__main__":
    main()

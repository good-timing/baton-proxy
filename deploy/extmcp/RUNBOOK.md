# Baton ExtMCP — integration runbook

For an engineering team running **agentgateway** who want intent + friction capture
at the gateway seam. Config-only on your side: attach one guardrail policy pointing
at a co-located Baton processor. No proxy in the network path, no SDK in your
backends, no fork of the gateway.

---

## 1. What it does

The processor is an **ExtMcp external processor** — the gateway calls it out over
gRPC (h2c) at the MCP method layer, exactly like Envoy `ext_authz`:

```
   MCP client (Copilot / Cowork / Desktop / Claude Code)
        │  MCP over Streamable-HTTP
        ▼
   agentgateway ──(tools/list: response)──► Baton processor : inject user_goal /
        │                                                      expected_result into
        │                                                      each tool's schema
        ├──(tools/call: request)──────────► Baton processor : capture intent + args +
        │                                                      identity/session, STRIP
        │                                                      the injected keys
        ▼
   your backend MCP server(s)   ◄── receives clean params (never sees the injected keys)

   Baton processor ──► BATON_EVENT_SINK (your console / your S3)   [async, off the hot path]
```

- **Injection** rides the real tool schemas; clients fill optional params even when
  they ignore instructions. The processor **strips** them before your backend runs.
- **FailOpen**: if the processor is slow or down, the gateway forwards the real call
  anyway. Capture is best-effort and never breaks work.
- **Latency**: the callout adds sub-millisecond p99 when co-located; on any real
  backend call (10–200 ms) it is <1%. Sink writes are async (background thread).

**Scope of this build — request-side capture.** The processor captures *prospective
intent on the real call* (what nothing else sees). The response/timeline facts
(tool errors, duration, retries) come from agentgateway's native **OTLP** export,
joined downstream — a separate track (see §7). agentgateway v1.3.1 gives the
response hook no request-correlation id, so the processor does **not** register a
response hook; there is no fragile in-processor pairing.

---

## 2. Prerequisites

- **agentgateway ≥ v1.3.x** (ExtMCP floor; validated on v1.3.1, git `dbaaf7e`).
- Ability to **co-locate** the processor with the gateway (sidecar container / same
  node). The gateway↔processor hop is ~99% of the added latency; keep it same-host.
- A **sink** the processor can reach: the Baton console URL (hosted), or an S3
  bucket you own (residency mode — §6).

---

## 3. The gateway policy (config on your side)

Attach the processor as an `mcpGuardrails` processor with **`tools/list: response`**
(inject) and **`tools/call: request`** (capture + strip). Run **FailOpen**.

**Kubernetes CRD form** (`AgentgatewayPolicy`):

```yaml
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayPolicy
metadata: { name: baton-capture }
spec:
  targetRefs:
    - { group: agentgateway.dev, kind: AgentgatewayBackend, name: <your-mcp-backend> }
  backend:
    mcp:
      guardrails:
        processors:
          - remote:
              backendRef: { name: baton-extmcp, port: 4445 }   # the co-located sidecar
              failureMode: FailOpen
            methods:
              tools/list: Response      # inject user_goal / expected_result
              tools/call: Request       # capture + strip
```

**Standalone form** (route-level `policies.mcpGuardrails`, lowercase phases) — see
`agentgateway.sandbox.yaml` in this directory for a complete, runnable example.

> Note: do **not** set `tools/call: Full`/`Response`. This build is request-side only.

---

## 4. Deploy the processor (sidecar — production)

Build the sidecar image (from the baton-proxy repo root):

```bash
docker build -f deploy/extmcp/Dockerfile -t baton-extmcp .
```

Run it co-located with the gateway (k8s sidecar container, or same-node service),
listening on `:4445`. All config is env:

| Env | Required | Meaning |
|---|---|---|
| `BATON_VENDOR_ID` | ✅ | vendor label for captured signal (e.g. `acme-mcp`) |
| `BATON_TENANT_ID` | | tenant (e.g. `customer:acme`); default `local` |
| `BATON_EVENT_SINK` | | where signal goes: `https://console.baton.cloud` \| `s3://bucket/prefix` \| `file://…` \| `stderr:` (comma-separated = fan-out) |
| `BATON_API_KEY` | for http(s) | bearer for the console sink (from your secret store) |
| `BATON_CONSENT_TOKEN` | for remote | per-install consent; the processor **refuses** to ship to a remote sink while this is the `local` placeholder |
| `BATON_PAYLOAD_SINK` | | residency split — raw payload here (e.g. `s3://your-bucket`), metadata to `BATON_EVENT_SINK` (§6) |
| `BATON_EXTMCP_PORT` | | h2c port (default `4445`) |
| `BATON_EXTMCP_SESSION_HEADER` | | header carrying the session id (default `mcp-session-id`) |
| `BATON_EXTMCP_IDENTITY_HEADERS` | | comma-list of identity headers to capture (default `x-gw-ims-user-id,x-gw-ims-org-id`) |
| `BATON_EXTMCP_FAIL_SLOW` / `BATON_EXTMCP_DENY_ALL` | | fault-injection knobs for FailOpen testing |

Secrets (`BATON_API_KEY`, `BATON_CONSENT_TOKEN`) come from your secret manager
(e.g. Cloud Run `--set-secrets`, k8s Secret) — the processor only reads env.

---

## 5. Sandbox quickstart (self-contained validation)

The all-in-one image bundles the gateway + processor + a toy backend so you can
confirm the mechanism in one run, then swap the toy backend for yours.

```bash
# build + run (capture streams to container logs via the default stderr sink)
docker build -f deploy/extmcp/Dockerfile.sandbox -t baton-extmcp-sandbox .
docker run --rm -p 8080:8080 -e BATON_VENDOR_ID=sandbox baton-extmcp-sandbox

# in another shell — drive it and assert inject → strip → capture:
python deploy/extmcp/smoke_extmcp.py --url http://127.0.0.1:8080/mcp
```

To assert the emitted capture too, run the container with a file sink on a mounted
volume and pass `--events-file` to the smoke (see the header of `smoke_extmcp.py`).

**FailOpen check**: kill the processor and re-run a `tools/call` — it must still
succeed (capture lost, work preserved).

---

## 6. Data path & residency

The sink is a **config flip**, not a code change:

- **Hosted (default to start):** `BATON_EVENT_SINK=https://console.baton.cloud` +
  `BATON_API_KEY`. Signal (metadata + payload) goes to the Baton console.
- **Raw stays in your walls (residency split):** set
  `BATON_EVENT_SINK=https://console.baton.cloud` **and**
  `BATON_PAYLOAD_SINK=s3://your-bucket`. The raw **payload** (intent text, args) is
  written to **your** S3 bucket at `{prefix}/{tenant}/{session}/{event_id}.json`;
  only the **metadata** envelope + an `s3://…` reference reaches the console. Data
  plane in your perimeter, control plane in Baton's. (Needs the `[s3]` extra —
  `pip install "baton-proxy[s3]"`, already in the image if you add boto3.)
- **Fully in your walls:** `BATON_EVENT_SINK=s3://your-bucket` (everything to S3) or
  `file://…` for an air-gapped run; sync rollups later.
- **Zero-access start (Tier 0):** you already emit OTLP by default — hand us a
  **sanitized sample** and we render dashboards on your data with no deployment.

The remote-consent guard treats `http`, `https`, and `s3` as remote: the processor
won't ship to any of them while `BATON_CONSENT_TOKEN` is the placeholder.

---

## 7. What's captured now vs. the OTLP-later track

**Now (this build):** prospective **intent** (`user_goal`/`expected_result`), the
real **tool + clean args**, **identity + session** from headers — on every real
`tools/call`. Emitted as `surface_snapshot` (the vendor surface + what we injected),
`tool_call_start`, and a first-per-session proactive `annotation`.

**Timeline (separate track):** tool status/errors, duration, retries, and
result-body silent-failure detection come from agentgateway's **native OTLP** export
— which the gateway already correlates (trace/span, session, tool) — joined to the
intent stream downstream. This needs no gateway change; point OTLP at a collector.

---

## 8. Correlation — the one honest gap

We probed v1.3.1 directly: on the **response** hook, `mcp_response` carries no
JSON-RPC id, no session, no tool; `metadata_context` is empty; and metadata set on
`McpRequestResult.metadata` does **not** propagate to the response. So a processor
**cannot** pair a response to its request under concurrency. Rather than ship a
fragile FIFO, we made capture **request-side only** (fully correct, concurrency-safe)
and take the timeline from OTLP (§7).

The **only** capability this defers is in-processor result-body silent-failure
detection. If we want that in-processor later, the minimal, protocol-native ask is:
**preserve the JSON-RPC `id` on `McpResponse`** (or thread `McpRequestResult.metadata`
into the paired response `metadata_context`). Not a precondition for anything here.

---

## 9. Open questions for the deep-dive

1. Your deployed agentgateway **binary version** (need ≥ v1.3.x).
2. Your actual **identity header names** (we default `x-gw-ims-*`; set
   `BATON_EXTMCP_IDENTITY_HEADERS` / `BATON_EXTMCP_SESSION_HEADER` to match).
3. **Routing:** do your non-code surfaces (Copilot / Cowork) actually send MCP
   traffic **through** this gateway? The seam only sees what transits it.
4. **OTLP content:** can your OTLP export carry request/response **content**, or just
   metadata? That decides whether the timeline (§7) needs anything from you.

# Security Policy

`baton-proxy` sits between an MCP client and an MCP server on a developer's own
machine. It launches the real server as a child process (or bridges to a remote
one over Streamable HTTP), injects an annotation tool into the handshake, and
emits events describing tool use to one or more sinks. It therefore sees every
tool call and result that passes through it, and it runs with the privileges of
the person who configured it. Security disclosures are taken seriously.

## Supported versions

| Version | Supported |
|---|---|
| 0.5.x (current public line; pre-1.0 = no API stability promise) | ✅ |
| < 0.5 | ❌ — please reproduce against the current release first |

## Reporting a vulnerability

**Please report security issues privately. Do not open a public GitHub issue.**

Email: **security@goodtiming.ai**

We aim to:

- **Acknowledge receipt within 3 business days.**
- **Provide an initial assessment within 7 business days.**
- **Coordinate a fix and disclosure timeline** appropriate to the severity,
  typically within **90 days** of confirmed receipt.

When reporting, please include:

- A description of the vulnerability and its impact.
- The affected version(s) or commit SHA.
- A minimal reproduction — an MCP server config, a proxy command line, a
  crafted tool response, a captured event, as applicable.
- Any known mitigations.
- Your preferred attribution for the eventual advisory, or "anonymous" if you
  would rather have no credit.

## Scope

In scope — everything in this repository:

- **`src/baton_proxy/`**, the published package. Notably the subprocess wrap
  (`proxy.py`), the Streamable-HTTP bridge (`transport_http.py`), the sinks and
  their egress paths (`sinks.py`), the PII scrubber (`scrub.py`), the emitter
  and its remote-sink consent guard (`emitter.py`), configuration and
  environment handling (`config.py`), and the scan driver (`scan.py`), which
  launches a headless `claude -p` on the user's own machine and billing.
- **`try/`**, the self-serve trial kit. It rewrites an entry in the user's MCP
  client config, writes a `state.json` recording that entry, and prints entries
  to a terminal an agent is reading. Its behaviour, limits and removal
  guarantee are documented in [`try/SECURITY.md`](try/SECURITY.md).
- **Supply chain for the `baton-proxy` package on PyPI** — a tampered release,
  a compromised maintainer account, or anything that ships code we did not
  publish.

Out of scope:

- Vulnerabilities in the MCP servers being wrapped. Those belong to their
  authors; please report them there.
- Vulnerabilities in the optional `boto3` dependency (`baton-proxy[s3]`) or in
  any other upstream package, unless the proxy's use of it is what creates the
  issue. The base package declares no dependencies at all.
- The Baton Console and SDK, which live in separate repositories.
- Attacks that require the attacker to already control the machine, the MCP
  client config, or the environment the proxy is launched with. Those are the
  proxy's trust boundary, not a boundary it defends.

## What we consider a vulnerability

- **Events reaching a remote sink while the consent token is still the
  install-time placeholder.** `emitter.py`'s `_guard_remote_consent` is meant to
  refuse to start in that case; a path around it is a bug.
- **A credential leaking.** `BATON_API_KEY`, the upstream bearer token the HTTP
  bridge reads from the environment, or a credential belonging to the wrapped
  server, reaching a log file, stderr, an event payload, or any endpoint other
  than the configured sink.
- **Cross-tenant labelling** — a way to make the proxy emit events under a
  `tenant_id` or `vendor_id` other than the configured one.
- **Scrubber failure within its documented ruleset.** `scrub.py` covers JWTs,
  `Bearer` tokens, `sk-` keys, AWS access-key ids, emails, phone numbers,
  Luhn-valid card numbers, and force-redacts a fixed set of key names. Output
  that still contains one of those, for content the rules should match, is a
  bug. What the scrubber deliberately does *not* cover is listed below.
- **The trial kit damaging or exposing a config.** Anything that makes
  `try/kit.py` write outside the single entry it wrapped, lose the recorded
  original, create `state.json` world-readable, or print a literal value its
  redaction rule covers (`env` values, `headers` values, the path and query of
  a `url`).
- **Any RCE, path traversal, or deserialization flaw** in the event-handling,
  config-reading or config-writing paths — including one triggered by a
  malicious *upstream server response*, since a wrapped server's output is
  parsed by the proxy before it reaches the client.

## What we do NOT consider a vulnerability

- **Business data in captured events.** The scrubber removes credentials and
  common PII patterns; it does not remove query results, table names, file
  contents or row data, and it is not intended to. This is stated in
  [`try/SECURITY.md`](try/SECURITY.md) §6, and reviewing the file before sharing
  it is the documented mitigation.
- **A credential passed as a command-line argument being printed** by the trial
  kit. Nothing can tell which argument is a secret, and blanking arguments would
  destroy the restore instructions those printouts exist to give. Documented
  limit; `env` is where credentials belong and `env` is what is protected.
- **`scan.py` launching `claude` on the user's machine.** It is an explicitly
  invoked subcommand that runs the user's own CLI against a temporary config,
  documented in the README, and it costs the user their own tokens by design.
- **Event loss under load.** The emitter's queue is bounded at 1000 events and
  drops the oldest on overflow, logging once per 100 drops (`emitter.py`).
  Deliberate overflow is the documented behaviour, not a vulnerability.
- **Reports requiring physical access** to the developer's machine.

## Related documents

[`try/SECURITY.md`](try/SECURITY.md) is a different document for a different
reader: it describes, for someone deciding whether to permit a trial, exactly
what the kit does to a machine and what does and does not leave it. It is not a
disclosure policy, and this file is not a surface map. Both point at the same
address.

Thank you for helping keep baton-proxy and its users safe.

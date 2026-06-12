# Live Claude elicitation test — example result

Date: 2026-06-11
Run mode: in-session (operator pasted steps 3a/3b/3c into a live Claude Code
session). Step 2 (full restart) was not performed before step 3 — see
"Caveats" below.

## Outcomes

| step | result | excerpt |
|---|---|---|
| 3a enumeration | ✓ | Listed `echo`, `boom`, `e2eproxy_annotate` and quoted the server's "MUST call this tool when you encounter friction" instruction. |
| 3b forward success | ✓ | `Echo: hello` |
| 3c forward error | ✓ | `MCP error -32000: boom` reached the model. |
| **3c elicitation** | **✗** | After the boom error the model's entire next message was `Errored as designed: MCP error -32000: boom.` — no `e2eproxy_annotate` call. |
| 5 session stitch | not run | `/v0/escalate` curl not executed for this run. |

## The key data point — why elicitation failed

The nudge ("MUST call this tool when you encounter an error") lost to the
`boom` tool's own description: `Always errors. Used to test tool_call_error
emission.`

When asked afterward why annotate hadn't fired, the model disclosed it was
a deliberate skip — the reasoning, verbatim:

> "boom is documented as 'Always errors. Used to test tool_call_error
> emission,' so the error is the intended behavior of a fixture, not real
> friction. Annotating it felt like noise."

When then asked "what do the instructions say?" the model re-read the
server instructions, acknowledged they don't carve out an exception for
fixture errors, and offered to annotate — but only after being prompted
twice. Per step 3c's rule ("Manual prompting to get elicitation =
elicitation failed"), this counts as ✗.

## Failure mode to flag

A fixture tool whose advertised purpose is "errors on purpose" reads to
the model as not-friction, even when the server-level instructions say
MUST. This is the specific way the nudge gets defeated in production: the
tool's own description out-prioritizes the server instruction.

This matters beyond fixtures — any real tool whose description says
something like "raises on invalid input" or "returns error for missing
permissions" could trigger the same opt-out reasoning. The nudge needs to
either (a) explicitly cover "even expected/documented errors", or (b) be
attached to the error response itself, not just the server instructions.

## Caveats

- **Session was not freshly restarted.** Step 2 was skipped; this session
  already had the tool list cached from earlier turns. A clean-restart
  rerun is still wanted before drawing strong conclusions.
- **Session is now contaminated for re-measurement.** The operator
  discussed the elicitation criterion in-band, so any subsequent boom call
  in this same session is no longer an organic measurement. A second data
  point requires a full Claude Code restart.
- **`/v0/escalate` round-trip not exercised.** The `session=<uuid>` line
  for this run should still be in `/tmp/baton-proxy-example.log`; the curl
  in step 5 can be run against it independently if a Console endpoint is
  available via `BATON_EVENT_SINK=https://...`.

# Security review — the Baton try kit

> **DRAFT — not yet accurate.** This document describes the try kit's intended
> behaviour. The setup, receipt and uninstall commands it names are not built yet.
> Do not publish this file until every claim in it is true of the code beside it.

This document is written for a security reviewer deciding whether to approve
running the Baton try kit inside your organisation. It assumes you will not talk
to us, and that you are reading this before anything is cloned.

Everything below is checkable in this repository. The last section shows you how
to re-derive it yourself in about a minute.

---

## 1. What this is

Baton is an MCP proxy. It sits between your agent client (Claude Code, Claude
Desktop) and one MCP server you already run, forwarding JSON-RPC in both
directions and recording what passed through.

The try kit is this repository's `try/` folder: a configuration recipe plus a
receipt command. It installs nothing. The proxy is the source you are reading —
there is no package fetch, no vendored copy, and no build step, so **the code you
review is the code that runs**.

**In the try configuration, nothing leaves your machine.** Section 4 is the whole
argument for that sentence, including the code paths that *could* send data and
why each one is inert.

## 2. What changes on your machine

Exactly one thing: one entry in your MCP client's configuration file
(`~/.claude.json`, or a project-local `.mcp.json`).

Before:

```json
"notion": {
  "command": "npx",
  "args": ["-y", "@notionhq/notion-mcp-server"],
  "env": { "NOTION_TOKEN": "${NOTION_TOKEN}" }
}
```

After:

```json
"notion": {
  "command": "/absolute/path/to/python3.12",
  "args": ["-m", "baton_proxy", "--", "npx", "-y", "@notionhq/notion-mcp-server"],
  "env": {
    "NOTION_TOKEN": "${NOTION_TOKEN}",
    "PYTHONPATH": "/absolute/path/to/baton-proxy/src",
    "BATON_TENANT_ID": "trial-4f2a9c11",
    "BATON_VENDOR_ID": "notion",
    "BATON_EVENT_SINK": "file:///absolute/path/to/baton-proxy/try/events.jsonl"
  }
}
```

The interpreter is written as an **absolute path** — the one that ran setup —
rather than a bare `python3`, which your MCP client would resolve against its own
`PATH`. A GUI-launched client on macOS inherits a minimal `PATH` where `python3`
is the system 3.9, which cannot run this package; the entry would fail at launch
rather than at setup. Setup refuses outright if it is run under anything older
than 3.11.

`PYTHONPATH` is what lets it find the package **in the checkout you just
reviewed, with nothing installed**. Both paths are absolute and
are written by the setup step; a relative path would resolve against whatever
directory your client happens to launch from. Verify it yourself before
restarting:

```bash
PYTHONPATH=src python3 -m baton_proxy --help
```

The server keeps its name and its environment. Your original command becomes the
proxy's argument, so the proxy starts your server as a child process and speaks
to it over stdin/stdout exactly as your client did.

Notes a reviewer should hold onto:

- **The existing entry is replaced, not duplicated.** A second entry beside the
  original would leave the agent able to call the unwrapped server, so nothing
  would be captured while everyone believed it was.
- **Credentials are not read, copied, or forwarded anywhere.** The `env` block is
  preserved verbatim, `${VAR}` references included; your MCP client expands them
  when it launches the server, as it does today. The proxy never resolves them.
  One exception, in the safe direction: the proxy strips every `BATON_`-prefixed
  variable from the child process's environment (`proxy.py`, `_child_env`), so
  your server never sees Baton's own configuration. If your server legitimately
  needs a variable of its own beginning `BATON_`, it will not receive it.
- **The change is inert until the client restarts.** An MCP client binds its
  server set at startup.
- **The original entry is recorded byte-for-byte before the write**, and the whole
  config file is backed up first. Removal restores it and prints the restored
  entry rather than claiming success.
- **Your config file's formatting is preserved** — the kit reuses the indent it
  finds and rewrites one entry. `~/.claude.json` as your client writes it comes
  back byte-for-byte identical after setup + uninstall; a hand-formatted file
  with collapsed one-line entries will have that whitespace normalized, with the
  content unchanged. Both are covered by tests.
- **Your config file's permissions are preserved.** The kit writes via a
  temporary file and an atomic rename, and copies the original file's mode onto
  the replacement — a config that was `0600` stays `0600`, through both setup and
  uninstall. Covered by a test.
- **Uninstall restores into the file as it is then**, not by copying the backup
  over it. Days pass between setup and removal and your client rewrites that file
  continuously; restoring the backup wholesale would silently discard everything
  else that changed. The backup is evidence, never the source of the restore.

## 3. What your agent sees that it did not before

This is the first thing a reviewer diffing `tools/list` will notice, so it is
stated before anything else.

**Two added tools.** Both are answered by the proxy itself; neither call reaches
your server.

- **`baton_annotate`** (`proxy.py`, `ANNOTATE_TOOL_NAME`). The agent calls it to
  record what the user was trying to do and where a tool call went wrong. Its
  only effect is a line in the local event file.
- **`baton_session_report`** (`proxy.py`, `_build_report_tool`). Renders a
  markdown summary of the current session by **reading the local event file**.
  It opens no network connection. It is injected whenever a file sink is
  configured — which the try setup does — so expect to see it
  (`report.py`, `should_inject_report_tool`).

**Two optional parameters grafted onto every upstream tool's schema** —
`user_goal` and `expected_result` (`proxy.py`, `_inject_goal_params`). They are
popped from the arguments before the call is forwarded, so **your server receives
exactly the arguments it would have received unwrapped**. Set
`BATON_INTENT_PARAM=off` to disable the injection entirely.

The one exception, stated because it is a real edge: if a call arrives before the
proxy has seen a `tools/list` in that process, it has no record of whether the
tool declares `user_goal` natively, and it strips the parameter anyway rather
than forward it (`proxy.py`, `_extract_one_goal_param` — it logs a warning when
it does). This matters only for a server that genuinely declares a parameter of
its own named `user_goal` or `expected_result`. On the normal path, where the
client lists tools before calling them, a natively-declared parameter is
recognised and forwarded untouched.

**A paragraph appended to your server's `instructions`.** The proxy appends a
short note telling the agent that `baton_annotate` exists
(`_llm_text.py`, `build_instructions_suffix`). It is appended to whatever your
server sent — never replaced.

Beyond those three additions, nothing is removed, renamed, or rewritten: tool
definitions, arguments, results and errors pass through unchanged.

## 3a. The kit's own code

Everything above describes the proxy. The kit itself is one file — `try/kit.py`,
standard library only, no imports from the proxy — providing three commands:

| command | what it touches |
|---|---|
| `setup <server>` | Reads your MCP config; copies the whole file to `try/config-backup.<timestamp>.json`; rewrites **one entry**; writes `try/state.json`. Nothing else on the machine. |
| `receipt` | Reads `try/events.jsonl` and `try/state.json`. Writes nothing, opens no connection. |
| `uninstall` | Rewrites that one entry back and deletes `try/state.json`. Leaves your events file and the backups for you to read or delete. |

It opens no network connection and starts no process — the §9 grep covers `try/`
as well as `src/` for exactly this reason. It is deliberately **not** a
`baton-proxy` subcommand: the proxy that runs in production has no ability to
write to your MCP configuration, and adding one to serve a trial would have
enlarged the surface you are auditing.

Where it refuses rather than guesses, because a wrong guess here is silent:

- the named server appears in more than one config scope
- the entry is a remote (http/sse) server, or already wrapped by someone else
- at uninstall, the entry no longer matches what setup wrote — it shows you both
  versions and changes nothing

The setup/uninstall pair is property-tested as a round trip
(`tests/test_try_kit.py`): for a corpus of config shapes, `uninstall(setup(x))`
returns the original bytes. That test is the reason the removal promise in §7 is
a guarantee rather than an intention.

## 4. What leaves your machine

**In the try configuration: nothing.** Events are appended to a local JSONL file.

The proxy nevertheless contains code that can open a network connection or start
a process, because the same source serves production deployments. A reviewer will
find these, so here is the complete list — five call sites, and this is all of
them:

| # | site | what it does | why it is inert here |
|---|---|---|---|
| 1 | `sinks.py` · `HttpSink.write` | POSTs events to `{url}/v0/events` | Built only when `BATON_EVENT_SINK` is an `http(s)://` URL. The try config sets a `file://` URL. It also **raises at startup** without `BATON_API_KEY`, which the try config does not set. |
| 2 | `sinks.py` · `S3Sink` | PUTs one object per event to an S3 bucket | Built only for an `s3://` sink. Requires `boto3`, which is an optional extra this package does not install — `dependencies = []`. Unreachable without a deliberate separate install. |
| 3 | `transport_http.py` · `StreamableHttpClient.post` | Speaks MCP over HTTPS to an upstream server | Only in `--url` mode, which is mutually exclusive with the `-- <command>` form the try config uses. When used, it connects to **the URL your own config names** — never to us. |
| 4 | `proxy.py` · `subprocess.Popen` | Starts the upstream MCP server | Runs exactly the command your config already contained. |
| 5 | `scan.py` · `subprocess.run` | Runs `claude -p` headlessly for a preflight report | Only under the `baton-proxy scan` subcommand. The try flow never invokes it. It also runs on your own Claude credentials, not ours. |

There is no telemetry, no version check, no crash reporting, no auto-update. The
proxy does not phone home on startup, on failure, or on exit.

### The guards that keep it that way

Turning on remote delivery is not a single flag:

- `Emitter._guard_remote_consent`, called from `start()` before any sink is
  built, **refuses to start** if the sink is `http`, `https` or `s3` while
  `BATON_CONSENT_TOKEN` is still the default placeholder `"local"`
  (`emitter.py`). It raises rather than degrading quietly.
- `HttpSink.__init__` raises without an API key (`sinks.py`).
- An unrecognised sink scheme raises at startup (`make_sink`). There is no
  silent fallback.

So reaching a remote endpoint requires changing at least three environment
variables in the config entry, and the proxy fails loudly at any halfway point.
A reviewer can therefore treat the presence of `BATON_EVENT_SINK=file://…` and
the absence of `BATON_API_KEY` in the config entry as sufficient.

## 5. What is recorded, exactly

Each event is one JSON line in `try/events.jsonl`. The envelope carries an event
id, type, session id, sequence number, timestamp, and the tenant/vendor labels
set in the config entry.

Recorded in full:

- **Tool call arguments** — the complete `params` object of every `tools/call`.
- **Tool call results** — the complete result returned by your server. **Not
  truncated and not summarised.**
- **Tool call errors** — error type and error body.
- **Tool definitions** — a `surface_snapshot` event carrying your server's full
  tool list, server info, capabilities and instructions, recorded once per
  distinct surface.
- **Intent** — whatever the agent wrote into `user_goal` / `expected_result` and
  into `baton_annotate`. This is free text the model composed.

Recorded in part:

- **Resource reads** — the URI, the read's arguments, and the duration.
  **Resource contents are not recorded.**
- **Prompt gets** — the prompt name, **the arguments passed to it**, and the
  duration. The rendered prompt is not recorded. Treat these arguments as free
  text the agent composed: they can carry substantive content.
- **Resource lists and prompt lists** — counts and durations only.

Not recorded at all: your credentials (they are never read by the proxy), your
filesystem, your shell history, and anything from MCP servers other than the one
wrapped.

One thing to expect: your client starts the server and lists its tools when a
session opens, so **every session records one tool-surface snapshot even if the
agent never calls the server.** Nothing further is recorded unless it does.

## 6. What the scrubber does — and what it does not

Every payload passes through `Scrubber` (`scrub.py`) before it reaches any sink,
including the local file. It is on by default and there is no way to configure it
off in the try kit.

**Redacted by pattern:** JWTs, `Bearer` header values, `sk-…` API keys, `AKIA…`
AWS access key ids, email addresses, North-American-format phone numbers, and
13–19 digit card numbers that pass a Luhn check.

**Redacted by field name**, regardless of value: `email`, `phone`, `ssn`,
`api_key`, `token`, `secret`, `password`, `user_name`.

### The limits, stated plainly

Read these as the actual scope, not as caveats:

1. **Business data is not scrubbed.** This is the important one. Query results,
   table and column names, document text, row contents, customer records — if
   your server returns it, it lands in the file. The scrubber targets credentials
   and personal identifiers, not the substance of the work.
2. **Depth limit of 10.** Values nested more than ten levels deep are passed
   through untouched (`DEPTH_LIMIT`, `scrub.py`).
3. **Strings only.** Numbers, booleans and byte strings are not examined, so a
   sensitive value stored as a non-string is not redacted.
4. **Patterns, not understanding.** Non-North-American phone formats, national id
   numbers, and credential formats outside the list above are not matched.

**The mitigation is procedural, and we name it as such: read the file before you
send it.** It is line-delimited JSON on your own disk, it never moves on its own,
and deciding whether it may leave is a step you take deliberately at the end. If
it should not leave, delete it; we will never know it existed.

## 7. Where the data lives, and how to remove everything

- The event file is `try/events.jsonl`, inside this checkout. It is
  git-ignored, and it **grows without bound** for as long as the wrap is in
  place. The receipt command reports its size honestly; run it whenever you like.
- The default sink also mirrors events to **stderr**, which your MCP client may
  capture into its own logs. The try configuration sets `BATON_EVENT_SINK`
  explicitly to the file only, so this does not apply — but if you hand-edit the
  sink, know that `stderr:` puts payloads into your client's log files.
- A small state file in `try/` records which config entry was wrapped, in which
  file, and its original contents. It exists so removal is exact and so a receipt
  can be produced days later from a fresh session.

**To remove the kit at any point, including mid-trial:** run the uninstall
command, which restores the recorded entry and prints it for you to check;
restart your client; delete this checkout. Nothing else was installed, so there
is nothing else to uninstall. Or do it by hand: put the original entry back and
delete the folder — that is the entire footprint.

## 8. Provenance

- **Apache-2.0**, source in this repository.
- **Zero runtime dependencies** (`dependencies = []` in `pyproject.toml`). Pure
  standard library. Python ≥ 3.11.
- 12 source files, ~4,800 lines, with a test suite you can run.
- The `baton-spec` git submodule is **used only by tests**; a plain `git clone`
  without `--recurse-submodules` runs fine.

## 9. Verify all of this yourself

The claims above are mechanical. Re-derive them:

```bash
# 1. Every network- or process-capable call site, proxy AND kit. Six matches:
#    the five in the §4 table, plus one comment line in transport_http.py.
#    The kit contributes none — it only reads and writes local files.
grep -rnE "urlopen\(|Popen\(|subprocess\.run\(|boto3\.client\(" src/ try/

#    Widen it if you would rather not trust our regex — this catches every
#    mention, imports and prose included, and there are no other call sites:
grep -rn "urlopen\|socket\|http.client\|requests\.\|boto3\|subprocess" src/ try/

# 2. The dependency list — expect it to be empty.
grep -n "dependencies" pyproject.toml

# 3. The scrubber's full ruleset, in one file.
sed -n '1,90p' src/baton_proxy/scrub.py

# 4. The startup guards that refuse a remote sink.
grep -n -A20 "_guard_remote_consent" src/baton_proxy/emitter.py

# 5. Run the tests, including the kit's own round-trip test — the one that
#    proves uninstall restores your config exactly. This is the only step that
#    installs anything: pytest and ruff, into a virtualenv you control. The kit
#    itself never needs it.
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/test_try_kit.py -v   # the config surgery, on its own
.venv/bin/pytest                            # everything
```

To watch it work before trusting it, run the proxy against your server and read
the file as it fills:

```bash
tail -f try/events.jsonl
```

Every line that will ever exist is a line you can read.

## 10. Reporting a vulnerability

Please report security issues privately to **security@goodtiming.ai** rather than
opening a public issue. We aim to acknowledge within 3 business days and to give
an initial assessment within 7.

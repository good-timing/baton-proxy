# Security review: the Baton try kit

This document is written for a security reviewer deciding whether to approve
running the Baton try kit inside your organisation. It is meant to stand on its
own, so you can review it without contacting us.

Everything below is checkable in this repository. Section 9 shows how to
re-derive it yourself in about a minute.

---

## 1. What this is

Baton Proxy is an MCP proxy. It sits between your agent client and one MCP
server you already run, forwarding JSON-RPC in both directions and recording
what passed through.

The try kit is this repository's `try/` folder: two Python files, `kit.py` and
`upload.py`, standard library only, no imports from the proxy. It installs
nothing. The proxy is the source you are reading, so the code you review is the
code that runs. Review a release tag, not `main`: the paste checks out the
latest tag, and §9 shows how to confirm which one is on disk.

Run the trial against a non-production server. The capture holds the full
results of every tool call (§5), and business data is not redacted (§6).

The kit works with Claude Code only. It edits `~/.claude.json`, which no other
client uses. Where this document says "your client" it means Claude Code.

**Nothing Baton records leaves your machine unless you send it.** Events are
written to a local file. Two commands can send that file, and you run both
yourself. Section 4 explains this in detail, including every code path that
could send data and why each one is inert here.

## 2. What changes on your machine

One entry in `~/.claude.json` (or in a project-local `.mcp.json`, if you pass
`--config-file`). The kit wraps two kinds of server: a stdio server, which your
client launches locally, and a remote server, which your client reaches over
HTTPS. Here is an example of each.

**Stdio, using the Notion MCP server.** Before:

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
    "BATON_TENANT_ID": "notion",
    "BATON_VENDOR_ID": "notion",
    "BATON_EVENT_SINK": "file:///absolute/path/to/baton-proxy/try/events.jsonl"
  }
}
```

The server keeps its name and its environment. Your original command becomes
the proxy's argument: the proxy starts your server as a child process and
speaks to it over stdin/stdout, exactly as your client did.

**Remote, using the GitHub MCP server.** The kit wraps a remote entry on one
condition: its only header is `Authorization: Bearer ...`, and that token is
written in the config file. Before:

```json
"github": {
  "type": "http",
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": { "Authorization": "Bearer ${GITHUB_PAT}" }
}
```

After:

```json
"github": {
  "command": "/absolute/path/to/python3.12",
  "args": ["-m", "baton_proxy", "--url", "https://api.githubcopilot.com/mcp/"],
  "env": {
    "BATON_UPSTREAM_AUTH_TOKEN": "${GITHUB_PAT}",
    "PYTHONPATH": "/absolute/path/to/baton-proxy/src",
    "BATON_TENANT_ID": "github",
    "BATON_VENDOR_ID": "github",
    "BATON_EVENT_SINK": "file:///absolute/path/to/baton-proxy/try/events.jsonl"
  }
}
```

Unlike the stdio case, this adds a process. Before the wrap, your client speaks
HTTPS to that endpoint directly. After it, baton-proxy runs as a local
subprocess, opens that connection instead, and holds your bearer token in its
environment. The destination and the credential do not change: the proxy sends
the same token to the URL your entry already named (`transport_http.py`), and
nowhere else.

In both cases the interpreter is written as an absolute path, the one that ran
setup, because a GUI-launched client on macOS resolves a bare `python3` against
a minimal `PATH` and gets the system 3.9, which cannot run this package. Setup
refuses to run under anything older than 3.11. `PYTHONPATH` points at the
checkout you reviewed; nothing is installed. Verify it yourself:

```bash
PYTHONPATH=src python3 -m baton_proxy --help
```

What a reviewer should know about the edit:

- **The entry is replaced, not duplicated.** A second entry beside the original
  would leave the agent able to call the unwrapped server, so nothing would be
  captured while everyone believed it was.
- **Credentials are never resolved.** The `env` block is preserved verbatim,
  `${VAR}` references included; your client expands them at launch, as it does
  today. The proxy strips every `BATON_`-prefixed variable from the child
  process's environment (`proxy.py`, `_child_env`), so your server never sees
  Baton's configuration. A server that legitimately needs a variable of its own
  beginning `BATON_` will not receive it.
- **The change reaches the next session your client starts, not the one running
  now.** A client binds its server set at startup. Nothing needs to be quit; a
  new terminal is enough.
- **The whole config file is backed up first** to `try/config-backup.<timestamp>.json`,
  and the original entry is recorded byte for byte in `try/state.json`. Both are
  created `0600`, because they hold a copy of your `env` block.
- **Formatting and permissions are preserved.** The kit reuses the indent it
  finds and rewrites one entry; it writes via a temporary file and an atomic
  rename, and copies the original mode onto the replacement. Both are covered by
  tests.
- **Uninstall restores into the file as it is then**, not by copying the backup
  over it, because your client rewrites that file continuously. It prints the
  restored entry, then re-reads the file and compares every byte against what
  was recorded. If the comparison fails it says so and keeps the state file.
- **Printouts hide literal values.** Wherever the kit displays a config entry,
  literal `env` and `headers` values are shown as `<literal value, not shown>`,
  and a URL as scheme and host only. Key names, the command and the arguments
  stay visible. The limit: a credential passed as a command-line argument
  (`--api-key sk-...` in `args`) still prints, because nothing can tell which
  argument is a secret. `env` is where credentials belong, and `env` is what is
  protected.
- **A server that signs you in to a third party may ask you to sign in again.**
  A server holding its own sign-in session can treat its first wrapped start as
  a new one and open a browser tab. The consent screen names a `localhost` port.
  That port is opened by your server, running as a child of the proxy, and the
  access you grant goes to the same third party it always did. The proxy opens
  no listening port of its own; `test_nothing_of_ours_opens_a_listening_port`
  sweeps `src/` and `kit.py` for the constructs that would.

### Remote entries, specifically

The kit does not resolve the token. `Bearer ${GITHUB_PAT}` becomes
`BATON_UPSTREAM_AUTH_TOKEN: "${GITHUB_PAT}"`, the reference, not the value, and
your client expands it at launch. That expansion is client behaviour we measured
on Claude Code 2.1.223, not something the MCP specification requires; if your
client does not expand `${VAR}` in `env`, the wrapped server fails to
authenticate when it next starts. If the token in your entry is a literal, it is
copied across as is, and also into `try/state.json`, which is why that file is
`0600`.

Run `python3 kit.py receipt` on the first day. An empty file is how you find a
broken wrap in an hour instead of at the end of the trial.

Refused, because each would produce a wrap that reports success and a server
that cannot authenticate: `sse` transport (the bridge implements Streamable HTTP
only); additional headers (the bridge sends exactly one); no credential in the
config (indistinguishable from OAuth, where your client holds the token and
never writes it down).

## 3. What your agent sees that it did not before

**Two added tools.** Both are answered by the proxy; neither call reaches your
server.

- `baton_annotate` (`proxy.py`, `ANNOTATE_TOOL_NAME`). The agent calls it to
  record what the user was trying to do and where a tool call went wrong. Its
  only effect is a line in the local event file.
- `baton_session_report` (`proxy.py`, `_build_report_tool`). Renders a markdown
  summary of the current session by reading the local event file. It opens no
  network connection. It is injected whenever a file sink is configured, which
  the try setup does (`report.py`, `should_inject_report_tool`).

**Three parameters added to every upstream tool's schema:** `user_goal`,
`expected_result` and `overall_task` (`proxy.py`, `_inject_goal_params`). They
are removed from the arguments before the call is forwarded, so your server
receives exactly the arguments it would have received unwrapped.

There is no switch that turns the parameters off; they are the reason the wrap
exists. The off switch is the wrap itself: `python3 kit.py uninstall` puts your
original entry back.

`user_goal` is listed in each tool's `required` array by default, and nothing
enforces it. The proxy appends the name to the advertised list and validates
nothing: a call that omits `user_goal` is forwarded exactly as it would have
been unwrapped, and your server never learns whether it was present.
`BATON_INTENT_PARAM=optional` drops it from the `required` array; that changes
how the parameter is advertised, not whether it is injected.

One edge: if a call arrives before the proxy has seen a `tools/list` in that
process, it cannot know whether the tool natively declares a parameter of one
of those three names, and it strips the parameter anyway, with a warning
(`proxy.py`, `_extract_one_goal_param`). On the normal path, where the client
lists tools before calling them, a natively declared parameter is recognised and
forwarded untouched.

**A paragraph appended to your server's `instructions`**, telling the agent that
`baton_annotate` exists (`_llm_text.py`, `build_instructions_suffix`). Appended
to whatever your server sent, never replacing it.

Beyond those three additions, nothing is removed, renamed or rewritten: tool
definitions, arguments, results and errors pass through unchanged.

### 3a. The kit's own code

Four commands, all run from the `try/` directory:

| command | what it touches |
|---|---|
| `setup <server>` | Reads your MCP config; copies the whole file to `try/config-backup.<timestamp>.json`; rewrites one entry; writes `try/state.json`. Nothing else on the machine. |
| `receipt` | Reads `try/events.jsonl` and `try/state.json`. Writes nothing, opens no connection. |
| `upload` | Reads `try/events.jsonl` and `try/upload.json` and POSTs the events to the workspace that file names. The only command that sends anything, and the only one that loads `upload.py`. It needs `upload.json`, a credential file we email you when we set up a workspace for you; it is not in this repository, and without it the command refuses. |
| `uninstall` | Rewrites that one entry back and deletes `try/state.json`. Leaves your events file and the backups for you to read or delete. |

All of the kit's network code is in `upload.py`. `kit.py` loads it by path from
inside the `upload` command, so `setup`, `receipt` and `uninstall` never put it
on their import graph. A reviewer who wants to know what can leave the machine
reads that one file.

`try/CLAUDE.md` is a plain-text instruction file for the agent. It grants no
capability. It tells the agent to use the commands above and what not to do:
never edit an MCP config by hand, never work around a command that refused,
never quote the captured events into the conversation, and never send the file.
When the person decides to send, the agent hands them the command to run; it
does not run it. That rule is also enforced in code: `upload` refuses to run
unless a person is typing at a terminal, and asks them to type `send` before
the first request, so an agent's shell cannot run it.

The kit refuses rather than guesses when the named server appears in more than
one config scope, when the entry is already wrapped by something other than this
kit, and at uninstall when the entry no longer matches what setup wrote (it
shows both versions and changes nothing). The setup/uninstall pair is
property-tested as a round trip (`tests/test_try_kit.py`): for a corpus of
config shapes, `uninstall(setup(x))` returns the original bytes.

## 4. What leaves your machine

**Nothing, unless you send it.** Events are appended to a local JSONL file.

Two things can send that file, and you run both:

- `receipt` ends by printing a `gzip` command and an address,
  **team@goodtiming.ai**. If the file goes there, it is because you attached it
  to an email yourself.
- `kit.py upload` POSTs the capture to a Baton workspace. It refuses to run
  unless a person is typing at a terminal, and asks them to type `send` first.
  It refuses without `try/upload.json`, a file we hand over by arrangement; a kit cloned from this
  repository does not have one and cannot obtain one. Its key lives in that file
  and never in your config entry, so the wrapped server has no credential and no
  code path that would use one. The proxy is not involved: `upload` reads a file
  that already exists, and the wrap opens no socket because of it.

One qualification, only if you wrapped a remote entry: that server's traffic was
already leaving your machine, because your client was dialling the endpoint
itself. It still goes to the same endpoint with the same token. What changes is
which process opens the connection. No new destination is introduced.

The proxy contains code that can open a network connection or start a process,
because the same source serves production deployments. Here is the complete
list, six call sites. Five are the proxy's; the sixth is the kit's, and it is
the only one that exists to send your data.

| # | site | what it does | why it is inert here |
|---|---|---|---|
| 1 | `sinks.py` · `HttpSink.write` | POSTs events to `{url}/v0/events` | Built only when `BATON_EVENT_SINK` is an `http(s)://` URL. The try config sets a `file://` URL. It also raises at startup without `BATON_API_KEY`, which the try config does not set. |
| 2 | `sinks.py` · `S3Sink` | PUTs one object per event to an S3 bucket | Built only for an `s3://` sink. Requires `boto3`, an optional extra this package does not install (`dependencies = []`). |
| 3 | `transport_http.py` · `StreamableHttpClient.post` | Speaks MCP over HTTPS to an upstream server | Only in `--url` mode. For a stdio wrap this is unreachable. For a remote wrap it is the path in use, and it connects to the URL your own config already named. Never to us. |
| 4 | `proxy.py` · `subprocess.Popen` | Starts the upstream MCP server | Runs exactly the command your config already contained. Not reached for a remote wrap. |
| 5 | `scan.py` · `subprocess.run` | Runs `claude -p` headlessly for a preflight report | Only under the `baton-proxy scan` subcommand. The try flow never invokes it. |
| 6 | `try/upload.py` · `open_request` | POSTs your captured events to `{console}/v0/events` | The kit's own, and the one thing here that can send your data. Reached only from `kit.py upload`, which refuses without an interactive terminal, a typed `send`, and `try/upload.json`. `setup`, `receipt` and `uninstall` do not load this module. |

There is no telemetry, no version check, no crash reporting, no auto-update. The
proxy does not phone home on startup, on failure, or on exit.

### The guards that keep it that way

Turning on remote delivery is not a single flag:

- `Emitter._guard_remote_consent`, called from `start()` before any sink is
  built, refuses to start if the sink is `http`, `https` or `s3` while
  `BATON_CONSENT_TOKEN` is still the default placeholder `"local"`
  (`emitter.py`). It raises rather than degrading quietly.
- `HttpSink.__init__` raises without an API key (`sinks.py`).
- An unrecognised sink scheme raises at startup (`make_sink`). There is no
  silent fallback.

Reaching a remote endpoint requires changing at least three environment
variables in the config entry, and the proxy fails loudly at any halfway point.
A reviewer can treat `BATON_EVENT_SINK=file://...` and the absence of
`BATON_API_KEY` in the config entry as sufficient.

## 5. What is recorded, exactly

Each event is one JSON line in `try/events.jsonl`. The envelope carries an event
id, type, session id, sequence number, timestamp, and the tenant and vendor
labels from the config entry. Both labels default to the name of the server you
wrapped; nothing checks them against anything. They exist so one file can be
told apart from another later.

Recorded in full:

- **Tool call arguments**: the complete `params` object of every `tools/call`.
- **Tool call results**: the complete result returned by your server. Not
  truncated and not summarised.
- **Tool call errors**: error type and error body.
- **Tool definitions**: a `surface_snapshot` event carrying your server's full
  tool list, server info, capabilities and instructions, recorded once per
  distinct surface.
- **Intent**: whatever the agent wrote into `user_goal`, `expected_result`,
  `overall_task` and `baton_annotate`. This is free text the model composed. An
  agent arguing that a result was wrong will restate the result, which is where
  a second copy of your data comes from (§6).

Recorded in part:

- **Resource reads**: the URI, the read's arguments, and the duration. Resource
  contents are not recorded.
- **Prompt gets**: the prompt name, the arguments passed to it, and the
  duration. The rendered prompt is not recorded.
- **Resource lists and prompt lists**: counts and durations only.

Not recorded: your credentials, your filesystem, your shell history, and
anything from MCP servers other than the one wrapped. For a stdio wrap the proxy
does not read credentials at all. For a remote wrap it reads
`BATON_UPSTREAM_AUTH_TOKEN` from its own environment to present it upstream, and
that value is never emitted, logged, or written anywhere but the config entry
and `try/state.json`.

For a remote wrap, one class of traffic is not captured: the bridge handles the
client-initiated request/response loop and does not open the standing GET SSE
channel, so server-initiated messages (sampling, elicitation, notifications)
pass outside it. Tool calls, results and intent are unaffected.

Your client starts the server and lists its tools when a session opens, so every
session records one tool-surface snapshot even if the agent never calls the
server. Nothing further is recorded unless it does.

## 6. What the scrubber does, and what it does not

Every payload passes through `Scrubber` (`scrub.py`) before it reaches the file.
It is on by default and cannot be configured off in the try kit.

**Redacted by pattern:** JWTs, `Bearer` header values, `sk-...` API keys,
`AKIA...` AWS access key ids, email addresses, North-American-format phone
numbers, and 13 to 19 digit strings that pass the Luhn checksum.

The last rule is deliberately loose. Card numbers pass Luhn; so does roughly one
in ten of every other long digit string (order numbers, epoch timestamps, record
ids), and those are redacted too rather than risk missing a card. `receipt`
reports them as `cc`; the count is of card-shaped numbers, not a finding that
card numbers were present. The redaction keeps no copy of what it replaced.

**Redacted by field name**, regardless of value: `email`, `phone`, `ssn`,
`api_key`, `token`, `secret`, `password`, `user_name`.

### The limits

1. **Business data is not scrubbed.** Query results, table and column names,
   document text, row contents, customer records: if your server returns it, it
   lands in the file. The scrubber targets credentials and personal identifiers,
   not the substance of the work.

   It can be in the file twice. The annotations are prose a model wrote, and an
   agent explaining why a tool call went wrong restates what came back. In our
   own end-to-end run the agent copied returned rows into an annotation's
   `context` field to make its argument. Those fields go through the same
   scrubber, and a customer name inside a sentence is not a pattern it matches.
2. **Depth limit of 10.** Values nested more than ten levels deep pass through
   untouched (`DEPTH_LIMIT`, `scrub.py`).
3. **Strings only.** Numbers, booleans and byte strings are not examined.
4. **Patterns, not understanding.** Non-North-American phone formats, national
   id numbers, and credential formats outside the list above are not matched.

**Read the file before you send it.** It is line-delimited JSON on your own
disk, it never moves on its own, and sending it is a step you take at the end.
Read the `annotation` lines as well as the results. If it should not leave,
delete it.

## 7. Where the data lives, and how to remove everything

- **`try/events.jsonl`** holds the payloads of §5. It is git-ignored, created
  `0600` (and re-set to `0600` when the proxy opens it), and it grows without
  bound for as long as the wrap is in place. The receipt reports its size.
- **`try/state.json`** records which entry was wrapped, in which file, and its
  original contents, so removal is exact and a receipt can be produced days
  later. Created `0600`; it is the one place the kit writes a literal `env`
  value to disk. `uninstall` deletes it once the restore is verified.
- **`try/config-backup.<timestamp>.json`** is the whole config file as it was
  before setup, `0600`. Evidence, never the source of the restore.
- **`try/upload.json`**, if we sent you one, holds a live API key issued for
  your workspace. It is the only credential in this trial that is ours. The kit
  never moves or copies it; `upload` reads it where you saved it. Delete it when
  you are done, and tell us if it was ever exposed, because only we can rotate
  it. If you did not receive one, nothing here is missing.
- The proxy's default sink also mirrors events to stderr, which a client may
  capture into its own logs. The try configuration sets `BATON_EVENT_SINK` to
  the file only, so this does not apply unless you hand-edit the sink.

**To remove the kit at any point, including mid-trial:** run
`python3 kit.py uninstall`, which restores the recorded entry, prints it, and
verifies the result against the file on disk; then delete this checkout. New
sessions use your original server from that point; a session already running
keeps the wrapped one it launched. Nothing else was installed. Or do it by
hand: put the original entry back and delete the folder.

## 8. Provenance

- Apache-2.0, source in this repository.
- Zero runtime dependencies (`dependencies = []` in `pyproject.toml`). Pure
  standard library. Python 3.11 or newer.
- Twelve source files under `src/baton_proxy/`, with a test suite you can run.
- The `baton-spec` git submodule is used only by tests. A plain `git clone`
  without `--recurse-submodules` runs fine; two schema-conformance tests skip
  without it, and `pytest -rs` prints the reason.

## 9. Verify all of this yourself

The claims above are mechanical. Re-derive them:

```bash
# 0. Which release is on disk. Expect a tag name with no suffix; a suffix like
#    -3-gabc1234 means commits past the tag, which is not what you reviewed.
git describe --tags

# 1. Every network- or process-capable call site, proxy AND kit. Expect seven
#    matches: the six in the §4 table plus one comment line in
#    transport_http.py. The excludes are your own data and credential, which
#    can contain any string; drop them on a fresh clone.
grep -rnE "urlopen\(|Popen\(|subprocess\.run\(|boto3\.client\(" --exclude=events.jsonl --exclude=state.json --exclude=upload.json --exclude='config-backup.*' src/ try/

#    Wider, if you would rather not trust our regex. This catches every
#    mention, imports and prose included; there are no other call sites.
grep -rn "urlopen\|socket\|http.client\|requests\.\|boto3\|subprocess" --exclude=events.jsonl --exclude=state.json --exclude=upload.json --exclude='config-backup.*' src/ try/

# 2. The dependency list. Expect it to be empty.
grep -n "dependencies" pyproject.toml

# 3. The scrubber's full ruleset, in one file.
sed -n '1,90p' src/baton_proxy/scrub.py

# 4. The startup guards that refuse a remote sink.
grep -n -A20 "_guard_remote_consent" src/baton_proxy/emitter.py

# 5. Run the tests, including the kit's round-trip test that proves uninstall
#    restores your config exactly. This is the only step that installs
#    anything: pytest and ruff, into a virtualenv you control. The kit itself
#    never needs it.
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/test_try_kit.py -v -rs   # the config surgery, on its own
.venv/bin/pytest -rs                            # everything, with skip reasons
```

To watch it work before trusting it, run the proxy against your server and read
the file as it fills:

```bash
tail -f try/events.jsonl
```

Every line that will ever exist is a line you can read.

## 10. Reporting a vulnerability

Please report security issues privately to **security@goodtiming.ai** rather
than opening a public issue. We aim to acknowledge within three business days
and to give an initial assessment within seven.

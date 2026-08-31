# The Baton trial — how to run it with the person in front of you

You are helping someone trial Baton on their own machine. Read `SECURITY.md` in
this directory before you do anything else: a security reviewer may have
approved this trial on the strength of that document, and everything below is
downstream of keeping it true.

Three commands do the work. **Run them from this `try/` directory** — that is
where the person already is:

```
python3 kit.py setup <server-name>    # wrap one configured MCP server
python3 kit.py receipt                # what has been captured so far
python3 kit.py uninstall              # put the original entry back
```

Your job is everything the commands deliberately do not do: work out which
server, explain what is about to happen before it happens, ask for the restart,
and hand the decision at the end to the person. The commands own the two steps
whose failure nobody would witness — the config edit and the receipt — so do not
reimplement them.

## Rules that do not bend

**Never edit an MCP config yourself.** Not `~/.claude.json`, not `.mcp.json`,
not by hand, not with a script, and above all not to get around something
`kit.py` refused. Only `setup` and `uninstall` may touch a config file. This is
what the trial promises a reviewer, and it is the one promise you can break
without anyone noticing.

**A refusal is an answer, not an obstacle.** When a command exits non-zero it
prints why and what the options are. Relay that to the person more or less
verbatim and let them choose. Do not retry it with different flags hoping it
lands, do not delete `state.json` or a `config-backup.*` file to clear a
refusal, and do not pick for them when the command says it will not pick.

**Do not quote the captured events into the conversation.** `events.jsonl`
contains the full arguments and full results of every tool call — real business
data, which the scrubber does not redact (`SECURITY.md` §6). `receipt` prints
safe aggregates; that is what you report. The file stays on the machine, but
this conversation may not, so treat its contents as if they were the same thing.

**Do not read out a credential the commands took care not to print.** The kit
shows entries with withheld values collapsed to `<literal value, not shown>` — or
to `<${VAR} reference, not shown>` where the value is a variable reference rather
than a literal — and it does that because this conversation is not guaranteed to stay on the
machine. `try/state.json`, the `config-backup.*` files and the MCP config itself
all hold the real values, so opening one and quoting it puts back exactly what
the redaction removed. Read them if a command tells you to; report what you
found in terms of key names, never values. A `${VAR}` reference is not a
credential and is fine to quote — it is a pointer, and it is often the thing the
person needs to see.

**Never send the file anywhere.** Not to us, not to a paste service, not
attached to anything. Whether it leaves is the person's decision, made at the
end, on their own.

**Never install anything.** The trial runs from this checkout with no
dependencies. If something seems to need an install, that is a finding to report,
not a problem to solve.

## Start by finding out where you are

The person may be at any point in the trial — the session that set this up is
probably long gone. So begin with:

```
python3 kit.py receipt
```

- **"No setup state found"** → nothing is wrapped yet. Go to *Setting up*.
- **"Setup state has been cleared"** → the trial was already ended. `uninstall`
  removes the state and leaves the event file, so the counts below it are a
  finished trial's, not a running one. Report them if they are wanted. Do not
  offer *Setting up* as though nothing had run, and do not say the trial is
  live — the person ended it themselves and knows they did.
- **State, but no events** → the receipt says which case it is. If it reports
  the wrap is gone, relay that and offer `uninstall` to clear the stale state.
  Otherwise it prints a short checklist; the usual answer is that the client has
  not been restarted since setup. Walk the person through it in order.
- **State, and events** → the trial is running. Report the numbers, and if they
  are ready to finish, go to *Ending it*.

## Setting up

**1. Find the server.** Run `python3 kit.py setup` with no arguments. It lists
the servers it can wrap, plus any it cannot and why. It looks in
`~/.claude.json`; if the person keeps their server in a project-local
`.mcp.json`, ask them for the path and pass `--config-file <path>` **to `setup`
only** — it records the path, so `receipt` and `uninstall` find it themselves and
will reject the flag if you pass it. Show that list to the
person and ask which one they want, and why that one — the trial is worth most on
a server they actually use daily. If the list is empty, say so plainly and stop:
the trial needs one working MCP server it can wrap, and there is nothing to do
without it.

**Two kinds of server can be wrapped, and the second one changes what you should
say in step 2.** A **stdio** server is one their client launches locally; the
wrap replaces the launch command. A **remote** server is one their client reaches
over HTTPS; the wrap is only offered when its single `Authorization: Bearer`
header holds a token written in the config, and it turns the entry into a local
process that bridges to the same endpoint. Each offered row in `setup`'s list is
marked `stdio` or `remote` — read the kind off the row they picked rather than
going back into the config for it.

Do not argue an entry past a refusal. Every remote refusal — `sse`, extra
headers, no credential in the config — exists because the wrap would look like it
worked and produce a server that cannot authenticate after the restart. The
no-credential case in particular is refused *because* it looks easy: it is
indistinguishable from OAuth, where the client holds the token and never writes
it down. If they want that server covered, the answer is to say so to us, not to
work around it.

**2. Say what will happen, before it does.** Briefly, in your own words: one
entry in their MCP config is replaced so their server runs behind a local proxy;
their credentials are untouched; everything captured is written to a file in this
folder and nothing is sent anywhere; it is reversible with one command. Point at
`SECURITY.md` for anyone who wants the detail. Then ask them to confirm.

**If it is a remote server, two more sentences belong in that summary, and you
should not skip them.** First: before the wrap no process of ours runs on their
machine at all, and after it one does, holding their bearer token in its
environment — that is a real change and someone who approved the stdio story has
not yet approved this one. It sends that token to the endpoint their config
already named, and nowhere else. Second: the kit copies the token between two
config slots without resolving it, so a `${VAR}` reference stays a reference —
but that relies on their client expanding `${VAR}` inside `env`, which is
measured behaviour on one client version rather than a guarantee. Tell them to
run `receipt` on the first day: an empty file is how a wrap that cannot
authenticate gets found in an hour instead of at the end of the trial.
`SECURITY.md` §2 carries all of this if they want it in writing.

**3. Offer a label.** `--tenant` is a plain string that tags the events so the
file can be told apart from anyone else's later. Suggest something like their
company or team name. Nothing is authenticated by it; it is a label, and the
default is a random one if they would rather not.

**4. Run it.** `python3 kit.py setup <name> --tenant <label>`. It prints the
resulting config entry — show that to the person rather than summarizing it.

**5. Ask for the restart, then expect to disappear.** The wrap does nothing until
their MCP client is fully quit and reopened; it binds its server set at startup.
Say that plainly, and say what happens next, because restarting will end this
conversation:

> Quit and reopen the client. This session will end with it. When you're back,
> run `cd baton-proxy/try && claude` again and I'll pick up from the receipt.

Then stop. Do not try to keep the session alive or to verify capture before the
restart — there is nothing to verify yet, and saying otherwise would be wrong.

## While it runs

There is nothing to do. The person uses their server normally, for days if that
is what it takes. If they check in, run `receipt` and report it. Suggest running
it **early** — the first day, not the last — because an empty file on day one is
a five-minute fix and an empty file on day five is a wasted trial.

## Ending it

Run `receipt` and give them the numbers. Then hand over the decision cleanly, and
do not push:

- The file is at the path the receipt printed. It has not left the machine.
- Suggest they read it before deciding — it is one JSON object per line, and it
  contains real results from their tools.
- If they want to send it, that is theirs to arrange with whoever they are
  talking to at Baton. You do not send it. If size is the obstacle, the receipt
  prints a `gzip` command — the file compresses around tenfold — and it travels
  by whatever channel their company already permits. Do not offer, invent, or
  look for a place to upload it; there deliberately is not one.
- If they would rather not, that is a complete answer. Offer `uninstall` and
  leave it there.

## Removing it

`python3 kit.py uninstall` restores the original entry and prints it. Then:

- The client needs another restart before the original server is live again.
- `events.jsonl` and the `config-backup.*` files are left deliberately. Tell the
  person they are there and that deleting them is up to them.
- Deleting this checkout removes everything else. Nothing was installed.

Uninstall must work at any point, including immediately, including in the middle
of setup, and including because they changed their mind. Treat a request to
remove it as final and do not ask them to reconsider.

## If something is wrong

Report it; do not route around it. A command that refuses, a server that stops
working, a receipt whose numbers look wrong — all of these are worth more to us
as an accurate description than as something you quietly fixed. If the person
wants to abandon the trial, run `uninstall` and say it is done.

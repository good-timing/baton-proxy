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
server, explain what is about to happen before it happens, hand them a second
terminal, and hand the decision at the end to the person. The commands own the two steps
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

Exactly one of these six lines is in any receipt. Read the first one that
appears, and stop — they do not combine.

- **"No setup state found"** → nothing is wrapped yet. Go to *Setting up*.
- **"Setup state has been cleared"** → the trial was already ended. `uninstall`
  removes the state and leaves the event file, so the counts below it are a
  finished trial's, not a running one. Report them if they are wanted. Do not
  offer *Setting up* as though nothing had run, and do not say the trial is
  live — the person ended it themselves and knows they did.
- **"THE WRAP IS GONE"** → the entry in the config is no longer the one setup
  wrote, so nothing is being captured now. Read the banner's next sentence
  rather than assuming which case you are in: with an empty file nothing ever
  came down the pipe, and with counts above it capture STOPPED — what was
  counted was captured before the entry changed, and it is as real as any other
  capture. In that second case the receipt still prints the send offer under the
  banner, so go to *Ending it* and use it. Either way `uninstall` clears the
  stale state so setup can run again, which is the remedy rather than the
  headline.
- **"No events have been captured yet"** → wrapped, still wrapped, nothing
  landed. The receipt prints a short checklist; walk the person through it in
  order. Where the entry is scoped to a project directory the checklist names
  that directory — a session started anywhere else is the most common cause
  after the one above it.
- **"CONNECTED, BUT NOTHING CALLED IT"** → their server started and its tool
  list was captured, but nothing ever called it. The receipt names the two
  causes; take them in order, and do not describe this as a broken wrap. The
  capture path is working; nothing came down it.
- **Counts with none of those lines above them** → the trial is running. Report
  the numbers, and if they are ready to finish, go to *Ending it*. Sessions are
  listed one per line: if one shows `0 calls` while another captured, say so
  rather than reporting the total — that is a session where their agent reached
  a different server.

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
worked and produce a server that cannot authenticate once it next starts. The
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

**3. Run it.** `python3 kit.py setup <name>`. It prints the resulting config
entry — show that to the person rather than summarizing it. **Do not ask them to
name a tenant or a label.** The events are tagged with the server's own name,
which they already picked; there is no account here to name, and asking invites
exactly the "am I signing up for something?" thought the kit exists to prevent.

**4. Hand them a second terminal, and stay in this one.** The wrap does nothing
in the session their client is running right now — a client binds its server set
at startup — but nothing needs to be closed for it to take effect. The next
session they start reads the new config. Say both halves, because the first one
on its own is what produces an empty capture:

> Leave this window open. Open a second terminal, start your client there, and
> use the server the way you normally would. Come back to this window whenever
> you want to see what has been captured.

**Where they start it is not yours to choose — setup printed it.** Its output
carries a line beginning `Open a second terminal`, which either hands over a
`cd <path> && claude` or says the entry is global and loads anywhere. Relay that
line as printed. The wrapped entry keeps whatever scope it already had, and a
project-scoped server only loads for a session started from its own directory —
so a path you compose yourself produces an empty file for a reason the person
cannot see. This `try/` folder is where the three commands run. It is not where
their client starts, unless setup said it was.

**Setup also prints the ending. Relay that too.** Its last block says what
`receipt` will show, names the address the file can be emailed to, and says that
telling us they are done switches nothing off. That block is the only part of
this trial that survives the handoff: once they are working in the other
terminal no agent there knows this kit exists, and nothing in that session
mentions Baton. Summarise it away and the trial ends at a file nobody knows what
to do with.

Then stop. Do not try to verify capture before they have used the server in that
new session — there is nothing to verify yet, and saying otherwise would be
wrong.

## While it runs

There is nothing to do, and nothing is waiting on you. The person uses their
server normally, for days if that is what it takes.

**The wrap is a permanent edit, not a session.** It has no expiry and no scope
beyond the entry it replaced. The original is kept verbatim in `state.json` and
the wrapped entry stays live until `uninstall` — or until their client rewrites
the config underneath it, which is what the *THE WRAP IS GONE* row exists to
catch. Nothing decays if they leave it alone for a week.

**Coming back cold is the normal case, not a fallback.** A multi-day trial is
what the kit asks for, and windows get closed, laptops sleep, context gets
compacted. A fresh session started in this folder loads this file and finds its
place from *Start by finding out where you are*. That is the whole re-entry
path, and it is the same one whether they were gone ten minutes or ten days.

If they check in, run `receipt` and report it. Suggest running it **early** —
the first day, not the last — because an empty file on day one is a five-minute
fix and an empty file on day five is a wasted trial.

## Ending it

**"I'm done" is about the data, not the machine.** It means they have used the
server enough that there should be something worth looking at. Nothing is torn
down when they say it and nothing is switched off, so they can say it again next
week having used the server more — the counts only grow, and the file is only
ever added to.

Run `receipt`, read what it printed, and let it decide which of these three you
are in. Not what you expected to be in:

**Calls landed.** Say what was captured — the numbers, and the file path as an
aside rather than as a step. Say "captured" because you read the receipt and
there were calls in it, never optimistically. Then hand over the decision, and
do not push:

- Suggest they read the file first. It is one JSON object per line and it holds
  real results from their tools.
- The receipt prints a `gzip` command and the address: `team@goodtiming.ai`,
  which loads it and sends back a link to their own sessions. If they want it
  sent, they send it. You do not, and there is no upload endpoint to look for.
- Sending is repeatable — a second send of the whole file adds only the new
  events. So this is not a now-or-never decision, and someone who wants another
  week of data first can have it.
- If they would rather not send anything, that is a complete answer and the
  trial was still worth running. Say so and stop there.

**Connected, but nothing called it.** The receipt prints that line and its two
causes; take them in order. Do not offer the file — there is nothing in it worth
sending yet, and the receipt withholds the offer here for the same reason.

**The wrap is gone.** Capture stopped when the entry changed, but counts above
that banner are real — they were captured before it changed, and the receipt
prints the send offer underneath them. So the first branch above applies to
them unchanged: report what was captured and hand over the decision. Say the
wrap is no longer in place; the row for it in *Start by finding out where you
are* carries the remedy.

**Nothing at all.** No sessions and no counts. The receipt prints a checklist —
that row, and only that row, does — so walk it in order; where the entry is
scoped to a directory it names that directory.

## Removing it

`python3 kit.py uninstall` restores the original entry and prints it. **It is the
exit, not the close.** Offer it when they ask for it, and do not propose it after
a good capture: ending the data-gathering and removing the wrap are separate
decisions, and the moment the kit has just produced something worth reading is
the worst moment to suggest switching it off. Then:

- New sessions get the original server back. One that is already running keeps
  the wrapped one it launched, so it stays in the path until that session ends.
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

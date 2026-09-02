# The Baton trial — how to run it with the person in front of you

You are helping someone trial Baton on their own machine. Read `SECURITY.md` in
this directory before you do anything else: a security reviewer may have
approved this trial on the strength of that document, and everything below is
downstream of keeping it true.

**This kit is written for Claude Code.** It finds and rewrites `~/.claude.json`,
which no other client uses, and the handoff below assumes a `claude` they can
start in a second terminal. Say that at the start if there is any doubt about
what they are running — finding out at the server list, after they have read the
security document and approved the clone, is the same trial ending later and
worse. `--config-file` points `setup` at another path, but nothing else about
the flow is adjusted for another client.

Three commands do the work. **Run them from this `try/` directory** — that is
where the person already is:

```
python3 kit.py setup <server-name>    # wrap one configured MCP server
python3 kit.py receipt                # what has been captured so far
python3 kit.py uninstall              # put the original entry back
```

There is a fourth, `python3 kit.py upload`, and it is **not yours to run** — see
*Rules that do not bend*. It sends the capture to a Baton workspace, exists only
where we set one up in advance, and refuses everywhere else. Most kits do not
have it available; the receipt says so itself by only mentioning it where it
works, so let the receipt tell you rather than assuming either way.

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

**Never explain a number you only counted.** The receipt reports what is in
the file. It does not know why any number is what it is, and neither do you —
the causes sit in their client, their prompts and their data, and none of those
reach this machine. Where a count has more than one cause the receipt names
them: relay that, do not pick one of them, and do not add one it does not name.
An intent of zero is not "you refused something", a `cc` count is not a card in
their data, and a session with no calls is not proof another server answered.
If they ask why, say what would settle it — usually using the server again with
the receipt open — rather than answering from the number.

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

**That includes `kit.py upload`, and especially that.** It is a real command in
this kit and it really sends the capture, so it is the one rule here you could
break by following an instruction rather than by improvising: you have a shell,
and typing it would take one line. Do not run it, do not offer to run it, and do
not run it when asked to — say that this one is theirs to type, and why. The
same goes for `gzip`: preparing the file is fine, sending it is not. If they ask
you to do it for them, that is the moment the rule is for.

**Never install anything.** The trial runs from this checkout with no
dependencies. If something seems to need an install, that is a finding to report,
not a problem to solve.

## The four times you ask

Four moments belong to the person and not to you: **which server to wrap**
(step 1), **whether to go ahead** after the security summary (step 2), **what
happens to the file** at the end, and **whether to remove the wrap**. Each of
them is a fork, and how you ask decides what they are agreeing to.

**Where your client can show a choice, show one.** In Claude Code that is
`AskUserQuestion` — the options arrive as things to pick rather than a paragraph
to answer in prose, and picking is what someone does when they have understood
the question. Two options is the usual shape and neither is pre-chosen.

**The disclosure stays in prose above the question. The question carries the
choice, never the fact.** Write the facts out first — in the shape the section
asks for — then keep the options short: *Go ahead* / *Show me the detail first*.
Never fold a fact into a label. "Results land in the file in the clear" is not
an option; it is the thing being consented to, and an option label is where a
fact goes to get skimmed. If something is only stated inside an option, it has
not been disclosed. Trading a wall of text for compressed consent is worse than
the wall of text.

**If the client has no chooser, ask in text and lose nothing.** Same prose, same
facts, the question alone on the last line, the options named in a sentence. The
text form is the one that has to be complete; the chooser is presentation. Not
every client has the tool, and an older client is a real prospect — the same
class of assumption as `${VAR}` expansion inside `env`, which is measured on one
client version rather than guaranteed.

## Start by finding out where you are

The person may be at any point in the trial — the session that set this up is
probably long gone. `SECURITY.md` is still the first thing to read, as the top
of this file says; this is the first thing to run, and nothing else you do with
the person comes before it:

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

**2. Say what will happen, before it does — four labelled lines, then the ask.**
The shape is the instruction and not a suggestion, because the previous version
of this paragraph said "briefly" and got ~900 words in four sections: "briefly"
is an adjective, and it loses to "be thorough about security" every time. Use
these four labels, in this order, one line each:

- **What changes** — one entry in their MCP config is replaced so their server
  runs behind a local proxy; their credentials are untouched; it is reversible
  with one command and stays in place until they run it.
- **What your agent sees** — two tools it did not have, `baton_annotate` and
  `baton_session_report`, both answered by the proxy and never by their server,
  plus three parameters grafted onto their existing tools' schemas, which the
  proxy strips back out before it forwards each call. One of the three,
  `user_goal`, is **marked required in the schema and never enforced** — say
  both halves: nothing validates it, a call that omits it is forwarded exactly
  as it would have been, and their server never sees any of the three.
- **What is captured** — every call through that server, with its full arguments
  and full results, written to a file in this folder. **Business data is not
  redacted.** That clause is the worst fact in the summary and the one a reviewer
  is listening for: state it in full even though the line is otherwise short.
  Shortening a line is not what the bound is for.
- **What leaves** — nothing unless they send it at the end, and no account
  either way. The wrap holds no credential and makes no network call; one
  command, `kit.py upload`, does send the capture, and only if they type it.
  Both halves belong in the line: a summary that says "nothing leaves" beside a
  send command that exists is a gap a reviewer will close for themselves, and
  worse than the longer sentence.

Then two more lines and nothing else: `SECURITY.md` §3 lists every addition, so
offer it explicitly rather than merely citing it; and the ask, alone on the last
line. Nothing may follow the question — an ask buried above a paragraph is how a
person ends up approving a summary they were still reading. This is the second
of the four forks, so put the ask in a chooser if you have one: the four lines
above it are the disclosure and they stay in prose either way — the rule is in
*The four times you ask*.

**If it is a remote server, add a fifth label — `If your server is remote` — and
that one gets three lines rather than one.** It is a separate consent and it does
not compress: someone who approved the stdio story has not yet approved this one.
First: before the wrap no process of ours runs on their machine at all, and after
it one does, holding their bearer token in its environment. It sends that token
to the endpoint their config already named, and nowhere else. Second: the kit
copies the token between two config slots without resolving it, so a `${VAR}`
reference stays a reference — but that relies on their client expanding `${VAR}`
inside `env`, which is measured behaviour on one client version rather than a
guarantee. Third: tell them to run `receipt` on the first day — an empty file is
how a wrap that cannot authenticate gets found in an hour instead of at the end
of the trial. `SECURITY.md` §2 carries all of this if they want it in writing.

**If their server signs them in to something — Notion, Google, a ticketing
system — say so before you run setup, not after.** One line, under the same
rule as the four above, and only where it applies. A server that holds its own
sign-in session may treat its first wrapped start as a new one and open a
browser tab asking them to authorize it again, and the consent screen will name
a `localhost` port. That port is their own server's, not ours, and the access
goes where it always went — but a browser window opening unbidden during a
security review reads as our tool authorizing itself against a third party, and
by then there is no good moment to explain. You cannot tell from the config
which servers do this, so ask rather than infer, and if they do not know, say it
as something that may happen rather than something that will. `SECURITY.md` §2
carries it in writing.

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

**Two of its rows are about intent, and they are different mechanisms.**
`intent captured` counts tool calls that carried the goal parameters the proxy
grafts onto their own tools' schemas — those ride the call itself, there is no
prompt attached to them and nothing to switch off, so a refusal cannot produce
a zero there. `annotations filed` counts what their agent chose to file with
`baton_annotate`, which is friction only — a wrong result, a dead end, a missing
capability — and zero is the ordinary case rather than a fault. That is also the
one number a refusal can explain, and it is invisible: a tool declined at a
prompt is declined inside their client and never reaches the proxy, so a refusal
and a smooth session are the same zero here. When either row is zero the receipt
prints what it can and cannot tell apart underneath it. Relay those lines and
stop there.

**`secrets redacted` counts patterns, not findings.** Where the kit prints a
note under a category — `cc` has one — that note is the reading of the number.
Give it whole rather than in your own words: it says what the count cannot mean,
and it is the sentence a person repeats to their security reviewer.

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
  sent, they send it. You do not — that is the rule, not a preference, and it
  holds even if they ask you to.
- **Where the receipt also offers `python3 kit.py upload`, relay it as a second
  option and nothing more.** It appears only for a trial we set up a workspace
  for in advance, which is why the receipt is the thing that knows: if that
  block is not in the output, the option does not exist and inventing it sends
  someone to a command that will refuse. Where it is there, it is the same
  decision as the email — their data, their call, their keystroke — and the
  file is worth reading first either way. Do not present it as the recommended
  one; it is shorter, not safer.
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

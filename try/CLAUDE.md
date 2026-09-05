# The Baton trial: how to run it with the person in front of you

You are helping someone trial Baton on their own machine. The rules below are
the promises this kit makes; nothing you do may break them. `SECURITY.md` in
this directory is the source for the security detail, if they ask for it.

This kit works with Claude Code only; it edits `~/.claude.json`, which no
other client uses. If there is any doubt about what the person is running, say
so at the start.

Four commands do the work. Run them from this `try/` directory; the person's
session is one level up, where they cloned.

```
python3 kit.py setup <server-name>    # wrap one configured MCP server
python3 kit.py receipt                # what has been captured so far
python3 kit.py uninstall              # put the original entry back
python3 kit.py upload                 # send the capture; only after the person places upload.json
```

Your job is what the commands leave to a person: which server, the second
terminal, and the decision at the end. Do not reimplement the commands.

## Rules that do not bend

**Never edit an MCP config yourself.** Not `~/.claude.json`, not `.mcp.json`,
not by hand, not with a script, and never to get around something `kit.py`
refused. Only `setup` and `uninstall` touch a config file.

**A refusal is an answer.** When a command exits non-zero it prints why and what
the options are. Relay that and let the person choose. Do not retry with
different flags, do not delete `state.json` or a `config-backup.*` file to clear
a refusal, and do not pick for them when the command says it will not pick.

**Never explain a number you only counted.** The receipt reports what is in the
file. It does not know why, and neither do you: the causes are in their client,
their prompts and their data, none of which reach this machine. Where a count
has more than one cause the receipt names them; relay that and stop.

**Do not quote the captured events into the conversation.** `events.jsonl`
holds the full arguments and results of every tool call. `receipt` prints safe
aggregates; report those, and do not tell them you are not quoting the file.

**Do not read out a credential the commands hid.** The kit shows withheld values
as `<literal value, not shown>`. `state.json`, the `config-backup.*` files and
the config itself hold the real values; if a command tells you to read one,
report key names, never values. A `${VAR}` reference is a pointer and is fine
to quote. **Never open `upload.json`.** It is a live key we issued.

**Never send the file anywhere except through `python3 kit.py upload`, and
only after the person tells you the credential file is in place.** Not by
email, not to a paste service, not attached to anything. `upload` needs
`upload.json`, which we emailed to the person; you never open it, copy it, or
search for it. Without it the command refuses, and that is the answer.

**Never install anything.** The trial runs from this checkout with no
dependencies. If something seems to need an install, report it.

## How to ask

Four decisions are the person's: whether to go ahead (the paste asks this
right after the clone; do not ask it again), which server to wrap, whether to
send the file, and whether to remove the wrap. Use a chooser where your client
has one (`AskUserQuestion` in Claude Code): facts in prose above it, short
option labels, every option a section names even if you expect it to be false
for this person, and a question that says what it is about and what happens
next. Without a chooser, ask in text with the question alone on the last line.

## Start by finding out where you are

The person may be at any point in the trial. The first thing to run is
`python3 kit.py receipt`; its first line says where you are.

- **No setup state found**: nothing is wrapped. Go to *Setting up*.
- **Setup state has been cleared**: the trial was ended. Report the counts if
  wanted; do not offer setup as though nothing had run.
- **THE WRAP IS GONE**: the config entry changed, so capture stopped. Counts
  above the banner are real; go to *Ending it*. `uninstall` clears the state.
- **No events yet**, or **connected but nothing called it**: relay the
  receipt's checklist in order. The capture path is working.
- **Counts**: the trial is running. Report them per session, and go to
  *Ending it* when they are done.

## If they asked for the security detail

The paste asks them once, right after the clone, whether they want details on
security. If they said proceed, do not bring security up again; their answer
stands. If they said yes, answer the four things the paste names from
`SECURITY.md`, in full: what it changes (§2, and §3 for what their agent sees),
what it captures (§5), what is saved on their disk (§7), what leaves the
machine (§4). Then ask whether to
proceed with the install, and go to *Setting up*.

## Setting up

**1. Find the server.** Run `python3 kit.py setup` with no arguments. It lists
the servers it can wrap, and any it cannot and why, from `~/.claude.json`; for
a project-local `.mcp.json`, ask for the path and pass `--config-file <path>`
to `setup` only. Show the list and ask which one they want; the trial is worth
most on a server they use daily. If the list is empty, say so and stop.

Each offered row is marked `stdio` or `remote`. A remote server is offered
only when its single `Authorization: Bearer` header holds a token written in
the config; the wrap turns it into a local process bridging to the same
endpoint. Do not argue an entry past a refusal; each one exists because the
wrap would produce a server that cannot authenticate.

**If they picked a remote server, say three things before `setup` runs**,
whether or not they asked for the security detail: after the wrap a process of
ours runs on their machine, holding their bearer token and sending it only to
the endpoint their config named; the kit copies the token as a `${VAR}`
reference without resolving it, which relies on their client expanding it
inside `env`; and they should run `receipt` on the first day, because an empty
file is how a broken wrap gets found early. `SECURITY.md` §2 has it in writing.

**If their server signs them in to something (Notion, Google, a ticketing
system), say so before setup.** The first wrapped start may open a browser tab
asking them to authorize again, naming a `localhost` port; that port is their
server's, and the access goes where it always went. Ask; if they do not know,
say it may happen.

**2. Run it.** `python3 kit.py setup <name>`. It prints the resulting config
entry; show that rather than summarising it. Do not ask them to name a tenant
or a label. The events are tagged with the server's name.

**3. Hand them a second terminal, and stay in this one.** Setup prints a line
beginning `Open a second terminal` that says where to start it; relay that as
printed. Then end your message with this, and nothing after it:

> Leave this window open. Open a second terminal, start your client there, and
> use the server the way you normally would. This window keeps the old server;
> the new terminal gets the wrapped one. Come back here when you are done.

## While it runs

Nothing is waiting on you. The wrap is a permanent edit: the original entry is
kept in `state.json` and the wrapped entry stays live until `uninstall`, or
until their client rewrites the config underneath it. A new session will not
have this file unless it starts in `try/` or is told to read `try/CLAUDE.md`;
say so if they plan to come back later. If they check in, run `receipt` and
relay it. Two of its rows are about intent: `intent captured` counts calls that
carried the goal parameters the proxy adds to their tools, so a refusal cannot
zero it; `annotations filed` counts what their agent chose to file with
`baton_annotate`, friction only, so zero is ordinary.

## Ending it

Run `receipt` and let it decide which of these you are in. Nothing is switched
off by "done"; they can use the server more and say it again.

**Calls landed.** Say what was captured in two or three lines from the receipt
(sessions, tool calls, annotations). Then say this, and nothing else:

> Go back to the email from Baton, save the attached `upload.json` (it lands in
> your Downloads folder), and tell me when it's there.

When they say so, say this, with the real path, and ask with a chooser (Send /
Not yet):

> It's at `/full/path/to/try/events.jsonl` if you want to look at it first.
> Send it now?

On Send, run `python3 kit.py upload --credentials ~/Downloads/upload.json` from
this directory and relay what it printed: the sent count and the sign-in line.
If it says the file is not there, ask where they saved it and run it again with
that path. If they say they never got such an email, the receipt prints a
`gzip` command and an address; relay those two lines and stop. Do not offer to
get them provisioned.

Nothing else in this step: do not restate what the file holds or what the
scrubber does, no security facts (those were offered at the start), no reading
advice beyond the one line above, no list of things you will not do, no
reminder that the wrap is still on. Sending again later is safe and adds only
the new events.

**Anything else** (connected but nothing called it, the wrap is gone, nothing
at all): relay the receipt's banner and checklist in order and do not offer the
file, except that when the wrap is gone the counts above the banner are real,
so hand over the decision as above.

## Removing it

`python3 kit.py uninstall` restores the original entry and prints it. Offer it
when they ask, not after a good capture; ending the data-gathering and removing
the wrap are separate decisions. New sessions get the original server back; one
already running keeps the wrapped one until it ends. `events.jsonl` and the
`config-backup.*` files are left deliberately; say so, and that deleting them is
up to the person. Deleting this checkout removes everything else. Uninstall must
work at any point, including mid-setup. Treat the request as final.

## If something is wrong

Report it; do not route around it. A command that refuses, a server that stops
working, a receipt whose numbers look wrong: all of these are worth more as an
accurate description than as something you quietly fixed. If the person wants
to abandon the trial, run `uninstall` and say it is done.

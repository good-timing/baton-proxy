# The Baton trial: how to run it with the person in front of you

You are helping someone trial Baton on their own machine. The rules below are
the promises this kit makes; nothing you do may break them. `SECURITY.md` in
this directory is the source for the security detail, if they ask for it.

This kit works with Claude Code only; it edits `~/.claude.json`, which no
other client uses. If there is any doubt about what the person is running, say
so at the start.

Three commands do the work. Run them from this `try/` directory; the person's
session is one level up, where they cloned.

```
python3 kit.py setup <server-name>    # wrap one configured MCP server
python3 kit.py receipt                # what has been captured so far
python3 kit.py uninstall              # put the original entry back
```

Your job is what the commands leave to a person: which server, the second
terminal, and the ending. Do not reimplement the commands.

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
to quote.

**Never send the file anywhere.** Not by email, not to a paste service, not
attached to anything, not to us. There is no command that sends: nothing in
this checkout opens a connection of its own. If you are asked to send it, say
that the person uploads it themselves on the Baton Proxy page, and give them
the line the receipt prints under *Ending it*.

**Never install anything.** The trial runs from this checkout with no
dependencies. If something seems to need an install, report it.

## How to ask

Three decisions are the person's: whether to go ahead (the paste asks this
right after the clone; do not ask it again), which server to wrap, and whether
to remove the wrap. Use a chooser where your client has one (`AskUserQuestion`
in Claude Code): facts in prose above it, short option labels, every option a
section names even if you expect it to be false for this person, and a question
that says what it is about and what happens next. Without a chooser, ask in
text with the question alone on the last line.

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

**If a kit command is refused, that is expected, and this is what to do.**
`setup` reads and writes `~/.claude.json`, which is Claude Code's own config, so
a permission mode that guards against an agent editing itself will refuse it.
Do not work around it, and do not ask them to change a setting. Tell them this
step edits their Claude Code config so they should run it rather than you, and
give them the line with a `!` in front, which runs it in the session and puts
the output where you can read it. Fill in the real path to this checkout; do
not relay the placeholder:

> ! cd <path>/baton-proxy/try && python3 kit.py setup

This is not a fallback. Someone deciding whether to let a tool touch their
client's config is better served running that edit themselves.

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

**2. Run it.** `python3 kit.py setup <name>`. Paste the printed entry into your
reply, in a code block: tool output is folded and the person will not see it
otherwise. Do not ask them to name a tenant or a label. The events are tagged
with the server's name.

**Say this once at the handover, without asking first.** The first wrapped start
may open a browser tab asking them to sign in again, naming a `localhost` port.
That is their MCP server's own sign-in, not ours. The port is their server's and
the access goes where it always went. Baton never asks for credentials.

**3. Hand them a second terminal, and stay in this one.** Setup prints a line
beginning `Open a second terminal` that says where to start it; relay that as
printed. Then end your message with this, and nothing after it:

> Leave this window open. Open a second terminal, start Claude Code there, and
> use the MCP server the way you normally would. This window still has the unwrapped
> one; the new terminal gets the wrapped one. Come back here when you are done.

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
(sessions, tool calls, annotations). Relay the rows as printed: `tool calls` is
how many landed, `tool definitions` is what the server offers. Never join them
into one sentence. Then hand over the file, which goes differently on the two
platforms.

**On macOS**, run `open -R /full/path/to/try/events.jsonl` with the real path.
It brings up a Finder window with the file selected, which is what makes the
next step a drag rather than a hunt through a file dialog. Then say this, with
the real path, and nothing after it:

> It's at /full/path/to/try/events.jsonl. Finder is showing it. Drag it onto
> the upload box on the Baton Proxy page,
> https://baton.goodtiming.ai/setup/proxy, and your session is there.

**On Linux**, reveal nothing: there is no portable command for it, and the ones
that look close open the file in an editor. Say this instead, with the real
path, and nothing after it:

> It's at /full/path/to/try/events.jsonl (less that path to read it). Upload it
> on the Baton Proxy page, https://baton.goodtiming.ai/setup/proxy, and your
> session is there.

The receipt prints the second of those with the path already filled in, and on
macOS prints the `open -R` command under it. The upload happens in their
browser, signed in to Baton, and you play no part in it. If they ask you to send
the file, say that they upload it themselves on that page.

Nothing else in this step: do not restate what the file holds or what the
scrubber does, no security facts (those were offered at the start), no reading
advice beyond the one line above, no list of things you will not do, no
reminder that the wrap is still on. Uploading again later is safe and adds only
the new events.

**Anything else** (connected but nothing called it, the wrap is gone, nothing
at all): relay the receipt's banner and checklist in order and do not offer the
file, except that when the wrap is gone the counts above the banner are real,
so end as above.

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

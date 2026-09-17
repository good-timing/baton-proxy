# The Baton trial: how to run it with the person in front of you

You are helping someone trial Baton on their own machine. The rules below are
the promises this kit makes; nothing you do may break them. `SECURITY.md` in
this directory is the source for the security detail, if they ask for it.

This kit works with Claude Code only; it reads `~/.claude.json` to copy the
server's settings without changing it, and that file is one no other client
uses. The wrapped copy goes in a `.mcp.json` in the checkout, which Claude Code
loads for that directory. If there is any doubt about what the person is
running, say so at the start.

Three commands do the work. Run them from this `try/` directory; the person's
session is one level up, where they cloned.

```
python3 kit.py setup <server-name>    # wrap one configured MCP server
python3 kit.py receipt                # what has been captured so far
python3 kit.py uninstall              # remove the wrap this checkout holds
```

Your job is what the commands leave to a person: which server, the second
terminal, and the ending. Do not reimplement the commands.

## Rules that do not bend

**Never edit an MCP config yourself.** Not `~/.claude.json`, not a `.mcp.json`,
not by hand, not with a script, and never to get around something `kit.py`
refused. Only `setup` and `uninstall` write one.

**A refusal is an answer.** When a command exits non-zero it prints why and what
the options are. Relay that and let the person choose. Do not retry with
different flags, do not delete `state.json` or a `config-backup.*` file to clear
a refusal, and do not pick for them when the command says it will not pick.

Two refusals name a way forward, and those are the exception. `setup` refuses a
`.mcp.json` left in this checkout by an earlier trial and says deleting it is
safe — that one file is the kit's own, inside the kit's own directory, and
removing it is allowed. `setup` also refuses an entry that cannot be copied out
of their config and offers `--in-place` instead. Relay either as printed, and
let the person choose. Neither is a licence to retry anything else.

**Never explain a number you only counted.** The receipt reports what is in the
file. It does not know why, and neither do you: the causes are in their client,
their prompts and their data, none of which reach this machine. Where a count
has more than one cause the receipt names them; relay that and stop.

**Do not quote the captured events into the conversation.** `events.jsonl`
holds the full arguments and results of every tool call. `receipt` prints safe
aggregates; report those, and do not tell them you are not quoting the file.

**Do not read out a credential the commands hid.** The kit shows withheld values
as `<literal value, not shown>`. `state.json`, the `config-backup.*` files, the
`.mcp.json` this checkout holds and the config itself hold the real values; if a
command tells you to read one, report key names, never values. A `${VAR}`
reference is a pointer and is fine to quote. The kit's own `.mcp.json` is on
this list for the same reason as the rest: if their original entry carried a
literal token rather than a `${VAR}`, the copy carries it too.

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
`setup` reads `~/.claude.json`, which is Claude Code's own config, so a
permission mode that guards against an agent touching its own settings will
refuse it — a read is enough to be refused, and being refused for reading is
not a sign anything is wrong. Do not work around it, and do not ask them to
change a setting. Tell them this step reads their Claude Code config so they
should run it rather than you, and give them the line with a `!` in front,
which runs it in the session and puts the output where you can read it. Fill in
the real path to this checkout; do not relay the placeholder. Tell them to copy
the line and paste it at their prompt. They may never have used `!` before, so
do not assume the line explains itself:

> ! cd <path>/baton-proxy/try && python3 kit.py setup

This is not a fallback. Someone deciding whether to let a tool read their
client's config is better served running it themselves.

Once a kit command has been refused, do not attempt `setup` again for the rest
of the trial, with or without a server name: hand it over the same way, as a
`!` line with the real path filled in, without trying it first. Every hand-over
carries the instruction to copy the line and paste it, not only the first one.

`setup` is the only command that opens their config. `receipt` and `uninstall`
read and write only the kit's own files — its state, its event file, and the
`.mcp.json` this checkout holds — so keep running those yourself.

A refusal can still reach them for a different reason: some permission modes
refuse to run code from a repository the person has just cloned, whatever it
touches. That refusal does not care which command it is, so if one of those two
is refused as well, hand it over the same way rather than treating it as a
contradiction.

**1. Find the server.** Run `python3 kit.py setup` with no arguments. It lists
the servers it can wrap, and any it cannot and why, from `~/.claude.json`. Show
the list and ask which one they want; the trial is worth most on a server they
use daily. A project-local `.mcp.json` is one more option in that chooser, not
a sentence above it: if they pick it, ask for the path and pass
`--src-config <path>` to `setup` only. If the list is empty, say so and stop.

Each offered row is marked `stdio` or `remote`. Offer only the rows the kit
lists as wrappable: it has already decided which servers it can carry, and it
prints its own reason beside each one it cannot. Do not argue an entry past a
refusal; each one exists because the wrap would leave them with a server that
does not work.

**2. Run it.** `python3 kit.py setup <name>`. If you ran it, paste the printed
entry into your reply, in a code block: tool output is folded and the person
will not see it otherwise. If they ran it themselves with `!`, they watched it
print: do not reprint the entry or repeat the guidance printed under it. Say
briefly, in plain words, what changed, which the raw output does not give them,
and move on. Do not ask them to name a tenant or a label. The events are tagged
with the server's name.

**Say this once at the handover, without asking first.** The first wrapped start
may open a browser tab asking them to sign in again, naming a `localhost` port.
That is their MCP server's own sign-in, not ours. The port is their server's and
the access goes where it always went. Baton never asks for credentials.

**3. Hand them a second terminal, and stay in this one.** Setup prints a line
beginning `Open a second terminal` that says where to start it, and a `cd`
command under it. Relay both as printed.

**The folder is the whole of it.** The wrap is a project config, so it loads
only for a session started in the directory setup named. A terminal opened
anywhere else gets their ordinary server and captures nothing, and that looks
exactly like a broken install. Fill in the real folder below; do not relay the
placeholder. Then end your message with this, and nothing after it:

> Leave this window open. Open a second terminal, run `cd <folder>`, and start
> Claude Code there. Use the MCP server the way you normally would. Starting it
> anywhere else will not record anything. This window still has the unwrapped
> one; the new terminal gets the wrapped one. Come back here when you are done.

Their first start in that folder asks two questions: whether they trust the
folder, and then whether to approve the server it defines. Both have to be
answered before anything is captured. Do not pre-empt them — say so only if
they come back with nothing recorded, where the receipt's checklist covers it.

## While it runs

Nothing is waiting on you. The wrap stays until `uninstall`: the `.mcp.json`
this checkout holds is not a file any client maintains, so nothing rewrites it
underneath them. It applies in this directory and nowhere else. A new session will not
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

`python3 kit.py uninstall` deletes the `.mcp.json` this checkout holds and says
what it removed. There is nothing to restore: their own config was never
changed, so the original server is what it always was. Offer it when they ask,
not after a good capture; ending the data-gathering and removing the wrap are
separate decisions. New sessions in this folder get the original server back;
one already running keeps the wrapped one until it ends. `events.jsonl` is left
deliberately; say so, and that deleting it is up to the person. Deleting this
checkout removes everything else. Uninstall must work at any point, including
mid-setup. Treat the request as final.

## If something is wrong

Report it; do not route around it. A command that refuses, a server that stops
working, a receipt whose numbers look wrong: all of these are worth more as an
accurate description than as something you quietly fixed. If the person wants
to abandon the trial, run `uninstall` and say it is done.

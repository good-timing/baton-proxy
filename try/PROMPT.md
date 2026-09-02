# The prompt

This is the paste. It is the first thing a prospect sees, it runs before
anything is on their disk, and it is the only part of the kit that is not
already in the kit — so it lives here rather than in an email thread.

Everything below the rule is the text. It assumes nothing has been cloned yet.

It travels two ways now, and the second one is why step 1 asks where to clone.
Pasted into a session, the person chose the directory by starting the session
there. Sent as a file — which is how a provisioned handover goes out, alongside
their `upload.json` — it is opened from wherever it was downloaded, and "the
current directory" silently means the downloads folder. The text has to work
either way, because we do not get to know which one happened.

---

This kit is written for Claude Code. It finds and rewrites `~/.claude.json`,
which no other client uses, so if I am running something else, say so now
rather than after I have approved things.

1. Clone https://github.com/good-timing/baton-proxy into the current directory.
   Do not search my machine for an existing copy — if I already had one, I would
   have told you. If you are reading this out of a file I downloaded rather than
   a message I typed, ask me where the kit should live first: this session may
   be running in my downloads folder, and that is not where I want it.

2. Read `baton-proxy/try/SECURITY.md` and `baton-proxy/try/CLAUDE.md`, then tell
   me in your own words, one line each and in this order: what changes, what
   your agent sees, what is captured, what leaves. One line each, not a section
   each — and where the honest answer will not fit in a line, give me the worst
   of it rather than the shortest of it. Then offer me the detail and ask me to
   confirm — name in the question what I am confirming and what you will do
   next, because "go ahead" on its own tells me nothing — with the question on
   the last line and nothing after it. Do not touch my config until you have.
   If I say I do not need the detail, skip ahead — it is my call, not yours.

3. List the MCP servers you could wrap and let me choose one. Do not choose for
   me, and do not wrap anything before I have said which.

4. From `baton-proxy/try`, run the setup command for the server I picked, then
   tell me what it changed. Leave this window open — it has the security detail
   and the config diff in it — and tell me where to start a second `claude` so
   the wrapped server actually loads.

I will use that server for real work, then come back to this window and tell you
I am done.

# The prompt

This is the paste. It is the first thing a prospect sees, it runs before
anything is on their disk, and it is the only part of the kit that is not
already in the kit — so it lives here rather than in an email thread.

Everything below the rule is the text. It assumes nothing has been cloned yet
and that the person is talking to a Claude Code session started wherever they
want the kit to land.

---

This kit is written for Claude Code. It finds and rewrites `~/.claude.json`,
which no other client uses, so if I am running something else, say so now
rather than after I have approved things.

1. Clone https://github.com/good-timing/baton-proxy into the current directory.
   Do not search my machine for an existing copy — if I already had one, I would
   have told you.

2. Read `baton-proxy/try/SECURITY.md` and `baton-proxy/try/CLAUDE.md`, then tell
   me in your own words, one line each and in this order: what changes, what
   your agent sees, what is captured, what leaves. One line each, not a section
   each — and where the honest answer will not fit in a line, give me the worst
   of it rather than the shortest of it. Then offer me the detail and ask me to
   confirm, with the question on the last line and nothing after it. Do not
   touch my config until you have. If I say I do not need the detail, skip
   ahead — it is my call, not yours.

3. List the MCP servers you could wrap and let me choose one. Do not choose for
   me, and do not wrap anything before I have said which.

4. From `baton-proxy/try`, run the setup command for the server I picked, then
   tell me what it changed. Leave this window open — it has the security detail
   and the config diff in it — and tell me where to start a second `claude` so
   the wrapped server actually loads.

I will use that server for real work, then come back to this window and tell you
I am done.

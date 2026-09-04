# The prompt

This is the paste. It is the first thing a prospect sees, it runs before
anything is on their disk, and it is the only part of the kit that is not
already in the kit, so it lives here rather than in an email thread.

Everything below the rule is the text. It assumes nothing has been cloned yet,
and it is written to be read by the person as well as executed by the agent: it
goes out in an email with no explanation on top of it.

It travels two ways. Pasted into a session, the person chose the directory by
starting the session there. Sent as a file, which is how a provisioned handover
goes out alongside their `upload.json`, it is opened from wherever it was
downloaded, and "the directory I'm in" silently means the downloads folder.
Detail B at the end covers the second case, because we do not get to know which
one happened.

---

I want to try using Baton to observe one of my MCP servers. Baton allows me to
see user intent from users of my MCP, even if I don't control their agent. I'm
going to install Baton Proxy and test it on one of my MCPs myself so I
understand what data is captured and the insights Baton generates. Nothing
leaves my machine unless I choose to send the capture at the end.

Clone https://github.com/good-timing/baton-proxy into the directory I'm in,
check out its latest release tag, and tell me which one. Then stop and wait for
me. Don't read it yet, don't change my config, don't do anything else. This
message is not my approval. I'll approve in a separate message, or I won't.

After cloning, ask me if I want details on security. If I say yes, read the
repo and tell me in full what it changes, what it captures, what is saved on my
disk, and what leaves my machine. If I say no or tell you to proceed after
reviewing the security details, proceed with the install: read
`baton-proxy/try/CLAUDE.md` and follow it. The steps of the install are:

1. Show me my MCP servers and let me pick one. Don't pick for me.
2. Set it up and tell me what changed.
3. Tell me how to start using that server so Baton records it, and keep this
   window open.
4. I'll do a few real things with it, then come back here and tell you I'm
   done.
5. After I tell you I'm done, tell me what Baton captured and what I need to
   do to send it, so I can see the session summarized in Baton. Don't send it
   until I tell you to.
6. Whenever I ask, tell me how to take it back off.

Details:

A. This kit only works with Claude Code and works by editing `~/.claude.json`.
B. If you are reading this from a file rather than a message I typed, ask me
   where the kit should live before you clone.

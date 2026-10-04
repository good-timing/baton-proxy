# Per-repo guidance for Claude Code sessions (baton-proxy)

## Comments

This overrides "match the surrounding comment density": much existing code is over-commented and is being trimmed. Do not copy its density.

A comment explains **why**, and only when the code does something unusual. Test each one: without it, would a competent reader make a wrong edit here? If not, don't write it.

- **What the code does** → say it with names. A one-line docstring only when a name can't carry it.
- **Why, for an unusual case** → 1–2 lines, in place.
- **A rule that must not break** → a test. The comment, if any, names the test.
- **Design reasoning** → the design note. If the code no longer matches the note, fix one of them; don't explain the gap in a comment.
- **History** (dates, commit hashes, ticket/decision ids, measurements, "this used to…", how a bug was found) → the commit message. Never in the code.

When editing near an existing comment, check it is still true; fix or delete a false one rather than adding a second.

Exception: docstrings that something reads at runtime (MCP tool/resource/prompt docstrings, which agents see) are product copy, not comments. Change them deliberately.

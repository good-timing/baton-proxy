# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]


## [0.6.11] — 2026-09-23

Three wire changes that were sitting on `main` under `0.6.10`, a number already
published as the behaviour they replace. Cut so `sdk_version` can tell them
apart: every event carries `baton-proxy/<version>`, so an unreleased wire change
is one a consumer cannot date.

### Changed

- **BREAKING (wire): the flat field `principal_id` becomes the object
  `principal: {id, source, form}`** (SPEC §11.4). All three members are
  required together, so the proxy emits the whole object or omits `principal`
  entirely — there is no event carrying an id whose classification a consumer
  has to guess. `id` is the same hashed value under the same `h1:` tag; the
  field name was never part of the HMAC message, so **no digest moves.**

  **`source` is `"asserted"`, and that is a CORRECTION rather than a new fact**
  (SPEC §13 (5)). The proxy resolves no identity of its own, and its one real
  caller — `baton-extmcp` — reads a gateway header (`x-gw-ims-user-id`) that no
  verifier in the producing stack ever checked. Under the retired encoding
  every such event stamped `h1:`, which the old rule read as attested, so these
  principals claimed an attestation that never happened. ⚠ **Consumer
  consequence: principals that read as verified stop reading as verified, with
  no change in the underlying identity.** That is the removal of a false claim,
  not a drop in data quality — and historical events cannot be corrected,
  because the wire recorded `h1:` and the distinguishing fact was never
  transmitted. A consumer reasoning about attestation over stored data must
  bound its window to this release or later.

  `form` is `"hashed"` on every emitted object: there is no raw mode here, and
  with no HMAC key configured the principal is dropped WHOLE rather than
  emitted verbatim — fail-open as before, but the whole member goes, never a
  null id inside a present object.

  ⚠ **NOT DEPLOYABLE AGAINST AN OLDER COLLECTOR.** `principal` is an *envelope*
  field, which is the level at which a closed schema refuses — so a collector
  that rejects unknown envelope members drops the whole event, not just the
  identity. Upgrade the collector to one that accepts `principal` before
  upgrading the proxy, and verify that against its ingest schema rather than its
  version string.

  Configuration is deliberately unchanged: `principal_id_hmac_key` and
  `BATON_PRINCIPAL_ID_HMAC_KEY` keep their names. SPEC does not ask for a
  rename and it would break every existing deployment to chase a wire field.

  ⚠ **`baton-extmcp` ships its matching `baton-spec` pin in the same release**
  — it builds its `Principal` from this package and its own floor has no
  ceiling, so the two cannot be split.

- **The `baton-spec` pin moves `d5c8016` → `f1e0280`.** It brings the principal
  object plus three unrelated additive things — the optional `result` on
  `ToolCallErrorPayload`, `transport_observed`, and regenerated vectors — none
  of which change what this proxy emits. ⚠ `result` is the field `3ba25d7`
  already emits, so the pin stops contradicting this repo's own isError work.

### Fixed

- **BREAKING (wire): a tool call that FAILED is no longer filed as a success.**
  MCP files a failed `tools/call` as a 200 whose body sets `isError`; a JSON-RPC
  `error` member means a protocol fault. The proxy classified on the `error`
  member alone, so every tool failure rode as `tool_call_end`.

  For a wire sensor that is **100% of failures, not a subset.** Measured on real
  stdio round trips across `mcp` 1.27.2 / 2.2.0 and `fastmcp` 2.14.7 / 4.0.3:
  every server converts a RAISED handler exception into a 200 carrying
  `isError: true` before the bytes leave the process. So the two failure shapes
  SPEC §11.4.3 defines — raise and return — are indistinguishable here, and the
  proxy always emits the returned form: `error_type: "tool_error"`, `error_body`
  unwrapped from the result's `content`, and the full envelope in a new optional
  `result` field so reclassifying does not move a structured body into a flat
  string.

  Consumers that count `tool_call_end` as "succeeded" will see those counts fall
  and `tool_call_error` rise. That is the correction, not a regression. The
  proxy's own `baton_session_report` trail changes with it: a failed call that
  rendered `` `tool` → ok (45ms) `` now renders `` `tool` → **tool_error**: … ``.

  Detection reads **one** spelling, `isError`, and that is version-proof rather
  than under-specified: the snake `is_error` is a Python attribute name from
  mcp 2.x's `mcp_types` rewrite, never a wire field — MCP's schema is camelCase
  and every server serialises `model_dump_json(by_alias=True)`. Nothing is
  truncated on the way; `baton-extmcp` cuts `error_body` at 2000 characters and
  that cut lands before PII scrubbing, which is a defect, not a model to copy.

  Baton's own synthesised refusals also carry `isError: true` and are answered
  without ever being tracked as pending, so they are never filed as vendor
  failures — pinned by a test rather than left to control flow.

- **The startup warning for the renamed HMAC variable named a field that no
  longer exists, and promised a remedy one of its two readers cannot honour.**
  It said `BATON_USER_ID_HMAC_KEY` is no longer read "so `principal_id` is OFF",
  which stopped being checkable the moment the flat field left the envelope — an
  operator grepping their JSONL for `principal_id` finds nothing whether
  identity is off or merely renamed. It now names the absent member,
  `principal`.

  ⚠ **And the remedy is qualified, because both callers of `from_env` print
  this line verbatim and it is actionable for only one of them.**
  `baton-extmcp` resolves a principal from its gateway header, so setting the
  key does restore the member there. The stdio proxy resolves no identity of its
  own — there is no `Principal(` construction outside `identity.py` — so an
  operator who set the key, grepped for `principal` and found nothing would
  conclude the fix had failed. The sentence now says where it applies.

### Removed

- **BREAKING: the `scan` subcommand is gone.** `baton-proxy scan --config <name>`
  drove a headless `claude -p` through a wrapped server and wrote a local
  `./baton-report.md`. It was the activation path before the try kit
  (`/setup/proxy`), which supersedes it and never invoked it. Removed outright
  rather than deprecated: nothing is pinned to it, and a shim nobody calls is
  removed on a grep rather than on an event.

  Going with it: the mechanical half of the friction report
  (`report.synthesize_scan` and the findings derivation beneath it). The report
  tool injected on a file sink, `baton_session_report`, is **unaffected** — it
  renders from model-filed reactive annotations and never used that path.

  `baton-proxy scan ...` is no longer a subcommand, so it is now read as a
  request to wrap an upstream server named `scan`, which fails at spawn.

  SECURITY.md §4's call-site table drops to four rows and §9's grep now
  promises five matches rather than six. Both counts are pinned by tests.


## [0.6.10] — 2026-09-18

### Fixed

- ⚠ **`SECURITY.md` §3a still described the old behaviour, in all three rows.** It told you `setup` backs up and rewrites your config, that `receipt` opens it, and that `uninstall` writes your entry back. None of that has been true by default since 0.6.9. This is the security document, the one people read precisely because they do not take the summary on trust, and it was saying the exact thing the 0.6.9 change exists to stop saying. The table is now a column per mode, so no row makes a promise without naming which wrap it belongs to, and the round-trip claim is split the same way: `uninstall(setup(x))` returning the original bytes is an `--in-place` property, while the default's promise is the stronger one, that your config file is byte-identical after setup.

- **A path with a space in it broke the commands we hand you.** `kit.py` quoted the paths in its `cd` lines but not in the `open -R` line it prints at the end, and `try/CLAUDE.md` spelled the same commands out again with no quotes at all. `/Users/you/Client Work/app` is an ordinary folder name, and an unquoted path stops at the first space — so the command succeeds against the wrong directory and says nothing. For the second terminal that means a session where the wrap never loads and nothing is captured, which looks exactly like a broken install. Five places fixed, and a test now sweeps every command in the doc that carries a path.

### Added

- **`SECURITY.md` now discloses `.claude/settings.json`**, which ships in this repo and pre-approves the kit's own command lines. Two things have to be true before it grants anything: it has to be the folder you started Claude Code in — the shared settings file is read from the session's working directory, not a subfolder, and the kit's commands run one level above the clone — and the folder has to be trusted, because Claude Code ignores a checked-in allow-list in a workspace you have not accepted the trust dialog for. It also notes what that dialog does for you: it reads the rules out by name before you accept. And it says plainly that one rule ends in `*`, which reaches `setup <server> --in-place`, and how to decline if you would rather your config could not be rewritten without a prompt.


## [0.6.9] — 2026-09-17

### Changed

- ⚠ **The trial no longer edits `~/.claude.json`. `setup` writes a `.mcp.json` in the checkout instead, and only reads their config to copy the server's settings.** Three prospects stopped at the same sentence rather than at anything the tool did. One, reading the paste: *"Now I'm thinking, oh God, this is going to permanently update my claude."* Another names the fix himself: *"It would be a lot better if the setting would allow you to wrap the MCP calls at project level, so as long as I work in this folder baton works."* A project `.mcp.json` outranks the user config for sessions started in its directory, so the wrap works without the file that holds every MCP credential they have being rewritten by a script from a repo they have not read.

  **What you change.** Nothing, if you take the default. `setup` now wraps into `baton-proxy/.mcp.json`, `uninstall` deletes that file, and there is nothing to restore because their own config was never written. The old behaviour is `setup --in-place`, which wraps the entry where it already sits and edits the config file it is in — the escape hatch for an entry that cannot be copied, and the only mode that still makes a backup.

  ⚠ **`--in-place` is not "global".** It does not move the entry, so it loads wherever the original loaded: everywhere for a top-level entry, and in ONE directory for an entry under a project key. `start_where` prints which; nothing should reason about the folder from the flag name.

- **The flags say their direction.** `--config-file` is now `--src-config`, which reads and never writes in any mode, and `--global` is now `--in-place`. The first pair could be combined into a write nobody asked for: `setup srv --config-file X` refused, the refusal said to re-run with `--global`, the flag stayed on the line, and X was edited. Neither name mentioned writing and the kit itself recommended the combination.

- **A server defined in more than one place is a choice, not a refusal.** `setup` prints a `--from` line per match instead of telling the person to rename one of their servers by hand, in the config they came here to protect.

- **The copied-entry path check warns instead of refusing.** Copying an entry into another directory can break a relative path, so `setup` says so — but it no longer stops. The check reads shape, not truth, and `npx -y mcp-remote <url> --header "Authorization: Bearer abc/def"` was refused outright because the base64 alphabet contains `/`. Under this design a false positive is expensive (the only exit it offered was `--in-place`, the exact act these prospects refuse) and a false negative is cheap (only the copy in the checkout breaks; their own config is never written).

### Fixed

- **A refusal could print a credential.** The copied-entry check interpolated raw values into a message `setup` writes to stderr, so a config with nothing wrong with it could put an `AWS_SECRET_ACCESS_KEY` on screen — and this kit is narrated by an agent, so whatever it prints is read into a model's context by design. All three positions — `command`, `args` and `env` — are now hidden in that message. The entry dump still shows `args`, which is `SECURITY.md`'s documented limit and is where the restore-recipe justification actually applies.

- **`try/CLAUDE.md` promises now name which wrap they are about.** Ten sentences held for the checkout wrap and were false under `--in-place`, including "there is nothing to restore" and "deleting this checkout removes everything else". The agent obeys that document, so each one would have been said to the person immediately after the opposite happened.


## [0.6.8] — 2026-09-14

### Changed

- ⚠ **BREAKING: `user_id` is now `principal_id`, on the wire and in configuration, with no aliases.** Events carry `principal_id` where they carried `user_id`, and `BATON_USER_ID_HMAC_KEY` is now `BATON_PRINCIPAL_ID_HMAC_KEY`. In Python, `Principal.user_id`, `hash_user_id` and `Config.user_id_hmac_key` are `Principal.principal_id`, `hash_principal_id` and `Config.principal_id_hmac_key`. The value is unchanged: the field name is not part of the HMAC, so every `h1:` hash is byte-identical. The field was documented as a person and as a customer at once, and a gateway's identity is often a service account; a principal is whoever the resolver named, at whatever grain that is. The same change ships in `baton-sdk` 0.8.6 and the TypeScript `@goodtiming/baton-sdk` 0.3.5.

  **What you change.** Rename the environment variable. The old one is not read; if it is set and the new one is not, the proxy logs a startup warning saying so, because hashed identity fails open and would otherwise just stop appearing. A collector must accept `principal_id` before this version sends to it; the hosted Console already does. ⚠ `baton-extmcp` builds `Principal` from this package, so it must move to this version in the same release.


## [0.6.7] — 2026-09-12

### Changed

- **The paste asks for the latest version tag, not the latest release tag.** In a live manual run on 0.6.6 the agent read "latest release tag" as GitHub's Releases page rather than a git tag. It ran `gh release list`, got back a prompt to run `gh auth login`, and handed the person two `!` lines telling them to authenticate the GitHub CLI, though it had the correct answer from `git tag` both before and after. That made the second screen of the trial an instruction to sign in to a third-party tool that has nothing to do with Baton, in a trial whose pitch is that nothing leaves their machine. The clone paragraph now says "check out its latest version tag", and the paste's pin moves with it: `PASTE_SHA256` is the new text's, and `PASTE_VERSION` is 0.6.7.


## [0.6.6] — 2026-09-12

### Changed

- **The paste tells the agent to hand over a command it cannot run, not to propose a setting change.** At the clone step the agent has only the paste, because `try/CLAUDE.md` is inside the repo it has not cloned yet, so every rule about handling a refusal was out of reach at the first refusal a prospect sees. In a harness run on 0.6.5, an agent met that refusal by offering to change the person's permission settings. The clone paragraph of the paste now ends: "If you can't run something, don't tell me to change a setting. Give me the command and I'll run it myself." The paste's pin moves with it: `PASTE_SHA256` is the new text's, and `PASTE_VERSION` is 0.6.6.

- **Every `!` hand-over carries the instruction to copy and paste it.** 0.6.5 added the instruction to the first hand-over after a refusal, then had the agent hand each remaining command over "the same way". In a harness run the agent read that as the `!` format alone, and a later hand-over went out without the instruction to copy the line. The refusal rule in `try/CLAUDE.md` now says every hand-over carries the instruction to copy the line and paste it, not only the first one, and the test on that paragraph pins the sentence.

- **`uninstall` says what the backups it leaves behind hold.** It listed the leftover `config-backup.*.json` files under "Deliberately left in place, for you to read or delete:", which says they are there and not what is in them. Each is a verbatim copy of the whole config, every server's `env` block with any literal credentials in it, so someone who uninstalled believing the tool was gone still had them on disk. The header now reads "Deliberately left in place. `config-backup.*` is a full copy of your config, every server's credentials included:". Nothing else changed: the backups stay where they are as evidence, and `uninstall` still deletes only `state.json`, and only once the restore verifies. A test pins the header and that behaviour together.

### Fixed

- **The config backup is created `0600` whatever the source's mode.** It was made with `shutil.copy2`, which copies the source file's mode, so a `~/.claude.json` at `0644` produced a world-readable backup holding a verbatim copy of every server's `env` block. `try/SECURITY.md` §2 and §7 both said the backup is `0600`. The backup is now opened `0600` and set to `0600` before the first byte is written, the same ordering the config write already uses. Three tests pin it: from a `0644` source, into a file that already exists, and through `setup` end to end.

- **`try/SECURITY.md` now covers what a remote wrap sends and what reaches stderr.** §2 and §4 disclose the two headers the bridge adds to every request to a remote server, `User-Agent: baton-proxy/<version>` and `Via: 1.1 baton-proxy`; a stdio wrap sends neither. §7 said captured events are not mirrored to stderr, which is true because the kit writes a file-only sink rather than because of the proxy's default, and it said nothing about the proxy's own status lines. Those go to stderr on every wrap, unscrubbed, where the client may keep them, and they can carry the full launch command or remote URL and the first 500 bytes of an HTTP error body. §7 now says both. Two tests tie these statements to the code: the headers are read off the bridge, and the default sink and the byte count are read from the source.

- **`try/SECURITY.md` stops calling `state.json` the one file holding a literal `env` value.** The backup holds the same values. Beside "`uninstall` deletes it", the old sentence told a reviewer that uninstalling removed their last copy of their credentials. §7 now names both files, and §5 names the backup among the places a literal upstream token is written.


## [0.6.5] — 2026-09-11

### Changed

- **The `!` line now comes with an instruction to paste it.** In a live run the agent handed the person the `!` line and never said what to do with it. In the terminal it renders as ordinary prose, the same colour as the sentence above it, and someone who has never used `!` in Claude Code does not know it is a command to copy into the prompt box; the whole handoff for a refused command depends on them knowing that. The refusal instruction in `try/CLAUDE.md` now tells the agent to have them copy the line and paste it at their prompt, and not to assume the line explains itself. The test on that paragraph pins both.

- **Setup no longer tells the person to check early.** 0.6.4 removed the day-one receipt nag from `try/CLAUDE.md`, but `setup` printed its own copy under a successful wrap: two lines telling them to run `receipt` on the first day because an empty file then is a quick fix and later a wasted trial. It told the person the wrap may well be broken before they had used it once, and handed them a check the ending already makes, since saying they are done runs `receipt`, which states what landed or that nothing did. Those two lines are gone from both setup paths, the first wrap and the re-entry; the rest of the printout is unchanged, and a test pins both halves.

- **The agent stops reprinting what the person watched print.** Step 2 told the agent to paste setup's printed entry because tool output is folded, which is true when the agent ran it. When the person ran it with `!` they watched it print, and in a live run the entry and the guidance under it appeared twice in a row. Step 2 is now conditional: the agent that ran setup still pastes the entry, and after a `!` run it reprints nothing, says briefly in plain words what changed, and moves on.


## [0.6.4] — 2026-09-11

### Changed

- **The main flow no longer delivers a security readout to someone who declined one.** `try/CLAUDE.md` had the agent say three things before wrapping a remote server "whether or not they asked for the security detail": that a process of ours holds their bearer token, that the token is copied as a `${VAR}` reference their client has to expand, and that they should run `receipt` on the first day. The paragraph is deleted, not moved. `SECURITY.md` §2 already carries the first two, shows the before-and-after config with the reference kept rather than asserting it, and reaches anyone who asks for the security detail in full. The day-one receipt line has no new home: the ending already runs `receipt` and says what was captured or that nothing was, so checking early was the same check handed to the person with less information, phrased as a warning that the wrap may not work. Step 1's paragraph on which rows are offered went the same way: it named the `Authorization: Bearer` header a remote entry needs, and now keeps only the operational rule, offer what the kit lists and do not argue past its refusals. The test that pinned the deleted paragraph is deleted; its replacement holds that the paragraph is gone, that the main flow never says "bearer" in any case nor names the `${VAR}` reference, and that §2 still carries both.

- **A refusal now carries over.** The refusal instruction from 0.6.2 handled each refused command on its own, so an auto-mode reader met the classifier's denial text once per config command: the listing, the wrap, and later the uninstall. Once a kit command has been refused, the agent now stops attempting `setup`, with or without a server name, and `uninstall` for the rest of the trial, and hands each one over as a `!` line without trying it first. It still tries the first one, because the model cannot see its permission mode and a refusal is the only signal it gets; in manual mode that first attempt is a prompt with a "don't ask again" option, which never trying would replace with a paste on every command. `receipt` stays with the agent while no wrap is in place, when it reads only the kit's own files. Once a wrap exists it also reads the config to check the wrap is still there, and that read is enough to be refused, so after a refusal it is handed over too; left with the agent, it would put the denial text on the finale, the one output the trial exists to produce.

- **Step 1 offers a project `.mcp.json` once.** It was named in the prose and again as an option in the chooser, and in a real run the agent relayed both, one under the other. It is now an option in the chooser only. `--config-file <path>` and its setup-only rule are unchanged.

### Fixed

- **`try/SECURITY.md` §3a said `receipt` reads only the kit's own files.** Once a wrap is in place it also reads the MCP config, to detect that the wrapped entry has gone. The row now says so, and still says it writes nothing and opens no connection. A new test records every file `receipt` reads, with no wrap and then with one, so the claim both documents now make is measured rather than trusted.


## [0.6.3] — 2026-09-11

### Changed

- The agent-facing instructions name the proxy for what it does, name the missing-tool case, and give the agent a reason to file.

## [0.6.2] — 2026-09-11

### Changed

- **A refused kit command now comes with an instruction, and the instruction is not a workaround.** `setup` reads and writes `~/.claude.json`, which is Claude Code's own config, and a permission mode that guards against an agent editing itself refuses that. Under Claude Code 2.1.268 in auto mode it stopped a trial at its first step. `try/CLAUDE.md` now opens *Setting up* by telling the agent the refusal is expected, that it must not route around it or ask the person to change a setting, and that it hands them the command with a `!` in front, so the person runs the edit to their own client's config and the output still lands in the session.

- **The re-authorization warning is said, not asked.** `try/CLAUDE.md` had the agent ask before setup whether the server signs them in to something, and every answer led to the same warning. It is now one unconditional line at the handover: the first wrapped start may open a sign-in tab naming a `localhost` port, that sign-in is their MCP server's own, and Baton never asks for credentials. The test that pinned the question now pins the line, its place between running setup and handing over the second terminal, and the absence of the question.

- **The handover names Claude Code.** The four lines `setup` prints about where to open the second terminal, and the block `try/CLAUDE.md` has the agent end its message on, said "your client". The kit works with Claude Code only, so a reader on another client could follow the line exactly and finish with an empty `events.jsonl` and no error. "Client" stays wherever it describes a mechanism rather than telling the person what to do, and the test on those four lines now holds every one of them to "start Claude Code".

### Added

- **`.claude/settings.json` pre-approves the four kit commands, and nothing wider.** `python3 kit.py setup`, `setup` with arguments, `receipt` and `uninstall` are allowed, so a manual-mode session stops asking for each one separately. Claude Code checks each part of a compound command on its own, and a `cd` into the working directory counts as read-only, so `cd <path>/baton-proxy/try && python3 kit.py setup` is covered too. The limit, from Claude Code's permissions and settings docs and not yet observed in a live session: it reads `.claude/settings.json` from the session's primary working directory, and applies its allow rules only after that folder's workspace trust is accepted. The paste starts the session in the directory the repository is cloned into, one level up, so as shipped these rules apply only to a session started inside the clone.


## [0.6.1] — 2026-09-11

### Changed

- **A caller can no longer assert its own runtime.** `detect_agent_runtime` honoured `_meta.baton.agent_runtime` and ranked it ABOVE the `claudecode/*` heuristic, on the reasoning that a client naming itself beats our guess about its key names. That reasoning is what fell: `agent_runtime` is self-reported and never attested, so the override let the thing being measured pick its own label. The SDK donor removed it in both spellings (nested at B5, reverse-DNS `io.baton/*` on 2026-09-09; SPEC §5.2 now reads "Recognized keys: none") and **this copy had not followed, so two sensors watching one client disagreed about what it was** — the single failure a hand-maintained cross-repo copy exists to prevent. Following meant DELETING the read, not adopting the new key name. The value is still carried: `runtime_meta` forwards `_meta` verbatim, so a client that sends one still reaches the Console as data to group on downstream.

  The test that asserted the override is now the test that asserts it is ignored, parametrised over both spellings — the nested one this file actually read, and the reverse-DNS one it never did, kept as a forward guard so "follow the donor" cannot later be misread as "adopt its new key". A second case pins that an override cannot *suppress* the heuristic either, which is the way a half-finished removal loses a detection rather than inventing one.

  The docstring's donor citation pointed at `baton/integrations/fastmcp/runtime_adapter.py`, a path that stopped existing at the adapter rename; it now names `integrations/runtime_adapter.py`.

- **`hash_user_id` takes `issuer`, closing a divergence this module's own header had opened.** `baton.identity` grew the parameter on 2026-09-09 — an OIDC `sub` is unique only within the provider that minted it (RFC 7519 §4.1.2), so two IdPs behind one vendor can hand the same `sub` to two people, who then hash to one `user_id` — while this file's docstring still promised the two copies were in lockstep. Not a correctness bug in anything emitted: the SDK's `issuer=None` reproduced this message byte-for-byte, so the two agreed on every value either had ever produced. Identical signature and identical append-only message layout adopted here.

  ⚠ **Held by a frozen cross-repo vector now, in both repos** (`tests/test_identity.py` and the SDK's `tests/test_identity_adapter.py` carry the same principal, tenant, key and two literal digests). Every other test of this function compares the implementation to itself — including the "issuer=None matches the pre-issuer form" check, whose two sides both come from one module — so a layout change applied to both repos on the same day would stay green in both while every `h1:` hash ever emitted became unreproducible. A literal computed before the change is the only assertion that reds for that. Do not change the layout a second time: the append-only shape is what makes `None` compatible, and a second divergence would have no compatible default to hide behind.

- **The trial ends on the Baton Proxy page, `https://baton.goodtiming.ai/setup/proxy`.** The console moves the paste and the upload box off Setup onto a page of their own, so 0.6.0's ending sends the reader to `/setup/agent`, a page that no longer takes the capture. `SETUP_URL`, the line `receipt` prints, the two endings and the send-rule answer in `try/CLAUDE.md`, and the `try/PROMPT.md` preamble now name the Baton Proxy page at its address. The paste below the rule is unchanged, so its pin is too.

### Documentation

- **`BATON_USER_ID_HMAC_KEY` now says how to generate one** (`openssl rand -hex 32`). The README said only "per-tenant secret" — no length, no randomness guidance — while the fixtures set it to `rig-test`, which is the value a copy-pasting operator keeps. The input space is emails and user ids: small and guessable. **A weak key is not a weaker pseudonym, it is none** — anyone holding the events dictionary-attacks the column back to raw identities.


## [0.6.0] — 2026-09-08

### Changed
- **The trial ends on Baton's Setup page.** `receipt` and `try/CLAUDE.md` now name the capture file and the page the person signs in to and uploads it on, `try/PROMPT.md` step 5 asks for what was captured and how to see it in Baton, and `try/SECURITY.md` says that nothing in the kit sends.
- **The last step reveals the capture in Finder on macOS.** The agent was given `open -R` to run before it hands the file over, and `receipt` gained one line printing the same command, because the upload box takes a drag and a path in a terminal cannot be dragged. Linux was left as it was: there is no portable reveal, and the commands that come closest open the capture in an editor.
- **`try/SECURITY.md` §5 and §6 state that `runtime_meta` bypasses the scrubber.** The client's request `_meta`, which for Claude Code is its tool-use id and progress token, was always recorded as it arrived and only the payload ever passed through `Scrubber`. The document described the redaction for the payload alone, so a reader could take it for the whole event.

### Removed
- **`kit.py upload`, `try/upload.py` and the emailed `upload.json` credential are gone.** Nothing in the checkout opens a network connection of its own, so the capture moves only when the person uploads it themselves in a browser.

## [0.5.5] — 2026-09-05

### Changed
- **`kit.py upload` is gated on the credential file rather than on who types it.** In 0.5.4 the agent was told never to run it and the person typed it themselves; it now refuses without `upload.json`, which we email and only the person can save, and the agent runs the command once they say it is in place.
- **The upload tail says where to sign in and what will arrive there.** It prints the `console_url` from the credential file with `/auth/email` appended, because the console's front page offers only Google sign-in, and it names the address the workspace was set up with.
- **The `delivered` row is now labelled `sent`.** A 201 says the console accepted the request, not that a row exists, and the old word said the second thing.
- **`receipt` offers upload first with the email path under it**, instead of two send paths of equal weight, and states the offer only when something was captured.
- **The note `setup` prints about how the trial ends no longer names a send path.** Which path applies is not known at setup time, so `receipt` states it when there is something to send.
- **`try/PROMPT.md`, `try/CLAUDE.md` and `try/SECURITY.md` were rewritten.** The security detail is opt-in and asked once by the paste, the paste checks out the latest release tag, and the trial is stated as one to run against a non-production server.
- **`try/SECURITY.md` §4 names `console_url`** as the destination of the POST, so a reviewer can read the host off the file they were sent.
- **`try/CLAUDE.md` tells the agent to open the capture with `less`** and to relay the `tool calls` and `tool definitions` rows apart. macOS has no handler for `.jsonl`, and an agent that joined the two rows reported calls to tools that were never called.

### Removed
- **The `secrets redacted` row and its Luhn note left `receipt`.** The count was card-shaped numbers rather than findings. `try/SECURITY.md` §6 still carries the measurement, for the reader it was written for.
- **The paragraph telling a person their file had not left the machine left `receipt`.**
- **"Delivered is not the same as stored" left the upload output and `try/upload.py`'s docstring.** The same fact is stated as "a 201 is not a row" where a reviewer looks for it.
- **The upload output no longer names an identity provider.** What reaches the person is a six-digit code either way, and the console names its own sign-in method.

### Fixed
- **`__version__` was 0.5.4 in a checkout tagged v0.5.5**, so captured events carried `sdk_version: baton-proxy/0.5.4` under that tag. `pyproject.toml` reads the version from that file, so the bump is the whole change.

## [0.5.4] — 2026-09-03

### Added
- **`BATON_PROACTIVE=on|off`, defaulting to `off`** — ported from baton-sdk's `VendorConfig.proactive_mode` (SPEC §13 2026-08-10b, §14 parity). It selects four legs together: the instructions head, the pre-call "BEFORE invoking any tool … you MUST call" paragraph, the annotation tool's description lead, and a handler that refuses an agent-filed pre-call annotation. It never removes the annotation tool — suppressing that also loses the reactive `feature_gap`, which is the product signal — and never touches the proxy's own synthesised proactive, so one turn-opener per session survives in both modes. Reactive clauses are byte-identical across modes. **The default ships `off`, matching the SDK**, on a live verification run: the pre-call paragraph does not ADD intent, it MOVES it — with the paragraph rendered the agent stated its expectation once in the annotation and omitted `expected_result` from all 5 subsequent calls, where a session without it carried 3/3, while `user_goal` held 8/8 in both on the schema advertisement alone. It also buys 277 of 1,236 chars against Claude Code's ~2,087-char `instructions` cap on a field the proxy APPENDS to, removes one approval prompt from inside the user's work window, and bought no friction — three real sessions with the paragraph rendered filed zero friction signals. **Consumer consequence, and it is the cost of this release:** agent-authored proactive annotations supply ~54% of turn boundaries on real traffic, and no time-gap threshold substitutes. Under the new default only the proxy's own synthesised proactive remains, so consumers SHOULD derive units from `call_workflow` transitions rather than from proactive annotations. That field carried on 8 of 8 calls in the verification run, so the replacement signal is flowing before the boundary it replaces was removed.
- **`overall_task`, a third injected param, riding every `tool_call_start` as `call_workflow`.** The proxy injected two params where baton-sdk injects three; the missing one is the task-label grouping key, so proxy-captured sessions emitted no label at all and a consumer grouping by task fell back to per-call intent text, which rewords freely and shatters one task into several. Mirrors the SDK exactly: the description is byte-identical to the SDK's (verified against source), the value is stripped before the start event is built so `params` stays the vendor-visible arguments, it rides EVERY start event rather than only the first — exact-string continuity across calls is the mechanism — and on the synthesised proactive it lands on `workflow`. `surface_hash` is unaffected: the snapshot records the vendor-true surface before injection. `seam_augmentations.intent_param.names` was already a plural list, so the third name extends it without a shape change.
- **`agent_runtime` now names the client, not the transport.** Every event carried `agent_runtime: "mcp-proxy"` unconditionally, so a console reading the field back learned that Baton was in the path and nothing about the app the person was working in. Two signals now outrank the constant, which stays as the fallback: the MCP handshake's `clientInfo.name`, latched on the emitter at `initialize`, and per-event `_meta` via `detect_agent_runtime`, whose rules match the SDK's so both sensors store the same token. The latch is the load-bearing one — `surface_snapshot` is sequence 1 and carries no `_meta`, and console consumers that ask a session what it ran in read its FIRST event. Per-event outranks the latch, which degrades correctly for a hosted adapter multiplexing clients. An unrecognised client passes through as sent rather than being mapped to a guess.

### Changed
- **`intent_param_mode` defaults to `required`** (was `optional`), safe precisely because of what the word means here: `required` appends `user_goal` to a tool's ADVERTISED required list and validates nothing, and the param is stripped before forwarding, so no call can fail for omitting it and the wrapped server never learns the difference. A wrapper that refused a customer's call to collect a telemetry string would be changing how the wrapped server behaves — the one thing it promises not to do. The number this exists to move is 89%: 1,465 of 1,644 real customer calls carried `user_goal` under `optional` on 0.5.2 (Aug 11–14). Asserted end to end: a call omitting `user_goal` entirely is driven through the real proxy subprocess on both transports and is served, with `call_intent: null` on the start event.
- **`BATON_INTENT_PARAM=off` is retired.** The injected params are the intent channel and they are stripped before forwarding, so a deployment that turns them off is a passthrough that records what happened with no record of why. The way to stop the injection is to stop wrapping the server. **The value is ignored with a warning rather than rejected**, because of who is likely to set it: `try/SECURITY.md` documented it as the way to disable injection, so raising would stop an MCP server from starting because its operator did exactly what our own security page told them to. It coerces to the current default (`required`); a value that was never valid still raises. `seam_augmentations.intent_param.mode` therefore never reports `off` from the proxy. **This is a deliberate producer divergence** — baton-sdk and baton-ts keep `off`, because their operator owns the server being instrumented.
- **`BATON_VENDOR_ID` is no longer required at startup; it defaults to the `local` placeholder, and the hard failure moves to the remote sink.** `Config.from_env()` raised without it, which meant the zero-config install did not degrade to an unlabelled event stream — it did not start. The proxy IS the MCP server from the client's perspective, so the wrapped server vanished from the client entirely, and the published quick start (wrap the command, set nothing) was the exact shape that triggered it, on five documentation surfaces. Nothing reading the label locally needs it to be true; it exists so an operator can grep their own JSONL, and `local` is honest there, matching `BATON_TENANT_ID` and `BATON_CONSENT_TOKEN` which have defaulted that way all along. The Console is the case that actually costs something — it buckets friction BY vendor, so a stream labelled `local` files rows under a vendor nobody owns — so `Emitter.start()` now refuses an http/https/s3 sink while the placeholder is in place, mirroring the existing consent guard exactly. **Not breaking for any install that already sets the variable**, which is every real path: `scan` sets it from the config entry name, and the Console's onboarding recipes and local-setup page all emit it.
- **The annotation tool's agent-facing params are renamed to match the injected ones**: `intent`/`expected_outcome`/`workflow` become `user_goal`/`expected_result`/`overall_task`. The same things were named differently depending on which surface the agent happened to be looking at, and the two task-label descriptions asked for opposite things while feeding one slot. **Agent-facing only — the wire keys are unchanged** (`intent`, `expected_outcome`, `workflow`), so stored events, the Console and the kit's receipt are untouched. `overall_task` gains the wording a scored experiment selected, including the repeat-verbatim clause the annotation field never carried; task grouping is exact-string, so that clause is the mechanism rather than advice.

### Fixed
- **The injected `user_goal` described itself as `OPTIONAL.` while the schema advertised it as required.** `intent_param_mode="required"` appends `user_goal` to each tool's advertised `required` list; the description shipping inside that same schema still opened `OPTIONAL.`, so the model was handed both claims and neither was reliably the one it read. Since `required` is now the default (0.5.3 shipped `optional`), this would have gone out as the default behaviour — on the one lever the mode has for moving the 89% fill rate it exists to move. The leading label now tracks the mode; the sentence after it is byte-identical across modes, because that text is measured and the mode is not a licence to reword it. `expected_result` and `overall_task` are never escalated in either mode, so their `OPTIONAL.` was true and is unchanged. No wire change: the description is advertisement only, nothing validates the param, and it is still stripped before forwarding.
- **A client disconnect never reached the upstream, so shutdown never ran.** `_pump_client_to_server` returned on stdin EOF without closing the UPSTREAM's stdin, so a healthy server sat on a pipe nobody would write to again and `child.wait()` never returned. Everything in `run_proxy`'s shutdown hangs off that wait — `drain_pending`, which gives every in-flight `*_start` a matching end, and `emitter.stop()`, which flushes the queue — so the client's SIGTERM landed seconds later with the last events unwritten. Silent no-emit at the one moment nobody is watching. The original code assumed shutdown always begins upstream-side; the commoner direction by far is the client quitting. Measured: the test suite runs in 13s with this and 192s without.
- **The upstream that ignores its own stdin EOF re-created the same hang one hop down.** `child.wait()` is now bounded, but only from the moment the CLIENT is gone — a grace clock running from startup would SIGTERM a healthy upstream seconds into a session meant to last hours. The grace is 1s and small on purpose: everything after the wait must finish before the client's own SIGTERM arrives. Escalation is TERM then KILL, armed by the client pump on its way out, so a live session arms no clock at all and the main thread does one blocking wait rather than polling. The earlier polling shape cost ~20 wakeups/sec per wrapped server, continuously.
- **The escalation signalled the direct child only, and reported our own SIGTERM as the upstream's verdict.** Wrapped servers are routinely launched through something that is not the server (`sh -c`, `npx`, `docker run`); a wrapper that does not forward SIGTERM dies while the real server keeps running and holds the stdout pipe open, leaking a process per session — measured at 3.24s to shut down against 1.22s for a direct child. The child now gets its own session and the whole group is signalled. `result` masks the signal we sent, and only that: a negative rc we did not cause, and every non-zero exit the upstream reached on its own, still travel. Previously a signalled child reported -15 and the shell reported 241, so for any server that ignores stdin EOF the client logged a crash on every clean exit. **Trade to know about:** `start_new_session=True` means the upstream no longer shares our process group, so a Ctrl-C in an interactive terminal reaches the proxy and not the server. MCP clients do not signal the group — they close stdin, then SIGTERM our pid — so the path that matters is unaffected.
- **Signalling the upstream's process group could signal our own.** `os.killpg` was called on `os.getpgid(child.pid)` with nothing checking whose group that is. `start_new_session=True` is what makes the child's group its own, so the code read as safe by construction — but the two live in different places and only one is load-bearing at the moment of the signal. Any state where the child ends up in our group turns shutdown into a SIGKILL of the proxy, the MCP client that launched it, and whatever else shares the terminal. The group is now signalled only once it is confirmed to be a different group, and declining the group still signals the direct child.
- **The shutdown close only ran on the ordinary way out of the pump loop.** It sat after `for line in sys.stdin:`, so it ran on EOF and on nothing else. Two other exits are reachable from the client side: `sys.stdin` decodes strict UTF-8, so a single non-UTF-8 byte raises `UnicodeDecodeError` out of the `for` itself, and `json.loads` on deeply nested input raises `RecursionError`, which `except json.JSONDecodeError` does not catch. Either kills the pump with the upstream's stdin still open, reaching the same unflushed-events shutdown through a different door — and an abrupt exit is when it matters most, because that is when there is something in the queue. A `finally` around the loop covers every exit.
- **Under `proactive_mode="off"` the agent's first instruction was to call `baton_annotate` "again"** with nothing to refer back to, while the tool description one field over said "Do NOT call it before a tool call". The word is now a token rendered only alongside the BEFORE paragraph it points at.
- **The `BATON_INTENT_PARAM='off'` coercion warning was emitted before logging was configured.** `_bootstrap` calls `Config.from_env()` and only then `_configure_logging`, so the warning went out through `logging.lastResort` — stderr only, unformatted, never teed to `BATON_PROXY_LOG_FILE`. Coercing instead of raising is defensible only because the operator is told, and an operator who checks the configured log file was told nothing. `from_env` now collects into `Config.startup_warnings` and the bootstrap drains it after logging is up.
- **Two ways past `proactive_mode="off"`.** `signal_type: ""` passed a gate keyed on `is None` and was enqueued AND confirmed as recorded proactive intent — the exact annotation the mode exists to refuse, filed anyway. And the refusal text ended "— and set signal_type", which reads as a repair instruction: an agent whose narration was refused satisfies it by re-sending with `other`. Nothing validates the field, so one invented word becomes a friction count someone acts on. The gate now takes the advertised enum in BOTH modes, and the refusal names the enum without proposing a substitute.
- **The `FileSink` hardening raised the one exception its handler could not catch.** `os.O_CLOEXEC` and `os.fchmod` are Unix-only; on Windows both are `AttributeError` and the handler beside them caught `OSError`, so a sink that promises availability-first raised out of `FileSink.__init__`, through an unguarded `emitter.start()`, and killed the wrapped MCP server at bootstrap. Windows now ends up unhardened and warned, which is the posture the docstring already states, instead of dead.

### Security
- **The events file was world-readable.** `state.json` was 0600 by a deliberate hardening because it holds a copy of an env block; `events.jsonl` was left at the default umask — and that is the file holding every tool call's full arguments and full results, the business data `SECURITY.md` §6 says plainly is NOT scrubbed. On a shared box the credential was protected and the corpus was published. Fixed in `FileSink` rather than in the trial kit, so every file-sink user gets it. `os.open` sets the mode only when it CREATES, so the chmod after it is not redundant: a file an earlier version left at 0644 stays there without it, and appending is exactly when it starts holding payloads.
- **`FileSink` sets 0600 with `fchmod` on the descriptor rather than `chmod` on the path.** The default sink is a fixed name in a world-writable directory, so on the shared box this hardening is FOR, the path can be a symlink another account planted between the open and the chmod; the descriptor names the file we actually opened. `O_CLOEXEC` stops the handle riding into the wrapped server. A failure to harden logs rather than raises, since `emitter.start()` is unguarded and raising would kill the proxy at bootstrap.
- **`scan`'s remote refusal printed the entry's URL verbatim, and for a large share of remote MCP servers that URL IS the credential.** Zapier and Composio put the token in the path, `?key=` is just as common, and userinfo is the third vector. The message goes to a terminal and is the kind of thing someone pastes into a support thread. It is now reduced to scheme and host, so the host still identifies which entry was refused and the secret does not travel.

### Notes
- No wire-format break. `call_workflow` is additive and already part of the shared `baton-spec` schema; `agent_runtime` changes values, not shape. The two default flips (`intent_param_mode` → `required`, `proactive_mode` → `off`) change captured behaviour on the next restart of an existing wrap — see the `BATON_PROACTIVE` entry for the turn-boundary consequence.


## [0.5.3] — 2026-08-13

### Added
- **`call_expected` on every `tool_call_start`.** `expected_result` is injected into every tool's schema and stripped from every call, but the value only ever reached a consumer through the session's first synthesised proactive annotation — making a per-call param into a per-session fact, and attaching whatever expectation the session happened to *open* with. Sessions commonly open with a docs or list read that states no expectation, so the calls doing the real work contributed nothing and inherited nothing. `enqueue_tool_call_start` now takes `call_expected` and writes it as a sibling of `params`, matching `call_intent`. The key is **omitted** when the param was not filled — "stated no expectation" and "stated an empty one" are different claims. The once-per-session annotation gate is unchanged; it governs annotations, not values.

### Changed
- **`intent_source` keys on either injected param, not on `user_goal` alone.** An agent may fill one without the other, and an expectation arriving with no goal is still injected-param capture. Matches baton-sdk, which keys on any injected param.

### Notes
- Additive on the wire; `call_expected` is already part of the shared `baton-spec` schema. The vendored submodule pin advanced to the schema revision carrying it — the conformance test failed loudly on the new key beforehand, which is the intended behaviour of `additionalProperties: false`.

## [0.5.2] — 2026-08-11

### Changed
- **Goal-param injection renamed to `user_goal` / `expected_result`.** The proxy previously injected a single namespaced `baton_intent` param; it now injects the same two vendor-neutral params as baton-sdk (SPEC §13), with per-param dispositions tracked independently in the registry and the shared `seam_augmentations.intent_param` shape (plural `names: list[str]`). `expected_result` additionally feeds the synthesised proactive annotation's `expected_outcome`, previously only reachable via a real annotate call. Straight rename, not additive — no consumers depended on the old param name; the wire fields (`call_intent`, `intent_source`) are unchanged.

### Added
- Cross-repo envelope conformance test against the shared `baton-spec` schema (vendored as a git submodule; dev/test only, no runtime effect).

## [0.5.1] — 2026-08-05

### Added
- **`session_id` / `principal` on `tool_call_end` and `tool_call_error`.** `Emitter.enqueue_tool_call_end` / `enqueue_tool_call_error` accept optional `session_id` and `principal` kwargs, so downstream sensors that pair results out-of-process (e.g. a gateway seam) can stamp the session and end-user identity on response-side events. Additive; omitted kwargs produce byte-identical output to 0.5.0.

## [0.5.0] — 2026-07-27

### Added
- **End-user identity capture (`user_id`).** New `baton_proxy.identity` module: `hash_user_id()` (HMAC-SHA256, keyed per tenant with the `tenant_id` folded into the message so the same principal never collides across tenants; `h1:` scheme prefix as the key-rotation seam) plus a `Principal` / `IdentityResolver` seam so each capture modality resolves a raw principal that the core hashes. Hashing happens **at the edge** in `Emitter._enqueue` — the raw principal never survives the method, so a console-bound sink only ever sees the hash.
- **`user_id` on the event envelope** — additive and nullable: emitted only when a principal resolves and an HMAC key is set, so output is byte-identical to 0.4.x when unused.
- **`BATON_USER_ID_HMAC_KEY`** config (per-tenant secret). Unset → fail-open: `user_id` is dropped, events still emit (it is additive analytics, never a consent/authz gate); logged once.

### Changed
- Scrubber `REDACT_FIELD_NAMES` now includes `user_name` (defence-in-depth for the identity PII half). `name` is deliberately excluded — it collides with legitimate payload keys (prompt names, tool names in surface snapshots).

## [0.4.1] — 2026-07-21

### Changed
- **HTTP bridge graceful degradation**: when the upstream is unreachable (connection failure, non-2xx, timeout, or an accepted-but-empty reply), the `--url` bridge now degrades the two handshake methods — `initialize` and `tools/list` — to a synthetic healthy response instead of a JSON-RPC error. Erroring `initialize` put some clients (notably Claude Cowork) into a permanent failed-connection state, wedging the entire session including the proxy's own injected tools; degrading it lets the client attach and keeps `baton_annotate` usable. `tools/list` returns just the injected baton tools (no phantom vendor tools). Every other method still degrades per-call (a JSON-RPC error for that id, which clients tolerate), so real tool calls against a dead upstream still surface as errors rather than a wedge. Fail-open throughout.

## [0.4.0] — 2026-07-21

### Added
- **S3 event sink** (`pip install baton-proxy[s3]`): `s3://bucket/prefix` is now a valid `BATON_EVENT_SINK` scheme, usable on its own or in the comma-separated `MultiSink` fan-out. `boto3` is lazy-imported and gated behind the `[s3]` extra, so the base package stays zero-dependency. The placeholder-consent guard treats `s3://` as a remote sink (refuses to ship under `BATON_CONSENT_TOKEN='local'`).

### Changed
- Emitter `enqueue_*` methods accept an optional per-call `session_id`, so a single processor serving many sessions stamps each event with the session read from that request rather than one process-wide id. Omitting it preserves the previous one-session-per-process behavior — backward compatible for the stdio and `--url` transports. (Enables out-of-tree processors, e.g. `baton-extmcp`, to reuse the emitter.)

## [0.3.1] — 2026-07-11

### Added
- **Surface snapshot capture** (`surface_snapshot` event): on each session's first complete `tools/list`, the proxy emits one snapshot of the upstream server's surface — serverInfo, capabilities, instructions, and the full tool list (names, descriptions, input schemas, annotations) — hashed over the **vendor-true** (pre-injection) surface, with a `seam_augmentations` block recording what the proxy adds (injected tools, the intent param, the instructions suffix). Emission is suppressed when the hash matches the last-emitted hash for the session, so a stable surface costs one event per session at most. Pagination-safe: partial `tools/list` pages are never snapshot. Consumers can materialize surface history from these events (a new hash = the surface changed) and pin proposed changes to the exact surface version they were authored against.

## [0.3.0] — 2026-07-07

### Added
- **Per-tool intent param injection** (`baton_intent`): the proxy adds an optional string parameter to every upstream tool's schema at `tools/list`, strips it at `tools/call` before forwarding, and captures the value as user intent. The parameter description reaches the model at call-compose time on every client — including clients that drop `InitializeResult.instructions` entirely (observed on Claude Desktop) — so intent capture no longer depends on instructions compliance. The session's first param intent also emits a proactive annotation (sequenced before its `tool_call_start`, suppressed once a real `baton_annotate` proactive has fired); every call's intent rides `tool_call_start.payload.call_intent` with `intent_source` provenance. Tools that already define a `baton_intent` parameter are left untouched (never stripped, never read). Modes via `BATON_INTENT_PARAM`: `optional` (default) | `required` | `off`. Works on both transports (stdio subprocess and `--url` HTTP bridge); fail-open throughout — an injection or strip error forwards the message unmodified.

## [0.2.2] — 2026-07-04

### Added
- **HTTPS bridge** (`baton-proxy --url <url>`): wrap a remote Streamable HTTP MCP server (spec 2025-03-26), not just a local stdio subprocess. The proxy stays stdio-facing to the client and forwards each message as an HTTP POST, streaming the JSON or SSE response back. Bearer auth via `BATON_UPSTREAM_AUTH_TOKEN`; read timeout via `BATON_UPSTREAM_TIMEOUT` (default 60s); captures and echoes `Mcp-Session-Id` and pins `MCP-Protocol-Version` after the handshake; sends a named `User-Agent` + `Via` header (urllib's default UA is Cloudflare-banned on many hosted endpoints). Stdlib only — no new dependencies.
- **Resource & prompt capture (A1)**: the proxy now emits lifecycle events for `resources/read`, `resources/list`, `prompts/get`, and `prompts/list`, alongside the existing `tools/call` capture.

### Fixed
- HTTP bridge fail-open: a 2xx upstream reply that answers nothing (empty body, `202`, or an SSE stream with no matching frame) no longer leaves the client blocked on that request; a malformed or non-object client message no longer crashes the bridge. Both now emit a synthetic error event and hand the client a JSON-RPC error rather than hanging.
- SSE responses: only `event: message` frames are parsed as JSON-RPC — a server interleaving a keepalive/ping/custom event with a data payload no longer injects a bogus message to the client.
- stdio: the two pump threads are serialized on stdout, so a synthesized `baton_annotate` response can no longer interleave with a real upstream response and corrupt the wire.
- `BATON_UPSTREAM_TIMEOUT=inf`/`nan` falls back to the default instead of silently disabling the read timeout.
- Calls still in flight at shutdown are resolved with a synthetic error, so a mid-call upstream exit no longer leaves a dangling `*_start` with no end/error.

### Packaging
- `pyproject.toml` references the `LICENSE` file instead of inline SPDX text.

## [0.2.1] — 2026-06-23

### Changed
- `baton-proxy scan` now drives the agent to record each friction through `baton_annotate` (intent + `signal_type` + `suggested_improvement`) the moment it hits it, instead of only summarizing at the end. Scan reports are synthesized from captured annotation events, so mechanical-only error findings that used to render thin (generic intent, no fix) now carry the agent's restated intent and a concrete suggested fix. (A live `scan --config github` went from 1 thin finding to 7 with verbatim fixes.)

### Fixed
- `synthesize_scan` now folds a model-filed reactive into the mechanical error finding for the same tool even when the reactive names that tool only in its text (not a structured `tool` field), matching only when exactly one errored tool name appears. Previously a tool that both errored and got annotated could surface as two near-duplicate findings, inflating the headline friction count.

### Removed
- Pinned per-server task plans (`scan_tasks.py`). They existed only to make the cold-visitor homepage-demo finding reproducible; the config-only scan flow retired that demo path, leaving no consumer. Every scan now uses the adversarial generic driver plan.

## [0.2.0] — 2026-06-23

### Added
- `baton-proxy scan --config <name>`: one-command preflight friction report. Resolves an MCP server you've already configured in Claude (from `./.mcp.json` or `~/.claude.json`, reusing its saved credentials), wraps it, drives a headless `claude` agent through it, and renders a local `baton-report.md` from captured events — no permanent install or Claude-config change. The report anchors on mechanical tool errors plus model-flagged friction signals, and is labeled preflight/inferred. Warns when `ANTHROPIC_API_KEY` is set (it bills the API account over a Claude login session).

## [0.1.2] — 2026-06-12

### Fixed
- `sdk_version` field in emitted events was hardcoded to `"baton-proxy/0.0.1"` and never picked up version bumps. Now derived from `baton_proxy.__version__` at module load. Caught while dogfooding the 0.1.1 install — events from a `pipx`-installed proxy were reporting the stale version.

## [0.1.1] — 2026-06-12

Docs-only release to refresh the PyPI project description. No code changes.

### Changed
- README diagram now shows the full sink fan-out (`stderr:` / `file://` / Baton Console) instead of just the Console.
- Intro broadens "emits to a Baton Console" to "emits to one or more sinks", matching what `BATON_EVENT_SINK` actually accepts.
- New "Related" section links [`baton-sdk`](https://github.com/good-timing/baton) (the in-process integration alternative) and the [Baton wire-protocol spec](https://github.com/good-timing/baton/blob/main/docs/SPEC.md).
- Quick-start install line gains a one-line rationale for `pipx` vs `pip`.

## [0.1.0] — 2026-06-12

Initial public release on PyPI.

### Added
- Subprocess-wrap MCP proxy: wraps a stdio MCP server, intercepts the handshake, injects friction-capture tools into the upstream server's `tools/list`.
- `baton_annotate` tool: lets Claude emit a per-call annotation event when it hits friction (unprompted).
- `baton_session_report` tool: returns a vendor-shareable markdown report of the session's friction (errors, slow calls, annotations). Local-sink installs only.
- Friction event emission per real tool call (`tool_call_start` / `tool_call_end` / `tool_call_error`) carrying session id, monotonic sequence, and the upstream MCP request's `_meta` block.
- Multi-sink fan-out via `BATON_EVENT_SINK`: `stderr:`, `file://`, and `http(s)://` schemes, comma-separated. Zero-config default writes to `stderr:` + `file:///tmp/baton-proxy.jsonl`.
- Consent guard: refuses to start when an `http(s)://` sink is paired with the placeholder `BATON_CONSENT_TOKEN=local`, or when an `http(s)://` sink is configured without `BATON_API_KEY`.
- Fail-open delivery: emission runs on a background thread; Console outage never blocks the MCP pipe.

[Unreleased]: https://github.com/good-timing/baton-proxy/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/good-timing/baton-proxy/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/good-timing/baton-proxy/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/good-timing/baton-proxy/releases/tag/v0.1.0

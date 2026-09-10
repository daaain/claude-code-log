# Peer agent messages render as "User (steering)" (#309)

**Status:** specified, not implemented. Branch `dev/agent-message-origin`,
based on `dev/agent-spawn-linking` (PR #316).

## The defect

A message sent by a peer agent to the session it reports into is rendered
as a **"User (steering)"** card — i.e. attributed to the human — with the
raw wrapper markup visible in the card body:

```
User (steering)                                    08/15/2026 09:04:06
<agent-message from="dep-scan-03">
DEBUG_HTML — 11 matches. All paths relative to …
```

That screenshot *is* issue #309. Two distinct defects in one card:

1. **Misattribution.** A peer agent's message is shown as the user
   steering the session. The sender's identity is present in the data and
   discarded.
2. **Leaked markup.** The `<agent-message from="…">` wrapper is rendered
   literally instead of being parsed into card structure — exactly what
   `<teammate-message>` already avoids.

## Why this sits on top of PR #316, not on `main`

The two changes share no code (see "Files" below) and could land in either
order. The reason to stack is the **payoff**, not a compile dependency:

`origin.senderTaskId` is an agentId in the *same namespace* that #316
links, and it resolves to the sender's subagent transcript **29 times out
of 29** on the reference archive. So the natural finished form of this
feature — an agent-message card that links through to the sender's
transcript — only renders as a live link once #316 makes those
transcripts present. Built on `main` alone, the link target does not
exist and the feature stops at re-labelling the card.

Build the parsing and attribution so they do **not** require #316; build
the cross-link so it degrades to plain text when the target is absent.

## The data

The carrier is **not** a message. It is `type: "attachment"` with
`attachment.type: "queued_command"`:

```json
{"type": "queued_command",
 "prompt": "<agent-message from=\"dep-scan-03\">\nDEBUG_HTML — 11 matches…",
 "commandMode": "prompt",
 "isMeta": true,
 "origin": {"kind": "peer",
            "from": "dep-scan-03",
            "senderTaskId": "adep-scan-03-fbd685bdcccfa4ce",
            "name": "dep-scan-03",
            "body": "DEBUG_HTML — 11 matches…"}}
```

Everything needed is already parsed in `origin`:

| field | use |
|---|---|
| `kind` | discriminator — `"peer"` vs `"human"` |
| `from` / `name` | sender attribution for the card header |
| `senderTaskId` | agentId of the sender's `subagents/agent-<id>.jsonl` |
| `body` | the message **without** the wrapper — no regex needed |

`prompt` carries the wrapper-wrapped form; `origin.body` carries the clean
body. Prefer `origin.body`, fall back to stripping `prompt`.

## Census (structural, whole reference archive)

`origin.kind` on `queued_command` attachments:

| kind | count | sessions |
|---|---|---|
| `human` | 2554 | 83 |
| *(no `origin` key)* | 2431 | 130 |
| `peer` | **29** | **3** |

`peer` `senderTaskId` → subagent file: **29 resolved, 0 unresolved.**

> **Measure this structurally, never with a raw-text grep.** The `origin`
> object is mirrored into a companion `user` entry, so `rg '"kind":"peer"'`
> double-counts (58 raw vs 29 real). Parse the line, check
> `type == "attachment"` and `attachment.type == "queued_command"`, then
> read `attachment.origin.kind`.

### `channel` is out of scope

A third kind, `channel` (6 occurrences, `{"kind":"channel","server":
"plugin:…"}`), appears **only inside `user` entries and never on a
`queued_command` attachment**. It is a different delivery path and needs
its own investigation. Do not fold it into this branch.

## Current behaviour

`claude_code_log/factories/attachment_factory.py` **never reads `origin`** —
zero references to the key. `_create_queued_command_message` promotes every
`queued_command` to the same steering message regardless of who sent it,
so all three kinds collapse into one card type.

## Files

Nothing here overlaps PR #316, which touches
`converter.py:_link_subagents_by_prompt_hash` /
`_collect_unresolved_task_results` (roughly lines 636–810).

- `factories/attachment_factory.py` — `_create_queued_command_message`
  (~L177) and the dispatch at ~L277. The branch point.
- `models.py` — a message model for a peer message, if the existing
  steering model cannot carry sender fields.
- `html/…_formatter.py` + the markdown renderer — **both** output formats
  need the new card; a change that only lands in HTML is half done.
- `renderer.py` — CSS class generation, and the timeline's message-type
  detection in `templates/components/timeline.html` must stay in step
  (see the note in `CLAUDE.md`).

## Prior art to follow

`<teammate-message>` is the same feature one layer over: see
`factories/teammate_factory.py` (`has_teammate_message`,
`iter_teammate_blocks`, `find_team_lead_body`) and
`html/teammate_formatter.py`. Match its card vocabulary so a peer message
and a teammate message read as siblings rather than as two inventions.

**But do not copy its parsing.** `teammate_factory` regexes a wrapper out
of free text because that is all it has. Here `origin` is already
structured — reaching for the regex would be re-deriving what the harness
handed you.

## Risks

`work/steering-queued-command.md` is the prior design note for this path
and documents that it is delicate: plugin transformers rewriting
`UserTextMessage` subclasses into fields with `items=[]`, an empty-card
bug, and the `queued_command` ↔ queue-operation `remove` pairing. Read it
before editing. A change that looks like a small `if` on `origin.kind` can
interact with all three.

Watch specifically:

- **Do not regress `human`.** It is 2554 of 2583 origin-carrying cases;
  `peer` is 29. The common path must be untouched.
- **`origin` absent** (2431 cases, older transcripts) must keep today's
  behaviour exactly.
- **Cache.** If the change alters parsed entries, it needs a
  `breaking_changes` entry in `cache.py` the same way #316 did — a stale
  cache otherwise serves the old cards. Verify by writing a cache under
  the old code and reading it back under the new. Note single-file
  conversion never builds a `CacheManager` (`if use_cache and
  input_path.is_dir()`), so a single-file render proves nothing about
  cached behaviour.

## Tests

- Synthetic fixtures in `test/test_data/`, built by hand as in
  `test_teammates_parsing.py` — a `queued_command` attachment per
  `origin.kind`: `peer`, `human`, and one with `origin` absent.
- Assert `peer` produces a sender-attributed card, that no literal
  `<agent-message` survives into either output format, and that `human`
  and origin-absent cases are byte-identical to before.
- **Mutation-check every new guard**: neutralise it and confirm the test
  goes red. A test that passes with the discriminator disabled is pinning
  something else.
- Snapshot updates run serially: `pytest … -n0 --snapshot-update`.

## Reference data

Issue #309 carries the screenshot. The reference session is
a local session with 8 `peer` attachments in its trunk under a local
`~/.claude/projects/<project>/` tree; the entry is the entry shown in the
issue's screenshot. That session has **8** `peer` attachments in its trunk,
rendering today as 8 "User (steering)" cards, and its
`subagents/` directory holds the ten `dep-scan-*` transcripts the messages
come from — which makes it the natural end-to-end check for the
cross-link once #316 has merged.

Verify a candidate fix against real data as well as fixtures: re-render
that session and confirm zero literal `<agent-message` occurrences remain
and the sender names appear as attribution.

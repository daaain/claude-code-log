# Identifier Consolidation — `data-message-id` / `id` / `data-uuid`

Design draft. Started as a follow-up discussion on PR #273 (in-page search),
which added a third per-message identifier attribute (`data-uuid`) on top of the
two the transcript already emits. Companion to
[`archive-search-server.md`](archive-search-server.md) §"The anchor problem".

## The three identifiers today

Each rendered `.message` card carries, pre-consolidation:

| Attribute | Value | Nature | Consumers |
|-----------|-------|--------|-----------|
| `id` | `msg-d-{index}` | positional render-slot, **unique** | native `#msg-` URL nav + the `unfoldAncestorsOf` hashchange handler; ~10 anchor generators (session TOC, task/cron/monitor back-links, fork links) |
| `data-message-id` | `d-{index}` | **same value** minus the `msg-` prefix | one: the fold machinery's `getMessageEl` querySelector |
| `data-uuid` | transcript UUID | **stable** across re-renders, **not unique** per card (text+tool_use chunks of one entry share it) | none at runtime (search matches by element identity); added in #273 for the archive-search anchor problem (annotations / cross-session deep-links) |

`message_id` = `d-{message_index}` is a **positional** slot id: it renumbers on
session growth, rewind-and-replay (same uuids, new order), and per `--detail`
variant. That positional fragility is exactly the "anchor problem" the
archive-search draft describes.

## C1 — drop `data-message-id` (LANDED on this branch)

`data-message-id` is a pure duplicate of `id` (same value minus `msg-`). Its
sole runtime consumer, the fold machinery's `getMessageEl`, now resolves the
card via `getElementById('msg-' + id)` (behaviour-identical — `id` is unique per
card). Dropped from both `.message` cards and their `.fold-bar` children (no JS
read the fold-bar copy); the three tests that keyed on it were migrated to `id`.

**3 attributes → 2.** Green (browser + unit + snapshot), independently landable,
zero neutrality/behaviour impact. This is the clear win regardless of C2.

## C2 — content-derived id `m-{uuid}-{k}` (PROTOTYPE, not on this branch)

Idea: fold the uuid *into* the id so a **single** identifier carries stable
identity, letting `data-uuid` be dropped too (2 → 1). The id body becomes
`m-{uuid}-{k}` where **k = the chunk index within the entry** (from
`chunk_message_content`). Because chunking is a pure function of the entry's own
immutable content list, `k` is content-derived and **stable across session
growth / rewind-replay / `--detail`** — the property `msg-d-N` lacks, and the
same stability the archive-search draft wants (it would supersede that draft's
"Option 1: separate `data-uuid`" by making the uuid parseable straight from the
`id`, e.g. `id.slice(...)`).

Fallbacks for cards without a `(uuid, k)`: session/branch headers →
`session-{render_session_id}`; synthetic cards carrying a uuid but no chunk
(ghost placeholders, attachments) → `m-{uuid}-s`; anything else → the positional
`d-{index}`.

**Prototype implementation** (commit `f57deeb` on `review/PR-273-monk`, *not*
merged here): a `TemplateMessage.stable_id` property + a single **post-render
resolution pass** (`_apply_stable_ids`) that builds an `index → stable_id` map
from the render context and rewrites the card `id=`, the `#…` anchor `href=`s,
and the fold `data-target=`s at the one point where ctx and the full-page HTML
coexist. It keeps the `msg-` prefix (so the JS hash handler / `getElementById`
are untouched) and drops `data-uuid`.

### Uniqueness gate — PASSED

The one assumption that could sink the scheme is `(uuid, k)` uniqueness — i.e.
no uuid ever renders two cards at the same chunk index (a uuid rendered at two
DOM slots would collide). Probed by running the full pipeline (dedup / fork /
ghost applied) and checking every uuid-bearing card:

- canonical fixtures: `dag_fork`, `dag_within_fork`, `dag_resume`, `dag_cycle`,
  `dag_simple`, `dedup_main`, `dedup_agent`, `sidechain{,_main,_agent}` —
  **0 duplicate `(uuid, k)`, 0 ghost+real-k0 collisions**;
- `real_projects` corpus (101 session files incl. nested subagents):
  **4357 uuid-bearing cards, 0 duplicate `(uuid, k)`, 0 collisions**.

175 synthetic cards (ghost/attachment) carry a uuid but no chunk index and take
the `m-{uuid}-s` fallback; verified none collide with a real `k=0` card of the
same uuid.

## The real cost (why the prototype's small diff is misleading)

> The stable-anchor benefit has no current consumer, and C2's small source diff
> (~93 lines) hides the real cost: the generators lack ctx, so a production impl
> needs per-generator resolver plumbing (~8 formatters / 4 modules), not my
> post-render regex shortcut; plus 9 test files recoupled.

Unpacking that:

- **Generators lack `ctx`.** The ~10 sites that emit `#msg-d-{index}` anchors
  (in `renderer.py`, `system_formatters.py`, `tool_formatters.py`,
  `async_formatter.py`, `session_nav.html`) format from a stored *integer*
  index and have **no `RenderingContext`** to resolve that index → the target
  card's `stable_id`. The prototype sidesteps this with a single regex pass over
  the assembled HTML — small, but a code smell (rewriting generated HTML). A
  production implementation instead threads a resolver `index → stable_id`
  through those formatters, *or* resolves the anchor where ctx exists (the
  link-wiring passes) and stores the resolved string on the models. Either way
  it touches ~8 formatter functions across 4 modules — a much larger, more
  invasive diff than the prototype's 93 lines.
- **Test recoupling.** The positional `d-N` format is baked into **9 test
  files** (regex id/anchor matchers). Two of them
  (`test_ghost_repair`'s dead-anchor guard, `test_template_rendering`'s
  suppressed-card guard) went **vacuously green** after the format change —
  their `msg-d-` regexes matched nothing, so the guards silently stopped testing
  anything (see the "id-migration → vacuous regex guards" lesson: grep the old
  token in tests and prove each selector still matches something).
- **Cross-page anchors.** The prototype's per-page pass handles same-page
  (combined) anchors correctly; index → `session-X.html#frag` links would need
  each target page's own `index → stable_id` map. (In practice the expandable
  nav uses file links without fragments, so this may be moot — verify before
  relying on it.)
- **Snapshot churn.** Every card id changes, so the full `.ambr` regenerates.

## Recommendation

1. **Land C1 now** (done on this branch) — a clean, low-risk 3 → 2.
2. **Gate C2 on the archive-search-server feature actually being built.** The
   stable-anchor benefit has *no runtime consumer today* — in-page search matches
   by element identity, not uuid. Until something reads the stable id, C2 is a
   large migration for a benefit nothing uses.
3. **If/when built, do C2 properly**, not via the prototype's post-render regex:
   per-generator resolver plumbing; drop the `msg-` prefix for the clean
   `m-{uuid}-{k}`; handle cross-page anchors; and re-pin the ~9 coupled tests
   (watch the vacuous-guard trap). Use `f57deeb` as the reference for *what* to
   build (scheme + gate), not as the diff to merge.

## Update: the consumer arrived, and did not need C2

Recommendation 2 gated C2 on "the archive-search-server feature actually
being built", on the grounds that the stable-anchor benefit had no runtime
consumer. A different consumer appeared first — the live-update **patch
protocol** in [`watch-mode.md`](watch-mode.md), which reconciles the DOM by
card id instead of replacing the transcript container. That doc's C20
argued C2 was its missing half. **Building the patch showed otherwise.**

- **Addressing.** C20 expected C2 to replace an O(page) key-map rebuild with
  one `getElementById`. It does not: the patch must fetch, parse and hash
  the whole page regardless, because the comparison hash cannot be taken
  from the live DOM (decoration has rewritten it — a prototype that tried
  skipped 0 of 1,181 nodes). The O(page) pass is structural; only a
  server-shipped delta removes it.
- **Insertions.** C2's advantage is real and was measured — on an
  out-of-order arrival, positional ids give 104 insertions + 103 deletions
  where `m-{uuid}-{k}` gives 1 and 0. But replaying three real sessions
  through the renderer, **45 of 47 growth steps were pure tail extensions**;
  the id sequence broke in exactly the other 2. So C2 buys the difference
  between a patch and a swap on **4% of ticks**, and the swap is correct,
  already implemented, and cheap.

Those two mid-tree arrivals also landed *near* the tail (index 306 of 311;
308/310/312 of 312) — interleavings, not deep insertions. If 4% ever
matters, widening the patch's extension test to tolerate a bounded
reordering window near the tail is one function, against a migration
through ~8 formatters, 4 modules and 9 coupled test files.

**C2 stays deferred.** The gate is unchanged in form but should now read:
take it when a consumer needs *stable anchors that outlive a render* —
archive-search annotations and cross-session deep-links, as originally
envisaged — not for patch addressing, which turned out not to need it.

## Status

- C1: implemented on `improved-search` (commit above this one).
- C2: prototype only, on `review/PR-273-monk` @ `f57deeb`; still deferred.
  Its first candidate consumer was built without it — see the update above.
  This doc is the record.

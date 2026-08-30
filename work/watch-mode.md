# Real-time watch mode — Design

Status: **designed, not implemented.** Decisions below are settled; the
measurements that forced them are recorded so a later reader can tell
which choices were reasoned and which were measured.

## Motivation

Two use cases, deliberately different in their tolerance for latency and
complexity:

1. **Markdown on disk.** Someone has `session-<id>.md` open in an IDE or
   Obsidian and wants it to keep up with the running session. The "client"
   is the editor's own file watcher; all we owe it is a fresh, non-torn
   file. Latency budget: a second or two, easily.
2. **Session page in the browser.** Someone has the generated HTML open
   under `serve` and wants the page to grow as the session does, "like the
   CLI". Latency budget: sub-second would be nice; loss of scroll position
   or fold state would not be.

Both reduce to the same engine (*detect change → re-render → notify*), and
differ only in how the client learns about it. That symmetry is the main
structural finding.

## Decisions

| # | decision |
|---|---|
| D1 | A `watch` subcommand owns the loop; `serve --watch` runs the same engine on a thread. |
| D2 | Add `source_size` to `cached_files` so the cache itself detects fast appends; the watcher then trusts `get_modified_files` rather than keeping parallel state. Detection is stat-polling. **Landed.** |
| D3 | Default scope is one project; `--all-projects` is opt-in. |
| D4 | Quiet-period debounce (~300 ms) with a max-latency cap (~2 s), both flags. |
| D5 | Container swap (option B) with uuid-set diffing, not full reload and not fragment patching. |
| D6 | Polling. SSE only if measurement later justifies it. |
| D7 | Route every output write through temp-file + `os.replace`. **Landed.** |
| D8 | Measured: the write + FTS update is fast enough. No lock-avoidance machinery needed. |
| D9 | ~~Fix `--output` destination-aware freshness~~ — already fixed; the doc that reported it was stale. **No work needed.** |
| D10 | Injectable clock and file-event source; unit tests drive ticks by hand. |
| D11 | The `stream-json` piping request (#43 follow-up) is **subsumed** by watch mode. No stream-json parser. |

---

## Constraints, measured

Measured on this box, not assumed. The live transcript is this session's
own JSONL; the timing corpus is a copy of the `claude-code-log` project
archive (64 MB, 32 session files, 8 cores, warm cache).

### C1. The source is append-only, and never rewritten in place

168 entries, **110 distinct UUIDs, zero duplicates** — no entry is ever
rewritten. A 4.5 KB append left the first 50 KB byte-identical.

**Consequence:** everything before the last known offset is stable. There
is nothing to diff — only a tail to consider.

### C2. The granularity floor is one complete message. Token streaming is impossible.

Follows from C1: an entry is written exactly once, so only when complete.
Confirmed by shape — the newest assistant entry was a whole 704-byte
`tool_use` block, on disk within the same second the message finished, not
batched to end-of-turn.

**Consequence, and the expectation to set with users:** we can never show
tokens arriving. The best achievable is *a whole message appearing
promptly*. "Smooth" must come from presentation (a fade-in on new cards),
not from streaming.

### C3. Appends are not in timestamp order

The last two lines of the live file were `attachment` entries stamped
`15:42:17.930` and `15:42:26.973`, appended *after* a `user` entry stamped
`15:42:26.971`.

**Consequence:** "append new lines to the bottom of the page" is wrong. A
newly-arrived line can belong earlier in the tree.

### C4. `file://` cannot fetch anything. `<script src>` is the only channel.

Probed in Chromium via Playwright — one page attempting each access,
loaded first from `file://`, then from a real `ArchiveServer` on loopback:

| | `file://` | `http://` (`ArchiveServer`) |
|---|---|---|
| `fetch('sibling.json')` | **BLOCKED** | OK |
| `fetch(location.href)` (self) | **BLOCKED** | OK |
| `XMLHttpRequest` HEAD | **BLOCKED** | OK |
| `<script src="sibling.js">` | OK | OK |
| `localStorage` | OK | OK |
| `history.scrollRestoration` | `auto` | `auto` |

**Consequence:** "polling works even without the server" is half right. A
`file://` page can poll only by injecting a `<script>` sidecar, and cannot
fetch page content — so its ceiling is a revision counter plus
`location.reload()`. Under `http://`, `fetch(location.href)` unlocks the
container swap.

### C5. Under `serve`, polling needs no new server code

`fetch(location.href)` already works against the stock
`SimpleHTTPRequestHandler`, which already does `Last-Modified`/304
(0.5–1.6 ms, per `archive-search-server.md`). A conditional GET on the
page's own URL is both the change-detection channel and the content
channel, in one request.

### C6. ~~⚠️~~ ✅ The session-scoped fast path was unreachable in watch mode

**This is the finding that most shapes the work.** Phase 1b
(`_try_current_or_session_scoped`, `converter.py:3027`) — the 0.6 s
session-scoped path from #315 — is vetoed outright when
`cache_was_updated` is true (`converter.py:3067-3073`). A watch tick
*always* has new bytes, so it is *always* vetoed.

Measured by spying on the conversion internals across simulated watch
ticks (append one line, clear the mtime tolerance, convert):

| mode | `try_session_scoped` | what actually ran | steady tick |
|---|---|---|---|
| `--combined yes` | `None` (vetoed) | Phase 1c streaming | **0.46 s** |
| `--combined no` | `None` (vetoed) | **full project load** (0.51 s of it) | **0.82 s** |

So today: streaming rescues the combined case, but `--combined no` — the
Obsidian mode, i.e. exactly use case 1 — **full-loads the whole project on
every tick**. `_incremental_cache_refresh` does fire (0.19 s) and is the
fixed floor of every tick.

**Consequence:** the enabling change for watch mode is to let Phase 1b run
when the cache *was* updated, restricted to the sessions the update
touched. With `--combined no` the combined-staleness veto is already
skipped, so `cache_was_updated` is genuinely the only blocker. This is a
converter change, not a watcher change, and it is the prerequisite that
makes watch mode cheap rather than merely possible.

#### ✅ Resolved

The veto's real concern was narrower than "the cache changed": Phase 1b's
staleness test is per-session **message counts**, so a session whose
content changed without its count changing would be missed. An
**incremental** refresh rules that out — `_incremental_cache_refresh`
only succeeds after proving every modified file's cached rows are an
exact *prefix* of its current rows (`converter.py:3866`), i.e. a pure
append, and with append-only sources a changed session always changes its
count. It also keeps the sidecar current via `merge_session_sidecar`,
which is the partial load's other requirement.

So `ensure_fresh_cache` now reports *how* it refreshed
(`CacheRefresh.NONE` / `INCREMENTAL` / `FULL`, via
`ensure_fresh_cache_detailed`; the old bool function remains as a
wrapper), and Phase 1b refuses only for `FULL`.

Measured on the same 64 MB / 32-session archive, a watch tick with
`--combined no`:

| | before | after |
|---|---|---|
| path taken | full project load | **`_load_stale_session_transcripts`** |
| tick (in-process, incl. hierarchy overhead) | 0.78 s | **0.31 s** |
| render alone | — | **0.09 s** |

Equivalence: two independent copies of the archive advanced through the
same appends, one with the session-scoped path and one with
`CLAUDE_CODE_LOG_SESSION_SCOPED=0`. **8 ticks, both formats, 28–29 output
files each, byte-identical every time.**

Pinned in `test/test_session_scoped_render.py::TestSessionScopedAfterAppend`
— including that a FULL refresh still declines, and that the appended
message actually reaches the output (an equivalence test alone would pass
if *both* paths rendered nothing, which is precisely how the first draft
of this measurement fooled itself).

### C7. ~~⚠️~~ ✅ The cache's 1-second mtime tolerance silently swallows fast appends

`cache.py:349` — `if abs(source_mtime - row["source_mtime"]) >= 1.0`. A
change landing within 1.0 s of the recorded mtime is **invisible**.
Reproduced: appending and converting immediately alternated 0.74 s (change
seen) / 0.29 s (change missed), run after run.

It fails in the worst shape: the *last* message of a turn, landing within
a second of the previous tick and then followed by silence, is stranded
until something else touches the file.

**Fixed** by migration 011 (D2). End-to-end re-measurement of the same
failure mode — append one line, convert immediately, no sleep — went
from alternating SEEN/MISSED to six appends seen out of six.

### C8. ✅ Every output write is non-atomic

Eight `write_text` sites in `converter.py` plus `render_pool.py:570`; only
image export uses temp-file + `os.replace` (`image_export.py:79`). Today a
narrow race; under a watch loop rewriting the same file every few seconds
while Obsidian re-reads it, routine. The 27 MB-page case is a wide window.

Quantified against a concurrent reader over a 4 MB payload: 40 plain
rewrites produced **142 torn reads**, including a fully-truncated 0-byte
read. **Fixed** in `9b20d77` (D7) — the same run produces none.

### C9. There is no wrapping container around the message tree

`transcript.html:252` emits
`{% for root in roots %}{{ render_message(root) }}{% endfor %}` directly
into `<body>`, between the filter toolbar and the floating buttons. A
container swap needs a `<div id="transcript">` added first — small and
safe, but it moves every HTML snapshot, so it wants its own commit.

### C10. Two identifiers, only one of them stable

- `id="msg-d-{N}"` (`renderer.py:403`) is **positional per-render**.
  Inserting anywhere but the tail renumbers every subsequent slot and
  breaks the existing `#msg-d-N` fork/tool-pair links on the page.
- `data-uuid` is **stable across re-renders** but **not unique per card** —
  one entry can render as sibling text + tool_use cards.

### C11. Fragment-level patching is still blocked, and the code says so

`work/render-format-once.md`'s step 3 landed as a *result* (format once,
reuse across trees) but not as an architecture: there is no seam that
renders one message in isolation. `RenderUnit.kind` is only `"page"` or
`"session"`; a fragment is produced by the inline `visit()` closure inside
`_annotate_tree_for_render` (`html/renderer.py:1550-1616`) and needs a
fully built `TemplateMessage`, a renderer whose `_ctx` came from *that*
tree, and the SHA-link `ContextVar`.

Decisively: **any fragment containing `msg-d-` is deliberately never
cached** (`html/renderer.py:1608-1609`), precisely because those links are
cross-tree positional. That is the same reason it cannot be patched into a
live page in isolation.

### C12. A torn last line is already tolerated

`json.JSONDecodeError` is caught per line in the load loop
(`converter.py:~434`), so reading mid-append skips the partial line and
picks it up next tick. Sampling the live file at 5 ms across a
473,810-byte append never caught a torn tail (0/3964 samples) — but only
one growth event was observed, so that is weak evidence and the existing
tolerance is what we should keep relying on. The skip prints a warning; a
watch loop needs it silenced.

### C13. Nothing watch-shaped exists yet

No `watchdog`, `inotify`, `watchfiles`, `asyncio`, no `--watch`, no push
channel. The only long-running loop in the package is
`ArchiveServer.serve_forever` (`server.py:194`).

---

## The shape

> **One watch engine. The server never renders — it re-runs the ordinary
> incremental conversion and tells the client the file on disk changed.**

Worth stating as a principle because it protects an invariant the project
leans on: *the generated HTML on disk is canonical and works from
`file://`*. If the server rendered fragments on demand there would be two
rendering paths that could disagree, and `serve` would need the fragment
store, the render memo and the cache write lock in-process.

- Use case 1's client is the editor's file watcher; use case 2's is the
  browser. Same engine, two notification adapters.
- Zero new rendering code.
- Latency is bounded by C6 — which is why C6's fix is a prerequisite, not
  an optimisation.

---

## Decision detail

### D1. `watch` subcommand

A resident process saves the ~0.25 s interpreter + import startup on every
tick (a bare `--help` costs that much), and serves use case 1 with no
server at all. `serve --watch` starts the same `WatchEngine` on a thread.
A `--watch` flag on `convert` was rejected: `convert` is a one-shot verb
and a flag that makes it never return is a surprising overload.

### D2. Put the size in the cache

`get_modified_files` already calls `jsonl_file.stat()` per file
(`cache.py:1544`), so `st_size` is **free** — no extra syscall. Add
`source_size` to `cached_files` (migration 011) and make the rule:

```
stale  if  size != cached_size  OR  |mtime delta| >= 1.0
```

This is **strictly tightening** — it can only mark more files stale, never
fewer — so it cannot break existing correctness. Pre-011 rows carry NULL
and fall back to mtime-only, following the pre-007 `subagents_fingerprint`
precedent already in `_cache_row_is_fresh`.

Why this is better than the watcher keeping its own state, as first
proposed: it fixes C7 for *every* caller, not just the watcher; it makes
the cache directly usable as the watcher's oracle (no parallel
bookkeeping); and it removes the sleep-based timing workarounds tests
currently need, since a rewritten fixture is detected immediately rather
than after a 1 s wait.

Residual hole: a same-size modification within the 1 s window. Impossible
for an append-only source (C1); acceptable elsewhere.

**Detection mechanism: stat-polling at ~250 ms.** Dependency-free,
portable, works on network filesystems, and at watch scope (one project,
~32 files) costs one `scandir`. `watchdog`/`watchfiles` would cut idle CPU
but adds a dependency and, on macOS, FSEvents coalescing latency of its
own. Revisit only if idle CPU is ever a real complaint.

### D3. Scope

Default is the project for `$PWD`; `watch <path>` and `--session-id`
narrow further; `--all-projects` is explicit opt-in. Re-converting a
665-project hierarchy per tick is the obvious wrong default, and it keeps
C6's per-project numbers honest.

### D4. Debounce

Claude Code appends several entries per turn, each within a second or so
of the last. Per-event re-rendering would thrash and spend most of a turn
rendering intermediate states. Quiet-period ~300 ms with a max-latency cap
~2 s, so an unbroken stream of appends still surfaces regularly. Both
flags — the right numbers differ between a small live project and a large
one.

### D5. Container swap, with a re-init contract

`fetch(location.href)` → parse → replace `#transcript` → diff the uuid set
→ rehydrate. Scroll is preserved naturally (appends are below the fold);
fold state is the real casualty and must be snapshotted and reapplied.

Full reload was rejected: it loses fold state *and* re-parses the whole
document. Fragment patching was rejected on C11 (no isolation seam,
`msg-d-` fragments deliberately uncacheable) compounded by C3
(out-of-order arrivals) and C10 (positional ids, non-unique uuids). B gets
most of the perceived benefit for a fraction of the risk.

The inventory is favourable. **Survives a swap for free:** listeners
delegated on `document` (`transcript.html:395, 538, 575`), everything
bound to the toolbar and floating buttons (outside the container), and
`localStorage`-backed prefs (raw/md user view, search state).

**Must be re-run, and none of it is currently re-runnable:**

| what | where | note |
|---|---|---|
| timestamp localisation | `components/timezone_converter.js` | an IIFE that rewrites `innerHTML` for every `[data-timestamp]`, batched via `requestIdleCallback`; **not exposed on `window`** |
| timeline rebuild | `components/timeline.html:46` `buildTimelineData()` | parses message types from DOM CSS classes; `window.timeline` and `applyTimelineFilters` exist, a rebuild entry point does not |
| filter re-application | `transcript.html` filter toolbar | current selection must apply to new cards |
| in-page search state | `components/search.html:1077` `initSearch` | |
| fold state | — | key by `data-uuid` + ordinal-among-siblings sharing it (C10) |
| new-card tagging | — | diff uuid sets before/after; class drives the CSS fade-in |

So the concrete deliverable of this stage is a **`window.claudeLogRehydrate(rootEl)`
contract** that each component registers into, plus scoping each of the
above to a subtree instead of the document. That refactor is most of the
work; the poller itself is small.

Also: tail-follow should be an explicit toggle with an "N new messages"
affordance — auto-scrolling someone who is reading is worse than not
updating. And **watch mode targets session pages**; a growing session can
push the paginated combined view across a page boundary, so that view gets
reload-or-nothing for now.

### D6. Polling

Note the asymmetry that cuts against the intuition that SSE is the proper
answer:

> With polling the server needs **no background machinery at all** — the
> check happens inside a request. SSE needs a watcher thread, a client
> registry, heartbeats and stream teardown regardless. SSE is *more*
> infrastructure, not less.

At a 1 s interval a 304 costs ~1 ms (C5); the efficiency argument for SSE
is worth about a millisecond a second per tab. WebSocket is not worth
discussing — traffic is one-directional and it means hand-rolling framing
on `http.server` or taking a dependency.

If SSE is ever built it must sit behind the same `Host` check
(`server.py:53`), send heartbeats, cap concurrent streams, and be closable
from `ArchiveServer.stop()`.

### D7. Atomic writes

Route every output write through a temp-file + `os.replace` helper — the
pattern `image_export.py:79` already uses. Its own commit; a small
correctness improvement that watch mode makes urgent rather than creates.

### D8. Contention — measured, and fine

Tick cost and search-read latency, measured with an FTS index present and
a reader thread hammering `search()` during ticks:

| | |
|---|---|
| tick, no FTS index | 0.46 s |
| tick, FTS index present | **0.52 s** (so the index update costs ~55 ms) |
| tick under concurrent read load | 0.52 s (unchanged) |
| search reads during ticks (n=103) | **p50 5.0 ms, p95 17.9 ms, max 113 ms, 0 errors** |

No `SQLITE_BUSY`, no measurable slowdown of either side. WAL plus
read-only thread-local connections (`api.py:63-71`) do their job.

**Consequence:** no lock-avoidance machinery is needed. A pleasant side
effect: because `save_cached_entries` also maintains the FTS index
(`cache.py:928-938`), a watch loop keeps **archive search live** for free,
for ~55 ms a tick.

Still open: a single-instance advisory guard per projects dir, so two
watchers (or a watcher plus a manual run) don't fight. Cheap insurance,
annoying to retrofit — decide during Stage 1.

### D9. `--output` destination-aware freshness — ✅ already fixed, no work needed

`work/obsidian-friendly-output.md` recorded this as an open follow-up:
freshness resolving against the *source* project dir, so `--output` runs
re-render everything every time. **That doc was stale.**
`is_transcript_stale` and `is_page_stale` both take an `output_dir` and
resolve against it. Re-measured on a 28-file Markdown projection:

| run | wall | files rewritten |
|---|---|---|
| `-o A` (cold) | 4.1 s | all |
| `-o A` again | **0.0 s** | `index.md` only |
| `-o B` (fresh dest) | 4.0 s | all |
| `-o A` again, after B | **0.0 s** | `index.md` only |
| `-o A` after one appended message | 0.8 s | **2 of 28** — the changed session + `index.md` |

So a watch loop pointed at a vault rewrites the changed session and the
index, not the whole projection. `obsidian-friendly-output.md` has been
corrected.

`index.md` is rewritten on every run by the deliberate always-regenerate
contract (so variant-flag changes refresh its links) — including on a
true no-op. That is not a problem under watch mode, which only converts
when it has already seen a change, but it does mean *any* tick touches
the index and wakes a vault indexer once. Acceptable; with D7 the write
is an atomic rename rather than a truncation.

**Stage 0 therefore has three items, not four.**

### D10. Testing

A watcher tested with `sleep()` is a flaky-test generator. The engine
takes its clock and file-event source as injectable collaborators, so unit
tests drive it by hand (`engine.tick()` after writing a fixture) and only
one or two Playwright tests cover the real path. D2 helps here directly —
with size in the cache, a rewritten fixture is detected without waiting
out the mtime tolerance. The polling tests must be live-server; the
existing browser suite is `file://`-based and C4 means it cannot cover
them.

---

## D11. The `stream-json` piping request

The feature request asks for `claude -p --output-format stream-json |
claude-code-log --stream-json`, on the premise that stream-json is missing
`parentUuid`, `isSidechain`, `userType`, `cwd`, `version` and `timestamp`.

**Verified: `claude -p` writes a normal, full-fidelity transcript JSONL**
to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` — `parentUuid`,
`isSidechain`, `uuid`, `timestamp`, `queue-operation` entries, everything,
identical in shape to an interactive session. (Confirmed on 2.1.251 by
running a headless prompt: the run failed at auth, and the full transcript
was written anyway.)

So the piped schema never needs to be parsed:

- **Live case** → `watch`, pointed at the project. Full renderer, every
  depth and format, all fidelity — strictly more than stream-json carries.
- **CI case** → run to completion, then a one-shot
  `claude-code-log --session-id <id> -o - -f md`. `-o -` already streams a
  rendered document to stdout with status on stderr. The session id is in
  the stream's own `result` event (`session_id`).

The premise is also stale: stream-json on 2.1.251 *does* carry `uuid`,
`timestamp`, `session_id` and `parent_tool_use_id`. What still differs is
naming (`session_id` vs `sessionId`, `parent_tool_use_id` vs
`parentUuid`), the absent `cwd`/`version`/`isSidechain`/`userType`, and
extra `system`/`init` and `result` event types.

**Recommendation: close as subsumed; do not build a stream-json parser.**
The cost being avoided is a second, divergent schema to track forever, for
strictly less information than the file we already have. If the pipe
ergonomics turn out to matter for CI, the cheap answer is a `--follow`
mode that tails the *session's own JSONL* and streams rendered output to
stdout — same engine, no new schema.

---

## Phasing

Each stage answers its own open questions before it starts; each is
independently shippable.

**Stage 0 — prerequisites.**

- ✅ **D7 atomic writes** (`9b20d77`). Every output write goes through
  `utils.atomic_write_text`. Pinned by a concurrent-reader test: 40
  plain rewrites of a 4 MB payload produced 142 torn reads including a
  0-byte one; the atomic path produces none.
- ✅ **D2 `source_size` column** (migration 011). The end-to-end C7
  failure mode — append, convert immediately, repeat — went from
  alternating SEEN/MISSED to six-for-six SEEN.
- ✅ **D9** — nothing to do; see above.
- ⬜ **C9 `<div id="transcript">` wrapper.** The only remaining item, and
  the only one that moves snapshots. Deferred to just before Stage 2,
  which is the first thing that needs it — landing it now would carry a
  large snapshot delta through two unrelated stages.

**Stage 1 — the engine + use case 1.** `claude-code-log watch [path]`:
stat-poll, debounce, call the existing conversion, one line per
regeneration. Serves the Markdown/Obsidian case completely.

*Questions to answer first:*
1. **Can Phase 1b be made reachable when the cache was updated, restricted
   to the touched sessions (C6)?** Without this, Stage 1 full-loads the
   project every tick. This is the stage's real work.
2. Tick cost on the 803 MB reference archive, not the 64 MB one — C6's
   numbers are the optimistic end.
3. Does a resident watcher's warm render memo beat the cold-memo figure?
   (In-process steady ticks were 0.46–0.82 s; the memo's contribution is
   unmeasured.)
4. Single-instance guard: needed, or overkill (D8)?

**Stage 2 — use case 2 under `serve`.** `serve --watch` runs the Stage 1
engine on a thread; the session page polls its own URL, swaps
`#transcript`, rehydrates, fades in new cards, offers follow-tail.

*Questions to answer first:*
1. What does the `claudeLogRehydrate` contract look like, and how much of
   the D5 table can be scoped to a subtree without restructuring?
2. Is fold-state restore by `data-uuid` + sibling ordinal actually clean?
   If it turns out ugly, that is the argument for reconsidering full
   reload.
3. What is the swap cost on a large session page (the 27 MB case)?

**Stage 3 — `file://` sidecar (optional).** A `session-<id>.live.js`
sidecar carrying a revision counter; the page injects it on a timer and
reloads on change (C4). Honest and cheap, but strictly worse than Stage 2,
so build it only if the no-server case proves to matter.

**Stage 4 — SSE and/or fragment patching (speculative).** Only after
Stage 2 has been lived with, and only if the poll interval or swap cost
demonstrably hurts. Fragment patching additionally needs the *architecture*
half of `render-format-once.md` step 3 — a real format phase and a
per-message render seam — which has not landed (C11).

# Real-time watch mode — Design

Status: **Stages 0–2 landed** on `feat/watch-mode`.
`claude-code-log watch` keeps Markdown (or HTML) on disk current, and
`claude-code-log serve --watch` makes an open session page grow as the
session does. Stage 3 (the `file://` sidecar) remains speculative and is
probably not worth building. Stage 4 was too — until the tick was
profiled: see "Where a tick's time goes" and "Appending the HTML rather
than replacing it" at the end. **Fixes A, C and B have all landed**, plus
a follow-up round on the refresh's own queries: a steady-state tick on
the 803 MB archive went **1.03 s → 0.145 s, a 7x tick**, and Fix B needed
no migration in the end — the proof lives in the resident watcher's
memory. The HTML half found that patching does *not* need the
per-message render seam C11 says is missing; that is the open work.

Decisions below are settled; the measurements that forced them are
recorded so a later reader can tell which choices were reasoned and which
were measured — and which turned out to be wrong.

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
| D1 | A `watch` subcommand owns the loop; `serve --watch` runs the same engine on a thread. **Subcommand landed; `serve --watch` is Stage 2.** |
| D2 | Add `source_size` to `cached_files` so the cache itself detects fast appends; the watcher then trusts `get_modified_files` rather than keeping parallel state. Detection is stat-polling. **Landed.** |
| D3 | Default scope is one project; `--all-projects` is opt-in. **Landed.** |
| D4 | Quiet-period debounce (~300 ms) with a max-latency cap (~2 s), both flags. **Landed.** |
| D5 | Container swap (option B) with uuid-set diffing, not full reload and not fragment patching. |
| D6 | Polling. SSE only if measurement later justifies it. |
| D7 | Route every output write through temp-file + `os.replace`. **Landed.** |
| D8 | Measured: the write + FTS update is fast enough. No lock-avoidance machinery needed. |
| D9 | ~~Fix `--output` destination-aware freshness~~ — already fixed; the doc that reported it was stale. **No work needed.** |
| D10 | Injectable clock; unit tests drive `tick()` by hand. **Landed** — the sleep injection turned out unnecessary once `run` used `Event.wait`. |
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

**Stage 1 — the engine + use case 1. ✅ Landed.**

- `claude_code_log/watch.py` — `WatchEngine`: stat-poll, debounce, deliver
  changed paths to a callback. No CLI or HTTP awareness. The scan is a
  cheap *trigger*, not a source of truth; the conversion already knows
  precisely what is stale, and since 011 the cache is the thing that gets
  freshness right. Two classes of file must never be seen as a change —
  dot-prefixed atomic-write temps and generated output, both of which
  land in the watched tree — or the loop feeds itself forever. Pinned by
  tests.
- `claude-code-log watch [path]` — scoped to one project by default,
  resolving the current directory's project; `--combined` defaults to
  `no`, which is what keeps a tick on the session-scoped path.
- Phase 1b reachability (`1ffc41b`) — the stage's real work; see C6.

End-to-end against a live-fed session: an appended message is visible in
the `.md` **0.26–0.36 s** later, dominated by the 0.3 s quiet period,
with the conversion itself at 0.01–0.02 s.

*A note on how the measurements went wrong first, since it cost real
time:* the render pool **spawns**, so a worker re-imports the probe
module. Two probe scripts did their fixture setup (`rmtree` + `copytree`
+ truncate) at module scope, so every worker re-ran it against the tree
the parent was writing into. That produced a `FileNotFoundError` on an
atomic-write temp and an `OSError: Directory not empty` that both looked
exactly like a regression in the atomic-write change, and a
`message_count` that appeared not to advance. All three were the missing
`if __name__ == "__main__":` guard. Any probe that drives a conversion
needs one.

### Stage 1, measured on real archives

The synthetic "large archive" built for this — one project duplicated
four times into 128 files / 180 MB — turned out to be a **worst case,
not a large case**. Every uuid appeared four times, which is exactly what
makes `_incremental_cache_refresh` decline; it fell back to the full
refresh, `CacheRefresh.FULL` vetoed Phase 1b, and every tick full-loaded
(4.1 s). The decline ladder was working as designed; the fixture was
lying. Real archives are what these numbers have to come from.

On `downloads/projects/-Users-dain-workspace-claude-code-log`
(**217 trunk files, 319 MB**), appending to the *largest* session file
(40 MB — the pessimistic pick):

| | |
|---|---|
| cold conversion | 15.2 s |
| **steady tick** | **1.12 s** |
| ↳ `_incremental_cache_refresh` | 0.75 s (**67% of the tick**) |
| ↳ `_load_stale_session_transcripts` | 0.14 s |

So the session-scoped path *does* engage on real data, and **the
bottleneck has moved**: rendering is now 12% of a tick and the cache
refresh is two thirds of it. That is the next thing to attack if 1.1 s
proves too slow; the render work is done.

Answers to Stage 1's other questions:

- **Q3 — does a resident watcher's warm memo help?** Barely. In-process
  ticks 4.11 s vs a fresh subprocess per tick 4.62 s (on the synthetic
  archive, where both paths were identical work). The ~0.5 s gap is
  interpreter startup; the render memo contributes essentially nothing
  across ticks, because the render is not where the time goes.
- **Q4 — single-instance guard?** Not needed. Three concurrent
  conversions against one cache: **0 failures**, no `SQLITE_BUSY`, and a
  clean conversion afterwards. Combined with D8's read-during-write
  numbers, the WAL setup handles this.

**Stage 2 — use case 2 under `serve`. ✅ Landed.**

- `serve --watch` (`6f7850d`) runs the Stage 1 engine on a daemon thread.
- `#transcript` wrapper (`1ec29ed`) — verified layout-neutral by
  pixel-comparing full-page screenshots, not by reading the CSS.
- The in-page poller + rehydrate contract (`f48de95`).

Measured in Chromium against a live-fed session: **~1 s from append to
visible**, scroll position preserved exactly, no navigation (a `window`
marker set before the update survives it), fold state kept, timestamps
localised on new cards, zero console errors.

Two bugs that only measurement found, both worth remembering:

1. **Fold state was preserved for nothing.** Keying the capture on
   `data-uuid` misses session headers and fork points — which carry no
   uuid, and on a single-session page the header is the *only* foldable
   node there is. The key now falls back `uuid → session-id →
   positional id`.
2. **⚠️ `Last-Modified` has one-second granularity**, so two updates
   inside the same second were invisible and the third append in a row
   simply never arrived. `Content-Length` now joins the comparison —
   *the same trap as C7's mtime tolerance, one layer up, with the same
   fix.* Its test fails 3/3 without it and passes 3/3 with it.

**Q3 answered — the swap cost is fine.** On a real **7.0 MB** session
page (523 messages):

| | |
|---|---|
| fetch | 31 ms |
| parse (`DOMParser`) | 63 ms |
| swap (`replaceWith`) | 17 ms |
| forced layout | 91 ms |
| **total** | **202 ms** |
| idle HEAD poll | **~1 ms** (5.6 ms first, then 0.9–2.2 ms) |

Extrapolating linearly, the 27 MB worst case would be ~800 ms of
main-thread work — but only *when that page changes*, and a 27 MB page is
a finished session nobody is appending to. The pages that actually update
are live ones, which start small. No size threshold needed.

Design points that survived contact:

- Only active over `http(s)` — a `file://` page still cannot fetch (C4),
  so the poller notices and does nothing. Pinned by a test.
- The server still never renders; the page re-fetches its own URL, so the
  conditional GET is both the change signal and the content, and there is
  no endpoint to keep in sync with the renderer.
- Container swap, not fragment patching (C3, C10, C11).

**Stage 3 — `file://` sidecar (optional).** A `session-<id>.live.js`
sidecar carrying a revision counter; the page injects it on a timer and
reloads on change (C4). Honest and cheap, but strictly worse than Stage 2,
so build it only if the no-server case proves to matter.

**Stage 4 — SSE and/or fragment patching (speculative).** Only after
Stage 2 has been lived with, and only if the poll interval or swap cost
demonstrably hurts. Fragment patching additionally needs the *architecture*
half of `render-format-once.md` step 3 — a real format phase and a
per-message render seam — which has not landed (C11). **See C19 below:
a patch protocol does not actually need that seam, because the delta can
be taken between two whole renders rather than produced by rendering one
message in isolation.**

---

## Where a tick's time goes — Stage 1's open question, measured

Stage 1 ended with "the bottleneck has moved: `_incremental_cache_refresh`
is 0.75 s of a 1.12 s tick; that is the next thing to attack". This
section answers *why* it costs that, and what appending rather than
replacing would and would not buy.

Measured on the same real archive as Stage 1 —
`downloads/projects/-Users-dain-workspace-claude-code-log`, **217 trunk
files, 319 MB**, appending held-back real lines to the largest session
file (**39.7 MB, 214 lines, 207 entries**), 8 cores, warm cache. The
corroborating small-archive runs use the live `~/.claude/projects` copy
(33 trunk files, 46 MB). Reproduced across three independent probes;
ticks are steady to ±0.1 s.

### C14. One appended line materialises the same 207 entries three times

Per-call trace of a steady tick (1.10–1.24 s):

| # | call site | what it does | cost |
|---|---|---|---|
| 1 | `_incremental_cache_refresh` → `load_transcript` | re-reads and re-parses the whole 39.7 MB file, then `save_cached_entries` **deletes every row for the file and re-inserts all 207** (re-`json.dumps` + `zlib.compress` each) | **488 ms** (307 ms of it the rewrite) |
| 2 | `_load_sessions_partial` → `load_cached_entries` | rebuilds the same entries from the rows just written (`zlib.decompress` + `json.loads` + pydantic) | **129 ms** |
| 3 | `_load_stale_session_transcripts` → `load_cached_entries` | rebuilds them a third time, for the render | **141 ms** |

That is ~760 ms of a 1.1 s tick spent producing three copies of one
list, and every one of the three costs is proportional to the *file*,
not to the append. Phase totals for the rest of the tick:
`get_stale_sessions` 85 ms, `get_session_file_map` ×3 53 ms,
`get_file_states` ×2 47 ms, `get_uuid_owners` ×4 35 ms,
`get_parent_uuid_dependents` / `get_request_id_entries` /
`get_metadata_target_files` ~45 ms combined.

Floors on the same file, for scale: `json.loads` over all 208 lines is
**154 ms**, `blake2b` over the whole 39.7 MB is **33 ms**, and a
`seek` + read of the last 200 KB is **0.13 ms**.

### C15. `get_library_version()` runs 173 times a tick

`is_transcript_stale` → `is_html_outdated` → `get_library_version()`, once
per session, and each call re-parses installed package metadata through
`importlib.metadata`: **35 ms a tick**, and it scales with the number of
sessions in the project rather than with anything that changed. Memoising
it took `get_stale_sessions` from 85 ms to 44 ms. The version cannot
change inside a process; this is a one-line `lru_cache`.

### Fix A — a parsed-entry store, measured — ✅ LANDED

The same shape as the existing `fragment_store`, one layer down: hold the
entries a conversion has already materialised, keyed on
`(path, size, mtime_ns)`, populated by `save_cached_entries` and read by
`load_cached_entries`. Prototyped by monkeypatch:

| | |
|---|---|
| tick, baseline | **1.079 s** (mean of 3) |
| tick, with the store | **0.750 s** (mean of 3) — **30% faster** |
| store traffic per tick | 2 hits, 1 miss — exactly ①→②③ above |

Equivalence: two copies of the 46 MB archive advanced through the same 8
appends, store on and off. **27 of 28 pages byte-identical every tick.**
The 28th is this session's own live transcript, whose *source* differed
by one line between the two `copytree`s (205 vs 206) — a fixture
artifact, not a divergence.

**As built** (`claude_code_log/entry_store.py`, dev-docs § 2.16): the
landed version reproduces the prototype — **0.997s → 0.702s (30%)**,
2 hits / 0 misses per tick — and the phase table confirms where it went:
the closure load fell from 255 ms to **7.1 ms**.

Two things the prototype got away with and the real one must not:

1. **The pipeline mutates entries in place.** `_integrate_agent_entries`
   appends `#agent-{id}` to `sessionId` and is *not* idempotent, and
   dedup re-parents around dropped copies. Today each consumer gets
   freshly deserialised objects; handing both the same list renders
   `…#agent-X#agent-X`. The prototype's fixture happened not to have
   sidechain agents in the modified session, so its byte-identity result
   was luck. `get` therefore returns a `deepcopy` — **2.0 ms and 0.83 MB**
   for the 207-entry, 39.7 MB session, because the bulk of an entry is
   immutable strings that `deepcopy` shares rather than copies. Pinned by
   a test that fails with exactly the doubled suffix when the copy is
   removed (verified by sabotage, not by assumption).
2. **Scope, so it cannot cost RAM anywhere else.** Threaded as a
   parameter like the fragment store, never a global and never on
   `CacheManager` (the TUI holds one across conversions); filled only by
   `_incremental_cache_refresh`, with the files it parsed; dropped after
   Phase 1b. A cold conversion stores nothing, and the streaming path is
   deliberately never handed one — its bounded residency depends on
   dropping each page's entries, which a store spanning pages would
   undo. Plus a per-file valve (decline under 6× available memory) and
   `CLAUDE_CODE_LOG_ENTRY_STORE=0`.

### Fix B — parse and write only the tail — ✅ LANDED, in memory rather than in the schema

**Built, and the shape changed on contact.** What follows is the plan;
"Fix B as built" below records what was actually true. Headline: the
persisted columns turned out to be unnecessary — a resident watcher
already knows what it parsed, so the proof lives in RAM and there is no
migration. Steady-state tick **0.70 s → 0.26 s**.

What remains after A is call ① — a full parse and a full row rewrite for
one appended line. Both are avoidable, and the incremental refresh
already proves the precondition: it only proceeds after showing the
cached rows are an exact *prefix* of the current rows. Today that proof
is a *consequence* of the full parse (`get_file_states` fingerprints
every row, fetching each row's compressed blob just to SHA it). Store
instead, per file, the byte length the cached rows cover plus a hash of
those bytes; then a tick can

1. hash the file's first `prefix_len` bytes (33 ms on 39.7 MB — and a
   strictly *stronger* proof than the row-fingerprint prefix, since
   identical bytes imply identical rows),
2. parse only what follows (~1 ms for one line),
3. `INSERT` only the new rows instead of `DELETE` + re-insert all
   (`messages.id` order is already the ordering the readers rely on, and
   appends preserve it).

Estimated ~450 ms off the remaining tick, i.e. **~1.1 s → ~0.3 s** with
A and B together. Not prototyped — unlike A it needs a migration and a
new decline path (hash mismatch → today's full parse).

### Fix B as built — what the territory actually looked like

The end number matched the estimate (**0.717 s → 0.257 s** steady state,
measured on the 803 MB archive), but three things about the route were
wrong in the plan above.

**1. The migration was unnecessary.** The persisted `(prefix_len,
prefix_hash)` columns exist to carry the append proof *across processes*.
A resident watcher doesn't need that: it parsed the file last tick and
can hold the offset and digest in RAM. So the store gained a
prefix-pinned mode (`put_prefix`/`get_prefix`), `watch` owns one for the
life of the loop, and `convert_jsonl_to` accepts a caller-owned store
instead of always making its own. No schema change, no migration, and the
proof is the same one either way — hash the prefix bytes (32 ms over
39.7 MB, against 143 ms to re-parse them) and compare.

The cost is scope: this only helps a resident loop. A one-shot run, the
TUI, and every tick-one still parse whole. That is the right trade for
the latency-sensitive case, but it is a narrower fix than the persisted
version would have been, and it is why the columns may still be worth
adding later.

**2. The write half was the bigger prize *and* the harder proof.** The
split, measured: of `load_transcript`'s 483 ms, the line loop is 143 ms
and `save_cached_entries` is 310 ms; the whole-file post-processing
(sidecar linking, prompt-hash linking, agent splicing) is **0.1 ms**, so
re-running it over a concatenated list is free. But:

> **A file being append-only does not make its *rows* append-only.** A
> trunk's cached rows carry its subagents' transcripts, spliced in at
> their anchors. A subagent still running — the normal case under
> `watch` — grows a block in the *middle* of the row sequence while the
> trunk file itself only gained lines at the end.

That is why the write is gated on the row list being provably just the
file's own parsed lines (no agent references, no sidecars, nothing
spliced, no length change from the whole-file passes). On the reference
archive that covers 136 of 185 trunk files; 26% reference subagents and
take the unchanged full rewrite. Underneath it,
`extend_cached_entries` independently refuses when the table no longer
holds the row count we think we wrote — the guard against another process
having rewritten the rows.

**That third layer is not theoretical.** With the two gates removed, the
caller offers a *wrong 96-entry slice* (splicing had shifted the
positions the offset refers to) and the row-count check refuses it. Which
also means the obvious test — "did an append-only write happen?" —
passes with the gate gone, because the layer below rescues it. The test
therefore asserts on the **offer**, not the write.

**3. Two of my own probes lied before the code did**, both in the same
way as the Stage 1 note above: a fixture that wasn't what it claimed.

- Copying a *subset* of a project's trunk files breaks parent chains and
  can make the session hierarchy cyclic — `renderer._depth` then blows
  the stack. It reproduces with the store disabled, so it is a
  pre-existing robustness edge (a truncated archive shouldn't recurse),
  not a regression, but it cost a debugging cycle. Copy whole projects.
- Copying the live source *twice* to compare two configurations captures
  two different files when one of them is the session currently being
  written. That showed up as a 3-row `messages` divergence and one
  differing page. Clone one snapshot instead — the same trap that made
  Fix A's first equivalence run report 27/28.

**Equivalence, at three levels** (`test/test_entry_store.py`):

| | |
|---|---|
| parse output, byte path vs text path | **162 fixture files, 0 mismatches** |
| parse output, resumed vs fresh | **90 files, 0 mismatches** |
| cache DB state vs a full-rewrite run | **6 ticks, 3 files growing, all tables match** |
| rendered HTML | identical throughout |

DB state is the bar rather than HTML for the same reason § 2.14 gives:
the first bug a write-path change produces is invisible in the rendered
bytes.

**What was left in the tick — ✅ also done.** At 0.26 s the remaining
items were the refresh's own cache queries, and they turned out to be
much cheaper to fix than "restructure the refresh". Every one of them was
scanning the entire project, which `EXPLAIN QUERY PLAN` says plainly:

    SEARCH m USING INDEX idx_messages_project_timestamp (project_id=?)

Measured per call on a 38,706-row archive:

| | before | after | how |
|---|---|---|---|
| `get_uuid_owners` | 17.2 ms | **0.9 ms** | index `(project_id, _uuid)` |
| `get_parent_uuid_dependents` | 21.5 ms | **0.6 ms** | index `(project_id, _parent_uuid)` |
| `get_request_id_entries` | 17.0 ms | **0.3 ms** | index `(project_id, _request_id)` |
| `get_metadata_target_files` | 14.5 ms | **0.2 ms** | *partial* index on `type` |
| `get_session_file_map` | 16.8 ms | **2.5 ms** | index `(project_id, session_id, file_id)` |
| `get_file_states` | 15.9 ms | **0.0 ms** | **query rewrite, no index** |

Three things worth keeping:

1. **`get_file_states` needed no migration at all.** It joined
   `cached_files` and filtered on `cf.file_name IN (...)`, which gives
   SQLite no indexed way in — resolving the names to `file_id` first and
   filtering on that uses the `idx_messages_file` index that has existed
   since 001.
2. **`get_uuid_owners` already had an index it wasn't using.**
   `idx_messages_uuid(_uuid)` has been there since 001, but the query
   filters `project_id = ? AND _uuid IN (...)`, and SQLite uses one index
   per table reference — so it took the `project_id` one and scanned.
3. **⚠️ The session index made three queries *slower* before it made
   them faster.** `(project_id, session_id, file_id)` lets the planner
   satisfy a bare `session_id IS NOT NULL` as a range scan over every
   session-bearing row, and it *prefers* that to seeking the handful of
   uuids the query asked for. `get_uuid_owners` went from 37 ms to
   **45 ms** on first measurement. Moving that predicate out of SQL and
   into Python — a one-line `if row["session_id"] is not None` — took it
   to 1.4 ms. Adding an index is not automatically safe for queries that
   don't want it.

Cost: cache DB **+8.4%** (39.5 → 42.8 MB on a 49 MB archive) and no write
regression — a cold conversion went 5.85 s → 5.79 s, five more indexes on
an INSERT being small next to compressing the row's content blob.

**Do these indexes help anything outside watch?** Checked every query the
codebase runs against `messages`, with the indexes present and dropped.
Mostly no — `load_cached_entries` and the FTS indexer already seek by
`file_id` (0.00 ms either way), and archiving's session DELETE is 0.4 ms
to begin with. Two things did come out of it:

- The refresh queries aren't watch-specific: `_incremental_cache_refresh`
  runs on *any* invocation with changed files over a warm cache, i.e. the
  ordinary daily run.
- **`load_session_entries` was scanning the whole project to load one
  session** — the TUI (`tui.py:1843`) and archived-session rendering
  (`converter.py:5153`). Same trap as the `session_id IS NOT NULL` one:
  `ORDER BY timestamp NULLS LAST` lets the planner take the *timestamp*
  index to get the sort free, then filter session by session. 84.5 ms for
  a 19 k-row archive's twelve busiest sessions.

  The fix that looked obvious — `ANALYZE` — is the *worse* of the two
  candidates, and measuring saved shipping it: it gets to 15.8 ms,
  `PRAGMA optimize` writes partial statistics that **don't change the
  plan at all** (verified: stats present, plan unchanged), and either way
  the plan then depends on when statistics were last gathered. Putting
  `timestamp` into the session index instead makes one index serve both
  the seek and the ordering: **84.5 ms → 7.2 ms**, no sort, no
  statistics, deterministic. End to end (the method also decompresses and
  validates) a caller sees 21.7 ms → 15.0 ms per session, for 0.4 MB.

  Ordering is the risk with any such change, so it is pinned: 234
  sessions, **29,605 tied-timestamp rows**, 1,237 NULL timestamps, zero
  ordering differences — plus a test that asserts the plan still uses the
  index and still needs no temp B-tree, which fails on exactly that
  message if the column is dropped.

**Steady-state tick: 0.257 s → 0.145 s.** Cumulatively **1.03 s →
0.145 s, a 7x tick**, and the render — where this investigation started —
is now a rounding error against it.

**The zlib knob — taken, at level 3, and I had the cost wrong.** Level 3
cuts the *full* row rewrite from 183 ms to 81 ms; `decompress` is
level-agnostic, so it is backward compatible and one line.

I first priced the size at **+18%**, from that same atypical 40 MB
session (207 entries, ~190 KB each). That number does not survive
contact with an archive: zlib's levels only diverge on large payloads,
and real transcripts are mostly small entries. Measured across a 49 MB
archive's **18,288 rows**, blobs grow 26.37 MB → 27.05 MB — **2.6%**,
with the DB file up 2.1%:

| | level 6 | level 3 |
|---|---|---|
| cold conversion | 6.15 s | **5.49 s** |
| cache DB | 38.3 MB | 39.1 MB |
| tick (full rewrite path) | 0.377 s | **0.329 s** |
| tick (resumed) | 0.352 s | **0.269 s** |

So it is not the watch-local trade I described — it makes cold
conversions 11% faster for 2% more disk, everywhere. One transition
artifact: re-serialising an entry changes its blob and therefore its row
fingerprint, so the first incremental refresh over a level-6 cache
declines to a full refresh once.

### Fix C — the cheap ones — ✅ LANDED (the two that need no migration)

`lru_cache` on `get_library_version` (C15, 35 ms) and reading the
`sessions` / `html_cache` tables once each in `get_stale_sessions`
instead of two queries per session through `is_transcript_stale`.
Together: **85 ms → 4.5 ms**, better than the 44 ms the version memo
alone predicted, because the per-session round-trips were most of the
rest. The per-session logic is inlined faithfully — same check order,
same reason strings; `is_transcript_stale` stays as it was for its other
callers.

Still open, because it needs a migration and belongs with Fix B: give
`get_file_states` a stored fingerprint column so it stops fetching every
row's compressed blob just to SHA it (47 ms).

---

## Appending the HTML rather than replacing it

### C16. The file is the wrong thing to append

Writing the whole page costs **5–6 ms for 3.9 MB** — about 1% of a tick.
Appending in place would save that 1% and would give back the torn-read
class D7/C8 just closed: `os.replace` is what makes a reader (Obsidian, a
browser mid-fetch) safe, and `r+b` + seek + write + truncate is not
atomic. **Recommendation: keep rewriting the file.** The cost worth
attacking is the *browser's*, not the disk's — Stage 2 measured 202 ms of
main-thread work per update on a 7 MB page (fetch 31 / `DOMParser` 63 /
swap 17 / forced layout 91), and that number scales with the page while
the change that caused it does not.

### C17. The page's shape is delta-friendly

Session pages split cleanly: **~140 KB of head** (styles, nav, toolbar)
before `<div id="transcript">`, **~98% body**, then a **9,157-byte tail**
— identical to the byte across the three largest pages sampled. Across 14
page updates the head and the tail were **byte-identical every time**.

### C18. But the body is *not* byte-append-only, for two nameable reasons

Over 14 updates: **0 were exact byte appends.** Six preserved ≥99.9% of
the old body, diverging only within the last ~1 KB; the other eight
diverged at **byte 598**. The causes, read off the diffs:

1. **Ancestor descendant counts.** `data-title-unfolded='Fold (all
   levels) all 227 descendants'` → `…228 descendants` on the root's fold
   bar. Any message added anywhere updates every ancestor's count, and
   the root's is 598 bytes into the container.
2. **Retroactive classes on existing cards.** `class='message thinking'`
   → `class='message thinking pair_first'` when the other half of a pair
   arrives — an already-rendered card legitimately changing.

Both are small and both are client-derivable, which matters for the
option below.

### C19. What actually changes between two renders is ~0.1–0.3% of the body

A line-level diff of the same updates, i.e. what a patch would have to
ship rather than what a byte-prefix comparison can prove:

| tick | body | change blocks | shipped | share of body | shape |
|---|---|---|---|---|---|
| 0 | 19,315 → 19,591 lines | 2 | +11.6 KB / −0.1 KB | 0.32% | tail-only |
| 6 | 19,591 → 19,615 | 9 | +2.1 KB / −1.3 KB | 0.09% | tail + earlier edits |
| 7 | 19,615 → 19,683 | 3 | +5.4 KB / −0.1 KB | 0.15% | tail-only |
| 9 | 19,683 → 19,706 | 9 | +2.3 KB / −1.3 KB | 0.10% | tail + earlier edits |

Even the "diverged at byte 598" ticks change **7 lines / 1.3 KB** away
from the tail. A 3.6 MB body is re-shipped to move 2–12 KB.

**The C11 blocker does not apply.** C11 says there is no seam that renders
*one message in isolation* — true, and it stays true. A patch protocol
does not need one: the server renders the whole page exactly as it does
today, diffs that output against the previous render of the same page,
and ships the difference. Renumbered `msg-d-N` ids (C10) and
out-of-order arrivals (C3) stop being correctness hazards and become
merely a *bigger diff*, with today's full container swap as the automatic
fallback when the diff exceeds some fraction of the body.

### Two ways to get there

**Option 1 — structural delta (no markup change).** Diff by card, keyed
on the `uuid → session-id → positional-id` + ordinal key `live_update.js`
already computes for fold state, and ship
`{replace: {key: html, …}, append: [html, …]}`. Handles C18's retro-edits
and C3's out-of-order arrivals natively. Needs a place to put the patch:
either a small `/api/` endpoint (the server would then need the previous
render, which it has — it is the file on disk) or a sidecar file the
poller `GET`s.

**Option 2 — make the body append-stable, then range-fetch it.** Remove
C18's two mutations from the server-rendered markup: compute descendant
counts in JS at rehydrate (the subtree is right there), and apply
`pair_first` client-side. The body then becomes byte-append-only for the
common case, a `Range: bytes=N-` on the page itself is the whole
protocol, and `SimpleHTTPRequestHandler` already serves ranges. Cheaper
protocol, but it moves rendering responsibility into JS and weakens the
`file://` page, so Option 1 is the safer first move.

Either way the client work goes from "202 ms and growing with the page"
to "a few ms, constant" and, unlike today, `claudeLogRehydrate` runs over
the new cards rather than the whole tree.

### C20. C2 from `identifier-consolidation.md` is the patch protocol's missing half

That draft deferred C2 — the content-derived card id `m-{uuid}-{k}` —
explicitly "pending a consumer", since "the stable-anchor benefit has no
runtime consumer today". **A patch protocol is that consumer**, and it
needs C2 for two things beyond tidiness:

- **Addressing.** A patch says `replace m-<uuid>-0`, and the client does
  one `getElementById`. Without it the client must rebuild the
  `uuid → ordinal` map over the *whole* tree to locate anything —
  `live_update.js` does exactly that today, three full-tree
  `querySelectorAll` passes per update (capture, restore, mark-new).
  That is O(page) work inside the update we are trying to make
  O(delta), and it is a real part of the 202 ms.
- **Insertions.** Every measured update here landed at the tail. C3 says
  arrivals are *not* always in timestamp order, and with positional
  `msg-d-N` one message landing mid-tree renumbers every later card and
  every `#msg-d-N` anchor aimed at them — the diff goes from ~2 KB to
  the whole body and the protocol falls back to a full swap. With
  `m-{uuid}-{k}` an insertion changes nothing about its neighbours, so
  the out-of-order case stays a small patch instead of a fallback.

So the sequencing is: C2 is not a prerequisite for a *tail-append*
patch, but it is what makes the patch robust rather than best-effort,
and it turns C10 from a hazard into a non-issue. Its cost is unchanged
from that draft (resolver plumbing through ~8 formatters / 4 modules,
9 coupled test files, full `.ambr` regeneration, and the vacuous-guard
trap).

It does **not** help the cache. `msg-d-N` and `data-uuid` are
render-only and never enter it; cache identity is the transcript uuid /
`sessionId` / `parentUuid` / `requestId` family, plus the DAG *line* ids
(`{trunk}@{uuid12}`, `{trunk}#agent-{id}`) that
`_incremental_cache_refresh` keeps normalising back to a trunk id with
its local `_trunk()`. Consolidating *that* family would simplify the
refresh, but it is a different consolidation from C2 and a clarity win,
not a speed one — the refresh's time is in parsing and serialising, not
in identifier handling.

### C21. B breaks A's seeding unless the store is cross-tick

A and B attack disjoint costs — B kills call ① (488 ms), A kills ② and
③ (270 ms) — so neither subsumes the other. But they interact, in a
direction that is easy to get wrong:

**A works today only because ① parses the whole file** and hands the
list to `save_cached_entries`, which is what populates the store; ② and
③ then hit it (measured: 2 hits, 1 miss per tick). Under B, ① parses
only the appended tail, so there is no full list to seed with, and ②
and ③ fall back to a full `load_cached_entries` each. **Implemented
naively, B gives back most of what A won.**

The fix makes them reinforce instead: keep the store **across ticks**,
holding `(prefix_len, entries)` per file, and on a proven append
*extend* the stored list with the tail entries B just parsed. A
resident `watch` process then builds the full entry list once and never
again — ① is a ~1 ms tail parse, ② and ③ are hits.

Note this revises Stage 1's Q3 ("a resident watcher's warm memo helps
barely — the ~0.5 s gap is interpreter startup"). That was measured on
the *render* memo, which was never where the time went. An entry store
is the thing that makes residency pay; a one-shot `convert` still
rebuilds the list once per run, where A alone still earns its 30%.

### C22. Neither fix grows the cache DB — B shrinks what it writes

A is purely in-process: already-parsed `TranscriptEntry` objects held
for the duration of a conversion. No schema, no rows, no bytes on disk.
What it costs is **memory** — CONTRIBUTING already puts a project's
in-memory transcript at ~3x its bytes on disk, so a 39.7 MB session is
~100 MB resident while the tick runs, bounded by the sessions in play
rather than by the archive. That is the argument for a valve like the
fragment store's, and for evicting on session change.

B adds two small columns to `cached_files` (`prefix_len`,
`prefix_hash`) — tens of bytes per file row, ~8 KB across a 217-file
project. Against that, it removes a large amount of *write* traffic.
Measured on the 46 MB archive, appending one line to a 626-row file:

| | |
|---|---|
| blobs rewritten per tick, today | **4.08 MB** (all 626 rows deleted and re-inserted) |
| same, under B | ~7 KB (one row) |
| cache DB file growth over 6 ticks | **0 KB** — SQLite reuses the freed pages, WAL checkpoints back |

So the DB does not grow either way; today's cost is write amplification
(~600x), not size.

### Suggested order

1. ✅ **Fix C and Fix A landed.** Tick **1.03 s → 0.717 s** on the 803 MB
   archive. The phase table afterwards:

   | phase | before | after |
   |---|---|---|
   | `load_transcript` (modified file) | 483 ms | 483 ms — *Fix B's target* |
   | ↳ `save_cached_entries` | 310 ms | 310 ms |
   | `_load_sessions_partial` (closure) | 255 ms | **7.1 ms** |
   | `get_stale_sessions` | 85 ms | **4.5 ms** |
   | `get_library_version` (×173) | 35 ms | **0.0 ms** |
   | **tick** | **1.03 s** | **0.717 s** |

2. ✅ **Fix B landed too**, as a cross-tick store rather than a
   migration (see "Fix B as built"). Steady-state tick in a resident
   `watch`:

   | phase | after A+C | after B |
   |---|---|---|
   | `load_transcript` (modified file) | 483 ms | **~35 ms** |
   | ↳ line loop | 143 ms | ~0 ms (tail only) |
   | ↳ prefix hash (new) | — | 32 ms |
   | ↳ `save_cached_entries` | 310 ms | ~5 ms (append-only) |
   | **tick** | **0.717 s** | **0.257 s** |

   C21 held exactly as written: B *does* break A's seeding, and the fix
   was the cross-tick store it predicted.

3. ✅ **The refresh's own queries** (migration 012 + one query rewrite),
   taking the tick to **0.145 s** — cumulatively **1.03 s → 0.145 s, a
   7x tick**. See "What was left in the tick" above.
3. **Option 1** for the browser, which is where the user-visible cost
   actually is, and which no longer depends on `render-format-once.md`.
   Land the tail-append case first; take C2 (C20) when the fallback
   rate on out-of-order arrivals proves it is worth the migration.

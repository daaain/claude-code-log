# Render step 3: format once, assemble many

**Status:** phase 1 (serial fragment store) landed on
`perf/render-memo-and-intra-project-jobs`. Each entry-derived message is
now formatted once per conversion and reused across the combined pages
and session files — `fragment_store.py`, consumed by
`_annotate_tree_for_render`, wired in `convert_jsonl_to`. Verified
byte-identical on five real projects (including the 803MB / 187-file
claude-code-log archive at a 50% hit rate) and by
`test_render_cache_equivalence.py::test_fragment_store_render_is_byte_identical`.

**Phase 2 progress (fed fragments):** three follow-up commits made the
store process-portable and wired it through the fan-out — the
hit-verification now stores a content *digest* instead of a retained
`MessageContent` ref (§ 4.9, premise corrected there), keys are
master-list *ordinals* instead of `id(entry)`, and the render pool now
moves fragments across the process boundary: page workers export their
store as a delta in the result tuple, the parent absorbs the deltas,
and each session unit is dispatched carrying its session's slice
(`RenderUnit.fed_fragments`), which the worker seeds its own store
from. Worker session hit rates measured 96–100% (they were 0 — workers
had no store at all), for −14% total CPU and a small wall win on an
8-core VM over a 34k-message archive (78.6s → 67.7s CPU, 15.3s → 14.3s
wall at 8 workers; serial 28.7s CPU). Byte-identical across every
configuration on that archive, and the pool equivalence test now
asserts the feed engaged. The remaining worker CPU is dominated by
bootstrap — each worker still loads the whole transcript — which is
the next target (§ 2's "no per-worker transcript copy"), along with
the flat pool and the § 7.5 threshold revisit. Also fixed:
`scripts/bench_render.py`'s serial-labeled rows now pin
`RENDER_JOBS=off` (they had been silently running the fan-out since
its default flipped on, making the table labels lie).
Phase 1 confirmed the duplicate work is structurally gone (64,968 lookups
→ 32,441 formats on that archive), but also that its *serial* wall-clock
win on top of a warm leaf memo is modest (~1s of 27s CPU) — the value is
what it unblocks: § 2's parallel format phase, fed workers, and the spill
that bounds fragment memory (186MB held in-memory on the 803MB archive,
~24% of disk bytes on smaller ones). The § 4.9 content-ref retention is
since resolved (equality guard replaced by a content digest, making the
store picklable/spillable — measured peak-RSS-neutral, see § 4.9).
Remaining: §§ 2's phases 2+ below, and the traps in § 4.8 discovered
while landing phase 1.

**Phase 3 progress (no per-worker transcript copy):** render workers no
longer load the transcript at all. Every dispatched unit carries its own
slice of the parent's master list (`RenderUnit.entries`) plus those
entries' master-list ordinals; session units are sliced by the same
trunk predicate every renderer's `generate_session` filters by (HTML/MD
use `sessionId == sid or startswith(f"{sid}#agent-")`, JSON uses
`get_parent_session_id(...) == sid` — same set), so the worker's
re-filter is idempotent. The one per-worker piece of state is a *slim*
SessionTree (`dag.slim_session_tree`, via the pool initializer): the
render path reads only `sessions`/`junction_points`/`workflow_*`, and
the per-message `nodes` mapping — whose pickled size is the whole
transcript — stays in the parent, with lookups on the slim form raising
so a future render-path consumer of `nodes` fails loudly rather than
silently diverging (`workflow_runs` still carries each workflow agent's
entries; small slice, and the renderer does read them). `RenderPool.submit`
declines entry-less units to the inline path, and `_make_render_pool`
declines when there is no pre-built session tree (a worker-side DAG
rebuild from a slice alone can genuinely differ). The memory cap and
the pool thresholds still assume the old full-copy footprint —
deliberately over-conservative until the § 7.5 re-size is measured.

Measured on the 8-core / 16GB VM, same archive as the post-feeding
table below, byte-identical in every configuration:

- `-app` single project: both (auto) went 12.4s / 59.8s CPU →
  **9.9s / 29.1s CPU** (2.69x over memo-only, from 2.11x). Per-worker
  CPU overhead at 8 workers fell ~4.4s → ~0.7s — the transcript-load
  tax is gone, and the worker-count knee should move with it.
- Hierarchy incremental (329MB stale): 30.6s / 108.4s CPU →
  **21.8s / 62.5s CPU** (2.87x, at +3% CPU over serial — down from
  +76%; the pre-feeding baseline was +305%). An 8-core VM now beats
  the same morning's 16-core Mac run (29.4s) on this scenario.
- Hierarchy full rebuild: unchanged (1.00x) — the still-unrevised
  memory cap grants 1 worker/project on 16GB, so the fan-out stays
  inert there until the § 7.5 re-size.

**Phase 4 progress (§ 7 item 5 re-size, 2026-08-26):** the memory cap
now charges measured post-feeding footprints instead of the old
full-copy-per-worker model. VmHWM polling across fanned conversions of
the 140MB/47k and 329MB/97k projects on the 8-core/16GB VM measured:
conversion parent 598MB / 1458MB (~4.4x transcript bytes — master list
+ fragment store + in-flight slices), largest worker 218MB / 329MB
(~136MB + 0.59x). `memory_capped_workers` now charges the parent
4.5x + base and each worker 0.8x + base (~1.2–1.35x margin over the
fit; the one under-charged pathology — a single session spanning most
of the transcript, whose slice re-inflates in its worker — is noted at
the constants). On this VM the incremental cap went 5 → 8 of 8 workers
and the measured scenario went 21.8s → **19.1s wall (3.24x over the
61.8s serial, at +6% CPU)**, byte-identical in every configuration.
Full rebuild on 8 cores stays 1 worker/project because the *core* split
(`jobs // stale`) binds before memory does — the § "flat pool" remains
the fix for that tail, with a cheaper interim below.

**Interim tail fix (landed with the re-size):** the all-projects loop
now holds a *dominant* stale project out of the pool and converts it
last, alone, with the whole render budget
(`converter._dominant_plan` + the hold-back wiring in phase 2).
Dominance is 2x the runner-up, compared on cached message counts when
every plan has one (bytes when not — and note bytes alone would *miss*
the reference giant: 1.08x runner-up's bytes, 2.07x its messages).
Measured on the 8-core VM: full rebuild 65.4s → **58.5s (1.12x)**,
byte-identical. The modest factor is the archive's shape, not the
mechanism: once the 97k-message giant is held back, the 47k runner-up
becomes the new pool wall and the sizes below it are level — exactly
the case the guard declines to hold back. An archive shaped
"one giant + dwarfs" gains more; several level-sized bigs need the
true flat pool (one worker pool over render units across projects,
per-project setups registered with workers, parse/plan still
per-project, cache writes still parent-owned per project), which
stays the end state.

**Phase 5 progress (§ 4.3 referenced-image names, 2026-08-27):**
referenced-mode image filenames are now content-addressed, which fixes
the combined-vs-session overwrite bug and lifts both the pool gate and
the fragment-store exclusion for the mode — see § 4.3 (RESOLVED) for
the details and the two latent inconsistencies it flushed out.

**Phase 6 progress (generalized hold-back, 2026-08-27):**
`_dominant_plan` (one project, 2x the runner-up) became
`_holdback_plans`: a greedy makespan comparison, largest project
first — hold while `pool(rest) + fanned(largest)` beats pooling
everything, with the fanned time taken from a deliberately
conservative speedup table (`_fanned_speedup`: 2.5x at 8+ workers vs
2.7-3.2x measured, because over-estimating serializes projects that
were better off pooled — the costly direction) over the workers a
lone conversion would actually get (memory-capped against the
project's own bytes, so a machine that can't fan never holds). It
reproduces every decision the 2x rule made — including the reference
archive, where after the 97k giant the twin 47k projects keep each
other's pool bounded and nothing more holds — and additionally holds
the shapes the 2x bar missed (a runner-up bounding a pool of smalls
without being 2x any of them; unit-pinned in
`test_render_cache.py::TestHoldbackPlans`). Multiple held projects
convert sequentially after the pool, smallest first, each with the
full render budget. Re-benchmarked over the reference archive on the
8-core box (where the held set is identical, so this is a
no-regression check plus fresher baselines): full rebuild 62.0s
memo-only → 53.9s both (1.15x, vs 1.12x measured for the
single-dominant rule), incremental 59.2s → 17.0s (3.48x),
byte-identical in every configuration.

**Flat-pool design analysis (2026-08-27, so nobody re-derives it):**
the true flat pool's *residual* win over the generalized hold-back is
smaller than it looks, and each candidate architecture has a sharp
edge. (a) A parent-serial flat pool (parent parses each project,
dispatches units to one shared pool) sinks the full rebuild: Σ parse
across the archive is ~50-60s of inherently serial work on the 8-core
VM, at or above today's whole full-rebuild wall — parallel parse
across project workers is load-bearing, so the parent-serial shape is
only viable with pipelined overlap, and even then bounded below by the
biggest parse chain. (b) Keeping parse in project workers means units
(carrying entry slices) must flow worker→parent→render-worker — double
pickling of the whole transcript — or through a Manager queue with the
same cost. (c) A token-governed variant (nested pools as today, but a
machine-wide render-slot semaphore so finished projects' capacity
flows to the still-running ones) preserves parallel parse and needs no
unit shipping, but its memory story is subtle: spawned-but-idle
workers retain their base+memo RSS across the shifting token
assignment, and the two-level cap formula would need rethinking.
(d) For *level-sized* projects specifically, concurrent-static is
already near-optimal once the machine is saturated (speedup is concave
in workers, so spreading cores across projects beats fanning one
wide); the static split's real failures are skew (now held back) and
the memory-capped regime where per-project jobs collapse to 1 (held
back too, when the makespan math clears). What a flat pool uniquely
adds: reclaiming the idle cores during a held-back project's serial
parse phase, and the drained-pool tail among projects too level to
hold. On the measured archives that residual is roughly 1.1-1.3x of
the full-rebuild scenario. Worth having eventually — (c) is the most
implementable shape — but it is no longer the dominant term.

**Still open after phase 6:** the flat pool (above — residual ~1.1-1.3x
on measured archives, token-governed nested pools the most
implementable shape); the fragment-text spill (§ 4.9 made the store
spillable, nothing spills yet — 186MB held in RAM on the 803MB
archive; note that fed session slices re-materialize fragments at
dispatch time, so a spill that matters must also throttle unit
submission); and a >8-worker sweep on the 16-core Mac now that the
per-worker transcript tax is gone (the knee at 8 was measured
pre-feeding; `auto` may no longer overshoot).

This is a handover note. It exists because the two landed optimisations
each hit a ceiling, and *the same restructuring removes both ceilings*.
Everything below was measured, not assumed — the numbers are here so you
don't have to re-derive them, and the traps are here because each one cost
real time to find.

---

## 1. Where things stand

Six commits, oldest first:

| commit | what |
|---|---|
| `0f70250` | Memoize the pure render leaves (Pygments, Markdown) |
| `9312ec1` | Fan a project's own pages and session files out over workers |
| `e8b732d` | Make the render fan-out opt-in and memory-safe |
| `84dad33` | Detect available memory on macOS, and report the cap up front |
| `fe273dd` | Support Windows in the memory probe and the benchmark |
| `dbc888d` | Turn the render fan-out on by default |

(then the phase 1–3 commits described in the status block above:
`3d4a967` fragment store, `bfdbb4c` digest verification, `58f62a0`
ordinal keys, `4b6f654` fed fragments, `dc02828` fed entry slices.)

Read `dev-docs/application_model.md` §§ 2.9–2.10 first — that is the
as-built reference for both, and it is current.

### The problem both steps attacked

A project conversion renders **every message twice**: once into its
combined-transcript page and once into its own `session-*.html`. Both go
through `HtmlRenderer.generate`, which rebuilds the tree from the same
source entries, so all per-message formatting is duplicated. Measured on a
118-file / 12k-message project: **22,420 `format_content` calls covering
11,113 distinct messages**.

- **Step 1 (memo)** caches the two expensive leaves — Pygments
  highlighting and mistune Markdown. 12.4s → 8.7s on that project.
- **Step 2 (fan-out)** renders the independent output files (combined
  pages + session files) in worker processes. 2.70x on a 16-core Mac for
  an incremental run.

### The two ceilings

**They fight each other.** Splitting units across processes gives every
worker a cold memo cache, so the page-vs-session duplication comes back.
On 4 cores, 47k messages:

| | wall | total CPU |
|---|---|---|
| neither | 44.8s | 43.5s |
| memo only | 27.6s | 26.2s |
| fan-out only | 22.5s | 57.0s |
| both | **20.0s** | 48.8s |

Both together is fastest, so they compose — but only sub-additively. The
fan-out alone is 2.0x; on top of the memo it is 1.38x.

**Workers are memory-hungry.** Each holds a full copy of the transcript
(they reload from cache rather than being fed), and a loaded transcript
costs 2–3x its bytes on disk (measured: 118MB → 236MB RSS; 140MB → 418MB).
Peak is `workers × project size`, multiplied again by the project pool
under `--all-projects`. This drove a 4GB dev VM into swap and wedged it,
which is why `render_pool.memory_capped_workers` exists.

16-core Mac, 8 real projects (1543MB of transcripts, largest 329MB):

| scenario | config | wall | CPU | cores used |
|---|---|---|---|---|
| full rebuild, 8 stale | memo only | 100.8s | 226.0s | 2.2 of 16 |
| full rebuild, 8 stale | both | 79.3s (1.27x) | 324.8s | 4.1 of 16 |
| incremental, 1 stale | memo only | 93.2s | 90.8s | 1.0 of 16 |
| incremental, 1 stale | both | **34.6s (2.70x)** | 367.9s | 10.6 of 16 |

**The full-rebuild row is the unsolved case.** Converting the largest
project alone takes 93.2s; converting all eight takes 100.8s — the other
seven cost 7.6s of wall clock between them, so the run is within 8% of
"how long does the biggest project take". The static budget split then
hands that project `jobs // stale projects` = 2 render workers while the
small ones finish and 13 cores idle. Same project alone gets 16 workers
and reaches 2.70x.

**Post-feeding re-measure (2026-08-24).** Both machines, same archive
(`downloads/projects`, 1539MB, largest 329MB), all rows byte-identical:

| machine | scenario | config | wall | CPU |
|---|---|---|---|---|
| 16-core Mac (63GB) | full rebuild, 8 stale | memo only | 98.5s | 223.2s |
| 16-core Mac (63GB) | full rebuild, 8 stale | both (3 workers/project) | 65.8s (1.50x) | 277.1s |
| 16-core Mac (63GB) | incremental, 1 stale | memo only | 93.6s | 91.1s |
| 16-core Mac (63GB) | incremental, 1 stale | both (16 workers) | **29.4s (3.18x)** | 286.1s |
| 8-core VM (16GB) | full rebuild, 8 stale | memo only | 62.3s | 188.2s |
| 8-core VM (16GB) | full rebuild, 8 stale | both (capped 1 worker/project) | 65.0s (0.96x) | 191.2s |
| 8-core VM (16GB) | incremental, 1 stale | memo only | 63.7s | 61.7s |
| 8-core VM (16GB) | incremental, 1 stale | both (capped 5 workers) | 30.6s (2.08x) | 108.4s |

Single-project sweep on `-app` (140MB), wall / CPU:

| workers | 16-core Mac | 8-core VM |
|---|---|---|
| serial (memo only) | 21.5s / 21.5s | 26.2s / 25.3s |
| 2 | 16.4s / 29.5s | 19.2s / 32.7s |
| 4 | 12.1s / 37.5s | 14.6s / 42.4s |
| 8 | **10.2s** / 54.8s | 12.7s / 60.6s |
| 16 | 10.9s / 99.8s | — |

What the numbers settle, in order of consequence:

1. **Feeding worked as designed**: incremental went 2.70x → 3.18x wall
   and 367.9s → 286.1s CPU against the pre-feeding table above.
2. **Per-worker bootstrap is now the whole remaining overhead, and it
   scales with project size**: ~5s CPU/worker on the 140MB project
   (99.8−21.5 over 16), ~12s/worker on the 329MB one (286.1−91.1 over
   16). That is the transcript reload — § 2's "no per-worker copy" is
   confirmed as the top item.
3. **The sweep saturates at 8 workers, and `auto` overshoots on big
   machines**: 16 workers is *slower* than 8 on the Mac (10.9s vs
   10.2s) at nearly double the CPU. The § 7.5 revisit has its answer:
   until the per-worker copy is gone, worker counts past ~8 are pure
   cost, so `resolve_render_jobs`'s auto should be capped (or scaled
   against project size) in the interim.
4. **The wall floor is the parent, not the workers**: the Mac at 16
   workers (29.4s) and the VM at 5 workers (30.6s) land on the same
   incremental wall. The residue is the serial parent bootstrap
   (load + parse + plan of the 329MB project). More workers cannot
   help; only the restructuring can.
5. **Full rebuild improved (1.27x → 1.50x) but stays the unsolved
   case**, and on a 16GB machine the memory cap makes the fan-out
   fully inert there (1 worker/project, 0.96x). The flat pool + cap
   relaxation remain the fix, both unlocked by item 2.

---

## 2. What step 3 is

Split rendering into two phases:

1. **Format once.** Compute each distinct message's rendered fragment
   exactly once, in parallel.
2. **Assemble many.** Build every combined page and every session file
   from those fragments.

Assembly is nearly free — `[TIMING] Template rendering (20022484 chars)
0.082s` for a 20MB page, against 0.776s of content formatting for the same
page. The expensive 72% is the formatting, and it is currently done twice.

### Why this removes both ceilings at once

- **No memo/parallelism conflict.** The duplication is eliminated
  structurally rather than papered over with a cache, so splitting work
  across processes no longer re-creates it. The leaf memo in
  `render_cache.py` stays useful for *intra*-pass repeats (the same file
  Read many times — Pygments hit rate was ~69%, well above the 50% that
  page-vs-session duplication alone implies), but it stops being
  load-bearing.
- **No per-worker transcript copy.** Fragments are small strings keyed by
  message, so a worker can be *fed* its slice instead of reloading the
  whole project. That removes the `workers × project size` memory
  multiplication, which in turn removes the reason for
  `memory_capped_workers` to be so conservative — and unblocks the
  full-rebuild tail, since the budget stops being rationed by RAM.
- **Enables a flat pool.** With units this cheap, the two nested pool
  levels (projects × render workers) can collapse into one pool over
  format units, which is the real fix for the static-split tail problem.

---

## 3. The seams

| what | where |
|---|---|
| Per-message formatting (the pre-order walk to split) | `html/renderer.py:1493` `_annotate_tree_for_render` |
| Render entry point | `html/renderer.py:1592` `generate` → `1642` `_generate_inner` |
| Session filter + back-link | `html/renderer.py:1735` `generate_session` |
| Tree construction (format-neutral) | `renderer.py:717` `generate_template_messages` |
| Combined-page loop | `converter.py:1696` `_generate_paginated_html` |
| Session-file loop | `converter.py:2789` `_generate_individual_session_files` |
| Unit dispatch (inline or pooled) | `converter.py:2728` `_dispatch_render_units` |
| Pool gating | `converter.py:2648` `_make_render_pool` |
| Worker implementation | `render_pool.py` |

`_annotate_tree_for_render` is the fulcrum. It walks the tree depth-first
and writes `rendered_title` / `rendered_html` / `rendered_timestamp` onto
each `TemplateMessage`, then the Jinja macro recurses over
`message.children`. Step 3 means computing that annotation once per
distinct message and reusing it across every tree that contains the
message.

---

## 4. Traps

Each of these cost real time to discover. None are obvious from the code.

### 4.1 `uuid` is NOT a safe key for a message fragment

Instrumenting a real project found 22,420 `format_content` calls, 11,113
distinct uuids, and **196 cases where the same uuid rendered different
HTML**. Those are genuine uuid *collisions* across distinct messages
(resumed/forked sessions re-use them), not context-sensitivity — one
sample showed an assistant text message and a todo-list tool use sharing
a uuid. Key on content, or on the identity of the source `TranscriptEntry`
(the `messages` list is shared across both render passes within one
`convert_jsonl_to` call). Do not key on uuid.

### 4.2 Markdown is not a pure function of its text

`html/utils.py::render_markdown` runs the SHA-linkifier plugin, which
resolves commit hashes against the per-render repo cwd carried by
`git_remote._render_repo_cwd` (a `ContextVar`, read via the public
`current_render_repo_cwd()`). The same text legitimately renders different
links in different projects. Any cache or fragment store must include that
cwd in its key, or a long-lived host (`serve`, the TUI) will serve one
project's commit links inside another's page. Pygments has no such
coupling. This is the *only* ContextVar affecting render output — verified
by grepping the package.

### 4.3 `image_export_mode="referenced"` is not parallel-safe (RESOLVED)

Renders used to write `images/image_NNNN.png` from a per-`generate()`
counter, so the combined and per-session passes assigned the same names
to *different* images (the last pass overwrote the other's files), and
concurrency would have let two processes write one file at once —
`_make_render_pool` declined for the mode, and the fragment store
excluded it. Resolved by content-addressing the filenames
(`image_export.export_image`: `image_<blake2b-of-decoded-bytes>.<ext>`,
written via unique temp file + atomic `os.replace`, skipped when the
file already exists): every pass, run, and worker converges on the same
file per image, so the pool gate and the fragment-store exclusion are
lifted. This also fixed two latent inconsistencies found on the way:
paginated combined pages ignored the conversion's image mode entirely
(built a default embedded renderer, both inline and in workers), and a
default Markdown conversion resolved to referenced *inside*
`get_renderer` while the pool gate checked the raw `None` param — so
pooled Markdown renders were already running the colliding counter.
Regression coverage: `test_image_export.py`
(`TestReferencedModeAcrossRenderPasses` parametrized over the paginated
and single-file combined paths, plus content-addressing unit tests) and
the inverted pool-gate test in `test_render_cache.py`.

### 4.4 Pages have a write-ordering dependency (already fixed — don't reintroduce)

`_generate_paginated_html` used to call
`_enable_next_link_on_previous_page(output_dir, page_num - 1, suffix)`
*while generating page N*, so page N's render edited page N-1's file on
disk. `31b09e6` moved that to a single idempotent post-pass over pages
`1..N-1` after all pages land. Keep it that way.

### 4.5 Output is written with `errors="replace"`

Transcripts can carry lone surrogates (issue #139). Every write uses
`write_text(..., encoding="utf-8", errors="replace")`. Match it, or you
will crash on real data that currently renders fine.

### 4.6 The parent owns every cache write

Workers render and write output files, nothing else. Staleness checks and
`update_html_cache` / `update_page_cache` stay in the parent so the SQLite
DB keeps a single writer. `_dispatch_render_units` takes an `on_written`
callback for exactly this. Preserve the property.

### 4.7 Every fallback path must stay a fallback

A pool that can't bootstrap (a library caller without the
`if __name__ == "__main__"` guard `spawn` needs), or a worker that dies,
must still produce complete correct output by rendering inline. Never let
a performance feature become a correctness requirement.

### 4.8 A message's rendered fragment is NOT a pure function of its entry

Discovered by hash-diffing real projects after the fixture-based
equivalence test already passed — fixture coverage was not enough. Three
distinct classes of cross-tree divergence exist, each with its own guard
in the landed phase 1 (all in `_annotate_tree_for_render` /
`fragment_store.py`):

1. **Per-tree `#msg-d-{N}` anchors.** Async task forward/back-links,
   background-job links and hook parent links embed the *partner*
   message's `message_index`, which is assigned per render tree — the
   same message links to `#msg-d-253` in its session tree and
   `#msg-d-893` in the combined tree. Guard: any fragment whose output
   contains `msg-d-` is never stored (output scan — catches every
   emitter, present and future; a false positive only declines caching).
2. **Tree-derived TemplateMessage flags.** `display_model` (a Task spawn
   card shows the sub-agent's model badge only in a tree that contains
   the sub-agent's transcript), `agent_depth`, pair presence,
   `spawns_collapsed_transcript`, `in_workflow_sidechannel`. Guard: a
   signature of these fields is part of the store key, so each variant
   occupies its own slot.
3. **Content built from cross-message ctx lookups.** A tool result whose
   paired `tool_use` lives in *another session* resolves its tool name in
   the combined tree but not in the session tree, producing different
   MessageContent for the same entry (observed: generic collapsible
   rendering vs. specialized). Guard: `get()` verifies the stored content
   object compares equal to the requesting tree's content (dataclass
   equality; `message_index`/`fragment_key` are `compare=False`), else
   it's a counted conflict served fresh.

The uuid-collision instrumentation quoted in § 4.1 could not have found
classes 1–3 (it compared within one project that happened not to exercise
them). Assume any new per-tree state is a divergence source until proven
otherwise, and verify with hash runs over `downloads/projects/` — the
fixture project exercises none of these classes.

### 4.9 Storing content refs retains memory (RESOLVED — with a corrected premise)

The content-equality guard originally kept a reference to one
MessageContent per fragment. Resolved by replacing the retained ref
with a 16-byte content digest (`fragment_store.content_digest`) — a
canonical BLAKE2b walk with the same field coverage as dataclass
`__eq__` (compare=True fields only), so the hit-verification
semantics are unchanged. Where Python `==` is looser than the
canonical form (bool/int unification, dict insertion order,
identity-`repr` objects) the digests differ and the lookup counts as
a conflict served fresh — divergence is only ever in the safe
direction. Unit-pinned in `test/test_fragment_store.py`; hash-runs
over five real archives byte-identical with unchanged hit rates
(32,441/64,968 on the 803MB archive; conflict counts 0–48 per
project, all served fresh).

**The premise needed correcting, though.** A direct A/B on the 803MB
archive (dev VM, serial, warm nothing) measured maxrss 1521MB with
the ref-retaining store and 1521MB with the digest store — dropping
the refs moves the *peak* not at all on this workload. The peak lands
at the end of the combined-page pass, where the stored content
objects alias strings the master entry list and the live tree hold
anyway; the +269MB store-on delta over store-off (1252MB) is the
186MB of fragment text plus per-entry dict/string overhead, not the
refs. So the digest's value is structural, not a memory win: the
store now holds only strings and bytes (picklable, spillable, cannot
pin object graphs in differently-shaped workloads), which is the
property the fed-worker format phase needs. Any future memory
bounding must target the fragment text itself.

---

## 5. How to verify

**Byte-equivalence is the acceptance test.** Any restructuring must
produce identical output to the serial, un-memoized path.

```bash
uv run pytest test/test_render_cache_equivalence.py -v
```

That converts a real 40-session project three ways (memo off, memo on,
fanned out over workers) and compares bytes, with guards so a silent
fallback to the inline path can't make the comparison vacuous. Extend it
with a step-3 configuration rather than writing a new harness.

Then the wider check, which hashes output across far more real data:

```bash
uv run python scripts/bench_render.py ~/.claude/projects/<big-project>
uv run python scripts/bench_render.py ~/.claude/projects --all-projects --projects 8
```

It copies to scratch space (never touches the real tree), warms the cache,
times every configuration, prints the memory cap up front, and fails loudly
if any configuration's output differs. A 233-file run confirmed all six
configurations byte-identical.

Full suite before pushing: `just ci`.

---

## 6. Environment notes for the dev box

- **The 8-core / 16GB box measures the fan-out fine** — it is the
  "8-core VM" every post-feeding table in this doc quotes. Core count
  changes the fan-out's answer, so cross-check performance claims on
  the 16-core Mac with `scripts/bench_render.py` before generalizing.
- **Don't run unbounded benchmarks without sizing them first.** An
  earlier `--all-projects` benchmark on a smaller VM exhausted RAM and
  wedged it (every core pegged, unresponsive, hard restart). The memory
  cap now prevents the worker explosion, but copying multi-GB project
  trees to scratch space is still worth sizing against free disk.
- The box runs all of `just ci`: the full suite including browser
  tests (Chromium and its system libraries are provisioned by the box
  config) and the pyright leg (Debian's nodejs is provisioned so the
  pyright wrapper uses its bundled JS instead of fetching node past
  the wall). `.venv` lives on a box-local shadow volume, so host and
  box binaries never clobber each other.
- Real transcripts for local testing: `downloads/projects/` (7.9GB, 84
  projects, with a warm cache DB). Test fixtures:
  `test/test_data/real_projects/`.

---

## 7. Suggested order

1. Read `dev-docs/application_model.md` §§ 2.9–2.10.
2. Extract the format phase behind the existing seam — produce a
   fragment store keyed per § 4.1, consumed by `_annotate_tree_for_render`.
   Keep it serial at first; prove byte-equivalence.
3. Confirm the duplicate work is gone (instrument `format_content` call
   counts: they should fall from ~22,420 to ~11,113 on the reference
   project), and measure serial wall clock.
4. Only then parallelise the format phase, and only then consider
   collapsing the two pool levels into one flat pool.
5. Revisit `memory_capped_workers`, `_MIN_MESSAGES_FOR_RENDER_POOL`
   (`converter.py:2645`) and `per_project_render_jobs`
   (`converter.py:4204`) — all three were sized around a per-worker
   transcript copy that step 3 should make unnecessary.

If step 3 turns out to be too invasive, the three landed pieces are
independently useful and each commit reverts cleanly.

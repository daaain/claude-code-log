# Render step 3: format once, assemble many

**Status:** proposed. Steps 1 and 2 have landed on
`perf/render-memo-and-intra-project-jobs` (stacked on
`feat/archive-search-server`); this is the follow-up they were built to
make safe to attempt.

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
| `f136c32` | Memoize the pure render leaves (Pygments, Markdown) |
| `31b09e6` | Fan a project's own pages and session files out over workers |
| `69ccbe3` | Make the render fan-out opt-in and memory-safe |
| `687bc7a` | Detect available memory on macOS, and report the cap up front |
| `eb152da` | Support Windows in the memory probe and the benchmark |
| `f66c759` | Turn the render fan-out on by default |

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

### 4.3 `image_export_mode="referenced"` is not parallel-safe

Renders write `images/image_NNNN.png` from `self._image_counter`, which
resets per `generate()` call (`html/renderer.py:1661`). Those filenames
already collide between the combined and per-session passes today;
concurrency would let two processes write one file at once.
`_make_render_pool` declines for this mode. Step 3 could actually *fix*
this properly by allocating image names once during the format phase —
worth doing, but out of scope unless it falls out naturally.

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

## 6. Environment notes for the dev VM

- **The 4-core / 4GB VM cannot measure the fan-out.** `memory_capped_workers`
  correctly declines on every real project there, so `auto` is
  indistinguishable from serial. Correctness work is fine locally;
  performance claims need the Mac and `scripts/bench_render.py`.
- **Don't run unbounded benchmarks on the VM.** An earlier
  `--all-projects` benchmark exhausted RAM and wedged it (all four cores
  pegged, unresponsive, hard restart). The memory cap now prevents the
  worker explosion, but copying multi-GB project trees to `/tmp` is still
  worth sizing first.
- **`.venv` is shared with the host Mac over the mount.** A `uv run` on
  either side replaces it with that platform's binaries and the other side
  then fails with `Exec format error` until it re-syncs. `uv sync` fixes
  it; pointing `UV_PROJECT_ENVIRONMENT` at different paths per platform
  would fix it properly.
- Real transcripts for local testing: `downloads/projects/` (7.8GB, 84
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

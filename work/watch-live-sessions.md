# `serve --watch` latency on a large archive — findings and plan

Status: investigation done, nothing implemented. Follows PR #321
("Make watch ticks cheap"), whose measurements were taken on a 217-file
archive with a few hundred entries per file. This note re-measures on
cboos's Windows archive, which has a different shape, and finds two
costs the earlier measurements could not see. Everything below was
measured on 2026-09-06 on this branch (`perf/watch-tick-latency`, tip
`1c38ba0`), Windows 11, 4 logical cores, cache DB 890 MB.

## The archive

| | |
|---|---|
| projects | 332 |
| trunk JSONL files | 1,607 (1.2 GB); 1,896 watched files with agent sidecars |
| largest project | `C--Users-cboos-SAM-main`: 10 files, 109 MB, live file 53 MB / **28,421 lines** |
| this repo's project | 7 files, 20 MB, live file 9 MB / 2,363 lines |

The reference archive PR #321 was tuned on had a 39.7 MB live file with
**207 entries**. Here the live files carry 100× the entry count at the
same byte size. Everything that costs per entry rather than per byte is
100× more expensive, and that turns out to be most of a tick.

## What `serve --watch` does per tick today

`WatchEngine([projects_path])` globs the whole archive every 0.25 s, and
on any change calls `process_projects_hierarchy(projects_path,
write_combined=False)` — a full plan pass over every project, then a
conversion of the ones that changed, then the index and search page.

## Measured

### 1. The no-change hierarchy pass: 95 s, of which ~90 s is SQLite connection churn

Three consecutive passes with nothing changed: 94.5 s and 97.4 s wall
(the first, a catch-up, 171 s). Per-project plan cost has a median of
0.2 s, ×332 = 67 s; the rest is the same cost outside the loop.

cProfile of one steady pass (104.9 s under the profiler):

| | calls | cumulative |
|---|---|---|
| `CacheManager._get_connection` | 7,354 | 89.5 s |
| ↳ `sqlite3.Connection.close` | 3,658 | **41.4 s** |
| ↳ `sqlite3.Connection.execute` (the three PRAGMAs dominate) | 16,724 | 37.2 s |
| ↳ `sqlite3.connect` | 3,658 | 11.1 s |
| `get_library_version` via `importlib.metadata` | 675 | 3.3 s |
| `pathlib.glob` | 7,348 | 4.3 s |
| `check_html_version` (opens each session page) | 670 | 1.6 s |

`_plan_project` opens **eleven** connections per project, each a fresh
`connect` + `PRAGMA journal_mode=WAL` + close. Every close is the *last*
connection on the database, so SQLite checkpoints and deletes the
`-wal`/`-shm` files each time, which on Windows against an 890 MB file is
where the 41 s goes. Micro-benchmark on the real DB:

| connect + 3 PRAGMAs + close | per cycle |
|---|---|
| no other connection open | 29.0 ms |
| one idle connection held open | **6.7 ms** |

Patched into the pass without touching the repo:

| strategy | steady pass |
|---|---|
| baseline, a connection per call | 95.9 s |
| one idle connection held for the pass, still a connection per call | **17.9 s** |
| one connection shared by every `_get_connection` in the process | **5.3 s** |

The 5.3 s remainder is the version lookup (3.3 s, see next), globbing
and file opens.

### 2. Regression on this branch: `get_library_version` lost its memo

`1c38ba0` inserted `_combined_link_stale` between
`@functools.lru_cache(maxsize=1)` and `def get_library_version`, so the
decorator now memoises the wrong (pure, two-argument) function and the
version lookup re-parses package metadata on every call again: 675 calls,
3.3 s per pass. §2.15 of the application model still describes it as
memoised. One-line fix: move the decorator back.

### 3. The watch scan itself: 0.4–0.9 s per poll, polled every 0.25 s

`watch.scan` runs two `**` globs over the archive and then `stat`s each of
the 1,896 hits. On Windows the stat is a separate syscall per file, while
`os.scandir` already returns size and mtime with the listing. Measured
idle: 0.43–0.91 s per scan; a `scandir` walk using `DirEntry.stat()`
returned the identical dict 4–7× faster. At the current poll interval the
watch thread never sleeps, and it shares the GIL with the server thread
answering the page's HEADs. (Under the design in §Plan B the roots shrink
to a handful of directories and this stops mattering for `serve`; it still
matters for `watch --all-projects`.)

### 4. A real one-line append tick on a big session: 15–27 s

The hierarchy pass hid this. Scratch copies of two projects, the live
file's last five lines held back and appended one per tick, entry store
held across ticks as `serve` does:

| project | cold | no-change tick | append ticks (one line each) |
|---|---|---|---|
| this repo (live file 9 MB, 2,363 lines) | 25.6 s | 0.06 s | 2.15, 1.32, 6.70, 1.37, 1.44 s |
| SAM-main (live file 53 MB, 28,421 lines) | 54.4 s | 0.07 s | 26.9, 15.5, 16.6, 17.8, 16.1 s |

Against a documented target of 0.26 s. cProfile of one SAM-main append
tick (25.4 s):

| | cumulative | what |
|---|---|---|
| `copy.deepcopy` | **16.0 s** | 4.6 M recursive calls copying pydantic entries: `entry_store.get_prefix` 4.5 s, `entry_store.get` 3.9 s (×2), `put_prefix` 3.3 s |
| `save_cached_entries` | 5.5 s | re-`json.dumps` + `zlib.compress` + insert of all 22,551 rows |
| `get_file_states` ×2 | 5.8 s | reads every row's `content` blob and `sha256`s it, both calls |
| `_load_sessions_partial` | 4.5 s | mostly the `get` deepcopy above |

Three separate costs, each O(entries in the file), none O(append):

- **The store copies on every read and write.** `entry_store.py`'s
  docstring argues `deepcopy` "is cheap and stays cheap because the bulk
  of an entry is immutable strings". That is true per byte and false per
  entry: it is ~0.2 ms per entry, three times a tick. The consumers that
  mutate are the reason for the copy; either they stop mutating (and the
  store hands out the same list, guarded by a test that mutation raises),
  or the copy becomes lazy — copy the slice a consumer touches, not the
  file.
- **The full row rewrite** is the "not in scope" item from PR #321: a
  trunk whose rows carry spliced subagent transcripts cannot append rows,
  so it deletes and re-inserts all of them. SAM-main has subagents; so
  does anything that uses the Agent tool, i.e. the sessions one actually
  watches.
- **`get_file_states` fingerprints from content.** Hashing 22 k blobs
  twice per tick to compare prefixes is 5.8 s here. A stored fingerprint
  column (one migration) makes the query index-only.

Without the deepcopy alone the tick is ~9 s; with all three addressed it
should land near the 0.26 s the note promises, since the remaining work is
proportional to the append.

## The proposal, evaluated

cboos's proposal: build the index at server start; when a session page is
opened, mark it *live* and watch only live sessions; a page that stops
asking is no longer live; regenerate the index every few minutes or on
explicit reload.

**It is the right shape, and the fixes above do not replace it.** Even at
5.3 s the no-change hierarchy pass is not something a 1 s-poll loop can
run per tick, and it will always be O(archive): every project is planned
so that the *index* can be rewritten, on a tick whose only purpose is one
session page. A tick has to cost O(what is live). Two further things the
proposal buys that the fixes do not:

- The hierarchy pass reads the whole cache DB on the watch thread while
  the server thread is answering HEADs a second apart; both share the GIL
  and the disk. Under the proposal the watch thread touches one project.
- The engine's roots collapse from the archive to the live projects'
  directories, which makes the scan cost (§3) irrelevant for `serve`.

The browser side needs nothing new to signal liveness: `live_update.js`
already HEADs its own URL every second and stops when the tab is hidden.
That HEAD *is* the heartbeat. A page whose tab is hidden or closed stops
polling, drops out of the live set after a timeout, and stops being
watched — exactly the semantics asked for, with no registration call.

What the proposal does not fix: the per-tick cost of the live session
itself (§4). A watched SAM-main session would still take 16 s per append
until §4 is addressed. Both are needed; they are independent.

## Plan

### A. Cheap fixes, in this order — they help every command, not just watch

✅ **Landed (2026-09-06), all three.** Steady no-change hierarchy pass on
this archive: **95 s → 2.1–2.8 s** (three consecutive runs: 65 s
catch-up, then 2.8 s, 2.1 s). The lease is `cache.connection_lease`,
taken by `process_projects_hierarchy` for its whole run;
`get_library_version` has its memo back with a test pinning the wrapper
to the function; `watch.scan` is a `scandir` walk with a parity test
against the glob form. Track C is what the live-session tick still
needs on big files; track B is unchanged.

1. **Restore the memo** on `get_library_version` (§2). One line, plus a
   test that asserts the function object is an `lru_cache` wrapper so a
   later insertion cannot move it silently again.
2. **Connection lease for the hierarchy pass** (§1). A module-level,
   per-thread lease in `cache.py`: `with CacheManager.lease(db_path):`
   opens one configured connection and `_get_connection` yields it when
   no `batch()` is active. `process_projects_hierarchy` takes the lease
   for its whole run, and `serve --watch` holds one for the process
   lifetime. Per-thread because `sqlite3` connections are thread-bound
   and the server thread has its own. Measured 96 s → 5.3 s. Keep the
   existing Windows-safety property: the lease closes on scope exit, so
   nothing holds the `-wal`/`-shm` files past the pass. Fallback if the
   shared connection turns out to interact badly with `batch()` or the
   pool workers: the sentinel alone (a held idle connection, no sharing)
   is three lines and gives 96 s → 18 s.
3. **`scandir`-based `watch.scan`** (§3). Same result dict, same
   filtering; `DirEntry.stat(follow_symlinks=False)`. Pinned by a test
   that both scans agree on a fixture tree with a dotfile, a sidecar and
   a nested agent directory.

### B. Live sessions under `serve --watch`

4. **Live registry** in `server.py`: `LiveSessions` with
   `touch(project_dir_name, session_id)` and `live(ttl) -> {project:
   {session,...}}`. `send_head` calls `touch` when the translated path
   matches `<projects>/<project>/session-<id>.html` (session pages only;
   combined and index pages are explicitly not live, matching the
   existing "combined pages stop tracking" decision). TTL 10 s: the page
   polls every 1 s and stops when hidden, so the set follows what is
   actually on screen. Thread-safe (requests arrive on server threads,
   reads on the watch thread).
5. **Roots as a provider.** `WatchEngine(roots=...)` accepts a callable
   evaluated per tick; `serve` passes `lambda: [projects_path / p for p
   in live.projects()]`. A project entering the live set is primed on
   entry so its history is not reported as one change. `watch
   --all-projects` and the `watch` command are unchanged.
6. **Tick converts live projects only.** `reconvert` calls
   `convert_jsonl_to(project_dir, write_combined=False,
   entry_store=store)` per live project, never
   `process_projects_hierarchy`. The session-scoped path then renders
   only the stale session(s) in that project. The index is not touched
   by a tick.
7. **Index on a timer and on demand.** `--index-interval` (default 5 min,
   0 to disable) runs the hierarchy plan pass on the watch thread between
   ticks, skipped whenever a live tick is pending so it never delays one.
   `/api/refresh-index` triggers the same pass and returns when done; a
   small "refresh" control on the index page calls it and reloads. With
   A2 the pass is ~5 s here and ~1 s on Linux.
8. **Lazy first render** (`--lazy`, later the default under `--watch`).
   Startup today converts the whole archive (171 s catch-up here, minutes
   cold). Instead: startup runs the plan pass and writes the index only;
   a GET of a session page whose project is stale converts that project
   inline before serving (one blocking request, seconds on a big project,
   once). Projects never cached still need one full load, which this
   defers to first click rather than start. This is the most invasive
   step and can ship after 4–7.

### C. The per-tick O(entries) costs (§4), each its own PR

9. **Stop copying the file to read a line.** Decide the store's mutation
   contract: find the consumers that mutate (`git grep` for in-place
   changes on `TranscriptEntry` after `load_transcript`), make them copy
   what they change, and have the store return its list. A test that
   freezes returned entries (or checks identity across two `get`s) pins
   it. Expected ~16 s off the SAM-main tick.
10. **Stored row fingerprints** (migration 014): a `fingerprint` column
    written by `save_cached_entries`, read by `get_file_states` instead of
    hashing `content`. ~5 s off.
11. **Append-only rows for spliced trunks** — the "not in scope" item in
    PR #321: stop splicing agent rows into trunk rows so an appended file
    is an appended row set. ~5 s off, and the change that lets §2.16's
    resume path apply to the sessions people actually watch.

### Verification

- Re-run this note's benchmarks after each step; the scripts are
  `hier_bench.py`, `hier_prof.py`, `hier_fix.py`, `append_bench.py`,
  `append_prof.py` (session scratchpad; worth moving under `scripts/`
  as a `bench_watch.py` with the held-back-lines method, so the numbers
  are reproducible on a copy of any real project).
- Targets on this archive: steady hierarchy pass ≤ 6 s (A), a
  `serve --watch` tick with one live session ≤ 1 s on the 9 MB session
  (B), ≤ 1 s on the 53 MB session (C).
- Browser tests for B: a live page receives an append; a page that has
  stopped polling causes no conversion (assert the engine's
  `conversions` counter); index refresh route works and the index page's
  control uses it.
- Docs: `docs/live-updates.md` (what "live" means, the index cadence,
  `--index-interval`, `--lazy`), `dev-docs/application_model.md` §2.15
  (the registry, the roots provider, and correct the memo claim).

### Side finding, unrelated

`claude-code-log convert --help` crashes with `UnicodeEncodeError` when
stdout is cp1252 (a Git Bash pipe on Windows): a `※` in the help text.
`PYTHONUTF8=1` works around it; the fix is to drop the character or set
`click.echo` up to degrade.

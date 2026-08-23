# Full-Archive Search via a Local Web Server — Design Draft

Status: **draft validated against a real archive** — see
[Measured findings](#measured-findings) for the numbers that corrected
several first-draft claims. Open decisions are resolved at the bottom.

## Motivation

The in-page search baked into generated HTML only sees the messages on the
current page. Searching *everything* — all projects, all sessions — needs an
index over the whole archive, and the SQLite cache already contains every
message. But SQLite isn't reachable from `file://` pages, so this needs a
small local web server.

Feature requests for tagging, note-taking and commenting on transcripts point
the same way: they need somewhere to persist writes, which static HTML can't
do. The server and its API should be designed so those can be added without
rework.

## Constraints

- **No new dependencies** — Python stdlib only (`http.server`, `sqlite3`,
  `json`). Jinja2/mistune etc. are already available if we render anything
  server-side.
- **Local, single-user** — bind `127.0.0.1`, no auth, no HTTPS.
- **`file://` output stays canonical** — the static HTML must keep working
  unchanged for people who never run the server.

## What we already have

The cache (`claude-code-log-cache.db`, one per projects directory) stores:

- `projects`, `sessions` — aggregates (timestamps, token totals, summaries,
  first user message, cwd).
- `messages` — **every message, fully normalised**, with `type`, `timestamp`,
  `session_id`, `_uuid`, sidechain/agent flags, token counts, and the full
  message content as **zlib-compressed JSON** in a BLOB column.

  The promoted columns are *metadata only*. The BLOB is
  `zlib.compress(json.dumps(entry.model_dump()))` — the **entire** entry
  (`cache.py:_serialize_entry`), so it both duplicates those columns and is
  the **only** place the searchable body lives: `text`, `thinking`,
  `tool_use.input`, `tool_result.content`, attachment payloads, plus fields
  no column carries at all (`gitBranch`, `teamName`, `spawnedAgentId`,
  `sourceToolUseID`, `toolUseResult`, `compactMetadata`, `hookErrors`,
  `message.model`, `stop_reason`). Nothing searchable can be recovered
  without decompressing it.
- `html_cache`, `html_pages`, `page_sessions` — which HTML file each
  session/page lives in and when it was generated (staleness detection
  already implemented, #271).

Two consequences:

1. Everything needed to search and to build result excerpts is already in one
   file — no new parsing pipeline.
2. The compressed BLOB means SQL can't scan content directly (`LIKE` doesn't
   see through zlib). Any search index needs an **extracted plain-text**
   representation.

The bundled `sqlite3` ships with **FTS5 enabled** (verified on this machine;
standard for CPython builds on all three OSes — still worth a runtime feature
check).

## Measured findings

All numbers below come from a real 8.0 GB archive
(`downloads/projects`, Aug 2026): **532,728 messages / 1,713 sessions /
77 projects**, cache DB **1,288 MB** (of which 928 MB is the content BLOB
column). Measured on the dev VM (Linux aarch64, Python 3.14.5, SQLite
3.51.2). Scratch harness: extract → build FTS → benchmark.

### Q1: do we need the zlib BLOB? — **Yes, twice**

1. **At index time** — it is the only source of searchable text (above).
2. **At query time** — for **snippets**, because
   `snippet()`/`highlight()` **do not work on a contentless FTS5 table**.
   They don't error either; they return `NULL` *silently*:

   ```python
   db.execute("create virtual table c using fts5(x, content='')")
   db.execute("insert into c(rowid,x) values(1,'hello world foo')")
   db.execute("select snippet(c,0,'[',']','…',5) from c where c match 'world'")
   # -> [(None,)]      <-- not an error. Silent NULL.
   ```

   So the draft's Option 1 sketch (contentless + `snippet()`) cannot work as
   written. The fix is cheap: decompress the BLOB for the ~20 result rows
   only and build the excerpt in Python. Measured cost for 20 rows:
   **~0.3 ms**. That is the whole reason the BLOB stays in the query path.

Everything else the API needs (project, session, uuid, type, timestamp,
tokens) is already in promoted columns — no decompression needed for those.

### Q2: index size — **+20%, not a doubling**

Extracted plain text, by field group (whole archive):

| group | text | note |
|---|---:|---|
| `text` | 64.2 MB | user + assistant prose |
| `thinking` | 18.8 MB | |
| `tool_input` | 72.3 MB | tool name + input JSON |
| `tool_result` | **404.2 MB** | 69% of all text |
| `attachment` | 25.0 MB | |
| `meta` | 1.4 MB | summaries, ai-titles, system |
| **total** | **585.9 MB** | |

Resulting FTS5 table, built over all 532,728 rows:

| variant | size | Δ on 1,288 MB cache | notes |
|---|---:|---:|---|
| `content=''` (contentless), all groups | **253 MB** | **+20%** | **recommended** |
| `content=''`, no `tool_result` | 94 MB | +7% | if noise wins |
| `content=''`, `detail=column` | 201 MB | +16% | ✗ breaks phrase/NEAR |
| **content-stored** (ordinary fts5) | **1,080 MB** | **+84%** | ✗ this is the "doubles the DB" case |

Built in place on a copy of the real cache: **1,288 MB → 1,541 MB**.

So the draft's "roughly comparable to the compressed content" was wrong in
both directions: contentless is *much* smaller (253 MB vs 928 MB), and a
content-stored table really would nearly double the file. **The
recommendation must say `content=''` explicitly** — it's the difference
between +20% and +84%.

**Base64 is a trap.** A naive "flatten all JSON values" extractor pulls
inline image payloads into the index: 657 MB of text and a 335 MB index
instead of 586 MB / 253 MB. Worst single offender was a 6.1 MB
`queue-operation` entry that is one `<ide_selection>` line plus a
screenshot; that type alone went 30.4 MB → 3.0 MB once base64 was skipped.
The extractor must walk *known* content-item shapes and drop
`image` / `source.data`, with a `^[A-Za-z0-9+/=_-]{512,}$` backstop.

### Q3: speed — **comfortably inside the few-hundred-ms ceiling**

Top-20 `ORDER BY rank`, warm cache, on the 253 MB contentless index:

| query | match | +join for metadata | +Python snippets | hits |
|---|---:|---:|---:|---:|
| rare (`zlib`) | 0.1 ms | 0.1 ms | 0.4 ms | 134 |
| mid (`pydantic`) | 2.1 ms | 2.1 ms | 2.3 ms | 2,804 |
| phrase (`"search index"`) | 1.3 ms | 1.0 ms | 1.5 ms | 23 |
| prefix (`tokeniz*`) | 0.7 ms | 0.7 ms | 0.9 ms | 938 |
| `NEAR(cache invalidation, 10)` | 1.2 ms | 1.0 ms | 1.3 ms | 677 |
| **pathological (`the`)** | 153 ms | 150 ms | 149 ms | 219,497 |

Cold (page cache evicted via `POSIX_FADV_DONTNEED`, including
`sqlite3.connect` + `ATTACH`): **6.5–45 ms**. End-to-end through the
prototype `search()` (escape → match → join → decompress → snippet):
**1.1–6.6 ms**.

Only a stopword-grade term costs ~150 ms, and it still fits the budget.

### Q4: ⚠️ the one real landmine — project filtering picks the wrong plan

Filtering to a project is a day-1 requirement, and the obvious SQL is
**840× too slow**:

```sql
-- 2,012 ms  ✗
select f.rowid from message_fts f
  join messages m on m.id = f.rowid
 where f.message_fts match ? and m.project_id = ?
 order by f.rank limit 20;
```

`EXPLAIN QUERY PLAN` shows why — SQLite makes `messages` the **outer** loop
and probes the FTS table by rowid, which is the FTS5 anti-pattern
(a contentless FTS5 rowid lookup has to walk doclists):

```
SEARCH m USING COVERING INDEX ix_pid (project_id=?)
SCAN f VIRTUAL TABLE INDEX 32:=M6          <-- FTS probed per row
USE TEMP B-TREE FOR ORDER BY
```

`CROSS JOIN` pins the FTS table as the outer loop and fixes it outright:

```sql
-- 2.4 ms  ✓
select f.rowid from message_fts f
  cross join messages m on m.id = f.rowid
 where f.message_fts match ? and m.project_id = ?
 order by f.rank limit 20;
```

| query, filtered to the largest project (112k msgs) | plain join | `CROSS JOIN` |
|---|---:|---:|
| `refactor` | 2,012 ms | **2.4 ms** |
| `sqlite` | 1,090 ms | **1.5 ms** |
| `pydantic` | 622 ms | **2.1 ms** |

Notes:
- A narrow side table of just the metadata columns does **not** help
  (2,020 ms) — the cost is the plan, not reading the inline BLOB. With
  `CROSS JOIN`, joining fat `messages` directly is already 2.5 ms, so **no
  auxiliary table is needed**.
- Session filtering is fine either way (1.0 ms) because `session_id` is
  selective enough.
- This must be a **commented invariant** in the query builder plus a
  regression test asserting the plan, or someone will "tidy up" the
  `CROSS JOIN` and silently reintroduce a 2-second search.

### Q5: build and refresh cost — **cheap enough to change decision 2**

| operation | time |
|---|---|
| decompress + extract, whole archive | **21 s** (26,475 msg/s) |
| full FTS backfill incl. `optimize` | **31 s** |
| delete FTS rows for the largest file (6,939 msgs) | **21 ms** |
| re-extract + reinsert that file | **160 ms** |

`DELETE FROM message_fts WHERE rowid = ?` requires **`contentless_delete=1`**
(SQLite ≥ 3.43) — without it, contentless deletes need the original column
values supplied back, which we don't keep. The draft omitted this flag.

Incremental maintenance is ~180 ms for the *worst* file in the archive, so
piggybacking on the existing invalidation path (`cache.py:782`,
`DELETE FROM messages WHERE file_id = ?` → reinsert) is essentially free.

### Q6: do the links work? — **98% yes, with two caveats**

Resolving 320 real search hits to `session-{id}.html` + a `data-uuid`
anchor: **315 resolved (98%)**; 4 failures were `summary` rows, 1 an
assistant entry. All 1,713 sessions have a generated per-session page, so
pagination never enters the deep-link path.

But **not every indexed message is renderable.** Sampling 25 sessions and
diffing cache uuids against `data-uuid` in the generated HTML:

| type | count | absent from HTML |
|---|---:|---:|
| `assistant` | 12,875 | 2% |
| `user` | 8,050 | 5% |
| `attachment` | 531 | 89% |
| `system` | 261 | 89% |
| `progress` | 3,403 | **100%** |
| `ai-title` | 482 | **100%** |
| `queue-operation` | 241 | **100%** |

Conveniently, `progress` contributes **0 bytes** of searchable text, so
skipping it drops 55,689 rows (10% of the archive) from the index for free.
`ai-title` and `queue-operation` do carry text and should be indexed, but
their results must link at **session** granularity, not message.

Browser-verified with Playwright: appending ~10 lines that read `?uuid=`,
call the existing `window.claudeLogRevealMessage(el)` and `scrollIntoView`,
the target lands on screen with no page errors — **100–520 ms** for pages
of 1.1–3.2 MB. Nothing new is needed in the renderer.

One wrinkle: in **5 of 8** samples the matched text sat inside a *closed*
`<details>` within the card (`innerClosedDetails: true`). Landing on the
card is right, but the match itself is collapsed. The existing search
component already opens `<details>` on the path to every highlight, so the
link format should be:

```
{project-slug}/session-{session_id}.html?uuid={message_uuid}&q={terms}
```

— `uuid` anchors and reveals, `q` re-runs in-page search for the highlight.

### Q7: two smaller gotchas

- **User input is not valid FTS syntax.** `vis-timeline` raises
  `OperationalError: no such column: timeline` — a bare hyphen reads as
  column-filter/NOT syntax. Every user term must be quoted
  (`"vis-timeline"`, doubling embedded `"`), with `tok*` handled as
  `"tok"*`. Otherwise ordinary queries are hard errors.
- **`projects.project_path` is an absolute path** to the *original*
  location (`/Users/dain/.claude/projects/-Users-dain-…`). Resolving a
  result to a file must join the served projects root with
  `Path(project_path).name`, never trust the stored path — otherwise a
  moved or copied archive resolves nothing (found the hard way: 240/240
  links "missing" on the copied archive).
- Session pages are large: p50 0.8 MB, p90 3.0 MB, p99 7.9 MB, max 27.2 MB
  (2.26 GB total). Fine over loopback, but it argues for per-session pages
  over combined ones as the deep-link target, and against any "render the
  whole project" dynamic view.

### What this changes in the draft

| draft claim | verdict |
|---|---|
| Cache has everything needed; no new parsing pipeline | ✅ confirmed |
| BLOB is opaque to SQL; needs extracted plain text | ✅ confirmed |
| FTS5 available | ✅ SQLite 3.51.2 |
| `data-uuid` emitted; anchor problem settled | ✅ confirmed — `ec4fb80` is this doc's own commit, so "Option 1 is implemented" already covers it; nothing since has changed it |
| Option 1 SQL: contentless + `snippet()` | ❌ `snippet()` returns NULL on contentless — build excerpts in Python |
| "Index size roughly comparable to compressed content" | ❌ 253 MB contentless (+20%); 1,080 MB if content-stored |
| `contentless_delete=1` | ❌ omitted, and required for incremental updates |
| Option 3 (Python BLOB scan) as the FTS5-missing fallback | ⚠️ ~20 s/query archive-wide — fine as a `regex=` escape hatch, not as a fallback for the feature |
| Serve static HTML, deep-link into it | ✅ 98%, with the `?uuid=&q=` form and non-renderable types handled |
| — | ➕ **new:** `CROSS JOIN` required or project filtering is 840× slower |
| — | ➕ **new:** base64 must be excluded from extraction (+28% index otherwise) |
| — | ➕ **new:** user query text must be FTS-escaped |

## Part 1: The server

### The axis that actually matters: the seam, not the framework

A-vs-B was framed as a framework choice, but measurement says the framework
is nearly irrelevant and the **seam** is what decides testability:

> Keep the search core — extractor, query builder, filters, result shaping —
> as **pure functions in a module with no HTTP awareness**. The HTTP layer is
> a thin adapter over it.

With that seam, unit tests call `search(q, project=…, fields=…)` directly at
sub-millisecond cost and never open a socket — which is the *entire*
testability argument for WSGI, obtained without WSGI. What's left for the
HTTP layer is ~100 lines of routing, best covered by the live-server browser
tests we need anyway (the frontend enhancement only activates over `http://`,
so those cannot be `file://` like the current suite).

Given the seam, Option A wins on a point the draft missed: **99% of requests
are static files, and Option B makes us write that ourselves.**

### Option A — `http.server.ThreadingHTTPServer` + `SimpleHTTPRequestHandler` *(recommended)*

Subclass `SimpleHTTPRequestHandler`, dispatch `/api/` prefixes to JSON
handlers, `super().do_GET()` for everything else, rooted via the
`directory=` argument.

Measured against the real 2.26 GB of generated pages:

| | result |
|---|---|
| 0.8 MB page (p50) | 6.9 ms |
| 27.2 MB page (largest) | 13.8 ms (~2 GB/s, loopback) |
| conditional GET | **304 in 0.5–1.6 ms** — free, and worth a lot over 2.26 GB |
| path traversal (5 encodings incl. `..%2f`, `%2e%2e/`) | **5/5 → 404** |
| query string on a static path (`?uuid=…&q=…`) | 200, handled |
| streaming | `copyfileobj` — 27 MB is never buffered in memory |

The draft's alternative — "a hand-rolled safe-path resolver" — should be
struck. That is precisely the CVE surface, and `translate_path` already
handles it.

### Option B — WSGI app via `wsgiref`

- Pro: callable in tests without a socket; portable to a real server later.
- Con: **`wsgiref` has no static file serving.** We would hand-write the
  path resolution, MIME typing, `Last-Modified`/304 handling and chunked
  streaming that Option A gets for free — for the 99% of traffic that is
  static, over an archive whose pages reach 27 MB.
- Con: its one advantage is neutralised by the seam above, and "portable to
  production" is an explicit non-goal.

**Recommendation:** A, with the search core kept HTTP-free.

### Implementation notes measured out of the prototype

1. **Threading is required — but not for the reason the draft gave.** The
   generated pages are self-contained: a real browser page load makes
   **exactly 1 request** to our server (verified with Playwright; the only
   external fetch is vis-timeline from unpkg, which never touches us). So
   there are no "parallel asset requests" to absorb. The real case is an API
   call landing during a large page transfer — and there threading is the
   difference between usable and not:

   | `/api/search` latency | idle | during 3× concurrent 27 MB downloads |
   |---|---:|---:|
   | `ThreadingHTTPServer` | 3.0 ms | **5.9 ms** |
   | `HTTPServer` (single-threaded) | 2.8 ms | 44.7 ms |

   Phased search (decision 6) means several in-flight API calls, which makes
   this more relevant, not less.

2. **`BrokenPipeError` must be swallowed.** A client that navigates away
   mid-transfer — routine with multi-MB pages — makes the stock handler dump
   a full traceback to stderr:

   ```
   Exception occurred during processing of request from ('127.0.0.1', 58424)
     ... shutil.copyfileobj(source, outputfile)
   BrokenPipeError: [Errno 32] Broken pipe
   ```

   Reproduced on the first Playwright run. Catch `ConnectionError` around the
   transfer (and override `log_message`) or the console fills with tracebacks
   during entirely normal use.

3. **Validate the `Host` header on day 1, not once writes exist.** The stock
   handler ignores `Host` entirely — verified: a request claiming
   `Host: attacker.example.com` is served `200`. That is the DNS-rebinding
   hole, and the draft deferred it to the write endpoints. Wrong way round
   for *this* server: the read side already exposes the full transcript
   archive — credentials pasted into prompts, private source, client work.
   Rebinding lets any page the user visits *read* all of it. Accept only
   `127.0.0.1[:port]`/`localhost[:port]`; it is a few lines.

4. **SQLite + threads.** `threading.local` read-only connections with
   `busy_timeout`. WAL is already enabled (`cache.py:360`), and reads are
   genuinely unblocked during a writer's open transaction — measured at
   **0.2 ms** while a write transaction was held open, so a background
   backfill would not stall search (it would still contend with the
   *converter* for the write lock — see decision 2).

5. **Route `/api/` before static, and reserve the prefix** so a project
   directory literally named `api` can't shadow the API.

### CLI integration

`claude-code-log serve [--port 8010] [--open-browser]` (or `--serve` flag on
the main command — see open decisions). Runs the normal conversion first so
the static HTML exists and the cache is fresh, then serves the projects
directory.

## Part 2: The search index

### Option 1 — FTS5 external-content table *(recommended)*

Final shape, after the measurements above:

```sql
CREATE VIRTUAL TABLE message_fts USING fts5(
    text, thinking, tool_input, tool_result, attachment, meta,
    content='',              -- contentless: +20% not +84% (see Q2)
    contentless_delete=1,    -- required for incremental deletes (Q5)
    tokenize='unicode61 remove_diacritics 2'
);
-- rowid == messages.id, so joining back to messages is a rowid lookup
```

At cache-write time (see decision 2) extract the searchable plain text from
each message's content JSON — walking *known* content-item shapes so
base64 image payloads are dropped (Q2) — and insert with
`rowid = messages.id`. Skip `type='progress'` entirely: 55,689 rows
carrying zero text and no rendered card (Q6).

Six columns rather than one, because it costs nothing in the same index and
buys (a) API-level field filtering — `tool_result` alone is 69% of the text
and is by far the noisiest, (b) cheap phased/streaming queries via FTS5
column filters: `{text thinking meta} : q` runs in 88 ms where the
unrestricted query needs 153 ms on the pathological term, and (c) knowing
*which* field matched, for the result card.

- Query: `... WHERE message_fts MATCH ? ORDER BY rank LIMIT 20` for ranking,
  then decompress those 20 BLOBs and build excerpts in Python — `snippet()`
  is unavailable on contentless tables (Q1).
- **Always `CROSS JOIN` to `messages`** when filtering (Q4).
- **Always escape user input** before `MATCH` (Q7).
- Incremental: the cache already knows which files changed; FTS rows are
  deleted (`DELETE FROM message_fts WHERE rowid = ?`, enabled by
  `contentless_delete=1`) and reinserted alongside the `messages` rows they
  mirror, on the same invalidation path — ~180 ms for the largest file.
- Caveats: FTS5 `MATCH` is token-based — no regex, and substring matching
  needs `token*` prefixes. Phrase/boolean/NEAR come free.

**Backfill strategy:** a migration (`008_message_fts.sql`) creates the table
empty; the one-time population (**31 s** for 532k messages) runs at first
`serve`, and from then on the index is maintained incrementally by the
normal cache write path (**~180 ms** worst-case per file). See decision 2.

### Option 2 — plain-text column + `LIKE`

Add `messages.text` (extracted, uncompressed), search with
`WHERE text LIKE '%term%' COLLATE NOCASE`.

- Pro: dead simple, no FTS availability concern, trivially supports substring
  match semantics identical to the in-page search.
- Con: full table scan per query (fine up to ~10⁵–10⁶ messages, then
  noticeably laggy), no ranking, no snippets (excerpt in Python), roughly
  doubles content storage.

### Option 3 — Python-side scan of compressed BLOBs

No schema change: stream `messages.content`, decompress, search in Python.

- Pro: zero migration; regex support for free.
- Con: slowest by far (decompress-everything per query); realistic only as a
  fallback or a `--regex` escape hatch that's allowed to be slow.

**Recommendation:** Option 1 (contentless, six columns), with Option 3 as the
`regex=true` escape hatch. Note Option 3 measures at **~20 s per query**
archive-wide (decompress + extract everything), so it is a deliberate
"this one is slow" mode with progress, never a transparent fallback. If
FTS5 is genuinely missing on some platform, the honest answer is to disable
archive search there rather than silently serve 20-second queries.

## Part 3: Serve static HTML or render on the fly?

### Option A — serve the generated HTML *(recommended)*

The server is a static file server plus a JSON API. Search results link to
the existing per-session pages: `project-slug/session-{id}.html#msg-…`.

- Pro: one rendering pipeline, one visual identity, `file://` and `http://`
  users see identical output; pagination, timeline, filtering all just work.
- Con: results must deep-link *into* those pages (anchor problem below), and
  pages can be stale — mitigated by running conversion at server start and/or
  re-checking `html_cache` freshness per request and regenerating on demand
  (the staleness machinery from #271 already answers "is this fresh?").

### Option B — render everything on the fly from cache

No static files; every page is rendered per-request from cached messages.

- Pro: always fresh; could render *just* the matched session or even a
  message-range slice, which static files can't.
- Con: a second render path to keep in visual/functional parity (the
  transcript template assumes whole-session DAG context: forks, pairing,
  fold counts); multi-second renders for big combined views; `file://`
  output becomes a second-class citizen.

### Option C — hybrid

Static serving (A) plus a small number of dynamic views where static can't
go: the search results page itself, and later perhaps "render this single
session fresh" for stale pages instead of full regeneration.

**Recommendation:** A, drifting toward C only when a concrete need appears.
The search results "page" doesn't even need server-side rendering: see next
section.

### Frontend: a separate search page, not an upgraded search box

The draft proposed *upgrading* the index-page search box to hit `/api/search`
when the API is reachable. **Rejected** — measuring what that box actually
does shows the two searches aren't the same tool wearing different hats.

#### What the index-page search does today

`components/search.html` is shared by every page and branches on
`searchState.isIndexPage`, detected by content (`.project-list` present,
line 80) rather than filename. On the index page it:

- Builds a client-side index from the DOM (`buildSearchIndex`, line 104) of
  **1,791 entries** on the real archive: 77 project cards (name + stats) and
  1,713 session links (project name + session title/summary + the
  first-user-message `<pre class='session-preview'>` + timestamp/message
  count).
- Matches case-insensitive substring (or regex), then renders a results panel
  grouped by project (`displayIndexSearchResults`, line 659) — "📁 Project
  Overview" / "💬 Session {8-char id}" cards with an excerpt, a match count
  and a link to the session page.
- Unlike transcript pages, it does **not** filter or highlight anything in
  place: `clearSearchFilterClasses` returns early (line 535) and
  `highlightInElement` is skipped (line 440). Escape closes the panel instead
  of clearing the query. State persists in `localStorage`.

**It is a session finder, not a content search.** The numbers:

| | |
|---|---:|
| searchable text on `index.html` (titles + previews) | **1.02 MB** |
| extracted text in the archive | 585.9 MB |
| **coverage** | **0.17%** |

Session previews are the untruncated first user message (median 408 chars).
So the box answers "which conversation was that?" over 0.17% of the archive;
archive search answers "where was this ever discussed?" over 100%. Merging
them behind one input would make the same keystrokes mean two different
things depending on whether a server happened to be running.

#### The shape

- **Index pages → global search.** The existing box stays **exactly as is**
  (no source toggle, no API path), and gains a **link** to the search page.
- **Session pages → local search.** Unchanged.
- **Combined transcript pages** are transcript pages, so they keep local
  search, and get the same link — pre-filtered to their own project.
- **Global search lives on its own page**, `search.html` at the projects
  root, so relative `{slug}/session-*.html` links resolve. It never touches
  the 1,713 generated session pages or the index page, which is what makes
  this cheap.

It reuses `search_styles.css` and the grouped-by-project result-card markup
from `displayIndexSearchResults` — that structure is already the right shape
for global results — but ships its own JS. Keep it free of `.project-list`
so the shared component's page-type detection can't misfire if it's ever
included.

#### URL params

`search.html?q=…&project=…&fields=…&type=…&from=…&to=…` — shareable,
bookmarkable, and back-button correct. One gotcha: use debounced
`replaceState` while typing and `pushState` only on submit/filter change,
or Back walks the user through every keystroke.

The project filter is set automatically from the originating page's link and
is visibly overridable (decision 7).

#### `file://` degradation

Generate `search.html` as a real static file always, and have it ping
`/api/ping` on load. Without the API it renders instructions
(`claude-code-log serve`) instead of a search box. That beats a server-only
route: the link from the index page never 404s, and the page documents the
feature to anyone who finds it. Nothing else in the archive changes for
`file://` users.

Note the enhancement is keyed to *how the page was loaded*, not to whether a
server happens to be running: a page opened over `file://` sends
`Origin: null` and is refused by the API regardless, so a user with the
server running but a `file://` bookmark gets the old behaviour. That is the
correct outcome — it just means "open it from the server" is the instruction,
and the served index page should be the thing people bookmark.

### The anchor problem

Message anchors in generated HTML are DAG-slot ids (`id="msg-d-42"`), which
are **per-render artifacts** — they can shift when a session gains messages.
The cache knows messages by `_uuid` (stable). To deep-link search results to
the exact message, pick one:

1. **Emit UUID anchors in the HTML** — add `data-uuid="…"` (or a second `id`)
   to each message card at render time. Small, static-friendly; also useful
   for annotations later (stable target ids). Costs a few bytes per message.
2. **Server-side uuid→slot mapping** — resolve at query time by re-walking
   the DAG; fragile and couples the server to renderer internals.
3. **Link to session page + client-side find** — pass `?highlight=<term>`
   and let the in-page search locate it; imprecise with repeated terms.

**Option 1 is implemented** (`transcript.html:184` emits `data-uuid` on every
message card that has a transcript UUID): the in-page search already uses it
to keep the current match pinned across option/filter re-searches. Deep links
from the server resolve it client-side
(`document.querySelector('[data-uuid="…"]')` → scroll + reveal), since
`data-uuid` is an attribute, not an `id` — or we add a second `id` if plain
fragment URLs turn out to matter.

Verified end-to-end in a browser (Q6): ~10 lines reading `?uuid=` and calling
the existing `window.claudeLogRevealMessage` hook land the target on screen
in 100–520 ms with no page errors. Three constraints the measurement added:

1. **Pass `&q=` as well as `?uuid=`.** In 5 of 8 samples the matched text sat
   inside a *closed* `<details>` inside the card; the in-page search already
   opens `<details>` on the path to every highlight, so handing it the query
   both highlights the term and uncollapses it. `uuid` alone gets you to the
   card, not to the match.
2. **`data-uuid` is not unique per card.** One transcript entry can render as
   several sibling cards (text + tool_use) sharing the entry UUID — the
   template says so explicitly. `querySelector` returning the first is the
   right behaviour for a deep link; just don't treat it as a per-card id.
3. **Not every indexed message has a card.** `progress` (skip — no text),
   `ai-title` and `queue-operation` (index, but link at session
   granularity), and ~89% of `attachment`/`system` entries. The API should
   return the message uuid only when it is expected to resolve, so the
   frontend can fall back to a plain session link rather than scrolling
   nowhere.

The existing on-load handler only understands `#msg-<slot-id>` fragments
(`transcript.html:1032` `unfoldAncestorsOf`), which are positional and
therefore unusable from the server. The uuid branch is a small addition
inside the same closure, reusing `revealMessage`.

## Part 4: Extensibility for tagging / notes / comments

### Durability: not in the cache DB

The cache is **disposable by design** — schema-version bumps and manual
deletion rebuild it from JSONL. User-authored annotations must survive that,
so they live in a separate DB (`claude-code-log-annotations.db` next to the
cache), keyed by **stable identifiers**: `project_path`, `session_id`,
message `_uuid`. Sketch:

```sql
CREATE TABLE annotations (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    kind TEXT NOT NULL,              -- 'tag' | 'note' | 'comment'
    body TEXT NOT NULL,              -- tag name, or markdown body
    -- target: exactly one granularity, stable across cache rebuilds
    project_path TEXT NOT NULL,
    session_id TEXT,                 -- NULL = project-level
    message_uuid TEXT                -- NULL = session-level
);
```

Annotations can then surface two ways (later): baked into HTML at generation
time (visible on `file://` too, read-only) and/or as a runtime overlay
fetched from the API (live, editable — only when served).

### API sketch

Everything under `/api/`, JSON, no versioning ceremony until we need it:

```
GET  /api/ping                     → {"ok": true, "version": "1.5.0",
                                      "index": {"ready": true, "messages": 532728}}
GET  /api/search?q=…&project=…&session=…&type=…&fields=…&from=…&to=…&limit=…&offset=…
     → {"results": [{project, session_id, message_uuid, timestamp, type,
                     field, snippet, link}, …], "total": n}
     -- fields=text,thinking,meta   → phase 1  (FTS5 column filter)
     -- fields=tool_input           → phase 2
     -- fields=tool_result,attachment → phase 3
     -- link: "{slug}/session-{id}.html?uuid={uuid}&q={terms}", and omits
     --       uuid for types that never render a card (Q6)
     -- field: which column matched, for the result card
GET  /api/projects                 → project list with aggregates (from cache)
GET  /api/projects/{slug}/sessions → session list (from cache)

-- later, annotations:
GET  /api/annotations?project=…&session=…
POST /api/annotations              {kind, body, project_path, session_id?, message_uuid?}
PATCH/DELETE /api/annotations/{id}
```

Even with "no auth" as a stated non-goal, two cheap safeguards are worth it
once write endpoints exist: bind to loopback only, and validate the `Host`
header (blocks DNS-rebinding pages from driving the API) — a random page you
visit *can* fire cross-origin POSTs at `localhost` otherwise.

## Phasing

0. **Phase 0a — Click group conversion, on its own.** The riskiest part of
   Phase 0 and entirely separable: a `Group` subclass whose `resolve_command`
   falls back to the default command. Lands as the **first commit of the
   serve PR**, with a test matrix over bare / positional / option-first
   invocations. Whatever else that shakes out as needing regression cover, we
   add before moving on.
1. **Phase 0b — `serve` command:** static serving of the projects directory
   over loopback + `/api/ping`, `Host` validation, `BrokenPipeError`
   suppression, startup freshness check (decision 5). Immediately useful (no
   more `file://` quirks), tiny.
2. **Phase 1 — archive search:**
   - `008_message_fts.sql` migration (contentless, six columns,
     `contentless_delete=1`).
   - Text extractor over the content JSON — known shapes only, base64
     excluded, `progress` skipped. This is the piece most worth unit-testing
     against `test/test_data/`, since silent extraction bugs cost 28% index
     size before anyone notices.
   - Backfill with progress at first `serve`; incremental hook on the
     existing invalidation path.
   - `/api/search` — escaped MATCH, `CROSS JOIN`, Python snippets, `fields=`
     and `project=` filters. Regression test asserting the query plan keeps
     FTS as the outer loop.
   - `?uuid=&q=` deep-link handler in `transcript.html` (~10 lines, reusing
     `revealMessage`) — the only change to existing generated pages.
   - New `search.html` page (own JS, reused styles + result cards), URL
     params, `file://` degradation.
   - "Search all projects" link on the index page, and on combined
     transcript pages pre-filtered to their project. The existing index-page
     search box is **not** modified.
3. **Phase 2 — annotations:** separate DB, CRUD endpoints, minimal UI overlay
   on transcript pages (tag chips + note box), render-time baking optional.

**Not now (Part 4 is design-constraint only):** the annotations DB shape is
recorded so Phase 1's API and stable-id choices don't preclude it — it is not
in the build.

## Decisions

1. **CLI shape — subcommand.** `claude-code-log serve`. The current CLI is one
   flat `@click.command()` with ~30 options (`cli.py:800`), so the conversion
   needs a `click.Group` subclass whose `resolve_command` falls back to the
   default command when `argv[0]` isn't a known subcommand — that keeps
   `claude-code-log`, `claude-code-log path/to.jsonl` and
   `claude-code-log --from-date …` working untouched. Worth a test matrix over
   the bare/positional/option-first invocations.

2. **Index build — lazily at first `serve`, then incrementally forever.** The
   measurements make this cheaper than the draft assumed: a full backfill is
   **31 s once**, and steady-state maintenance is **~180 ms for the largest
   file in an 8 GB archive**, so "taxes every run" isn't a real cost. So:
   migration creates the table empty; first `serve` backfills with progress;
   the normal cache-write path keeps it fresh from then on. An explicit
   `serve --reindex` for recovery.

   *Progress UI:* yes — 31 s is too long to be silent. Reuse the existing
   conversion progress reporting rather than inventing one.

   *On folding it into a background task behind the index page* (your
   suggestion, and the downsides you asked about):
   - **SQLite has one writer.** A background backfill contends with the
     converter writing the same DB. WAL (already enabled, `cache.py:360`)
     lets readers through, but two writers will hit `SQLITE_BUSY`. Ordering
     the backfill *after* conversion avoids this entirely.
   - **Partial index = wrong answers, not slow answers.** A search during
     backfill silently returns a subset. That needs the UI to say
     "indexing, 43%" — which is more frontend work than a 31 s startup bar,
     for a one-time cost.
   - **Recommendation:** block at first `serve` with a progress bar. Revisit
     backgrounding only if someone's archive makes 31 s feel like minutes.

3. **Search semantics — accept the difference, separate box.** As you said:
   they complement each other. In-page search stays substring/regex over the
   loaded DOM; archive search is FTS token/prefix/phrase. Two visibly distinct
   entry points, documented, no attempt to unify. `regex=true` (Option 3,
   ~20 s/query) deferred past Phase 1 and gated behind an explicit "slow
   search" affordance if it lands at all.

4. ~~**UUID anchors**~~ **Decided & done:** `data-uuid` is emitted on message
   cards (see "The anchor problem"). Link format
   `session-{id}.html?uuid={uuid}&q={terms}`.

5. **Staleness while serving — startup check, like the TUI.** Mirror
   `tui.py:135`'s `ensure_fresh_cache(..., silent=True)` at server start, plus
   the `html_cache` freshness check from #271 per page request, regenerating
   that one session on demand. Per-request checking is cheap (an indexed
   lookup) and avoids re-converting an 8 GB archive to fix one stale page.
   Watchers/inotify stay out of scope.

6. **Search fields — index all six groups; `tool_result` off by default,
   every group configurable.** All groups cost 253 MB together vs 94 MB
   without `tool_result`; the extra 159 MB buys the 69% of the archive where
   tool output lives, which is useful but noisy — and noise is a *filtering*
   problem, not an indexing one, so it gets indexed and filtered rather than
   dropped.

   **Default search scope excludes `tool_result`.** Each group is
   individually configurable by flag and env var, in the usual precedence
   (flag > env > default):

   ```
   --search-fields text,thinking,tool_input,attachment,meta   (default; note: no tool_result)
   --search-fields all | none | +tool_result | -thinking      (additive/subtractive forms)
   CLAUDE_CODE_LOG_SEARCH_FIELDS=text,thinking,tool_result
   ```

   and per-request via `fields=` on `/api/search`, so the UI toggle and the
   configured default are the same mechanism. A group being *indexed* and a
   group being *searched by default* stay separate concepts — an
   `--index-fields` counterpart controls the former for anyone who wants the
   94 MB index instead of 253 MB.

   *Phased/streaming results* (your idea, and it works): FTS5 column filters
   make each phase genuinely cheaper, not just reordered —
   `{text thinking meta} : q` is 88 ms where the unrestricted query is 153 ms
   on the pathological term, and ~1 ms on normal ones. Phase 1
   `{text thinking meta}`, phase 2 `{tool_input}`, phase 3
   `{tool_result attachment}`, each streamed as it lands. At 1–6 ms per phase
   for realistic queries this is a UX nicety, not a necessity — worth building
   the API to allow it (a `fields=` parameter is enough) and only wiring the
   streaming UI if it proves useful.

7. **Scope — one projects dir per server; search across all its projects.**
   One cache DB, one server, exactly as the converter is invoked. Search spans
   every project in that directory by default; `project=` filters, and a page
   served from within a project defaults that filter to its own project with
   a visible, overridable control. Cross-archive federation stays a non-goal.

   Implementation note: don't resolve a result's directory from
   `projects.project_path` — it is an absolute path to the archive's original
   location (Q7). Join the served root with `Path(project_path).name`.

---

## Implementation diary

Built in one session on `feat/archive-search-server`, six commits, phases in
order. What follows is what the plan got right, what it got wrong, and what
only showed up once the code met the real 8 GB archive.

### Commits

| | |
|---|---|
| `43045af` | Click group with `convert` as the default command (Phase 0a) |
| `019fb66` | `serve`: loopback static server + `/api/ping` (Phase 0b) |
| `f7a2977` | Search core: extraction, FTS5 index, queries |
| `0562976` | `/api/search` and the `serve` wiring |
| `77a82b5` | `search.html`, index link, uuid deep links |
| `64c44a1` | Reindex files the cache has rewritten |

### The plan held up

Everything the measurement phase established survived contact with the code.
Contentless FTS5 landed at **+263 MB on a 1,288 MB cache (+20%)**, essentially
the predicted 253 MB. The production extractor reproduced the prototype's
585.9 MB of text exactly. `CROSS JOIN` kept project-filtered search at 2.4 ms.
The `?uuid=&q=` deep link worked first try in a browser.

Doing the measurement before the design was worth more than the design itself:
three of the four bugs below were *variants* of problems already found by
measuring, caught quickly because the shape was familiar.

### Four bugs, none of which fail loudly

Every real bug in this build degraded behaviour silently. None threw.

1. **Orphaned index rows.** `_delete_file_rows` originally derived the FTS
   rowids to delete from `messages WHERE file_id = ?`. But both delete paths
   run *after* the messages are gone — the cache replaces a file's rows before
   reindex, and a removed file has none left — so it deleted nothing in
   exactly the cases that matter. Fixed with a `search_indexed_rows` mapping,
   which also makes the index self-contained. Caught by
   `test_removing_a_file_removes_its_rows`.

2. **`/api/search` at 2.6 seconds.** The search core was 2.4 ms, but every
   request called `index_status`, which counted rows in `messages`. Because
   the content BLOB is stored inline, that full scan reads ~930 MB. Same root
   cause as the `CROSS JOIN` finding — the inline BLOB makes any full scan of
   `messages` expensive — but in a place the plan never considered. Both
   counts now come from small aggregate tables, and the request path uses a
   cheap `index_ready` check. **2.6 s → 2.4 ms.**

3. **Stale results for changed files.** `ensure_index` skipped any file id it
   had already seen, so a *modified* transcript kept its old rows: a rewrite
   preserves `file_id`, making "already indexed" the wrong question. Fixed by
   recording and comparing `cached_files.cached_mtime`. This one was found by
   writing the test, not by running the feature.

4. **Misreported totals under a filter.** `total` is deliberately the
   index-wide match count (cheap even at 219k hits). Quoting it next to
   filtered results produced "Showing 5 of 102 matches" describing two
   different result sets. Now only shown unfiltered. Found in the browser.

The pattern is consistent enough to be worth stating: **this feature's
failure mode is silence.** Which is why three tests assert things no user
would ever see — a query plan, the statements a request issues, and that a
no-op reindex writes nothing.

### Things the plan didn't anticipate

- **Deep-link ordering.** The plan had `?uuid=` and `&q=` as independent
  halves. They interact: `performSearch` filters and unfolds, which moves the
  layout, so a scroll performed first lands somewhere else. The search
  component now scrolls *after* searching, and `transcript.html` defers when
  `q` is present. `openSearch()` also grew `{focus: false}` — focusing the
  input scrolls the page and fought the scroll-to-message.
- **`--help` on a Click group.** Routing it to `convert` keeps the familiar
  option list but makes the group's own help unreachable, so subcommands are
  advertised through `convert`'s epilog — generated from `group.commands` so
  it can't drift.
- **Progress reporting granularity.** Progress is per *file* (3,048 of them),
  not per message. Simpler, and it matches the unit of invalidation.

### Deviations from the plan

- **No `008_message_fts.sql` migration.** The plan had one. If FTS5 were
  missing, a failing migration would break the whole cache for a feature the
  user may never use. The index is created lazily from Python behind a
  feature check instead, and versioned through its own meta table. It is
  derived data — droppable and rebuildable — so the schema version doesn't
  need to track it.
- **`total` is index-wide, not filter-aware.** Counting through a filter is
  not cheap; the frontend compensates by only quoting the number when nothing
  is filtered.
- **Phased/streaming search not wired up.** The API takes `fields=` and the
  column filters are measurably cheaper, so the mechanism is there. At 1–6 ms
  per query the UI would be solving a problem nobody has. Deliberately left.

### Numbers, as built

| | |
|---|---|
| Archive | 532,728 messages / 1,713 sessions / 77 projects / 1,288 MB cache |
| Indexed | 477,039 rows (`progress` skipped) |
| Index size | +263 MB (+20%) |
| Full build | ~50 s, once |
| No-op restart | 6 ms |
| Typical query, through HTTP | 1.4–3.8 ms |
| Stopword (`the`) | 112 ms |
| Project-filtered | 2.4 ms |
| Search-as-you-type, in browser | 243 ms to first results |

### Follow-up: both gaps closed

**The cache write path now maintains the index.** `save_cached_entries`
calls `reindex_files(..., commit=False)` inside its own transaction, so an
ordinary `claude-code-log` run keeps an existing index current and a file's
stale rows leave with its stale messages instead of waiting for the next
server start. It is a no-op (one `sqlite_master` lookup) for anyone who has
never built an index.

Measured cost on a re-conversion where *every* file changed — the worst
case, since normally only a few do: **3.3 s → 4.1 s (+25%)** over 7,516
messages. `CLAUDE_CODE_LOG_SEARCH_AUTO_INDEX=0` opts out. The gate lives at
the call site, not inside `reindex_files`, so it disables the automatic
hook without silently disabling explicit calls.

Considered and rejected: extracting from the in-memory entries that
`_serialize_entry` already has, to avoid re-decompressing what was just
compressed. It would need the AUTOINCREMENT rowids of a just-inserted batch,
which means inferring ids from insertion order — a correctness risk for
maybe 40% of an already-small overhead.

**The "pre-existing flake" was a real test bug, not parallelism.** It
reproduced roughly one run in six *in isolation*, which is what had it
misfiled. The cache treats a file as unchanged within a 1.0 s mtime
tolerance, and the tests forced a change with `time.sleep(1.1)` — 0.1 s of
margin. On this VM a 1.1 s sleep was measured producing an mtime delta of
**0.917 s**: the filesystem's timestamp source drifts from the monotonic
clock `sleep` uses, so the cache saw the file as fresh.

Fixed with a `bump_mtime` helper in `conftest.py` that sets the mtime
explicitly — deterministic, instant, and it states the intent instead of
hoping a sleep clears a threshold. Nine sites across three files had the
same latent bug (`test_cache_sqlite_integrity.py`, `test_cache.py`,
`test_html_regeneration.py`), and ~9.9 s of sleeping is gone from the suite.

Removing those sleeps then exposed a *second* use they had been quietly
serving. Several regeneration tests assert `output.mtime > original_mtime`
to mean "this file was rewritten"; with the 1.1 s cushion gone, the same
backwards clock jitter made a regenerated file land **16 ms earlier** than
the original. The sleeps had been masking a false assumption — that a
rewrite always produces a later timestamp — rather than preventing a race.
Those seven assertions now use `!=`, which is what the tests actually mean:
an untouched file keeps its mtime exactly, a rewritten one does not.

15/15 consecutive clean runs of the three files afterwards.

### Follow-up: implicit prefix search

Typing `tokeni` and getting nothing until you add `*` reads as a broken
search box, so unquoted words are now matched as prefixes by default
(`fts_escape`), with `"quoted"` as the opt-out. The question was what it
costs: a prefix query merges the doclists of *every* indexed term under the
prefix, and `ORDER BY rank` scores all of them — there is no early exit.

Measured on a synthetic index at real-archive scale (532,728 rows, 164 MB
— the local dev archive is 1,260 messages, so the local text was replicated
to that row count with per-row unique tokens to grow the term index too).
Top-20 plus the `count(*)` both a search does:

| prefix | total | hits | | whole word | total |
|---|---:|---:|---|---|---:|
| `r*` | 389 ms | 284,022 | | `re` | 14 ms |
| `t*` | 501 ms | 283,703 | | | |
| `co*` | 217 ms | 137,969 | | `co` | 0.6 ms |
| `ren*` | 20 ms | 19,452 | | `the` | 56 ms |
| `the*` | 78 ms | 68,914 | | `cache` | 34 ms |
| `render*` | 19 ms | 19,444 | | `test` | 32 ms |

So the answer is length-dependent, and sharply: **3 characters is where it
stops mattering.** From there up the worst case is ~110 ms and the typical
one 20–40 ms, against 4–50 ms for the same words matched whole — under the
200 ms input debounce either way. At one or two characters it is 100–500 ms,
worse than the pathological stopword query this design already tolerates
(153 ms), and it fires on the way to *every* longer query as the user types.

Hence `IMPLICIT_PREFIX_MIN_LENGTH = 3`: shorter words are matched whole,
which is cheap and no less useful than a result list covering a third of the
archive would have been. An explicit `*` is still honoured at any length —
if you ask for `s*`, that's your call.

Two smaller consequences:

- The excerpt highlighter mirrors the rule, extending a highlight to the end
  of the word it matched (`render` inside `renderer`) — but stopping at
  non-alphanumerics, because the tokenizer splits there too (`cache` matches
  `cache_manager`, and highlighting past the underscore overstates it).
- `"vis-timeline"` now escapes to `"vis-timeline"*`: a phrase whose *last*
  token is a prefix, which is what FTS5's phrase-star means and what the
  user typing it wants.

### Known gaps

- **`/api/search` has no `regex=` escape hatch.** Deferred as planned.
- **Phased/streaming search is not wired up.** The `fields=` mechanism is
  there; the UI would be solving a problem nobody has at 1–6 ms per query.

# Full-Archive Search via a Local Web Server — Design Draft

Status: **first draft for discussion** (see [Open decisions](#open-decisions))

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

## Part 1: The server

### Option A — `http.server.ThreadingHTTPServer` + small router *(recommended)*

A `BaseHTTPRequestHandler` subclass with a tiny route table: paths starting
with `/api/` dispatch to JSON handlers, everything else falls through to
static file serving rooted at the projects directory
(`SimpleHTTPRequestHandler` machinery or a hand-rolled safe-path resolver).

- ~150 lines for routing + static serving; threading handles the browser's
  parallel asset requests.
- SQLite + threads: open one connection per request (cheap, WAL mode), or a
  `threading.local` connection. The existing `cache.py` context-manager
  pattern fits.

### Option B — WSGI app via `wsgiref`

A plain WSGI callable served by `wsgiref.simple_server` (subclassed with
`ThreadingMixIn` — `wsgiref` is single-threaded out of the box).

- Pro: the app is directly callable in tests (no socket needed); trivially
  portable to a production server later.
- Con: slightly more plumbing for the same result, and "portable to
  production" is explicitly a non-goal here.

**Recommendation:** A. We're never going to deploy this; test coverage can
use a live server on an ephemeral port (browser tests will do that anyway,
mirroring `test_timeline_browser.py`).

### CLI integration

`claude-code-log serve [--port 8010] [--open-browser]` (or `--serve` flag on
the main command — see open decisions). Runs the normal conversion first so
the static HTML exists and the cache is fresh, then serves the projects
directory.

## Part 2: The search index

### Option 1 — FTS5 external-content table *(recommended)*

```sql
CREATE VIRTUAL TABLE message_fts USING fts5(
    text,                         -- extracted plain text
    content='',                   -- contentless: we keep our own mapping
    tokenize='unicode61'
);
-- rowid == messages.id, so joining back to messages is free
```

At cache-write time (or lazy backfill, below) extract the searchable plain
text from each message's content JSON — text blocks, thinking, tool inputs,
tool results — the same fields the in-page search sees via `textContent`.
Insert with `rowid = messages.id`.

- Query: `SELECT rowid, snippet(message_fts, 0, '<mark>', '</mark>', '…', 12)
  FROM message_fts WHERE message_fts MATCH ? ORDER BY rank` — ranking and
  excerpts come free.
- Index size: roughly comparable to the compressed content (text × ~50%);
  acceptable for a local cache.
- Incremental: the cache already knows which files changed; FTS rows are
  deleted/reinserted alongside the `messages` rows they mirror (same
  invalidation path, `INSERT ... VALUES('delete', ...)` for contentless
  tables — or simplest, use an *external content* table pointing at a real
  `message_text` column, see Option 2 hybrid).
- Caveats: FTS5 `MATCH` syntax is token-based — no regex, and substring
  matching needs `token*` prefixes. Phrase/boolean queries come free.

**Backfill strategy:** a migration (`008_message_fts.sql`) creates the table;
population happens lazily the first time the server starts (with a progress
line), not during normal HTML generation — keeps `claude-code-log`'s default
run fast, and only server users pay the cost.

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

**Recommendation:** Option 1, with Option 3 kept in mind as the later
`regex=true` query parameter implementation. If FTS5 turns out to be missing
on some platform, fall back to Option 3 rather than maintaining Option 2's
extra column.

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

### Frontend: progressive enhancement of the existing pages

The generated `index.html` already has a search box with a results panel. The
enhancement path that keeps one UI:

- On load, the page pings `/api/ping` (only succeeds when served over
  `http://`; on `file://` the fetch fails instantly and silently).
- When the API is present, the index-page search upgrades to archive-wide
  search: same input, same results panel, but results come from `/api/search`
  (grouped by project/session, with FTS snippets) instead of the DOM index.
- Transcript pages could similarly offer "search whole archive for this term"
  from the existing toolbar.

No separate app, no duplicated styling, zero change for `file://` users.

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

**Option 1 is implemented** (`transcript.html` emits `data-uuid` on every
message card that has a transcript UUID): the in-page search already uses it
to keep the current match pinned across option/filter re-searches. Deep links
from the server would resolve it client-side
(`document.querySelector('[data-uuid="…"]')` → scroll + reveal), since
`data-uuid` is an attribute, not an `id` — or we add a second `id` if plain
fragment URLs turn out to matter.

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
GET  /api/ping                     → {"ok": true, "version": "1.5.0"}
GET  /api/search?q=…&project=…&session=…&type=…&from=…&to=…&limit=…&offset=…
     → {"results": [{project, session_id, message_uuid, timestamp, type,
                     snippet, link}, …], "total": n}
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

1. **Phase 0 — `serve` command:** static serving of the projects directory
   over loopback + `/api/ping`. Immediately useful (no more `file://`
   quirks), tiny.
2. **Phase 1 — archive search:** FTS migration + lazy backfill, `/api/search`,
   progressive enhancement of the index-page search box. UUID anchors in
   generated HTML for deep links.
3. **Phase 2 — annotations:** separate DB, CRUD endpoints, minimal UI overlay
   on transcript pages (tag chips + note box), render-time baking optional.

## Open decisions

1. **CLI shape:** subcommand (`claude-code-log serve`) vs flag (`--serve`)?
   Subcommand reads better and leaves room for `serve --no-convert`, but the
   CLI is currently flat (single Click command) — converting to a group would need
   backward-compat care for the default invocation.
2. **When does the FTS index build?** Lazily at first server start
   (recommended above) vs during every cache write (keeps it always-fresh but
   taxes every `claude-code-log` run) vs explicit `--index` step.
3. **Search semantics parity:** in-page search is substring + optional regex;
   FTS5 is token/prefix-based. Accept the difference (documented), or add the
   slow Python-scan `regex=true` path in Phase 1 rather than later?
4. ~~**UUID anchors:** agree to emit them at render time (HTML size cost ~30
   bytes/message) — decides the deep-link story for both search and
   annotations.~~ **Decided & done:** `data-uuid` is emitted on message cards
   (see "The anchor problem" above).
5. **Staleness policy while serving:** convert once at startup only, or check
   `html_cache` freshness on each page request and regenerate on demand?
   (Watchers/inotify feel out of scope for stdlib-only.)
6. **Scope of search fields:** content text only, or also tool inputs/outputs
   (they dominate the byte count — searching them is very useful but noisy;
   maybe an FTS column per field group so the API can filter: `text`,
   `thinking`, `tools`)?
7. **One server for multiple project directories?** The cache is per projects
   dir; presumably the server takes the same path arguments as the converter
   and serves whatever it converted. Multi-archive federation = non-goal?

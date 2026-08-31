# Application Model

`claude-code-log` reads Claude Code transcript files (JSONL on disk) and
produces readable HTML, Markdown, and structured JSON views, with
optional caching, a TUI for navigation, and per-project aggregate
pages.

This document is the entry point for `dev-docs/`: a high-level view of
the parts, what each does, and where to read about them in detail. For
end-user documentation see the project [`README.md`](../README.md);
for contributor onboarding see [`CONTRIBUTING.md`](../CONTRIBUTING.md);
for user-facing operations docs see [`docs/`](../docs/).

---

## 1. Subsystems at a glance

| Subsystem | Owner module(s) | Deep-dive |
|---|---|---|
| CLI | [`cli.py`](../claude_code_log/cli.py) | inlined below (§ 2.1) |
| TUI | [`tui.py`](../claude_code_log/tui.py) | inlined below (§ 2.2) |
| Cache (SQLite) | [`cache.py`](../claude_code_log/cache.py) + [`migrations/`](../claude_code_log/migrations/) | inlined below (§ 2.3); user-facing in [`docs/restoring-archived-sessions.md`](../docs/restoring-archived-sessions.md) |
| Migrations | [`migrations/`](../claude_code_log/migrations/) + `migrations/runner.py` | inlined below (§ 2.4) |
| Parsing | [`parser.py`](../claude_code_log/parser.py), [`factories/`](../claude_code_log/factories/) | [rendering-architecture.md § 3](rendering-architecture.md) |
| Message taxonomy | [`models.py`](../claude_code_log/models.py) | [messages.md](messages.md) |
| DAG (sessions, forks, agents) | [`dag.py`](../claude_code_log/dag.py) | [dag.md](dag.md) |
| Sync sub-agents (#79) | [`converter.py`](../claude_code_log/converter.py), `factories/agent_metadata_factory.py` | [agents.md § 1](agents.md) |
| Async task agents (#90) | `converter.py`, `factories/task_notification_factory.py` | [agents.md § 2](agents.md) |
| Teammates (#91) | `renderer.py`, `factories/teammate_factory.py`, `html/teammate_formatter.py` | [teammates.md](teammates.md) |
| Dynamic workflows (#174) | [`workflow.py`](../claude_code_log/workflow.py), `converter.py`, `renderer.py` | [workflows.md](workflows.md) |
| Rendering pipeline | [`renderer.py`](../claude_code_log/renderer.py), `html/`, `markdown/`, `json/` | [rendering-architecture.md](rendering-architecture.md) |
| Fold-bar / message hierarchy | `html/templates/components/`, JS in `transcript.html` | [message-hierarchy.md](message-hierarchy.md) |
| CSS class taxonomy | `html/templates/components/*.css` | [css-classes.md](css-classes.md) |
| JSON export (#36) | [`json/`](../claude_code_log/json/) | inlined below (§ 2.5) |
| Depth filter | renderer.py § Depth filtering, `models.RenderingDepth` | inlined below (§ 2.6) |
| Image export | [`image_export.py`](../claude_code_log/image_export.py) | inlined below (§ 2.7) |
| Performance profiling | [`renderer_timings.py`](../claude_code_log/renderer_timings.py) | inlined below (§ 2.8) |
| Intra-project render fan-out | [`render_pool.py`](../claude_code_log/render_pool.py) (mechanism) + [`render_dispatch.py`](../claude_code_log/render_dispatch.py) (policy) | inlined below (§ 2.10) |
| Diagnosing hangs (SIGUSR1) | [`cli.py`](../claude_code_log/cli.py) `_install_stack_dump_signal` | inlined below (§ 2.11) |
| Watch mode / live page updates | [`watch.py`](../claude_code_log/watch.py), `html/templates/components/live_update.js` | inlined below (§ 2.15); design in [`work/watch-mode.md`](../work/watch-mode.md); user-facing in [`docs/live-updates.md`](../docs/live-updates.md) |
| Adding a new tool renderer | [`factories/tool_factory.py`](../claude_code_log/factories/tool_factory.py), `html/tool_formatters.py` | [implementing-a-tool-renderer.md](implementing-a-tool-renderer.md) (how-to) |
| Which tools have a specialized renderer or provider adapter | `TOOL_INPUT_MODELS` / `TOOL_OUTPUT_PARSERS` in [`factories/tool_factory.py`](../claude_code_log/factories/tool_factory.py), plus provider adapters | [tools-coverage.md](tools-coverage.md) (Claude and Codex status vs. upstream references) |
| Plugin system (third-party message transformers) | [`plugins.py`](../claude_code_log/plugins.py), [`factories/priorities.py`](../claude_code_log/factories/priorities.py), `Renderer._dispatch_format` | [plugins.md](plugins.md) |

A note on cross-cutting concerns: some behaviour spans several rows
of the table above and isn't owned by any single subsystem. **Label
and preview composition** (session header titles, branch labels,
fork-point box captions) is the most common one — it touches the
DAG layer (which decides what's a branch), the renderer's session
machinery (which assembles the label text), and the parsing layer
(which feeds the preview source). See the `SessionHeaderMessage`
entry in § 4 for the function-level surface.

---

## 2. Subsystems without their own deep-dive

The subsystems above with "inlined below" pointers don't have a
dedicated dev-doc — the paragraph here is the canonical reference.

### 2.1 CLI

[`cli.py`](../claude_code_log/cli.py) is the command-line entry point
(`claude-code-log`) built on Click. The default invocation processes
the entire `~/.claude/projects/` hierarchy; explicit paths target a
single transcript or directory. Major flags:

- `--tui` — launch the interactive TUI (§ 2.2).
- `--depth {session,user,assistant,agent,tool,hook}` (default `tool`) —
  how deep into the message hierarchy to render (§ 2.6). Legacy
  `--detail {full,high,low,minimal,user-only}` is a deprecated alias.
- `--from-date "yesterday"`, `--to-date "today"` — natural-language
  date filtering via `dateparser`.
- `--open-browser` — open the generated `index.html` after rendering.
- `--no-cache` / `--update-cache` — bypass or force-refresh the
  SQLite cache (§ 2.3).
- `--format {html,md,markdown,json}` — switch output format (HTML is
  the default; Markdown is mainly used for sharing transcripts inline;
  JSON exports the processed tree for downstream tooling — see § 2.5).
- `--compact` — Markdown-only; suppresses repeated headings.
- `--page-size N` — paginate the combined-transcript HTML/Markdown
  output, packing whole sessions into pages of up to N messages each
  (sessions are never split across pages, so individual pages may
  overflow). Per-session HTML files are not paginated.
- `--jobs N` / `-j N` — worker processes for the all-projects conversion
  phase (default: CPU count; `1` disables parallelism). Also caps the
  per-project render fan-out, which is opt-in via
  `$CLAUDE_CODE_LOG_RENDER_JOBS` (§ 2.10).

CLI orchestration delegates to `converter.py` (which owns the
high-level "load + render + write" flow) and never touches `renderer.py`
directly. Output paths follow a stable convention so the cache and
re-renders can find existing files: `combined_transcripts.html`,
`session-{id}.html`, `index.html`, with `--depth` and `--compact`
adding suffixes per `utils.variant_suffix`.

For the all-projects invocation, `process_projects_hierarchy` runs in
three phases: **plan** (sequential, cheap — per-project staleness via
cache mtimes, also ensures DB schema/migrations exist), **execute**
(stale projects fan out over a `ProcessPoolExecutor` with the `spawn`
start method, largest-first; rendering is CPU-bound pure Python and
projects are independent, so this scales near-linearly with cores; when
the per-project render fan-out is enabled, any budget left over after one
worker per stale project is handed to it, § 2.10), and **collect**
(sequential — per-project index data is read back from the cache and the
cross-project `index.html` is written last). Workers
run silent; the parent prints one progress line per project as results
arrive. All workers share the WAL-mode SQLite cache DB — each writes
only its own project's rows, and WAL serialises the short write
transactions. A pool-level failure (e.g. a library caller without the
`if __name__ == "__main__"` guard `spawn` needs) degrades to inline
sequential processing with a warning rather than aborting.

### 2.2 TUI

[`tui.py`](../claude_code_log/tui.py) is a Textual application that
browses the projects index, drills into individual sessions, and
exposes quick actions: render session to HTML, resume a session via
`claude --resume`, archive a session (move to cache-only), and so on.

Architecture is straightforward Textual: a few `Screen` subclasses,
a `DataTable` for the session list, key bindings dispatched through
Textual's `BINDINGS` mechanism. The TUI reads through `cache.py`
exclusively (never re-parses JSONL itself) — opening a 50-project
hierarchy takes milliseconds because cache hydration is incremental.

The "archive" action is interesting: it moves a session's source JSONL
out of `~/.claude/projects/` while keeping the cache row intact. The
session then renders from cache only. See
[`docs/restoring-archived-sessions.md`](../docs/restoring-archived-sessions.md)
for the user-facing behaviour and recovery flow.

### 2.3 Cache (SQLite)

[`cache.py`](../claude_code_log/cache.py) maintains a SQLite database
at `~/.claude/projects/claude-code-log-cache.db` (or
`$CLAUDE_CODE_LOG_CACHE_PATH`). Stored data:

- Per-session: id, summary, first/last timestamps, message count,
  per-role token totals, `team_name` (added in migration 005).
- Per-message: a denormalised view used by archived-session
  restoration (the cache holds enough to re-render even after the
  source JSONL is deleted). Each row's `content` is the entry as
  zlib-compressed JSON, written at `CONTENT_COMPRESSION_LEVEL` (3, not
  zlib's default 6: levels only diverge on large payloads, so across a
  real 18,288-row archive it costs 2.6% more bytes and makes a cold
  conversion 11% faster — compressing rows is the largest item in a
  watch tick). Reading is level-agnostic, so rows written at any level
  still load.
- Per-rendered-HTML: the HTML output itself, indexed by source file
  mtime + depth + compact flag (migrations 002–004) — so
  re-runs with unchanged inputs serve the cached HTML directly.
- Cross-session sidecar (migration 008): compact projections of a full
  load's `SessionTree` — per-session parent linkage, junction points,
  and cross-session dedup winners — persisted so the session-scoped
  incremental path (§ 2.12) can regenerate one stale session without
  loading the project. Rewritten wholesale on every full directory
  load, inside the same batch as the per-file writes, so it is exactly
  as fresh as `cached_files`.

Invalidation is mtime-based: when a JSONL's mtime is newer than its
cache row, the session is reparsed. The schema-version row also
invalidates the entire HTML cache when migrations bump the version,
since rendered output may have changed even when source data hasn't.

Paginated output carries an extra invalidation axis. `--page-size`
assigns sessions to pages chronologically, and that assignment is
recomputed from scratch on every run, so a page's *membership* can
change while every session it already held is untouched: a new session
lands on the partly-filled last page, or sessions imported from another
machine sort into the middle by their original timestamps and shift
everything after them along. Per-session freshness alone can't see
this, so `is_page_stale()` also compares the run's computed session
list against the cached `page_sessions` rows and reports
`sessions_changed`. Without that comparison the affected pages report
`up_to_date`, and the new sessions land in the cache and in their own
`session-*.html` but never in any combined page (issue #308).

Connections run in WAL mode with `synchronous=NORMAL` (durable across
app crashes; only a power/OS crash can lose the last commit — fine for a
regenerable cache). By default `_get_connection()` opens and closes a
connection per call, so no file handle lingers to block temp-dir cleanup
on Windows. A build issues ~190 such opens, which dominates cache-build
cost, so the converter wraps its hotspots (`ensure_fresh_cache`, the
per-file load loop, per-session generation) in `CacheManager.batch()`:
one shared connection reused for the scope and closed on exit (including
on exception). `batch()` nesting is a no-op reuse, so the wraps compose.

Freshness checks are batched too (issue #12): `get_modified_files()`
fetches every cached row for the project in one query (one connection
open, or zero extra inside a `batch()` scope) and rules out
subagent-sidecar fingerprints with one scandir per parent directory,
instead of a per-file query + `is_dir()` probes. This is what makes
TUI startup near-instant on multi-thousand-file archives.

For the operations / recovery side (archived sessions, manual
deletion, `cleanupPeriodDays`), see
[`docs/restoring-archived-sessions.md`](../docs/restoring-archived-sessions.md).

### 2.4 Migrations

[`claude_code_log/migrations/`](../claude_code_log/migrations/) is a
small migration system. Each migration is a `NNN_description.sql` file
applied in numeric order by `migrations/runner.py`. The schema-version
table tracks which migrations have run; `cache.py` invokes the runner
the first time a given cache DB is opened in a process (memoised per
DB path, re-checked if the file disappears), so a fresh checkout
running against an old cache DB transparently upgrades.

Current migrations:

- `001_initial_schema.sql` — sessions table + per-message metadata.
- `002_html_cache.sql` — adds the rendered-HTML cache layer.
- `003_html_pagination.sql` / `004_html_pagination_variant.sql` —
  per-page HTML chunks for `--page-size`.
- `005_session_team_name.sql` — adds `team_name` to sessions for the
  teammates feature (PR #125).
- `006_session_ai_title.sql` / `007_subagents_fingerprint.sql` —
  `ai_title` on sessions; subagent-sidecar fingerprint on cached files.
- `008_session_sidecar.sql` — cross-session sidecar tables
  (`session_parents`, `junction_uuids`, `dedup_winners`,
  `sidecar_state`) for session-scoped incremental rendering (§ 2.12).
- `009_sessions_hidden.sql` — adds `hidden` to sessions, so warmup /
  empty sessions stay cached but out of the rendered set.
- `010_sessions_residual_count.sql` — adds `residual_count` to
  sessions: the per-session entries a full load traverses but
  `message_count` doesn't cover, which the incremental cache refresh
  needs to recompute `projects.total_message_count` by delta (§ 2.14).
  Deliberately NULLable — a NULL means "unknown basis", and the
  refresh declines rather than compute a delta from it.
- `011_cached_file_size.sql` — adds `source_size` to cached files, so
  freshness no longer misses an append that lands inside the mtime
  tolerance (§ 2.15). NULL on pre-011 rows falls back to mtime alone.
- `012_message_lookup_indexes.sql` — composite indexes for the
  refresh's per-tick lookups, each of which was walking every row in
  the project (17–22 ms a call on a 38,706-row archive, 0.2–2.5 ms
  after). Pure performance: no columns, nothing to backfill. Two
  things about it are worth knowing:
  - `(project_id, session_id, timestamp, file_id)` carries `timestamp`
    for a caller *outside* the refresh: `load_session_entries` (the TUI,
    and rendering an archived session) filters on session but orders by
    timestamp, and without that column the planner takes the timestamp
    index and walks the whole project to load one session. With it, the
    seek and the ordering come from one index and no sort is needed.
  - That same index is double-edged — it also lets the planner satisfy
    a bare `session_id IS NOT NULL` as a range scan over every
    session-bearing row and *prefer* that to seeking the uuids a query
    asked for, so three queries had to move that predicate out of SQL
    and into Python to keep it from making them slower.

Recreating-tables migrations toggle `PRAGMA foreign_keys = OFF/ON`
around the rebuild to avoid losing rows to cascade-deletes during the
swap.

### 2.5 JSON export

[`claude_code_log/json/`](../claude_code_log/json/) is a thin renderer
that mirrors `HtmlRenderer` / `MarkdownRenderer`: same
`generate(...)` / `generate_session(...)` / `generate_projects_index(...)`
surface, same `--depth` and `--compact` honoring. Output is a
structured JSON document — top-level `version` / `title` / `detail` /
`compact` / `sessions` / `messages` keys; each node carries
`index` / `type` / `title` / `timestamp` / `session_id` / `content`,
plus optional `parent_uuid` / `agent_id` / `pair_first` etc. when
present. Children are nested directly under their parent's
`children` array — it's the same tree the HTML/Markdown renderers
walk, serialized verbatim.

The renderer runs entries through `generate_template_messages` (the
same format-neutral pipeline § 3 describes), so JSON output inherits
**all** post-factory polishing for free: slash-command normalisation
(bare `<command-name>X</command-name>` → `/X`), command-args
hardening, teammate session-color enrichment, etc. There is no
JSON-specific cleanup pass — the rule of thumb is: *if it shows up
right in HTML/Markdown, it shows up right in JSON*. This is the
operative example of the **factory-layer normalisation seam**: raw
`TranscriptEntry` data is polished once at factory time into the
typed `MessageContent` models that all three renderers share, so
display polish lives in one place rather than being re-implemented
per output format.

A few JSON-specific touches:

- `_json_default` unwraps Pydantic models embedded in `MessageContent`
  dataclasses (tool inputs/outputs are Pydantic; `dataclasses.asdict`
  doesn't recurse into them, so without this hook they'd stringify
  via `__repr__` and lose structure). Also handles `Enum` and `Path`.
- `is_outdated(file_path)` reads the `version` field from existing
  JSON output and compares against the current library version —
  same invalidation contract as the HTML cache so re-runs skip
  unchanged outputs. It guards on `Path.is_file()` (not `exists()`)
  so a non-regular destination like `/dev/stdout` is treated as
  outdated rather than opened, which would deadlock the version sniff
  (issue #223). An explicit `--output` bypasses the skip entirely
  (`force_regenerate`, issue #221) since the version marker can't tell
  which source produced a user-chosen file.
- `combined_transcripts.json` per project; `session-{id}.json` for
  individual sessions. The naming respects `variant_suffix` for
  detail/compact variants.

The projects-index JSON (`all-projects-summary.json`) is a parallel
top-level file — same shape as HTML's `index.html` but consumable by
external tools (dashboards, query scripts, `jq` pipelines).

### 2.6 Depth filter

The `--depth` flag (#159) lets users dial how deep into the message
hierarchy to render — `session > user > assistant > agent > tool > hook`
— naming the level by the node the output stops at. It maps onto
`models.RenderingDepth` (which keeps the older verbosity names internally)
via `models.DEPTH_TO_DETAIL`:

- `hook` (= `RenderingDepth.HOOK`) — everything, incl. hooks + system notices.
- `tool` (= `TOOL`, **default**, `DEFAULT_DEPTH`) — detailed but
  cleaned: drops system/hook noise while keeping the full conversation
  and tool I/O.
- `agent` (= `AGENT`) — drops most tool I/O, keeps the conversation plus a
  curated set of "interaction signal" tools (WebSearch, WebFetch, Task,
  Agent — the ones that show *what the agent did*, not *what it read*).
  See `_LOW_KEEP_TOOLS` in [`renderer.py`](../claude_code_log/renderer.py).
- `assistant` (= `ASSISTANT`) — drops all tool I/O (user + assistant only).
- `user` (= `USER`) — drops everything except user messages and
  steering (for feeding downstream agents, e.g. a requirements doc).
- `session` (= `SESSION`, depth-only, no `--detail` spelling) — session
  structure only: session/branch headers + fork landmarks, every message
  body dropped. Handled by an explicit branch in
  `_ghost_template_by_depth` (the `visible_at` predicate keeps
  threshold-less built-ins like `UserTextMessage` visible at every level,
  so "drop even user messages" can't be expressed via `depth_visibility`).

The legacy `--detail full|high|low|minimal|user-only` is kept as a
deprecated alias (removed in 2.0) and maps to the same `RenderingDepth`s.
Filenames use a single canonical suffix per level — the `--depth` name
(`.hook/.agent/.assistant/.user/.session`), with the default `tool`/TOOL
suffix-less — regardless of which option selected it (so `--detail low`
and `--depth agent` share the `.agent` file). See `utils.variant_suffix`.

Recaps (`AwaySummaryMessage`) are a cross-cutting exception: they are a
high-level summary of activity, so they stay visible at *every* content
level (`depth_visibility = USER`), including `user`. The `--no-recaps`
flag suppresses them at all levels — giving `--depth user --no-recaps`
for a truly user-only view, or `--depth assistant --no-recaps` to drop the
recap/agent redundancy (#179).

Filtering happens in a single *post-render* pass on `TemplateMessage`:
`_ghost_template_by_depth` sets each non-visible slot in
`RenderingContext.messages` to `None` ("ghosting"), keyed by the content
class's `depth_visibility` predicate (plus the `_LOW_KEEP_TOOLS`
allowlist at `low` and sidechain dropping below `HOOK`). Indices stay
stable — surviving messages keep their `message_index`, so there is no
reindex; the rendered tree simply skips ghost slots. Earlier revisions
ran a *second*, pre-render `_filter_by_depth` pass on `TranscriptEntry`
plus a `_reindex_filtered_context` remap after every deletion; the
ghosting model collapsed both into this one axis.

Important interaction: `_pair_skill_tool_uses` also ghosts in place (the
slash-command body and the redundant "Launching skill" tool_result).
Because anchor-target references can be cached before a slot is ghosted —
a branch header's `parent_message_index`, `session_first_message`
entries, junction forward-links — each ghosting step sanitizes them
afterward: `_pair_skill_tool_uses` calls `_drop_anchor_refs_into_ghosts`
and `_ghost_template_by_depth` calls `_repair_stale_anchor_refs`, so no
`#msg-d-{N}` backlink dangles (see PR #131 fix). See
[rendering-architecture.md § 5](rendering-architecture.md) for the full
pass order.

### 2.7 Image export

[`image_export.py`](../claude_code_log/image_export.py) is
format-agnostic: HTML and Markdown both call into it. Three modes
(matching the `--image-export-mode` CLI choices):

- `placeholder` — drop the image and render a placeholder marker
  in its place.
- `embedded` — base64-encode the image directly into the output as
  a data URL.
- `referenced` — write the image to disk next to the output and
  embed a `src=` reference.

Default is `embedded` for HTML (single self-contained file) and
`referenced` for Markdown (keeps the `.md` text small and lets
images live as separate PNGs alongside).

Referenced filenames are **content-addressed** — `images/image_<digest>.<ext>`
from a BLAKE2b digest of the decoded bytes — so a given image maps to
exactly one file no matter which render pass, run, or worker process
exports it, and re-exporting is idempotent (an existing file is already
the right bytes; writers go through a unique temp file + atomic
`os.replace`, so concurrent exporters of the same image replace
identical content). This replaced a per-render counter whose names
collided between the combined-page and per-session passes — each pass
restarted at `image_0001` and assigned the same names to different
images, so the last pass to run overwrote the other's files. Content
addressing is also what lets referenced-mode renders use the fragment
store (§ 2.9) and the render fan-out (§ 2.10).

### 2.8 Performance profiling

[`renderer_timings.py`](../claude_code_log/renderer_timings.py)
provides `log_timing(label, t_start)` context managers used throughout
`renderer.py`. Set `CLAUDE_CODE_LOG_DEBUG_TIMING=1` to print per-phase
times to stderr — useful for spotting which phase regressed when a
large transcript suddenly takes seconds longer than before.

### 2.9 Render memo caches

A project conversion renders every message **twice**: once into its
combined-transcript page and once into its individual `session-*.html`.
Both go through `HtmlRenderer.generate`, which rebuilds the tree from the
same source entries, so the per-message formatting work is duplicated
wholesale. On a 118-file, 12k-message project that is 22,420
`format_content` calls covering 11,113 distinct messages.

[`render_cache.py`](../claude_code_log/render_cache.py) memoizes the two
dominant leaves of that work — Pygments highlighting
(`html/renderer_code.py::highlight_code_with_pygments`) and mistune
Markdown (`html/utils.py::_render_markdown_memoized`, behind
`render_markdown` / `render_user_markdown` / `render_markdown_inline`).
Measured effect on that project: 12.4s → 8.7s wall, with all 88 output
files byte-identical. Hit rates run ~69% (Pygments) and ~59% (Markdown);
Pygments exceeds the 50% that page-vs-session duplication alone implies
because the same file contents are commonly re-read across messages.

Two properties matter for correctness:

- **Markdown is not a pure function of its text.** The SHA-linkifier
  plugin resolves commit hashes against the per-render repo cwd carried
  by `git_remote._render_repo_cwd` (read via `current_render_repo_cwd()`),
  so identical text legitimately renders different links in different
  projects. That cwd is part of the Markdown key — without it a
  long-lived host (`serve`, the TUI) would serve one project's commit
  links inside another's page. Pygments has no such coupling and keys on
  its arguments alone.
- **Bounded by bytes, not entries.** Rendered fragments are large, so
  `functools.lru_cache(maxsize=N)` gives no control over footprint. The
  caches evict LRU against a byte budget and refuse any single entry
  above 1/8 of it, so one huge highlighted file cannot evict everything
  behind it. `CLAUDE_CODE_LOG_RENDER_CACHE_MB` sets the budget (default
  192 MB per cache; `0` disables memoization and restores the previous
  always-recompute behaviour). Typical projects stay far under it — the
  measured project held 13.3 MB of Pygments and 2.7 MB of Markdown with
  zero evictions.

`CLAUDE_CODE_LOG_DEBUG_TIMING=1` prints hit rate, entry count and bytes
per cache alongside the render timings.

**The fragment store sits above the leaf memo** and removes the
page-vs-session duplication itself rather than its expensive leaves:
[`fragment_store.py`](../claude_code_log/fragment_store.py) caches each
message's complete formatted fragment — the `(title, html, timestamp)`
triple `_annotate_tree_for_render` writes onto the tree — for the length
of one conversion, so the per-session pass reuses what the combined-page
pass formatted (measured: 64,968 lookups → 32,441 formats, a 50% hit
rate, on a 803MB/187-file archive). One store per `convert_jsonl_to`
call, created by `_make_fragment_store` (HTML only) and handed to the
combined-page and session-file loops; it dies with the conversion, so
nothing about it ever needs invalidating.
`CLAUDE_CODE_LOG_FRAGMENT_STORE=0` disables it for bisecting.
Because the store is a RAM-for-CPU trade — measured serial peaks on the
803MB reference project: 1252MB store-off vs 1521MB store-on, the
+269MB being the fragment text plus per-entry overhead —
`_make_fragment_store` carries a memory valve: when available memory is
under `_MIN_AVAILABLE_MEMORY_PER_TRANSCRIPT_BYTE` (2.4×, the store-on
peak plus margin) times the project's transcript bytes, the conversion runs
store-less at its pre-store footprint instead of trading its last RAM
for CPU. An explicit `CLAUDE_CODE_LOG_FRAGMENT_STORE=1` overrides the
valve; whenever the valve trips, the render pool's far higher memory
bar has already declined, so a store-less conversion is always serial.

A fragment is *not* a pure function of its source entry, and the store
carries three guards for the three ways the same message legitimately
renders differently in different trees (each found by hash-diffing real
projects — see `work/render-format-once.md` § 4.8): fragments embedding
per-tree `#msg-d-{N}` anchors are never stored (output scan); a
signature of the tree-derived render inputs (pair presence,
`display_model`, `agent_depth`, sidechannel/collapse flags) is part of
the store key; and `get()` verifies the requesting tree's content
digest matches the one stored at `put` time before serving.
`fragment_store.content_digest` is a canonical BLAKE2b walk over
exactly the compare-relevant state — same field coverage as dataclass
equality, with the per-tree `message_index`/`fragment_key` fields
excluded via `compare=False`, and any looseness of Python `==` that
the canonical form can't express (bool/int, dict order,
identity-`repr` objects) resolving to a conflict served fresh, never
a false hit. A digest is stored rather than the content object so the
store holds only strings and bytes — picklable, spillable, and unable
to pin object graphs — the property the planned fed-worker format
phase depends on. On the reference 803MB archive this is peak-RSS-
neutral (measured 1521MB either way: the stored contents alias
objects the master entry list keeps alive, and the store-on peak of
+269MB over store-off is the 186MB of fragment text plus per-entry
overhead), so the fragment text itself is what any future memory
bounding must address. Keys are `(id(source_entry), part_ordinal)` — entry
identity, stamped in the pass-2 render loop — because transcript uuids
collide across resumed/forked sessions. Renders with
`image_export_mode="referenced"` participate like any other: image
filenames are content-addressed (§ 2.7), so a stored fragment's
`src=` names the file the first formatting of that message already
wrote, and replaying the fragment skips only a write that has
happened.

### 2.10 Intra-project render fan-out

[`render_pool.py`](../claude_code_log/render_pool.py) fans a single
project's output files — its combined pages and its per-session files —
out over worker processes, and
[`render_dispatch.py`](../claude_code_log/render_dispatch.py) is the
policy layer that decides when it is used: `build_render_pool` (should
this conversion have a pool), `worth_dispatching` (is this batch worth
sending) and `dispatch_render_units` (run the batch, falling back
inline). The dependency runs one way — `render_dispatch` imports
`render_pool`, never the reverse — so a worker never loads the policy it
executes. This sits *below* the project-level pool in
§ 2.1 and exists because that one leaves cores idle in two shapes:
the all-projects wall clock is bounded by the largest single project
(measured: 5 real projects, 4 cores, 195s of work, 65.0s wall — exactly
the largest project's own time, at 2.18 cores average), and an
incremental run has only one or two stale projects to fan out at all.

Workers are **fed, never self-sufficient**: they do not load the
transcript at all. Each dispatched unit carries its own slice of the
parent's master message list (`RenderUnit.entries` — a page's sessions'
entries, or one session's trunk + integrated agent entries), the
master-list ordinal of each entry (`entry_ordinals`, so fragment-store
keys name the same positions in every process), and — for session units —
the session's slice of the parent's fragment store
(`RenderUnit.fed_fragments`). The one per-worker piece of state is a
*slim* `SessionTree` (`dag.slim_session_tree`, sent via the pool
initializer): the render path reads only the DAG lines, junction points
and workflow dicts, so the per-message `nodes` mapping — whose pickled
size is the whole transcript — stays behind (its lookups raise on a slim
tree, so a future render-path consumer of `nodes` fails loudly in a
worker instead of silently diverging). The parent keeps every staleness
check and cache write to itself (so the DB stays single-writer — a
worker's own `CacheManager`, kept only for the per-session combined-link
lookup, is constructed `read_only=True`: no migrations, no project-row
upsert, `mode=ro` connections), slices
session units with the same trunk predicate `generate_session` filters
by (so the worker's re-filter of the fed slice is idempotent), and the
pool starts lazily on first submit. `RenderPool.submit` declines a unit
with no entry slice to the inline path, and a pool is only created when
the conversion has a pre-built session tree — a worker-side DAG rebuild
from a slice alone could genuinely differ.

**Which batches are worth dispatching** is decided per batch by
`render_dispatch.worth_dispatching`, on the work a batch carries rather than
the number of units in it. A conversion dispatches two batches: its
stale *pages* (one unit per ~`page_size` messages, where nearly all the
render time is) and its stale *session files* (cheap, because the page
pass has already put their fragments in the store). Weighing them by
unit count got this exactly backwards — an 8-unit floor meant a project
with fewer than 8 pages rendered its expensive page batch inline and
fanned out only the cheap session batch, paying the pool's ~1s of
`spawn` + import for the batch with the least to gain. Weighed by entry
count against `_MIN_ENTRIES_FOR_RENDER_POOL` (4,000, about twice what
that startup costs at the ~2,000 entries/s the render phase sustains),
projects between ~4k and ~15k messages went from 0.86–1.05× to
1.27–2.27×, and projects below it went from 0.82–0.94× — a real loss —
back to 1.00×. Two refinements complete the rule: a lone unit never
dispatches (it would render in a worker while the parent waits), and
once the pool has started (`RenderPool.started`) any multi-unit batch
does, since the startup is then sunk — which is how the session batch
rides along behind the page batch that paid for it.
`_MIN_MESSAGES_FOR_RENDER_POOL` is the same number, and only a
short-circuit: every batch is a subset of the project's message list, so
a project below it could never form a batch that clears the gate.

Before the feed, each worker re-loaded the whole transcript from the
warm cache in its initializer (~0.7s at 12k messages; ~12s of CPU per
worker at 329MB of transcript), so per-worker cost scaled with project
size rather than unit size — measured post-fragment-feed on a 16-core
Mac, 16 workers still burned 286.1s of CPU to do 91.1s of serial work,
almost all of it transcript reloads, and 16 workers came out *slower*
than 8. Feeding moves each entry at most twice per conversion (its page
unit, then its session unit) instead of once per worker. Fragments cross
the boundary as before (plain strings + digests — § 2.9's store made
them portable): a page worker returns its fragment store as a delta in
its result, the parent absorbs it, and session units dispatch afterwards
carrying their slices. Pages always drain before sessions dispatch, so
the feed covers essentially every message; every fed fragment is
digest-verified on use, so the worst a stale slice can do is cost the
recompute it would have cost anyway.

**Memory: the cap charges measured post-feeding footprints.** The
conversion *parent* is the heavyweight — its master entry list (Pydantic
entries, TemplateMessage tree, SessionTree) plus the fragment store's
text and in-flight pickled slices measured ~4.4× the transcript's bytes
on disk (598MB on a 140MB/47k-message project, 1458MB on 329MB/97k; VmHWM
polling, 2026-08-26). Fed workers hold only base imports, their memo
caches and the in-flight unit's slice: the largest worker measured
~136MB + 0.59× transcript across the same runs.
`render_pool.memory_capped_workers` charges the parent 4.5× + base and
each worker 0.8× + base — a ~1.2–1.35× margin over the fit, because the
alternative failure mode is real: when workers each held a full copy, an
unguarded `auto` on a large archive exhausted RAM and drove the machine
into swap, pegging every core — far worse than rendering serially. (The
one under-charged pathology is a single session spanning most of the
transcript, whose unit slice re-inflates toward the parent's ~3× in its
worker; it is one outlier inside the headroom, and such projects yield
too few units to fan wide.) The cap reads available memory as cgroup
limit first (inside a container the host's totals are a lie), then
`MemAvailable`, then free physical pages, taking 60% of it as budget.
The all-projects parent applies the same cap with
`concurrent_projects=resolved_jobs` before handing out any budget, and
each worker re-checks against its own project before starting a pool.
Unknown memory allows at most 2 workers.

Both non-Linux platforms need their own probe, since each would otherwise
fall through to that unknown-memory branch and be capped at 2 workers
regardless of cores or RAM — a 16-core Mac measured the whole fan-out on
2 before this was fixed. macOS has neither `MemAvailable` nor
`SC_AVPHYS_PAGES`: `_darwin_available_bytes` shells out to `vm_stat`
(present on every macOS, no dependency) and counts free + inactive +
speculative + purgeable pages, since excluding the cheaply reclaimable
ones reports a busy Mac as having almost nothing free. Windows has no
`/proc` and no `os.sysconf` at all: `_windows_available_bytes` reads
`ullAvailPhys` from `GlobalMemoryStatusEx` via `ctypes`. Every probe
returns None rather than raising, so an unreadable platform stays
conservative instead of failing a conversion.

**On by default** at the CPU count, settled by the 16-core measurement
below. `$CLAUDE_CODE_LOG_RENDER_JOBS` overrides: `1` or `off` disables it,
`auto` is the default, an integer pins a count — see
`render_pool.resolve_render_jobs`. `--jobs` never enables or disables it;
it only caps it, so the two pool levels together can't oversubscribe. An
explicit `convert_jsonl_to(render_jobs=N)` overrides the environment; the
default `None` consults it. `render_dispatch.build_render_pool` declines regardless for
single-file mode, a missing cache manager, a missing pre-built session
tree (workers render fed slices against the conversion's tree),
projects below `_MIN_MESSAGES_FOR_RENDER_POOL`, and machines without
the memory the cap formula demands; a pool that *is* created still only
starts if some batch clears `worth_dispatching` above.
`image_export_mode="referenced"` used to decline too, when renders
allocated `images/image_NNNN.png` names from a per-call counter that
collided across passes and processes; referenced filenames are now
content-addressed (`image_export.export_image` digests the decoded
bytes and writes via temp-file + atomic replace), so any pass, run, or
worker exporting the same image converges on the same file and the
mode fans out like any other.

One ordering change made the pages parallelisable: the paginated writer
used to reveal page N-1's "Next" link while generating page N, so page N's
render edited page N-1's file. That fixup now runs once after all pages
land — order-independent and idempotent.

**Measured** on 4 cores, 47k-message project (240 session files, 218
output units); the serial floor — cache check, transcript load, planning,
all in the parent and none of it parallelised — is 4.7s:

| | wall | total CPU |
|---|---|---|
| neither optimisation | 44.8s | 43.5s |
| memo only (§ 2.9) | 27.6s | 26.2s |
| fan-out only (4 workers) | 22.5s | 57.0s |
| both | **20.0s** | 48.8s |

Read those together, because the pair is easy to misread. The fan-out
parallelises perfectly well on its own — 44.8s → 22.5s is 2.0× on 4 cores
against a 4.7s serial floor. It looks weak *on top of the memo* (27.6s →
20.0s, 1.38×) only because the two optimisations attack the same work: the
memo removes the page-vs-session duplication, and splitting units across
processes gives each worker a cold cache and brings some of it back. Both
together is still the fastest configuration, so they compose — just
sub-additively.

A 16-core Mac over 8 real projects (1543MB of transcripts, largest 329MB)
shows what happens with cores to spare — and why the two `--all-projects`
scenarios diverge so sharply:

| scenario | config | wall | CPU | cores used |
|---|---|---|---|---|
| full rebuild, 8 projects | memo only | 100.8s | 226.0s | 2.2 of 16 |
| full rebuild, 8 projects | both | 79.3s (1.27x) | 324.8s | 4.1 of 16 |
| incremental, 1 stale | memo only | 93.2s | 90.8s | 1.0 of 16 |
| incremental, 1 stale | both | **34.6s (2.70x)** | 367.9s | 10.6 of 16 |

The incremental row is what flipped the default: 2.70x on the everyday
shape of a run, using 10.6 cores instead of 1.

The full-rebuild row is the *unsolved* case, and the numbers say exactly
why. Converting the largest project alone takes 93.2s; converting all
eight takes 100.8s. The other seven cost 7.6s of wall clock between them —
the run is, to within 8%, "how long does the biggest project take". The
static budget split then gives that project `jobs // stale projects` = 2
render workers while the seven small ones finish and 13 cores go idle. So
the fan-out helps it barely (1.27x) even though the same project alone
reaches 2.70x. Pool-bounding projects are now handled by hold-back:
`_holdback_plans` runs a greedy makespan comparison, largest first
(hold while `pool(rest) + fanned(largest)` beats pooling everything,
with fanned time from the conservative `_fanned_speedup` of the
workers a lone memory-capped conversion would get), keeps what it
holds out of the pool and converts each alone with the full render
budget afterwards. Costs compare cached message counts when every
plan has one (bytes alone would miss a dense giant), bytes otherwise.
On the reference archive it holds exactly the 97k giant — measured
65.4s → 58.5s (1.12x) on the 8-core VM under the original
single-dominant rule, whose decision the general rule reproduces
there, since the twin 47k projects keep each other's pool bounded —
and it additionally covers shapes the 2x-dominance bar missed, such
as a runner-up that bounds a pool of small projects without being 2x
any of them. What it deliberately never does is serialize level-sized
projects (a certain loss — they already saturate the pool) or hold
anything a machine can't actually fan. Fixing the truly general case —
several level-sized projects sharing one static split — still needs
the split to be dynamic, or a single flat pool over render units
rather than two nested levels.

Two consequences. First, core count matters a lot: 48.8s of CPU over 4
cores is 12.2s plus the 4.7s floor, so a 10-core machine should land near
9.5s rather than 20s. Second, the per-worker cost (then: a full
transcript reload plus a cold memo) grows with worker count, so the
speedup saturates rather than scaling indefinitely.

Those tables predate the fragment feed and the entry feed, which removed
most of that per-worker cost in two steps. Re-measured on an 8-core VM
over the same 8-project archive (1539MB): with fragments fed, the
incremental scenario reached 2.08x at +76% CPU over serial (the
pre-feed baseline burned +305%); with entries fed as well — no worker
transcript loads at all — it reached **2.87x at +3% CPU** (62.6s →
21.8s wall, 62.5s CPU vs 60.5s serial), and the single-project sweep's
per-worker CPU overhead fell from ~4.4s to ~0.7s at 8 workers. With the
memory cap re-sized to the measured post-feeding footprints (above),
the same VM's incremental cap went 5 → 8 of 8 workers and the scenario
reached **3.24x at +6% CPU** (61.8s → 19.1s wall, 63.3s CPU vs 59.5s
serial), byte-identical in every configuration. The full-rebuild
scenario improved to 1.12x via the hold-back (above, since
generalized from the single-dominant rule to the greedy makespan
comparison); what remains is its general case — several level-sized
projects, where the *core* split `jobs // stale` still grants 1
render worker/project — and the serial parent floor (load + parse +
plan). Both land with the remaining "format once, assemble many"
step: the flat pool over render units. Planned in
[`work/render-format-once.md`](../work/render-format-once.md).

### 2.11 Diagnosing hangs (SIGUSR1 stack dump)

When `claude-code-log` appears stuck (100% CPU, no output), a
single `SIGUSR1` to the running process dumps the live Python
stack of every thread to stderr without killing it:

```bash
# In another terminal
kill -USR1 $(pgrep -f claude-code-log | head -1)
```

The handler is wired in `cli.py::_install_stack_dump_signal()` via
`faulthandler.register(SIGUSR1, all_threads=True, chain=False)` and
installed before any heavy work in the entry point. POSIX-only —
Windows lacks `SIGUSR1`, the install is a silent no-op there. Unlike
`py-spy`, this needs no root and no extra install, since the runtime
is already wired to dump itself on demand. Added by PR #135 to make
the DAG cyclic-children class of bug diagnosable in the field; useful
for any future hang.

### 2.12 Session-scoped incremental rendering

Converting a project used to load its entire transcript even when the
cache was fresh and a single session file needed regenerating — the
master list, DAG and trees exist before any staleness is consulted.
The session-scoped path (streaming stage 2 of
[`work/render-format-once.md`](../work/render-format-once.md)) removes
that: when the Phase-1b gate in `convert_jsonl_to` finds the cache
fresh, the combined output current, and only session files stale,
`converter._load_stale_session_transcripts` loads *only the files
holding those sessions' entries* and renders them through the ordinary
`_generate_individual_session_files`, byte-identical to the full path.

Three pieces make the partial load faithful:

- **The cross-session sidecar** (migration 008, `cache.SessionSidecar`)
  is persisted by every full unfiltered directory load, in the same
  batch as the per-file cache writes: per-session parent linkage
  (resume/fork attachment points), junction points with their ordered
  target lists, and the dedup winner for every uuid carried by more
  than one session (resume replay prefixes). All three are compact
  projections of the `SessionTree` the load already built. A partial
  load enforces the winner map up front (losing copies drop before the
  DAG sees them), patches parent/junction facts whose other end isn't
  loaded, and adds empty ancestor stub lines so depth chains resolve.
- **Discovery by the cache's file map**, not by filename stem
  (`CacheManager.get_session_file_map`): real archives contain files
  whose entries span two sessions (a continuation written into the
  previous session's file) and sessions with no file of their own.
  Co-resident fresh sessions load along with a stale one and are
  skipped by the per-session staleness check. An archived stale
  session (cached rows, source deleted) is skipped outright — the
  full path loads everything and still renders nothing for it.
- **A pagination-aware combined-freshness check**
  (`_combined_output_is_stale`): the gate used to ask
  `is_transcript_stale("combined_transcripts.html")`, a name a
  paginated project has no cache row for — so paginated projects never
  early-exited and full-loaded on every direct conversion. The helper
  replays the pagination pass's plan from cached session data alone
  (same session→page assignment, same `is_page_stale` per page, same
  invalidation triggers). This also makes the plain
  "everything is current" early exit fire for paginated projects.

Equivalence is held to byte-identity by
`test/test_session_scoped_render.py` — real fixture projects with the
full loader monkeypatched to raise, plus a synthetic project pinning
resume-replay dedup, cross-session fork parents, junctions and
ancestor chains — and by hash runs over real archives (85 and 187
output files byte-identical on the two measured projects, with 64 and
129 session files regenerated through the partial path; sidecars of
300–742 dedup winners in play). Measured on the 8-core VM against the
803MB reference archive, warm cache: one stale session file went
4.6s → 0.6s (7.9x), and a fully-fresh direct conversion 4.6s → 0.4s
(the early exit finally firing for a paginated project).

`CLAUDE_CODE_LOG_SESSION_SCOPED=0` disables the path for bisecting.
Every decline (no sidecar yet, date filters, `--no-cache`, an
inconsistent file map) falls back to the full load, whose behaviour is
unchanged — the sidecar is an optimization input, never a correctness
requirement.

### 2.13 Page-granular streaming conversion

The session-scoped path (§ 2.12) still full-loads whenever the
*combined* output is stale, so peak RAM stayed bounded below by the
largest single project — a machine under ~2x its largest project could
not convert it at all. The streaming path (stage 3 of
[`work/render-format-once.md`](../work/render-format-once.md)) removes
that floor for paginated HTML conversions:
`converter._stream_paginated_conversion` plans the session→page
assignment purely from cached session data (the same
`_assign_sessions_to_pages` call, via the shared `_plan_page` helper,
so the plans cannot drift), then for each page needing work — a stale
page, or stale session files on it — loads *only the files holding
that page's sessions* through the § 2.12 partial-load machinery,
renders the page and its stale session files together against a
per-page fragment store, and drops it all before the next page. Peak
residency becomes max(one page's source files), not the project.

When it runs:

- **Structurally**: HTML directory conversions with a fresh-able cache,
  no date filters, no `--force`-style regeneration, combined output
  enabled, and pagination in play per the cached counts. The sidecar
  must exist; since `ensure_fresh_cache` now persists it too (same
  batch as the per-file cache writes), a run whose cache was just
  refreshed can stream instead of loading the project a second time.
- **By a memory valve, with a sparse fallback**: in auto mode the path
  always engages when available memory is under 2.4x the project's
  transcript bytes — literally the same knee as the fragment-store valve
  (§ 2.9): both read `_MIN_AVAILABLE_MEMORY_PER_TRANSCRIPT_BYTE`, one
  constant so the two can't drift. The ladder is continuous: below ~2.4x
  the store had already declined, and streaming takes over the serial
  conversion; the fan-out's own (far higher) memory bar has long since
  declined by then, which is why streaming renders inline without
  losing anything. With more memory than that (or none measurable),
  the pass still runs — but in *sparse* mode: once it has planned its
  pages and counted the ones needing work (stale/missing page, or
  stale session files on it — pure cache and stat queries), it
  declines itself unless that count is at most a third of the plan
  (`_STREAMING_MAX_SPARSE_FRACTION`), falling through to the full load
  + fan-out. The reasoning is measured, not structural: streaming's
  wall scales with the pages needing work while the full path pays the
  whole-project load regardless, so sparse work (the daily-run shape)
  streams faster even where the fan-out is available — but a dense
  rebuild renders serially and loses to the fan-out on a roomy
  machine. On the 8-core/16GB VM against a 137MB/26-page archive
  (`scripts/bench_render.py`): incremental (1 page + 3 sessions
  stale) streamed 2.0s/276MB peak RSS vs the fan-out full path's
  3.3s/542MB, while a full rebuild streamed 11.1s vs the fan-out's
  6.7s; the crossover sits near 36% of pages stale and moves only
  weakly with core count (the full load, not rendering, is the fixed
  cost), so 1/3 keeps a margin under it.
  `CLAUDE_CODE_LOG_STREAMING=1` forces the path wherever structurally
  eligible, bypassing both the valve and the sparse gate; `=0`
  disables it (the bisecting knob).

Two details are load-bearing for correctness:

- **Strict file resolution**: a page load requires every one of its
  sessions to resolve to a complete, present source-file set
  (`_resolve_session_source_files(strict=True)`); any gap declines the
  whole pass to the full load rather than rendering a page with a
  session's remnant.
- **The co-resident-session restriction**: a source file can span two
  sessions that sit on *different* pages, so one page's load can carry
  a partially-loaded session from another page. The per-page session
  pass is therefore restricted to that page's own stale sessions
  (`_generate_individual_session_files(restrict_to_sessions=...)`) —
  without it, the partial session renders truncated and its cache row
  then reads current forever (pinned by mutation test in
  `test/test_streaming_render.py::TestFileSpanningSessions`).

Byte-identity is held by `test/test_streaming_render.py` (a real
fixture with the full loader monkeypatched to raise, a synthetic
project whose resume/fork couplings span the page split, and the
file-spanning trap above) and by hash runs over the two coupling-heavy
real archives — every scenario (warm parity, full rebuild,
incremental) byte-identical on both. Measured on the 8-core/16GB VM,
serial, warm cache (full path → streamed): 296MB project full rebuild
454MB → 305MB peak RSS; 803MB project full rebuild 1490MB → 591MB at
slightly lower wall (28.2s → 25.2s), incremental 1092MB → 587MB at
7.6s → 4.6s. The remaining ~590MB is the largest page's co-resident
files plus interpreter baseline — the floor scales with page size,
not archive size.

Declines fall through to the full load unchanged. The cache refresh
itself no longer full-loads on changed sources — that is § 2.14.

### 2.14 Incremental cache refresh

The last full-residency path was `ensure_fresh_cache` itself: one
changed file re-walked every file through `load_directory_transcripts`
to recompute three things — per-session cache rows, project
aggregates, and the sidecar. Streaming stage 4
(`converter._incremental_cache_refresh`) recomputes all three from a
bounded *closure* of the modified files instead:

- **Per-file parse**: each modified file's old identity state
  (sessions, uuids, parentUuids, requestIds, residual type counts) is
  captured from the messages table, then `load_transcript` re-parses
  it (rewriting its rows exactly as always). Old uuids must be a
  subset of new — a shrunk/rewritten file means history changed and
  declines.
- **Closure** (SQL projections, no entry loading): the modified
  files' sessions, plus every session holding a copy of a modified
  uuid (re-election partners), plus owners of external attachment
  points (the dedup winner's session when duplicated), plus the
  complete old target list of any junction a modified entry touches
  or whose uuid is re-elected (so target order rebuilds natively),
  plus files holding cross-file metadata (summaries by leafUuid,
  ai-titles by session) for closure sessions. Sidecar-derived ids are
  trunk-normalized — junction owners/targets can be branch-qualified
  `{trunk}@{uuid12}` line ids that the file map doesn't know.
- **Partial load**: the closure's files go through
  `_load_sessions_partial` with two refresh-mode deviations — old
  dedup-winner enforcement *exempts* modified uuids (their election
  re-runs natively on the full candidate set, which the closure
  guarantees is loaded), and the sidecar junction patch *skips*
  junctions marked native (the old row would erase a new fork/resume
  target).
- **Facts persist**, restricted to closure sessions (all complete by
  strict file resolution — the co-resident partial-session trap
  applies to cache facts too): session rows upserted; junction rows
  rewritten for tree junctions owned by closure sessions (patched
  ones rewrite their old rows identically — harmless); parent rows
  and re-elected winners replaced; project aggregates move by *delta*
  — new minus old session-row contributions, plus the modified files'
  summary-row delta, with bookends extended from the loaded entries'
  own timestamp strings (the DB normalizes formats).

**The two migrations the delta needs.** `total_message_count` is
`len(messages)` of a full load — the *traversed* entry list — so
moving it by delta requires every traversed entry to be attributable
to a persisted session row:

- **009 (`sessions.hidden`)**: warmup-only and empty/agent-only
  sessions used to be filtered before the write, leaving their
  contribution off the record. The writer now persists them from one
  unfiltered `compute_session_data` pass, flagged `hidden = 1`, and
  every read site meaning "sessions a human would render" filters
  `hidden = 0` — so the visible set is byte-identical to the old
  filtered computation.
- **010 (`sessions.residual_count`)**: entries a session owns that
  `compute_session_data` skips (`attachment`, `ai-title`), counted
  from the traversed list by `compute_session_residuals`. Counting
  them from cached message rows instead is *wrong*, because
  attachments are parsed into the cache but can be dropped by DAG
  traversal — a real-archive holdback caught exactly that (+16). A
  session owning only such entries gets a row of its own (hidden,
  since it has no first user message) so the arithmetic has somewhere
  to put them. The column is deliberately NULLable: a pre-010 row's
  contribution is unknown rather than zero, and the refresh declines
  on one.

Together they close the identity the delta relies on, verified across
the full 78-project corpus:

    total_message_count = Σ(message_count + residual_count) + #summary rows

`Summary` entries are the one class with no session attribution, and
they bypass traversal (appended wholesale), so counting their cached
rows is exact.

**Decline ladder** (each falls through to the unchanged full load):
missing sidecar/project data, deleted source files (archival is the
full path's business), shrunk files, a pre-009 cache (a closure
session with prior entries but no row) or a pre-010 one (a closure
session whose `residual_count` is NULL), attachments involved in
cross-session dedup, a requestId with independent surviving copies on
both sides of the closure boundary (D1 attribution would need global
traversal order — same-uuid replay spans are dedup-resolved and
safe), any strict-resolution gap, and a closure larger than
max(4 files, a third of the project).

The design identities were verified empirically before implementation
across the full 78-project real corpus (I1: total_message_count == Σ
unfiltered per-session counts + typed residual; I2: token totals == Σ
per-session tokens; I3: bookends == raw min/max; zero failures), and
the implementation is held to *DB-state equivalence* — session rows,
aggregates, and all three sidecar tables equal to a full refresh's,
plus rendered byte-identity — by
`test/test_incremental_cache_refresh.py` and by holdback runs on real
archives (hold back the newest files, warm the cache, restore them,
then compare the incremental refresh against the full one: matched on
the 803MB reference archive, 296MB repower, and 1003MB
platform-frontend-next). DB state is the bar rather than HTML because
the first bug this caught — a modified file's new summaries titling
sessions outside the closure — was invisible in the rendered bytes.

Measured on the 8-core/16GB VM, first conversion after three new
sessions appear in the 803MB reference archive (peak RSS / wall):
full refresh + full render 1131MB / 10.6s; full refresh + streamed
render 901MB / 8.1s; incremental refresh + streamed render **582MB /
6.7s**. Once the render streams, the refresh's full load *is* the
peak — which is what this section removes, completing "no archive too
big for the machine". `CLAUDE_CODE_LOG_INCREMENTAL_CACHE=0` is the
kill switch.


### 2.15 Watch mode and live page updates

Two commands keep output current while a session is still being written:
`claude-code-log watch` (a resident loop) and `claude-code-log serve
--watch` (the same loop on a thread beside the HTTP server). See
[`work/watch-mode.md`](../work/watch-mode.md) for the design and the
measurements behind it.

**The engine never renders.** `claude_code_log/watch.py` polls
`(size, mtime_ns)` over `**/*.jsonl` and `**/agent-*.meta.json` under
the watched roots, debounces, and calls a callback; the callback runs the
ordinary conversion. The scan is a *trigger*, not a source of truth — a
false positive costs one no-op conversion, while the conversion already
knows precisely what is stale. Two file classes are excluded because both
land in the watched tree and would make the loop feed itself: dot-prefixed
atomic-write temp files, and generated output.

Debounce is a quiet period (`--quiet-period`, 300ms) with a max-latency
cap (`--max-latency`, 2s): a turn writes several entries in quick
succession, so without the quiet period most of a turn is spent rendering
states nobody sees; without the cap a long unbroken stream would never
surface. `tick()` (poll once) is split from `run()` (wait) and the clock
is injectable, so tests drive ticks by hand against a fake clock.

**What makes a tick cheap** is §2.12's session-scoped path, which is
reachable here only because `ensure_fresh_cache_detailed` reports *how*
it refreshed (`CacheRefresh.NONE`/`INCREMENTAL`/`FULL`). Phase 1b's
staleness test is per-session message counts, so it refuses a FULL
refresh — which carries no guarantee that a session's content didn't
change while its count stayed the same. An INCREMENTAL refresh does carry
it: §2.14's ladder only succeeds after proving each modified file's
cached rows are an exact prefix of its current rows, i.e. a pure append.
`--combined` therefore defaults to `no` for `watch`: with a combined
output present, the session's growth makes it stale and the conversion
falls to the streaming path instead.

**What the tick then spends its time on** was, once §2.12 was reachable,
no longer the render (12% of it) but the cache refresh. Profiled on the
803MB / 217-file reference archive appending to its largest session file
(39.7MB, 207 entries), a 1.03s tick materialised *the same entries three
times*: §2.14's refresh parsed the file from source (488ms, of which
307ms re-serialising and re-inserting every row), then the closure load
rebuilt them from those rows (129ms), then the session-scoped render
rebuilt them again (141ms). Two changes removed most of that:

- **A per-conversion parsed-entry store** (`entry_store.py`, §2.16)
  serves the refresh's list to the other two consumers: closure load
  255ms → 7ms.
- **The staleness sweep stopped scaling with the session count.**
  `get_stale_sessions` ran two SQLite queries *per session* through
  `is_transcript_stale`, and each of those called `get_library_version()`,
  which re-parsed installed package metadata every time — 173 calls and
  35ms a tick. The version lookup is now `lru_cache`d (it cannot change
  inside a process) and the two tables are read once and joined in
  Python: 85ms → 4.5ms.

Tick: **1.03s → 0.717s**. `load_transcript`'s full re-parse and full row
rewrite of the modified file (483ms) was then the remaining bulk, and
§2.16's cross-tick resumption takes it to ~35ms: 0.257s steady state in a
resident `watch`. What remained after that was the refresh's own cache
queries, every one of which was scanning the whole project — fixed by
migration 012's indexes plus one query rewrite (`get_file_states` joined
`cached_files` and filtered on `file_name`, which no index could serve;
resolving names to `file_id` first made it 15.9ms → 0.0ms).

**A steady-state watch tick on the 803MB reference archive is 0.145s**,
from 1.03s — with rendering, which was where this began, now a rounding
error against it.

**The served page updates itself** via
`html/templates/components/live_update.js`, active only over `http(s)` —
a `file://` page cannot fetch anything, not even its own URL, so the
poller notices and does nothing. It polls a HEAD of its own URL and
compares `Last-Modified` **and `Content-Length`**: HTTP dates have
one-second granularity, so two updates inside the same second are
otherwise invisible (the same trap as the cache's mtime tolerance, which
§2.3 solves the same way). On a change it re-fetches the page and either
**patches** or **swaps**.

**Patching** applies when the new render's node-key sequence *extends*
the one on screen: the nodes whose own markup changed are replaced, new
ones are inserted, and everything else is left alone — keeping its
scroll, fold, `<details>` and localised timestamps because it is
literally the same DOM. Change detection is a per-node hash of the
node's own markup, taken from the **freshly parsed document, never from
the live DOM**: decoration rewrites the live tree (timestamp
localisation replaces `innerHTML`), so a hash taken from it never
matches one taken from server bytes. Nothing is emitted server-side for
this.

A node's own markup is its `:scope > .message` *plus* every
non-`.message-node` child of its `.children` — a fork point renders
inside `.children` so folding hides it with the subtree, and on a
fork-only slot it is the node's only content and carries its id.

**Swapping** (replace `#transcript` wholesale) is the fallback for
everything else: renumbered or reordered ids, deletions, a node with no
key, more than 40 changed nodes, and the first update of a session,
which is where the hashes are first taken. It is also what every update
did before patching existed. Measured across three real sessions
replayed through the renderer, 45 of 47 growth steps are pure
extensions; the other 2 are out-of-order arrivals that renumber the
positional `msg-d-N` ids and take the swap.

A swap destroys anything that decorated the old markup, so components
register with `window.claudeLogOnRehydrate(fn)` (defined at the top of
`<body>`, before every component include) and the poller calls
`window.claudeLogRehydrate(root)` — over the whole container after a
swap, and over *only the elements it placed* after a patch. Passing the
containing node instead is a live trap: the session header's fold bar
counts its descendants, so it is replaced on every append, and its node
is the entire page. Currently registered: timestamp localisation (scoped
to a subtree) and the timeline rebuild. Delegated listeners and
everything bound to the toolbar or floating buttons survive untouched
and must **not** register. On the swap path only, fold state and
`<details>` are captured and restored by the poller, keyed `data-uuid` →
`data-session-id` → positional `id` — session headers and fork points
carry no uuid, and on a single-session page the header is the only
foldable node.

Measured: ~1s from append to visible. On a 2.4MB / 896-card page, a swap
touches 897 cards, re-localises 896 timestamps and blocks the main
thread 107ms; a patch of the same append touches **3 cards, 2
timestamps, 61ms**. The remaining 61ms is fetch plus `DOMParser` of the
whole page, which only a server-shipped delta would remove. Idle polls
cost ~1ms.

**Timestamp localisation drains against the idle deadline**, not in
fixed batches. `timezone_converter.js` used 25 elements per
`requestIdleCallback`, which made the *callback count* the cost: a 4MB
page's 1,180 timestamps are 8ms of work but took 766ms of wall clock,
and a 27MB page's 5,010 took 3.3s — in document order, so a live
update's new cards were localised last and the fade-in played over a raw
ISO string. Draining while `timeRemaining()` allows (checked per 32
elements, `{timeout: 200}`, first slice on the current task under a 24ms
budget) takes those to **13ms and 35ms**.


### 2.16 Parsed-entry store

`entry_store.py`. A conversion that refreshes the cache incrementally
(§2.14) parses each modified file from source, and then loads those same
entries back out of the rows it has just written — once for the closure
load and once for the session-scoped render (§2.12). Each rebuild is a
`zlib.decompress` + `json.loads` + Pydantic validation pass over the
whole file, so a one-line append to a 39.7MB session paid it twice at
~130ms each. The store holds the list the first pass produced and serves
it to the other two.

Four properties keep the invalidation surface at zero, and they are the
reason it is a parameter rather than a memo:

- **One store per conversion, threaded explicitly.** Never a global, and
  never hung off `CacheManager` — the TUI keeps one of those across many
  conversions.
- **Only `_incremental_cache_refresh` fills it**, with the files it just
  parsed, and `convert_jsonl_to` drops it after Phase 1b. A cold or full
  conversion therefore stores nothing, and the streaming path (§2.13) is
  deliberately never handed one: its bounded residency depends on
  dropping each page's entries before the next page loads, which a store
  spanning pages would defeat. What it holds is bounded by *what
  changed*, not by the archive.
- **Hits are verified against the file.** `get` re-stats and compares
  `(size, mtime_ns)` against the stamp captured *before* the parse. A
  stamp taken before the parse can only be older than the entries
  describe, so a file that grew mid-parse declines to the cache rather
  than serving a list its stamp misdescribes.
- **Handouts are deep copies**, because the pipeline mutates entries in
  place: `_integrate_agent_entries` appends `#agent-{id}` to `sessionId`
  and is *not* idempotent, and dedup re-parents around dropped copies.
  Today each consumer gets freshly deserialised objects; serving the same
  objects twice would render `…#agent-X#agent-X`. The copy stays cheap
  because the bulk of an entry is immutable strings, which `deepcopy`
  shares rather than copies — 2.0ms and 0.83MB for the 207-entry, 39.7MB
  session, against 123ms to rebuild it from the cache.

Held to byte-identity with the store disabled, over repeated appends on a
fixture whose 170 sidechain entries exercise the mutation above
(`test/test_entry_store.py`), with the copy isolation pinned by a test
that fails with exactly the doubled suffix when the copy is removed.
`CLAUDE_CODE_LOG_ENTRY_STORE=0` is the kill switch; a per-file memory
valve at `put` time declines to hold a file when available memory is
under 6x its bytes, and `=1` overrides that valve.

**Owned across ticks, a store does more.** `watch` keeps one for the life
of the loop and passes it to every conversion (`convert_jsonl_to` takes
an optional store; a caller-owned one outlives the call, an internally
made one doesn't). That turns the store from a within-tick cache into a
*resumption* point, via a second, prefix-pinned mode:

- **The parse resumes.** `put_prefix` records the byte offset the parse
  consumed plus a BLAKE2b digest of the bytes below it, and the next tick
  hashes that prefix and reads only the tail. The digest is a *stronger*
  check than the row-fingerprint prefix comparison it saves — identical
  bytes imply identical rows — and it costs 32 ms over 39.7 MB against
  143 ms to re-parse them. A mismatch (rewound session, replayed history)
  drops the prefix and re-reads from the top. Byte reading replaces text
  reading only when a store is present; every other caller keeps the
  identical text path, and the held entries are the *pre*-post-processing
  parse products, since the whole-file passes (sidecar linking,
  prompt-hash linking, agent splicing — 0.1 ms together) must re-run over
  the concatenated list. The cut is on **entries as well as bytes**: a
  final line whose newline hasn't landed — a torn append, or a file
  simply stored without a trailing one — parses and is returned like any
  other, but neither its bytes nor its entry is held, so the next tick
  re-reads that line instead of handing its entry back a second time.
  Prefixes are charged against the same budget as whole-file entries and
  evicted alongside them, since a `watch` store never goes out of scope.
- **The cache write appends.** `CacheManager.extend_cached_entries`
  inserts only the new rows instead of `save_cached_entries`' delete-and-
  rewrite, which re-runs `json.dumps` + `zlib.compress` over every entry
  (310 ms of a tick, for one added line).

The write needs a proof the read doesn't, and it is the subtle part:

> **A file being append-only does not make its rows append-only.** A
> trunk's cached rows carry its subagents' transcripts, spliced in at
> their anchors, so a subagent still running — the normal case under
> `watch` — grows a block in the *middle* of the row sequence while the
> trunk file only gained lines at the end.

So `_appended_rows` offers rows only when the list is provably just the
file's own parsed lines: resumed from a verified prefix, no agent
references, no sidecars, nothing spliced, and no length change from the
whole-file passes. That covers 136 of the reference archive's 185 trunk
files; the 26% that reference subagents take the unchanged full rewrite.
Beneath it, `extend_cached_entries` independently refuses when the table
no longer holds the row count the caller thinks it wrote — the guard
against another process having rewritten them. With the gates removed the
caller offers a *wrong 96-entry slice* and that check catches it, so the
tests assert on the offer rather than only on the write. That count and
the insert run inside one `BEGIN IMMEDIATE`, because Python's sqlite3
opens a transaction on the first write and not on a `SELECT`: without
the explicit lock a second writer sharing the cache could append in
between, and the check would let both appends land.

Both cache writers are stamped with the source file's `(mtime, size)` as
captured **before** the parse — the same discipline as the sidecar
fingerprint beside it, and as the entry store's own stamp. A session
appended to mid-read would otherwise be recorded at the size it reached
while the rows are only what was parsed, marking an incomplete cache
current; stamped with what was parsed, the growth invalidates instead.

Steady-state tick on the 803MB reference archive: **0.717s → 0.257s**
(cumulatively 1.03s → 0.26s). Equivalence is held at three levels — parse
output against the text path (162 fixture files whole-file, 90 resumed),
**cache DB state** against a full-rewrite run over 6 ticks with 3 files
growing, and rendered HTML throughout. DB state is the bar for the same
reason as §2.14: the first bug of this kind is invisible in the rendered
bytes.

Resumption only helps a resident loop — a one-shot run, the TUI, and
every tick-one still parse whole. Persisting `(prefix_len, prefix_hash)`
in `cached_files` would extend it across processes; that migration was
considered and not needed for the case that motivated it.

---

## 3. Data lifecycle

```
                 ┌──────────────────┐
                 │  JSONL file(s)   │
                 │ (~/.claude/...)  │
                 └────────┬─────────┘
                          │
                  parser.py + factories/
                          │
                          ▼
              ┌───────────────────────┐
              │ list[TranscriptEntry] │  (typed Pydantic models)
              └───────────┬───────────┘
                          │
                  factories/ dispatch
                          │
                          ▼
            ┌─────────────────────────┐
            │ list[TemplateMessage]   │  (each carrying a typed
            │  with MessageContent    │   MessageContent variant)
            └─────────────┬───────────┘
                          │
              renderer.py (generate_template_messages):
                build DAG → pair → reorder → relocate
                subagent blocks → build hierarchy →
                cleanup sidechain dups → populate caches
                          │
                          ▼
               ┌──────────────────────┐
               │ Tree of TemplateMsg  │
               │  + RenderingContext  │  (caches: teammate_colors,
               │  + nav data          │   task_subjects, etc.)
               └──────────┬───────────┘
                          │
      ┌────────────┬─────────────┴─────────────┬────────────┐
      ▼            ▼                           ▼            ▼
html/renderer.py   markdown/renderer.py    json/renderer.py
      │                  │                      │
      ▼                  ▼                      ▼
 index.html +        *.md                   combined_transcripts.json
 session-*.html      (single file)          session-*.json
                                            all-projects-summary.json
      │                  │                      │
      └──────────────────┼──────────────────────┘
                         │
              ┌──────────┴────────────┐
              ▼                       ▼
          cache.py              image_export.py
          (SQLite)              (HTML / Markdown only —
                                 JSON serialises paths)
```

Cache reads/writes happen *in parallel* with the main pipeline:
`cache.py` is consulted before parsing (cache hit → skip parse), after
rendering (write the rendered HTML), and during TUI navigation (the
TUI never re-parses).

---

## 4. Cross-cutting glossary

Terms that appear across multiple subsystems — defined once here.

- **TranscriptEntry**: typed Pydantic model for a single line in the
  source JSONL. Variants: `User`, `Assistant`, `Summary`, `System`,
  `Passthrough`, `QueueOperation`. See
  [`parser.py`](../claude_code_log/parser.py) and
  [`models.py`](../claude_code_log/models.py).

- **MessageContent**: render-time content variant produced by the
  factories from `TranscriptEntry`. Many flavours
  (`UserTextMessage`, `ToolUseMessage`, `TeammateMessage`, …). One
  `TranscriptEntry` may yield multiple `MessageContent`s (a single
  assistant turn with N tool_uses produces N+1 messages). See
  [messages.md](messages.md) for the full taxonomy.

- **TemplateMessage**: the render-time wrapper around a
  `MessageContent`. Carries `message_index`, parent/child links,
  pair_first/pair_middle/pair_last, ancestry, and the renderer-format
  CSS classes. Defined in [`renderer.py`](../claude_code_log/renderer.py).

- **RenderingContext**: mutable cache attached to one render pass.
  Holds the message registry plus nested per-session caches
  (`teammate_colors`, `task_subjects`, `task_id_for_tool_use`,
  `session_first_message`, etc.). Caches are session-scoped because
  combined-transcripts mode merges multiple sessions and per-session
  identifiers (teammate_id, task_id) aren't globally unique.

- **session_id**: the JSONL's `sessionId` field. Often a UUID string.
  In some renderer paths a *synthetic* form is used:
  - `{trunk}#agent-{agentId}` for sub-agent transcripts (so they
    form a separate DAG-line attached to their spawning trunk).
  - `{trunk}@{first_uuid_prefix}` for branch sessions (rewinds /
    parallel-tool_use forks). See [dag.md](dag.md).

- **render_session_id**: the session id that should be used when
  walking `ctx.messages` to find content for rendering, accounting
  for synthetic rewrites.

- **sidechain**: a sub-agent's transcript entries are flagged
  `isSidechain: true`. The DAG layer integrates them into the parent
  session's tree under the spawning Task/Agent tool_use anchor. See
  [agents.md](agents.md), [dag.md](dag.md).

- **agent_id**: identifier copied from a Task/Agent tool_result
  (either `toolUseResult.agentId` or parsed from the Markdown
  metadata tail). Used to stitch sub-agent JSONL files into the
  trunk DAG. See [agents.md](agents.md).

- **workflow run**: one execution of the `Workflow` tool — a JS
  orchestrator fanning out into phase-grouped side-channel sub-agents,
  left on disk under `<sid>/subagents/workflows/<runId>/`. Parsed by
  `workflow.py` into a `WorkflowRun` and spliced into the message tree
  at the Workflow tool_use site. See [workflows.md](workflows.md).

- **fork point** / **branch**: when a session has multiple children
  with the same parent, the parent is the fork point and each child
  initiates a branch. Real forks come from `/exit` rewinds; spurious
  forks (parallel tool_uses, structural-only siblings) are collapsed
  by `_walk_session_with_forks`. See [dag.md](dag.md).

- **SessionHeaderMessage**: the synthetic content type produced for
  every session boundary in the rendered output — the header that
  appears above each session's first real message. Two flavours:
  *trunk* headers for top-level sessions, and *branch* headers for
  fork branches (the "branch heading" you'll see referenced in bug
  reports). Both headers are constructed by `_build_trunk_header` /
  `_build_branch_header` (in `renderer.py`); the branch header's
  title is composed by `_branch_label` in the shape `Branch •
  <uuid8> • <preview>`, with the preview computed once by scanning
  the branch's DAG-line uuids for the first user entry with text
  (via `extract_text_content` in `parser.py` + `create_session_preview`
  in `utils.py`, which calls `simplify_command_tags` to strip raw
  `<command-name>` XML soup down to `/cmd`). When troubleshooting
  branch-heading rendering, those are the functions to inspect.

- **pair_first / pair_middle / pair_last**: a pair of messages
  rendered as one logical unit (tool_use + tool_result, Slash + UserSlash,
  thinking + assistant). `pair_middle` exists for triples — currently
  the slash-command `(UserSlash → Slash → CommandOutput)` shape.

- **depth**: see § 2.6.

- **detail-aware tools**: the curated set of tools whose I/O survives
  `--depth agent` because they convey *what the agent did*, not *what
  it read* (`WebSearch`, `WebFetch`, `Task`, `Agent`).

- **passthrough**: a `PassthroughTranscriptEntry` is a non-conversation
  entry (hook callbacks, progress updates, last-prompt markers). The
  DAG layer keeps them in the structure but the renderer typically
  hides them.

---

## 5. Where to start reading

Common entry questions and their best first stop:

- "How does a JSONL line become an HTML row?"
  → [rendering-architecture.md](rendering-architecture.md).
- "Why are forks rendered weirdly / what is a branch session?"
  → [dag.md](dag.md).
- "What message types exist and what do they look like?"
  → [messages.md](messages.md) plus the samples in `messages/`.
- "I want to add support for a new Claude Code tool."
  → [implementing-a-tool-renderer.md](implementing-a-tool-renderer.md).
- "I want to write a third-party plugin (e.g. for an MCP tool we
  don't ship)."
  → [plugins.md](plugins.md).
- "How does folding / collapsible content work?"
  → [message-hierarchy.md](message-hierarchy.md).
- "What CSS classes does a message div get?"
  → [css-classes.md](css-classes.md).
- "How are sub-agent transcripts (sync, async, teammates) integrated?"
  → [agents.md](agents.md), then [teammates.md](teammates.md) for the
  teammates-specific machinery.
- "How does a dynamic-workflow run (phases, agents, orchestrator
  script) get rendered?"
  → [workflows.md](workflows.md).
- "I want to extend the cache / change the schema."
  → § 2.3, § 2.4 here, then read the migration files in order.
- "How do I export to JSON for downstream tooling?"
  → § 2.5 here (and `--format json` from § 2.1).
- "claude-code-log is hung — how do I see what it's doing?"
  → § 2.11 (`SIGUSR1` stack dump).
- "What's planned but not implemented?"
  → [`work/`](../work/) — each `.md` is an in-flight or proposed plan.

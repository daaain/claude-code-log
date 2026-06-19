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
| Detail-level filter | renderer.py § Detail-level filtering, `models.DetailLevel` | inlined below (§ 2.6) |
| Image export | [`image_export.py`](../claude_code_log/image_export.py) | inlined below (§ 2.7) |
| Performance profiling | [`renderer_timings.py`](../claude_code_log/renderer_timings.py) | inlined below (§ 2.8) |
| Diagnosing hangs (SIGUSR1) | [`cli.py`](../claude_code_log/cli.py) `_install_stack_dump_signal` | inlined below (§ 2.9) |
| Adding a new tool renderer | [`factories/tool_factory.py`](../claude_code_log/factories/tool_factory.py), `html/tool_formatters.py` | [implementing-a-tool-renderer.md](implementing-a-tool-renderer.md) (how-to) |
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
- `--detail {full,high,low,minimal,user-only}` — drop content from
  the rendered output (§ 2.6).
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

CLI orchestration delegates to `converter.py` (which owns the
high-level "load + render + write" flow) and never touches `renderer.py`
directly. Output paths follow a stable convention so the cache and
re-renders can find existing files: `combined_transcripts.html`,
`session-{id}.html`, `index.html`, with `--detail` and `--compact`
adding suffixes per `utils.variant_suffix`.

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
  source JSONL is deleted).
- Per-rendered-HTML: the HTML output itself, indexed by source file
  mtime + detail-level + compact flag (migrations 002–004) — so
  re-runs with unchanged inputs serve the cached HTML directly.

Invalidation is mtime-based: when a JSONL's mtime is newer than its
cache row, the session is reparsed. The schema-version row also
invalidates the entire HTML cache when migrations bump the version,
since rendered output may have changed even when source data hasn't.

For the operations / recovery side (archived sessions, manual
deletion, `cleanupPeriodDays`), see
[`docs/restoring-archived-sessions.md`](../docs/restoring-archived-sessions.md).

### 2.4 Migrations

[`claude_code_log/migrations/`](../claude_code_log/migrations/) is a
small migration system. Each migration is a `NNN_description.sql` file
applied in numeric order by `migrations/runner.py`. The schema-version
table tracks which migrations have run; `cache.py` invokes the runner
on every connection open, so a fresh checkout running against an old
cache DB transparently upgrades.

Current migrations:

- `001_initial_schema.sql` — sessions table + per-message metadata.
- `002_html_cache.sql` — adds the rendered-HTML cache layer.
- `003_html_pagination.sql` / `004_html_pagination_variant.sql` —
  per-page HTML chunks for `--page-size`.
- `005_session_team_name.sql` — adds `team_name` to sessions for the
  teammates feature (PR #125).

Recreating-tables migrations toggle `PRAGMA foreign_keys = OFF/ON`
around the rebuild to avoid losing rows to cascade-deletes during the
swap.

### 2.5 JSON export

[`claude_code_log/json/`](../claude_code_log/json/) is a thin renderer
that mirrors `HtmlRenderer` / `MarkdownRenderer`: same
`generate(...)` / `generate_session(...)` / `generate_projects_index(...)`
surface, same `--detail` and `--compact` honoring. Output is a
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
  unchanged outputs.
- `combined_transcripts.json` per project; `session-{id}.json` for
  individual sessions. The naming respects `variant_suffix` for
  detail/compact variants.

The projects-index JSON (`all-projects-summary.json`) is a parallel
top-level file — same shape as HTML's `index.html` but consumable by
external tools (dashboards, query scripts, `jq` pipelines).

### 2.6 Detail-level filter

The `--detail` flag (and `models.DetailLevel`) lets users dial down
how much of the transcript renders:

- `full` (default) — everything.
- `high` — detailed but cleaned: drops system/hook noise while
  keeping the full conversation and tool I/O.
- `low` — drops most tool I/O, keeps the conversation plus a curated
  set of "interaction signal" tools (WebSearch, WebFetch, Task, Agent —
  the ones that show *what the agent did*, not *what it read*). See
  `_LOW_KEEP_TOOLS` in [`renderer.py`](../claude_code_log/renderer.py).
- `minimal` — drops all tool I/O.
- `user-only` — drops everything except user messages and steering
  (designed for feeding to downstream agents, e.g. building a
  requirements doc).

Recaps (`AwaySummaryMessage`) are a cross-cutting exception: they are a
high-level summary of activity, so they stay visible at *every* level
(`detail_visibility = USER_ONLY`), including `user-only`. The `--no-recaps`
flag suppresses them at all levels — giving `--detail user-only --no-recaps`
for a truly user-only view, or `--detail minimal --no-recaps` to drop the
recap/agent redundancy (#179).

Filtering happens in a single *post-render* pass on `TemplateMessage`:
`_ghost_template_by_detail` sets each non-visible slot in
`RenderingContext.messages` to `None` ("ghosting"), keyed by the content
class's `detail_visibility` predicate (plus the `_LOW_KEEP_TOOLS`
allowlist at `low` and sidechain dropping below `FULL`). Indices stay
stable — surviving messages keep their `message_index`, so there is no
reindex; the rendered tree simply skips ghost slots. Earlier revisions
ran a *second*, pre-render `_filter_by_detail` pass on `TranscriptEntry`
plus a `_reindex_filtered_context` remap after every deletion; the
ghosting model collapsed both into this one axis.

Important interaction: `_pair_skill_tool_uses` also ghosts in place (the
slash-command body and the redundant "Launching skill" tool_result).
Because anchor-target references can be cached before a slot is ghosted —
a branch header's `parent_message_index`, `session_first_message`
entries, junction forward-links — each ghosting step sanitizes them
afterward: `_pair_skill_tool_uses` calls `_drop_anchor_refs_into_ghosts`
and `_ghost_template_by_detail` calls `_repair_stale_anchor_refs`, so no
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

### 2.8 Performance profiling

[`renderer_timings.py`](../claude_code_log/renderer_timings.py)
provides `log_timing(label, t_start)` context managers used throughout
`renderer.py`. Set `CLAUDE_CODE_LOG_DEBUG_TIMING=1` to print per-phase
times to stderr — useful for spotting which phase regressed when a
large transcript suddenly takes seconds longer than before.

### 2.9 Diagnosing hangs (SIGUSR1 stack dump)

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

- **detail level**: see § 2.6.

- **detail-aware tools**: the curated set of tools whose I/O survives
  `--detail low` because they convey *what the agent did*, not *what
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
  → § 2.9 (`SIGUSR1` stack dump).
- "What's planned but not implemented?"
  → [`work/`](../work/) — each `.md` is an in-flight or proposed plan.

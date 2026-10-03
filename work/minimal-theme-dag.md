# `--theme minimal`: a compact light/dark theme with a DAG layout

Status: **spec + phased plan; P1, P2, P3a, P3b, P4, P5 and P6 landed (see § 6);
§ 7 decisions 1–5 taken.** Branch of origin:
`claude/sweet-mccarthy-64a24r`.

This file is the single source of truth for the feature. Each phase in
§ 5 is written to be executed by a fresh agent with no other context: read
§ 1–4 once (they are short relative to the code they describe), then only
your phase section. When a phase lands, tick it in § 6 and record anything
that diverged from this plan **in this file**, in the same commit.

Mockups (interactive "Design Component" files agreed with the user) are
committed next to this file in
[`work/minimal-theme-dag-mockups/`](minimal-theme-dag-mockups/). They only
render inside the design canvas (they need a `support.js` runtime that is
not in the repo), so treat them as **source code to read**, not pages to
open. Everything load-bearing from them is restated below; read them when
you need exact pixel values or want to see the algorithm in context.

| File | What it is | Read it for |
|---|---|---|
| `MinimalLook.dc.html` (was "Hairline C") | The chosen look: time rail with role dots | CSS tokens, row grid, collapse previews, fold-depth toolbar (`renderVals` of the depth/theme state) |
| `DagRail.dc.html` | DAG engine, branches folded by default | **The packing + rail-slot algorithm** in `renderVals()` (lines ~221–381), the rail slot CSS (`.sl`, `.v`, `.vt`, `.vb`, `.dash`, `.h`, `.r`, `.cd`, `.cu`, `.dot`) |
| `DagThreads.dc.html` | Same engine, default interleaved, with indentation + tint for interleaved rows | The `.c3.in-*` tint rules (diff it against `DagRail`) |
| `DagLanes.dc.html` | Same engine, default columns | Column header / collapsed strip / grid-template-columns |

The mockups use Google Fonts (JetBrains Mono, Source Sans 3). **The
product must not**: system font stacks only (see § 1.2).

---

## 1. Design spec (agreed with the user, refined against the code)

### 1.1 Goal and invariants

- An **opt-in** theme for HTML transcript output, selected with
  `--theme minimal` (or `CLAUDE_CODE_LOG_THEME=minimal`). The existing
  look is the `classic` theme; `--theme default` means "the built-in
  default", resolved through `utils.DEFAULT_THEME` (currently `classic`,
  so switching the default later is a one-line change). Decided in P2,
  see § 7 decision 7.
- **Default (classic) output stays byte-identical** once the plumbing lands. The
  only phase allowed to change default bytes is P1 (groundwork bug fixes,
  explicitly approved, regenerated with `just update-snapshot` and
  reviewed at block level per CONTRIBUTING "Recognising the race").
  Every later phase must show **zero** diff in
  `test/__snapshots__/test_snapshot_html.ambr` for existing snapshot
  names (new snapshot names for the minimal theme are expected — a
  purely additive `+N/-0`).
- Light + dark; much denser vertically; long content collapses to a
  preview; sub-agents and forks drawn as a DAG.
- Works offline: no web fonts, no new network fetches. (The existing
  vis-timeline lazy load from unpkg is pre-existing and stays as is.)
- Without JavaScript the page still reads correctly: nested, as today,
  styled by the minimal CSS.
- Timeline and filter parity per CLAUDE.md: whatever the filter hides in
  the transcript it hides in the timeline, in every branch mode.

### 1.2 Look

- No cards, shadows or gradient background. Plain `--bg`. Content column
  `max-width: 960px`, centred, 16px side padding (10px under 640px);
  widens (`max-width: none`) when any swimlane column is open.
- Each message is a row:
  `[~58px gutter: time (mono, muted) above role label (small mono,
  role-coloured) and tokens] [~18px rail: role-coloured dot on a
  continuous vertical hairline] [content]`.
  Messages are separated by spacing only (3px vertical padding); turns
  (user prompts) are separated by a single 1px `--rule2` rule.
- Fonts — system stacks only:
  - `--sans: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif`
  - `--mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace`
- User prompt text: `--userbg` tint, 4px radius, `2px 8px` padding.
  Thinking: muted italic. Tool call line: mono `.8em/1.6`, details in
  `--muted`. Tool output: `--code` box, mono `.78em/1.55`, 4px radius.
  Tool errors: `--errbg` box with `--err` text plus an `exit N`/`error`
  pill in the gutter. Diffs: `--add`/`--addbg`, `--del`/`--delbg` full
  bleed lines. Hooks/system lines: `.9em`, muted.
- Gutter on phones (<640px): rail moves to column 1, gutter becomes a
  single row above the content (see the mockup's `@media` rule).

**Palette** (verbatim from the mockups; light on the left, dark on the
right). These become custom properties on `:root` under the selectors in
§ 1.3.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#ffffff` | `#14161a` | page |
| `--fg` | `#1f2328` | `#e4e6e9` | text |
| `--muted` | `#5f6670` | `#969da7` | metadata, dim text |
| `--rule` | `#e3e6ea` | `#2a2e35` | hairlines, borders |
| `--rule2` | `#1f2328` | `#c9ccd1` | turn separator, header rule |
| `--code` | `#f3f4f6` | `#1d2025` | code / output boxes |
| `--userbg` | `#fbf2e8` | `#2a2118` | user prompt tint |
| `--user` | `#a14a00` | `#f0a35c` | user role |
| `--asst` | `#6d28d9` | `#b8a1f8` | assistant role |
| `--tool` | `#1a7f37` | `#6fcf8f` | tool role |
| `--sys` / `--warn` | `#8a5d00` | `#e0b45a` | system / warnings |
| `--err` | `#b42318` | `#ff8b84` | errors |
| `--errbg` | `#fdeceb` | `#3a1b1b` | error box |
| `--note` | `#1f5fbf` | `#7cb4ff` | async results, links |
| `--ring` | `#2f8f46` | `#6fcf8f` | agent nest line |
| `--add` / `--addbg` | `#116329` / `#e6f6eb` | `#86e0a2` / `#14291c` | diff add |
| `--del` / `--delbg` | `#a40e26` / `#fdeaec` | `#ff9da2` / `#331a1d` | diff del |
| `--ok` / `--okbg` | `#116329` / `#dff3e5` | `#86e0a2` / `#14291c` | success pill |
| `--l0` | `#9aa1aa` | `#5d646e` | main lane line |
| `--lA` | `#2f8f46` | `#6fcf8f` | lane colour 1 |
| `--lB` | `#1e6fd9` | `#7cb4ff` | lane colour 2 |
| `--lF` | `#b0307a` | `#f08bc4` | lane colour 3 / forks |

Lane colours cycle `lA, lB, lF` by rail slot (add two more — e.g. reuse
`--sys` and `--asst` — if a fourth/fifth concurrent slot is common in
practice; decide in P6 from the fixtures).

### 1.3 Dark mode

- Follows `prefers-color-scheme` by default; an in-page Auto / Light /
  Dark segmented toggle (mockup `.seg.icons`, inline SVG icons) persists
  the choice in `localStorage` key `claude-code-log:theme`
  (`auto|light|dark`, same key style as the existing
  `claude-code-log:user-view`). All storage access in `try/catch`.
- Mechanism: `data-theme="light|dark"` on `<html>`, absent for auto.
  Token blocks:
  ```css
  :root { /* light tokens */ }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) { /* dark tokens */ }
  }
  :root[data-theme="dark"] { /* dark tokens */ }
  ```
  Also set `color-scheme: light dark` (and `color-scheme: dark` / `light`
  in the forced blocks) so form controls and scrollbars follow.
- Apply the stored choice from a tiny inline `<script>` in `<head>`
  (before the stylesheet paints) to avoid a flash.
- Pygments: the existing light token CSS stays; a generated
  **github-dark** token sheet (Pygments 2.20 ships `github-dark`) is
  scoped under both dark selectors. Generated by a committed script with
  a drift test (P3b).
- Timeline (vis-timeline): item and group colours must come from CSS
  custom properties so they follow the theme. P1 moves the JS-inlined
  colours into CSS classes; the minimal theme then just redefines the
  variables. Where JS genuinely needs a colour value (none expected after
  P1), read it with `getComputedStyle(document.documentElement)`.

### 1.4 Collapse

- Long content shows a 2–3 line preview with a fade, a `+N lines`
  button reveals all, `− less` collapses. **Restyle the existing
  `<details>` collapsibles** — they already carry previews:
  - `details.collapsible-code` (`html/utils.py::render_collapsible_code`)
    — has `<span class='line-count'>N lines</span>` + `.preview-content`
    in the `<summary>`, `.code-full` body.
  - `details.collapsible-details` (`html/tool_formatters.py` ~543,
    ~1574, ~1606) — `.preview-content` in the summary, `.details-content`
    body, **no line count**.
  - `details.tool-param-collapsible` (params tables) — leave behaviour,
    restyle only.
- Preview height: `--pv: 4.4em` for code, `2.8em` for prose; mask
  `linear-gradient(#000 45%, transparent)` (mockup `.clip`).
- The `+N lines` label: from `.line-count` where present; otherwise the
  minimal JS computes it from the body text (`\n` count) and writes it to
  a `data-more` attribute on the `<details>` (rehydrate-safe, see § 3.4).
  **No formatter changes** — formatter output is shared with the default
  theme and with the fragment store.
- `− less`: when open, the summary shows only a small `− less` affordance;
  the minimal JS also appends one `− less` button after the body of long
  open blocks (closes the `<details>` and scrolls its summary into view).

### 1.5 Fold depth and toolbar

- A compact sticky top toolbar replaces the floating buttons in this
  theme. It holds: fold depth (Prompts / Steps / All), Branches (Main
  only / Interleaved / Columns), search & filter, timeline, colour theme
  toggle, and — in a small overflow group — the remaining existing
  controls (details toggle, `uuid` debug, `md`/`raw`, resume, follow,
  scroll-to-top). **Every existing feature stays reachable.**
- **Implementation rule:** the toolbar contains the *same* button
  elements with the *same ids* as today's floating buttons
  (`toggleDetails`, `toggleTimeline`, `filterMessages`, `toggleDebug`,
  `toggleUserView`, `resumeSession`, `followUpdates`, scroll-top
  anchor). The existing JS binds by id and throws on `null`; moving the
  markup (minimal template branch only) and restyling `.floating-btn`
  under the theme keeps all of it working untouched.
- Fold depth maps onto the existing fold-bar state machine
  (`transcript.html` `applyFoldState(messageEl, 'folded'|'first'|'open')`,
  see dev-docs/message-hierarchy.md):
  - **Prompts** — session headers and branch headers `first`; every user
    message `folded` (each turn shows the prompt + its fold-bar summary
    line).
  - **Steps** (default, matches the mockup) — session/branch headers
    `first`; user messages `open` for main-lane descendants; every
    `<details>` closed (previews). Sub-agent subtrees are governed by the
    Branches mode (§ 1.6), not by fold depth.
  - **All** — everything `open` and every `<details>` open (same set as
    the existing "toggle all details").
  - Manual fold-bar clicks after choosing a depth override it locally
    (the segmented control then shows no active state until the next
    choice), mirroring the mockup's `ov` overrides.
- The fold bar renders in this theme as a single muted mono line with a
  rotating chevron (`.fold` / `.chev` in the mockup), using the existing
  labels (`get_immediate_children_label()`); both buttons of the
  two-button bar stay (left: one level, right: all levels).

### 1.6 DAG layout

**Branches** are (a) sub-agent transcripts — `Task`/`Agent` sidechains,
sync and async, at any nesting depth — and (b) rewind forks (branch
pseudo-sessions). **Teammates are not branches**: their spawn row gets a
hyperlink anchor to the corresponding message of the teammate thread
(§ 1.6.5). Workflow sub-agents (#174) stay nested as today (out of scope;
see § 7).

#### 1.6.1 Per-branch modes

- **folded** (DEFAULT — "Main only"): branch contents hidden. The spawn
  row shows a branch control: chevron + branch name + summary
  `N steps · tokens · duration` (mockup `.bctl`/`.fold`) and a
  `Column ⇥` mini button. The rail draws the branch lane as a **dashed**
  line from the spawn row to its merge row (sync: the spawn's
  tool_result; async: the task-notification; fork: none → a short
  stub).
- **interleaved**: branch messages merged into the main stream by
  timestamp, tagged with the lane name in the gutter (`.tag`), drawn on
  their own rail lane with curved fork/merge connectors (git
  `log --graph` style). Rows of interleaved branches get the
  DagThreads tint + indent (`.c3.in-*`). **Max 3 interleaved per branch
  group** (§ 1.6.3); selecting a 4th folds the least-recently-selected.
- **column** (swimlane): branch moved into its own column to the right,
  rows time-aligned with packing (messages in different columns may share
  a row; time order kept top to bottom). A column can collapse to a
  narrow vertical strip (34px, rotated label) and expand again; columns
  are unlimited; the page scrolls horizontally — the user decides when
  it's too squashed.

#### 1.6.2 Global control

`Branches: Main only | Interleaved | Columns` in the toolbar sets every
lane at once (Interleaved = first 3 of each branch group, the rest
folded; Columns = all). The segment shows "on" only when every lane
matches (mockup `bm`). Persist the global choice in `localStorage`
(`claude-code-log:branches`); per-lane overrides are in-memory only.

#### 1.6.3 Branch overflow ("+N more branches")

A **branch group** is the set of branches spawned within one user turn
(the nearest main-lane user message ancestor of the spawn row; for forks,
the fork point's turn). When a group has more than 3 branches, only the
first 3 (by spawn time) get controls and rail lanes; a
`+N more branches` toggle on the 3rd control's row reveals the rest
(controls + dashed lanes), so the whole tree stays reachable. The cap on
interleaving (3) applies per group, LRU by selection. **Decided (§ 7
decision 5):** the cap and the `+N more branches` overflow are per user
turn, not per page.

#### 1.6.4 Merge semantics

| Branch kind | Spawn row | Merge row | Lane id |
|---|---|---|---|
| sync agent | the `Task`/`Agent` tool_use card | its paired tool_result card (arrives at completion) | `agent-<agentId>` |
| async agent (`run_in_background`) | the tool_use card | the `<task-notification>` card (`task-notification` class, real arrival time on the main line) | `agent-<agentId>` |
| nested agent (#213) | the spawning tool_use inside the parent lane | its tool_result inside the parent lane | `agent-<agentId>` (parent lane recorded) |
| rewind fork | the fork-point card (the message carrying `junction_forward_links`) | none (stub; forks don't merge) | `branch-<branch sid>` |

**Which branch of a fork continues "main"?** At a rewind, the DAG layer
makes *every* child a branch pseudo-session (dev-docs/dag.md § 7); none
is the trunk. **Decided (§ 7 decision 3):** the **earliest** branch
continues the main lane (its branch header renders as a slim `⑂ rewound`
marker, its messages are main), later branches are fork lanes. This
matches the mockup (fork drawn from an earlier main row) and keeps lane
assignment stable when a new rewind appears during `serve --watch`.

#### 1.6.5 Teammates

Teammate spawns (`TaskInput.team_name` and `name` set; dev-docs
teammates.md) are excluded from lanes and modes. Today their transcript
renders inline, nested under the spawning tool_result (it lives in
`subagents/agent-*.jsonl` with a synthetic `{trunk}#agent-{id}` session
id — there is no separate per-teammate page). In this theme:

- the teammate subtree stays nested where it is today and is collapsed
  by default (no lane, no rail lane, never a DAG branch);
- the spawn row shows `→ <name>'s thread` linking to the first card of
  that thread (`#msg-d-N`, revealed through the existing
  `window.claudeLogRevealMessage` / `hashchange` handler);
- `SendMessage` cards and `<teammate-message>` cards link to the matching
  message in the other side's thread where it can be resolved (match on
  the message text / `sender_task_id`; an unresolved link renders no
  anchor rather than a dead one).
- If a teammate thread turns out to live in a different session file
  (a future Claude Code layout), link with the existing stable deep link
  `session-<sid><suffix>.html?uuid=<uuid>` (handled by
  `revealMessageByUuid` in `transcript.html`).

**Decided (§ 7 decision 4):** keep the threads nested, collapsed by
default, not DAG branches; links go to the matching anchor on the same
page.

#### 1.6.6 Architecture (validated)

The suggested architecture holds, with one refinement forced by live
updates:

- **Server** renders branch messages in their current DOM positions
  (nested `.message-node > .children`, as today), adding `data-*`
  attributes in the minimal theme only (§ 3.3).
- **A JS module** (`minimal_dag.js`) re-lays out the page at runtime
  **without moving any DOM node**: when active it adds `dag-on` to the
  stage, which turns `.message-node` and `.children` into
  `display: contents` so every card becomes a grid item of one CSS grid,
  and assigns each visible card an inline `grid-row` / `grid-column`.
  Folded/hidden branch cards get a class (`dag-hidden`).
  *Why not move nodes:* `live_update.js` patches cards **in place** and
  relies on the nested structure (`stableKeys`, `applyOwn`, fold bars
  re-synced from their own `.children`); the fold state machine,
  `revealMessage`, search's ancestor walk and the filter all depend on
  nesting too. With `display: contents` all of them keep working, and a
  fold (inline `display:none` on a `.children`) still hides the subtree
  because an inline `display` beats the class rule.
- **Ownership split** (no shared mutable state):
  - fold state machine owns `.children` inline `display`;
  - filter/search own `filtered-hidden` / `search-hidden` classes;
  - the DAG engine owns `dag-*` classes and inline `grid-row`,
    `grid-column` on cards, plus everything inside `#dag-rail`.
- **Rail:** main-lane line + dots are pure CSS (`.message::before` dot,
  stage `::before` line) so they also work without JS. Branch lanes and
  connectors are an **SVG overlay** in `#dag-rail`, a sibling of
  `#transcript` inside the stage (outside the live-update swap target),
  drawn from measured card boxes (`getBoundingClientRect`, batched reads)
  and redrawn on `ResizeObserver`. Port the geometry of the mockup's slot
  CSS (2px lines, 8px-radius curves `cd`/`cu`, dashed `3px/4px`, 8px dot
  with a 2px `--bg` ring at `--dy = 3px + .7em`). Rationale for SVG over
  the mockup's per-row CSS slot pieces: per-row slot elements would be
  rows × slots extra nodes inside the grid (tens of thousands on a large
  page) and would have to live inside the swap target.
- **Relayout triggers:** one `MutationObserver` on `#transcript`
  (`subtree`, `childList`, `attributes` with
  `attributeFilter: ['class','style']`), coalesced to one
  `requestAnimationFrame`, disconnected while the engine applies its own
  writes; plus `claudeLogOnRehydrate` (live update), `resize`, and the
  toolbar controls. The engine is a pure function of (DOM, mode state),
  so re-running it is always safe.
- **No JS:** the stage has no `dag-on`, nothing is `display: contents`,
  the minimal CSS renders the nested tree (sidechain `.children` get a
  2px `--ring` left border like the mockup `.nest`).

---

## 2. Codebase reality — findings that shape the plan

### 2.1 Groundwork defects found (P1 fixes them)

1. **Undefined CSS custom properties** (no definition anywhere, no
   fallback) — the declarations using them are silently invalid today:
   - `--color-bg-secondary`, `--color-bg-tertiary` —
     `message_styles.css` (~1281, ~1286), `pygments_styles.css` (5, 44,
     88, 102, 166)
   - `--color-blue`, `--color-green`, `--color-purple`,
     `--color-border-dim`, `--color-text-dim`, `--color-text-secondary` —
     `pygments_styles.css`
   - `--code-bg` (`message_styles.css` ~426, ~466; `--code-bg-color` is
     the defined one), `--secondary-text` (~406, ~416)
   - `--font-mono` has fallbacks (`message_styles.css` ~281, ~1796) and
     `--accent-color` has a fallback (`session_nav_styles.css` 54) — fix
     for consistency (`--font-monospace` is the defined name).
   Re-derive the list with the one-liner in P1 rather than trusting it.
2. **`.line-count` selector mismatch** — `pygments_styles.css:78`
   targets `.tool-result .line-count`, but the card class is
   `tool_result` (underscore; `html/utils.py::CSS_CLASS_REGISTRY`). The
   rule never matches. (`.tool-result-json`, `.tool-result-image` are
   unrelated inner classes.)
3. **Timeline colours inlined in JS** —
   `components/timeline.html`: `messageTypeGroups` (lines ~25–43) sets
   `style: 'background-color: #…'` per group; the container `<div>`
   (line ~5) and resize handle (~8–9) carry inline colours
   (`background: white`, `#ddd`, `#999`); `onTimelineSelect` (~304)
   flashes `#fff3cd` via `style.backgroundColor`. Item colours are
   already CSS (`timeline_styles.css` `.vis-item.timeline-item-*`), but
   with literals.

### 2.2 How CSS and JS reach the page

- One template, `claude_code_log/html/templates/transcript.html`, inlines
  every stylesheet in `<head>` (lines 11–23, `{% include %}` of
  `components/*.css`) and every script (timezone, live update, the big
  `DOMContentLoaded` block ~314–1183, `components/timeline.html`,
  `components/search.html`).
- Jinja env: `html/utils.py::get_template_environment()` — **no
  `trim_blocks`/`lstrip_blocks`**, so every `{% if %}` line leaves its
  newline in the output. For byte-identity put theme conditionals
  **inline**, glued to existing text, e.g.
  `<div id="transcript"{% if minimal %} class="…"{% endif %}>` and
  `{% include 'components/teammate_styles.css' %}{% if minimal %}
  …{% endif %}` — never on their own line in shared regions.
- `HtmlRenderer._generate_inner` (`html/renderer.py` ~1713–1803) renders
  `transcript.html` with a fixed kwarg set; add `theme` there.
- `generate_projects_index` (`index.html`) and
  `generate_archive_search_html` (`archive_search.html`) are separate
  templates; out of scope for the theme (§ 7).
- Pagination relies on literal markers in the page header:
  `<!-- PAGINATION_NEXT_LINK_START -->`…`class="page-nav-link next`…
  `<!-- PAGINATION_NEXT_LINK_END -->` (`converter.py` `_NEXT_LINK_PATTERN`,
  and a 512KB bounded-read assumption that the block sits right after
  the inlined `<style>`). The minimal template branch must keep that
  markup verbatim and must not push it past ~512KB (it is a performance
  guard, not correctness).
- **The `#transcript` rehydrate contract** (`transcript.html` 27–53):
  anything that decorates cards after load registers with
  `window.claudeLogOnRehydrate(fn)`; delegated listeners on `document`
  must **not** be registered there.

### 2.3 Option plumbing: the `no_recaps` precedent

`--no-recaps` is the most recent render-variant flag and touches every
path the theme must. Follow it file by file:

- `utils.py::variant_suffix(depth, compact, format, no_timestamps,
  no_recaps)` — filename infix; each variant gets its own files and
  **its own cache rows** (html_cache is keyed by filename; html_pages by
  `variant_suffix`, migration 004).
- `renderer.py`: `Renderer` class attrs (~5342: `depth`, `compact`,
  `no_recaps`), `get_renderer(...)` (~5705) sets them.
- `converter.py` functions carrying `no_recaps`:
  `_render_page_unit_inline`, `_generate_paginated_html` (+ inner
  `_render_page_inline`), `_stream_paginated_conversion`,
  `_try_current_or_session_scoped`, `_try_streaming`,
  `convert_jsonl_to`, `_generate_individual_session_files`,
  `generate_single_session_file`, `render_normalized_session_file`,
  `render_provider_wholesale`, `process_projects_hierarchy`,
  `_process_projects_hierarchy` (+ `_conversion_kwargs`, which feeds the
  spawn-pool workers). `variant_suffix` call sites: ~2771, ~3222–3223,
  ~3677, ~3820, ~5053, ~5403, ~5680, ~6521.
- `render_pool.py`: `_WorkerSetup` (~141), `_build_worker_renderer`
  (~484), `make_render_pool` (~691). `render_dispatch.py`:
  `build_render_pool` (~89, ~158).
- `html/renderer.py::generate_session` (~1842) builds the combined
  back-link from `variant_suffix(...)`. (After § 7 decision 2 the theme
  shares filenames, so neither `variant_suffix` nor the back-link carry
  it; P2 threads `theme` beside `no_recaps` everywhere else.)
- `cli.py`: `convert` options (~1104–1124 is where `--no-timestamps` /
  `--no-recaps` live), `_render_provider_input_file`,
  `_run_provider_wholesale`, `serve` (calls
  `process_projects_hierarchy(projects_path, silent=True)` at startup and
  in `reconvert`), `watch` (`process_projects_hierarchy` /
  `convert_jsonl_to` in its `convert` closure).
- `tui.py::SessionBrowser._ensure_session_file` (~1811) hard-codes
  `session-{id}.{ext}` and uses `get_renderer(format)` (no depth either);
  `run_session_browser` is launched from `cli.py::_launch_tui_with_cache_check`.

### 2.4 Staleness: the theme lives in the generator stamp

*Superseded recommendation:* this section originally proposed making the
theme a filename variant (`combined_transcripts.minimal.html`, …). The
user decided otherwise (§ 7 decision 2): **`--theme minimal` overwrites
the normal output files** (`combined_transcripts.html`, `_N` pages,
`session-*.html`, `index.html`). That rules out filename keying, so the
theme has to be part of every staleness decision instead.

As built in P2 — the cleanest persisted marker turned out to be the one
every check already reads: the generator comment on line 2 of each page.

- Classic: `<!-- Generated by claude-code-log v1.2.3 -->` (unchanged, so
  classic bytes are identical to pre-theme output).
- Minimal: `<!-- Generated by claude-code-log v1.2.3 theme=minimal -->`.
- `html/renderer.py::html_generator_stamp(theme)` is the expected stamp;
  `check_html_version` reads the whole stamp back, `check_html_theme`
  parses the theme out of it.
- `HtmlRenderer.is_outdated` (via `renderer.theme`),
  `renderer.is_html_outdated(path, theme)` and the cache's
  `is_transcript_stale` / `get_stale_sessions` / `is_page_stale`
  (`theme=` keyword) compare against the stamp a page rendered *now*
  would carry, so a page of the other theme is `file_version_mismatch`
  on every path: single-file, paginated, streaming, session-scoped,
  render pool, the all-projects plan, provider wholesale, TUI export,
  `watch`/`serve`.
- Cache rows (`html_cache`, `html_pages`) are shared between themes — no
  migration, no theme column. A row's `library_version` matches, and the
  file sniff that follows catches the theme.
- Older releases parse the whole stamp as the version, so they see a
  minimal page as outdated too.
- A changed built-in default is caught as well: only resolved names are
  stamped, so an archive built with `--theme default` (= classic) is
  stale once `DEFAULT_THEME` names another theme.
- `--combined no` never writes the combined output, yet its session
  pages link back to it. To keep archives single-themed, a `--combined
  no` run (every `watch` tick) whose combined output carries the other
  theme is promoted to rewrite it once
  (`converter.combined_theme_mismatch`, used by `convert_jsonl_to` and
  `_plan_project`).

The pre-#159 `--detail minimal` filename collision
(`combined_transcripts.minimal.html`) is moot: no theme filename exists.

### 2.5 Where branch data lives in the render tree

All of this is in `renderer.py` unless noted; see dev-docs/agents.md,
dag.md, teammates.md, message-hierarchy.md.

- **Tree shape** — `_build_message_hierarchy` (~2437) assigns levels
  (`_get_message_hierarchy_level` ~2337): session header 0, branch header
  0.5, user 1, assistant/thinking/system-cmd 2, tools/system-info/hooks/
  task-notification 3, sidechain user/assistant/thinking 4, sidechain
  tools 5; a depth-`d` agent shifts by `2*(d-1)`. The template then
  recurses `message.children`.
- **Sub-agent transcripts** — spliced by `_relocate_subagent_blocks`
  (~2153) right after the spawning tool_result; they become that
  tool_result's `.children`. Every agent card has
  `meta.is_sidechain = True`, `meta.session_id = "{trunk}#agent-{agentId}"`,
  `meta.agent_id = agentId` (membership), and
  `TemplateMessage.agent_depth >= 1` (also emitted as CSS
  `agent-depth-{d}`, `agent-ring-{1..5}`, `agent-deep`). The spawn
  reference is `meta.spawned_agent_id` on the spawning tool_result entry.
  `_cleanup_sidechain_duplicates` (~3579) drops the duplicate prompt/last
  answer; `spawns_collapsed_transcript` marks an emptied nested spawn.
- **Spawn cards** — `ToolUseMessage` whose input is `TaskInput`
  (`models.py` ~1410: `description`, `subagent_type`,
  `run_in_background`, `team_name`, `name`); paired result
  `ToolResultMessage` with `output: TaskOutput` (`metadata:
  AgentResultMetadata` → `agent_id`, `total_tokens`, `tool_uses`,
  `duration_ms`; `async_final_answer`). `display_model` carries the
  sub-agent's model on the spawn card (`_surface_agent_models` ~5092).
- **Async results** — `TaskNotificationMessage` (`models.py` ~999:
  `task_id` == agent id, `usage: TaskNotificationUsage`,
  `result_is_duplicate`, `spawning_task_message_index`), linked by
  `_link_async_notifications` (~3341). Card classes
  `user task-notification`; at level 3 under the preceding assistant.
- **Forks** — branch pseudo-sessions: `SessionHeaderMessage` with
  `is_branch=True` (`TemplateMessage.is_branch_header`, `branch_depth`,
  `content.parent_message_index` = fork-point index,
  `content.attachment_uuid`), built in `_build_branch_header` (~4395);
  branch messages have `render_session_id = "{sid}@{uuid12}"`. The fork
  point message carries `junction_forward_links`
  `[(branch_sid, branch_header_index, preview)]` and
  `fork_point_preview`; `fork_only` slots render just the fork-point box.
  Branch headers are children of the **session header** (level 0.5), so
  in the DOM the trunk turns come first, then each branch header with
  its own turns.
- **Teammates** — spawns with `TaskInput.team_name`/`name`; their
  threads are ordinary agent blocks (above). `TeammateMessage` cards
  (`user teammate`), `SendMessage` tool cards
  (`html/teammate_formatter.py`); per-session colours via `--cc-*` vars
  (`teammate_styles.css`) set inline as `style="--cc-color: var(--cc-…)"`.
- **Workflows** — `_splice_workflow_runs` grafts phase/agent cards
  (`tool_use workflow_phase|workflow_agent`) under the Workflow
  tool_use. Not lanes in this feature.

### 2.6 What is already on message DOM nodes

From `transcript.html` `render_message` (137–278):

- wrapper `div.message-node` → card `div.message.<classes>` (+
  `pair_first|pair_middle|pair_last` when paired) → sibling
  `div.children` (holds child `.message-node`s and the `.fork-point` box
  when the node is a junction).
- card `id='msg-d-N'` (positional, unique per page);
  `data-uuid` (stable transcript uuid, **not** unique per card);
  session headers carry `data-session-id`.
- timestamp: `.header .timestamp[data-timestamp=<ISO>]`, optional
  `data-duration` on pair-last; `timezone_converter.js` rewrites the text
  but keeps the attribute.
- `.token-usage` text, `.debug-info`, `.content(.markdown)`, `.fold-bar`
  with `.fold-bar-section[data-action=fold-one|fold-all][data-target=d-N]`.
- branch headers: `session-header branch-header`, inline
  `margin-left: {branch_depth*2}em`; fork boxes `.fork-point` with
  `a.fork-point-branch[href=#msg-d-N]`.

### 2.7 Existing runtime JS the theme must coexist with

All inside `transcript.html` unless noted.

- **Fold state machine** — delegated click on `.fold-bar-section`
  (~925); `applyFoldState` (~985), `setInitialFoldState` (~1024),
  `syncFoldBar` registered on rehydrate (~1073–1099),
  `revealMessage` → `window.claudeLogRevealMessage` (~1107–1127),
  hash / `?uuid=` deep links (~1129–1182). **Not exported**: the minimal
  JS needs `window.claudeLogApplyFoldState = applyFoldState` (add the
  export in P4; it is in a closure today).
- **`<details>` toggles** — `toggleAllDetails` over
  `details.collapsible-details, details.collapsible-code,
  details.tool-param-collapsible` (~507–544); params-table expand logic
  and a capturing `toggle` listener (~546–659).
- **Filter** — `applyFilter` (~739) toggles `filtered-hidden` on
  `.message:not(.session-header)`; sidechain cards need both the
  `sidechain` toggle and their own type; memory is independent (#192).
  Counts by class queries (~686–877). URL `?filter=`.
- **Timeline** — `components/timeline.html`: `buildTimelineData` walks
  `.message:not(.session-header)` and derives the group from classes
  (memory > sidechain > system-* > slash-command > … > first type
  class); listens to filter toggles (~571–613) for group visibility and
  to search for per-item `timeline-filtered-hidden`; click scrolls with
  `messageEl.offsetTop` (~284–310) — **breaks under
  `display: contents` ancestors** (offsetParent changes); switch to
  `getBoundingClientRect().top + scrollY` (P1, harmless for default).
- **Search** — `components/search.html`: `search-hidden` /
  `search-match` / `search-context` classes, ancestor walk via
  `parentElement` + `.children` (~234–261), opens `<details>` to reveal
  matches, calls `claudeLogRevealMessage`.
- **Live update** — `components/live_update.js`: `serve` only (http/s);
  HEAD poll; patches changed cards in place (`liveCard.replaceWith`),
  else replaces `#transcript` wholesale; calls
  `window.claudeLogRehydrate(el)` on new/replaced nodes. Stable keys from
  `data-uuid` + ordinal, `data-session-id`, then `id`.
- **Rehydrate hooks today**: timezone, fold-bar resync, timeline
  rebuild (`scheduleRebuild`), filter/search re-application.

---

## 3. Target design (implementation-level)

### 3.1 Files

```
claude_code_log/html/templates/components/minimal/
  tokens.css          light + dark tokens, color-scheme, font stacks
  layout.css          page, stage, row grid, gutter, rail (CSS part), turns,
                      toolbar, phone layout, no-JS nested rendering
  components.css      overrides for every default component in minimal
                      (tool params, todo, diffs, ask-user-question, bash,
                      teammates, workflow, fork points, session nav, page
                      nav, filter toolbar, search, timeline, collapsibles)
  pygments_dark.css   GENERATED github-dark tokens under dark selectors
  dag.css             dag-on grid, lane tint, branch controls, columns,
                      strips, SVG rail styling
  theme_init.js       <head> inline: apply stored data-theme (tiny)
  minimal.js          toolbar wiring, theme toggle, fold depth,
                      collapse labels / "− less"
  minimal_dag.js      DAG model + layout + rail + branch controls
scripts/generate_minimal_pygments_css.py
```

Include order (minimal only): after every default stylesheet,
`tokens.css`, `layout.css`, `components.css`, `pygments_dark.css`,
`dag.css`. Every minimal selector is prefixed with `.theme-minimal`
(class on `<body>`; set in the minimal template branch) so it wins over
default rules by specificity without `!important` (use `!important`
only to beat vis-timeline's own `!important` and inline styles).

Layering over the default CSS (rather than replacing it) is deliberate:
formatter output relies on hundreds of default component rules (params
tables, todo, diff, ansi…). The cost is overriding legacy layout hacks
(e.g. `.collapsible-details { margin-top: -2em }`,
`.tool_result .collapsible-code { margin-top: -2.5em }`, body gradient,
`.floating-btn` positioning, branch-header inline `margin-left`). P3a/P3b
list them.

### 3.2 Template changes (minimal branch only)

`transcript.html` receives `theme` (`"default"|"minimal"`); define
`{% set minimal = theme == 'minimal' %}` at the top **glued to an
existing line**. Minimal-only additions:

- `<html … data-theme-name='minimal'>`; `theme_init.js` inline in head.
- extra `<style>` content (the five sheets).
- `<body class='theme-minimal'>`.
- a header block: title + session meta line (mockup `.top`/`.smeta`) and
  the toolbar `<nav class='mn-toolbar'>` containing the moved buttons
  (same ids) — in the minimal branch the floating buttons are rendered
  inside the toolbar instead of at the bottom.
- a stage: `<div class='mn-stage'><div id='dag-rail' aria-hidden='true'></div><div id="transcript">…</div></div>`
  (`#transcript` id and contents unchanged).
- card data attributes (§ 3.3).
- `minimal.js` + `minimal_dag.js` included inside the existing
  `DOMContentLoaded` block **after** the fold machinery (so
  `claudeLogApplyFoldState` exists), or as separate scripts that wait for
  it.

### 3.3 Server-side lane annotation (P5)

A new format-neutral module **`claude_code_log/lanes.py`** (so Markdown/
JSON could use it later — "backportable") with:

```python
@dataclass
class LaneInfo:
    lane_id: str            # "agent-<agentId>" | "branch-<branch sid>"
    kind: str               # "agent" | "async-agent" | "fork" | "teammate"
    name: str               # Task description / teammate name / branch preview
    parent_lane: str        # "main" or the enclosing lane id (nesting)
    spawn_index: int | None # message_index of the spawn row
    merge_index: int | None # message_index of the merge row (None for forks)
    steps: int              # rendered cards in the lane, pair_last excluded
    total_tokens: int | None# AgentResultMetadata / TaskNotificationUsage
    duration_ms: int | None # metadata, else last-first timestamp in the lane
    first_ts: str | None
    last_ts: str | None

def annotate_lanes(roots: list[TemplateMessage]) -> dict[str, LaneInfo]:
    """Set TemplateMessage.lane_id on every node and return lane metadata."""
```

Rules: a node's lane is `agent-<meta.agent_id>` when `meta.is_sidechain`
and `meta.session_id` contains `#agent-`; `branch-<render_session_id>`
when its nearest branch header ancestor is a branch (minus the
"main-continuation" branch, § 1.6.4 decision); otherwise `main`.
Teammate lanes get `kind="teammate"`. Add `lane_id: str = "main"` to
`TemplateMessage.__init__` (renderer.py ~238) — a render-time field, not
part of the fragment-store key (it is emitted by the template, not by
formatters). Call `annotate_lanes` from `HtmlRenderer._generate_inner`
**only when `self.theme == "minimal"`** (keeps default cost and bytes
unchanged).

Attributes (minimal only):

| Where | Attribute | Value |
|---|---|---|
| every `.message` card | `data-lane` | lane id or `main` |
| spawn card (agent tool_use / fork-point owner) | `data-spawns` | space-separated lane ids it opens |
| merge card | `data-merges` | space-separated lane ids it closes |
| lane head (agent: the spawn tool_use card; fork: the branch header card) | `data-lane-id`, `data-lane-kind`, `data-lane-name`, `data-lane-parent`, `data-lane-stats` (`"6 steps · 48.4k tokens · 2m 13s"`, preformatted server-side), `data-lane-ts` (`first_ts last_ts`) | from `LaneInfo` |
| teammate spawn card | `data-teammate-link` | `d-N` of the thread's first card |

Card timestamps are read from the existing
`.header .timestamp[data-timestamp]`; cards without one (session/branch
headers, fork boxes) inherit the previous card's time in their lane.

### 3.4 Client: `minimal.js`

- **Theme toggle**: reads/writes `claude-code-log:theme`, sets
  `document.documentElement.dataset.theme`, updates `aria-pressed`.
- **Collapse**: on load and on rehydrate, for each `<details>` of the
  three collapsible classes inside the scope: compute `+N lines` (from
  `.line-count` or the body's `\n` count), set `data-more`; long open
  blocks get a trailing `− less` button (delegated click on `document`,
  registered once).
- **Fold depth**: `applyDepth('prompts'|'steps'|'all')` → uses
  `window.claudeLogApplyFoldState` per § 1.5; persisted in
  `localStorage` `claude-code-log:fold-depth`; manual fold-bar clicks
  clear the segmented "on" state.
- **Gutter time**: render a short local `HH:MM:SS` into a
  `.mn-time` span from `data-timestamp` (the full localised string from
  `timezone_converter.js` stays as the `title`). Rehydrate-aware.

### 3.5 Client: `minimal_dag.js` (P6–P7)

Build a model from the DOM, compute a layout, apply it:

1. **Model** — walk `#transcript .message` in DOM order (skip
   `filtered-hidden`, `search-hidden` and cards inside a `.children` with
   inline `display:none`): `{el, lane, ts, spawns[], merges[]}`; lanes
   from `[data-lane-id]` heads: `{id, kind, name, parent, stats, from:
   spawn el, to: merge el}`; group lanes into branch groups (§ 1.6.3).
2. **Visible set** — main cards always; a lane's cards iff its mode is
   `interleaved` or `column` **and** its parent lane is visible (a nested
   lane inside a folded lane is hidden regardless of its own mode).
3. **Order** — k-way merge of each visible lane's DOM-ordered sequence
   by `ts` (stable: ties keep DOM order, main first), so each lane's
   internal order is untouched and pairs stay adjacent within a lane.
4. **Rows** — port `DagRail.dc.html` `renderVals()` "Pack rows" verbatim:
   `key = column lane or 'main'`; `r = max(prev, nextFree[key])`; a lane's
   first row `> rowOf[spawn]`; a merge row `> lastRowOf[lane]`; `prev = r`.
   In non-column modes this yields one card per row.
5. **Rail slots** — port the "Rail" part: slot 0 = main; railed lanes
   (folded + interleaved, not columned) get slots by greedy interval
   colouring over `[spawnRow, endRow]` (end = merge row, else last row
   of the lane when interleaved, else the spawn row → stub), reusing the
   lowest free slot (the mockup hard-codes `slot`; generalise). Per row
   and slot produce the same states as the mockup (`v`, `vt`, `vb`,
   `dash`, `cd`, `cd stub`, `cu`, `h`, `r`, `dot k-*`), then draw them
   as SVG paths at measured y positions.
6. **Apply** — set `--rail-w` (18px × slots in use) on the stage, grid
   template `var(--gut) var(--rail-w) minmax(360px,1fr)` + one
   `minmax(300px,1fr)` (or `34px` when collapsed to a strip) per column
   (mockup `gtc`); write `grid-row`/`grid-column` per visible card
   (column cards span their column only and use the compact column row
   grid `46px minmax(0,1fr)`), `dag-hidden` on hidden cards, column
   headers/strips as elements in `#dag-rail`'s sibling header layer
   (outside `#transcript`), then draw the SVG.
7. **Controls** — branch control markup is injected **outside the
   swap-sensitive card markup**: render it into a per-spawn-card
   container that the engine owns (`.mn-bctl`, appended to the spawn
   card's `.content`; re-created after every rehydrate because a patched
   card loses it). Delegated click handlers on `document`.
8. **Selection** — per group LRU list of interleaved lanes; cap 3.

Performance guard: one batched read pass (`getBoundingClientRect`) then
one write pass; skip relayout when the page is hidden; measured budget
< 50ms for 2,000 visible cards on a mid laptop (P6 adds a crude timing
log under `?debug-dag`).

### 3.6 Testing strategy

- **Unit** (`just test`): option plumbing, suffixes, staleness,
  `lanes.py` on fixtures, template byte-identity of the default.
- **Snapshot**: new tests in `test/test_snapshot_html.py`
  (`TestMinimalThemeHTMLSnapshots`) for `representative_messages.jsonl`
  and the `async_agents` + `nested_agents` + a fork fixture
  (`dag_fork.jsonl` / `dag_within_fork.jsonl`) rendered with
  `--theme minimal`. Existing snapshots must not change. Regenerate only
  with `just update-snapshot`; expect `+N/-0`.
- **Browser** (`@pytest.mark.browser`, Playwright, see
  `test/test_nested_agents_browser.py` for the pattern and
  `test/conftest.py` for `page`/`context` fixtures): theme toggle +
  persistence, collapse, fold depth, three branch modes, overflow,
  teammate anchors, filter/timeline parity, live-update relayout,
  no horizontal scroll at 375px in main-only mode.

---

## 4. Constraints every phase must honour

- Read `CLAUDE.md` and `CONTRIBUTING.md`. Run **`just ci`** before each
  commit (format, lint, ty, pyright, unit + TUI + browser tests). If
  Chromium is missing: `uv run playwright install chromium`.
- Snapshots: never a bare parallel `--snapshot-update`; use
  `just update-snapshot`. After it, inspect at **block level** (snapshot
  names added/removed, per-block diffs), not the raw `-N`.
- Phases after P1: existing `.ambr` blocks byte-identical.
- `dev-docs/` is as-built: update the relevant page in the same commit
  as the behaviour change (named per phase below). Don't edit
  `CHANGELOG.md`. British English in prose.
- Keep timeline + filter parity (CLAUDE.md "Timeline Component").
- One phase = one (or a few) commits on the working branch; commit
  messages end with the attribution lines your session's system
  reminder specifies. Don't push unless asked.
- Update § 6 (progress) of this file at the end of your phase.

---

## 5. Phases

Sizes: S ≈ <300 changed lines, M ≈ 300–900, L ≈ 900–1,800. Every phase
is independently committable with `just ci` green; later phases only
depend on earlier ones having landed.

### P1 — Groundwork fixes (default output changes, intentionally) — S

**Goal:** fix the three defects in § 2.1 in the default theme so the
minimal theme can build on variables, plus one latent timeline bug.

**Files:** `claude_code_log/html/templates/components/global_styles.css`,
`message_styles.css`, `pygments_styles.css`, `session_nav_styles.css`,
`timeline_styles.css`, `components/timeline.html`,
`test/__snapshots__/test_snapshot_html.ambr` (regenerated),
`dev-docs/css-classes.md`.

**Steps:**
1. Re-derive undefined vars:
   ```bash
   cd claude_code_log/html/templates
   grep -ohE 'var\(--[a-zA-Z0-9-]+' components/*.css components/*.html *.html | sed 's/var(//' | sort -u > /tmp/used
   grep -ohE '^\s*--[a-zA-Z0-9-]+\s*:' components/*.css components/*.html *.html | tr -d ' :' | sort -u > /tmp/def
   comm -23 /tmp/used /tmp/def
   ```
   Define the genuinely intended ones in `global_styles.css :root`
   (`--color-bg-secondary`, `--color-bg-tertiary`, `--color-blue`,
   `--color-green`, `--color-purple`, `--color-border-dim`,
   `--color-text-dim`, `--color-text-secondary`) with restrained values
   that fit the current palette (e.g. `#f6f8fa`, `#eef1f4`, `#1e6fd9`,
   `#2e7d32`, `#7b1fa2`, `#d0d7de`, `#8c959f`, `#57606a`); rename the
   misspelt uses (`--code-bg` → `--code-bg-color`, `--secondary-text` →
   `--text-secondary`, `--font-mono` → `--font-monospace`); give
   `--accent-color` a definition equal to its fallback.
2. Fix `.tool-result .line-count` → `.tool_result .line-count` in
   `pygments_styles.css`.
3. Timeline: replace each group's `style: 'background-color: …'` in
   `messageTypeGroups` with `className: 'timeline-group-<id>'` and move
   the colours to `timeline_styles.css` as
   `.vis-label.timeline-group-<id> { background-color: var(--timeline-<id>-bg, <same hex>); }`
   (scope to `.vis-label` — vis applies a group `className` to the label
   *and* the row, the old inline `style` only hit the label). Move the
   container/resize-handle inline colours to CSS
   (`#timeline-container { background: var(--timeline-bg, white); border-bottom: 1px solid var(--timeline-border, #ddd) }`
   etc.; keep layout inline styles if you prefer, only colours must
   move). Replace the `#fff3cd` select flash with a
   `timeline-flash` class defined in `timeline_styles.css`. Re-point the
   item rules `.vis-item.timeline-item-*` to
   `var(--timeline-item-<type>-bg, <hex>)` / `-border`.
4. Timeline scroll: in `onTimelineSelect` replace
   `messageEl.offsetTop` with
   `messageEl.getBoundingClientRect().top + window.scrollY`
   (required by `display: contents` later; identical result today
   because cards have no positioned ancestor — verify).
5. `just update-snapshot`; inspect: only CSS/JS text inside each block
   should differ, no message markup.

**Acceptance:** `just ci` green; snapshot diff limited to style/script
text; a quick manual or Playwright check that the timeline still
colours groups (extend `test/test_timeline_browser.py` with an
assertion that a group label has the expected computed background). Visual delta of the default theme
limited to the previously-broken rules now applying (Pygments block
background, line-count badge, read-tool colours) — describe it in the
commit message.

**Tests:** in `test/test_timeline_browser.py`: open a rendered page, toggle
the timeline, assert `getComputedStyle(label).backgroundColor` for the
user group equals `rgb(227, 242, 253)`; set
`--timeline-user-bg: rgb(1, 2, 3)` on `:root`, assert it follows.

**Dev-docs:** `css-classes.md` — note the timeline group classes and
variables.

**As built (P1 landed):**
- **The step-1 one-liner misses underscores.** `[a-zA-Z0-9-]` truncates
  `--timeline-tool_use-bg` to `--timeline-tool`; use `[a-zA-Z0-9_-]`.
  After P1 the only used-but-undefined properties are the 42
  `--timeline-*` ones, all deliberately undefined in the default theme
  and all read with a fallback (`var(--timeline-…, <hex>)`). A later
  theme defines them; nothing else needs defining.
- **`.tool-result .preview-text` fixed too.** The `.line-count` rule in
  `pygments_styles.css` was a two-selector list; its sibling
  `.tool-result .preview-text` was dead for the same reason (the card
  class is `tool_result`). Both now use `.tool_result`.
- **Group label rules are `.vis-labelset .vis-label.timeline-group-<id>`**
  (one more class than planned) so they outrank vis-timeline's own
  `.vis-labelset .vis-label` rules as reliably as the inline style did.
  The group row (`.vis-group.timeline-group-<id>`) stays uncoloured
  (asserted in the test).
- **More timeline variables than listed:** resize handle
  `--timeline-handle-bg`/`-hover-bg`/`-active-bg`, grip
  `--timeline-grip`/`-grip-hover`/`-grip-active`, flash
  `--timeline-flash-bg`, container `--timeline-bg`/`--timeline-border`.
  The handle's `:hover`/`:active` rules dropped their `!important` (they
  only needed it to beat the inline style that is now gone).
  `.message.timeline-flash` keeps `!important` instead, standing in for
  the inline style it replaces. Item rules exist only for the eight types
  that had them; the other nine group types still fall back to vis's
  item colours, as before. Full table: `dev-docs/css-classes.md`
  § "Timeline Classes and Colour Variables".
- **`offsetTop` vs bounding rect verified identical** for all 86 cards
  (everything unfolded, scrolled) across the async-agents, nested-agents,
  teammates, workflow and sidechain renders.
- **Snapshot diff (block level):** 10 blocks, none added or removed;
  the nine transcript blocks carry an identical +158/−53 diff (CSS/JS
  text only), and the index block +14/−1 (`global_styles.css` `:root` and
  `session_nav_styles.css`, which `index.html` also inlines). No message
  markup changed.
- **Tests added:** `test_timeline_group_label_colours_come_from_css`
  (uses `representative_messages.jsonl` — `sidechain.jsonl` has no user
  group) and `test_timeline_select_flashes_message_via_class`.
- **Browser tests in a CCR container:** Chromium's NSS store did not
  trust the agent-proxy CA. `ignore_https_errors` got pages loaded, but
  Chromium does not cache responses with certificate errors, so every
  test re-fetched vis-timeline from unpkg and the fetch failed
  intermittently (`ERR_TOO_MANY_RETRIES`, 30 s timeouts). Fix (outside
  the repo):
  `apt-get install -y libnss3-tools && certutil -d sql:$HOME/.pki/nssdb -A -n ccr-agent-proxy -t "C,," -i /root/.ccr/agent-proxy-ca.crt`.
  `just` itself: `uv tool install rust-just`.

### P2 — Theme plumbing: `--theme {classic,minimal,default}` end to end — M

**Goal:** the option exists everywhere HTML is produced and takes part in
every staleness decision; the minimal output is, for now, the classic
page plus a `theme-minimal` body class, a `theme=minimal` generator stamp
and an empty minimal stylesheet slot. Classic output byte-identical.

*Revised after the § 7 decisions* (originally the theme was to be a
filename variant): themes overwrite the same files (decision 2), the
original look is named `classic` and `default` resolves through
`DEFAULT_THEME` (decision 7), and `CLAUDE_CODE_LOG_THEME` sets the theme
when `--theme` isn't passed (decision 8).

**Files:** `utils.py` (`THEMES`, `THEME_CHOICES`, `DEFAULT_THEME`,
`CLASSIC_THEME`, `THEME_ENV_VAR`, `normalize_theme`, `output_theme`),
`renderer.py` (`Renderer.theme`, `get_renderer(theme=)`,
`is_html_outdated(path, theme)`), `html/renderer.py` (stamp helpers,
`is_outdated`, `_generate_inner` passes `theme`, convenience functions),
`cache.py` (`theme=` on the three staleness checks), `converter.py`
(every function in § 2.3), `render_pool.py`, `render_dispatch.py`,
`cli.py` (`convert`, provider helpers, `serve`, `watch`, TUI launch),
`tui.py`, `html/templates/transcript.html`, new
`html/templates/components/minimal/tokens.css`, tests, docs.

**Steps:** thread `theme` beside `no_recaps` through § 2.3 (keyword,
default `DEFAULT_THEME`); stamp the theme into the generator comment and
compare it in every staleness check (§ 2.4); CLI option + env var with
precedence flag > env > built-in default; template branch (below).

**Acceptance:** `just ci` green with **no** `.ambr` change;
`claude-code-log <dir> --theme minimal` rewrites the same filenames with
the minimal stamp (pages with `--page-size` too), a second run reports
everything current, switching back regenerates everything again.

**As built (P2 landed):**
- **Names.** `utils.THEMES = ("classic", "minimal")`,
  `THEME_CHOICES` adds `"default"`; `normalize_theme` is
  case-insensitive and maps `default` → `DEFAULT_THEME` (one constant;
  `CLASSIC_THEME` separately names the stamp-less theme so moving the
  default never changes what a classic page looks like on disk).
  Everything downstream of the CLI/`get_renderer` holds a *resolved*
  name; `output_theme(format, theme)` forces `classic` for Markdown/JSON.
- **Marker.** § 2.4: the generator comment carries ` theme=<name>` for
  non-classic themes; `html_generator_stamp` / `check_html_theme` in
  `html/renderer.py`. Cache rows are shared between themes, no migration.
- **No mixed archives under `--combined no`.**
  `converter.combined_theme_mismatch(dir, suffix, theme)`: when the
  combined output (page 1 — same name paginated or not) carries another
  theme, `convert_jsonl_to` promotes the run to `write_combined=True`
  once, and `_plan_project` counts it as work. Only for HTML runs that
  write session pages (a `--combined no` run's pages link back to it).
  Single-file exports (`--session-id`, TUI) write just their file; the
  next archive run in the other theme rewrites it (stamp mismatch).
- **Index / search page** are written on every run anyway, so they need
  no stamp; neither is themed yet (§ 7 decision 6). `get_renderer(...)`
  for the index is called without a theme.
- **CLI.** `--theme` (Click `Choice(THEME_CHOICES)`, no Click default
  and no Click `envvar`) on `convert`, `serve`, `watch`;
  `cli._resolve_theme` applies flag > `CLAUDE_CODE_LOG_THEME` > default
  and raises a `UsageError` naming the variable and the valid choices for
  an unknown env value (an empty variable counts as unset). An explicit
  `--theme` with `--format md|json` warns; the env var alone doesn't.
  `--tui` passes the theme to `run_session_browser(theme=)`.
- **TUI.** `SessionBrowser(html_theme=)` — *not* `theme`, which is
  Textual's UI theme (`"gruvbox"`). `_ensure_session_file` keeps the
  `session-{id}.{ext}` name and renders via
  `get_renderer(format, theme=self.html_theme)`.
- **Render pool.** `_WorkerSetup.theme` (last field, defaulted), passed
  by `build_render_pool(theme=)` → `make_render_pool(theme=)`.
- **Template.** Line 1: `<!DOCTYPE html>{% set minimal = theme ==
  'minimal' %}`; line 2's stamp:
  `{% if theme and theme != 'classic' %} theme={{ theme }}{% endif %}`;
  `{% include 'components/teammate_styles.css' %}{% if minimal %}` +
  newline + `{% include 'components/minimal/tokens.css' %}{% endif %}`;
  `<body{% if minimal %} class='theme-minimal'{% endif %}>`. The template
  receives the resolved name as `theme`. **Later CSS keys on
  `body.theme-minimal`** (P3a adds the `:root` token blocks; § 3.2's
  `data-theme-name` on `<html>` is not emitted yet — add it in P3a if
  needed, glued to the existing `<html lang='en'>` line).
- **Fragment store / memo caches** are untouched: formatter output is
  theme-independent (§ 1.4 "no formatter changes"), and a store lives
  inside one conversion, which has one theme. If a later phase ever makes
  a formatter theme-dependent, the theme must join the store key.
- **Tests:** `test/test_theme_option.py` (36): names and the `default`
  constant indirection, classic byte-identity vs unthemed and
  `default`, minimal = classic + exactly three markers, stamp
  round-trip, changed-default staleness, theme round trips (single file,
  paginated, streaming forced and spied, `--combined no` promotion,
  render pool with dispatch counted, all-projects incl. the watch
  shape), CLI precedence/env error/alias/warning, `serve`/`watch`
  plumbing, TUI export (`tui` marker). No `.ambr` change.
- **Docs:** README "Choosing a Theme", `docs/live-updates.md` tuning
  table, `dev-docs/application_model.md` § 2.1 / 2.2 / 2.15.

### P3a — Minimal look: tokens, layout, toolbar, light/dark toggle — L

**Goal:** the minimal page looks like `MinimalLook.dc.html` for the
common message types, light and dark, with the toolbar and theme toggle;
no DAG, no fold-depth control yet (toolbar slot reserved).

**Files:** `components/minimal/tokens.css`, `layout.css`,
`theme_init.js`, `minimal.js` (theme toggle + gutter time only),
`transcript.html` (minimal branches: head script, header/meta line,
toolbar with moved buttons, stage wrapper, `.mn-time` span in the
header), tests, `dev-docs/css-classes.md`.

**Steps:**
1. Tokens per § 1.2/§ 1.3 (three blocks). Font stacks. `body` explicit
   background; remove the gradient (`.theme-minimal` override).
2. Row layout: make `.theme-minimal .message` a grid
   `58px 18px minmax(0,1fr)`; `.header` → gutter column (role label from
   the existing title span, `.timestamp` → `.mn-time`, `.token-usage`
   muted); `.content` → column 3; `.fold-bar` → column 3 below content;
   dot via `.message::before` in column 2 coloured by role class
   (`user`→`--user`, `assistant`→`--asst`, `thinking`→hollow muted,
   `tool_use|tool_result`→`--tool`, `system*`→`--sys`,
   `task-notification`→`--note`, `error`→`--err`); continuous hairline
   via the stage's `::before` at the rail column. Pair cards
   (`pair_first`/`pair_last`): the last half drops gutter text and dot
   and tucks under the first.
3. Turn separation: a 1px `--rule2` rule above each main-lane user card
   except the first in a session; no card borders/backgrounds/shadows.
4. Neutralise legacy layout hacks under `.theme-minimal`: negative
   margins on `.collapsible-details` / `.tool_result .collapsible-code`,
   branch-header inline `margin-left` (`!important` needed — inline
   style), `.message` padding/border-radius/box-shadow, `.session-divider`.
5. Toolbar: `<nav class='mn-toolbar'>` sticky top, segmented controls
   (`.seg`), icon buttons (`.ibtn`), overflow group; the moved buttons
   keep ids; hide `.floating-btn` positioning. Follow button stays hidden
   until `live_update.js` enables it (it toggles a class — check the
   CSS hook it uses and mirror it).
6. Theme toggle (`minimal.js`), `theme_init.js` in `<head>`.
7. Phone layout (<640px) per mockup; verify no horizontal scroll.
8. Nested (no-JS) rendering of sidechains: `.children` containing
   `.message.sidechain` get `border-left: 2px solid var(--ring)` and a
   small left padding (mockup `.nest`).

**Acceptance:** `just ci` green, existing snapshots unchanged; new
snapshot `test_minimal_representative_html` added via
`just update-snapshot` (`+N/-0`); visual check of light and dark with
Playwright screenshots attached to the PR/commit description (not
committed).

**Tests** (browser, new `test/test_minimal_theme_browser.py`):
toggle Auto/Light/Dark sets `data-theme` and survives reload
(localStorage); in forced dark, `getComputedStyle(body).backgroundColor`
is `rgb(20, 22, 26)`; with `prefers-color-scheme: dark` emulation and
Auto, same; every moved button exists and its existing behaviour works
(click `#filterMessages` opens `.filter-toolbar`; `#toggleTimeline`
shows the timeline); 375px viewport → `scrollWidth <= clientWidth`.

**Dev-docs:** `css-classes.md` (minimal classes, `theme-minimal` scope,
token table); `rendering-architecture.md` (template theme branch).

**As built (P3a landed):**
- **Files.** `components/minimal/tokens.css`, `layout.css`,
  `theme_init.js`, `minimal.js` as planned, plus
  `components/minimal/header.html` (page header + sticky toolbar, included
  from the minimal branch to keep `transcript.html` readable) and
  `claude_code_log/html/minimal_theme.py` — template-only helpers
  registered as Jinja globals `mn_role_label`, `mn_is_generic_title`,
  `mn_compact_tokens`, `mn_gutter_time`, `mn_page_meta` in
  `html/utils.get_template_environment` (classic never calls them).
- **Gutter (deviation from step 2).** The title span can't be split by CSS
  (`📝 Edit <span class='tool-summary'>…`), so the role label is not taken
  from it: the minimal branch emits `<span class='mn-role'>` (one word
  from the card's CSS classes; a tool's `tool_name`), `<span
  class='mn-time'>` (server: UTC time of day; `minimal.js` localises it from
  the sibling `.timestamp[data-timestamp]`, full local stamp + duration as
  `title`) and `<span class='mn-tok'>` (`in · out`, in = input + cache
  creation + cache read, full string as `title`) inside `.header-info`.
  The title span gets `class='mn-title'` and stays the row's first line
  in column 3 (the mockup's `.call` line); role-only titles (`🤷 User`,
  `🤖 Assistant`, `💭 Thinking`, `🔗 Sub-assistant`, …) also get
  `mn-generic` and are hidden. The title span is still the header's first
  `span` (search reads `.header span`). Card classes, ids, uuids,
  timestamps and fold bars are byte-for-byte the classic ones
  (`test_theme_option.py::test_minimal_keeps_every_card_hook`).
- **Row grid.** `var(--gut) var(--rail) minmax(0,1fr)` × rows title /
  debug / content / fold bar / `1fr` slack; `.header` is
  `display: contents`, `.header-info` (the gutter) and the `::before` dot
  span all five rows, so a gutter taller than the content grows the slack
  row instead of pushing the content down (spanning items crossing a
  flexible track only grow that track). Consequence: a one-line answer
  with tokens is three gutter lines tall.
- **Specificity, two tiers** (matters for P4–P7): `display: grid` on cards
  is set through `.theme-minimal :where(#transcript .message:not(.session-header))`
  (one class), so `.message.filtered-hidden` / `.message.search-hidden`
  (`display: none`, two classes) still win; box properties use
  `.theme-minimal #transcript …` (id) to beat classic per-type margins
  without `!important`. **P6's `dag-hidden` and anything else that hides a
  card must be at least two classes**, and the stage grid there must
  account for each card already being a grid with its own column
  template (`--gut`/`--rail` are custom properties, so a column layout can
  shrink them per lane).
- **Rail.** The hairline is `.mn-stage::before` at
  `calc(var(--gut) + var(--rail) / 2)`; session headers and fork boxes get
  `background: var(--bg)` + `z-index: 1` to mask it. `#dag-rail` is not
  emitted yet (P6 adds it inside `.mn-stage`).
- **Classic variables mapped to tokens** on `body.theme-minimal`
  (`--text-primary`, `--code-bg-color`, `--user-color`, `--agent-ring-1`,
  `--message-padding: 0`, `--font-ui`/`--font-monospace`, …), which
  re-themes every classic rule that reads a variable in both schemes.
- **Toolbar.** Main row: `#filterMessages`, `#toggleTimeline`,
  `#followUpdates` (still hidden until `live_update.js` adds
  `.live-active`), `#resumeSession` (single-session pages), the theme
  `.mn-seg`, the overflow `<details class='mn-more'>` (`#toggleDetails`,
  `#toggleUserView`, `#toggleDebug`; closes on outside click / Escape) and
  the scroll-top anchor. Same elements, ids and classes as the classic
  stack (`floating-btn` kept so classic state hooks apply); the bottom
  stack is not rendered in this theme. Icons are CSS masks on `::before`
  (`--mn-icon`) and the button text is `font-size: 0`, because the page's
  scripts rewrite `textContent` (timeline 📆/🗓️, details 📦/🗃️, md/raw);
  menu items label themselves with `content: attr(title)`, which those
  scripts keep current. P4/P6 controls go before `.mn-sp` (a Jinja comment
  marks the slot). `data-theme-name` on `<html>` was not needed and is not
  emitted.
- **Sticky stacking.** `minimal.js` keeps `--mn-bar-h` and `--mn-filter-h`
  on `<html>` (ResizeObserver); the filter panel sticks at `--mn-bar-h`,
  the timeline at their sum (`!important` over its scripted inline `top`),
  the resume toast drops below the toolbar's current bottom
  (`--mn-toast-top`, set on click).
- **Pulled forward from P3b** (to keep the common types legible in dark):
  code/pre boxes, Edit diffs (add/del tokens), tool-error box, user tint,
  thinking/steering/system text, Markdown spacing, flattened
  `.navigation` / `.page-navigation` / `.session-link`, search input and
  active filter chips. **Still P3b:** Pygments dark (classic token colours
  are poor on `--code` dark — e.g. `.nf` blue), timeline colours (white
  container in dark), tool params tables, bash command box, todo,
  ask-user-question, teammate `--cc-*` tints (they follow the OS scheme,
  not `data-theme`), workflow groups (still classic indents), fork-point
  internals, the depth badge, gutter error pill.
- **Fold bar** is flattened to one muted mono line (both sections kept);
  the chevron summary and fold depth are P4.
- **Storage.** `theme_init.js` and `minimal.js` wrap every storage access
  in `try/catch`; Auto removes the key. Pre-existing, not fixed (it would
  change classic bytes): the classic `DOMContentLoaded` handler reads
  `claude-code-log:user-view` unguarded, so with storage blocked the rest
  of that handler (filter, folds) doesn't run — in both themes.
- **Head size.** The minimal head is ~30KB larger (representative page:
  `<h1>` at 146KB vs 116KB), well inside the converter's 512KB bounded
  read; `test_minimal_theme.py` asserts the pagination markers stay in it.
- **Tests.** `test/test_minimal_theme.py` (helpers, gutter markup,
  pagination markers), `test/test_minimal_theme_browser.py` (19, marker
  `browser`: toggle + reload persistence, Auto under emulated
  `prefers-color-scheme`, explicit choice beats the system, choice applied
  before `<body>` exists, blocked storage, every control in the toolbar and
  working, sticky toolbar, filter/search still hide grid cards,
  timeline/filter parity, 375px without horizontal scroll). The P2 test
  `test_minimal_differs_only_by_theme_markers` was replaced by
  `test_minimal_keeps_every_card_hook`.
- **Snapshot.** `TestMinimalThemeHTMLSnapshots.test_minimal_representative_html`
  added with `just update-snapshot`: `+9404/-0`, block level one block
  added, none changed or removed.
- **Screenshots** are committed (on request, instead of only attached) in
  [`work/minimal-theme-dag-screenshots/p3a/`](minimal-theme-dag-screenshots/p3a/):
  representative transcript light/dark × desktop/phone, async-agents
  light/dark desktop with the sub-agent group unfolded.

### P3b — Minimal components, dark completeness, Pygments dark, timeline — L

**Goal:** every component looks right in minimal light and dark.

**Files:** `components/minimal/components.css`, `pygments_dark.css`
(generated), `scripts/generate_minimal_pygments_css.py`, tests,
dev-docs.

**Steps:**
1. Audit colour literals per default file (counts at the time of
   writing: message_styles 128, global_styles 91, pygments 67, search 36,
   teammate 34, timeline.html 24 (fewer after P1), project_card 24 (index
   only — skip), timeline_styles 23, session_nav 23, filter 14,
   edit_diff 8, page_nav 7, todo 6):
   `grep -noE '#[0-9a-fA-F]{3,8}\b|rgba?\(' <file>`. For each rule that
   is visible on a transcript page, add a `.theme-minimal` override
   mapped to tokens. Teammate colours: redefine `--cc-*` and `--cc-*-bg`
   for dark. ANSI output (`html/ansi_colors.py` inline styles) stays.
2. Tool rendering per § 1.2: call line, output box, error box + pill
   (the gutter pill can be CSS on `.tool_result.error .header::after`
   content `"error"`; an `exit N` value is only available in the Bash
   result text — keep it CSS-only unless trivially available), diffs
   (`edit_diff_styles.css` classes), hooks/system dim.
3. Pygments dark: the script runs
   `HtmlFormatter(style="github-dark").get_style_defs(".highlight")` and
   rewrites each selector twice — prefixed with
   `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .theme-minimal …}`
   and `:root[data-theme="dark"] .theme-minimal …` — writing
   `pygments_dark.css`. Test that the committed file equals the script's
   output (drift guard).
4. Timeline in minimal: redefine the P1 `--timeline-*` variables for
   light/dark; vis-timeline's own CSS (axis, labels, items) overridden
   for dark under `.theme-minimal`.
5. Filter toolbar, search bar, session ToC (`session_nav`), page nav,
   fork-point boxes, todo lists, ask-user-question, workflow cards:
   flat, hairline-bordered, token colours.

**Acceptance:** `just ci`; minimal snapshot updated (expected, only the
minimal block changes — confirm block-level); a browser test sampling
computed colours in dark for: code block background, a Pygments keyword
(`.highlight .k`), diff add line, timeline group label, filter toggle.
Run `uv run python scripts/generate_style_guide.py` with the minimal
theme (add a `--theme` flag to it if it lacks one) and eyeball both
schemes.

**Dev-docs:** `css-classes.md` (component overrides, Pygments dark);
`CONTRIBUTING.md` one line on regenerating `pygments_dark.css`.

**As built (P3b landed):**
- **Files.** `components/minimal/components.css` (every remaining
  component), `components/minimal/pygments_dark.css` (generated) and
  `scripts/generate_minimal_pygments_css.py` as planned; small additions to
  `layout.css` (call line, compact session header, turn rule), `tokens.css`
  (dark `--cc-*`), `minimal.js` (session-summary more/less) and
  `minimal_theme.py` (`call_title`, `session_header`, registered as
  `mn_call_title` / `mn_session_header`). `mn_is_generic_title` is no longer
  called by the template (the function stays; `call_title` uses it).
- **Variables first, literals second.** `components.css` opens by mapping
  the remaining classic custom properties to tokens (todo, question/answer,
  workflow, depth rings 2–5 → `--lB`/`--asst`/`--user`/`--lF`, translucent
  tints → transparent, every `--timeline-*`), then overrides literal rules
  by component. ANSI colours stay as planned.
- **Pygments dark.** The script emits github-dark's `.highlight .<tok>`
  rules under both dark selectors. Deviation from step 3: each rule also
  resets what the classic light sheet sets on that token and github-dark
  doesn't (`font-weight: bold` on keywords, `.err`'s red border), and
  tokens only the classic sheet styles get the reset alone — otherwise light
  styling leaked into dark. github-dark's background and the unprefixed
  line-number rules are dropped (the box keeps `--code`). `--check` mode
  exists; the drift test is `test_minimal_theme.py::TestPygmentsDark`.
- **Teammate tints** follow the in-page toggle: `--cc-*-bg` are
  `color-mix(…13%, var(--bg))` on `body.theme-minimal`, dark `--cc-*` live in
  the token blocks; badges use `--bg` text.
- **Timeline** follows the scheme: `--timeline-*` as role tints of `--bg`;
  vis-timeline's own sheet (loaded from unpkg after ours) overridden for
  borders, grid, axis text, generic items and the tooltip.
- **Error pill** (step 2): CSS-only on the gutter's role label (`error`,
  lowercase), shown for a paired failed result too; no `exit N` (only in
  the Bash result text).
- **Call line (review feedback 1).** The title span is a full
  `{% if minimal %}…{% else %}classic{% endif %}`: `call_title` splits the
  leading pictograph and a leading repeat of the gutter label (tool name,
  role label, or an alias: `Todo List`, `Async result`, `Slash Command`,
  `Task`, `Error`, `Teammate`, `Sub-assistant`) into a hidden `.mn-tn` span;
  the full title is the span's `title` (plus the classic `title_hint`).
  `.mn-tn` stays in the DOM, so search/timeline text is unchanged. Titles
  with nothing left (`🛠️ TodoWrite`, `🚨 Error`, `🤷 Slash Command`) become
  `mn-generic` (hidden). Only ASCII-free pictographs count as decoration,
  so `/cmd` or `$ x` titles keep their first character.
- **Session header (review feedback 2).** Non-branch headers render
  `.mn-sh-sum` (summary, clamped to two lines, full text as `title`,
  `+ more`/`− less` from `minimal.js` when clamped — on load, rehydrate,
  resize) then `.mn-sh-id` and the model as dim mono metadata; the
  `Session:` prefix and `• id` suffix are gone. Branch headers unchanged.
  The formatter output is reused (the escaped title is replaced in it), so
  the team badge and `continues from` back-link stay.
- **Classic fix (review feedback 3).** The classic `DOMContentLoaded`
  handler's `claude-code-log:user-view` read *and* write are wrapped in
  `try/catch`. Classic bytes changed by exactly that: snapshot update
  `+1564/-97`; block level — no block added or removed, all nine classic
  HTML blocks identical apart from the two try/catch hunks (checked by
  substituting the old text back), the minimal block changed as intended.
  `search.html`'s `restoreSearchState` still reads storage unguarded, so
  with storage blocked the in-page search doesn't initialise (both themes;
  filters, folds and the md/raw toggle now do). Left for a follow-up: it
  changes classic and index output.
- **Turn rule** no longer drawn above a slash command's output
  (`.command-output`, `pair_middle`/`pair_last` excluded).
- **Style guide.** `scripts/generate_style_guide.py --theme minimal
  [--output-dir DIR]` renders only the transcript guide (default output: a
  scratch dir, so the committed classic guide is untouched).
- **Collapsibles** (`details.collapsible-*` previews, the `+N lines`
  summaries) are legible in both schemes but otherwise classic — P4.
- **Head size.** The minimal head grew by ~50KB (components ≈ 31KB,
  Pygments dark ≈ 15KB): representative page `<h1>` at 196KB vs classic
  116KB, still well inside the 512KB bounded read.
- **Tests.** `test_minimal_theme.py`: `TestCallTitle`, `TestSessionHeader`,
  `TestPygmentsDark` (drift + selector shape), page markup. New
  `test_minimal_components_browser.py` (13, marker `browser`): Pygments
  tokens differ from light and reach 4.5:1 on the dark code box (toggle and
  system), light choice beats a dark system; the acceptance sample (code
  box, diff add line, filter chip and dot); timeline container/labels/axis/
  items dark; teammate tints follow `data-theme`; call line + search still
  matching the hidden name; error pill; session summary clamp/expand;
  blocked storage for classic and minimal.
- **Screenshots** in
  [`work/minimal-theme-dag-screenshots/p3b/`](minimal-theme-dag-screenshots/p3b/):
  representative light/dark, edge cases (error pill, params, Pygments),
  teammates, workflow, todo, fork, timeline — desktop, dark unless named.

### P4 — Collapse previews and fold-depth control — M

**Goal:** § 1.4 and § 1.5 fully working.

**Files:** `transcript.html` (export
`window.claudeLogApplyFoldState = applyFoldState;` — a default-template
change of one line: **this alters default bytes**; to keep byte-identity
put the export inside a `{% if minimal %}…{% endif %}` glued inline, or
better expose it from the minimal script by having the template's fold
block assign it only when `document.body.classList.contains('theme-minimal')`
— prefer the Jinja gate), `components/minimal/layout.css` /
`components.css` (collapse styling, fold bar), `minimal.js` (collapse
labels, `− less`, fold depth), tests, `dev-docs/message-hierarchy.md`.

**Steps:**
1. CSS: closed `<details>` show the summary preview clipped to `--pv`
   with the mask; `.line-count` restyled as the `+N lines` button text
   (`.more`); `details[data-more]` without a `.line-count` gets
   `summary::after { content: attr(data-more) }`; open → summary shows
   `− less`; body full.
2. JS per § 3.4; register the label pass with `claudeLogOnRehydrate`.
3. Fold depth segmented control in the toolbar; mapping per § 1.5;
   default `steps`; persisted.
4. Make the fold bar a one-line chevron summary (both sections kept).

**Acceptance & tests** (browser): a long Read result shows ≤ 4.4em +
fade, `+N lines` text equals the real line count; click opens, `− less`
closes and scrolls back; Prompts hides every assistant card in a turn
(assert `display` of child containers), Steps shows main-lane steps with
all `<details>` closed, All opens every collapsible; manual fold-bar
click clears the segment's `on`; state survives a reload; existing
fold-bar browser tests still pass (default theme).

**Dev-docs:** `message-hierarchy.md` (fold-depth mapping onto states
A/B/C).

**As built (P4 landed):**
- **Exports, minimal-gated.** The `{% if minimal %}` block glued after
  `setInitialFoldState();` exports `window.claudeLogApplyFoldState`,
  `window.claudeLogSyncFoldBar` and `window.claudeLogUpdateDetailsToggle`
  (the 📋 label) and calls `window.claudeLogMinimalInitFolds()` — defined
  by `minimal.js`, which runs at parse time, before `DOMContentLoaded`. So
  the stored depth lands after the classic initial state and **before** the
  hash / `?uuid=` deep links and search's `?q=` reveal anything: a link
  always beats the depth (tested). Classic bytes: unchanged.
- **Depth = own container per card, then sync** (deviation from "apply
  `first`/`open` top-down"): each card with a fold bar decides only whether
  its own `.children` is shown (`claudeLogApplyFoldState(card, 'folded')`
  to hide), then `claudeLogSyncFoldBar` re-derives every bar's icons — O(n),
  order-free, and the same code serves the live-update path. Prompts:
  headers shown, everything else folded. Steps: everything shown except a
  card whose immediate children include a deeper agent (`.sidechain` with a
  higher `agent-depth-N` than the card; non-sidechain = 0) — sync/async/
  teammate spawns and workflow agents — which stays folded (P6's "Main
  only" will own these). All: everything shown, every collapsible open.
  Table in dev-docs/message-hierarchy.md "Fold depth".
- **Overrides.** A fold-bar click **and the 📋 open/close-all** clear the
  segment's `on` (all `aria-pressed="false"`); opening one preview doesn't
  (the mockup's per-item `ov` keeps the depth too). Re-choosing a depth
  re-applies it everywhere. Stored in `claude-code-log:fold-depth` (always
  written, default `steps`), storage in `try/catch`.
- **Live update.** The rehydrate hook applies the current depth only to
  cards tagged `live-new` (not yet seen, tracked in a `WeakSet`) and, on
  the patch path (scope ≠ `#transcript`), to a card whose `.children` it
  has never seen (a card that just gained its first children takes the new
  container wholesale). The swap's restored state and every hand override
  are left alone; tested on a real `serve`-style server through a swap
  then a patch, at Prompts and at All.
- **Collapse labels.** `data-more` is written on the **`<summary>`**, not
  the `<details>` (deviation): CSS `attr()` reads only the pseudo-element's
  own element. Values: `+ N lines` (from `.line-count`, else the body's
  `\n` count — `N` is the block's **total** line count, as the acceptance
  asks, not "lines beyond the preview", which would need layout per block),
  `+ N items` for a params table fold, `+ more` when ≤ 2 lines (a long
  single line). Labels and `− less` are generated content (also the
  trailing `button.mn-less`, appended to blocks of ≥ 12 lines/items), so
  `textContent` — search, timeline — never sees them. No JS: the
  `.line-count` is the label (`+ ` prefix), else `+ more`.
- **Previews.** `--pv` 4.4em (code, output), 2.8em (`.preview-content.markdown`
  — assistant text, thinking, Task results — and params); the mask is
  `linear-gradient(#000 calc(var(--pv) * .45), transparent var(--pv))`,
  measured in `--pv` so a preview shorter than the clip fades only at its
  foot. Open: summary = one `− less` line. Params keep their key-toggle
  design: label beside the preview, a keyed row's open summary stays hidden
  (classic). Hooks, IDE selections and workflow errors (other `<details>`)
  are untouched.
- **Pygments polish (pulled in).** Line-numbered code lost its stacked top
  margins/td padding (`.highlight`, its table and both `<pre>`s each added
  one), so a clipped code preview starts with code.
- **Fold bar** per step 4: chevron masks (`--mn-chev`, double for
  `.fold-all-levels`) on `.fold-icon::before`, rotated from `.folded`; the
  glyph text the state machine writes is hidden.
- **Cross-theme fix (requested).** `search.html`: `restoreSearchState`'s
  read **and** `saveSearchState`'s write are in `try/catch` — the write too,
  because `performSearch` calls it mid-search and a throw there left
  `isSearching` stuck, so the search never ran. Snapshots via `just
  update-snapshot` (`+601/-51`): block level, no block added or removed;
  the ten classic/index blocks each `+9` and identical to before once the
  two search hunks are substituted back; the minimal block `+460` (CSS, JS,
  toolbar).
- **Head size.** Minimal representative `<h1>` at 198KB (P3b: 196KB);
  classic 113KB.
- **Tests.** New `test/test_minimal_fold_browser.py` (23, marker `browser`):
  preview clip/fade/labels (= real line counts), open/`− less`/scroll-back,
  trailing `− less` only on long blocks, params key toggle, 📋 still works,
  labels after a rehydrate, no-JS label fallback; Steps default, Prompts,
  All, reload persistence, fold-bar override, preview click keeps the
  depth, sub-agents folded at Steps, deep link beats a stored depth, blocked
  storage; filter at every depth, search revealing a match past a preview at
  Prompts, timeline independent of depth; live update (swap + patch)
  preserving depth and overrides, All on new cards; in-page search with
  storage blocked (classic + minimal). Adjusted: P3b's blocked-storage test
  (minimal at Steps has nothing folded), P3a's (no page error at all now).
- **Seen, not fixed (P3a layout):** a paired call's first half
  (`pair_first`, empty content) is as tall as its two-line gutter, so a
  ~15px gap sits between a call line and its output box. The timeline's
  "Tool" filter doesn't drop vis items — classic too, pre-existing. (Both
  fixed in P5.)
- **Screenshots** in
  [`work/minimal-theme-dag-screenshots/p4/`](minimal-theme-dag-screenshots/p4/):
  a real sample session (`real_projects/…claude-code-log-sample/fe869ecb…`)
  at Prompts / Steps / All, light and dark, desktop.

### P5 — Server-side lane annotation and data attributes — M

**Goal:** § 3.3. Pure server work; the page renders exactly as after P4
(attributes are inert until P6).

**Files:** new `claude_code_log/lanes.py`, `renderer.py`
(`TemplateMessage.lane_id`), `html/renderer.py` (call
`annotate_lanes` when minimal; pass lanes to the template),
`transcript.html` (attributes, minimal-gated, inline), tests,
`dev-docs/agents.md` § 5.4 / `dag.md` (lane ids), `application_model.md`
§ 1 table row for `lanes.py`.

**Steps:**
1. Implement `annotate_lanes` per § 3.3; stats: steps = rendered cards
   in the lane excluding `is_last_in_pair`; tokens/duration from
   `TaskOutput.metadata` (sync) or `TaskNotificationMessage.usage`
   (async), else duration from first/last timestamps, tokens omitted;
   format like `6 steps · 48.4k tokens · 2m 13s` (the pair-duration
   string built near `renderer.py` ~2327 and
   `html/async_formatter.py::_format_usage_rows` are the existing
   formatting precedents — share a helper rather than adding a third).
2. Merge rows: sync → the spawn's paired tool_result
   (`pair_last` index); async → the `TaskNotificationMessage` with
   `task_id == agent_id` (use `spawning_task_message_index` to pair).
3. Forks: lane per branch header; spawn row = fork-point card
   (`content.parent_message_index`); the main-continuation branch is the
   earliest one at the rewind (§ 7 decision 3).
4. Teammates: `kind="teammate"`, `data-teammate-link`.
5. Template: attributes from § 3.3, emitted only when minimal; read
   `lanes` from a dict keyed by `message_index` passed into the template.

**Acceptance:** `just ci`; default snapshots unchanged; minimal
snapshots gain attributes only (block-level check).

**Tests** (unit, new `test/test_lanes.py`): fixtures
`test/test_data/nested_agents/` (load with `load_directory_transcripts`
— the single-file loader skips agent integration, see teammates.md § 8),
`async_agents/`, `teammates/`, `dag_fork.jsonl`, `dag_within_fork.jsonl`:
lane ids, kinds, parents (nested chain depth 3), spawn/merge indices,
stats strings, teammate exclusion; new minimal snapshot
`test_minimal_async_agents_html` (+ nested agents).

**As built (P5 landed):**
- **Module.** `claude_code_log/lanes.py`: `LaneInfo` as § 3.3 plus `tag`,
  `meta`, `depth`, `head_index`, `first_index`, `turn_index`, `rank`,
  `cards`, `agent_id` / `branch_session_id` and a `stats` property.
  **Deviation:** `annotate_lanes(roots)` returns a `LaneModel` —
  `lanes: dict[str, LaneInfo]` (spawn order) plus `continuations`
  (`{branch-header index: (lane continued, fork-point index)}`, the
  "earliest branch continues" headers, which are not lanes) — rather than a
  bare dict. Rendering it as attributes is HTML-only:
  `html/minimal_theme.lane_attributes(model)` → `{message_index: [(name,
  value)]}`, and the Jinja global `mn_lane_attrs(message, lane_attrs)`
  emits them (double-quoted, escaped). `HtmlRenderer._generate_inner` calls
  both after `_annotate_tree_for_render` (so `should_render` is known),
  **only for `theme == "minimal"`**; classic gets no call and no bytes.
  `TemplateMessage.lane_id` (default `"main"`) is render-time only, not in
  the fragment-store key.
- **Shared helpers** (plan step 1): `utils.format_duration` now produces
  the pair duration (`took …`, byte-identical) and the lane stats;
  `utils.compact_count` moved from `minimal_theme` (re-exported there);
  `utils.parse_timestamp`; `renderer.spawned_agent_id_of` was
  `_relocate_subagent_blocks`' inner `_spawned_id`, now shared so the lane
  model and the block relocation agree on which card opens which agent.
  `async_formatter._format_usage_rows` keeps its own `15.5s` format (moving
  it would change classic output past 60 s).
- **Rules as built.** Agent lane = the card's own `{trunk}#agent-<id>`
  session line; spawn row = the agent's tool_result `pair_first` (an
  interrupted spawn without a result: the stamped tool_use card); merge =
  that tool_result (sync) or the `<task-notification>` (async: matched on
  `task_id`, else `spawning_task_message_index`; kind `async-agent` when
  `run_in_background` or a notification matched). Fork lanes per § 1.6.4
  and § 7 decision 3: branch headers grouped by `attachment_uuid`
  (fallback `parent_message_index`), the one whose first message is
  earliest continues the fork point's lane; non-agent cards take the lane
  of their `render_session_id`. **A lane exists only once it has a
  rendered card** — a transcript that deduplicated into its spawn pair
  (#213: `nsleaf11/12/21`, `nschain3` in the fixture) or was stripped at a
  reduced depth leaves an ordinary tool call; nodes of a dropped lane are
  re-homed to its parent. **Teammates**: their threads keep the spawner's
  lane; the model records them (`kind="teammate"`, `rank` 0) only for the
  link. **Workflow** agents (and everything under a Workflow tool_use)
  inherit their lane. **Turn** = the top-level card (a direct child of a
  session or branch header) holding the spawn row — so a fork point that is
  itself a prompt is its own turn, and a turn inside a fork branch is a
  group of its own; **rank** = 1-based order in that turn by spawn
  timestamp, then DOM position, then first message (nested lanes rank in
  their top-level turn's group). Steps = rendered cards minus each pair's
  second half (headers excluded); tokens/duration from the notification's
  `<usage>` (async) then the result tail's metadata, else the span of the
  lane's timestamps (omitted when zero).
- **Data available, as asked:** nested agents to any depth (synthetic
  visible 3-deep chain tested); an agent on a fork branch (parent = the
  fork lane); a fork inside a fork lane; a `/compact` continuing a branch
  (#331) stays in that fork lane; a fork point filtered to a `fork_only`
  landmark still spawns (attributes on the `.fork-point[id]` box). **Forks
  inside sub-agents: the data doesn't allow them** — agent lines map to
  their parent's render sid and never become branch pseudo-sessions, so an
  agent-internal rewind stays in the agent's lane (tested).
- **No JSON island** (deliberately): `live_update.js` patches changed
  *cards* in place by hashing each card's own markup, so per-card
  attributes stay current through a patch (a lane's growth changes its
  head card's `data-lane-stats`/`-ts`, a merge arriving adds `data-lane-to`
  — both just patch that card), whereas a separate data block would only
  refresh on a wholesale swap. Every render path (single page, paginated,
  forced streaming, render pool, session-scoped `--combined no`) goes
  through `HtmlRenderer.generate`, so lanes are computed per page/session
  file; `test_lanes.py::TestRenderPaths` checks all of them produce the
  same lane data.
- **Schema P6/P7 consume** (all minimal-only; `d-N` = a card id without
  the `msg-` prefix):

  | On | Attribute | Value |
  |---|---|---|
  | every `.message` card, and a fork-only `.fork-point[id]` box | `data-lane` | `main` \| `agent-<agentId>` \| `branch-<branch sid>` |
  | spawn card (agent tool_use; fork-point card or fork-only box) | `data-spawns` | space-separated lane ids it opens |
  | merge card (sync tool_result; async `<task-notification>`) | `data-merges` | space-separated lane ids it closes |
  | lane head (agent: the spawn tool_use; fork: its branch header) | `data-lane-id` | lane id |
  | | `data-lane-kind` | `agent` \| `async-agent` \| `fork` |
  | | `data-lane-name` | Task description / branch preview (fallback `Branch <uuid8>`) |
  | | `data-lane-tag` | first word of the name, lower-case, ≤ 12 chars (`fork` for forks) — the gutter tag |
  | | `data-lane-meta` | agents: `subagent_type · model · async` (parts present); forks: `rewind` (P6 adds the local time from `data-lane-from`'s card) |
  | | `data-lane-parent` | `main` or the enclosing lane id |
  | | `data-lane-depth` | `1` from main, +1 per enclosing lane |
  | | `data-lane-from` | `d-N` of the spawn card (absent if unresolvable) |
  | | `data-lane-to` | `d-N` of the merge card (absent for forks and unfinished agents) |
  | | `data-lane-turn` | `d-N` of the user turn (branch group) |
  | | `data-lane-rank` | 1-based rank in that turn (> 3 → "+N more branches") |
  | | `data-lane-stats` | `6 steps · 48.4k tokens · 2m 13s` |
  | | `data-lane-ts` | `<first ISO> <last ISO>` of the lane's cards |
  | branch header that continues its fork point's lane | `data-lane-continues` | the lane it continues (its `data-lane` too) |
  | | `data-lane-from` | `d-N` of the fork point |
  | teammate spawn tool_use | `data-teammate-link` | `d-N` of the thread's first card |
  | | `data-teammate-name` | the teammate's name |

  A lane's cards are every element with that `data-lane`; nested lanes'
  cards sit inside their parent lane's DOM subtree. Branch headers that
  start a fork lane carry the fork's `data-lane` themselves.
- **For P6/P7.** Card timestamps still come from
  `.timestamp[data-timestamp]`; the fork lane's head (a branch header) has
  none — use the first value of `data-lane-ts`. An agent's cards nest
  under its spawn's tool_result, so in the DOM they follow a sync lane's
  merge card although they pre-date it: order by timestamp, not by DOM. A lane without
  `data-lane-to` (fork, unfinished or interrupted-without-result agent)
  draws a stub, or to its last row when interleaved. `SendMessage` ↔
  `<teammate-message>` links (§ 1.6.5) are not resolved yet (P7).
- **Extra item 1 — tool pairs (P4's "seen, not fixed").** `layout.css`,
  wide layout only: a `pair_first` card's gutter is `height: 0` (children
  `flex-shrink: 0`) and hangs into the result half's empty gutter, and its
  dot gets a negative bottom margin (`calc(-0.7em - 3.5px)`) — the dot
  spans the `1fr` slack row and was sizing it by its own margin box
  (~13px) even with the gutter gone. The first block under a call line (a
  `tool_use` body, a `pair_middle`/`pair_last` output) and the code box at
  the head of its preview lose their top margins. Measured on the sample
  session: call → output 14–19px before, 2px after; dot position
  unchanged. Test: `test_minimal_theme_browser.py::TestRowLayout::test_tool_call_and_output_read_as_one_unit`
  (fails on the old CSS). Screenshots (sample session, light, desktop) in
  [`work/minimal-theme-dag-screenshots/p5/`](minimal-theme-dag-screenshots/p5/):
  one Read pair before/after, and a stretch of the session after.
- **Extra item 2 — timeline "Tool" filter (both themes).** The toggle's
  type is `tool`, the timeline groups `tool_use`/`tool_result`, so the
  groups never hid. `timeline.html` now maps toggles to groups with the
  transcript's own expansion (`tool` → `tool_use` + `tool_result`, `user` →
  `user` + `bash-input` + `bash-output` — the same bug for bash lines) and,
  for exact parity, also hides **items** whose card is `filtered-hidden`
  (the per-item pass that already mirrored search): a sub-assistant's tool
  call (sidechain group) or an async result now hides with its card. Test:
  `test_timeline_browser.py::test_tool_filter_removes_tool_items_from_timeline[classic|minimal]`
  (fails on the old timeline).
- **Snapshots** (`just update-snapshot`): block level — 2 blocks added
  (`test_minimal_async_agents_html`, `test_minimal_nested_agents_html`),
  none removed; the nine classic transcript blocks each change by the same
  43 diff lines, every one a line of the timeline component's old/new text
  (nothing else); the index block is unchanged; the minimal representative
  block `+74/-25` (timeline, the pair CSS, `data-lane="main"` on every
  card).
- **Tests.** New `test/test_lanes.py` (26): fixtures async/nested/
  teammates/workflow/`dag_within_fork`/`dag_compact_after_rewind`, synthetic
  many-forks-in-one-turn (ranks 1–4), fork-in-fork, `fork_only` landmark,
  visible 3-deep chain, agent on a fork branch, rewind inside an agent;
  HTML attributes (head, merge, fork headers, escaping, classic carries
  none, a notification arriving updates the head card); all render paths
  agree. `test_theme_option.py::test_minimal_keeps_every_card_hook` strips
  the lane attributes before comparing with classic.

### P6 — DAG engine: main-only + interleaved + rail — L

**Goal:** § 1.6.1 (folded, interleaved), § 1.6.2 (Main only /
Interleaved), § 1.6.4, § 3.5 steps 1–8 except columns.

**Files:** `components/minimal/minimal_dag.js`, `dag.css`,
`transcript.html` (include + `#dag-rail` already there from P3a),
tests, new `dev-docs/minimal-theme.md` (architecture as built: ownership
split, triggers, algorithm) linked from `application_model.md` § 1.

**Steps:** follow § 3.5. Port the pack and rail code from
`work/minimal-theme-dag-mockups/DagRail.dc.html` `renderVals()`
(generalise the fixed `LANES[x].slot`, keep the per-row slot states),
then render slot states as SVG. Default mode folded; branch control on
the spawn card with stats, chevron, `Column ⇥` button (disabled until
P7); LRU cap 3 per group; tags in the gutter for interleaved rows;
DagThreads tint/indent for interleaved rows.

**Acceptance & tests** (browser, `test/test_minimal_dag_browser.py`, on
a converted tmp copy of `nested_agents/` + `async_agents/` + a fork
fixture):
- default: no sidechain card visible; each spawn card has a branch
  control with the server stats text; SVG has one dashed path per
  visible branch from spawn to merge (async: merge at the notification).
- interleave one lane: its cards become visible, their visual order
  (sorted by `getBoundingClientRect().top`) is non-decreasing in
  `data-timestamp` across main + lane; connector paths exist at spawn
  and merge rows.
- interleave a 4th lane in a group → the least recently selected folds.
- global Main only / Interleaved switch all lanes; persists.
- filter parity: turning off "Sub-assistant" hides interleaved lane
  cards *and* the timeline's sidechain group; turning it back restores
  the layout (MutationObserver relayout).
- fold parity: folding a user turn containing a spawn hides its lane
  rows and rail.
- live update: call `window.claudeLogRehydrate(document.getElementById('transcript'))`
  after replacing `#transcript` with a clone that has one extra card →
  layout recomputed, no stale inline `grid-row` on removed nodes.
- timeline click still scrolls to the card (P1 fix).
- no-JS: with JavaScript disabled the page shows the nested sidechain
  blocks (Playwright `java_script_enabled=False` context).

**As built (P6 landed):**
- **Files.** `components/minimal/minimal_dag.js` (engine), `dag.css`;
  `transcript.html` (dag.css after `pygments_dark.css`; `<div id='dag-rail'
  aria-hidden='true'>` as the stage's first child — it was *not* there from
  P3a; the engine inside the minimal `<script>` after `minimal.js`);
  `header.html` (Branches segment); `layout.css` (card rows renumbered, see
  below); new `test/dag_demo_fixture.py` (mockup-shaped project:
  two async agents interleaving with main tool calls, a synchronous agent with
  a nested one, a rewound prompt; `turns=N` repeats it for timing) and
  `test/test_minimal_dag_browser.py` (25, marker `browser`); new
  [`dev-docs/minimal-theme.md`](../dev-docs/minimal-theme.md) (as-built
  architecture, linked from application_model.md § 1, CLAUDE.md, mkdocs
  nav).
- **Always a grid; rows packed over every lane (deviation from § 3.5
  steps 2–6, for performance).** `dag-on` makes `#transcript` a one-column
  grid for good and every grid item (card, fork-point box, nested block)
  gets `grid-row`; rows come from ordering *all* lanes as if interleaved, and
  a folded lane is hidden with empty (0-height) rows. Toggling a lane, a
  filter, a search or a fold rewrites no other card's row. The first build
  (grid only while something was interleaved, open lanes only) cost
  400–600 ms per lane click on a 2,761-card page — the browser re-laid every
  later row; now 60–90 ms (below).
- **`dag-hidden` sits on wrappers, not cards**: a folded sub-agent lane's
  container (`.children`) or a fork lane's `.message-node` (its head is the
  branch header). An interleaved sub-agent container gets `dag-entry`
  (`display: contents !important`), so **the Branches mode, not the fold
  depth, decides whether a sub-agent shows** (§ 1.5) — P4's
  `test_steps_folds_sub_agents_and_all_opens_them` now asserts that All opens
  the containers while Main only keeps the lanes hidden. The owning card
  (`dag-owner`, the spawn's result) hides its fold bar: the branch control is
  the one handle. Folding an ancestor (a user turn) still hides the lane —
  inline `display: none` on the ancestor container.
- **Nested groups that are not lanes stay nested** (`dag-block`, one grid
  item, `display: block`, nested gutter): teammate threads (§ 1.6.5),
  workflow phases (§ 7 decision 6), old-style sidechains. A lane spawned
  inside such a block is not walked (no control, no rail; renders nested).
- **Order.** k-way merge by timestamp (main first on ties); a lane waits for
  its spawn, a merge card for its lane's end. Added beyond the mockup (whose
  items were whole steps): **a tool call's result half follows its call at
  once** unless it merges a lane — otherwise concurrent lanes split every
  pair. A synchronous spawn's call and result are separated by the lane's
  rows; both halves then get `dag-split` and show their own gutter + dot (the
  result reads as the merge row).
- **Rail.** Greedy interval colouring over `[spawn, end]`, nested lanes right
  of their parent, interleaved lanes first, max 6 slots (folded lanes
  without a slot keep only their control). Rail column = 18px + 16px × slots
  in use (`--dag-slots`); main line stays at 9px. Connectors as SVG paths:
  8px radius, dashed `3 4` vertical for folded lanes, 12px stub for a folded
  fork; a nested lane forks from / merges into its parent's slot. Paths carry
  `data-lane` / `data-part` (fork, lane, merge, stub) for tests. **Colours
  (§ 1.2 decision):** forks always `--lF`; agents by slot `--lA`, `--lB`,
  `--sys`, `--asst` (the fixtures reach 3 concurrent lanes; the mockup
  reserves pink for forks, so agents don't use it).
- **Interleaved rows** (`dag-in dag-sN dag-lc-x`): DagThreads indent
  (8px + 16px × slot; phone 6 + 6 × slot) and 9% lane tint from the content
  column, dot on the lane's slot, lane tag in the gutter as generated content
  (`--dag-tag`, from `data-lane-tag`) — search and the timeline never see it.
- **Branch control** (`.mn-bctls`, appended to the spawn card, **card grid
  row 4**: rows are now title / debug / content / controls / fold bar /
  slack, phone +1): chevron + `data-label` (generated content) = stats (forks:
  `⑂ <name> · stats`) + ` · interleaved`; title = name · meta (forks:
  `rewound to HH:MM:SS`, local time of the fork point); `Column ⇥` rendered
  **disabled** (P7). Re-created after a live patch drops it.
- **Continuation headers** (`data-lane-continues`) render as a slim
  `⑂ rewound · …` line (fold bar and back-link hidden); branch headers in the
  stream start at the content column and no longer mask the rail.
- **Cap / global.** LRU per `data-lane-turn`, cap 3; selecting a nested lane
  selects its parents first and never evicts them; folding a lane folds its
  nested lanes. Global *Interleaved* = rank ≤ 3 per turn. Stored
  `claude-code-log:branches` (`main` default). The segment is `hidden` until
  the page has a lane (and without JS). New lanes from a live update take the
  global choice only if their turn has room.
- **Pulled forward from P8:** search, `#msg-` links (load + `hashchange`) and
  `?uuid=` deep links open the lane holding their target (the classic reveal
  hooks are wrapped by intercepting their assignment). A search matching
  cards in more than three lanes of one turn leaves only the last three open
  — P8's parity sweep should decide whether search should lift the cap.
  Timeline clicks on a card in a folded lane still don't open it (as with
  P4's folded sub-agents) — P8.
- **Performance** (`?debug-dag` logs each relayout; `window.claudeLogDag
  .timing()`), synthetic `write_dag_demo(dir, 60)` page — 2,761 cards,
  2,821 grid items, 300 lanes — headless Chromium at 1280px: steady
  relayout 30–36 ms (engine JS); open/fold one lane 35–40 ms JS, 60–90 ms
  with the browser's style + layout; global Interleaved ≈ 50 ms JS / 800 ms
  total (1,560 cards appear); global Main only ≈ 420 ms; load ≈ 85 ms JS
  (680 ms with the page's first full layout). Inside the § 3.5 budget
  (< 50 ms for 2,000 visible cards) for the engine's own work.
- **Head size.** Minimal representative `<h1>` at 211KB (P5: 198KB; dag.css
  ≈ 11KB in the head; the engine, ≈ 54KB, is at the end of the body).
- **Snapshots** (`just update-snapshot`): block level — none added or
  removed; every classic block and the index identical; the three minimal
  blocks each `+1374/-14` (CSS/JS text, the rail host, the row renumbering).
- **Screenshots** in
  [`work/minimal-theme-dag-screenshots/p6/`](minimal-theme-dag-screenshots/p6/)
  (64-colour PNGs): the demo fixture (`async-agents-*`) and
  `dag_within_fork.jsonl` (`fork-*`), Main only and Interleaved, light and
  dark, desktop.
- **For P7.** Hooks are marked: `GLOBAL_MODES` and the header's Columns
  button; `pack()`'s `key` (per-column `nextFree`); the per-lane `.mn-bcol`
  button. Column cards need `grid-column` and the stage grid more columns
  (`#transcript`'s `grid-template-columns`), and column lanes must be left
  out of `railLanes` (the mockup's `railed` = folded + interleaved only).
  Overflow (`+N more branches`) is not done: every lane gets a control and,
  while slots last, a rail lane.

### P7 — Columns, branch overflow, teammate anchors — L

**Goal:** § 1.6.1 column mode, § 1.6.3 overflow, § 1.6.5 teammates.

**Files:** `minimal_dag.js`, `dag.css`, `minimal.js` (toolbar
Columns), possibly `html/teammate_formatter.py` **only if** a link must
live inside formatter output (prefer the template/engine-owned
`data-teammate-link`), tests, `dev-docs/minimal-theme.md`,
`dev-docs/teammates.md` (§ 6.1 note on the minimal link).

**Steps:**
1. Column mode: lanes in columns get `grid-column: 4 + i`, packed rows
   (mockup "Pack rows" with per-column `nextFree`), column header
   (name, meta + stats, `⇤ Interleave`, `Collapse`), collapse to a 34px
   strip with vertical label and expand; stage widens
   (`max-width:none`) and the scroller scrolls horizontally; main column
   keeps `minmax(360px,1fr)`.
2. Nested lanes in columns: each lane its own column ordered by spawn
   time (a nested lane is placed right of its parent column).
3. Branch overflow per § 1.6.3 (`+N more branches` / `− fewer`).
4. Teammates: spawn row link `→ <name>'s thread` to `#msg-d-N`
   (existing hash handler reveals it); `SendMessage` ↔
   `<teammate-message>` links where resolvable.

**Acceptance & tests** (browser): Columns puts each lane's cards in a
distinct column with tops time-ordered within the page; two cards from
different columns can share a row (equal `top` within 1px); collapse to
strip/expand; horizontal scroll appears when columns exceed the
viewport; a fixture with ≥ 4 branches in one turn (build one in
`test/test_data/` with a small generator script modelled on
`scripts/gen_nested_agents_fixture.py`, or synthesise in-test) shows 3
controls + `+N more branches`, revealing the rest; teammate spawn link
navigates to and reveals the teammate thread's first card; teammates
never get a lane or column.

### P8 — Docs, polish, parity sweep — M

**Goal:** user-facing docs, remaining parity, performance sanity.

**Files:** `docs/themes.md` (new) + `mkdocs.yml` nav, `README.md` (one
line + screenshot optional), `docs/live-updates.md` (theme works under
`serve --watch`), `dev-docs/application_model.md` (§ 1 table row
"Themes"), `dev-docs/css-classes.md`, `dev-docs/minimal-theme.md`
(final), `scripts/generate_example_output.py` (optionally also render a
minimal example page for the docs site), tests.

**Steps:**
1. User docs: what the theme is, `--theme minimal` /
   `CLAUDE_CODE_LOG_THEME`, classic/minimal/default naming, that themes
   overwrite the same files (README "Choosing a Theme" already has the
   P2 basics), toolbar,
   branch modes, dark mode, offline note (timeline still loads
   vis-timeline from unpkg).
2. Parity sweep: every filter toggle × every branch mode × timeline;
   search hit inside a folded lane reveals it (search calls
   `claudeLogRevealMessage`; the engine must switch that lane to
   interleaved when a match is inside it — add if missing).
3. Performance: render a large real-ish fixture
   (`test/test_data/real_projects/` if suitable) and record relayout
   time in `dev-docs/minimal-theme.md`.
4. `just docs-build` strict passes.

**Acceptance:** `just ci` and `just docs-build` green; all browser
tests from P3a–P7 green together.

---

## 6. Progress

- [x] P1 groundwork fixes
- [x] P2 theme plumbing
- [x] P3a look, toolbar, light/dark
- [x] P3b components, Pygments dark, timeline
- [x] P4 collapse + fold depth
- [x] P5 lane annotation
- [x] P6 DAG engine (main-only, interleaved, rail)
- [ ] P7 columns, overflow, teammates
- [ ] P8 docs and polish

---

## 7. Decisions and risks

Decisions 1–5 and 7–8 were taken by the user (1 implicitly by approving
P1; the rest before P2) and are recorded as decided; 6 remains open.

1. **Groundwork changes default bytes (P1).** *Decided:* one explicit,
   reviewed regeneration in P1 (landed); classic byte-identity from P2
   on.
2. **Output files.** *Decided:* `--theme minimal` **overwrites** the
   normal output files (`combined_transcripts.html`, `_N` pages,
   `session-*.html`, `index.html` …) — no filename variant. The theme is
   therefore part of every staleness decision, so switching theme
   regenerates everything and switching back does too; an output written
   in one theme is never current for the other, including session files
   and pages regenerated incrementally (no mixed-theme archives). As
   built: the generator stamp (§ 2.4, P2 as-built). The old
   `--detail minimal`-era filename collision is no longer a concern.
3. **Which fork branch continues "main".** *Decided:* the **earliest**
   branch at a rewind continues the main line; later branches are fork
   lanes (§ 1.6.4).
4. **Teammates.** *Decided:* keep teammate threads nested where they
   are, collapsed by default, not DAG branches; spawn / `SendMessage` /
   `<teammate-message>` rows hyperlink to the matching anchor on the same
   page (§ 1.6.5). The `session-<sid>.html?uuid=` fallback only matters
   if a future layout moves teammates into their own files.
5. **Branch-group cap.** *Decided:* the cap of 3 interleaved branches —
   and the `+N more branches` overflow — applies **per user turn**
   (§ 1.6.3).
6. **Workflow sub-agents** (#174) are not lanes in this feature; the
   index page and `search.html` are not themed. Both could follow.
   *(Open.)*
7. **Theme names.** *Decided (with P2):* the existing look is `classic`;
   valid values are `classic`, `minimal` and `default`. `default` means
   "the built-in default theme" — `classic` today, possibly `minimal`
   later — and resolves through the single constant
   `utils.DEFAULT_THEME`, so switching is a one-line change. Only
   resolved names are rendered and stamped, so an archive built with
   `default` regenerates if the constant changes rather than passing as
   fresh. `--theme default` is an explicit flag (it overrides the
   environment). Help/docs wording: "classic (current look), minimal, or
   default (the built-in default, currently classic)". Classic output
   stays byte-identical.
8. **`CLAUDE_CODE_LOG_THEME`.** *Decided (with P2):* sets the theme when
   `--theme` isn't passed, wherever the option exists (`convert`/default
   command, `serve`, `watch`, TUI exports); precedence flag > env >
   built-in default; it may be `default` too; an unknown value is a
   usage error naming the valid choices, never a silent fallback.
9. **Risks:** `display: contents` + one big grid on very large combined
   pages (mitigated: pagination, measured budget in P6, columns only on
   demand); live-update patches stripping engine-owned controls
   (mitigated: re-created on rehydrate, engine is idempotent); keeping
   classic bytes identical under Jinja without `trim_blocks` (every
   conditional inline; P2 adds a byte-identity test).

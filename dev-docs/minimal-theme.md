# Minimal theme

As-built reference for the minimal HTML theme (`--theme minimal`): where
its parts live (§ 0), its branch layout — the DAG engine (§ 1–8) — and the
polish, previews, parity guarantees and lane attributes it rests on
(§ 9–12). The user guide is [`docs/themes.md`](../docs/themes.md). The
feature's design and phase-by-phase history is
[`work/minimal-theme-dag.md`](../work/minimal-theme-dag.md) — a historical
record now; where it and this page disagree, this page (and the code) win.

## 0. Map

| Part | Where | Reference |
|---|---|---|
| `--theme` / `CLAUDE_CODE_LOG_THEME`, `default` → `utils.DEFAULT_THEME`, same filenames, the generator stamp that makes a page from the other theme stale | `cli._resolve_theme`, `utils.normalize_theme`, `html/renderer.html_generator_stamp` | [application_model.md § 2.1](application_model.md#21-cli) |
| Template branches (`{% if minimal %}`, glued inline so classic bytes never change), header and toolbar | `transcript.html`, `components/minimal/header.html` | [rendering-architecture.md § 8](rendering-architecture.md#theme-branches-in-transcripthtml) |
| Look: tokens, row grid, components, light/dark, dark Pygments | `components/minimal/` `tokens.css`, `layout.css`, `components.css`, `pygments_dark.css` (generated), `theme_init.js` | [css-classes.md § Minimal Theme](css-classes.md#minimal-theme-theme-minimal) |
| Server helpers: gutter, call line, session header, page meta, cross links, lane attributes | `html/minimal_theme.py` | css-classes.md (table), § 7, § 12 |
| Minimal-only formatter output: compact spawn rows, answers on merge rows, previews of long answers, diffs and commands | `HtmlRenderer.format_TaskInput` / `format_TaskOutput` / `format_TaskNotificationMessage` / `format_EditInput` / `format_MultiEditInput` / `format_BashInput`, `utils.render_markdown_preview`, `tool_formatters.collapse_long_diff` | § 7, § 9, § 10 |
| Collapse labels, fold depth, colour-scheme toggle, keyboard fold bars | `components/minimal/minimal.js` (the live filter / search refresh is shared: `transcript.html`, `search.html`) | [message-hierarchy.md § Fold depth](message-hierarchy.md#fold-depth-minimal-theme), § 9, § 11 |
| Lane model (which cards form a branch, spawn / merge rows, turn, rank, stats, running state) | `lanes.py` | [agents.md § 6](agents.md#6-branch-lanes-minimal-theme), [dag.md § Branch lanes](dag.md#branch-lanes-minimal-theme), § 12 |
| DAG engine: branch modes, rail, columns, live relayout | `components/minimal/minimal_dag.js`, `dag.css` | § 1–8 |
| Teammate anchors | `lanes.teammate_links`, `minimal_theme.cross_links` | § 7, [teammates.md](teammates.md#minimal-theme-anchors) |
| Tests | `test_minimal_theme.py`, `test_lanes.py`, `test_theme_option.py`, `test_minimal_*_browser.py` (look, components, folds, DAG, columns, live, polish, parity sweep and smoke test), `TestMinimalThemeHTMLSnapshots`; fixtures `test/dag_demo_fixture.py`, `test/dag_live_fixture.py` | § 11 |

## 1. What it does

**Branches** are sub-agent transcripts (sync, async, nested to any depth)
and rewind forks — every lane P5 annotates (`data-lane-id` heads). Teammate
threads and workflow phases are not branches: they stay nested blocks.

Each branch is in one of four modes:

- **folded** (default, "Main only"): its cards are hidden; the spawn row
  carries a control (chevron, `N steps · tokens · duration`, for an async
  agent `Result ↓` and `launched ▸`, then `Column ⇥` — § 9) and the rail
  draws a **dashed** lane from the spawn row to the merge row
  (sync: the tool result; async: the `<task-notification>`, each holding
  the agent's answer, a two-line preview when long — § 10), or a short
  **stub** for a fork, which never merges;
- **interleaved**: its cards join the main stream at their real times, on
  their own rail slot, indented and tinted in the lane colour, with the
  lane's tag in the gutter, connected by curved fork / merge connectors (a
  fork lane runs to its last row);
- **column** (a swimlane): its cards move into a column of their own right
  of the main line, rows time-aligned with it (§ 3, step 4) — cards in
  different columns share rows, time still runs top to bottom. The column's
  head (sticky under the toolbar) names the lane and offers *⇤*
  (interleave) and *−* (collapse to a strip) — spelt out as *⇤ Interleave*
  and *Collapse* once the head is 400px wide — with its stats, then its
  meta, on a second line (§ 9); the spawn row's control reads
  `· in column →` and its button becomes *⇤ Interleave*. A column has no
  rail lane. Unlimited columns; the page widens and scrolls sideways;
- **strip**: a column collapsed to 34px, its name set vertically (click to
  expand back to a column); its cards are hidden.

A sub-agent that has not returned yet is drawn **running** on a live page
(§ 8): instead of a stub, its lane carries on to the newest row and ends in
an open marker, and its control shows a pulsing `running` label — with
`· quiet 42m` once it has been silent for a minute; when the result or
notification arrives it becomes the ordinary merge.

A lane nested in a column lane that is *interleaved* shows inside that
column (tinted, its tag in the column's gutter); put in a column itself, it
gets its own column, placed right after its parent's (columns are ordered by
spawn row, depth first).

At most **3 lanes per user turn** are interleaved; selecting a 4th folds the
least recently selected lane of that turn (never one the new lane is nested
in). Interleaving a nested lane interleaves its folded parents first (a
parent in a column stays there); putting one in a column puts its folded
parents in columns. Folding a lane folds every lane nested in it. Columns
are not capped.

**Branch overflow** (per user turn, spec § 1.6.3): of a turn's *top-level*
lanes (spawned from the main line), the first three by rank keep a control
and a rail lane; the rest stay folded behind a `+N more branches` toggle on
the third control (`− fewer branches` when shown). A lane that is not
folded always shows its control. Nested lanes are left out of the count:
they only appear once their parent is opened, and counting them would make
opening one lane hide another's control.

The toolbar's **Branches** segment (`.mn-branches`, shown once the page has a
branch) sets every lane: *Main only* folds all, *Interleaved* interleaves the
first three (by `data-lane-rank`) of each turn, *Columns* puts every lane in
a column. The choice persists in `localStorage` `claude-code-log:branches`
(`main` | `interleaved` | `columns`); per-lane changes are in-memory and
clear the segment's `on`. Lanes that appear later (live update) take the
global choice (interleaved only if their turn has room).

**Reveals** — search matches, `#msg-…` links (load and `hashchange`),
`?uuid=` deep links and timeline clicks — open the lane holding their
target before revealing it (`claudeLogRevealMessage` /
`claudeLogRevealMessageByUuid` are wrapped by intercepting their assignment,
so the wrap holds whenever the classic page sets them; the timeline calls
`claudeLogRevealMessage` before it scrolls, minimal theme only). A reveal
opens the lane as a column under the global *Columns* choice, or when the
lane (or one it is nested in) is a column or a strip, and interleaved
otherwise — **without evicting anything**: a search that hits more than
three lanes of one turn shows every hit, past the cap. The next manual
selection in that turn trims it back to three.

## 2. Layout without moving nodes

The server renders cards where it always has: nested
`.message-node > .children`, each card with its `data-lane`. The engine
never moves a DOM node (live updates patch cards in place by position; the
fold machine, filter and search walk the nesting).

- `dag-on` on `.mn-stage` turns every `.message-node` and `.children` into
  `display: contents` and `#transcript` into a one-column grid. Each card
  keeps its own internal grid (gutter / rail / content).
- Every grid item — a card, a fork-point box, a nested block — gets an
  inline `grid-row`. **Rows are packed as if every lane were interleaved**:
  a folded lane is hidden and its rows are empty (zero height). Order and
  rows therefore depend only on the DOM, so opening or folding one lane
  rewrites only that lane's cards (`test_rows_are_stable_when_a_lane_toggles`);
  filters, searches and folds rewrite no rows at all.
- A fold (inline `display: none` on a `.children`) still hides its subtree:
  inline styles beat the `display: contents` rule.

Containers the engine classifies on each walk:

| Container | Class | Display (with `dag-on`) |
|---|---|---|
| A sub-agent lane's transcript (first child card's lane ≠ the owner card's, `agent-…`), interleaved | `dag-entry` | `contents !important` — whatever the fold depth did |
| …folded | `dag-hidden` | `none !important` |
| A fork lane's node (its head is the branch header), folded | `dag-hidden` on the `.message-node` | `none !important` |
| Teammate thread / workflow phases / old-style sidechain (deeper `.sidechain` in the same lane, or `workflow_*`) | `dag-block` | `block`, nested look kept, one grid item |
| Anything else | — | `contents` |

The card that owns a lane container gets `dag-owner` (its fold bar is hidden
— the branch control is the one handle).

**Columns** add tracks to the same grid: `--dag-cols` on the stage
(`minmax(520px, 2fr)` for main — it gets twice a column's share of the
spare width —, `minmax(300px, 1fr)` per column, `34px` per strip; on a
phone, `dag-panes`, every column and main is the viewport's width minus the
page padding, and `<html>` snaps sideways from one column to the next with
`scroll-snap-type: x mandatory`, strips passed over), row 1 for the heads
(`--dag-head-h`, 46px: the name, truncated, and the two actions on the
first line; stats, `running …`, then the agent type and model on the
second, truncated from the end; everything in the head's `title`), cards from row 2 (always —
so entering columns renumbers nothing by itself). Every item gets an inline
`grid-column` while columns exist (auto-placement would otherwise put a
definite-row item after the last placed one). Column cards (`dag-col`, plus
`dag-colin` for a lane interleaved inside a column) use the compact row of
the mockup's `.w-col`: gutter left-aligned, no rail, no dot. The column
**chrome** — per column a background spanning every row (lane-colour edge,
5% tint) holding its sticky head or strip button, plus the main column's
"Main session" head — is a set of engine-owned `.dag-chrome` grid items at
the head of `#transcript`: first in DOM order, so they paint under the
cards. Nothing else reads them (live updates key on `.message-node`, search,
the filter and the timeline on `.message`); a wholesale swap drops them
until the next relayout. `<body>` gets `dag-wide` (no 960px cap, at least
`--dag-min` wide), so the page scrolls sideways while the header, toolbar,
filter panel, timeline and session navigation keep to the viewport
(`position: sticky; left`, `--dag-view` = the viewport width without its
scrollbar).

## 3. The relayout

A pure function of (DOM, lane modes), in `relayout()`:

1. **Lanes** from `[data-lane-id]` heads (`readLanes`), new ones adopted.
2. **Walk** (`buildModel`): one recursive pass over the nested DOM; every
   item records its lane, whether it is hidden (folded container, folded
   lane, `filtered-hidden` / `search-hidden`), and the spawn / merge items
   of each lane (`data-spawns` / `data-merges`).
3. **Order** (`order`): each lane keeps its DOM order; lanes are k-way merged
   by timestamp (`.timestamp[data-timestamp]`, else the previous card's in the
   lane; a fork head uses its `data-lane-ts`), main first on ties. A lane
   cannot start before its spawn; a merge card waits until its lane is
   exhausted; a tool call's result half follows its call at once unless it
   merges a lane (the synchronous-agent case). Deadlocks (clock skew) fall
   back to lane order.
4. **Pack** (`pack`): a port of `DagRail.dc.html` / `DagLanes.dc.html`'s
   "Pack rows", `nextFree` per column key (`columnKey`: a lane in a column
   or strip is its own key, any other lane takes its parent's, main lanes
   `main`). Without columns this is one item per row.
5. **Rail slots** (`railLanes`): slot 0 is the main line; each lane with
   something to draw gets the lowest free slot over `[spawn row, end row]`
   (greedy interval colouring; a nested lane starts right of its parent).
   Lanes laid out in a column (and lanes behind a turn's overflow) get no
   rail.
   End = merge row, else the page's last row for a running lane (§ 8), else
   the lane's last row when interleaved, else the spawn row. Interleaved lanes are placed first, folded ones while slots last
   (6). Colours: forks `--lF`, agents by slot `--lA`, `--lB`, `--sys`,
   `--asst`.
6. **Write** (MutationObserver disconnected; the only layout read,
   the viewport width, happens before it): engine classes (`dag-hidden`,
   `dag-entry`, `dag-block`, `dag-owner`, `dag-in`, `dag-sN`, `dag-lc-x`,
   `dag-split`, `dag-col`, `dag-colin`, `dag-ack`) diffed per element; `grid-row`,
   `grid-column` and `--dag-tag` only when changed; `--dag-slots` (the rail
   column widens by one `--dag-step` per slot) and `--dag-cols` on the
   stage (and `dag-panes` for phone columns), `dag-wide` / `--dag-min` /
   `--dag-view` on `<body>`; the column
   chrome; branch controls ensured on every spawn card (overflow: `hidden`
   on the extra lanes' controls, the `+N more branches` toggle).
7. **Draw** (`draw`): one batched read of the few rows the rail needs (spawn,
   merge, first / last shown row of each lane), then the SVG in `#dag-rail`
   is rebuilt. Paths carry `data-lane` and `data-part` (`fork`, `lane`,
   `merge`, `stub`); a folded lane's `lane` part is dashed (`3 4`).
   Connector radius 8px; dot centre = `padding-top + .7em` of the card.

`dag-split` marks a tool pair whose halves are separated by interleaved rows
(a synchronous spawn and its result): both halves then show their own gutter
and dot.

## 4. Ownership and triggers

The engine owns, and nothing else writes: the `dag-*` classes, inline
`grid-row`, `grid-column` and `--dag-tag` on cards, `.mn-bctls` (appended to
spawn cards, grid row 4 of the card), the `.dag-chrome` column elements at
the head of `#transcript`, `dag-on` / `dag-cols` / `--dag-slots` /
`--dag-cols` on the stage, `dag-wide` / `--dag-min` / `--dag-view` on
`<body>`, and `#dag-rail`. It writes nothing else. The fold machine owns `.children`
inline `display`; filter and search own their classes.

| Trigger | Action |
|---|---|
| MutationObserver on `#transcript` (structure; `style` on `.children`; a card's hidden classes changing) | relayout in the next frame |
| `claudeLogOnRehydrate` (live swap or patch) | re-observe a swapped `#transcript`; relayout in a microtask, coalesced over the update's rehydrate calls — before the browser lays out or paints the new markup (a patched card's control and classes come back) |
| ResizeObserver on `#transcript`, `resize`, the phone media query | redraw only — relayout when phone columns (`dag-panes`, sized to the viewport) are shown, or the phone query flips with columns |
| `animationend` of `live-new-in` (a live card's fade-in, a transform) | redraw only: the rail measured the card mid-way |
| a 30 s timer, on a live page | relayout while a lane runs (its `quiet …` label), or if one stopped / started reading as running |
| branch control / column head / toolbar click, a reveal that opens a lane | relayout now |
| hidden page | deferred until `visibilitychange` |

Labels (control text, gutter tag) are generated content (`attr(data-label)`,
`var(--dag-tag)`), so search and the timeline never index them.

`window.claudeLogDag` exposes `relayout()`, `timing()`, `mode(laneId)`
(`folded` | `interleaved` | `column` | `strip`, as shown: a lane inside a
folded one reads `folded`), `running(laneId)`, `quietMs(laneId)` (how long a
running lane has been quiet, else `null`) and `runningMaxQuietMs` for tests; `?debug-dag` logs each relayout's timing to the console.

Engine state that outlives a relayout — per-lane modes, the per-turn LRU of
interleaved lanes, the turns whose `+N more branches` are shown — is keyed
by what a live update cannot renumber: lane ids (`agent-<id>`,
`branch-<sid>`) and, for turns, the turn card's `data-uuid` (the
positional `d-N` in `data-lane-turn` shifts whenever an entry lands
mid-page and the update swaps).

## 5. Performance

Measured on a synthetic 60-repetition page (`test/dag_demo_fixture.py`,
`write_dag_demo(dir, 60)`: 2,761 cards, 2,821 grid items, 300 lanes),
headless Chromium, 1280px:

| Operation | Engine (JS) | Including the browser's style + layout |
|---|---|---|
| Steady relayout, Main only | ≈ 30–36 ms | — |
| Open or fold one lane | ≈ 35–40 ms | 60–90 ms |
| Global Interleaved (300 lanes, +1,560 cards shown) | ≈ 50 ms | ≈ 800 ms |
| Global Main only | ≈ 50 ms | ≈ 420 ms |
| First layout at load (includes the page's first full layout) | ≈ 85 ms | ≈ 680 ms |

The first design flipped `#transcript` between block and grid and packed
only the open lanes, so every toggle renumbered every later row: one lane
cost 400–600 ms there. Packing every lane fixed that.

Columns (P7), same page and browser: putting a lane in a column (or back)
re-packs rows across the page, so the browser re-lays the whole grid.

| Operation | Engine (JS) | Including the browser's style + layout |
|---|---|---|
| One lane to a column / back to interleaved | ≈ 40–45 ms | ≈ 350–370 ms |
| Steady relayout, one column | ≈ 41–44 ms | — |
| Global Columns (300 columns, 90,424px wide, 2,162 rows) | ≈ 80–110 ms | ≈ 0.95–1.2 s |
| Steady relayout, 300 columns | ≈ 46–78 ms | — |
| Collapse one of 300 columns to a strip | ≈ 55 ms | ≈ 530 ms |
| Columns → Main only | ≈ 60–75 ms | ≈ 470 ms |

Global Columns on a page this size is the user's explicit choice ("the user
decides when it's too squashed"); per-lane columns stay well under half a
second.

**Real projects (P8).** The demo's cards are tiny; real cards are not, and
on a real page the browser's style and layout dominate. Headless Chromium,
1280px, a 4-core VM, medians of three; "engine" is the relayout's own
timing (its draw forces the page's layout, so it includes that), "with
paint" ends after the next frame:

| Page | Cards / lanes | Size (classic → minimal) | First relayout | Main only ↔ Interleaved ↔ Columns | Prompts / Steps / All |
|---|---|---|---|---|---|
| `…claude-code-log-sample` (combined) | 1,434 / 5 forks | 9.98 → 10.55MB | 0.55s | 0.40–0.51s engine, 0.51–0.64s with paint | 0.26 / 0.54 / 1.57s |
| `…coderabbit-review-helper` | 1,134 / 6 agents | 4.69 → 5.18MB | 0.49s | 0.16–0.43s, 0.22–0.52s | 0.18 / 0.39 / 0.83s |
| `-experiments-worktrees` | 311 / 6 agents | 0.94 → 1.25MB | 0.06s | 0.05–0.10s, 0.07–0.12s | 0.03 / 0.05 / 0.08s |

The engine's own JavaScript (model, order, write) stays at 20–45ms on the
largest page; a steady relayout (nothing changed) is ≈ 20ms. *All* opens
every `<details>` on the page, which is the browser laying out every
long block in full.

**Load.** Time until a page is laid out after `load` (median of three):
sample 1.85s classic → 2.79s minimal, coderabbit 1.18s → 2.29s, worktrees
0.36s → 0.53s. Most of the difference is one more full style and layout
pass when the engine turns the grid on (`dag-on`) after the browser's
first, nested layout — the parser yields on a large page, so that first
layout happens before `DOMContentLoaded`. P8 removed two avoidable ones
(≈ 0.3–0.4s on these pages): `minimal.js` measured the toolbar at parse
time (forcing a layout of the still-nested page) and then wrote
`--mn-bar-h` / `--mn-filter-h` on the root, and changing a custom property
on the root restyles every element; it now measures from its
ResizeObserver's first report, reads before it writes, and writes only a
real change (`tokens.css` declares the usual 46px / 0px). Session
summaries are measured in the first frame rather than at parse time.

**Conversion (P8).** `scripts/bench_render.py --theme {classic,minimal}`
on the eight `test/test_data/real_projects` (19MB of transcripts, 4
cores; two runs per theme, "memo only" row): a full rebuild 4.0–4.2s
classic (10.2–10.4s CPU) vs 4.1–4.5s minimal (10.9–11.3s CPU), i.e. ≈ +6%
CPU for the lane annotation and the minimal-only formatting; an
incremental run (the 9MB sample stale) 3.4–3.5s vs 3.5–3.6s. Output grows
from 47.0MB to 57.9MB (+23%): every page inlines the minimal stylesheets
and scripts on top of the classic ones — ≈ +250KB per page (a small
transcript: 288KB classic, 540KB minimal) — plus the lane attributes.

## 6. Without JavaScript

No `dag-on`: nothing is `display: contents`, sub-agent transcripts render
nested under their spawning result (2px `--ring` line), fork branches as
branch headers, the Branches segment stays `hidden` and no controls exist.
Teammate anchors and the async `Result ↓` link are server-rendered and work
as plain links; an async spawn's result card shows its launch line and that
link as before (folding them into the branch control is the engine's, § 9).

## 7. Teammate anchors and results at the merge row

Server-side, minimal theme only (classic and Markdown output unchanged):

- **Teammates** are not branches: their threads stay nested and collapsed
  by default (a `dag-block`), and `lanes.teammate_links` links the two
  ends of each exchange on the page — the spawn card to the thread's first
  card, a `SendMessage` to the `<teammate-message>` that delivered it and
  back. `html/minimal_theme.cross_links` renders a `.mn-xlinks` row of
  `#msg-d-N` anchors (grid row 4, like the branch controls; labels as
  generated content). See
  [teammates.md § Minimal theme: anchors](teammates.md#minimal-theme-anchors).
- **An async agent's answer** is shown on its `<task-notification>` card —
  the branch's merge row, at the time it arrived — and not on the spawn:
  the spawn's result keeps the launch line (wrapped in `.mn-ack`) and a
  `Result ↓` link (`.mn-async-jump`, to
  `TaskOutput.async_notification_index`), and drops the Agent id row (the
  notification shows it as its Task ID). With JavaScript both fold into the
  spawn's branch control (§ 9). A sync
  agent's answer already is its merge row (the paired tool result). The
  page holds one copy of the answer either way
  ([agents.md § 2.3](agents.md#23-the-fold-phase-3)).

## 8. Live updates and running lanes

Under `serve` (`live_update.js`, [application_model.md § 2.15](application_model.md#215-watch-mode-and-live-page-updates))
a page patches changed cards in place, or swaps `#transcript` wholesale
when ids renumber — which a sub-agent growing mid-page always does (its
block sits under its spawn, not at the end). Either way the engine
relayouts before the next paint (§ 4) from the same state: lane modes,
columns, the cap's LRU and the overflow survive, the fold machine and
`live_update.js` keep fold state, `<details>` and scroll position, and
`ensureControls` / `syncChrome` diff against what is there, so a patch or a
swap never leaves a second control or column head. Tested end to end
against a real `serve --watch` (session page) and the `watch --combined
yes` conversion (combined page) in `test/test_minimal_dag_live_browser.py`,
on a session that grows on disk like a live one
(`test/dag_live_fixture.py`).

**Running lanes.** An agent lane with no merge row is either still running
or ended without one (a crash, a killed background agent, a session that
stopped). The rule is split so the HTML stays a pure function of the
transcript:

- **Server** (`lanes.py`, `data-lane-state` on the lane head): `ended`
  when the page proves it — a *synchronous* agent's parent line (same lane,
  same session) has a later model step or prompt (the parent blocks on a
  synchronous call; steering, tool calls, results, notifications and hooks
  prove nothing), a `TaskStop` of the agent's id whose result says it
  stopped (P7c; a stop that found nothing proves nothing), or the lane is
  nested in a lane that merged or ended — else `open`. Merged lanes and
  forks carry no state; a killed or failed background agent still gets its
  `<task-notification>`, which is its merge row. An async agent is never
  ended by its parent moving on: the parent does not wait for it.
- **Client** (`minimal_dag.js`, rule revised in P7c): an `open` lane reads
  as **running** on a page served live (`window.claudeLogLiveUpdate` —
  never from `file://`), **however long it has been quiet**: agents wait on
  background processes, watchers and Monitor tasks that can run silently
  for hours. Its label says how long instead — `running · quiet 42m`
  (`2h 5m`, `2d 3h`), from the lane's last activity (its newest card, or a
  nested lane's; else the spawn), omitted under a minute and brought up to
  date by a 30 s timer. One bound remains, per session: a session whose
  newest card (any lane; per session, so one live session on a combined
  page does not wake another's) is more than **a week** old
  (`RUNNING_MAX_QUIET_MS`) reads as stopped. `serve` runs over whole
  archives, and an agent that died with a session closed months ago is
  not running; a week is far past any silent wait seen in practice.
  (P7b's rule — running only while the session's newest card was at most
  30 minutes old — called long background waits ended.)

Everything else — `ended`, `open` on a static page, or `open` in a session
silent for over a week — reads as **ended without a result**: the control
says `· no result`, and the rail draws it
as before (a stub while folded, to its last row while interleaved).

A running lane, folded or interleaved, takes a rail slot over `[spawn row,
last row]`, so it runs beside everything after its spawn: fork connector,
then a lane (dashed while folded, the dashes drifting towards the open end;
solid while interleaved) to the bottom of the newest shown row, ending in
an open circle (`data-part='end'`, `dag-end`, filled with `--bg`). Its
control gets `is-running` and a `running` pill (`.mn-brun`, generated
content, `running · quiet …` once quiet); a running column's head adds the
same to its meta. Animations
stop under `prefers-reduced-motion`.

**Server-side freshness.** A running agent appends to
`<sid>/subagents/agent-<id>.jsonl` while the trunk sits still (a
synchronous spawn blocks it). The trunk's cached rows and the entry
store's held list both carry the agents' spliced transcripts, so both are
pinned to the agent transcripts too: the cache's sub-agent fingerprint
covers `agent-*.jsonl` (count, newest `mtime_ns`, total bytes) as well as
the sidecars, and the entry store's stamp includes that fingerprint.
Before P7b a watch served the agent's block as it was at the last trunk
change (`test_lanes.py::TestLiveGrowth`).

## 9. Compact spawn rows and other polish (P7c, P8)

- **Spawn rows.** A `Task` / `Agent` spawn on the main line is its call
  line (description, subagent type, `[async #id]`, model — dim metadata),
  the prompt and the branch control. A prompt longer than two lines or 160
  characters is the shared `<details>` preview of its first two non-blank
  lines (`tool_formatters.format_task_prompt_preview`), its `+N lines`
  label beside the preview; the `Run background` row is left out (the call
  line's `[async …]` says it). Teammate fields on the spawn, agent metadata
  on its result and an async notification's task fields read as one dim
  wrapped line of `key value` pairs.
- **Async acknowledgement.** An async spawn's result card that holds only
  `.mn-ack` + `.mn-async-jump` gets `dag-ack` (hidden) from the engine; the
  spawn's control gains `Result ↓` (`.mn-bres`, to `#msg-<data-lane-to>`,
  the notification) and `launched ▸` (`.mn-back`, `aria-expanded`), which
  shows the card again (state per lane id, `acksShown`). Without JavaScript
  the card reads as before.
- **Fold bars** are `--dim` at rest (light `#868d96`, 3.3:1; dark `#6b727c`,
  3.7:1 against `--bg`), `--muted` while their card is hovered or the bar
  has focus, `--fg` on the section itself; a main prompt's bar stays
  `--muted`. `minimal.js` makes the sections focusable buttons
  (`tabindex=0`, `role=button`, Enter / Space click), re-applied on
  rehydrate.
- **Forks.** The fork-point box is one muted line — `⑂ Fork point` and the
  branch links (truncated, full text in `title`); the header's preview of
  the card above is wrapped in `.fork-point-preview` (minimal template only)
  and hidden. The session navigation lays a fork point and its branches
  out as one wrapped line of short links (sessions still start a line).
- **Interleaved gutters.** A call half's gutter hangs into its result
  half's; the lane tag (and an error pill) now sits on the result half, one
  gutter line (1.85em) down, so the lines never overlap.
- **One-line controls (P8).** Wider than a phone, a branch control never
  wraps: its items are `flex: none` and only the chevron button's label
  (the stats, `· no result`) shrinks, with an ellipsis. The mode suffix
  (`· interleaved`, `· in column →`) is a separate attribute, `data-mode`,
  drawn by the button's `::before` after the label (`order: 2`), so it
  never truncates with the stats; the stats are in the button's `title`
  and `aria-label`. In Columns with four columns at 1600px the main column
  has ≈ 470px of content, which used to push `⇤ Interleave` onto a line of
  its own. A phone keeps wrapping.
- **Column heads (P8).** Line 1: the name (truncated) and two compact
  actions, `⇤` and `−` (`data-label`; a container query writes their
  `data-long` words, *⇤ Interleave* / *Collapse*, once the head is 400px
  wide). Line 2, `.dag-colmeta`: the lane's stats first, then `running …`,
  then its meta (agent type, model) — so a 300px column keeps the stats
  and drops the meta first. P7c's head put the actions beside the meta and
  truncated it in almost every column.

## 10. Previews of long content (P8)

The theme previews long blocks with the formatters' shared `<details>`
(`render_collapsible_code`; restyled by `components.css` with a fade,
`+ N lines` and `− less`, [message-hierarchy.md](message-hierarchy.md#fold-depth-minimal-theme)).
The classic formatters only make a block collapsible past their own
thresholds (20 lines for Markdown), and some blocks never — so in "Main
only" a multi-screen agent report or a 300-line diff still filled the
screen. Minimal-only, server-side (classic output untouched):

| Block | Inline when | Otherwise | Where |
|---|---|---|---|
| Sub-agent prompt on its spawn row (P7c) | ≤ 2 lines and ≤ 160 characters | the first two non-blank lines | `utils.render_markdown_preview` via `tool_formatters.format_task_prompt_preview` |
| A sync agent's answer on its merge row (`TaskOutput`) | ≤ 3 lines and ≤ 320 characters (`ANSWER_INLINE_*`) | the first two non-blank lines | `format_task_output(preview=True)` |
| An async agent's answer on its `<task-notification>` (the merge row) | as above; a JSON payload keeps its 10-line code preview | as above | `render_async_result_body(preview=True)` |
| An `Edit` diff, each `MultiEdit` diff | ≤ 12 lines | the first three diff lines | `tool_formatters.collapse_long_diff` |
| A `Bash` command (a heredoc, an inline script) | ≤ 12 lines | the first three lines | `format_bash_input(collapse=True)` |

The body is always the block exactly as classic renders it, so search
(which reads the card's text), the "All" fold depth (which opens every
`<details>`) and `− less` work unchanged. A heading at the top of an
answer is drawn at body size in the preview, so `## Summary` does not fill
the two-line clip. On a real project (`-src-deep-manifest`, Main only) the
tallest card went from 5,275px (an `Edit` diff) to 522px (an assistant
reply, which is content and is not previewed).

## 11. Parity: filter, search, timeline and live updates (P8)

Whatever the filter hides in the transcript it hides in the timeline, in
every branch mode and at every fold depth, and neither the filter nor the
search changes a lane's mode. `test/test_minimal_parity_browser.py` checks
it exhaustively on the demo, with the timeline open: every visible filter
toggle off and on again × Prompts / Steps / All × Main only / Interleaved /
Columns, each time asserting that no card the filter or search hides is
visible, that every timeline item is shown exactly when its card is (group
visible and item not individually hidden), that every card has its grid
row and that turning the toggle back on restores exactly the cards shown
before; then two toggles off at once across the depths, a search into a
folded lane in each mode (it opens, then clears cleanly), and a live swap
with a filter set and with a search set.

**What the sweep found.** A live update (`serve --watch`) brings the
server's markup — a patched card, or on a swap a whole new `#transcript` —
with no `filtered-hidden` and none of the search's classes, and the
transcript's filter and search never looked at it again: a page with the
Tool toggle off showed every tool card of the new markup (both themes;
pre-existing). P8 fixed it for this theme only (classic bytes could not
change then); the follow-up moved the fix into the shared templates, so
both themes take one path:

- **The refresh** (`transcript.html`, a rehydrate hook registered at parse
  time between the timeline's and the DAG engine's): once per update it
  queues one microtask that calls `window.claudeLogApplyFilter` (the
  transcript's `applyFilter`) when any toggle is off, then
  `window.claudeLogRefreshSearch` (`search.html`), then re-syncs the
  timeline's per-item state. Registration order puts it after the
  timeline's rebuild and before the engine's relayout, which therefore
  lays out the already-filtered page.
- **Quiet.** The search refresh re-indexes and re-runs the query
  (`performSearch(…, {quiet: true})`) without navigating to the current
  match, revealing matches or opening `<details>`, so a live page never
  moves under the reader; the filter observer's own re-search after the
  refreshed filter is quiet too (`quietRefresh`, a 300ms window).
- **The current match survives.** `preserveCurrent` matched the current
  match by element identity, which a live update breaks (a patch replaces
  a changed card, a swap every card): a current match that left the page
  is found again by its `data-uuid` and its position among the cards
  sharing it (`cardKey`), so *4 of 4* becomes *4 of 5*, not *1 of 5*.
- **The filter observer reacts to `filtered-hidden` only.** It used to
  re-run the search on any class change of a card except search's own,
  and not quietly — so a live update's `live-new` tag (and the engine's
  `dag-*` classes) re-ran the search and scrolled to the current match.
  Nothing else on a card changes what the search reads.

`test/test_live_update.py::TestLiveUpdateKeepsFilterAndSearch` covers it
against a real `serve --watch`, in both themes, on a patch and on the
swap: a type filter hides the answers an update brings (and the toggle's
count follows), and an active search marks the new matching card,
hides the rest, keeps the current match and leaves the scroll position
alone.

The smoke test in the same file renders the demo and three real projects
(`-experiments-worktrees`, `…coderabbit-review-helper`, `-experiments-ideas`)
and clicks through every colour scheme × fold depth × branch mode, takes
one lane through interleaved → column → strip → column → interleaved from
its own controls and opens the timeline, failing on any page error or
console error (other than a blocked unpkg fetch) and on any broken
invariant above.

## 12. Lane attributes

The server's half of the engine (`lanes.annotate_lanes` →
`html/minimal_theme.lane_attributes` → the `mn_lane_attrs` Jinja global),
minimal theme only; `d-N` is a card id without the `msg-` prefix:

| On | Attribute | Value |
|---|---|---|
| every `.message` card, and a fork-only `.fork-point[id]` box | `data-lane` | `main` \| `agent-<agentId>` \| `branch-<branch sid>` |
| spawn card (agent tool_use; fork-point card or fork-only box) | `data-spawns` | space-separated lane ids it opens |
| merge card (sync tool_result; async `<task-notification>`) | `data-merges` | space-separated lane ids it closes |
| lane head (agent: the spawn tool_use; fork: its branch header) | `data-lane-id` | lane id |
| | `data-lane-kind` | `agent` \| `async-agent` \| `fork` |
| | `data-lane-name` | Task description / branch preview (fallback `Branch <uuid8>`) |
| | `data-lane-tag` | first word of the name, lower-case, ≤ 12 chars (`fork` for forks) — the gutter tag |
| | `data-lane-meta` | agents: `subagent_type · model · async` (the parts present); forks: `rewind` |
| | `data-lane-parent` | `main` or the enclosing lane id |
| | `data-lane-depth` | `1` from main, +1 per enclosing lane |
| | `data-lane-from` | `d-N` of the spawn card (absent if unresolvable) |
| | `data-lane-to` | `d-N` of the merge card (absent for forks and agents without a result) |
| | `data-lane-state` | agents without a merge row only: `open` \| `ended` (§ 8) |
| | `data-lane-turn` | `d-N` of the user turn (branch group) |
| | `data-lane-rank` | 1-based rank in that turn (> 3 → `+N more branches`) |
| | `data-lane-stats` | `6 steps · 48.4k tokens · 2m 13s` |
| | `data-lane-ts` | `<first ISO> <last ISO>` of the lane's cards |
| branch header that continues its fork point's lane | `data-lane-continues` | the lane it continues (its `data-lane` too) |
| | `data-lane-from` | `d-N` of the fork point |
| teammate spawn tool_use | `data-teammate-link` | `d-N` of the thread's first card |
| | `data-teammate-name` | the teammate's name |

A lane's cards are every element with that `data-lane`; nested lanes'
cards sit inside their parent lane's DOM subtree. Branch headers that
start a fork lane carry the fork's `data-lane` themselves. There is no
JSON island on purpose: `live_update.js` patches changed cards by hashing
each card's own markup, so per-card attributes stay current through a
patch (a lane's growth changes its head card's `data-lane-stats` / `-ts`;
a merge arriving adds `data-lane-to`). Every render path (single page,
paginated, streaming, render pool, session-scoped) goes through
`HtmlRenderer.generate`, so lanes are computed per page or session file
(`test_lanes.py::TestRenderPaths`).

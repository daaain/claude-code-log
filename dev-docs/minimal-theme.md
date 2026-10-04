# Minimal theme: the DAG engine

As-built reference for the branch layout of the minimal HTML theme
(`--theme minimal`). Design and phase history:
[`work/minimal-theme-dag.md`](../work/minimal-theme-dag.md) (§ 1.6, § 3.5,
P6 and P7 "As built"). The look, toolbar and fold depth are covered in
[css-classes.md § Minimal Theme](css-classes.md#minimal-theme-theme-minimal)
and [message-hierarchy.md § Fold depth](message-hierarchy.md#fold-depth-minimal-theme);
the lane data the engine reads in [agents.md § 6](agents.md#6-branch-lanes-minimal-theme).

## 1. What it does

**Branches** are sub-agent transcripts (sync, async, nested to any depth)
and rewind forks — every lane P5 annotates (`data-lane-id` heads). Teammate
threads and workflow phases are not branches: they stay nested blocks.

Each branch is in one of four modes:

- **folded** (default, "Main only"): its cards are hidden; the spawn row
  carries a control (chevron, `N steps · tokens · duration`, `Column ⇥`)
  and the rail draws a **dashed** lane from the spawn row to the merge row
  (sync: the tool result; async: the `<task-notification>`), or a short
  **stub** for a fork, which never merges;
- **interleaved**: its cards join the main stream at their real times, on
  their own rail slot, indented and tinted in the lane colour, with the
  lane's tag in the gutter, connected by curved fork / merge connectors (a
  fork lane runs to its last row);
- **column** (a swimlane): its cards move into a column of their own right
  of the main line, rows time-aligned with it (§ 3, step 4) — cards in
  different columns share rows, time still runs top to bottom. The column's
  head (sticky under the toolbar) names the lane, with its meta and stats,
  and offers *⇤ Interleave* and *Collapse*; the spawn row's control reads
  `· in column →` and its button becomes *⇤ Interleave*. A column has no
  rail lane. Unlimited columns; the page widens and scrolls sideways;
- **strip**: a column collapsed to 34px, its name set vertically (click to
  expand back to a column); its cards are hidden.

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
(`minmax(360px, 1fr)` for main, `minmax(300px, 1fr)` per column, `34px` per
strip), row 1 for the heads (`--dag-head-h`), cards from row 2 (always —
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
   End = merge row, else the lane's last row when interleaved, else the spawn
   row. Interleaved lanes are placed first, folded ones while slots last
   (6). Colours: forks `--lF`, agents by slot `--lA`, `--lB`, `--sys`,
   `--asst`.
6. **Write** (MutationObserver disconnected; the only layout read,
   the viewport width, happens before it): engine classes (`dag-hidden`,
   `dag-entry`, `dag-block`, `dag-owner`, `dag-in`, `dag-sN`, `dag-lc-x`,
   `dag-split`, `dag-col`, `dag-colin`) diffed per element; `grid-row`,
   `grid-column` and `--dag-tag` only when changed; `--dag-slots` (the rail
   column widens by one `--dag-step` per slot) and `--dag-cols` on the
   stage, `dag-wide` / `--dag-min` / `--dag-view` on `<body>`; the column
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
| `claudeLogOnRehydrate` (live swap or patch) | re-observe a swapped `#transcript`; relayout in the next frame (a patched card's control and classes come back) |
| ResizeObserver on `#transcript`, `resize`, the phone media query | redraw only |
| branch control / column head / toolbar click, a reveal that opens a lane | relayout now |
| hidden page | deferred until `visibilitychange` |

Labels (control text, gutter tag) are generated content (`attr(data-label)`,
`var(--dag-tag)`), so search and the timeline never index them.

`window.claudeLogDag` exposes `relayout()`, `timing()` and `mode(laneId)`
(`folded` | `interleaved` | `column` | `strip`, as shown: a lane inside a
folded one reads `folded`) for tests; `?debug-dag` logs each relayout's
timing to the console.

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

## 6. Without JavaScript

No `dag-on`: nothing is `display: contents`, sub-agent transcripts render
nested under their spawning result (2px `--ring` line), fork branches as
branch headers, the Branches segment stays `hidden` and no controls exist.
Teammate anchors and the async `Result ↓` link are server-rendered and work
as plain links.

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
  the spawn's result keeps the launch line and a `Result ↓` link
  (`.mn-async-jump`, to `TaskOutput.async_notification_index`), and drops
  the Agent id row (the notification shows it as its Task ID). A sync
  agent's answer already is its merge row (the paired tool result). The
  page holds one copy of the answer either way
  ([agents.md § 2.3](agents.md#23-the-fold-phase-3)).

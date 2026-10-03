# Minimal theme: the DAG engine

As-built reference for the branch layout of the minimal HTML theme
(`--theme minimal`). Design and phase history:
[`work/minimal-theme-dag.md`](../work/minimal-theme-dag.md) (§ 1.6, § 3.5,
P6 "As built"). The look, toolbar and fold depth are covered in
[css-classes.md § Minimal Theme](css-classes.md#minimal-theme-theme-minimal)
and [message-hierarchy.md § Fold depth](message-hierarchy.md#fold-depth-minimal-theme);
the lane data the engine reads in [agents.md § 6](agents.md#6-branch-lanes-minimal-theme).

## 1. What it does

**Branches** are sub-agent transcripts (sync, async, nested to any depth)
and rewind forks — every lane P5 annotates (`data-lane-id` heads). Teammate
threads and workflow phases are not branches: they stay nested blocks.

Each branch is either

- **folded** (default, "Main only"): its cards are hidden; the spawn row
  carries a control (chevron, `N steps · tokens · duration`, a disabled
  `Column ⇥` for P7) and the rail draws a **dashed** lane from the spawn row
  to the merge row (sync: the tool result; async: the `<task-notification>`),
  or a short **stub** for a fork, which never merges;
- **interleaved**: its cards join the main stream at their real times, on
  their own rail slot, indented and tinted in the lane colour, with the
  lane's tag in the gutter, connected by curved fork / merge connectors (a
  fork lane runs to its last row).

At most **3 lanes per user turn** are interleaved; selecting a 4th folds the
least recently selected lane of that turn (never one the new lane is nested
in). Interleaving a nested lane interleaves its parents first; folding a lane
folds the lanes nested in it.

The toolbar's **Branches** segment (`.mn-branches`, shown once the page has a
branch) sets every lane: *Main only* folds all, *Interleaved* interleaves the
first three (by rank) of each turn. The choice persists in `localStorage`
`claude-code-log:branches` (`main` | `interleaved`); per-lane changes are
in-memory and clear the segment's `on`. Lanes that appear later (live
update) take the global choice if their turn has room.

Search matches, `#msg-…` links (load and `hashchange`) and `?uuid=` deep
links inside a folded lane interleave it before revealing the target
(`claudeLogRevealMessage` / `claudeLogRevealMessageByUuid` are wrapped by
intercepting their assignment, so the wrap holds whenever the classic page
sets them).

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
4. **Pack** (`pack`): a port of `DagRail.dc.html`'s "Pack rows" (`nextFree`
   per column key — always `main` until P7's columns).
5. **Rail slots** (`railLanes`): slot 0 is the main line; each lane with
   something to draw gets the lowest free slot over `[spawn row, end row]`
   (greedy interval colouring; a nested lane starts right of its parent).
   End = merge row, else the lane's last row when interleaved, else the spawn
   row. Interleaved lanes are placed first, folded ones while slots last
   (6). Colours: forks `--lF`, agents by slot `--lA`, `--lB`, `--sys`,
   `--asst`.
6. **Write** (MutationObserver disconnected): engine classes (`dag-hidden`,
   `dag-entry`, `dag-block`, `dag-owner`, `dag-in`, `dag-sN`, `dag-lc-x`,
   `dag-split`) diffed per element; `grid-row` and `--dag-tag` only when
   changed; `--dag-slots` on the stage (the rail column widens by one
   `--dag-step` per slot); branch controls ensured on every spawn card.
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
`grid-row` and `--dag-tag` on cards, `.mn-bctls` (appended to spawn cards,
grid row 4 of the card), `dag-on` and `--dag-slots` on the stage, and
`#dag-rail`. It writes nothing else. The fold machine owns `.children`
inline `display`; filter and search own their classes.

| Trigger | Action |
|---|---|
| MutationObserver on `#transcript` (structure; `style` on `.children`; a card's hidden classes changing) | relayout in the next frame |
| `claudeLogOnRehydrate` (live swap or patch) | re-observe a swapped `#transcript`; relayout in the next frame (a patched card's control and classes come back) |
| ResizeObserver on `#transcript`, `resize`, the phone media query | redraw only |
| branch control / toolbar click, a reveal that opens a lane | relayout now |
| hidden page | deferred until `visibilitychange` |

Labels (control text, gutter tag) are generated content (`attr(data-label)`,
`var(--dag-tag)`), so search and the timeline never index them.

`window.claudeLogDag` exposes `relayout()`, `timing()` and `mode(laneId)`
for tests; `?debug-dag` logs each relayout's timing to the console.

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

## 6. Without JavaScript

No `dag-on`: nothing is `display: contents`, sub-agent transcripts render
nested under their spawning result (2px `--ring` line), fork branches as
branch headers, the Branches segment stays `hidden` and no controls exist.

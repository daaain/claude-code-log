# CSS Classes for Message Types

> See [application_model.md](application_model.md) for the system overview.

This document provides a comprehensive reference for CSS class combinations used in Claude Code Log HTML output, their CSS rule support status, and pairing behavior.

**Generated from analysis of:** 29 session HTML files (3,244 message elements)
**Last updated:** 2025-12-07

---

## Quick Reference

### Support Status Legend

| Status | Meaning |
|--------|---------|
| ✅ Full | Has dedicated CSS selectors for this combination |
| ⚠️ Partial | Inherits from parent selectors only |
| ❌ None | No CSS rules found |

---

## Base Message Types

| Type | Description | CSS Support |
|------|-------------|-------------|
| `assistant` | Assistant response | ✅ Full |
| `bash-input` | Bash command input | ✅ Full |
| `bash-output` | Bash command output | ✅ Full |
| `image` | User-attached image | ✅ Full |
| `session-header` | Session header divider | ✅ Full |
| `system` | System message (user-initiated) | ✅ Full |
| `system-away-summary` | Away-summary recap | ✅ Full |
| `system-error` | System error (assistant-generated) | ✅ Full |
| `system-info` | System info message | ✅ Full |
| `system-warning` | System warning (assistant-generated) | ✅ Full |
| `thinking` | Extended thinking content | ✅ Full |
| `tool_result` | Tool result (success) | ✅ Full |
| `tool_use` | Tool use message | ✅ Full |
| `user` | Basic user message | ✅ Full |
| `unknown` | Unknown message type | ❌ None |

---

## Modifier Classes

| Modifier | Applied To | Description |
|----------|------------|-------------|
| `compacted` | `user` | Compacted conversation summary |
| `command-output` | `user` | Slash command output content |
| `error` | `tool_result` | Tool execution error |
| `memory` | `tool_use`, `tool_result` | Auto-memory interaction: Read/Write/Edit on a `~/.claude/projects/<slug>/memory/` path (#192). Drives the 🧠 title, the `memory` filter toggle, and the timeline `memory` lane. |
| `pair_first` | Various | First message in a pair |
| `pair_last` | Various | Last message in a pair |
| `pair_middle` | Various | Middle message (never used so far) |
| `sidechain` | Various | Sub-agent (Task) message. The sidechain block under its spawning tool_result is framed by a single tool-green group line + 2em indent — the line continues the spawning card's border color (same pairing principle as the workflow phase/agent group lines). |
| `slash-command` | `user` | Expanded slash command prompt |
| `steering` | `user` | User steering via queue operation |
| `system-info` | `system` | System info level |
| `system-hook` | `system` | Hook execution summary |
| `system-away-summary` | `system` | Away-summary recap (left-aligned, narrative) |
| `workflow_phase` | `tool_use` | Spliced dynamic-workflow phase card (#174). The `tool_use` base keeps it under the "Tool Use" filter toggle; the modifier drives the depth-driven indent + dark-green card/group border, the 🧩 title, and a dedicated timeline lane. |
| `workflow_agent` | `tool_use` | Spliced dynamic-workflow agent card (#174). Same pattern as `workflow_phase` (grey card/group border, 🤖 title, own timeline lane). Its `.children` container indents the agent's grafted side-channel transcript one level further. |

### Runtime State Classes (applied by JavaScript)

These never appear in the generated HTML; they are toggled at runtime on `.message` elements:

| Class | Applied By | Description |
|-------|------------|-------------|
| `filtered-hidden` | type filter toggles (`transcript.html`) | Message hidden by the Search & Filter type toggles. The timeline mirrors this per lane (group visibility, through the same toggle expansion: `tool` → `tool_use` + `tool_result`, `user` → `user` + `bash-input` + `bash-output`) and per item (`timeline-filtered-hidden`), so an item in a lane no toggle governs alone — a sub-assistant's tool call, an async result — hides with its card. |
| `search-match` | search (`components/search.html`) | Message matches the active search query; contains `.search-highlight` spans. Folded ancestors are unfolded and collapsed `<details>` around each highlight are opened, so every highlight is actually visible. |
| `search-context` | search | Ancestor of a match, kept visible-but-dimmed when "Show context" is on. |
| `search-hidden` | search | Message hidden by search-as-filter (strict mode hides everything that isn't a match or context). The timeline mirrors this per-item via `timeline-filtered-hidden`. |
| `timeline-flash` | timeline (`components/timeline.html`, `onTimelineSelect`) | Brief (2 s) highlight of the message a timeline item was selected for. Coloured by `--timeline-flash-bg` (fallback `#fff3cd`) in `timeline_styles.css`; `!important` so it beats every card background, as the inline style it replaced did. |

Message cards also carry a `data-uuid` attribute (the transcript UUID — stable
across re-renders, unlike the positional `msg-d-N`/`data-message-id` slot ids).
Search uses it to keep the current match pinned when the same query is re-run
after an option or filter change.

**Image zoom** (`components/image_zoom.js` + `image_zoom.css`, both themes).
Every message image, a Markdown one included, is capped at its message's
width by `:where(#transcript .message) img` in `message_styles.css`. Its
zero specificity lets a rule that sizes an image on purpose (the artifact
favicon's `1em`) win.
A message image (`#transcript .message img`) gets `cc-zoomable`, a `zoom-in`
cursor, on hover when it is drawn smaller than its natural size. That is
measured, not marked: nothing is emitted per image. Clicking one opens the
page's single `dialog.cc-zoom`, created on first use with `showModal()`: a
`.cc-zoom-frame` holding the image and a `button.cc-zoom-close` (×).
- The frame is the image at fit-to-frame scale, at most 90% of the viewport
  each way, and refits on resize.
- The wheel zooms about the cursor between fit and the natural size, never
  past it.
- A left-drag pans (`cc-zoom-pannable` / `cc-zoom-panning` for the grab
  cursors), clamped so the image always covers the frame.
- ESC, a backdrop click or × closes the dialog, and focus returns to the
  page image.
- An image inside a link or a `<summary>` (and the artifact favicon) keeps
  its own click behaviour.

### Timeline Classes and Colour Variables

The vis-timeline component (`components/timeline.html`) keeps every colour
in `components/timeline_styles.css`; nothing colour-related is set inline
or from JS. The per-type colours (group labels, items) and the
container, resize-handle and select-flash colours each read a
`--timeline-*` custom property whose fallback is the default theme's
value (the remaining vis-timeline overrides use shared tokens such as
`--border-light` or literals). The default theme defines **none**
of them — a theme recolours the timeline by defining the properties alone.

| Class / element | Set by | Colour variable(s) |
|-----------------|--------|--------------------|
| `timeline-group-<id>` | `messageTypeGroups[<id>].className`; vis-timeline puts it on the group's label (`.vis-label`) **and** its row (`.vis-group`) | `--timeline-<id>-bg` (e.g. `--timeline-user-bg`, `--timeline-tool_use-bg`). The rule is scoped to `.vis-labelset .vis-label.timeline-group-<id>`, so only the label is coloured. `<id>` is the timeline message type: `user`, `assistant`, `tool_use`, `tool_result`, `thinking`, `system`, `image`, `sidechain`, `memory`, `slash-command`, `command-output`, `bash-input`, `bash-output`, `teammate`, `task-notification`, `workflow_phase`, `workflow_agent`. |
| `timeline-item-<type>` | `buildTimelineData()` on each item | `--timeline-item-<type>-bg`, `--timeline-item-<type>-border` (rules exist for `user`, `assistant`, `tool_use`, `tool_result`, `thinking`, `system`, `image`, `sidechain`; other types keep vis-timeline's item colours). |
| `timeline-filtered-hidden` | filter/search sync | — (`display: none`) |
| `#timeline-container` | template | `--timeline-bg`, `--timeline-border` |
| `#timeline-resize-handle` (and its grip `> div`) | template | `--timeline-handle-bg`, `--timeline-handle-hover-bg`, `--timeline-handle-active-bg`, `--timeline-grip`, `--timeline-grip-hover`, `--timeline-grip-active` |
| `.message.timeline-flash` | `onTimelineSelect` | `--timeline-flash-bg` |

`onTimelineSelect` scrolls to `getBoundingClientRect().top + window.scrollY`
rather than `offsetTop`, so the target stays right whatever the card's
offset parent is (e.g. a layout that sets `display: contents` or positions
a wrapper).

### Minimal Theme (`--theme minimal`)

The minimal theme (work/minimal-theme-dag.md) **layers over** the classic
stylesheets rather than replacing them: the minimal page inlines every
classic sheet, then `components/minimal/tokens.css`,
`components/minimal/layout.css`, `components/minimal/components.css` and
the generated `components/minimal/pygments_dark.css` and
`components/minimal/dag.css`. Card classes, ids, `data-uuid`, fold bars
and `.timestamp[data-timestamp]` are identical to the classic page card for
card (asserted in `test_theme_option.py`), so the filter, timeline, search,
fold state machine and live update behave the same in both themes.

**Scope.** Every minimal rule is scoped under `.theme-minimal`, the class
on `<body>` (set only in the template's minimal branch). Card rules use
two specificity tiers on purpose:

- `display: grid` on cards goes through
  `.theme-minimal :where(#transcript .message:not(.session-header))` — one
  class of specificity — so the classic hide rules (`.message.filtered-hidden`,
  `.message.search-hidden`, both `display: none`) still win;
- box properties go through `.theme-minimal #transcript …`, whose id outranks
  the class-only classic per-type margins and card chrome without `!important`.

`!important` is used only against inline styles: the branch-header indent
and the timeline container's scripted `top`.

**Other pages.** The project index and the archive search page take
`tokens.css`, `chrome.css` (the page, header, toolbar and segmented-control
rules, which `layout.css` includes for transcripts) and `pages.css`, scoped
under `body.mn-page` (`mn-index` / `mn-search`). Their rows reuse the
transcript row's grid and role colours (`--rc`) under their own classes —
`.mn-prow` (a project), `.mn-srow` (a session: `.mn-sgut` gutter,
`.mn-sbody`), `.mn-hit` with `.mn-k-<role>` (a search hit: `.mn-hgut`,
`.mn-hbody`) — and keep the classic hooks the scripts read
(`.project-card`, `.project-name a`, `.project-stats`, `.session-link`,
`.session-preview`, `.session-link-meta`, `.search-result-item a`,
`.search-result-excerpt`). See
[minimal-theme.md § 13](minimal-theme.md#13-project-index-and-archive-search).

**Tokens** (`tokens.css`). Light values on `:root`; dark values under
`@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }`
and again under `:root[data-theme="dark"]`; `color-scheme` follows
(`light dark` in Auto, forced in either explicit mode).

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` / `--fg` | `#ffffff` / `#1f2328` | `#14161a` / `#e4e6e9` | page, text |
| `--muted` | `#5f6670` | `#969da7` | metadata, dim text |
| `--dim` | `#868d96` | `#6b727c` | fold bars at rest (P7c; ≥ 3:1 on `--bg`) |
| `--rule` / `--rule2` | `#e3e6ea` / `#1f2328` | `#2a2e35` / `#c9ccd1` | the rail, project rules; turn and header rules |
| `--hair` | `#edeff1` | `#22262c` | the faint rule between rows (`--rule` 65% towards `--bg`) |
| `--code` | `#f3f4f6` | `#1d2025` | code and output boxes |
| `--userbg` | `#fbf2e8` | `#2a2118` | user prompt tint |
| `--user` `--asst` `--tool` | `#a14a00` `#6d28d9` `#1a7f37` | `#f0a35c` `#b8a1f8` `#6fcf8f` | role colours |
| `--sys` = `--warn` | `#8a5d00` | `#e0b45a` | system, warnings |
| `--err` / `--errbg` | `#b42318` / `#fdeceb` | `#ff8b84` / `#3a1b1b` | errors |
| `--note` | `#1f5fbf` | `#7cb4ff` | async results, links |
| `--ring` | `#2f8f46` | `#6fcf8f` | sub-agent nest line |
| `--add`/`--addbg`, `--del`/`--delbg` | `#116329`/`#e6f6eb`, `#a40e26`/`#fdeaec` | `#86e0a2`/`#14291c`, `#ff9da2`/`#331a1d` | diffs |
| `--ok` / `--okbg` | `#116329` / `#dff3e5` | `#86e0a2` / `#14291c` | success pill |
| `--l0` `--lA` `--lB` `--lF` | `#9aa1aa` `#2f8f46` `#1e6fd9` `#b0307a` | `#5d646e` `#6fcf8f` `#7cb4ff` `#f08bc4` | DAG lanes (P6) |
| `--sans`, `--mono` | system stacks | — | fonts (no web fonts) |
| `--gut`, `--rail`; `--gut-nest`, `--rail-nest` | `72px`, `18px`; `60px`, `16px` | — | row geometry; inside a sub-agent group (the gutters hold the role icon beside a ~10-character label) |
| `--ic`, `--ic-gap` | `12px`, `3px` | — | the gutter's role icon and its gap to the label |

`tokens.css` also points the classic custom properties at the tokens on
`body.theme-minimal` (`--text-primary: var(--fg)`, `--code-bg-color:
var(--code)`, `--user-color: var(--user)`, `--message-padding: 0`, …), so
every classic rule that reads a variable follows the scheme without being
restated. `components.css` extends that mapping (todo status/priority,
question/answer, workflow and depth-ring colours, the translucent card
tints, the `--timeline-*` properties) and overrides the rules that carry
colour literals one by one, by component (output boxes, params tables,
todo, AskUserQuestion/plan, teammates, workflow groups, forks and nav,
the search & filter panel, vis-timeline). ANSI colours are left alone.

Teammate colours: the dark `--cc-*` values live in the dark token blocks,
and `body.theme-minimal` derives every `--cc-*-bg` tint as
`color-mix(in srgb, var(--cc-…) 13%, var(--bg))`, so the tints follow the
in-page toggle (the classic tints switch on `prefers-color-scheme` only).
Badge text is `--bg` on the teammate colour in both schemes.

**Pygments dark** (`pygments_dark.css`, generated by
`scripts/generate_minimal_pygments_css.py`; a unit test guards drift).
Pygments' `github-dark` token colours, each rule written twice — under
`@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) .theme-minimal .highlight .<tok> }`
and under `:root[data-theme="dark"] .theme-minimal .highlight .<tok>`.
Each rule also resets what the classic (light) sheet sets on the same
token and github-dark doesn't (bold keywords, the `.err` border), so no
light styling leaks into dark. No background: the code box keeps
`--code`. Light mode uses the classic sheet unchanged.

**Classes and elements added by the minimal branch** (never in classic
output):

| Class / element | Where | Role |
|---|---|---|
| `body.theme-minimal` | `<body>` | scope of every minimal rule |
| `html[data-theme="light"\|"dark"]` | `<html>`, set by `theme_init.js` (head) and `minimal.js` | explicit colour scheme; absent = Auto. Stored in `localStorage` `claude-code-log:theme` (`light`/`dark`; Auto removes the key) |
| `header.mn-top`, `.mn-title-row`, `.mn-pill`, `.mn-smeta` | page header | title, main model, meta line (`minimal_theme.page_meta`) |
| `nav.mn-toolbar` | sticky toolbar | holds the classic floating buttons (same ids and classes, see below), `.mn-sp` spacer, `.mn-seg.mn-seg-icons` theme toggle (`button[data-mn-theme=auto\|light\|dark]`, `.on` + `aria-pressed`), `details.mn-more` overflow menu (`.mn-menu`) |
| `.mn-ibtn`, `.mn-i-<icon>` | toolbar buttons | icon is a CSS mask on `::before` (`--mn-icon`); the button's own text — which the page's scripts rewrite — is hidden with `font-size: 0` |
| `html.mn-parsing` | `<html>`, set by `theme_init.js` (head), removed by its `DOMContentLoaded` listener (the page's first) | hides `.mn-stage` while the page parses, so the transcript's first layout is the DAG engine's grid ([minimal-theme.md § 5](minimal-theme.md#5-performance)); never present without JavaScript |
| `.mn-stage` | wraps `#dag-rail` and `#transcript` | draws the rail's continuous hairline (`::before`); `dag-on` + `--dag-slots` from the DAG engine |
| `#dag-rail` | first child of `.mn-stage` | the DAG engine's SVG rail (branch lanes, connectors; in Columns each column's lane, its pointers faded by a per-colour `dag-fade-<c>` gradient — [minimal-theme.md § 2.1](minimal-theme.md#21-column-lanes)) |
| `.mn-seg.mn-branches`, `button[data-mn-branches=main\|interleaved]` | toolbar, after the depth segment | global Branches mode; `hidden` until the engine finds a branch |
| `dag-*` classes, `.mn-bctls` / `.mn-bctl` / `.mn-bfold` / `.mn-bmini` / `.mn-bres` / `.mn-back` | cards, containers, spawn cards | written by `minimal_dag.js` only — see [minimal-theme.md](minimal-theme.md). `.mn-bfold[data-label]` is the stats (truncated with an ellipsis on a narrow line), `.mn-bfold[data-mode]` the untruncated `· interleaved` / `· in column →` after it (P8) |
| `.dag-colhead`, `.dag-colname`, `.dag-colmeta`, `.dag-colacts > .mn-bmini[data-long]` | a column's sticky head (engine-owned) | name and actions on line 1, stats then meta on line 2; the actions read `⇤` / `−` and take their `data-long` words in a head at least 400px wide (container query) — [minimal-theme.md § 9](minimal-theme.md#9-compact-spawn-rows-and-other-polish-p7c-p8) |
| `:is(.task-prompt, .task-result, .task-notification-result) > details.collapsible-code` | a spawn's prompt, an agent's answer on its merge row | the two-line preview of a long prompt or answer (`utils.render_markdown_preview`), its `+ N lines` beside the last preview line; headings in the preview at body size (P7c, P8) |
| `.mn-ack` | an async spawn's result `.content` | wraps the launch acknowledgement (`HtmlRenderer.format_TaskOutput`, P7c); the engine folds the card into the spawn's control (`dag-ack`) |
| `.fork-point-preview` | inside `.fork-point-header` | the header's ` • preview` (the card above's text), hidden — the box is one line (P7c) |
| `.fold-bar-section[tabindex][role=button]` | fold bars | made keyboard-focusable by `minimal.js` (Enter / Space click) |
| `.mn-seg.mn-depth`, `button[data-mn-depth=prompts\|steps\|all]` | toolbar, before `.mn-sp` | fold depth (dev-docs/message-hierarchy.md "Fold depth"); `.on` + `aria-pressed` mark the current choice, none after a manual override. Stored in `localStorage` `claude-code-log:fold-depth` |
| `summary[data-more]` | the `<summary>` of `details.collapsible-code`, `.collapsible-details`, `.tool-param-collapsible` | the closed block's label (`+ N lines`, `+ N items` for a params table, `+ more`), written by `minimal.js` and drawn by `summary::after { content: attr(data-more) }` |
| `button.mn-less` | last child of a long (≥ 12 lines/items) collapsible | trailing `− less` (label in CSS): closes the block and scrolls its top back under the toolbar |
| `.mn-title`, `.mn-generic` | the card title span | the row's call line (column 3, mono). `minimal_theme.call_title` moves the title's leading pictograph and a leading repeat of the gutter label (tool name, role label, or an alias such as `Todo List`, `Async result`, `Task`) into a hidden `.mn-tn` span, so the line shows only the path/arguments (`.tool-summary` in `--fg`, details such as `.tool-subagent`/`.message-model` dimmed); the whole title is the span's `title` (plus the classic `title_hint`). `.mn-tn` stays in the DOM, so search and the timeline, which read the title's `textContent`, see the same text as in classic. `.mn-generic` (nothing left to show, or a role-only title) hides the line; when the hidden title said more than a bare role (a name-only tool title, `🚨 Error`), its tooltip moves to `.mn-role` (`call_title`'s `role_tooltip`), since the ellipsized gutter label is then the only place the name shows; any other ellipsized `.mn-role` gets its full text as `title` on first hover (`minimal.js`: only the rendered width says it is truncated) |
| `.mn-tn` | inside `.mn-title` | hidden pictograph + repeated name (mockup `.tn`) |
| `.mn-sh-sum`, `.mn-sh-id`, `.mn-sh-more` | non-branch session header's `.header` | compact header (`minimal_theme.session_header`): the summary alone, clamped to two lines (`-webkit-line-clamp`; full text as `title`; `.mn-open` unclamps), then the short session id, team badge and model as dim mono metadata. `minimal.js` adds the `+ more` / `− less` button (`.mn-sh-more`) only when the clamp hides text, on load, rehydrate and resize |
| `.mn-role`, `.mn-time`, `.mn-tok` | inside `.header-info` | gutter: role label (`minimal_theme.role_label`; `z-index: 1`, so a pair's next card can't cover the label where it overflows a shorter first card and swallow its hover), short time (server UTC, localised by `minimal.js` from the sibling `.timestamp[data-timestamp]`), compact tokens (`in · out`, full string as `title`) |
| `svg.mn-sprite` > `symbol#mi-<glyph>` | right after `<body>`'s opening tag, outside `#transcript` | the page's one icon sprite (`minimal_icons.icon_sprite`): every glyph, so a live update that brings a new kind finds its symbol; zero-size, `aria-hidden` |
| `svg.mn-ic[aria-hidden] > use[href="#mi-<glyph>"]` | first child of `.mn-role`; a branch header's `.header` (in place of `↳`) | the role icon (`minimal_icons.role_icon`): `fill: none`, stroke `currentColor` (`--rc`; `--lF` on a branch header), 1.5, round caps and joins, `--ic` square. Absolutely placed in the label's padding: at its right end on a wide row (a column of icons beside the rail; the label truncates before it), leading it on a phone and in a column (`dag-col`), inside the `error` pill. No text, so search, the filter and the timeline read only the label — [minimal-theme.md § 9](minimal-theme.md#9-compact-spawn-rows-and-other-polish-p7c-p8) |
| `.message::after` | every row but a session header | the hairline: 1px of `--hair` on the row's top edge, content column only (grid column 3; 2 on a phone) — none on a tool pair's result half (unless `dag-split`), a turn-rule row, a session header's first row, or a column's first card (`dag-cfirst`, written by the engine). The index's `.mn-srow + .mn-srow` and the search page's `.mn-hit + .mn-hit > a` draw the same line |

**Row layout.** A card is a grid `var(--gut) var(--rail) minmax(0, 1fr)` with
rows title / debug / content / branch controls (P6, empty unless the card
spawns a branch) / fold bar / `1fr` slack. `.header` is
`display: contents`: its title span lands in column 3, its `.header-info`
becomes the gutter (spanning every row, so a gutter taller than the content
grows the slack row instead of pushing the content down). `.message::before`
is the role dot on the rail, coloured by `--rc`, a per-card custom property
set from the type classes; the gutter's role label carries the role icon
(`.mn-ic`) and `.message::after` the hairline above the row (both in the
table above). `pair_middle`/`pair_last` drop gutter, dot and hairline.
On wide screens a `pair_first` card's gutter (`height: 0`) and dot
(negative bottom margin) add no height — the gutter hangs into the next
half's empty gutter — and the first block under a call line (a
`tool_use` body, a `pair_middle`/`pair_last` output) has no top margin,
so a call and its output read as one unit.
A main-lane `user` card that isn't a session's first gets the `--rule2`
turn rule. Sub-agent groups (`.children` holding `.message.sidechain`) are
indented to the content column with a 2px left line (classic ring colours)
and use the nested gutter/rail. Under 640px the rail becomes column 1 and
the gutter a single line above the content.

**DAG layout (`dag.css`, P6).** With JavaScript the engine flattens the
nesting (`display: contents`) into one grid and lays sub-agents and forks
out as lanes; the nested look above is what the page shows without it. As
built in [minimal-theme.md](minimal-theme.md).

**Gutter error pill.** A failed `tool_result` (and a `system-error`) shows
its role label as a lowercase `error` pill (`--err` on `--errbg`); a paired
failed result keeps that pill in its gutter although paired halves
otherwise drop the gutter — one gutter line down (`padding-top: 1.85em`,
wide layout), below the call's role, which hangs into that row (P7c).

**Workflow groups.** The phases group and each phase's / agent's
`.children` hang under the content column like a sub-agent group (one 2px
line per level — phase `--tool`, agent `--l0` — and the nested
gutter/rail) instead of the classic 2em-per-level indents. That is the
look without JavaScript; with the DAG engine (`dag-on`) phase and agent
cards are rows on the main gutter and each agent's transcript is a branch
lane (`dag.css` resets the nested gutter; minimal-theme.md § 1.1).

**Search & filter panel.** The classic `.filter-toggle` buttons render as
the mockup's chips: a dot in the type's role colour (filled when active),
active chips in `--fg`, hidden types struck through. Same elements and
classes, so the filter script is untouched.

**Timeline.** `components.css` defines every `--timeline-*` property
(group labels and items as role-colour tints of `--bg`, borders in the
role colour) and overrides vis-timeline's own sheet (panel borders, grid
lines, axis text, generic items, the tooltip) under `.theme-minimal`, so
the timeline follows the scheme too.

**Collapse previews.** The formatters' `<details>` (`.collapsible-code`,
`.collapsible-details`, `.tool-param-collapsible`) keep their markup; the
theme restyles them as the mockup's `.clip`. Closed: the summary's
`.preview-content` / `.tool-param-preview` is clipped to `--pv` (4.4em for
code and output, 2.8em for prose — a `.preview-content.markdown` — and
params) under a `mask-image` fade measured in `--pv`, followed by the
`data-more` label (params: beside the preview). Open: the preview is
hidden and the summary is a single `− less` line; a keyed params row keeps
its classic open state (summary hidden, the key's ⏷ closes it). All labels
are generated content, so search and the timeline (which read
`textContent`) never see them. Without JS a `.collapsible-code` shows its
formatter-written `.line-count` as the label (`+ ` prefix), the others
`+ more`. The classic 📋 open/close-all and every per-item toggle work as
before: the styling keys on `[open]` only.

**Fold bar.** One mono line; each section's classic glyph
(`.fold-icon`, rewritten by the fold state machine) is hidden and replaced
by a chevron mask on `::before` (`--mn-chev`, double for
`.fold-all-levels`), pointing right while the section is `.folded` and
rotated down otherwise. Dimmed at rest (`--dim`), `--muted` while the card
is hovered or the bar has keyboard focus (`:focus-within`), `--fg` on the
hovered / `:focus-visible` section; a main prompt's bar (`.message.user`,
not a notification, teammate, sidechain or steering card) stays `--muted`
(P7c).

**Spawn rows** (P7c). A spawn's prompt is a two-line `.preview-content`
whose `+N lines` label sits beside it (`.task-prompt > details > summary`
in a row); teammate fields on a spawn (`.teammate-spawn-card`), agent
metadata on its result and an async notification's
`.task-notification-card` render as one dim wrapped line of `dt dd` pairs.

**Forks and navigation** (P7c). `.fork-point` is one muted mono line
(`⑂ Fork point` + truncated `.fork-point-branch` links, full text in
`title`). In `.session-nav`, `.session-fork-point` and its `.session-branch`
items flow side by side (flex, wrapped; each truncated), sessions start a
line of their own.

**Toolbar offsets.** `minimal.js` keeps `--mn-bar-h` (toolbar height) and
`--mn-filter-h` (search & filter panel height, 0 when closed) on `<html>`;
the filter panel sticks at `--mn-bar-h`, the timeline at their sum, and the
resume toast drops below the toolbar (`--mn-toast-top`).

---

## Pairing Behavior

Message pairing creates visual groupings for related messages. The `pair_first` and `pair_last` classes control styling of paired messages.

### Pairing Rules by Type

| Base Type | Can Be `pair_first` | Can Be `pair_last` |
|-----------|---------------------|-------------------|
| `assistant` | No | Yes |
| `bash-input` | Yes | No |
| `bash-output` | No | Yes |
| `system` | Yes | Yes |
| `thinking` | Yes | No |
| `tool_result` | No | Yes |
| `tool_use` | Yes | No |
| `user` | No | Yes |

### Common Pairing Patterns

| First Message | Last Message | Linked By |
|---------------|--------------|-----------|
| `tool_use` | `tool_result` | `tool_use_id` |
| `bash-input` | `bash-output` | Sequential |
| `thinking` | `assistant` | Sequential |
| `user` (slash-command) | `user` (command-output) | Sequential |
| `system` (system-info) | `system` (system-info) | Paired info |

---

## All Class Combinations by Support Level

### ✅ Full Support (25 combinations)

These combinations have dedicated CSS selectors:

| Combination | Description | Occurrences |
|-------------|-------------|-------------|
| `assistant` | Assistant response | 419 |
| `assistant ` | Assistant (paired with thinking) | 104 |
| `assistant sidechain` | Sub-assistant response | 73 |
| `bash-input` | Bash command input | 5 |
| `bash-output` | Bash command output | 5 |
| `image` | Image content | (rare) |
| `session-header` | Session header divider | 29 |
| `system` | System message (user-initiated) | 20 |
| `system system-hook` | Hook summary message | (rare) |
| `system-error` | System error (assistant-generated) | (rare) |
| `system-info` | System info message | 118 |
| `system-warning` | System warning (assistant-generated) | (rare) |
| `thinking` | Thinking content | 199 |
| `thinking  pair_first` | Thinking (first in pair) | 104 |
| `thinking sidechain` | Sub-assistant thinking | (rare) |
| `tool_result` | Tool result (success) | 863 |
| `tool_result error` | Tool result (error) | 83 |
| `tool_result sidechain` | Sub-assistant tool result | 83 |
| `tool_use` | Tool use message | 946 |
| `tool_use sidechain` | Sub-assistant tool use | 84 |
| `user` | Basic user message | 88 |
| `user command-output` | Slash command output | 19 |
| `user compacted` | Compacted user conversation | (rare) |
| `user slash-command` | Slash command invocation | 20 |
| `user steering` | Out-of-band steering input | (rare) |

### ⚠️ Partial Support (7 combinations)

These combinations inherit from parent selectors but have no dedicated rules:

| Combination | Description | Inherits From |
|-------------|-------------|---------------|
| `assistant  pair_last` | Assistant (last in pair) | `.assistant`, `.` |
| `tool_result error sidechain` | Sub-assistant tool error | `.tool_result`, `.error`, `.sidechain` |
| `unknown sidechain` | Unknown sidechain type | `.sidechain` |
| `user compacted sidechain` | Compacted sidechain user | `.user`, `.compacted`, `.sidechain` |
| `user sidechain` | Sub-assistant user prompt (deprecated) | `.user`, `.sidechain` |
| `user slash-command sidechain` | Sidechain slash command | `.user`, `.slash-command`, `.sidechain` |
| `user command-output pair_last` | Command output in pair | `.user`, `.command-output` |

### ❌ No Support (1 combination)

| Combination | Description | Note |
|-------------|-------------|------|
| `unknown` | Unknown message type | Fallback type - should rarely appear |

---

## Fold-Bar Support

The fold-bar component uses `data-border-color` attribute to style borders based on message types. Below shows which combinations have dedicated fold-bar styling.

### Has Fold-Bar Styling (27 combinations)

- `assistant`
- `assistant sidechain`
- `bash-input`
- `bash-output`
- `image`
- `image sidechain`
- `session-header`
- `system`
- `system-away-summary`
- `system-error`
- `system-info`
- `system-warning`
- `thinking`
- `thinking sidechain`
- `tool_result`
- `tool_result error`
- `tool_result error sidechain`
- `tool_result sidechain`
- `tool_use`
- `tool_use sidechain`
- `unknown`
- `unknown sidechain`
- `user`
- `user command-output`
- `user compacted`
- `user compacted sidechain`
- `user sidechain`
- `user slash-command`
- `user slash-command sidechain`

### Missing Fold-Bar Styling (5 combinations)

These combinations appear in HTML but lack dedicated fold-bar border colors:

- `assistant ` (uses base `assistant` color)
- `assistant  pair_last` (uses base `assistant` color)
- `system system-hook` (uses base `system` color)
- `thinking  pair_first` (uses base `thinking` color)
- `user steering` (uses base `user` color)

---

## Detailed Breakdown by Base Type

### `assistant` (596 occurrences, 3 variations)
- 419× `assistant` (standalone)
- 104× `assistant pair_last `
- 73× `assistant sidechain`

### `bash-input` (5 occurrences, 1 variation)
- 5× `bash-input pair_first `

### `bash-output` (5 occurrences, 1 variation)
- 5× `bash-output pair_last `

### `system` (138 occurrences, 3 variations)
- 59× `system pair_first  system-info`
- 59× `system pair_last  system-info`
- 20× `system pair_first `

### `thinking` (303 occurrences, 2 variations)
- 199× `thinking` (standalone)
- 104× `thinking pair_first `

### `tool_result` (1,030 occurrences, 4 variations)
- 863× `tool_result pair_last `
- 83× `tool_result error pair_last `
- 83× `tool_result pair_last  sidechain`
- 1× `tool_result error pair_last  sidechain`

### `tool_use` (1,030 occurrences, 2 variations)
- 946× `tool_use pair_first `
- 84× `tool_use pair_first  sidechain`

### `user` (128 occurrences, 4 variations)
- 88× `user` (standalone)
- 20× `user pair_first  slash-command`
- 19× `user command-output pair_last `
- 1× `user pair_last  slash-command` (unpaired)

---

## Key Observations

1. **Pairing Consistency**: Tools (`tool_use` + `tool_result`) and bash commands (`bash-input` + `bash-output`) always appear as pairs, with `pair_first` on the input/use side and `pair_last` on the output/result side.

2. **Thinking-Assistant Pattern**: `thinking` messages that are paired are always `pair_first`, paired with an `assistant` message that is `pair_last`.

3. **Sidechains**: The `sidechain` modifier appears on:
   - `assistant` messages (73 occurrences)
   - `tool_use` and `tool_result` pairs (84 and 84 occurrences respectively)

4. **Error Handling**: The `error` modifier only appears on `tool_result` messages (84 total error results).

5. **System Messages**: Have 3 variations:
   - System info pairs (118 total, always paired)
   - Generic system pairs (20, `pair_first`)

6. **Slash Commands**: User messages with `slash-command` and `command-output` pair together:
   - `user slash-command` (20 occurrences, `pair_first`)
   - `user command-output` (19 occurrences, `pair_last`)

7. **Rare Cases**:
   - `tool_result` with both `error` and `sidechain` (1 occurrence)
   - `bash-input`/`bash-output` pairs (5 pairs total)

---

## Structural Classes (Not Semantic)

In addition to the semantic classes above, messages include structural classes:

- **Session IDs**: `session-{uuid}` - identifies which session a message belongs to
- **Ancestry Markers**: `d-{number}` - indicates descendant depth in the message tree

These are excluded from semantic analysis but appear in all HTML output.

---

## CSS Selector Statistics

- **Total CSS selectors in templates**: 495
- **Message-related selectors**: 78
- **Fold-bar combinations**: 28
- **Full support combinations**: 25
- **Partial support combinations**: 7
- **No support combinations**: 1

---

## References

- Source: [css_class_combinations_summary.md](/tmp/css_class_combinations_summary.md)
- Source: [css_rules_analysis.md](/tmp/css_rules_analysis.md)
- CSS templates: [claude_code_log/templates/](../claude_code_log/templates/)
- Messages documentation: [messages.md](messages.md)

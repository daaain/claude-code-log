# Themes

HTML transcripts come in two looks:

- **`classic`** — the original look: cards, a gradient background, a stack
  of floating buttons. It is the default.
- **`minimal`** — a compact light/dark theme: messages as dense rows on a
  time rail, long content folded to short previews, and sub-agents and
  rewind forks drawn as **branches** beside the main conversation, which
  you can fold away, weave into the main line, or open side by side in
  columns.

<figure markdown>
  ![The minimal theme in "Main only" mode: a prompt, the assistant's reply and two background agents folded to one line each, with dashed lanes on the rail](assets/themes/main-light.png)
  <figcaption>The minimal theme, <em>Main only</em>: each sub-agent is folded to a line on the row that spawned it, and a dashed lane on the rail runs to where its result arrives.</figcaption>
</figure>

The [example output](example.md) shows a real page in both themes.

## Choosing a theme

Pass `--theme`, or set `CLAUDE_CODE_LOG_THEME`:

```bash
claude-code-log --theme minimal                 # the whole archive
claude-code-log --theme minimal path/to/project # one project
claude-code-log serve --theme minimal           # the local server (and serve --watch)
claude-code-log watch --theme minimal           # watch mode

# The same through the environment — also picked up by the TUI's HTML exports
export CLAUDE_CODE_LOG_THEME=minimal
claude-code-log
claude-code-log --theme classic                 # an explicit flag always wins
```

The accepted values are:

| Value | Meaning |
|---|---|
| `classic` | the original look |
| `minimal` | the compact light/dark theme described below |
| `default` | the built-in default — currently `classic`. Use it to follow whatever the default becomes rather than to pin a look |

Precedence is `--theme`, then `CLAUDE_CODE_LOG_THEME`, then the built-in
default. `--theme default` is an explicit choice too, so it overrides the
environment. An unknown value in `CLAUDE_CODE_LOG_THEME` is an error that
lists the valid ones, never a silent fallback.

Themes apply to HTML only: with `--format md` (or `json`) an explicit
`--theme` is ignored with a warning.

### Switching theme rewrites the archive

Both themes write the **same files** — `combined_transcripts.html`, its
`_2`, `_3` … pages, `session-*.html` — rather than separate copies, and
every page records the theme it was rendered in. So the first run after a
switch regenerates every page in the new theme, and switching back
regenerates them again; an archive is never left half in one theme and half
in the other. Later runs are incremental as usual.

The project index (`index.html`) and the archive search page
(`search.html`) look the same in both themes.

## The minimal theme

### Reading a page

Each message is one row: a narrow **gutter** on the left with its time,
role (`User`, `Assistant`, the tool name …) and tokens, a coloured **dot**
on a continuous vertical **rail**, then the content. A tool call and its
output read as one unit; a failed result shows an `error` pill in the
gutter. User prompts are tinted and each one starts a new turn, separated
by a thin rule.

Long content shows a **two- or three-line preview** that fades out, with a
`+ N lines` label: a long tool output, file, diff, Bash script, sub-agent
prompt or sub-agent answer. Click the preview (or the label) to open it
and `− less` to close it again; long blocks repeat the `− less` at their
foot.

### The toolbar

The toolbar sticks to the top of the page:

| Control | What it does |
|---|---|
| **Prompts · Steps · All** | Fold depth. *Prompts*: every turn folded to its prompt. *Steps* (default): every turn open, long blocks as previews, sub-agents folded. *All*: everything open, every block in full. Folding a card by hand (its fold bar) clears the selection; choosing a depth again re-applies it everywhere |
| **Main only · Interleaved · Columns** | How every branch is shown (below). Only there when the page has branches |
| 🔍 | Search and filter (also `/`) — the same search and message-type filters as the classic page |
| 📆 | The timeline |
| ⏬ | Follow new messages — only on a page served live (see [Watching a session as it runs](live-updates.md)) |
| ▶ | Copy the command that resumes the session in Claude Code |
| Auto · Light · Dark | Colour scheme. *Auto* follows your system |
| ⋯ | Open or close all details, show user prompts as raw text, show message ids |

The page remembers the fold depth, the branch mode and the colour scheme
in your browser's local storage. Browsers keep that per origin: every page
opened from disk shares one set of choices, and the pages a
`claude-code-log serve` serves share another. If storage is blocked the
page still works, with the defaults.

### Branches

A **branch** is a sub-agent's transcript — synchronous, background
(`run_in_background`) or nested inside another agent, to any depth — or a
**rewind fork** (a conversation that was rewound and continued
differently; the earliest continuation stays the main line). Each branch
has a control on the row that spawned it — its step count, tokens and
duration — and can be shown in one of four ways:

- **Folded** — *Main only*, the default. Just the control; a dashed lane
  on the rail runs from the spawn to the row where the result came back
  (for a background agent, where its notification arrived). The answer
  itself is on that row, as a preview.
- **Interleaved** — click the control's chevron. The branch's messages
  join the main line at the times they happened, indented, tinted in the
  branch's colour and tagged in the gutter, on a lane of their own that
  forks from the spawn and merges back at the result. At most three
  branches of one turn are interleaved at once: a fourth folds the one you
  opened least recently.
- **In a column** — *Column ⇥* on the control. The branch moves into a
  column of its own to the right, its rows still aligned in time with the
  main line, so you can read both side by side; the page widens and
  scrolls sideways. The column's head names the branch and gives its
  stats; *⇤* puts it back into the main line and *−* collapses it to a
  narrow strip (click the strip to open it again).
- **A strip** — a collapsed column.

The toolbar's **Branches** buttons set every branch at once: *Main only*
folds them all, *Interleaved* interleaves the first three of each turn, and
*Columns* opens every branch in a column. A turn with more than three
branches shows the first three controls and a `+N more branches` toggle for
the rest.

<figure markdown>
  ![The same page in "Interleaved" mode: two agents' steps merged into the main line by time, each on a coloured lane](assets/themes/interleaved-light.png)
  <figcaption><em>Interleaved</em>: the agents' steps woven into the main line at their real times, each on a lane of its own colour.</figcaption>
</figure>

<figure markdown>
  ![The same page in "Columns" mode: the main session on the left and one column per branch](assets/themes/columns-light.png)
  <figcaption><em>Columns</em>: one column per branch beside the main session, rows aligned in time.</figcaption>
</figure>

Search, the timeline and links find their way into branches: a search hit,
a `#msg-…` link or a timeline click inside a folded branch opens it (as a
column if you are in *Columns*, interleaved otherwise) before scrolling to
it. The filter hides the same messages in the transcript and the timeline
whichever way the branches are shown.

**Teammates** (agent teams) are not branches: a teammate's thread stays
folded under the message that spawned it, and the spawn, each
`SendMessage` and each delivered `<teammate-message>` link to one another.
Workflow agents stay nested too.

On a phone, *Columns* shows one column at a time, the width of the screen:
swipe sideways from one to the next.

### Running agents

On a page served live (`claude-code-log serve --watch`), a sub-agent that
has not returned yet is drawn as **running**: its lane carries on to the
newest message and ends in an open circle, and its control shows a pulsing
*running* label — *running · quiet 42m* once it has been silent for a
minute. When its result arrives it becomes an ordinary merge. Lanes you
opened, interleaved or put in a column stay that way as the page updates,
and a filter or search you have set applies to the new messages too. See
[Watching a session as it runs](live-updates.md).

A page opened from disk never says *running*: an agent without a result
there reads as *no result*.

<figure markdown>
  ![A live page with one synchronous agent interleaved and running, and a background agent folded and running](assets/themes/running-dark.png)
  <figcaption>Dark scheme, served live: a running synchronous agent (interleaved, open end) beside a running background agent (folded, dashed).</figcaption>
</figure>

### Dark mode

The theme follows your system's light or dark setting until you pick
*Light* or *Dark* in the toolbar; *Auto* goes back to following the system.
Everything follows the choice — code highlighting, diffs, the timeline,
teammate colours.

<figure markdown>
  ![The "Main only" page in the dark scheme](assets/themes/main-dark.png)
  <figcaption>The same page in the dark scheme.</figcaption>
</figure>

### Offline, and without JavaScript

The theme uses your system's fonts and loads nothing from the network. The
one exception is the timeline, which — as in the classic theme — fetches
its charting library from unpkg the first time you open it.

Without JavaScript the page still reads correctly: branches are shown
nested under the message that spawned them, and long blocks keep their
previews (they open with a click; `<details>` needs no script). The
toolbar's controls need JavaScript.

### Not themed (yet)

- The project index and the archive search page keep the classic look.
- Markdown and JSON output have no themes.
- Workflow agents are shown nested, not as branches.

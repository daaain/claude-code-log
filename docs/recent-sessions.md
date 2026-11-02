# Recent sessions across all projects

`recent.html`, next to the index, lists the sessions that changed lately
across every project, newest update first. Follow **Recent sessions** on
the index page, or open it directly:

```bash
claude-code-log --open-browser   # writes the pages, opens index.html
open ~/.claude/projects/recent.html
```

It is written on every full run, in both themes, and works opened as a
file: no server is involved.

## What a row shows

- **How long ago it last changed, and whose move it is.** The block at the
  start of the row says *asked you* (the last reply ends on a question),
  *error* (an API error was the last entry), *finished* (the turn ended
  with nothing asked) or *mid-turn* (the last entry is a tool call or
  result, or a prompt with no reply yet).
- **The title, project, worktree and branch**, with a link to the session's
  own page.
- **A strip of the last six hours and the next one.** Each turn is a bar
  from your prompt to the agent's last entry, with its tool calls as thin
  ticks and a marker for how it ended. Where markers would crowd, the
  earlier ones shrink to dots; hover one for its time. Turns from before
  the strip's window are counted at its left edge.
- **The cache countdown.** The prompt cache lasts an hour from the last
  update, drawn as a pale band. From 45 minutes on, the row says how many
  minutes are left: reply soon and the next turn still reads the cached
  context.

Press the block to open the row: the turn, tool-call and message counts on
the left, and your last prompt and the agent's last message in full on the
right.

The numbers at the top filter the list (*your move* is everything that
asked you, errored or finished); *by project* groups the rows.

!!! note "From the transcripts only"
    A transcript cannot tell an agent that stopped from one waiting
    quietly, so these states are about whose move it is, not about whether
    anything is still running. A *mid-turn* session may have died; one
    that *finished* may still be open in a terminal.

## Keeping it current

The page refreshes itself. A page opened as a file can't fetch anything,
but it can load a script, so the data lives in `recent-data.js` beside
it, and the page reloads that file every 15 seconds and redraws its rows
without losing what you have open. Times and the cache countdown keep
moving between reloads.

To have something to reload, keep the data file current:

```bash
claude-code-log watch                 # the project you're in; also refreshes recent-data.js
claude-code-log watch --all-projects  # the whole archive
```

If the data stops changing, the page says how old it is.

The watch refreshes `recent-data.js` when the watched project sits in the
default projects directory and no `--output` is given; with a custom
output tree, a full run (or `watch --all-projects`) keeps it current.

## With foldyard

When agents run in [foldyard](https://github.com/twistco/foldyard) boxes,
the supervisor copies their transcripts into `~/.claude/projects` every
`[claude] transcript_sync_seconds`. A `claude-code-log watch` on top of that
keeps this page current, and the id each row copies is the handle
`fy agent attach` and `claude --resume` take. Open the page as a file
rather than through `claude-code-log serve`: foldyard's
[agents guide](https://twistco.github.io/foldyard/agents/) explains why.

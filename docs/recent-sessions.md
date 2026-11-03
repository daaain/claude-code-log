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
  start of the row says *asked you* (the last reply ends on a question,
  or Claude is waiting on an answer to its questions or on your approval
  of its plan), *error* (an API error was the last entry), *finished* (the
  turn ended with nothing asked), *background* (it finished, but work it
  started in the background is still running or a wake-up is due) or
  *mid-turn* (the last entry is a tool call or result, or a prompt with
  no reply yet).
- **The title, project, worktree and branch**, with a link to the session's
  own page (none with `--combined only`, which writes no session pages).
- **A strip of the last four hours and the next one.** Each turn is a bar
  from your prompt to the agent's last entry, with its tool calls as thin
  ticks and a marker for how it ended. Where markers would crowd, the
  earlier ones shrink to dots. Hover a marker for its time and the start
  of that turn's prompt, or of how it ended. Turns from before the
  strip's window are counted at its left edge. A turn Claude started by
  itself (background work reporting back, or a wake-up firing) begins
  with a bell or an alarm clock instead of the prompt marker.
- **Background work.** Background commands, agents and monitors are thin
  lines under the turns, from launch to their end: a tick when they
  completed, a cross when they failed, a bar when they were stopped, a
  hollow bar when they ended with the session (you quit Claude Code while
  they ran), and an arrow at *now* while they are still running. Work
  that never reported its end is drawn dashed. Lines that don't overlap share a lane;
  past three lanes, the rest fold into the third as *+N more*. A pending
  wake-up is an alarm clock at the time it is due, often in the hour
  ahead.
- **The cache countdown.** The prompt cache lasts an hour from the last
  update, drawn as a pale band. From 45 minutes on, the row says how many
  minutes are left: reply soon and the next turn still reads the cached
  context.

Press the block (or the chevron beside it) to open the row: the turn,
tool-call and message counts on the left, and your last prompt and the
agent's last message on the right, rendered as Markdown. When Claude is
waiting on you, the last message is what it is waiting on: its questions
with their options, or the plan it wants approved. Below it, a
*Background* list names every piece of background work with its times
and how it ended, running ones first.

The numbers at the top filter the list (*your move* is everything that
asked you, errored or finished); *by project* groups the rows.

!!! note "From the transcripts only"
    A transcript cannot tell an agent that stopped from one waiting
    quietly, so these states are about whose move it is, not about whether
    anything is still running. A *mid-turn* session may have died; one
    that *finished* may still be open in a terminal. The same goes for
    background work: work with no end recorded counts as running for an
    hour after the session's last entry (a monitor stops sooner if its
    timeout says so), and after that it is shown as *no end recorded*.
    The exception is quitting: when Claude Code exits, it leaves one last
    line in the transcript, so a session you quit is never *mid-turn* or
    *waiting*, and the work it left running *ended with the session*.

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

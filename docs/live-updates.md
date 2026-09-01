# Watching a session as it runs

Two commands keep generated output current while Claude Code is still
writing to a transcript.

## Markdown (or HTML) on disk

```bash
claude-code-log watch
```

With no arguments this watches the project for the current directory —
which is what you want when running it alongside a live session. It
converts once up front, then re-converts whenever a transcript changes.
Anything that reloads files picks the changes up: an editor, Obsidian, a
browser refresh.

For an Obsidian vault:

```bash
claude-code-log watch -f md -o ~/vault/claude
```

A tick regenerates only the session that changed, so a vault indexer sees
one file move, not the whole projection.

Obsidian auto-updates, but doesn't auto-scroll, so expect something like this:

<video controls style="display: block; width: 100%; height: auto; max-width: 100%;" src="https://github.com/user-attachments/assets/36dd429a-03b3-4bc0-9449-adf55fff3b30"></video>


## The served page, updating itself

```bash
claude-code-log serve --watch
```

Open a session page and it grows as messages arrive — roughly a second
behind the CLI, without a reload. Your scroll position, folded sections
and open disclosures all survive, and new messages fade in.

A **follow** button (⏬) joins the buttons down the right-hand edge once
the page is being served. Click it to keep the page pinned to the newest
message; while you are not following, it shows a count of how many have
arrived since you last looked.

It is the *session* pages that track a live session. Watch ticks leave
the combined pages alone, for the same reason `watch` defaults to
`--combined no`: regenerating them is what forces a tick to reload the
whole project rather than just the session that grew. The combined pages
written at startup stay on disk and keep serving — they simply stop
tracking the live session until you restart.

This works only over the server. A page opened from `file://` cannot
fetch anything at all — not even itself — so it stays static, exactly as
before. Nothing about the generated HTML changes.

This is how following a session looks like:

<video controls style="display: block; width: 100%; height: auto; max-width: 100%;" src="https://github.com/user-attachments/assets/600696aa-dd2b-4979-8cc9-2f62acfc996f"></video>

## What "real-time" can and cannot mean here

**It cannot show tokens arriving.** Claude Code writes a transcript entry
exactly once, when the message is complete — it never rewrites a line, so
there is no partial message on disk to display. The finest granularity
available anywhere in this tool is *one whole message*, appearing
promptly after it finishes. The fade-in is there to make that arrival
legible, not to imitate streaming.

Everything else follows from that. A message typically appears within a
second or so of completing: the watcher waits briefly for the burst of
entries in a turn to settle (a turn writes several), converts, and the
page notices on its next poll.

## Tuning

| Flag | Default | What it does |
|---|---|---|
| `--interval` | `0.25` | Seconds between filesystem polls |
| `--quiet-period` | `0.3` | Wait for changes to settle before converting |
| `--max-latency` | `2.0` | Convert anyway after this long, so a long unbroken stream still surfaces |
| `--all-projects` | off | Watch the whole archive instead of one project |
| `--combined` | `no` | `no` keeps ticks cheap: only the changed session is regenerated |

Raise `--quiet-period` if conversions feel too frequent on a large
project; lower `--interval` if you want changes noticed sooner.

## Cost

A tick re-converts only what changed. On a 319 MB, 217-file archive a
tick is about a second; on a small project it is hundredths of a second.
While nothing is changing, the browser's poll is a `HEAD` for the page's
own metadata — about a millisecond, and no body — and the watcher is a
`stat` of the project directory. The page is re-fetched in full only
once that metadata says it moved.

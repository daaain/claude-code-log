# Viewing a date window

`--from-date` and `--to-date` turn a run into a view of that window:
its index, combined pages and session pages, and every link they show,
come from the window.

```bash
# Yesterday's work across all projects, in a directory of its own
claude-code-log --from-date "yesterday" --to-date "yesterday" -o ~/yesterday

# One project's last week
claude-code-log /path/to/project --from-date "last week" -o ~/last-week
```

Both bounds take natural language (`"today"`, `"3 days ago"`,
`"2026-01-02 15:00"`); either may be left out.

## What the view shows

- **The index** lists only the projects with a session in the window, and
  each project card counts the window: its messages, its tokens, the
  time between its first and last message. Each session it lists is
  shown with its usual title and the window's part of it.
- **A project's combined page** holds the window's messages.
- **A session page** is written for each session with a message in the
  window, and only those. A session that only crosses the window, with
  messages before and after it but none inside, is not part of it.
- **The index, the combined pages and the session pages state their
  window** in their title, e.g. *(from 2026-01-02 to 2026-01-02)*, so they
  are never taken for the whole archive.
- **The search page and the recent sessions page** are written as on any
  run and do not state the window. Search covers the whole archive (it
  needs `claude-code-log serve`), and the recent sessions page lists the
  sessions active in the last three days, in the window or not, linking
  only the session pages the view wrote.

Archived projects, whose transcripts are gone, are not part of a view:
there is no source left to take a window of.

## Where the view goes

A date-filtered run writes into a destination that holds none of the
pages it is about to write. Other files in the directory do not count,
nor do the vendored `assets/` and referenced images, which are the same
bytes in any view.

If one of its pages is already there, the run writes nothing and exits
with an error naming the first such file. This is what happens when you
re-run a date filter in place, over the pages of an earlier run:

```text
Error: /home/me/.claude/projects/index.html already exists. A date-filtered run
writes only into a destination that holds none of the files it writes:
pass --force to overwrite them (nothing is removed), or choose another
--output.
```

You then have two ways out:

- **`-o` / `--output` another directory.** This is the usual way: the
  full view stays where it is, and the window's view goes next to it.
- **`--force`** writes the window's view over the existing pages. It
  removes nothing: pages of the full view that the window does not
  rewrite (session pages outside the window, other projects' pages) stay
  where they are, no longer linked from the new index. `--force` is only
  accepted together with a date window. To clear the others out, mark the
  time before the run and delete the pages it did not rewrite (check the
  list without `-delete` first):

    ```bash
    touch /tmp/before-view
    claude-code-log --from-date "yesterday" --force
    find ~/.claude/projects -name '*.html' ! -newer /tmp/before-view -delete
    ```

## The render cache

The cache records which pages are current, by project and file name. A
view written in place, with `--force`, marks the pages it replaced, so
the next run without a date filter rewrites them in full. A view written
anywhere else leaves those records alone: the full view's pages, which
it did not touch, stay current.

The transcripts themselves are cached as usual. A date-filtered run
refreshes the cache exactly as a run without a filter would, so the next
plain run finds it up to date.

!!! note
    Sessions from other providers (`--provider codex`) are not covered yet:
    their date-filtered run still writes over the full view, and takes no
    `--force`.

# Searching the whole archive

The search box baked into every generated page only sees the messages on
that page. To search *everything* — every message, every session, every
project — start the local server:

```bash
claude-code-log serve
```

Then open <http://127.0.0.1:8010/search.html>, or follow the
**“Search inside all transcripts…”** link on the index page or on any
project's combined transcript.

## Why it needs a server

Archive search reads the SQLite cache, and a page opened from `file://`
cannot reach a database. The generated HTML stays canonical and keeps
working without the server — this only adds an origin so the page can ask
for search results.

Everything else is unchanged: the same pages, the same styling, the same
in-page search on `file://`.

## Two searches, on purpose

| | Where | What it searches |
|---|---|---|
| **In-page search** (<kbd>/</kbd>) | Any session page | The messages on that page — substring, optional regex |
| **Index page search** | `index.html` | Session titles and first-message previews — a conversation finder |
| **Archive search** | `search.html` | Every message body in every project — full text |

They are deliberately separate. On a real archive the index page's box
covers about 0.17% of the text — it is there to answer "which conversation
was that?", not "where did I ever discuss this?".

## Query syntax

Archive search is token-based (SQLite FTS5), not substring:

| Query | Meaning |
|---|---|
| `pydantic` | words starting with `pydantic` |
| `tokeni` | words starting with `tokeni` — finds `tokenizer` |
| `"cache"` | the whole word `cache`, never `cached` |
| `cache invalidation` | both words, anywhere in the message |
| `"cache invalidation"` | that exact phrase |

A word you type is matched as a prefix, so a half-typed one still finds
what you meant; quote it to match it whole. Punctuation is handled for you
— `vis-timeline` searches for the two words together, and no input can
produce a syntax error.

!!! note
    Words of one or two characters are matched whole even unquoted. A
    two-letter prefix matches a third of the archive, which is both slow
    (hundreds of milliseconds, on every keystroke on the way to a longer
    word) and useless as a result list.

!!! note
    Because it is token-based, a mid-word substring like `ydant` finds
    nothing — matching starts at the beginning of a word. Use the in-page
    search on a session page for substring or regex matching.

## Filters

**Project** restricts the search to one project. Following a link from a
project's combined transcript pre-selects that project; you can always
widen it back to all projects.

**Search in** picks which parts of a message are searched. Every group has
its own toggle:

| Toggle | What it covers | On by default |
|---|---|---|
| **Text** | What you and Claude wrote to each other | ✅ |
| **Thinking** | Claude's extended-thinking blocks | ✅ |
| **Tool input** | Tool names and the arguments they were called with | ✅ |
| **Tool results** | What tools returned — file dumps, tracebacks | ❌ |
| **Attachments** | Pasted and attached content | ✅ |
| **Meta** | Session summaries, generated titles, system messages | ✅ |

Tool results are off by default because they are roughly 69% of a typical
archive's text and mostly file dumps and tracebacks, so they drown out
prose unless you actually want them. Turning the others off is the way to
go the other direction: **Text** alone finds what was *said* about
something, without every command that mentioned it.

A **reset** button appears as soon as the selection differs from the
server's default, and switching everything off says so rather than
reporting no matches.

The query and every filter live in the URL, so results are shareable and
the Back button works. The `fields` parameter takes the same spec as
`--search-fields` below, so `?fields=text` and `?fields=+tool_result` both
set the toggles.

!!! note
    A group that `--index-fields` left out of the index can't be searched
    whatever the toggle says, so its checkbox is disabled and the page
    notes it as *not indexed*.

## Jumping to a result

Result links land on the exact message, scroll it into view, and highlight
the term — including when the match is inside a collapsed tool-result
block, which the page opens for you.

A few message types (progress markers, generated titles, queued commands)
have no card of their own; those results link to the session instead.

## Configuring what gets searched

By default everything is *indexed* but tool results are excluded from
*searching*, so you can turn them on per-search without a rebuild.

`--search-fields` sets where the page's toggles *start*; you can still
change them per search afterwards.

```bash
# Change the default search scope
claude-code-log serve --search-fields text,thinking
claude-code-log serve --search-fields +tool_result     # add to the default
claude-code-log serve --search-fields -thinking        # remove from it
claude-code-log serve --search-fields all
claude-code-log serve --search-fields none             # every toggle off

# Or via the environment
CLAUDE_CODE_LOG_SEARCH_FIELDS=text,thinking claude-code-log serve
```

The field groups are `text`, `thinking`, `tool_input`, `tool_result`,
`attachment` and `meta`. A spec is either an absolute list, `all`/`none`,
or `+`/`-` deltas against the default — mixing an absolute name with a
delta is an error rather than a guess.

To shrink the index itself — at the cost of not being able to search those
groups at all — narrow `--index-fields` (or
`CLAUDE_CODE_LOG_INDEX_FIELDS`). Dropping `tool_result` took a real
253 MB index down to 94 MB. Changing this forces a rebuild.

## The index

The first `serve` builds a full-text index inside the existing cache
database. On a 532,000-message archive that takes about 50 seconds, once,
with a progress bar; afterwards each start only re-reads transcripts that
changed, which is milliseconds.

It adds roughly **20%** to the cache file (263 MB on a 1.29 GB cache).

```bash
claude-code-log serve --reindex     # rebuild from scratch
claude-code-log serve --no-index    # start without touching the index
```

The index is derived data: deleting the cache rebuilds both.

### Staying current between server runs

Once an index exists, ordinary `claude-code-log` runs keep it up to date —
you don't have to restart the server to search new conversations.

That costs about 25% extra on a conversion in which *every* transcript
changed (3.3 s → 4.1 s over 7,516 messages); a normal run touches a handful
of files and the difference isn't noticeable. Nothing is added for people
who have never built an index.

To opt out and let the next `serve` catch up instead:

```bash
CLAUDE_CODE_LOG_SEARCH_AUTO_INDEX=0 claude-code-log
```

## Other options

```bash
claude-code-log serve --port 9000
claude-code-log serve --open-browser
claude-code-log serve --no-convert     # skip the startup conversion
claude-code-log serve --projects-dir /path/to/projects
```

The server binds to `127.0.0.1` only, and rejects requests that don't
address it as loopback — a page on the open web can otherwise point a
hostname at your machine and read the archive through your browser.

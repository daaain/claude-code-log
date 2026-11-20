# What fills the HTML after the AVIF export

**Status:** findings (issue #418). No rendering change made; the
candidates below are for a decision.

Measured on the real archive from #416 (`.ccl-data/projects`, 6.2GB of
transcripts, 127 projects), rendered by `feat/image-followups` in the
classic theme with `--all-projects` defaults: **2,636 pages, 5.37GB of
HTML** (plus 0.24GB of AVIF). The script attributes each page's bytes to
the page chrome before the first card and after the last, and to each
message card up to the next one; a tool result is counted under the tool
of the call before it.

## Where the bytes go

| | GB | Share |
|---|---|---|
| Session pages | 2.96 | 55% |
| Combined pages | 2.40 | 45% |
| Page chrome (inline `<style>` 0.31, `<script>` 0.45) | 0.78 | 15% |
| Tool results | 2.84 | 53% |
| — of which Read | 1.80 | 34% |
| — of which Bash | 0.65 | 12% |
| — of which Edit | 0.19 | 4% |
| Tool calls (Bash 0.31, Write 0.27, Edit 0.22) | 0.99 | 18% |
| Thinking | 0.36 | 7% |
| Assistant text | 0.28 | 5% |
| User text | 0.10 | 2% |

Every message is written twice, once in its project's combined page and
once in its session page, so roughly 2.4GB of the total is that second
copy.

## Read results are mostly highlighting markup

A Read result is the file's content, Pygments-highlighted in a table
with line numbers, after a highlighted 5-line preview. Of its 1.80GB:

| | GB |
|---|---|
| Token spans: `<span class="…">` | 0.91 |
| Their `</span>` | 0.38 |
| — of which line numbers (`<span class="normal">  12</span>` per line) | 0.28 |
| The previews | 0.08 |

Spans are 72% of the Read results' bytes; a sample of 6,645 Read results
held 145MB of HTML for 40MB of visible text.

## Candidates, largest first

1. **Shared CSS and JS.** Each page inlines ~300KB of style and script
   (0.78GB in all). Writing them once beside the pages, as `assets/`
   already holds vis-timeline, takes nearly all of that; a page then needs
   `assets/` for its styles too, which it already needs for its timeline.
   Snapshot tests and the "self-contained page" expectations (stdout
   streaming, `embedded` mode) would need an inline fallback.
2. **Plainer large code blocks.** Highlighting is most of a Read result.
   Highlighting only the first N lines (or none past N, as a file viewer
   does) and writing the rest as escaped text would cut most of the
   1.29GB of spans for a visible loss on long files only.
3. **Line numbers without spans.** Pygments' table line numbers wrap
   each number in `<span class="normal">`; one plain `<pre>` of numbers
   (or CSS counters) looks the same and saves ~0.28GB.
4. **One copy per message.** `--combined no` as the `--all-projects`
   default would drop 2.40GB, but the combined pages are a feature
   people browse; this is a product call, not a trim.

Bash results (0.65GB) are mostly plain output already, ANSI-coloured
spans aside, so they offer little beyond the generic options above.

## Reproducing

```bash
CLAUDE_CODE_LOG_CACHE_PATH=/tmp/c.db uv run claude-code-log \
    .ccl-data/projects --all-projects -o /tmp/out
```

then attribute the bytes per card: split each page at
`<div class='message `, take the first class as the type, and the
header's tool name (`<div class='header'>` → first `<span>`) for tool
calls; a result inherits the name of the call before it.

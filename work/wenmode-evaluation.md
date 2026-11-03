# wenmode as a mistune replacement — evaluation and migration (#323)

**Verdict, first pass (2026-09-10): not a byte-identical replacement,
and not a meaningful speed-up on our workload.** Measured against
wenmode 0.15.0 and mistune 3.3.0; sections *Question* through
*Integration surface* below are that evaluation, kept as the record.

**Then the bar changed.** Byte-identity was the right question for
"is this a drop-in", and the wrong one for "should we move": mistune's
own bugs are not worth reproducing, and the integration surface
measured ~85% smaller on wenmode. The migration was done; the section
*Migration: every rendering difference, classified* at the end is the
deliverable for its review.

**Then upstream fixed its side (2026-09-11).** The five wenmode bugs
below were reported on #323 and fixed in wenmode 0.15.1, which also
added two options that replaced local workarounds. With 0.15.1 no
rendering difference from mistune is a regression; see *wenmode 0.15.1*
at the end.

## Question

Can wenmode (mistune's author's reimplementation) replace mistune as a
faster drop-in, with byte-identical HTML on real transcripts?

## Method

1. Extract every markdown-rendered body (assistant/user text blocks)
   from a JSONL corpus, dedupe. Two corpora:
   - **fixtures**: `test/test_data/**/*.jsonl` — 957 unique bodies,
     450 KB.
   - **real**: one real project (this repository's own transcripts) —
     5936 unique bodies, 2.3 MB. Private; not in the repo.
2. Render each body through our exact mistune configuration
   (`strikethrough, footnotes, table, url, task_lists, def_list`,
   `escape=True`, `hard_wrap=True`) and through wenmode, and count
   bodies whose HTML differs byte for byte.
3. Time both with best-of-5 per body, interleaved (the box was
   shared; interleaving cancels load drift — a first naive run showed
   3.5x, the interleaved best-of-5 shows 1.55x).
4. Attribute the end-to-end share with
   `CLAUDE_CODE_LOG_DEBUG_TIMING=1 CLAUDE_CODE_LOG_RENDER_JOBS=1
   claude-code-log --no-cache <project copy>`.

Run with `uv run --with wenmode python <script>`; no lockfile change.

## Compatibility

| configuration | fixtures differ | real differ |
|---|---:|---:|
| wenmode `github()` preset, `HTMLRenderer(escape=True)` | 117 / 957 (12%) | 392 / 5936 (6.6%) |
| + hard-wrap `text` handler | 88 / 957 | 333 / 5936 |
| + full mistune-emulation layer (below) | 42 / 957 (4.4%) | 65 / 5936 (1.1%) |

What the emulation layer had to reproduce (each a renderer handler or
rule swap, ~100 lines):

- **hard_wrap** — wenmode has no option; a `text` handler turns `\n`
  into `<br />\n`.
- **tables** — mistune indents cells two spaces and uses
  `style="text-align:…"`; wenmode uses `align="…"`.
- **task lists** — mistune: `<li class="task-list-item"><input
  class="task-list-item-checkbox" type="checkbox" disabled/>`;
  wenmode: `<li><input disabled="" type="checkbox"> `.
- **list whitespace** — tight item with nested list: mistune
  `<li>a<ul>`, wenmode `<li>a\n<ul>`; loose: `<li><p>` vs `<li>\n<p>`.
- **raw HTML** — mistune in `escape=True` mode renders an HTML *block*
  as `<p>` + escaped text + `</p>` with no markdown inside and no
  `<br />`; wenmode escapes the block bare. Needs a context flag to
  tell block from inline `Html` nodes, and `HtmlBlock(disallowed_tags=())`
  / `RawHtml(disallowed_tags=())` to disable wenmode's GFM tag filter
  (which leaves `<script>` as `&lt;script>`, `>` unescaped).
- **entities** — mistune does not decode `&copy;` (renders
  `&amp;copy;`); wenmode decodes to `©`. Drop `CharacterReference`.
- **footnotes** — markup differs entirely (`fn-1` vs
  `user-content-fn-1`, extra `<h2 class="sr-only">`); not emulated,
  rare in transcripts.

The 1.1% residue is **parser semantics**, not formatting:

- **single-tilde strikethrough** (21 of the 65 real residuals):
  wenmode's GFM `Strikethrough` accepts `~a~`; mistune needs `~~`.
  LLM prose says "~2, ~6, ~14 min" constantly, so every paragraph with
  two approximations would sprout `<del>`. Fixable with a custom rule.
- **wenmode bug**: with the `Table` rule active, a list line
  containing `|` cannot interrupt a paragraph — `a\n- b | c` renders
  as one paragraph (`commonmark()` and GitHub both render paragraph +
  list). Reproduced on `Wenmode([Table, *commonmark()])`. Common in
  transcripts (bullets quoting shell pipes or table-ish text).
- **mistune's `url` plugin** swallows trailing `**` into the link
  (`**https://x/pull/287**` → `href="…/287**"`); wenmode strips it.
  wenmode is right; byte-identity would mean reproducing the bug.
- **mistune + `hard_wrap`** renders backslash-newline as a literal
  `\<br />` (5 real bodies show a stray `\`); wenmode renders the
  CommonMark hard break. Same remark.
- **HTML-block boundaries** inside list items (lazy continuation vs
  block start) — edge cases where the two parsers split differently.

So "100% backward compatible" is reachable only by reproducing
mistune's parser quirks on top of wenmode, which defeats the purpose.

## Speed

Parser alone, best-of-5 per body, interleaved, load ≈ 5:

| target | real (2.3 MB) | fixtures (450 KB) |
|---|---:|---:|
| mistune, our plugin set | 835 ms (2.8 MB/s) | 208 ms |
| mistune, no plugins | 658 ms | 179 ms |
| wenmode `github()` | 544 ms (4.3 MB/s) | 121 ms |
| wenmode + emulation layer | 530 ms — **1.57x** | 120 ms — **1.74x** |

Consistent with wenmode's own published 1.2–1.6x over mistune.

End-to-end on the same project (serial, no cache, 22 files, 48 740
messages, 30 s wall), the parser is not where the time goes:

| component | time |
|---|---:|
| "Markdown rendering" bucket (includes the two below) | 18.0 s |
| ├ Pygments inside fences | 4.4 s |
| ├ **SHA-link resolver: git subprocesses** | **≈ 11.6 s** |
| └ mistune parse + render | ≈ 2 s |
| Content formatting total | 26.2 s |

The resolver figure: our renderer over the real corpus takes 2.3 s
with no repo context and 13.5 s cold with one — 549 unique SHA
candidates, each costing one `git branch -r --contains` (≈ 22 ms),
and one `git rev-parse --verify` (≈ 3 ms) for the 259 that are
reachable (`claude_code_log/git_remote.py`). Warm it is 2.3 s again.
That cost recurs per process (the `lru_cache` is in-process, so every
render worker pays it), and it scales with the number of distinct
SHAs a project mentions. The two instruments are the CLI timing run
over the project (22 files, 48 740 messages) and the extracted-bodies
corpus (5936 bodies) fed to the renderer singleton; the ≈ 11 s is the
difference between the corpus pair and matches the CLI bucket's
remainder.

A 1.55x parser would save ≈ 0.7 s of 30 s here (2%), less once the
61% markdown memo hit rate is counted. Replacing the two-subprocess-
per-SHA resolver with one `git rev-list --remotes` per cwd plus set
membership would save ≈ 11 s of the same 30 s.

## Other observations

- wenmode is beta (0.15.0, first release 2026-06); six of its last
  seven minor releases carry a **Breaking Changes** section. Our
  mistune baseline: 3.1.4 → 3.2.1 → 3.3.0 needed zero changes.
- wenmode's own docs list "preserving exact output compatibility with
  an existing integration" as a reason to stay on another parser.
- `positions=True` on every node would let `linkify_shas_in_text`
  (the hand-rolled round-trip tokenizer in `markdown_plugins.py`) be
  replaced by a parse-and-splice. That is the one *structural*
  argument for wenmode, and it belongs to the Markdown output path,
  which already uses mistune's own `MarkdownRenderer` for
  `_protect_html_tags`. Out of scope for #323 by the issue's own
  framing; noted for a future md→md discussion.
- The snapshot suite was not the instrument here: the corpus diff is
  a superset of what it would show, and no swap was committed.

## Integration surface: our extensions, rewritten for wenmode

A follow-up question: would the code where we integrate deeply be
simpler and more robust on wenmode? Measured by writing the four
integrations against wenmode 0.15.0 and running the project's own
linkifier test cases (`test/test_commit_linkifier.py`) plus new ones
against them — 45 pass — and by comparing behaviour on the real corpus.

| integration | today (mistune) | wenmode equivalent |
|---|---|---|
| bare-SHA links + codespan-SHA links | two inline rules, 129 lines | one post-parse AST transform, ~65 lines |
| Pygments on fenced code | renderer monkey-patch, 40 lines | one `code` handler, 13 lines |
| `_protect_html_tags` (md→md) | `MarkdownRenderer` subclass, 34 lines | one `html` handler, 4 lines |
| `linkify_shas_in_text` (Markdown output) | hand-rolled tokenizer, 238 lines | parse with `positions=True` + splice, 35 lines |

What disappears with the transform approach, each a documented
fragility in `markdown_plugins.py` today:

- **Combined-regex group renumbering** — mistune concatenates rule
  patterns, so `m.group(1)` and backreferences are unusable; the
  transform matches on parsed `Text` / `InlineCode` node values.
- **`before="codespan"` ordering** — load-bearing today; the transform
  runs after parsing, when code spans and links already exist.
- **`state.in_link` guard** (a `getattr` with a default) — replaced by
  ancestry: the walk knows it is under a `Link`.
- **Renderer monkey-patching** for Pygments — a handler registration.
- **The hand-rolled tokenizer's documented gaps** (tab-indented code,
  reference links, images with titles) — the real parser handles them;
  the splice edits only the byte ranges the parser reported, so the
  source stays byte-identical outside the links.

Equivalence on the real corpus (all-resolving resolver):

- HTML side: the transform links **exactly the same SHA multiset as the
  two mistune plugins in 5936 / 5936 bodies**.
- Markdown side: 5857 / 5936 bodies identical to `linkify_shas_in_text`.
  The 79 differ only inside raw HTML blocks (`<task-notification>`
  bodies), where the hand-rolled tokenizer links UUID fragments in file
  paths and the parser-based version, like the HTML output, does not.

One behavioural difference worth choosing deliberately: the AST sees
``` ``abc1234`` ``` as the same `InlineCode` node as `` `abc1234` ``, so
double-backtick SHAs get linked too (the mistune plugin is
single-backtick only). The splice version keeps the single-backtick
restriction because it checks the source bytes.

The transform is attached as a `RootTransform` on a trigger-only rule.
`RootTransform` is public API, exported from `wenmode.rules`; the probe
below first imported it from the private `wenmode._parser.transforms`,
which was a mistake of this evaluation, not a gap in wenmode. Root
transforms block wenmode's streaming mode, which we do not use.

### The code

```python
"""wenmode equivalents of our mistune integrations, for the #323 follow-up.

1. SHA linkification (bare + codespan) as ONE post-parse AST transform.
2. Pygments as a `code` renderer handler.
3. `_protect_html_tags` as MarkdownRenderer + one `html` handler.
4. `linkify_shas_in_text` as parse-with-positions + splice.
"""
from __future__ import annotations

import html as _html
import re
from typing import Callable, Optional

from wenmode import HTMLRenderer, Wenmode
from wenmode.nodes import Html, InlineCode, Link, Node, Parent, Position, Text
from wenmode.presets import github
from wenmode.renderers import MarkdownRenderer
from wenmode.renderers.html import render_code as default_render_code
from wenmode.rules import InlineRule, RootTransform

SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
Resolver = Callable[[str], Optional[str]]


# ---------------------------------------------------------------- 1. transform
def _link(url: str, child: Node, position: Position | None) -> Link:
    return Link(url=url, children=[child], position=position)


def linkify_tree(node: Parent, resolve: Resolver, in_link: bool = False) -> None:
    """Rewrite ``node.children`` in place: SHA-shaped text runs and
    SHA-only inline code become links, except under an existing link."""
    out: list[Node] = []
    for child in node.children:
        if isinstance(child, Link):
            linkify_tree(child, resolve, in_link=True)
            out.append(child)
        elif in_link:
            out.append(child)
        elif isinstance(child, InlineCode):
            url = resolve(child.value) if SHA_RE.fullmatch(child.value) else None
            out.append(_link(url, child, child.position) if url else child)
        elif isinstance(child, Text):
            out.extend(_split_text(child, resolve))
        else:
            if isinstance(child, Parent):
                linkify_tree(child, resolve, in_link)
            out.append(child)
    node.children = out


def _split_text(node: Text, resolve: Resolver) -> list[Node]:
    text, pos, parts, last = node.value, node.position, [], 0
    for m in SHA_RE.finditer(text):
        url = resolve(m.group(0))
        if url is None:
            continue
        if m.start() > last:
            parts.append(_text(text[last : m.start()], pos, last, m.start()))
        sub = _text(m.group(0), pos, m.start(), m.end())
        parts.append(_link(url, sub, sub.position))
        last = m.end()
    if not parts:
        return [node]
    if last < len(text):
        parts.append(_text(text[last:], pos, last, len(text)))
    return parts


def _text(value: str, base: Position | None, start: int, end: int) -> Text:
    position = Position(base.start + start, base.start + end) if base else None
    return Text(value=value, position=position)


class ShaLinks(InlineRule):
    """Trigger-only rule (no pattern, no opener) that carries the transform."""

    name = "sha_links"

    def __init__(self, resolve: Resolver) -> None:
        super().__init__()
        resolver = resolve

        class _T(RootTransform):
            name = "sha_links"

            def transform(self, parser, root, state):
                linkify_tree(root, resolver)

        self.root_transforms = [_T()]


# ---------------------------------------------------------------- 2. pygments
def pygments_code(renderer, node, ctx):
    if not node.lang:
        return default_render_code(renderer, node, ctx)
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import TextLexer, get_lexer_by_name
    from pygments.util import ClassNotFound

    try:
        lexer = get_lexer_by_name(node.lang.split()[0], stripall=False)
    except ClassNotFound:
        lexer = TextLexer()
    return str(highlight(node.value, lexer, HtmlFormatter(linenos=False, cssclass="highlight", wrapcode=True)))


# ---------------------------------------------------------------- 3. md→md
def protect_html_tags(text: str) -> str:
    wen = Wenmode(github(), renderer=MarkdownRenderer())
    wen.register_renderer_handlers({"markdown": {"html": lambda r, n, c: _html.escape(n.value)}})
    return wen.render(text).rstrip("\n")


# ---------------------------------------------------------------- 4. positions
def linkify_shas_in_text(text: str, resolve: Resolver) -> str:
    if not text:
        return text
    wen = Wenmode(github(), positions=True)
    root = wen.parse(text)
    edits: list[tuple[int, int, str]] = []

    def walk(node: Parent, in_link: bool) -> None:
        for child in node.children:
            if isinstance(child, Link):
                walk(child, True)
            elif in_link:
                continue
            elif isinstance(child, InlineCode) and child.position:
                if SHA_RE.fullmatch(child.value) and (url := resolve(child.value)):
                    s, e = child.position.start, child.position.end
                    if text[s:e] == f"`{child.value}`":  # single-backtick only
                        edits.append((s, e, f"[{text[s:e]}]({url})"))
            elif isinstance(child, Text) and child.position:
                base = child.position.start
                if text[base : child.position.end] != child.value:
                    continue  # value was normalised (entities, escapes): leave alone
                for m in SHA_RE.finditer(child.value):
                    if url := resolve(m.group(0)):
                        edits.append((base + m.start(), base + m.end(), f"[{m.group(0)}]({url})"))
            elif isinstance(child, Parent):
                walk(child, in_link)

    walk(root, False)
    out, last = [], 0
    for s, e, rep in sorted(edits):
        out.append(text[last:s]); out.append(rep); last = e
    out.append(text[last:])
    return "".join(out)


def build_html(resolve: Resolver) -> Wenmode:
    wen = Wenmode([*github(), ShaLinks(resolve)], renderer=HTMLRenderer(escape=True))
    return wen
```

## Emulation layer (for reproduction)

```python
"""wenmode configured to emulate mistune's HTML (escape=True, hard_wrap=True)."""
from wenmode import HTMLRenderer, Wenmode
from wenmode.presets import github
from wenmode.plugins import definition_list
from wenmode.nodes import Paragraph
from wenmode.renderers.html import SOFT_BREAK_SPACE_RE
from wenmode.rules import HtmlBlock, RawHtml

DROP = {"html_block", "raw_html", "character_reference"}

def rules():
    return [r for r in github() if r.name not in DROP] + [
        HtmlBlock(disallowed_tags=()), RawHtml(disallowed_tags=())]

def text(renderer, node, ctx):
    v = renderer.escape_html(SOFT_BREAK_SPACE_RE.sub("", node.value))
    return v.replace("\n", "<br />\n")

def _inline(renderer, node, ctx, open_, close):
    ctx.inline_depth = getattr(ctx, "inline_depth", 0) + 1
    try:
        return open_ + renderer.render_children(node.children, ctx) + close
    finally:
        ctx.inline_depth -= 1

def paragraph(renderer, node, ctx):
    return _inline(renderer, node, ctx, "<p>", "</p>\n")

def heading(renderer, node, ctx):
    return _inline(renderer, node, ctx, f"<h{node.depth}>", f"</h{node.depth}>\n")

def html(renderer, node, ctx):
    if getattr(ctx, "inline_depth", 0) > 0:
        return renderer.escape_html(node.value)
    return "<p>" + renderer.escape_html(node.value.strip()) + "</p>\n"

def list_(renderer, node, ctx):
    tag = "ol" if node.ordered else "ul"
    attrs = f' start="{node.start}"' if node.ordered and node.start not in (None, 1) else ""
    return f"<{tag}{attrs}>\n" + "".join(
        list_item(renderer, i, node.spread, ctx) for i in node.children) + f"</{tag}>\n"

def list_item(renderer, item, loose, ctx):
    body = "".join(
        renderer.render_children(c.children, ctx) if isinstance(c, Paragraph) and not loose
        else renderer.render_node(c, ctx) for c in item.children)
    if item.checked is None:
        return "<li>" + body + "</li>\n"
    cb = '<input class="task-list-item-checkbox" type="checkbox" disabled' + (
        " checked/>" if item.checked else "/>")
    body = body.replace("<p>", "<p>" + cb, 1) if body.startswith("<p>") else cb + body
    return '<li class="task-list-item">' + body + "</li>\n"

def table(renderer, node, ctx):
    head, body = node.children[0], node.children[1:]
    out = "<table>\n<thead>\n" + _row(renderer, head, "th", node.align, ctx) + "</thead>\n"
    if body:
        out += "<tbody>\n" + "".join(_row(renderer, r, "td", node.align, ctx) for r in body) + "</tbody>\n"
    return out + "</table>\n"

def _row(renderer, row, tag, align, ctx):
    out = "<tr>\n"
    for i, cell in enumerate(row.children):
        style = f' style="text-align:{align[i]}"' if i < len(align) and align[i] else ""
        out += f"  <{tag}{style}>{renderer.render_children(cell.children, ctx)}</{tag}>\n"
    return out + "</tr>\n"

class MistuneEmulation:
    def setup(self, wen, /):
        wen.register_renderer_handlers({"html": {
            "text": text, "list": list_, "table": table, "html": html,
            "paragraph": paragraph, "heading": heading}})

def build():
    return Wenmode(rules(), renderer=HTMLRenderer(escape=True),
                   plugins=[definition_list, MistuneEmulation()])
```

Compare against `mistune.create_markdown(plugins=["strikethrough",
"footnotes", "table", "url", "task_lists", "def_list"], escape=True,
hard_wrap=True)` over the bodies extracted from a corpus (every
`message.content` string and every `{"type": "text"}` item).

## Migration: every rendering difference, classified

The bar for the migration: reproduce what is *desirable*, not what is
merely current; every remaining difference in rendered output is
labelled improvement, neutral or regression, with the reason. Same
corpora and method as above, old pipeline reconstructed from `75b7fc9`
(mistune 3.3.0 with our plugins, `escape=True`, `hard_wrap=True`),
new pipeline `render_markdown` as shipped, both with the repository
bound so SHA links fire.

What the migration keeps on purpose: every soft break renders as
`<br />`; strikethrough needs `~~`; block-level raw HTML is escaped
*and* wrapped in `<p>`; e-mail shaped tokens are not autolinked; link
targets use mistune's scheme denylist, not wenmode's allowlist; the
footnotes heading is visually hidden. On wenmode 0.15.0 three more
workarounds were needed (trailing quotes in bare URLs, the table
rule's ordering, and the first two above as local code); 0.15.1
covers all of them with fixes or options.

The tables below were measured on 0.15.0. Re-measured on 0.15.1 the
counts are identical except that the three regression rows are gone:
see *wenmode 0.15.1*.

### Bodies that differ

| corpus | differ | after the four markup-only normalisations | unexplained |
|---|---:|---:|---:|
| real (5936 bodies) | 143 (2.4%) | 29 | 0 |
| fixtures (957 bodies) | 65 (6.8%) | 21 | 0 |

### Causes

| verdict | cause | real | fixtures |
|---|---|---:|---:|
| neutral | table cells: no 2-space indent, `align=` instead of `style="text-align:…"` | 77 | 11 |
| neutral | loose list item: newline between `<li>` and `<p>` | 59 | 31 |
| neutral | tight item: newline before a nested `<ul>`/`<pre>`/… | 29 | 17 |
| neutral | indented code block keeps its final newline (spec output) | 2 | 6 |
| neutral | task list: GFM checkbox markup (no `task-list-item` classes; nothing styled them) | 2 | 1 |
| improvement | backslash-newline is a hard break, not a stray literal `\` (Claude Code's shift-enter writes these) | 0 | 11 |
| improvement | bare URL no longer swallows a trailing `**` or `"` into the href | 7 | 2 |
| improvement | character references decoded: `&amp;amp;` shows `&`, `&copy;` shows © | 6 | 1 |
| improvement | list tightness per CommonMark where mistune rendered loose (reference: markdown-it-py and commonmark.py agree) | 5 | 1 |
| improvement | table rows with ragged cell counts no longer dropped | 2 | 0 |
| improvement | pipes in plain output no longer mis-parsed as a table (mistune dropped a line each time) | 0 | 2 |
| improvement | `\|` inside a code span in a table cell unescaped (GFM) | 1 | 0 |
| neutral | raw-HTML block boundary inside lists/paragraphs — pasted `<bash-stdout>`/`<analysis>` blobs where the two parsers split differently; both are garbage-in | 15 | 5 |
| neutral | backtick-escape edge case inside a code span | 3 | 0 |
| neutral | pasted diff text: an empty `+` line ends the list (spec) | 2 | 0 |
| neutral | code span inside a list item: continuation indent stripped (spec) | 1 | 0 |
| **regression** | wenmode: a list followed by a blank line and a *different* list marker renders loose | 2 | 0 |
| **regression** | wenmode: `N.` (N ≠ 1) after a dedented bullet item joins the item instead of starting a list (hits Claude Code's own compaction prompt) | 1 | 0 |
| **regression** | wenmode: a line after an indented code block inside a list item is lazily continued (pasted diffs) | 1 | 0 |

The three regressions are spacing or grouping, not content loss, and
each is a wenmode parser bug with a reproduction below; all three are
fixed in 0.15.1. The two
regressions found and *fixed* during the migration — quotes swallowed
into bare-URL hrefs, and `cci:`/`file:line` link targets dropped by
the allowlist — no longer appear in the table.

### Markdown output

The Markdown path does not re-render at all any more:
`_protect_html_tags` splices entity-escaped copies over raw-HTML node
ranges (and over a `<` in text that a lax viewer could read as a tag
start), and `linkify_shas_in_text` splices links; everything else is
byte-identical to the source. The seven Markdown snapshots are
unchanged. Compared with the mistune round-trip on the 531 real bodies
containing `<`, 210 now differ — all of them the *old* renderer's
normalisations disappearing: leading whitespace it stripped, `\``
escapes it dropped, an autolink it rewrote.

### The escape contract, proved

38 XSS payloads (script tags, event handlers, `javascript:`/`data:`/
`vbscript:` in every link and image form, entity- and comment-wrapped
scripts, payloads inside every block construct) through all three HTML
renderers: no live tag, no `on*` attribute, no unsafe scheme survives.
`test/test_xss_browser.py` (browser) and `test/test_markdown_rendering.py`
pass unchanged.

### wenmode 0.15.0 bugs found, with reproductions — all fixed in 0.15.1

Each verified against the CommonMark reference implementation
(`commonmark.py` 0.9.1) and markdown-it-py 4.2 (`commonmark` preset),
which agree with each other and disagree with wenmode.

1. **Table rule blocks list interruption.** `Wenmode([Table, *commonmark()]).render("a\n- b | c")`
   gives one paragraph; the reference gives paragraph + list. Cause:
   `_parser/interrupts.py` asks only the *first* matching block opener
   whether it may interrupt a paragraph, and `table` is first in the
   preset. Was worked around here by ordering the table rule last, at
   the cost of one shape (a table whose header row starts with a list
   marker became a list item holding a table); the workaround and its
   cost are gone with 0.15.1.
2. **Trailing blank lines make a list loose.** `"- a\n- b\n\n1. c\n"`
   and `"- a\n- b\n\n\ntext\n"` both render the bullet list loose.
   Cause: `rules/blocks/list.py::consume_blank_list_line` sets
   `item_spread` when the line after the blank is *any* list marker, or
   another blank followed by content the item would not own.
3. **A non-1 ordered marker after a dedented bullet item.**
   `"  - a\n2. b\n"` renders `2. b` as a continuation line of item `a`;
   the reference closes the bullet list and starts `<ol start="2">`.
   The "must start at 1 to interrupt a paragraph" rule is being applied
   against a container the line has already fallen out of.
4. **Lazy continuation after an indented code block in a list item.**
   `"+ x\n\n      code\n@@ y\n"`-shaped input (a pasted diff) keeps the
   `@@ y` line inside the item; the reference closes the item since a
   code block cannot be lazily continued.
5. **Extended autolink keeps trailing quotes.** `'x "https://a/b"'`
   links to `https://a/b%22`; cmark-gfm strips `"` and `'` as trailing
   punctuation. Was worked around here; gone with 0.15.1.

Feature requests that would remove local code here: an option on
`Strikethrough` for the two-tilde-only form (added in 0.15.1 as
`allow_single_tilde=False`); a disallowed-tags override that escapes
fully rather than half (not needed: disabling the filter and letting
`escape=True` escape everything is the intended combination).

### wenmode 0.15.1

Released 2026-09-11 in response to the reproductions above. Upstream's
reading: the five bugs are fixed, and the other differences listed in
this document are GFM spec behaviour, which wenmode follows strictly.

**The five reproductions on 0.15.1**, stock presets, same references:

| # | bug | 0.15.1 |
|---|---|---|
| 1 | table rule blocks list interruption | fixed |
| 2 | trailing blank lines make a list loose | fixed |
| 3 | non-1 ordered marker after a dedented bullet item | fixed |
| 4 | lazy continuation after an indented code block in an item | fixed |
| 5 | extended autolink keeps trailing quotes | fixed |

**Local code removed**, each replaced by upstream and pinned by a test
in `test/test_markdown_rendering.py` that fails on 0.15.0 behaviour:

| was | now |
|---|---|
| `DoubleTildeStrikethrough` subclass | `Strikethrough(allow_single_tilde=False)` |
| hard-wrap `text` handler | `HTMLRenderer(soft_break="br")` |
| table rule reordered last, plus a guard for its absence | stock preset order |
| quote-trimming `parse` override on the autolink rule | upstream trimming; the subclass keeps only the e-mail opt-out |

Removing them changed no rendered body on either corpus: the options
are exact replacements. The mutation check ran the new tests on 0.15.0
with the two new constructor options accepted and ignored, so each
test failed on the old *behaviour*, not on an unknown argument.

**Corpus diff against mistune, before and after the bump:**

| corpus | differ on 0.15.0 | differ on 0.15.1 | regressions 0.15.0 → 0.15.1 |
|---|---:|---:|---:|
| real (5936) | 143 | 143 | 4 bodies → 0 |
| fixtures (957) | 65 | 65 | 0 → 0 |

The bump changed exactly four real bodies, the four listed as
regressions above; each now matches markdown-it-py's structure. The
143 that still differ from mistune do so for the improvement and
neutral causes in the table. No HTML or Markdown snapshot changed.

**Spec differences accepted as GFM.** Upstream's remark covers the
remaining non-bug differences: footnote and task-list markup, table
`align=`, list whitespace, entity decoding, the GFM tag filter and
bare e-mail autolinks. We accept strict GFM for all of them except two
local choices already listed above, both kept because on real
transcripts they would strike or link text its author never marked up:
single-tilde strikethrough (now through wenmode's own option) and bare
e-mail autolinks (`ruff@0.6.0`, `git@github.com:`).

**Visible text, measured separately** (tags stripped, entities decoded,
whitespace-insensitive), final pipeline on 0.15.1 against mistune:

| corpus | bodies whose visible text differs | of which: bare URL no longer swallows `**` | entity decoding (`&amp;amp;` now reads `&`) | stray `\` before a line break gone | other |
|---|---:|---:|---:|---:|---:|
| real (5936) | 18 | 7 | 8 | 0 | 3 |
| fixtures (957) | 16 | 2 | 1 | 11 | 2 |

No body falls in two columns (the columns sum to the total). The
"other" bodies are a table row or an escaped pipe that mistune dropped
or left escaped, lines mistune folded into a phantom table (fixtures),
and malformed backtick escaping inside a code span that each engine
garbles differently.

The rest of the differing bodies change markup only. Every
visible-text change is an improvement except the garbled code spans,
which are malformed input either way.

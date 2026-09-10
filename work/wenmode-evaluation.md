# wenmode as a mistune replacement — evaluation (#323)

**Verdict: not a 100% backward-compatible replacement, and not a
meaningful speed-up on our workload.** Measured 2026-09-10 against
wenmode 0.15.0 and mistune 3.3.0.

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

"""wenmode rules, transforms and renderer handlers shared between the
HTML and Markdown output paths.

The Markdown engine is `wenmode <https://github.com/lepture/wenmode>`_
(mistune's successor by the same author; the switch is #323). Everything
this project adds on top of wenmode's ``github`` preset lives here:

- ``sha_links`` — a post-parse transform that turns commit SHAs into
  links (issue #156): bare ``7c2e6f6`` tokens in prose and
  ``​`5baac35`​``-style code spans alike, when a caller-supplied
  resolver returns a URL. No-op for SHAs the resolver can't map
  (typical of in-flight local-only commits), so the rendered
  transcript doesn't sprout broken links.
- ``DoubleTildeStrikethrough`` — GFM proper accepts ``~one~``;
  transcript prose says "~2, ~6 min" all the time, so we require
  ``~~two~~``.
- ``BlockHtmlMarker`` — tags block-level raw-HTML nodes so the HTML
  renderer can wrap the escaped text in a paragraph.
- ``linkify_shas_in_text`` — the Markdown output's equivalent of
  ``sha_links``: same predicate, but splices ``[sha](url)`` into the
  *source* text, using wenmode's source positions, so everything
  outside the links stays byte-identical.

## Why a transform rather than inline rules

mistune needed two inline-parser rules whose registration order was
load-bearing (the codespan variant had to fire before the built-in
``codespan`` rule), an ``in_link`` state guard, and could only match on
``m.group(0)`` because it concatenates rule patterns and renumbers
groups. A transform runs after parsing, when code spans, links and
emphasis already exist as nodes: SHA detection becomes a walk over
``Text`` and ``InlineCode`` nodes that skips anything under a ``Link``.

## Word-boundary heuristic

``SHA_PATTERN`` matches 7-to-40-char lowercase hex runs at word
boundaries. False-positive shapes worth noting: ``0xdeadbeef`` (the
``deadbeef`` part matches), 7+ char all-hex identifiers, digit runs.
The resolver gate filters those; see ``git_remote.py``.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Callable, Iterator, Optional

from wenmode import Wenmode
from wenmode.nodes import Html, InlineCode, Link, Node, Parent, Position, Text
from wenmode.presets import github
from wenmode.rules import (
    ExtendedAutolink,
    InlineCandidate,
    InlineRule,
    RootTransform,
    Strikethrough,
)
from wenmode.rules.delimiters import find_delimited_span

Resolver = Callable[[str], Optional[str]]

# Word-bounded run of 7-40 lowercase hex chars. Mirrors the standard
# git short-SHA shape (``git config --global core.abbrev`` defaults to
# 7); 40 is the full SHA-1 length.
SHA_PATTERN = r"\b[0-9a-f]{7,40}\b"
_SHA_RE = re.compile(SHA_PATTERN)


# ---------------------------------------------------------------------
# SHA links: one transform for prose SHAs and codespan SHAs
# ---------------------------------------------------------------------


def _link(url: str, child: Node, position: Optional[Position]) -> Link:
    return Link(url=url, children=[child], position=position)


def _text(value: str, base: Optional[Position], start: int, end: int) -> Text:
    position = Position(base.start + start, base.start + end) if base else None
    return Text(value=value, position=position)


def _split_text(node: Text, resolve: Resolver) -> list[Node]:
    """Split a text node around every resolvable SHA it contains."""
    text, pos = node.value, node.position
    parts: list[Node] = []
    last = 0
    for m in _SHA_RE.finditer(text):
        url = resolve(m.group(0))
        if url is None:
            continue
        if m.start() > last:
            parts.append(_text(text[last : m.start()], pos, last, m.start()))
        sha = _text(m.group(0), pos, m.start(), m.end())
        parts.append(_link(url, sha, sha.position))
        last = m.end()
    if not parts:
        return [node]
    if last < len(text):
        parts.append(_text(text[last:], pos, last, len(text)))
    return parts


def linkify_tree(node: Parent, resolve: Resolver, in_link: bool = False) -> None:
    """Rewrite ``node``'s subtree in place, linking resolvable SHAs.

    - A ``Text`` node is split around each resolvable SHA, which becomes
      a ``Link`` wrapping a ``Text``.
    - An ``InlineCode`` node whose whole value is a resolvable SHA is
      wrapped in a ``Link`` (``<a href="…"><code>sha</code></a>``).
    - Nothing under an existing ``Link`` is touched: a SHA the author
      already linked (``[abc1234](url)``) is not double-wrapped.
    - Fenced and indented code are ``Code`` nodes, never ``Text``, so
      they are skipped by construction.
    """
    out: list[Node] = []
    for child in node.children:
        if isinstance(child, Link):
            linkify_tree(child, resolve, in_link=True)
            out.append(child)
        elif in_link:
            out.append(child)
        elif isinstance(child, InlineCode):
            url = resolve(child.value) if _SHA_RE.fullmatch(child.value) else None
            out.append(_link(url, child, child.position) if url else child)
        elif isinstance(child, Text):
            out.extend(_split_text(child, resolve))
        else:
            if isinstance(child, Parent):
                linkify_tree(child, resolve, in_link)
            out.append(child)
    node.children = out


class ShaLinks(InlineRule):
    """Trigger-only rule (no pattern, no opener) carrying the transform.

    wenmode attaches document-wide transforms to rules, so the
    transform rides on a rule that never matches anything itself.
    ``resolve`` returns the URL to link to, or ``None`` to leave the
    SHA unchanged; wrap it in ``functools.lru_cache`` upstream.
    """

    name = "sha_links"

    def __init__(self, resolve: Resolver) -> None:
        super().__init__()
        self.root_transforms = [_ShaLinksTransform(resolve)]


class _ShaLinksTransform(RootTransform):
    name = "sha_links"

    def __init__(self, resolve: Resolver) -> None:
        self.resolve = resolve

    def transform(self, parser: Any, root: Any, state: Any) -> None:
        linkify_tree(root, self.resolve)


def make_sha_plugin(resolve: Resolver) -> Any:
    """Build a wenmode plugin that links resolvable git commit SHAs.

    Suitable for ``Wenmode(..., plugins=[make_sha_plugin(resolve)])``.
    Handles both the bare-prose and the code-span forms.
    """

    class _Plugin:
        def setup(self, wen: Wenmode, /) -> None:
            wen.register_rule(ShaLinks(resolve))

    return _Plugin()


# ---------------------------------------------------------------------
# Strikethrough: ``~~two~~`` only
# ---------------------------------------------------------------------


class DoubleTildeStrikethrough(Strikethrough):
    """GFM strikethrough restricted to the two-tilde form.

    wenmode's ``Strikethrough`` follows the GFM spec, which also
    accepts a single tilde. Transcript prose uses ``~`` for
    "approximately" constantly ("~2, ~6 min"), and two of those in one
    paragraph would otherwise strike through everything between them.
    """

    def parse(
        self, parser: Any, text: str, candidate: Any, state: Any
    ) -> tuple[Optional[Node], int]:
        start = candidate.start
        if not text.startswith("~~", start):
            return None, start
        parsed = find_delimited_span(text, start, "~", max_run=2, reject_adjacent=True)
        if parsed is None or parsed.value_start - start != 2:
            return None, start
        return super().parse(parser, text, candidate, state)


# ---------------------------------------------------------------------
# Autolinks: a quote after a URL is not part of it
# ---------------------------------------------------------------------


class TranscriptAutolink(ExtendedAutolink):
    """GFM extended autolink for URLs only, dropping trailing quotes.

    wenmode 0.15 strips ``?!.,:*_~`` from the end of a bare URL but not
    ``"`` or ``'``, so ``url = "https://x/y"`` — every JSON or TOML
    dump in a transcript — linked to ``https://x/y%22``. cmark-gfm
    treats both quotes as trailing punctuation; so do we.

    Bare e-mail addresses are not linked: ``ruff@0.6.0``,
    ``git@github.com:owner/repo`` and ``user@host`` in shell output all
    match the GFM e-mail grammar and none of them is mail.
    """

    def search_email(self, text: str, pos: int) -> Any:
        return None

    def parse(
        self, parser: Any, text: str, candidate: Any, state: Any
    ) -> tuple[Optional[Node], int]:
        match = candidate.match
        assert match is not None
        value = match.group(0)
        # Quotes and GFM's own trailing punctuation, in any order
        # (``…tokenizer",`` ends in a comma *after* the quote).
        trimmed = value.rstrip("\"'?!.,:*_~")
        if trimmed == value:
            return super().parse(parser, text, candidate, state)
        # Re-match on the shortened text so the base class sees a
        # candidate whose match ends where the URL does.
        shorter = self.compiled.match(
            text[: candidate.start + len(trimmed)], candidate.start
        )
        if shorter is None:
            return None, candidate.start
        return super().parse(
            parser, text, InlineCandidate(candidate.start, shorter), state
        )


# ---------------------------------------------------------------------
# Block-level raw HTML: mark it so the HTML renderer can wrap it
# ---------------------------------------------------------------------

# Parents whose children are inline content. An ``Html`` node under any
# other parent is an HTML *block*.
_INLINE_PARENTS = frozenset(
    {
        "paragraph",
        "heading",
        "tableCell",
        "emphasis",
        "strong",
        "delete",
        "link",
        "linkReference",
    }
)


def mark_block_html(node: Parent) -> None:
    for child in node.children:
        if isinstance(child, Html):
            if node.type not in _INLINE_PARENTS:
                child.data = {**(child.data or {}), "block": True}
        elif isinstance(child, Parent):
            mark_block_html(child)


class _MarkBlockHtml(RootTransform):
    name = "block_html_marker"

    def transform(self, parser: Any, root: Any, state: Any) -> None:
        mark_block_html(root)


class BlockHtmlMarker(InlineRule):
    """Trigger-only rule carrying the block-HTML marking transform."""

    name = "block_html_marker"
    root_transforms = [_MarkBlockHtml()]


# ---------------------------------------------------------------------
# The shared rule set
# ---------------------------------------------------------------------


def transcript_rules(
    resolve: Optional[Resolver] = None, *, autolink: bool = True
) -> list[Any]:
    """wenmode's ``github`` preset adjusted for transcript content.

    - strikethrough requires ``~~``;
    - bare URLs drop a trailing quote (``autolink=False`` leaves bare
      URLs as text — the Markdown output's round-trip wants no
      rewriting it does not need);
    - raw HTML is parsed without wenmode's GFM tag filter — the HTML
      renderer escapes *every* raw-HTML node (``escape=True``), and the
      filter would otherwise leave ``<script>`` half-escaped as
      ``&lt;script>``;
    - block-level raw HTML is marked for the renderer;
    - the table rule is ordered after the other block openers;
    - SHA links, when a resolver is given.

    Pair with ``transcript_plugins()`` when constructing a ``Wenmode``.
    """
    from wenmode.rules import HtmlBlock, RawHtml

    rules: list[Any] = []
    table: Any = None
    for rule in github():
        if rule.name == "strikethrough":
            rules.append(DoubleTildeStrikethrough())
        elif rule.name == "html_block":
            rules.append(HtmlBlock(disallowed_tags=()))
        elif rule.name == "raw_html":
            rules.append(RawHtml(disallowed_tags=()))
        elif rule.name == "table":
            table = rule
        elif rule.name == "extended_autolink":
            if autolink:
                rules.append(TranscriptAutolink())
        else:
            rules.append(rule)
    # The table rule goes last. wenmode (0.15) matches block openers with
    # one alternation and asks only the first matching rule whether it
    # may interrupt a paragraph; ``table`` says no, so with it first a
    # list item containing a pipe (``- b | c``) right after a paragraph
    # line stayed part of the paragraph. The reorder makes the
    # list/heading/blockquote openers answer first; reproduction in
    # work/wenmode-evaluation.md for the upstream report. The cost: a
    # table whose header row starts with a list marker (``- a | b``
    # over ``---|---``) becomes a list item containing a table with
    # header ``a`` instead of a table with header ``- a``.
    if table is None:
        raise RuntimeError("wenmode's github() preset no longer has a 'table' rule")
    rules.append(table)
    rules.append(BlockHtmlMarker())
    if resolve is not None:
        rules.append(ShaLinks(resolve))
    return rules


def transcript_plugins() -> list[Any]:
    """wenmode plugins every transcript pipeline installs."""
    from wenmode.plugins import definition_list

    return [definition_list]


# ---------------------------------------------------------------------
# Markdown output: splice links into the source text
# ---------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def position_parser() -> Wenmode:
    """The transcript parser with source positions, for splicing edits
    into Markdown text (``linkify_shas_in_text``, ``_protect_html_tags``)."""
    return Wenmode(
        transcript_rules(autolink=False), plugins=transcript_plugins(), positions=True
    )


def walk_nodes(node: Node) -> Iterator[Node]:
    """Yield ``node`` and every descendant, depth first."""
    yield node
    if isinstance(node, Parent):
        for child in node.children:
            yield from walk_nodes(child)


def linkify_shas_in_text(text: str, resolve: Resolver) -> str:
    """Substitute resolvable SHAs in ``text`` with Markdown links.

    Used by the Markdown output path where text bodies are emitted
    directly, e.g. ``MarkdownRenderer.format_AssistantTextMessage``.
    Same predicate as the HTML side's ``sha_links`` transform — prose
    SHAs become ``[sha](url)``, single-backtick code spans holding
    exactly a SHA become ``[`sha`](url)``, anything inside code blocks,
    mixed code spans or existing links is left alone — but applied by
    splicing into the *source*: the text is parsed with source
    positions, and only the byte ranges of the matched nodes are
    rewritten, so everything else survives verbatim.
    """
    if not text:
        return text
    root = position_parser().parse(text)
    edits: list[tuple[int, int, str]] = []

    def walk(node: Parent, in_link: bool) -> None:
        for child in node.children:
            if isinstance(child, Link):
                walk(child, True)
            elif in_link:
                continue
            elif isinstance(child, InlineCode):
                if child.position is None or not _SHA_RE.fullmatch(child.value):
                    continue
                start, end = child.position.start, child.position.end
                # Single-backtick form only: the source must be exactly
                # `` `sha` ``, not ``` ``sha`` ``` or `` ` sha ` ``.
                if text[start:end] != f"`{child.value}`":
                    continue
                url = resolve(child.value)
                if url is not None:
                    edits.append((start, end, f"[{text[start:end]}]({url})"))
            elif isinstance(child, Text):
                if child.position is None:
                    continue
                base = child.position.start
                if text[base : child.position.end] != child.value:
                    # The node's value was normalised away from the
                    # source (entities, escapes): offsets inside it are
                    # not source offsets, so leave it alone.
                    continue
                for m in _SHA_RE.finditer(child.value):
                    url = resolve(m.group(0))
                    if url is not None:
                        edits.append(
                            (base + m.start(), base + m.end(), f"[{m.group(0)}]({url})")
                        )
            elif isinstance(child, Parent):
                walk(child, in_link)

    walk(root, False)
    if not edits:
        return text
    out: list[str] = []
    last = 0
    for start, end, replacement in sorted(edits):
        out.append(text[last:start])
        out.append(replacement)
        last = end
    out.append(text[last:])
    return "".join(out)

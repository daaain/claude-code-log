"""XSS: every Markdown interpolation surface routes through one gate (#245).

Round-4 follow-up: the per-message + page/project/session headings were gated
last; this generalises the gate to `safe_markdown_inline` and routes the inline
link-label / list surfaces through it too — so "neutralise raw HTML from every
source" is a single structural property, not a per-site convention.

Surfaces (markdown/renderer.py), each driven end-to-end here:
- WebSearch result link title          → format_WebSearchOutput
- projects-index project heading + link → generate_projects_index
- per-project session-link label        → generate_projects_index (combined off)
- expand-paths tree label               → generate_projects_index (expand_paths)
- (TOC label + headings are pinned in test_xss_titles.py)
"""

from __future__ import annotations

from typing import Any, Optional

from claude_code_log.markdown.renderer import MarkdownRenderer, safe_markdown_inline
from claude_code_log.models import (
    MessageMeta,
    SystemMessage,
    WebSearchLink,
    WebSearchOutput,
)
from claude_code_log.renderer import TemplateMessage

PAYLOAD = "<img src=x onerror=alert(1)>"


def _no_raw_tag(md: str) -> None:
    assert "<img" not in md.lower(), md
    assert "&lt;img" in md.lower(), md


# ----------------------------- the gate itself -------------------------------


class TestSafeMdInlineGate:
    def test_tag_is_entity_escaped(self):
        assert safe_markdown_inline(PAYLOAD) == "&lt;img src=x onerror=alert(1)&gt;"

    def test_plain_text_passes_through_byte_identical(self):
        # No `<` → no mistune round-trip → markdown escaping not re-normalised.
        for s in ("just text", "a **bold** label", r"escaped \*\*stars\*\*", ""):
            assert safe_markdown_inline(s) == s


# ----------------------------- inline surfaces -------------------------------


class TestMarkdownInlineSurfaces:
    def test_websearch_link_title(self):
        out = WebSearchOutput(
            query="q",
            links=[WebSearchLink(title=PAYLOAD, url="https://example.com")],
        )
        # format_WebSearchOutput ignores the message arg; any real
        # TemplateMessage satisfies the signature.
        msg = TemplateMessage(
            SystemMessage(
                meta=MessageMeta(
                    uuid="u", session_id="s", timestamp="2025-01-01T00:00:00Z"
                ),
                level="info",
                text="x",
            )
        )
        md = MarkdownRenderer().format_WebSearchOutput(out, msg)
        _no_raw_tag(md)
        # The URL is preserved (only the label fragment is neutralised).
        assert "(https://example.com)" in md

    def test_project_index_heading_and_session_link(self):
        # combined_suppressed → plain `## {display_name}` heading + per-session
        # `- [{summary}](file)` links. Payload in BOTH the cwd-derived
        # display_name and a session summary.
        project_summaries: list[dict[str, Any]] = [
            {
                "name": "-home-u-proj",
                "html_file": "proj/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 1,
                "last_modified": 1700000000.0,
                "working_directories": [f"/home/u/{PAYLOAD}"],
                "combined_suppressed": True,
                "sessions": [
                    {
                        "id": "11110000",
                        "file": "proj/session-11110000.md",
                        "summary": PAYLOAD,
                    }
                ],
            }
        ]
        md = MarkdownRenderer().generate_projects_index(project_summaries)
        _no_raw_tag(md)
        assert "## <img" not in md.lower()  # heading neutralised
        assert "- [<img" not in md.lower()  # session-link label neutralised
        assert "(./proj/session-11110000.md)" in md  # link target preserved

    def test_expand_paths_tree_label(self):
        # expand_paths_tree mode builds the index as a directory tree whose
        # leaf labels are session labels (summary-derived).
        project_summaries: list[dict[str, Any]] = [
            {
                "name": "-home-u-proj",
                "html_file": "proj/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 1,
                "last_modified": 1700000000.0,
                "working_directories": ["/home/u/proj"],
                "combined_suppressed": True,
                "sessions": [
                    {
                        "id": "11110000",
                        "file": "proj/session-11110000.md",
                        "summary": PAYLOAD,
                    }
                ],
            }
        ]
        md = MarkdownRenderer().generate_projects_index(
            project_summaries, expand_paths_tree=True
        )
        _no_raw_tag(md)
        assert "[<img" not in md.lower()


# ------------------------------- message bodies ------------------------------
# GHSA-3jq4-r9jm-q682: formatter output reached the .md without a gate (the
# TodoWrite tool_result first, then ~30 more bodies). ``_render_message`` now
# passes every body through ``_gate_body``; these pin the gate and sweep it
# across every fixture shape we have.

import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from claude_code_log.converter import convert_jsonl_to  # noqa: E402
from claude_code_log.markdown.renderer import _gate_body, _own_tag  # noqa: E402
from claude_code_log.markdown_plugins import position_parser, walk_nodes  # noqa: E402
from wenmode.nodes import Html  # noqa: E402

TEST_DATA = Path(__file__).parent / "test_data"
_LIVE_PAYLOAD = re.compile(r"<img src=x onerror=")

# Keys whose values steer parsing/dispatch rather than carry text; tainting
# them would just make the fixture unparseable and test nothing.
_STRUCTURAL_KEYS = {
    "type", "uuid", "parentUuid", "logicalParentUuid", "leafUuid", "sessionId",
    "timestamp", "role", "tool_use_id", "id", "name", "requestId", "agentId",
    "media_type", "subtype", "model", "version", "userType", "stop_reason",
    "level", "isSidechain", "operation", "status", "source",
}  # fmt: skip


def _viewer_renderers() -> list[Any]:
    """Parsers standing in for viewers, more of them than the gate uses.

    The gate's own second opinion can't also be the judge, or a gap in it
    is a gap in the test. So besides CommonMark+tables and gfm-like with
    linkify and footnotes, this adds definition lists, task lists, front
    matter and attributes (Obsidian-style extensions), and switches off
    markdown-it's own link filter, so a ``javascript:`` target the
    renderer let through shows up instead of being dropped here.
    """
    from markdown_it import MarkdownIt

    class AllLinks(MarkdownIt):
        def validateLink(self, url: str) -> bool:  # noqa: N802 (markdown-it's name)
            return True

    from mdit_py_plugins.attrs import attrs_plugin
    from mdit_py_plugins.deflist import deflist_plugin
    from mdit_py_plugins.footnote import footnote_plugin
    from mdit_py_plugins.front_matter import front_matter_plugin
    from mdit_py_plugins.tasklists import tasklists_plugin

    renderers = [
        AllLinks("commonmark", {"html": True}).enable(["table", "strikethrough"]),
        AllLinks("gfm-like", {"html": True, "linkify": True}).use(footnote_plugin),
        AllLinks("commonmark", {"html": True})
        .enable(["table", "strikethrough"])
        .use(deflist_plugin)
        .use(tasklists_plugin)
        .use(front_matter_plugin)
        .use(attrs_plugin),
    ]
    return renderers


_VIEWERS = _viewer_renderers()


def _viewer_html(md: str) -> str:
    """``md`` as each viewer renders it, concatenated."""
    return "\n".join(r.render(md + "\n") for r in _VIEWERS)


_HARMFUL_HREF = re.compile(
    r"""(?:href|src)="\s*(?:javascript|vbscript|data:text)""", re.IGNORECASE
)


def _raw_html_payloads(md: str) -> list[str]:
    """Live payload tags in ``md``, by either parser's reading of it.

    The renderer's gate trusts wenmode's view of what is code, so wenmode
    alone can't be the judge: every bypass found in review was a place where
    a viewer's parser disagreed with it. The ``.md`` is rendered with
    markdown-it and any live payload in the HTML counts, as does a wenmode
    raw-HTML node carrying one (the parser the gate itself uses).
    """
    html = _viewer_html(md)
    hits = [m.group(0) for m in _LIVE_PAYLOAD.finditer(html)]
    hits += [m.group(0) for m in _HARMFUL_HREF.finditer(html)]
    for node in walk_nodes(position_parser().parse(md)):
        if isinstance(node, Html) and node.position is not None:
            raw = md[node.position.start : node.position.end]
            if _LIVE_PAYLOAD.search(raw):
                hits.append(raw)
    return hits


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _render_md(jsonl: Path, tmp_path: Path) -> str:
    out = tmp_path / (jsonl.stem + ".md")
    convert_jsonl_to(
        "md",
        jsonl,
        out,
        use_cache=False,
        silent=True,
        generate_individual_sessions=False,
    )
    return out.read_text(encoding="utf-8")


class TestBodyGate:
    def test_transcript_tag_is_escaped(self):
        assert _gate_body(f"> {PAYLOAD}") == "> &lt;img src=x onerror=alert(1)&gt;"

    def test_own_tags_survive(self):
        body = (
            _own_tag("<details>")
            + "\n"
            + _own_tag("<summary>")
            + "x"
            + _own_tag("</summary>")
            + f"\n\n{PAYLOAD}\n"
            + _own_tag("</details>")
        )
        assert _gate_body(body) == (
            "<details>\n<summary>x</summary>\n\n"
            "&lt;img src=x onerror=alert(1)>\n</details>"
        )

    def test_forged_marker_is_not_honoured(self):
        # Transcript text can contain the marker characters but not the
        # per-process token, so it can't pass a tag through the gate.
        forged = "deadbeefdeadbeef:img src=x onerror=alert(1)"
        assert "<img" not in _gate_body(forged)

    def test_fenced_code_survives_byte_for_byte(self):
        body = f"```\n{PAYLOAD}\n```\n\nand `plain` inline"
        assert _gate_body(body) == body

    @pytest.mark.parametrize("span", ["Vec<u8>", PAYLOAD, "a*b*_c_|[d](e)\\<x>"])
    def test_code_span_with_a_tag_renders_as_the_same_code(self, span: str):
        # A code span holding `<` becomes our own <code> element, every
        # punctuation character encoded: no raw `<` for a viewer to
        # misread, and the same rendering as the span.
        from markdown_it import MarkdownIt

        render = MarkdownIt("commonmark").render
        span_md = f"and `{span}` inline"
        gated = _gate_body(span_md)
        assert "<" not in gated.replace("<code>", "").replace("</code>", "")
        expected = render(span_md) if "<" in span else None
        if expected is not None:
            assert MarkdownIt("commonmark", {"html": True}).render(gated) == expected


class TestToolResultBodies:
    """The advisory's paths, end to end through the converter."""

    def _rows(self, name: str, content: Any) -> list[dict[str, Any]]:
        base = {"sessionId": "s1", "cwd": "/x", "version": "1",
                "userType": "external", "isSidechain": False}  # fmt: skip
        tool_input = {"todos": []} if name == "TodoWrite" else {"x": 1}
        return [
            {**base, "type": "assistant", "uuid": "a1", "parentUuid": None,
             "timestamp": "2026-01-01T00:00:00Z",
             "message": {"id": "a1", "type": "message", "role": "assistant",
                         "model": "m", "content": [{"type": "tool_use", "id": "t1",
                                                    "name": name, "input": tool_input}]}},
            {**base, "type": "user", "uuid": "u1", "parentUuid": "a1",
             "timestamp": "2026-01-01T00:00:01Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": content}]}},
        ]  # fmt: skip

    def test_todowrite_result_string(self, tmp_path: Path):
        f = tmp_path / "todo.jsonl"
        _write_jsonl(f, self._rows("TodoWrite", f"Todos ok. {PAYLOAD}"))
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == []
        assert "Todos ok. &lt;img" in md

    def test_unknown_tool_result_text_list(self, tmp_path: Path):
        f = tmp_path / "list.jsonl"
        _write_jsonl(
            f, self._rows("SomeUnknownTool", [{"type": "text", "text": PAYLOAD}])
        )
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == []
        assert "&lt;img" in md


# How the payload is attached to a field. Each shape is one way review
# found to make the gate's parser and a viewer's disagree about what is
# code, so a field tainted with it reaches the viewer as live HTML unless
# the second-opinion check catches it.
_TAINT_SHAPES = {
    "plain": "{v}{p}",
    "code-span": "{v}`{p}`",
    "newline": "{v}\n{p}",
    "odd-break-indent": "{v}\u2028\u2028    {p}",
    "vertical-tab-indent": "{v}\x0b\x0b    {p}",
    "table-cell": "{v}\n\n|a|b|\n|-|-|\n|`{p}|`|",
    "open-fence": "{v}\n```\n{p}",
    "newline-fence": "{v}\n```\n```\n{p}\n```",
    "autolink-code-span": "{v} https://example.com/`{p}`",
    "javascript-link": "{v} [x](javascript:alert({n}))",
    "javascript-image": "{v} ![x](javascript:alert({n}))",
    "javascript-autolink": "{v} <javascript:alert({n})>",
    "javascript-definition": "{v}\n\n[d{n}]: javascript:alert({n})\n\n[d{n}]",
    "label-breakout": "{v}](javascript:alert({n})) [x",
}


def _taint(value: Any, key: Optional[str], counter: list[int], shape: str) -> Any:
    if isinstance(value, dict):
        return {k: _taint(v, k, counter, shape) for k, v in value.items()}
    if isinstance(value, list):
        return [_taint(v, key, counter, shape) for v in value]
    if (
        isinstance(value, str)
        and key not in _STRUCTURAL_KEYS
        and not re.fullmatch(r"[0-9a-f-]{8,}", value)
    ):
        counter[0] += 1
        payload = f"<img src=x onerror=f{counter[0]}>"
        return _TAINT_SHAPES[shape].format(v=value, p=payload, n=counter[0])
    return value


@pytest.mark.parametrize("shape", sorted(_TAINT_SHAPES))
@pytest.mark.parametrize(
    "fixture",
    sorted(p.relative_to(TEST_DATA).as_posix() for p in TEST_DATA.glob("*.jsonl")),
)
def test_no_raw_html_from_any_transcript_field(
    fixture: str, shape: str, tmp_path: Path
):
    """Every text-bearing field of every fixture, tainted, renders inert.

    The structural guard: a new formatter that interpolates transcript text
    without escaping it still can't put live markup in the ``.md``. Each
    taint shape is one way to make a viewer's parser disagree with the
    gate's, and a viewer's parser (markdown-it) is the judge.
    """
    counter = [0]
    rows = []
    for line in (TEST_DATA / fixture).read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.dumps(_taint(json.loads(line), None, counter, shape)))
        except json.JSONDecodeError:
            rows.append(line)
    tainted = tmp_path / Path(fixture).name
    tainted.write_text("\n".join(rows) + "\n", encoding="utf-8")
    md = _render_md(tainted, tmp_path)
    assert _raw_html_payloads(md) == []


# ------------------------------ body isolation -------------------------------
# The gate judges each body alone, but the .md is read as one document: a body
# that leaves a fence open, or one whose indented first line continues the
# previous body's list item, would put the next body's fenced (raw) HTML in
# prose. ``_seal_body`` makes every body parse in the document as it did alone.

from markdown_it import MarkdownIt  # noqa: E402

from claude_code_log.markdown.renderer import _seal_body  # noqa: E402

# Bodies that leave a block open behind them.
_OPEN_ENDINGS = [
    "text\n```",
    "text\n```py",
    "text\n~~~~",
    "```\ncode",
    "  ```\ncode\n",
    "- item",
    "1. item",
    "[^1]: note",
    "> quote",
    "- ```\n  code",
]
# Bodies whose code holds the payload raw, as the gate leaves it.
_CODE_STARTS = [
    f"```\n{PAYLOAD}\n```",
    f"~~~~\n{PAYLOAD}\n~~~~",
    f"    {PAYLOAD}",
    f"\t{PAYLOAD}",
    f"  ```\n{PAYLOAD}\n  ```",
    f"     ```\n{PAYLOAD}\n     ```",
    f"\n    {PAYLOAD}",
]


def _live_in_markdown_it(md: str) -> bool:
    return "<img src=x" in MarkdownIt("commonmark", {"html": True}).render(md)


class TestSealBody:
    def test_open_backtick_fence_is_closed(self):
        assert _seal_body("text\n```") == "text\n```\n```"

    def test_closer_matches_opener_char_and_length(self):
        assert _seal_body("~~~~ py\nx\n~~~") == "~~~~ py\nx\n~~~\n~~~~"
        assert _seal_body("`````\nx\n```") == "`````\nx\n```\n`````"

    def test_closed_and_nested_fences_untouched(self):
        for body in (
            "```\nx\n```",
            "```\nx\n```  ",
            "> ```\n> x",
            "- ```\n  x",
            "x\n\n    ```\n    x",
            "``` a ` b",
        ):
            assert _seal_body(body) == body

    def test_indented_start_is_sealed(self):
        assert _seal_body("    code") == "<!-- -->\n\n    code"
        assert _seal_body("\tcode") == "<!-- -->\n\n\tcode"
        assert _seal_body("\n  x") == "<!-- -->\n\n\n  x"

    def test_unindented_start_untouched(self):
        for body in ("text", "- item", "```\nx\n```", "", "   "):
            assert _seal_body(body) == body

    @pytest.mark.parametrize("ending", _OPEN_ENDINGS)
    @pytest.mark.parametrize("start", _CODE_STARTS)
    def test_next_body_code_stays_code(self, ending: str, start: str):
        # The precondition: each body alone is inert, the payload is code.
        for body in (ending, start):
            assert _raw_html_payloads(_gate_body(body)) == []
        md = "\n\n".join([_gate_body(ending), _gate_body(start)])
        assert _raw_html_payloads(md) == [], md
        assert not _live_in_markdown_it(md), md


class TestBodyIsolationEndToEnd:
    """Both cross-body breakouts, through the converter."""

    _BASE = {"sessionId": "s1", "cwd": "/x", "version": "1",
             "userType": "external", "isSidechain": False}  # fmt: skip

    def _tool_pair(
        self, name: str, tool_input: dict[str, Any], result: str, parent: Any
    ) -> list[dict[str, Any]]:
        b = self._BASE
        return [
            {**b, "type": "assistant", "uuid": "a1", "parentUuid": parent,
             "timestamp": "2026-01-01T00:00:01Z",
             "message": {"id": "a1", "type": "message", "role": "assistant",
                         "model": "m", "content": [{"type": "tool_use", "id": "t1",
                                                    "name": name, "input": tool_input}]}},
            {**b, "type": "user", "uuid": "u1", "parentUuid": "a1",
             "timestamp": "2026-01-01T00:00:02Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "t1", "content": result}]}},
        ]  # fmt: skip

    def test_user_text_ending_in_open_fence(self, tmp_path: Path):
        # The reviewer's case: the user's open fence was closed by the tool
        # result's opening fence, leaving the result's HTML in prose.
        user = {**self._BASE, "type": "user", "uuid": "u0", "parentUuid": None,
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello\n```"}}  # fmt: skip
        f = tmp_path / "fence.jsonl"
        _write_jsonl(f, [user, *self._tool_pair("Foo", {"x": 1}, PAYLOAD, "u0")])
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == []
        assert not _live_in_markdown_it(md)

    def test_todowrite_list_then_indented_result(self, tmp_path: Path):
        # The TodoWrite input ends in a checklist; an indented result is code
        # alone but continued the last list item, as live HTML.
        todos = {"todos": [{"content": "a", "status": "pending", "activeForm": "a"}]}
        f = tmp_path / "list.jsonl"
        _write_jsonl(f, self._tool_pair("TodoWrite", todos, f"    {PAYLOAD}", None))
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == []
        assert not _live_in_markdown_it(md)


class TestMarkdownIndexLinkTargets:
    """Index link targets come from directory names: never a breakout, a
    ``javascript:`` scheme or a network path."""

    @staticmethod
    def _index(dir_name: str, *, combined: bool, expand: bool = False) -> str:
        summaries: list[dict[str, Any]] = [
            {
                "name": dir_name,
                "html_file": f"{dir_name}/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 1,
                "last_modified": 1700000000.0,
                "working_directories": ["/home/u/proj"],
                "combined_suppressed": not combined,
                "sessions": [
                    {
                        "id": "11110000",
                        "file": f"{dir_name}/session-11110000.md",
                        "summary": "s",
                    }
                ],
            }
        ]
        return MarkdownRenderer().generate_projects_index(
            summaries, expand_paths_tree=expand
        )

    @pytest.mark.parametrize(
        "combined,expand", [(True, False), (False, False), (False, True)]
    )
    @pytest.mark.parametrize(
        "dir_name",
        [
            "a)<img src=x onerror=alert(1)>",
            "javascript:alert(1)",
            "\\\\evil.example\\x",
        ],
    )
    def test_targets_are_relative_and_closed(
        self, dir_name: str, combined: bool, expand: bool
    ) -> None:
        import os

        if os.sep == "\\" and "\\" in dir_name:
            pytest.skip("a backslash is a separator on Windows")
        html = _viewer_html(self._index(dir_name, combined=combined, expand=expand))
        assert _LIVE_PAYLOAD.search(html) is None, html
        hrefs = re.findall(r'href="([^"]*)"', html)
        assert hrefs, html
        for href in hrefs:
            assert href.startswith("./"), hrefs


class TestViewerDisagreements:
    """Each review finding (M1-M4, M6), pinned end to end or at the gate."""

    @pytest.mark.parametrize(
        "brk", ["\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85"]
    )
    def test_odd_line_breaks_cannot_fake_a_code_block(self, brk: str) -> None:
        md = _gate_body(f"ok{brk}{brk}    {PAYLOAD}")
        assert _raw_html_payloads(md) == [], md

    def test_table_cell_cannot_hide_a_tag_in_a_code_span(self) -> None:
        md = _gate_body(f"|a|b|\n|-|-|\n|`{PAYLOAD}|`|")
        assert _raw_html_payloads(md) == [], md

    @pytest.mark.parametrize(
        "path", [f"`{PAYLOAD}`", f"a\n```\n{PAYLOAD}\n```", f"a\n{PAYLOAD}"]
    )
    def test_collapsible_summary_is_escaped_html(self, tmp_path: Path, path: str):
        rows = TestToolResultBodies()._rows("Write", "ok")
        rows[0]["message"]["content"][0]["input"] = {"file_path": path, "content": "x"}
        f = tmp_path / "write.jsonl"
        _write_jsonl(f, rows)
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == [], md

    def test_heading_with_a_newline_stays_one_line(self, tmp_path: Path) -> None:
        rows = TestToolResultBodies()._rows("Write", "ok")
        rows[0]["message"]["content"][0]["input"] = {
            "file_path": f"a\n{PAYLOAD}",
            "content": "x",
        }
        f = tmp_path / "heading.jsonl"
        _write_jsonl(f, rows)
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == [], md

    def test_toc_label_cannot_open_a_fence(self, tmp_path: Path) -> None:
        # A summary ending in a fence opener used to start a fence in the TOC
        # that the first user message's own fence then closed, leaving that
        # message's fenced payload outside any fence.
        rows = [
            {"type": "summary", "summary": "x\n```", "leafUuid": "a1"},
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/x",
                "sessionId": "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
                "version": "1",
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {"role": "user", "content": f"hi\n```\n{PAYLOAD}\n```"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u1",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/x",
                "sessionId": "29ccd257-68b1-427f-ae5f-6524b7cb6f20",
                "version": "1",
                "timestamp": "2025-01-01T00:00:01Z",
                "message": {
                    "id": "m",
                    "type": "message",
                    "role": "assistant",
                    "model": "x",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        ]
        f = tmp_path / "toc.jsonl"
        _write_jsonl(f, rows)
        md = _render_md(f, tmp_path)
        assert _raw_html_payloads(md) == [], md

    def test_quoted_html_keeps_its_blockquote_markers(self) -> None:
        md = _gate_body('> <Steps>\n>   <Step title="a">\n>   </Step>\n> </Steps>')
        assert md.splitlines() == [
            "> &lt;Steps>",
            ">   &lt;Step title=&quot;a&quot;&gt;",
            ">   &lt;/Step&gt;",
            "> &lt;/Steps&gt;",
        ]

    def test_code_inside_an_html_block_still_reads_as_code(self) -> None:
        # Only the `<` opening an HTML block is escaped, and the text parsed
        # again: a fence inside stays code (no `&amp;` shown), a code span
        # with a tag becomes our own <code>, inner tags are escaped.
        from markdown_it import MarkdownIt

        body = (
            '<Tabs>\n<Tab title="Win">\n```powershell\n'
            'Remove-Item "$env:USERPROFILE\\.claude" && echo ok\n```\n'
            "Downloads to `~/.claude/claude-<version>` then\n</Tab>\n</Tabs>"
        )
        gated = _gate_body(body)
        html = MarkdownIt("commonmark", {"html": True}).render(gated)
        assert (
            'Remove-Item "$env:USERPROFILE\\.claude" &amp;&amp; echo ok'
            in html.replace("&quot;", '"')
        )
        assert "<code>~/.claude/claude-&lt;version&gt;</code>" in html
        assert _raw_html_payloads(gated) == []

    def test_document_check_stands_on_its_own(self) -> None:
        # The final pass over a whole document catches what a body-level
        # check would, even when nothing before it did.
        from claude_code_log.markdown.renderer import neutralise_foreign_html

        doc = f"<!-- Generated -->\n\n# T\n\n|a|b|\n|-|-|\n|`{PAYLOAD}|`|\n\n<details>\n<summary>s</summary>\n\nx\n</details>"
        out = neutralise_foreign_html(doc)
        assert _raw_html_payloads(out) == [], out
        assert "<details>" in out and "<summary>s</summary>" in out
        assert out.startswith("<!-- Generated -->")


def test_heading_gate_has_the_last_word_on_image_alt_code() -> None:
    # In an image's alt text, markdown-it ends a "code span" wenmode sees.
    from claude_code_log.markdown.renderer import safe_markdown_label

    title = f"Bash: *x ![`](x){PAYLOAD}`*"
    assert _raw_html_payloads(f"### {safe_markdown_label(title)}") == []


def test_rewritten_spans_do_not_turn_a_line_into_a_fence() -> None:
    # Found in real transcripts: the backticks of the code spans after a
    # leading ``` were all that kept the line from opening a fence.
    from markdown_it import MarkdownIt

    body = (
        "> Import `Job` separately:```ts\n"
        "> import { PgBoss, type Job } from 'pg-boss';\n"
        "> ```Then use `Job<T>` instead of `PgBoss.Job<T>`.\n"
        "> \n> Let me fix it."
    )
    gated = _gate_body(body)
    html = MarkdownIt("commonmark", {"html": True}).render(gated)
    assert "<pre>" not in html
    assert "Then use" in html and "Let me fix it." in html
    assert _raw_html_payloads(gated) == []


def test_a_gate_run_twice_leaks_no_marker() -> None:
    # Formatters gate fragments themselves, then the body gate runs again;
    # a harmful link around the first run's <code> marker must not split it.
    from claude_code_log.markdown.renderer import safe_markdown_inline

    twice = _gate_body(safe_markdown_inline("x [a `b<c>` d](javascript:alert(1)) y"))
    assert "\ue000" not in twice and "\ue001" not in twice
    assert _raw_html_payloads(twice) == []


def test_a_code_span_around_a_first_pass_marker_keeps_it() -> None:
    # Found in a real transcript: a formatter's own gate pass leaves a
    # <code> marker, and the body gate then sees a code span around it.
    from claude_code_log.markdown.renderer import safe_markdown_inline

    first = safe_markdown_inline("a `x<y>` b")
    twice = _gate_body(f"[`{first}<z>`]")
    assert "\ue000" not in twice and "\ue001" not in twice
    assert _raw_html_payloads(twice) == []

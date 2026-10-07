"""YAML front matter at the start of a Markdown body renders as data.

Read as plain Markdown, the closing ``---`` of a front-matter block is a
setext underline and the whole block becomes one heading. A whole body (a
message, a Markdown file, a plan) that opens with front matter shows it
with the tool-params renderer instead — a YAML code block when it is not a
non-empty mapping — in both HTML themes and in Markdown output. Inline
renders, previews and params values never parse front matter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from wenmode import Wenmode
from wenmode.plugins import frontmatter as wen_frontmatter

from claude_code_log.converter import load_transcript
from claude_code_log.frontmatter import (
    RawFrontmatter,
    load_frontmatter,
    split_frontmatter,
)
from claude_code_log.html.renderer import generate_html
from claude_code_log.markdown.renderer import MarkdownRenderer

LONG_DESCRIPTION = (
    "Use this skill when the **transcript** needs a summary; it carries "
    "`code`, a [link](https://example.com) and enough words to fold."
)
XSS_TICK = "a` <img src=x onerror=alert(2)> `b"

USER_DOC = (
    "---\n"
    "name: fm-skill\n"
    "tags:\n  - alpha\n  - beta\n"
    "meta:\n  owner: team\n  level: 3\n"
    "created: 2026-10-07\n"
    f"description: {LONG_DESCRIPTION}\n"
    "note: <script>alert(1)</script>\n"
    f"tick: '{XSS_TICK}'\n"
    "block: |\n  first line\n\n  # Not a heading\n"
    "---\n"
    "# Body heading\n\nBody text.\n"
)
# Unquoted ": " inside a value: a YAML error, so a code block.
INVALID_DOC = "---\nname: x\ndescription: Use when: it breaks\n---\nAfter invalid.\n"
SCALAR_DOC = "---\njust a scalar line\n---\nAfter scalar.\n"
# A horizontal rule opening the text, then a later one: not front matter.
RULE_DOC = "---\n\nBetween rules.\n\n---\nAfter rules.\n"
# A fenced block below the first line is not front matter either.
LATE_DOC = "Intro line.\n\n---\nlate: block\n---\nAfter late.\n"
PARAM_VALUE = "---\nparam_key: param value\n---\nparam body"
LONG_DOC = (
    "---\nname: long-doc\ndescription: a long one\n---\n"
    + "\n".join(f"Line {i} of the long body." for i in range(30))
    + "\n"
)


def _entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.34",
    }
    base.update(fields)
    return base


def _user(uuid: str, parent: str | None, minute: int, content: Any) -> dict[str, Any]:
    return _entry(
        type="user",
        timestamp=f"2026-04-19T10:{minute:02d}:00.000Z",
        parentUuid=parent,
        uuid=uuid,
        message={"role": "user", "content": content},
    )


def _assistant(
    uuid: str, parent: str, minute: int, content: list[Any]
) -> dict[str, Any]:
    return _entry(
        type="assistant",
        timestamp=f"2026-04-19T10:{minute:02d}:00.000Z",
        parentUuid=parent,
        uuid=uuid,
        requestId=f"req-{uuid}",
        message={
            "id": f"msg-{uuid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-test",
            "content": content,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def _text(text: str) -> list[Any]:
    return [{"type": "text", "text": text}]


def _write(tmp_path: Path) -> Path:
    entries = [
        _user("u1", None, 0, USER_DOC),
        _assistant("a1", "u1", 1, _text(INVALID_DOC)),
        _assistant("a2", "a1", 2, _text(SCALAR_DOC)),
        _assistant("a3", "a2", 3, _text(RULE_DOC)),
        _assistant("a4", "a3", 4, _text(LATE_DOC)),
        _assistant(
            "a5",
            "a4",
            5,
            [
                {
                    "type": "tool_use",
                    "id": "toolu_param",
                    "name": "SomeUnknownTool",
                    "input": {"payload": PARAM_VALUE, "tool_tick": "run `x` now"},
                }
            ],
        ),
        _assistant(
            "a6",
            "a5",
            6,
            [
                {
                    "type": "tool_use",
                    "id": "toolu_write",
                    "name": "Write",
                    "input": {"file_path": "/repo/docs/long.md", "content": LONG_DOC},
                }
            ],
        ),
    ]
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def _between(html: str, start: str, end: str) -> str:
    """The slice of ``html`` from ``start`` up to the next ``end``."""
    tail = html[html.index(start) :]
    return tail[: tail.index(end)] if end in tail else tail


@pytest.fixture(params=["classic", "minimal"])
def html(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    return generate_html(load_transcript(_write(tmp_path)), "fm", theme=request.param)


class TestHtml:
    def test_mapping_renders_as_params_table(self, html: str) -> None:
        block = _between(html, "<div class='frontmatter'>", "Body heading")
        assert "tool-params-table" in block
        for key in ("name", "tags", "meta", "created", "description"):
            assert f"</span>{key}</" in block
        assert "fm-skill" in block
        # A list and a nested map are nested tables; a date stays a date.
        assert block.count("tool-params-nested") >= 2
        assert ">alpha<" in block and ">beta<" in block
        assert ">owner<" in block or "</span>owner</td>" in block
        assert "2026-10-07" in block
        # The long description is Markdown.
        assert "<strong>transcript</strong>" in block
        assert '<a href="https://example.com"' in block

    def test_no_heading_from_front_matter(self, html: str) -> None:
        assert "<h1>Body heading</h1>" in html
        assert "<h2>name: fm-skill" not in html
        assert "<h2>tags:" not in html
        assert "<h2>name: x" not in html
        assert "<h2>just a scalar line" not in html

    def test_front_matter_values_are_escaped(self, html: str) -> None:
        assert "<script>alert(1)" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;img src=x onerror=alert(2)&gt;" in html

    def test_invalid_yaml_is_a_code_block(self, html: str) -> None:
        block = _between(html, "description: Use when", "After invalid.")
        # Pygments-highlighted YAML, content kept, no params table.
        assert "it breaks" in block
        assert "tool-params-table" not in block
        before = html[: html.index("description: Use when")]
        assert before.rindex("class='frontmatter'") > before.rindex("fm-skill")

    def test_scalar_is_a_code_block(self, html: str) -> None:
        before = html[: html.index("After scalar.")]
        assert "just a scalar line" in before
        assert "<h2>just a scalar line" not in before

    def test_rule_and_late_block_unaffected(self, html: str) -> None:
        rules = _between(html, "Between rules.", "After rules.")
        assert "<hr" in rules and "frontmatter" not in rules
        # Not at the start: Markdown as before (the setext reading included).
        late = _between(html, "Intro line.", "After late.")
        assert "frontmatter" not in late
        assert "<h2>late: block</h2>" in late

    def test_param_value_is_not_front_matter(self, html: str) -> None:
        # Markdown inside a params value never parses front matter (it
        # keeps the plain reading, setext heading included).
        value = _between(html, "param_key", "param body")
        assert "frontmatter" not in value
        # User doc, invalid YAML, scalar, Write content: the param is not one.
        assert html.count("class='frontmatter'") == 4

    def test_markdown_file_preview_skips_front_matter(self, html: str) -> None:
        write = _between(html, "write-tool-content", "Line 29 of")
        assert "class='frontmatter'" in write
        preview = _between(write, "<div class='preview-content", "</summary>")
        assert "Line 0 of the long body." in preview
        assert "long-doc" not in preview
        assert "<h2>" not in preview


class TestMarkdown:
    @pytest.fixture
    def md(self, tmp_path: Path) -> str:
        return MarkdownRenderer().generate(load_transcript(_write(tmp_path)), "fm")

    def test_mapping_as_key_value_list(self, md: str) -> None:
        assert "**name:** `fm-skill`" in md
        assert '"alpha"' in md and '"owner": "team"' in md
        assert "**created:** `2026-10-07`" in md
        assert "\nname: fm-skill\n" not in md
        assert "# Body heading" in md

    def test_assistant_front_matter_is_quoted(self, md: str) -> None:
        assert "> ```yaml\n> name: x\n> description: Use when: it breaks\n> ```" in md
        assert "> After invalid." in md

    def test_values_are_escaped(self, md: str) -> None:
        # The only raw tags left are short values, each inside a code span:
        # the one carrying backticks gets a span wide enough to hold them.
        assert "**note:** `<script>alert(1)</script>`" in md
        assert md.count("<script>") == 1
        assert f"**tick:** ``{XSS_TICK}``" in md
        assert md.count("<img src=x") == 1

    def test_multiline_value_is_fenced(self, md: str) -> None:
        # A line break in a code span would let "# Not a heading" render as
        # a heading downstream; the value is a fenced block instead.
        assert "**block:**\n\n```\nfirst line\n\n# Not a heading\n" in md
        assert "`first line" not in md

    def test_tool_param_backtick_stays_in_its_span(self, md: str) -> None:
        # The same params rendering serves tool input.
        assert "**tool_tick:** ``run `x` now``" in md
        assert "**payload:**\n\n```\n---\nparam_key" in md

    def test_rule_unaffected(self, md: str) -> None:
        assert "> ---\n> \n> Between rules.\n> \n> ---\n> After rules." in md


class TestUnits:
    def test_split_matches_wenmode_rule(self) -> None:
        wen = Wenmode(plugins=[wen_frontmatter.configure(load=lambda s: s)])
        for text in (
            USER_DOC,
            INVALID_DOC,
            SCALAR_DOC,
            LATE_DOC,
            "---\r\na: 1\r\n---\r\nx",
            "--- \na: 1\n---  \nbody",
            "---\na: 1\n",
        ):
            data = wen.parse(text).data or {}
            split = split_frontmatter(text)
            assert (split is not None) == ("frontmatter" in data), text
            if split is not None:
                assert split[0] == data["frontmatter"]

    def test_rules_are_not_front_matter(self) -> None:
        assert split_frontmatter(RULE_DOC) is None
        assert split_frontmatter("---\n---\nx") is None
        assert split_frontmatter("\n---\na: 1\n---\n") is None

    def test_safe_loading(self) -> None:
        assert load_frontmatter("a: 1\nb: [x]\n") == {"a": 1, "b": ["x"]}
        unsafe = "a: !!python/object/apply:os.system ['true']\n"
        assert isinstance(load_frontmatter(unsafe), RawFrontmatter)
        assert isinstance(load_frontmatter("a: &x [1]\nb: *x\n"), RawFrontmatter)
        assert isinstance(load_frontmatter("- a\n- b\n"), RawFrontmatter)
        assert isinstance(load_frontmatter("{}\n"), RawFrontmatter)

    def test_bounds(self) -> None:
        nested = "".join("  " * i + f"k{i}:\n" for i in range(40)) + "  " * 40 + "x\n"
        assert isinstance(load_frontmatter(nested), RawFrontmatter)  # depth
        shallow = "".join("  " * i + f"k{i}:\n" for i in range(5)) + "  " * 5 + "x\n"
        assert isinstance(load_frontmatter(shallow), dict)
        big = "".join(f"key{i}: value\n" for i in range(7000))
        assert isinstance(load_frontmatter(big), RawFrontmatter)  # size


DEEP_FLOW = "---\na: " + "[" * 1000 + "]" * 1000 + "\n---\n\nAfter deep flow.\n"
# Under the size bound (one-space indents), yet deep enough that PyYAML's
# recursive composer raises RecursionError.
DEEP_BLOCK = (
    "---\n"
    + "".join(" " * i + f"k{i}:\n" for i in range(350))
    + " " * 350
    + "leaf\n---\n\nAfter deep block.\n"
)
# Over the size bound, but trivially valid YAML: shown raw, not loaded.
OVER_CAP = (
    "---\n"
    + "".join(f"key{i}: value {i}\n" for i in range(7000))
    + "---\n\nAfter big block.\n"
)


@pytest.mark.parametrize(
    ("doc", "after", "marker"),
    [
        (DEEP_FLOW, "After deep flow.", "[[[["),
        (DEEP_BLOCK, "After deep block.", "k349:"),
        (OVER_CAP, "After big block.", "key6999: value 6999"),
    ],
    ids=["deep-flow", "deep-block", "over-cap"],
)
class TestBounds:
    """A front matter past the bounds renders raw; the conversion goes on."""

    def _write_one(self, tmp_path: Path, doc: str) -> Path:
        path = tmp_path / "s1.jsonl"
        path.write_text(json.dumps(_user("u1", None, 0, doc)) + "\n", encoding="utf-8")
        return path

    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_html(
        self, tmp_path: Path, theme: str, doc: str, after: str, marker: str
    ) -> None:
        html = generate_html(
            load_transcript(self._write_one(tmp_path, doc)), "fm", theme=theme
        )
        assert after in html
        block = html[html.index("class='frontmatter'") : html.index(after)]
        assert "tool-params-table" not in block
        assert marker.split(":")[0] in block

    def test_markdown(self, tmp_path: Path, doc: str, after: str, marker: str) -> None:
        md = MarkdownRenderer().generate(
            load_transcript(self._write_one(tmp_path, doc)), "fm"
        )
        assert after in md
        assert "```yaml\n" in md
        assert marker in md


TEAMMATE_DOC = (
    "---\nname: handoff-note\nowner: indexer\n---\n"
    "# Handoff\n\nThe index is rebuilt; **review** the slow queries next.\n"
)


class TestTeammateBody:
    """A peer message body that opens with front matter is a document too."""

    def _write(self, tmp_path: Path) -> Path:
        text = (
            "Another Claude session sent a message:\n"
            f'<teammate-message teammate_id="indexer" color="blue">\n{TEAMMATE_DOC}'
            "</teammate-message>\n\nAfter the peer message."
        )
        path = tmp_path / "s1.jsonl"
        path.write_text(json.dumps(_user("u1", None, 0, text)) + "\n", encoding="utf-8")
        return path

    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_html(self, tmp_path: Path, theme: str) -> None:
        html = generate_html(load_transcript(self._write(tmp_path)), "fm", theme=theme)
        body = html.split('<div class="teammate-body">')[1].split("After the peer")[0]
        assert "class='frontmatter'" in body and "handoff-note</td>" in body
        assert "<h1>Handoff</h1>" in body and "<strong>review</strong>" in body
        assert "<h2>name:" not in html

    def test_markdown(self, tmp_path: Path) -> None:
        md = MarkdownRenderer().generate(load_transcript(self._write(tmp_path)), "fm")
        assert "> **name:** `handoff-note`" in md
        assert "> # Handoff" in md
        assert "> name: handoff-note" not in md

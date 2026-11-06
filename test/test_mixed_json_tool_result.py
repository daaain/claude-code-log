"""Mixed JSON/text tool results (issue #362).

A tool result that starts with one complete JSON document on its own
line(s) and continues with plain-text lines used to render as plain text,
because the JSON detection only fired when the *whole* result parsed. Now
the JSON head renders as JSON and the tail as text, in both renderers,
driven by one shared split (``utils.split_json_prefix``).

The split is deliberately narrow: the JSON document must end at a line
boundary. Text that merely starts with ``{`` (an invalid document, or a
document with trailing junk on its line) stays text.
"""

import json
import re
from typing import Any, Union

import pytest

from claude_code_log.html.tool_formatters import format_tool_result_content_raw
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    MessageMeta,
    ToolResultContent,
    ToolResultMessage,
)
from claude_code_log.renderer import TemplateMessage
from claude_code_log.utils import split_json_prefix

# One fixture for both renderers: the shape from issue #362 — an MCP tool
# answers with a one-line JSON verdict, then free-text sections.
JSON_HEAD = json.dumps(
    {
        "run": 4,
        "result": {"head": "the plain-click cases stand alone", "clean": ""},
        "calls": {"ok": {"Write": 1, "Bash": 9}},
    }
)
TEXT_TAIL = (
    "---- texts(4).target: 6 lines ----\n"
    "tests/test_glue.py::test_glue\n"
    "tests/test_view.py::test_view\n"
    "[gw1] PASSED tests/test_glue.py::test_glue\n"
    "[gw0] PASSED tests/test_view.py::test_view\n"
    "\n"
    "============ 2 passed in 5.90s ============"
)
MIXED = f"{JSON_HEAD}\n{TEXT_TAIL}"

# A pretty-printed head spans several lines before the text starts.
PRETTY_HEAD = json.dumps({"status": "ok", "items": [1, 2]}, indent=2)
MIXED_PRETTY = f"{PRETTY_HEAD}\n{TEXT_TAIL}"


def _result(
    content: Union[str, list[dict[str, Any]]], is_error: bool = False
) -> ToolResultContent:
    return ToolResultContent(
        type="tool_result", tool_use_id="tu", content=content, is_error=is_error
    )


def _md_message(output: ToolResultContent) -> TemplateMessage:
    meta = MessageMeta(
        uuid="u", session_id="s", timestamp="2025-01-01T00:00:00Z", is_sidechain=False
    )
    content = ToolResultMessage(
        meta=meta, output=output, tool_use_id="tu", tool_name="mcp__x__y"
    )
    return TemplateMessage(content)


class TestSplitJsonPrefix:
    """The shared definition of "mixed"."""

    def test_mixed_one_line_head(self):
        assert split_json_prefix(MIXED) == (JSON_HEAD, TEXT_TAIL)

    def test_mixed_multi_line_head(self):
        assert split_json_prefix(MIXED_PRETTY) == (PRETTY_HEAD, TEXT_TAIL)

    def test_pure_json_has_empty_tail(self):
        assert split_json_prefix(JSON_HEAD) == (JSON_HEAD, "")
        # Trailing blank lines are not a tail.
        assert split_json_prefix(JSON_HEAD + "\n\n  \n") == (JSON_HEAD, "")

    def test_leading_whitespace_tolerated(self):
        assert split_json_prefix("  \n" + MIXED) == (JSON_HEAD, TEXT_TAIL)

    def test_array_head(self):
        head = '[{"id": 1}, {"id": 2}]'
        assert split_json_prefix(f"{head}\nrest") == (head, "rest")

    @pytest.mark.parametrize(
        "content",
        [
            "plain output\nlines",
            '{"status": "ok", trailing\nmore',  # invalid head
            '{"a": 1} and then words\nmore',  # junk on the JSON's line
            '{"a": 1}{"b": 2}\nmore',  # two documents on one line
            "{not json}\nmore",
            "42\nmore",  # scalars are not a JSON head
            '"quoted"\nmore',
            "",
        ],
    )
    def test_not_mixed(self, content: str):
        assert split_json_prefix(content) is None

    def test_windows_newline_after_head(self):
        assert split_json_prefix(f"{JSON_HEAD}\r\nrest") == (JSON_HEAD, "rest")


class TestHtmlMixedResult:
    def test_json_head_renders_table_and_tail_renders_text(self):
        html = format_tool_result_content_raw(_result(MIXED))
        assert "tool-result-json" in html
        assert "tool-params-table" in html
        # The tail is there, as escaped text, after the table.
        assert "2 passed in 5.90s" in html
        assert html.index("tool-result-json") < html.index("2 passed in 5.90s")
        # The JSON head is not duplicated into the text part: the only
        # ``<pre>`` blocks are the tail (the table has none).
        pres = re.findall(r"<pre>(.*?)</pre>", html, flags=re.DOTALL)
        assert pres
        for pre in pres:
            assert pre.startswith("---- texts(4)")

    def test_multi_line_head(self):
        html = format_tool_result_content_raw(_result(MIXED_PRETTY))
        assert "tool-result-json" in html
        assert "2 passed in 5.90s" in html

    def test_long_tail_is_collapsible(self):
        html = format_tool_result_content_raw(
            _result(f"{JSON_HEAD}\n" + "line\n" * 100)
        )
        assert "tool-result-json" in html
        assert "collapsible-details" in html

    def test_list_content_text_item(self):
        """MCP results arrive as a list of text items — same rendering."""
        html = format_tool_result_content_raw(
            _result([{"type": "text", "text": MIXED}])
        )
        assert "tool-result-json" in html
        assert "2 passed in 5.90s" in html

    def test_tail_is_escaped(self):
        html = format_tool_result_content_raw(
            _result(f"{JSON_HEAD}\n<script>alert(1)</script>")
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_error_result_stays_text(self):
        html = format_tool_result_content_raw(_result(MIXED, is_error=True))
        assert "tool-result-json" not in html

    @pytest.mark.parametrize(
        "content",
        [
            '{"a": 1} and then words\nmore',
            '{"status": "ok", trailing\nmore',
            "{not json}\nmore",
        ],
    )
    def test_merely_starts_with_brace_stays_text(self, content: str):
        html = format_tool_result_content_raw(_result(content))
        assert "tool-result-json" not in html
        assert "<pre>" in html

    def test_empty_object_head_stays_text(self):
        """An empty container has no table to show: whole result as text."""
        html = format_tool_result_content_raw(_result("{}\nrest"))
        assert "tool-result-json" not in html
        assert "rest" in html


class TestMarkdownMixedResult:
    def setup_method(self):
        self.r = MarkdownRenderer()

    def _render(self, output: ToolResultContent) -> str:
        return self.r.format_ToolResultContent(output, _md_message(output))

    def test_json_head_fenced_as_json_tail_fenced_plain(self):
        out = self._render(_result(MIXED))
        assert out == f"```json\n{JSON_HEAD}\n```\n\n```\n{TEXT_TAIL}\n```"

    def test_multi_line_head(self):
        out = self._render(_result(MIXED_PRETTY))
        assert out == f"```json\n{PRETTY_HEAD}\n```\n\n```\n{TEXT_TAIL}\n```"

    def test_pure_json_fenced_as_json(self):
        out = self._render(_result(JSON_HEAD))
        assert out == f"```json\n{JSON_HEAD}\n```"

    def test_list_content_text_item(self):
        out = self._render(_result([{"type": "text", "text": MIXED}]))
        assert out == f"```json\n{JSON_HEAD}\n```\n\n```\n{TEXT_TAIL}\n```"

    def test_error_result_stays_plain_fence(self):
        out = self._render(_result(MIXED, is_error=True))
        assert out == f"```\n{MIXED}\n```"

    @pytest.mark.parametrize(
        "content",
        [
            '{"a": 1} and then words\nmore',
            '{"status": "ok", trailing\nmore',
            "{not json}\nmore",
        ],
    )
    def test_merely_starts_with_brace_stays_plain_fence(self, content: str):
        out = self._render(_result(content))
        assert out == f"```\n{content}\n```"

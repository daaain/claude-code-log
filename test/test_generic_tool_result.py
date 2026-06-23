"""Tests for generic (fallback) tool-result rendering of structured content.

For a tool without a specialized output parser (e.g. ToolSearch), the
result ``content`` can be a list of typed items other than ``text`` /
``image`` — notably ``tool_reference`` blocks. These used to be silently
dropped, leaving the result side blank (issue #227). Both the HTML and
Markdown fallbacks must now surface them.
"""

from typing import Any, Union

from claude_code_log.html.tool_formatters import format_tool_result_content_raw
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    MessageMeta,
    ToolResultContent,
    ToolResultMessage,
)
from claude_code_log.renderer import TemplateMessage


def _result(
    content: Union[str, list[dict[str, Any]]], is_error: bool = False
) -> ToolResultContent:
    return ToolResultContent(
        type="tool_result", tool_use_id="tu", content=content, is_error=is_error
    )


def _md_message(
    output: ToolResultContent, tool_name: str = "ToolSearch"
) -> TemplateMessage:
    meta = MessageMeta(
        uuid="u", session_id="s", timestamp="2025-01-01T00:00:00Z", is_sidechain=False
    )
    content = ToolResultMessage(
        meta=meta,
        output=output,
        tool_use_id="tu",
        tool_name=tool_name,
    )
    return TemplateMessage(content)


# A real ToolSearch result shape (issue #227).
_TOOL_REFERENCE = {
    "type": "tool_reference",
    "tool_name": "mcp__plugin_clmail__terminal",
}


class TestHtmlStructuredContent:
    def test_tool_reference_is_not_dropped(self):
        """The bug: a tool_reference-only result rendered nothing."""
        html = format_tool_result_content_raw(_result([_TOOL_REFERENCE]))
        assert html.strip()
        assert "tool-result-json" in html
        assert "mcp__plugin_clmail__terminal" in html
        # Not an empty <pre>.
        assert html.strip() != "<pre></pre>"

    def test_multiple_tool_references(self):
        html = format_tool_result_content_raw(
            _result(
                [
                    {"type": "tool_reference", "tool_name": "WebSearch"},
                    {"type": "tool_reference", "tool_name": "WebFetch"},
                ]
            )
        )
        assert "WebSearch" in html
        assert "WebFetch" in html

    def test_text_and_structured_items_both_render(self):
        html = format_tool_result_content_raw(
            _result(
                [
                    {"type": "text", "text": "some text result"},
                    _TOOL_REFERENCE,
                ]
            )
        )
        assert "some text result" in html
        assert "mcp__plugin_clmail__terminal" in html

    def test_structured_items_escape_html(self):
        """Generated table must escape, never inject raw HTML."""
        html = format_tool_result_content_raw(
            _result([{"type": "tool_reference", "tool_name": "<script>x</script>"}])
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_is_error_structured_still_renders_table(self):
        """A typed-item result still renders the table when is_error.

        Decision (cboos): tool_reference (and other typed) items are not
        error *message text*, so they keep the structured table rendering —
        the is_error->read-as-text guard only covers the JSON-string path.
        Pins the ordering so a future refactor can't move the guard ahead
        of the structured path and silently swallow these.
        """
        html = format_tool_result_content_raw(_result([_TOOL_REFERENCE], is_error=True))
        assert "tool-result-json" in html
        assert "mcp__plugin_clmail__terminal" in html

    def test_string_content_unchanged(self):
        """Plain string results keep the legacy <pre> rendering."""
        html = format_tool_result_content_raw(_result("hello"))
        assert html.strip() == "<pre>hello</pre>"

    def test_text_only_list_unchanged(self):
        html = format_tool_result_content_raw(
            _result([{"type": "text", "text": "plain"}])
        )
        assert html.strip() == "<pre>plain</pre>"

    def test_empty_list_does_not_crash(self):
        html = format_tool_result_content_raw(_result([]))
        # No structured items, no text → empty <pre>, but no exception.
        assert "<pre>" in html


class TestMarkdownStructuredContent:
    def setup_method(self):
        self.r = MarkdownRenderer()

    def test_tool_reference_renders_json_block(self):
        out = self.r.format_ToolResultContent(
            _result([_TOOL_REFERENCE]), _md_message(_result([_TOOL_REFERENCE]))
        )
        assert out.strip()
        assert "mcp__plugin_clmail__terminal" in out
        assert "```json" in out

    def test_text_item_renders_as_text(self):
        output = _result([{"type": "text", "text": "hi there"}, _TOOL_REFERENCE])
        out = self.r.format_ToolResultContent(output, _md_message(output))
        assert "hi there" in out
        # text is not embedded inside the json block
        assert out.index("hi there") < out.index("```json")
        assert "mcp__plugin_clmail__terminal" in out

    def test_is_error_structured_renders_json_block(self):
        """Parity with HTML: typed items render even when is_error."""
        output = _result([_TOOL_REFERENCE], is_error=True)
        out = self.r.format_ToolResultContent(output, _md_message(output))
        assert "```json" in out
        assert "mcp__plugin_clmail__terminal" in out

    def test_string_content_unchanged(self):
        output = _result("plain string")
        out = self.r.format_ToolResultContent(output, _md_message(output))
        assert "plain string" in out

    def test_todowrite_special_case_unchanged(self):
        output = _result("Todos updated")
        out = self.r.format_ToolResultContent(
            output, _md_message(output, tool_name="TodoWrite")
        )
        assert out == "Todos updated"

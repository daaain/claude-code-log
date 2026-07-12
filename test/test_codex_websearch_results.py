"""Codex WebSearch input and result rendering adaptation."""

from typing import Any

from claude_code_log.factories.tool_factory import create_tool_output
from claude_code_log.html.tool_formatters import format_websearch_input
from claude_code_log.models import ToolResultContent, WebSearchInput, WebSearchOutput
from claude_code_log.providers.codex import CodexProvider


class TestProvider(CodexProvider):
    __test__ = False

    def normalize_tool_output(
        self, value: object, tool_name: str
    ) -> str | list[dict[str, Any]]:
        return self._tool_output(value, tool_name=tool_name)


def _direct_result(output: str) -> list[dict[str, str]]:
    return [
        {
            "type": "input_text",
            "text": "Script completed\nWall time 1.5 seconds\nOutput:\n",
        },
        {"type": "input_text", "text": output},
    ]


def test_query_is_shown_only_in_websearch_title() -> None:
    query = "site:developers.openai.com/codex " * 10
    assert format_websearch_input(WebSearchInput(query=query)) == ""


def test_codex_websearch_result_is_treated_as_markdown_summary() -> None:
    provider = TestProvider()
    markdown = "## Result\n\n- [Synthetic](https://example.invalid/result)"
    normalized = provider.normalize_tool_output(_direct_result(markdown), "WebSearch")
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-search",
        content=normalized,
    )

    output = create_tool_output("WebSearch", raw)

    assert normalized == markdown
    assert isinstance(output, WebSearchOutput)
    assert output.summary == markdown


def test_websearch_errors_keep_generic_result_rendering() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-search",
        content="Search failed",
        is_error=True,
    )

    assert create_tool_output("WebSearch", raw) is raw

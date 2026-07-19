"""Codex WebSearch input and result rendering adaptation."""

from typing import Any

from claude_code_log.factories.tool_factory import create_tool_output
from claude_code_log.html.tool_formatters import (
    format_webfetch_output,
    format_websearch_input,
    format_websearch_output,
)
from claude_code_log.models import (
    ToolResultContent,
    WebFetchOutput,
    WebSearchInput,
    WebSearchOutput,
)
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


def test_long_query_retains_shared_claude_websearch_body() -> None:
    query = "site:developers.openai.com/codex " * 10
    assert format_websearch_input(WebSearchInput(query=query)) == (
        f'<div class="websearch-query">{query}</div>'
    )


def test_codex_websearch_result_is_treated_as_markdown_summary() -> None:
    provider = TestProvider()
    markdown = "## Result\n\n- [Synthetic](https://example.invalid/result)"
    normalized, structured = provider._adapt_tool_result(
        _direct_result(markdown), tool_name="WebSearch", is_error=False
    )
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-search",
        content=normalized,
    )

    output = create_tool_output("WebSearch", raw, tool_use_result=structured)

    assert normalized == markdown
    assert isinstance(output, WebSearchOutput)
    assert output.summary == markdown


def _serialized_web_result() -> str:
    return """Codex App Server
 (https://developers.openai.com/codex/app-server/)
citeturn12view0 [wordlim: 200] Content type: text/html; Source: open({"ref_id":"https://developers.openai.com/codex/app-server/#threads"}); Redirected to URL: https://learn.chatgpt.com/docs/app-server; Total lines: 2345
L73:   * cite29† Overview  L74:   * cite40† Quickstart 
--------------------------------------------------------------------------------
Codex use cases (https://developers.openai.com/codex/use-cases)
citeturn12search1 [wordlim: 200] Crawled: 3 days ago; A useful summary.
"""


def _assert_normalized_web_result(normalized: str) -> None:
    assert (
        "## [Codex App Server](https://developers.openai.com/codex/app-server/)"
        in normalized
    )
    assert (
        "> Fetched text/html · [canonical source](https://learn.chatgpt.com/docs/app-server) · 2,345 lines"
        in normalized
    )
    assert "  * Overview\n  * Quickstart" in normalized
    assert "> Crawled 3 days ago\n\nA useful summary." in normalized
    assert "cite" not in normalized
    assert "[wordlim:" not in normalized


def test_codex_websearch_serialization_becomes_readable_markdown() -> None:
    normalized, structured = TestProvider()._adapt_tool_result(
        _direct_result(_serialized_web_result()),
        tool_name="WebSearch",
        is_error=False,
    )
    raw = ToolResultContent(
        type="tool_result", tool_use_id="call-search", content=normalized
    )
    output = create_tool_output("WebSearch", raw, tool_use_result=structured)

    assert isinstance(normalized, str)
    assert isinstance(output, WebSearchOutput)
    assert output.source_refs == ["turn12view0", "turn12search1"]
    _assert_normalized_web_result(normalized)
    rendered = format_websearch_output(output)
    assert "id='web-ref-turn12view0'" in rendered
    assert "id='web-ref-turn12search1'" in rendered


def test_codex_webfetch_uses_the_same_result_normalizer() -> None:
    normalized, structured = TestProvider()._adapt_tool_result(
        _direct_result(_serialized_web_result()),
        tool_name="WebFetch",
        is_error=False,
    )
    raw = ToolResultContent(
        type="tool_result", tool_use_id="call-find", content=normalized
    )
    output = create_tool_output("WebFetch", raw, tool_use_result=structured)

    assert isinstance(normalized, str)
    assert isinstance(output, WebFetchOutput)
    assert output.source_refs == ["turn12view0", "turn12search1"]
    _assert_normalized_web_result(normalized)
    rendered = format_webfetch_output(output)
    assert "id='web-ref-turn12view0'" in rendered
    assert "id='web-ref-turn12search1'" in rendered


def test_websearch_errors_keep_generic_result_rendering() -> None:
    raw = ToolResultContent(
        type="tool_result",
        tool_use_id="call-search",
        content="Search failed",
        is_error=True,
    )

    assert create_tool_output("WebSearch", raw) is raw

    normalized, structured = TestProvider()._adapt_tool_result(
        "Search failed", tool_name="WebSearch", is_error=True
    )
    assert normalized == "Search failed"
    assert structured is None

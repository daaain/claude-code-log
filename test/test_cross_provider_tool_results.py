"""Provider-neutral tool result contracts shared by all transcript sources."""

from claude_code_log.factories.tool_factory import create_tool_output
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    MessageMeta,
    TaskOutput,
    ToolResultContent,
    ToolResultMessage,
    WebSearchOutput,
)
from claude_code_log.renderer import TemplateMessage


def _result(
    content: str | list[dict[str, str]], *, is_error: bool = False
) -> ToolResultContent:
    return ToolResultContent(
        type="tool_result",
        tool_use_id="tool-use",
        content=content,
        is_error=is_error,
    )


def _message(output: ToolResultContent, tool_name: str) -> TemplateMessage:
    meta = MessageMeta(
        uuid="entry", session_id="session", timestamp="2026-07-14T00:00:00Z"
    )
    return TemplateMessage(
        ToolResultMessage(
            meta=meta,
            output=output,
            tool_use_id=output.tool_use_id,
            tool_name=tool_name,
        )
    )


def test_task_json_with_task_name_remains_literal_shared_content() -> None:
    content = '{"task_name":"analysis","status":"complete"}'
    output = create_tool_output("Task", _result(content))

    assert isinstance(output, TaskOutput)
    assert output.result == content


def test_task_error_report_remains_literal_and_error_flag_is_preserved() -> None:
    raw = _result('{"task_name":"analysis","error":"failed"}', is_error=True)
    output = create_tool_output("Task", raw)

    assert isinstance(output, TaskOutput)
    assert output.result == raw.content
    assert raw.is_error is True


def test_normal_task_markdown_still_uses_shared_task_renderer() -> None:
    markdown = "## Report\n\nShared task result."
    output = create_tool_output("Task", _result(markdown))

    assert isinstance(output, TaskOutput)
    assert output.result == markdown


def test_plain_markdown_websearch_result_stays_generic_in_both_renderers() -> None:
    markdown = "## Provider text\n\n- not a recognized Claude search envelope"
    raw = _result(markdown)
    output = create_tool_output("WebSearch", raw)

    assert output is raw
    message = _message(raw, "WebSearch")
    html = HtmlRenderer().format_ToolResultContent(raw, message)
    rendered_markdown = MarkdownRenderer().format_ToolResultContent(raw, message)
    assert "<pre>" in html and "Provider text" in html
    assert rendered_markdown.startswith("```")
    assert "Provider text" in rendered_markdown


def test_websearch_error_stays_generic() -> None:
    raw = _result("Search failed", is_error=True)
    assert create_tool_output("WebSearch", raw) is raw


def test_structured_claude_websearch_result_remains_specialized() -> None:
    raw = _result("Search completed")
    structured = {
        "query": "shared query",
        "results": [
            {"content": [{"title": "Result", "url": "https://example.invalid/result"}]},
            "Shared summary.",
        ],
    }

    output = create_tool_output("WebSearch", raw, tool_use_result=structured)

    assert isinstance(output, WebSearchOutput)
    assert output.query == "shared query"
    assert output.summary == "Shared summary."


def test_codex_todo_transport_is_not_a_shared_factory_shape() -> None:
    raw = _result(
        [
            {
                "type": "input_text",
                "text": "Script completed\nWall time: 0.0 seconds\nOutput:\n",
            },
            {"type": "input_text", "text": "{}"},
        ]
    )

    assert create_tool_output("TodoWrite", raw) is raw

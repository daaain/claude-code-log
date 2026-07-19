"""Built-in rendering for Codex OpenAI Developer Docs calls."""

from claude_code_log.builtin_plugins.codex_docs import (
    CodexDocInputMessage,
    CodexDocResultMessage,
)
from claude_code_log.factories.tool_factory import (
    create_tool_result_message,
    create_tool_use_message,
)
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    MessageMeta,
    ToolResultContent,
    ToolUseContent,
    ToolUseMessage,
)
from claude_code_log.plugins import reset_cache
from claude_code_log.renderer import TemplateMessage


def test_builtin_codex_doc_pair_renders_link_and_collapsible_markdown() -> None:
    reset_cache()
    meta = MessageMeta.empty()
    context: dict[str, ToolUseContent] = {}
    tool_use = ToolUseContent(
        type="tool_use",
        id="docs:batch:0",
        name="CodexDoc",
        input={
            "url": "https://learn.chatgpt.com/docs/hooks",
            "anchor": "#config-shape",
        },
    )
    use = create_tool_use_message(meta, tool_use, context).content
    result = create_tool_result_message(
        meta,
        ToolResultContent(
            type="tool_result",
            tool_use_id=tool_use.id,
            content="# Config shape\n\n" + "Documentation line.\n" * 25,
        ),
        context,
    ).content

    assert isinstance(use, CodexDocInputMessage)
    assert isinstance(result, CodexDocResultMessage)

    html = HtmlRenderer(image_export_mode="placeholder")
    use_html = html.format_content(TemplateMessage(use))
    result_html = html.format_content(TemplateMessage(result))
    assert "https://learn.chatgpt.com/docs/hooks" in use_html
    assert "#config-shape" in use_html
    assert "<details" in result_html
    assert "<h1>Config shape</h1>" in result_html

    markdown = MarkdownRenderer()
    assert "docs/hooks#config-shape" in markdown.format_content(TemplateMessage(use))
    assert markdown.format_content(TemplateMessage(result)).startswith("# Config shape")
    reset_cache()


def test_builtin_doc_transformer_does_not_claim_raw_mcp_name() -> None:
    reset_cache()
    raw_name = "mcp__openaiDeveloperDocs__fetch_openai_doc"
    tool_use = ToolUseContent(
        type="tool_use",
        id="raw-doc",
        name=raw_name,
        input={"url": "https://learn.chatgpt.com/docs/hooks"},
    )

    content = create_tool_use_message(MessageMeta.empty(), tool_use, {}).content

    assert type(content) is ToolUseMessage
    assert isinstance(content, ToolUseMessage)
    assert content.tool_name == raw_name
    reset_cache()

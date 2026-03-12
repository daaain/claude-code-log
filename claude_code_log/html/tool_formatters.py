"""HTML rendering functions for tool use and tool result content.

This module contains all HTML formatters for specific tools:
- AskUserQuestion tool (input + result)
- ExitPlanMode tool (input + result)
- TodoWrite tool
- Read/Write/Edit/Multiedit tools
- Bash tool
- Task tool
- Generic parameter table rendering
- Tool use content dispatcher

These formatters take tool-specific input/output data and generate
HTML for display in transcripts.
"""

import base64
import binascii
import json
import re
from typing import Any, cast

from .utils import (
    escape_html,
    render_file_content_collapsible,
    render_markdown_collapsible,
)
from ..utils import strip_error_tags
from ..models import (
    AskUserQuestionInput,
    AskUserQuestionItem,
    AskUserQuestionOutput,
    BashInput,
    BashOutput,
    EditInput,
    EditOutput,
    ExitPlanModeInput,
    ExitPlanModeOutput,
    GrepInput,
    MultiEditInput,
    ReadInput,
    ReadOutput,
    TaskInput,
    TaskOutput,
    TodoWriteInput,
    ToolResultContent,
    WebSearchInput,
    WebSearchOutput,
    WebFetchInput,
    WebFetchOutput,
    WriteInput,
    WriteOutput,
)
from .ansi_colors import convert_ansi_to_html
from .renderer_code import render_single_diff


# -- AskUserQuestion Tool -----------------------------------------------------


def _render_question_item(q: AskUserQuestionItem) -> str:
    """Render a single question item to HTML."""
    html_parts: list[str] = ['<div class="question-block">']

    # Header (if present)
    if q.header:
        escaped_header = escape_html(q.header)
        html_parts.append(f'<div class="question-header">{escaped_header}</div>')

    # Question text with Q: label
    question_text = escape_html(q.question)
    html_parts.append(
        f'<div class="question-text"><span class="qa-label">Q:</span> {question_text}</div>'
    )

    # Options (if present)
    if q.options:
        select_hint = "(select multiple)" if q.multiSelect else "(select one)"
        html_parts.append(f'<div class="question-options-hint">{select_hint}</div>')
        html_parts.append('<ul class="question-options">')
        for opt in q.options:
            label = escape_html(opt.label)
            if opt.description:
                desc_html = f'<span class="option-desc"> — {escape_html(opt.description)}</span>'
            else:
                desc_html = ""
            html_parts.append(
                f'<li class="question-option"><strong>{label}</strong>{desc_html}</li>'
            )
        html_parts.append("</ul>")

    html_parts.append("</div>")  # Close question-block
    return "".join(html_parts)


def format_askuserquestion_input(ask_input: AskUserQuestionInput) -> str:
    """Format AskUserQuestion tool use content with prominent question display.

    Args:
        ask_input: Typed AskUserQuestionInput with questions list and/or single question.

    Handles multiple questions in a single tool use, each with optional header,
    options (with label and description), and multiSelect flag.
    """
    # Build list of questions from both formats
    questions: list[AskUserQuestionItem] = list(ask_input.questions)

    # Handle single question format (legacy)
    if not questions and ask_input.question:
        questions.append(AskUserQuestionItem(question=ask_input.question))

    if not questions:
        return '<div class="askuserquestion-content"><em>No question</em></div>'

    # Build HTML for all questions
    html_parts: list[str] = ['<div class="askuserquestion-content">']
    for q in questions:
        html_parts.append(_render_question_item(q))
    html_parts.append("</div>")  # Close askuserquestion-content
    return "".join(html_parts)


def format_askuserquestion_result(content: str) -> str:
    """Format AskUserQuestion tool result with styled question/answer pairs.

    Parses the result format:
    'User has answered your questions: "Q1"="A1", "Q2"="A2". You can now continue...'

    Returns HTML with styled Q&A blocks matching the input styling.
    """
    # Check if this is a successful answer
    if not content.startswith("User has answered your question"):
        # Return as-is for errors or unexpected format
        return ""

    # Extract the Q&A portion between the colon and the final sentence
    # Pattern: 'User has answered your questions: "Q"="A", "Q"="A". You can now...'
    match = re.match(
        r"User has answered your questions?: (.+)\. You can now continue",
        content,
        re.DOTALL,
    )
    if not match:
        return ""

    qa_portion = match.group(1)

    # Parse "Question"="Answer" pairs
    # Pattern: "question text"="answer text"
    qa_pattern = re.compile(r'"([^"]+)"="([^"]+)"')
    pairs = qa_pattern.findall(qa_portion)

    if not pairs:
        return ""

    # Build styled HTML
    html_parts: list[str] = [
        '<div class="askuserquestion-content askuserquestion-result">'
    ]

    for question, answer in pairs:
        escaped_q = escape_html(question)
        escaped_a = escape_html(answer)
        html_parts.append('<div class="question-block answered">')
        html_parts.append(
            f'<div class="question-text"><span class="qa-label">Q:</span> {escaped_q}</div>'
        )
        html_parts.append(
            f'<div class="answer-text"><span class="qa-label answer">A:</span> {escaped_a}</div>'
        )
        html_parts.append("</div>")

    html_parts.append("</div>")
    return "".join(html_parts)


# -- ExitPlanMode Tool --------------------------------------------------------


def format_exitplanmode_input(exit_input: ExitPlanModeInput) -> str:
    """Format ExitPlanMode tool use content with collapsible plan markdown.

    Args:
        exit_input: Typed ExitPlanModeInput with plan content.

    Renders the plan markdown in a collapsible section, similar to Task tool results.
    """
    if not exit_input.plan:
        return '<div class="plan-content"><em>No plan</em></div>'

    return render_markdown_collapsible(exit_input.plan, "plan-content")


def format_exitplanmode_result(content: str) -> str:
    """Format ExitPlanMode tool result, truncating the redundant plan echo.

    When a plan is approved, the result contains:
    1. A confirmation message
    2. Path to saved plan file
    3. "## Approved Plan:" followed by full plan text (redundant)

    We truncate everything after "## Approved Plan:" to avoid duplication.
    For error results (plan not approved), we keep the full content.
    """
    # Check if this is a successful approval
    if "User has approved your plan" in content:
        # Truncate at "## Approved Plan:"
        marker = "## Approved Plan:"
        marker_pos = content.find(marker)
        if marker_pos > 0:
            # Keep everything before the marker, strip trailing whitespace
            return content[:marker_pos].rstrip()

    # For errors or other cases, return as-is
    return content


# -- WebSearch Tool -----------------------------------------------------------


def format_websearch_input(search_input: WebSearchInput) -> str:
    """Format WebSearch tool use content showing the search query.

    Args:
        search_input: Typed WebSearchInput with query parameter.

    Only shows the query if it exceeds 100 chars (truncated in title).
    Otherwise returns empty since the full query is already in the title.
    """
    if len(search_input.query) <= 100:
        return ""  # Full query shown in title
    escaped_query = escape_html(search_input.query)
    return f'<div class="websearch-query">{escaped_query}</div>'


def _websearch_as_markdown(output: WebSearchOutput) -> str:
    """Convert WebSearch output to markdown: summary, then links at bottom."""
    parts: list[str] = []

    # Summary first (the analysis text)
    if output.summary:
        parts.append(output.summary)

    # Links at the bottom after a separator
    if output.links:
        if parts:
            parts.append("")  # Blank line before separator
            parts.append("---")
            parts.append("")  # Blank line after separator
        for link in output.links:
            parts.append(f"- [{link.title}]({link.url})")
    elif not output.summary:
        # Only show "no results" if there's also no summary
        parts.append("*No results found*")

    return "\n".join(parts)


def format_websearch_output(output: WebSearchOutput) -> str:
    """Format WebSearch tool result as collapsible markdown.

    Args:
        output: Parsed WebSearchOutput with preamble, links, and summary.

    Combines preamble + links as markdown list + summary into a single
    markdown block, rendered as collapsible content.
    """
    markdown_content = _websearch_as_markdown(output)
    return render_markdown_collapsible(markdown_content, "websearch-results")


# -- Grep Tool ----------------------------------------------------------------


def format_grep_input(grep_input: GrepInput) -> str:
    """Format Grep tool use content showing search parameters.

    Args:
        grep_input: Typed GrepInput with pattern, path, glob, type, etc.

    Each field is shown on its own line with a consistent label: value format.
    """
    html_parts = ["<div class='grep-tool-content'>"]
    html_parts.append(f"<div class='grep-tool-field'><span class='grep-tool-label'>pattern:</span> <code>{escape_html(grep_input.pattern)}</code></div>")
    if grep_input.path:
        html_parts.append(f"<div class='grep-tool-field'><span class='grep-tool-label'>path:</span> <code>{escape_html(grep_input.path)}</code></div>")
    if grep_input.glob:
        html_parts.append(f"<div class='grep-tool-field'><span class='grep-tool-label'>glob:</span> <code>{escape_html(grep_input.glob)}</code></div>")
    if grep_input.type:
        html_parts.append(f"<div class='grep-tool-field'><span class='grep-tool-label'>type:</span> <code>{escape_html(grep_input.type)}</code></div>")
    if grep_input.output_mode:
        html_parts.append(f"<div class='grep-tool-field'><span class='grep-tool-label'>mode:</span> <code>{escape_html(grep_input.output_mode)}</code></div>")
    if grep_input.multiline:
        html_parts.append(f"<div class='grep-tool-field'><span class='grep-tool-label'>multiline:</span> <code>true</code></div>")
    html_parts.append("</div>")
    return "".join(html_parts)


# -- TodoWrite Tool -----------------------------------------------------------


def format_todowrite_input(todo_input: TodoWriteInput) -> str:
    """Format TodoWrite tool use content as a todo list.

    Args:
        todo_input: Typed TodoWriteInput with list of todo items.
    """
    if not todo_input.todos:
        return """
        <div class="todo-content">
            <p><em>No todos found</em></p>
        </div>
        """

    # Status emojis
    status_emojis = {"pending": "⏳", "in_progress": "🔄", "completed": "✅"}

    # Build todo list HTML - todos are typed TodoWriteItem objects
    todo_items: list[str] = []
    for todo in todo_input.todos:
        todo_id = escape_html(todo.id) if todo.id else ""
        content = escape_html(todo.content) if todo.content else ""
        status = todo.status or "pending"
        priority = todo.priority or "medium"
        status_emoji = status_emojis.get(status, "⏳")

        # CSS class for styling
        item_class = f"todo-item {status} {priority}"

        id_html = f'<span class="todo-id">#{todo_id}</span>' if todo.id else ""
        todo_items.append(f"""
            <div class="{item_class}">
                <span class="todo-status">{status_emoji}</span>
                <span class="todo-content">{content}</span>
                {id_html}
            </div>
        """)

    todos_html = "".join(todo_items)

    return f"""
    <div class="todo-list">
        {todos_html}
    </div>
    """


# -- File Tools (Read/Write) --------------------------------------------------


def format_read_input(read_input: ReadInput) -> str:  # noqa: ARG001
    """Format Read tool use content showing file path.

    Args:
        read_input: Typed ReadInput with file_path, offset, and limit.

    Note: File path is now shown in the header, so we skip content here.
    """
    # File path is now shown in header, so no content needed
    # Don't show offset/limit parameters as they'll be visible in the result
    return ""


# -- Tool Result Formatting ---------------------------------------------------
# Parsing (parse_read_output, parse_edit_output) is now in factories/tool_factory.py


def format_read_output(output: ReadOutput) -> str:
    """Format Read tool result as HTML with syntax highlighting.

    Args:
        output: Parsed ReadOutput

    Returns:
        HTML string with syntax-highlighted, collapsible file content
    """
    # Build system reminder suffix if present
    suffix_html = ""
    if output.system_reminder:
        escaped_reminder = escape_html(output.system_reminder)
        suffix_html = (
            f"<div class='system-reminder'>🤖 <em>{escaped_reminder}</em></div>"
        )

    return render_file_content_collapsible(
        output.content,
        output.file_path,
        "read-tool-result",
        linenostart=output.start_line,
        suffix_html=suffix_html,
    )


def format_edit_output(output: EditOutput) -> str:
    """Format Edit tool result as HTML with syntax highlighting.

    Args:
        output: Parsed EditOutput

    Returns:
        HTML string with syntax-highlighted, collapsible file content
    """
    return render_file_content_collapsible(
        output.message,  # message contains the code snippet
        output.file_path,
        "edit-tool-result",
        linenostart=output.start_line,
    )


def format_write_output(output: WriteOutput) -> str:
    """Format Write tool result as HTML.

    Args:
        output: Parsed WriteOutput with first line acknowledgment

    Returns:
        HTML string with the acknowledgment message
    """
    escaped_message = escape_html(output.message)
    return f"<pre>{escaped_message} ...</pre>"


def format_bash_output(output: BashOutput) -> str:
    """Format Bash tool result as HTML with ANSI color support.

    Args:
        output: Parsed BashOutput with content and ANSI flag

    Returns:
        HTML string with ANSI colors converted or plain text
    """
    content = output.content
    if output.has_ansi:
        full_html = convert_ansi_to_html(content)
    else:
        full_html = escape_html(content)

    # For short content, show directly
    if len(content) <= 200:
        return f"<pre>{full_html}</pre>"

    # For longer content, use collapsible details
    preview_html = escape_html(content[:200]) + "..."
    return f"""
    <details class="collapsible-details">
        <summary>
            <div class="preview-content"><pre>{preview_html}</pre></div>
        </summary>
        <div class="details-content">
            <pre>{full_html}</pre>
        </div>
    </details>
    """


def format_task_output(output: TaskOutput) -> str:
    """Format Task tool result as HTML with markdown rendering.

    Args:
        output: Parsed TaskOutput with agent's response

    Returns:
        HTML string with markdown rendered in collapsible section
    """
    return render_markdown_collapsible(output.result, "task-result")


def format_askuserquestion_output(output: AskUserQuestionOutput) -> str:
    """Format AskUserQuestion tool result with styled Q&A pairs.

    Args:
        output: Parsed AskUserQuestionOutput with Q&A pairs

    Returns:
        HTML string with styled question/answer blocks
    """
    html_parts: list[str] = [
        '<div class="askuserquestion-content askuserquestion-result">'
    ]

    for qa in output.answers:
        escaped_q = escape_html(qa.question)
        escaped_a = escape_html(qa.answer)
        html_parts.append('<div class="question-block answered">')
        html_parts.append(
            f'<div class="question-text"><span class="qa-label">Q:</span> {escaped_q}</div>'
        )
        html_parts.append(
            f'<div class="answer-text"><span class="qa-label answer">A:</span> {escaped_a}</div>'
        )
        html_parts.append("</div>")

    html_parts.append("</div>")
    return "".join(html_parts)


def format_exitplanmode_output(output: ExitPlanModeOutput) -> str:
    """Format ExitPlanMode tool result as HTML.

    Args:
        output: Parsed ExitPlanModeOutput with truncated message

    Returns:
        HTML string with the (truncated) result message
    """
    escaped_content = escape_html(output.message)
    return f"<pre>{escaped_content}</pre>"


def format_write_input(write_input: WriteInput) -> str:
    """Format Write tool use content with Pygments syntax highlighting.

    Args:
        write_input: Typed WriteInput with file_path and content.
    Note: File path is now shown in the header, so we skip it here.
    """
    return render_file_content_collapsible(
        write_input.content, write_input.file_path, "write-tool-content"
    )


# -- Edit Tools (Edit/Multiedit) ----------------------------------------------


def format_edit_input(edit_input: EditInput) -> str:
    """Format Edit tool use content as a diff view with intra-line highlighting.

    Args:
        edit_input: Typed EditInput with old_string, new_string, replace_all.
    Note: File path is now shown in the header, so we skip it here.
    """
    html_parts = ["<div class='edit-tool-content'>"]

    if edit_input.replace_all:
        html_parts.append(
            "<div class='edit-replace-all'>🔄 Replace all occurrences</div>"
        )

    # Use shared diff rendering helper
    html_parts.append(render_single_diff(edit_input.old_string, edit_input.new_string))
    html_parts.append("</div>")

    return "".join(html_parts)


def format_multiedit_input(multiedit_input: MultiEditInput) -> str:
    """Format Multiedit tool use content showing multiple diffs.

    Args:
        multiedit_input: Typed MultiEditInput with file_path and list of edits.
    """
    escaped_path = escape_html(multiedit_input.file_path)

    html_parts = ["<div class='multiedit-tool-content'>"]

    # File path header
    html_parts.append(f"<div class='multiedit-file-path'>📝 {escaped_path}</div>")
    html_parts.append(
        f"<div class='multiedit-count'>Applying {len(multiedit_input.edits)} edits</div>"
    )

    # Render each edit as a diff - edits are typed EditItem objects
    for idx, edit in enumerate(multiedit_input.edits, 1):
        html_parts.append(
            f"<div class='multiedit-item'><div class='multiedit-item-header'>Edit #{idx}</div>"
        )
        html_parts.append(render_single_diff(edit.old_string, edit.new_string))
        html_parts.append("</div>")

    html_parts.append("</div>")
    return "".join(html_parts)


# -- Bash Tool ----------------------------------------------------------------


def format_bash_input(bash_input: BashInput) -> str:
    """Format Bash tool use content in VS Code extension style.

    Args:
        bash_input: Typed BashInput with command, description, timeout, etc.
    Note: Description is now shown in the header, so we skip it here.
    """
    escaped_command = escape_html(bash_input.command)

    html_parts = ["<div class='bash-tool-content'>"]
    html_parts.append(f"<pre class='bash-tool-command'>{escaped_command}</pre>")
    html_parts.append("</div>")

    return "".join(html_parts)


# -- Task Tool ----------------------------------------------------------------


def format_task_input(task_input: TaskInput) -> str:
    """Format Task tool content with markdown-rendered prompt.

    Args:
        task_input: Typed TaskInput with prompt, subagent_type, etc.

    Task tool spawns sub-agents. We render the prompt as the main content.
    The sidechain user message (which would duplicate this prompt) is skipped.

    For long prompts (>20 lines), the content is made collapsible with a
    preview of the first few lines to keep the transcript vertically compact.
    """
    return render_markdown_collapsible(task_input.prompt, "task-prompt")


# -- WebFetch Tool ------------------------------------------------------------


def format_webfetch_input(webfetch_input: WebFetchInput) -> str:
    """Format WebFetch tool use content.

    Args:
        webfetch_input: Typed WebFetchInput with url and prompt.

    The URL is shown in the title, so we only show the prompt here if it's
    substantial enough to warrant display.
    """
    # If prompt is short, it can fit in the title - return empty
    if len(webfetch_input.prompt) <= 100:
        return ""

    # Show the prompt for longer queries
    escaped_prompt = escape_html(webfetch_input.prompt)
    return f'<div class="webfetch-prompt">{escaped_prompt}</div>'


def format_webfetch_output(output: WebFetchOutput) -> str:
    """Format WebFetch tool result as collapsible markdown.

    Args:
        output: Parsed WebFetchOutput with result and metadata

    Returns:
        HTML string with markdown rendered in collapsible section,
        plus metadata badge showing HTTP status and timing.
    """
    # Build metadata badge
    badge_parts: list[str] = []
    if output.code is not None:
        status_class = "success" if output.code == 200 else "error"
        badge_parts.append(
            f'<span class="webfetch-status webfetch-status-{status_class}">{output.code}</span>'
        )
    if output.bytes is not None:
        # Format bytes nicely
        if output.bytes >= 1024 * 1024:
            size_str = f"{output.bytes / (1024 * 1024):.1f} MB"
        elif output.bytes >= 1024:
            size_str = f"{output.bytes / 1024:.1f} KB"
        else:
            size_str = f"{output.bytes} bytes"
        badge_parts.append(f'<span class="webfetch-size">{size_str}</span>')
    if output.duration_ms is not None:
        if output.duration_ms >= 1000:
            time_str = f"{output.duration_ms / 1000:.1f}s"
        else:
            time_str = f"{output.duration_ms}ms"
        badge_parts.append(f'<span class="webfetch-duration">{time_str}</span>')

    badge_html = ""
    if badge_parts:
        badge_html = f'<div class="webfetch-meta">{" ".join(badge_parts)}</div>'

    # Render the result as markdown in a collapsible section
    content_html = render_markdown_collapsible(output.result, "webfetch-result")

    return f"{badge_html}{content_html}"


# -- Generic Parameter Table --------------------------------------------------


def render_params_table(params: dict[str, Any]) -> str:
    """Render a dictionary of parameters as an HTML table.

    Reusable for tool parameters, diagnostic objects, etc.
    """
    if not params:
        return "<div class='tool-params-empty'>No parameters</div>"

    html_parts = ["<table class='tool-params-table'>"]

    for key, value in params.items():
        escaped_key = escape_html(str(key))

        # If value is structured (dict/list), render as JSON
        if isinstance(value, (dict, list)):
            try:
                formatted_value = json.dumps(value, indent=2, ensure_ascii=False)
                escaped_value = escape_html(formatted_value)

                # Make long structured values collapsible
                if len(formatted_value) > 200:
                    preview = escape_html(formatted_value[:100]) + "..."
                    value_html = f"""
                        <details class='tool-param-collapsible'>
                            <summary>{preview}</summary>
                            <pre class='tool-param-structured'>{escaped_value}</pre>
                        </details>
                    """
                else:
                    value_html = (
                        f"<pre class='tool-param-structured'>{escaped_value}</pre>"
                    )
            except (TypeError, ValueError):
                # Fallback: convert to string when JSON serialization fails
                escaped_value = escape_html(str(cast(object, value)))
                value_html = escaped_value
        else:
            # Simple value, render as-is (or collapsible if long)
            escaped_value = escape_html(str(value))

            # Make long string values collapsible
            if len(str(value)) > 100:
                preview = escape_html(str(value)[:80]) + "..."
                value_html = f"""
                    <details class='tool-param-collapsible'>
                        <summary>{preview}</summary>
                        <div class='tool-param-full'>{escaped_value}</div>
                    </details>
                """
            else:
                value_html = escaped_value

        html_parts.append(f"""
            <tr>
                <td class='tool-param-key'>{escaped_key}</td>
                <td class='tool-param-value'>{value_html}</td>
            </tr>
        """)

    html_parts.append("</table>")
    return "".join(html_parts)


# -- Tool Result Content Fallback Formatter -----------------------------------


def format_tool_result_content_raw(tool_result: ToolResultContent) -> str:
    """Format raw ToolResultContent as HTML (fallback formatter).

    This handles tool results that don't have specialized output types,
    including structured content with embedded images.

    Args:
        tool_result: The raw tool result content
    """
    # Handle both string and structured content
    if isinstance(tool_result.content, str):
        raw_content = tool_result.content
        has_images = False
        image_html_parts: list[str] = []
    else:
        # Content is a list of structured items, extract text and images
        content_parts: list[str] = []
        image_html_parts: list[str] = []
        for item in tool_result.content:
            item_type = item.get("type")
            if item_type == "text":
                text_value = item.get("text")
                if isinstance(text_value, str):
                    content_parts.append(text_value)
            elif item_type == "image":
                # Handle image content within tool results
                source = cast(dict[str, Any], item.get("source", {}))
                if source:
                    media_type: str = str(source.get("media_type", "image/png"))
                    # Restrict to safe image types to prevent XSS via SVG
                    allowed_media_types = {
                        "image/png",
                        "image/jpeg",
                        "image/gif",
                        "image/webp",
                    }
                    if media_type not in allowed_media_types:
                        continue
                    data: str = str(source.get("data", ""))
                    if data:
                        # Validate base64 data to prevent corruption/injection
                        try:
                            base64.b64decode(data, validate=True)
                        except (binascii.Error, ValueError):
                            continue
                        data_url = f"data:{media_type};base64,{data}"
                        image_html_parts.append(
                            f'<img src="{escape_html(data_url)}" alt="Tool result image" '
                            f'class="tool-result-image" />'
                        )
        raw_content = "\n".join(content_parts)
        has_images = len(image_html_parts) > 0

    # Strip <tool_use_error> XML tags but keep the content inside
    # Also strip redundant "String: ..." portions that echo the input
    if raw_content:
        raw_content = strip_error_tags(raw_content)
        # Remove "String: ..." portions that echo the input
        raw_content = re.sub(r"\nString:.*$", "", raw_content, flags=re.DOTALL)

    # Format the content
    full_html = escape_html(raw_content)
    preview_html = (
        escape_html(raw_content[:200]) + "..."
        if len(raw_content) > 200
        else escape_html(raw_content)
    )

    # Build final HTML based on content length and presence of images
    if has_images:
        # Combine text and images
        text_html = f"<pre>{full_html}</pre>" if full_html else ""
        images_html = "".join(image_html_parts)
        combined_content = f"{text_html}{images_html}"

        # Always make collapsible when images are present
        preview_text = "Text and image content"
        return f"""
    <details class="collapsible-details">
        <summary>
            <span class='preview-text'>{preview_text}</span>
        </summary>
        <div class="details-content">
            {combined_content}
        </div>
    </details>
    """
    else:
        # Text-only content
        # For simple content, show directly without collapsible wrapper
        if len(raw_content) <= 200:
            return f"<pre>{full_html}</pre>"

        # For longer content, use collapsible details
        return f"""
    <details class="collapsible-details">
        <summary>
            <div class="preview-content"><pre>{preview_html}</pre></div>
        </summary>
        <div class="details-content">
            <pre>{full_html}</pre>
        </div>
    </details>
    """


# -- Public Exports -----------------------------------------------------------

__all__ = [
    # Tool input formatters (called by HtmlRenderer.format_{InputClass})
    "format_askuserquestion_input",
    "format_exitplanmode_input",
    "format_todowrite_input",
    "format_read_input",
    "format_write_input",
    "format_edit_input",
    "format_multiedit_input",
    "format_bash_input",
    "format_grep_input",
    "format_task_input",
    "format_websearch_input",
    "format_webfetch_input",
    # Tool output formatters (called by HtmlRenderer.format_{OutputClass})
    "format_read_output",
    "format_write_output",
    "format_edit_output",
    "format_bash_output",
    "format_task_output",
    "format_askuserquestion_output",
    "format_exitplanmode_output",
    "format_websearch_output",
    "format_webfetch_output",
    # Fallback for ToolResultContent
    "format_tool_result_content_raw",
    # Legacy formatters (still used)
    "format_askuserquestion_result",
    "format_exitplanmode_result",
    # Generic
    "render_params_table",
]

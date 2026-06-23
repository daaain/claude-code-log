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
from collections.abc import Iterable
from typing import Any, Optional, cast

from .utils import (
    escape_html,
    is_memory_path,
    render_collapsible_code,
    render_async_result_body,
    render_file_content_collapsible,
    render_markdown_collapsible,
    render_markdown_inline,
    render_user_markdown,
    render_user_markdown_collapsible,
    resolve_memory_body_links,
)
from ..utils import strip_error_tags
from ..workflow import resolve_workflow_header
from ..models import (
    AskUserQuestionInput,
    AskUserQuestionItem,
    AskUserQuestionOption,
    AskUserQuestionOutput,
    BashInput,
    BashOutput,
    EditInput,
    EditOutput,
    CronCreateInput,
    CronCreateOutput,
    CronDeleteInput,
    CronDeleteOutput,
    CronListInput,
    CronListOutput,
    ExitPlanModeInput,
    ExitPlanModeOutput,
    GrepInput,
    MonitorInput,
    MonitorOutput,
    MultiEditInput,
    ReadInput,
    ScheduleWakeupInput,
    ScheduleWakeupOutput,
    ReadOutput,
    TaskInput,
    TaskOutput,
    TaskStopInput,
    TaskStopOutput,
    TodoWriteInput,
    ToolResultContent,
    WebSearchInput,
    WebSearchOutput,
    WebFetchInput,
    WebFetchOutput,
    WorkflowAgentMessage,
    WorkflowPhaseMessage,
    WorkflowToolInput,
    WriteInput,
    WriteOutput,
)
from .ansi_colors import convert_ansi_to_html
from .renderer_code import render_single_diff


# -- AskUserQuestion Tool -----------------------------------------------------


def _render_question_heading(header: Optional[str], question: str) -> list[str]:
    """Header chip + ``Q:`` line. Question text and header are LLM-authored,
    so render them as inline Markdown (issue #180)."""
    parts: list[str] = []
    if header:
        parts.append(
            f'<div class="question-header">{render_markdown_inline(header)}</div>'
        )
    parts.append(
        f'<div class="question-text"><span class="qa-label">Q:</span> '
        f"{render_markdown_inline(question)}</div>"
    )
    return parts


def _render_option_li(opt: AskUserQuestionOption, selected: bool) -> str:
    """One option ``<li>``; ``selected`` marks the user's choice (issue #180).

    Label and description are LLM-authored Markdown, rendered inline."""
    li_class = "question-option selected" if selected else "question-option"
    check = (
        '<span class="option-check" aria-hidden="true">✓</span> ' if selected else ""
    )
    if opt.description:
        desc_html = f'<span class="option-desc"> — {render_markdown_inline(opt.description)}</span>'
    else:
        desc_html = ""
    label = render_markdown_inline(opt.label)
    return f'<li class="{li_class}">{check}<strong>{label}</strong>{desc_html}</li>'


def _render_question_item(q: AskUserQuestionItem) -> str:
    """Render a single (unanswered) question item to HTML."""
    html_parts: list[str] = ['<div class="question-block">']
    html_parts.extend(_render_question_heading(q.header, q.question))

    if q.options:
        select_hint = "(select multiple)" if q.multiSelect else "(select one)"
        html_parts.append(f'<div class="question-options-hint">{select_hint}</div>')
        html_parts.append('<ul class="question-options">')
        for opt in q.options:
            html_parts.append(_render_option_li(opt, selected=False))
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
    # Extract the Q&A portion between the colon and the final sentence. The
    # summary sentence has used two wordings across harness versions (#180):
    # 'User has answered your questions: "Q"="A", ... . You can now continue...'
    # 'Your questions have been answered: "Q"="A", ... . You can now continue...'
    match = re.match(
        r"(?:User has answered your questions?|Your questions have been answered): "
        r"(.+)\. You can now continue",
        content,
        re.DOTALL,
    )
    if not match:
        # Return as-is for errors or unexpected format
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


# -- Grep Tool ----------------------------------------------------------------


def format_grep_input(grep_input: GrepInput) -> str:
    """Format Grep tool use content as generic params table, minus pattern.

    The pattern is already shown in the title, so we render remaining
    parameters (path, glob, type, output_mode, -A, -B, etc.) using the
    generic params table. Returns empty if pattern was the only parameter.
    """
    params = grep_input.model_dump(exclude={"pattern"}, exclude_none=True)
    if not params:
        return ""
    return render_params_table(params)


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

    # Auto-memory files are Markdown (MEMORY.md + topic .md), so render a
    # recalled-memory body as rendered Markdown rather than syntax-highlighted
    # source — using the project's usual collapsible-markdown helper (#192).
    if is_memory_path(output.file_path):
        # Escape HTML: memory files are untrusted content — raw <script>/HTML
        # must render as text, not live DOM when the transcript is opened.
        body = render_user_markdown_collapsible(output.content, "read-tool-result")
        return resolve_memory_body_links(body, output.file_path) + suffix_html

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


def format_taskstop_input(_taskstop_input: TaskStopInput) -> str:
    """Format TaskStop tool_use body — empty.

    The id lives in the title (with backlink), and there's nothing
    else useful to render: ``TaskStopInput.task_id`` is the only
    field. Returning empty keeps the spawn card compact.
    """
    return ""


def format_taskstop_output(output: TaskStopOutput) -> str:
    """Format TaskStop tool_result as HTML.

    Two states:
    - ``stopped=True`` (success): muted ``Stopped`` badge followed
      by the harness message in a ``<pre>`` (often echoes the
      original command — useful context for the reader).
    - ``stopped=False`` (not-found / error): error-styled badge plus
      the message. This is the common case in practice; the polled
      task often completes naturally before the stop lands.

    No markdown rendering — the message is plain text from the
    harness.
    """
    badge_class = "taskstop-ok" if output.stopped else "taskstop-err"
    badge_label = "Stopped" if output.stopped else "Not stopped"
    parts: list[str] = [
        f"<div class='taskstop-result'>"
        f"<span class='taskstop-badge {badge_class}'>{badge_label}</span>"
    ]
    if output.message:
        parts.append(
            f"<pre class='taskstop-message'>{escape_html(output.message)}</pre>"
        )
    parts.append("</div>")
    return "".join(parts)


def format_task_output(output: TaskOutput) -> str:
    """Format Task tool result as HTML with markdown rendering.

    For async-spawned Tasks (issue #90), ``output.result`` is just the
    "Async agent launched successfully…" stub; the real answer arrives
    later via the ``<task-notification>`` and is folded onto
    ``output.async_final_answer`` by ``_link_async_notifications``.
    Render the stub first, then the folded answer in a separate
    collapsible "Result" section so the spawn carries the actual
    agent output where the reader expects it.

    Args:
        output: Parsed TaskOutput with agent's response

    Returns:
        HTML string with markdown rendered in collapsible section
    """
    parts: list[str] = [render_markdown_collapsible(output.result, "task-result")]
    if output.async_final_answer:
        parts.append(
            '<div class="task-async-answer-label">'
            "Result <small>(from async notification)</small>"
            "</div>"
        )
        parts.append(
            render_async_result_body(output.async_final_answer, "task-async-answer")
        )
    return "".join(parts)


def _answer_selections(answer: str, multi_select: bool) -> set[str]:
    """Return the set of option labels the answer selected.

    Single-select answers equal one option label verbatim (so an exact match
    handles labels that themselves contain a comma). Multi-select answers join
    the chosen labels with ", " — split on that delimiter and match each part.
    """
    selections = {answer.strip()}
    if multi_select:
        selections.update(part.strip() for part in answer.split(", "))
    return {s for s in selections if s}


def format_askuserquestion_output(
    output: AskUserQuestionOutput,
    questions_by_text: Optional[dict[str, AskUserQuestionItem]] = None,
) -> str:
    """Format AskUserQuestion tool result with styled, answered Q&A blocks.

    Each answered question renders the offered options with the chosen one(s)
    highlighted — a self-contained "what was offered → what was picked" card
    (issue #180). Options/header come from the answer's own structured data, or
    from ``questions_by_text`` (the paired tool_use input) when the result text
    alone didn't carry them (text-fallback and clarify-rejection paths).

    Selection handling:
    - An answer matching an offered option highlights that option.
    - A free-form answer (matches no option) renders as an extra *selected*
      block after the options, so the user's typed reply is shown as the choice.
    - An empty answer (the user left that question unanswered) shows the options
      with none selected.
    """
    questions_by_text = questions_by_text or {}
    html_parts: list[str] = [
        '<div class="askuserquestion-content askuserquestion-result">'
    ]

    for qa in output.answers:
        paired = questions_by_text.get(qa.question)
        header = qa.header or (paired.header if paired else None)
        options = qa.options or (paired.options if paired else [])
        multi_select = qa.multi_select or (paired.multiSelect if paired else False)

        html_parts.append('<div class="question-block answered">')
        html_parts.extend(_render_question_heading(header, qa.question))

        selections = (
            _answer_selections(qa.answer, multi_select) if qa.answer else set[str]()
        )
        matched_any = False
        if options:
            html_parts.append('<ul class="question-options">')
            for opt in options:
                is_selected = opt.label in selections
                matched_any = matched_any or is_selected
                html_parts.append(_render_option_li(opt, selected=is_selected))
            html_parts.append("</ul>")

        if qa.answer and not matched_any:
            # Free-form reply (or no options to match against): show the typed
            # answer as the selected block so it reads as the user's choice.
            html_parts.append(
                '<ul class="question-options"><li class="question-option selected">'
                '<span class="option-check" aria-hidden="true">✓</span> '
                f"<strong>{render_markdown_inline(qa.answer)}</strong></li></ul>"
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
    # Memory files are Markdown — render a written memory body as rendered
    # Markdown rather than highlighted source (#192).
    if is_memory_path(write_input.file_path):
        # Escape HTML (untrusted memory content) — see format_read_output.
        body = render_user_markdown_collapsible(
            write_input.content, "write-tool-content"
        )
        return resolve_memory_body_links(body, write_input.file_path)
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


# -- Monitor Tool -------------------------------------------------------------


def format_monitor_input(monitor_input: MonitorInput) -> str:
    """Format Monitor tool use as a key-value grid.

    Renders four rows — ``description``, ``command``, ``timeout_ms``,
    ``persistent`` — using the same ``tool-params-table`` shape as
    other multi-field tool inputs. The ``command`` value (often a
    multi-line bash poll-loop) renders inside a collapsible block so
    a long script doesn't dominate the card.

    The ``description`` is shown both in the title and the body; the
    body row anchors the rendering to the harness's exact field name
    and keeps the card useful when a future title format changes.
    """
    command = monitor_input.command
    line_count = command.count("\n") + 1
    escaped_command = escape_html(command)
    if line_count > 5 or len(command) > 300:
        # Use the collapsible-code helper for the same visual treatment
        # other multi-line tool bodies get (line-count badge + preview).
        preview_lines = "\n".join(command.splitlines()[:3])
        preview_html = f"<pre>{escape_html(preview_lines)}</pre>"
        full_html = f"<pre>{escaped_command}</pre>"
        command_cell = render_collapsible_code(preview_html, full_html, line_count)
    else:
        command_cell = f"<pre class='monitor-command'>{escaped_command}</pre>"

    # Compose the table by hand so the ``command`` row carries the
    # specialised cell instead of a generic stringification.
    rows: list[str] = []
    rows.append(
        f"<tr><td class='tool-param-key'>description</td>"
        f"<td class='tool-param-value'>{escape_html(monitor_input.description)}</td></tr>"
    )
    rows.append(
        f"<tr><td class='tool-param-key'>command</td>"
        f"<td class='tool-param-value'>{command_cell}</td></tr>"
    )
    if monitor_input.timeout_ms is not None:
        rows.append(
            f"<tr><td class='tool-param-key'>timeout_ms</td>"
            f"<td class='tool-param-value'>{escape_html(str(monitor_input.timeout_ms))}</td></tr>"
        )
    if monitor_input.persistent is not None:
        rows.append(
            f"<tr><td class='tool-param-key'>persistent</td>"
            f"<td class='tool-param-value'>{escape_html(str(monitor_input.persistent))}</td></tr>"
        )
    return f"<table class='tool-params-table monitor-input'>{''.join(rows)}</table>"


def format_monitor_output(output: MonitorOutput) -> str:
    """Format Monitor tool result — the start-confirmation paragraph.

    The harness emits a single paragraph confirming the monitor was
    armed and naming the task id. Render verbatim inside a paragraph
    block; the body is short enough that no collapsibility is worth
    the chrome.
    """
    return f"<div class='monitor-output'>{escape_html(output.text)}</div>"


# -- ScheduleWakeup / Cron* Tools ---------------------------------------------
#
# A small family of scheduling tools sharing a common rendering shape:
# a key-value grid for inputs (often with one long ``prompt`` field
# rendered collapsibly via ``render_collapsible_code``) and a short
# status paragraph for outputs. Built-in tools that previously fell
# through to the generic params-table render (#148).


def format_schedulewakeup_input(inp: ScheduleWakeupInput) -> str:
    """Format ScheduleWakeup tool use as the prompt rendered as Markdown.

    ``delaySeconds`` and ``reason`` already appear in the title
    (``⏰ ScheduleWakeup +<delay>s — <reason>``); duplicating them
    in the body adds noise. The prompt is the only field worth
    showing — and it's typically Markdown content (slash commands,
    inline code, prose) rather than preformatted text, so render
    it via ``render_markdown_collapsible`` to honour the formatting
    while keeping long prompts collapsible.
    """
    return render_markdown_collapsible(inp.prompt, "schedulewakeup-prompt")


def format_schedulewakeup_output(output: ScheduleWakeupOutput) -> str:
    """Format ScheduleWakeup result — short status paragraph verbatim."""
    return f"<div class='schedulewakeup-output'>{escape_html(output.text)}</div>"


def format_croncreate_input(inp: CronCreateInput) -> str:
    """Format CronCreate tool use as the prompt rendered as Markdown.

    ``cron`` is already in the title (``⏰ CronCreate <cron>``) and
    the harness's confirmation echoes back ``recurring`` / ``durable``
    in human-readable form, so the body doesn't need to repeat any
    input scalars. The prompt is typically Markdown content (slash
    commands, inline code, prose) rather than preformatted text;
    render via ``render_markdown_collapsible`` to honour the
    formatting while keeping long prompts collapsible.
    """
    return render_markdown_collapsible(inp.prompt, "croncreate-prompt")


def format_croncreate_output(output: CronCreateOutput) -> str:
    """Format CronCreate result — short status paragraph verbatim."""
    return f"<div class='croncreate-output'>{escape_html(output.text)}</div>"


def format_cronlist_input(_inp: CronListInput) -> str:
    """Format CronList tool use — empty body (nothing to display)."""
    # The title carries everything; CronList takes no inputs.
    return ""


def format_cronlist_output(output: CronListOutput) -> str:
    """Format CronList result.

    When the parser produced a structured ``jobs`` list, render as a
    compact table; otherwise fall back to a verbatim paragraph so the
    raw text is visible (the harness's exact format is loosely
    documented and may drift).
    """
    if not output.jobs:
        return f"<pre class='cronlist-output'>{escape_html(output.text)}</pre>"

    rows = [
        "<thead><tr><th>id</th><th>schedule</th><th>prompt</th></tr></thead>",
        "<tbody>",
    ]
    for job in output.jobs:
        # Truncate prompt at table width — full prompt is in the
        # original CronCreate card upstream, so the list view favours
        # density over completeness. The harness already truncates at
        # output time (with a trailing ``…``), so the input is already
        # short in practice.
        preview = job.prompt if len(job.prompt) <= 80 else job.prompt[:77] + "…"
        # Cross-link the id back to the originating CronCreate card
        # when the renderer's ``_link_cron_jobs_by_id`` pass found a
        # match. Plain ``<code>`` otherwise.
        if job.creating_call_message_index is not None:
            anchor = f"msg-d-{job.creating_call_message_index}"
            id_html = (
                f"<a class='cron-id-backlink' href='#{anchor}'>"
                f"<code>{escape_html(job.id)}</code></a>"
            )
        else:
            id_html = f"<code>{escape_html(job.id)}</code>"
        rows.append(
            f"<tr>"
            f"<td>{id_html}</td>"
            f"<td>{escape_html(job.description)}</td>"
            f"<td>{escape_html(preview)}</td>"
            f"</tr>"
        )
    rows.append("</tbody>")
    return f"<table class='cronlist-output-table'>{''.join(rows)}</table>"


def format_crondelete_input(inp: CronDeleteInput) -> str:
    """Format CronDelete tool use — empty body (id is in the title)."""
    del inp  # id surfaces via title_CronDeleteInput.
    return ""


def format_crondelete_output(output: CronDeleteOutput) -> str:
    """Format CronDelete result.

    Renders the harness status line verbatim. When the renderer's
    ``_link_cron_jobs_by_id`` pass matched the cancelled job id back
    to its originating ``CronCreate`` card, wrap that occurrence of
    the id in an anchor so the reader can navigate.
    """
    text = output.text
    if (
        output.creating_call_message_index is not None
        and output.job_id
        and output.job_id in text
    ):
        anchor = f"msg-d-{output.creating_call_message_index}"
        before, sep, after = text.partition(output.job_id)
        # Escape each fragment independently so the anchor isn't
        # corrupted by ``escape_html`` running on the whole string.
        body = (
            f"{escape_html(before)}"
            f"<a class='cron-id-backlink' href='#{anchor}'>"
            f"<code>{escape_html(sep)}</code></a>"
            f"{escape_html(after)}"
        )
        return f"<div class='crondelete-output'>{body}</div>"
    return f"<div class='crondelete-output'>{escape_html(output.text)}</div>"


# -- Generic Parameter Table --------------------------------------------------


# Nesting depth at which structured values stop recursing into tables
# and fall back to the JSON dump, so a pathological payload can't blow
# up the DOM.
_PARAMS_TABLE_MAX_DEPTH = 4

# Per-container breadth cap (CodeRabbit, PR #216): the depth guard and
# the folded-by-default display don't stop the HTML for a huge array
# from being GENERATED — one <tr> per element even when collapsed.
# Wider containers fall back to the JSON dump.
_PARAMS_TABLE_MAX_ITEMS = 200


def _json_dump_value_html(formatted_value: str) -> str:
    """Escaped JSON dump in a ``<pre>``, collapsible when long."""
    escaped_value = escape_html(formatted_value)
    if len(formatted_value) > 200:
        preview = escape_html(formatted_value[:100]) + "..."
        return f"""
                        <details class='tool-param-collapsible'>
                            <summary><span class='tool-param-preview'>{preview}</span></summary>
                            <pre class='tool-param-structured'>{escaped_value}</pre>
                        </details>
                    """
    return f"<pre class='tool-param-structured'>{escaped_value}</pre>"


def _raw_string_value_html(value: str) -> str:
    """Escaped plain text, collapsible when long."""
    escaped_value = escape_html(value)
    if len(value) > 100:
        preview = escape_html(value[:80]) + "..."
        return f"""
                    <details class='tool-param-collapsible'>
                        <summary><span class='tool-param-preview'>{preview}</span></summary>
                        <div class='tool-param-full'>{escaped_value}</div>
                    </details>
                """
    return escaped_value


def _string_value_html(value: str) -> str:
    """Render a string param value, treating it as potential Markdown.

    Strings that are obviously markup rather than Markdown — XML/HTML
    (``<``) or JSON (``{`` / ``[``) after leading whitespace — keep the
    escaped raw-text rendering. Both Markdown paths use the
    ``escape=True`` renderers: params were always HTML-escaped, so the
    Markdown upgrade must not open a raw-HTML injection route.

    Long values keep the legacy >100-char ``<details>`` fold (length-,
    not line-based — a long single-line prompt must fold too), with the
    rendered Markdown as the expanded body.
    """
    if value.lstrip()[:1] in ("<", "{", "["):
        return _raw_string_value_html(value)
    if len(value) > 100:
        preview = escape_html(value[:80]) + "..."
        rendered = render_user_markdown(value)
        return f"""
                    <details class='tool-param-collapsible'>
                        <summary><span class='tool-param-preview'>{preview}</span></summary>
                        <div class='tool-param-markdown markdown'>{rendered}</div>
                    </details>
                """
    return render_markdown_inline(value)


def _structured_value_html(value: "dict[Any, Any] | list[Any]", depth: int) -> str:
    """Render a dict/list param value as a nested key/value table.

    ``depth`` is the nesting level of the table containing this value;
    past ``_PARAMS_TABLE_MAX_DEPTH`` (and for empty containers) the
    value falls back to the JSON dump. The table always renders inside
    a collapsed fold with a JSON-text preview — size-independent, so
    sibling rows look consistent (no "auto-expanded" short values).
    """
    try:
        formatted_value = json.dumps(value, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        # Fallback: convert to string when JSON serialization fails
        return escape_html(str(cast(object, value)))

    if (
        not value
        or depth >= _PARAMS_TABLE_MAX_DEPTH
        or len(value) > _PARAMS_TABLE_MAX_ITEMS
    ):
        return _json_dump_value_html(formatted_value)

    if isinstance(value, dict):
        items: Iterable[tuple[Any, Any]] = value.items()
        kind = "properties"
    else:
        items = enumerate(value)
        kind = "rows"
    table_html = _params_table_html(items, depth + 1)
    return _table_fold_html(formatted_value, table_html, kind)


def _table_fold_html(formatted_value: str, table_html: str, kind: str) -> str:
    """Wrap a rendered params table in a collapsed fold.

    The summary holds ONLY the preview text — never interactive elements
    (a button inside ``<summary>`` is an accessibility violation Chrome
    flags: not consistently reachable by keyboard/AT). When the table has
    row-level folds, the rows-toggle button lives in a controls strip
    AFTER the summary, inside the details — natively hidden while closed,
    visible when open (wired up in transcript.html). The "<details" probe
    is exact: structures always fold, so a deeper fold can only exist
    inside a direct-row fold. All-scalar containers get a plain fold —
    no dead button.
    """
    preview = escape_html(formatted_value[:100])
    if len(formatted_value) > 100:
        preview += "..."
    if "<details" in table_html:
        return f"""
                        <details class='tool-param-collapsible tool-param-collapsible-rows'>
                            <summary><span class='tool-param-preview'>{preview}</span></summary>
                            <div class='tool-param-fold-controls'><button type='button' class='tool-param-rows-toggle' data-state='collapsed' data-kind='{kind}'><span class='tool-param-fold-glyph'></span><span class='tool-param-fold-label'>expand all {kind}</span></button></div>
                            {table_html}
                        </details>
                    """
    return f"""
                        <details class='tool-param-collapsible'>
                            <summary><span class='tool-param-preview'>{preview}</span></summary>
                            {table_html}
                        </details>
                    """


def _params_root_html(table_html: str) -> str:
    """Wrap a top-level params/result table with an expand-all control.

    The button opens/closes every fold inside this renderer at once and
    keeps the nested rows-toggle buttons in sync (wired up in
    transcript.html, state derived from the DOM). Only emitted when the
    tree actually contains folds — no dead button on flat tables.
    """
    if "<details" not in table_html:
        return table_html
    return (
        "<div class='tool-params-root'>"
        "<div class='tool-params-controls'>"
        "<button type='button' class='tool-params-expand-all'"
        " data-state='collapsed'>"
        "<span class='tool-param-fold-glyph'></span>"
        "<span class='tool-param-fold-label'>expand all</span></button>"
        "</div>"
        f"{table_html}"
        "</div>"
    )


def _param_value_html(value: Any, depth: int) -> str:
    """Dispatch a single param value to its hybrid rendering."""
    if isinstance(value, (dict, list)):
        return _structured_value_html(cast("dict[Any, Any] | list[Any]", value), depth)
    if isinstance(value, str):
        return _string_value_html(value)
    # Scalars (int/float/bool/None): plain escaped text as before.
    return _raw_string_value_html(str(value))


def _params_table_html(items: "Iterable[tuple[Any, Any]]", depth: int) -> str:
    """Build one key/value table; nested levels get a marker class.

    A fold-valued row hoists its ▶/▼ toggle into the KEY column (wired in
    transcript.html), so the value summary stays free of per-fold collapse
    chrome — only the previews (closed) and the rows-toggle buttons (open)
    remain there. The ``<details`` probe is exact: every fold helper emits
    a ``<details`` as the value's first element, and non-fold values never
    start with one (their text is escaped).
    """
    css = "tool-params-table" if depth == 0 else "tool-params-table tool-params-nested"
    html_parts = [f"<table class='{css}'>"]
    for key, value in items:
        escaped_key = escape_html(str(key))
        value_html = _param_value_html(value, depth)
        if value_html.lstrip().startswith("<details"):
            # The button wraps glyph AND key text — the whole key is the
            # click target (and keyboard-reachable, unlike a td handler).
            # The empty glyph span carries the browser's native disclosure
            # marker (CSS display:list-item) — the same symmetric icon a
            # <summary> shows, immune to italic contexts.
            key_cell = (
                "<button type='button' class='tool-param-key-toggle'"
                " aria-expanded='false'>"
                "<span class='tool-param-fold-glyph'></span>"
                f"{escaped_key}</button>"
            )
            row_attr = " class='tool-param-row-fold'"
        else:
            # Scalar rows reserve the same glyph slot (empty) so every key
            # text starts at the same x.
            key_cell = f"<span class='tool-param-fold-glyph'></span>{escaped_key}"
            row_attr = ""
        html_parts.append(f"""
            <tr{row_attr}>
                <td class='tool-param-key'>{key_cell}</td>
                <td class='tool-param-value'>{value_html}</td>
            </tr>
        """)
    html_parts.append("</table>")
    return "".join(html_parts)


def render_params_table(params: dict[str, Any]) -> str:
    """Render a dictionary of parameters as an HTML table.

    Reusable for tool parameters, diagnostic objects, etc. Values render
    as a JSON/Markdown hybrid: strings are treated as Markdown (unless
    they look like XML/HTML or JSON), dicts and lists recurse into
    nested tables, scalars stay plain.
    """
    if not params:
        return "<div class='tool-params-empty'>No parameters</div>"

    return _params_root_html(_params_table_html(params.items(), 0))


# -- Tool Result Content Fallback Formatter -----------------------------------


def _json_result_table_html(raw_content: str) -> Optional[str]:
    """Render a tool-result string as a params-style table when it
    parses as a non-empty JSON object/array; ``None`` otherwise.

    Objects become key/value tables, arrays become index/value tables,
    with values rendered by the same hybrid rules as tool params
    (Markdown-aware strings, nested structures folded).
    """
    if raw_content.lstrip()[:1] not in ("{", "["):
        return None
    try:
        parsed = json.loads(raw_content)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed:
        container = cast("dict[Any, Any]", parsed)
        items: Iterable[tuple[Any, Any]] = container.items()
        kind = "properties"
    elif isinstance(parsed, list) and parsed:
        container = cast("list[Any]", parsed)
        items = enumerate(container)
        kind = "rows"
    else:
        return None
    if len(container) > _PARAMS_TABLE_MAX_ITEMS:
        # Breadth cap: let huge results keep the legacy collapsible
        # text rendering instead of generating one row per element.
        return None
    table_html = _params_table_html(items, 0)
    # Breadth guard: results past the legacy 200-char threshold keep
    # its folded-by-default behavior — a huge JSON array must not
    # render as an unfolded thousand-row table.
    if len(raw_content) > 200:
        table_html = _table_fold_html(raw_content, table_html, kind)
    return f"<div class='tool-result-json'>{_params_root_html(table_html)}</div>"


def _structured_items_html(items: "list[dict[str, Any]]") -> str:
    """Render leftover typed content items as a JSON/Markdown hybrid table.

    Used for tool results whose ``content`` is a list carrying items other
    than ``text``/``image`` — e.g. ToolSearch's ``tool_reference`` blocks.
    These used to be dropped, leaving the result side blank (issue #227);
    now they render with the same table machinery as JSON results.
    """
    raw = json.dumps(items, ensure_ascii=False)
    table = _json_result_table_html(raw)
    if table is not None:
        return table
    # Breadth-capped (or otherwise non-tabular): fall back to a JSON dump.
    pretty = json.dumps(items, indent=2, ensure_ascii=False)
    return f"<div class='tool-result-json'>{_json_dump_value_html(pretty)}</div>"


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
        structured_html = ""
    else:
        # Content is a list of structured items, extract text and images.
        # Any other typed item (e.g. ``tool_reference`` from ToolSearch) is
        # kept verbatim and rendered as a JSON/Markdown hybrid table below,
        # instead of being silently dropped (issue #227).
        content_parts: list[str] = []
        image_html_parts: list[str] = []
        structured_items: list[dict[str, Any]] = []
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
            else:
                # Any other typed item (the loop already assumes dict via
                # ``item.get`` above) — keep it for the JSON table.
                structured_items.append(item)
        raw_content = "\n".join(content_parts)
        has_images = len(image_html_parts) > 0
        # Render leftover typed items (no text, not images) as a JSON table.
        structured_html = (
            _structured_items_html(structured_items) if structured_items else ""
        )

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
        # Combine text, structured items and images
        text_html = f"<pre>{full_html}</pre>" if full_html else ""
        images_html = "".join(image_html_parts)
        combined_content = f"{text_html}{structured_html}{images_html}"

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
        # Typed items with no plain text (e.g. ToolSearch's tool_reference):
        # the JSON table is the whole result (issue #227). When text is also
        # present, show it first, then the structured items.
        if structured_html:
            if not raw_content:
                return structured_html
            text_html = f"<pre>{full_html}</pre>" if full_html else ""
            return f"{text_html}{structured_html}"

        # Text-only content that parses as structured JSON renders as a
        # params-style table (not for errors — those read as text).
        if not tool_result.is_error:
            json_table = _json_result_table_html(raw_content)
            if json_table is not None:
                return json_table

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


# -- Workflow tool input (issue #174) -----------------------------------------


def format_workflow_input(workflow_input: WorkflowToolInput) -> str:
    """Format a ``Workflow`` tool_use (issue #174): a header from the script's
    ``meta`` block (name / description / phase pills) above the JavaScript
    orchestrator source, syntax-highlighted and collapsible when long."""
    script = workflow_input.script or ""
    name, description, phases = resolve_workflow_header(
        workflow_input.workflow_run, script
    )

    header_parts: list[str] = []
    if name:
        header_parts.append(f"<span class='workflow-name'>{escape_html(name)}</span>")
    if description:
        header_parts.append(
            f"<span class='workflow-description'>{escape_html(description)}</span>"
        )
    if phases:
        # When the splice recorded the phase cards' message indices (parallel
        # to this snapshot-sourced phase list), each pill links to its card —
        # the hashchange handler unfolds the target on click.
        anchors = workflow_input.phase_anchor_indices or []
        pill_parts: list[str] = []
        for i, p in enumerate(phases):
            label = escape_html(p)
            if i < len(anchors):
                pill_parts.append(
                    f"<a class='workflow-phase-pill' href='#msg-d-{anchors[i]}'>"
                    f"{label}</a>"
                )
            else:
                pill_parts.append(f"<span class='workflow-phase-pill'>{label}</span>")
        header_parts.append(
            f"<span class='workflow-phases'>{''.join(pill_parts)}</span>"
        )
    header = (
        f"<div class='workflow-meta'>{''.join(header_parts)}</div>"
        if header_parts
        else ""
    )

    if not script.strip():
        return header

    body = render_file_content_collapsible(
        script,
        "workflow.js",
        "workflow-script",
        line_threshold=12,
        preview_line_count=6,
    )
    return f"{header}{body}"


# -- Workflow run tree: phase + agent cards (issue #174 PR3) -------------------


def format_workflow_phase_content(content: WorkflowPhaseMessage) -> str:
    """Format a spliced workflow *phase* card body: the phase ``detail`` plus
    its agent count. The phase title is the card heading (``title_content``)."""
    parts: list[str] = []
    if content.detail:
        parts.append(
            f"<span class='workflow-phase-detail'>{escape_html(content.detail)}</span>"
        )
    if content.agent_count:
        unit = "agent" if content.agent_count == 1 else "agents"
        parts.append(
            f"<span class='workflow-phase-count'>{content.agent_count} {unit}</span>"
        )
    if not parts:
        return ""
    return f"<div class='workflow-phase-meta'>{''.join(parts)}</div>"


def format_workflow_agent_content(content: WorkflowAgentMessage) -> str:
    """Format a spliced workflow *agent* card body: a metadata chrome line
    (model / state / tokens / tool calls) above the agent's result — a
    ``StructuredOutput`` dict pretty-printed + highlighted as JSON, a plain
    string rendered as collapsible Markdown. The agent's side-channel
    transcript renders separately as this node's ``.children``."""
    meta_bits: list[str] = []
    if content.model:
        meta_bits.append(
            f"<span class='workflow-agent-model'>{escape_html(content.model)}</span>"
        )
    if content.state:
        meta_bits.append(
            f"<span class='workflow-agent-state'>{escape_html(content.state)}</span>"
        )
    if content.tokens is not None:
        meta_bits.append(
            f"<span class='workflow-agent-tokens'>{content.tokens} tokens</span>"
        )
    if content.tool_calls is not None:
        unit = "call" if content.tool_calls == 1 else "calls"
        meta_bits.append(
            f"<span class='workflow-agent-tools'>{content.tool_calls} tool {unit}</span>"
        )
    parts: list[str] = []
    if meta_bits:
        parts.append(f"<div class='workflow-agent-meta'>{''.join(meta_bits)}</div>")

    result = content.result
    if isinstance(result, dict):
        # A dict-shaped StructuredOutput result renders like a generic tool's
        # parameters — the shared key/value table — instead of a raw JSON dump.
        # Going through render_params_table means future upgrades to the
        # generic renderer (e.g. Markdown-aware string values) apply here
        # automatically.
        parts.append(
            f"<div class='workflow-agent-result'>"
            f"{render_params_table(cast('dict[str, Any]', result))}</div>"
        )
    elif isinstance(result, list):
        # List-shaped results keep the pretty-printed JSON view (an index→value
        # table reads worse than the literal). NOT via render_async_result_body
        # — its JSON heuristic only fires on `{"`-shaped text, so a `[...]`
        # payload would fall through to the markdown path (CR #210).
        pretty = json.dumps(result, indent=2, ensure_ascii=False)
        parts.append(
            render_file_content_collapsible(
                pretty,
                "result.json",
                "workflow-agent-result",
                line_threshold=10,
                preview_line_count=6,
            )
        )
    elif isinstance(result, str) and result.strip():
        parts.append(render_markdown_collapsible(result, "workflow-agent-result"))
    elif content.result_preview:
        parts.append(
            f"<span class='workflow-agent-result-preview'>"
            f"{escape_html(content.result_preview)}</span>"
        )
    return "".join(parts)


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
    "format_task_input",
    "format_taskstop_input",
    "format_grep_input",
    "format_websearch_input",
    "format_webfetch_input",
    "format_workflow_input",
    "format_workflow_phase_content",
    "format_workflow_agent_content",
    "format_monitor_input",
    "format_schedulewakeup_input",
    "format_croncreate_input",
    "format_cronlist_input",
    "format_crondelete_input",
    # Tool output formatters (called by HtmlRenderer.format_{OutputClass})
    "format_read_output",
    "format_write_output",
    "format_edit_output",
    "format_bash_output",
    "format_task_output",
    "format_taskstop_output",
    "format_askuserquestion_output",
    "format_exitplanmode_output",
    "format_websearch_output",
    "format_webfetch_output",
    "format_monitor_output",
    "format_schedulewakeup_output",
    "format_croncreate_output",
    "format_cronlist_output",
    "format_crondelete_output",
    # Fallback for ToolResultContent
    "format_tool_result_content_raw",
    # Legacy formatters (still used)
    "format_askuserquestion_result",
    "format_exitplanmode_result",
    # Generic
    "render_params_table",
]

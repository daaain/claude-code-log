"""HTML formatters for the async-agents feature (issue #90).

Renders:

- ``TaskOutput`` polling-tool input + output cards (minimal — the
  agent's full transcript already inlines as a sidechain elsewhere).
- ``TaskNotificationMessage`` cards: the User entry Claude Code
  injects when an async-spawned ``Task`` finishes. Body shape:
  metadata ``<dl>`` + collapsible Markdown for the ``<result>``.

Mirrors the per-tool-card style used by ``teammate_formatter`` so
the visual language stays consistent.
"""

from __future__ import annotations

from typing import Optional

from ..models import (
    TaskNotificationMessage,
    TaskNotificationUsage,
    TaskOutputInput,
    TaskOutputResult,
)
from .utils import escape_html, render_async_result_body


def _row(label: str, value_html: str) -> str:
    """Render one ``<dt>label</dt><dd>value</dd>`` pair (already-HTML value)."""
    return f"<dt>{escape_html(label)}</dt><dd>{value_html}</dd>"


def _code(value: str) -> str:
    """``<code>``-wrapped escaped value (used for ids, paths, statuses)."""
    return f"<code>{escape_html(value)}</code>"


def _format_usage_rows(usage: Optional[TaskNotificationUsage]) -> list[str]:
    """Optional usage stats as `<dt>/<dd>` rows. Skips fields the parser
    didn't capture so older transcripts (or partial completions) don't
    render empty cells."""
    if usage is None:
        return []
    rows: list[str] = []
    if usage.total_tokens is not None:
        rows.append(_row("Tokens", _code(f"{usage.total_tokens:,}")))
    if usage.tool_uses is not None:
        rows.append(_row("Tool uses", _code(str(usage.tool_uses))))
    if usage.duration_ms is not None:
        # 15506 ms → "15.5 s" reads better in this slot than the raw int.
        seconds = usage.duration_ms / 1000.0
        rows.append(_row("Duration", _code(f"{seconds:.1f}s")))
    return rows


# ---------------------------------------------------------------------------
# TaskOutput (polling tool)
# ---------------------------------------------------------------------------


def format_taskoutput_input(input_: TaskOutputInput) -> str:
    """Body for a TaskOutput tool_use.

    Minimal — the title already shows the task_id. We surface
    ``block`` / ``timeout`` only when they're set so vanilla calls
    render an empty body.
    """
    rows: list[str] = []
    if input_.block:
        rows.append(_row("Block", _code("true")))
    if input_.timeout:
        rows.append(_row("Timeout", _code(f"{input_.timeout} ms")))
    if not rows:
        return ""
    return f'<dl class="teammate-tool-card task-output-card">{"".join(rows)}</dl>'


def format_taskoutput_output(output: TaskOutputResult) -> str:
    """Body for a TaskOutput tool_result.

    Surfaces the metadata (``retrieval_status``, ``status``,
    ``task_type``) and a *link-only* hint at the truncated transcript
    file. The agent's full work already lives inline as a sidechain in
    the same document — duplicating it here would just bloat the
    rendering.
    """
    rows: list[str] = []
    if output.retrieval_status:
        rows.append(_row("Retrieval", _code(output.retrieval_status)))
    if output.task_type:
        rows.append(_row("Type", _code(output.task_type)))
    if output.status:
        # Reuse the TaskList palette for at-a-glance status colour.
        # Lifted out of the strict TaskList selector in commit
        # ``7c364bc`` precisely so other cards can use it.
        status_class = (
            f"status-{output.status}"
            if output.status in {"completed", "in_progress", "pending", "blocked"}
            else "status-unknown"
        )
        rows.append(
            _row(
                "Status",
                f"<span class='task-status {status_class}'>"
                f"{escape_html(output.status)}</span>",
            )
        )
    if output.output_truncated and output.output_file:
        rows.append(_row("Transcript", _code(output.output_file)))
    if not rows:
        return ""
    return f'<dl class="teammate-tool-card task-output-card">{"".join(rows)}</dl>'


# ---------------------------------------------------------------------------
# TaskNotification (User entry)
# ---------------------------------------------------------------------------


def format_task_notification_content(
    content: TaskNotificationMessage,
    include_duplicate: bool = False,
    preview: bool = False,
) -> str:
    """Render a ``<task-notification>`` user entry as a metadata card +
    collapsible Markdown body for ``<result>``.

    Title is set by ``HtmlRenderer.title_TaskNotificationMessage``
    (``🔄 Async result • <summary>``). When the renderer's Phase 3
    pass (`_link_async_notifications`) flagged the body as a duplicate
    of the spawning Task's last sidechain sub-assistant, the body
    collapses to a backlink-only stub instead of doubling the content —
    unless ``include_duplicate`` (the minimal theme, which shows the answer
    here, at the async branch's merge row, and not on the spawn). The
    minimal theme also asks for ``preview``: a Markdown answer longer than a
    few lines is a two-line preview (``utils.render_markdown_preview``).
    """
    rows: list[str] = []
    if content.task_id:
        if content.spawning_task_message_index is not None:
            # Backlink anchor format matches the rest of the renderer
            # ("d-{N}" → "msg-d-{N}"; the template emits the corresponding
            # id on every message div). Wrapping the Task ID itself in
            # the anchor (rather than a separate "Spawn" row) keeps the
            # affordance close to the value the reader is looking up
            # — matches issue #142's spec for the Monitor backlink and
            # is a cleaner shape for the agent-spawn case too.
            anchor = f"msg-d-{content.spawning_task_message_index}"
            task_id_html = (
                f"<a class='task-notification-backlink' href='#{anchor}'>"
                f"<code>{escape_html(content.task_id)}</code></a>"
            )
        else:
            task_id_html = _code(content.task_id)
        rows.append(_row("Task ID", task_id_html))
    if content.status:
        status_class = (
            f"status-{content.status}"
            if content.status in {"completed", "failed", "in_progress", "pending"}
            else "status-unknown"
        )
        rows.append(
            _row(
                "Status",
                f"<span class='task-status {status_class}'>"
                f"{escape_html(content.status)}</span>",
            )
        )
    rows.extend(_format_usage_rows(content.usage))
    if content.transcript_path:
        rows.append(_row("Transcript", _code(content.transcript_path)))

    parts: list[str] = []
    if rows:
        parts.append(
            f'<dl class="teammate-tool-card task-notification-card">'
            f"{''.join(rows)}</dl>"
        )
    if content.result_text and (include_duplicate or not content.result_is_duplicate):
        parts.append(
            render_async_result_body(
                content.result_text, "task-notification-result", preview=preview
            )
        )
    return "".join(parts)

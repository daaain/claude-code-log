"""HTML formatters for the experimental teammates feature.

Renders:

- ``TeammateMessage`` content (one card per ``<teammate-message>`` block
  via ``format_teammate_content``).
- The six teammate tool-input/-output cards used by the team-lead to
  manage teams and teammates (``format_teamcreate_input`` …
  ``format_sendmessage_input`` + matching outputs).
- Supporting badges / palette helpers.

The formatters are deliberately format-neutral in data (they take the
typed model shapes from ``models.py``) and produce self-contained HTML
fragments styled by ``components/teammate_styles.css``.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

from ..models import (
    SendMessageInput,
    SendMessageOutput,
    TaskCreateInput,
    TaskCreateOutput,
    TaskInput,
    TaskListItem,
    TaskListOutput,
    TaskOutput,
    TaskUpdateInput,
    TaskUpdateOutput,
    TeamCreateInput,
    TeamCreateOutput,
    TeamDeleteInput,
    TeamDeleteOutput,
    TeammateMessage,
    TeammateMessageBlock,
)
from .utils import escape_html, render_markdown, render_markdown_collapsible

# Palette names the CSS recognises (kept in sync with teammate_styles.css).
_PALETTE: frozenset[str] = frozenset(
    {
        "blue",
        "cyan",
        "green",
        "yellow",
        "orange",
        "red",
        "pink",
        "purple",
        "gray",
    }
)

# Claude Code statuses known to the task-list CSS. Unknown statuses fall
# through with the default gray badge.
_STATUS_CLASSES: frozenset[str] = frozenset(
    {"completed", "in_progress", "pending", "blocked", "deleted"}
)


# ---------------------------------------------------------------------------
# Palette helpers
# ---------------------------------------------------------------------------


def _color_token(color: Optional[str]) -> Optional[str]:
    """Return a palette name for *color* if it's one we style, else None.

    Unknown colors fall back to the default gray styling via CSS variable
    fallback — we just don't emit the ``--cc-color`` override.
    """
    if not color:
        return None
    normalised = color.strip().lower()
    return normalised if normalised in _PALETTE else None


def _color_style(color: Optional[str]) -> str:
    """Return an inline ``style="..."`` attribute setting --cc-color, or ''."""
    token = _color_token(color)
    if token is None:
        return ""
    return (
        f' style="--cc-color: var(--cc-{token}); --cc-color-bg: var(--cc-{token}-bg);"'
    )


def _teammate_badge(
    teammate_id: str,
    color: Optional[str],
    *,
    icon: str = "▎",
) -> str:
    """Render an inline colored teammate-id pill."""
    style = _color_style(color) or _neutral_badge_style()
    return (
        f'<span class="teammate-badge"{style}>'
        f'<span class="teammate-icon">{icon}</span>'
        f"{escape_html(teammate_id)}"
        f"</span>"
    )


def _neutral_badge_style() -> str:
    # When no recognised color is present, fall back to the gray token
    # *explicitly* so the badge still contrasts with the card background.
    return ' style="--cc-color: var(--cc-gray);"'


# ---------------------------------------------------------------------------
# TeammateMessage content
# ---------------------------------------------------------------------------


def format_teammate_content(
    content: TeammateMessage,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    """Render a TeammateMessage as one card per block, plus surrounding text.

    Each block produces a ``<div class="teammate-message">`` containing a
    header (colored badge + optional summary) and a Markdown-rendered body.
    ``teammate_id="system"`` blocks are flagged with ``teammate-system`` so
    the CSS can apply a distinct neutral palette.

    Blocks without an inline ``color="..."`` attribute fall back to
    ``teammate_colors[block.teammate_id]`` so later heartbeat / status
    messages stay visually linked to the teammate's color without having
    to re-send the attribute each time.
    """
    parts: list[str] = []

    if content.leading_text:
        parts.append(
            f'<div class="teammate-surrounding-text">'
            f"{escape_html(content.leading_text)}</div>"
        )

    for block in content.blocks:
        parts.append(_format_teammate_block(block, teammate_colors))

    if content.trailing_text:
        parts.append(
            f'<div class="teammate-surrounding-text">'
            f"{escape_html(content.trailing_text)}</div>"
        )

    return "".join(parts)


def _format_teammate_block(
    block: TeammateMessageBlock,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    classes = ["teammate-message"]
    if block.is_system:
        classes.append("teammate-system")
    class_attr = " ".join(classes)
    # Inline color wins; fall back to the learned session color.
    color = block.color or _lookup_color(teammate_colors, block.teammate_id)
    style = _color_style(color)

    header_parts: list[str] = [
        _teammate_badge(block.teammate_id, color),
    ]
    if block.summary:
        header_parts.append(
            f'<span class="teammate-summary">{escape_html(block.summary)}</span>'
        )

    body_text = block.body.strip()
    if not body_text:
        body_html = ""
    else:
        # Some teammate notifications come through as JSON payloads
        # (e.g. ``{"type":"idle_notification",...}``) rather than
        # markdown prose. Render those as a compact key:value list so
        # they read as data, not as a code blob.
        json_html = _try_render_json_body(body_text)
        body_html = json_html if json_html is not None else render_markdown(body_text)

    return (
        f'<div class="{class_attr}"{style}>'
        f'<div class="teammate-message-header">{"".join(header_parts)}</div>'
        f'<div class="teammate-body">{body_html}</div>'
        f"</div>"
    )


def _try_render_json_body(text: str) -> Optional[str]:
    """If *text* parses as a JSON object, render it as a key:value list.

    Returns None when the body isn't JSON, isn't an object, or is too
    nested to surface usefully — the caller falls back to Markdown.
    Cheap pre-check (`{`/`}` framing) keeps the parser off the typical
    Markdown path.
    """
    if not (text.startswith("{") and text.endswith("}")):
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed_dict: dict[str, Any] = parsed  # pyright: ignore[reportUnknownVariableType]
    rows: list[str] = []
    for key, value in parsed_dict.items():
        rows.append(
            f"<dt>{escape_html(str(key))}</dt><dd>{_format_json_scalar(value)}</dd>"
        )
    if not rows:
        return None
    return f'<dl class="teammate-json">{"".join(rows)}</dl>'


def _format_json_scalar(value: Any) -> str:
    """Render a JSON scalar (or nested structure) as inline HTML.

    Strings get a ``<code>`` wrapper; other scalars are stringified.
    Nested dicts/lists fall back to a compact JSON dump in a ``<code>``
    so the output stays one-row-per-key.
    """
    if isinstance(value, str):
        return f"<code>{escape_html(value)}</code>"
    if isinstance(value, bool) or value is None:
        return f"<code>{escape_html(str(value).lower() if value is not None else 'null')}</code>"
    if isinstance(value, (int, float)):
        return f"<code>{escape_html(str(value))}</code>"
    return f"<code>{escape_html(json.dumps(value, separators=(', ', ': ')))}</code>"


# ---------------------------------------------------------------------------
# Tool cards
# ---------------------------------------------------------------------------


def format_teamcreate_input(input_: TeamCreateInput) -> str:
    rows: list[tuple[str, str]] = [("Team", escape_html(input_.team_name))]
    if input_.agent_type:
        rows.append(("Agent type", escape_html(input_.agent_type)))
    if input_.description:
        rows.append(("Description", escape_html(input_.description)))
    return _render_card("team-card", rows)


def format_teamcreate_output(output: TeamCreateOutput) -> str:
    rows: list[tuple[str, str]] = [("Team", escape_html(output.team_name))]
    if output.lead_agent_id:
        rows.append(("Lead", escape_html(output.lead_agent_id)))
    if output.team_file_path:
        rows.append(("Config", f"<code>{escape_html(output.team_file_path)}</code>"))
    return _render_card("team-card team-create-output", rows)


def format_teamdelete_input(_input: TeamDeleteInput) -> str:
    # TeamDelete takes no meaningful parameters in practice.
    return '<div class="teammate-tool-card team-card team-delete-input"></div>'


def format_teamdelete_output(
    output: TeamDeleteOutput,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    status = "Deleted" if output.success else "Refused"
    rows: list[tuple[str, str]] = [("Status", escape_html(status))]
    if output.team_name:
        rows.append(("Team", escape_html(output.team_name)))
    if output.message:
        rows.append(("Message", escape_html(output.message)))
    if output.active_members:
        badges = " ".join(
            _teammate_badge(m, _lookup_color(teammate_colors, m))
            for m in output.active_members
        )
        rows.append(("Active members", badges))
    css = "team-card team-delete-output"
    if not output.success:
        css += " teammate-refused"
    return _render_card(css, rows)


def _status_pill(status: str) -> str:
    """Render a small-caps status pill with palette color (in_progress → blue,
    completed → green, etc.).

    Mirrors the TaskList row's ``.task-status`` styling so the same
    ``IN PROGRESS`` rendering appears on TaskCreate / TaskUpdate cards.
    Unknown statuses get the default gray.
    """
    status_class = f"status-{status}" if status in _STATUS_CLASSES else "status-unknown"
    return f"<span class='task-status {status_class}'>{escape_html(status)}</span>"


def format_taskcreate_input(input_: TaskCreateInput) -> str:
    """Body for TaskCreate: combined activeForm + description as Markdown.

    Subject moved to the tool-use title (``Task #N <subject> [created]``)
    by ``HtmlRenderer.title_TaskCreateInput``. The remaining fields read
    naturally as a single document: ``activeForm`` is a short
    "in-progress" verb phrase ("Writing relay.py tests"), so it slots in
    as a bold-italic heading; ``description`` is the body that may run
    several paragraphs with lists and code spans. Stitching them with a
    blank-line separator and routing through ``render_markdown_
    collapsible`` produces a flowing document rather than two labeled
    rows. Either field on its own degrades naturally.
    """
    parts: list[str] = []
    if input_.activeForm:
        parts.append(f"***{input_.activeForm}***")
    if input_.description:
        parts.append(input_.description)
    if not parts:
        return ""
    return render_markdown_collapsible("\n\n".join(parts), "task-create-description")


def format_taskcreate_output(_output: TaskCreateOutput) -> str:
    """Tool result body: empty.

    Task id + subject are surfaced via the tool-use title; no separate
    result card is needed.
    """
    return ""


def format_taskupdate_input(
    input_: TaskUpdateInput,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    """Body for TaskUpdate: ``Owner`` (badge) + ``Status`` (pill).

    Task id moved to the tool-use title (``Task #N <subject> [updated]``)
    by ``HtmlRenderer.title_TaskUpdateInput``. The status pill reuses
    the TaskList palette (``in_progress`` → blue, etc.) for visual
    consistency.
    """
    rows: list[tuple[str, str]] = []
    if input_.owner:
        color = _lookup_color(teammate_colors, input_.owner)
        rows.append(("Owner", _teammate_badge(input_.owner, color)))
    if input_.status:
        rows.append(("Status", _status_pill(input_.status)))
    if not rows:
        return ""
    return _render_card("task-update-card", rows)


def format_taskupdate_output(output: TaskUpdateOutput) -> str:
    """Tool result body: empty unless there's something to surface.

    Task id + ``[updated]`` come from the tool-use title; a bare
    ``Status: updated`` row is redundant on success. But on failure
    we MUST surface the negative outcome — otherwise the title alone
    falsely claims success. ``from→to`` transitions are preserved when
    present (information the title can't show).
    """
    if not output.success:
        # Show whatever raw text the tool returned so the reader has
        # something to dig into; fall back to a bare "failed" badge
        # when the parser captured nothing useful.
        rows: list[tuple[str, str]] = [
            ("Status", "<code class='task-update-failed'>failed</code>")
        ]
        if output.raw_text:
            rows.append(("Detail", escape_html(output.raw_text)))
        return _render_card("task-update-card", rows)
    if output.status_change is None:
        return ""
    from_s = output.status_change.from_status
    to_s = output.status_change.to_status
    if not (from_s or to_s):
        return ""
    rows = [
        (
            "Transition",
            f"{_status_pill(from_s or '?')}"
            f"<span class='status-arrow'>→</span>"
            f"{_status_pill(to_s or '?')}",
        )
    ]
    return _render_card("task-update-card", rows)


def format_tasklist_output(
    output: TaskListOutput,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    header = (
        "<thead><tr>"
        "<th class='task-id'>#</th>"
        "<th class='task-status'>Status</th>"
        "<th class='task-subject'>Subject</th>"
        "<th class='task-owner'>Owner</th>"
        "</tr></thead>"
    )
    rows: list[str] = []
    for task in output.tasks:
        rows.append(_format_tasklist_row(task, teammate_colors))
    return f"<table class='task-list'>{header}<tbody>{''.join(rows)}</tbody></table>"


def _format_tasklist_row(
    task: TaskListItem,
    teammate_colors: Optional[dict[str, str]],
) -> str:
    status = task.status or ""
    status_class = f"status-{status}" if status in _STATUS_CLASSES else "status-unknown"
    owner_html = ""
    if task.owner:
        color = _lookup_color(teammate_colors, task.owner)
        owner_html = _teammate_badge(task.owner, color)
    return (
        "<tr>"
        f"<td class='task-id'>#{escape_html(task.id)}</td>"
        f"<td class='task-status {status_class}'>{escape_html(status)}</td>"
        f"<td class='task-subject'>{escape_html(task.subject)}</td>"
        f"<td class='task-owner'>{owner_html}</td>"
        "</tr>"
    )


def format_sendmessage_input(
    input_: SendMessageInput,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    """Body for SendMessage: just the message content as markdown.

    Recipient + ``To`` go in the title via
    ``HtmlRenderer.title_SendMessageInput``. The ``type`` field is
    almost always ``"message"`` and reads as noise; surfaced only when
    it's something else (e.g. a future ``"signal"`` variant).
    """
    del teammate_colors  # recipient now goes in the title
    parts: list[str] = []
    if input_.type and input_.type != "message":
        parts.append(
            f'<div class="send-message-type">'
            f"Type: <code>{escape_html(input_.type)}</code></div>"
        )
    if input_.content:
        parts.append(render_markdown_collapsible(input_.content, "send-message-body"))
    return "".join(parts)


def format_sendmessage_output(
    output: SendMessageOutput,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    status = "Sent" if output.success else "Failed"
    rows: list[tuple[str, str]] = [("Status", escape_html(status))]
    if output.target:
        # Mirror format_sendmessage_input: show the target as a colored
        # badge so the link between request and result is visible.
        color = _lookup_color(teammate_colors, output.target)
        rows.append(("Target", _teammate_badge(output.target, color)))
    if output.request_id:
        rows.append(("Request", f"<code>{escape_html(output.request_id)}</code>"))
    if output.message:
        rows.append(("Message", escape_html(output.message)))
    return _render_card("send-message-card", rows)


# ---------------------------------------------------------------------------
# Task (teammate-spawn) extensions
# ---------------------------------------------------------------------------


def format_task_input_teammate_extras(
    input_: TaskInput,
    teammate_colors: Optional[dict[str, str]] = None,
) -> str:
    """Return a small HTML fragment surfacing teammate-spawn fields on Task.

    Empty string when no teammate fields are present — callers can append
    this fragment to the existing Task rendering.
    """
    rows: list[tuple[str, str]] = []
    if input_.name:
        color = _lookup_color(teammate_colors, input_.name)
        rows.append(("Teammate", _teammate_badge(input_.name, color)))
    if input_.team_name:
        rows.append(("Team", escape_html(input_.team_name)))
    if input_.mode:
        rows.append(("Mode", escape_html(input_.mode)))
    if input_.run_in_background:
        rows.append(("Run", "background"))
    if not rows:
        return ""
    return _render_card("teammate-spawn-card", rows)


def format_task_output_teammate_extras(
    output: TaskOutput,
    teammate_colors: Optional[dict[str, str]] = None,
    include_agent_id: bool = True,
) -> str:
    """Return a fragment for teammate metadata on a Task tool_result.

    Pulls fields from both ``output.metadata`` (parsed from the tail) and
    the teammate-linkage fields on TaskOutput itself. ``include_agent_id``
    off drops the Agent row (the minimal theme's async spawn, whose
    notification card shows the id as its Task ID).
    """
    rows: list[tuple[str, str]] = []
    meta = output.metadata
    if meta and meta.agent_id and include_agent_id:
        rows.append(("Agent", f"<code>{escape_html(meta.agent_id)}</code>"))
    if meta and meta.worktree_path:
        rows.append(("Worktree", f"<code>{escape_html(meta.worktree_path)}</code>"))
    if meta and meta.worktree_branch:
        rows.append(("Branch", f"<code>{escape_html(meta.worktree_branch)}</code>"))
    if meta and (
        meta.total_tokens is not None
        or meta.tool_uses is not None
        or meta.duration_ms is not None
    ):
        rows.append(
            (
                "Usage",
                _format_usage(meta.total_tokens, meta.tool_uses, meta.duration_ms),
            )
        )
    if output.teammate_id:
        # Inline output.color is specific to this agent-spawn result and
        # should win over the session-wide cache learned from earlier
        # <teammate-message> blocks.
        color = output.color or _lookup_color(teammate_colors, output.teammate_id)
        rows.append(("Teammate", _teammate_badge(output.teammate_id, color)))
    if not rows:
        return ""
    return _render_card("teammate-spawn-card", rows)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_card(css_class: str, rows: Iterable[tuple[str, str]]) -> str:
    row_html = "".join(
        f"<dt>{escape_html(label)}</dt><dd>{value}</dd>" for label, value in rows
    )
    return f'<dl class="teammate-tool-card {css_class}">{row_html}</dl>'


def _format_usage(
    total_tokens: Optional[int],
    tool_uses: Optional[int],
    duration_ms: Optional[int],
) -> str:
    parts: list[str] = []
    if total_tokens is not None:
        parts.append(f"{total_tokens:,} tokens")
    if tool_uses is not None:
        parts.append(f"{tool_uses} tool use{'s' if tool_uses != 1 else ''}")
    if duration_ms is not None:
        seconds = duration_ms / 1000.0
        parts.append(f"{seconds:.1f}s")
    return escape_html(" · ".join(parts))


def _lookup_color(
    teammate_colors: Optional[dict[str, str]], name: str
) -> Optional[str]:
    if teammate_colors is None:
        return None
    return teammate_colors.get(name)

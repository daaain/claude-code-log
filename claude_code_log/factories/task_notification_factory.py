"""Factory for ``<task-notification>`` content in User entries (issue #90).

Async-spawned ``Task`` agents (the kind with ``run_in_background=True``)
return their final result via a synthetic User entry that Claude Code
injects into the trunk session when the agent completes. The entry's
``message.content`` is a raw string shaped like::

    <task-notification>
    <task-id>a8b740b</task-id>
    <status>completed</status>
    <summary>Agent "..." completed</summary>
    <result>... markdown of agent's final response ...</result>
    <usage>total_tokens: 23099
    tool_uses: 2
    duration_ms: 15506</usage>
    </task-notification>
    Full transcript available at: /tmp/.../tasks/a8b740b.output

The ``<result>`` body usually duplicates the last sub-assistant
message in the spawned agent's sidechain — Phase 3 of the
async-agents work uses that to fold the result into the spawning
Task's tool_result so the notification card itself can render as a
backlink-only stub.

This module:

- Detects whether a body of text is a ``<task-notification>`` payload.
- Parses the metadata fields, the ``<result>`` body, the ``<usage>``
  block, and the trailing ``Full transcript available at:`` line.
- Wraps the set in a ``TaskNotificationMessage`` content model.

The structure deliberately mirrors ``teammate_factory`` so the user-
factory dispatch can hook the two side by side.
"""

from __future__ import annotations

import re
from typing import Optional

from ..models import (
    MessageMeta,
    TaskNotificationMessage,
    TaskNotificationUsage,
)


_NOTIFICATION_RE = re.compile(
    r"<task-notification>\s*(?P<body>.*?)\s*</task-notification>",
    re.DOTALL,
)

# Single-tag fields inside the notification. ``tool-use-id`` is the
# originating ``toolu_...`` id from the spawning tool_use; surfaced here
# so the renderer can backlink the notification's Task ID value to the
# original tool_use card (#142). Older notifications didn't carry it,
# so the absence is benign — the dict get below returns None.
_FIELD_RE = re.compile(
    r"<(?P<tag>task-id|tool-use-id|status|summary)>(?P<body>.*?)</(?P=tag)>",
    re.DOTALL,
)
# The free-text blocks: an agent's ``<result>`` (its Markdown answer) and a
# Monitor's ``<event>`` (one notification per line its command prints; no
# status, and the summary only names the monitor). Either can quote any
# tag, its own included, so a block runs to the *last* closing tag of its
# kind (greedy): a notification has one of each at most.
_FREE_TEXT_RE = re.compile(r"<(?P<tag>result|event)>(?P<body>.*)</(?P=tag)>", re.DOTALL)
_USAGE_RE = re.compile(r"<usage>\s*(?P<body>.*?)\s*</usage>", re.DOTALL)

# ``Full transcript available at: <path>`` trailer outside the
# notification block. The path can contain spaces — capture rest-of-line.
_TRANSCRIPT_RE = re.compile(
    r"Full transcript available at:\s*(?P<path>.+?)\s*$",
    re.MULTILINE,
)

# Inside <usage>, older Claude Code wrote ``key: value`` lines; current
# versions write child tags (``<subagent_tokens>N</subagent_tokens>…``).
_USAGE_LINE_RE = re.compile(r"^\s*(\w+)\s*:\s*(.+?)\s*$", re.MULTILINE)
_USAGE_TAG_RE = re.compile(r"<(\w+)>\s*([^<]*?)\s*</\1>")
# The token count's name in each form. Other keys (a workflow's
# ``agent_count``, ``agents_done``, …) are not usage and are ignored.
_TOKEN_KEYS = ("total_tokens", "subagent_tokens")


def has_task_notification(text: str) -> bool:
    """Return True iff *text* contains a ``<task-notification>`` block."""
    return "<task-notification>" in text and _NOTIFICATION_RE.search(text) is not None


def _parse_usage(body: str) -> TaskNotificationUsage:
    """Parse a ``<usage>`` block, in either form.

    Child tags (current) take the place of ``key: value`` lines (older);
    the token count is ``subagent_tokens`` in the one and
    ``total_tokens`` in the other. Unknown keys are ignored; values that
    don't parse as ``int`` are skipped (each known field is
    integer-shaped in practice).
    """
    pairs = _USAGE_TAG_RE.findall(body) or [
        (m.group(1), m.group(2)) for m in _USAGE_LINE_RE.finditer(body)
    ]
    fields: dict[str, int] = {}
    for key, value in pairs:
        try:
            fields[key] = int(value)
        except ValueError:
            continue
    tokens = next((fields[k] for k in _TOKEN_KEYS if k in fields), None)
    return TaskNotificationUsage(
        total_tokens=tokens,
        tool_uses=fields.get("tool_uses"),
        duration_ms=fields.get("duration_ms"),
    )


def _take_free_text(body: str) -> tuple[dict[str, str], str]:
    """``({tag: text}, rest)``: the ``<result>`` and ``<event>`` blocks,
    each taken out where it first opens, and the body without them.

    A result is Markdown, trimmed; an event is the command's output, kept
    verbatim (its indentation is part of it).
    """
    taken: dict[str, str] = {}
    rest = body
    while (match := _FREE_TEXT_RE.search(rest)) and match.group("tag") not in taken:
        text = match.group("body")
        taken[match.group("tag")] = (
            text.strip() if match.group("tag") == "result" else text
        )
        rest = rest[: match.start()] + rest[match.end() :]
    return taken, rest


def create_task_notification_message(
    meta: MessageMeta, text: str
) -> Optional[TaskNotificationMessage]:
    """Create a ``TaskNotificationMessage`` for *text* if it carries one.

    Returns ``None`` when no ``<task-notification>`` block is present so
    the caller can fall back to the default User text rendering.
    Malformed payloads (no fields parsed) also return ``None`` —
    surfacing the raw text under the regular User card preserves the
    information until the format firms up.
    """
    notification = _NOTIFICATION_RE.search(text)
    if notification is None:
        return None

    body = notification.group("body")

    # Take the free-text blocks out first, then search what is left for
    # ``<usage>`` and the single-tag header fields. ``<result>`` bodies are
    # agent-authored markdown and frequently contain literal HTML/XML, and
    # an ``<event>`` is whatever the monitored command printed — a
    # ``<summary>demo</summary>`` (or a ``<usage>``) inside either would
    # otherwise clobber the real field, poisoning the downstream
    # fold/dedup path. Whichever block opens first is the real one; a
    # second tag of the same kind is part of its text, never a field.
    free_text, rest = _take_free_text(body)
    result_text = free_text.get("result", "")

    usage_match = _USAGE_RE.search(rest)
    usage = _parse_usage(usage_match.group("body")) if usage_match else None

    header_body = rest
    if usage_match is not None:
        header_body = header_body.replace(usage_match.group(0), "", 1)

    fields: dict[str, str] = {}
    for match in _FIELD_RE.finditer(header_body):
        fields[match.group("tag")] = match.group("body").strip()

    transcript_match = _TRANSCRIPT_RE.search(text)
    transcript_path = (
        transcript_match.group("path") if transcript_match is not None else None
    )

    if not (fields or result_text):
        # ``<task-notification>...</task-notification>`` with nothing
        # we recognise. Don't claim it.
        return None

    return TaskNotificationMessage(
        meta=meta,
        task_id=fields.get("task-id", ""),
        status=fields.get("status", ""),
        summary=fields.get("summary", ""),
        event=free_text.get("event", ""),
        result_text=result_text,
        usage=usage,
        transcript_path=transcript_path,
        raw_text=text,
        tool_use_id=fields.get("tool-use-id") or None,
    )

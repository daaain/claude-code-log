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
# ``event`` is a Monitor's: one notification per line its command prints,
# carrying the line (no status; the summary just names the monitor).
_FIELD_RE = re.compile(
    r"<(?P<tag>task-id|tool-use-id|status|summary|event)>(?P<body>.*?)</(?P=tag)>",
    re.DOTALL,
)
_RESULT_RE = re.compile(r"<result>\s*(?P<body>.*?)\s*</result>", re.DOTALL)
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

    # Extract ``<result>`` and ``<usage>`` first, then strip their full
    # match text from the search surface before scanning for the
    # single-tag header fields. ``<result>`` bodies are agent-authored
    # markdown and frequently contain literal HTML/XML — a
    # ``<summary>demo</summary>`` snippet inside ``<result>`` would
    # otherwise clobber the real notification ``<summary>`` field
    # (and similarly for ``<status>`` / ``<task-id>``), poisoning the
    # downstream fold/dedup path.
    result_match = _RESULT_RE.search(body)
    result_text = result_match.group("body") if result_match else ""

    usage_match = _USAGE_RE.search(body)
    usage = _parse_usage(usage_match.group("body")) if usage_match else None

    header_body = body
    for block_match in (result_match, usage_match):
        if block_match is not None:
            header_body = header_body.replace(block_match.group(0), "", 1)

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
        event=fields.get("event", ""),
        result_text=result_text,
        usage=usage,
        transcript_path=transcript_path,
        raw_text=text,
        tool_use_id=fields.get("tool-use-id") or None,
    )

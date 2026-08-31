"""Factory for <teammate-message> content in User entries.

Teammate messages land in User entries whose `message.content` is a raw
string (loaded as a single TextContent item by create_message_content).
The text carries one or more `<teammate-message>` blocks like:

    <teammate-message teammate_id="alice" color="blue" summary="done">
    body here
    </teammate-message>

Multiple blocks from different teammates can be intermingled, and a
"system" teammate_id is used for termination notices.

This module:

- Detects whether a body of text contains any teammate-message blocks.
- Extracts each block's attributes and body as TeammateMessageBlock.
- Wraps the set in a TeammateMessage content model, preserving any
  surrounding non-block text for rendering context.
- Exposes a small helper used by the converter's prompt-hash subagent
  linking path to recover the "team-lead" prompt body verbatim.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from ..models import (
    MessageMeta,
    TeammateMessage,
    TeammateMessageBlock,
)


# Attribute run inside the opening tag — a sequence of
# ``name="value"`` (or single-quoted) pairs separated by whitespace.
# Strict XML [10] forbids literal ``<``, ``&`` and the matching quote
# inside attribute values; ``>`` is legal. We're parsing free-form
# transcript text rather than well-formed XML though, and real
# summaries routinely include ``&`` ("tests & docs done") or ``>``
# ("15% -> 96% coverage"), so the only character we reject is ``<``
# and the matching quote — anything else is in the value. The original
# naïve ``[^>]*`` truncated openings at the first literal ``>``.
ATTR_RUN = r'(?:\s+[\w][\w-]*(?:\s*=\s*(?:"[^"<]*"|\'[^\'<]*\'))?)*\s*'
_ATTR_RUN = ATTR_RUN

# One full <teammate-message ...>...</teammate-message> block. Attribute
# parsing happens on the opening tag's attribute run (captured group 1).
# Using DOTALL so the body may contain newlines (typically does).
_BLOCK_RE = re.compile(
    rf"<teammate-message\b({ATTR_RUN})>(.*?)</teammate-message>",
    re.DOTALL,
)

# Attribute pairs inside an opening tag. Accepts double or single quotes.
_ATTR_RE = re.compile(r'(\w[\w-]*)\s*=\s*"([^"]*)"|(\w[\w-]*)\s*=\s*\'([^\']*)\'')

# System teammate_id marks terminate/status notifications.
SYSTEM_ID = "system"
_SYSTEM_ID = SYSTEM_ID


def has_teammate_message(text: str) -> bool:
    """Return True iff *text* contains any `<teammate-message>` block."""
    return "<teammate-message" in text and _BLOCK_RE.search(text) is not None


def iter_teammate_blocks(text: str) -> Iterable[TeammateMessageBlock]:
    """Yield TeammateMessageBlock for each `<teammate-message>` block in *text*."""
    for match in _BLOCK_RE.finditer(text):
        attrs = _parse_attrs(match.group(1))
        body = match.group(2).strip("\n")
        teammate_id = attrs.get("teammate_id", "")
        yield TeammateMessageBlock(
            teammate_id=teammate_id,
            body=body,
            color=attrs.get("color"),
            summary=attrs.get("summary"),
            is_system=(teammate_id == _SYSTEM_ID),
        )


def find_team_lead_body(text: str) -> Optional[str]:
    """Return the body of the first `<teammate-message teammate_id="team-lead">`.

    Used by the subagent-linking prompt-hash fallback: when a Task
    tool_use lacks a structured agentId, we match the tool_use's prompt
    input against the team-lead body in a subagent JSONL's first entry.
    """
    for block in iter_teammate_blocks(text):
        if block.teammate_id == "team-lead":
            return block.body
    return None


def create_teammate_message(meta: MessageMeta, text: str) -> Optional[TeammateMessage]:
    """Create a TeammateMessage for *text* if it carries any block.

    Returns None when no `<teammate-message>` block is present so the caller
    can fall back to the default User text rendering.
    """
    if not has_teammate_message(text):
        return None

    blocks = list(iter_teammate_blocks(text))
    if not blocks:
        return None

    leading, trailing = _split_surrounding_text(text)
    return TeammateMessage(
        meta=meta,
        blocks=blocks,
        leading_text=leading,
        trailing_text=trailing,
    )


def _parse_attrs(attr_run: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(attr_run):
        key = match.group(1) or match.group(3)
        value = match.group(2) if match.group(1) else match.group(4)
        if key is not None and value is not None:
            out[key] = value
    return out


def _split_surrounding_text(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return any non-block text before the first / after the last block."""
    first = _BLOCK_RE.search(text)
    if first is None:
        return None, None
    leading = text[: first.start()].strip()

    last_match = None
    for match in _BLOCK_RE.finditer(text):
        last_match = match
    assert last_match is not None  # _BLOCK_RE matched above at least once
    trailing = text[last_match.end() :].strip()

    return (leading or None, trailing or None)

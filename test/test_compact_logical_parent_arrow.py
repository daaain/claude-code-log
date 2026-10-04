"""The debug uuid line of a ``/compact`` boundary shows its logical parent.

A ``compact_boundary`` has ``parentUuid: null`` and names the message it
continues in ``logicalParentUuid``. The debug line prints ``uuid → parent``,
so without a fallback every /compact looked like a broken chain. The
boundary now renders ``uuid ⇢ logical`` (a dashed arrow, so it reads as
different from a real ``parentUuid``), while ``meta.parent_uuid`` stays
empty — it drives parent lookup and pairing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

USER_UUID = "11111111-aaaa-0000-0000-000000000001"
ASSISTANT_UUID = "22222222-bbbb-0000-0000-000000000002"
BOUNDARY_UUID = "33333333-cccc-0000-0000-000000000003"
SUMMARY_UUID = "44444444-dddd-0000-0000-000000000004"

DASHED = "<span title='logical parent (across /compact)'>&#8674;</span>"


def _entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.0",
    }
    base.update(fields)
    return base


def _render(
    tmp_path: Path, logical_parent: Optional[str], theme: str = "classic"
) -> str:
    boundary = _entry(
        type="system",
        subtype="compact_boundary",
        timestamp="2025-07-01T10:02:00.000Z",
        parentUuid=None,
        uuid=BOUNDARY_UUID,
        content="Conversation compacted",
        level="info",
        compactMetadata={"trigger": "manual", "preTokens": 120000},
    )
    if logical_parent is not None:
        boundary["logicalParentUuid"] = logical_parent
    entries = [
        _entry(
            type="user",
            timestamp="2025-07-01T10:00:00.000Z",
            parentUuid=None,
            uuid=USER_UUID,
            message={"role": "user", "content": "hello"},
        ),
        _entry(
            type="assistant",
            timestamp="2025-07-01T10:01:00.000Z",
            parentUuid=USER_UUID,
            uuid=ASSISTANT_UUID,
            message={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude",
                "content": [{"type": "text", "text": "hi there"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        boundary,
        _entry(
            type="user",
            timestamp="2025-07-01T10:02:01.000Z",
            parentUuid=BOUNDARY_UUID,
            uuid=SUMMARY_UUID,
            isCompactSummary=True,
            message={"role": "user", "content": "Summary of the conversation."},
        ),
    ]
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return generate_html(load_transcript(path), "compact", theme=theme)


def _debug_line(uuid: str, rest: str = "") -> str:
    return f"<div class='debug-info'>{uuid[:12]}{rest}</div>"


def test_boundary_with_logical_parent_shows_dashed_arrow(tmp_path: Path):
    html = _render(tmp_path, ASSISTANT_UUID)
    assert _debug_line(BOUNDARY_UUID, f" {DASHED} {ASSISTANT_UUID[:12]}") in html, (
        "boundary should point at its logical parent with the dashed arrow"
    )
    # Only the boundary carries it.
    assert html.count(DASHED) == 1


def test_boundary_without_logical_parent_shows_no_arrow(tmp_path: Path):
    html = _render(tmp_path, None)
    assert _debug_line(BOUNDARY_UUID) in html
    assert "&#8674;" not in html


def test_ordinary_messages_keep_the_solid_arrow(tmp_path: Path):
    html = _render(tmp_path, ASSISTANT_UUID)
    assert _debug_line(USER_UUID) in html
    assert _debug_line(ASSISTANT_UUID, f" &rarr; {USER_UUID[:12]}") in html
    assert _debug_line(SUMMARY_UUID, f" &rarr; {BOUNDARY_UUID[:12]}") in html


def test_minimal_theme_boundary_shows_dashed_arrow(tmp_path: Path):
    """The minimal theme shares the debug uuid line, arrow and tooltip."""
    html = _render(tmp_path, ASSISTANT_UUID, theme="minimal")
    assert "theme-minimal" in html
    assert _debug_line(BOUNDARY_UUID, f" {DASHED} {ASSISTANT_UUID[:12]}") in html, (
        "minimal boundary should point at its logical parent with the dashed arrow"
    )
    assert html.count(DASHED) == 1
    # Ordinary messages keep the solid arrow.
    assert _debug_line(ASSISTANT_UUID, f" &rarr; {USER_UUID[:12]}") in html
    assert _debug_line(SUMMARY_UUID, f" &rarr; {BOUNDARY_UUID[:12]}") in html

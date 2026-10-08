"""A session preview skips the first message's leading YAML front matter.

The preview (the first user message's label in the table of contents,
the index cards and the TUI) starts at the body, as the rendering shows
it. It uses the rendering's own detector, so a ``---`` followed by a
blank line is a horizontal rule and stays in the preview. A message
holding only front matter gives an empty preview, and the next user
message supplies it, as for any empty first message.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import compute_session_data, load_transcript
from claude_code_log.renderer import _collect_session_info
from claude_code_log.utils import create_session_preview

FRONT_MATTER = "---\nname: release-checklist\ndescription: steps\n---\n\n"
BODY = "Please tag the release once CI is green."


class TestCreateSessionPreview:
    def test_front_matter_is_skipped(self) -> None:
        assert create_session_preview(FRONT_MATTER + BODY) == BODY

    def test_front_matter_only_is_empty(self) -> None:
        assert create_session_preview("---\nonly: front matter\n---\n") == ""

    def test_a_leading_rule_is_unchanged(self) -> None:
        text = "---\n\nA rule first, then text."
        assert create_session_preview(text) == text

    def test_ordinary_previews_are_unchanged(self) -> None:
        assert create_session_preview(BODY) == BODY
        assert create_session_preview("A note\n---\nlate: block\n---") == (
            "A note\n---\nlate: block\n---"
        )

    def test_front_matter_after_a_system_reminder(self) -> None:
        # The reminder is not part of the rendered text, so the front
        # matter is still at its start.
        text = "<system-reminder>context</system-reminder>\n" + FRONT_MATTER + BODY
        assert create_session_preview(text) == BODY


def _user(uuid: str, parent: str | None, minute: int, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": f"2026-04-19T10:{minute:02d}:00.000Z",
        "parentUuid": parent,
        "uuid": uuid,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.34",
        "message": {"role": "user", "content": text},
    }


@pytest.fixture
def messages(tmp_path: Path, request: pytest.FixtureRequest) -> Any:
    first, second = request.param
    entries = [_user("u1", None, 0, first), _user("u2", "u1", 1, second)]
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return load_transcript(path)


CASES = [
    pytest.param((FRONT_MATTER + BODY, "A later question."), BODY, id="front-matter"),
    pytest.param(
        ("---\nonly: front matter\n---\n", "A later question."),
        "A later question.",
        id="front-matter-only",
    ),
    pytest.param((BODY, "A later question."), BODY, id="ordinary"),
]


@pytest.mark.parametrize(("messages", "expected"), CASES, indirect=["messages"])
class TestSessionPreviews:
    def test_cached_session_data(self, messages: Any, expected: str) -> None:
        assert compute_session_data(messages)["s1"].first_user_message == expected

    def test_session_navigation(self, messages: Any, expected: str) -> None:
        sessions, _, _ = _collect_session_info(messages, {})
        assert sessions["s1"]["first_user_message"] == expected

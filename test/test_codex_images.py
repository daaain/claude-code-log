"""Codex user-image normalization."""

from __future__ import annotations

import base64
from pathlib import Path

from claude_code_log.models import ImageContent, TextContent, UserTranscriptEntry
from claude_code_log.providers.codex import (
    CodexProvider,
    CodexSessionIdentity,
    _DecodedRecord,
)


THREAD_ID = "88888888-8888-4888-8888-888888888888"


def _normalize(content: list[dict[str, str]]) -> UserTranscriptEntry:
    record = _DecodedRecord(
        line_no=1,
        timestamp="2026-07-16T12:00:00Z",
        kind="response_item",
        payload={"type": "message", "role": "user", "content": content},
    )
    identity = CodexSessionIdentity(thread_id=THREAD_ID, path=Path("rollout.jsonl"))
    entries = list(CodexProvider()._normalize_records(identity, [record], None))
    assert len(entries) == 1
    assert isinstance(entries[0], UserTranscriptEntry)
    return entries[0]


def test_readable_image_paths_become_ordered_base64_content(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.webp"
    first.write_bytes(b"png payload")
    second.write_bytes(b"webp payload")

    entry = _normalize(
        [
            {
                "type": "input_text",
                "text": f'<image name=[Image #1] path="{first}">',
            },
            {"type": "input_image", "image_url": "data:image/png;base64,c3RhbGU="},
            {"type": "input_text", "text": "</image>"},
            {
                "type": "input_text",
                "text": (
                    f'<image path="{second}" name="[Image #2]"></image>'
                    "Before [Image #1], between [Image #2], after."
                ),
            },
        ]
    )

    assert [type(item) for item in entry.message.content] == [
        TextContent,
        ImageContent,
        TextContent,
        ImageContent,
        TextContent,
    ]
    images = [
        item for item in entry.message.content if isinstance(item, ImageContent)
    ]
    assert [(image.source.media_type, image.source.data) for image in images] == [
        ("image/png", base64.b64encode(first.read_bytes()).decode("ascii")),
        ("image/webp", base64.b64encode(second.read_bytes()).decode("ascii")),
    ]
    assert "stale" not in str(entry.message.content)
    assert "<image" not in str(entry.message.content)
    assert "</image>" not in str(entry.message.content)


def test_missing_or_unsupported_images_leave_code_placeholders(tmp_path: Path) -> None:
    unsupported = tmp_path / "not-an-image.txt"
    unsupported.write_text("not an image", encoding="utf-8")
    missing = tmp_path / "missing.png"

    entry = _normalize(
        [
            {
                "type": "input_text",
                "text": (
                    f'<image name=[Image #1] path="{missing}"></image>'
                    f'<IMAGE name="[Image #2]" path="{unsupported}"></IMAGE>'
                    "Missing [Image #1] and unsupported [Image #2]."
                ),
            }
        ]
    )

    assert entry.message.content == [
        TextContent(
            type="text",
            text="Missing `[Image #1]` and unsupported `[Image #2]`.",
        )
    ]
    assert "<image" not in str(entry.message.content).lower()


def test_image_tag_without_a_path_is_removed_and_placeholder_is_code() -> None:
    entry = _normalize(
        [
            {
                "type": "input_text",
                "text": "<image name=[Image #7]>Look: [Image #7]</image>",
            }
        ]
    )

    assert entry.message.content == [
        TextContent(type="text", text="Look: `[Image #7]`")
    ]

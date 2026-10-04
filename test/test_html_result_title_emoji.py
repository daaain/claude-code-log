"""A tool_result title that starts with an emoji replaces the host icon.

The transcript template prefixes a result header with 🧰 (🚨 on error)
unless the title already carries its own icon — the rule tool_use
headers had for 🛠️. A plugin result titled ``📨 Mails …`` used to
render ``🧰 📨 Mails …``: two icons, the host's first.

Driven through ``HtmlRenderer.generate()`` so pairing, depth filtering
and the real template header all run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Optional

import pytest

from claude_code_log import plugins
from claude_code_log.converter import load_transcript
from claude_code_log.models import (
    MessageContent,
    MessageMeta,
    RenderingDepth,
    ToolResultMessage,
)
from claude_code_log.renderer import Renderer, TemplateMessage, get_renderer

SESSION = "sess-result-emoji"
TOOL_NAME = "mcp__test_local__mails"
TITLE = "📨 Mails from test"

# Header span of each tool_result card (standalone or pair_last).
_RESULT_HEADER = re.compile(
    r"<div class='message tool_result[^']*'[^>]*>\s*"
    r"<div class='header'>\s*<span[^>]*>(.*?)</span>",
    re.DOTALL,
)


def _entry(
    uuid: str, parent: Optional[str], ts: str, role: str, content: list[dict[str, Any]]
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": role, "content": content}
    entry: dict[str, Any] = {
        "type": role,
        "timestamp": ts,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": SESSION,
        "version": "2.1.0",
        "uuid": uuid,
        "message": message,
    }
    if role == "assistant":
        entry["requestId"] = f"req-{uuid}"
        message.update(
            id=f"msg-{uuid}",
            type="message",
            model="claude-opus-4-7",
            stop_reason="tool_use",
            stop_sequence=None,
        )
    return entry


def _entries(is_error: bool = False) -> list[dict[str, Any]]:
    return [
        _entry(
            "u1",
            None,
            "2026-10-04T09:00:00Z",
            "user",
            [{"type": "text", "text": "hello"}],
        ),
        _entry(
            "a1",
            "u1",
            "2026-10-04T09:00:01Z",
            "assistant",
            [{"type": "tool_use", "id": "tu_1", "name": TOOL_NAME, "input": {}}],
        ),
        _entry(
            "u2",
            "a1",
            "2026-10-04T09:00:02Z",
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "mail body",
                    "is_error": is_error,
                }
            ],
        ),
    ]


def _result_headers(tmp_path: Path, depth: RenderingDepth, is_error: bool) -> list[str]:
    path = tmp_path / "t.jsonl"
    path.write_text(
        "\n".join(json.dumps(e) for e in _entries(is_error)) + "\n", encoding="utf-8"
    )
    html = get_renderer("html", depth=depth).generate(load_transcript(path))
    assert html is not None
    return [h.strip() for h in _RESULT_HEADER.findall(html)]


@dataclass
class _EmojiTitleResult(ToolResultMessage):
    """Plugin result visible at USER depth, titled with its own icon."""

    depth_visibility: ClassVar[RenderingDepth] = RenderingDepth.USER

    def format_markdown(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return "mail body"

    def title(self, _renderer: Renderer, _message: TemplateMessage) -> Optional[str]:
        return TITLE


class _EmojiTitleResultTransformer:
    name: ClassVar[str] = "test.emoji_title.result"
    priority: ClassVar[int] = 0
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolResultMessage,)

    def transform(
        self, content: MessageContent, _meta: MessageMeta
    ) -> Optional[MessageContent]:
        if not isinstance(content, ToolResultMessage) or content.tool_name != TOOL_NAME:
            return None
        return _EmojiTitleResult(
            meta=content.meta,
            tool_use_id=content.tool_use_id,
            output=content.output,
            is_error=content.is_error,
            tool_name=content.tool_name,
            file_path=content.file_path,
        )


@pytest.fixture(autouse=True)
def emoji_title_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugins, "_cached_transformers", [_EmojiTitleResultTransformer()]
    )


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(RenderingDepth.AGENT, id="standalone"),
        pytest.param(RenderingDepth.TOOL, id="paired"),
    ],
)
@pytest.mark.parametrize("is_error", [False, True], ids=["ok", "error"])
def test_result_title_emoji_replaces_host_icon(
    tmp_path: Path, depth: RenderingDepth, is_error: bool
) -> None:
    headers = _result_headers(tmp_path, depth, is_error)
    assert headers == [TITLE]

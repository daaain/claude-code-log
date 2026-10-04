"""An empty title suppresses the Markdown HEADING, never the body.

A non-error ``ToolResultMessage`` titles itself ``""`` on purpose
(``Renderer.title_ToolResultMessage``): paired, it renders under its
tool_use's heading. Standing alone — an orphan whose tool_use is absent,
or one whose tool_use was ghosted by ``--depth`` — it has no heading,
and Markdown used to drop its body with the heading while HTML kept it.

Driven through ``generate()`` so pairing, depth filtering and the
``_render_message`` heading/body split all run as in a real conversion.
"""

from __future__ import annotations

import json
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

SESSION = "sess-empty-title"
TOOL_NAME = "mcp__test_local__empty_title"
BODY = "EMPTY_TITLE_RESULT_BODY"


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


def _prompt() -> dict[str, Any]:
    return _entry(
        "u1", None, "2026-10-04T09:00:00Z", "user", [{"type": "text", "text": "hello"}]
    )


def _tool_use(tool_use_id: str) -> dict[str, Any]:
    return _entry(
        "a1",
        "u1",
        "2026-10-04T09:00:01Z",
        "assistant",
        [{"type": "tool_use", "id": tool_use_id, "name": TOOL_NAME, "input": {}}],
    )


def _tool_result(tool_use_id: str, parent: str) -> dict[str, Any]:
    return _entry(
        "u2",
        parent,
        "2026-10-04T09:00:02Z",
        "user",
        [{"type": "tool_result", "tool_use_id": tool_use_id, "content": BODY}],
    )


def _render(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    fmt: str,
    depth: RenderingDepth = RenderingDepth.HOOK,
    compact: bool = False,
) -> str:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    renderer = get_renderer(fmt, depth=depth, compact=compact)
    out = renderer.generate(load_transcript(path))
    assert out is not None
    return out


@dataclass
class _EmptyTitleResult(ToolResultMessage):
    """Plugin result visible at USER depth, with an explicitly empty title."""

    depth_visibility: ClassVar[RenderingDepth] = RenderingDepth.USER

    def format_markdown(self, _renderer: Renderer, _message: TemplateMessage) -> str:
        return BODY

    def title(self, _renderer: Renderer, _message: TemplateMessage) -> Optional[str]:
        return ""


class _EmptyTitleResultTransformer:
    name: ClassVar[str] = "test.empty_title.result"
    priority: ClassVar[int] = 0
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolResultMessage,)

    def transform(
        self, content: MessageContent, _meta: MessageMeta
    ) -> Optional[MessageContent]:
        if not isinstance(content, ToolResultMessage) or content.tool_name != TOOL_NAME:
            return None
        return _EmptyTitleResult(
            meta=content.meta,
            tool_use_id=content.tool_use_id,
            output=content.output,
            is_error=content.is_error,
            tool_name=content.tool_name,
            file_path=content.file_path,
        )


@pytest.fixture
def empty_title_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugins, "_cached_transformers", [_EmptyTitleResultTransformer()]
    )


@pytest.mark.parametrize("compact", [False, True])
def test_orphan_tool_result_body_renders(tmp_path: Path, compact: bool) -> None:
    """Built-in path, no plugin: a tool_result whose tool_use is absent."""
    entries = [_prompt(), _tool_result("tu_missing", parent="u1")]
    html = _render(tmp_path, entries, "html")
    md = _render(tmp_path, entries, "md", compact=compact)
    assert BODY in html
    assert md.count(BODY) == 1


def test_standalone_empty_title_result_at_agent_depth(
    tmp_path: Path, empty_title_plugin: None
) -> None:
    """The tool_use (TOOL depth) is ghosted at AGENT; the USER-visible
    result stands alone with an empty title and must keep its body."""
    entries = [_prompt(), _tool_use("tu_1"), _tool_result("tu_1", parent="a1")]
    md = _render(tmp_path, entries, "md", depth=RenderingDepth.AGENT)
    assert md.count(BODY) == 1


def test_paired_empty_title_result_body_once(
    tmp_path: Path, empty_title_plugin: None
) -> None:
    """Paired at TOOL depth the body renders under the tool_use heading —
    once, not again from the result's own (headless) slot."""
    entries = [_prompt(), _tool_use("tu_1"), _tool_result("tu_1", parent="a1")]
    md = _render(tmp_path, entries, "md", depth=RenderingDepth.TOOL)
    assert md.count(BODY) == 1

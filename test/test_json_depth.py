"""Deeply nested JSON from a transcript never fails a render.

``json.loads`` raises ``RecursionError`` past its nesting limit, and the
params renderers recurse once per level of a decoded value. Every site
that decodes transcript JSON catches the first, and a value nested
deeper than ``json_depth.MAX_DATA_DEPTH`` is not shown as a table.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.converter import load_transcript
from claude_code_log.factories.tool_factory import _try_load_json_text
from claude_code_log.html.renderer import generate_html
from claude_code_log.html.tool_formatters import _json_result_table_html
from claude_code_log.html.user_formatters import format_workflow_sidechannel_user_text
from claude_code_log.html.utils import render_async_result_body
from claude_code_log.json_depth import MAX_DATA_DEPTH, exceeds_depth
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import ToolResultContent

PAST_DECODER = 100_000  # json.loads raises RecursionError
PAST_BOUND = 500  # decodes, but too deep for nested tables


def _nested(depth: int) -> str:
    return '{"a": ' + "[" * depth + "]" * depth + "}"


def _entry(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "s1",
        "version": "2.1.34",
    }
    base.update(fields)
    return base


def _write_tool_result(tmp_path: Path, content: str) -> Path:
    entries = [
        _entry(
            type="user",
            timestamp="2026-04-19T10:00:00.000Z",
            parentUuid=None,
            uuid="u1",
            message={"role": "user", "content": "Before the tool."},
        ),
        _entry(
            type="assistant",
            timestamp="2026-04-19T10:01:00.000Z",
            parentUuid="u1",
            uuid="a1",
            requestId="req-a1",
            message={
                "id": "msg-a1",
                "type": "message",
                "role": "assistant",
                "model": "claude-test",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_deep",
                        "name": "mcp__probe__fetch",
                        "input": {},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
        _entry(
            type="user",
            timestamp="2026-04-19T10:02:00.000Z",
            parentUuid="a1",
            uuid="u2",
            message={
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_deep",
                        "content": content,
                    }
                ],
            },
        ),
        _entry(
            type="user",
            timestamp="2026-04-19T10:03:00.000Z",
            parentUuid="u2",
            uuid="u3",
            message={"role": "user", "content": "After the tool."},
        ),
    ]
    path = tmp_path / "s1.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "depth", [PAST_DECODER, PAST_BOUND], ids=["past-decoder", "past-bound"]
)
class TestDeepToolResult:
    @pytest.mark.parametrize("theme", ["classic", "minimal"])
    def test_html(self, tmp_path: Path, theme: str, depth: int) -> None:
        messages = load_transcript(_write_tool_result(tmp_path, _nested(depth)))
        html = generate_html(messages, "deep", theme=theme)
        assert "Before the tool." in html and "After the tool." in html
        assert "tool-result-json" not in html

    def test_markdown(self, tmp_path: Path, depth: int) -> None:
        messages = load_transcript(_write_tool_result(tmp_path, _nested(depth)))
        md = MarkdownRenderer().generate(messages, "deep")
        assert "Before the tool." in md and "After the tool." in md


@pytest.mark.parametrize(
    "depth", [PAST_DECODER, PAST_BOUND], ids=["past-decoder", "past-bound"]
)
class TestOtherDecodeSites:
    def test_tool_result_table(self, depth: int) -> None:
        assert _json_result_table_html(_nested(depth)) is None

    def test_async_result_body(self, depth: int) -> None:
        html = render_async_result_body(_nested(depth), "task-result")
        assert "task-result" in html

    def test_sidechannel_embedded_block(self, depth: int) -> None:
        block = '{\n"a": ' + "[" * depth + "]" * depth + "\n}"
        html = format_workflow_sidechannel_user_text(f"Intro.\n\n{block}\n\nOutro.")
        assert "Outro." in html
        assert "embedded-json" not in html

    def test_teammate_tool_output_text(self, depth: int) -> None:
        # This site feeds typed fields, not a table: only the decode guard.
        result = ToolResultContent(
            type="tool_result", tool_use_id="t", content=_nested(depth)
        )
        loaded = _try_load_json_text(result)
        assert loaded is None if depth == PAST_DECODER else loaded is not None


class TestExceedsDepth:
    def test_boundary(self) -> None:
        chain: dict[str, Any] = {}
        node = chain
        for _ in range(MAX_DATA_DEPTH - 1):
            node["a"] = {}
            node = node["a"]
        assert not exceeds_depth(chain)  # MAX_DATA_DEPTH containers
        node["a"] = {}
        assert exceeds_depth(chain)

    def test_scalars_and_flat(self) -> None:
        assert not exceeds_depth(5)
        assert not exceeds_depth({})
        assert not exceeds_depth({f"k{i}": i for i in range(10_000)})
        assert exceeds_depth([[[]]], limit=2)
        assert not exceeds_depth([[[]]], limit=3)

    def test_shallow_json_still_tabulates(self) -> None:
        html = _json_result_table_html('{"a": [1, {"b": 2}]}')
        assert html is not None and "tool-params-table" in html

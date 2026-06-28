"""XSS: the message-TITLE path must escape transcript-derived fields (#245).

The header renders ``{{ message_title | safe }}`` — titles legitimately carry
structural HTML (e.g. the spawn-collapsed ``<span>`` marker), so there is no
central escaping. Each ``title_*`` method that interpolates a transcript field
must therefore escape that field on the HTML path. daaain's PR secured the
message *body* (the shared Markdown renderer → ``escape=True``) but left four
title sinks unescaped; these pin them.

The four sinks (all on the shared base ``Renderer``; the HTML renderer escapes
on its side only, so the Markdown renderer doesn't get HTML-entity-escaped
titles):

1. generic / mcp__* / custom tool name — ``title_ToolUseMessage`` fallback
2. hook name — ``title_HookAttachmentMessage``
3. workflow phase title — ``title_WorkflowPhaseMessage``
4. workflow agent label — ``title_WorkflowAgentMessage``
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import HtmlRenderer, generate_html
from claude_code_log.models import (
    BashInput,
    HookAttachmentMessage,
    MessageMeta,
    ToolUseMessage,
    WorkflowAgentMessage,
    WorkflowPhaseMessage,
)
from claude_code_log.renderer import TemplateMessage

PAYLOAD = "<img src=x onerror=alert(1)>"
ESCAPED = "&lt;img src=x onerror=alert(1)&gt;"


def _meta() -> MessageMeta:
    return MessageMeta(uuid="u", session_id="s", timestamp="2025-01-01T00:00:00Z")


def _title(content) -> str:
    msg = TemplateMessage(content)
    return HtmlRenderer().title_content(msg)


class TestTitlePathEscaping:
    def test_generic_tool_name_escaped(self, tmp_path: Path):
        # A tool with no specialized title method (generic / mcp__* / custom)
        # falls back to its raw name in the header. Exercise the real render
        # path with the payload AS the tool name.
        rows = [
            {
                "type": "user",
                "uuid": "u0",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "external",
                "cwd": "/x",
                "sessionId": "s1",
                "version": "1.0",
                "timestamp": "2025-01-01T00:00:00Z",
                "message": {"role": "user", "content": "go"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "parentUuid": "u0",
                "isSidechain": False,
                "userType": "external",
                "cwd": "/x",
                "sessionId": "s1",
                "version": "1.0",
                "timestamp": "2025-01-01T00:00:01Z",
                "requestId": "r1",
                "message": {
                    "id": "m1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude",
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": PAYLOAD, "input": {}}
                    ],
                },
            },
        ]
        f = tmp_path / "x.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        html = generate_html(load_transcript(f, silent=True), "x")
        assert PAYLOAD not in html
        assert ESCAPED in html

    def test_hook_name_escaped(self):
        content = HookAttachmentMessage(meta=_meta(), kind="success", hook_name=PAYLOAD)
        out = _title(content)
        assert out.startswith("Hook · ")
        assert PAYLOAD not in out
        assert ESCAPED in out

    def test_workflow_phase_title_escaped(self):
        content = WorkflowPhaseMessage(meta=_meta(), title=PAYLOAD)
        out = _title(content)
        assert out.startswith("Phase: ")
        assert PAYLOAD not in out
        assert ESCAPED in out

    def test_workflow_agent_label_escaped(self):
        content = WorkflowAgentMessage(meta=_meta(), label=PAYLOAD)
        out = _title(content)
        assert out.startswith("Agent ")
        assert PAYLOAD not in out
        assert ESCAPED in out

    def test_specialized_tool_name_still_escaped_and_unbroken(self):
        # Regression guard: the specialized path (BashInput → _tool_title)
        # already escapes; a benign tool name renders normally.
        content = ToolUseMessage(
            meta=_meta(),
            input=BashInput(command="ls", description="list"),
            tool_use_id="t1",
            tool_name="Bash",
        )
        out = _title(content)
        assert "Bash" in out
        assert "&lt;" not in out  # nothing to escape, not mangled

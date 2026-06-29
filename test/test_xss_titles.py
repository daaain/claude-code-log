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
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    BashInput,
    HookAttachmentMessage,
    MessageMeta,
    SystemMessage,
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

    def test_system_level_escaped(self):
        # ``level`` is free-text from the transcript, not an enum. The title is
        # ``System {level.title()}``; the title-casing folds the tag name's
        # case (``<Img …>``) but the tag would still fire (HTML attrs are
        # case-insensitive), so it must be escaped AFTER ``.title()``.
        content = SystemMessage(meta=_meta(), level=PAYLOAD, text="x")
        out = _title(content)
        assert out.startswith("System ")
        # No live tag at any case; the dangerous ``<`` is entity-escaped, and
        # ``.title()`` didn't corrupt the entity (no ``&Lt;``).
        assert "<img" not in out.lower()
        assert "&lt;" in out and "&Lt;" not in out

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


class TestMarkdownTitlePathProtected:
    """The Markdown *output* reaches the same title fields at one
    format-neutral heading site (markdown/renderer.py); they must be
    neutralised there too (via ``_protect_html_tags``, markdown-appropriate)
    so the .md heading doesn't carry a raw ``<img onerror=…>`` for a
    downstream viewer to execute (#245)."""

    def test_generic_tool_name_protected_in_markdown_heading(self, tmp_path: Path):
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
        md = MarkdownRenderer().generate(load_transcript(f, silent=True), "x")
        # The raw tag must not survive into the heading…
        assert f"# {PAYLOAD}" not in md
        assert "<img" not in md.lower()
        # …it's entity-escaped instead.
        assert "&lt;img" in md.lower()

    def test_project_display_name_protected_in_index_heading(self):
        # Real path (not a synthetic title arg): a project's display_name is
        # derived from its cwd — get_project_display_name returns Path(cwd).name
        # — so a crafted cwd lands the payload in the `##` project heading of
        # the Markdown index. Exercise that end to end.
        project_summaries = [
            {
                "name": "-home-u-proj",
                "html_file": "proj/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 1,
                "last_modified": 1700000000.0,
                "working_directories": [f"/home/u/{PAYLOAD}"],
            }
        ]
        md = MarkdownRenderer().generate_projects_index(project_summaries)
        # The crafted cwd basename IS the payload; the heading must neutralise
        # it, not emit a raw tag.
        assert "## [<img" not in md.lower()
        assert "## <img" not in md.lower()
        assert "&lt;img" in md.lower()

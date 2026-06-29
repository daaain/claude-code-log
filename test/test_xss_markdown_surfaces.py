"""XSS: every Markdown interpolation surface routes through one gate (#245).

Round-4 follow-up: the per-message + page/project/session headings were gated
last; this generalises the gate to `safe_markdown_inline` and routes the inline
link-label / list surfaces through it too — so "neutralise raw HTML from every
source" is a single structural property, not a per-site convention.

Surfaces (markdown/renderer.py), each driven end-to-end here:
- WebSearch result link title          → format_WebSearchOutput
- projects-index project heading + link → generate_projects_index
- per-project session-link label        → generate_projects_index (combined off)
- expand-paths tree label               → generate_projects_index (expand_paths)
- (TOC label + headings are pinned in test_xss_titles.py)
"""

from __future__ import annotations

from typing import Any

from claude_code_log.markdown.renderer import MarkdownRenderer, safe_markdown_inline
from claude_code_log.models import (
    MessageMeta,
    SystemMessage,
    WebSearchLink,
    WebSearchOutput,
)
from claude_code_log.renderer import TemplateMessage

PAYLOAD = "<img src=x onerror=alert(1)>"


def _no_raw_tag(md: str) -> None:
    assert "<img" not in md.lower(), md
    assert "&lt;img" in md.lower(), md


# ----------------------------- the gate itself -------------------------------


class TestSafeMdInlineGate:
    def test_tag_is_entity_escaped(self):
        assert safe_markdown_inline(PAYLOAD) == "&lt;img src=x onerror=alert(1)&gt;"

    def test_plain_text_passes_through_byte_identical(self):
        # No `<` → no mistune round-trip → markdown escaping not re-normalised.
        for s in ("just text", "a **bold** label", r"escaped \*\*stars\*\*", ""):
            assert safe_markdown_inline(s) == s


# ----------------------------- inline surfaces -------------------------------


class TestMarkdownInlineSurfaces:
    def test_websearch_link_title(self):
        out = WebSearchOutput(
            query="q",
            links=[WebSearchLink(title=PAYLOAD, url="https://example.com")],
        )
        # format_WebSearchOutput ignores the message arg; any real
        # TemplateMessage satisfies the signature.
        msg = TemplateMessage(
            SystemMessage(
                meta=MessageMeta(
                    uuid="u", session_id="s", timestamp="2025-01-01T00:00:00Z"
                ),
                level="info",
                text="x",
            )
        )
        md = MarkdownRenderer().format_WebSearchOutput(out, msg)
        _no_raw_tag(md)
        # The URL is preserved (only the label fragment is neutralised).
        assert "(https://example.com)" in md

    def test_project_index_heading_and_session_link(self):
        # combined_suppressed → plain `## {display_name}` heading + per-session
        # `- [{summary}](file)` links. Payload in BOTH the cwd-derived
        # display_name and a session summary.
        project_summaries: list[dict[str, Any]] = [
            {
                "name": "-home-u-proj",
                "html_file": "proj/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 1,
                "last_modified": 1700000000.0,
                "working_directories": [f"/home/u/{PAYLOAD}"],
                "combined_suppressed": True,
                "sessions": [
                    {
                        "id": "11110000",
                        "file": "proj/session-11110000.md",
                        "summary": PAYLOAD,
                    }
                ],
            }
        ]
        md = MarkdownRenderer().generate_projects_index(project_summaries)
        _no_raw_tag(md)
        assert "## <img" not in md.lower()  # heading neutralised
        assert "- [<img" not in md.lower()  # session-link label neutralised
        assert "(proj/session-11110000.md)" in md  # link target preserved

    def test_expand_paths_tree_label(self):
        # expand_paths_tree mode builds the index as a directory tree whose
        # leaf labels are session labels (summary-derived).
        project_summaries: list[dict[str, Any]] = [
            {
                "name": "-home-u-proj",
                "html_file": "proj/combined_transcripts.html",
                "jsonl_count": 1,
                "message_count": 1,
                "last_modified": 1700000000.0,
                "working_directories": ["/home/u/proj"],
                "combined_suppressed": True,
                "sessions": [
                    {
                        "id": "11110000",
                        "file": "proj/session-11110000.md",
                        "summary": PAYLOAD,
                    }
                ],
            }
        ]
        md = MarkdownRenderer().generate_projects_index(
            project_summaries, expand_paths_tree=True
        )
        _no_raw_tag(md)
        assert "[<img" not in md.lower()

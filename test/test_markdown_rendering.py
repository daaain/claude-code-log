#!/usr/bin/env python3
"""Test cases for server-side markdown rendering."""

import json
import tempfile
from pathlib import Path
from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html


def test_server_side_markdown_rendering():
    """Test that markdown is rendered server-side and marked.js is not included."""
    # Assistant message with markdown content
    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:44:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "assistant",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_md_001",
        "requestId": "req_001",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {
                    "type": "text",
                    "text": "# Test Markdown\n\nThis is **bold** text and `code` inline.",
                }
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(assistant_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Test Transcript")

        # Should NOT include marked.js script references
        assert "marked" not in html, "Should not include marked.js reference"
        assert "import { marked }" not in html, "Should not import marked module"
        assert "marked.parse" not in html, "Should not use marked.parse function"
        assert "DOMContentLoaded" not in html or "marked" not in html, (
            "Should not have markdown-related DOM handlers"
        )

        # Should include rendered HTML from markdown
        assert "<h1>Test Markdown</h1>" in html, (
            "Should render markdown heading as HTML"
        )
        assert "<strong>bold</strong>" in html, "Should render bold text as HTML"
        assert "<code>code</code>" in html, "Should render inline code as HTML"

        print("✓ Test passed: Markdown is rendered server-side")

    finally:
        test_file_path.unlink()


def test_user_message_markdown_rendered_with_raw_preserved():
    """User messages are now rendered as Markdown by default (with a
    toggle to raw). The raw text must still be preserved in the
    `.user-raw` `<pre>` view so the user can switch to it."""
    user_message = {
        "type": "user",
        "timestamp": "2025-06-11T22:44:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_md_002",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "# A rendered heading\n\n**And some bold text**",
                }
            ],
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(user_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Test Transcript")

        # Markdown view renders the heading and bold.
        assert "<h1>A rendered heading</h1>" in html, (
            "User markdown should be rendered as HTML"
        )
        assert "<strong>And some bold text</strong>" in html, (
            "User markdown bold should render"
        )
        # Raw view still carries the literal text so the toggle works.
        assert "<pre class='user-raw'># A rendered heading" in html, (
            "Raw user text must be preserved alongside the rendered view"
        )
        assert "**And some bold text**" in html, (
            "Raw asterisks must remain intact in the raw view"
        )

        print("✓ Test passed: User messages are markdown rendered with raw preserved")

    finally:
        test_file_path.unlink()


def test_user_ill_formed_markdown_falls_back_to_raw():
    """When Markdown rendering produces ill-formed HTML (unclosed tags,
    malformed nesting), emit only the raw `<pre>` with no toggle."""
    from claude_code_log.html.user_formatters import format_user_text_content

    # A bare opening tag is preserved verbatim by the escape=True
    # renderer, so it doesn't produce ill-formed output — we instead
    # exercise the fallback explicitly via the helper API below.
    # This test focuses on the contract: well-formed → dual view,
    # otherwise → bare <pre>.

    # Normal text: should emit the dual view container.
    good = format_user_text_content("hello **there**")
    assert "class='user-content'" in good
    assert "class='user-md'" in good
    assert "class='user-raw'" in good

    # Force the fallback by patching render_user_markdown to return
    # deliberately ill-formed HTML. `mock.patch` handles the restore
    # and sidesteps ty's invalid-assignment complaint about raw attr
    # rebinding.
    from unittest.mock import patch

    with patch(
        "claude_code_log.html.user_formatters.render_user_markdown",
        return_value="<p>unclosed",
    ):
        bad = format_user_text_content("hello")
        # Ill-formed → bare <pre>, no toggle, no dual-view wrapper.
        assert bad == "<pre>hello</pre>"


def test_is_well_formed_html_unit() -> None:
    """Unit coverage for the dual-view gate helper."""
    from claude_code_log.html.utils import is_well_formed_html

    # Well-formed outputs mistune actually produces.
    assert is_well_formed_html("<p><strong>hi</strong></p>\n")
    assert is_well_formed_html("<p>hi<br>there</p>")  # void element
    assert is_well_formed_html("<ul><li>a</li><li>b</li></ul>")
    assert is_well_formed_html("")  # empty is trivially balanced

    # Mistune emits XHTML self-closing syntax for void elements
    # (<br />, <hr />, <img />). `handle_startendtag` must not
    # double-count these as an unbalanced open+close.
    assert is_well_formed_html("<p>hi<br />there</p>")
    assert is_well_formed_html("<hr />")
    assert is_well_formed_html('<img src="x" alt="y" />')

    # Unbalanced cases → fall back to raw.
    assert not is_well_formed_html("<p>hi")
    assert not is_well_formed_html("<p>hi</em>")  # mismatched close
    assert not is_well_formed_html("</p>")  # stray close


def test_user_markdown_with_newline_keeps_dual_view() -> None:
    """Regression: mistune turns a newline in user text into a XHTML
    `<br />`. `format_user_text_content` must keep the dual-view wrapper
    rather than falling back to bare `<pre>`."""
    from claude_code_log.html.user_formatters import format_user_text_content

    out = format_user_text_content("line1\nline2")
    assert "class='user-content'" in out
    assert "class='user-md'" in out
    assert "class='user-raw'" in out
    # The rendered Markdown actually contains the self-closing <br />.
    assert "<br />" in out or "<br/>" in out


def test_render_user_markdown_escapes_html() -> None:
    """User-side renderer must escape raw HTML so users can't inject tags."""
    from claude_code_log.html.utils import render_user_markdown

    assert "&lt;script&gt;" in render_user_markdown("<script>alert(1)</script>")
    # Markdown features still work.
    assert "<strong>bold</strong>" in render_user_markdown("**bold**")


def test_render_markdown_escapes_html() -> None:
    """The shared (assistant/tool/web-authored) renderer must escape raw HTML.

    Regression for the XSS where a transcript whose assistant text, tool
    result, or fetched web content contained ``<script>``/``onerror`` got
    rendered as live DOM — popping ``alert()`` dialogs when the HTML was
    opened. Assistant output is NOT trusted: it routinely echoes arbitrary
    user/file/web content (e.g. "write an E2E test that types <script>…").
    """
    from claude_code_log.html.utils import render_markdown

    payload = '<b>bold</b><img src="x" onerror="alert(\'x\')"><script>alert(1)</script>'
    out = render_markdown(payload)
    # No live markup reaches the DOM (escaped tags carry a dead
    # ``onerror=&quot;`` but never a live ``onerror="`` on a real tag).
    assert "<script>" not in out
    assert "<img" not in out
    assert 'onerror="' not in out
    assert "<b>bold</b>" not in out
    # Escaped to entities, content preserved as readable text.
    assert "&lt;script&gt;" in out
    assert "alert(1)" in out
    # Markdown features still render.
    assert "<strong>bold</strong>" in render_markdown("**bold**")
    assert "<code>" in render_markdown("`code`")


def test_markdown_neutralises_dangerous_url_schemes() -> None:
    """Markdown links/images must not emit ``javascript:``/``data:`` hrefs.

    Escaping raw HTML tags (the primary fix) does nothing for this XSS class —
    it lives in link/image URLs, not tags. mistune rewrites unsafe schemes to
    a harmless placeholder; this guards that behaviour against a renderer-config
    change silently dropping it. Covers both content renderers.
    """
    from claude_code_log.html.utils import render_markdown, render_user_markdown

    for render in (render_markdown, render_user_markdown):
        for src in (
            "[click](javascript:alert(1))",
            "![x](javascript:alert(1))",
            "<javascript:alert(1)>",
            "[x](data:text/html,<script>alert(1)</script>)",
        ):
            out = render(src)
            # The scheme is dangerous only as a live href/src attribute;
            # appearing as visible link text is harmless.
            assert 'href="javascript:' not in out, (render.__name__, src, out)
            assert 'src="javascript:' not in out, (render.__name__, src, out)
            assert 'href="data:' not in out, (render.__name__, src, out)
            assert 'src="data:' not in out, (render.__name__, src, out)


def test_assistant_text_does_not_inject_live_html() -> None:
    """End-to-end: assistant text with HTML must not emit live tags."""
    from claude_code_log.html.assistant_formatters import (
        format_assistant_text_content,
    )
    from claude_code_log.models import (
        AssistantTextMessage,
        MessageMeta,
        TextContent,
    )

    meta = MessageMeta(
        session_id="test-session",
        timestamp="2024-01-01T00:00:00Z",
        uuid="test-uuid",
    )
    payload = "Sure! I'll enter <script>alert(1)</script> into the field."
    content = AssistantTextMessage(meta, items=[TextContent(type="text", text=payload)])
    out = format_assistant_text_content(content)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


if __name__ == "__main__":
    test_server_side_markdown_rendering()
    test_user_message_markdown_rendered_with_raw_preserved()
    test_user_ill_formed_markdown_falls_back_to_raw()
    test_is_well_formed_html_unit()
    test_render_user_markdown_escapes_html()
    print("\n✅ All markdown rendering tests passed!")

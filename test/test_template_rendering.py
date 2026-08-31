#!/usr/bin/env python3
"""Test cases for template rendering with representative JSONL data."""

import json
import tempfile
from pathlib import Path
import pytest
from claude_code_log.converter import convert_jsonl_to_html
from claude_code_log.html.renderer import generate_projects_index_html
from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html


class TestTemplateRendering:
    """Test template rendering with various message types."""

    def test_representative_messages_render(self):
        """Test that representative messages render correctly."""
        test_data_path = (
            Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        )

        # Convert to HTML
        html_file = convert_jsonl_to_html(test_data_path)
        html_content = html_file.read_text(encoding="utf-8")

        # Basic HTML structure checks
        assert "<!DOCTYPE html>" in html_content
        assert "<html lang='en'>" in html_content
        assert (
            "<title>Claude Transcript - representative_messages</title>" in html_content
        )

        # Check for session header (should have one)
        session_header_count = html_content.count("session-header")
        assert session_header_count >= 1, (
            f"Expected at least 1 session header, got {session_header_count}"
        )

        # Check that all message types are present
        assert "class='message user" in html_content
        assert "class='message assistant" in html_content
        # Summary messages are now integrated into session headers
        assert "session-summary" in html_content or "Summary:" in html_content

        # Check specific content
        assert (
            "Hello Claude! Can you help me understand how Python decorators work?"
            in html_content
        )
        assert "Python decorators" in html_content
        assert "Tool Use:" in html_content
        assert "Tool Result" in html_content  # Changed: no colon for non-error results

        # Check that markdown elements are rendered server-side
        assert (
            "<code>@time_it" in html_content
        )  # Inline code blocks are rendered to HTML
        assert "decorator factory" in html_content
        assert "<strong>" in html_content  # Bold text is rendered to strong tags
        assert "<code>" in html_content  # Inline code is rendered to code tags

    def test_edge_cases_render(self):
        """Test that edge cases render without errors."""
        from claude_code_log.models import RenderingDepth

        test_data_path = Path(__file__).parent / "test_data" / "edge_cases.jsonl"

        # Render at FULL: this fixture exercises full-detail edge content —
        # slash-command framing (SlashCommandMessage) and local-command
        # output (CommandOutputMessage) are ``depth_visibility = FULL``, so
        # the new HIGH default (--depth tool, #159) intentionally cleans them.
        # ``use_cache=False`` avoids a stale shared test_data/*.html masking a
        # wrong-level render between runs.
        html_file = convert_jsonl_to_html(
            test_data_path, depth=RenderingDepth.HOOK, use_cache=False
        )
        html_content = html_file.read_text(encoding="utf-8")

        # Basic checks
        assert "<!DOCTYPE html>" in html_content
        assert "<title>Claude Transcript - edge_cases</title>" in html_content

        # Check markdown content is rendered to HTML (for assistant messages)
        # User messages should remain as-is in pre tags, assistant messages should be rendered
        # Note: Need to check which messages are user vs assistant to know what to expect

        # Check long text handling
        assert "Lorem ipsum dolor sit amet" in html_content

        # Check tool error handling
        assert "Tool Result" in html_content
        assert "🚨 Error" in html_content  # Changed: error indicator format
        assert "Tool execution failed" in html_content

        # Check system message filtering (caveat should be filtered out)
        assert "Caveat: The messages below were generated" not in html_content

        # Check command message handling
        assert "Slash Command" in html_content
        assert "test-command" in html_content

        # Check local command output is present (output from /context can be interesting)
        assert "message user command-output" in html_content
        assert "Line 1 of output" in html_content

        # Check special characters
        assert "café, naïve, résumé" in html_content
        assert "🎉 emojis 🚀" in html_content
        assert "∑∆√π∞" in html_content

    def test_multi_session_rendering(self):
        """Test multi-session rendering with proper session divider handling."""
        test_data_dir = Path(__file__).parent / "test_data"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Copy test files to temporary directory
            import shutil

            shutil.copy(
                test_data_dir / "representative_messages.jsonl",
                temp_path / "session_a.jsonl",
            )
            shutil.copy(
                test_data_dir / "session_b.jsonl", temp_path / "session_b.jsonl"
            )

            # Convert directory to HTML
            html_file = convert_jsonl_to_html(temp_path)
            html_content = html_file.read_text(encoding="utf-8")

            # Should have session headers for each session
            session_headers = html_content.count("session-header")
            assert session_headers >= 1, (
                f"Expected at least 1 session header, got {session_headers}"
            )

            # Check both sessions' content is present
            assert "Hello Claude! Can you help me understand" in html_content
            assert "This is from a different session file" in html_content
            assert "without any session divider above it" in html_content

    def test_empty_messages_handling(self):
        """Test handling of empty or invalid messages."""
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            jsonl_file = temp_path / "empty_test.jsonl"

            # Create file with empty content
            jsonl_file.write_text("", encoding="utf-8")

            # Should not crash
            html_file = convert_jsonl_to_html(jsonl_file)
            html_content = html_file.read_text(encoding="utf-8")

            assert "<!DOCTYPE html>" in html_content
            assert "<title>Claude Transcript - empty_test</title>" in html_content

            # Should have no messages
            assert "class='message" not in html_content

    def test_tool_content_rendering(self):
        """Test detailed tool use and tool result rendering."""
        test_data_path = (
            Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        )

        messages = load_transcript(test_data_path)
        html_content = generate_html(messages)

        # Check tool use formatting
        assert "Tool Use:" in html_content
        assert "Edit" in html_content
        assert "tool-use" in html_content

        # Check tool result formatting
        assert "Tool Result" in html_content  # Changed: no colon for non-error results
        assert "File created successfully" in html_content
        assert "tool-result" in html_content

        # Check tool input details
        assert 'class="collapsible-details"' in html_content
        assert "<summary>" in html_content
        assert "Input:" in html_content
        assert "details-content" in html_content

    def test_timestamp_formatting(self):
        """Test that timestamps are formatted correctly."""
        test_data_path = (
            Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        )

        html_file = convert_jsonl_to_html(test_data_path)
        html_content = html_file.read_text(encoding="utf-8")

        # Check timestamp format (YYYY-MM-DD HH:MM:SS)
        assert "2025-07-03 15:50:07" in html_content
        assert "2025-07-03 15:52:07" in html_content
        assert "class='timestamp'" in html_content

    def test_index_template_rendering(self):
        """Test index template with project summaries."""
        # Create mock project summaries
        project_summaries = [
            {
                "name": "test-project-1",
                "path": Path("/tmp/project1"),
                "html_file": "test-project-1/combined_transcripts.html",
                "jsonl_count": 3,
                "message_count": 15,
                "last_modified": 1700000000.0,  # Mock timestamp
            },
            {
                "name": "-user-workspace-my-app",
                "path": Path("/tmp/project2"),
                "html_file": "-user-workspace-my-app/combined_transcripts.html",
                "jsonl_count": 2,
                "message_count": 8,
                "last_modified": 1700000100.0,  # Mock timestamp
            },
        ]

        # Generate index HTML
        index_html = generate_projects_index_html(project_summaries)

        # Basic structure checks
        assert "<!DOCTYPE html>" in index_html
        assert "<title>Claude Code Projects</title>" in index_html
        assert "class='project-list'" in index_html
        assert "class='summary'" in index_html

        # Check project data
        assert "test-project-1" in index_html
        assert (
            "user/workspace/my/app" in index_html
        )  # Dash formatting should be applied
        assert "📁 3 transcript files" in index_html
        assert "💬 15 messages" in index_html
        assert "📁 2 transcript files" in index_html
        assert "💬 8 messages" in index_html

        # Check summary statistics
        assert "2" in index_html  # Total projects
        assert "5" in index_html  # Total JSONL files (3+2)
        assert "23" in index_html  # Total messages (15+8)

    def test_css_classes_applied(self):
        """Test that correct CSS classes are applied to different message types."""
        test_data_path = (
            Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        )

        html_file = convert_jsonl_to_html(test_data_path)
        html_content = html_file.read_text(encoding="utf-8")

        # Check message type classes
        assert "class='message user" in html_content
        assert "class='message assistant" in html_content
        # Summary messages are now integrated into session headers
        assert "session-summary" in html_content or "Summary:" in html_content

        # Check tool message classes (tools are now top-level messages, may include pair_first/pair_last classes)
        assert "tool_use" in html_content and "class='message" in html_content
        assert "tool_result" in html_content and "class='message" in html_content

    def test_server_side_markdown_rendering(self):
        """Test that markdown is rendered server-side, not client-side."""
        test_data_path = (
            Path(__file__).parent / "test_data" / "representative_messages.jsonl"
        )

        html_file = convert_jsonl_to_html(test_data_path)
        html_content = html_file.read_text(encoding="utf-8")

        # Should NOT have client-side JavaScript for markdown rendering
        assert "marked" not in html_content
        assert "DOMContentLoaded" not in html_content or "marked" not in html_content
        assert "querySelectorAll('.content')" not in html_content
        assert "marked.parse" not in html_content

        # Should have server-side rendered markdown in assistant messages
        # Check for elements that indicate markdown was rendered
        assert "<strong>" in html_content  # Bold text should be rendered
        assert "<code>" in html_content  # Code should be rendered
        assert "<p>" in html_content  # Paragraphs should be rendered
        assert (
            "<ul>" in html_content or "<ol>" in html_content
        )  # Lists should be rendered

    def test_html_escaping(self):
        """Test that HTML special characters are properly escaped."""
        # Create test data with HTML characters
        test_data = {
            "type": "user",
            "timestamp": "2025-06-14T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "human",
            "cwd": "/tmp",
            "sessionId": "test",
            "version": "1.0.0",
            "uuid": "test_001",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Testing HTML escaping: <script>alert('xss')</script> & ampersands \"quotes\"",
                    }
                ],
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            jsonl_file = temp_path / "escape_test.jsonl"

            with open(jsonl_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(test_data) + "\n")

            html_file = convert_jsonl_to_html(jsonl_file)
            html_content = html_file.read_text(encoding="utf-8")

            # Check that HTML is escaped
            assert "&lt;script&gt;" in html_content
            assert "&amp;" in html_content
            assert "&quot;" in html_content
            # The user's payload must not survive as a live tag.
            #
            # This used to count `<script>` occurrences and allow up to
            # two, which meant bumping the number every time the page grew
            # a script block of its own — while saying nothing about
            # whether the *content* was escaped. Assert the property
            # itself: the payload's opening tag is escaped everywhere it
            # appears, so it can never execute.
            assert "<script>alert(" not in html_content
            # Both renderings of the payload are escaped: the raw-text view
            # keeps the apostrophe literal, the Markdown view entity-encodes
            # it. Neither carries a live `<script>`.
            assert "&lt;script&gt;alert('xss')&lt;/script&gt;" in html_content
            assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in html_content


class TestNestedDomSkipsEmptyLeaves:
    """Pinning test for the nested-DOM ``should_render`` skip (PR0, #174).

    The flat ``_flatten_preorder`` renderer dropped leaf messages that
    formatted to nothing (e.g. TaskCreate/TaskUpdate tool_results whose
    output formatter returns ``""``) so they didn't show as bare,
    timestamp-only cards. The nested-DOM rewrite moved that decision onto
    ``TemplateMessage.should_render`` (computed in
    ``HtmlRenderer._annotate_tree_for_render``) which the recursive
    ``render_message`` macro must honour.

    Regression guard: if the macro ever renders unconditionally again
    (e.g. ``{% elif message.should_render %}`` reverted to ``{% else %}``),
    these empty-formatting tool_results reappear as empty cards and this
    test fails.
    """

    def test_empty_formatting_tool_results_emit_no_card(self):
        """``task_id_linking.jsonl`` contains TaskCreate/TaskUpdate
        tool_results that format to empty HTML. Their message cards
        (``id='msg-d-<idx>'``) must be absent from the rendered output."""
        from claude_code_log.html.renderer import HtmlRenderer
        from claude_code_log.renderer import generate_template_messages

        test_file = Path(__file__).parent / "test_data" / "task_id_linking.jsonl"
        messages = load_transcript(test_file)

        # Identify the nodes the renderer marks should_render=False by
        # running the same annotation the HTML path uses.
        roots, _nav, _ctx = generate_template_messages(messages)
        renderer = HtmlRenderer(image_export_mode="placeholder")
        renderer._annotate_tree_for_render(roots)

        suppressed_ids: list[str] = []

        def collect(node):
            if not node.should_render and node.message_id:
                suppressed_ids.append(node.message_id)
            for child in node.children:
                collect(child)

        for root in roots:
            collect(root)

        # The fixture must actually exercise the skip, else the guard is
        # vacuous.
        assert suppressed_ids, (
            "fixture no longer contains empty-formatting tool_results; "
            "pick another fixture that does so this guard stays meaningful"
        )

        html = generate_html(messages, "Task ID Linking")

        for mid in suppressed_ids:
            assert f"id='msg-{mid}'" not in html, (
                f"empty-formatting node {mid} rendered a card — the "
                f"should_render skip regressed in the template macro"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

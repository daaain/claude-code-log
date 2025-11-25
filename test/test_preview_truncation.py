"""Tests for code preview truncation in tool results.

Regression test for the bug where Pygments highlighted code previews
weren't truncated because the code assumed multiple <tr> rows per line,
but HtmlFormatter(linenos="table") produces a single <tr> with two <td>s.
"""

from pathlib import Path

from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html, _truncate_highlighted_preview


class TestPreviewTruncation:
    """Tests for preview truncation in collapsible code blocks."""

    def test_truncate_highlighted_preview_function(self):
        """Test the _truncate_highlighted_preview helper directly."""
        # Simulate Pygments output with 10 lines
        html = """<div class="highlight"><table class="highlighttable"><tr><td class="linenos"><div class="linenodiv"><pre><span class="normal"> 1</span>
<span class="normal"> 2</span>
<span class="normal"> 3</span>
<span class="normal"> 4</span>
<span class="normal"> 5</span>
<span class="normal"> 6</span>
<span class="normal"> 7</span>
<span class="normal"> 8</span>
<span class="normal"> 9</span>
<span class="normal">10</span></pre></div></td><td class="code"><div><pre><span></span>line 1
line 2
line 3
line 4
line 5
line 6
line 7
line 8
line 9
line 10
</pre></div></td></tr></table></div>"""

        # Truncate to 5 lines
        result = _truncate_highlighted_preview(html, 5)

        # Should have lines 1-5 in linenos
        assert '<span class="normal"> 1</span>' in result
        assert '<span class="normal"> 5</span>' in result
        # Should NOT have lines 6-10
        assert '<span class="normal"> 6</span>' not in result
        assert '<span class="normal">10</span>' not in result

        # Should have lines 1-5 in code
        assert "line 1" in result
        assert "line 5" in result
        # Should NOT have lines 6-10
        assert "line 6" not in result
        assert "line 10" not in result

    def test_edit_tool_result_preview_truncation(self):
        """Test that Edit tool results have truncated previews in collapsible blocks.

        Regression test for: Preview extraction was looking for multiple <tr> tags,
        but Pygments produces a single <tr> with two <td>s, so the fallback showed
        full content instead of truncated preview.
        """
        test_data_path = Path(__file__).parent / "test_data" / "edit_tool.jsonl"

        messages = load_transcript(test_data_path)
        html = generate_html(messages, "Edit Tool Test")

        # The Edit tool result has 17 lines (>12), so should be collapsible
        assert "collapsible-code" in html, "Should have collapsible code block"

        # Find the preview content section
        assert "preview-content" in html, "Should have preview content"

        # The preview should only show first 5 lines, not all 17
        # Line 15 is "_timing_data: Dict[str, Any] = {}" - should be in preview
        # Line 31 is 'def timing_stat(list_name: str) -> Iterator[None]:"' - should NOT be in preview

        # Extract preview content (between preview-content div tags)
        import re

        preview_match = re.search(
            r"<div class=['\"]preview-content['\"]>(.*?)</div>\s*</summary>",
            html,
            re.DOTALL,
        )
        assert preview_match, "Should find preview-content div"
        preview_html = preview_match.group(1)

        # Preview should have early lines (within first 5)
        # Line 15 (line 1 of snippet): "_timing_data"
        assert "_timing_data" in preview_html, "Preview should contain line 15 content"

        # Preview should NOT have later lines (beyond first 5)
        # Line 26 (line 12 of snippet): "if DEBUG_TIMING:"
        # Note: Pygments wraps tokens in <span> tags, so check for identifier
        assert "DEBUG_TIMING" not in preview_html, (
            "Preview should NOT contain line 26 content (beyond 5 lines)"
        )

        # Line 30-31 (line 16-17 of snippet): "@contextmanager"
        assert "contextmanager" not in preview_html, (
            "Preview should NOT contain line 30 content (beyond 5 lines)"
        )

    def test_full_content_still_available(self):
        """Test that full content is still available in the expanded section."""
        test_data_path = Path(__file__).parent / "test_data" / "edit_tool.jsonl"

        messages = load_transcript(test_data_path)
        html = generate_html(messages, "Edit Tool Test")

        # The full content section should have all lines
        import re

        full_match = re.search(
            r"<div class=['\"]code-full['\"]>(.*?)</div>\s*</details>",
            html,
            re.DOTALL,
        )
        assert full_match, "Should find code-full div"
        full_html = full_match.group(1)

        # Full content should have both early and late lines
        # Note: Pygments wraps tokens in <span> tags, so we check for the identifier
        assert "_timing_data" in full_html, "Full content should contain line 15"
        assert "DEBUG_TIMING" in full_html, "Full content should contain line 26"
        assert "contextmanager" in full_html, "Full content should contain line 30"

"""Unit tests for MarkdownRenderer helper methods.

Tests for the private utility methods that handle content escaping and formatting.
"""

import pytest

from claude_code_log.markdown.renderer import MarkdownRenderer, _protect_html_tags
from claude_code_log.utils import strip_error_tags


@pytest.fixture
def renderer():
    """Create a MarkdownRenderer instance for testing."""
    return MarkdownRenderer()


class TestCodeFence:
    """Tests for the _code_fence() method."""

    def test_simple_text(self, renderer):
        """Basic text gets wrapped in triple backticks."""
        result = renderer._code_fence("hello world")
        assert result == "```\nhello world\n```"

    def test_with_language(self, renderer):
        """Language hint is added after opening fence."""
        result = renderer._code_fence("print('hi')", "python")
        assert result == "```python\nprint('hi')\n```"

    def test_text_with_triple_backticks(self, renderer):
        """Text containing ``` gets wrapped with longer fence."""
        content = "```python\ncode\n```"
        result = renderer._code_fence(content)
        assert result == "````\n```python\ncode\n```\n````"

    def test_text_with_four_backticks(self, renderer):
        """Text containing ```` gets wrapped with even longer fence."""
        content = "````\ncode\n````"
        result = renderer._code_fence(content)
        assert result == "`````\n````\ncode\n````\n`````"

    def test_text_with_inline_backticks(self, renderer):
        """Single or double backticks don't trigger longer fence."""
        content = "use `code` or ``code``"
        result = renderer._code_fence(content)
        # Two backticks is max, so we use 3 (standard)
        assert result == "```\nuse `code` or ``code``\n```"

    def test_text_with_mixed_backticks(self, renderer):
        """Mixed backtick lengths uses fence longer than max."""
        content = "` `` ``` ```` `````"
        result = renderer._code_fence(content)
        # Max is 5, so fence should be 6
        assert result.startswith("``````\n")
        assert result.endswith("\n``````")

    def test_empty_text(self, renderer):
        """Empty text still gets wrapped."""
        result = renderer._code_fence("")
        assert result == "```\n\n```"


class TestEscapeHtmlTag:
    """Tests for the _escape_html_tag() method."""

    def test_escape_details_tag(self, renderer):
        """Escapes </details> to prevent closing outer details block."""
        text = "Some text with </details> in it"
        result = renderer._escape_html_tag(text, "details")
        assert result == "Some text with &lt;/details> in it"

    def test_escape_summary_tag(self, renderer):
        """Escapes </summary> to prevent closing summary block."""
        text = "Click </summary> here"
        result = renderer._escape_html_tag(text, "summary")
        assert result == "Click &lt;/summary> here"

    def test_no_tag_present(self, renderer):
        """Text without the tag remains unchanged."""
        text = "No tags here"
        result = renderer._escape_html_tag(text, "details")
        assert result == "No tags here"

    def test_multiple_occurrences(self, renderer):
        """All occurrences are escaped."""
        text = "</details>foo</details>bar</details>"
        result = renderer._escape_html_tag(text, "details")
        assert result == "&lt;/details>foo&lt;/details>bar&lt;/details>"

    def test_case_sensitive(self, renderer):
        """Tag matching is case-sensitive."""
        text = "</DETAILS> and </details>"
        result = renderer._escape_html_tag(text, "details")
        # Only lowercase is escaped
        assert result == "</DETAILS> and &lt;/details>"


class TestEscapeStars:
    """Tests for the _escape_stars() method."""

    def test_single_asterisk_escaped(self, renderer):
        """Single asterisk gets escaped."""
        text = "a * b = c"
        result = renderer._escape_stars(text)
        assert result == "a \\* b = c"

    def test_multiple_asterisks_escaped(self, renderer):
        """All asterisks get escaped."""
        text = "a * b * c *"
        result = renderer._escape_stars(text)
        assert result == "a \\* b \\* c \\*"

    def test_already_escaped_asterisk(self, renderer):
        """Already escaped \\* becomes \\\\\\*."""
        text = "show \\* files"
        result = renderer._escape_stars(text)
        assert result == "show \\\\\\* files"

    def test_mixed_escaped_and_bare(self, renderer):
        """Mix of escaped and bare asterisks."""
        text = "foo * bar \\* baz"
        result = renderer._escape_stars(text)
        assert result == "foo \\* bar \\\\\\* baz"

    def test_no_asterisks(self, renderer):
        """Text without asterisks is unchanged."""
        text = "no asterisks here"
        result = renderer._escape_stars(text)
        assert result == text

    def test_empty_text(self, renderer):
        """Empty text remains empty."""
        result = renderer._escape_stars("")
        assert result == ""


class TestCollapsible:
    """Tests for the _collapsible() method."""

    def test_basic_collapsible(self, renderer):
        """Basic collapsible block structure."""
        result = renderer._collapsible("Click me", "Hidden content")
        expected = (
            "<details>\n<summary>Click me</summary>\n\nHidden content\n</details>"
        )
        assert result == expected

    def test_escapes_details_in_content(self, renderer):
        """Content with </details> is escaped."""
        result = renderer._collapsible("Title", "Has </details> tag")
        assert "&lt;/details>" in result
        assert "</details> tag" not in result

    def test_escapes_summary_in_summary(self, renderer):
        """Summary with </summary> is escaped."""
        result = renderer._collapsible("Title </summary> here", "Content")
        assert "&lt;/summary>" in result
        assert "</summary> here" not in result

    def test_escapes_details_in_summary(self, renderer):
        """Summary with </details> is also escaped."""
        result = renderer._collapsible("Bad </details> title", "Content")
        assert "&lt;/details>" in result

    def test_preserves_other_html(self, renderer):
        """Other HTML tags are not escaped."""
        result = renderer._collapsible("Title", "Has <code>code</code> here")
        assert "<code>code</code>" in result


class TestQuote:
    """Tests for the _quote() method."""

    def test_single_line(self, renderer):
        """Single line gets prefixed with '> '."""
        result = renderer._quote("hello")
        assert result == "> hello"

    def test_multiline(self, renderer):
        """Each line gets prefixed."""
        result = renderer._quote("line1\nline2\nline3")
        assert result == "> line1\n> line2\n> line3"

    def test_empty_lines(self, renderer):
        """Empty lines still get the prefix."""
        result = renderer._quote("line1\n\nline3")
        assert result == "> line1\n> \n> line3"

    def test_empty_text(self, renderer):
        """Empty string gets single prefix."""
        result = renderer._quote("")
        assert result == "> "

    def test_escapes_summary_tags(self, renderer):
        """Escapes <summary> tags that would interfere with <details> rendering."""
        text = "Some text\n<summary>\nMore text\n</summary>\nEnd"
        result = renderer._quote(text)
        # Tags on their own lines get escaped with backslash
        assert "> \\<summary>" in result
        assert "> \\</summary>" in result

    def test_preserves_inline_summary_tags(self, renderer):
        """Does not escape <summary> tags that are inline with other content."""
        text = "The <summary> tag is used in HTML"
        result = renderer._quote(text)
        # Inline tags are not escaped
        assert result == "> The <summary> tag is used in HTML"


class TestStripErrorTags:
    """Tests for the strip_error_tags() utility function."""

    def test_simple_error(self):
        """Single error tag is stripped, content preserved."""
        text = "<tool_use_error>File not found</tool_use_error>"
        result = strip_error_tags(text)
        assert result == "File not found"

    def test_multiline_error(self):
        """Multiline error content is preserved."""
        text = (
            "<tool_use_error>String to replace not found.\nString: foo</tool_use_error>"
        )
        result = strip_error_tags(text)
        assert result == "String to replace not found.\nString: foo"

    def test_no_error_tag(self):
        """Text without error tags is unchanged."""
        text = "Normal tool output"
        result = strip_error_tags(text)
        assert result == "Normal tool output"

    def test_nested_content(self):
        """Error with complex content is handled."""
        text = "<tool_use_error>Error: Code has <angle> brackets</tool_use_error>"
        result = strip_error_tags(text)
        assert result == "Error: Code has <angle> brackets"

    def test_empty_error(self):
        """Empty error tag produces empty string."""
        text = "<tool_use_error></tool_use_error>"
        result = strip_error_tags(text)
        assert result == ""


class TestExcerpt:
    """Tests for the _excerpt() method."""

    def test_short_text(self, renderer):
        """Short text is returned as-is."""
        result = renderer._excerpt("Hello world")
        assert result == "Hello world"

    def test_stops_at_question(self, renderer):
        """Stops at question mark followed by space."""
        result = renderer._excerpt("Is this working? I hope so.")
        assert result == "Is this working?"

    def test_stops_at_exclamation(self, renderer):
        """Stops at exclamation mark followed by space."""
        result = renderer._excerpt("Great success! This is a test.")
        assert result == "Great success!"

    def test_ignores_short_sentence(self, renderer):
        """Does not stop at sentence ending if too short (min 12 chars)."""
        result = renderer._excerpt("Hello! This is a test.", max_len=30)
        # "Hello!" is only 6 chars, so continues to next sentence
        assert result == "Hello! This is a test."

    def test_stops_at_period(self, renderer):
        """Stops at period followed by space."""
        result = renderer._excerpt("First sentence. Second sentence.")
        assert result == "First sentence."

    def test_no_stop_at_lone_period(self, renderer):
        """Does not stop at period without space (like in .gitignore)."""
        result = renderer._excerpt("Check the .gitignore file", max_len=30)
        # Should continue past the lone period
        assert ".gitignore" in result

    def test_word_boundary(self, renderer):
        """Truncation continues to end of current word."""
        # "This is a longer sentence..." - max_len 20 hits mid-word in "sentence"
        result = renderer._excerpt("This is a longer sentence that goes on", max_len=20)
        # Should not cut "sentence" mid-word
        assert result == "This is a longer sentence…"

    def test_empty_text(self, renderer):
        """Empty text returns empty string."""
        result = renderer._excerpt("")
        assert result == ""

    def test_multiline_takes_first(self, renderer):
        """Takes first non-empty line."""
        result = renderer._excerpt("\n\nFirst line\nSecond line")
        assert result == "First line"

    def test_ellipsis_when_truncated(self, renderer):
        """Adds ellipsis when truncated."""
        result = renderer._excerpt("A very long text that exceeds limit", max_len=15)
        assert result.endswith("…")


def _permissive_downstream():
    """A stand-in for a permissive downstream Markdown viewer: raw HTML
    passes through live, so anything ``_protect_html_tags`` left as a
    tag would show up as one."""
    from wenmode import HTMLRenderer, Wenmode
    from wenmode.presets import github

    return Wenmode(
        github(), renderer=HTMLRenderer(escape=False, sanitize_urls=False)
    ).render


class TestProtectHtmlTags:
    """Tests for the _protect_html_tags() module-level helper.

    The helper parses with wenmode and splices entities over raw-HTML nodes
    renderer. Inline and block HTML tokens get HTML-entity-escaped, so
    the tag text survives but no downstream Markdown renderer can
    interpret it as live markup. All non-HTML Markdown is preserved —
    including inline code spans, fenced blocks, and indented code
    blocks the author may have used specifically to quote HTML.
    """

    # -- Outputs that must be exactly the escaped form -------------------

    def test_plain_text_unchanged(self):
        """Text with no tags is returned verbatim."""
        assert _protect_html_tags("just some prose") == "just some prose"

    def test_escapes_bare_inline_tag(self):
        """Bare inline ``<br>`` is entity-escaped."""
        assert (
            _protect_html_tags("line one<br>line two") == "line one&lt;br&gt;line two"
        )

    def test_escapes_inline_open_and_close(self):
        """Both opening and closing inline tags are escaped."""
        assert (
            _protect_html_tags("some <b>bold</b> text")
            == "some &lt;b&gt;bold&lt;/b&gt; text"
        )

    def test_escapes_tag_with_attributes(self):
        """Inline tags with attributes escape the whole tag including attrs."""
        assert (
            _protect_html_tags('say <a href="x">link</a> please')
            == "say &lt;a href=&quot;x&quot;&gt;link&lt;/a&gt; please"
        )

    def test_escapes_self_closing_tag(self):
        """XHTML-style ``<br />`` is escaped whole."""
        assert _protect_html_tags("text<br />more") == "text&lt;br /&gt;more"

    def test_escapes_multiple_inline_tags_on_one_line(self):
        """Multiple inline tags on the same line are each escaped."""
        assert (
            _protect_html_tags("text <b>bold</b> and <i>italic</i>")
            == "text &lt;b&gt;bold&lt;/b&gt; and &lt;i&gt;italic&lt;/i&gt;"
        )

    # -- The user's key concern: already-quoted HTML must survive --------

    def test_inline_code_already_wrapped(self):
        """A tag already inside inline code isn't re-written.

        This is the user-flagged concern from the coderabbit review:
        ``use `x <br> y` here`` must not be corrupted.
        """
        assert _protect_html_tags("use `x <br> y` here") == "use `x <br> y` here"

    # -- Edge cases that used to leak with adaptive-backtick delimiters --

    def test_stray_backtick_before_tag_does_not_leak(self):
        """A lone ``\\``` adjacent to a tag can't merge with a wrapper
        delimiter — there isn't one to merge with.
        """
        permissive = _permissive_downstream()
        rendered = permissive(_protect_html_tags("x `<br> y")).strip()
        assert "&lt;br&gt;" in rendered
        # No live <br>.
        assert rendered.replace("&lt;br&gt;", "").count("<br") == 0

    def test_backtick_in_attribute_does_not_leak(self):
        """Tags with backticks in attributes don't break either."""
        permissive = _permissive_downstream()
        rendered = permissive(_protect_html_tags('<span title="`">x</span>')).strip()
        assert "&lt;span" in rendered
        # No live <span>.
        assert "<span" not in rendered.replace("&lt;span", "")

    def test_block_html_with_inner_fence_does_not_leak(self):
        """Block HTML containing a ``` fence doesn't break."""
        permissive = _permissive_downstream()
        rendered = permissive(_protect_html_tags("<div>\n```\n</div>")).strip()
        assert "&lt;div&gt;" in rendered
        assert "<div" not in rendered.replace("&lt;div", "")

    # -- Markdown constructs that must pass through ----------------------

    def test_autolink_not_wrapped(self):
        """CommonMark autolink ``<https://...>`` is preserved."""
        assert (
            _protect_html_tags("see <https://example.com/path> for info")
            == "see <https://example.com/path> for info"
        )

    def test_email_autolink_not_wrapped(self):
        """CommonMark email autolink ``<you@example.com>`` is preserved."""
        assert (
            _protect_html_tags("email <you@example.com> directly")
            == "email <you@example.com> directly"
        )

    def test_less_than_not_wrapped(self):
        """Bare ``<3`` and ``<=`` aren't mistaken for tags."""
        assert _protect_html_tags("x < 3 and x <= 5") == "x < 3 and x <= 5"

    def test_inside_fenced_code_block(self):
        """Tags inside a ``` fence stay literal (code-block token, not HTML)."""
        text = "Here is code:\n\n```html\n<script>x</script>\n```"
        # The fence contents survive intact — the `<script>` inside is
        # a code-block token, not an inline-HTML one, so our overrides
        # don't touch it.
        result = _protect_html_tags(text)
        assert "```html\n<script>x</script>\n```" in result

    def test_inside_tilde_fence(self):
        """~~~ fences are respected just like ``` fences."""
        text = "~~~\n<br>\n~~~"
        assert _protect_html_tags(text) == text


class TestFormatUserTextMessage:
    """Integration tests for MarkdownRenderer.format_UserTextMessage().

    These cover the Markdown dual-view gate introduced in the user-
    markdown PR: clean Markdown is emitted inline (with HTML-tag
    protection), ill-formed output falls back to a code fence.
    """

    def _make(self, text: str):
        from claude_code_log.models import MessageMeta, TextContent, UserTextMessage

        return UserTextMessage(
            meta=MessageMeta.empty(),
            items=[TextContent(type="text", text=text)],
        )

    def test_clean_markdown_emitted_inline(self, renderer):
        """`# heading` and `**bold**` pass the gate → raw Markdown out."""
        content = self._make("# Hi\n\n**bold**")
        result = renderer.format_UserTextMessage(content, None)
        assert result == "# Hi\n\n**bold**"

    def test_plain_text_emitted_inline(self, renderer):
        """Plain prose renders cleanly → emitted as-is (no code fence)."""
        content = self._make("please review the PR when you have time")
        result = renderer.format_UserTextMessage(content, None)
        assert result == "please review the PR when you have time"

    def test_raw_html_tags_escaped_inline(self, renderer):
        """Clean Markdown with bare ``<script>`` gets the tag entity-escaped."""
        content = self._make("Please run <script>alert(1)</script> safely.")
        result = renderer.format_UserTextMessage(content, None)
        assert result == "Please run &lt;script&gt;alert(1)&lt;/script&gt; safely."

    def test_ill_formed_falls_back_to_code_fence(self, renderer, monkeypatch):
        """When the Markdown gate rejects the output, code-fence it."""
        # Force the gate to fail by monkey-patching render_user_markdown
        # to return deliberately ill-formed HTML.
        import claude_code_log.markdown.renderer as mr

        monkeypatch.setattr(mr, "render_user_markdown", lambda _t: "<p>unclosed")
        content = self._make("anything")
        result = renderer.format_UserTextMessage(content, None)
        assert result == "```\nanything\n```"

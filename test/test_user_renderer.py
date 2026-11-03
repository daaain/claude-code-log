"""Tests for user message parsing and rendering.

Split into:
- Parsing tests: test create_compacted_summary_message(), create_user_memory_message()
- Content model tests: test create_user_message()
- HTML rendering tests: test full pipeline from JSONL to HTML
"""

import json
import tempfile
from pathlib import Path


from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.html.user_formatters import (
    format_compacted_summary_content,
    format_user_memory_content,
    format_user_text_model_content,
)
from claude_code_log.models import (
    CompactedSummaryMessage,
    ContentItem,
    ImageContent,
    ImageSource,
    MessageMeta,
    TextContent,
    UserMemoryMessage,
    UserTextMessage,
)
from claude_code_log.factories import (
    COMPACTED_SUMMARY_PREFIX,
    create_compacted_summary_message,
    create_user_memory_message,
    create_user_message,
)
from claude_code_log.parser import extract_text_content


# =============================================================================
# Parsing Tests - create_compacted_summary_message()
# =============================================================================


class TestCreateCompactedSummaryMessage:
    """Tests for create_compacted_summary_message() factory function (takes content list)."""

    def test_create_compacted_summary_message_detected(self):
        """Test that compacted summary is detected and content combined."""
        text = (
            f"{COMPACTED_SUMMARY_PREFIX}. The conversation is summarized below:\n"
            "Summary content here."
        )
        content_list = [TextContent(type="text", text=text)]

        result = create_compacted_summary_message(MessageMeta.empty(), content_list)

        assert result is not None
        assert isinstance(result, CompactedSummaryMessage)
        assert result.summary_text == text

    def test_create_compacted_summary_message_not_detected(self):
        """Test that regular text is not detected as compacted summary."""
        text = "This is a regular user message."
        content_list = [TextContent(type="text", text=text)]

        result = create_compacted_summary_message(MessageMeta.empty(), content_list)

        assert result is None

    def test_create_compacted_summary_message_empty_list(self):
        """Test that empty content list returns None."""
        result = create_compacted_summary_message(MessageMeta.empty(), [])
        assert result is None

    def test_create_compacted_summary_message_combines_multiple_texts(self):
        """Test that multiple text items are combined with double newlines."""
        first_text = f"{COMPACTED_SUMMARY_PREFIX}. Part 1."
        second_text = "Part 2."
        third_text = "Part 3."
        content_list = [
            TextContent(type="text", text=first_text),
            TextContent(type="text", text=second_text),
            TextContent(type="text", text=third_text),
        ]

        result = create_compacted_summary_message(MessageMeta.empty(), content_list)

        assert result is not None
        expected = "\n\n".join([first_text, second_text, third_text])
        assert result.summary_text == expected


# =============================================================================
# Parsing Tests - create_user_memory_message()
# =============================================================================


class TestParseUserMemory:
    """Tests for create_user_memory_message() parser function."""

    def test_create_user_memory_message_detected(self):
        """Test that user memory input tag is detected correctly."""
        text = "<user-memory-input>Memory content from CLAUDE.md</user-memory-input>"

        result = create_user_memory_message(MessageMeta.empty(), text)

        assert result is not None
        assert isinstance(result, UserMemoryMessage)
        assert result.memory_text == "Memory content from CLAUDE.md"

    def test_create_user_memory_message_with_surrounding_text(self):
        """Test memory tag extraction from mixed content."""
        text = "Some prefix <user-memory-input>The actual memory</user-memory-input> suffix"

        result = create_user_memory_message(MessageMeta.empty(), text)

        assert result is not None
        assert result.memory_text == "The actual memory"

    def test_create_user_memory_message_multiline(self):
        """Test multiline memory content."""
        memory_content = "Line 1\nLine 2\nLine 3"
        text = f"<user-memory-input>{memory_content}</user-memory-input>"

        result = create_user_memory_message(MessageMeta.empty(), text)

        assert result is not None
        assert result.memory_text == memory_content

    def test_create_user_memory_message_not_detected(self):
        """Test that regular text without tag returns None."""
        text = "Regular text without memory tag."

        result = create_user_memory_message(MessageMeta.empty(), text)

        assert result is None

    def test_create_user_memory_message_strips_whitespace(self):
        """Test that memory content whitespace is stripped."""
        text = "<user-memory-input>  \n  Content with spaces  \n  </user-memory-input>"

        result = create_user_memory_message(MessageMeta.empty(), text)

        assert result is not None
        assert result.memory_text == "Content with spaces"


# =============================================================================
# Content Model Tests - create_user_message()
# =============================================================================


class TestParseUserMessageContentCompacted:
    """Tests for create_user_message() handling compacted summaries."""

    def test_compacted_summary_single_text_item(self):
        """Test compacted summary with single text content item."""
        text = f"{COMPACTED_SUMMARY_PREFIX}. The conversation summary."
        content_list = [TextContent(type="text", text=text)]

        content_model = create_user_message(
            MessageMeta.empty(), content_list, extract_text_content(content_list)
        )

        assert content_model is not None
        assert isinstance(content_model, CompactedSummaryMessage)
        assert content_model.summary_text == text

    def test_compacted_summary_multiple_text_items(self):
        """Test compacted summary with multiple text content items combines all."""
        first_text = f"{COMPACTED_SUMMARY_PREFIX}. Summary part 1."
        second_text = "Summary part 2."
        third_text = "Summary part 3."
        content_list = [
            TextContent(type="text", text=first_text),
            TextContent(type="text", text=second_text),
            TextContent(type="text", text=third_text),
        ]

        content_model = create_user_message(
            MessageMeta.empty(), content_list, extract_text_content(content_list)
        )

        assert content_model is not None
        assert isinstance(content_model, CompactedSummaryMessage)
        # All text items should be combined with double newlines
        expected = "\n\n".join([first_text, second_text, third_text])
        assert content_model.summary_text == expected


class TestParseUserMessageContentMemory:
    """Tests for create_user_message() handling user memory."""

    def test_user_memory_detected(self):
        """Test user memory content is detected and returned."""
        text = "<user-memory-input>CLAUDE.md content here</user-memory-input>"
        content_list = [TextContent(type="text", text=text)]

        content_model = create_user_message(
            MessageMeta.empty(), content_list, extract_text_content(content_list)
        )

        assert content_model is not None
        assert isinstance(content_model, UserMemoryMessage)
        assert content_model.memory_text == "CLAUDE.md content here"


class TestParseUserMessageContentRegular:
    """Tests for create_user_message() handling regular user text."""

    def test_regular_text(self):
        """Test regular user text without special markers."""
        text = "Hello, please help me with this code."
        content_list = [TextContent(type="text", text=text)]

        content_model = create_user_message(
            MessageMeta.empty(), content_list, extract_text_content(content_list)
        )

        assert content_model is not None
        assert isinstance(content_model, UserTextMessage)
        assert len(content_model.items) == 1
        assert isinstance(content_model.items[0], TextContent)
        assert content_model.items[0].text == text

    def test_empty_content_list(self):
        """Test empty content list returns None."""
        content_list: list[ContentItem] = []

        content_model = create_user_message(
            MessageMeta.empty(), content_list, extract_text_content(content_list)
        )

        assert content_model is None

    def test_numbered_image_references_control_image_placement(self):
        """Claude image blocks render where their text references occur.

        The reference numbers are paste ids, so the association comes from
        ``imagePasteIds`` — here it happens to coincide with block order, which
        is what lets this case exercise placement on its own. ``[Image #3]``
        names no recorded paste id and stays literal.
        """
        first = ImageContent(
            type="image",
            source=ImageSource(type="base64", media_type="image/png", data="first"),
        )
        second = ImageContent(
            type="image",
            source=ImageSource(type="base64", media_type="image/png", data="second"),
        )
        content_list: list[ContentItem] = [
            first,
            second,
            TextContent(
                type="text",
                text="Compare [Image #2] with [Image #1]; keep [Image #3].",
            ),
        ]

        content_model = create_user_message(
            MessageMeta.empty(),
            content_list,
            extract_text_content(content_list),
            image_paste_ids=[1, 2],
        )

        assert isinstance(content_model, UserTextMessage)
        assert content_model.items == [
            TextContent(type="text", text="Compare "),
            second,
            TextContent(type="text", text=" with "),
            first,
            TextContent(type="text", text="; keep [Image #3]."),
        ]
        html = format_user_text_model_content(content_model)
        assert "[Image #1]" not in html
        assert "[Image #2]" not in html
        assert html.index("second") < html.index("first")
        assert "[Image #3]" in html

    def test_unreferenced_images_keep_their_original_position(self):
        image = ImageContent(
            type="image",
            source=ImageSource(type="base64", media_type="image/png", data="image"),
        )
        content_list: list[ContentItem] = [
            TextContent(type="text", text="Before"),
            image,
            TextContent(type="text", text="After"),
        ]

        content_model = create_user_message(
            MessageMeta.empty(), content_list, extract_text_content(content_list)
        )

        assert isinstance(content_model, UserTextMessage)
        assert content_model.items == [
            TextContent(type="text", text="Before"),
            image,
            TextContent(type="text", text="After"),
        ]


# =============================================================================
# Formatting Tests - format_compacted_summary_content()
# =============================================================================


class TestFormatCompactedSummaryMessage:
    """Tests for format_compacted_summary_content() formatter function."""

    def test_format_compacted_summary_basic(self):
        """Test basic compacted summary formatting."""
        content = CompactedSummaryMessage(
            MessageMeta.empty(), summary_text="Summary:\n- Point 1\n- Point 2"
        )

        html = format_compacted_summary_content(content)

        # Should render as markdown (not preformatted)
        assert "<ul>" in html or "<li>" in html  # Markdown list rendering
        assert "Point 1" in html
        assert "Point 2" in html

    def test_format_compacted_summary_collapsible(self):
        """Test that long compacted summaries are collapsible."""
        # Create long content that exceeds threshold
        long_summary = "Summary:\n" + "\n".join([f"- Point {i}" for i in range(50)])
        content = CompactedSummaryMessage(
            MessageMeta.empty(), summary_text=long_summary
        )

        html = format_compacted_summary_content(content)

        # Should be collapsible for long content
        assert "<details" in html
        assert "<summary" in html


# =============================================================================
# Formatting Tests - format_user_memory_content()
# =============================================================================


class TestFormatUserMemoryMessage:
    """Tests for format_user_memory_content() formatter function."""

    def test_format_user_memory_basic(self):
        """Test basic user memory formatting."""
        content = UserMemoryMessage(
            MessageMeta.empty(), memory_text="CLAUDE.md content"
        )

        html = format_user_memory_content(content)

        assert "<pre>" in html
        assert "</pre>" in html
        assert "CLAUDE.md content" in html

    def test_format_user_memory_escapes_html(self):
        """Test that HTML characters are escaped."""
        content = UserMemoryMessage(
            MessageMeta.empty(), memory_text="<script>alert('xss')</script>"
        )

        html = format_user_memory_content(content)

        assert "&lt;script&gt;" in html
        assert "<script>" not in html


# =============================================================================
# Formatting Tests - format_user_text_model_content()
# =============================================================================


class TestFormatUserTextModelContent:
    """Tests for format_user_text_model_content() formatter function."""

    def test_format_user_text_basic(self):
        """User text is emitted as a dual view (rendered Markdown +
        raw `<pre>`), with the raw view preserving the original text."""
        content = UserTextMessage(
            MessageMeta.empty(),
            items=[TextContent(type="text", text="User question here")],
        )

        html = format_user_text_model_content(content)

        # Dual-view container with Markdown as default.
        assert "user-content" in html
        # Both views present; raw preserves the literal text.
        assert "<pre class='user-raw'>User question here</pre>" in html
        assert "class='user-md'" in html
        assert "User question here" in html

    def test_format_user_text_escapes_html(self):
        """Test that HTML characters are escaped."""
        content = UserTextMessage(
            MessageMeta.empty(),
            items=[TextContent(type="text", text='Test <b>bold</b> & "quotes"')],
        )

        html = format_user_text_model_content(content)

        assert "&lt;b&gt;" in html
        assert "&amp;" in html
        assert "&quot;" in html


# =============================================================================
# HTML Rendering Tests - Full Pipeline
# =============================================================================


def _create_user_message(text: str, session_id: str = "test_session") -> dict:
    """Helper to create a user message dict for testing."""
    return {
        "type": "user",
        "timestamp": "2025-06-11T22:45:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": "user_001",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


class TestCompactedSummaryRendering:
    """Tests for compacted summary rendering through full pipeline."""

    def test_compacted_summary_renders_with_title(self):
        """Test compacted summary shows appropriate title in HTML."""
        text = f"{COMPACTED_SUMMARY_PREFIX}. The previous session summary."
        message = _create_user_message(text)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(message) + "\n")
            f.flush()
            test_file_path = Path(f.name)

        try:
            messages = load_transcript(test_file_path)
            html = generate_html(messages, "Test Compacted")

            # Should have compacted in title or class
            assert "compacted" in html.lower()
            # Content should be present (the prefix text should be rendered)
            assert "This session is being continued" in html
        finally:
            test_file_path.unlink()

    def test_compacted_summary_css_class(self):
        """Test compacted summary has appropriate CSS class."""
        text = f"{COMPACTED_SUMMARY_PREFIX}. Summary content."
        message = _create_user_message(text)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(message) + "\n")
            f.flush()
            test_file_path = Path(f.name)

        try:
            messages = load_transcript(test_file_path)
            html = generate_html(messages, "Test Compacted CSS")

            # Check for compacted CSS class
            assert (
                'class="message user compacted' in html
                or "class='message user compacted" in html
            )
        finally:
            test_file_path.unlink()


class TestUserMemoryRendering:
    """Tests for user memory rendering through full pipeline."""

    def test_user_memory_renders_with_title(self):
        """Test user memory shows Memory title in HTML."""
        text = "<user-memory-input>Contents of CLAUDE.md</user-memory-input>"
        message = _create_user_message(text)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(message) + "\n")
            f.flush()
            test_file_path = Path(f.name)

        try:
            messages = load_transcript(test_file_path)
            html = generate_html(messages, "Test Memory")

            # Should have Memory in title
            assert "Memory" in html
            # Content should be present
            assert "Contents of CLAUDE.md" in html
        finally:
            test_file_path.unlink()

    def test_user_memory_preserves_content(self):
        """Test user memory content is properly preserved."""
        memory_text = "# CLAUDE.md\n\n- Rule 1\n- Rule 2"
        text = f"<user-memory-input>{memory_text}</user-memory-input>"
        message = _create_user_message(text)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(message) + "\n")
            f.flush()
            test_file_path = Path(f.name)

        try:
            messages = load_transcript(test_file_path)
            html = generate_html(messages, "Test Memory Content")

            # Content should be preserved (HTML escaped)
            assert "# CLAUDE.md" in html
            assert "Rule 1" in html
            assert "Rule 2" in html
        finally:
            test_file_path.unlink()


class TestRegularUserMessageRendering:
    """Tests for regular user message rendering through full pipeline."""

    def test_regular_user_message(self):
        """Test regular user message renders correctly."""
        text = "Hello, can you help me with Python?"
        message = _create_user_message(text)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(message) + "\n")
            f.flush()
            test_file_path = Path(f.name)

        try:
            messages = load_transcript(test_file_path)
            html = generate_html(messages, "Test Regular")

            # Should have user class without special modifiers. With
            # nested-DOM rendering the message div no longer carries d-N
            # ancestry classes, so the class string ends right after the
            # type ("user") — match the bare close or a trailing modifier.
            assert "Hello, can you help me with Python?" in html
            assert (
                "class='message user'" in html
                or "class='message user " in html
                or 'class="message user"' in html
                or 'class="message user ' in html
            )
        finally:
            test_file_path.unlink()

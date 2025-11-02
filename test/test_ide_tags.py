"""Tests for IDE tag preprocessing in user messages."""

from claude_code_log.renderer import (
    extract_ide_notifications,
    render_user_message_content,
    render_message_content,
)
from claude_code_log.models import TextContent, ImageContent, ImageSource


def test_extract_ide_opened_file_tag():
    """Test that <ide_opened_file> tags are extracted correctly."""
    text = (
        "<ide_opened_file>The user opened the file "
        "e:\\Workspace\\test.py in the IDE. This may or may not be related to the current task."
        "</ide_opened_file>\n"
        "Here is my actual question."
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have one notification
    assert len(notifications) == 1
    # Should contain the IDE notification div
    assert "<div class='ide-notification'>" in notifications[0]
    # Should have bot emoji prefix
    assert "🤖" in notifications[0]
    # Should escape the content properly
    assert (
        "e:\\Workspace\\test.py" in notifications[0]
        or "e:\\\\Workspace\\\\test.py" in notifications[0]
    )
    # Remaining text should not have the tag
    assert remaining == "Here is my actual question."


def test_extract_multiple_ide_tags():
    """Test handling multiple IDE tags in one message."""
    text = (
        "<ide_opened_file>First file opened.</ide_opened_file>\n"
        "Some text in between.\n"
        "<ide_opened_file>Second file opened.</ide_opened_file>"
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have two IDE notifications
    assert len(notifications) == 2
    # Each should have bot emoji
    assert all("🤖" in n for n in notifications)
    # Remaining text should have text in between but no tags
    assert "Some text in between." in remaining
    assert "<ide_opened_file>" not in remaining


def test_extract_no_ide_tags():
    """Test that messages without IDE tags are unchanged."""
    text = "This is a regular user message without any IDE tags."

    notifications, remaining = extract_ide_notifications(text)

    # Should have no notifications
    assert len(notifications) == 0
    # Remaining text should be unchanged
    assert remaining == text


def test_extract_multiline_ide_tag():
    """Test IDE tags with multiline content."""
    text = (
        "<ide_opened_file>The user opened the file\n"
        "e:\\Workspace\\test.py in the IDE.\n"
        "This may or may not be related.</ide_opened_file>\n"
        "User question follows."
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have one notification with multiline content
    assert len(notifications) == 1
    assert "🤖" in notifications[0]
    assert (
        "e:\\Workspace\\test.py" in notifications[0]
        or "e:\\\\Workspace\\\\test.py" in notifications[0]
    )
    # Remaining should have the user question
    assert remaining == "User question follows."


def test_extract_special_chars_in_ide_tag():
    """Test that special HTML characters are escaped in IDE tag content."""
    text = (
        '<ide_opened_file>File with <special> & "characters" in path.</ide_opened_file>'
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have one notification
    assert len(notifications) == 1
    # Should escape HTML special characters
    assert "&lt;special&gt;" in notifications[0]
    assert "&amp;" in notifications[0]
    assert (
        "&quot;characters&quot;" in notifications[0]
        or "&#x27;characters&#x27;" in notifications[0]
    )
    # Remaining should be empty
    assert remaining == ""


def test_render_user_message_with_multi_item_content():
    """Test rendering user message with multiple content items (text + image)."""
    # Simulate a user message with text containing IDE tag plus an image
    text_with_tag = (
        "<ide_opened_file>User opened example.py</ide_opened_file>\n"
        "Please review this code and this screenshot:"
    )
    image_item = ImageContent(
        type="image",
        source=ImageSource(
            type="base64",
            media_type="image/png",
            data="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        ),
    )

    content_list = [
        TextContent(type="text", text=text_with_tag),
        image_item,
    ]

    content_html, is_compacted = render_user_message_content(content_list)

    # Should extract IDE notification
    assert "🤖" in content_html
    assert "ide-notification" in content_html
    assert "User opened example.py" in content_html

    # Should render remaining text
    assert "Please review this code" in content_html

    # Should render image
    assert "<img src=" in content_html
    assert "data:image/png;base64" in content_html

    # Should not be compacted
    assert is_compacted is False


def test_render_message_content_single_text_item():
    """Test that single TextContent item takes fast path for user messages."""
    content = [TextContent(type="text", text="Simple user message")]

    html = render_message_content(content, "user")

    # Should be wrapped in <pre> for user messages
    assert html.startswith("<pre>")
    assert html.endswith("</pre>")
    assert "Simple user message" in html


def test_render_message_content_single_text_item_assistant():
    """Test that single TextContent item takes fast path for assistant messages."""
    content = [TextContent(type="text", text="**Bold** response")]

    html = render_message_content(content, "assistant")

    # Should be rendered as markdown (no <pre>)
    assert "<pre>" not in html
    # Markdown should be processed
    assert "<strong>Bold</strong>" in html or "<b>Bold</b>" in html


def test_extract_ide_diagnostics():
    """Test extraction of IDE diagnostics from post-tool-use-hook."""
    text = (
        "<post-tool-use-hook><ide_diagnostics>["
        '{"filePath": "/e:/Workspace/test.py", "line": 12, "column": 6, '
        '"message": "Package not installed", "code": "[object Object]", "severity": "Hint"},'
        '{"filePath": "/e:/Workspace/other.py", "line": 5, "column": 1, '
        '"message": "Unused import", "severity": "Warning"}'
        "]</ide_diagnostics></post-tool-use-hook>\n"
        "Here is my question."
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have two diagnostic notifications (one per diagnostic object)
    assert len(notifications) == 2

    # Each should have the warning emoji and "IDE Diagnostic" label
    assert all("⚠️" in n for n in notifications)
    assert all("IDE Diagnostic" in n for n in notifications)

    # Should render as tables
    assert all("<table class='tool-params-table'>" in n for n in notifications)

    # Should contain diagnostic fields
    assert "filePath" in notifications[0]
    assert "/e:/Workspace/test.py" in notifications[0]
    assert "Package not installed" in notifications[0]

    assert "filePath" in notifications[1]
    assert "/e:/Workspace/other.py" in notifications[1]
    assert "Unused import" in notifications[1]

    # Remaining text should not have the hook tags
    assert remaining == "Here is my question."
    assert "<post-tool-use-hook>" not in remaining


def test_extract_mixed_ide_tags():
    """Test handling both ide_opened_file and ide_diagnostics together."""
    text = (
        "<ide_opened_file>User opened config.json</ide_opened_file>\n"
        "<post-tool-use-hook><ide_diagnostics>["
        '{"line": 10, "message": "Syntax error"}'
        "]</ide_diagnostics></post-tool-use-hook>\n"
        "Please review."
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have 2 notifications total: 1 file open + 1 diagnostic
    assert len(notifications) == 2

    # First should be file open notification
    assert "🤖" in notifications[0]
    assert "User opened config.json" in notifications[0]

    # Second should be diagnostic
    assert "⚠️" in notifications[1]
    assert "IDE Diagnostic" in notifications[1]
    assert "Syntax error" in notifications[1]

    # Remaining text should be clean
    assert remaining == "Please review."


def test_extract_ide_selection_short():
    """Test extraction of short IDE selection."""
    text = (
        "<ide_selection>The user selected the lines 7 to 7 from file.py:\n"
        "nx_utils\n\n"
        "This may or may not be related to the current task.</ide_selection>\n"
        "Can you explain this?"
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have one notification
    assert len(notifications) == 1

    # Should have pencil emoji
    assert "📝" in notifications[0]

    # Should contain the selection text
    assert "nx_utils" in notifications[0]
    assert "lines 7 to 7" in notifications[0]

    # Short selections should not be in a collapsible details element
    assert "<details" not in notifications[0]

    # Remaining text should not have the tag
    assert remaining == "Can you explain this?"
    assert "<ide_selection>" not in remaining


def test_extract_ide_selection_long():
    """Test extraction of long IDE selection with collapsible rendering."""
    long_selection = "The user selected lines 1 to 50:\n" + ("line content\n" * 30)
    text = f"<ide_selection>{long_selection}</ide_selection>\nWhat does this do?"

    notifications, remaining = extract_ide_notifications(text)

    # Should have one notification
    assert len(notifications) == 1

    # Should have pencil emoji
    assert "📝" in notifications[0]

    # Long selections should be in a collapsible details element
    assert "<details class='ide-selection-collapsible'>" in notifications[0]
    assert "<summary>" in notifications[0]
    assert "<pre class='ide-selection-content'>" in notifications[0]

    # Should show preview in summary (truncated)
    assert "..." in notifications[0]  # Preview indicator

    # Should contain the full content in the pre block
    assert "line content" in notifications[0]

    # Remaining text should not have the tag
    assert remaining == "What does this do?"
    assert "<ide_selection>" not in remaining


def test_extract_ide_selection_with_special_chars():
    """Test that special HTML characters are escaped in IDE selection."""
    text = '<ide_selection>Code with <brackets> & "quotes"</ide_selection>'

    notifications, remaining = extract_ide_notifications(text)

    # Should have one notification
    assert len(notifications) == 1

    # Should escape HTML special characters
    assert "&lt;brackets&gt;" in notifications[0]
    assert "&amp;" in notifications[0]
    assert (
        "&quot;quotes&quot;" in notifications[0]
        or "&#x27;quotes&#x27;" in notifications[0]
    )

    # Remaining should be empty
    assert remaining == ""


def test_extract_all_ide_tag_types():
    """Test handling all IDE tag types together."""
    text = (
        "<ide_opened_file>User opened main.py</ide_opened_file>\n"
        "<ide_selection>selected_variable</ide_selection>\n"
        "<post-tool-use-hook><ide_diagnostics>["
        '{"line": 5, "message": "Unused variable"}'
        "]</ide_diagnostics></post-tool-use-hook>\n"
        "Please help."
    )

    notifications, remaining = extract_ide_notifications(text)

    # Should have 3 notifications total: 1 file + 1 selection + 1 diagnostic
    assert len(notifications) == 3

    # First should be file open
    assert "🤖" in notifications[0]
    assert "User opened main.py" in notifications[0]

    # Second should be selection
    assert "📝" in notifications[1]
    assert "selected_variable" in notifications[1]

    # Third should be diagnostic
    assert "⚠️" in notifications[2]
    assert "IDE Diagnostic" in notifications[2]
    assert "Unused variable" in notifications[2]

    # Remaining text should be clean
    assert remaining == "Please help."

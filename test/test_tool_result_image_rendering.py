"""Test image rendering within tool results."""

from claude_code_log.renderer import format_tool_result_content
from claude_code_log.models import ToolResultContent


def test_tool_result_with_image():
    """Test that tool results containing images are rendered correctly with collapsible blocks."""
    # Sample base64 image data (1x1 red pixel PNG)
    sample_image_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="

    # Tool result with text and image
    tool_result = ToolResultContent(
        type="tool_result",
        tool_use_id="screenshot_123",
        content=[
            {"type": "text", "text": "Screenshot captured successfully"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": sample_image_data,
                },
            },
        ],
        is_error=False,
    )

    html = format_tool_result_content(tool_result)

    # Should be collapsible when images are present
    assert '<details class="collapsible-details">' in html
    assert "<summary>" in html
    assert "Text and image content" in html

    # Should contain the text
    assert "Screenshot captured successfully" in html

    # Should contain the image with proper data URL
    assert "<img src=" in html
    assert f"data:image/png;base64,{sample_image_data}" in html
    assert 'alt="Tool result image"' in html

    # Should have proper styling
    assert "max-width: 100%" in html
    assert "border: 1px solid #ddd" in html


def test_tool_result_with_only_image():
    """Test tool result with only an image (no text)."""
    sample_image_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="

    tool_result = ToolResultContent(
        type="tool_result",
        tool_use_id="screenshot_456",
        content=[
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": sample_image_data,
                },
            },
        ],
        is_error=False,
    )

    html = format_tool_result_content(tool_result)

    # Should be collapsible
    assert '<details class="collapsible-details">' in html
    assert "Text and image content" in html

    # Should contain the image with JPEG media type
    assert f"data:image/jpeg;base64,{sample_image_data}" in html


def test_tool_result_with_multiple_images():
    """Test tool result with multiple images."""
    image_data_1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    image_data_2 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR42mNk+M/AyMDIwAAACRoB/1M6xG8AAAAASUVORK5CYII="

    tool_result = ToolResultContent(
        type="tool_result",
        tool_use_id="multi_screenshot_789",
        content=[
            {"type": "text", "text": "Multiple screenshots captured"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_data_1,
                },
            },
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_data_2,
                },
            },
        ],
        is_error=False,
    )

    html = format_tool_result_content(tool_result)

    # Should contain both images
    assert html.count("<img src=") == 2
    assert f"data:image/png;base64,{image_data_1}" in html
    assert f"data:image/png;base64,{image_data_2}" in html

    # Should contain the text
    assert "Multiple screenshots captured" in html


def test_tool_result_text_only_unchanged():
    """Test that text-only tool results still work as before."""
    tool_result = ToolResultContent(
        type="tool_result",
        tool_use_id="text_only_123",
        content="This is just text content",
        is_error=False,
    )

    html = format_tool_result_content(tool_result)

    # Short text should not be collapsible
    assert '<details class="collapsible-details">' not in html
    assert "<pre>This is just text content</pre>" in html


def test_tool_result_structured_text_only():
    """Test tool result with structured text (no images)."""
    tool_result = ToolResultContent(
        type="tool_result",
        tool_use_id="structured_text_456",
        content=[
            {"type": "text", "text": "First line"},
            {"type": "text", "text": "Second line"},
        ],
        is_error=False,
    )

    html = format_tool_result_content(tool_result)

    # Should contain both text lines
    assert "First line" in html
    assert "Second line" in html

    # Should not be treated as having images
    assert "Text and image content" not in html

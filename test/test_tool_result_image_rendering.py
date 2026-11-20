"""Test image rendering within tool results and assistant messages."""

from claude_code_log.html.tool_formatters import format_tool_result_content_raw
from claude_code_log.html.assistant_formatters import format_assistant_text_content
from claude_code_log.models import (
    AssistantTextMessage,
    ImageContent,
    ImageSource,
    MessageMeta,
    TextContent,
    ToolResultContent,
)


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

    html = format_tool_result_content_raw(tool_result)

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

    # Should have proper CSS class for styling
    assert 'class="tool-result-image"' in html


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

    html = format_tool_result_content_raw(tool_result)

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

    html = format_tool_result_content_raw(tool_result)

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

    html = format_tool_result_content_raw(tool_result)

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

    html = format_tool_result_content_raw(tool_result)

    # Should contain both text lines
    assert "First line" in html
    assert "Second line" in html

    # Should not be treated as having images
    assert "Text and image content" not in html


# =============================================================================
# Assistant Message Image Tests
# =============================================================================
# These tests prepare for future image generation capabilities where Claude
# might return ImageContent in assistant messages.


def _make_meta() -> MessageMeta:
    """Create a minimal MessageMeta for testing."""
    return MessageMeta(
        session_id="test-session",
        timestamp="2024-01-01T00:00:00Z",
        uuid="test-uuid",
    )


def test_assistant_message_with_image():
    """Test assistant message containing an image (future image generation)."""
    sample_image_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="

    content = AssistantTextMessage(
        _make_meta(),
        items=[
            ImageContent(
                type="image",
                source=ImageSource(
                    type="base64",
                    media_type="image/png",
                    data=sample_image_data,
                ),
            ),
        ],
    )

    html = format_assistant_text_content(content)

    # Should contain the image with proper data URL
    assert "<img src=" in html
    assert f"data:image/png;base64,{sample_image_data}" in html
    assert 'class="uploaded-image"' in html


def test_assistant_message_with_text_and_image():
    """Test assistant message with interleaved text and image."""
    sample_image_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="

    content = AssistantTextMessage(
        _make_meta(),
        items=[
            TextContent(type="text", text="Here is the generated image:"),
            ImageContent(
                type="image",
                source=ImageSource(
                    type="base64",
                    media_type="image/png",
                    data=sample_image_data,
                ),
            ),
            TextContent(type="text", text="The image shows a red pixel."),
        ],
    )

    html = format_assistant_text_content(content)

    # Should contain the text (rendered as markdown)
    assert "Here is the generated image:" in html
    assert "The image shows a red pixel." in html

    # Should contain the image
    assert "<img src=" in html
    assert f"data:image/png;base64,{sample_image_data}" in html


def test_assistant_message_with_multiple_images():
    """Test assistant message with multiple generated images."""
    image_data_1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    image_data_2 = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR42mNk+M/AyMDIwAAACRoB/1M6xG8AAAAASUVORK5CYII="

    content = AssistantTextMessage(
        _make_meta(),
        items=[
            TextContent(type="text", text="Generated two variations:"),
            ImageContent(
                type="image",
                source=ImageSource(
                    type="base64",
                    media_type="image/png",
                    data=image_data_1,
                ),
            ),
            ImageContent(
                type="image",
                source=ImageSource(
                    type="base64",
                    media_type="image/jpeg",
                    data=image_data_2,
                ),
            ),
        ],
    )

    html = format_assistant_text_content(content)

    # Should contain both images
    assert html.count("<img src=") == 2
    assert f"data:image/png;base64,{image_data_1}" in html
    assert f"data:image/jpeg;base64,{image_data_2}" in html


def test_tool_result_image_held_by_reference():
    """A parsed tool result's image is a reference (`image_store`): it
    renders from the held bytes, and is left out, like an unsafe one, when
    its bytes can't be found."""
    import base64

    from claude_code_log import image_store

    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
    )
    ref = image_store.hold_image(raw, "image/png")
    missing = {**ref, "digest": "f" * 32}
    tool_result = ToolResultContent(
        type="tool_result",
        tool_use_id="read_1",
        content=[
            {"type": "text", "text": "Read it"},
            {"type": "image", "source": ref},
            {"type": "image", "source": missing},
        ],
        is_error=False,
    )

    html = format_tool_result_content_raw(tool_result)

    data_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    assert html.count("<img src=") == 1
    assert data_url in html

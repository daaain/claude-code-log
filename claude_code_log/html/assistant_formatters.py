"""HTML formatters for assistant message content.

This module formats assistant message content types to HTML.
Part of the thematic formatter organization:
- system_formatters.py: SystemMessage, HookSummaryMessage
- user_formatters.py: SlashCommandMessage, CommandOutputMessage, BashInputMessage
- assistant_formatters.py: AssistantTextMessage, ThinkingMessage, ImageContent
- tool_formatters.py: tool use/result content

Content models are defined in models.py, this module only handles formatting.
"""

from typing import Callable, Optional

from ..image_export import export_image
from ..models import (
    AssistantTextMessage,
    ImageContent,
    ThinkingMessage,
    UnknownMessage,
)
from .utils import escape_html, render_markdown_collapsible

# Type alias for image formatter callback
ImageFormatter = Callable[[ImageContent], str]


# =============================================================================
# Formatting Functions
# =============================================================================


def format_assistant_text_content(
    content: AssistantTextMessage,
    line_threshold: int = 30,
    preview_line_count: int = 10,
    image_formatter: Optional[ImageFormatter] = None,
) -> str:
    """Format assistant text content as HTML.

    Iterates through content.items preserving order:
    - TextContent: Rendered as markdown with collapsible support
    - ImageContent: Rendered as inline <img> tag

    Args:
        content: AssistantTextMessage with text/items to render
        line_threshold: Number of lines before content becomes collapsible
        preview_line_count: Number of preview lines to show when collapsed
        image_formatter: Optional callback for image formatting. If None, uses
            format_image_content() which embeds images as base64 data URLs.

    Returns:
        HTML string with markdown-rendered, optionally collapsible content
    """
    formatter = image_formatter or format_image_content
    parts: list[str] = []
    for item in content.items:
        if isinstance(item, ImageContent):
            parts.append(formatter(item))
        else:  # TextContent
            if item.text.strip():
                text_html = render_markdown_collapsible(
                    item.text,
                    "assistant-text",
                    line_threshold=line_threshold,
                    preview_line_count=preview_line_count,
                )
                parts.append(text_html)
    return "\n".join(parts)


def format_thinking_content(
    content: ThinkingMessage,
    line_threshold: int = 20,
    preview_line_count: int = 5,
) -> str:
    """Format thinking content as HTML.

    Args:
        content: ThinkingMessage with the thinking text
        line_threshold: Number of lines before content becomes collapsible
        preview_line_count: Number of preview lines to show when collapsed

    Returns:
        HTML string with markdown-rendered, optionally collapsible thinking content
    """
    return render_markdown_collapsible(
        content.thinking,
        "thinking-content",
        line_threshold=line_threshold,
        preview_line_count=preview_line_count,
    )


def format_image_content(image: ImageContent) -> str:
    """Format image content as HTML with embedded base64 data.

    This is the default image formatter for backward compatibility.
    For other export modes (referenced, placeholder), use the renderer's
    _format_image() method via the image_formatter callback.

    Args:
        image: ImageContent with base64 image data

    Returns:
        HTML img tag with data URL
    """
    src = export_image(image, mode="embedded")
    if src is None:
        return "[Image]"
    # Escape the src: export_image allowlists the media type and
    # validates the base64, but the data: URL still must not be able to
    # break out of the attribute (issue #277).
    return f'<img src="{escape_html(src)}" alt="image" class="uploaded-image" />'


def format_unknown_content(content: UnknownMessage) -> str:
    """Format unknown content type as HTML.

    Args:
        content: UnknownMessage with the type name

    Returns:
        HTML paragraph with escaped type name
    """
    escaped_type = escape_html(content.type_name)
    return f"<p>Unknown content type: {escaped_type}</p>"


# =============================================================================
# Public Exports
# =============================================================================

__all__ = [
    # Formatting functions
    "format_assistant_text_content",
    "format_thinking_content",
    "format_image_content",
    "format_unknown_content",
]

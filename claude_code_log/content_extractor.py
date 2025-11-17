#!/usr/bin/env python3
"""Extract data from ContentItem objects without formatting.

This module provides shared content extraction logic used by both HTML and text renderers.
It separates data extraction from presentation formatting.
"""

import json
from typing import Any, Dict, List, Union, Optional
from dataclasses import dataclass

from .models import (
    ContentItem,
    TextContent,
    ToolUseContent,
    ToolResultContent,
    ThinkingContent,
    ImageContent,
)


@dataclass
class ExtractedText:
    """Extracted text content."""

    text: str


@dataclass
class ExtractedThinking:
    """Extracted thinking content."""

    thinking: str
    signature: Optional[str] = None


@dataclass
class ExtractedToolUse:
    """Extracted tool use content."""

    name: str
    id: str
    input: Dict[str, Any]


@dataclass
class ExtractedToolResult:
    """Extracted tool result content."""

    tool_use_id: str
    is_error: bool
    content: Union[str, List[Dict[str, Any]]]


@dataclass
class ExtractedImage:
    """Extracted image content."""

    media_type: str
    data: str


# Union type for all extracted content
ExtractedContent = Union[
    ExtractedText,
    ExtractedThinking,
    ExtractedToolUse,
    ExtractedToolResult,
    ExtractedImage,
]


def extract_content_data(content: ContentItem) -> Optional[ExtractedContent]:
    """Extract raw data from ContentItem without any formatting.

    Args:
        content: A ContentItem object (TextContent, ToolUseContent, etc.)

    Returns:
        Extracted data as a dataclass, or None if content type is unknown
    """
    # Handle TextContent
    if isinstance(content, TextContent) or (
        hasattr(content, "type") and getattr(content, "type") == "text"
    ):
        text = getattr(content, "text", str(content))
        return ExtractedText(text=text)

    # Handle ThinkingContent
    elif isinstance(content, ThinkingContent) or (
        hasattr(content, "type") and getattr(content, "type") == "thinking"
    ):
        thinking_text = getattr(content, "thinking", "")
        signature = getattr(content, "signature", None)
        return ExtractedThinking(thinking=thinking_text, signature=signature)

    # Handle ToolUseContent
    elif isinstance(content, ToolUseContent) or (
        hasattr(content, "type") and getattr(content, "type") == "tool_use"
    ):
        tool_name = getattr(content, "name", "unknown")
        tool_id = getattr(content, "id", "")
        tool_input = getattr(content, "input", {})
        return ExtractedToolUse(name=tool_name, id=tool_id, input=tool_input)

    # Handle ToolResultContent
    elif isinstance(content, ToolResultContent) or (
        hasattr(content, "type") and getattr(content, "type") == "tool_result"
    ):
        tool_use_id = getattr(content, "tool_use_id", "")
        is_error = getattr(content, "is_error", False)
        content_data = getattr(content, "content", "")
        return ExtractedToolResult(
            tool_use_id=tool_use_id, is_error=is_error, content=content_data
        )

    # Handle ImageContent
    elif isinstance(content, ImageContent) or (
        hasattr(content, "type") and getattr(content, "type") == "image"
    ):
        source = getattr(content, "source", {})
        media_type = (
            getattr(source, "media_type", "unknown")
            if hasattr(source, "media_type")
            else "unknown"
        )
        data = getattr(source, "data", "") if hasattr(source, "data") else ""
        return ExtractedImage(media_type=media_type, data=data)

    # Unknown content type
    return None


def format_tool_input_json(tool_input: Dict[str, Any], indent: int = 2) -> str:
    """Format tool input as indented JSON string.

    Args:
        tool_input: Tool input dictionary
        indent: Number of spaces for JSON indentation

    Returns:
        Formatted JSON string
    """
    return json.dumps(tool_input, indent=indent)


def is_text_content(content: ContentItem) -> bool:
    """Check if content is TextContent."""
    return isinstance(content, TextContent) or (
        hasattr(content, "type") and getattr(content, "type") == "text"
    )


def is_thinking_content(content: ContentItem) -> bool:
    """Check if content is ThinkingContent."""
    return isinstance(content, ThinkingContent) or (
        hasattr(content, "type") and getattr(content, "type") == "thinking"
    )


def is_tool_use_content(content: ContentItem) -> bool:
    """Check if content is ToolUseContent."""
    return isinstance(content, ToolUseContent) or (
        hasattr(content, "type") and getattr(content, "type") == "tool_use"
    )


def is_tool_result_content(content: ContentItem) -> bool:
    """Check if content is ToolResultContent."""
    return isinstance(content, ToolResultContent) or (
        hasattr(content, "type") and getattr(content, "type") == "tool_result"
    )


def is_image_content(content: ContentItem) -> bool:
    """Check if content is ImageContent."""
    return isinstance(content, ImageContent) or (
        hasattr(content, "type") and getattr(content, "type") == "image"
    )

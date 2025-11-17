#!/usr/bin/env python3
"""Render Claude transcript data to plain text/markdown format."""

import json
from typing import List, Dict, Optional

from .models import (
    TranscriptEntry,
    AssistantTranscriptEntry,
    UserTranscriptEntry,
    SummaryTranscriptEntry,
    SystemTranscriptEntry,
    ContentItem,
    UsageInfo,
)
from .parser import extract_text_content
from .renderer import format_timestamp
from .content_extractor import (
    extract_content_data,
    ExtractedText,
    ExtractedThinking,
    ExtractedToolUse,
    ExtractedToolResult,
    format_tool_input_json,
)


def format_usage_info(usage: Optional[UsageInfo]) -> str:
    """Format token usage information."""
    if not usage:
        return ""

    parts: List[str] = []
    if usage.input_tokens is not None:
        parts.append(f"Input: {usage.input_tokens}")
    if usage.output_tokens is not None:
        parts.append(f"Output: {usage.output_tokens}")
    if usage.cache_creation_input_tokens:
        parts.append(f"Cache Creation: {usage.cache_creation_input_tokens}")
    if usage.cache_read_input_tokens:
        parts.append(f"Cache Read: {usage.cache_read_input_tokens}")

    return " | ".join(parts) if parts else ""


def render_text_content(content: ContentItem, indent: int = 0) -> str:
    """Render a single content item as plain text."""
    prefix = "  " * indent

    # Extract data from content item
    extracted = extract_content_data(content)

    if extracted is None:
        return f"{prefix}[UNKNOWN CONTENT TYPE: {type(content).__name__}]"

    # Handle text content
    if isinstance(extracted, ExtractedText):
        lines = extracted.text.split("\n")
        return "\n".join(f"{prefix}{line}" for line in lines)

    # Handle thinking content
    elif isinstance(extracted, ExtractedThinking):
        lines = extracted.thinking.split("\n")
        result: List[str] = [f"{prefix}[THINKING]"]
        result.extend(f"{prefix}  {line}" for line in lines)
        return "\n".join(result)

    # Handle tool use
    elif isinstance(extracted, ExtractedToolUse):
        result: List[str] = [f"{prefix}[TOOL USE: {extracted.name}]"]
        if extracted.id:
            result.append(f"{prefix}  ID: {extracted.id}")
        if extracted.input:
            # Format input as JSON with indentation
            input_json = format_tool_input_json(extracted.input, indent=2)
            for line in input_json.split("\n"):
                result.append(f"{prefix}  {line}")
        return "\n".join(result)

    # Handle tool result
    elif isinstance(extracted, ExtractedToolResult):
        status = "ERROR" if extracted.is_error else "RESULT"
        result: List[str] = [f"{prefix}[TOOL {status}]"]
        if extracted.tool_use_id:
            result.append(f"{prefix}  Tool Use ID: {extracted.tool_use_id}")

        # Format content
        if isinstance(extracted.content, str):
            lines = extracted.content.split("\n")
            for line in lines:
                result.append(f"{prefix}  {line}")
        elif isinstance(extracted.content, list):  # type: ignore[reportUnnecessaryIsInstance]
            # Handle structured content
            for item in extracted.content:
                if isinstance(item, dict):  # type: ignore[reportUnnecessaryIsInstance]
                    item_json = json.dumps(item, indent=2)
                    for line in item_json.split("\n"):
                        result.append(f"{prefix}  {line}")
                else:
                    result.append(f"{prefix}  {item}")
        else:
            result.append(f"{prefix}  {extracted.content}")

        return "\n".join(result)

    # Handle image content
    else:  # ExtractedImage
        return f"{prefix}[IMAGE: {extracted.media_type}]"


def render_message_contents(content_list: List[ContentItem], indent: int = 0) -> str:
    """Render a list of content items."""
    if not content_list:
        return ""

    parts: List[str] = []
    for content in content_list:
        rendered = render_text_content(content, indent)
        if rendered:
            parts.append(rendered)

    return "\n".join(parts)


def render_user_message(message: UserTranscriptEntry, format_type: str = "text") -> str:
    """Render a user message in plain text format."""
    lines: List[str] = []

    # Header
    timestamp = format_timestamp(message.timestamp)
    if format_type == "markdown":
        lines.append(f"### User ({timestamp})")
        lines.append("")
    else:
        lines.append("=" * 80)
        lines.append(f"USER | {timestamp}")
        if message.cwd:
            lines.append(f"Working Directory: {message.cwd}")
        lines.append("=" * 80)

    # Content
    if hasattr(message.message, "content"):
        content = message.message.content
        if isinstance(content, str):
            lines.append(content)
        else:
            # Content is List[ContentItem]
            lines.append(render_message_contents(content))

    lines.append("")
    return "\n".join(lines)


def render_assistant_message(
    message: AssistantTranscriptEntry, format_type: str = "text"
) -> str:
    """Render an assistant message in plain text format."""
    lines: List[str] = []

    # Header
    timestamp = format_timestamp(message.timestamp)
    usage_str = (
        format_usage_info(message.message.usage)
        if hasattr(message.message, "usage")
        else ""
    )

    if format_type == "markdown":
        lines.append(f"### Assistant ({timestamp})")
        if usage_str:
            lines.append(f"*{usage_str}*")
        lines.append("")
    else:
        lines.append("-" * 80)
        lines.append(f"ASSISTANT | {timestamp}")
        if usage_str:
            lines.append(f"Tokens: {usage_str}")
        if message.message.model:
            lines.append(f"Model: {message.message.model}")
        lines.append("-" * 80)

    # Content
    if hasattr(message.message, "content") and message.message.content:
        lines.append(render_message_contents(message.message.content))

    lines.append("")
    return "\n".join(lines)


def render_summary(message: SummaryTranscriptEntry, format_type: str = "text") -> str:
    """Render a session summary."""
    if format_type == "markdown":
        return f"**Session Summary:** {message.summary}\n\n"
    else:
        return f"[SESSION SUMMARY] {message.summary}\n\n"


def render_system_message(
    message: SystemTranscriptEntry, format_type: str = "text"
) -> str:
    """Render a system message."""
    timestamp = format_timestamp(message.timestamp)
    level = getattr(message, "level", "info").upper()

    if format_type == "markdown":
        return f"*System {level} ({timestamp}):* {message.content}\n\n"
    else:
        return f"[SYSTEM {level}] {timestamp}: {message.content}\n\n"


def generate_text(
    messages: List[TranscriptEntry],
    title: Optional[str] = None,
    format_type: str = "text",
    include_summaries: bool = False,
    include_system_messages: bool = False,
) -> str:
    """Generate plain text or markdown from transcript messages.

    Args:
        messages: List of transcript entries to render
        title: Optional title for the output
        format_type: Output format - "text" or "markdown"
        include_summaries: Whether to include session summaries
        include_system_messages: Whether to include system messages

    Returns:
        Formatted text output
    """
    if not title:
        title = "Claude Transcript"

    lines: List[str] = []

    # Add title
    if format_type == "markdown":
        lines.append(f"# {title}")
        lines.append("")
    else:
        lines.append("=" * 80)
        lines.append(title.center(80))
        lines.append("=" * 80)
        lines.append("")

    # Group messages by session if needed
    session_summaries: Dict[str, str] = {}
    uuid_to_session: Dict[str, str] = {}

    # Build mapping from message UUID to session ID for summaries
    for message in messages:
        if hasattr(message, "uuid") and hasattr(message, "sessionId"):
            message_uuid = getattr(message, "uuid", "")
            session_id = getattr(message, "sessionId", "")
            if (
                message_uuid
                and session_id
                and isinstance(message, AssistantTranscriptEntry)
            ):
                uuid_to_session[message_uuid] = session_id

    # Map summaries to sessions
    if include_summaries:
        for message in messages:
            if isinstance(message, SummaryTranscriptEntry):
                leaf_uuid = message.leafUuid
                if leaf_uuid in uuid_to_session:
                    session_summaries[uuid_to_session[leaf_uuid]] = message.summary

    # Track current session for summary insertion
    current_session = None
    session_started = False

    # Render messages
    for message in messages:
        # Handle session changes
        if hasattr(message, "sessionId"):
            session_id = getattr(message, "sessionId", "")
            if session_id and session_id != current_session:
                current_session = session_id
                session_started = True

                # Add session separator
                if format_type == "markdown":
                    lines.append(f"## Session: {session_id[:8]}...")
                    if session_id in session_summaries:
                        lines.append(f"**Summary:** {session_summaries[session_id]}")
                    lines.append("")
                else:
                    lines.append("\n" + "#" * 80)
                    lines.append(f"# SESSION: {session_id}")
                    if session_id in session_summaries:
                        lines.append(f"# Summary: {session_summaries[session_id]}")
                    lines.append("#" * 80)
                    lines.append("")

        # Render message based on type
        if isinstance(message, UserTranscriptEntry):
            lines.append(render_user_message(message, format_type))
        elif isinstance(message, AssistantTranscriptEntry):
            lines.append(render_assistant_message(message, format_type))
        elif isinstance(message, SummaryTranscriptEntry):
            # Skip summaries if not including them or if we already showed it in session header
            if include_summaries and not session_started:
                lines.append(render_summary(message, format_type))
        elif isinstance(message, SystemTranscriptEntry):
            if include_system_messages:
                lines.append(render_system_message(message, format_type))
        else:
            # QueueOperationTranscriptEntry - skip in text output
            pass

        if session_started:
            session_started = False

    return "\n".join(lines)


def generate_markdown(
    messages: List[TranscriptEntry], title: Optional[str] = None
) -> str:
    """Generate markdown format output (convenience wrapper)."""
    return generate_text(
        messages, title, format_type="markdown", include_summaries=True
    )


def _truncate_lines(text: str, max_lines: int = 10) -> str:
    """Truncate text to maximum number of lines."""
    lines_list = text.split("\n")
    if len(lines_list) <= max_lines:
        return text

    truncated = "\n".join(lines_list[:max_lines])
    remaining = len(lines_list) - max_lines
    return f"{truncated}\n… +{remaining} lines"


def generate_chat(messages: List[TranscriptEntry], title: Optional[str] = None) -> str:
    """Generate compact chat format output - clean conversation flow with tool use.

    Args:
        messages: List of transcript entries to render
        title: Optional title (not used in chat format for cleaner output)

    Returns:
        Formatted chat-style text output
    """
    lines: List[str] = []

    for message in messages:
        # Render user and assistant messages for conversation flow
        if isinstance(message, UserTranscriptEntry):
            # Check for tool results first
            has_tool_result = False
            if hasattr(message.message, "content") and isinstance(
                message.message.content, list
            ):
                for item in message.message.content:
                    extracted = extract_content_data(item)

                    if isinstance(extracted, ExtractedToolResult):
                        has_tool_result = True
                        # Show tool result with truncated output
                        if isinstance(extracted.content, str):
                            truncated = _truncate_lines(extracted.content, 10)
                            # Indent each line of the result
                            indented_lines: List[str] = []
                            for line in truncated.split("\n"):
                                indented_lines.append(f"     {line}")
                            lines.append(f"  ⎿  {indented_lines[0]}")
                            lines.extend(indented_lines[1:])
                        lines.append("")

            # If no tool result, show user message
            if not has_tool_result:
                if hasattr(message.message, "content"):
                    content = message.message.content
                    if isinstance(content, str):
                        text = content
                    else:
                        # Extract text from content list
                        text = extract_text_content(content)

                    if text:
                        lines.append(f"> {text}")
                        lines.append("")

        elif isinstance(message, AssistantTranscriptEntry):
            # Show assistant text and tool use
            if hasattr(message.message, "content") and message.message.content:
                text_parts: List[str] = []
                tool_parts: List[str] = []

                for item in message.message.content:
                    extracted = extract_content_data(item)

                    if isinstance(extracted, ExtractedText):
                        if extracted.text:
                            text_parts.append(extracted.text)

                    elif isinstance(extracted, ExtractedToolUse):
                        # Show tool use compactly
                        # Format tool use on one line or truncated
                        if extracted.input:
                            input_str = json.dumps(
                                extracted.input, separators=(",", ":")
                            )
                            if len(input_str) > 100:
                                input_str = input_str[:100] + "…"
                            tool_parts.append(f"⏺ {extracted.name}({input_str})")
                        else:
                            tool_parts.append(f"⏺ {extracted.name}()")

                # Output assistant message
                if text_parts or tool_parts:
                    if text_parts:
                        combined_text = "\n".join(text_parts)
                        lines.append(f"⏺ {combined_text}")
                    if tool_parts:
                        for tool_line in tool_parts:
                            lines.append(tool_line)
                    lines.append("")

        # Skip summaries, system messages, and queue operations for clean chat flow

    return "\n".join(lines)

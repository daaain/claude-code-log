"""Factory for creating TranscriptEntry and ContentItem instances from raw data.

This module creates typed model instances from JSONL transcript data:
- TranscriptEntry subclasses (User, Assistant, Summary, System, QueueOperation)
- ContentItem subclasses (Text, ToolUse, ToolResult, Thinking, Image)

Also provides:
- Conditional casts for TranscriptEntry discrimination
- Usage info normalization
"""

from typing import Any, Callable, Sequence, cast

from pydantic import BaseModel

from ..transcript_ids import sanitize_entry_ids
from ..models import (
    # Content types
    ContentItem,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
    # Transcript entry types
    AiTitleTranscriptEntry,
    AssistantTranscriptEntry,
    AttachmentTranscriptEntry,
    MessageType,
    PassthroughTranscriptEntry,
    QueueOperationTranscriptEntry,
    SummaryTranscriptEntry,
    SystemTranscriptEntry,
    TranscriptEntry,
    UsageInfo,
    UserTranscriptEntry,
)


# =============================================================================
# Content Item Registry
# =============================================================================

# Maps content type strings to their model classes
CONTENT_ITEM_CREATORS: dict[str, type[BaseModel]] = {
    "text": TextContent,
    "tool_result": ToolResultContent,
    "image": ImageContent,
    "tool_use": ToolUseContent,
    "thinking": ThinkingContent,
}

# Content types allowed in each context
USER_CONTENT_TYPES: Sequence[str] = ("text", "tool_result", "image")
ASSISTANT_CONTENT_TYPES: Sequence[str] = ("text", "tool_use", "thinking")


# =============================================================================
# Conditional Casts
# =============================================================================


def as_user_entry(entry: TranscriptEntry) -> UserTranscriptEntry | None:
    """Return entry as UserTranscriptEntry if it is one, else None."""
    if entry.type == MessageType.USER:
        return cast(UserTranscriptEntry, entry)
    return None


def as_assistant_entry(entry: TranscriptEntry) -> AssistantTranscriptEntry | None:
    """Return entry as AssistantTranscriptEntry if it is one, else None."""
    if entry.type == MessageType.ASSISTANT:
        return cast(AssistantTranscriptEntry, entry)
    return None


# =============================================================================
# Usage Info Normalization
# =============================================================================


def normalize_usage_info(usage_data: dict[str, Any] | None) -> UsageInfo | None:
    """Normalize usage data from JSON to UsageInfo."""
    if usage_data is None:
        return None
    return UsageInfo.model_validate(usage_data)


# =============================================================================
# Content Item Creation
# =============================================================================


def create_content_item(
    item_data: dict[str, Any],
    type_filter: Sequence[str] | None = None,
) -> ContentItem:
    """Create a ContentItem from raw data using the registry.

    Args:
        item_data: The raw dictionary data
        type_filter: Sequence of content type strings to allow, or None to allow all
            (e.g., USER_CONTENT_TYPES, ASSISTANT_CONTENT_TYPES)

    Returns:
        ContentItem instance, with fallback to TextContent for unknown types
    """
    try:
        content_type = item_data.get("type", "")

        if type_filter is None or content_type in type_filter:
            model_class = CONTENT_ITEM_CREATORS.get(content_type)
            if model_class is not None:
                return cast(ContentItem, model_class.model_validate(item_data))

        # Fallback to text content for unknown/disallowed types
        return TextContent(type="text", text=str(item_data))
    except Exception:
        return TextContent(type="text", text=str(item_data))


def create_message_content(
    content_data: Any,
    type_filter: Sequence[str] | None = None,
) -> list[ContentItem]:
    """Create a list of ContentItems from message content data.

    Always returns a list for consistent downstream handling. String content
    is wrapped in a TextContent item.

    Args:
        content_data: Raw content data (string or list of items)
        type_filter: Sequence of content type strings to allow, or None to allow all
    """
    if isinstance(content_data, str):
        return [TextContent(type="text", text=content_data)]
    elif isinstance(content_data, list):
        content_list = cast(list[Any], content_data)
        result: list[ContentItem] = []
        for item in content_list:
            if isinstance(item, dict):
                result.append(
                    create_content_item(cast(dict[str, Any], item), type_filter)
                )
            else:
                # Non-dict items (e.g., raw strings) become TextContent
                result.append(TextContent(type="text", text=str(item)))
        return result
    else:
        return [TextContent(type="text", text=str(content_data))]


# =============================================================================
# Transcript Entry Creation
# =============================================================================


def _create_user_entry(data: dict[str, Any]) -> UserTranscriptEntry:
    """Create a UserTranscriptEntry from raw data."""
    data_copy = data.copy()
    if "message" in data_copy and "content" in data_copy["message"]:
        data_copy["message"] = data_copy["message"].copy()
        data_copy["message"]["content"] = create_message_content(
            data_copy["message"]["content"],
            USER_CONTENT_TYPES,
        )
    # Parse toolUseResult if present and it's a list of content items
    if "toolUseResult" in data_copy and isinstance(data_copy["toolUseResult"], list):
        # Check if it's a list of content items (MCP tool results)
        tool_use_result = cast(list[Any], data_copy["toolUseResult"])
        if (
            tool_use_result
            and isinstance(tool_use_result[0], dict)
            and "type" in tool_use_result[0]
        ):
            data_copy["toolUseResult"] = [
                create_content_item(cast(dict[str, Any], item))
                for item in tool_use_result
                if isinstance(item, dict)
            ]
    return UserTranscriptEntry.model_validate(data_copy)


def _create_assistant_entry(data: dict[str, Any]) -> AssistantTranscriptEntry:
    """Create an AssistantTranscriptEntry from raw data."""
    data_copy = data.copy()

    if "message" in data_copy and "content" in data_copy["message"]:
        message_copy = data_copy["message"].copy()
        message_copy["content"] = create_message_content(
            message_copy["content"],
            ASSISTANT_CONTENT_TYPES,
        )

        # Normalize usage data to support both Anthropic and custom formats
        if "usage" in message_copy:
            message_copy["usage"] = normalize_usage_info(message_copy["usage"])

        data_copy["message"] = message_copy
    return AssistantTranscriptEntry.model_validate(data_copy)


def _create_queue_operation_entry(
    data: dict[str, Any],
) -> QueueOperationTranscriptEntry:
    """Create a QueueOperationTranscriptEntry from raw data."""
    data_copy = data.copy()
    if "content" in data_copy and isinstance(data_copy["content"], list):
        data_copy["content"] = create_message_content(data_copy["content"])
    return QueueOperationTranscriptEntry.model_validate(data_copy)


# Registry mapping entry types to their creator functions
ENTRY_CREATORS: dict[str, Callable[[dict[str, Any]], TranscriptEntry]] = {
    "user": _create_user_entry,
    "assistant": _create_assistant_entry,
    "summary": lambda data: SummaryTranscriptEntry.model_validate(data),
    "ai-title": lambda data: AiTitleTranscriptEntry.model_validate(data),
    "system": lambda data: SystemTranscriptEntry.model_validate(data),
    "queue-operation": _create_queue_operation_entry,
    "attachment": lambda data: AttachmentTranscriptEntry.model_validate(data),
}


def create_transcript_entry(data: dict[str, Any]) -> TranscriptEntry:
    """Create a TranscriptEntry from a JSON dictionary.

    Uses a registry-based dispatch to create the appropriate TranscriptEntry
    subclass based on the 'type' field in the data.

    Args:
        data: Dictionary parsed from JSON

    Returns:
        The appropriate TranscriptEntry subclass

    Raises:
        ValueError: If the data doesn't match any known transcript entry type
    """
    # Every entry — parsed from JSONL or rebuilt from a cache row — passes
    # here, so an invalid id never reaches a model (transcript_ids.py).
    sanitize_entry_ids(data)
    entry_type = data.get("type")
    creator = ENTRY_CREATORS.get(entry_type)  # type: ignore[arg-type]
    if creator is not None:
        return creator(data)
    # Fall back to PassthroughTranscriptEntry for unknown types with DAG fields
    if data.get("uuid") and data.get("sessionId"):
        return PassthroughTranscriptEntry(
            uuid=data["uuid"],
            parentUuid=data.get("parentUuid"),
            sessionId=data["sessionId"],
            timestamp=data.get("timestamp", ""),
            type=entry_type,
            isSidechain=data.get("isSidechain", False),
            agentId=data.get("agentId"),
        )
    raise ValueError(f"Unknown transcript entry type: {entry_type}")

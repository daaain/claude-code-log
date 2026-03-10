#!/usr/bin/env python3
"""Render Claude transcript data to HTML format."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple, cast
from datetime import datetime

if TYPE_CHECKING:
    from .cache import CacheManager

from .models import (
    MessageContent,
    MessageMeta,
    MessageType,
    TranscriptEntry,
    AssistantTranscriptEntry,
    SystemTranscriptEntry,
    SummaryTranscriptEntry,
    QueueOperationTranscriptEntry,
    UserTranscriptEntry,
    ContentItem,
    TextContent,
    ToolResultContent,
    ToolUseContent,
    ThinkingContent,
    UsageInfo,
    # Structured content types
    AssistantTextMessage,
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    HookSummaryMessage,
    SessionHeaderMessage,
    SlashCommandMessage,
    SystemMessage,
    TaskOutput,
    ThinkingMessage,
    ToolResultMessage,
    ToolUseMessage,
    UnknownMessage,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserSteeringMessage,
    UserTextMessage,
)
from .parser import extract_text_content
from .factories import (
    as_assistant_entry,
    as_user_entry,
    create_assistant_message,
    create_meta,
    create_system_message,
    create_thinking_message,
    create_tool_result_message,
    create_tool_use_message,
    create_user_message,
    ToolItemResult,
)
from .utils import (
    format_timestamp,
    format_timestamp_range,
    get_project_display_name,
    should_skip_message,
    should_use_as_session_starter,
    create_session_preview,
)
from .renderer_timings import (
    log_timing,
)


# -- Rendering Context --------------------------------------------------------


@dataclass
class RenderingContext:
    """Context for a single rendering operation.

    Holds render-time state that should not pollute MessageContent.
    This enables parallel-safe rendering where each render gets its own context.

    Attributes:
        messages: Registry of all TemplateMessage objects (message_index = index).
        tool_use_context: Maps tool_use_id -> ToolUseContent for result rendering.
        session_first_message: Maps session_id -> index of first message in session.
    """

    messages: list[TemplateMessage] = field(
        default_factory=lambda: []  # type: list[TemplateMessage]
    )
    tool_use_context: dict[str, ToolUseContent] = field(
        default_factory=lambda: {}  # type: dict[str, ToolUseContent]
    )
    session_first_message: dict[str, int] = field(
        default_factory=lambda: {}  # type: dict[str, int]
    )

    def register(self, message: "TemplateMessage") -> int:
        """Register a TemplateMessage and assign its message_index.

        Sets message_index on both the TemplateMessage and its content,
        enabling content→TemplateMessage lookups during rendering.

        Args:
            message: The TemplateMessage to register.

        Returns:
            The assigned message_index (= index in messages list).
        """
        msg_index = len(self.messages)
        message.message_index = msg_index
        message.content.message_index = msg_index  # Enable content→message lookup
        self.messages.append(message)
        return msg_index

    def get(self, message_index: int) -> Optional["TemplateMessage"]:
        """Get a TemplateMessage by its message_index.

        Args:
            message_index: The message_index (index) to look up.

        Returns:
            The TemplateMessage if found, None if out of range.
        """
        if 0 <= message_index < len(self.messages):
            return self.messages[message_index]
        return None


# -- Template Classes ---------------------------------------------------------


class TemplateMessage:
    """Structured message data for template rendering.

    This is the primary render-time object that wraps MessageContent. Each
    MessageContent has exactly one TemplateMessage wrapper.

    TemplateMessage holds all render-time state:
    - message_index: Index in RenderingContext.messages (unique identifier)
    - Pairing metadata: pair_first, pair_last, pair_duration
    - Hierarchy metadata: ancestry
    - Tree structure: children, fold/unfold counts

    All identity/context fields come from meta (timestamp, session_id, etc.)
    and content (tool_use_id, has_markdown, token_usage, etc.).
    """

    def __init__(
        self,
        content: "MessageContent",
        *,  # Force keyword arguments after this
        ancestry: Optional[list[int]] = None,
    ):
        # Content carries its own meta
        self.content = content
        self.meta = content.meta

        # Unique index in RenderingContext.messages (assigned by ctx.register())
        self.message_index: Optional[int] = None

        # Pairing metadata (assigned by _mark_pair())
        self.pair_first: Optional[int] = None  # Index of first message in pair
        self.pair_last: Optional[int] = None  # Index of last message in pair
        self.pair_duration: Optional[str] = None  # Duration string for pair_last

        # Rendering metadata
        self.ancestry = ancestry or []

        # Fold/unfold counts
        self.immediate_children_count = 0  # Direct children only
        self.total_descendants_count = 0  # All descendants recursively
        # Type-aware counting for smarter labels
        self.immediate_children_by_type: dict[
            str, int
        ] = {}  # {"assistant": 2, "tool_use": 3}
        self.total_descendants_by_type: dict[str, int] = {}  # All descendants by type

        # Children for tree-based rendering
        self.children: list["TemplateMessage"] = []

    # -- Properties derived from content/meta --

    @property
    def type(self) -> str:
        """Get message type from content."""
        return self.content.message_type

    @property
    def is_session_header(self) -> bool:
        """Check if this message is a session header."""
        return isinstance(self.content, SessionHeaderMessage)

    @property
    def has_children(self) -> bool:
        """Check if this message has any children."""
        return bool(self.children)

    @property
    def is_paired(self) -> bool:
        """Check if this message is part of a pair."""
        return self.pair_first is not None or self.pair_last is not None

    @property
    def is_first_in_pair(self) -> bool:
        """Check if this is the first message in a pair (has pair_last set)."""
        return self.pair_last is not None

    @property
    def is_last_in_pair(self) -> bool:
        """Check if this is the last message in a pair (has pair_first set)."""
        return self.pair_first is not None

    @property
    def pair_role(self) -> Optional[str]:
        """Get the pairing role for CSS class.

        Returns:
            "pair_first" if this is the first message in a pair,
            "pair_last" if this is the last message in a pair,
            None if not paired.
        """
        if self.is_first_in_pair:
            return "pair_first"
        if self.is_last_in_pair:
            return "pair_last"
        return None

    @property
    def message_id(self) -> Optional[str]:
        """Get formatted message ID for HTML element IDs.

        Returns "d-{message_index}" for all messages, or None if not registered.
        All messages use a unified format based on their index.
        """
        if self.message_index is None:
            return None
        return f"d-{self.message_index}"

    @property
    def session_id(self) -> str:
        """Get session_id from meta."""
        return self.meta.session_id

    @property
    def parent_uuid(self) -> Optional[str]:
        """Get parent_uuid from meta."""
        return self.meta.parent_uuid

    @property
    def agent_id(self) -> Optional[str]:
        """Get agent_id from meta."""
        return self.meta.agent_id

    @property
    def token_usage(self) -> Optional[str]:
        """Get token_usage from content (if available)."""
        return getattr(self.content, "token_usage", None)

    @property
    def is_sidechain(self) -> bool:
        """Check if this is a sidechain message."""
        return self.meta.is_sidechain

    @property
    def tool_use_id(self) -> Optional[str]:
        """Get tool_use_id from content (if ToolUseMessage or ToolResultMessage)."""
        return getattr(self.content, "tool_use_id", None)

    @property
    def title_hint(self) -> Optional[str]:
        """Generate title hint from tool_use_id."""
        tool_id = self.tool_use_id
        if tool_id:
            # Escape for HTML attribute
            escaped = tool_id.replace("&", "&amp;").replace('"', "&quot;")
            return f"ID: {escaped}"
        return None

    def get_immediate_children_label(self) -> str:
        """Generate human-readable label for immediate children."""
        return _format_type_counts(self.immediate_children_by_type)

    def get_total_descendants_label(self) -> str:
        """Generate human-readable label for all descendants."""
        return _format_type_counts(self.total_descendants_by_type)


def _format_type_counts(type_counts: dict[str, int]) -> str:
    """Format type counts into human-readable label.

    Args:
        type_counts: Dictionary of message type to count

    Returns:
        Human-readable label like "3 assistant, 4 tools" or "8 messages"

    Examples:
        {"assistant": 3, "tool_use": 4} -> "3 assistant, 4 tools"
        {"tool_use": 2, "tool_result": 2} -> "2 tool pairs"
        {"assistant": 1} -> "1 assistant"
        {"thinking": 3} -> "3 thoughts"
    """
    if not type_counts:
        return "0 messages"

    # Type name mapping for better readability
    type_labels = {
        "assistant": ("assistant", "assistants"),
        "user": ("user", "users"),
        "tool_use": ("tool", "tools"),
        "tool_result": ("result", "results"),
        "thinking": ("thought", "thoughts"),
        "system": ("system", "systems"),
        "system-warning": ("warning", "warnings"),
        "system-error": ("error", "errors"),
        "system-info": ("info", "infos"),
        "sidechain": ("task", "tasks"),
    }

    # Handle special case: tool_use and tool_result together = "tool pairs"
    # Create a modified counts dict that combines tool pairs
    modified_counts = dict(type_counts)
    if (
        "tool_use" in modified_counts
        and "tool_result" in modified_counts
        and modified_counts["tool_use"] == modified_counts["tool_result"]
    ):
        # Replace tool_use and tool_result with tool_pair
        pair_count = modified_counts["tool_use"]
        del modified_counts["tool_use"]
        del modified_counts["tool_result"]
        modified_counts["tool_pair"] = pair_count

    # Add tool_pair label
    type_labels_with_pairs = {
        **type_labels,
        "tool_pair": ("tool pair", "tool pairs"),
    }

    # Build label parts
    parts: list[str] = []
    for msg_type, count in sorted(
        modified_counts.items(), key=lambda x: x[1], reverse=True
    ):
        singular, plural = type_labels_with_pairs.get(
            msg_type, (msg_type, f"{msg_type}s")
        )
        label = singular if count == 1 else plural
        parts.append(f"{count} {label}")

    # Return combined label
    if len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return f"{parts[0]}, {parts[1]}"
    else:
        # For 3+ types, show top 2 and "X more"
        remaining = sum(type_counts.values()) - sum(
            type_counts[t] for t in list(type_counts.keys())[:2]
        )
        return f"{parts[0]}, {parts[1]}, {remaining} more"


class TemplateProject:
    """Structured project data for template rendering."""

    def __init__(self, project_data: dict[str, Any]):
        self.name = project_data["name"]
        self.html_file = project_data["html_file"]
        self.jsonl_count = project_data["jsonl_count"]
        self.message_count = project_data["message_count"]
        self.last_modified = project_data["last_modified"]
        self.total_input_tokens = project_data.get("total_input_tokens", 0)
        self.total_output_tokens = project_data.get("total_output_tokens", 0)
        self.total_cache_creation_tokens = project_data.get(
            "total_cache_creation_tokens", 0
        )
        self.total_cache_read_tokens = project_data.get("total_cache_read_tokens", 0)
        self.latest_timestamp = project_data.get("latest_timestamp", "")
        self.earliest_timestamp = project_data.get("earliest_timestamp", "")
        self.sessions = project_data.get("sessions", [])
        self.working_directories = project_data.get("working_directories", [])

        # Format display name using shared logic
        self.display_name = get_project_display_name(
            self.name, self.working_directories
        )

        # Format last modified date
        last_modified_dt = datetime.fromtimestamp(self.last_modified)
        self.formatted_date = last_modified_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Format interaction time range
        if self.earliest_timestamp and self.latest_timestamp:
            if self.earliest_timestamp == self.latest_timestamp:
                # Single interaction
                self.formatted_time_range = format_timestamp(self.latest_timestamp)
            else:
                # Time range
                earliest_formatted = format_timestamp(self.earliest_timestamp)
                latest_formatted = format_timestamp(self.latest_timestamp)
                self.formatted_time_range = (
                    f"{earliest_formatted} to {latest_formatted}"
                )
        elif self.latest_timestamp:
            self.formatted_time_range = format_timestamp(self.latest_timestamp)
        else:
            self.formatted_time_range = ""

        # Format last interaction timestamp (kept for backward compatibility)
        if self.latest_timestamp:
            self.formatted_last_interaction = format_timestamp(self.latest_timestamp)
        else:
            self.formatted_last_interaction = ""

        # Format token usage
        self.token_summary = ""
        if self.total_input_tokens > 0 or self.total_output_tokens > 0:
            token_parts: list[str] = []
            if self.total_input_tokens > 0:
                token_parts.append(f"Input: {self.total_input_tokens}")
            if self.total_output_tokens > 0:
                token_parts.append(f"Output: {self.total_output_tokens}")
            if self.total_cache_creation_tokens > 0:
                token_parts.append(
                    f"Cache Creation: {self.total_cache_creation_tokens}"
                )
            if self.total_cache_read_tokens > 0:
                token_parts.append(f"Cache Read: {self.total_cache_read_tokens}")
            self.token_summary = " | ".join(token_parts)


class TemplateSummary:
    """Summary statistics for template rendering."""

    def __init__(self, project_summaries: list[dict[str, Any]]):
        self.total_projects = len(project_summaries)
        self.total_jsonl = sum(p["jsonl_count"] for p in project_summaries)
        self.total_messages = sum(p["message_count"] for p in project_summaries)

        # Calculate aggregated token usage
        self.total_input_tokens = sum(
            p.get("total_input_tokens", 0) for p in project_summaries
        )
        self.total_output_tokens = sum(
            p.get("total_output_tokens", 0) for p in project_summaries
        )
        self.total_cache_creation_tokens = sum(
            p.get("total_cache_creation_tokens", 0) for p in project_summaries
        )
        self.total_cache_read_tokens = sum(
            p.get("total_cache_read_tokens", 0) for p in project_summaries
        )

        # Find the most recent and earliest interaction timestamps across all projects
        self.latest_interaction = ""
        self.earliest_interaction = ""
        for project in project_summaries:
            # Check latest timestamp
            latest_timestamp = project.get("latest_timestamp", "")
            if latest_timestamp and (
                not self.latest_interaction
                or latest_timestamp > self.latest_interaction
            ):
                self.latest_interaction = latest_timestamp

            # Check earliest timestamp
            earliest_timestamp = project.get("earliest_timestamp", "")
            if earliest_timestamp and (
                not self.earliest_interaction
                or earliest_timestamp < self.earliest_interaction
            ):
                self.earliest_interaction = earliest_timestamp

        # Format the latest interaction timestamp
        if self.latest_interaction:
            self.formatted_latest_interaction = format_timestamp(
                self.latest_interaction
            )
        else:
            self.formatted_latest_interaction = ""

        # Format the time range
        if self.earliest_interaction and self.latest_interaction:
            if self.earliest_interaction == self.latest_interaction:
                # Single interaction
                self.formatted_time_range = format_timestamp(self.latest_interaction)
            else:
                # Time range
                earliest_formatted = format_timestamp(self.earliest_interaction)
                latest_formatted = format_timestamp(self.latest_interaction)
                self.formatted_time_range = (
                    f"{earliest_formatted} to {latest_formatted}"
                )
        else:
            self.formatted_time_range = ""

        # Format token usage summary
        self.token_summary = ""
        if self.total_input_tokens > 0 or self.total_output_tokens > 0:
            token_parts: list[str] = []
            if self.total_input_tokens > 0:
                token_parts.append(f"Input: {self.total_input_tokens}")
            if self.total_output_tokens > 0:
                token_parts.append(f"Output: {self.total_output_tokens}")
            if self.total_cache_creation_tokens > 0:
                token_parts.append(
                    f"Cache Creation: {self.total_cache_creation_tokens}"
                )
            if self.total_cache_read_tokens > 0:
                token_parts.append(f"Cache Read: {self.total_cache_read_tokens}")
            self.token_summary = " | ".join(token_parts)


# -- Template Generation ------------------------------------------------------


def generate_template_messages(
    messages: list[TranscriptEntry],
) -> Tuple[list[TemplateMessage], list[dict[str, Any]], RenderingContext]:
    """Generate root messages and session navigation from transcript messages.

    This is the format-neutral rendering step that produces data structures
    ready for template rendering by any format-specific renderer.

    Args:
        messages: List of transcript entries to process.

    Returns:
        A tuple of (root_messages, session_nav, context) where:
        - root_messages: Tree of TemplateMessages (session headers with children)
        - session_nav: Session navigation data with summaries and metadata
        - context: RenderingContext with message registry for index lookups
    """
    from .utils import get_warmup_session_ids

    # Performance timing
    t_start = time.time()

    # Filter out warmup-only sessions
    with log_timing("Filter warmup sessions", t_start):
        warmup_session_ids = get_warmup_session_ids(messages)
        if warmup_session_ids:
            messages = [
                msg
                for msg in messages
                if getattr(msg, "sessionId", None) not in warmup_session_ids
            ]

    # Pre-process to find session summaries
    with log_timing("Session summary processing", t_start):
        session_summaries = prepare_session_summaries(messages)

    # Filter messages (removes summaries, warmup, empty, etc.)
    with log_timing("Filter messages", t_start):
        filtered_messages = _filter_messages(messages)

    # Pass 1: Collect session metadata and token tracking
    with log_timing("Collect session info", t_start):
        sessions, session_order, show_tokens_for_message = _collect_session_info(
            filtered_messages, session_summaries
        )

    # Pass 2: Render messages to TemplateMessage objects
    ctx: RenderingContext | None = None
    with log_timing(
        lambda: f"Render messages ({len(ctx.messages) if ctx else 0} messages)", t_start
    ):
        ctx = _render_messages(filtered_messages, sessions, show_tokens_for_message)

    # Prepare session navigation data (uses ctx for session header indices)
    session_nav: list[dict[str, Any]] = []
    with log_timing(
        lambda: f"Session navigation building ({len(session_nav)} sessions)", t_start
    ):
        session_nav = prepare_session_navigation(sessions, session_order, ctx)

    # Reorder messages so each session's messages follow their session header
    # This fixes interleaving that occurs when sessions are resumed
    with log_timing("Reorder session messages", t_start):
        template_messages = _reorder_session_template_messages(ctx.messages)

    # Identify and mark paired messages (command+output, tool_use+tool_result, etc.)
    with log_timing("Identify message pairs", t_start):
        _identify_message_pairs(template_messages)

    # Reorder messages so pairs are adjacent while preserving chronological order
    with log_timing("Reorder paired messages", t_start):
        template_messages = _reorder_paired_messages(template_messages)

    # Reorder sidechains to appear after their Task results
    # This must happen AFTER pair reordering, since that moves tool_results
    with log_timing("Reorder sidechain messages", t_start):
        template_messages = _reorder_sidechain_template_messages(template_messages)

    # Build hierarchy (message_id and ancestry) based on final order
    # This must happen AFTER all reordering to get correct parent-child relationships
    with log_timing("Build message hierarchy", t_start):
        _build_message_hierarchy(template_messages)

    # Mark messages that have children for fold/unfold controls
    with log_timing("Mark messages with children", t_start):
        _mark_messages_with_children(template_messages)

    # Build tree structure by populating children fields
    # Returns root messages (typically session headers) with children populated
    # HtmlRenderer flattens this via pre-order traversal for template rendering
    with log_timing("Build message tree", t_start):
        root_messages = _build_message_tree(template_messages)

    # Clean up sidechain duplicates on the tree structure
    # - Remove first UserTextMessage (duplicate of Task input prompt)
    # - Remove last AssistantTextMessage (duplicate of Task output)
    with log_timing("Cleanup sidechain duplicates", t_start):
        _cleanup_sidechain_duplicates(root_messages)

    return root_messages, session_nav, ctx


# -- Session Utilities --------------------------------------------------------


def prepare_session_summaries(messages: list[TranscriptEntry]) -> dict[str, str]:
    """Extract session summaries from messages.

    Returns:
        Dict mapping session_id to summary text.
    """
    session_summaries: dict[str, str] = {}
    uuid_to_session: dict[str, str] = {}
    uuid_to_session_backup: dict[str, str] = {}

    # Build mapping from message UUID to session ID
    for message in messages:
        if hasattr(message, "uuid") and hasattr(message, "sessionId"):
            message_uuid = getattr(message, "uuid", "")
            session_id = getattr(message, "sessionId", "")
            if message_uuid and session_id:
                # There is often duplication, in that case we want to prioritise the assistant
                # message because summaries are generated from Claude's (last) success message
                if type(message) is AssistantTranscriptEntry:
                    uuid_to_session[message_uuid] = session_id
                else:
                    uuid_to_session_backup[message_uuid] = session_id

    # Map summaries to sessions via leafUuid -> message UUID -> session ID
    for message in messages:
        if isinstance(message, SummaryTranscriptEntry):
            leaf_uuid = message.leafUuid
            if leaf_uuid in uuid_to_session:
                session_summaries[uuid_to_session[leaf_uuid]] = message.summary
            elif (
                leaf_uuid in uuid_to_session_backup
                and uuid_to_session_backup[leaf_uuid] not in session_summaries
            ):
                session_summaries[uuid_to_session_backup[leaf_uuid]] = message.summary

    return session_summaries


def prepare_session_navigation(
    sessions: dict[str, dict[str, Any]],
    session_order: list[str],
    ctx: RenderingContext,
) -> list[dict[str, Any]]:
    """Prepare session navigation data for template rendering.

    Args:
        sessions: Dictionary mapping session_id to session info dict
        session_order: List of session IDs in display order
        ctx: RenderingContext with session_first_message indices

    Returns:
        List of session navigation dicts for template rendering
    """
    session_nav: list[dict[str, Any]] = []

    for session_id in session_order:
        session_info = sessions[session_id]

        # Skip empty sessions (agent-only, no user messages)
        if not session_info["first_user_message"]:
            continue

        # Format timestamp range
        first_ts = session_info["first_timestamp"]
        last_ts = session_info["last_timestamp"]
        timestamp_range = format_timestamp_range(first_ts, last_ts)

        # Format token usage summary
        token_summary = ""
        total_input = session_info["total_input_tokens"]
        total_output = session_info["total_output_tokens"]
        total_cache_creation = session_info["total_cache_creation_tokens"]
        total_cache_read = session_info["total_cache_read_tokens"]

        if total_input > 0 or total_output > 0:
            token_parts: list[str] = []
            if total_input > 0:
                token_parts.append(f"Input: {total_input}")
            if total_output > 0:
                token_parts.append(f"Output: {total_output}")
            if total_cache_creation > 0:
                token_parts.append(f"Cache Creation: {total_cache_creation}")
            if total_cache_read > 0:
                token_parts.append(f"Cache Read: {total_cache_read}")
            token_summary = "Token usage – " + " | ".join(token_parts)

        # Get message_index for session header (for unified d-{index} links)
        message_index = ctx.session_first_message.get(session_id)

        session_nav.append(
            {
                "id": session_id,
                "message_index": message_index,
                "summary": session_info["summary"],
                "timestamp_range": timestamp_range,
                "first_timestamp": first_ts,
                "last_timestamp": last_ts,
                "message_count": session_info["message_count"],
                "first_user_message": session_info["first_user_message"]
                if session_info["first_user_message"] != ""
                else "[No user message found in session.]",
                "token_summary": token_summary,
            }
        )

    return session_nav


# Type alias for chunk output: either a list of regular items or a single special item
ContentChunk = list[ContentItem] | ContentItem


def _is_special_item(item: ContentItem) -> bool:
    """Check if a content item is a 'special' item that should be its own chunk.

    Special items (tool_use, tool_result, thinking) become their own TemplateMessages.
    Regular items (text, image) are accumulated together.
    """
    item_type = getattr(item, "type", None)
    return isinstance(
        item, (ToolUseContent, ToolResultContent, ThinkingContent)
    ) or item_type in ("tool_use", "tool_result", "thinking")


def chunk_message_content(content: list[ContentItem]) -> list[ContentChunk]:
    """Split message content into chunks for TemplateMessage creation.

    This function processes a list of content items and produces chunks where:
    - "Special" items (tool_use, tool_result, thinking) each become their own chunk
    - "Regular" items (text, image) are accumulated into list chunks

    When a special item is encountered, any accumulated regular items are flushed
    as a list chunk first, then the special item is added as a single-item chunk.

    Args:
        content: List of ContentItem from the message

    Returns:
        List of chunks where each chunk is either:
        - A list[ContentItem] of accumulated text/image items
        - A single ContentItem (tool_use, tool_result, or thinking)

    Example:
        Input: [text, image, thinking, text, text, tool_use]
        Output: [[text, image], thinking, [text, text], tool_use]
    """
    if not content:
        return []

    chunks: list[ContentChunk] = []
    accumulated: list[ContentItem] = []

    for item in content:
        if _is_special_item(item):
            # Flush accumulated regular items as a chunk
            if accumulated:
                chunks.append(accumulated)
                accumulated = []
            # Add special item as its own chunk
            chunks.append(item)
        else:
            # Accumulate regular items (text, image), skip empty text
            if hasattr(item, "text"):
                if not getattr(item, "text", "").strip():
                    continue  # Skip empty text
            accumulated.append(item)

    # Flush any remaining accumulated items
    if accumulated:
        chunks.append(accumulated)

    return chunks


# -- Message Pairing ----------------------------------------------------------


@dataclass
class PairingIndices:
    """Indices for efficient message pairing lookups.

    All indices are built in a single pass for efficiency.
    Stores message references directly (not list positions).
    """

    # (session_id, tool_use_id) -> TemplateMessage for tool_use messages
    tool_use: dict[tuple[str, str], TemplateMessage]
    # (session_id, tool_use_id) -> TemplateMessage for tool_result messages
    tool_result: dict[tuple[str, str], TemplateMessage]
    # uuid -> TemplateMessage for system messages (parent-child pairing)
    uuid: dict[str, TemplateMessage]


def _build_pairing_indices(messages: list[TemplateMessage]) -> PairingIndices:
    """Build indices for efficient message pairing lookups.

    Single pass through messages to build all indices needed for pairing.
    Stores message references directly for robust lookup after reordering.
    """
    tool_use_index: dict[tuple[str, str], TemplateMessage] = {}
    tool_result_index: dict[tuple[str, str], TemplateMessage] = {}
    uuid_index: dict[str, TemplateMessage] = {}

    for msg in messages:
        # Index tool_use and tool_result by (session_id, tool_use_id)
        if msg.tool_use_id and msg.session_id:
            key = (msg.session_id, msg.tool_use_id)
            if msg.type == "tool_use":
                tool_use_index[key] = msg
            elif msg.type == "tool_result":
                tool_result_index[key] = msg

        # Index system messages by UUID for parent-child pairing
        if msg.meta.uuid and msg.type == "system":
            uuid_index[msg.meta.uuid] = msg

    return PairingIndices(
        tool_use=tool_use_index,
        tool_result=tool_result_index,
        uuid=uuid_index,
    )


def _mark_pair(first: TemplateMessage, last: TemplateMessage) -> None:
    """Mark two messages as a pair by setting their pair indices."""
    first_index = first.message_index
    last_index = last.message_index
    if first_index is not None and last_index is not None:
        first.pair_last = last_index
        last.pair_first = first_index


def _try_pair_adjacent(
    current: TemplateMessage,
    next_msg: TemplateMessage,
) -> bool:
    """Try to pair adjacent messages based on their types.

    Returns True if messages were paired, False otherwise.

    Adjacent pairing rules:
    - user slash-command + user command-output
    - bash-input + bash-output
    - thinking + assistant
    """
    # Slash command + command output (both are user messages)
    if isinstance(
        current.content, (SlashCommandMessage, UserSlashCommandMessage)
    ) and isinstance(next_msg.content, CommandOutputMessage):
        _mark_pair(current, next_msg)
        return True

    # Bash input + bash output
    if current.type == "bash-input" and next_msg.type == "bash-output":
        _mark_pair(current, next_msg)
        return True

    # Thinking + assistant
    if current.type == "thinking" and next_msg.type == "assistant":
        _mark_pair(current, next_msg)
        return True

    return False


def _try_pair_by_index(
    current: TemplateMessage,
    indices: PairingIndices,
) -> None:
    """Try to pair current message with another using index lookups.

    Index-based pairing rules (can be any distance apart):
    - tool_use + tool_result (by tool_use_id within same session)
    - system parent + system child (by uuid/parent_uuid)
    """
    # Tool use + tool result (by tool_use_id within same session)
    if current.type == "tool_use" and current.tool_use_id and current.session_id:
        key = (current.session_id, current.tool_use_id)
        if key in indices.tool_result:
            _mark_pair(current, indices.tool_result[key])

    # System child message finding its parent (by parent_uuid)
    if current.type == "system" and current.parent_uuid:
        if current.parent_uuid in indices.uuid:
            _mark_pair(indices.uuid[current.parent_uuid], current)


def _identify_message_pairs(messages: list[TemplateMessage]) -> None:
    """Identify and mark paired messages (e.g., command + output, tool use + result).

    Modifies messages in-place by setting is_paired and pair_role fields.

    Uses a two-pass algorithm:
    1. First pass: Build indices for efficient lookups (tool_use_id, uuid, parent_uuid)
    2. Second pass: Sequential scan for adjacent pairs and index-based pairs

    Pairing types:
    - Adjacent: system+output, bash-input+output, thinking+assistant
    - Indexed: tool_use+result (by ID), system parent+child (by UUID)
    """
    # Pass 1: Build all indices for efficient lookups
    indices = _build_pairing_indices(messages)

    # Pass 2: Sequential scan to identify pairs
    i = 0
    while i < len(messages):
        current = messages[i]

        # Skip session headers
        if current.is_session_header:
            i += 1
            continue

        # Try adjacent pairing first (can skip next message if paired)
        if i + 1 < len(messages):
            next_msg = messages[i + 1]
            if _try_pair_adjacent(current, next_msg):
                i += 2
                continue

        # Try index-based pairing (doesn't skip, continues to next message)
        _try_pair_by_index(current, indices)

        i += 1


def _reorder_paired_messages(messages: list[TemplateMessage]) -> list[TemplateMessage]:
    """Reorder messages so paired messages are adjacent while preserving chronological order.

    - Unpaired messages and first messages in pairs maintain chronological order
    - Last messages in pairs are moved immediately after their first message
    - Timestamps are enhanced to show duration for paired messages

    Uses dictionary-based approach to find pairs efficiently:
    1. Build index of all pair_last messages by tool_use_id
    2. Single pass through messages, inserting pair_last immediately after pair_first
    """
    from datetime import datetime

    # Build index of pair_last messages by (session_id, tool_use_id)
    # Session ID is included to prevent cross-session pairing when sessions are resumed
    # Stores message references directly (not list positions)
    pair_last_index: dict[tuple[str, str], TemplateMessage] = {}

    for msg in messages:
        if msg.is_last_in_pair and msg.tool_use_id and msg.session_id:
            key = (msg.session_id, msg.tool_use_id)
            pair_last_index[key] = msg

    # Create reordered list
    reordered: list[TemplateMessage] = []
    already_added: set[int] = set()  # Track by message_index (unique per message)

    for msg in messages:
        msg_index = msg.message_index
        if msg_index in already_added:
            continue

        reordered.append(msg)
        if msg_index is not None:
            already_added.add(msg_index)

        # If this is the first message in a pair, immediately add its pair_last
        # Key includes session_id to prevent cross-session pairing on resume
        if msg.is_first_in_pair:
            pair_last: Optional[TemplateMessage] = None

            # Check for tool_use_id based pairs
            if msg.tool_use_id and msg.session_id:
                key = (msg.session_id, msg.tool_use_id)
                if key in pair_last_index:
                    pair_last = pair_last_index[key]

            # Only append if we haven't already added this pair_last
            # (handles case where multiple pair_firsts match the same pair_last)
            if pair_last is not None:
                last_msg_index = pair_last.message_index
                if last_msg_index is not None and last_msg_index not in already_added:
                    reordered.append(pair_last)
                    already_added.add(last_msg_index)

                # Calculate duration between pair messages
                try:
                    first_ts = msg.meta.timestamp if msg.meta else None
                    last_ts = pair_last.meta.timestamp if pair_last.meta else None
                    if first_ts and last_ts:
                        # Parse ISO timestamps
                        first_time = datetime.fromisoformat(
                            first_ts.replace("Z", "+00:00")
                        )
                        last_time = datetime.fromisoformat(
                            last_ts.replace("Z", "+00:00")
                        )
                        duration = last_time - first_time

                        # Format duration nicely
                        total_seconds = duration.total_seconds()
                        if total_seconds < 1:
                            duration_str = f"took {int(total_seconds * 1000)} ms"
                        elif total_seconds < 60:
                            duration_str = f"took {total_seconds:.1f}s"
                        else:
                            minutes = int(total_seconds // 60)
                            seconds = int(total_seconds % 60)
                            duration_str = f"took {minutes}m {seconds}s"

                        # Store duration in pair_last for template rendering
                        pair_last.pair_duration = duration_str
                except (ValueError, AttributeError):
                    pass

    return reordered


# -- Message Hierarchy --------------------------------------------------------


def _get_message_hierarchy_level(msg: TemplateMessage) -> int:
    """Determine the hierarchy level for a message based on its type and modifiers.

    Correct hierarchy based on logical nesting:
    - Level 0: Session headers
    - Level 1: User messages
    - Level 2: System commands/errors, Assistant, Thinking
    - Level 3: Tool use/result, System info/warning (nested under assistant)
    - Level 4: Sidechain user/assistant/thinking (nested under Task tool result)
    - Level 5: Sidechain tools (nested under sidechain assistant)

    Note: Sidechain user messages (duplicate of Task input prompt) and the last
    sidechain assistant (duplicate of Task output) are cleaned up from the tree
    by _cleanup_sidechain_duplicates after tree building.

    Returns:
        Integer hierarchy level (1-5, session headers are 0)
    """
    msg_type = msg.type
    is_sidechain = msg.is_sidechain

    # User messages at level 1 (under session), level 4 for sidechain
    if msg_type == "user":
        return 4 if is_sidechain else 1

    # System info/warning at level 3 (tool-related, e.g., hook notifications)
    # Get level from SystemMessage if available
    system_level = msg.content.level if isinstance(msg.content, SystemMessage) else None
    if (
        msg_type == "system"
        and system_level in ("info", "warning")
        and not is_sidechain
    ):
        return 3

    # System commands/errors at level 2 (siblings to assistant)
    if msg_type == "system" and not is_sidechain:
        return 2

    # Sidechain assistant/thinking at level 4 (nested under Task tool result)
    if is_sidechain and msg_type in ("assistant", "thinking"):
        return 4

    # Sidechain tools at level 5
    if is_sidechain and msg_type in ("tool_use", "tool_result"):
        return 5

    # Main assistant/thinking at level 2 (nested under user)
    if msg_type in ("assistant", "thinking"):
        return 2

    # Main tools at level 3 (nested under assistant)
    if msg_type in ("tool_use", "tool_result"):
        return 3

    # Default to level 1
    return 1


def _build_message_hierarchy(messages: list[TemplateMessage]) -> None:
    """Build ancestry for all messages based on their current order.

    This should be called after all reordering operations (pair reordering, sidechain
    reordering) to ensure the hierarchy reflects the final display order.

    The hierarchy is determined by message type using _get_message_hierarchy_level(),
    and a stack-based approach builds proper parent-child relationships.

    Ancestry stores message_index integers. Templates prefix with "d-" for CSS classes.

    Args:
        messages: List of template messages in their final order (modified in place)
    """
    # Stack of (level, message_index) tuples
    hierarchy_stack: list[tuple[int, int]] = []

    for message in messages:
        # Session headers are level 0
        if message.is_session_header:
            current_level = 0
        else:
            # Determine level from message type and modifiers
            current_level = _get_message_hierarchy_level(message)

        # Pop stack until we find the appropriate parent level
        while hierarchy_stack and hierarchy_stack[-1][0] >= current_level:
            hierarchy_stack.pop()

        # Build ancestry from remaining stack (list of message_index integers)
        ancestry = [msg_index for _, msg_index in hierarchy_stack]

        # Push current message onto stack
        if message.message_index is not None:
            hierarchy_stack.append((current_level, message.message_index))

        # Update the message ancestry
        message.ancestry = ancestry


def _mark_messages_with_children(messages: list[TemplateMessage]) -> None:
    """Calculate child and descendant counts for messages.

    Efficiently calculates:
    - immediate_children_count: Count of direct children only
    - total_descendants_count: Count of all descendants recursively

    Time complexity: O(n) where n is the number of messages.

    Args:
        messages: List of template messages to process
    """
    # Build index of messages by message_index for O(1) lookup
    message_by_index: dict[int, TemplateMessage] = {}
    for message in messages:
        if message.message_index is not None:
            message_by_index[message.message_index] = message

    # Process each message and update counts for ancestors
    for message in messages:
        if not message.ancestry:
            continue  # Top-level message, no parents

        # Skip counting pair_last messages (second in a pair)
        # Pairs are visually presented as a single unit, so we only count the first
        if message.is_last_in_pair:
            continue

        # Get immediate parent (last in ancestry list)
        immediate_parent_index = message.ancestry[-1]

        # Get message type for categorization
        msg_type = message.type

        # Increment immediate parent's child count
        if immediate_parent_index in message_by_index:
            parent = message_by_index[immediate_parent_index]
            parent.immediate_children_count += 1
            # Track by type
            parent.immediate_children_by_type[msg_type] = (
                parent.immediate_children_by_type.get(msg_type, 0) + 1
            )

        # Increment descendant count for ALL ancestors
        for ancestor_index in message.ancestry:
            if ancestor_index in message_by_index:
                ancestor = message_by_index[ancestor_index]
                ancestor.total_descendants_count += 1
                # Track by type
                ancestor.total_descendants_by_type[msg_type] = (
                    ancestor.total_descendants_by_type.get(msg_type, 0) + 1
                )


def _build_message_tree(messages: list[TemplateMessage]) -> list[TemplateMessage]:
    """Build tree structure by populating children fields based on ancestry.

    This function takes a flat list of messages (with message_index and ancestry
    already set by _build_message_hierarchy) and populates the children field
    of each message to form an explicit tree structure.

    The tree structure enables:
    - Recursive template rendering with nested DOM elements
    - Simpler JavaScript fold/unfold (just hide/show children container)
    - More natural parent-child traversal

    Args:
        messages: List of template messages with message_index and ancestry set

    Returns:
        List of root messages (those with empty ancestry). Each message's
        children field is populated with its direct children.
    """
    # Build index of messages by message_index for O(1) lookup
    message_by_index: dict[int, TemplateMessage] = {}
    for message in messages:
        if message.message_index is not None:
            message_by_index[message.message_index] = message

    # Clear any existing children (in case of re-processing)
    for message in messages:
        message.children = []

    # Collect root messages (those with no ancestry)
    root_messages: list[TemplateMessage] = []

    # Populate children based on ancestry
    for message in messages:
        if not message.ancestry:
            # Root message (level 0, no parent)
            root_messages.append(message)
        else:
            # Has a parent - add to parent's children
            immediate_parent_index = message.ancestry[-1]
            if immediate_parent_index in message_by_index:
                parent = message_by_index[immediate_parent_index]
                parent.children.append(message)

    return root_messages


# Pattern to match agentId lines added to Task results for resume functionality
# e.g., "agentId: a7c9965 (for resuming to continue this agent's work if needed)"
_AGENT_ID_LINE_PATTERN = re.compile(r"\n*agentId:\s*\w+\s*\([^)]*\)\s*$", re.IGNORECASE)


def _normalize_for_dedup(text: str) -> str:
    """Normalize text for deduplication matching.

    Strips trailing agentId lines that may be added to Task results
    but not present in the sidechain assistant's final message.
    """
    return _AGENT_ID_LINE_PATTERN.sub("", text).strip()


def _cleanup_sidechain_duplicates(root_messages: list[TemplateMessage]) -> None:
    """Clean up duplicate content in sidechains after tree is built.

    For each Task tool_use or tool_result with sidechain children:
    - Remove the first UserTextMessage (duplicate of Task input prompt)
    - For tool_result: Remove last AssistantTextMessage if it matches the result

    Sidechain messages can be children of either tool_use or tool_result depending
    on timestamp order - tool_use during execution, tool_result after completion.

    Args:
        root_messages: List of root messages with children populated
    """

    def process_message(message: TemplateMessage) -> None:
        """Recursively process a message and its children."""
        # Recursively process children first (depth-first)
        for child in message.children:
            process_message(child)

        # Check if this is a Task tool_use or tool_result with sidechain children
        is_task_tool_use = (
            message.type == "tool_use"
            and isinstance(message.content, ToolUseMessage)
            and message.content.tool_name == "Task"
        )
        is_task_tool_result = (
            message.type == "tool_result"
            and isinstance(message.content, ToolResultMessage)
            and message.content.tool_name == "Task"
        )

        if not ((is_task_tool_use or is_task_tool_result) and message.children):
            return

        children = message.children

        # Remove first sidechain UserTextMessage (duplicate of Task input prompt)
        # Must be specifically UserTextMessage, not ToolResultMessage or other user types
        # When removing, adopt its children to preserve sidechain tool messages
        if (
            children
            and children[0].is_sidechain
            and isinstance(children[0].content, UserTextMessage)
        ):
            removed = children.pop(0)
            # Adopt orphaned children (tool_use/tool_result from sidechain)
            if removed.children:
                # Insert at beginning to maintain order
                children[:0] = removed.children

        # For tool_result only: replace last matching AssistantTextMessage with dedup
        if not is_task_tool_result:
            return

        # Extract task result text from parsed TaskOutput
        tool_result_msg = cast(ToolResultMessage, message.content)
        if not isinstance(task_output := tool_result_msg.output, TaskOutput):
            return
        if not (result := task_output.result):
            return
        if not (task_result_text := _normalize_for_dedup(result.strip())):
            return

        for i in range(len(children) - 1, -1, -1):
            child = children[i]
            child_content = child.content
            if (
                child.type == "assistant"
                and child.is_sidechain
                and isinstance(child_content, AssistantTextMessage)
            ):
                # Extract text on-demand for dedup check (only for sidechain assistant)
                child_raw = "\n".join(
                    item.text
                    for item in child_content.items
                    if isinstance(item, TextContent)
                )
                child_text = _normalize_for_dedup(child_raw) if child_raw else None
            else:
                child_text = None
            if child_text and child_text == task_result_text:
                # Drop duplicate sidechain assistant message
                del children[i]
                break

    for root in root_messages:
        process_message(root)


# -- Message Reordering -------------------------------------------------------


def _reorder_session_template_messages(
    messages: list[TemplateMessage],
) -> list[TemplateMessage]:
    """Reorder template messages to group all messages under their correct session headers.

    When a user resumes session A into session B, Claude Code copies messages from
    session A into session B's JSONL file (keeping their original sessionId). After
    global chronological sorting, these copied messages get interleaved. This function
    fixes that by grouping all messages by session_id and inserting them after their
    corresponding session header.

    This must be called BEFORE _identify_message_pairs and _reorder_paired_messages,
    since those functions expect messages to be in session-grouped order.

    Args:
        messages: Template messages (including session headers)

    Returns:
        Reordered messages with all messages grouped under their session headers
    """
    # First pass: extract session headers and group non-header messages by session_id
    session_headers: list[TemplateMessage] = []
    session_messages_map: dict[str, list[TemplateMessage]] = {}

    for message in messages:
        if message.is_session_header:
            session_headers.append(message)
            # Initialize the list for this session (preserves session order)
            if message.session_id and message.session_id not in session_messages_map:
                session_messages_map[message.session_id] = []
        else:
            session_id = message.session_id
            if session_id:
                if session_id not in session_messages_map:
                    session_messages_map[session_id] = []
                session_messages_map[session_id].append(message)

    # If no session headers, return original order
    if not session_headers:
        return messages

    # Second pass: for each session header, insert all messages with that session_id
    result: list[TemplateMessage] = []
    used_sessions: set[str] = set()

    for header in session_headers:
        result.append(header)
        session_id = header.session_id

        if session_id and session_id in session_messages_map:
            # Messages are already in timestamp order from original processing
            result.extend(session_messages_map[session_id])
            used_sessions.add(session_id)

    # Append any messages that weren't matched to a session header (shouldn't happen normally)
    for session_id, msgs in session_messages_map.items():
        if session_id not in used_sessions:
            result.extend(msgs)

    return result


def _reorder_sidechain_template_messages(
    messages: list[TemplateMessage],
) -> list[TemplateMessage]:
    """Reorder template messages to place sidechains immediately after their Task results.

    When parallel Task agents run, their sidechain messages may appear in arbitrary
    order based on when each agent finishes. This function reorders messages so that
    each sidechain's messages appear right after the Task result that references them.

    Note: Deduplication of sidechain content (first user message = Task input,
    last assistant message = Task output) is handled later by _cleanup_sidechain_duplicates
    after the tree structure is built.

    This must be called AFTER _reorder_paired_messages, since that function moves
    tool_results next to their tool_uses, which changes where the agentId-bearing
    messages end up.

    Args:
        messages: Template messages including sidechains

    Returns:
        Reordered messages with sidechains properly placed after their Task results
    """
    # First pass: extract sidechains grouped by agent_id
    main_messages: list[TemplateMessage] = []
    sidechain_map: dict[str, list[TemplateMessage]] = {}

    for message in messages:
        is_sidechain = message.is_sidechain
        agent_id = message.agent_id

        if is_sidechain and agent_id:
            # Group sidechain messages by agent_id
            if agent_id not in sidechain_map:
                sidechain_map[agent_id] = []
            sidechain_map[agent_id].append(message)
        else:
            main_messages.append(message)

    # If no sidechains, return original order
    if not sidechain_map:
        return messages

    # Second pass: insert sidechains after their Task result messages
    result: list[TemplateMessage] = []
    used_agents: set[str] = set()

    for message in main_messages:
        result.append(message)

        # Check if this is a Task tool_result that references a sidechain (via agent_id)
        # We only insert after tool_result (not tool_use) to avoid duplicates if
        # tool_use ever gets agent_id in the future
        agent_id = message.agent_id

        # Only insert sidechain if not already inserted (handles case where
        # multiple tool_results have the same agent_id)
        if (
            agent_id
            and message.type == MessageType.TOOL_RESULT
            and agent_id in sidechain_map
            and agent_id not in used_agents
        ):
            # Insert the sidechain messages for this agent right after this message
            # Note: ancestry will be rebuilt by _build_message_hierarchy() later
            result.extend(sidechain_map[agent_id])
            used_agents.add(agent_id)

    # Append any sidechains that weren't matched (shouldn't happen normally)
    for agent_id, sidechain_msgs in sidechain_map.items():
        if agent_id not in used_agents:
            result.extend(sidechain_msgs)

    return result


def _filter_messages(messages: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Filter messages to those that should be rendered.

    This function filters out:
    - Summary messages (already attached to sessions)
    - Queue operations except 'remove' (steering messages)
    - Messages with no meaningful content (no text and no tool items)
    - Messages matching should_skip_message() (warmup, etc.)

    System messages are included as they need special processing in _render_messages.

    Note: Sidechain user prompts (duplicates of Task input) are removed later
    by _cleanup_sidechain_duplicates after tree building.

    Args:
        messages: List of transcript entries to filter

    Returns:
        Filtered list of messages that should be rendered
    """
    filtered: list[TranscriptEntry] = []

    for message in messages:
        # Skip summary messages
        if isinstance(message, SummaryTranscriptEntry):
            continue

        # Skip most queue operations - only process 'remove' for counts
        if isinstance(message, QueueOperationTranscriptEntry):
            if message.operation != "remove":
                continue

        # System messages bypass other checks but are included
        if isinstance(message, SystemTranscriptEntry):
            filtered.append(message)
            continue

        # Get message content for filtering checks
        message_content: list[ContentItem]
        if isinstance(message, QueueOperationTranscriptEntry):
            content = message.content
            message_content = content if isinstance(content, list) else []
        else:
            message_content = message.message.content

        text_content = extract_text_content(message_content)

        # Skip if no meaningful content
        if not text_content.strip():
            # Check for tool items
            has_tool_items = any(
                isinstance(item, (ToolUseContent, ToolResultContent, ThinkingContent))
                or getattr(item, "type", None)
                in ("tool_use", "tool_result", "thinking")
                for item in message_content
            )
            if not has_tool_items:
                continue

        # Skip messages that should be filtered out
        if should_skip_message(text_content):
            continue

        # Message passes all filters
        filtered.append(message)

    return filtered


def _collect_session_info(
    messages: list[TranscriptEntry],
    session_summaries: dict[str, str],
) -> tuple[
    dict[str, dict[str, Any]],  # sessions
    list[str],  # session_order
    set[str],  # show_tokens_for_message
]:
    """Collect session metadata and token tracking from pre-filtered messages.

    This function iterates through messages to:
    - Build session metadata (timestamps, message counts, first user message)
    - Track token usage per session (deduplicating by requestId)
    - Determine which messages should display token usage

    Note: Messages should be pre-filtered by _filter_messages. System messages
    in the input are skipped for session tracking purposes.

    Args:
        messages: Pre-filtered list of transcript entries
        session_summaries: Dict mapping session_id to summary text

    Returns:
        Tuple containing:
        - sessions: Session metadata dict mapping session_id to info
        - session_order: List of session IDs in chronological order
        - show_tokens_for_message: Set of message UUIDs that should display tokens
    """
    sessions: dict[str, dict[str, Any]] = {}
    session_order: list[str] = []

    # Track requestIds to avoid double-counting token usage
    seen_request_ids: set[str] = set()
    # Track which messages should show token usage (first occurrence of each requestId)
    show_tokens_for_message: set[str] = set()

    for message in messages:
        # Skip system messages for session tracking
        if isinstance(message, SystemTranscriptEntry):
            continue

        # Get message content
        if isinstance(message, QueueOperationTranscriptEntry):
            message_content = message.content if message.content else []
        else:
            message_content = message.message.content  # type: ignore

        text_content = extract_text_content(message_content)  # type: ignore[arg-type]

        # Get session info
        session_id = getattr(message, "sessionId", "unknown")

        # Initialize session if new
        if session_id not in sessions:
            current_session_summary = session_summaries.get(session_id)

            # Get first user message content for preview
            first_user_message = ""
            if as_user_entry(message) and should_use_as_session_starter(text_content):
                first_user_message = create_session_preview(text_content)

            sessions[session_id] = {
                "id": session_id,
                "summary": current_session_summary,
                "first_timestamp": getattr(message, "timestamp", ""),
                "last_timestamp": getattr(message, "timestamp", ""),
                "message_count": 0,
                "first_user_message": first_user_message,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cache_read_tokens": 0,
            }
            session_order.append(session_id)

        # Update first user message if this is a user message and we don't have one yet
        elif as_user_entry(message) and not sessions[session_id]["first_user_message"]:
            if should_use_as_session_starter(text_content):
                sessions[session_id]["first_user_message"] = create_session_preview(
                    text_content
                )

        sessions[session_id]["message_count"] += 1

        # Update last timestamp for this session
        current_timestamp = getattr(message, "timestamp", "")
        if current_timestamp:
            sessions[session_id]["last_timestamp"] = current_timestamp

        # Extract and accumulate token usage for assistant messages
        # Only count tokens for the first message with each requestId to avoid duplicates
        if assistant_entry := as_assistant_entry(message):
            assistant_message = assistant_entry.message
            request_id = assistant_entry.requestId
            message_uuid = assistant_entry.uuid

            if (
                assistant_message.usage
                and request_id
                and request_id not in seen_request_ids
            ):
                # Mark this requestId as seen to avoid double-counting
                seen_request_ids.add(request_id)
                # Mark this specific message UUID as one that should show token usage
                show_tokens_for_message.add(message_uuid)

                usage = assistant_message.usage
                sessions[session_id]["total_input_tokens"] += usage.input_tokens
                sessions[session_id]["total_output_tokens"] += usage.output_tokens
                if usage.cache_creation_input_tokens:
                    sessions[session_id]["total_cache_creation_tokens"] += (
                        usage.cache_creation_input_tokens
                    )
                if usage.cache_read_input_tokens:
                    sessions[session_id]["total_cache_read_tokens"] += (
                        usage.cache_read_input_tokens
                    )

    return sessions, session_order, show_tokens_for_message


def _render_messages(
    messages: list[TranscriptEntry],
    sessions: dict[str, dict[str, Any]],
    show_tokens_for_message: set[str],
) -> RenderingContext:
    """Pass 2: Render pre-filtered messages to TemplateMessage objects.

    This pass creates the actual TemplateMessage objects for rendering:
    - Creates session headers when entering new sessions
    - Processes text content into HTML
    - Handles tool use, tool result, thinking, and image content
    - Collects timing statistics

    Note: Messages are pre-filtered by _collect_session_info, so no additional
    filtering is needed here except for system message processing.

    Args:
        messages: Pre-filtered list of transcript entries from _collect_session_info
        sessions: Session metadata from _collect_session_info
        show_tokens_for_message: Set of message UUIDs that should display tokens

    Returns:
        RenderingContext with all TemplateMessage objects registered
    """
    # Create rendering context for this operation
    ctx = RenderingContext()

    # Track which sessions have had headers added
    seen_sessions: set[str] = set()

    for message in messages:
        message_type = message.type

        # Handle system messages (already filtered in pass 1)
        if isinstance(message, SystemTranscriptEntry):
            system_content = create_system_message(message)
            if system_content:
                system_msg = TemplateMessage(system_content)
                ctx.register(system_msg)
            continue

        # Skip summary messages (should be filtered in pass 1, but be defensive)
        if isinstance(message, SummaryTranscriptEntry):
            continue

        # Handle queue-operation 'remove' messages as user messages
        if isinstance(message, QueueOperationTranscriptEntry):
            message_content = message.content if message.content else []
            message_type = MessageType.QUEUE_OPERATION
            # QueueOperationTranscriptEntry has limited fields (no uuid, agentId, etc.)
            meta = MessageMeta(
                session_id=message.sessionId,
                timestamp=message.timestamp,
                uuid="",
            )
            effective_type = "user"
        else:
            message_content = message.message.content
            meta = create_meta(message)
            effective_type = message_type

        # Chunk content: regular items (text/image) accumulate, special items (tool/thinking) separate
        if isinstance(message_content, list):
            chunks = chunk_message_content(message_content)
        else:
            # String content - wrap in list with single TextContent
            content_str: str = message_content.strip() if message_content else ""
            if content_str:
                chunks: list[ContentChunk] = [
                    [TextContent(type="text", text=content_str)]  # pyright: ignore[reportUnknownArgumentType]
                ]
            else:
                chunks = []

        # Skip messages with no content
        if not chunks:
            continue

        # Get session info
        session_id = meta.session_id or "unknown"
        session_summary = sessions.get(session_id, {}).get("summary")

        # Add session header if this is a new session
        if session_id not in seen_sessions:
            seen_sessions.add(session_id)
            current_session_summary = session_summary
            session_title = (
                f"{current_session_summary} • {session_id[:8]}"
                if current_session_summary
                else session_id[:8]
            )

            # Create meta with session_id for the session header
            session_header_meta = MessageMeta(
                session_id=session_id,
                timestamp="",
                uuid="",
            )
            session_header_content = SessionHeaderMessage(
                session_header_meta,
                title=session_title,
                session_id=session_id,
                summary=current_session_summary,
            )
            # Register and track session's first message
            session_header = TemplateMessage(session_header_content)
            msg_index = ctx.register(session_header)
            ctx.session_first_message[session_id] = msg_index

        # Extract token usage for assistant messages
        # Only show token usage for the first message with each requestId to avoid duplicates
        usage_to_show: Optional[UsageInfo] = None
        if assistant_entry := as_assistant_entry(message):
            assistant_message = assistant_entry.message
            message_uuid = assistant_entry.uuid
            if assistant_message.usage and message_uuid in show_tokens_for_message:
                usage_to_show = assistant_message.usage

        # Track whether we've used the usage (only use on first content chunk)
        usage_used = False

        # Process each chunk - regular chunks (list) become text/image messages,
        # special chunks (single item) become tool/thinking messages
        for chunk in chunks:
            # Each chunk needs its own meta copy to preserve original values
            chunk_meta = replace(meta)

            # Regular chunk: list of text/image items
            if isinstance(chunk, list):
                # Extract text for pattern detection
                chunk_text = extract_text_content(chunk)

                # Dispatch to user or assistant parser based on effective_type
                content_model: Optional[MessageContent] = None
                # (user message parsing handles all type detection internally)
                if effective_type == "user":
                    content_model = create_user_message(
                        chunk_meta,
                        chunk,  # Pass the chunk items
                        chunk_text,  # Pre-extracted text for pattern detection
                        is_slash_command=chunk_meta.is_meta,
                    )
                elif effective_type == "assistant":
                    # Pass usage only on first chunk
                    chunk_usage = usage_to_show if not usage_used else None
                    usage_used = True
                    content_model = create_assistant_message(
                        chunk_meta, chunk, chunk_usage
                    )

                # Convert to UserSteeringMessage for queue-operation 'remove' messages
                if (
                    isinstance(message, QueueOperationTranscriptEntry)
                    and message.operation == "remove"
                    and isinstance(content_model, UserTextMessage)
                ):
                    content_model = UserSteeringMessage(
                        items=content_model.items, meta=chunk_meta
                    )

                # Skip empty chunks or when no content model was created
                if not chunk or content_model is None:
                    continue

                chunk_msg = TemplateMessage(content_model)
                ctx.register(chunk_msg)

            else:
                # Special chunk: single tool_use/tool_result/thinking item
                tool_item = chunk

                # Dispatch to appropriate handler based on item type
                tool_result: ToolItemResult
                if isinstance(tool_item, ToolUseContent):
                    tool_result = create_tool_use_message(
                        chunk_meta, tool_item, ctx.tool_use_context
                    )
                elif isinstance(tool_item, ToolResultContent):
                    # Extract toolUseResult from user entries for structured parsing
                    entry_tool_use_result = None
                    if isinstance(message, UserTranscriptEntry):
                        entry_tool_use_result = message.toolUseResult
                    tool_result = create_tool_result_message(
                        chunk_meta,
                        tool_item,
                        ctx.tool_use_context,
                        entry_tool_use_result,
                    )
                elif isinstance(tool_item, ThinkingContent):
                    # Pass usage only if not yet used
                    chunk_usage = usage_to_show if not usage_used else None
                    usage_used = True
                    content = create_thinking_message(
                        chunk_meta, tool_item, chunk_usage
                    )
                    tool_result = ToolItemResult(
                        message_type=content.message_type,
                        content=content,
                    )
                else:
                    # Handle unknown content types
                    tool_result = ToolItemResult(
                        message_type="unknown",
                        content=UnknownMessage(
                            chunk_meta, type_name=str(type(tool_item))
                        ),
                    )

                # Skip if no content (shouldn't happen, but be safe)
                if tool_result.content is None:
                    continue

                tool_msg = TemplateMessage(tool_result.content)
                ctx.register(tool_msg)

    return ctx


# -- Project Index Generation -------------------------------------------------


def prepare_projects_index(
    project_summaries: list[dict[str, Any]],
) -> tuple[list["TemplateProject"], "TemplateSummary"]:
    """Prepare project data for rendering in any format.

    Args:
        project_summaries: List of project summary dictionaries.

    Returns:
        A tuple of (template_projects, template_summary) for use by renderers.
    """
    # Sort projects by last modified (most recent first)
    sorted_projects = sorted(
        project_summaries, key=lambda p: p["last_modified"], reverse=True
    )

    # Convert to template-friendly format
    template_projects = [TemplateProject(project) for project in sorted_projects]
    template_summary = TemplateSummary(project_summaries)

    return template_projects, template_summary


def title_for_projects_index(
    project_summaries: list[dict[str, Any]],
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """Generate a title for the projects index page.

    Determines a meaningful title based on working directories from projects,
    with optional date range suffix.

    Args:
        project_summaries: List of project summary dictionaries.
        from_date: Optional start date filter string.
        to_date: Optional end date filter string.

    Returns:
        A title string for the projects index page.
    """
    title = "Claude Code Projects"

    if project_summaries:
        # Collect all working directories from all projects
        all_working_dirs: set[str] = set()
        for project in project_summaries:
            working_dirs = project.get("working_directories", [])
            if working_dirs:
                all_working_dirs.update(working_dirs)

        # Use the common parent directory if available
        if all_working_dirs:
            # Find the most common parent directory
            from pathlib import Path

            working_paths = [Path(wd) for wd in all_working_dirs]

            if len(working_paths) == 1:
                # Single working directory - use its name
                title = f"Claude Code Projects - {working_paths[0].name}"
            else:
                # Multiple working directories - try to find common parent
                try:
                    # Find common parent
                    common_parts: list[str] = []
                    if working_paths:
                        # Get parts of first path
                        first_parts = working_paths[0].parts
                        for i, part in enumerate(first_parts):
                            # Check if this part exists in all paths
                            if all(
                                len(p.parts) > i and p.parts[i] == part
                                for p in working_paths
                            ):
                                common_parts.append(part)
                            else:
                                break

                        if len(common_parts) > 1:  # More than just root "/"
                            common_path = Path(*common_parts)
                            title = f"Claude Code Projects - {common_path.name}"
                except Exception:
                    # Fall back to default title if path analysis fails
                    pass

    # Add date range suffix if provided
    if from_date or to_date:
        date_range_parts: list[str] = []
        if from_date:
            date_range_parts.append(f"from {from_date}")
        if to_date:
            date_range_parts.append(f"to {to_date}")
        date_range_str = " ".join(date_range_parts)
        title += f" ({date_range_str})"

    return title


# -- Renderer Classes ---------------------------------------------------------


class Renderer:
    """Base class for transcript renderers.

    Subclasses implement format-specific rendering (HTML, Markdown, etc.).

    The method-based dispatcher pattern:
    - Base class defines format_xyz_message() methods for each content type
    - Each method documents its fallback chain (which method it delegates to)
    - format_content() walks the MRO to find the most specific method
    - Subclasses override methods to implement format-specific rendering
    """

    def _dispatch_format(self, obj: Any, message: TemplateMessage) -> str:
        """Dispatch to format_{ClassName}(obj, message) based on object type."""
        for cls in type(obj).__mro__:
            if cls is object:
                break
            if method := getattr(self, f"format_{cls.__name__}", None):
                return method(obj, message)
        return ""

    def _dispatch_title(self, obj: Any, message: TemplateMessage) -> Optional[str]:
        """Dispatch to title_{ClassName}(obj, message) based on object type."""
        for cls in type(obj).__mro__:
            if cls is object:
                break
            if method := getattr(self, f"title_{cls.__name__}", None):
                return method(obj, message)
        return None

    def format_content(self, message: TemplateMessage) -> str:
        """Format message content by dispatching to type-specific method.

        Looks for a method named format_{ClassName} (e.g., format_SystemMessage).
        Walks the content type's MRO to find the most specific format method.

        Args:
            message: TemplateMessage with content to format.

        Returns:
            Formatted string (e.g., HTML), or empty string if no handler found.
        """
        return self._dispatch_format(message.content, message)

    def title_content(self, message: TemplateMessage) -> str:
        """Get message title by dispatching to type-specific title method.

        Looks for a method named title_{ClassName} (e.g., title_ToolUseMessage).
        Falls back to type-based title derived from message_type.

        Args:
            message: TemplateMessage to get title for.

        Returns:
            Title string for the message header.
        """
        # Try title_{ClassName} dispatch
        for cls in type(message.content).__mro__:
            if cls is object:
                break
            if method := getattr(self, f"title_{cls.__name__}", None):
                return method(message.content, message)
        # Fallback: convert message_type to title case
        return message.content.message_type.replace("_", " ").replace("-", " ").title()

    # -------------------------------------------------------------------------
    # Title Methods (return title strings for message headers)
    # -------------------------------------------------------------------------
    # These methods return title strings for specific content types.
    # Override in subclasses for format-specific titles (e.g., HTML with icons).

    def title_SystemMessage(self, content: SystemMessage, _: TemplateMessage) -> str:
        level = content.level or "unknown"
        return f"System {level.title()}"

    def title_HookSummaryMessage(
        self, _content: HookSummaryMessage, _: TemplateMessage
    ) -> str:
        return "System Hook"

    def title_SlashCommandMessage(
        self, content: SlashCommandMessage, _message: TemplateMessage
    ) -> str:
        return "Slash Command"

    def title_CommandOutputMessage(
        self, _content: CommandOutputMessage, _: TemplateMessage
    ) -> str:
        return ""  # Empty title for command output

    def title_BashInputMessage(
        self, _content: BashInputMessage, _: TemplateMessage
    ) -> str:
        return "Bash command"

    def title_BashOutputMessage(
        self, _content: BashOutputMessage, _: TemplateMessage
    ) -> str:
        return ""  # Empty title for bash output

    def title_CompactedSummaryMessage(
        self, _content: CompactedSummaryMessage, _: TemplateMessage
    ) -> str:
        return "User (compacted conversation)"

    def title_UserMemoryMessage(
        self, _content: UserMemoryMessage, _: TemplateMessage
    ) -> str:
        return "Memory"

    def title_UserSlashCommandMessage(
        self, _content: UserSlashCommandMessage, _: TemplateMessage
    ) -> str:
        return "User (slash command)"

    def title_UserTextMessage(
        self, _content: UserTextMessage, _message: TemplateMessage
    ) -> str:
        return "User"

    def title_UserSteeringMessage(
        self, _content: UserSteeringMessage, _: TemplateMessage
    ) -> str:
        return "User (steering)"

    def title_AssistantTextMessage(
        self, _content: AssistantTextMessage, message: TemplateMessage
    ) -> str:
        # Sidechain assistant messages get special title
        if message.meta.is_sidechain:
            return "Sub-assistant"
        return "Assistant"

    def title_ThinkingMessage(
        self, _content: ThinkingMessage, _message: TemplateMessage
    ) -> str:
        return "Thinking"

    def title_UnknownMessage(self, _content: UnknownMessage, _: TemplateMessage) -> str:
        return "Unknown Content"

    # Tool title methods (dispatch to input/output title methods)
    def title_ToolUseMessage(
        self, content: ToolUseMessage, message: TemplateMessage
    ) -> str:
        if title := self._dispatch_title(content.input, message):
            return title
        return content.tool_name  # Default to tool name

    def title_ToolResultMessage(
        self, content: ToolResultMessage, message: TemplateMessage
    ) -> str:
        if content.is_error:
            return "Error"
        if title := self._dispatch_title(content.output, message):
            return title
        return ""  # Tool results typically don't need a title

    # Tool input title stubs (override in subclasses for custom titles)
    # def title_BashInput(self, input: "BashInput", message: "TemplateMessage") -> str: ...
    # def title_ReadInput(self, input: "ReadInput", message: "TemplateMessage") -> str: ...
    # def title_EditInput(self, input: "EditInput", message: "TemplateMessage") -> str: ...
    # def title_TaskInput(self, input: "TaskInput", message: "TemplateMessage") -> str: ...
    # def title_TodoWriteInput(self, input: "TodoWriteInput", message: "TemplateMessage") -> str: ...

    # -------------------------------------------------------------------------
    # Format Method Stubs (override in subclasses)
    # -------------------------------------------------------------------------
    # System content formatters
    # def format_SystemMessage(self, content: "SystemMessage", message: "TemplateMessage") -> str: ...
    # def format_HookSummaryMessage(self, content: "HookSummaryMessage", _: "TemplateMessage") -> str: ...
    # def format_SessionHeaderMessage(self, content: "SessionHeaderMessage", _: "TemplateMessage") -> str: ...

    # User content formatters
    # def format_UserTextMessage(self, content: "UserTextMessage", _: "TemplateMessage") -> str: ...
    # ...

    # Assistant content formatters
    # def format_AssistantTextMessage(self, content: "AssistantTextMessage", _: "TemplateMessage") -> str: ...
    # def format_ThinkingMessage(self, content: "ThinkingMessage", _: "TemplateMessage") -> str: ...
    # def format_UnknownMessage(self, content: "UnknownMessage", _: "TemplateMessage") -> str: ...

    # Tool content formatters (dispatch to input/output formatters)
    def format_ToolUseMessage(
        self, content: ToolUseMessage, message: TemplateMessage
    ) -> str:
        """Dispatch to format_{InputClass} based on content.input type."""
        return self._dispatch_format(content.input, message)

    def format_ToolResultMessage(
        self, content: ToolResultMessage, message: TemplateMessage
    ) -> str:
        """Dispatch to format_{OutputClass} based on content.output type."""
        return self._dispatch_format(content.output, message)

    # Tool input formatters
    # def format_BashInput(self, input: "BashInput", _: "TemplateMessage") -> str: ...
    # def format_ReadInput(self, input: "ReadInput") -> str: ...
    # def format_WriteInput(self, input: "WriteInput") -> str: ...
    # def format_EditInput(self, input: "EditInput") -> str: ...
    # def format_MultiEditInput(self, input: "MultiEditInput") -> str: ...
    # def format_GlobInput(self, input: "GlobInput") -> str: ...
    # def format_GrepInput(self, input: "GrepInput") -> str: ...
    # def format_TaskInput(self, input: "TaskInput") -> str: ...
    # def format_TodoWriteInput(self, input: "TodoWriteInput") -> str: ...
    # def format_AskUserQuestionInput(self, input: "AskUserQuestionInput") -> str: ...
    # def format_ExitPlanModeInput(self, input: "ExitPlanModeInput") -> str: ...
    # def format_ToolUseContent(self, input: "ToolUseContent") -> str: ...  # fallback

    # Tool output formatters
    # def format_ReadOutput(self, output: "ReadOutput") -> str: ...
    # def format_WriteOutput(self, output: "WriteOutput") -> str: ...
    # def format_EditOutput(self, output: "EditOutput") -> str: ...
    # def format_BashOutput(self, output: "BashOutput") -> str: ...
    # def format_TaskOutput(self, output: "TaskOutput") -> str: ...
    # def format_AskUserQuestionOutput(self, output: "AskUserQuestionOutput") -> str: ...
    # def format_ExitPlanModeOutput(self, output: "ExitPlanModeOutput") -> str: ...
    # def format_ToolResultContent(self, output: "ToolResultContent") -> str: ...  # fallback

    # -------------------------------------------------------------------------
    # Rendering Entry Points
    # -------------------------------------------------------------------------

    def generate(
        self,
        messages: list[TranscriptEntry],
        title: Optional[str] = None,
        combined_transcript_link: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Generate output from transcript messages.

        Args:
            messages: List of transcript entries to render.
            title: Optional title for the output.
            combined_transcript_link: Optional link to combined transcript.
            output_dir: Optional output directory for referenced images.

        Returns None by default; subclasses override to return formatted output.
        """
        return None

    def generate_session(
        self,
        messages: list[TranscriptEntry],
        session_id: str,
        title: Optional[str] = None,
        cache_manager: Optional["CacheManager"] = None,
        output_dir: Optional[Path] = None,
    ) -> Optional[str]:
        """Generate output for a single session.

        Args:
            messages: List of transcript entries.
            session_id: Session ID to generate output for.
            title: Optional title for the output.
            cache_manager: Optional cache manager.
            output_dir: Optional output directory for referenced images.

        Returns None by default; subclasses override to return formatted output.
        """
        return None

    def generate_projects_index(
        self,
        project_summaries: list[dict[str, Any]],
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[str]:
        """Generate a projects index page.

        Returns None by default; subclasses override to return formatted output.
        """
        return None

    def is_outdated(self, file_path: Path) -> Optional[bool]:
        """Check if a rendered file is outdated.

        Returns None by default; subclasses override to return True/False.
        """
        return None


def get_renderer(format: str, image_export_mode: Optional[str] = None) -> Renderer:
    """Get a renderer instance for the specified format.

    Args:
        format: The output format ("html", "md", or "markdown").
        image_export_mode: Image export mode ("placeholder", "embedded", "referenced").
            If None, defaults to "embedded" for HTML and "referenced" for Markdown.

    Returns:
        A Renderer instance for the specified format.

    Raises:
        ValueError: If the format is not supported.
    """
    if format == "html":
        from .html.renderer import HtmlRenderer

        # For HTML, default to embedded mode (current behavior)
        mode = image_export_mode or "embedded"
        return HtmlRenderer(image_export_mode=mode)
    elif format in ("md", "markdown"):
        from .markdown.renderer import MarkdownRenderer

        # For Markdown, default to referenced mode
        mode = image_export_mode or "referenced"
        return MarkdownRenderer(image_export_mode=mode)
    raise ValueError(f"Unsupported format: {format}")


def is_html_outdated(html_file_path: Path) -> bool:
    """Check if an HTML file is outdated based on its version comment.

    This is a convenience function that uses the HtmlRenderer's is_outdated method.

    Returns:
        True if the file should be regenerated (missing version, different version, or file doesn't exist).
        False if the file is current.
    """
    from .html.renderer import HtmlRenderer

    renderer = HtmlRenderer()
    return renderer.is_outdated(html_file_path)

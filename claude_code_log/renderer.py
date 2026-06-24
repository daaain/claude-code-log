#!/usr/bin/env python3
"""Render Claude transcript data to HTML format."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any, Optional, Tuple, cast
from datetime import datetime

if TYPE_CHECKING:
    from .cache import CacheManager
    from .dag import SessionTree
    from .workflow import WorkflowRun

from .models import (
    DetailLevel,
    MessageContent,
    MessageMeta,
    MessageType,
    TranscriptEntry,
    AiTitleTranscriptEntry,
    AssistantTranscriptEntry,
    AttachmentTranscriptEntry,
    PassthroughTranscriptEntry,
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
    AwaySummaryMessage,
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    HookAttachmentMessage,
    HookSummaryMessage,
    SessionHeaderMessage,
    SlashCommandMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskOutput,
    ThinkingMessage,
    ToolResultMessage,
    ToolUseMessage,
    WorkflowAgentMessage,
    WorkflowPhaseMessage,
    WorkflowToolInput,
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
from .factories.attachment_factory import create_attachment_message
from .utils import (
    format_timestamp,
    format_timestamp_range,
    get_parent_session_id,
    get_project_display_name,
    is_agent_session,
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
            A slot may be ``None`` — a "ghost" — when a pass drops the message
            from the visible render path while preserving its index slot so
            every stored reference (parent_message_index, session_first_message,
            junction_forward_links, pair_first/last/middle) stays valid.
            Iterators that don't want to see ghosts should use
            ``_visible(...)`` or ``if m is None: continue``.
        tool_use_context: Maps tool_use_id -> ToolUseContent for result rendering.
        session_first_message: Maps session_id -> index of first message in session.
    """

    messages: list[Optional[TemplateMessage]] = field(
        default_factory=lambda: []  # type: list[Optional[TemplateMessage]]
    )
    tool_use_context: dict[str, ToolUseContent] = field(
        default_factory=lambda: {}  # type: dict[str, ToolUseContent]
    )
    session_first_message: dict[str, int] = field(
        default_factory=lambda: {}  # type: dict[str, int]
    )
    junction_targets: dict[str, list[str]] = field(
        default_factory=lambda: {}  # type: dict[str, list[str]]
    )
    # Teammate-color map for per-session fallback when a <teammate-message>
    # block lacks an inline `color=` or a TaskUpdate/SendMessage/TaskList
    # row names a teammate without carrying the color itself.
    #
    # Scoped by session_id because combined_transcripts.html merges
    # multiple sessions: session A's alice=blue must NOT override session
    # B's alice=red. First sighting wins *within* a session.
    #
    # Shape: session_id -> { teammate_id -> palette color name }.
    teammate_colors: dict[str, dict[str, str]] = field(
        default_factory=lambda: {}  # type: dict[str, dict[str, str]]
    )
    # Per-session map of TaskCreate-assigned task_id → subject. Lets the
    # TaskUpdate tool_use title surface the human-readable subject of a
    # task that was created earlier in the same session, since
    # TaskUpdateInput only carries the bare ``taskId``. Populated by
    # ``_populate_task_metadata`` from TaskCreate tool_results (and from
    # TaskList rows as a fallback). Session-scoped for the same reason
    # as ``teammate_colors``.
    task_subjects: dict[str, dict[str, str]] = field(
        default_factory=lambda: {}  # type: dict[str, dict[str, str]]
    )
    # Per-session map of tool_use_id → task_id, populated from TaskCreate
    # tool_results. Used by the TaskCreate tool_use title formatter to
    # display the assigned ``#N`` next to the subject (TaskCreateInput
    # itself doesn't know the id; the backend mints it on creation).
    task_id_for_tool_use: dict[str, dict[str, str]] = field(
        default_factory=lambda: {}  # type: dict[str, dict[str, str]]
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

        Returns ``None`` if the index is out of range OR if the slot
        has been ghosted (``ctx.messages[idx] = None``). Callers
        already check the return value for ``None`` (out-of-range
        was the only failure mode pre-ghosting); the ghost case
        flows through the same check.

        Args:
            message_index: The message_index (index) to look up.

        Returns:
            The TemplateMessage if found and not ghosted, else None.
        """
        if 0 <= message_index < len(self.messages):
            return self.messages[message_index]
        return None


def _visible(
    messages: Iterable[Optional["TemplateMessage"]],
) -> Iterator["TemplateMessage"]:
    """Yield only non-ghost messages.

    Ghosts are ``None`` slots in ``RenderingContext.messages`` —
    see the ``RenderingContext.messages`` docstring for the model. Use
    this helper instead of ``for m in messages`` when the loop body
    shouldn't
    touch a ghosted slot.
    """
    for m in messages:
        if m is not None:
            yield m


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

        # Pairing metadata (assigned by _mark_pair() / _mark_triple())
        self.pair_first: Optional[int] = None  # Index of first message in pair
        self.pair_middle: Optional[int] = None  # Index of middle message (triples only)
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

        # Set by _graft_agent_sidechannel (#174): True for every node grafted
        # from a workflow agent's side-channel transcript. Formatters use it
        # to render those user prompts as collapsible Markdown with embedded
        # JSON blocks extracted into params tables.
        self.in_workflow_sidechannel: bool = False

        # Agent-nesting depth of this message's session line (#213 visual
        # layer): 0 for the trunk / non-agent messages, 1 for a directly
        # spawned sub-agent, 2 for a sub-agent of a sub-agent, … Set by
        # _build_message_hierarchy (chasing spawned_agent_id links); drives
        # the per-depth group-line colour ramp, the spawn-card depth badge,
        # and the deep-chain indent compression.
        self.agent_depth: int = 0

        # Set by _cleanup_sidechain_duplicates on a Task/Agent spawn
        # tool_result whose sub-agent transcript collapsed ENTIRELY into the
        # prompt + result already shown (the agent answered directly, with no
        # surviving tool calls or thinking). Lets the renderer mark it so a
        # fully-elided transcript reads as "nothing hidden" rather than as a
        # spawn that produced no transcript at all (#213 visual layer).
        self.spawns_collapsed_transcript: bool = False

        # Per-render annotations populated by the HTML renderer's tree walk
        # (HtmlRenderer._annotate_tree_for_render). The recursive template
        # macro reads these instead of receiving a flat (msg, title, html,
        # ts) tuple. ``should_render`` is False for leaf nodes that format
        # to nothing (e.g. TaskCreate/TaskUpdate tool_results) so the macro
        # emits no card for them.
        self.rendered_title: str = ""
        self.rendered_html: str = ""
        self.rendered_timestamp: str = ""
        self.should_render: bool = True

        # Within-session fork tracking: effective session/branch ID for grouping
        self._render_session_id: Optional[str] = None

        # Junction forward links: [(branch_sid, branch_header_msg_index, branch_preview)]
        # Set on messages that are fork points, for rendering forward links
        self.junction_forward_links: list[tuple[str, Optional[int], str]] = []

        # Fork point preview text (short excerpt of fork point message content)
        self.fork_point_preview: str = ""

        # Set by ``_ghost_template_by_detail`` when this slot is a fork point
        # whose own message body is filtered out at the current detail level,
        # but which is kept (not ghosted to None) so the fork point stays a
        # visible, anchorable landmark. The template renders only the
        # fork-point box for such a slot, not the message card (issue #233
        # follow-up — fork points survive detail filtering like the branches
        # they connect, instead of vanishing and orphaning the branches).
        self.fork_only: bool = False

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
    def is_branch_header(self) -> bool:
        """Check if this is a branch (within-session fork) header."""
        return isinstance(self.content, SessionHeaderMessage) and self.content.is_branch

    @property
    def branch_depth(self) -> int:
        """Depth of this branch header in the session tree (0 for non-branches)."""
        if isinstance(self.content, SessionHeaderMessage) and self.content.is_branch:
            return self.content.depth
        return 0

    @property
    def has_children(self) -> bool:
        """Check if this message has any children."""
        return bool(self.children)

    @property
    def is_paired(self) -> bool:
        """Check if this message is part of a pair (or triple)."""
        return self.pair_first is not None or self.pair_last is not None

    @property
    def is_first_in_pair(self) -> bool:
        """Check if this is the first message in a pair (has pair_last set
        but no `pair_first`, since the middle of a triple has both)."""
        return self.pair_last is not None and self.pair_first is None

    @property
    def is_middle_in_pair(self) -> bool:
        """Check if this is the middle message in a triple (has both
        `pair_first` and `pair_last` set, pointing at its surrounding members)."""
        return self.pair_first is not None and self.pair_last is not None

    @property
    def is_last_in_pair(self) -> bool:
        """Check if this is the last message in a pair (has `pair_first` set
        but no `pair_last`, since the middle of a triple has both)."""
        return self.pair_first is not None and self.pair_last is None

    @property
    def pair_role(self) -> Optional[str]:
        """Get the pairing role for CSS class.

        Returns:
            "pair_first" if this is the first message in a pair,
            "pair_middle" if this is the middle message in a triple,
            "pair_last" if this is the last message in a pair,
            None if not paired.
        """
        if self.is_first_in_pair:
            return "pair_first"
        if self.is_middle_in_pair:
            return "pair_middle"
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
    def render_session_id(self) -> str:
        """Get effective session/branch ID for grouping.

        Returns render_session_id if set (for within-session fork branches),
        otherwise falls back to meta.session_id.
        """
        return self._render_session_id or self.meta.session_id

    @render_session_id.setter
    def render_session_id(self, value: str) -> None:
        self._render_session_id = value

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
        "workflow_phase": ("phase", "phases"),
        "workflow_agent": ("agent", "agents"),
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
        # `--combined no` (#151 follow-up): when set, the index should
        # link directly to per-session files (`session["file"]`) rather
        # than the (skipped) combined-transcript file.
        self.combined_suppressed: bool = bool(
            project_data.get("combined_suppressed", False)
        )
        # Teammates feature — distinct team names across this project's
        # sessions. Computed in get_all_cached_projects from each
        # SessionCacheData.team_name.
        self.team_names: list[str] = sorted(project_data.get("team_names", []))

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
    session_tree: Optional["SessionTree"] = None,
    detail: DetailLevel | str = DetailLevel.FULL,
    no_recaps: bool = False,
) -> Tuple[list[TemplateMessage], list[dict[str, Any]], RenderingContext]:
    """Generate root messages and session navigation from transcript messages.

    This is the format-neutral rendering step that produces data structures
    ready for template rendering by any format-specific renderer.

    Args:
        messages: List of transcript entries to process.
        session_tree: Optional pre-built SessionTree from DAG construction.
            When provided, avoids an expensive DAG rebuild.
        detail: Output detail level controlling which message types are included.
            Accepts either a DetailLevel enum or a plain string (e.g. "low").

    Returns:
        A tuple of (root_messages, session_nav, context) where:
        - root_messages: Tree of TemplateMessages (session headers with children)
        - session_nav: Session navigation data with summaries and metadata
        - context: RenderingContext with message registry for index lookups
    """
    from .utils import get_warmup_session_ids

    # Normalize plain string to DetailLevel for convenience (e.g. from CLI)
    if not isinstance(detail, DetailLevel):
        detail = DetailLevel(detail)

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

    # Pre-process to find session summaries. AI-generated session titles
    # ("ai-title" entries) override any leafUuid-mapped summary so the
    # session header and back-link labels use the curated short title
    # whenever Claude Code has emitted one.
    with log_timing("Session summary processing", t_start):
        session_summaries = prepare_session_summaries(messages)
        session_summaries.update(prepare_session_ai_titles(messages))

    # Pre-process: collect teamName per session (teammates feature) so
    # session headers can surface a team badge without re-scanning later.
    with log_timing("Session team-name processing", t_start):
        session_team_names = prepare_session_team_names(messages)

    # Extract session hierarchy from DAG (reuse pre-built tree when available)
    with log_timing("Extract session hierarchy", t_start):
        session_hierarchy, junction_targets = _extract_session_hierarchy(
            messages, session_tree=session_tree
        )

    # Filter messages (removes summaries, warmup, empty, etc.)
    with log_timing("Filter messages", t_start):
        filtered_messages = _filter_messages(messages)

    # Detail-level filtering happens entirely post-render via
    # ``_ghost_template_by_detail`` (single-axis collapse — Phase 3 of the
    # ghosting epic). The pre-render ``_filter_by_detail`` is gone; the
    # per-class ``detail_visibility`` predicate now drives all stripping.

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
        ctx = _render_messages(
            filtered_messages,
            sessions,
            show_tokens_for_message,
            session_hierarchy,
            session_summaries,
            session_team_names,
            junction_targets,
        )

    # Fold Skill-tool bodies (isMeta slash-command entries) into their
    # originating tool_use. Runs before the detail filter so the body
    # survives alongside the tool_use at HIGH — and the now-redundant
    # slash-command + "Launching skill" tool_result are dropped once.
    with log_timing("Pair Skill tool_uses", t_start):
        _pair_skill_tool_uses(ctx)

    # Populate junction forward links on fork-point messages
    with log_timing("Link junction forwards", t_start):
        _link_junction_forwards(ctx)

    # Detail-level post-render: ghost non-visible slots in place.
    # ``_ghost_template_by_detail`` sets ``ctx.messages[i] = None``
    # for each filtered slot and repairs anchor-target references
    # (``session_first_message``, ``parent_message_index``,
    # ``junction_forward_links``) so dropped fork-points don't leave
    # dead ``#msg-d-{N}`` links.
    # ``--no-recaps`` suppresses recaps even at FULL, so run the ghost pass
    # whenever filtering OR recap suppression is requested (#179).
    if detail != DetailLevel.FULL or no_recaps:
        with log_timing(f"Detail post-render filter ({detail.value})", t_start):
            _ghost_template_by_detail(ctx, detail, no_recaps=no_recaps)

    # Prepare session navigation data (uses ctx for session header indices)
    session_nav: list[dict[str, Any]] = []
    with log_timing(
        lambda: f"Session navigation building ({len(session_nav)} sessions)", t_start
    ):
        session_nav = prepare_session_navigation(
            sessions, session_order, ctx, session_hierarchy
        )

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

    # Pull each subagent's thread back next to its trunk Task/Agent
    # tool_result. Pair-reordering left them stranded at the trunk tail,
    # which would collapse every agent's content under whichever
    # tool_result rendered last.
    with log_timing("Relocate subagent blocks", t_start):
        template_messages = _relocate_subagent_blocks(template_messages)

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

    # Accumulate teammate_id -> color map from <teammate-message color="...">
    # blocks so downstream formatters (TaskUpdate owner badges, SendMessage
    # recipient, TaskList rows) can colorize names the entry itself didn't
    # annotate.
    with log_timing("Collect teammate colors", t_start):
        _populate_teammate_colors(ctx)

    # Build task_id ↔ subject / tool_use_id maps so TaskCreate / TaskUpdate
    # tool_use titles can surface the human-readable subject + assigned id.
    with log_timing("Collect task metadata", t_start):
        _populate_task_metadata(ctx)

    # Async-agents (#90): pair each ``<task-notification>`` whose
    # ``<result>`` body duplicates the last sub-assistant in the
    # spawning Task's sidechain with that spawn, fold the answer onto
    # ``TaskOutput.async_final_answer``, and flag the notification
    # ``result_is_duplicate``. The format-specific renderers honour the
    # flag — at ``DetailLevel.LOW`` they return empty for the
    # duplicate's title and body, so the rendering loop's existing
    # "skip empty messages" elision drops the card without us having
    # to delete + reindex (which would invalidate ancestry classes,
    # backlink fields, and session nav anchors). The notification
    # itself stays in ``ctx.messages`` — only its rendered output
    # disappears at LOW.
    with log_timing("Link async notifications", t_start):
        _link_async_notifications(ctx, detail)

    # Link parsed dynamic-workflow runs to their Workflow tool_use by taskId
    # (#174 PR3) so the formatter can render snapshot-first meta (and step 3
    # can splice the phase/agent tree).
    with log_timing("Link workflow runs", t_start):
        _link_workflow_runs(
            ctx,
            session_tree.workflow_runs if session_tree is not None else {},
            session_tree.workflow_links if session_tree is not None else None,
        )

    # Independent pass: link tool-use-id-bearing notifications (e.g.
    # built-in Monitor task-end) back to their originating tool_use.
    # Distinct from the agent-spawn flow above — there's no fold or
    # dedup, just a backlink on the Task ID value (#142).
    with log_timing("Link tool_use notifications", t_start):
        _link_tool_use_notifications(ctx)

    # Independent pass: cross-link CronList rows and CronDelete results
    # back to the originating CronCreate by job id (#148). Build the
    # job_id → message_index map from CronCreate outputs, then write
    # the link onto each consumer site.
    with log_timing("Link cron jobs by id", t_start):
        _link_cron_jobs_by_id(ctx)

    # Independent pass: cross-link TaskOutput polls back to the
    # originating Bash (run_in_background) or Task (async-agent) call,
    # and TaskUpdate calls back to the originating TaskCreate (#154).
    # Two id spaces share the pass — background-process ids
    # (alphanumeric, e.g. ``bcc00rq51``) and todo-list ids
    # (``"1"``-style); the consumer's input model carries the right
    # shape unambiguously.
    with log_timing("Link task_id consumers", t_start):
        _link_task_id_consumers(ctx)

    # MUST be last (#174 PR3): splice each linked WorkflowRun's phase/agent
    # sub-tree under its Workflow tool_use node. Registers synthetic + grafted
    # nodes (appending to ctx.messages via the monotonic ctx.register
    # allocator), so it has to follow every ctx.messages-iterating pass above.
    with log_timing("Splice workflow runs", t_start):
        _splice_workflow_runs(ctx)

    return root_messages, session_nav, ctx


# -- Session Utilities --------------------------------------------------------


def _extract_session_hierarchy(
    messages: list[TranscriptEntry],
    session_tree: Optional["SessionTree"] = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Extract session hierarchy from DAG for rendering.

    Args:
        messages: Transcript entries (used to build DAG if tree not provided).
        session_tree: Pre-built SessionTree to reuse (avoids expensive rebuild).

    Returns:
        (hierarchy, junction_targets) where:
        - hierarchy: session_id -> {parent_session_id, attachment_uuid, depth}
        - junction_targets: uuid -> [target session IDs]
    """
    if session_tree is not None:
        tree = session_tree
    else:
        from .dag import build_dag_from_entries

        tree = build_dag_from_entries(messages)

    depth_cache: dict[str, int] = {}

    def _depth(sid: str) -> int:
        if sid in depth_cache:
            return depth_cache[sid]
        dl = tree.sessions.get(sid)
        if dl is None or dl.parent_session_id is None:
            depth_cache[sid] = 0
            return 0
        d = 1 + _depth(dl.parent_session_id)
        depth_cache[sid] = d
        return d

    hierarchy: dict[str, dict[str, Any]] = {}
    for sid, dag_line in tree.sessions.items():
        hierarchy[sid] = {
            "parent_session_id": dag_line.parent_session_id,
            "attachment_uuid": dag_line.attachment_uuid,
            "depth": _depth(sid),
            "is_branch": dag_line.is_branch,
            "original_session_id": dag_line.original_session_id,
            "first_uuid": dag_line.uuids[0] if dag_line.uuids else None,
            # Full DAG-line uuid sequence. ``_build_branch_header``
            # scans this to find the first user entry with text for
            # the branch preview — needed when the branch's first
            # entry is an assistant turn (e.g. "No response requested."
            # after ``/exit``) and the trigger-message-only path
            # would leave the preview empty.
            "uuids": list(dag_line.uuids),
        }

    junction_targets: dict[str, list[str]] = {}
    for uuid, jp in tree.junction_points.items():
        junction_targets[uuid] = jp.target_sessions

    return hierarchy, junction_targets


def _build_uuid_to_render_sid(
    session_hierarchy: dict[str, dict[str, Any]] | None,
) -> dict[str, str]:
    """Build a ``uuid → render_session_id`` map from the SessionTree.

    Each rendered ``TemplateMessage`` carries a ``render_session_id``
    that groups it with the correct session in the final tree
    (trunk, within-session branch, or agent's parent). Pre-D11 this
    was tracked by a loop-local ``current_render_session`` variable
    flipped on each branch-start trigger — a re-derivation of what
    the ``SessionTree`` already knew authoritatively. The loop
    variable carried a latent bug: if a branch's ``first_uuid`` was
    dropped before rendering (structurally, by ``_filter_messages`` —
    or, before the single-axis collapse, by the pre-render detail
    filter), the trigger never fired and subsequent branch messages
    silently inherited the *previous* branch's sid (or ``None``).

    Reading the map up front fixes this — every uuid in a branch
    DAG-line maps to that branch's sid regardless of which entries
    survive filtering.

    Map contents:

    - **Branch lines** (``is_branch=True``, sid contains ``@``) —
      every uuid maps to the branch sid.
    - **Agent lines** (sid contains ``#agent-``) — every uuid maps
      to the agent's *immediate* parent (``parent_session_id`` from
      the hierarchy, falling back to ``get_parent_session_id(sid)``).
      Immediate means: a nested agent B inside agent A maps to A's
      synthetic sid, NOT transitively to the trunk. Replicates the
      pre-D11 inline resolution.
    - **Trunk lines** — uuids are OMITTED. The caller's
      ``map.get(uuid)`` returns ``None``, leaving the
      ``TemplateMessage``'s ``_render_session_id`` unset, which
      then falls back to ``meta.session_id`` at read time
      (``TemplateMessage.render_session_id`` property).

    Returns the empty map if ``session_hierarchy`` is ``None`` or
    empty — every uuid falls through to the trunk path.
    """
    result: dict[str, str] = {}
    if not session_hierarchy:
        return result
    for sid, hier in session_hierarchy.items():
        line_uuids: list[str] = hier.get("uuids") or []
        if is_agent_session(sid):
            parent = hier.get("parent_session_id") or get_parent_session_id(sid)
            if parent:
                for uuid in line_uuids:
                    result[uuid] = parent
        elif hier.get("is_branch"):
            for uuid in line_uuids:
                result[uuid] = sid
        # Trunk sessions are deliberately omitted — see docstring.
    return result


def prepare_session_team_names(messages: list[TranscriptEntry]) -> dict[str, str]:
    """Extract the teamName per session (teammates feature).

    Returns:
        Dict mapping session_id → team_name. First non-None ``teamName``
        sighting per session wins (Claude Code stamps every entry with the
        same teamName for the duration of a team's activity).
    """
    out: dict[str, str] = {}
    for message in messages:
        team_name = getattr(message, "teamName", None)
        if not team_name:
            continue
        session_id = getattr(message, "sessionId", "")
        if not session_id:
            continue
        out.setdefault(session_id, team_name)
    return out


def prepare_session_ai_titles(messages: list[TranscriptEntry]) -> dict[str, str]:
    """Extract Claude Code AI-generated session titles from messages.

    Multiple ``ai-title`` entries may appear per session as the title is
    refined; the last one wins.

    Returns:
        Dict mapping session_id to ai_title text.
    """
    out: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AiTitleTranscriptEntry):
            out[message.sessionId] = message.aiTitle
    return out


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


def branch_short_uuid(branch_sid: str) -> str:
    """Return the 8-char prefix of the branch root's UUID.

    Branch session IDs follow the ``{trunk}@{first_uuid_prefix}`` shape; the
    last segment after ``@`` is the branch root's UUID truncated to 12
    chars by ``_walk_session_with_forks``. We surface its first 8 chars as
    the stable identifier in branch labels.

    Cross-module helper — the markdown renderer composes
    ``branch-<uuid8>`` anchor keys and a defensive heading fallback off
    the same rule, and centralising them here prevents drift if the
    suffix length or splitting convention ever changes (e.g. if branch
    sids ever switch separators).
    """
    return branch_sid.split("@")[-1][:8]


def _branch_label_suffix(branch_sid: str, preview: str) -> str:
    """The ``<uuid8>`` or ``<uuid8> • <preview>`` tail of a branch label.

    Single source of truth for the format that follows the literal
    ``"Branch "`` head in :func:`_branch_label`. The fork-point box's
    template renders ``Branch &bull; {{ branch_preview }}`` on its own
    side, so it needs only this suffix — composing the full
    :func:`_branch_label` and slicing off ``"Branch • "`` would couple
    the consumer to the head's exact literal, which makes future
    tweaks (i18n, an icon, separator change) silently breaking.

    Truncates ``preview`` to 80 chars plus a single ``…`` (U+2026)
    when the source is longer.
    """
    short_uuid = branch_short_uuid(branch_sid)
    if not preview:
        return short_uuid
    short = preview[:80]
    if len(preview) > 80:
        short += "…"
    return f"{short_uuid} • {short}"


def _branch_label(branch_sid: str, preview: str) -> str:
    """Compose the consistent ``Branch • <uuid8> • <preview>`` label.

    Used in three places that all need to agree:
    - the body branch-header title (``SessionHeaderMessage.title``),
    - the session/graph index nav (``first_user_message``),
    - the fork-point box's per-branch link (the trailing-text portion,
      via :func:`_branch_label_suffix`).

    Always includes the 8-char UUID — both as a stable navigation handle
    when the preview is missing or generic, and to disambiguate two
    branches whose previews happen to start the same way (two `/exit`
    branches, two slash-command branches with similar prefixes, …).

    Truncates ``preview`` to 80 chars plus a single ``…`` (U+2026) when
    the source is longer, keeping the body header on one line. The
    single-character ellipsis matters: ``"..."`` (3 chars) would push
    the truncated preview to 83 visible chars and contradict the
    docstring's "80 + ellipsis" cap.
    """
    return f"Branch • {_branch_label_suffix(branch_sid, preview)}"


def _tool_summary_label(tool_name: Optional[str], description: Optional[str]) -> str:
    """Compose a short ``Tool — description`` label for nav previews.

    Used so a fork point or branch whose first message is a tool call reads as
    e.g. ``Bash — Timeline d-N dependency check (retry)`` instead of an empty
    preview (#179 follow-up / dev/tool-use-continuation).
    """
    name = (tool_name or "Tool").strip()
    desc = (description or "").strip().splitlines()[0].strip() if description else ""
    if len(desc) > 70:
        desc = desc[:70] + "…"
    return f"{name} — {desc}" if desc else name


def _entry_nav_summary(entry: "TranscriptEntry") -> str:
    """Type-aware one-line summary of a transcript entry, for branch previews.

    Mirrors ``_fork_point_preview``'s label shapes but reads the raw entry
    (available at branch-header build time): user/assistant text → the text,
    a tool call → ``Bash — <description>``, thinking → ``Thinking``. Returns
    "" for entries with no summarisable content (system/hook/attachment), so
    the caller walks to the branch's first meaningful message.
    """
    if isinstance(entry, UserTranscriptEntry):
        content = entry.message.content
        if isinstance(content, str):
            return create_session_preview(content)
        items: list[ContentItem] = content
    elif isinstance(entry, AssistantTranscriptEntry):
        items = entry.message.content
    else:
        return ""
    for item in items:
        if isinstance(item, TextContent):
            text = item.text.strip()
            if text:
                return create_session_preview(text)
        elif isinstance(item, ToolUseContent):
            description = item.input.get("description")
            return _tool_summary_label(
                item.name, description if isinstance(description, str) else None
            )
        elif isinstance(item, ThinkingContent):
            return "Thinking"
    return ""


def _fork_point_preview(fork_msg: "TemplateMessage", ctx: RenderingContext) -> str:
    """Get a meaningful preview for a fork point message.

    If the fork point is a system hook (common with /rewind), walk up
    to the parent message to find more descriptive content. Tool-use and
    thinking fork points (the common shape for tool-flow forks) get a
    type-aware label so the fork point names the message it sits on.
    """
    msg = fork_msg
    # Walk up past system hooks to find a meaningful message
    for _ in range(3):  # limit walk depth
        if not isinstance(
            msg.content,
            (
                SystemMessage,
                HookSummaryMessage,
                AwaySummaryMessage,
                SessionHeaderMessage,
            ),
        ):
            break
        # Find parent by looking at parent_uuid
        parent_uuid = msg.meta.parent_uuid
        if not parent_uuid:
            break
        parent = next(
            (m for m in _visible(ctx.messages) if m.meta.uuid == parent_uuid), None
        )
        if parent is None:
            break
        msg = parent

    # Extract a label from the found message, type-aware so tool-use and
    # thinking fork points name their message rather than rendering empty.
    content = msg.content
    if isinstance(content, (AssistantTextMessage, UserTextMessage)):
        parts = [item.text for item in content.items if isinstance(item, TextContent)]
        text = " ".join(parts).strip()
    elif isinstance(content, ToolUseMessage):
        text = _tool_summary_label(
            content.tool_name, getattr(content.input, "description", None)
        )
    elif isinstance(content, ThinkingMessage):
        text = "Thinking"
    elif isinstance(content, ToolResultMessage):
        text = f"{content.tool_name} result" if content.tool_name else "Tool result"
    else:
        return ""

    if not text:
        return ""
    # Truncate for nav display
    short = text[:80]
    if len(text) > 80:
        short += "..."
    return short


def _link_junction_forwards(ctx: RenderingContext) -> None:
    """Populate forward navigation links on within-session fork points.

    For each junction (a message whose DAG node fans out into multiple
    branch sessions), attach links from the fork-point message to each
    branch's first message so the output can offer "jump to branch"
    navigation. Runs once ``ctx.messages`` is final.

    The branch preview is read directly from each branch's
    ``SessionHeaderMessage.preview`` and re-composed via
    ``_branch_label_suffix`` — the body header, the index nav and this
    fork-point box all share that single ``preview`` source. A fork with
    fewer than two navigable branches is dropped (spurious
    parallel-tool_use forks are collapsed at the DAG level; this is
    defense-in-depth for any residual single-branch shell).
    """
    if not ctx.junction_targets:
        return
    # Build UUID → TemplateMessage index for fast lookup
    uuid_to_msg: dict[str, TemplateMessage] = {}
    # Build msg_index → TemplateMessage for branch preview lookup
    idx_to_msg: dict[int, TemplateMessage] = {}
    for msg in _visible(ctx.messages):
        if msg.meta.uuid:
            uuid_to_msg[msg.meta.uuid] = msg
        if msg.message_index is not None:
            idx_to_msg[msg.message_index] = msg
    for uuid, target_sids in ctx.junction_targets.items():
        # Only add forward links for within-session fork branches
        branch_targets = [sid for sid in target_sids if "@" in sid]
        if branch_targets and uuid in uuid_to_msg:
            fork_msg = uuid_to_msg[uuid]
            fork_msg.fork_point_preview = _fork_point_preview(fork_msg, ctx)
            for branch_sid in branch_targets:
                branch_idx = ctx.session_first_message.get(branch_sid)
                if branch_idx is not None:
                    # Read the branch's preview directly from the
                    # SessionHeaderMessage rather than parsing its
                    # composed title — the body header, the index
                    # nav and this fork-point box all read the same
                    # raw ``preview`` field and re-compose via
                    # ``_branch_label`` / ``_branch_label_suffix``
                    # independently.
                    preview_text = ""
                    branch_header = idx_to_msg.get(branch_idx)
                    if branch_header and isinstance(
                        branch_header.content, SessionHeaderMessage
                    ):
                        preview_text = branch_header.content.preview or ""
                    # The fork-point template prepends
                    # ``Branch &bull; ...`` itself, so we hand it
                    # only the suffix — single source of truth for
                    # the format keeps the index nav, the body
                    # header and this link aligned even if the
                    # ``Branch • `` head ever changes.
                    link_suffix = _branch_label_suffix(branch_sid, preview_text)
                    fork_msg.junction_forward_links.append(
                        (branch_sid, branch_idx, link_suffix)
                    )
            # A real fork has ≥ 2 navigable branches. Drop the
            # indicator when the DAG-level layer left only a
            # single-branch shell (e.g. a passthrough sibling whose
            # first message was filtered out — the spurious
            # parallel-tool_use forks are now collapsed at the DAG
            # level, but defense-in-depth here covers any residual
            # cases). When ≥ 2 branches remain, surface the
            # indicator regardless of whether titles are
            # human-readable previews or UUID-only fallbacks — the
            # backlinks are useful navigation either way.
            if len(fork_msg.junction_forward_links) < 2:
                fork_msg.junction_forward_links.clear()
                fork_msg.fork_point_preview = ""


def prepare_session_navigation(
    sessions: dict[str, dict[str, Any]],
    session_order: list[str],
    ctx: RenderingContext,
    session_hierarchy: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Prepare session navigation data for template rendering.

    Args:
        sessions: Dictionary mapping session_id to session info dict
        session_order: List of session IDs in display order
        ctx: RenderingContext with session_first_message indices
        session_hierarchy: Optional hierarchy data from _extract_session_hierarchy()

    Returns:
        List of session navigation dicts for template rendering
    """
    session_nav: list[dict[str, Any]] = []

    for session_id in session_order:
        # Skip agent sidechain sessions (they appear inline, not in nav)
        if is_agent_session(session_id):
            continue
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

        # Get hierarchy data
        hier = (session_hierarchy or {}).get(session_id, {})
        parent_sid = hier.get("parent_session_id")
        parent_message_index = (
            ctx.session_first_message.get(parent_sid) if parent_sid else None
        )

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
                "parent_session_id": parent_sid,
                "parent_message_index": parent_message_index,
                "depth": hier.get("depth", 0),
            }
        )

    # Add branch pseudo-sessions from hierarchy
    if session_hierarchy:
        # Lift each branch's raw ``preview`` directly off its
        # SessionHeaderMessage. ``_build_branch_header`` already
        # computed it by scanning the branch's DAG-line for the first
        # user entry with non-empty text — handling plain text, slash
        # commands and other user shapes uniformly via
        # ``extract_text_content``, including the assistant-start
        # case (the post-pass ``_enrich_branch_titles`` used to
        # back-fill). We just read what's there.
        branch_previews: dict[str, str] = {}
        for msg in _visible(ctx.messages):
            if not isinstance(msg.content, SessionHeaderMessage):
                continue
            if not msg.content.is_branch:
                continue
            sid = msg.content.session_id
            if sid in branch_previews:
                continue
            preview = msg.content.preview or ""
            if preview:
                branch_previews[sid] = preview

        # Group branches by their junction point (attachment_uuid)
        junction_branches: dict[str, list[dict[str, Any]]] = {}
        for sid, hier in session_hierarchy.items():
            if hier.get("is_branch"):
                attachment = hier.get("attachment_uuid", "")
                junction_branches.setdefault(attachment, []).append(
                    {"sid": sid, **hier}
                )

        # For each junction point, insert fork-point and branch nav items
        for attachment_uuid, branches in junction_branches.items():
            # Drop branches whose first message was filtered out (e.g. a
            # passthrough attachment) — their #msg-d-None anchor points
            # nowhere. If no navigable branches remain, the fork point
            # itself is useless and is dropped too.
            navigable_branches = [
                b
                for b in branches
                if ctx.session_first_message.get(b["sid"]) is not None
            ]
            if not navigable_branches:
                continue

            # Find the session nav item that contains this junction
            parent_sid = navigable_branches[0].get("parent_session_id", "")
            parent_nav_idx = next(
                (i for i, n in enumerate(session_nav) if n["id"] == parent_sid),
                None,
            )
            if parent_nav_idx is None:
                continue

            parent_depth = session_nav[parent_nav_idx]["depth"]
            insert_pos = parent_nav_idx + 1
            # Skip past any existing children of this parent
            while (
                insert_pos < len(session_nav)
                and session_nav[insert_pos].get("depth", 0) > parent_depth
            ):
                insert_pos += 1

            # Fork point nav item — find the junction message and a
            # meaningful preview (walk up past system hooks to find it).
            # Leave ``fork_msg_idx`` None until the fork point is actually
            # located among the visible messages: if it was ghosted (e.g.
            # a slash body or launch tool_result consumed by
            # ``_pair_skill_tool_uses``), the ``_visible`` loop skips it and
            # the anchor must be *omitted*, not retargeted at the parent
            # session header. Falling back to ``session_first_message`` here
            # would silently undo ``_drop_anchor_refs_into_ghosts``.
            fork_msg_idx = None
            fork_preview = ""
            fork_msg = None
            for msg in _visible(ctx.messages):
                if msg.meta.uuid == attachment_uuid and msg.message_index is not None:
                    fork_msg_idx = msg.message_index
                    fork_msg = msg
                    break
            if fork_msg is not None:
                fork_preview = _fork_point_preview(fork_msg, ctx)

            fork_label = (
                f"Fork point • {fork_preview}"
                if fork_preview
                else f"Fork point ({len(navigable_branches)} branches)"
            )

            fork_nav = {
                "id": f"fork-{attachment_uuid[:12]}",
                "message_index": fork_msg_idx,
                "summary": None,
                "timestamp_range": "",
                "first_timestamp": "",
                "last_timestamp": "",
                "message_count": 0,
                "first_user_message": fork_label,
                "token_summary": "",
                "parent_session_id": parent_sid,
                "parent_message_index": fork_msg_idx,
                "depth": parent_depth + 1,
                "is_fork_point": True,
            }
            session_nav.insert(insert_pos, fork_nav)
            insert_pos += 1

            # Branch nav items
            for branch in navigable_branches:
                branch_sid = branch["sid"]
                branch_msg_idx = ctx.session_first_message.get(branch_sid)
                branch_nav = {
                    "id": branch_sid,
                    "message_index": branch_msg_idx,
                    "summary": None,
                    "timestamp_range": "",
                    "first_timestamp": "",
                    "last_timestamp": "",
                    "message_count": 0,
                    "first_user_message": _branch_label(
                        branch_sid, branch_previews.get(branch_sid, "")
                    ),
                    "token_summary": "",
                    "parent_session_id": parent_sid,
                    "parent_message_index": fork_msg_idx,
                    "depth": parent_depth + 2,
                    "is_branch": True,
                }
                session_nav.insert(insert_pos, branch_nav)
                insert_pos += 1

    # Surface compact_boundary ruptures as navigational landmarks.
    # A CompactedSummaryMessage marks the point where `/compact` was run and
    # pre-compaction context was replaced with a summary — a real content
    # discontinuity that's useful to jump to.
    compact_by_session: dict[str, list[TemplateMessage]] = {}
    for msg in _visible(ctx.messages):
        if isinstance(msg.content, CompactedSummaryMessage):
            compact_by_session.setdefault(msg.render_session_id, []).append(msg)

    # Build a uuid → TemplateMessage lookup so each compaction landmark
    # can read preTokens / trigger from its preceding system entry.
    uuid_to_msg: dict[str, TemplateMessage] = {
        msg.meta.uuid: msg for msg in _visible(ctx.messages) if msg.meta.uuid
    }

    for comp_sid, comp_msgs in compact_by_session.items():
        comp_msgs.sort(key=lambda m: m.meta.timestamp)
        parent_nav_idx = next(
            (i for i, n in enumerate(session_nav) if n["id"] == comp_sid),
            None,
        )
        if parent_nav_idx is None:
            continue
        parent_depth = session_nav[parent_nav_idx]["depth"]
        insert_pos = parent_nav_idx + 1
        # Skip past any existing children of this parent (branches, etc.)
        while (
            insert_pos < len(session_nav)
            and session_nav[insert_pos].get("depth", 0) > parent_depth
        ):
            insert_pos += 1

        for comp_msg in comp_msgs:
            if comp_msg.message_index is None:
                continue
            label = _compact_nav_label(comp_msg, uuid_to_msg)
            comp_nav = {
                "id": f"compact-{comp_msg.message_index}",
                "message_index": comp_msg.message_index,
                "summary": None,
                "timestamp_range": "",
                "first_timestamp": comp_msg.meta.timestamp,
                "last_timestamp": "",
                "message_count": 0,
                "first_user_message": label,
                "token_summary": "",
                "parent_session_id": comp_sid,
                "parent_message_index": session_nav[parent_nav_idx]["message_index"],
                "depth": parent_depth + 1,
                "is_compaction_point": True,
            }
            session_nav.insert(insert_pos, comp_nav)
            insert_pos += 1

    return session_nav


def _compact_nav_label(
    comp_msg: "TemplateMessage",
    uuid_to_msg: dict[str, "TemplateMessage"],
) -> str:
    """Build the nav label for a CompactedSummaryMessage landmark.

    Enriches with preTokens (rounded to thousands) when the parent
    system/compact_boundary entry exposes it via `SystemMessage`,
    plus the summary's own formatted timestamp.

    Example: "Conversation compacted (115k tokens) • 2026-04-14 09:09"
    """
    parts: list[str] = ["Conversation compacted"]
    parent_uuid = comp_msg.meta.parent_uuid
    if parent_uuid:
        parent = uuid_to_msg.get(parent_uuid)
        if parent is not None and isinstance(parent.content, SystemMessage):
            pre_tokens = parent.content.compact_pre_tokens
            if pre_tokens:
                if pre_tokens >= 1000:
                    parts[0] += f" ({pre_tokens // 1000}k tokens)"
                else:
                    parts[0] += f" ({pre_tokens} tokens)"
    ts = format_timestamp(comp_msg.meta.timestamp) if comp_msg.meta.timestamp else ""
    if ts:
        parts.append(ts)
    return " • ".join(parts)


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

        # Index system messages by UUID for parent-child pairing.
        # Exclude hook flavours (``HookAttachmentMessage`` from #128 and
        # ``HookSummaryMessage``): they share ``type == "system"`` but
        # are out-of-band callbacks, not conversation system entries.
        # Indexing them here would let a chained system entry (e.g. a
        # ``stop_hook_summary`` whose ``parentUuid`` is the hook
        # attachment) pair the hook as its parent — visible as a
        # spurious "▼ 1 system" fold-bar on every hook in dense
        # transcripts (e.g. ClMail bursts).
        if (
            msg.meta.uuid
            and msg.type == "system"
            and not isinstance(msg.content, (HookAttachmentMessage, HookSummaryMessage))
        ):
            uuid_index[msg.meta.uuid] = msg

    return PairingIndices(
        tool_use=tool_use_index,
        tool_result=tool_result_index,
        uuid=uuid_index,
    )


def _mark_pair(first: TemplateMessage, last: TemplateMessage) -> None:
    """Mark two messages as a pair by setting their pair indices.

    Each member stores a pointer to the *other* end of the pair, not its
    own role:

    - ``first.pair_last`` is the **forward** link from the first member
      to its partner (the last member).
    - ``last.pair_first`` is the **back** link from the last member to
      its partner (the first member).

    So this function does NOT set ``first.pair_first`` — that field
    stays ``None`` on a first-role member, and is read by
    ``is_first_in_pair`` to detect the role. Likewise it does not set
    ``last.pair_last``. Mistaking the asymmetry has caused two bugs
    (#137 chain pairing; CodeRabbit-flagged sibling-overwrite); guard
    logic at call sites must check both fields when deciding whether a
    parent is already involved in any pair.
    """
    first_index = first.message_index
    last_index = last.message_index
    if first_index is not None and last_index is not None:
        first.pair_last = last_index
        last.pair_first = first_index


def _mark_triple(
    first: TemplateMessage, middle: TemplateMessage, last: TemplateMessage
) -> None:
    """Mark three messages as a triple (pair_first → pair_middle → pair_last).

    Used for the `(UserSlash caveat, SlashCommand, CommandOutput)` sequence
    that wraps every `/cmd`-style invocation in real transcripts: the three
    messages share one timestamp and represent a single logical event.
    """
    first_index = first.message_index
    middle_index = middle.message_index
    last_index = last.message_index
    if first_index is None or middle_index is None or last_index is None:
        return
    first.pair_middle = middle_index
    first.pair_last = last_index
    middle.pair_first = first_index
    middle.pair_last = last_index
    last.pair_first = first_index


def _try_pair_adjacent(
    current: TemplateMessage,
    next_msg: TemplateMessage,
) -> bool:
    """Try to pair adjacent messages based on their types.

    Returns True if messages were paired, False otherwise.

    Adjacent pairing rules (2-message — checked after the 3-message rule
    in `_try_pair_triple`):
    - slash-command invocation + slash-command expanded prompt (either order)
    - user slash-command + user command-output
    - bash-input + bash-output
    - thinking + assistant
    """
    # Slash command invocation + expanded prompt — represent one logical
    # event (the typed `/cmd` and the prompt-text the harness sent in its
    # place) and may appear in either order: `/init` shows Slash → UserSlash,
    # while `/exit` shows UserSlash (caveat) → Slash.
    if (
        isinstance(current.content, SlashCommandMessage)
        and isinstance(next_msg.content, UserSlashCommandMessage)
    ) or (
        isinstance(current.content, UserSlashCommandMessage)
        and isinstance(next_msg.content, SlashCommandMessage)
    ):
        _mark_pair(current, next_msg)
        return True

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


def _try_pair_triple(
    a: TemplateMessage, b: TemplateMessage, c: TemplateMessage
) -> bool:
    """Try to pair three adjacent messages as a single logical event.

    Returns True if pair_first/pair_middle/pair_last were assigned.

    Triple pairing rules:
    - `(UserSlashCommand caveat, SlashCommand /cmd, CommandOutput)` — the
      common `/exit`, `/clear`, `/context`, `/todos`, `/doctor` shape:
      the harness emits a caveat preamble, the typed slash command, and
      the command's output as three sibling user messages with a single
      timestamp. Grouping them keeps the slash-command title authoritative
      in Markdown and avoids an orphan output that would otherwise lose
      its rendered body.
    """
    if (
        isinstance(a.content, UserSlashCommandMessage)
        and isinstance(b.content, SlashCommandMessage)
        and isinstance(c.content, CommandOutputMessage)
    ):
        _mark_triple(a, b, c)
        return True
    return False


def _is_continuation_content(msg: TemplateMessage) -> bool:
    """Is this message assistant *continuation* content (prose/thinking)?

    Used by the tool_use↔tool_result pairing rule: when the assistant kept
    talking between issuing a tool call and receiving its (lagging) result —
    a ``max_tokens`` split, a thinking block, the prose of a next turn —
    the result must stay in its chronological place rather than be pulled
    back across the continuation (the DAG linearizes this shape instead of
    forking; see ``dag._is_continuation_fork``).

    Sidechain / subagent-session messages don't count: they interleave
    between every Task/Agent tool_use and its result by construction and
    are relocated after pair-reordering (``_relocate_subagent_blocks``).
    Sibling tool_use/tool_result messages don't count either, so parallel
    tool batches keep pairing adjacent as before.
    """
    if msg.is_sidechain or "#agent-" in msg.session_id:
        return False
    if isinstance(msg.content, AssistantTextMessage):
        return bool(msg.content.items)  # an empty split carries no visible content
    return msg.type == "thinking"


def _continuation_between(
    first: TemplateMessage,
    last: TemplateMessage,
    positions: dict[int, int],
    continuation_prefix: list[int],
) -> bool:
    """True when assistant continuation content sits strictly between
    ``first`` and ``last`` in the current linear order."""
    i = positions.get(id(first))
    j = positions.get(id(last))
    if i is None or j is None or j <= i + 1:
        return False
    return continuation_prefix[j] - continuation_prefix[i + 1] > 0


def _try_pair_by_index(
    current: TemplateMessage,
    indices: PairingIndices,
    positions: dict[int, int],
    continuation_prefix: list[int],
) -> None:
    """Try to pair current message with another using index lookups.

    Index-based pairing rules (can be any distance apart):
    - tool_use + tool_result (by tool_use_id within same session) —
      *unless* assistant continuation content sits between them, in which
      case the lagging result keeps its chronological position so the
      continuation renders in between (see ``_is_continuation_content``)
    - system parent + system child (by uuid/parent_uuid)
    """
    # Tool use + tool result (by tool_use_id within same session)
    if current.type == "tool_use" and current.tool_use_id and current.session_id:
        key = (current.session_id, current.tool_use_id)
        if key in indices.tool_result:
            result = indices.tool_result[key]
            if not _continuation_between(
                current, result, positions, continuation_prefix
            ):
                _mark_pair(current, result)

    # System child message finding its parent (by parent_uuid).
    # The uuid index only contains system messages, so this is a
    # system→system pairing path. Skip when the candidate parent is
    # **already involved in any pair** — both ``pair_first`` and
    # ``pair_last`` must be ``None`` for the call to fire. Two distinct
    # failure modes this guard covers (#137):
    #
    # 1. **Chain.** Each system entry's ``parentUuid`` = the previous
    #    system entry's ``uuid`` (common with ``/context`` / ``/cost``
    #    multi-step output). Without a guard, ``_mark_pair`` fires on
    #    every link, leaving each interior node with both ``pair_first``
    #    AND ``pair_last`` set, which ``is_middle_in_pair`` reads as a
    #    triple-middle. The chain-bug guard alone (``pair_first is
    #    None``) catches this — the second link sees the parent has
    #    already been paired AS A CHILD (``pair_first`` set).
    #
    # 2. **Siblings sharing a parent.** Two system entries with the
    #    same ``parentUuid``. The chain-bug guard alone misses this:
    #    ``_mark_pair`` only sets ``parent.pair_last``, never
    #    ``parent.pair_first``, so a parent that's already someone's
    #    *first* still passes the ``pair_first is None`` check. The
    #    second sibling's call would overwrite ``parent.pair_last`` and
    #    leave the first sibling's ``pair_first`` pointing at a parent
    #    whose ``pair_last`` no longer points back. The full guard
    #    (``pair_first is None and pair_last is None``) only pairs
    #    virgin parents, so siblings beyond the first render as
    #    standalone cards rather than half-pairs.
    # Symmetry with the index-build above: hook flavours don't act as
    # children in the system→system parent/child pairing either. A
    # ``hook_blocking_error`` attachment chained behind a previous
    # system entry shouldn't render as a paired sub-row of that
    # entry; both flavours stand on their own.
    if (
        current.type == "system"
        and current.parent_uuid
        and not isinstance(current.content, (HookAttachmentMessage, HookSummaryMessage))
    ):
        parent = indices.uuid.get(current.parent_uuid)
        if (
            parent is not None
            and parent.pair_first is None
            and parent.pair_last is None
        ):
            _mark_pair(parent, current)


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

    # Positions + prefix counts of continuation content, so the tool
    # pairing rule can tell in O(1) whether assistant prose/thinking sits
    # between a tool_use and its lagging result (in which case they must
    # not pair — the continuation renders in between).
    positions = {id(msg): pos for pos, msg in enumerate(messages)}
    continuation_prefix = [0]
    for msg in messages:
        continuation_prefix.append(
            continuation_prefix[-1] + (1 if _is_continuation_content(msg) else 0)
        )

    # Pass 2: Sequential scan to identify pairs
    i = 0
    while i < len(messages):
        current = messages[i]

        # Skip session headers
        if current.is_session_header:
            i += 1
            continue

        # Try 3-message triple before 2-message adjacent — the triple's
        # predicate (UserSlash → Slash → CommandOutput) is strictly more
        # specific than the adjacent slash-command rules, and applying the
        # adjacent rule first would consume the first two and orphan the
        # third (the dominant `/exit`-style pattern in real transcripts).
        if i + 2 < len(messages):
            if _try_pair_triple(current, messages[i + 1], messages[i + 2]):
                i += 3
                continue

        # Try adjacent pairing (can skip next message if paired)
        if i + 1 < len(messages):
            next_msg = messages[i + 1]
            if _try_pair_adjacent(current, next_msg):
                i += 2
                continue

        # Try index-based pairing (doesn't skip, continues to next message)
        _try_pair_by_index(current, indices, positions, continuation_prefix)

        i += 1


def _relocate_subagent_blocks(
    messages: list[TemplateMessage],
) -> list[TemplateMessage]:
    """Move each subagent's content to immediately follow its trunk anchor.

    After ``_reorder_paired_messages`` brings each Task/Agent tool_use ↔
    tool_result pair adjacent, the subagent thread that conceptually
    nests under the tool_result (its sidechain entries via parentUuid)
    has been pushed to the tail of the trunk section. Without
    relocation, ``_build_message_hierarchy``'s level-stack collapses
    every subagent thread under whichever anchor sits last in render
    order — alice/bob/carol all end up as children of one tool_result.

    This pass walks the message list, identifies each subagent block by
    its synthetic ``{trunk}#agent-{agentId}`` sessionId (stamped by
    ``_integrate_agent_entries``), and re-inserts the block right after
    its spawn anchor — the Task/Agent tool_result whose
    ``meta.spawned_agent_id`` (or legacy trunk ``meta.agent_id``)
    matches. Anchors may themselves sit inside another agent's block
    (nested agents, issue #213): blocks are emitted recursively so each
    lands at its spawn position at any depth. The block keeps its
    parentUuid-derived order; only its position in the linear message
    list moves.

    Empty subagent session headers (which ``_reorder_session_template_
    messages`` leaves at the end) are excluded from blocks and stay
    where they are — the level-stack ignores them at level 0 anyway.
    """
    from .models import ToolResultMessage

    blocks: dict[str, list[TemplateMessage]] = {}
    block_ids: set[int] = set()
    for msg in messages:
        if msg.is_session_header:
            continue
        sid = msg.meta.session_id or ""
        if "#agent-" in sid:
            agent_id = sid.rsplit("#agent-", 1)[-1]
            blocks.setdefault(agent_id, []).append(msg)
            block_ids.add(id(msg))

    if not blocks:
        return messages

    result: list[TemplateMessage] = []

    def _spawned_id(msg: TemplateMessage) -> Optional[str]:
        """The agent spawned at this message, if it's a spawn anchor.

        The sidecar-resolved ``spawned_agent_id`` (issue #213) works at any
        nesting depth — an anchor INSIDE agent A's block links agent B's
        block. The fallback is the legacy trunk shape: a trunk-session
        tool_result whose ``agent_id`` is a reference backpatched from
        ``toolUseResult.agentId`` (the ``tool_name`` would normally be
        ``"Task"`` or ``"Agent"``, but the tool_factory's context-lookup
        occasionally fails to populate it — e.g. when the tool_use sits in
        a session-fork branch — so the agent_id alone decides).
        """
        if msg.meta.spawned_agent_id:
            return msg.meta.spawned_agent_id
        if (
            isinstance(msg.content, ToolResultMessage)
            and msg.meta.agent_id
            and "#agent-" not in (msg.meta.session_id or "")
        ):
            return msg.meta.agent_id
        return None

    def _emit(msg: TemplateMessage) -> None:
        """Emit a message, then any block it anchors — recursively, so a
        nested agent's block lands right after its spawn entry inside the
        parent agent's block (one frame per nesting level)."""
        result.append(msg)
        spawned = _spawned_id(msg)
        if spawned:
            block = blocks.pop(spawned, None)
            if block:
                for member in block:
                    _emit(member)

    for msg in messages:
        if id(msg) in block_ids:
            continue
        _emit(msg)

    # Defensive: emit any subagent block whose anchor we never saw, so
    # content is never silently dropped.
    for block in blocks.values():
        result.extend(block)

    return result


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
    - Level 1: User messages (including ``TeammateMessage`` — a User
      whose content is one or more ``<teammate-message>`` blocks)
    - Level 2: System commands/errors, Assistant, Thinking
    - Level 3: Tool use/result, System info/warning (nested under assistant)
    - Level 4: Sidechain user/assistant/thinking (nested under Task tool result)
    - Level 5: Sidechain tools (nested under sidechain assistant)

    Note: Sidechain user messages (duplicate of Task input prompt) and the last
    sidechain assistant (duplicate of Task output) are cleaned up from the tree
    by _cleanup_sidechain_duplicates after tree building.

    The sidechain levels here (4/5) are the DEPTH-1 block: for nested agents
    (issue #213) ``_build_message_hierarchy`` shifts a depth-``d``
    transcript's levels by ``2 * (d - 1)`` on top of this function's result.

    Returns:
        Integer hierarchy level (1-5, session headers are 0; before the
        caller's nesting-depth shift)
    """
    msg_type = msg.type
    is_sidechain = msg.is_sidechain

    # User messages at level 1 (under session), level 4 for sidechain.
    # ``"teammate"`` shares the User's level: a TeammateMessage is just
    # a User entry whose content is a stack of <teammate-message>
    # blocks (see TeammateMessage.message_type). Pre-fix, the
    # fall-through to level 1 placed sidechain Teammate prompts (the
    # team-lead's wrapped prompt to a teammate) ABOVE their spawning
    # Task tool_result, swallowing every subsequent Task tool_use as
    # a child.
    if msg_type in ("user", "teammate"):
        return 4 if is_sidechain else 1

    # Async-agent task notifications (issue #90) arrive as User
    # entries but they're status updates, not new conversation turns.
    # Treating them as level 1 makes the next assistant nest under
    # the notification (since assistant level 2 > notification level
    # 1) — wrong: the next assistant is starting a NEW turn, not
    # responding to the notification. Place them at level 3 instead
    # so they sit under the preceding assistant (which originally
    # spawned the async work) without claiming subsequent turns as
    # descendants.
    if msg_type == "task_notification":
        return 3

    # System info/warning at level 3 (tool-related, e.g., hook notifications)
    # Get level from SystemMessage if available
    system_level = msg.content.level if isinstance(msg.content, SystemMessage) else None
    if (
        msg_type == "system"
        and system_level in ("info", "warning")
        and not is_sidechain
    ):
        return 3

    # Hook flavours (HookAttachmentMessage from #128, HookSummaryMessage)
    # are out-of-band callbacks, not conversation turns. They both
    # carry msg_type == "system" but aren't SystemMessage instances,
    # so the SystemMessage-level check above doesn't fire. Sit them at
    # level 3 alongside system info — otherwise they default to level
    # 2 and claim subsequent system_info entries (e.g. ``/color`` →
    # ``Session color set to: green`` pair) as children, which both
    # mis-anchors the hook AND prevents the two related system_info
    # entries from pairing under their real parent. Out of scope: a
    # full "is this a leaf?" predicate; the stack-based hierarchy
    # only needs the level adjustment.
    if msg_type == "system" and isinstance(
        msg.content, (HookAttachmentMessage, HookSummaryMessage)
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

    Branch-headers (within-session forks) sit at fractional level 0.5 —
    between the parent session-header (0) and user messages (1) — so they
    nest under the parent session rather than restart the ancestry. This
    lets fold controls on the parent session cascade into branch content.

    Nested agents (issue #213): every message of a depth-``d`` agent
    transcript has its level shifted by ``2 * (d - 1)`` — the size of the
    per-transcript level span the depth-1 sidechain rules already use
    (assistant 4 / tools 5) — so a sub-sub-agent's entries nest under its
    spawning tool pair instead of flattening into the parent agent's
    level. Depth comes from chasing the ``spawned_agent_id`` links; agent
    sessions without one (legacy data) default to depth 1, reproducing
    the pre-#213 behavior exactly.

    Args:
        messages: List of template messages in their final order (modified in place)
    """
    # Map each agent session line to the session line that spawned it,
    # via the sidecar-resolved spawn anchors (see _relocate_subagent_blocks).
    parent_sid: dict[str, str] = {}
    for message in messages:
        spawned = message.meta.spawned_agent_id
        if not spawned:
            continue
        sid = message.meta.session_id or ""
        trunk = sid.split("#agent-", 1)[0]
        parent_sid[f"{trunk}#agent-{spawned}"] = sid

    depth_cache: dict[str, int] = {}

    def _agent_depth(sid: str) -> int:
        """Agent-nesting depth of a session line (trunk 0, direct spawn 1, …).

        Unknown parents (legacy data without spawn links) count as direct
        trunk spawns. The provisional cache entry doubles as a cycle
        breaker for corrupt linkage."""
        if "#agent-" not in sid:
            return 0
        cached = depth_cache.get(sid)
        if cached is not None:
            return cached
        depth_cache[sid] = 1
        parent = parent_sid.get(sid)
        depth = 1 + _agent_depth(parent) if parent and parent != sid else 1
        depth_cache[sid] = depth
        return depth

    # Stack of (level, message_index) tuples. Levels may be fractional for
    # within-session branch-headers; see class-level note.
    hierarchy_stack: list[tuple[float, int]] = []

    for message in messages:
        # Branch-headers sit between session (0) and user (1) so they stay
        # within their parent session's ancestry chain.
        current_level: float
        if message.is_branch_header:
            current_level = 0.5
        elif message.is_session_header:
            current_level = 0
        else:
            # Determine level from message type and modifiers, shifted by
            # the agent-nesting depth of the message's session line.
            current_level = _get_message_hierarchy_level(message)
            message.agent_depth = _agent_depth(message.meta.session_id or "")
            if message.agent_depth > 1:
                current_level += 2 * (message.agent_depth - 1)

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


def _populate_teammate_colors(ctx: RenderingContext) -> None:
    """Walk registered TemplateMessages and collect teammate colors.

    Source of truth is the ``color`` attribute on each
    ``<teammate-message>`` block (parsed by teammate_factory into
    ``TeammateMessageBlock``). First sighting of a teammate_id with a
    recognized color wins *within each session* — teammate colors are
    stable per-session, and scoping by session_id avoids
    combined-transcript cross-contamination (alice=blue in session A
    must not override alice=red in session B).
    """
    from .models import TeammateMessage

    for template_msg in _visible(ctx.messages):
        content = template_msg.content
        if not isinstance(content, TeammateMessage):
            continue
        session_id = template_msg.meta.session_id if template_msg.meta else ""
        session_colors = ctx.teammate_colors.setdefault(session_id, {})
        for block in content.blocks:
            if (
                block.teammate_id
                and block.color
                and block.teammate_id not in session_colors
            ):
                session_colors[block.teammate_id] = block.color


def _populate_task_metadata(ctx: RenderingContext) -> None:
    """Build per-session task_id → subject and tool_use_id → task_id maps.

    Sources, in priority order:
    1. ``TaskCreateOutput`` (definitive — backend-assigned id paired with
       the input subject).
    2. ``TaskListOutput`` rows (snapshot fallback — recovers subject for
       tasks created before the loaded slice or whose Create
       tool_result is missing).

    Session-scoped (mirrors ``teammate_colors``) to avoid
    combined-transcript collisions across sessions.
    """
    from .models import (
        TaskCreateOutput,
        TaskListOutput,
        ToolResultMessage,
    )

    for template_msg in _visible(ctx.messages):
        content = template_msg.content
        if not isinstance(content, ToolResultMessage):
            continue
        session_id = template_msg.meta.session_id if template_msg.meta else ""
        output = content.output
        if isinstance(output, TaskCreateOutput) and output.task_id:
            subjects = ctx.task_subjects.setdefault(session_id, {})
            if output.subject:
                subjects.setdefault(output.task_id, output.subject)
            id_map = ctx.task_id_for_tool_use.setdefault(session_id, {})
            if content.tool_use_id:
                id_map.setdefault(content.tool_use_id, output.task_id)
        elif isinstance(output, TaskListOutput):
            subjects = ctx.task_subjects.setdefault(session_id, {})
            for task in output.tasks:
                if task.id and task.subject:
                    subjects.setdefault(task.id, task.subject)


# Pattern for the agentId line that Claude Code emits on async-Task
# tool_results, e.g.::
#
#     agentId: a8b740b (internal ID - do not mention to user. ...)
_ASYNC_AGENT_ID_LINE_RE = re.compile(
    r"^\s*agentId:\s*(?P<agent_id>\w+)\s*\(",
    re.MULTILINE,
)


def _link_cron_jobs_by_id(ctx: RenderingContext) -> None:
    """Cross-link Cron* tool consumers back to the originating
    ``CronCreate`` by job id (#148).

    The harness echoes the new job's id back from ``CronCreate`` and
    re-uses it in ``CronList`` rows and ``CronDelete`` confirmations.
    Build the ``job_id → message_index`` map from the parsed output
    of every ``CronCreate`` in the transcript, then stamp the
    matching ``creating_call_message_index`` on each consumer site
    so the formatter can wrap the rendered id in an anchor pointing
    back to the originating card.

    Reuses ``ToolResultMessage`` / ``ToolUseMessage`` discovery that
    other passes already do — the only new bookkeeping is the
    job-id index. No fold or dedup; the link is purely affordance.
    """
    from .models import (
        CronCreateOutput,
        CronDeleteOutput,
        CronListOutput,
    )

    # Step 1: index CronCreate calls by job id.
    # The job id is parsed onto ``CronCreateOutput.job_id`` by the
    # tool factory; the call's ``message_index`` lives on the
    # corresponding ``ToolUseMessage`` (the call), not the
    # ``ToolResultMessage`` (the response). pair_first wires the two
    # together — the create call is the one a reader expects to
    # navigate to.
    job_id_to_call_index: dict[str, int] = {}
    for tm in _visible(ctx.messages):
        if not isinstance(tm.content, ToolResultMessage):
            continue
        if tm.content.tool_name != "CronCreate":
            continue
        if not isinstance(tm.content.output, CronCreateOutput):
            continue
        job_id = tm.content.output.job_id
        if not job_id:
            continue
        # pair_first holds the matching tool_use's message_index after
        # the standard tool_use ↔ tool_result pairing. Fall back to the
        # tool_result's own index if pairing didn't fire (defensive).
        target_idx = tm.pair_first if tm.pair_first is not None else tm.message_index
        if target_idx is not None:
            job_id_to_call_index.setdefault(job_id, target_idx)
    if not job_id_to_call_index:
        return

    # Step 2: stamp consumer sites.
    for tm in _visible(ctx.messages):
        if not isinstance(tm.content, ToolResultMessage):
            continue
        output = tm.content.output
        if isinstance(output, CronListOutput):
            for job in output.jobs:
                if job.creating_call_message_index is None:
                    target = job_id_to_call_index.get(job.id)
                    if target is not None:
                        job.creating_call_message_index = target
        elif isinstance(output, CronDeleteOutput):
            if output.creating_call_message_index is None and output.job_id:
                target = job_id_to_call_index.get(output.job_id)
                if target is not None:
                    output.creating_call_message_index = target


def _link_task_id_consumers(ctx: RenderingContext) -> None:
    """Cross-link ``TaskOutput`` / ``TaskStop`` calls and ``TaskUpdate``
    calls back to the originating tool_use that minted their task_id
    (#154; PR #158 follow-up adds ``TaskStop`` as a second background-id
    consumer and extends the pass with a forward link from the spawn
    card to its first consumer).

    Two parallel id spaces share the same shape:

    1. **Background-process ids** (alphanumeric, e.g. ``bcc00rq51``).
       Source: ``Bash`` tool_results carrying
       ``BashOutput.background_task_id`` (set when ``run_in_background=true``)
       OR ``Task`` tool_results' parsed ``agentId`` (the async-agent
       launch confirmation). Consumers: ``TaskOutputInput.task_id``
       and ``TaskStopInput.task_id`` (both poll/terminate by id).
    2. **Todo-list ids** (``"1"`` / ``"2"`` / …). Source:
       ``TaskCreate`` tool_results' ``TaskCreateOutput.task_id``.
       Consumer: ``TaskUpdateInput.taskId``.

    For each consumer with a matching id, stamp
    ``creating_call_message_index`` on the input model so the title
    formatter can wrap ``#<id>`` in an anchor pointing back to the
    spawn card. Same backlink shape as PR #142 / #147 (Monitor) and
    #148 / #152 (Cron*); no fold or dedup, just affordance.

    For the background-process id space, the pass *also* runs in the
    forward direction: the minted id is hoisted onto the spawn's input
    model (``BashInput.minted_background_task_id`` /
    ``TaskInput.minted_agent_id``) so the spawn card's title surfaces
    ``#<id>`` directly, and the first consumer's index lands on
    ``linked_consumer_message_index`` for a forward-link anchor. This
    is the first "renderer-set input field driven by tool_result data"
    pattern — see dev-docs/implementing-a-tool-renderer.md.

    Keys are ``(render_session_id, task_id)`` tuples so todo ids like
    ``#1`` don't cross-link between sessions in combined-transcripts
    renders, AND so within-session forks (which share the underlying
    ``session_id`` but each carry a distinct ``render_session_id``)
    don't cross-link between branches either (CodeRabbit on
    ff16eb3 / #158). Background ids are random alphanumeric and
    unlikely to collide across sessions in practice, but they ride
    the same shape for symmetry.
    """
    from .models import (
        BashInput,
        BashOutput,
        TaskCreateOutput,
        TaskInput,
        TaskOutputInput,
        TaskStopInput,
        TaskUpdateInput,
        ToolResultMessage,
        ToolUseMessage,
    )

    # Step 1: index originating tool_uses by (session_id, id), and
    # hoist the minted id onto the spawn's input model so the spawn
    # card's title can surface ``#<id>`` even before any consumer is
    # seen later in the transcript.
    bg_task_id_to_call_index: dict[tuple[str, str], int] = {}
    todo_task_id_to_call_index: dict[tuple[str, str], int] = {}
    for tm in _visible(ctx.messages):
        if not isinstance(tm.content, ToolResultMessage):
            continue
        # The CALL we want to link to is the tool_use, not the result —
        # readers expect the click to land on the spawn card. pair_first
        # holds the matching tool_use's message_index after standard
        # pairing; fall back to the tool_result's own index defensively.
        target_idx = tm.pair_first if tm.pair_first is not None else tm.message_index
        if target_idx is None:
            continue
        session_key = tm.render_session_id or ""
        output = tm.content.output
        # Background-process ids — Bash structured field OR async-agent
        # launch confirmation (recovered by the existing helper).
        bg_id: Optional[str] = None
        if isinstance(output, BashOutput) and output.background_task_id:
            bg_id = output.background_task_id
        elif tm.content.tool_name in ("Task", "Agent") or (
            isinstance(output, TaskOutput) or tm.meta.agent_id
        ):
            # Only call ``_async_agent_id_from_tool_result`` when the
            # tool_result actually represents a Task/Agent spawn (or has
            # a strong async-agent signal — parsed ``TaskOutput`` output
            # or an ``agent_id`` already tagged on the entry's meta).
            # Without this gate the helper's raw-text ``agentId:`` regex
            # fallback would index unrelated tool_results that happen to
            # mention "agentId:" in their text, mis-stamping
            # ``creating_call_message_index`` / forward-link slots
            # (CodeRabbit on 5baac35; mirrors the existing gate in
            # ``_link_async_notifications``).
            bg_id = _async_agent_id_from_tool_result(tm.content)
        if bg_id is not None:
            bg_task_id_to_call_index.setdefault((session_key, bg_id), target_idx)
            # Forward hoist: stamp the minted id on the spawn's input.
            spawn_tm = ctx.get(target_idx)
            if spawn_tm is not None and isinstance(spawn_tm.content, ToolUseMessage):
                spawn_input = spawn_tm.content.input
                if (
                    isinstance(spawn_input, BashInput)
                    and spawn_input.minted_background_task_id is None
                ):
                    spawn_input.minted_background_task_id = bg_id
                elif (
                    isinstance(spawn_input, TaskInput)
                    and spawn_input.minted_agent_id is None
                ):
                    spawn_input.minted_agent_id = bg_id
        # Todo-list ids — TaskCreate's backend-assigned id.
        if isinstance(output, TaskCreateOutput) and output.task_id:
            todo_task_id_to_call_index.setdefault(
                (session_key, output.task_id), target_idx
            )

    if not bg_task_id_to_call_index and not todo_task_id_to_call_index:
        return

    # Step 2: stamp consumer call sites — look up within the same session
    # — and record the first consumer's index per (session, bg_id) for
    # the forward-link direction.
    bg_task_id_to_first_consumer: dict[tuple[str, str], int] = {}
    for tm in _visible(ctx.messages):
        if not isinstance(tm.content, ToolUseMessage):
            continue
        session_key = tm.render_session_id or ""
        input_model = tm.content.input
        if isinstance(input_model, (TaskOutputInput, TaskStopInput)):
            if input_model.task_id:
                key = (session_key, input_model.task_id)
                if input_model.creating_call_message_index is None:
                    target = bg_task_id_to_call_index.get(key)
                    if target is not None:
                        input_model.creating_call_message_index = target
                # Forward record: first consumer wins (document order).
                if tm.message_index is not None:
                    bg_task_id_to_first_consumer.setdefault(key, tm.message_index)
        elif isinstance(input_model, TaskUpdateInput):
            if input_model.creating_call_message_index is None and input_model.taskId:
                target = todo_task_id_to_call_index.get(
                    (session_key, input_model.taskId)
                )
                if target is not None:
                    input_model.creating_call_message_index = target

    # Step 3: stamp the spawn's ``linked_consumer_message_index`` so the
    # spawn card's ``#<id>`` becomes a forward anchor to the first
    # consumer. Only background-process ids carry the forward link;
    # todo-ids don't (TaskCreate's body already shows the subject).
    for key, consumer_idx in bg_task_id_to_first_consumer.items():
        call_idx = bg_task_id_to_call_index.get(key)
        if call_idx is None:
            continue
        spawn_tm = ctx.get(call_idx)
        if spawn_tm is None or not isinstance(spawn_tm.content, ToolUseMessage):
            continue
        spawn_input = spawn_tm.content.input
        if (
            isinstance(spawn_input, BashInput)
            and spawn_input.linked_consumer_message_index is None
        ):
            spawn_input.linked_consumer_message_index = consumer_idx
        elif (
            isinstance(spawn_input, TaskInput)
            and spawn_input.linked_consumer_message_index is None
        ):
            spawn_input.linked_consumer_message_index = consumer_idx


def _link_tool_use_notifications(ctx: RenderingContext) -> None:
    """Link ``<task-notification>`` entries that carry ``<tool-use-id>``
    back to the originating tool_use card (#142 — built-in ``Monitor``).

    Distinct from ``_link_async_notifications``: the agent-spawn flow
    matches by ``agent_id`` and folds the duplicated answer onto the
    spawning Task; this pass matches by ``tool_use_id`` directly and
    only sets the backlink anchor — no fold, no dedup. The Monitor
    notification's ``<result>`` field is empty by design (it carries
    a single status summary, not a duplicate body), so there's nothing
    to dedup against the original tool_use's result.

    Reuses ``spawning_task_message_index`` as the backlink slot to
    keep the formatter logic single-shape — the field name reads as
    "the linked tool_use card's index" in either flow.
    """
    # Build tool_use_id → message_index map across all messages.
    tool_use_index: dict[str, int] = {}
    for tm in _visible(ctx.messages):
        if tm.type == "tool_use" and tm.tool_use_id and tm.message_index is not None:
            # First occurrence wins — multiple tool_uses sharing the same
            # tool_use_id is malformed input; the backlink target should
            # be the earliest, matching reading order.
            tool_use_index.setdefault(tm.tool_use_id, tm.message_index)
    if not tool_use_index:
        return

    for tm in _visible(ctx.messages):
        content = tm.content
        if not isinstance(content, TaskNotificationMessage):
            continue
        if content.spawning_task_message_index is not None:
            # Already linked by ``_link_async_notifications`` — don't
            # overwrite (the agent-spawn link is a stronger signal).
            continue
        if not content.tool_use_id:
            continue
        target_idx = tool_use_index.get(content.tool_use_id)
        if target_idx is not None:
            content.spawning_task_message_index = target_idx


_WF_TASK_ID_RE = re.compile(r"Task ID:\s*(\S+)")


def _result_text_for_taskid(output: Any) -> str:
    """Best-effort plain text of a tool_result output for taskId extraction."""
    for attr in ("content", "result"):
        value = getattr(output, attr, None)
        if isinstance(value, str):
            return value
    return ""


def _link_workflow_runs(
    ctx: RenderingContext,
    workflow_runs: "dict[str, WorkflowRun]",
    links: "Optional[dict[str, WorkflowRun]]" = None,
) -> None:
    """Link each parsed WorkflowRun to its Workflow tool_use by taskId (#174 PR3).

    Two paths:

    1. **Preferred** — a precomputed ``{tool_use_id: WorkflowRun}`` map built at
       full-session scope (``SessionTree.workflow_links``, via
       :func:`workflow.map_workflow_runs_by_tool_use`). Resolved BEFORE
       pagination, it links a Workflow tool_use to its run even when the
       tool_use and its tool_result land on different pages — and it's how
       single-file rendering links too.
    2. **Fallback** — when no map is supplied (e.g. a direct
       ``generate_template_messages`` call): scan this render's tool_results for
       ``Task ID: <taskId>`` (the runId lives only in the dropped
       ``toolUseResult``) and match to ``WorkflowRun.task_id``. Works when the
       tool_use and its tool_result share this ``ctx.messages`` (no pagination).

    Either way the run is stashed on the tool_use's ``WorkflowToolInput``,
    enabling the snapshot-first meta header and the phase/agent tree splice.
    """
    if links:
        for tm in _visible(ctx.messages):
            content = tm.content
            if (
                isinstance(content, ToolUseMessage)
                and content.tool_name == "Workflow"
                and isinstance(content.input, WorkflowToolInput)
                and content.tool_use_id
            ):
                run = links.get(content.tool_use_id)
                if run is not None:
                    content.input.workflow_run = run
        return
    if not workflow_runs:
        return
    runs_by_task = {r.task_id: r for r in workflow_runs.values() if r.task_id}
    if not runs_by_task:
        return
    inputs_by_tool_use_id: dict[str, WorkflowToolInput] = {}
    for tm in _visible(ctx.messages):
        content = tm.content
        if (
            isinstance(content, ToolUseMessage)
            and content.tool_name == "Workflow"
            and isinstance(content.input, WorkflowToolInput)
            and content.tool_use_id
        ):
            inputs_by_tool_use_id[content.tool_use_id] = content.input
    if not inputs_by_tool_use_id:
        return
    for tm in _visible(ctx.messages):
        content = tm.content
        if not (
            isinstance(content, ToolResultMessage) and content.tool_name == "Workflow"
        ):
            continue
        wf_input = inputs_by_tool_use_id.get(content.tool_use_id)
        if wf_input is None:
            continue
        match = _WF_TASK_ID_RE.search(_result_text_for_taskid(content.output))
        if match:
            run = runs_by_task.get(match.group(1))
            if run is not None:
                wf_input.workflow_run = run


def _splice_workflow_runs(ctx: RenderingContext) -> None:
    """Splice each linked ``WorkflowRun`` as a sub-tree under its Workflow
    tool_use node — phases → agents → each agent's side-channel transcript
    (#174 PR3, step 3).

    Strategy B: a self-contained sub-tree built *after* ``_build_message_tree``
    and attached via ``.children`` (the render walks recurse children, so no
    ancestry rebuild is needed). Runs LAST in ``generate_template_messages``:
    it appends synthetic + grafted nodes through ``ctx.register`` — an
    inherently session-wide monotonic index allocator (``len(ctx.messages)``)
    that keeps indices collision-free across several / concurrent workflows in
    one session — so it must follow every pass that iterates ``ctx.messages``.
    """
    hosts: list[tuple[TemplateMessage, Any]] = []
    for tm in _visible(ctx.messages):
        content = tm.content
        if (
            isinstance(content, ToolUseMessage)
            and content.tool_name == "Workflow"
            and isinstance(content.input, WorkflowToolInput)
            and content.input.workflow_run is not None
        ):
            hosts.append((tm, content.input.workflow_run))
    # Register AFTER collecting hosts — registering mutates ctx.messages.
    for host, run in hosts:
        _splice_one_workflow_run(ctx, host, run)


def _splice_one_workflow_run(
    ctx: RenderingContext, host: TemplateMessage, run: Any
) -> None:
    """Build + attach one run's phase/agent sub-tree at ``host`` (the
    Workflow tool_use node).

    The sub-tree is attached to the PAIRED tool_result when present (falling
    back to the tool_use for a still-running workflow with no result yet):
    the tool_use/tool_result pair renders as one visually joined unit
    (``pair_first`` flat bottom + ``pair_last`` flat top), so hanging the
    run tree off the tool_use would wedge it *between* the two cards and
    break the pair's outline. Off the result, the pair stays adjacent and
    the tree reads as the run's outcome below it.
    """
    if host.message_index is None:
        return
    attach = host
    if host.pair_last is not None:
        partner = ctx.get(host.pair_last)
        if partner is not None:
            attach = partner
    # Phase grouping when the snapshot supplied phases; else a flat agent list
    # directly under the attach node (running / no-snapshot view).
    if getattr(run, "has_snapshot", False) and run.phases:
        groups: list[tuple[Any, list[Any]]] = [
            (phase, list(phase.agents)) for phase in run.phases
        ]
    else:
        groups = [(None, list(run.agents))]

    spliced_top: list[TemplateMessage] = []
    phase_anchor_indices: list[int] = []
    for phase, agents in groups:
        if phase is not None:
            phase_tm = _new_synthetic_node(
                ctx,
                WorkflowPhaseMessage(
                    meta=MessageMeta.empty(),
                    title=phase.title,
                    detail=phase.detail,
                    agent_count=len(agents),
                ),
                parent=attach,
            )
            spliced_top.append(phase_tm)
            if phase_tm.message_index is not None:
                phase_anchor_indices.append(phase_tm.message_index)
            agent_parent: Optional[TemplateMessage] = phase_tm
        else:
            agent_parent = None

        agent_nodes: list[TemplateMessage] = []
        for agent in agents:
            base = agent_parent if agent_parent is not None else attach
            agent_tm = _new_synthetic_node(
                ctx,
                WorkflowAgentMessage(
                    meta=MessageMeta.empty(),
                    label=agent.label or agent.agent_id,
                    model=agent.model,
                    state=agent.state,
                    tokens=agent.tokens,
                    tool_calls=agent.tool_calls,
                    result=agent.result,
                    result_preview=agent.result_preview,
                ),
                parent=base,
            )
            _graft_agent_sidechannel(ctx, agent_tm, agent.entries)
            agent_nodes.append(agent_tm)

        if agent_parent is not None:
            agent_parent.children = agent_nodes
        else:
            spliced_top.extend(agent_nodes)

    attach.children = list(attach.children) + spliced_top
    _recount_spliced_children(ctx, attach, spliced_top)

    # Let the tool-input formatter link each phase pill to its phase card
    # (#msg-d-{N} anchors; the hashchange handler unfolds the target).
    # Only set when the splice grouped by snapshot phases — the pill list in
    # the header comes from the same snapshot source, so order matches.
    if phase_anchor_indices and isinstance(host.content, ToolUseMessage):
        wf_input = host.content.input
        if isinstance(wf_input, WorkflowToolInput):
            wf_input.phase_anchor_indices = phase_anchor_indices


def _new_synthetic_node(
    ctx: RenderingContext, content: "MessageContent", *, parent: TemplateMessage
) -> TemplateMessage:
    """Register a synthetic workflow node (phase/agent) and set its ancestry
    from ``parent``. Index allocation is via ``ctx.register`` (monotonic)."""
    tm = TemplateMessage(content)
    ctx.register(tm)
    if parent.message_index is not None:
        tm.ancestry = list(parent.ancestry) + [parent.message_index]
    return tm


def _graft_agent_sidechannel(
    ctx: RenderingContext,
    agent_tm: TemplateMessage,
    entries: "list[TranscriptEntry]",
) -> None:
    """Render an agent's side-channel transcript and graft it under ``agent_tm``.

    Re-renders ``entries`` through the normal pipeline (its own
    ``RenderingContext``), then re-registers every produced node into the MAIN
    ctx so each ``message_index`` is unique + monotonic, and remaps pairing
    references (``pair_first``/``pair_middle``/``pair_last``) into the new index
    space so markdown pairing (which resolves partners via the main ctx) still
    works. Jump-to-call backlinks computed inside the sub-context are not
    remapped — a best-effort limitation; workflow agent transcripts are
    typically simple read-heavy chains.
    """
    if not entries:
        return
    # The side-channel is rendered at the default FULL detail regardless of the
    # main render's detail level (the splice only fires at FULL/HIGH anyway —
    # the Workflow tool_use host is dropped at LOW and below). So an agent's
    # transcript may carry FULL-only content (e.g. system/hook entries) even at
    # ``--detail high`` (monk PR3 review N2). Acceptable: the side-channel is an
    # opt-in deep-dive under a fold.
    sub_roots, _sub_nav, _sub_ctx = generate_template_messages(entries)
    old_to_new: dict[int, int] = {}

    def _reindex(node: TemplateMessage, parent: TemplateMessage) -> None:
        old = node.message_index
        ctx.register(node)
        node.in_workflow_sidechannel = True
        if old is not None and node.message_index is not None:
            old_to_new[old] = node.message_index
        if parent.message_index is not None:
            node.ancestry = list(parent.ancestry) + [parent.message_index]
        for child in node.children:
            _reindex(child, node)

    grafted: list[TemplateMessage] = []
    for root in sub_roots:
        if root.is_session_header:
            # Defensive: agent transcripts don't emit a session header, but if
            # one appears, graft its children rather than the header chrome.
            for child in list(root.children):
                _reindex(child, agent_tm)
                grafted.append(child)
        else:
            _reindex(root, agent_tm)
            grafted.append(root)

    def _remap_pairs(node: TemplateMessage) -> None:
        for attr in ("pair_first", "pair_middle", "pair_last"):
            old = getattr(node, attr)
            if old is not None and old in old_to_new:
                setattr(node, attr, old_to_new[old])
        for child in node.children:
            _remap_pairs(child)

    for node in grafted:
        _remap_pairs(node)
    agent_tm.children = grafted


def _recount_spliced_children(
    ctx: RenderingContext,
    host: TemplateMessage,
    new_children: list[TemplateMessage],
) -> None:
    """Set descendant counts over each newly-spliced subtree, then add their
    contribution to ``host`` and propagate it up ``host``'s existing ancestors.

    Counts are *incremented* on the host (not reset), so this is correct even
    if the host already had tree children. Within the workflow subtree every
    child is counted (the pairing-skip nuance of
    :func:`_mark_messages_with_children` is dropped — these are fold-control
    label hints inside the run, where the simpler convention is fine)."""

    def _counts(node: TemplateMessage) -> None:
        node.immediate_children_count = 0
        node.total_descendants_count = 0
        node.immediate_children_by_type = {}
        node.total_descendants_by_type = {}
        for child in node.children:
            _counts(child)
            child_type = child.type
            node.immediate_children_count += 1
            node.immediate_children_by_type[child_type] = (
                node.immediate_children_by_type.get(child_type, 0) + 1
            )
            node.total_descendants_count += 1 + child.total_descendants_count
            node.total_descendants_by_type[child_type] = (
                node.total_descendants_by_type.get(child_type, 0) + 1
            )
            for sub_type, sub_count in child.total_descendants_by_type.items():
                node.total_descendants_by_type[sub_type] = (
                    node.total_descendants_by_type.get(sub_type, 0) + sub_count
                )

    added_total = 0
    added_by_type: dict[str, int] = {}
    for child in new_children:
        _counts(child)
        child_type = child.type
        host.immediate_children_count += 1
        host.immediate_children_by_type[child_type] = (
            host.immediate_children_by_type.get(child_type, 0) + 1
        )
        contribution = 1 + child.total_descendants_count
        added_total += contribution
        added_by_type[child_type] = added_by_type.get(child_type, 0) + 1
        for sub_type, sub_count in child.total_descendants_by_type.items():
            added_by_type[sub_type] = added_by_type.get(sub_type, 0) + sub_count

    host.total_descendants_count += added_total
    for sub_type, sub_count in added_by_type.items():
        host.total_descendants_by_type[sub_type] = (
            host.total_descendants_by_type.get(sub_type, 0) + sub_count
        )
    # Propagate the added descendants up the host's existing ancestors so their
    # fold-control labels stay accurate.
    for ancestor_index in host.ancestry:
        ancestor = ctx.get(ancestor_index)
        if ancestor is None:
            continue
        ancestor.total_descendants_count += added_total
        for sub_type, sub_count in added_by_type.items():
            ancestor.total_descendants_by_type[sub_type] = (
                ancestor.total_descendants_by_type.get(sub_type, 0) + sub_count
            )


def _link_async_notifications(
    ctx: RenderingContext, detail: DetailLevel = DetailLevel.FULL
) -> None:
    """Stitch the async-agent flow into a single coherent rendering
    (issue #90).

    Async-agent flow:

    1. Assistant emits ``Task`` tool_use with ``run_in_background=True``.
    2. Tool_result body says "Async agent launched successfully" + an
       ``agentId: <id>`` line.
    3. Sidechain entries from ``subagents/agent-<id>.jsonl`` get
       relocated under that tool_result by ``_relocate_subagent_blocks``.
       The last sub-assistant carries the agent's actual answer.
    4. Some time later, Claude Code injects a User entry with a
       ``<task-notification>`` whose ``<result>`` body duplicates that
       same answer.

    Without stitching, the agent's answer is buried at the tail of the
    sidechain and duplicated again much later in the notification
    card. This pass:

    - Folds the agent's final answer into the spawning Task's
      ``TaskOutput.async_final_answer`` so ``format_task_output``
      renders it as a "Result" section right under the spawn.
    - Removes the matching last sub-assistant from the sidechain tree
      (similar to ``_cleanup_sidechain_duplicates`` for sync Tasks)
      so the answer doesn't appear twice.
    - Wires ``spawning_task_message_index`` on the notification so
      its card carries a backlink anchor to the spawn, then flags
      ``result_is_duplicate`` so the formatter collapses the
      duplicated body.

    Three views — spawn / sidechain / notification — converge on a
    single visible copy of the answer at the spawn, with a sidechain
    that shows the agent's *work* (not its final summary), and a
    notification reduced to a navigation card.

    The pass splits in two so it stays correct at every detail level:

    - **Spawn-fold (FULL/HIGH/LOW):** when a notification's
      ``task_id`` matches a Task/Agent tool_result's ``agent_id``,
      fold the notification's ``result_text`` onto the tool_result's
      ``TaskOutput.async_final_answer`` and flag the notification
      ``result_is_duplicate`` (so its card collapses to a backlink).
      The notification body is the canonical source of the agent's
      answer; pairing by ``agent_id`` is enough — sidechain text
      doesn't need to match.
    - **Sidechain-only dedup:** when the last sub-assistant text
      matches the notification's ``result_text``, drop it from the
      tree. This branch is a no-op at LOW/MINIMAL/USER_ONLY where
      ``_ghost_template_by_detail`` has ghosted the sidechain entries
      (so they fall out of the rendered tree) — and that's fine,
      because there's no duplicate left to remove.

    At MINIMAL/USER_ONLY the spawn fold is skipped entirely: the
    Task tool_result is dropped by ``_ghost_template_by_detail``,
    so there's nothing to fold onto. We leave the notification card
    intact so the agent's answer remains visible somewhere — the
    notification body becomes the only surviving copy.
    """
    spawn_target_kept = detail not in (DetailLevel.MINIMAL, DetailLevel.USER_ONLY)
    # Index notifications by task_id so we can find them in O(1).
    notifications: dict[str, TaskNotificationMessage] = {}
    for tm in _visible(ctx.messages):
        if isinstance(tm.content, TaskNotificationMessage) and tm.content.task_id:
            notifications.setdefault(tm.content.task_id, tm.content)
    if not notifications:
        return

    # Walk every tool_result, find the async-agent's id, link the
    # notification, and (when the sidechain is present) drop its
    # duplicate tail. We don't gate on ``tool_name == "Task"|"Agent"``
    # up front because that field comes from pair-id, which can leave
    # a tool_result orphaned in fork/branch shapes where the spawning
    # tool_use sits in a different branch — yet the tool_result still
    # carries the canonical ``agentId:`` line, so
    # ``_async_agent_id_from_tool_result`` can recover the link. After
    # the agent-id matches, gate the non-Task/Agent path on a stronger
    # signal — a parsed ``TaskOutput`` output or an ``agentId`` already
    # tagged on the entry's meta — so an unrelated tool_result that
    # happens to mention "agentId:" in its raw text doesn't hijack a
    # notification meant for a real spawn.
    for tm in _visible(ctx.messages):
        content = tm.content
        if not isinstance(content, ToolResultMessage):
            continue
        agent_id = _async_agent_id_from_tool_result(content)
        if agent_id is None:
            continue
        if content.tool_name not in ("Task", "Agent") and not (
            isinstance(content.output, TaskOutput) or tm.meta.agent_id
        ):
            continue
        notification = notifications.get(agent_id)
        if notification is None:
            continue
        if not notification.result_text:
            continue

        # ---- Branch 1: spawn-fold from the notification --------------
        # Wire the backlink anchor on the notification: prefer the
        # spawning tool_use (where the reader expects the spawn to
        # live in the rendered transcript). pair_first holds that
        # index when the pair was matched. Set this even at
        # MINIMAL/USER_ONLY — when the spawn is filtered the index is
        # harmless, and at LOW it lets us link back to the surviving
        # tool_use card.
        spawn_idx = tm.pair_first if tm.pair_first is not None else tm.message_index
        if spawn_idx is not None:
            notification.spawning_task_message_index = spawn_idx

        # Skip the actual fold when the spawning Task tool_result will
        # be dropped post-render — without a target, the fold has no
        # place to land and the notification body becomes the only
        # surviving copy of the agent's answer. Same logic when the
        # output isn't a parsed ``TaskOutput`` (path 3 of
        # ``_async_agent_id_from_tool_result`` matches via raw-text
        # regex on shapes the parser couldn't structure): there's no
        # ``async_final_answer`` field to write into, so suppressing
        # the notification body would silently lose the answer.
        if spawn_target_kept and isinstance(content.output, TaskOutput):
            content.output.async_final_answer = notification.result_text
            notification.result_is_duplicate = True

        # ---- Branch 2: sidechain-only dedup --------------------------
        # When the last sub-assistant text matches the notification's
        # result body, drop the duplicate from the sidechain tree so
        # the answer only appears once (folded into the spawn). This
        # branch is the only piece that needs the sidechain — at
        # LOW/MINIMAL/USER_ONLY ``_ghost_template_by_detail`` ghosts the
        # sidechain entries (excluded from the tree), so
        # ``_last_sidechain_assistant`` returns None and we skip this branch.
        located = _last_sidechain_assistant(tm)
        if located is None:
            continue
        last_msg, parent, idx = located
        last_text = _assistant_text(last_msg)
        if not last_text:
            continue
        if _normalize_for_dedup(last_text) != _normalize_for_dedup(
            notification.result_text
        ):
            continue
        if 0 <= idx < len(parent.children) and parent.children[idx] is last_msg:
            del parent.children[idx]


def _async_agent_id_from_tool_result(content: ToolResultMessage) -> Optional[str]:
    """Return the async-agent ``agent_id`` of a Task/Agent tool_result, if any.

    Three sources, in order:

    1. ``TaskOutput.metadata.agent_id`` — ``parse_agent_result_metadata``
       extracts the ``agentId: <id>`` line from any Task tool_result
       tail; the async-agent flow always emits one.
    2. ``TaskOutput.agent_id`` — set by the teammates pathway.
    3. Fallback regex on the raw output text — covers older transcripts
       or shapes the parser hasn't fully captured.
    """
    output = content.output
    if isinstance(output, TaskOutput):
        if output.metadata is not None and output.metadata.agent_id:
            return output.metadata.agent_id
        if output.agent_id:
            return output.agent_id
    raw = _tool_result_raw_text(content)
    if not raw:
        return None
    match = _ASYNC_AGENT_ID_LINE_RE.search(raw)
    return match.group("agent_id") if match else None


def _tool_result_raw_text(content: ToolResultMessage) -> str:
    """Best-effort string body of a ToolResultMessage's parsed output.

    Most paths set ``raw_text`` on the parsed dataclass; the
    fully-generic ``ToolResultContent`` keeps the original ``content``
    field instead. Tries both so the agentId line can be located
    regardless of which parser path the tool_result took.
    """
    output = content.output
    raw = getattr(output, "raw_text", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(output, ToolResultContent):
        if isinstance(output.content, str):
            return output.content
        # list[dict] shape — pull text items out
        return "\n".join(
            str(item.get("text", ""))
            for item in output.content
            if item.get("type") == "text"
        )
    return ""


def _last_sidechain_assistant(
    message: TemplateMessage,
) -> Optional[tuple[TemplateMessage, TemplateMessage, int]]:
    """Find the last sidechain ``AssistantTextMessage`` descendant of
    *message* in document order.

    Returns ``(msg, parent, index_in_parent)`` so the caller can both
    inspect the message's text AND remove it from its parent's
    children — used by ``_link_async_notifications`` to fold the
    agent's final answer into the spawning Task and drop the
    duplicate from the sidechain.

    Walks the tree depth-first, scanning each node's direct children
    for a candidate so the (parent, index) pair stays available
    without threading auxiliary state through the stack.
    """
    last: Optional[tuple[TemplateMessage, TemplateMessage, int]] = None
    stack: list[TemplateMessage] = [message]
    while stack:
        current = stack.pop()
        for idx, child in enumerate(current.children):
            if child.is_sidechain and isinstance(child.content, AssistantTextMessage):
                last = (child, current, idx)
        # Push children REVERSED so popping yields document-order
        # traversal — the naive ``extend(children)`` reversed it and
        # returned the FIRST sidechain assistant rather than the LAST.
        stack.extend(reversed(current.children))
    return last


def _assistant_text(message: TemplateMessage) -> str:
    """Concatenate all ``TextContent`` items from an
    ``AssistantTextMessage``; ``""`` for non-assistant content.
    """
    if not isinstance(message.content, AssistantTextMessage):
        return ""
    return "\n".join(
        item.text for item in message.content.items if isinstance(item, TextContent)
    )


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

        # Check if this is a Task/Agent tool_use or tool_result with sidechain
        # children. ``Agent`` is the teammates-feature spawn tool name; same
        # subagent dedup semantics as ``Task``.
        _spawn_tool_names = {"Task", "Agent"}
        is_task_tool_use = (
            message.type == "tool_use"
            and isinstance(message.content, ToolUseMessage)
            and message.content.tool_name in _spawn_tool_names
        )
        is_task_tool_result = (
            message.type == "tool_result"
            and isinstance(message.content, ToolResultMessage)
            and message.content.tool_name in _spawn_tool_names
        )

        if not ((is_task_tool_use or is_task_tool_result) and message.children):
            return

        children = message.children

        # Remove the first sidechain UserTextMessage child (duplicate of the
        # Task/Agent input prompt). Scan the full children list rather than
        # just position 0: under parallel-Task spawning, the parent
        # tool_use's first DAG child is the next sibling tool_use (per
        # parentUuid chain), so the sidechain user appears later in the
        # children list.
        for sidechain_idx, child in enumerate(children):
            if child.is_sidechain and isinstance(child.content, UserTextMessage):
                removed = children.pop(sidechain_idx)
                # Adopt orphaned children (tool_use/tool_result from sidechain)
                # at the same position so the sidechain content threads in
                # the right place.
                if removed.children:
                    children[sidechain_idx:sidechain_idx] = removed.children
                break

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

        # Fully-collapsed nested spawn (#213 visual layer): the sub-agent's
        # whole transcript was just prompt → answer (both already shown), so
        # dedup emptied it. Mark it — but only for a NESTED spawn (the result
        # card itself sits inside an agent transcript, ``agent_depth >= 1``);
        # trunk-level direct sub-agents keep their pre-#213 rendering. The
        # marker distinguishes "transcript identical to result" from "spawn
        # with no transcript at all", which otherwise look the same.
        if not children and message.agent_depth >= 1:
            message.spawns_collapsed_transcript = True

    for root in root_messages:
        process_message(root)


# -- Message Reordering -------------------------------------------------------


def _reorder_session_template_messages(
    messages: list[Optional[TemplateMessage]],
) -> list[TemplateMessage]:
    """Reorder template messages to group all messages under their correct session headers.

    When a user resumes session A into session B, Claude Code copies messages from
    session A into session B's JSONL file (keeping their original sessionId). After
    global chronological sorting, these copied messages get interleaved. This function
    fixes that by grouping all messages by session_id and inserting them after their
    corresponding session header.

    This must be called BEFORE _identify_message_pairs and _reorder_paired_messages,
    since those functions expect messages to be in session-grouped order.

    Accepts ghost-aware input (None slots); ghosts are silently dropped
    from the returned list, so downstream passes that consume this
    function's output don't need ghost-awareness.

    Args:
        messages: Template messages (including session headers); may contain None ghost slots.

    Returns:
        Reordered messages with all messages grouped under their session headers
    """
    # First pass: extract session headers and group non-header messages by session_id
    session_headers: list[TemplateMessage] = []
    session_messages_map: dict[str, list[TemplateMessage]] = {}

    for message in messages:
        if message is None:
            continue
        if message.is_session_header:
            session_headers.append(message)
            # Initialize the list for this session (preserves session order)
            sid = message.render_session_id
            if sid and sid not in session_messages_map:
                session_messages_map[sid] = []
        else:
            sid = message.render_session_id
            if sid:
                if sid not in session_messages_map:
                    session_messages_map[sid] = []
                session_messages_map[sid].append(message)

    # If no session headers, return original order — but materialise
    # the non-ghost subset so the caller sees ``list[TemplateMessage]``
    # (callers downstream of this function are typed against the
    # non-Optional shape and shouldn't have to ghost-skip).
    if not session_headers:
        return list(_visible(messages))

    # Second pass: for each session header, insert all messages with that session_id
    result: list[TemplateMessage] = []
    used_sessions: set[str] = set()

    for header in session_headers:
        result.append(header)
        sid = header.render_session_id

        if sid and sid in session_messages_map:
            # Messages are already in timestamp order from original processing
            result.extend(session_messages_map[sid])
            used_sessions.add(sid)

    # Append any messages that weren't matched to a session header (shouldn't happen normally)
    for sid, msgs in session_messages_map.items():
        if sid not in used_sessions:
            result.extend(msgs)

    return result


def _queue_op_content_as_list(
    content: Optional[list[ContentItem] | str],
) -> list[ContentItem]:
    """Normalise `QueueOperationTranscriptEntry.content` to a ContentItem list.

    The Pydantic model allows `content` to be a plain string (raw
    steering text) or a list of content items. Several filter passes
    reason about the content as a uniform list, so wrap a non-empty
    string in a single `TextContent` and fall through to `[]` for
    None / empty / other shapes.
    """
    if isinstance(content, list):
        return content
    if isinstance(content, str) and content.strip():
        return [TextContent(type="text", text=content)]
    return []


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

        # Skip ai-title entries (folded into session metadata, not rendered)
        if isinstance(message, AiTitleTranscriptEntry):
            continue

        # Skip passthrough entries (structural DAG nodes, not rendered)
        if isinstance(message, PassthroughTranscriptEntry):
            continue

        # Attachment entries (issue #128): include — pass-2 walker will
        # call ``create_attachment_message`` to surface hook payloads at
        # full detail. Non-hook attachment flavours produce ``None`` and
        # are silently dropped at registration time, mirroring the
        # pre-#128 PassthroughTranscriptEntry behaviour. Detail-level
        # filtering happens later: ``_ghost_template_by_detail`` drops
        # ``HookAttachmentMessage`` at HIGH and below.
        if isinstance(message, AttachmentTranscriptEntry):
            filtered.append(message)
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
            message_content = _queue_op_content_as_list(message.content)
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


# -- Detail-level filtering ---------------------------------------------------
#
# Pre-render: strip content items from TranscriptEntry based on detail level.
# Post-render: remove TemplateMessage types created by factories from text that
# shouldn't appear at the given level (bash I/O, slash commands, etc.).

# Tool names kept at --detail low (interaction + key signals).
# ``Agent`` is the teammates-feature spawn name (aliased to TaskInput
# in the tool factory); it must be paired with ``Task`` so real
# teammate transcripts keep their spawn-and-result pairs at low detail.
_LOW_KEEP_TOOLS = {"WebSearch", "WebFetch", "Task", "Agent"}

# Per-class detail-level filtering lives on the content classes themselves
# via ``MessageContent.visible_at`` / the ``detail_visibility`` ClassVar
# (see ``models.py`` and ``dev-docs/plugins.md`` §6). The thin wrapper
# below survives only because it is called from many sites in this
# module; new code should call ``content.visible_at(detail)`` directly.


def _content_visible_at(content: "MessageContent", detail: DetailLevel) -> bool:
    """Return True iff ``content`` is visible at the given detail level.

    Thin delegate to :meth:`MessageContent.visible_at`, which reads the
    class-side ``detail_visibility`` ClassVar via the monotone-down
    ordering on :class:`DetailLevel`. Plugin classes participate
    automatically through the same predicate.
    """
    return content.visible_at(detail)


_LAUNCHING_SKILL_PREFIX = "Launching skill:"


def _is_launching_skill_payload(output: Any) -> bool:
    """Whether *output* looks like Claude Code's redundant Skill marker.

    Claude Code emits the literal ``"Launching skill: <name>"`` text for the
    tool_result that pairs with a Skill tool_use. That pair gets folded into
    the tool_use card; the tool_result is dropped. Anything else carrying the
    same tool_use_id (an error result, a repurposed payload in a malformed
    transcript) stays visible.

    Handles both string- and list-shaped ToolResultContent.content.
    """
    if not isinstance(output, ToolResultContent):
        return False
    content = output.content
    if isinstance(content, str):
        return content.lstrip().startswith(_LAUNCHING_SKILL_PREFIX)
    # Pydantic typed `content` as Union[str, list[dict[str, Any]]] — after
    # the str-check, content is the list shape. Iterate text items and
    # match the prefix on the first one that carries it.
    for item in content:
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.lstrip().startswith(_LAUNCHING_SKILL_PREFIX):
            return True
    return False


def _pair_skill_tool_uses(ctx: RenderingContext) -> None:
    """Fold the `isMeta=True` user body of a Skill invocation into its tool_use.

    Claude Code emits three separate entries for a Skill invocation:
        1. assistant `Skill` tool_use
        2. user tool_result containing the literal string "Launching skill: <name>"
        3. user `isMeta=True` entry whose `sourceToolUseID` matches (1) and whose
           text is the expanded skill body (markdown, often 100+ lines).

    Rendered as-is, (3) appears as a bare "🧑 User (slash command)" block
    visually disjoint from (1). Pair them: attach (3)'s text as
    `skill_body` on the Skill `ToolUseMessage`, drop (2) and (3) from
    `ctx.messages`, and re-index so later passes see a clean slate.

    The lookup is keyed by ``(render_session_id, source_tool_use_id)``:
    combined transcripts traverse multiple sessions and tool_use ids are
    only session-unique, so a global key risks folding the wrong body on
    a stray id collision. Tool_result removal is similarly scoped and only
    drops the canonical, non-error ``"Launching skill:"`` payload — an
    error result or a divergent payload sharing the tool_use_id stays
    visible.

    See issue #93.
    """
    # Build the lookup keyed by (render_session_id, tool_use_id) so combined
    # transcripts spanning multiple sessions can't cross-pair via stray
    # tool_use_id collisions.
    slash_by_source: dict[tuple[str, str], TemplateMessage] = {}
    for msg in _visible(ctx.messages):
        if (
            isinstance(msg.content, UserSlashCommandMessage)
            and msg.meta.source_tool_use_id
        ):
            slash_by_source[(msg.render_session_id, msg.meta.source_tool_use_id)] = msg

    if not slash_by_source:
        return

    # Index the canonical "Launching skill:" tool_results once, keyed the
    # same way as the slash bodies. tool_use_ids are session-unique, so each
    # (render_session_id, tool_use_id) maps to at most one non-error launch
    # result — this keeps folding linear instead of rescanning ctx.messages
    # per Skill. An error result or a divergent payload sharing the
    # tool_use_id is excluded here and stays visible.
    launch_result_by_source: dict[tuple[str, str], int] = {}
    for msg in _visible(ctx.messages):
        if (
            isinstance(msg.content, ToolResultMessage)
            and not msg.content.is_error
            and msg.message_index is not None
            and _is_launching_skill_payload(msg.content.output)
        ):
            launch_result_by_source.setdefault(
                (msg.render_session_id, msg.content.tool_use_id), msg.message_index
            )

    consumed_indices: set[int] = set()
    for msg in _visible(ctx.messages):
        if not (
            isinstance(msg.content, ToolUseMessage) and msg.content.tool_name == "Skill"
        ):
            continue
        slash = slash_by_source.get((msg.render_session_id, msg.content.tool_use_id))
        if slash is None or not isinstance(slash.content, UserSlashCommandMessage):
            continue
        # Fold the body into the Skill tool_use and mark the slash-command consumed.
        msg.content.skill_body = slash.content.text
        if slash.message_index is not None:
            consumed_indices.add(slash.message_index)
        # Drop the matching, redundant "Launching skill: ..." tool_result.
        launch_idx = launch_result_by_source.get(
            (msg.render_session_id, msg.content.tool_use_id)
        )
        if launch_idx is not None:
            consumed_indices.add(launch_idx)

    if not consumed_indices:
        return

    # Ghost the consumed slots in place. The TemplateMessages are
    # freed by GC; the indices around them stay stable so every
    # stored reference (parent_message_index, junction_forward_links,
    # session_first_message, pair_first/last/middle) keeps pointing
    # at the right slot — no reindex needed. Downstream iterators
    # see None at the ghosted positions and skip via ``_visible(...)``
    # or an explicit ``if m is None: continue``.
    for idx in consumed_indices:
        ctx.messages[idx] = None

    # A branch header's ``parent_message_index`` and the
    # ``session_first_message`` map are cached during ``_render_messages``
    # — *before* this pass. If a fork point happened to land on a slot we
    # just ghosted, that cached index now refers to a ``None`` slot and the
    # rendered ``#msg-d-{N}`` backlink would dangle (the anchor href is
    # emitted from the raw index; ``ctx.get()`` returning ``None`` doesn't
    # suppress it). Null those stale refs so the anchor is simply omitted.
    # ``junction_forward_links`` are populated *after* this pass
    # (``_link_junction_forwards``), so they need no repair here.
    _drop_anchor_refs_into_ghosts(ctx)


def _drop_anchor_refs_into_ghosts(ctx: RenderingContext) -> None:
    """Null anchor-target refs that now point at a ghosted (``None``) slot.

    Keeps the index-stability contract honest for the only anchor sources
    cached before ghosting runs: ``session_first_message`` and each branch
    header's ``parent_message_index``. See ``_pair_skill_tool_uses`` for
    why junction forward links are out of scope. Phase 2 of the ghosting
    epic adds a broader ``_repair_stale_anchor_refs`` for the detail-filter
    ghost path; this is the skill-fold-scoped counterpart.
    """
    ctx.session_first_message = {
        sid: idx
        for sid, idx in ctx.session_first_message.items()
        if ctx.get(idx) is not None
    }
    for msg in _visible(ctx.messages):
        if (
            isinstance(msg.content, SessionHeaderMessage)
            and msg.content.parent_message_index is not None
            and ctx.get(msg.content.parent_message_index) is None
        ):
            msg.content.parent_message_index = None


def _ghost_template_by_detail(
    ctx: RenderingContext,
    detail: DetailLevel,
    no_recaps: bool = False,
) -> None:
    """Ghost (set to None) ctx.messages slots that aren't visible at ``detail``.

    Visibility is computed by :func:`_content_visible_at`, which delegates
    to each content class's ``visible_at`` predicate / ``detail_visibility``
    ClassVar. At ``LOW``, the ``_LOW_KEEP_TOOLS`` allowlist narrows
    built-in tool messages further to a specific set of tool names. At
    ``MINIMAL`` / ``LOW`` / ``USER_ONLY`` sidechain messages are also
    dropped.

    After ghosting, anchor-target references that pointed to now-ghosted
    slots are repaired so ``#msg-d-{N}`` links don't dangle:

    - ``ctx.session_first_message[sid]`` entries pointing at a ghost are
      dropped (the session falls out of the nav).
    - ``SessionHeaderMessage.parent_message_index`` is nulled when its
      fork-point target is ghosted — the back-ref then renders as plain
      text via the existing ``is not None`` guard in
      ``html/system_formatters.py``.
    - ``junction_forward_links`` tuples whose ``branch_idx`` target is
      ghosted are dropped; if fewer than 2 navigable branches remain,
      the indicator is elided (mirrors the population pass's invariant).

    This replaces the kept-list + reindex pattern that
    ``_reindex_filtered_context`` used pre-ghosting: indices stay stable,
    only references to ghosted targets are sanitized in place.
    """
    for idx, msg in enumerate(ctx.messages):
        if msg is None:
            continue
        visible = _content_visible_at(msg.content, detail)

        # LOW keep-list: built-in ToolUseMessage / ToolResultMessage declare
        # ``detail_visibility = LOW``, so the predicate keeps them at LOW;
        # the keep-list then narrows that set to a few specific tool names
        # (Web/Task/Agent). Plugin subclasses that declare *their own*
        # ``detail_visibility`` opt out — their declared visibility is
        # authoritative, letting a plugin (e.g. clmail communicate) be
        # visible at LOW without core needing to update _LOW_KEEP_TOOLS.
        # Detection: the class introduces ``detail_visibility`` in its own
        # ``__dict__`` AND is not one of the built-in bases (which declare
        # the LOW baseline that this keep-list is designed to narrow).
        if visible and detail == DetailLevel.LOW:
            content_cls = type(msg.content)
            declares_own_visibility = (
                "detail_visibility" in content_cls.__dict__
                and content_cls not in (ToolUseMessage, ToolResultMessage)
            )
            if (
                isinstance(msg.content, (ToolUseMessage, ToolResultMessage))
                and not declares_own_visibility
            ):
                tool_name = getattr(msg.content, "tool_name", "")
                if tool_name not in _LOW_KEEP_TOOLS:
                    visible = False

        if visible and (
            detail in (DetailLevel.MINIMAL, DetailLevel.LOW, DetailLevel.USER_ONLY)
            and msg.is_sidechain
        ):
            visible = False

        # ``--no-recaps`` (#179): recaps are otherwise visible at every level
        # (AwaySummaryMessage.detail_visibility == USER_ONLY); this is the
        # explicit opt-out, applied regardless of detail level.
        if visible and no_recaps and isinstance(msg.content, AwaySummaryMessage):
            visible = False

        if not visible:
            # A fork point is navigational structure, not message content: the
            # branches it connects (session headers) always survive detail
            # filtering, so the fork point must too — otherwise the branches
            # render with no visible fork point above them and the back-links
            # have nothing to anchor to (#233 follow-up). Keep the slot as a
            # fork-only landmark (body suppressed, just the box) instead of
            # ghosting it to None. Because the slot stays non-None, the
            # back-link / nav anchor reactivate for free in
            # ``_repair_stale_anchor_refs`` (which only nulls refs to None
            # slots). The box data (``junction_forward_links`` /
            # ``fork_point_preview``) was already populated by
            # ``_link_junction_forwards``, which runs before this pass.
            #
            # Load-bearing invariant: this no-dead-anchor guarantee depends on
            # branch session headers ALWAYS surviving detail filtering
            # (``SessionHeaderMessage`` declares no ``detail_visibility`` →
            # ``visible_at`` is True at every level). If a branch could be
            # ghosted, a 2-branch fork could drop below 2 survivors →
            # ``_repair_stale_anchor_refs`` empties ``junction_forward_links``
            # → the template suppresses the box → this kept slot would render
            # with no ``id='msg-d-N'`` and a still-visible sibling branch's
            # back-link would dangle. Pinned by
            # ``test_branch_headers_always_visible_keeps_fork_anchored`` so a
            # future ``SessionHeaderMessage.detail_visibility`` trips a test
            # rather than silently activating a dead anchor (monk review note).
            if msg.junction_forward_links:
                msg.fork_only = True
            else:
                ctx.messages[idx] = None

    _repair_stale_anchor_refs(ctx)


def _repair_stale_anchor_refs(ctx: RenderingContext) -> None:
    """Null/drop anchor-target references that point at ghosted slots.

    Pure cleanup pass — call after any function that ghosts messages
    which could be the target of a stored ``#msg-d-{N}`` anchor. Cheap
    and idempotent; running it twice is a no-op.
    """
    ctx.session_first_message = {
        sid: idx
        for sid, idx in ctx.session_first_message.items()
        if ctx.get(idx) is not None
    }

    for msg in _visible(ctx.messages):
        if isinstance(msg.content, SessionHeaderMessage):
            parent_idx = msg.content.parent_message_index
            if parent_idx is not None and ctx.get(parent_idx) is None:
                msg.content.parent_message_index = None
        if msg.junction_forward_links:
            kept: list[tuple[str, Optional[int], str]] = []
            for branch_sid, branch_idx, link_suffix in msg.junction_forward_links:
                if branch_idx is not None and ctx.get(branch_idx) is None:
                    # Target message ghosted — drop the link.
                    continue
                kept.append((branch_sid, branch_idx, link_suffix))
            msg.junction_forward_links = kept
            # If the fork now has fewer than 2 navigable branches, mirror
            # the elision the population pass does and drop the indicator.
            if len(msg.junction_forward_links) < 2:
                msg.junction_forward_links = []
                msg.fork_point_preview = ""


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

        # Attachment entries (#128) carry no user/assistant content and
        # don't anchor session metadata; their session is inherited from
        # whichever real turn anchors them via parentUuid. Skip here so
        # they don't bump message_count or last_timestamp.
        if isinstance(message, AttachmentTranscriptEntry):
            continue

        # Get message content
        message_content: list[ContentItem]
        if isinstance(message, QueueOperationTranscriptEntry):
            message_content = _queue_op_content_as_list(message.content)
        else:
            # After filtering out System/Summary/Passthrough upstream in
            # _filter_messages, `message` is User/Assistant here — both
            # expose `.message.content: list[ContentItem]`. The inner
            # cast narrows the union explicitly so pyright's strict mode
            # and ty both see a clean `list[ContentItem]` on the RHS.
            message_content = cast(
                "UserTranscriptEntry | AssistantTranscriptEntry", message
            ).message.content

        text_content = extract_text_content(message_content)

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


def _build_trunk_header(
    session_id: str,
    trunk_summary: Optional[str],
    session_hierarchy: dict[str, dict[str, Any]] | None,
    session_summaries: dict[str, str] | None,
    session_team_names: dict[str, str] | None,
    ctx: RenderingContext,
) -> SessionHeaderMessage:
    """Build the SessionHeaderMessage content for a trunk session when
    first encountered. Caller is responsible for wrapping in
    ``TemplateMessage``, calling ``ctx.register(...)``, and updating
    ``ctx.session_first_message`` — that positional placement stays in
    the render loop to preserve ``#msg-d-{N}`` anchor stability.
    """
    session_title = (
        f"{trunk_summary} • {session_id[:8]}" if trunk_summary else session_id[:8]
    )
    session_header_meta = MessageMeta(
        session_id=session_id,
        timestamp="",
        uuid="",
    )
    hier = (session_hierarchy or {}).get(session_id, {})
    parent_sid = hier.get("parent_session_id")
    parent_msg_idx = ctx.session_first_message.get(parent_sid) if parent_sid else None
    return SessionHeaderMessage(
        session_header_meta,
        title=session_title,
        session_id=session_id,
        summary=trunk_summary,
        parent_session_id=parent_sid,
        parent_session_summary=(session_summaries or {}).get(parent_sid)
        if parent_sid
        else None,
        parent_message_index=parent_msg_idx,
        depth=hier.get("depth", 0),
        attachment_uuid=hier.get("attachment_uuid"),
        team_name=(session_team_names or {}).get(session_id),
    )


def _build_branch_header(
    branch_sid: str,
    message: TranscriptEntry,
    session_hierarchy: dict[str, dict[str, Any]] | None,
    session_summaries: dict[str, str] | None,
    session_team_names: dict[str, str] | None,
    uuid_to_entry: dict[str, TranscriptEntry] | None,
    ctx: RenderingContext,
) -> SessionHeaderMessage:
    """Build the SessionHeaderMessage content for a within-session
    branch (fork) when first encountered. ``message`` is the first
    entry of the branch (the trigger that opened the helper) and
    ``uuid_to_entry`` is the DAG-line uuid → entry map used to scan
    for the branch preview. Caller handles ``TemplateMessage``
    wrapping, ``render_session_id`` tagging, ``ctx.register(...)``,
    and ``ctx.session_first_message`` updates so positional placement
    (anchor stability) stays in the render loop.
    """
    b_hier = (session_hierarchy or {}).get(branch_sid, {})
    parent_sid = b_hier.get("parent_session_id")
    # Look up the fork point message index (attachment_uuid),
    # not the parent session header.
    attachment_uuid = b_hier.get("attachment_uuid")
    parent_msg_idx = None
    if attachment_uuid:
        for msg in _visible(ctx.messages):
            if msg.meta.uuid == attachment_uuid and msg.message_index is not None:
                parent_msg_idx = msg.message_index
                break
    # When the fork point itself isn't visible (e.g. a content-less system
    # node ghosted at reduced detail), leave ``parent_msg_idx`` None so the
    # branch header renders "from ⟂ Fork point" as plain text — NOT retargeted
    # at the parent session header, which produced a misleading anchor that
    # jumped to the wrong place (issue #233). At full detail the #233
    # placeholder keeps the fork point visible, so this fallback is only
    # reached in the genuinely-unresolvable case.
    original_sid = b_hier.get("original_session_id", message.sessionId)
    branch_summary = (session_summaries or {}).get(original_sid)

    # Compute the branch preview by scanning the branch's DAG-line
    # uuids for the first user entry with non-empty text. This is the
    # single source of truth for the preview — there is no post-pass
    # widening it later.
    #
    # Why scan instead of just inspecting ``message`` (the trigger)?
    # Branches sometimes start with an assistant turn ("No response
    # requested." after ``/exit``) or a tool_result, leaving the
    # trigger with no user text. The trigger-only path used to leave
    # the preview empty and a separate ``_enrich_branch_titles`` pass
    # would walk ``ctx.messages`` post-hoc to back-fill. Scanning the
    # DAG-line uuids in order achieves the same outcome in one pass.
    #
    # ``extract_text_content`` is the same helper the
    # ``UserSlashCommandMessage`` path uses, so slash-command bodies
    # like ``<command-name>/exit</command-name>...`` collapse to
    # ``/exit`` (5 chars) via ``create_session_preview`` — #129
    # precedence is preserved structurally: a slash command at the
    # branch root is the *first* user entry with text, so the scan
    # picks it before any later (longer but less informative) user
    # turn ever gets considered.
    branch_uuids: list[str] = b_hier.get("uuids") or []
    branch_preview = ""
    if uuid_to_entry:
        # Preferred: the first branch-local USER text. Scanning forward past a
        # leading assistant turn (the canonical ``/exit`` → "No response
        # requested." case) and staying bounded to ``branch_uuids`` keeps the
        # spawned-agent inner prompt out of the label (see test_branch_label_source).
        for branch_uuid in branch_uuids:
            entry = uuid_to_entry.get(branch_uuid)
            if entry is None:
                continue
            user_entry = as_user_entry(entry)
            if user_entry is None:
                continue
            branch_text = extract_text_content(user_entry.message.content)
            if branch_text:
                branch_preview = create_session_preview(branch_text)
                break
        # Fallback: a branch with no user text — an assistant continuation or
        # tool-flow branch — gets a type-aware summary of its first meaningful
        # message ("Thinking" / "Bash — <desc>") instead of a bare uuid label
        # (dev/tool-use-continuation).
        if not branch_preview:
            for branch_uuid in branch_uuids:
                entry = uuid_to_entry.get(branch_uuid)
                if entry is None:
                    continue
                summary = _entry_nav_summary(entry)
                if summary:
                    branch_preview = summary
                    break
    branch_title = _branch_label(branch_sid, branch_preview)

    branch_header_meta = MessageMeta(
        session_id=branch_sid,
        timestamp="",
        uuid="",
    )

    # Get fork point preview for backlink text
    fork_context = ""
    if attachment_uuid:
        for fmsg in _visible(ctx.messages):
            if fmsg.meta.uuid == attachment_uuid:
                fork_context = _fork_point_preview(fmsg, ctx)
                break

    # Branches inherit the team_name of the original (pre-fork) session:
    # a within-session fork doesn't change which team is active.
    _team_names = session_team_names or {}
    branch_team_name = _team_names.get(branch_sid) or _team_names.get(
        original_sid or ""
    )

    return SessionHeaderMessage(
        branch_header_meta,
        title=branch_title,
        session_id=branch_sid,
        summary=branch_summary,
        parent_session_id=parent_sid,
        parent_session_summary=fork_context or None,
        parent_message_index=parent_msg_idx,
        depth=b_hier.get("depth", 0),
        attachment_uuid=b_hier.get("attachment_uuid"),
        is_branch=True,
        original_session_id=original_sid,
        # Canonical first uuid of the branch DAG-line, NOT the
        # trigger's uuid. Post-D11 the trigger may be a later entry
        # (the first one to survive ``_filter_messages``) rather
        # than ``dag_line.uuids[0]`` itself, so reading from the
        # hierarchy keeps the header's ``first_uuid`` field pointing
        # at the canonical entry regardless of filtering.
        first_uuid=b_hier.get("first_uuid") or "",
        team_name=branch_team_name,
        preview=branch_preview or None,
    )


def _is_unrendered_within_session_fork(uuid: str, ctx: RenderingContext) -> bool:
    """True if ``uuid`` is a within-session fork point (≥1 branch target).

    A within-session branch session id carries an ``@`` (``{line}@{uuid12}``);
    cross-session continuations don't. Used at the system build site to decide
    whether a content-less, about-to-be-dropped node must be kept as an
    anchorable fork-point landmark (issue #233).
    """
    return any("@" in sid for sid in ctx.junction_targets.get(uuid, []))


def _fork_placeholder_content(message: SystemTranscriptEntry) -> SystemMessage:
    """Minimal ``SystemMessage`` landmark for a dropped fork-point node (#233).

    Reuses ``SystemMessage`` rather than a bespoke type so the placeholder
    inherits the existing CSS / emoji / timeline / filter handling, and so
    ``_fork_point_preview`` (which already walks up *past* system hosts) treats
    it as the expected fork host. The label is the raw ``(subtype)`` — honest
    about what the otherwise-invisible node is, and general across subtypes.
    ``SystemMessage.detail_visibility`` is ``FULL``, so the placeholder ghosts
    at reduced detail just like any system entry; the fork anchor then degrades
    to no-link (handled in ``_build_branch_header`` / the nav), not a wrong one.
    """
    return SystemMessage(
        level="info",
        text=f"({message.subtype})" if message.subtype else "(system)",
        meta=create_meta(message),
    )


def _render_messages(
    messages: list[TranscriptEntry],
    sessions: dict[str, dict[str, Any]],
    show_tokens_for_message: set[str],
    session_hierarchy: dict[str, dict[str, Any]] | None = None,
    session_summaries: dict[str, str] | None = None,
    session_team_names: dict[str, str] | None = None,
    junction_targets: dict[str, list[str]] | None = None,
) -> RenderingContext:
    """Pass 2: Render pre-filtered messages to TemplateMessage objects.

    This pass creates the actual TemplateMessage objects for rendering:
    - Creates session headers when entering new sessions
    - Creates branch headers at within-session fork points
    - Processes text content into HTML
    - Handles tool use, tool result, thinking, and image content
    - Collects timing statistics

    Note: Messages are pre-filtered by _collect_session_info, so no additional
    filtering is needed here except for system message processing.

    Args:
        messages: Pre-filtered list of transcript entries from _collect_session_info
        sessions: Session metadata from _collect_session_info
        show_tokens_for_message: Set of message UUIDs that should display tokens
        session_hierarchy: Optional hierarchy data from _extract_session_hierarchy()
        session_summaries: Optional session summaries for parent backlinks
        junction_targets: Optional junction target data from _extract_session_hierarchy()

    Returns:
        RenderingContext with all TemplateMessage objects registered
    """
    # Create rendering context for this operation
    ctx = RenderingContext()
    if junction_targets:
        ctx.junction_targets = junction_targets

    # uuid → render_session_id map, derived once up front from the
    # SessionTree (via ``session_hierarchy``). Single source of truth
    # for how each rendered TemplateMessage is grouped:
    #   - branch uuids → the branch sid;
    #   - agent uuids → the agent's immediate parent sid (matches the
    #     pre-D11 ``hier.get("parent_session_id")`` resolution);
    #   - trunk uuids → omitted from the map (lookup returns None,
    #     TemplateMessage.render_session_id falls back to
    #     meta.session_id at read time).
    # Replaces the pre-D11 ``current_render_session`` loop variable.
    # See ``_build_uuid_to_render_sid`` for the latent-bug fix this
    # closes at non-FULL detail.
    uuid_to_render_sid = _build_uuid_to_render_sid(session_hierarchy)

    # uuid → entry map for branch-preview scanning. ``_build_branch_header``
    # walks each branch's DAG-line uuids (from ``session_hierarchy[sid]
    # ["uuids"]``) and looks up the entries here to find the first user
    # entry with non-empty text. Built once over the filtered message
    # list; entries removed by structural filtering are simply absent
    # from the map and skipped by the scan.
    uuid_to_entry: dict[str, TranscriptEntry] = {}
    for entry in messages:
        entry_uuid = getattr(entry, "uuid", "")
        if entry_uuid:
            uuid_to_entry.setdefault(entry_uuid, entry)

    # Track which sessions have had headers added.
    seen_sessions: set[str] = set()

    for message in messages:
        message_type = message.type
        msg_session_id = getattr(message, "sessionId", "") or ""
        message_uuid = getattr(message, "uuid", "") or ""

        # Pre-D11 inline derivation (``current_render_session`` +
        # agent-parent resolution) collapsed into one map lookup.
        # ``None`` for trunk uuids — the TemplateMessage's
        # ``_render_session_id`` stays unset and falls back to
        # ``meta.session_id`` at read time.
        effective_session: Optional[str] = uuid_to_render_sid.get(message_uuid)

        # Branch header: fires off the SAME map-driven trigger that
        # assigns ``render_session_id``. A branch sid contains ``@``;
        # agent messages whose parent is a branch ALSO have such a
        # mapping, but the branch header is owned by the branch's own
        # entries — skip the trigger when the message's own sessionId
        # is an agent (``#agent-`` in the id), so the agent's
        # presence inside a branch doesn't double-create a header
        # for that branch via an out-of-order agent entry. (The
        # branch's own first surviving entry always precedes its
        # agents in the message stream because
        # ``_integrate_agent_entries`` splices agents at the anchor
        # tool_result, which is itself a branch-line entry.)
        if (
            effective_session
            and "@" in effective_session
            and effective_session not in seen_sessions
            and not is_agent_session(msg_session_id)
        ):
            # Ensure the branch's PARENT trunk session header is
            # registered FIRST. At non-FULL detail every trunk
            # message of the branch's parent session can be
            # filtered out, leaving a branch descendant as the
            # first surviving entry for that trunk session. Without
            # this guard, the branch header would register before
            # any trunk header → ``_reorder_session_template_messages``
            # (which preserves session-header encounter order) would
            # emit the branch header as a root section instead of
            # nesting it under its parent trunk, and the branch
            # header's ``parent_message_index`` would also be
            # ``None`` because the trunk header isn't in
            # ``ctx.session_first_message`` yet. Closes the
            # CodeRabbit-flagged regression on PR #190.
            #
            # ``msg_session_id`` is the JSONL ``sessionId`` of the
            # branch's own entry (the trunk session id, e.g. ``s1``
            # — branch sids only exist in the in-memory DAG, never
            # in the source JSONL). Use it as the parent trunk sid.
            parent_trunk_sid = msg_session_id or "unknown"
            if (
                parent_trunk_sid
                and parent_trunk_sid not in seen_sessions
                and not is_agent_session(parent_trunk_sid)
            ):
                seen_sessions.add(parent_trunk_sid)
                trunk_summary = sessions.get(parent_trunk_sid, {}).get("summary")
                trunk_header_content = _build_trunk_header(
                    parent_trunk_sid,
                    trunk_summary,
                    session_hierarchy,
                    session_summaries,
                    session_team_names,
                    ctx,
                )
                trunk_header = TemplateMessage(trunk_header_content)
                msg_index = ctx.register(trunk_header)
                ctx.session_first_message[parent_trunk_sid] = msg_index

            seen_sessions.add(effective_session)
            branch_header_content = _build_branch_header(
                effective_session,
                message,
                session_hierarchy,
                session_summaries,
                session_team_names,
                uuid_to_entry,
                ctx,
            )
            branch_header = TemplateMessage(branch_header_content)
            branch_header.render_session_id = effective_session
            msg_index = ctx.register(branch_header)
            ctx.session_first_message[effective_session] = msg_index

        # Handle system messages (already filtered in pass 1)
        if isinstance(message, SystemTranscriptEntry):
            system_content = create_system_message(message)
            # A content-less system entry (e.g. ``turn_duration``,
            # ``stop_hook_summary`` with no body) is normally dropped — but if
            # it is a *within-session fork point* (issue #233), dropping it
            # leaves the fork's nav/back-link anchors dangling (they fall back
            # to the session header). Synthesize a minimal placeholder so the
            # fork point is a real, anchorable landmark and the existing
            # fork-point machinery (``_link_junction_forwards`` box,
            # ``_build_branch_header`` back-link, nav anchor) resolves to it.
            if system_content is None and _is_unrendered_within_session_fork(
                message.uuid, ctx
            ):
                system_content = _fork_placeholder_content(message)
            if system_content:
                system_msg = TemplateMessage(system_content)
                if effective_session:
                    system_msg.render_session_id = effective_session
                ctx.register(system_msg)
            continue

        # Handle attachment entries (issue #128). The factory returns
        # ``None`` for non-hook flavours; those are silently dropped
        # here, mirroring how Passthrough was handled before #128.
        if isinstance(message, AttachmentTranscriptEntry):
            attachment_content = create_attachment_message(message)
            if attachment_content:
                attachment_msg = TemplateMessage(attachment_content)
                if effective_session:
                    attachment_msg.render_session_id = effective_session
                ctx.register(attachment_msg)
            continue

        # Skip summary, ai-title, and passthrough entries (should be
        # filtered in pass 1, but be defensive — they lack .message /
        # BaseTranscriptEntry fields used by the rendering path below)
        if isinstance(
            message,
            (
                SummaryTranscriptEntry,
                AiTitleTranscriptEntry,
                PassthroughTranscriptEntry,
            ),
        ):
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

        # Add session header if this is a new session. Subagent sessions
        # (synthetic ``{trunk}#agent-{agentId}`` sessionId from
        # ``_integrate_agent_entries``) get NO header — their chunks are
        # relocated under the trunk Task/Agent tool_result by
        # ``_relocate_subagent_blocks`` and render inline as part of the
        # trunk session.
        is_agent = is_agent_session(session_id)
        if session_id not in seen_sessions:
            seen_sessions.add(session_id)
            if not is_agent:
                # Pre-D11 also reset ``current_render_session`` here;
                # the new map-driven derivation needs no reset (each
                # uuid carries its own render_session_id via the
                # ``uuid_to_render_sid`` lookup above).
                session_header_content = _build_trunk_header(
                    session_id,
                    session_summary,
                    session_hierarchy,
                    session_summaries,
                    session_team_names,
                    ctx,
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
                if effective_session:
                    chunk_msg.render_session_id = effective_session
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
                if effective_session:
                    tool_msg.render_session_id = effective_session
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

    detail: DetailLevel = DetailLevel.FULL
    compact: bool = False
    # When True, suppress ``※ recap`` (away_summary) messages at every detail
    # level (#179). Recaps are otherwise always visible (see
    # ``AwaySummaryMessage.detail_visibility``).
    no_recaps: bool = False

    # Output format identifier consulted by the class-side dispatch path
    # below. Subclasses override to ``"html"`` etc.; the default
    # ``"markdown"`` makes the base Renderer behave correctly when used
    # standalone (it emits markdown anyway). See _dispatch_format docstring.
    _class_dispatch_format: str = "markdown"

    def _dispatch_format(self, obj: Any, message: TemplateMessage) -> str:
        """Dispatch to format_{ClassName}(obj, message) based on object type.

        Two-strategy resolution walking ``type(obj).__mro__``:

        1. **Renderer-side** ``format_<ClassName>(self, obj, message)``
           method. Preserves all built-in dispatch unchanged — the
           renderer class carries hand-written format_BashInput /
           format_ToolUseMessage / etc.
        2. **Class-side** ``format_<output>(self, renderer, message)``
           method on the content class itself (where ``<output>`` is
           ``markdown`` or ``html`` per ``_class_dispatch_format``).
           Used by plugin-defined ``MessageContent`` subclasses that
           carry their own render methods.

        Renderer-side wins first per MRO node (matrix in
        ``work/tool-renderer-plugins.md`` §``_dispatch_format``
        resolution order). A plugin subclass that wants to shadow a
        built-in renderer method does so by defining the class-side
        method on the *plugin* subclass — the MRO walk visits it
        before the built-in's renderer method registers.
        """
        method_attr = f"format_{self._class_dispatch_format}"
        for cls in type(obj).__mro__:
            if cls is object:
                break
            # Strategy 1: renderer-side method.
            if method := getattr(self, f"format_{cls.__name__}", None):
                return method(obj, message)
            # Strategy 2: class-side method declared *on this MRO node*
            # (intentionally not inherited — each class opts in).
            class_method = cls.__dict__.get(method_attr)
            if class_method is not None:
                return class_method(obj, self, message)
        return ""

    def _dispatch_title(self, obj: Any, message: TemplateMessage) -> Optional[str]:
        """Dispatch to title_{ClassName}(obj, message) based on object type.

        Same two-strategy resolution as :meth:`_dispatch_format`:
        renderer-side ``title_<ClassName>`` first, then class-side
        ``title()`` declared on the MRO node. Returns ``None`` if no
        handler exists (caller falls back to a default).
        """
        for cls in type(obj).__mro__:
            if cls is object:
                break
            if method := getattr(self, f"title_{cls.__name__}", None):
                return method(obj, message)
            class_method = cls.__dict__.get("title")
            if class_method is not None:
                return class_method(obj, self, message)
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

        Delegates to :meth:`_dispatch_title` so plugin-defined
        ``MessageContent`` subclasses can supply their own class-side
        ``title()`` method (Strategy 2 of the plugin dispatch contract).
        Without delegation, the renderer-only MRO walk fires
        ``title_ToolUseMessage`` on the base renderer before a plugin
        subclass's class-side ``title()`` is reached — silently
        ignoring the plugin's contribution at the top level.

        Falls back to a title-cased ``message_type`` when neither
        strategy yields a title (which is what happens for built-in
        message classes that have no renderer-side title method either).
        """
        # Use `is not None` rather than truthiness: a handler that
        # returns an empty string (e.g. title_ToolResultMessage for
        # non-error results) is asserting "no header content needed",
        # not "I didn't handle this". The walrus / truthy form would
        # incorrectly fall through to the message_type default.
        title = self._dispatch_title(message.content, message)
        if title is not None:
            return title
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

    def title_HookAttachmentMessage(
        self, content: HookAttachmentMessage, _: TemplateMessage
    ) -> str:
        # Title surfaces the hook event + name (e.g. "Hook ·
        # PostToolUse:TaskUpdate") so distinct hooks don't blur into
        # one another in long transcripts. Falls back to the kind
        # discriminator when name/event aren't recorded.
        label = content.hook_name or content.hook_event or content.kind
        return f"Hook · {label}"

    def title_AwaySummaryMessage(
        self, _content: AwaySummaryMessage, _: TemplateMessage
    ) -> str:
        return "Recap"

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

    def title_WorkflowPhaseMessage(
        self, content: WorkflowPhaseMessage, _: TemplateMessage
    ) -> str:
        # Format-neutral header label for a spliced workflow phase card
        # (#174 PR3). The agent count + detail render in the body.
        return f"Phase: {content.title}" if content.title else "Phase"

    def title_WorkflowAgentMessage(
        self, content: WorkflowAgentMessage, _: TemplateMessage
    ) -> str:
        # "Agent <label>" (no colon — labels are colon-shaped already,
        # e.g. "map:converter-load-pipeline").
        return f"Agent {content.label}" if content.label else "Agent"

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
        session_tree: Optional["SessionTree"] = None,
    ) -> Optional[str]:
        """Generate output from transcript messages.

        Args:
            messages: List of transcript entries to render.
            title: Optional title for the output.
            combined_transcript_link: Optional link to combined transcript.
            output_dir: Optional output directory for referenced images.
            session_tree: Optional pre-built SessionTree (avoids rebuilding DAG).

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
        session_tree: Optional["SessionTree"] = None,
        suppress_combined_link: bool = False,
    ) -> Optional[str]:
        """Generate output for a single session.

        Args:
            messages: List of transcript entries.
            session_id: Session ID to generate output for.
            title: Optional title for the output.
            cache_manager: Optional cache manager.
            output_dir: Optional output directory for referenced images.
            session_tree: Optional pre-built SessionTree (avoids rebuilding DAG).
            suppress_combined_link: When True, omit the per-session
                "Back to combined transcript" affordance (used under
                `--combined no` where the combined file is not written
                and the back-link would 404).

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


def get_renderer(
    format: str,
    image_export_mode: Optional[str] = None,
    detail: DetailLevel = DetailLevel.FULL,
    compact: bool = False,
    no_timestamps: bool = False,
    no_recaps: bool = False,
) -> Renderer:
    """Get a renderer instance for the specified format.

    Args:
        format: The output format ("html", "md", or "markdown").
        image_export_mode: Image export mode ("placeholder", "embedded", "referenced").
            If None, defaults to "embedded" for HTML and "referenced" for Markdown.
        detail: Output detail level controlling which message types are included.
        compact: If True, merge consecutive same-type headings (Markdown only).
        no_timestamps: If True, suppress per-message timestamp lines
            in Markdown output (issue #160). Ignored for HTML/JSON
            since they don't emit those lines.
        no_recaps: If True, suppress ``※ recap`` (away_summary) messages at
            every detail level (issue #179). Recaps are otherwise always
            visible.

    Returns:
        A Renderer instance for the specified format.

    Raises:
        ValueError: If the format is not supported.
    """
    if format == "html":
        from .html.renderer import HtmlRenderer

        # For HTML, default to embedded mode (current behavior)
        mode = image_export_mode or "embedded"
        renderer = HtmlRenderer(image_export_mode=mode)
    elif format in ("md", "markdown"):
        from .markdown.renderer import MarkdownRenderer

        # For Markdown, default to referenced mode
        mode = image_export_mode or "referenced"
        renderer = MarkdownRenderer(image_export_mode=mode, no_timestamps=no_timestamps)
    elif format == "json":
        from .json.renderer import JsonRenderer

        renderer = JsonRenderer()
    else:
        raise ValueError(f"Unsupported format: {format}")
    renderer.detail = detail
    renderer.compact = compact
    renderer.no_recaps = no_recaps
    return renderer


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

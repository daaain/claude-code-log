"""Factory modules for creating typed objects from raw data."""

from .agent_metadata_factory import (
    parse_agent_result_metadata,
)
from .meta_factory import (
    # Metadata creation
    create_meta,
)
from .system_factory import (
    # System message detection
    is_system_message,
    # System message creation
    create_system_message,
)
from .user_factory import (
    # User message type detection
    is_bash_input,
    is_bash_output,
    is_command_message,
    is_local_command_output,
    # User message creation
    create_bash_input_message,
    create_bash_output_message,
    create_command_output_message,
    create_compacted_summary_message,
    create_ide_notification_content,
    extract_system_reminder_content,
    create_slash_command_message,
    create_user_memory_message,
    create_user_message,
    # Command-tag cleanup helper (used by system_factory, utils preview path)
    simplify_command_tags,
    # Patterns and constants
    COMPACTED_SUMMARY_PREFIX,
    IDE_DIAGNOSTICS_PATTERN,
    IDE_OPENED_FILE_PATTERN,
    IDE_SELECTION_PATTERN,
    SYSTEM_REMINDER_PATTERN,
)
from .assistant_factory import (
    # Assistant message creation
    create_assistant_message,
    create_thinking_message,
)
from .teammate_factory import (
    # Teammate-message parsing
    create_teammate_message,
    find_team_lead_body,
    has_teammate_message,
    iter_teammate_blocks,
)
from .tool_factory import (
    # Tool message creation
    create_tool_input,
    create_tool_use_message,
    create_tool_result_message,
    # Tool processing result
    ToolItemResult,
    # Tool input models mapping
    TOOL_INPUT_MODELS,
)
from .transcript_factory import (
    # Content type constants
    ASSISTANT_CONTENT_TYPES,
    USER_CONTENT_TYPES,
    # Conditional casts
    as_assistant_entry,
    as_user_entry,
    # Usage normalization
    normalize_usage_info,
    # Content item creation
    create_content_item,
    create_message_content,
    # Transcript entry creation
    create_transcript_entry,
)

__all__ = [
    # Agent metadata tail parsing
    "parse_agent_result_metadata",
    # Metadata creation
    "create_meta",
    # Content type constants
    "USER_CONTENT_TYPES",
    "ASSISTANT_CONTENT_TYPES",
    # Conditional casts
    "as_user_entry",
    "as_assistant_entry",
    # Usage normalization
    "normalize_usage_info",
    # Content item creation
    "create_content_item",
    "create_message_content",
    # Transcript entry creation
    "create_transcript_entry",
    # System message detection
    "is_system_message",
    # System message creation
    "create_system_message",
    # User message type detection
    "is_bash_input",
    "is_bash_output",
    "is_command_message",
    "is_local_command_output",
    # User message creation
    "create_bash_input_message",
    "create_bash_output_message",
    "create_command_output_message",
    "create_compacted_summary_message",
    "create_ide_notification_content",
    "extract_system_reminder_content",
    "create_slash_command_message",
    "create_user_memory_message",
    "create_user_message",
    # Command-tag cleanup helper
    "simplify_command_tags",
    # Patterns and constants
    "COMPACTED_SUMMARY_PREFIX",
    "IDE_DIAGNOSTICS_PATTERN",
    "IDE_OPENED_FILE_PATTERN",
    "IDE_SELECTION_PATTERN",
    "SYSTEM_REMINDER_PATTERN",
    # Assistant message creation
    "create_assistant_message",
    "create_thinking_message",
    # Teammate-message parsing
    "create_teammate_message",
    "find_team_lead_body",
    "has_teammate_message",
    "iter_teammate_blocks",
    # Tool message creation
    "create_tool_input",
    "create_tool_use_message",
    "create_tool_result_message",
    # Tool processing result
    "ToolItemResult",
    # Tool input models mapping
    "TOOL_INPUT_MODELS",
]

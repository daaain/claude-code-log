"""HTML-specific rendering utilities package.

Re-exports all functions from utils and formatter modules for backward compatibility.
"""

from .utils import (
    css_class_from_message,
    escape_html,
    get_message_emoji,
    get_template_environment,
    is_session_header,
    render_collapsible_code,
    render_file_content_collapsible,
    render_markdown,
    render_markdown_collapsible,
    starts_with_emoji,
)
from .tool_formatters import (
    # Tool input formatters (called by HtmlRenderer.format_{InputClass})
    format_askuserquestion_input,
    format_bash_input,
    format_edit_input,
    format_grep_input,
    format_exitplanmode_input,
    format_multiedit_input,
    format_read_input,
    format_task_input,
    format_todowrite_input,
    format_webfetch_input,
    format_write_input,
    # Tool output formatters (called by HtmlRenderer.format_{OutputClass})
    format_askuserquestion_output,
    format_bash_output,
    format_edit_output,
    format_exitplanmode_output,
    format_read_output,
    format_task_output,
    format_webfetch_output,
    format_write_output,
    # Fallback formatter
    format_tool_result_content_raw,
    # Legacy formatters (still used)
    format_askuserquestion_result,
    format_exitplanmode_result,
    # Generic
    render_params_table,
)
from .system_formatters import (
    format_hook_summary_content,
    format_session_header_content,
    format_system_content,
)
from ..models import (
    AssistantTextMessage,
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    IdeDiagnostic,
    IdeNotificationContent,
    IdeOpenedFile,
    IdeSelection,
    SessionHeaderMessage,
    SlashCommandMessage,
    ThinkingMessage,
    UserMemoryMessage,
    UserTextMessage,
)
from ..factories import (
    create_bash_input_message,
    create_bash_output_message,
    create_command_output_message,
    create_ide_notification_content,
    create_slash_command_message,
)
from .user_formatters import (
    format_bash_input_content,
    format_bash_output_content,
    format_command_output_content,
    format_compacted_summary_content,
    format_ide_notification_content,
    format_slash_command_content,
    format_user_memory_content,
    format_user_text_content,
    format_user_text_model_content,
)
from .assistant_formatters import (
    format_assistant_text_content,
    format_image_content,
    format_thinking_content,
)

__all__ = [
    # utils
    "css_class_from_message",
    "escape_html",
    "get_message_emoji",
    "get_template_environment",
    "is_session_header",
    "render_collapsible_code",
    "render_file_content_collapsible",
    "render_markdown",
    "render_markdown_collapsible",
    "starts_with_emoji",
    # tool_formatters (input) - called by HtmlRenderer.format_{InputClass}
    "format_askuserquestion_input",
    "format_bash_input",
    "format_edit_input",
    "format_grep_input",
    "format_exitplanmode_input",
    "format_multiedit_input",
    "format_read_input",
    "format_task_input",
    "format_todowrite_input",
    "format_webfetch_input",
    "format_write_input",
    # tool_formatters (output) - called by HtmlRenderer.format_{OutputClass}
    "format_askuserquestion_output",
    "format_bash_output",
    "format_edit_output",
    "format_exitplanmode_output",
    "format_read_output",
    "format_task_output",
    "format_webfetch_output",
    "format_write_output",
    # Fallback formatter
    "format_tool_result_content_raw",
    # Legacy formatters (still used)
    "format_askuserquestion_result",
    "format_exitplanmode_result",
    # Generic
    "render_params_table",
    # system_formatters
    "format_hook_summary_content",
    "format_session_header_content",
    "format_system_content",
    # system content models
    "SessionHeaderMessage",
    # user_formatters (content models)
    "SlashCommandMessage",
    "CommandOutputMessage",
    "BashInputMessage",
    "BashOutputMessage",
    "CompactedSummaryMessage",
    "UserMemoryMessage",
    "UserTextMessage",
    "IdeNotificationContent",
    "IdeOpenedFile",
    "IdeSelection",
    "IdeDiagnostic",
    # user_formatters (formatting)
    "format_slash_command_content",
    "format_command_output_content",
    "format_bash_input_content",
    "format_bash_output_content",
    "format_compacted_summary_content",
    "format_user_memory_content",
    "format_user_text_content",
    "format_user_text_model_content",
    "format_ide_notification_content",
    # user_factory (message creation)
    "create_slash_command_message",
    "create_command_output_message",
    "create_bash_input_message",
    "create_bash_output_message",
    "create_ide_notification_content",
    # assistant_formatters (content models)
    "AssistantTextMessage",
    "ThinkingMessage",
    # assistant_formatters (formatting)
    "format_assistant_text_content",
    "format_thinking_content",
    "format_image_content",
]

"""Factory for user transcript entries.

This module handles creation of MessageContent from user transcript entries:
- SlashCommandMessage: Slash command invocations
- CommandOutputMessage: Local command output
- BashInputMessage: Bash command input
- BashOutputMessage: Bash command output
- UserTextMessage: Regular user text (with optional IDE notifications)
- UserSlashCommandMessage: Expanded slash command prompts (isMeta)
- CompactedSummaryMessage: Compacted conversation summaries
- UserMemoryMessage: User memory content
- UserSteeringMessage: User steering prompts (queue-operation 'remove')

Also provides:
- is_command_message: Check if text is a slash command
- is_local_command_output: Check if text is local command output
- is_bash_input: Check if text is bash input
- is_bash_output: Check if text is bash output
"""

import json
import re
from typing import Any, Optional, Union, cast

from ..models import (
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    ContentItem,
    IdeDiagnostic,
    IdeNotificationContent,
    IdeOpenedFile,
    IdeSelection,
    ImageContent,
    MessageMeta,
    SlashCommandMessage,
    SystemReminderContent,
    TaskNotificationMessage,
    TeammateMessage,
    TextContent,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserTextMessage,
)
from ..plugins import apply_transformers
from .task_notification_factory import (
    create_task_notification_message,
    has_task_notification,
)
from .teammate_factory import create_teammate_message, has_teammate_message

_IMAGE_PLACEHOLDER_RE = re.compile(r"\[Image\s+#(?P<number>[1-9][0-9]*)\]")


# =============================================================================
# Message Type Detection
# =============================================================================


def is_command_message(text_content: str) -> bool:
    """Check if a message contains command information that should be displayed."""
    return "<command-name>" in text_content and "<command-message>" in text_content


def is_local_command_output(text_content: str) -> bool:
    """Check if a message contains local command output."""
    return "<local-command-stdout>" in text_content


def is_bash_input(text_content: str) -> bool:
    """Check if a message contains bash input command."""
    return "<bash-input>" in text_content and "</bash-input>" in text_content


def is_bash_output(text_content: str) -> bool:
    """Check if a message contains bash command output."""
    return "<bash-stdout>" in text_content or "<bash-stderr>" in text_content


# =============================================================================
# Command-tag Cleanup
# =============================================================================


_COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
# DOTALL + non-greedy: the harness writes free-form text into
# ``<command-args>`` and free-form text legitimately contains ``<``
# (e.g. ``/explain <Component>``). The earlier ``[^<]*`` form silently
# dropped such args because the closing ``</command-args>`` couldn't
# match. ``(.*?)`` is robust to ``<`` in payload; the closing tag
# anchors termination. ``</command-args>`` literal in args remains
# theoretically unsafe but isn't an emission shape the harness produces.
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_LOCAL_STDOUT_RE = re.compile(
    r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL
)
_LOCAL_STDERR_RE = re.compile(
    r"<local-command-stderr>(.*?)</local-command-stderr>", re.DOTALL
)


def _normalize_slash(cmd: str) -> str:
    """Prefix ``/`` to a command name that doesn't already carry one.

    Modern harness emissions (2025+) write ``<command-name>/exit</command-name>``
    with the leading slash; legacy / synthetic fixtures occasionally
    write the bare form (``<command-name>init</command-name>``). Display
    paths and structured content models want a single shape so mixed
    transcripts don't render ``/exit`` next to ``test-command``.
    """
    return cmd if cmd.startswith("/") else f"/{cmd}"


def simplify_command_tags(text: str) -> str:
    """Strip Claude Code's command-tag XML soup down to its semantic core.

    Three shapes covered:

    - ``<command-name>/X</command-name><command-message>X</command-message><command-args>Y</command-args>``
      → ``"/X"`` (or ``"/X Y"`` when args are non-empty). The
      ``<command-message>`` field is always a redundant restatement of
      the name without the leading ``/``, never carrying information.
      Bare ``<command-name>X</command-name>`` (legacy emission) is
      normalised to ``"/X"`` for display consistency with modern
      ``/X``-prefixed emissions.
    - ``<local-command-stdout>X</local-command-stdout>`` → ``"X"`` —
      the harness's user-facing hint for dialog-style commands
      (``"Status dialog dismissed"``, ``"Skills dialog dismissed"``,
      …).
    - ``<local-command-stderr>X</local-command-stderr>`` → ``"X"``
      (paired stderr equivalent).

    Returns the text unchanged when none of the patterns match — keeps
    the helper safe to apply opportunistically as part of a preview
    pipeline (slash-command branch headers, system-info messages, …)
    that also receives non-command text.
    """
    name_match = _COMMAND_NAME_RE.search(text)
    if name_match:
        cmd = _normalize_slash(name_match.group(1).strip())
        args_match = _COMMAND_ARGS_RE.search(text)
        args = args_match.group(1).strip() if args_match else ""
        return f"{cmd} {args}".strip() if args else cmd

    for regex in (_LOCAL_STDOUT_RE, _LOCAL_STDERR_RE):
        match = regex.search(text)
        if match:
            return match.group(1).strip()

    return text


# =============================================================================
# Slash Command Creation
# =============================================================================


def create_slash_command_message(
    meta: MessageMeta,
    text: str,
) -> Optional[SlashCommandMessage]:
    """Create SlashCommandMessage from text containing command tags.

    ``command_name`` is normalised to ``/cmd`` shape — the modern
    harness emits the leading slash, but legacy ``<command-name>init``
    fixtures don't, and the typed content model is consumed by display
    paths that expect a single shape (HTML/Markdown title formatters,
    JSON output). ``command_args`` is captured verbatim including any
    ``<`` characters in the payload (see ``_COMMAND_ARGS_RE``).

    Args:
        text: Raw text that may contain command-name, command-args, command-contents tags
        meta: Message metadata

    Returns:
        SlashCommandMessage if tags found, None otherwise
    """
    command_name_match = _COMMAND_NAME_RE.search(text)
    if not command_name_match:
        return None

    command_name = _normalize_slash(command_name_match.group(1).strip())

    command_args_match = _COMMAND_ARGS_RE.search(text)
    command_args = command_args_match.group(1).strip() if command_args_match else ""

    # Parse command contents, handling JSON format
    command_contents_match = re.search(
        r"<command-contents>(.+?)</command-contents>", text, re.DOTALL
    )
    command_contents = ""
    if command_contents_match:
        contents_text = command_contents_match.group(1).strip()
        # Try to parse as JSON and extract the text field
        try:
            contents_json: Any = json.loads(contents_text)
            if isinstance(contents_json, dict) and "text" in contents_json:
                text_dict = cast(dict[str, Any], contents_json)
                text_value = text_dict["text"]
                command_contents = str(text_value)
            else:
                command_contents = contents_text
        except json.JSONDecodeError:
            command_contents = contents_text

    return SlashCommandMessage(
        command_name=command_name,
        command_args=command_args,
        command_contents=command_contents,
        meta=meta,
    )


def create_command_output_message(
    meta: MessageMeta,
    text: str,
) -> Optional[CommandOutputMessage]:
    """Create CommandOutputMessage from text containing local-command-stdout tags.

    Args:
        text: Raw text that may contain local-command-stdout tags
        meta: Message metadata

    Returns:
        CommandOutputMessage if tags found, None otherwise
    """
    stdout_match = re.search(
        r"<local-command-stdout>(.*?)</local-command-stdout>",
        text,
        re.DOTALL,
    )
    if not stdout_match:
        return None

    stdout_content = stdout_match.group(1).strip()
    # Check if content looks like markdown (starts with markdown headers)
    is_markdown = bool(re.match(r"^#+\s+", stdout_content, re.MULTILINE))

    return CommandOutputMessage(
        stdout=stdout_content, is_markdown=is_markdown, meta=meta
    )


# =============================================================================
# Bash Input/Output Creation
# =============================================================================


def create_bash_input_message(
    meta: MessageMeta,
    text: str,
) -> Optional[BashInputMessage]:
    """Create BashInputMessage from text containing bash-input tags.

    Args:
        text: Raw text that may contain bash-input tags
        meta: Message metadata

    Returns:
        BashInputMessage if tags found, None otherwise
    """
    bash_match = re.search(r"<bash-input>(.*?)</bash-input>", text, re.DOTALL)
    if not bash_match:
        return None

    return BashInputMessage(command=bash_match.group(1).strip(), meta=meta)


def create_bash_output_message(
    meta: MessageMeta,
    text: str,
) -> Optional[BashOutputMessage]:
    """Create BashOutputMessage from text containing bash-stdout/bash-stderr tags.

    Args:
        text: Raw text that may contain bash-stdout/bash-stderr tags
        meta: Message metadata

    Returns:
        BashOutputMessage if tags found, None otherwise
    """
    stdout_match = re.search(r"<bash-stdout>(.*?)</bash-stdout>", text, re.DOTALL)
    stderr_match = re.search(r"<bash-stderr>(.*?)</bash-stderr>", text, re.DOTALL)

    if not stdout_match and not stderr_match:
        return None

    stdout = stdout_match.group(1).strip() if stdout_match else None
    stderr = stderr_match.group(1).strip() if stderr_match else None

    # Convert empty strings to None for cleaner representation
    if stdout == "":
        stdout = None
    if stderr == "":
        stderr = None

    return BashOutputMessage(stdout=stdout, stderr=stderr, meta=meta)


# =============================================================================
# IDE Notification Creation
# =============================================================================

# Shared regex patterns for IDE notification tags
IDE_OPENED_FILE_PATTERN = re.compile(
    r"<ide_opened_file>(.*?)</ide_opened_file>", re.DOTALL
)
IDE_SELECTION_PATTERN = re.compile(r"<ide_selection>(.*?)</ide_selection>", re.DOTALL)
IDE_DIAGNOSTICS_PATTERN = re.compile(
    r"<post-tool-use-hook>\s*<ide_diagnostics>(.*?)</ide_diagnostics>\s*</post-tool-use-hook>",
    re.DOTALL,
)

# Canonical <system-reminder> matcher (issue #275). Shared with utils.py's
# session-preview helpers (which strip the block so raw tags never reach the
# index), the same way the IDE patterns above are shared.
SYSTEM_REMINDER_PATTERN = re.compile(
    r"<system-reminder>(.*?)</system-reminder>", re.DOTALL
)


def create_ide_notification_content(text: str) -> Optional[IdeNotificationContent]:
    """Create IdeNotificationContent from text containing IDE tags.

    Handles:
    - <ide_opened_file>: Simple file open notifications
    - <ide_selection>: Code selection notifications
    - <post-tool-use-hook><ide_diagnostics>: JSON diagnostic arrays

    Args:
        text: Raw text that may contain IDE notification tags

    Returns:
        IdeNotificationContent if any tags found, None otherwise
    """
    opened_files: list[IdeOpenedFile] = []
    selections: list[IdeSelection] = []
    diagnostics: list[IdeDiagnostic] = []
    remaining_text = text

    # Pattern 1: <ide_opened_file>content</ide_opened_file>
    for match in IDE_OPENED_FILE_PATTERN.finditer(remaining_text):
        content = match.group(1).strip()
        opened_files.append(IdeOpenedFile(content=content))

    remaining_text = IDE_OPENED_FILE_PATTERN.sub("", remaining_text)

    # Pattern 2: <ide_selection>content</ide_selection>
    for match in IDE_SELECTION_PATTERN.finditer(remaining_text):
        content = match.group(1).strip()
        selections.append(IdeSelection(content=content))

    remaining_text = IDE_SELECTION_PATTERN.sub("", remaining_text)

    # Pattern 3: <post-tool-use-hook><ide_diagnostics>JSON</ide_diagnostics></post-tool-use-hook>
    for match in IDE_DIAGNOSTICS_PATTERN.finditer(remaining_text):
        json_content = match.group(1).strip()
        try:
            parsed_diagnostics: Any = json.loads(json_content)
            if isinstance(parsed_diagnostics, list):
                diagnostics.append(
                    IdeDiagnostic(
                        diagnostics=cast(list[dict[str, Any]], parsed_diagnostics)
                    )
                )
            else:
                # Not a list, store as raw content
                diagnostics.append(IdeDiagnostic(raw_content=json_content))
        except (json.JSONDecodeError, ValueError):
            # JSON parsing failed, store raw content
            diagnostics.append(IdeDiagnostic(raw_content=json_content))

    remaining_text = IDE_DIAGNOSTICS_PATTERN.sub("", remaining_text)

    # Only return if we found any IDE tags
    if not opened_files and not selections and not diagnostics:
        return None

    return IdeNotificationContent(
        opened_files=opened_files,
        selections=selections,
        diagnostics=diagnostics,
        remaining_text=remaining_text.strip(),
    )


def extract_system_reminder_content(
    text: str,
) -> tuple[Optional[SystemReminderContent], str]:
    """Peel ``<system-reminder>`` block(s) out of user-message text.

    A user message may carry a system reminder (e.g. the ``/cd`` notice) and
    real content after it (e.g. a CLAUDE.md). The reminder is extracted as an
    annotation while the remaining text renders as ordinary user content —
    mirroring :func:`create_ide_notification_content`.

    Returns ``(SystemReminderContent | None, remaining_text)``. When no reminder
    block is present, returns ``(None, text)`` unchanged so a reminder-free
    message is byte-identical to before this feature.
    """
    reminders = [m.group(1).strip() for m in SYSTEM_REMINDER_PATTERN.finditer(text)]
    if not reminders:
        return None, text
    remaining_text = SYSTEM_REMINDER_PATTERN.sub("", text)
    return SystemReminderContent(reminders=reminders), remaining_text


# =============================================================================
# Compacted Summary and User Memory Creation
# =============================================================================

# Pattern for compacted session summary detection
COMPACTED_SUMMARY_PREFIX = "This session is being continued from a previous conversation that ran out of context"


def create_compacted_summary_message(
    meta: MessageMeta,
    content_list: list[ContentItem],
) -> Optional[CompactedSummaryMessage]:
    """Create CompactedSummaryMessage from content list.

    Compacted summaries are generated when a session runs out of context and
    needs to be continued. They contain a summary of the previous conversation.

    If the first text item starts with the compacted summary prefix, all text
    items are combined into a single CompactedSummaryMessage.

    Args:
        content_list: List of ContentItem from user message
        meta: Message metadata

    Returns:
        CompactedSummaryMessage if first text is a compacted summary, None otherwise
    """
    if not content_list or not isinstance(content_list[0], TextContent):
        return None

    first_text = content_list[0].text
    if not first_text.startswith(COMPACTED_SUMMARY_PREFIX):
        return None

    # Combine all text content for compacted summaries
    texts = [item.text for item in content_list if isinstance(item, TextContent)]
    all_text = "\n\n".join(texts)
    return CompactedSummaryMessage(summary_text=all_text, meta=meta)


# Pattern for user memory input tag
USER_MEMORY_PATTERN = re.compile(
    r"<user-memory-input>(.*?)</user-memory-input>", re.DOTALL
)


def create_user_memory_message(
    meta: MessageMeta,
    text: str,
) -> Optional[UserMemoryMessage]:
    """Create UserMemoryMessage from text containing user-memory-input tag.

    User memory input contains context that the user has provided from
    their CLAUDE.md or other memory sources.

    Args:
        text: Raw text that may contain user memory input tag
        meta: Message metadata

    Returns:
        UserMemoryMessage if tag found, None otherwise
    """
    match = USER_MEMORY_PATTERN.search(text)
    if match:
        memory_content = match.group(1).strip()
        return UserMemoryMessage(memory_text=memory_content, meta=meta)
    return None


# =============================================================================
# User Message Content Creation
# =============================================================================

# Type alias for content models returned by create_user_message
UserMessageContent = Union[
    SlashCommandMessage,
    CommandOutputMessage,
    BashInputMessage,
    BashOutputMessage,
    CompactedSummaryMessage,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserTextMessage,
    TeammateMessage,
    TaskNotificationMessage,
]


def create_user_message(
    meta: MessageMeta,
    content_list: list[ContentItem],
    text_content: str,
    is_slash_command: bool = False,
) -> Optional[UserMessageContent]:
    """Wrapper: build the candidate, then run plugin transformers.

    The body lives in :func:`_classify_user_message`; this wrapper
    applies the plugin transformer pass to the result so every
    classification path (slash-command, bash, teammate, ...) becomes
    rewriteable by a plugin, not just the generic-text fallback.
    Transformers whose ``applies_to`` doesn't subclass-match the
    candidate's type pass through with no-op cost.
    """
    candidate = _classify_user_message(
        meta, content_list, text_content, is_slash_command
    )
    if candidate is None:
        return None
    transformed = apply_transformers(candidate, meta)
    return cast("UserMessageContent", transformed)


def _classify_user_message(
    meta: MessageMeta,
    content_list: list[ContentItem],
    text_content: str,
    is_slash_command: bool = False,
) -> Optional[UserMessageContent]:
    """Create a user message content model from content items.

    This is the main entry point for creating user message content.
    It handles all user message types by detecting patterns in the text:
    - Slash commands (<command-name>, <command-message>)
    - Local command output (<local-command-stdout>)
    - Bash input (<bash-input>)
    - Bash output (<bash-stdout>, <bash-stderr>)
    - Compacted summaries (special prefix)
    - User memory (<user-memory-input>)
    - Slash command expanded prompts (isMeta=True)
    - Regular user text with IDE notifications

    Args:
        content_list: List of ContentItem from user message
        text_content: Pre-extracted text content for pattern detection
        is_slash_command: True for slash command expanded prompts (isMeta=True)
        meta: Message metadata

    Returns:
        A content model, or None if content_list is empty.
    """
    if not content_list:
        return None

    # Check for special message patterns first (before generic parsing)
    if is_command_message(text_content):
        return create_slash_command_message(meta, text_content)

    if is_local_command_output(text_content):
        return create_command_output_message(meta, text_content)

    if is_bash_input(text_content):
        return create_bash_input_message(meta, text_content)

    if is_bash_output(text_content):
        return create_bash_output_message(meta, text_content)

    if has_teammate_message(text_content):
        if teammate := create_teammate_message(meta, text_content):
            return teammate

    # Async-agent completion: Claude Code injects a User entry with a
    # ``<task-notification>`` payload when an async-spawned Task
    # finishes (issue #90). Cheap detector first; the parser handles
    # the rest. Surfaces metadata + result_text as a typed content.
    if has_task_notification(text_content):
        if notification := create_task_notification_message(meta, text_content):
            return notification

    # Slash command expanded prompts - combine all text as markdown
    if is_slash_command:
        all_text = "\n\n".join(
            getattr(item, "text", "") for item in content_list if hasattr(item, "text")
        )
        return UserSlashCommandMessage(text=all_text, meta=meta) if all_text else None

    # Get first text item for special case detection
    first_text_item = next(
        (item for item in content_list if hasattr(item, "text")),
        None,
    )
    first_text = getattr(first_text_item, "text", "") if first_text_item else ""

    # Check for compacted session summary first (handles text combining internally)
    if compacted := create_compacted_summary_message(meta, content_list):
        return compacted

    # Check for user memory input
    if user_memory := create_user_memory_message(meta, first_text):
        return user_memory

    # Claude Code stores image blocks separately from their numbered references.
    # Resolve those references here so every provider shares the same rendering.
    images = [
        image for item in content_list if (image := _as_image_content(item)) is not None
    ]
    referenced_images = {
        int(match.group("number"))
        for item in content_list
        if hasattr(item, "text")
        for match in _IMAGE_PLACEHOLDER_RE.finditer(getattr(item, "text"))
        if int(match.group("number")) <= len(images)
    }

    # Build items list preserving order, extracting IDE notifications from text
    items: list[
        TextContent | ImageContent | IdeNotificationContent | SystemReminderContent
    ] = []
    image_number = 0

    for item in content_list:
        # Check for text content
        if hasattr(item, "text"):
            item_text: str = getattr(item, "text")

            # Peel <system-reminder> block(s) first as an annotation; the rest
            # continues through IDE extraction and renders as user content.
            # The annotation is prepended regardless of the reminder's original
            # position in the text — a deliberate simplification, since a real
            # reminder (the /cd notice) leads the message and any CLAUDE.md
            # follows it. A mid/trailing reminder would hoist to the top.
            reminder_content, item_text = extract_system_reminder_content(item_text)
            if reminder_content is not None:
                items.append(reminder_content)

            if ide_content := create_ide_notification_content(item_text):
                # Add IDE notification item first
                items.append(ide_content)
                remaining_text: str = ide_content.remaining_text
            else:
                remaining_text = item_text

            # Add remaining text as TextContent if non-empty
            if remaining_text.strip():
                _append_text_with_images(items, remaining_text, images)
        elif (image := _as_image_content(item)) is not None:
            image_number += 1
            if image_number not in referenced_images:
                items.append(image)

    return UserTextMessage(items=items, meta=meta)


def _as_image_content(item: ContentItem) -> Optional[ImageContent]:
    if isinstance(item, ImageContent):
        return item
    if hasattr(item, "source") and getattr(item, "type", None) == "image":
        return ImageContent.model_validate(item.model_dump())
    return None


def _append_text_with_images(
    items: list[
        TextContent | ImageContent | IdeNotificationContent | SystemReminderContent
    ],
    text: str,
    images: list[ImageContent],
) -> None:
    """Replace valid numbered image references while preserving surrounding text."""
    cursor = 0
    for match in _IMAGE_PLACEHOLDER_RE.finditer(text):
        number = int(match.group("number"))
        if number > len(images):
            continue
        if match.start() > cursor:
            items.append(TextContent(type="text", text=text[cursor : match.start()]))
        items.append(images[number - 1])
        cursor = match.end()
    if cursor < len(text):
        items.append(TextContent(type="text", text=text[cursor:]))

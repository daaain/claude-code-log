# Message Types in Claude Code Transcripts

> See [application_model.md](application_model.md) for the system overview.

This document describes all message types found in Claude Code JSONL transcript files and their corresponding output representations. The goal is to define an **intermediate representation** that captures the logical message structure independent of HTML rendering.

## Overview

Claude Code transcripts contain messages in JSONL format. Each line represents an input message that gets transformed through:

1. **Input Layer** (JSONL): Raw Claude Code transcript data
2. **Intermediate Layer** (TemplateMessage): Format-neutral logical representation
3. **Output Layer** (HTML): Rendered visual output

This document maps input types to their intermediate and output representations.

---

## Data Flow: From Transcript Entries to Rendered Messages

```text
JSONL Parsing (parser.py)
│
├── UserTranscriptEntry
│   ├── TextContent → User message variants:
│   │   ├── UserSlashCommandMessage (isMeta) or SlashCommandMessage (<command-name> tags)
│   │   ├── CommandOutputMessage (<local-command-stdout> tags)
│   │   ├── BashInputMessage (<bash-input> tags)
│   │   ├── CompactedSummaryMessage (compacted conversation)
│   │   ├── UserSteeringMessage (queue-operation "remove")
│   │   └── Plain user text
│   ├── ToolResultContent → ToolResultMessage with output:
│   │   ├── ReadOutput (cat-n formatted file content)
│   │   ├── EditOutput (cat-n formatted edit result)
│   │   └── ToolResultContent (generic fallback)
│   └── ImageContent → Image messages
│
├── AssistantTranscriptEntry
│   ├── TextContent → AssistantTextMessage
│   ├── ThinkingContent → ThinkingMessage
│   └── ToolUseContent → ToolUseMessage with parsed inputs:
│       ├── ReadInput, WriteInput, EditInput, MultiEditInput
│       ├── BashInput, GlobInput, GrepInput
│       ├── TaskInput, TodoWriteInput, AskUserQuestionInput
│       └── ExitPlanModeInput
│
├── SystemTranscriptEntry
│   ├── SystemMessage (level: info/warning/error)
│   ├── HookSummaryMessage (subtype: stop_hook_summary)
│   └── AwaySummaryMessage (subtype: away_summary)
│
├── SummaryTranscriptEntry → Session metadata (not rendered)
│
└── QueueOperationTranscriptEntry
    └── "remove" operation → UserSteeringMessage (rendered as user)
```

---

## Intermediate Representation: TemplateMessage

The intermediate representation is `TemplateMessage`, a Python class (in `renderer.py`) that captures all fields needed for rendering.

### Key Fields

```python
class TemplateMessage:
    # Identity
    type: str                  # Base type: "user", "assistant", "tool_use", etc.
    message_id: str            # Unique ID within session (e.g., "msg-0", "tool-1")
    uuid: str                  # Original JSONL uuid

    # Content (format-neutral)
    content: Optional[MessageContent]  # Structured content model
    # Note: HTML is generated during template rendering, not stored in the message

    # Display
    message_title: str         # Display title (e.g., "User", "Assistant")
    is_sidechain: bool         # Sub-agent message flag (via content.meta)
    # Note: has_markdown is accessed via content.has_markdown
    # Note: CSS classes are derived from content type via CSS_CLASS_REGISTRY

    # Metadata
    raw_timestamp: str         # ISO 8601 timestamp
    session_id: str            # Session UUID

    # Hierarchy
    children: List[TemplateMessage]  # Child messages (tree mode)
    ancestry: List[str]        # Parent message IDs for fold/unfold

    # Pairing
    is_paired: bool            # True if part of a pair
    pair_role: Optional[str]   # "pair_first", "pair_last", "pair_middle"

    # Tool-specific
    tool_use_id: Optional[str]  # ID linking tool_use to tool_result
```

### Content Type → CSS Classes

CSS classes are derived from the content type using `CSS_CLASS_REGISTRY` (in `html/utils.py`). This ensures the content type is the single source of truth for display styling.

| css_class | Content Type | Dynamic Modifier |
|-----------|--------------|------------------|
| `"user"` | `UserTextMessage` | — |
| `"user compacted"` | `CompactedSummaryMessage` | — |
| `"user slash-command"` | `SlashCommandMessage`, `UserSlashCommandMessage` | — |
| `"user command-output"` | `CommandOutputMessage` | — |
| `"user steering"` | `UserSteeringMessage` | — |
| `"assistant"` | `AssistantTextMessage` | — |
| `"tool_use"` | `ToolUseMessage` | — |
| `"tool_result"` | `ToolResultMessage` | — |
| `"tool_result error"` | `ToolResultMessage` | `is_error=True` |
| `"thinking"` | `ThinkingMessage` | — |
| `"bash-input"` | `BashInputMessage` | — |
| `"bash-output"` | `BashOutputMessage` | — |
| `"system system-info"` | `SystemMessage` | `level="info"` |
| `"system system-warning"` | `SystemMessage` | `level="warning"` |
| `"system system-error"` | `SystemMessage` | `level="error"` |
| `"system system-hook"` | `HookSummaryMessage` | — |
| `"system system-away-summary"` | `AwaySummaryMessage` | — |

The `sidechain` modifier is added when `msg.is_sidechain=True` (a cross-cutting concern that applies to any message type).

**Note**: See [css-classes.md](css-classes.md) for complete CSS support status.

---

# Part 1: User Messages (UserTranscriptEntry)

User transcript entries (`type: "user"`) contain human input, tool results, and images.

## 1.1 Content Types in User Messages

User messages contain `ContentItem` instances that are either:
- **TextContent**: User-typed text (with various semantic variants)
- **ToolResultContent**: Results from tool execution
- **ImageContent**: User-attached images

## 1.2 User Text Variants

Based on flags and tag patterns in `TextContent`, user text messages are classified into specialized content types defined in `models.py`.

### Regular User Prompt

- **Condition**: No special flags or tags
- **Content Model**: Plain `TextContent`
- **CSS Class**: `user`
- **Files**: [user.json](messages/user/user.json) | [user.jsonl](messages/user/user.jsonl)

```json
{
  "type": "user",
  "message": {
    "role": "user",
    "content": [{ "type": "text", "text": "Help me fix this bug..." }]
  },
  "isSidechain": false
}
```

### Slash Command (isMeta)

- **Condition**: `isMeta: true` flag
- **Content Model**: `UserSlashCommandMessage` (models.py)
- **CSS Class**: `user slash-command`
- **Files**: [user_slash_command.json](messages/user/user_slash_command.json)

```json
{
  "type": "user",
  "message": { "content": "Caveat: The messages below were generated..." },
  "isMeta": true
}
```

```python
@dataclass
class UserSlashCommandMessage(MessageContent):
    text: str  # LLM-generated markdown instruction text
```

> **Note**: These are LLM-generated instruction prompts from slash commands.
> The text is markdown formatted and rendered as collapsible markdown.

### Slash Command (Tags)

- **Condition**: Contains `<command-name>` tags
- **Content Model**: `SlashCommandMessage` with parsed name/args/contents
- **CSS Class**: `user slash-command`
- **Files**: [user_command.json](messages/user/user_command.json)

```python
@dataclass
class SlashCommandMessage(MessageContent):
    command_name: str      # e.g., "/model", "/context"
    command_args: str      # Arguments after command
    command_contents: str  # Content inside command
```

> **Note**: Both built-in commands (e.g., `/init`, `/model`, `/context`) and
> user-defined commands (e.g., `/my-command` from `~/.claude/commands/my-command.md`)
> use the same `<command-name>` tag format. There is no field in the JSONL to
> differentiate between them.

### Command Output

- **Condition**: Contains `<local-command-stdout>` tags
- **Content Model**: `CommandOutputMessage`
- **CSS Class**: `user command-output`
- **Files**: [command_output.json](messages/user/command_output.json)

```python
@dataclass
class CommandOutputMessage(MessageContent):
    stdout: str        # Command output text
    is_markdown: bool  # True if content appears to be markdown
```

### Bash Input

- **Condition**: Contains `<bash-input>` tags
- **Content Model**: `BashInputMessage`
- **CSS Class**: `bash-input` (filtered by User)
- **Files**: [bash_input.json](messages/user/bash_input.json)

```python
@dataclass
class BashInputMessage(MessageContent):
    command: str  # The bash command that was executed
```

### Bash Output

The corresponding output uses `<bash-stdout>` and optionally `<bash-stderr>` tags:

- **Condition**: Contains `<bash-stdout>` tags
- **Content Model**: `BashOutputMessage`
- **CSS Class**: `bash-output` (filtered by User)
- **Files**: [bash_output.json](messages/user/bash_output.json)

### Compacted Conversation

- **Condition**: Contains "(compacted conversation)" marker
- **Content Model**: `CompactedSummaryMessage`
- **CSS Class**: `user compacted`

```python
@dataclass
class CompactedSummaryMessage(MessageContent):
    summary_text: str  # The compacted conversation summary
```

### User Steering (Queue Remove)

- **Condition**: `QueueOperationTranscriptEntry` with `operation: "remove"`
- **Content Model**: `UserSteeringMessage` (extends `UserTextMessage`)
- **CSS Class**: `user steering`
- **Title**: "User (steering)"

```python
@dataclass
class UserSteeringMessage(UserTextMessage):
    """Message for user steering prompts (queue-operation 'remove')."""
    pass  # Inherits items from UserTextMessage
```

Steering messages represent user interrupts that cancel queued operations.

### User Memory

- **Condition**: Contains `<user-memory-input>` tags
- **Content Model**: `UserMemoryMessage`
- **CSS Class**: `user`

```python
@dataclass
class UserMemoryMessage(MessageContent):
    memory_text: str  # The memory content from the tag
```

### Sidechain User (Sub-agent)

- **Condition**: `isSidechain: true`
- **CSS Class**: `user sidechain`
- **Note**: Typically skipped during rendering (duplicates Task prompt)
- **Files**: [user_sidechain.json](messages/user/user_sidechain.json)

### Async Task Notification (`<task-notification>`)

When an async-spawned `Task` (with `run_in_background=True`) completes,
Claude Code injects a synthetic User entry whose `message.content` is
a raw `<task-notification>…</task-notification>` block.
`task_notification_factory.create_task_notification_message` parses
the embedded fields into a structured content model.

- **Condition**: `message.content` is a string containing `<task-notification>…</task-notification>`
- **Content Model**: `TaskNotificationMessage` (a `MessageContent` subclass — *not* a `UserTextMessage` variant)
- **CSS Classes**: `user task_notification` (the `user` class is what keeps the runtime "User" filter toggle showing the card)

```python
@dataclass
class TaskNotificationMessage(MessageContent):
    task_id: str
    status: str               # "completed", "failed", …
    summary: str              # Agent "<description>" completed
    result_text: str          # the agent's final answer body
    usage: Optional[TaskNotificationUsage]
    transcript_path: Optional[str]   # "Full transcript available at: …"
    raw_text: Optional[str]
    # Phase 3 dedup markers
    result_is_duplicate: bool = False
    spawning_task_message_index: Optional[int] = None
```

The `result_text` is the canonical source for the spawn-fold —
`_link_async_notifications` copies it onto
`TaskOutput.async_final_answer` of the spawning `Task` tool_result so
the answer renders in place. See
[agents.md § 2 (Async task agents)](agents.md#2-async-task-agents-90)
for the end-to-end flow and detail-level matrix.

### IDE Notifications

User messages may contain IDE notification tags that are parsed into structured content:

- **Condition**: Contains `<ide_opened_file>`, `<ide_selection>`, or `<ide_diagnostics>` tags
- **Content Model**: `IdeNotificationContent` containing lists of:
  - `IdeOpenedFile`: File open notifications
  - `IdeSelection`: Code selection notifications
  - `IdeDiagnostic`: Diagnostic messages (parsed JSON or raw text fallback)
- **CSS Class**: Notifications rendered as inline elements within user message

```python
@dataclass
class IdeOpenedFile:
    content: str  # Raw content from the tag

@dataclass
class IdeSelection:
    content: str  # Raw selection content

@dataclass
class IdeDiagnostic:
    diagnostics: Optional[List[Dict[str, Any]]]  # Parsed JSON
    raw_content: Optional[str]  # Fallback if parsing failed

@dataclass
class IdeNotificationContent:  # NOT a MessageContent subclass
    """Embedded within UserTextMessage.items alongside TextContent/ImageContent."""
    opened_files: List[IdeOpenedFile]
    selections: List[IdeSelection]
    diagnostics: List[IdeDiagnostic]
    remaining_text: str  # Text after notifications extracted
```

## 1.3 Tool Results (ToolResultContent)

Tool results appear as `ToolResultContent` items in user messages, linked to their corresponding `ToolUseContent` via `tool_use_id`.

### Tool Result Output Models

| Tool | Output Model | Key Fields | Files |
|------|--------------|------------|-------|
| Read | `ReadOutput` | file_path, content, start_line, num_lines, is_truncated | [tool_result](messages/tools/Read-tool_result.json) |
| Edit | `EditOutput` | file_path, success, diffs, message, start_line | [tool_result](messages/tools/Edit-tool_result.json) |
| Write | `WriteOutput` | file_path, success, message | [tool_result](messages/tools/Write-tool_result.json) |
| Bash | `BashOutput` | content, has_ansi | [tool_result](messages/tools/Bash-tool_result.json) |
| Task | `TaskOutput` *(see note)* | result, metadata, async_final_answer | [tool_result](messages/tools/Task-tool_result.json) |
| TaskOutput | `TaskOutputResult` *(see note)* | retrieval_status, task_id, task_type, status, output_truncated, output_file | (async-agent polling tool — issue #90) |
| TaskStop | `TaskStopOutput` | stopped, message | (kills a background task by id — PR #158 follow-up) |
| AskUserQuestion | `AskUserQuestionOutput` | answers, raw_message | [tool_result](messages/tools/AskUserQuestion-tool_result.json) |
| ExitPlanMode | `ExitPlanModeOutput` | message, approved | [tool_result](messages/tools/ExitPlanMode-tool_result.json) |
| Glob | `GlobOutput` *(TODO)* | pattern, files, truncated | [tool_result](messages/tools/Glob-tool_result.json) |
| Grep | `GrepOutput` *(TODO)* | pattern, matches, output_mode, truncated | [tool_result](messages/tools/Grep-tool_result.json) |
| (error) | — | is_error: true | [Bash error](messages/tools/Bash-tool_result_error.json) |

**(TODO)**: Glob and Grep output models defined in models.py but not yet used.

**Note on `TaskOutput` vs `TaskOutputResult`**: two unrelated dataclasses with overlapping names.
`TaskOutput` is the parsed output of the **`Task` tool_result** (carries the launch stub or final
answer text, agent metadata, and the Phase 3 `async_final_answer` field).
`TaskOutputResult` is the parsed output of the **`TaskOutput` *polling* tool's tool_result** (the
`<retrieval_status>/<task_id>/<status>/<output>[Truncated…]` body that the assistant explicitly
polls between an async-Task launch and its completion notification). The fold writes into
`TaskOutput.async_final_answer` — i.e. the Task tool_result, not the polling tool's result. See
[agents.md § 2.2](agents.md#22-the-two-taskoutput-names).

### Generic Tool Result

- **CSS Class**: `tool_result`
- **Content**: Raw string or structured content

```json
{
  "type": "user",
  "message": {
    "content": [{
      "type": "tool_result",
      "tool_use_id": "toolu_xxx",
      "is_error": false,
      "content": "..."
    }]
  }
}
```

### Tool Result Error

- **Condition**: `is_error: true`
- **CSS Class**: `tool_result error`
- **Files**: [Bash-tool_result_error.json](messages/tools/Bash-tool_result_error.json)

### Read Tool Result → ReadOutput

Read tool results in cat-n format are parsed into structured `ReadOutput`:
- **Files**: [Read-tool_result.json](messages/tools/Read-tool_result.json)

```python
@dataclass
class ReadOutput(MessageContent):
    file_path: str
    content: str           # File content (may be truncated)
    start_line: int        # 1-based starting line number
    num_lines: int         # Number of lines in content
    total_lines: int       # Total lines in file
    is_truncated: bool
    system_reminder: Optional[str]  # Embedded system reminder
```

### Edit Tool Result → EditOutput

Edit tool results with cat-n snippets are parsed into structured `EditOutput`:
- **Files**: [Edit-tool_result.json](messages/tools/Edit-tool_result.json)

```python
@dataclass
class EditOutput(MessageContent):
    file_path: str
    success: bool
    diffs: List[EditDiff]  # Changes made
    message: str           # Result message or code snippet
    start_line: int        # Starting line for display
```

### Tool Result Rendering Wrapper

Tool results are wrapped in `ToolResultMessage` for rendering, which provides additional context and typed output:

```python
@dataclass
class ToolResultMessage(MessageContent):
    tool_use_id: str
    output: ToolOutput  # Specialized output or ToolResultContent fallback
    is_error: bool = False
    tool_name: Optional[str] = None   # Name of the tool
    file_path: Optional[str] = None   # File path for Read/Edit/Write

# ToolOutput is a union type for tool results
ToolOutput = Union[
    ReadOutput,
    WriteOutput,
    EditOutput,
    BashOutput,
    TaskOutput,
    AskUserQuestionOutput,
    ExitPlanModeOutput,
    ToolResultContent,  # Generic fallback for unparsed results
]
```

## 1.4 Images (ImageContent)

- **CSS Class**: `image`
- **Files**: [image.json](messages/user/image.json)

```json
{
  "type": "user",
  "message": {
    "content": [{
      "type": "image",
      "source": {
        "type": "base64",
        "media_type": "image/png",
        "data": "iVBORw0KGgo..."
      }
    }]
  }
}
```

Image data is structured using `ImageSource`:

```python
class ImageSource(BaseModel):
    type: Literal["base64"]
    media_type: str  # e.g., "image/png"
    data: str        # Base64-encoded image data

class ImageContent(BaseModel, MessageContent):
    type: Literal["image"]
    source: ImageSource
```

---

# Part 2: Assistant Messages (AssistantTranscriptEntry)

Assistant transcript entries (`type: "assistant"`) contain Claude's responses.

## 2.1 Content Types in Assistant Messages

Assistant messages contain `ContentItem` instances that are:
- **TextContent**: Claude's text response
- **ThinkingContent**: Extended thinking blocks
- **ToolUseContent**: Tool invocations

## 2.2 Assistant Text → AssistantTextMessage

- **Content Model**: `AssistantTextMessage` (models.py)
- **CSS Class**: `assistant` (or `assistant sidechain`)
- **Files**: [assistant.json](messages/assistant/assistant.json)

```python
@dataclass
class AssistantTextMessage(MessageContent):
    items: list[TextContent | ImageContent]  # Interleaved text and images
    token_usage: Optional[str]               # Formatted token usage string
```

### Sidechain Assistant

- **Condition**: `isSidechain: true`
- **CSS Class**: `assistant sidechain`
- **Title**: "Sub-assistant"
- **Files**: [assistant_sidechain.json](messages/assistant/assistant_sidechain.json)

## 2.3 Thinking Content → ThinkingMessage

- **Content Model**: `ThinkingMessage` (models.py)
- **CSS Class**: `thinking`
- **Files**: [thinking.json](messages/assistant/thinking.json)

```python
@dataclass
class ThinkingMessage(MessageContent):
    thinking: str              # The thinking text
    signature: Optional[str]   # Thinking block signature
    token_usage: Optional[str] # Formatted token usage string
```

```json
{
  "type": "assistant",
  "message": {
    "content": [{ "type": "thinking", "thinking": "Let me analyze..." }]
  }
}
```

## 2.4 Tool Use → ToolUseMessage with Typed Inputs

Tool invocations are parsed from `ToolUseContent` (JSONL) and wrapped in `ToolUseMessage` for rendering:

```python
@dataclass
class ToolUseMessage(MessageContent):
    input: ToolInput  # Specialized (BashInput, etc.) or ToolUseContent fallback
    tool_use_id: str  # From ToolUseContent.id
    tool_name: str    # From ToolUseContent.name

# ToolInput is a union of typed input models
ToolInput = Union[
    BashInput, ReadInput, WriteInput, EditInput, MultiEditInput,
    GlobInput, GrepInput, TaskInput, TodoWriteInput,
    AskUserQuestionInput, ExitPlanModeInput,
    ToolUseContent,  # Generic fallback when no specialized parser
]
```

The original `ToolUseContent` (Pydantic model) provides:
- `name`: The tool name (e.g., "Read", "Bash", "Task")
- `id`: Unique ID for pairing with results
- `input`: Raw input dictionary
- `parsed_input` property: Returns typed input model via `parse_tool_input()`

### Tool Input Models (models.py)

| Tool | Input Model | Key Fields |
|------|-------------|------------|
| Read | `ReadInput` | file_path, offset, limit |
| Write | `WriteInput` | file_path, content |
| Edit | `EditInput` | file_path, old_string, new_string, replace_all |
| MultiEdit | `MultiEditInput` | file_path, edits[] |
| Bash | `BashInput` | command, description, timeout, run_in_background |
| Glob | `GlobInput` | pattern, path |
| Grep | `GrepInput` | pattern, path, glob, type, output_mode, multiline, head_limit, offset |
| Task | `TaskInput` | prompt, subagent_type, description, model, run_in_background, resume |
| TodoWrite | `TodoWriteInput` | todos[] |
| AskUserQuestion | `AskUserQuestionInput` | questions[], question |
| ExitPlanMode | `ExitPlanModeInput` | plan, launchSwarm, teammateCount |

### Tool Input Helper Models

Some tool inputs contain nested structures with their own models:

```python
# MultiEdit tool uses EditItem for individual edits
class EditItem(BaseModel):
    old_string: str
    new_string: str

# TodoWrite tool uses TodoWriteItem for individual todos
class TodoWriteItem(BaseModel):
    content: str = ""
    status: str = "pending"
    activeForm: str = ""
    id: Optional[str] = None
    priority: Optional[str] = None

# AskUserQuestion tool uses nested models for questions/options
class AskUserQuestionOption(BaseModel):
    label: str = ""
    description: Optional[str] = None

class AskUserQuestionItem(BaseModel):
    question: str = ""
    header: Optional[str] = None
    options: List[AskUserQuestionOption] = []
    multiSelect: bool = False
```

### Tool Use Message Structure

- **CSS Class**: `tool_use` (or `tool_use sidechain`)
- **Files**: See [messages/tools/](messages/tools/) (e.g., `Read-tool_use.json`)

```json
{
  "type": "assistant",
  "message": {
    "content": [{
      "type": "tool_use",
      "id": "toolu_xxx",
      "name": "Read",
      "input": { "file_path": "/path/to/file" }
    }]
  }
}
```

---

# Part 3: System Messages (SystemTranscriptEntry)

System transcript entries (`type: "system"`) convey notifications and hook summaries.

## 3.1 Content Types for System Messages

System messages are parsed into structured content models in `models.py`:
- **SystemMessage**: For info/warning/error messages
- **HookSummaryMessage**: For hook execution summaries
- **AwaySummaryMessage**: For away_summary recap entries

## 3.2 System Info/Warning/Error → SystemMessage

- **Content Model**: `SystemMessage` (models.py)
- **CSS Class**: `system system-info`, `system system-warning`, `system system-error`
- **Files**: [system_info.json](messages/system/system_info.json)

```python
@dataclass
class SystemMessage(MessageContent):
    level: str  # "info", "warning", "error"
    text: str   # Raw text content (may contain ANSI codes)
```

```json
{
  "type": "system",
  "content": "Running PostToolUse:MultiEdit...",
  "level": "info"
}
```

## 3.3 Hook Summary → HookSummaryMessage

- **Content Model**: `HookSummaryMessage` (models.py)
- **Condition**: `subtype: "stop_hook_summary"`
- **CSS Class**: `system system-hook`

```python
@dataclass
class HookInfo:
    command: str

@dataclass
class HookSummaryMessage(MessageContent):
    has_output: bool
    hook_errors: List[str]
    hook_infos: List[HookInfo]
```

## 3.4 Away Summary (Recap) → AwaySummaryMessage

- **Content Model**: `AwaySummaryMessage` (models.py)
- **Condition**: `subtype: "away_summary"`
- **CSS Class**: `system system-away-summary`
- **Header**: `📝 Recap` (icon from `get_message_emoji`, title from `title_AwaySummaryMessage`)
- **Detail levels**: visible at EVERY level (`detail_visibility = USER_ONLY`) — a recap is itself a high-level summary of activity (#179). Suppress with `--no-recaps` (handled in `_ghost_template_by_detail`), which drops them at all levels including FULL.

Claude Code emits these system entries when a session resumes after a break — narrative prose summarising recent activity. The factory strips a trailing `" (disable recaps in /config)"` UI hint when present (suffix-match, not global) so all renderers inherit the polished form.

```python
@dataclass
class AwaySummaryMessage(MessageContent):
    text: str  # markdown-eligible recap body
```

```json
{
  "type": "system",
  "subtype": "away_summary",
  "content": "We're adding a project-level layout to validate projectId in the prepare route tree …"
}
```

---

# Part 4: Metadata Entries

These entry types primarily contain metadata, with some rendered conditionally.

## 4.1 Summary (SummaryTranscriptEntry)

- **Purpose**: Session summary for navigation
- **Files**: [summary.json](messages/system/summary.json)

```json
{
  "type": "summary",
  "summary": "Claude Code warmup for deep-manifest project",
  "leafUuid": "b83b0f5f-8bfc-4b98-8368-16162a6e9320"
}
```

The `leafUuid` links the summary to the last message of the session.

## 4.2 Queue Operation (QueueOperationTranscriptEntry)

- **Purpose**: User interrupts and steering during assistant responses
- **Rendered**: Only `remove` operations (as `UserSteeringContent`)
- **CSS Class**: `user steering`
- **Files**: [queue_operation.json](messages/system/queue_operation.json)

## 4.3 File History Snapshot

- **Purpose**: File state snapshots for undo/redo
- **Not Rendered**
- **Files**: [file_history_snapshot.json](messages/system/file_history_snapshot.json)

---

# Part 5: Renderer Content Models

These models are created during rendering to represent synthesized content not directly from JSONL entries.

## 5.1 SessionHeaderMessage

Session headers are rendered at the start of each session:

```python
@dataclass
class SessionHeaderMessage(MessageContent):
    title: str           # e.g., "Session 2025-12-13 10:30"
    session_id: str      # Session UUID
    summary: Optional[str] = None  # Session summary if available
```

## 5.2 Workflow Run Nodes (#174)

Two synthetic node types materialise a dynamic-workflow run's structure
when the splice pass attaches it at the Workflow tool_use site (see
[workflows.md](workflows.md)). They are built from the parsed run, not
from JSONL entries, and use `MessageMeta.empty()`:

```python
@dataclass
class WorkflowPhaseMessage(MessageContent):   # message_type = "workflow_phase"
    title: str = ""        # heading renders as "Phase: <title>" (🧩)
    detail: str = ""
    agent_count: int = 0

@dataclass
class WorkflowAgentMessage(MessageContent):   # message_type = "workflow_agent"
    label: str = ""        # heading renders as "Agent <label>" (🤖)
    model: str = ""
    state: str = ""
    tokens: Optional[int] = None
    tool_calls: Optional[int] = None
    result: Any = None     # dict (StructuredOutput) | str | None
    result_preview: str = ""
```

A phase's `.children` are its agent nodes; an agent's `.children` are
its grafted side-channel transcript messages.

---

# Part 6: Infrastructure Models

## 6.1 CSS Class Registry

Display styling is derived from content types using `CSS_CLASS_REGISTRY` in `html/utils.py`. This registry maps `MessageContent` subclasses to their CSS classes:

```python
CSS_CLASS_REGISTRY: dict[type[MessageContent], list[str]] = {
    # System message types
    SystemMessage: ["system"],  # level added dynamically
    HookSummaryMessage: ["system", "system-hook"],
    HookAttachmentMessage: ["system", "system-hook-attachment"],
    AwaySummaryMessage: ["system", "system-away-summary"],
    # User message types
    UserTextMessage: ["user"],
    UserSteeringMessage: ["user", "steering"],
    SlashCommandMessage: ["user", "slash-command"],
    UserSlashCommandMessage: ["user", "slash-command"],
    UserMemoryMessage: ["user"],
    CompactedSummaryMessage: ["user", "compacted"],
    CommandOutputMessage: ["user", "command-output"],
    TeammateMessage: ["user", "teammate"],
    TaskNotificationMessage: ["user", "task-notification"],
    # Assistant message types
    AssistantTextMessage: ["assistant"],
    # Tool message types
    ToolUseMessage: ["tool_use"],
    ToolResultMessage: ["tool_result"],  # error added dynamically
    # Other message types
    ThinkingMessage: ["thinking"],
    SessionHeaderMessage: ["session_header"],
    BashInputMessage: ["bash-input"],
    BashOutputMessage: ["bash-output"],
    UnknownMessage: ["unknown"],
    # Dynamic-workflow synthetic nodes (#174): tool_use keeps them under
    # the runtime "Tool Use" filter toggle; the modifier drives styling
    # and the timeline lanes.
    WorkflowPhaseMessage: ["tool_use", "workflow_phase"],
    WorkflowAgentMessage: ["tool_use", "workflow_agent"],
}
```

The `_get_css_classes_from_content()` function walks the content type's MRO to find the matching registry entry, then adds dynamic modifiers (e.g., `system-{level}` for `SystemMessage`).

The only cross-cutting modifier is `is_sidechain`, which is stored directly on `TemplateMessage` and appended to CSS classes when true.

## 6.2 UsageInfo

Token usage tracking for assistant messages:

```python
class UsageInfo(BaseModel):
    input_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    service_tier: Optional[str] = None
    server_tool_use: Optional[Dict[str, Any]] = None
```

## 6.3 BaseTranscriptEntry

Base class for all transcript entries, providing common fields:

```python
class BaseTranscriptEntry(BaseModel):
    parentUuid: Optional[str]  # UUID of parent message
    isSidechain: bool          # Whether this is a sub-agent message
    userType: str              # User type identifier
    cwd: str                   # Working directory
    sessionId: str             # Session UUID
    version: str               # Transcript format version
    uuid: str                  # Unique message ID
    timestamp: str             # ISO 8601 timestamp
    isMeta: Optional[bool] = None   # Slash command marker
    agentId: Optional[str] = None   # Sub-agent ID
    gitBranch: Optional[str] = None # Git branch name when available
```

---

# Part 7: Message Relationships

## 7.1 Hierarchy (Parent/Child)

The message hierarchy is determined by **sequence and message type**, not by `parentUuid`:

- Session headers are topmost (Level 0)
- User messages follow at Level 1
- Assistant responses and system messages nest under user messages (Level 2)
- Tool use/result pairs nest under assistant responses (Level 3)
- Sidechain messages nest under their Task result (Level 4+)

```text
Session header (Level 0)
└── User message (Level 1)
    ├── System message (Level 2)
    └── Assistant response (Level 2)
        └── Tool use/result pair (Level 3)
            └── Sidechain messages (Level 4+)
```

**Note**: `parentUuid` links messages temporally (which message preceded this one) but is not used for rendering hierarchy.

## 7.2 Tool Pairing

`tool_use` and `tool_result` messages are paired by `tool_use_id`:

| First | Last | Link |
|-------|------|------|
| `tool_use` | `tool_result` | `tool_use.id` = `tool_result.tool_use_id` |

### Other Pairings

| First | Last | Link |
|-------|------|------|
| `bash-input` | `bash-output` | Sequential |
| `thinking` | `assistant` | Sequential |
| `slash-command` | `command-output` | Sequential |

## 7.3 Sidechain Linking

Sub-agent messages (from `Task` tool):
- Have `isSidechain: true`
- Have `agentId` linking to the Task
- Appear nested under their Task result

---

# Part 8: Tool Reference

## Available Tools by Category

### File Operations

| Tool | Use Sample | Result Sample | Input Model | Output Model |
|------|------------|---------------|-------------|--------------|
| Read | [tool_use](messages/tools/Read-tool_use.json) | [tool_result](messages/tools/Read-tool_result.json) | `ReadInput` | `ReadOutput` |
| Write | [tool_use](messages/tools/Write-tool_use.json) | [tool_result](messages/tools/Write-tool_result.json) | `WriteInput` | `WriteOutput` *(TODO)* |
| Edit | [tool_use](messages/tools/Edit-tool_use.json) | [tool_result](messages/tools/Edit-tool_result.json) | `EditInput` | `EditOutput` |
| MultiEdit | [tool_use](messages/tools/MultiEdit-tool_use.json) | [tool_result](messages/tools/MultiEdit-tool_result.json) | `MultiEditInput` | — |
| Glob | [tool_use](messages/tools/Glob-tool_use.json) | [tool_result](messages/tools/Glob-tool_result.json) | `GlobInput` | `GlobOutput` *(TODO)* |
| Grep | [tool_use](messages/tools/Grep-tool_use.json) | [tool_result](messages/tools/Grep-tool_result.json) | `GrepInput` | `GrepOutput` *(TODO)* |

### Shell Operations

| Tool | Use Sample | Result Sample | Input Model | Output Model |
|------|------------|---------------|-------------|--------------|
| Bash | [tool_use](messages/tools/Bash-tool_use.json) | [tool_result](messages/tools/Bash-tool_result.json) | `BashInput` | `BashOutput` *(TODO)* |
| BashOutput | [tool_use](messages/tools/BashOutput-tool_use.json) | [tool_result](messages/tools/BashOutput-tool_result.json) | — | — |
| KillShell | [tool_use](messages/tools/KillShell-tool_use.json) | [tool_result](messages/tools/KillShell-tool_result.json) | — | — |

### Agent Operations

| Tool | Use Sample | Result Sample | Input Model | Output Model |
|------|------------|---------------|-------------|--------------|
| Task | [tool_use](messages/tools/Task-tool_use.json) | [tool_result](messages/tools/Task-tool_result.json) | `TaskInput` | `TaskOutput` |
| TaskOutput *(async-agent polling)* | — | — | `TaskOutputInput` | `TaskOutputResult` |
| TaskStop *(kill background task)* | — | — | `TaskStopInput` | `TaskStopOutput` |
| TodoWrite | [tool_use](messages/tools/TodoWrite-tool_use.json) | [tool_result](messages/tools/TodoWrite-tool_result.json) | `TodoWriteInput` | — |
| AskUserQuestion | [tool_use](messages/tools/AskUserQuestion-tool_use.json) | [tool_result](messages/tools/AskUserQuestion-tool_result.json) | `AskUserQuestionInput` | — |
| ExitPlanMode | [tool_use](messages/tools/ExitPlanMode-tool_use.json) | [tool_result](messages/tools/ExitPlanMode-tool_result.json) | `ExitPlanModeInput` | — |

### Web Operations

| Tool | Use Sample | Result Sample | Input Model | Output Model |
|------|------------|---------------|-------------|--------------|
| WebFetch | [tool_use](messages/tools/WebFetch-tool_use.json) | [tool_result](messages/tools/WebFetch-tool_result.json) | — | — |
| WebSearch | [tool_use](messages/tools/WebSearch-tool_use.json) | [tool_result](messages/tools/WebSearch-tool_result.json) | — | — |

---

## References

- [css-classes.md](css-classes.md) - Complete CSS class reference with support status
- [models.py](../claude_code_log/models.py) - Pydantic models for transcript data
- [renderer.py](../claude_code_log/renderer.py) - Main rendering module
- [html/](../claude_code_log/html/) - HTML-specific formatters (formatting only, content models in models.py)
  - [system_formatters.py](../claude_code_log/html/system_formatters.py) - SystemMessage, HookSummaryMessage, AwaySummaryMessage formatting
  - [user_formatters.py](../claude_code_log/html/user_formatters.py) - User message formatting
  - [assistant_formatters.py](../claude_code_log/html/assistant_formatters.py) - AssistantTextMessage, ThinkingMessage, ImageContent formatting
  - [tool_formatters.py](../claude_code_log/html/tool_formatters.py) - Tool use/result formatting
- [parser.py](../claude_code_log/parser.py) - JSONL parsing and text extraction
- [factories/](../claude_code_log/factories/) - Content creation from parsed data
  - [user_factory.py](../claude_code_log/factories/user_factory.py) - `create_user_message()`, `create_*_message()` functions
  - [assistant_factory.py](../claude_code_log/factories/assistant_factory.py) - `create_assistant_message()`, `create_thinking_message()`
  - [tool_factory.py](../claude_code_log/factories/tool_factory.py) - `create_tool_use_message()`, `create_tool_result_message()`
  - [system_factory.py](../claude_code_log/factories/system_factory.py) - `create_system_message()`
  - [meta_factory.py](../claude_code_log/factories/meta_factory.py) - `create_meta()`
- [rendering-architecture.md](rendering-architecture.md) - Rendering pipeline and Renderer class hierarchy
- [../work/rendering-next.md](../work/rendering-next.md) - Future rendering improvements

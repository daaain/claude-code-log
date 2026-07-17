"""Pydantic models for Claude Code transcript JSON structures."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Union, Optional, Literal

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """Primary message type classification.

    This enum covers both JSONL entry types and rendering types.
    Using str as base class maintains backward compatibility with string comparisons.

    JSONL Entry Types (from transcript files):
    - USER, ASSISTANT, SYSTEM, SUMMARY, QUEUE_OPERATION

    Rendering Types (derived during processing):
    - TOOL_USE, TOOL_RESULT, THINKING, IMAGE
    - BASH_INPUT, BASH_OUTPUT
    - SESSION_HEADER, UNKNOWN
    """

    # JSONL entry types
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    SUMMARY = "summary"
    QUEUE_OPERATION = "queue-operation"
    AI_TITLE = "ai-title"

    # Rendering/display types (derived from content)
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    IMAGE = "image"
    BASH_INPUT = "bash-input"
    BASH_OUTPUT = "bash-output"
    SESSION_HEADER = "session-header"
    UNKNOWN = "unknown"

    # System subtypes (for css_class)
    SYSTEM_INFO = "system-info"
    SYSTEM_WARNING = "system-warning"
    SYSTEM_ERROR = "system-error"


class DetailLevel(str, Enum):
    """Output detail level controlling which message types are included.

    Levels form a hierarchy: full > high > low > minimal > user-only.
    """

    FULL = "full"  # Everything
    HIGH = "high"  # Detailed but cleaned (no system/hook noise)
    LOW = "low"  # Interaction-focused + key signals
    MINIMAL = "minimal"  # User + assistant messages only
    USER_ONLY = "user-only"  # User prompts + steering only (for downstream agents)

    def includes(self, threshold: "DetailLevel") -> bool:
        """Return True iff this level is verbose enough to show content
        whose ``detail_visibility`` is declared at ``threshold``.

        Monotone-down: ``FULL.includes(X)`` is True for every ``X``;
        ``USER_ONLY.includes(X)`` is True only for ``USER_ONLY`` itself.
        """
        return _DETAIL_ORDER[self] <= _DETAIL_ORDER[threshold]


# Verbosity ordering: lower index = more verbose. The ``DetailLevel.includes``
# predicate (and ``MessageContent.visible_at``) treats a level as "verbose
# enough" iff ``order[current] <= order[threshold]``.
_DETAIL_ORDER: dict[DetailLevel, int] = {
    DetailLevel.FULL: 0,
    DetailLevel.HIGH: 1,
    DetailLevel.LOW: 2,
    DetailLevel.MINIMAL: 3,
    DetailLevel.USER_ONLY: 4,
}

# Guard against drift: if a new DetailLevel value is added without an
# entry here, ``includes`` would raise KeyError silently on first use.
# Fail loudly at import time instead. Uses ``if ... raise`` rather than
# ``assert`` so the check survives ``python -O`` / ``PYTHONOPTIMIZE``.
if set(_DETAIL_ORDER.keys()) != set(DetailLevel):
    raise RuntimeError(
        f"_DETAIL_ORDER missing entries for: {set(DetailLevel) - set(_DETAIL_ORDER.keys())}"
    )


# =============================================================================
# JSONL Content Models (Pydantic)
# =============================================================================
# Low-level content types parsed from JSONL transcript entries.
# These are defined first as they're the "input" types from transcript files.


class TextContent(BaseModel):
    """Text content block within a message content array."""

    type: Literal["text"]
    text: str


class ImageSource(BaseModel):
    """Base64-encoded image source data."""

    type: Literal["base64"]
    media_type: str
    data: str


class ImageContent(BaseModel):
    """Image content.

    This represents an image within a content array, not a standalone message.
    Images are always part of UserTextMessage.items or AssistantTextMessage.items.
    """

    type: Literal["image"]
    source: ImageSource


class UsageInfo(BaseModel):
    """Token usage information for tracking API consumption."""

    input_tokens: Optional[int] = None
    cache_creation_input_tokens: Optional[int] = None
    cache_read_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    service_tier: Optional[str] = None
    server_tool_use: Optional[dict[str, Any]] = None


class ToolUseContent(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ToolResultContent(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, list[dict[str, Any]]]
    is_error: Optional[bool] = None
    agentId: Optional[str] = None  # Reference to agent file for sub-agent messages


class ThinkingContent(BaseModel):
    type: Literal["thinking"]
    thinking: str
    signature: Optional[str] = None


# Content item types that appear in message content arrays
ContentItem = Union[
    TextContent,
    ToolUseContent,
    ToolResultContent,
    ThinkingContent,
    ImageContent,
]


class UserMessageModel(BaseModel):
    role: Literal["user"]
    content: list[ContentItem]
    usage: Optional["UsageInfo"] = (
        None  # For type compatibility with AssistantMessageModel
    )


class AssistantMessageModel(BaseModel):
    """Assistant message model."""

    id: str
    type: Literal["message"]
    role: Literal["assistant"]
    model: str
    content: list[ContentItem]
    stop_reason: Optional[str] = None
    stop_sequence: Optional[str] = None
    usage: Optional[UsageInfo] = None


# Tool result type - flexible to accept various result formats from JSONL
# The specific parsing/formatting happens in tool_formatters.py using
# ReadOutput, EditOutput, etc. (see Tool Output Content Models section)
ToolUseResult = Union[
    str,
    list[Any],  # Covers list[TodoWriteItem], list[ContentItem], etc.
    dict[str, Any],  # Covers structured results
]


class BaseTranscriptEntry(BaseModel):
    parentUuid: Optional[str]
    isSidechain: bool
    userType: str
    cwd: str
    sessionId: str
    version: str
    uuid: str
    timestamp: str
    isMeta: Optional[bool] = None
    agentId: Optional[str] = None  # Agent ID for sidechain messages
    gitBranch: Optional[str] = None  # Git branch name when available
    teamName: Optional[str] = None  # Active team name (teammates feature)
    # Synthetic (set by the loader, never by Claude Code): the id of the
    # sub-agent spawned by this entry's Agent/Task tool_use or tool_result,
    # resolved from ``subagents/agent-<id>.meta.json`` (``toolUseId``) or the
    # trunk's ``toolUseResult.agentId``. Distinct from ``agentId``, which is
    # *membership* (whose transcript this entry belongs to) — inside an agent
    # transcript the two necessarily differ, which is what makes nested
    # agent→agent spawns (issue #213) linkable.
    #
    # A single field suffices because Claude Code streams one content block
    # per assistant entry (parallel spawns arrive as separate entries) and
    # tool_results anchor 1:1 on their own entries. The degenerate
    # several-resultless-spawns-in-one-entry shape — unobserved in real
    # transcripts — degrades to the relocation tail-append, never to data
    # loss (see ``converter._apply_subagent_meta_links``).
    spawnedAgentId: Optional[str] = None


class UserTranscriptEntry(BaseTranscriptEntry):
    type: Literal["user"]
    message: UserMessageModel
    toolUseResult: Optional[ToolUseResult] = None
    agentId: Optional[str] = None  # From toolUseResult when present
    # Present on isMeta=True entries produced by a Skill tool invocation —
    # carries the id of the originating tool_use so the renderer can fold
    # the skill body into that tool_use block. See issue #93.
    sourceToolUseID: Optional[str] = None


class AssistantTranscriptEntry(BaseTranscriptEntry):
    type: Literal["assistant"]
    message: AssistantMessageModel
    requestId: Optional[str] = None


class SummaryTranscriptEntry(BaseModel):
    type: Literal["summary"]
    summary: str
    leafUuid: str
    cwd: Optional[str] = None
    sessionId: None = None  # Summaries don't have a sessionId


class AiTitleTranscriptEntry(BaseModel):
    """AI-generated session title.

    Claude Code emits these as session-level metadata (no uuid, no parent
    chain). Multiple entries may be written per session as the title is
    refined; the last one wins.
    """

    type: Literal["ai-title"]
    aiTitle: str
    sessionId: str


class SystemTranscriptEntry(BaseTranscriptEntry):
    """System messages like warnings, notifications, hook summaries, etc."""

    type: Literal["system"]
    content: Optional[str] = None
    subtype: Optional[str] = None  # e.g., "stop_hook_summary"
    level: Optional[str] = None  # e.g., "warning", "info", "error"
    # Hook summary fields (for subtype="stop_hook_summary")
    hasOutput: Optional[bool] = None
    hookErrors: Optional[list[str]] = None
    hookInfos: Optional[list[dict[str, Any]]] = None
    preventedContinuation: Optional[bool] = None
    # Compact boundary fields (for subtype="compact_boundary"); primarily
    # `preTokens`, `trigger`, `postTokens`, `durationMs`. Read at factory
    # time into SystemMessage.compact_pre_tokens / compact_trigger.
    compactMetadata: Optional[dict[str, Any]] = None


class QueueOperationTranscriptEntry(BaseModel):
    """Queue operations (enqueue/dequeue/remove) for message queueing tracking.

    enqueue/dequeue are internal operations that track when messages are queued and dequeued.
    They are parsed but not rendered, as the content duplicates actual user messages.

    'remove' operations are out-of-band user inputs made visible to the agent while working
    for "steering" purposes. These should be rendered as user messages with a 'steering' CSS class.
    Content can be a list of ContentItems or a simple string (for 'remove' operations).
    """

    type: Literal["queue-operation"]
    operation: Literal["enqueue", "dequeue", "remove", "popAll"]
    timestamp: str
    sessionId: str
    content: Optional[Union[list[ContentItem], str]] = (
        None  # List for enqueue, str for remove/popAll
    )


class PassthroughTranscriptEntry(BaseModel):
    """Structural-only entry for DAG chain continuity.

    Captures entries that have uuid/parentUuid and participate in the
    DAG chain but are not rendered (e.g. ``progress`` async-hook
    callbacks, ``agent-setting``, ``pr-link``, ``ai-title``). Without
    these, messages whose parentUuid points to a dropped entry would
    become false roots in the DAG.

    Note: ``attachment`` entries (hook callbacks, deferred tool deltas,
    etc.) are now their own typed ``AttachmentTranscriptEntry`` so the
    hook payload is available for full-detail rendering. They keep the
    same structural-DAG semantics — see ``_StructuralEntry`` in
    ``dag.py``.
    """

    uuid: str
    parentUuid: Optional[str] = None
    sessionId: str
    timestamp: str
    type: Optional[str] = None  # Original type (e.g. "progress")
    isSidechain: bool = False
    agentId: Optional[str] = None


class AttachmentTranscriptEntry(BaseTranscriptEntry):
    """Out-of-band ``type: "attachment"`` entry produced by the harness.

    Claude Code emits ``attachment`` entries for hook callbacks
    (``hook_success``, ``hook_blocking_error``, …), deferred-tool
    deltas, queued commands, file references, todo/task reminders, and
    similar harness-side metadata. They sit in the JSONL chain — they
    have ``uuid``/``parentUuid`` and participate in the DAG — but are
    not part of the user/assistant conversation.

    The ``attachment`` payload is a heterogeneous dict whose shape
    depends on ``attachment.type``. Hook flavours
    (``hook_success``, ``hook_additional_context``, ``hook_blocking_error``,
    ``hook_non_blocking_error``) are surfaced at full-detail by the
    ``HookAttachmentMessage`` factory; other flavours stay structural
    (visible in the DAG, dropped from the rendered output) until they
    grow a dedicated factory branch.

    Anchoring: the example in issue #128 confirmed ``parentUuid`` is
    the right anchor (a ``UserPromptSubmit`` hook attachment carried a
    ``toolUseID`` that matched nothing in the project — the
    ``parentUuid`` did). So this class follows the same parent edge as
    every other transcript entry.
    """

    type: Literal["attachment"]
    attachment: dict[str, Any] = Field(default_factory=dict)
    # Most real-world attachments carry the full BaseTranscriptEntry
    # context (userType/cwd/version), but minimal/synthetic fixtures
    # (and conceivably future harness shapes) may omit them. Default
    # them so validation is forgiving — the rendering layer treats
    # attachments as structural noise anyway and these fields are
    # only consulted for ``MessageMeta`` plumbing.
    userType: str = "external"  # pyright: ignore[reportIncompatibleVariableOverride]
    cwd: str = ""  # pyright: ignore[reportIncompatibleVariableOverride]
    version: str = ""  # pyright: ignore[reportIncompatibleVariableOverride]


TranscriptEntry = Union[
    UserTranscriptEntry,
    AssistantTranscriptEntry,
    SummaryTranscriptEntry,
    AiTitleTranscriptEntry,
    SystemTranscriptEntry,
    QueueOperationTranscriptEntry,
    AttachmentTranscriptEntry,
    PassthroughTranscriptEntry,
]


# =============================================================================
# Message Metadata
# =============================================================================
# Common metadata fields extracted from transcript entries.


@dataclass
class MessageMeta:
    """Common metadata extracted from transcript entries.

    These fields are shared across all message types and are used to create
    the TemplateMessage wrapper for rendering.

    Note: formatted_timestamp is computed at render time, not stored here.
    """

    # Identity fields
    session_id: str
    timestamp: str  # Raw ISO timestamp
    uuid: str
    parent_uuid: Optional[str] = None

    # Context fields
    is_sidechain: bool = False
    is_meta: bool = False  # User slash command (isMeta=True in transcript)
    source_tool_use_id: Optional[str] = (
        None  # Skill pairing (see UserTranscriptEntry.sourceToolUseID)
    )
    agent_id: Optional[str] = None
    # Sub-agent spawned by this entry's Agent/Task tool call (issue #213);
    # see BaseTranscriptEntry.spawnedAgentId for the membership/reference
    # distinction.
    spawned_agent_id: Optional[str] = None
    cwd: str = ""
    git_branch: Optional[str] = None
    team_name: Optional[str] = None  # Active team name (teammates feature)
    # The model id the assistant entry ran on (AssistantMessageModel.model),
    # e.g. ``claude-opus-4-8``. Only assistant entries carry it; None elsewhere.
    # Surfaced on sub-agent message headers so a reader can see which model a
    # spawned agent used (issue #246).
    model: Optional[str] = None

    @classmethod
    def empty(cls, uuid: str = "") -> "MessageMeta":
        """Create a placeholder MessageMeta with empty/default values.

        Useful for cases where full metadata isn't available at creation time
        (e.g., SummaryTranscriptEntry where session_id is matched later).
        """
        return cls(session_id="", timestamp="", uuid=uuid)


# =============================================================================
# Message Content Models
# =============================================================================
# Structured content models for format-neutral message representation.
# These replace the direct HTML generation in renderer.py, allowing different
# renderers (HTML, text, etc.) to format the content appropriately.


@dataclass
class MessageContent:
    """Base class for structured message content.

    Subclasses represent specific content types that renderers can format
    appropriately for their output format.

    The `meta` field is required and first positional, ensuring all message
    content always has associated metadata. Use MessageMeta.empty() when
    full metadata isn't available at creation time.

    Note: Render-time relationship data (pairing, hierarchy, children) is stored
    on TemplateMessage, not here. MessageContent is pure transcript data,
    except for message_index which links back to TemplateMessage for render-time
    lookups (e.g., accessing paired messages).
    """

    meta: MessageMeta

    # Set by RenderingContext.register() to enable content→TemplateMessage lookup
    # Using init=False to avoid dataclass inheritance issues with required fields
    message_index: Optional[int] = field(default=None, init=False, repr=False)

    @property
    def message_type(self) -> str:
        """Return the message type identifier for this content.

        Subclasses MUST override this to return their specific type.
        This is used for CSS classes, filtering, and type-based rendering.
        """
        raise NotImplementedError("Subclasses must implement message_type property")

    @property
    def has_markdown(self) -> bool:
        """Whether this content should be rendered as markdown.

        Subclasses that contain markdown content should override to return True.
        """
        return False

    def visible_at(self, detail: DetailLevel) -> bool:
        """Return True iff this content is visible at ``detail``.

        Resolution: each subclass MAY declare a class-level
        ``detail_visibility: ClassVar[DetailLevel]`` that names the LEAST
        verbose level at which it should still render. The predicate is
        monotone-down — a class declared at ``HIGH`` is visible at
        ``FULL`` and ``HIGH`` and dropped at ``LOW`` and below
        (see :meth:`DetailLevel.includes`).

        Classes without a declared threshold are always visible — useful
        for built-ins like ``UserTextMessage`` that have no level-based
        filtering rule. Plugin subclasses inherit the threshold from
        their base via normal ClassVar inheritance unless they declare
        their own (documented contract — see ``dev-docs/plugins.md``).
        """
        threshold = getattr(type(self), "detail_visibility", None)
        if threshold is None:
            return True
        return detail.includes(threshold)


@dataclass
class SystemMessage(MessageContent):
    """System message with level indicator.

    Used for info, warning, and error system messages.
    """

    # System info/warning/error noise — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    level: str  # "info", "warning", "error"
    text: str  # Raw text content (may contain ANSI codes)
    # Populated only for subtype="compact_boundary" entries: the token
    # count before compaction and what triggered it ("manual"/"auto").
    # Surfaced in the nav landmark label for /compact points.
    compact_pre_tokens: Optional[int] = None
    compact_trigger: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "system"


@dataclass
class HookInfo:
    """Information about a single hook execution."""

    command: str
    # Could add more fields like exit_code, duration, etc.


@dataclass
class HookSummaryMessage(MessageContent):
    """Hook execution summary.

    Used for subtype="stop_hook_summary" system messages.
    """

    # Hook noise — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    has_output: bool
    hook_errors: list[str]  # Error messages from hooks
    hook_infos: list[HookInfo]  # Info about each hook executed

    @property
    def message_type(self) -> str:
        return "system"


@dataclass
class AwaySummaryMessage(MessageContent):
    """Recap of recent activity, emitted by Claude Code's "away_summary" feature.

    Used for system entries with subtype="away_summary" — narrative prose
    summarising what the assistant was doing, intended to surface when the
    user comes back to a session. Distinct from HookSummaryMessage (tool noise)
    and the level-bearing SystemMessage (info/warning/error).
    """

    # Recaps are a high-level summary of activity, so they stay visible at
    # EVERY detail level — including user-only, where "user said this, agent
    # did that (just the recap)" is exactly the wanted view (#179). Declared at
    # the least-verbose threshold (USER_ONLY) via the class-attribute
    # detail-visibility mechanism. Suppress them explicitly with ``--no-recaps``
    # (handled in ``_ghost_template_by_detail``), not by lowering this.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.USER_ONLY

    text: str  # Recap prose; may contain light markdown.

    @property
    def message_type(self) -> str:
        return "system"

    @property
    def has_markdown(self) -> bool:
        return True


@dataclass
class HookAttachmentMessage(MessageContent):
    """Hook callback recorded as a ``type: "attachment"`` entry (issue #128).

    Distinct from ``HookSummaryMessage`` (which represents the
    ``stop_hook_summary`` *system* entry — a roll-up of the hooks that
    ran for a single Stop event). This one represents an individual
    hook invocation captured by the harness as an attachment, with
    full payload (command, exit code, stdout/stderr, duration). Visible
    only at ``DetailLevel.FULL``; dropped at HIGH and below alongside
    other hook noise (see ``detail_visibility`` below).

    ``kind`` distinguishes the attachment flavour:

    - ``success`` (``hook_success``): hook ran cleanly, payload is
      command + stdout/stderr + exitCode + durationMs.
    - ``additional_context`` (``hook_additional_context``):
      ``UserPromptSubmit`` / ``SessionStart`` hook injected extra
      prompt context. Payload is a list of strings in ``content``.
    - ``blocking_error`` (``hook_blocking_error``): hook prevented
      the tool call. Payload is a nested ``blockingError`` object.
    - ``non_blocking_error`` (``hook_non_blocking_error``): hook
      reported an error but didn't block. Same shape as ``success``
      but with non-zero ``exitCode``.
    """

    # Hook attachment noise — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    kind: str  # success / additional_context / blocking_error / non_blocking_error
    hook_event: str = ""  # PostToolUse / UserPromptSubmit / Stop / SessionStart / ...
    hook_name: str = ""  # e.g. "PostToolUse:TaskUpdate"
    tool_use_id: Optional[str] = None
    command: Optional[str] = None
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    content: str = ""  # Free-form hook content (joined if list)
    stdout: str = ""
    stderr: str = ""
    blocking_error: Optional[str] = None  # blockingError.blockingError text

    @property
    def message_type(self) -> str:
        return "system"


# =============================================================================
# User Message Content Models
# =============================================================================
# Structured content models for user message variants.
# These classify user text based on flags and tag patterns.


@dataclass
class SlashCommandMessage(MessageContent):
    """Content for slash command invocations (e.g., /context, /model).

    These are user messages containing command-name, command-args, and
    command-contents tags parsed from the text.
    """

    # Slash-command framing — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    command_name: str
    command_args: str
    command_contents: str

    @property
    def message_type(self) -> str:
        return "user"


@dataclass
class CommandOutputMessage(MessageContent):
    """Content for local command output (e.g., output from /context).

    These are user messages containing local-command-stdout tags.
    """

    # Local-command output — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    stdout: str
    is_markdown: bool  # True if content appears to be markdown

    @property
    def message_type(self) -> str:
        return "user"


@dataclass
class BashInputMessage(MessageContent):
    """Content for inline bash commands in user messages.

    These are user messages containing bash-input tags.
    """

    # Inline bash — kept through HIGH, dropped at LOW and below.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.HIGH

    command: str

    @property
    def message_type(self) -> str:
        return "bash-input"


@dataclass
class BashOutputMessage(MessageContent):
    """Content for bash command output.

    These are user messages containing bash-stdout and/or bash-stderr tags.
    """

    # Inline bash output — kept through HIGH, dropped at LOW and below.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.HIGH

    stdout: Optional[str] = None  # Raw stdout content (may contain ANSI codes)
    stderr: Optional[str] = None  # Raw stderr content (may contain ANSI codes)

    @property
    def message_type(self) -> str:
        return "bash-output"


# Note: ToolResultMessage and ToolUseMessage are defined in the
# "Tool Message Models" section (before Tool Input Models).


@dataclass
class CompactedSummaryMessage(MessageContent):
    """Content for compacted session summaries.

    These are user messages that contain previous conversation context
    in a compacted format when sessions run out of context.
    Parsed by parse_compacted_summary() in parser.py, formatted by
    format_compacted_summary_content() in html/user_formatters.py.
    """

    # /compact framing — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    summary_text: str

    @property
    def message_type(self) -> str:
        return "user"

    @property
    def has_markdown(self) -> bool:
        return True


@dataclass
class UserMemoryMessage(MessageContent):
    """Content for user memory input.

    These are user messages containing user-memory-input tags.
    Parsed by parse_user_memory() in parser.py, formatted by
    format_user_memory_content() in html/user_formatters.py.
    """

    # Memory-input framing — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    memory_text: str

    @property
    def message_type(self) -> str:
        return "user"


@dataclass
class UserSlashCommandMessage(MessageContent):
    """Content for slash command expanded prompts (isMeta=True).

    These are LLM-generated instruction text from slash commands.
    The text is markdown formatted and rendered as such.
    Formatted by format_user_slash_command_content() in html/user_formatters.py.
    """

    # Slash-command expansion framing — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    text: str

    @property
    def message_type(self) -> str:
        return "user"


@dataclass
class IdeOpenedFile:
    """IDE notification for an opened file."""

    content: str  # Raw content from the tag


@dataclass
class IdeSelection:
    """IDE notification for a code selection."""

    content: str  # Raw selection content


@dataclass
class IdeDiagnostic:
    """IDE diagnostic notification.

    Contains either parsed JSON diagnostics or raw content if parsing failed.
    """

    diagnostics: Optional[list[dict[str, Any]]] = None  # Parsed diagnostic objects
    raw_content: Optional[str] = None  # Fallback if JSON parsing failed


@dataclass
class IdeNotificationContent:
    """Content for IDE notification tags (embedded within user messages).

    This is NOT a MessageContent subclass - it's used as an item within
    UserTextMessage.items alongside TextContent and ImageContent.

    Represents IDE notification tags like:
    - <ide_opened_file>: File open notifications
    - <ide_selection>: Code selection notifications
    - <post-tool-use-hook><ide_diagnostics>: Diagnostic JSON arrays

    Format-neutral: stores structured data, not HTML.
    """

    opened_files: list[IdeOpenedFile]
    selections: list[IdeSelection]
    diagnostics: list[IdeDiagnostic]
    remaining_text: str  # Text after notifications extracted


@dataclass
class UserTextMessage(MessageContent):
    """Content for user text with interleaved images and IDE notifications.

    The `items` field preserves the original order of content:
    - TextContent: Text portions of the message
    - ImageContent: Inline images
    - IdeNotificationContent: IDE notification tags (extracted from text)

    Empty items list indicates no content.
    """

    # Interleaved content items preserving original order
    items: list[  # pyright: ignore[reportUnknownVariableType]
        TextContent | ImageContent | IdeNotificationContent
    ] = field(default_factory=list)

    @property
    def message_type(self) -> str:
        return "user"


@dataclass
class UserSteeringMessage(UserTextMessage):
    """Content for user steering prompts (queue-operation "remove").

    These are user messages that steer the conversation by removing
    items from the queue. Inherits from UserTextMessage.
    """

    pass


@dataclass
class TeammateMessageBlock:
    """A single <teammate-message> block extracted from a User entry.

    `is_system=True` corresponds to blocks with teammate_id="system"
    (e.g. teammate_terminated notifications).

    This is a plain data container — the renderer-facing MessageContent
    wrapper is TeammateMessage (which holds one or more of these blocks).
    """

    teammate_id: str
    body: str
    color: Optional[str] = None
    summary: Optional[str] = None
    is_system: bool = False


@dataclass
class TeammateMessage(MessageContent):
    """Content for one or more <teammate-message> blocks in a single User entry.

    Teammate messages are emitted when the experimental teammates feature
    (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) is active. A single User
    entry may carry several `<teammate-message>` blocks — potentially from
    different teammates — so the factory groups them into `blocks` and
    preserves any non-matching surrounding text in `leading_text` /
    `trailing_text`. The renderer decides whether to present each block
    as its own card or merge them into one.
    """

    blocks: list[TeammateMessageBlock] = field(
        default_factory=lambda: list[TeammateMessageBlock]()
    )
    leading_text: Optional[str] = None
    trailing_text: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "teammate"

    @property
    def has_markdown(self) -> bool:
        return True


@dataclass
class TaskNotificationUsage:
    """Usage stats reported in a ``<task-notification>`` block.

    All fields are optional — older transcripts (or non-completion
    statuses like ``failed``) may omit some.
    """

    total_tokens: Optional[int] = None
    tool_uses: Optional[int] = None
    duration_ms: Optional[int] = None


@dataclass
class TaskNotificationMessage(MessageContent):
    """User entry shape for async-agent task completion notifications.

    Claude Code's async-agent feature (issue #90) injects a User entry
    into the trunk session when an async-spawned ``Task`` (the kind
    with ``run_in_background=True``) finishes. Content is an XML-tagged
    block of the form::

        <task-notification>
        <task-id>a8b740b</task-id>
        <status>completed</status>
        <summary>Agent "..." completed</summary>
        <result>... markdown of agent's final response ...</result>
        <usage>total_tokens: 23099
        tool_uses: 2
        duration_ms: 15506</usage>
        </task-notification>
        Full transcript available at: /tmp/.../tasks/a8b740b.output

    The ``result`` body usually duplicates the last sub-assistant
    message in the spawned agent's sidechain — Phase 3 of the
    async-agents plan folds it into the spawning Task's tool_result so
    the notification card can render as a backlink-only stub.
    """

    task_id: str = ""
    status: str = ""
    summary: str = ""
    result_text: str = ""
    usage: Optional[TaskNotificationUsage] = None
    transcript_path: Optional[str] = None
    raw_text: Optional[str] = None  # Original content if parsing dropped fields
    # ``<tool-use-id>`` from the notification block — matches the
    # originating tool_use's ``id`` (e.g. ``toolu_01...``). Used to emit
    # a backlink from the notification card's Task ID value to the
    # original tool_use card (#142 — Monitor tool task-end backlink).
    # Optional because older notifications didn't carry this field.
    tool_use_id: Optional[str] = None
    # Phase 3 dedup marker: when True, ``result_text`` duplicates the
    # last sub-assistant in the spawning Task's sidechain (which is
    # already rendered inline). The renderer should then collapse the
    # body to a backlink-only stub pointing at the spawning Task's
    # message_index, preserving the uuid chain without doubling the
    # content.
    result_is_duplicate: bool = False
    spawning_task_message_index: Optional[int] = None

    @property
    def message_type(self) -> str:
        return "task_notification"

    @property
    def has_markdown(self) -> bool:
        # The <result> body is typically Markdown.
        return bool(self.result_text) and not self.result_is_duplicate


# =============================================================================
# Assistant Message Content Models
# =============================================================================
# Structured content models for assistant message variants.
# These classify assistant message parts for format-neutral rendering.


@dataclass
class AssistantTextMessage(MessageContent):
    """Content for assistant text messages with interleaved images.

    These are the text portions of assistant messages that get
    rendered as markdown with syntax highlighting.

    The `items` field preserves the original order of content:
    - TextContent: Text portions of the message
    - ImageContent: Inline images

    Empty items list indicates no content.
    """

    # Assistant prose — visible at MINIMAL and above; dropped only at
    # USER_ONLY (which keeps user prompts + steering only).
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.MINIMAL

    # Interleaved content items preserving original order
    items: list[  # pyright: ignore[reportUnknownVariableType]
        TextContent | ImageContent
    ] = field(default_factory=list)

    # Token usage string (formatted from UsageInfo when available)
    token_usage: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "assistant"

    @property
    def has_markdown(self) -> bool:
        return True


@dataclass
class ThinkingMessage(MessageContent):
    """Message for assistant thinking/reasoning blocks.

    These are the <thinking> blocks that show the assistant's
    internal reasoning process.

    Note: This is distinct from ThinkingContent (the Pydantic model
    for parsing JSONL). This dataclass is for rendering purposes.
    """

    # Thinking blocks — kept through HIGH, dropped at LOW and below.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.HIGH

    thinking: str
    signature: Optional[str] = None

    # Token usage string (formatted from UsageInfo when available)
    token_usage: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "thinking"

    @property
    def has_markdown(self) -> bool:
        return True


# Note: ToolUseMessage is also an assistant content type, defined in
# "Tool Message Models" section (before Tool Input Models).


@dataclass
class UnknownMessage(MessageContent):
    """Content for unknown/unrecognized content types.

    Used as a fallback when encountering content types that don't have
    specific handlers. Stores the type name for display purposes.
    """

    # Unknown content — visible only at FULL.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    type_name: str  # The name/description of the unknown type

    @property
    def message_type(self) -> str:
        return "unknown"


# =============================================================================
# Renderer Content Models
# =============================================================================
# Structured content models for renderer-specific elements.
# These are used by the HTML renderer but represent format-neutral data.


@dataclass
class SessionHeaderMessage(MessageContent):
    """Content for session headers in transcript rendering.

    Represents the header displayed at the start of each session
    with session title and optional summary. Includes hierarchy
    fields for parent/child session relationships.
    """

    title: str
    session_id: str
    summary: Optional[str] = None
    parent_session_id: Optional[str] = None
    parent_session_summary: Optional[str] = None
    parent_message_index: Optional[int] = None  # d-{N} index for backlink
    depth: int = 0  # 0 = root, 1 = child, etc.
    attachment_uuid: Optional[str] = None
    is_branch: bool = False  # True for within-session fork branches
    original_session_id: Optional[str] = None  # Original session_id before fork split
    first_uuid: Optional[str] = None  # First UUID in this branch (for forward links)
    # Branch preview text *before* it is composed into ``title`` by
    # ``_branch_label``. Stored separately so downstream sites
    # (fork-point box per-branch link, session/graph index nav) can
    # recompose the label from ``(session_id, preview)`` rather than
    # parsing the title string. Empty / None means "no preview" — the
    # composed title is then ``Branch • <uuid8>`` only.
    preview: Optional[str] = None
    # Teammates feature — set when the session was active in a team. Sourced
    # from the first non-None ``teamName`` of any entry in the session.
    team_name: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "session_header"


# =============================================================================
# Tool Message Models
# =============================================================================
# High-level message wrappers for tool invocations and results.
# These wrap the specialized Tool Input/Output models for rendering.


@dataclass
class ToolResultMessage(MessageContent):
    """Message for tool results with rendering context.

    Wraps ToolResultContent or specialized output with additional context
    needed for rendering, such as the associated tool name and file path.
    """

    # Tool results — kept through LOW (narrowed there by the orthogonal
    # tool-name keep-list in ``_ghost_template_by_detail``), dropped at
    # MINIMAL and below.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    tool_use_id: str
    output: (
        "ToolOutput"  # Specialized (ReadOutput, etc.) or generic (ToolResultContent)
    )
    is_error: bool = False
    tool_name: Optional[str] = None  # Name of the tool that produced this result
    file_path: Optional[str] = None  # File path for Read/Edit/Write tools

    @property
    def message_type(self) -> str:
        return "tool_result"

    @property
    def has_markdown(self) -> bool:
        """TaskOutput results contain markdown (agent responses)."""
        return isinstance(self.output, TaskOutput)


@dataclass
class ToolUseMessage(MessageContent):
    """Message for tool invocations.

    Wraps ToolUseContent with the parsed input for specialized formatting.
    Falls back to the original ToolUseContent when no specialized parser exists.
    """

    # Tool invocations — kept through LOW (narrowed there by the orthogonal
    # tool-name keep-list in ``_ghost_template_by_detail``), dropped at
    # MINIMAL and below.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    input: "ToolInput"  # Specialized (BashInput, etc.) or ToolUseContent fallback
    tool_use_id: str  # From ToolUseContent.id
    tool_name: str  # From ToolUseContent.name
    # Skill-tool pairing (issue #93): when the Skill tool is invoked,
    # the expanded slash-command body shipped as a separate isMeta=True
    # user entry is folded into this field so the Skill tool_use renders
    # as a single visual unit.
    skill_body: Optional[str] = None

    @property
    def message_type(self) -> str:
        return "tool_use"


@dataclass
class WorkflowPhaseMessage(MessageContent):
    """Synthetic node (#174 PR3): a dynamic-workflow *phase* header card.

    Spliced under a ``Workflow`` tool_use node (Strategy B — built post
    ``_build_message_tree``, not parsed from the raw transcript). Its
    ``.children`` are the phase's :class:`WorkflowAgentMessage` nodes. Built
    from a parsed ``WorkflowPhase``; uses ``MessageMeta.empty()`` for meta.
    """

    title: str = ""
    detail: str = ""
    agent_count: int = 0

    @property
    def message_type(self) -> str:
        return "workflow_phase"


@dataclass
class WorkflowAgentMessage(MessageContent):
    """Synthetic node (#174 PR3): one dynamic-workflow *sub-agent* card.

    Spliced under its :class:`WorkflowPhaseMessage` (or directly under the
    Workflow tool_use when there's no snapshot/phase grouping). Its
    ``.children`` are the agent's side-channel transcript messages. ``result``
    is the agent's output — a dict for ``StructuredOutput`` agents, a string
    for plain-text agents, or ``None`` while in flight.
    """

    label: str = ""
    model: str = ""
    state: str = ""
    tokens: Optional[int] = None
    tool_calls: Optional[int] = None
    result: Any = None
    result_preview: str = ""

    @property
    def message_type(self) -> str:
        return "workflow_agent"


# =============================================================================
# Tool Input Models
# =============================================================================
# Typed models for tool inputs.
# These provide type safety and IDE autocompletion for tool parameters.


class BashInput(BaseModel):
    """Input parameters for the Bash tool.

    Note on ``run_in_background``: this is the *caller's* hint that the
    command should run async. In practice the harness may *also*
    background a command on its own (e.g. timeout-driven) without
    setting this flag — in that case the async signal lives only on
    the result side as ``toolUseResult.backgroundTaskId``. Use
    ``minted_background_task_id`` (populated post-link-pass) as the
    authoritative signal for "is this a background spawn?", not this
    field alone.
    """

    command: str
    description: Optional[str] = None
    timeout: Optional[int] = None
    run_in_background: Optional[bool] = None
    dangerouslyDisableSandbox: Optional[bool] = None

    # Renderer-set: the minted ``background_task_id`` hoisted from the
    # matching ``BashOutput`` by ``_link_task_id_consumers``. Surfaces
    # ``#<id>`` directly on the spawn-card title (instead of burying it
    # in the result text) for background runs, making the task itself
    # visually prominent (PR #158 follow-up). Also the authoritative
    # "is background?" signal — see class docstring.
    minted_background_task_id: Optional[str] = None
    # Renderer-set: ``message_index`` of the first ``TaskOutput`` poll
    # (or ``TaskStop``) that consumed our minted id. Forward counterpart
    # to ``creating_call_message_index`` on the consumer side — wraps
    # the spawn's ``#<id>`` in a forward-link anchor when set.
    linked_consumer_message_index: Optional[int] = None


class ReadInput(BaseModel):
    """Input parameters for the Read tool."""

    file_path: str
    offset: Optional[int] = None
    limit: Optional[int] = None


class WriteInput(BaseModel):
    """Input parameters for the Write tool."""

    file_path: str
    content: str


class EditInput(BaseModel):
    """Input parameters for the Edit tool."""

    file_path: str
    old_string: str
    new_string: str
    replace_all: Optional[bool] = None


class EditItem(BaseModel):
    """Single edit item for MultiEdit tool."""

    file_path: Optional[str] = None
    old_string: str
    new_string: str


class MultiEditInput(BaseModel):
    """Input parameters for the MultiEdit tool."""

    file_path: str
    edits: list[EditItem]


class GlobInput(BaseModel):
    """Input parameters for the Glob tool."""

    pattern: str
    path: Optional[str] = None


class GrepInput(BaseModel):
    """Input parameters for the Grep tool.

    Note: Extra fields like -A, -B, -C are allowed for flexibility.
    """

    pattern: str
    path: Optional[str] = None
    glob: Optional[str] = None
    type: Optional[str] = None
    output_mode: Optional[Literal["content", "files_with_matches", "count"]] = None
    multiline: Optional[bool] = None
    head_limit: Optional[int] = None
    offset: Optional[int] = None

    model_config = {"extra": "allow"}  # Allow -A, -B, -C, -i, -n fields


class TaskInput(BaseModel):
    """Input parameters for the Task tool."""

    prompt: str
    subagent_type: str
    description: str
    model: Optional[Literal["sonnet", "opus", "haiku"]] = None
    run_in_background: Optional[bool] = None
    resume: Optional[str] = None
    # Teammate-spawned Task fields (CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1).
    # Populated when the team-lead spawns a named teammate via Task.
    team_name: Optional[str] = None
    name: Optional[str] = None
    mode: Optional[str] = None

    # Renderer-set: minted ``agentId`` from the async-launch confirmation
    # (the tool_result for a ``run_in_background=True`` ``Task``). Mirrors
    # ``BashInput.minted_background_task_id`` so the async-spawn card
    # surfaces ``#<id>`` directly in its title.
    minted_agent_id: Optional[str] = None
    # Renderer-set: ``message_index`` of the first ``TaskOutput`` poll
    # for this agent. Forward counterpart to ``creating_call_message_index``.
    linked_consumer_message_index: Optional[int] = None


class TodoWriteItem(BaseModel):
    """Single todo item for TodoWrite tool (input format).

    All fields have defaults for lenient parsing of legacy/malformed data.
    """

    content: str = ""
    status: str = "pending"  # Allow any string, not just Literal, for flexibility
    activeForm: str = ""
    id: Optional[str] = None
    priority: Optional[str] = None  # Allow any string for flexibility


class TodoWriteInput(BaseModel):
    """Input parameters for the TodoWrite tool."""

    todos: list[TodoWriteItem]


class AskUserQuestionOption(BaseModel):
    """Option for an AskUserQuestion question.

    All fields have defaults for lenient parsing.
    """

    label: str = ""
    description: Optional[str] = None


class AskUserQuestionItem(BaseModel):
    """Single question in AskUserQuestion input.

    All fields have defaults for lenient parsing.
    """

    question: str = ""
    header: Optional[str] = None
    options: list[AskUserQuestionOption] = Field(
        default_factory=lambda: list[AskUserQuestionOption]()
    )
    multiSelect: bool = False


class AskUserQuestionInput(BaseModel):
    """Input parameters for the AskUserQuestion tool.

    Supports both modern format (questions list) and legacy format (single question).
    """

    questions: list[AskUserQuestionItem] = Field(
        default_factory=lambda: list[AskUserQuestionItem]()
    )
    question: Optional[str] = None  # Legacy single question format


class ExitPlanModeInput(BaseModel):
    """Input parameters for the ExitPlanMode tool."""

    plan: str = ""
    launchSwarm: Optional[bool] = None
    teammateCount: Optional[int] = None


class WebSearchInput(BaseModel):
    """Input parameters for the WebSearch tool."""

    query: str


class MonitorInput(BaseModel):
    """Input parameters for the built-in ``Monitor`` tool.

    Streams stdout from a long-running shell command, emitting one
    notification per line. Used to watch CI checks, log tails, or
    poll-loops that emit only on state change.
    """

    description: str
    command: str
    timeout_ms: Optional[int] = None
    persistent: Optional[bool] = None


class ScheduleWakeupInput(BaseModel):
    """Input parameters for the built-in ``ScheduleWakeup`` tool.

    Schedules a self-resume tick — used by ``/loop`` dynamic mode to
    self-pace iterations. Carries a ``delaySeconds`` (clamped to
    [60, 3600] by the runtime), a one-line ``reason`` for telemetry /
    user display, and the ``prompt`` to fire on wake-up (often the
    same ``/loop …`` invocation passed back unchanged so the next
    firing repeats the task).
    """

    delaySeconds: int
    reason: str
    prompt: str


class CronCreateInput(BaseModel):
    """Input parameters for the built-in ``CronCreate`` tool.

    Schedules a prompt to be enqueued at a future time. ``cron`` is a
    standard 5-field expression in the user's local timezone;
    ``prompt`` is fired at each match. ``recurring`` (default True)
    fires on every match until deleted or the 7-day auto-expiry hits;
    ``durable`` (default False) persists the job to
    ``.claude/scheduled_tasks.json`` so it survives session restart.
    """

    cron: str
    prompt: str
    recurring: Optional[bool] = None
    durable: Optional[bool] = None


class CronListInput(BaseModel):
    """Input parameters for the built-in ``CronList`` tool.

    Lists all cron jobs in the current session. Takes no inputs.
    """

    # No fields — kept as an explicit model so dispatch routes
    # through the tool-input pipeline rather than the generic
    # fallback. Pydantic's default ``extra="ignore"`` accepts the
    # empty input dict the harness sends.


class CronDeleteInput(BaseModel):
    """Input parameters for the built-in ``CronDelete`` tool.

    Cancels a previously-scheduled cron job by id. The id is the
    short job identifier returned by ``CronCreate``.
    """

    id: str


class WebFetchInput(BaseModel):
    """Input parameters for the WebFetch tool."""

    url: str
    prompt: str


class ArtifactInput(BaseModel):
    """Input parameters for the Artifact tool.

    Deploys a self-contained HTML or Markdown file as a (default-private)
    claude.ai web page. Only ``file_path`` is required here — the live tool
    schema also requires ``favicon``, but it's defaulted so variant
    transcripts still parse. Keep the rest optional and tolerate unknown
    fields so schema tweaks across Claude Code versions don't drop the
    message to the generic params table.
    """

    file_path: str
    favicon: str = ""
    description: Optional[str] = None
    label: Optional[str] = None
    url: Optional[str] = None  # Existing artifact URL to redeploy to
    force: Optional[bool] = None

    model_config = {"extra": "allow"}


class SkillInput(BaseModel):
    """Input parameters for the Skill tool."""

    skill: str

    # Skill calls may carry an optional ``args`` string and Claude Code
    # has shipped variants over time; tolerate unknown fields rather than
    # falling back to the generic params table.
    model_config = {"extra": "allow"}


# =============================================================================
# Teammates feature tool inputs
# =============================================================================
# CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 surfaces six team-management tools.
# Schemas are lenient (model_config extra="allow") so unknown fields survive.


class TeamCreateInput(BaseModel):
    """Input parameters for the TeamCreate tool."""

    team_name: str = ""
    description: Optional[str] = None
    agent_type: Optional[str] = None

    model_config = {"extra": "allow"}


class TeamDeleteInput(BaseModel):
    """Input parameters for the TeamDelete tool (typically empty)."""

    team_name: Optional[str] = None

    model_config = {"extra": "allow"}


class TaskCreateInput(BaseModel):
    """Input parameters for the TaskCreate tool (teammate task board)."""

    subject: str = ""
    description: Optional[str] = None
    activeForm: Optional[str] = None

    model_config = {"extra": "allow"}


class TaskUpdateInput(BaseModel):
    """Input parameters for the TaskUpdate tool (teammate task board)."""

    taskId: str = ""
    owner: Optional[str] = None
    status: Optional[str] = None

    # Renderer-set: message_index of the originating ``TaskCreate``
    # whose tool_result minted this taskId. Wired by
    # ``_link_task_id_consumers`` so the title formatter can wrap
    # ``#<taskId>`` in an anchor pointing back to the create card
    # (#154). Optional because the create call may live outside the
    # loaded slice (multi-session loads, partial fixtures).
    creating_call_message_index: Optional[int] = None

    model_config = {"extra": "allow"}


class TaskListInput(BaseModel):
    """Input parameters for the TaskList tool (empty in practice)."""

    model_config = {"extra": "allow"}


class SendMessageInput(BaseModel):
    """Input parameters for the SendMessage tool (team-lead → teammate)."""

    type: Optional[str] = None
    recipient: Optional[str] = None
    content: str = ""

    model_config = {"extra": "allow"}


class TaskOutputInput(BaseModel):
    """Input parameters for the TaskOutput polling tool (async agents).

    Async-spawned ``Task`` agents return their result later via a
    ``<task-notification>`` user message; the assistant can also poll
    explicitly with ``TaskOutput`` between launch and notification.

    The same tool also polls ``run_in_background=true`` Bash calls
    (``taskType: local_bash``) — both shapes share the
    ``task_id`` → originating-call cross-link wired by
    ``_link_task_id_consumers``.
    """

    task_id: str = ""
    block: bool = False
    timeout: Optional[int] = None

    # Renderer-set: message_index of the originating tool_use whose
    # result minted this task_id (a ``Bash`` with ``run_in_background``
    # for ``local_bash`` taskType, or a ``Task`` with
    # ``run_in_background`` for ``local_agent`` taskType). Wired by
    # ``_link_task_id_consumers`` so the title formatter can wrap
    # ``#<task_id>`` in an anchor pointing back to the spawn card
    # (#154). Optional because the spawn may live outside the loaded
    # slice or use a synchronous shape that doesn't echo the id back.
    creating_call_message_index: Optional[int] = None

    model_config = {"extra": "allow"}


class TaskStopInput(BaseModel):
    """Input parameters for the TaskStop tool (kill a background task).

    Counterpart to ``TaskOutput``: same ``task_id`` shape, same id
    space (background-process ids minted by ``Bash`` with
    ``run_in_background=true`` or async-agent ``Task`` launches). The
    only field is the id of the background task to terminate.

    Cross-links to the spawn card the same way ``TaskOutputInput``
    does (PR #158 follow-up) — shares ``_link_task_id_consumers``.
    """

    task_id: str = ""

    # Renderer-set: same role as ``TaskOutputInput.creating_call_message_index``
    # — message_index of the originating spawn card so the formatter
    # can wrap ``#<task_id>`` in a backlink anchor.
    creating_call_message_index: Optional[int] = None

    model_config = {"extra": "allow"}


class WorkflowToolInput(BaseModel):
    """Input parameters for the dynamic ``Workflow`` tool (issue #174).

    A Workflow tool_use comes in several invocation shapes: inline ``script``
    (the JavaScript orchestrator source), ``scriptPath`` (source in a file on
    disk — no source in the input at all), or ``name`` (a saved workflow),
    optionally combined with ``args`` (the script's input value) and
    ``resumeFromRunId`` (resume a prior run). Only the inline shape carries
    the source; for the other shapes the renderer falls back to the script
    stored in the run's terminal snapshot. The effective script's
    ``export const meta = {...}`` block (name / description / phases) is
    surfaced as a header and the script body is syntax-highlighted as
    JavaScript by the formatter.
    """

    script: str = ""
    script_path: str = Field(default="", alias="scriptPath")
    name: str = ""
    args: Optional[Any] = None
    resume_from_run_id: str = Field(default="", alias="resumeFromRunId")

    # Renderer-set (issue #174 PR3): the parsed WorkflowRun linked to this
    # tool_use by taskId, when its <runId>.json snapshot was found on disk.
    # Lets the formatter prefer the authoritative snapshot meta (name /
    # description / phases) over the best-effort JS-`meta` regex. Typed Any to
    # avoid importing the dataclass into this Pydantic model.
    workflow_run: Optional[Any] = None

    # Renderer-set by the splice pass: message_index of each spliced
    # WorkflowPhaseMessage card, in snapshot-phase order (parallel to the
    # header's phase-pill list, which comes from the same snapshot). Lets the
    # formatter render each pill as a #msg-d-{N} anchor link to its phase card.
    phase_anchor_indices: Optional[list[int]] = None

    model_config = {"extra": "allow"}


# Union of all typed tool inputs
ToolInput = Union[
    BashInput,
    ReadInput,
    WriteInput,
    EditInput,
    MultiEditInput,
    GlobInput,
    GrepInput,
    TaskInput,
    TodoWriteInput,
    AskUserQuestionInput,
    ExitPlanModeInput,
    WebSearchInput,
    WebFetchInput,
    ArtifactInput,
    MonitorInput,
    ScheduleWakeupInput,
    CronCreateInput,
    CronListInput,
    CronDeleteInput,
    SkillInput,
    TeamCreateInput,
    TeamDeleteInput,
    TaskCreateInput,
    TaskUpdateInput,
    TaskListInput,
    SendMessageInput,
    TaskOutputInput,
    TaskStopInput,
    WorkflowToolInput,
    ToolUseContent,  # Generic fallback when no specialized parser
]


# =============================================================================
# Tool Output Models
# =============================================================================
# Typed models for tool outputs (symmetric with Tool Input Models).
# These are data containers stored inside ToolResultMessage.output,
# NOT standalone message types (so they don't inherit from MessageContent).


@dataclass
class ReadOutput:
    """Parsed Read tool output.

    Represents the result of reading a file with optional line range.
    Symmetric with ReadInput for tool_use → tool_result pairing.
    """

    file_path: str
    content: str  # File content (may be truncated)
    start_line: int  # 1-based starting line number
    num_lines: int  # Number of lines in content
    total_lines: int  # Total lines in file
    is_truncated: bool  # Whether content was truncated
    system_reminder: Optional[str] = None  # Embedded system reminder text


@dataclass
class WriteOutput:
    """Parsed Write tool output.

    Symmetric with WriteInput for tool_use → tool_result pairing.
    """

    file_path: str
    success: bool
    message: str  # First line acknowledgment (truncated from full output)


@dataclass
class EditDiff:
    """Single diff hunk for edit operations."""

    old_text: str
    new_text: str


@dataclass
class EditOutput:
    """Parsed Edit tool output.

    Contains diff information for file edits.
    Symmetric with EditInput for tool_use → tool_result pairing.
    """

    file_path: str
    success: bool
    diffs: list[EditDiff]  # Changes made
    message: str  # Result message or code snippet
    start_line: int = 1  # Starting line number for code display


@dataclass
class BashOutput:
    """Parsed Bash tool output.

    Symmetric with BashInput for tool_use → tool_result pairing.
    Contains the output with ANSI flag for terminal formatting.

    ``background_task_id`` is the short alphanumeric id the harness
    mints when ``run_in_background=true`` is set on the call. The
    structured ``backgroundTaskId`` field is more reliable than text-
    parsing the confirmation paragraph; we capture it for the
    cross-link from later ``TaskOutput`` polling cards back to the
    spawning Bash call (#154).
    """

    content: str  # Output content (stdout/stderr combined)
    has_ansi: bool  # True if content contains ANSI escape sequences
    background_task_id: Optional[str] = None


@dataclass
class AgentResultMetadata:
    """Structured metadata parsed from the Markdown tail of a Task tool_result.

    Teammate-spawned agents (and async task agents) embed linking and
    accounting info at the end of the agent's response:

        agentId: <id> (use SendMessage with to: '...' to continue this agent)
        worktreePath: /.../worktrees/agent-<id>
        worktreeBranch: worktree-agent-<id>
        <usage>total_tokens: 48421
        tool_uses: 24
        duration_ms: 802753</usage>

    Exposes the parsed fields so the renderer can surface them structurally
    (and so the converter can use `agent_id` to link subagent JSONL files
    back to their spawning Task tool_use).
    """

    agent_id: Optional[str] = None
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
    total_tokens: Optional[int] = None
    tool_uses: Optional[int] = None
    duration_ms: Optional[int] = None


@dataclass
class TaskOutput:
    """Parsed Task (sub-agent) tool output.

    Symmetric with TaskInput for tool_use → tool_result pairing.
    Contains the agent's final response as markdown. Teammate-spawned tasks
    also populate `metadata` (see AgentResultMetadata) — set by the tool
    factory when the result tail carries `agentId:` / `worktreePath:` / ...
    """

    result: str  # Agent's response (markdown), metadata tail stripped
    metadata: Optional[AgentResultMetadata] = None
    # Teammate-spawn pathway fields. Populated when the Task was spawned for
    # a named teammate; sourced from the tool_use input and/or tool_result.
    teammate_id: Optional[str] = None
    agent_id: Optional[str] = None
    color: Optional[str] = None
    # Async-agent (issue #90) fold: when the spawn's ``result`` body is
    # just the "Async agent launched successfully…" stub, the actual
    # answer arrives later via the ``<task-notification>`` User entry
    # AND lives at the end of the spawned agent's sidechain.
    # ``_link_async_notifications`` copies it here from the matching
    # last sub-assistant so the renderer can fold the answer into the
    # spawn's tool_result card. The same pass removes the duplicated
    # sub-assistant from the sidechain rendering so the answer doesn't
    # appear twice.
    async_final_answer: Optional[str] = None


@dataclass
class GlobOutput:
    """Parsed Glob tool output.

    Symmetric with GlobInput for tool_use → tool_result pairing.

    TODO: Not currently used - tool results handled as raw strings.
    """

    pattern: str
    files: list[str]  # Matching file paths
    truncated: bool  # Whether list was truncated


@dataclass
class GrepOutput:
    """Parsed Grep tool output.

    Symmetric with GrepInput for tool_use → tool_result pairing.

    TODO: Not currently used - tool results handled as raw strings.
    """

    pattern: str
    matches: list[str]  # Matching lines/files
    output_mode: str  # "content", "files_with_matches", or "count"
    truncated: bool


@dataclass
class AskUserQuestionAnswer:
    """Single Q&A pair from AskUserQuestion result.

    When the structured ``toolUseResult`` is available it also carries the
    originating question's ``header`` / ``options`` / ``multi_select`` so the
    answer can be rendered as a self-contained card (the offered options with
    the chosen one highlighted) instead of a bare Q→A line.
    """

    question: str
    answer: str
    header: Optional[str] = None
    options: list[AskUserQuestionOption] = field(
        default_factory=lambda: list[AskUserQuestionOption]()
    )
    multi_select: bool = False


@dataclass
class AskUserQuestionOutput:
    """Parsed AskUserQuestion tool output.

    Symmetric with AskUserQuestionInput for tool_use → tool_result pairing.
    Contains the Q&A pairs extracted from the result message.
    """

    answers: list[AskUserQuestionAnswer]  # Q&A pairs
    raw_message: str  # Original message for fallback


@dataclass
class ExitPlanModeOutput:
    """Parsed ExitPlanMode tool output.

    Symmetric with ExitPlanModeInput for tool_use → tool_result pairing.
    Truncates redundant plan echo on success.
    """

    message: str  # Truncated message (without redundant plan)
    approved: bool  # Whether the plan was approved


@dataclass
class MonitorOutput:
    """Parsed output for the built-in ``Monitor`` tool's start
    confirmation.

    The result text is a single paragraph like ``Monitor started
    (task <id>, timeout <n>ms). You will be notified on each event.
    Keep working — do not poll or sleep. Events may arrive while
    you are waiting for the user — an event is not their reply.``

    We capture the raw text and (optionally) the parsed task id
    when the format matches; the renderer uses the raw text by
    default and the task id is currently informational only.
    """

    text: str
    task_id: Optional[str] = None


@dataclass
class ScheduleWakeupOutput:
    """Parsed output for ``ScheduleWakeup`` — the start confirmation.

    The harness emits a one-line confirmation like
    ``Next wakeup scheduled for HH:MM:SS (in Ns).``. Captured
    verbatim; ``next_at`` and ``in_seconds`` parsed when the format
    matches for downstream consumers (currently informational only).
    """

    text: str
    next_at: Optional[str] = None
    in_seconds: Optional[int] = None


@dataclass
class CronCreateOutput:
    """Parsed output for ``CronCreate``.

    The harness echoes a short confirmation that includes the new
    job id (e.g. ``Scheduled cron job <id>``); we capture the raw
    text and the parsed id when the format matches.
    """

    text: str
    job_id: Optional[str] = None


@dataclass
class CronListItem:
    """One row in a ``CronList`` result.

    Field names match the harness's real ``CronList`` output
    (captured during the #148 experiment):
      ``<id> — <description> (<kind>) [<scope>]: <prompt>``

    The harness echoes a *human-readable* schedule (``Every 2 minutes``
    / ``Daily at 8:57``), not the original cron expression — recovered
    from the originating ``CronCreate`` card upstream when needed.

    ``creating_call_message_index`` is set by the renderer's
    ``_link_cron_jobs_by_id`` pass when a ``CronCreate`` call earlier
    in the transcript produced this job id; the formatter wraps the
    rendered ``id`` cell in an anchor pointing back to the originating
    card. Optional because the create call may not be in the loaded
    transcript (multi-session sessions, partial loads).
    """

    id: str
    description: str
    prompt: str
    recurring: Optional[bool] = None
    durable: Optional[bool] = None
    creating_call_message_index: Optional[int] = None


@dataclass
class CronListOutput:
    """Parsed output for ``CronList``.

    Captures the raw text body for fallback display plus an
    optional structured list of jobs when the format is parseable.
    The format is documented loosely in the harness; the renderer
    falls back to the raw text whenever parsing doesn't yield a
    populated list.
    """

    text: str
    jobs: list[CronListItem]


@dataclass
class CronDeleteOutput:
    """Parsed output for ``CronDelete`` — short status line.

    The harness echoes back the cancelled job id (``Cancelled job
    <id>.``); we capture it for the cross-link from the rendered
    status text back to the originating ``CronCreate`` card. The
    ``creating_call_message_index`` is wired by the same renderer
    pass that populates ``CronListItem.creating_call_message_index``.
    """

    text: str
    job_id: Optional[str] = None
    creating_call_message_index: Optional[int] = None


@dataclass
class WebSearchLink:
    """Single search result link from WebSearch output."""

    title: str
    url: str


@dataclass
class WebSearchOutput:
    """Parsed WebSearch tool output.

    Symmetric with WebSearchInput for tool_use → tool_result pairing.
    Parsed as preamble/links/summary for flexible rendering.
    """

    query: str
    links: list[WebSearchLink]
    preamble: Optional[str] = None  # Text before the Links (usually query header)
    summary: Optional[str] = None  # Markdown analysis after the links


@dataclass
class WebFetchOutput:
    """Parsed WebFetch tool output.

    Symmetric with WebFetchInput for tool_use → tool_result pairing.
    Contains the fetched URL's processed content as markdown.
    """

    url: str  # The URL that was fetched
    result: str  # The processed markdown result
    bytes: Optional[int] = None  # Size of fetched content
    code: Optional[int] = None  # HTTP status code
    code_text: Optional[str] = None  # HTTP status text (e.g., "OK")
    duration_ms: Optional[int] = None  # Time taken in milliseconds


@dataclass
class ArtifactOutput:
    """Parsed Artifact tool output.

    Symmetric with ArtifactInput for tool_use → tool_result pairing.
    Wire format (observed on Claude Code 2.1.198): text content
    ``Published <path> at <url>`` plus a structured toolUseResult
    ``{url, path, title}`` where ``title`` is extracted from the page.
    """

    url: Optional[str] = None  # Deployed page URL
    path: Optional[str] = None  # Source file that was published
    title: Optional[str] = None  # Page title extracted by the harness
    raw_text: Optional[str] = None  # Fallback text when structured data absent
    # Carried over from the paired tool_use input (the wire result has no
    # favicon/label) so the result line can show them (issue #262).
    favicon: Optional[str] = None
    label: Optional[str] = None


# =============================================================================
# Teammates feature tool outputs
# =============================================================================


@dataclass
class TeamCreateOutput:
    """Parsed TeamCreate tool output."""

    team_name: str
    team_file_path: Optional[str] = None
    lead_agent_id: Optional[str] = None
    raw_text: Optional[str] = None  # Original content if JSON parsing failed


@dataclass
class TeamDeleteOutput:
    """Parsed TeamDelete tool output."""

    success: bool
    message: str = ""
    team_name: Optional[str] = None
    active_members: Optional[list[str]] = None
    raw_text: Optional[str] = None


@dataclass
class TaskCreateOutput:
    """Parsed TaskCreate tool output (teammate task board)."""

    task_id: str
    subject: str = ""
    raw_text: Optional[str] = None


@dataclass
class TaskStatusChange:
    """from → to status transition for TaskUpdate."""

    from_status: Optional[str] = None
    to_status: Optional[str] = None


@dataclass
class TaskUpdateOutput:
    """Parsed TaskUpdate tool output (teammate task board)."""

    success: bool
    task_id: str = ""
    updated_fields: Optional[dict[str, Any]] = None
    status_change: Optional[TaskStatusChange] = None
    raw_text: Optional[str] = None


@dataclass
class TaskListItem:
    """Single task row in TaskList output."""

    id: str
    subject: str = ""
    status: Optional[str] = None
    owner: Optional[str] = None
    blocked_by: Optional[list[str]] = None


@dataclass
class TaskListOutput:
    """Parsed TaskList tool output (teammate task board)."""

    tasks: list[TaskListItem]
    raw_text: Optional[str] = None


@dataclass
class SendMessageOutput:
    """Parsed SendMessage tool output (team-lead → teammate)."""

    success: bool
    message: str = ""
    request_id: Optional[str] = None
    target: Optional[str] = None
    raw_text: Optional[str] = None


@dataclass
class TaskOutputResult:
    """Parsed TaskOutput polling tool result (async agents).

    The raw payload is XML-tagged metadata wrapping a (usually
    truncated) snapshot of the agent's transcript. We surface the
    metadata and discard the snapshot — the agent's full transcript
    already renders inline as a sidechain in our HTML, and the
    completion result reaches the trunk via the
    ``<task-notification>`` user message.
    """

    retrieval_status: str = ""
    task_id: str = ""
    task_type: str = ""
    status: str = ""
    output_truncated: bool = False
    output_file: Optional[str] = None
    raw_text: Optional[str] = None


@dataclass
class TaskStopOutput:
    """Parsed TaskStop tool result.

    Two real-world shapes observed:

    - **Success** — ``toolUseResult = {"message": "Successfully stopped
      task: <id> (<echoed command>); ..."}``. We surface the success
      flag and the message verbatim; the message body usually echoes
      back the original command which is itself informative.
    - **Error** — ``toolUseResult = "Error: No task found with ID:
      <id>"`` (plain string). The common case in practice — the task
      already completed naturally before the stop landed. We capture
      the error text and set ``stopped=False``.

    Symmetric with ``TaskStopInput``. The renderer reads ``stopped``
    to choose between success and error styling.
    """

    stopped: bool  # True on success, False on error / not-found
    message: str = ""  # Human-readable message from the harness


# Union of all specialized output types + ToolResultContent as generic fallback
ToolOutput = Union[
    ReadOutput,
    WriteOutput,
    EditOutput,
    BashOutput,
    TaskOutput,
    AskUserQuestionOutput,
    ExitPlanModeOutput,
    WebSearchOutput,
    WebFetchOutput,
    ArtifactOutput,
    MonitorOutput,
    ScheduleWakeupOutput,
    CronCreateOutput,
    CronListOutput,
    CronDeleteOutput,
    TeamCreateOutput,
    TeamDeleteOutput,
    TaskCreateOutput,
    TaskUpdateOutput,
    TaskListOutput,
    SendMessageOutput,
    TaskOutputResult,
    TaskStopOutput,
    # TODO: Add as parsers are implemented:
    # GlobOutput, GrepOutput
    ToolResultContent,  # Generic fallback for unparsed results
]

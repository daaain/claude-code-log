"""HTML-specific rendering utilities.

This module contains all HTML generation code:
- CSS class computation from message type and modifiers
- Message emoji generation
- HTML escaping and markdown rendering
- Collapsible content rendering
- Tool-specific HTML formatters
- Message content HTML rendering
- Template environment management

The functions here transform format-neutral TemplateMessage data into
HTML-specific output.
"""

import functools
import html
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

import mistune
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .renderer_code import highlight_code_with_pygments, truncate_highlighted_preview
from ..models import (
    AssistantTextMessage,
    AwaySummaryMessage,
    HookAttachmentMessage,
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    HookSummaryMessage,
    MessageContent,
    SessionHeaderMessage,
    SlashCommandMessage,
    SystemMessage,
    TaskNotificationMessage,
    TeammateMessage,
    ThinkingMessage,
    ToolResultMessage,
    ToolUseMessage,
    UnknownMessage,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserSteeringMessage,
    UserTextMessage,
    WorkflowAgentMessage,
    WorkflowPhaseMessage,
)
from ..renderer_timings import timing_stat

if TYPE_CHECKING:
    from ..renderer import TemplateMessage


# -- Auto-memory detection ----------------------------------------------------
# Claude's auto-memory (https://code.claude.com/docs/en/memory#auto-memory)
# has no dedicated tool: it surfaces in transcripts as ordinary Read/Write/Edit
# calls whose file_path points into the per-project memory directory. We anchor
# on the full default path ``~/.claude/projects/<slug>/memory/`` rather than a
# bare ``memory/`` substring so a project's own ``memory/`` folder can't yield
# false positives (issue #192).
#
# The regex is forward-slash anchored; ``_normalize_sep`` folds Windows
# backslash paths to forward slashes first so detection works on windows-latest
# (transcript file_paths may use the native separator).
#
# Limitation (acceptable for v1; see work/parse-memory-spike.md): a custom
# ``autoMemoryDirectory`` setting relocates memory outside this path and won't
# be detected — that needs config plumbing, so it stays deferred.
_MEMORY_PATH_RE = re.compile(r"/\.claude/projects/[^/]+/memory/")


# File tools whose paths we flag as memory interactions. Bash references a
# memory path inside its command string (not a typed field) and is rare, so
# it's intentionally out of scope for v1 (issue #192).
_MEMORY_TOOL_NAMES = frozenset({"Read", "Write", "Edit"})


def _normalize_sep(file_path: str) -> str:
    """Fold Windows backslash separators to forward slashes for matching."""
    return file_path.replace("\\", "/")


def is_memory_path(file_path: Optional[str]) -> bool:
    """True if ``file_path`` lives inside an auto-memory directory."""
    return (
        bool(file_path)
        and _MEMORY_PATH_RE.search(_normalize_sep(file_path)) is not None
    )


# -- Markdown-file detection --------------------------------------------------
# A file whose own format is Markdown. Read/Write of such a file renders the
# body as Markdown rather than Pygments-highlighted source (issue #232).
# Auto-memory files (always ``.md``) are a subset: ``is_memory_path`` ⊂
# ``is_markdown_path``. The memory specialization that survives is the 🧠 title
# (and memory's relative-link resolution + always-Markdown body); for any other
# ``.md`` the generalization keys on this predicate instead.
_MARKDOWN_EXTS = (".md", ".markdown")


def is_markdown_path(file_path: Optional[str]) -> bool:
    """True if ``file_path`` names a Markdown file (``.md`` / ``.markdown``)."""
    return bool(file_path) and _normalize_sep(file_path).lower().endswith(
        _MARKDOWN_EXTS
    )


def is_memory_tool(tool_name: Optional[str], file_path: Optional[str]) -> bool:
    """True if a tool call/result is an auto-memory interaction.

    A file tool (Read/Write/Edit) acting on a path inside a ``memory/``
    directory. Used to tag both the ``tool_use`` call and its paired
    ``tool_result`` with the ``memory`` CSS modifier.
    """
    return tool_name in _MEMORY_TOOL_NAMES and is_memory_path(file_path)


def memory_short_path(file_path: str) -> str:
    """Return the path relative to the ``memory/`` directory.

    e.g. ``…/memory/MEMORY.md`` -> ``MEMORY.md``,
    ``…/memory/sub/topic.md`` -> ``sub/topic.md``. Separators are normalized to
    ``/`` (so Windows paths render consistently). Falls back to the normalized
    path if the marker isn't found (shouldn't happen for memory paths).
    """
    normalized = _normalize_sep(file_path)
    parts = _MEMORY_PATH_RE.split(normalized, maxsplit=1)
    return parts[-1] if len(parts) > 1 else normalized


# Relative href/src in rendered memory-file markdown (e.g. a MEMORY.md link to
# a sibling topic file: ``feedback_x.md``). Absolute URLs (scheme://), in-page
# anchors (#…), root-absolute (/…) and protocol-relative (//…) are left alone.
_RELATIVE_LINK_ATTR_RE = re.compile(r'((?:href|src)=")([^"#/][^":]*)(")', re.IGNORECASE)


def resolve_memory_body_links(html: str, file_path: str) -> str:
    """Anchor relative links in rendered memory-file markdown to the memory dir.

    Memory files live at ``…/<slug>/memory/`` and link to sibling memory files
    with bare relative paths. When that markdown is rendered into a transcript
    page that lives one level up (``…/<slug>/``), the browser resolves those
    links against the page's directory and drops the ``memory/`` segment (#192).
    Rewriting them to ``file://`` URLs under the memory file's own directory
    fixes the target and is independent of where the page is written.
    """
    dir_path = _normalize_sep(file_path).rsplit("/", 1)[0]
    # file:// URLs need a leading slash before the path. POSIX paths already
    # start with "/" (-> file:///home/...); a Windows drive-letter path
    # (C:/Users/...) does not, so add one (-> file:///C:/Users/...).
    if not dir_path.startswith("/"):
        dir_path = "/" + dir_path
    base = "file://" + dir_path

    def _rewrite(m: "re.Match[str]") -> str:
        target = m.group(2)
        # Skip anything with a scheme (foo://…, mailto:) — the ``[^":]`` guard
        # in the pattern already excludes ``:`` so only truly relative targets
        # reach here, but keep the wrapper resilient.
        return f"{m.group(1)}{base}/{target}{m.group(3)}"

    return _RELATIVE_LINK_ATTR_RE.sub(_rewrite, html)


# -- CSS Class Registry -------------------------------------------------------
# Maps content types to their CSS classes.
# The first class is typically the base type (user, assistant, system, etc.),
# followed by any static modifiers.

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
    # Async-agent <task-notification> entry (issue #90). Carries the
    # `user` class so the runtime "User" filter toggle keeps it visible
    # — without it, the rendered <div> would only carry the
    # `task-notification` modifier, which matches no filter toolbar
    # type and would be permanently flagged `filtered-hidden`. The
    # modifier itself uses hyphens to match the rest of this registry
    # (`system-hook`, `slash-command`, `command-output`, `bash-input`,
    # `bash-output`, `task-notification-card`/`-backlink` in CSS) — the
    # underlying message-type identifier `"task_notification"` (set on
    # `TaskNotificationMessage.message_type`) keeps its underscore.
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
    # Dynamic-workflow synthetic nodes (#174 PR3): phase/agent cards spliced
    # under a Workflow tool_use. The `tool_use` class keeps them visible under
    # the runtime "Tool Use" filter toggle (the splice lives inside a tool_use
    # subtree); the `workflow_phase`/`workflow_agent` modifiers drive styling
    # and the timeline's dedicated detection branch.
    WorkflowPhaseMessage: ["tool_use", "workflow_phase"],
    WorkflowAgentMessage: ["tool_use", "workflow_agent"],
}


def _get_css_classes_from_content(content: MessageContent) -> list[str]:
    """Get CSS classes from content type using the registry.

    Walks the MRO to find a matching registry entry, then adds
    any dynamic modifiers based on content attributes.
    """
    for cls in type(content).__mro__:
        if not issubclass(cls, MessageContent):
            continue
        if classes := CSS_CLASS_REGISTRY.get(cls):
            result = list(classes)
            # Dynamic modifiers based on content attributes
            if isinstance(content, SystemMessage):
                result.append(f"system-{content.level}")
            elif isinstance(content, ToolResultMessage):
                if content.is_error:
                    result.append("error")
                # Memory interaction (recalled memory): tag the result so the
                # filter/timeline hide or isolate the whole call+result pair.
                if is_memory_tool(content.tool_name, content.file_path):
                    result.append("memory")
            elif isinstance(content, ToolUseMessage):
                # Memory interaction (writing/recalling memory): a Read/Write/
                # Edit on a path inside the project's memory/ directory (#192).
                file_path = getattr(content.input, "file_path", None)
                if is_memory_tool(content.tool_name, file_path):
                    result.append("memory")
            return result
    return []


# -- CSS and Message Display --------------------------------------------------


def css_class_from_message(msg: "TemplateMessage") -> str:
    """Generate CSS class string from message type and modifiers.

    Uses CSS_CLASS_REGISTRY to derive classes from content type,
    with fallback to msg.type for messages without registered content.

    The order of classes follows the original pattern:
    1. Message type (from content type or msg.type fallback)
    2. Content-derived modifiers (e.g., slash-command, compacted, error)
    3. Cross-cutting modifier flags: steering, sidechain

    Args:
        msg: The template message to generate CSS classes for

    Returns:
        Space-separated CSS class string (e.g., "user slash-command sidechain")
    """
    # Get base classes and content-derived modifiers from content type
    if msg.content:
        parts = _get_css_classes_from_content(msg.content)
        if not parts:
            parts = [msg.type]  # Fallback if content type not in registry
    else:
        parts = [msg.type]

    # Cross-cutting modifier flags (not derivable from content type alone)
    if msg.is_sidechain:
        parts.append("sidechain")

    # Agent-nesting depth (#213 visual layer). A message at depth d (d >= 1)
    # carries:
    #   - ``agent-depth-{d}``       exact depth (data / tests / timeline)
    #   - ``agent-ring-{1..5}``     5-colour cycle bucket for the group line
    #   - ``agent-deep``            d >= 6 → compress the indent step
    # All three are derivable from d, but CSS can't compute them, so we emit
    # the classes. Depth 0 (trunk / non-agent) adds nothing.
    if msg.agent_depth >= 1:
        d = msg.agent_depth
        parts.append(f"agent-depth-{d}")
        parts.append(f"agent-ring-{((d - 1) % 5) + 1}")
        if d >= 6:
            parts.append("agent-deep")

    return " ".join(parts)


def is_session_header(msg: "TemplateMessage") -> bool:
    """Check if message is a session header.

    Args:
        msg: The template message to check

    Returns:
        True if message content is a SessionHeaderMessage
    """
    return isinstance(msg.content, SessionHeaderMessage)


def get_message_emoji(msg: "TemplateMessage") -> str:
    """Return appropriate emoji for message type.

    Args:
        msg: The template message to get emoji for

    Returns:
        Emoji string for the message type, or empty string if no emoji
    """
    msg_type = msg.type

    if msg_type == "session_header":
        return "📋"
    elif msg_type == "user":
        # Command output has no emoji (neutral - can be from built-in or user command)
        if isinstance(msg.content, CommandOutputMessage):
            return ""
        return "🤷"
    elif msg_type == "bash-input":
        return "💻"
    elif msg_type == "assistant":
        if msg.is_sidechain:
            return "🔗"
        return "🤖"
    elif msg_type == "system":
        # Recap entries (away_summary) carry the same `system` message_type
        # as generic system info but are content, not noise. Give them a
        # distinct memo glyph so they read at-a-glance in scrolling lists,
        # and drop the matching label from the body chrome (one icon, one
        # title — see format_away_summary_content).
        if isinstance(msg.content, AwaySummaryMessage):
            return "📝"
        # Hook entries (both the typed attachment from #128 and the
        # legacy stop_hook_summary system entry) get a hook glyph at
        # the title level so the body summary doesn't have to repeat
        # "🪝 Hook output" as a redundant subheader.
        if isinstance(msg.content, (HookAttachmentMessage, HookSummaryMessage)):
            return "🪝"
        return "⚙️"
    elif msg_type == "tool_use":
        return "🛠️"
    elif msg_type == "tool_result":
        if isinstance(msg.content, ToolResultMessage) and msg.content.is_error:
            return "🚨"
        return "🧰"
    elif msg_type == "thinking":
        return "💭"
    elif msg_type == "image":
        return "🖼️"
    elif msg_type == "workflow_phase":
        return "🧩"
    elif msg_type == "workflow_agent":
        return "🤖"
    return ""


# -- HTML Utilities -----------------------------------------------------------


def escape_html(text: str) -> str:
    """Escape HTML special characters in text.

    Also normalizes line endings (CRLF -> LF) to prevent double spacing in <pre> blocks.
    """
    # Normalize CRLF to LF to prevent double line breaks in HTML
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(normalized)


def _create_pygments_plugin() -> Any:
    """Create a mistune plugin that uses Pygments for code block syntax highlighting."""
    from pygments import highlight
    from pygments.lexer import Lexer
    from pygments.lexers import get_lexer_by_name, TextLexer
    from pygments.formatter import Formatter
    from pygments.formatters import HtmlFormatter
    from pygments.util import ClassNotFound

    def plugin_pygments(md: Any) -> None:
        """Plugin to add Pygments syntax highlighting to code blocks."""
        original_render = md.renderer.block_code

        def block_code(code: str, info: Optional[str] = None) -> str:
            """Render code block with Pygments syntax highlighting if language is specified."""
            if info:
                # Language hint provided, use Pygments
                lang = info.split()[0] if info else ""
                # Default to plain text lexer
                lexer: Lexer = TextLexer()
                try:
                    lexer = get_lexer_by_name(lang, stripall=False)
                except ClassNotFound:
                    pass  # Already have default

                formatter: Formatter = HtmlFormatter(
                    linenos=False,  # No line numbers in markdown code blocks
                    cssclass="highlight",
                    wrapcode=True,
                )
                # Track Pygments timing if enabled
                with timing_stat("_pygments_timings"):
                    return str(highlight(code, lexer, formatter))
            else:
                # No language hint, use default rendering
                return original_render(code, info)

        md.renderer.block_code = block_code

    return plugin_pygments


@functools.lru_cache(maxsize=1)
def _get_markdown_renderer() -> mistune.Markdown:
    """Get cached Mistune markdown renderer with Pygments syntax highlighting."""
    from ..markdown_plugins import make_codespan_sha_plugin, make_sha_plugin
    from ..git_remote import resolve_sha_for_current_render

    return mistune.create_markdown(
        plugins=[
            "strikethrough",
            "footnotes",
            "table",
            "url",
            "task_lists",
            "def_list",
            _create_pygments_plugin(),
            # SHA → commit-URL linkifier (issue #156). Resolver reads
            # the per-render cwd from a ContextVar, so the cached
            # singleton renderer keeps working unchanged across
            # transcripts from different repos.
            make_sha_plugin(resolve_sha_for_current_render),
            # Codespan-wrapped variant: `5baac35` → <a><code>…</code></a>.
            # Registers with before="codespan" so it fires before
            # mistune's built-in rule consumes the backticks.
            make_codespan_sha_plugin(resolve_sha_for_current_render),
        ],
        escape=False,  # Don't escape HTML since we want to render markdown properly
        hard_wrap=True,  # Line break for newlines (checklists in Assistant messages)
    )


def render_markdown(text: str) -> str:
    """Convert markdown text to HTML using mistune with Pygments syntax highlighting."""
    # Track markdown rendering time if enabled
    with timing_stat("_markdown_timings"):
        renderer = _get_markdown_renderer()
        return str(renderer(text))


_INLINE_PARA_BOUNDARY_RE = re.compile(r"</p>\s*<p>")


def render_markdown_inline(text: str) -> str:
    """Render markdown collapsed to inline-safe HTML.

    Question/option text from AskUserQuestion is LLM-authored, so it carries
    real Markdown (inline code, emphasis, links). Callers embed the result
    inside inline wrappers (``<strong>``, ``<li>``), so this unwraps the
    enclosing ``<p>`` and folds any paragraph boundaries into ``<br>`` —
    a multi-paragraph free-form reply must not emit block ``<p>`` inside an
    inline element.

    Uses the ``escape=True`` renderer so raw HTML in the text (``<script>``,
    ``<option>``, …) is escaped to literal characters rather than injected —
    these short fields were previously HTML-escaped, so keep that contract.
    """
    with timing_stat("_markdown_timings"):
        renderer = _get_user_markdown_renderer()
        rendered = str(renderer(text)).strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        inner = rendered[len("<p>") : -len("</p>")]
        return _INLINE_PARA_BOUNDARY_RE.sub("<br>", inner)
    return rendered


@functools.lru_cache(maxsize=1)
def _get_user_markdown_renderer() -> mistune.Markdown:
    """Markdown renderer for user-authored text.

    Differs from the shared renderer in one critical way: ``escape=True``
    so a user typing raw ``<script>`` or other HTML sees the literal
    characters rendered as code, not injected into the DOM. Assistant
    content uses ``escape=False`` deliberately (tool output renders
    pre-formed HTML); user content must not bypass escaping.
    """
    from ..markdown_plugins import make_codespan_sha_plugin, make_sha_plugin
    from ..git_remote import resolve_sha_for_current_render

    return mistune.create_markdown(
        plugins=[
            "strikethrough",
            "footnotes",
            "table",
            "url",
            "task_lists",
            "def_list",
            _create_pygments_plugin(),
            # See _get_markdown_renderer for the SHA-link plugins'
            # contract; identical here — escape=True only affects
            # HTML in user text bodies, not link emission.
            make_sha_plugin(resolve_sha_for_current_render),
            make_codespan_sha_plugin(resolve_sha_for_current_render),
        ],
        escape=True,
        hard_wrap=True,
    )


def render_user_markdown(text: str) -> str:
    """Render user-authored Markdown with HTML escaping enabled."""
    with timing_stat("_markdown_timings"):
        renderer = _get_user_markdown_renderer()
        return str(renderer(text))


# HTML tags that are self-closing / void per the HTML spec — these do
# not appear on the open-tag stack in `is_well_formed_html`.
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


def is_well_formed_html(fragment: str) -> bool:
    """Check that an HTML fragment has balanced open/close tags.

    Uses stdlib `html.parser.HTMLParser` to walk the fragment, tracking
    an open-tag stack. Returns False if:

    - A close tag appears with no matching open tag on the stack.
    - Tags remain open at end of input.
    - The parser raises (malformed start tag, bad attribute quoting, etc.).

    Void elements (``<br>``, ``<img>``, …) don't push to the stack, and
    XHTML-style self-closing syntax (``<br />``, ``<hr />``) is treated
    as equivalent — mistune emits XHTML self-closing for voids so the
    check must accept both.
    """
    from html.parser import HTMLParser

    stack: list[str] = []
    errors: list[str] = []

    class _Checker(HTMLParser):
        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, Optional[str]]]
        ) -> None:
            if tag not in _VOID_HTML_TAGS:
                stack.append(tag)

        def handle_endtag(self, tag: str) -> None:
            if not stack:
                errors.append(f"unexpected </{tag}>")
                return
            if stack[-1] != tag:
                errors.append(
                    f"mismatched close: expected </{stack[-1]}>, got </{tag}>"
                )
                return
            stack.pop()

        def handle_startendtag(
            self, tag: str, attrs: list[tuple[str, Optional[str]]]
        ) -> None:
            # ``<tag />`` — XHTML self-closing. The element opens and
            # closes in one token, so neither push nor pop is needed.
            # Don't fall through to the default implementation, which
            # calls `handle_starttag` + `handle_endtag` and would make
            # a non-void `<br />` look unbalanced.
            return

        def error(
            self, message: str
        ) -> None:  # pragma: no cover — stdlib subclasses don't call this in 3.10+
            errors.append(message)

    parser = _Checker(convert_charrefs=True)
    try:
        parser.feed(fragment)
        parser.close()
    except Exception as exc:  # parser itself raised — treat as ill-formed
        errors.append(str(exc))

    return not errors and not stack


# -- Collapsible Content Rendering --------------------------------------------


def render_collapsible_code(
    preview_html: str,
    full_html: str,
    line_count: int,
    is_markdown: bool = False,
) -> str:
    """Render a collapsible code/content block with preview.

    Creates a details element with a line count badge and preview content
    that expands to show the full content.

    Args:
        preview_html: HTML content to show in the collapsed summary
        full_html: HTML content to show when expanded
        line_count: Number of lines (shown in the badge)
        is_markdown: If True, adds 'markdown' class to preview and full content divs

    Returns:
        HTML string with collapsible details element
    """
    markdown_class = " markdown" if is_markdown else ""
    return f"""<details class='collapsible-code'>
        <summary>
            <span class='line-count'>{line_count} lines</span>
            <div class='preview-content{markdown_class}'>{preview_html}</div>
        </summary>
        <div class='code-full{markdown_class}'>{full_html}</div>
    </details>"""


def _markdown_collapsible(
    raw_content: str,
    css_class: str,
    render_fn: "Callable[[str], str]",
    line_threshold: int,
    preview_line_count: int,
) -> str:
    """Shared body for the collapsible-markdown helpers, parameterized by the
    markdown render function (escape=False for assistant/tool output vs
    escape=True for untrusted content)."""
    rendered_html = render_fn(raw_content)

    lines = raw_content.splitlines()
    if len(lines) <= line_threshold:
        # Short content, show inline
        return f'<div class="{css_class} markdown">{rendered_html}</div>'

    # Long content - make collapsible with rendered preview
    preview_lines = lines[:preview_line_count]
    preview_text = "\n".join(preview_lines)
    if len(lines) > preview_line_count:
        preview_text += "\n\n..."
    # Render truncated markdown (produces valid HTML with proper tag closure)
    preview_html = render_fn(preview_text)

    collapsible = render_collapsible_code(
        preview_html, rendered_html, len(lines), is_markdown=True
    )
    return f'<div class="{css_class}">{collapsible}</div>'


def render_markdown_collapsible(
    raw_content: str,
    css_class: str,
    line_threshold: int = 20,
    preview_line_count: int = 5,
) -> str:
    """Render markdown content, making it collapsible if it exceeds a line threshold.

    For long content, creates a collapsible details element with a preview.
    For short content, renders inline with the specified CSS class.

    Uses the ``escape=False`` renderer — for assistant/tool-authored content
    (Task results, WebSearch/WebFetch, plans) that may emit pre-formed HTML.
    For untrusted content (e.g. memory files), use
    ``render_user_markdown_collapsible`` instead.

    Args:
        raw_content: The raw text content to render as markdown
        css_class: CSS class for the wrapper div (e.g., "task-prompt", "task-result")
        line_threshold: Number of lines above which content becomes collapsible (default 20)
        preview_line_count: Number of lines to show in the preview (default 5)

    Returns:
        HTML string with rendered markdown, optionally wrapped in collapsible details
    """
    return _markdown_collapsible(
        raw_content, css_class, render_markdown, line_threshold, preview_line_count
    )


def render_user_markdown_collapsible(
    raw_content: str,
    css_class: str,
    line_threshold: int = 20,
    preview_line_count: int = 5,
) -> str:
    """Like ``render_markdown_collapsible`` but with HTML escaping enabled.

    For content that is not trusted to emit pre-formed HTML — e.g. auto-memory
    files (#192), whose markdown may contain literal ``<script>``/HTML that must
    render as escaped text, not live DOM, when the transcript is opened.
    """
    return _markdown_collapsible(
        raw_content,
        css_class,
        render_user_markdown,
        line_threshold,
        preview_line_count,
    )


def render_file_content_collapsible(
    code_content: str,
    file_path: str,
    css_class: str,
    linenostart: int = 1,
    line_threshold: int = 12,
    preview_line_count: int = 5,
    suffix_html: str = "",
) -> str:
    """Render file content with syntax highlighting, collapsible if long.

    Highlights code using Pygments and wraps in a collapsible details element
    if the content exceeds the line threshold. Uses preview truncation from
    already-highlighted HTML to avoid double Pygments calls.

    Args:
        code_content: The raw code content to highlight
        file_path: File path for syntax detection (extension-based)
        css_class: CSS class for the wrapper div (e.g., 'write-tool-content')
        linenostart: Starting line number for Pygments (default 1)
        line_threshold: Number of lines above which content becomes collapsible
        preview_line_count: Number of lines to show in the preview
        suffix_html: Optional HTML to append after the code (inside wrapper div)

    Returns:
        HTML string with highlighted code, collapsible if >line_threshold lines
    """
    # Highlight code with Pygments (single call)
    highlighted_html = highlight_code_with_pygments(
        code_content, file_path, linenostart=linenostart
    )

    html_parts = [f"<div class='{css_class}'>"]

    lines = code_content.splitlines()
    if len(lines) > line_threshold:
        # Extract preview from already-highlighted HTML (avoids double highlighting)
        preview_html = truncate_highlighted_preview(
            highlighted_html, preview_line_count
        )
        html_parts.append(
            render_collapsible_code(preview_html, highlighted_html, len(lines))
        )
    else:
        # Show directly without collapsible
        html_parts.append(highlighted_html)

    if suffix_html:
        html_parts.append(suffix_html)

    html_parts.append("</div>")
    return "".join(html_parts)


def render_async_result_body(text: str, css_class: str) -> str:
    """Render an async-result body, JSON-aware (issue #174 D4).

    A JSON-shaped payload — ``lstrip()`` starts with ``{"`` per cboos's
    heuristic, and so possibly truncated — is pretty-printed when it parses
    and Pygments-highlighted as JSON (collapsible when long); everything
    else (plain markdown answers from non-workflow async agents) falls back
    to the existing markdown rendering, so this is a safe no-op for them.

    (Spilling to the full untruncated ``tool-results/<taskId>.txt`` is a
    separate enhancement — it needs the taskId plumbed to this layer — and
    is deferred to a follow-up.)
    """
    stripped = (text or "").lstrip()
    if stripped.startswith('{"'):
        rendered = text
        try:
            rendered = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pass  # truncated / variant payload — highlight the raw text as-is
        return render_file_content_collapsible(
            rendered,
            "result.json",
            css_class,
            line_threshold=10,
            preview_line_count=6,
        )
    return render_markdown_collapsible(text, css_class)


# -- Template Environment -----------------------------------------------------


def starts_with_emoji(text: str) -> bool:
    """Check if a string starts with an emoji character.

    Checks common emoji Unicode ranges:
    - Misc Technical: U+2300 - U+23FF (⏰ ⏳ ⏱️ ⏲️ ⏸ ⏹ ⏺ ⏏ ↩ etc.)
    - Misc Symbols: U+2600 - U+26FF
    - Dingbats: U+2700 - U+27BF
    - Misc Symbols and Pictographs: U+1F300 - U+1F5FF
    - Emoticons: U+1F600 - U+1F64F
    - Transport and Map Symbols: U+1F680 - U+1F6FF
    - Supplemental Symbols: U+1F900 - U+1F9FF

    Used by the transcript template to suppress the default ``🛠️``
    emoji prefix when a tool title already starts with its own icon.
    Misses here cause a redundant wrench to appear in front of an
    otherwise-iconified title (e.g. ``🛠️ ⏰ ScheduleWakeup ...``);
    Misc Technical (U+2300-U+23FF) is included because the alarm
    clock and other time/control glyphs live there but are real
    emoji in practice.
    """
    if not text:
        return False

    first_char = text[0]
    code_point = ord(first_char)

    return (
        0x2300 <= code_point <= 0x23FF  # Misc Technical (⏰ ⏳ ⏱️ ...)
        or 0x2600 <= code_point <= 0x26FF  # Misc Symbols
        or 0x2700 <= code_point <= 0x27BF  # Dingbats
        or 0x1F300 <= code_point <= 0x1F5FF  # Misc Symbols and Pictographs
        or 0x1F600 <= code_point <= 0x1F64F  # Emoticons
        or 0x1F680 <= code_point <= 0x1F6FF  # Transport and Map Symbols
        or 0x1F900 <= code_point <= 0x1F9FF  # Supplemental Symbols
    )


@functools.lru_cache(maxsize=1)
def get_template_environment() -> Environment:
    """Get cached Jinja2 template environment for HTML rendering.

    Creates a Jinja2 environment configured with:
    - Template loading from the templates directory
    - HTML auto-escaping
    - Custom template filters/functions (starts_with_emoji)

    Returns:
        Configured Jinja2 Environment (cached after first call)
    """
    templates_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(templates_dir),
        autoescape=select_autoescape(["html", "xml"]),
    )
    # Add custom filters/functions
    # Cast to Any to bypass Jinja2's overly strict globals type
    globals_dict: Any = env.globals
    globals_dict["starts_with_emoji"] = starts_with_emoji
    return env

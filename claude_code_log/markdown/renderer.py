"""Markdown renderer implementation for Claude Code transcripts."""

from __future__ import annotations

import functools
import html as _html
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import mistune
from mistune.renderers.markdown import MarkdownRenderer as _MistuneMarkdownRenderer

from ..cache import get_library_version
from ..html.utils import (
    is_memory_path,
    is_well_formed_html,
    memory_short_path,
    render_user_markdown,
)
from ..utils import format_timestamp, generate_unified_diff, strip_error_tags
from ..models import (
    AssistantTextMessage,
    AwaySummaryMessage,
    BashInputMessage,
    BashOutputMessage,
    CommandOutputMessage,
    CompactedSummaryMessage,
    DetailLevel,
    HookAttachmentMessage,
    HookSummaryMessage,
    ImageContent,
    SessionHeaderMessage,
    SlashCommandMessage,
    SystemMessage,
    TaskNotificationMessage,
    TeammateMessage,
    TextContent,
    ThinkingMessage,
    ToolResultMessage,
    ToolUseMessage,
    TranscriptEntry,
    UnknownMessage,
    UserMemoryMessage,
    UserSlashCommandMessage,
    UserTextMessage,
    # Tool input types
    AskUserQuestionInput,
    BashInput,
    EditInput,
    ExitPlanModeInput,
    GlobInput,
    GrepInput,
    MultiEditInput,
    ReadInput,
    SendMessageInput,
    TaskCreateInput,
    TaskInput,
    TaskStopInput,
    TaskStopOutput,
    TaskListInput,
    TaskOutputInput,
    TaskUpdateInput,
    TeamCreateInput,
    TeamDeleteInput,
    TodoWriteInput,
    ToolUseContent,
    SkillInput,
    WebSearchInput,
    WebFetchInput,
    WorkflowAgentMessage,
    WorkflowPhaseMessage,
    WorkflowToolInput,
    MonitorInput,
    MonitorOutput,
    ScheduleWakeupInput,
    ScheduleWakeupOutput,
    CronCreateInput,
    CronCreateOutput,
    CronListInput,
    CronListOutput,
    CronDeleteInput,
    CronDeleteOutput,
    WriteInput,
    # Tool output types
    AskUserQuestionOutput,
    BashOutput,
    EditOutput,
    ExitPlanModeOutput,
    GlobOutput,
    ReadOutput,
    SendMessageOutput,
    TaskCreateOutput,
    TaskListOutput,
    TaskOutput,
    TaskOutputResult,
    TaskUpdateOutput,
    TeamCreateOutput,
    TeamDeleteOutput,
    ToolResultContent,
    WebSearchOutput,
    WebFetchOutput,
    WriteOutput,
)
from ..renderer import (
    Renderer,
    RenderingContext,
    TemplateMessage,
    branch_short_uuid,
    generate_template_messages,
    prepare_projects_index,
    title_for_projects_index,
)

if TYPE_CHECKING:
    from ..cache import CacheManager
    from ..dag import SessionTree


# Colored-circle emoji convention for teammate colors in Markdown output.
# Markdown can't carry CSS-style color, so prefix each teammate mention
# with the closest circle emoji to preserve the "at-a-glance" color cue.
_COLOR_CIRCLE: dict[str, str] = {
    "blue": "🔵",
    "cyan": "🟦",
    "green": "🟢",
    "yellow": "🟡",
    "orange": "🟠",
    "red": "🔴",
    "pink": "🌸",
    "purple": "🟣",
    "gray": "⚪",
    "system": "⬛",
    "default": "⚪",
}


# Matches the display form ``format_timestamp`` returns on success
# (``YYYY-MM-DD HH:MM:SS``). The function falls back to its raw input
# on parse failure, so we validate the rendered shape rather than
# presence-of-space — see ``_format_message_timestamp``.
_TIMESTAMP_DISPLAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _inline_code(value: str) -> str:
    """Wrap *value* in a CommonMark inline code span that survives backticks.

    CommonMark doesn't honor backslash escapes inside code spans, so a
    naive `` `foo`bar` `` would close the span at the inner tick. The
    idiomatic recipe is to widen the fence past the longest run of
    backticks in the value, and pad with a space when the value
    starts/ends with a backtick (otherwise the leading/trailing tick
    fuses with the fence).
    """
    if not value:
        return "``"  # empty code span
    longest = 0
    run = 0
    for ch in value:
        if ch == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    fence = "`" * (longest + 1)
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def _teammate_marker(name: str, color: Optional[str]) -> str:
    """Return a `🟢 name` marker for a teammate in Markdown output."""
    circle = _COLOR_CIRCLE.get((color or "").lower(), _COLOR_CIRCLE["default"])
    return f"{circle} {_inline_code(name)}"


def _table_cell(value: Any) -> str:
    """Escape a value for inclusion in a Markdown table cell.

    `|` breaks the row into cells; `\\n` breaks the row entirely.
    GitHub-flavored Markdown allows `<br>` inside table cells, so we
    preserve line intent with `<br>` rather than stripping the newline.
    None is rendered as an empty cell.
    """
    return str(value or "").replace("\n", "<br>").replace("|", r"\|")


def _session_anchor(session_or_id: "SessionHeaderMessage | str") -> str:
    """Compose a unique Markdown anchor key for a session header.

    Trunk session ids look like ``d602eb5f-...`` and we render them as
    ``session-d602eb5f``. Branch session ids look like
    ``d602eb5f-...@0e09007a-cd8`` (or deeper nestings with multiple
    ``@`` segments). They all *start* with the trunk uuid, so the
    naive ``session_id[:8]`` collides across every branch under the
    same trunk and TOC links can only land on the first matching
    heading.

    Branches use ``branch-<uuid8>`` where ``<uuid8>`` is the first 8
    chars of the deepest ``@`` segment (the branch root's UUID
    prefix). Mirrors the visible ``Branch • <uuid8>`` label so a
    reader can correlate the TOC entry with the inline header.

    Accepts either a ``SessionHeaderMessage`` or a raw session id
    string.
    """
    sid = session_or_id if isinstance(session_or_id, str) else session_or_id.session_id
    if "@" in sid:
        return f"branch-{branch_short_uuid(sid)}"
    return f"session-{sid[:8]}"


class _TagProtectingMarkdownRenderer(_MistuneMarkdownRenderer):
    """Mistune re-emitter that neutralises raw HTML tokens.

    Mistune's stock ``MarkdownRenderer`` round-trips a parsed Markdown
    document back to Markdown text. We inherit it and override the two
    HTML hooks — ``inline_html`` (tags like ``<br>``, ``<script>``) and
    ``block_html`` (block-level HTML chunks) — to emit the token's raw
    content HTML-escaped to entities instead of passing the tag through
    verbatim.

    Escaping to entities sidesteps the class of edge cases that any
    backtick-wrapping strategy has to contend with (stray backticks in
    the surrounding text merging with the wrapper delimiter, attribute
    values carrying backticks, block HTML containing a fence inside,
    …). The tradeoff is that in very strict downstream renderers the
    entities can end up visible as literal text; permissive renderers
    like GitHub correctly display the tag text. Either way the tag
    itself never reaches the HTML output as live markup.
    """

    def inline_html(self, token: dict[str, Any], state: Any) -> str:
        return _html.escape(token.get("raw", ""))

    def block_html(self, token: dict[str, Any], state: Any) -> str:
        return _html.escape(token.get("raw", ""))


@functools.lru_cache(maxsize=1)
def _get_tag_protecting_markdown() -> mistune.Markdown:
    """Cache the mistune pipeline used by :func:`_protect_html_tags`."""
    return mistune.create_markdown(renderer=_TagProtectingMarkdownRenderer())


def _protect_html_tags(text: str) -> str:
    """Wrap raw HTML/XML tags in inline code backticks.

    Mirrors the HTML renderer's ``escape=True`` policy for user content:
    tags that a user typed (``<script>``, ``<details>``, bare ``<br>``,
    …) are rendered as literal text in any downstream Markdown viewer
    rather than interpreted as HTML. Without this, a permissive viewer
    could execute ``<script>`` or interpret ``<iframe>`` — the HTML
    output escapes them via mistune's ``escape=True``; the Markdown
    output delegates to the viewer, so we need to neutralise tags at
    emission time.

    Parses the text with mistune and re-emits it through a
    tag-protecting renderer. Because the parser already distinguishes
    raw HTML from inline code spans (``` `x <br> y` ```), fenced blocks,
    and indented code blocks, all of those are preserved unchanged and
    only the actual HTML tokens are wrapped. The round-trip may apply
    minor cosmetic normalisation (e.g. indented HTML becomes a fenced
    block), which is acceptable since the goal is tag-neutralisation,
    not byte-for-byte source preservation.
    """
    if not text:
        return text
    # `mistune.Markdown.__call__` is typed as returning a union of `str`
    # and a token list; with a renderer set it always returns a string.
    rendered = _get_tag_protecting_markdown()(text)
    return str(rendered).rstrip("\n")


def _render_expand_paths_tree(template_projects: list[Any]) -> list[str]:
    """Render `--expand-paths` Markdown index as a nested bullet-list
    directory tree.

    Each unique path-component along the way becomes a bullet at its
    nesting depth; sessions are leaf bullets under their parent dir.
    The path is derived from `project.html_file` (the combined-link
    relative path) — when ``combined_suppressed`` is set the per-session
    `file` entries replace the project-level combined link.

    Returns a list of Markdown lines (joined by `\\n` by the caller),
    or an empty list when no tree-shapable data is present (signal to
    fall back to the flat layout).
    """
    # Tree node:
    #   {"_links": list[(label, url, timestamp_range)],  # at this level
    #    "<child-name>": <subtree-node>}
    root: dict[str, Any] = {}

    def _insert(path_parts: list[str], label: str, url: str, ts: str) -> None:
        """Walk `path_parts` down the tree; record the link at the leaf
        directory. The leaf is the second-to-last path component (the
        last is the filename itself)."""
        node = root
        # The last part of path_parts is the filename; walk up to its
        # parent directory.
        for part in path_parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node.setdefault("_links", []).append((label, url, ts))

    def _to_posix(s: str) -> str:
        """Defensive normalisation: split on `/` only after folding
        any backslashes the upstream may have produced (Windows
        `Path` stringification, historical regressions, callers that
        construct URLs from native paths)."""
        return s.replace("\\", "/")

    for project in template_projects:
        if project.combined_suppressed and project.sessions:
            # Use the per-session links — each session becomes a leaf
            # under its parent dir.
            for session in project.sessions:
                url = session.get("file")
                if not url:
                    continue
                url = _to_posix(url)
                summary = session.get("summary")
                short_id = (session.get("id") or "")[:8]
                label = summary if summary else f"session {short_id}"
                ts = session.get("timestamp_range") or ""
                _insert(url.split("/"), label, url, ts)
        else:
            # Combined-link mode: use the project's html_file as the
            # single leaf under its parent dir. Translate `.html`
            # filenames so the Markdown index links land at `.md` peers.
            url = _to_posix(project.html_file).replace(".html", ".md")
            ts = project.formatted_time_range or ""
            _insert(url.split("/"), project.display_name, url, ts)

    if not root:
        return []

    lines: list[str] = []

    def _emit(node: dict[str, Any], depth: int) -> None:
        indent = "  " * depth
        # Directories first (alphabetical), then leaf-link entries.
        for name in sorted(k for k in node if k != "_links"):
            lines.append(f"{indent}- **{name}/**")
            _emit(node[name], depth + 1)
        for label, url, ts in node.get("_links", []):
            ts_suffix = f" — *{ts}*" if ts else ""
            lines.append(f"{indent}- [{label}]({url}){ts_suffix}")

    _emit(root, 0)
    return lines


class MarkdownRenderer(Renderer):
    """Markdown renderer for Claude Code transcripts."""

    def __init__(
        self,
        image_export_mode: str = "referenced",
        *,
        no_timestamps: bool = False,
    ):
        """Initialize the Markdown renderer.

        Args:
            image_export_mode: Image export mode - "placeholder", "embedded", or "referenced"
            no_timestamps: When True, suppress the per-message timestamp
                line emitted after each heading (issue #160).
        """
        super().__init__()
        self.image_export_mode = image_export_mode
        self._output_dir: Path | None = None
        self._image_counter = 0
        self._ctx: RenderingContext | None = None
        # session_id -> {teammate_id -> color}, snapshotted at render
        # start. Scoped to avoid cross-session contamination in
        # combined transcripts (see RenderingContext.teammate_colors).
        self._teammate_colors_by_session: dict[str, dict[str, str]] = {}
        # Per-message timestamp emission (issue #160): emit a line
        # immediately after each heading using a date-on-change rule.
        # Last-date-seen state lives here so the rule observes the
        # actual rendered order, including any `compact`-mode heading
        # suppression.
        self.no_timestamps: bool = no_timestamps
        self._last_date_seen: Optional[str] = None

    # -------------------------------------------------------------------------
    # Private Utility Methods
    # -------------------------------------------------------------------------

    def _colors_for(self, message: TemplateMessage) -> dict[str, str]:
        """Return the teammate_id→color map scoped to *message*'s session."""
        sid = message.meta.session_id if message.meta else ""
        return self._teammate_colors_by_session.get(sid, {})

    def _quote(self, text: str) -> str:
        """Prefix each line with '> ' to create a blockquote.

        Also escapes <summary> tags that would interfere with <details> rendering.
        """
        # Escape <summary> and </summary> tags on their own lines
        text = re.sub(r"^(</?summary>)$", r"\\\1", text, flags=re.MULTILINE)
        return "\n".join(f"> {line}" for line in text.split("\n"))

    def _linkify_shas(self, text: str) -> str:
        """Substitute resolvable git SHAs with Markdown links (issue #156).

        The Markdown output emits text bodies directly (no mistune
        round-trip), so it can't lean on the inline-parser plugin used
        by the HTML side. Mirrors the plugin's behaviour by scanning
        raw text and emitting ``[sha](url)`` only when the resolver
        returns a URL — local-only commits stay as plain text.
        """
        from ..git_remote import resolve_sha_for_current_render
        from ..markdown_plugins import linkify_shas_in_text

        return linkify_shas_in_text(text, resolve_sha_for_current_render)

    def _code_fence(self, text: str, lang: str = "") -> str:
        """Wrap text in a fenced code block with adaptive delimiter.

        If the text contains backticks, uses a longer delimiter to avoid conflicts.
        """
        # Find longest sequence of backticks in text
        max_ticks = 2
        for match in re.finditer(r"`+", text):
            max_ticks = max(max_ticks, len(match.group()))
        fence = "`" * max(3, max_ticks + 1)
        return f"{fence}{lang}\n{text}\n{fence}"

    def _escape_html_tag(self, text: str, tag: str) -> str:
        """Escape HTML closing tags to prevent breaking markdown structure.

        Replaces </tag> with &lt;/tag> to prevent premature closing.
        """
        return text.replace(f"</{tag}>", f"&lt;/{tag}>")

    def _escape_stars(self, text: str) -> str:
        """Escape asterisks for safe use inside emphasis markers.

        - * becomes \\*
        - \\* becomes \\\\\\* (preserves the escaped asterisk)
        """
        # First double all backslashes
        text = text.replace("\\", "\\\\")
        # Then escape all asterisks
        text = text.replace("*", "\\*")
        return text

    def _collapsible(self, summary: str, content: str) -> str:
        """Wrap content in a collapsible <details> block."""
        # Escape closing tags that would break the structure
        safe_summary = self._escape_html_tag(summary, "summary")
        safe_summary = self._escape_html_tag(safe_summary, "details")
        safe_content = self._escape_html_tag(content, "details")
        return f"<details>\n<summary>{safe_summary}</summary>\n\n{safe_content}\n</details>"

    def _format_image(self, image: ImageContent) -> str:
        """Format image based on export mode."""
        from ..image_export import export_image

        self._image_counter += 1
        src = export_image(
            image,
            self.image_export_mode,
            output_dir=self._output_dir,
            counter=self._image_counter,
        )
        if src is None:
            return "[Image]"
        return f"![image]({src})"

    def _lang_from_path(self, path: str) -> str:
        """Get language hint from file extension."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".jsx": "javascript",
            ".json": "json",
            ".html": "html",
            ".css": "css",
            ".scss": "scss",
            ".sh": "bash",
            ".bash": "bash",
            ".zsh": "bash",
            ".md": "markdown",
            ".yml": "yaml",
            ".yaml": "yaml",
            ".toml": "toml",
            ".rs": "rust",
            ".go": "go",
            ".java": "java",
            ".c": "c",
            ".cpp": "cpp",
            ".h": "c",
            ".hpp": "cpp",
            ".rb": "ruby",
            ".php": "php",
            ".sql": "sql",
            ".xml": "xml",
            ".swift": "swift",
            ".kt": "kotlin",
            ".scala": "scala",
            ".r": "r",
            ".R": "r",
            ".lua": "lua",
            ".pl": "perl",
            ".ex": "elixir",
            ".exs": "elixir",
            ".erl": "erlang",
            ".hs": "haskell",
            ".ml": "ocaml",
            ".fs": "fsharp",
            ".clj": "clojure",
            ".vim": "vim",
            ".dockerfile": "dockerfile",
            ".Dockerfile": "dockerfile",
        }
        ext = Path(path).suffix.lower() if path else ""
        return ext_map.get(ext, "")

    def _excerpt(self, text: str, max_len: int = 40, min_len: int = 12) -> str:
        """Extract first line excerpt, truncating at word/sentence boundary.

        - Stops at sentence endings ("? ", "! ", ". " but not lone ".")
          only if at least min_len characters
        - If over max_len, continues to end of current word
        - Adds "…" if truncated
        """
        # Get first non-empty line
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Check for early sentence endings (but enforce minimum length)
            for ending in ("? ", "! ", ". "):
                pos = line.find(ending)
                if min_len <= pos < max_len:
                    return line[: pos + 1]  # Include the punctuation

            # If line fits, return as-is
            if len(line) <= max_len:
                return line

            # Find word boundary after max_len
            # Start from max_len and continue until non-word char
            end = max_len
            while end < len(line) and re.match(r"\w", line[end]):
                end += 1

            return line[:end] + "…"

        return ""

    def _get_message_text(self, msg: TemplateMessage) -> str:
        """Extract text content from a message for excerpt generation."""
        content = msg.content
        if isinstance(content, ThinkingMessage):
            return content.thinking
        if isinstance(content, (AssistantTextMessage, UserTextMessage)):
            # Get first text item
            for item in content.items:
                if isinstance(item, TextContent) and item.text.strip():
                    return item.text
        return ""

    # -------------------------------------------------------------------------
    # System Content Formatters
    # -------------------------------------------------------------------------

    def format_SystemMessage(self, content: SystemMessage, _: TemplateMessage) -> str:
        """Format → ``ℹ️ message text`` for one-liners; for multi-line
        content (e.g. ``/context`` ASCII grid), wrap the body in an
        adaptive fenced code block so CommonMark preserves whitespace
        and grid alignment.

        Mirrors the HTML side's ``<pre class='system-content'>`` shape.
        Without the fence, CommonMark collapses internal newlines into
        spaces unless every line ends with two trailing spaces — wrong
        for grid content. ``_code_fence`` widens the fence past any
        backtick run in the body (defensive — unlikely in `/context`
        grids but free given the helper).
        """
        level_prefix = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(
            content.level, ""
        )
        if "\n" in content.text:
            return f"{level_prefix}\n{self._code_fence(content.text)}"
        return f"{level_prefix} {content.text}"

    def format_AwaySummaryMessage(
        self, content: AwaySummaryMessage, _: TemplateMessage
    ) -> str:
        """Format → '📝 Recap: <text>'."""
        return f"📝 Recap: {content.text}"

    def format_HookSummaryMessage(
        self, content: HookSummaryMessage, _: TemplateMessage
    ) -> str:
        """Format → command list + errors (no redundant "Hook produced output").

        Mirrors the HTML side: the message title already carries the
        🪝 + "System Hook" header, so the body drops the generic
        subhead and only emits actual content (commands, errors).
        Returns empty when there's nothing useful to show.
        """
        parts: list[str] = []
        if content.hook_infos:
            for info in content.hook_infos:
                # ``_inline_code`` widens its fence past any backtick run
                # in the value, so a hook command like ``echo `pwd``` stays
                # legible instead of breaking the surrounding span.
                parts.append(_inline_code(info.command))
        if content.hook_errors:
            for error in content.hook_errors:
                parts.append(f"❌ Error: {error}")
        return "\n\n".join(parts) if parts else ""

    def format_HookAttachmentMessage(
        self, content: HookAttachmentMessage, _: TemplateMessage
    ) -> str:
        """Format → multi-line summary of a single hook callback.

        Mirrors the HTML rendering structure (header line + per-stream
        fenced blocks) but in plain Markdown so it survives in
        downstream tools that consume the ``--format md`` output.
        """
        header_pieces: list[str] = []
        if content.hook_name:
            header_pieces.append(_inline_code(content.hook_name))
        elif content.hook_event:
            header_pieces.append(_inline_code(content.hook_event))
        if content.exit_code is not None:
            header_pieces.append(f"exit {_inline_code(str(content.exit_code))}")
        if content.duration_ms is not None:
            header_pieces.append(f"{_inline_code(str(content.duration_ms))} ms")
        prefix = (
            "🚨" if content.kind in ("blocking_error", "non_blocking_error") else "🪝"
        )
        header = (
            f"{prefix} Hook · " + " · ".join(header_pieces)
            if header_pieces
            else (f"{prefix} Hook")
        )

        body_parts: list[str] = [header]
        if content.command:
            body_parts.append(self._code_fence(content.command, lang="bash"))
        if content.blocking_error:
            body_parts.append(self._code_fence(content.blocking_error))
        if content.content:
            body_parts.append(self._code_fence(content.content))
        if content.stdout:
            body_parts.append("**stdout:**")
            body_parts.append(self._code_fence(content.stdout))
        if content.stderr:
            body_parts.append("**stderr:**")
            body_parts.append(self._code_fence(content.stderr))
        return "\n\n".join(body_parts)

    def format_SessionHeaderMessage(
        self, content: SessionHeaderMessage, _: TemplateMessage
    ) -> str:
        """Format → '<a id="session-abc12345"></a>' (or '<a id="branch-…"></a>')."""
        # Return just the anchor - it will be placed before the heading.
        # Branches need a per-branch-unique key because every branch's
        # session_id starts with the trunk's uuid, so ``session_id[:8]``
        # collides across branches and the TOC could only land on the
        # first matching heading.
        return f'<a id="{_session_anchor(content)}"></a>'

    def title_SessionHeaderMessage(
        self, content: SessionHeaderMessage, _: TemplateMessage
    ) -> str:
        """Title → '📋 Session `abc12345`: summary — Team: `t`' (or '🌿 Branch …').

        Branch session headers surface the ``Branch • <uuid8> • <preview>``
        shape that the renderer's ``_branch_label`` helper composes for
        HTML output (stored on ``content.title``). Without this, a branch
        would render as ``📋 Session `<trunk-uuid>`: <summary>``,
        indistinguishable from the trunk session it forked from — the
        synthetic branch session_id starts with the trunk's uuid and the
        ``[:8]`` slice can't see past it.
        """
        if content.is_branch:
            # ``_render_messages`` always composes ``content.title`` via
            # ``_branch_label``, so the empty-title path is purely
            # defensive — but if it ever fires we still need a
            # branch-flavoured heading. Falling through to the trunk
            # path below would surface ``📋 Session `<trunk-uuid>``` for
            # a branch (the ``[:8]`` slice can't see past the shared
            # trunk prefix), which is exactly the duplicate-anchor /
            # confusable-heading shape we just fixed.
            if content.title:
                title = f"🌿 {content.title}"
            else:
                title = f"🌿 Branch • {branch_short_uuid(content.session_id)}"
            if content.team_name:
                title = f"{title} — Team: {_inline_code(content.team_name)}"
            return title
        session_short = content.session_id[:8]
        if content.summary:
            title = f"📋 Session `{session_short}`: {content.summary}"
        else:
            title = f"📋 Session `{session_short}`"
        if content.team_name:
            # Boundary hygiene: a malformed transcript could in theory
            # carry a backtick in teamName. CommonMark code spans don't
            # honor backslash escapes inside them — the idiomatic guard
            # is to widen the fence beyond the longest run of backticks
            # in the value (and pad with a space when it starts/ends
            # with a backtick, otherwise the wider fence matches the
            # leading/trailing tick).
            title = f"{title} — Team: {_inline_code(content.team_name)}"
        return title

    # -------------------------------------------------------------------------
    # User Content Formatters
    # -------------------------------------------------------------------------

    def format_UserTextMessage(
        self, content: UserTextMessage, _: TemplateMessage
    ) -> str:
        """Format → user text as Markdown when clean, else fenced code.

        Mirrors the HTML renderer's dual-view gate: try rendering the
        text as Markdown; if mistune produces well-formed HTML the
        source was recognisable Markdown (or plain text that happens not
        to conflict with Markdown syntax), so emit the raw text inline
        (headings/bold/lists render naturally downstream). Otherwise
        wrap in a code fence so the literal content is preserved.

        When emitting inline, raw HTML-like tags are wrapped in backticks
        (see :func:`_protect_html_tags`) to keep parity with the HTML
        path's ``escape=True`` safety posture.
        """
        parts: list[str] = []
        for item in content.items:
            if isinstance(item, ImageContent):
                parts.append(self._format_image(item))
            elif isinstance(item, TextContent):
                if item.text.strip():
                    rendered = render_user_markdown(item.text)
                    if is_well_formed_html(rendered):
                        # Inline-clean text: SHA-linkify the prose. Code-
                        # fenced fallback below stays untouched (it's
                        # literally a code block — SHAs in there should
                        # not become links, mirroring the HTML side
                        # where the mistune plugin doesn't fire inside
                        # codespans / fenced code).
                        parts.append(_protect_html_tags(self._linkify_shas(item.text)))
                    else:
                        parts.append(self._code_fence(item.text))
        return "\n\n".join(parts)

    def title_UserTextMessage(
        self, _content: UserTextMessage, _message: TemplateMessage
    ) -> str:
        """Title → '🤷 User: *excerpt...*'."""
        if excerpt := self._excerpt(self._get_message_text(_message)):
            return f"🤷 User: *{self._escape_stars(excerpt)}*"
        return "🤷 User"

    def format_UserSlashCommandMessage(
        self, content: UserSlashCommandMessage, _: TemplateMessage
    ) -> str:
        """Format → blockquoted text."""
        # UserSlashCommandMessage has a text attribute (markdown), quote to protect it
        return self._quote(content.text) if content.text.strip() else ""

    def format_SlashCommandMessage(
        self, content: SlashCommandMessage, _: TemplateMessage
    ) -> str:
        """Format → '**Args:** `args`' + fenced contents."""
        parts: list[str] = []
        # Command name is in the title, only include args and contents here
        if content.command_args:
            # Use the adaptive inline-code helper: ``command_args`` is
            # free-form user input that legitimately contains backticks
            # (``run `date` && ...``). A naïve single-tick span would
            # close at the first inner tick and emit a mix of plain text
            # and unmatched ticks downstream.
            parts.append(f"**Args:** {_inline_code(content.command_args)}")
        if content.command_contents:
            parts.append(self._code_fence(content.command_contents))
        return "\n\n".join(parts)

    def title_SlashCommandMessage(
        self, content: SlashCommandMessage, _message: TemplateMessage
    ) -> str:
        """Title → '🤷 Command `/cmd`'."""
        # command_name already includes the leading slash; harness emits
        # short identifiers (no backticks) but use the adaptive helper
        # for symmetry with the args site and to stay safe if the
        # emission shape ever drifts.
        return f"🤷 Command {_inline_code(content.command_name)}"

    def title_UserSlashCommandMessage(
        self, _content: UserSlashCommandMessage, _: TemplateMessage
    ) -> str:
        # When paired with a SlashCommand, borrow its `🤷 Command /cmd` title.
        # Markdown's `_render_message` skips middle and last members entirely,
        # so for the (UserSlash → Slash) and (UserSlash → Slash → Output)
        # orderings produced by `/exit`-like flows the SlashCommand's own title
        # would otherwise be lost. Check pair_middle first (triples) then
        # pair_last (2-msg pair). Mirrors `title_ThinkingMessage`'s precedent.
        if _.is_first_in_pair and self._ctx is not None:
            for partner_idx in (_.pair_middle, _.pair_last):
                if partner_idx is None:
                    continue
                if (partner := self._ctx.get(partner_idx)) and isinstance(
                    partner.content, SlashCommandMessage
                ):
                    return f"🤷 Command {_inline_code(partner.content.command_name)}"
        # Fallback to base behaviour ("User (slash command)").
        return super().title_UserSlashCommandMessage(_content, _)

    def format_CommandOutputMessage(
        self, content: CommandOutputMessage, _: TemplateMessage
    ) -> str:
        """Format → blockquote (markdown) or fenced code block."""
        if content.is_markdown:
            # Quote markdown output to protect it
            return self._quote(content.stdout)
        return self._code_fence(content.stdout)

    def format_BashInputMessage(
        self, content: BashInputMessage, _: TemplateMessage
    ) -> str:
        """Format → '```bash\\n$ command\\n```'."""
        return self._code_fence(f"$ {content.command}", "bash")

    def format_BashOutputMessage(
        self, content: BashOutputMessage, _: TemplateMessage
    ) -> str:
        """Format → fenced code block (ANSI stripped)."""
        # Combine stdout and stderr, strip ANSI codes for markdown output
        parts: list[str] = []
        if content.stdout:
            parts.append(content.stdout)
        if content.stderr:
            parts.append(content.stderr)
        output = "\n".join(parts)
        output = re.sub(r"\x1b\[[0-9;]*m", "", output)
        return self._code_fence(output)

    def format_CompactedSummaryMessage(
        self, content: CompactedSummaryMessage, _: TemplateMessage
    ) -> str:
        """Format → blockquoted summary."""
        # Quote to protect embedded markdown
        return self._quote(content.summary_text)

    def format_UserMemoryMessage(
        self, content: UserMemoryMessage, _: TemplateMessage
    ) -> str:
        """Format → fenced code block."""
        return self._code_fence(content.memory_text)

    def format_TeammateMessage(
        self, content: TeammateMessage, message: TemplateMessage
    ) -> str:
        """Format → one blockquote per <teammate-message> block.

        Each block's header is a single line with a colored-circle emoji +
        teammate-id (bold) + optional italic summary; the body follows as
        a block-quoted markdown segment. Markdown can't color, so the
        circle-emoji convention gives a quick visual cue in the plain
        rendering.
        """
        session_colors = self._colors_for(message)
        parts: list[str] = []
        if content.leading_text:
            parts.append(content.leading_text)
        for block in content.blocks:
            parts.append(self._format_teammate_block_markdown(block, session_colors))
        if content.trailing_text:
            parts.append(content.trailing_text)
        return "\n\n".join(p for p in parts if p)

    def _format_teammate_block_markdown(
        self, block: Any, session_colors: dict[str, str]
    ) -> str:
        # Inline color wins; fall back to the session-scoped color map
        # so later heartbeat/status blocks (which usually omit `color=`)
        # still get the right circle-emoji marker.
        color = block.color or session_colors.get(block.teammate_id)
        emoji = _COLOR_CIRCLE.get((color or "").lower(), _COLOR_CIRCLE["default"])
        if block.is_system:
            emoji = _COLOR_CIRCLE["system"]
        header_parts: list[str] = [f"{emoji} **{block.teammate_id}**"]
        if block.summary:
            header_parts.append(f"*{block.summary}*")
        header = " · ".join(header_parts)
        body = block.body.strip()
        if not body:
            return header
        return f"{header}\n\n{self._quote(body)}"

    # -------------------------------------------------------------------------
    # Assistant Content Formatters
    # -------------------------------------------------------------------------

    def format_AssistantTextMessage(
        self, content: AssistantTextMessage, _: TemplateMessage
    ) -> str:
        """Format → blockquoted text."""
        parts: list[str] = []
        for item in content.items:
            if isinstance(item, ImageContent):
                parts.append(self._format_image(item))
            else:  # TextContent
                if item.text.strip():
                    # Quote to protect embedded markdown; SHA-linkify
                    # before quoting so the substitution happens on
                    # the natural text shape (a leading "> " would
                    # confuse the word-boundary regex anchor).
                    parts.append(self._quote(self._linkify_shas(item.text)))
        return "\n\n".join(parts)

    def format_ThinkingMessage(
        self, content: ThinkingMessage, _: TemplateMessage
    ) -> str:
        """Format → <details><summary>Thinking...</summary>blockquote</details>."""
        # SHA-linkify before quoting so the substitution sees a clean
        # word boundary (a leading ``> `` would confuse the regex
        # anchor) and parity with the HTML side — assistant thinking
        # there flows through the mistune pipeline which already
        # has the SHA plugin registered.
        quoted = self._quote(self._linkify_shas(content.thinking))
        return self._collapsible("Thinking...", quoted)

    def format_UnknownMessage(self, content: UnknownMessage, _: TemplateMessage) -> str:
        """Format → '*Unknown content type: ...*'."""
        return f"*Unknown content type: {content.type_name}*"

    # -------------------------------------------------------------------------
    # Tool Input Formatters
    # -------------------------------------------------------------------------

    def format_BashInput(self, input: BashInput, _: TemplateMessage) -> str:
        """Format → '```bash\\n$ command\\n```'."""
        # Description is in the title, just show the command with $ prefix
        return self._code_fence(f"$ {input.command}", "bash")

    def format_ReadInput(self, input: ReadInput, _: TemplateMessage) -> str:
        """Format → '*(lines N–M)*' or empty."""
        # File path goes in the collapsible summary of ReadOutput
        # Just show line range hint here if applicable
        if input.offset or input.limit:
            start = input.offset or 0
            end = start + (input.limit or 0)
            return f"*(lines {start}–{end})*"
        return ""

    def format_WriteInput(self, input: WriteInput, _: TemplateMessage) -> str:
        """Format → collapsible with file path + fenced content."""
        summary = f"<code>{input.file_path}</code>"
        content = self._code_fence(input.content, self._lang_from_path(input.file_path))
        return self._collapsible(summary, content)

    def format_EditInput(self, input: EditInput, _: TemplateMessage) -> str:
        """Format → '```diff\\n...\\n```'."""
        # Diff is visible; result goes in collapsible in format_EditOutput
        diff_text = generate_unified_diff(input.old_string, input.new_string)
        return self._code_fence(diff_text, "diff")

    def format_MultiEditInput(self, input: MultiEditInput, _: TemplateMessage) -> str:
        """Format → multiple '**Edit N:**' + diff blocks."""
        # All diffs visible; result goes in collapsible in format_EditOutput
        parts: list[str] = []
        for i, edit in enumerate(input.edits, 1):
            parts.append(f"**Edit {i}:**")
            diff_text = generate_unified_diff(edit.old_string, edit.new_string)
            parts.append(self._code_fence(diff_text, "diff"))
        return "\n\n".join(parts)

    def format_GlobInput(self, _input: GlobInput, _: TemplateMessage) -> str:
        """Format → '' (pattern in title)."""
        # Pattern and path are in the title
        return ""

    def format_GrepInput(self, input: GrepInput, _: TemplateMessage) -> str:
        """Format → 'Glob: `pattern`' or empty."""
        # Pattern and path are in the title, only show glob filter if present
        if input.glob:
            return f"Glob: `{input.glob}`"
        return ""

    def format_TaskInput(self, input: TaskInput, _: TemplateMessage) -> str:
        """Format → collapsible 'Instructions' with prompt."""
        # Description is now in the title, just show prompt as collapsible
        return (
            self._collapsible("Instructions", self._quote(input.prompt))
            if input.prompt
            else ""
        )

    def format_TodoWriteInput(self, input: TodoWriteInput, _: TemplateMessage) -> str:
        """Format → '- ⬜ task1\\n- ✅ task2'."""
        parts: list[str] = []
        for todo in input.todos:
            status_icon = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(
                todo.status, "⬜"
            )
            parts.append(f"- {status_icon} {todo.content}")
        return "\n".join(parts)

    def format_AskUserQuestionInput(
        self, _input: AskUserQuestionInput, _: TemplateMessage
    ) -> str:
        """Format → '' (rendered with output)."""
        # Input is rendered together with output in format_AskUserQuestionOutput
        return ""

    def format_ExitPlanModeInput(
        self, _input: ExitPlanModeInput, _: TemplateMessage
    ) -> str:
        """Format → '' (title only)."""
        # Title contains "Exiting plan mode", body is empty
        return ""

    def format_WebSearchInput(self, _input: WebSearchInput, _: TemplateMessage) -> str:
        """Format → '' (query shown in title)."""
        # Query is shown in the title, body is empty
        return ""

    def format_WebFetchInput(self, input: WebFetchInput, _: TemplateMessage) -> str:
        """Format → '' (url in title, prompt if long)."""
        if len(input.prompt) > 100:
            return self._code_fence(input.prompt)
        return ""

    def format_WorkflowToolInput(
        self, input: WorkflowToolInput, _: TemplateMessage
    ) -> str:
        """Format → optional one-line meta header + the JS script in a fenced
        ```js block (issue #174).

        Registering ``Workflow`` in TOOL_INPUT_MODELS is format-neutral, so
        this method is required for parity — without it the Markdown renderer
        would emit nothing for the script (a regression from the pre-PR
        ToolUseContent ``**script:**`` fallback).
        """
        from ..workflow import resolve_workflow_header

        script = input.script or ""
        name, description, phases = resolve_workflow_header(input.workflow_run, script)

        parts: list[str] = []
        header_bits: list[str] = []
        # Neutralize raw HTML tags in script-derived fields before injecting
        # into Markdown (CR #205) — mirrors the file's _protect_html_tags
        # posture so a permissive viewer can't interpret e.g. a <script> in a
        # workflow name/description/phase. (The script body itself is fenced.)
        if name:
            header_bits.append(f"**{_protect_html_tags(name)}**")
        if description:
            header_bits.append(_protect_html_tags(description))
        header = " — ".join(header_bits)
        if phases:
            safe_phases = ", ".join(_protect_html_tags(p) for p in phases)
            phase_text = f"_(phases: {safe_phases})_"
            header = f"{header} {phase_text}" if header else phase_text
        if header:
            parts.append(header)
        if script.strip():
            parts.append(self._code_fence(script, "js"))
        return "\n\n".join(parts)

    def format_WorkflowPhaseMessage(
        self, content: WorkflowPhaseMessage, _: TemplateMessage
    ) -> str:
        """Format → a spliced workflow phase card body: detail + agent count
        (issue #174 PR3). The phase title is the heading (``title_content``)."""
        bits: list[str] = []
        if content.detail:
            bits.append(_protect_html_tags(content.detail))
        if content.agent_count:
            unit = "agent" if content.agent_count == 1 else "agents"
            bits.append(f"_{content.agent_count} {unit}_")
        return " — ".join(bits)

    def format_WorkflowAgentMessage(
        self, content: WorkflowAgentMessage, _: TemplateMessage
    ) -> str:
        """Format → a spliced workflow agent card body: a meta line (model /
        state / tokens / tool calls) above the result — a StructuredOutput dict
        fenced as JSON, a plain string emitted as Markdown (issue #174 PR3)."""
        parts: list[str] = []
        meta_bits: list[str] = []
        if content.model:
            meta_bits.append(f"model: `{_protect_html_tags(content.model)}`")
        if content.state:
            meta_bits.append(f"state: {_protect_html_tags(content.state)}")
        if content.tokens is not None:
            meta_bits.append(f"{content.tokens} tokens")
        if content.tool_calls is not None:
            unit = "call" if content.tool_calls == 1 else "calls"
            meta_bits.append(f"{content.tool_calls} tool {unit}")
        if meta_bits:
            parts.append("_" + " · ".join(meta_bits) + "_")

        result = content.result
        if isinstance(result, (dict, list)):
            parts.append(
                self._code_fence(
                    json.dumps(result, indent=2, ensure_ascii=False), "json"
                )
            )
        elif isinstance(result, str) and result.strip():
            parts.append(result)
        elif content.result_preview:
            parts.append(_protect_html_tags(content.result_preview))
        return "\n\n".join(parts)

    def format_MonitorInput(self, input: MonitorInput, _: TemplateMessage) -> str:
        """Format → bullet list of fields with the command in a fenced block.

        Mirrors the HTML 4-row grid in plain Markdown. The command's
        adaptive fence width comes from ``_code_fence``; the description
        is also in the title but kept here so the body stands alone.
        """
        lines: list[str] = [f"- **description:** {input.description}"]
        if input.timeout_ms is not None:
            lines.append(f"- **timeout_ms:** {input.timeout_ms}")
        if input.persistent is not None:
            lines.append(f"- **persistent:** {input.persistent}")
        lines.append("")
        lines.append("**command:**")
        lines.append("")
        lines.append(self._code_fence(input.command, "bash"))
        return "\n".join(lines)

    def format_MonitorOutput(self, output: MonitorOutput, _: TemplateMessage) -> str:
        """Format → start-confirmation paragraph verbatim."""
        return output.text

    # -- ScheduleWakeup / Cron* family ---------------------------------------

    def format_ScheduleWakeupInput(
        self, input: ScheduleWakeupInput, _: TemplateMessage
    ) -> str:
        """Format → prompt as raw Markdown.

        ``delaySeconds`` and ``reason`` are already in the title
        (``⏰ ScheduleWakeup +<delay>s — <reason>``); duplicating them
        in the body adds noise. The prompt is typically Markdown
        content (slash commands, inline code, prose) rather than
        preformatted text, so emit it directly into the Markdown
        document instead of wrapping in a fenced code block.
        """
        return input.prompt

    def format_ScheduleWakeupOutput(
        self, output: ScheduleWakeupOutput, _: TemplateMessage
    ) -> str:
        """Format → status paragraph verbatim."""
        return output.text

    def format_CronCreateInput(self, input: CronCreateInput, _: TemplateMessage) -> str:
        """Format → prompt as raw Markdown.

        ``cron`` is already in the title (``⏰ CronCreate <cron>``)
        and the harness's confirmation echoes back ``recurring`` /
        ``durable`` in human-readable form, so the body doesn't need
        to repeat any input scalars. The prompt is typically Markdown
        content; emit it directly rather than wrapping in a fenced
        code block.
        """
        return input.prompt

    def format_CronCreateOutput(
        self, output: CronCreateOutput, _: TemplateMessage
    ) -> str:
        """Format → confirmation paragraph verbatim."""
        return output.text

    def format_CronListInput(self, _input: CronListInput, _: TemplateMessage) -> str:
        """Format → '' (no inputs; title is sufficient)."""
        return ""

    def format_CronListOutput(self, output: CronListOutput, _: TemplateMessage) -> str:
        """Format → bullet list of jobs when parseable, raw text otherwise."""
        if not output.jobs:
            return self._code_fence(output.text)
        lines: list[str] = []
        for job in output.jobs:
            preview = job.prompt if len(job.prompt) <= 80 else job.prompt[:77] + "…"
            lines.append(f"- `{job.id}` — {job.description} — {preview}")
        return "\n".join(lines)

    def format_CronDeleteInput(
        self, _input: CronDeleteInput, _: TemplateMessage
    ) -> str:
        """Format → '' (id in title)."""
        return ""

    def format_CronDeleteOutput(
        self, output: CronDeleteOutput, _: TemplateMessage
    ) -> str:
        """Format → status paragraph verbatim."""
        return output.text

    def format_SkillInput(self, _input: SkillInput, _: TemplateMessage) -> str:
        """Format → '' (skill name in title; body folded in via skill_body)."""
        return ""

    # --- Teammate-feature tool inputs --------------------------------------

    def format_TeamCreateInput(self, input: TeamCreateInput, _: TemplateMessage) -> str:
        """Format → bullet list with team/agent_type/description."""
        lines: list[str] = [f"- **Team:** `{input.team_name}`"]
        if input.agent_type:
            lines.append(f"- **Agent type:** `{input.agent_type}`")
        if input.description:
            lines.append(f"- **Description:** {input.description}")
        return "\n".join(lines)

    def format_TeamDeleteInput(
        self, _input: TeamDeleteInput, _: TemplateMessage
    ) -> str:
        """Format → '' (TeamDelete takes no meaningful params)."""
        return ""

    def format_TaskCreateInput(self, input: TaskCreateInput, _: TemplateMessage) -> str:
        """Format → subject + description bullets."""
        lines: list[str] = [f"- **Subject:** {input.subject}"]
        if input.activeForm:
            lines.append(f"- **Active form:** {input.activeForm}")
        if input.description:
            lines.append(f"- **Description:** {input.description}")
        return "\n".join(lines)

    def format_TaskUpdateInput(
        self, input: TaskUpdateInput, message: TemplateMessage
    ) -> str:
        """Format → '#N status:foo owner:🔵 name'."""
        parts: list[str] = [f"`#{input.taskId}`"]
        if input.status:
            parts.append(f"**status:** `{input.status}`")
        if input.owner:
            color = self._colors_for(message).get(input.owner)
            parts.append(f"**owner:** {_teammate_marker(input.owner, color)}")
        return " · ".join(parts)

    def format_TaskListInput(self, _input: TaskListInput, _: TemplateMessage) -> str:
        """Format → '' (TaskList takes no params)."""
        return ""

    def format_SendMessageInput(
        self, input: SendMessageInput, message: TemplateMessage
    ) -> str:
        """Format → recipient marker + type + blockquoted content."""
        lines: list[str] = []
        if input.recipient:
            color = self._colors_for(message).get(input.recipient)
            lines.append(f"**To:** {_teammate_marker(input.recipient, color)}")
        if input.type:
            lines.append(f"**Type:** `{input.type}`")
        result = "\n\n".join(lines) if lines else ""
        if input.content:
            body = self._quote(input.content)
            result = f"{result}\n\n{body}" if result else body
        return result

    def format_TaskOutputInput(self, input: TaskOutputInput, _: TemplateMessage) -> str:
        """Format → task_id + optional block/timeout (async-agents poll)."""
        parts: list[str] = []
        if input.task_id:
            parts.append(f"`#{input.task_id}`")
        if input.block:
            parts.append("**block:** `true`")
        if input.timeout:
            parts.append(f"**timeout:** `{input.timeout} ms`")
        return " · ".join(parts)

    def format_TaskStopInput(self, _input: TaskStopInput, _: TemplateMessage) -> str:
        """Format → empty (id lives in the title, no further params)."""
        return ""

    def format_TaskStopOutput(self, output: TaskStopOutput, _: TemplateMessage) -> str:
        """Format → ``Stopped`` / ``Not stopped`` badge + verbatim message
        (PR #158 follow-up; same intent as the HTML formatter but plain text).

        Uses ``_code_fence`` so the fence widens past any backticks in
        the harness message (the success-shape message often echoes the
        original command, which can plausibly include shell-quoted
        backticks).
        """
        badge = "**Stopped**" if output.stopped else "**Not stopped**"
        if output.message:
            return f"{badge}\n\n{self._code_fence(output.message)}"
        return badge

    def format_TaskOutputResult(
        self, output: TaskOutputResult, _: TemplateMessage
    ) -> str:
        """Format → status / type / retrieval + transcript path.

        Skip the truncated ``<output>`` snapshot — the agent's full
        transcript already inlines as a sidechain.
        """
        parts: list[str] = []
        if output.task_id:
            parts.append(f"`#{output.task_id}`")
        if output.task_type:
            parts.append(f"**type:** `{output.task_type}`")
        if output.status:
            parts.append(f"**status:** `{output.status}`")
        if output.retrieval_status:
            parts.append(f"**retrieval:** `{output.retrieval_status}`")
        head = " · ".join(parts) if parts else ""
        if output.output_truncated and output.output_file:
            head = (
                f"{head}\n\n**Transcript:** `{output.output_file}`"
                if head
                else (f"**Transcript:** `{output.output_file}`")
            )
        return head

    def format_TaskNotificationMessage(
        self, content: TaskNotificationMessage, _: TemplateMessage
    ) -> str:
        """Format → metadata bullets + collapsible Markdown body for an
        async-agent ``<task-notification>`` user entry (issue #90).

        When ``_link_async_notifications`` flagged the body as a
        duplicate of the spawning Task's last sub-assistant, the body
        is dropped and only metadata + a "Spawn" reference remains so
        the Markdown reader still has navigation context without
        doubling the result content. At ``DetailLevel.LOW`` the
        whole card is "ghosted" (returns ``""``) — paired with
        ``title_TaskNotificationMessage`` returning ``""`` too,
        ``_render_message``'s "no title, no content" elision drops
        the entry from the rendered output without touching
        ``ctx.messages``.
        """
        if self.detail == DetailLevel.LOW and content.result_is_duplicate:
            return ""
        lines: list[str] = []
        if content.task_id:
            lines.append(f"- **Task ID:** `{content.task_id}`")
        if content.status:
            lines.append(f"- **Status:** `{content.status}`")
        usage = content.usage
        if usage is not None:
            if usage.total_tokens is not None:
                lines.append(f"- **Tokens:** `{usage.total_tokens:,}`")
            if usage.tool_uses is not None:
                lines.append(f"- **Tool uses:** `{usage.tool_uses}`")
            if usage.duration_ms is not None:
                lines.append(f"- **Duration:** `{usage.duration_ms / 1000:.1f}s`")
        if content.transcript_path:
            lines.append(f"- **Transcript:** `{content.transcript_path}`")
        if content.spawning_task_message_index is not None:
            lines.append(
                f"- **Spawn:** ↱ Task `#d-{content.spawning_task_message_index}`"
            )
        head = "\n".join(lines)
        if content.result_text and not content.result_is_duplicate:
            body = self._collapsible("Result", content.result_text)
            return f"{head}\n\n{body}" if head else body
        return head

    def format_ToolUseContent(self, content: ToolUseContent, _: TemplateMessage) -> str:
        """Fallback for unknown tool inputs - render as key/value list."""
        return self._render_params(content.input)

    def format_ToolUseMessage(
        self, content: ToolUseMessage, message: TemplateMessage
    ) -> str:
        """Append the folded Skill body (if set) as raw markdown (issue #93)."""
        rendered = super().format_ToolUseMessage(content, message)
        if content.skill_body:
            # The skill body is already markdown; include it verbatim, with
            # a blank line separator from the params list above.
            rendered = (
                f"{rendered}\n\n{content.skill_body}"
                if rendered
                else content.skill_body
            )
        return rendered

    def _render_params(self, params: dict[str, Any]) -> str:
        """Render parameters as a markdown key/value list."""
        if not params:
            return "*No parameters*"

        lines: list[str] = []
        for key, value in params.items():
            if isinstance(value, (dict, list)):
                # Structured value - render as JSON code block
                formatted = json.dumps(value, indent=2, ensure_ascii=False)
                lines.append(f"**{key}:**")
                lines.append(self._code_fence(formatted, "json"))
            elif isinstance(value, str) and len(value) > 100:
                # Long string - render as code block
                lines.append(f"**{key}:**")
                lines.append(self._code_fence(value))
            else:
                # Simple value - inline
                lines.append(f"**{key}:** `{value}`")
        return "\n\n".join(lines)

    # -------------------------------------------------------------------------
    # Tool Output Formatters
    # -------------------------------------------------------------------------

    def format_ReadOutput(self, output: ReadOutput, _: TemplateMessage) -> str:
        """Format → collapsible with file path + syntax-highlighted content."""
        summary = f"<code>{output.file_path}</code>" if output.file_path else "Content"
        lang = self._lang_from_path(output.file_path or "")
        content = self._code_fence(output.content, lang)
        return self._collapsible(summary, content)

    def format_WriteOutput(self, output: WriteOutput, _: TemplateMessage) -> str:
        """Format → '✓ Wrote N bytes'."""
        return f"✓ {output.message}"

    def format_EditOutput(self, output: EditOutput, _: TemplateMessage) -> str:
        """Format → collapsible with result or '✓ Edited'."""
        if msg := output.message:
            content = self._code_fence(msg, self._lang_from_path(output.file_path))
            return self._collapsible(f"<code>{output.file_path}</code>", content)
        return "✓ Edited"

    def format_BashOutput(self, output: BashOutput, _: TemplateMessage) -> str:
        """Format → fenced code block (ANSI stripped, diff detected)."""
        # Strip ANSI codes for markdown output
        text = re.sub(r"\x1b\[[0-9;]*m", "", output.content)
        # Detect git diff output
        lang = "diff" if text.startswith("diff --git a/") else ""
        return self._code_fence(text, lang)

    def format_GlobOutput(self, output: GlobOutput, _: TemplateMessage) -> str:
        """Format → '- `file1`\\n- `file2`' or '*No files found*'."""
        if not output.files:
            return "*No files found*"
        return "\n".join(f"- `{f}`" for f in output.files)

    # Note: GrepOutput is not used (tool results handled as raw strings)
    # Grep results fall back to format_ToolResultContent

    def format_AskUserQuestionOutput(
        self, output: AskUserQuestionOutput, message: TemplateMessage
    ) -> str:
        """Format AskUserQuestion with interleaved Q/options/A.

        Uses message.pair_first to look up paired input for question options.
        """
        # Get questions from paired input via pair_first
        questions_map: dict[str, Any] = {}
        if message.pair_first is not None and self._ctx:
            pair_msg = self._ctx.get(message.pair_first)
            if pair_msg and isinstance(pair_msg.content, ToolUseMessage):
                input_content = pair_msg.content.input
                if isinstance(input_content, AskUserQuestionInput):
                    questions_map = {q.question: q for q in input_content.questions}

        parts: list[str] = []
        for qa in output.answers:
            # Question in italics
            parts.append(f"**Q:** *{qa.question}*")

            # Options from paired input (if available)
            if qa.question in questions_map:
                q = questions_map[qa.question]
                for option in q.options:
                    parts.append(f"- {option.label}: {option.description}")

            # Answer
            parts.append(f"**A:** {qa.answer}")
            parts.append("")  # Blank line between Q&A pairs

        return "\n\n".join(parts).rstrip()

    def format_TaskOutput(self, output: TaskOutput, _: TemplateMessage) -> str:
        """Format → collapsible 'Report' with blockquoted result.

        For async-spawned Tasks (issue #90), ``output.result`` is just
        the "Async agent launched successfully…" stub. The real answer
        is folded onto ``output.async_final_answer`` by
        ``_link_async_notifications``; emit it as a second collapsible
        block so the spawn carries the actual agent answer.
        """
        parts: list[str] = [self._collapsible("Report", self._quote(output.result))]
        if output.async_final_answer:
            parts.append(
                self._collapsible(
                    "Result (from async notification)",
                    output.async_final_answer,
                )
            )
        return "\n\n".join(parts)

    def format_ExitPlanModeOutput(
        self, output: ExitPlanModeOutput, _: TemplateMessage
    ) -> str:
        """Format → '✓ Approved' or '✗ Not approved'."""
        status = "✓ Approved" if output.approved else "✗ Not approved"
        if output.message:
            return f"{status}\n\n{output.message}"
        return status

    def format_WebSearchOutput(
        self, output: WebSearchOutput, _: TemplateMessage
    ) -> str:
        """Format → summary, then links at bottom after separator."""
        parts: list[str] = []

        # Summary first (the analysis text)
        if output.summary:
            parts.append(self._quote(output.summary))

        # Links at the bottom after a separator
        if output.links:
            if parts:
                parts.append("")
                parts.append("---")
                parts.append("")
            for link in output.links:
                parts.append(f"- [{link.title}]({link.url})")
        elif not output.summary:
            # Only show "no results" if there's also no summary
            parts.append("*No results found*")

        return "\n".join(parts)

    def format_WebFetchOutput(self, output: WebFetchOutput, _: TemplateMessage) -> str:
        """Format → metadata line + blockquoted result.

        WebFetch results are AI-generated summaries, not raw content,
        so a collapsible section isn't needed - use blockquote directly.
        """
        meta_parts: list[str] = []
        if output.code is not None:
            status = f"{output.code} {output.code_text or ''}".strip()
            meta_parts.append(status)
        if output.bytes is not None:
            if output.bytes >= 1024 * 1024:
                meta_parts.append(f"{output.bytes / (1024 * 1024):.1f} MB")
            elif output.bytes >= 1024:
                meta_parts.append(f"{output.bytes / 1024:.1f} KB")
            else:
                meta_parts.append(f"{output.bytes} bytes")
        if output.duration_ms is not None:
            if output.duration_ms >= 1000:
                meta_parts.append(f"{output.duration_ms / 1000:.1f}s")
            else:
                meta_parts.append(f"{output.duration_ms}ms")
        meta_line = f"*{' · '.join(meta_parts)}*\n\n" if meta_parts else ""
        return meta_line + self._quote(output.result)

    # --- Teammate-feature tool outputs -------------------------------------

    def format_TeamCreateOutput(
        self, output: TeamCreateOutput, _: TemplateMessage
    ) -> str:
        """Format → bullet list: team / lead / config."""
        lines: list[str] = [f"- **Team:** `{output.team_name}`"]
        if output.lead_agent_id:
            lines.append(f"- **Lead:** `{output.lead_agent_id}`")
        if output.team_file_path:
            lines.append(f"- **Config:** `{output.team_file_path}`")
        return "\n".join(lines)

    def format_TeamDeleteOutput(
        self, output: TeamDeleteOutput, message: TemplateMessage
    ) -> str:
        """Format → ✓/✗ status + message + active members (colored markers)."""
        status = "✓ Deleted" if output.success else "✗ Refused"
        lines: list[str] = [status]
        if output.team_name:
            lines.append(f"**Team:** `{output.team_name}`")
        if output.message:
            lines.append(output.message)
        if output.active_members:
            colors = self._colors_for(message)
            markers = [
                _teammate_marker(name, colors.get(name))
                for name in output.active_members
            ]
            lines.append(f"**Active members:** {', '.join(markers)}")
        return "\n\n".join(lines)

    def format_TaskCreateOutput(
        self, output: TaskCreateOutput, _: TemplateMessage
    ) -> str:
        """Format → '#N — subject'."""
        if output.subject:
            return f"`#{output.task_id}` — {output.subject}"
        return f"`#{output.task_id}`"

    def format_TaskUpdateOutput(
        self, output: TaskUpdateOutput, _: TemplateMessage
    ) -> str:
        """Format → ✓ Updated #N (fields)."""
        status = "✓ Updated" if output.success else "✗ Not updated"
        prefix = f"{status} `#{output.task_id}`"
        if output.updated_fields:
            fields = ", ".join(f"`{name}`" for name in output.updated_fields)
            prefix = f"{prefix} — {fields}"
        if output.status_change is not None and (
            output.status_change.from_status or output.status_change.to_status
        ):
            prefix = (
                f"{prefix}\n\n"
                f"**{output.status_change.from_status or '?'}**"
                f" → **{output.status_change.to_status or '?'}**"
            )
        return prefix

    def format_TaskListOutput(
        self, output: TaskListOutput, message: TemplateMessage
    ) -> str:
        """Format → Markdown table with id/status/subject/owner."""
        if not output.tasks:
            return "*No tasks*"
        colors = self._colors_for(message)
        lines: list[str] = [
            "| # | Status | Subject | Owner |",
            "|---|---|---|---|",
        ]
        for task in output.tasks:
            owner_marker = (
                _teammate_marker(task.owner, colors.get(task.owner))
                if task.owner
                else ""
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"#{_table_cell(task.id)}",
                        _table_cell(task.status),
                        _table_cell(task.subject),
                        # owner_marker is HTML-safe + colon-free by
                        # construction (palette circle + backticked id),
                        # so skip escaping to keep the backticks live.
                        owner_marker,
                    ]
                )
                + " |"
            )
        return "\n".join(lines)

    def format_SendMessageOutput(
        self, output: SendMessageOutput, message: TemplateMessage
    ) -> str:
        """Format → ✓/✗ status + target + request id + message."""
        status = "✓ Sent" if output.success else "✗ Failed"
        lines: list[str] = [status]
        if output.target:
            colors = self._colors_for(message)
            lines.append(
                f"**To:** {_teammate_marker(output.target, colors.get(output.target))}"
            )
        if output.request_id:
            lines.append(f"**Request:** `{output.request_id}`")
        if output.message:
            lines.append(output.message)
        return "\n\n".join(lines)

    def format_ToolResultContent(
        self, output: ToolResultContent, message: TemplateMessage
    ) -> str:
        """Fallback for unknown tool outputs."""
        # TodoWrite success message - render as plain text, not code fence
        content = message.content
        if isinstance(content, ToolResultMessage) and content.tool_name == "TodoWrite":
            if isinstance(output.content, str):
                return output.content
            return ""
        # Default: code fence
        if isinstance(output.content, str):
            text = strip_error_tags(output.content)
            return self._code_fence(text)
        # Structured list content: render text items as text and keep any
        # other typed items (e.g. ToolSearch's tool_reference) as a JSON
        # block, instead of dumping the whole list verbatim (issue #227).
        text_parts: list[str] = []
        structured_items: list[dict[str, Any]] = []
        for item in output.content:
            if item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
            else:
                structured_items.append(item)
        parts: list[str] = []
        text = strip_error_tags("\n".join(text_parts)).strip()
        if text:
            parts.append(text)
        if structured_items:
            parts.append(
                self._code_fence(
                    json.dumps(structured_items, indent=2, ensure_ascii=False), "json"
                )
            )
        return "\n\n".join(parts)

    # -------------------------------------------------------------------------
    # Title Methods (for tool use dispatch)
    # -------------------------------------------------------------------------

    def title_BashInput(self, input: BashInput, _: TemplateMessage) -> str:
        """Title → '💻 Bash: *description* [async #<id>]'.

        Async hint when the spawn is backgrounded (PR #158 follow-up).
        Gated on EITHER ``input.run_in_background`` (caller's hint) OR
        ``input.minted_background_task_id`` (propagated from the
        tool_result's ``backgroundTaskId`` by
        ``_link_task_id_consumers``). The harness backgrounds some
        Bash commands without setting the input flag, so the
        result-side signal is the authoritative one. Markdown carries
        no clickable anchor; the ``#<id>`` is plain inline-code so it
        still greps cleanly across the document.
        """
        base = (
            f"💻 Bash: *{self._escape_stars(input.description)}*"
            if input.description
            else "💻 Bash"
        )
        if not (input.run_in_background or input.minted_background_task_id):
            return base
        suffix = self._async_id_hint(input.minted_background_task_id)
        return f"{base} {suffix}"

    def title_ReadInput(self, input: ReadInput, _: TemplateMessage) -> str:
        """Title → '👀 Read `filename`', or '🧠 Read memory `short-path`' for
        an auto-memory path (#192; mirrors the HTML renderer)."""
        if is_memory_path(input.file_path):
            return f"🧠 Read memory `{memory_short_path(input.file_path)}`"
        return f"👀 Read `{Path(input.file_path).name}`"

    def title_WriteInput(self, input: WriteInput, _: TemplateMessage) -> str:
        """Title → '✍️  Write `filename`', or '🧠 Write memory `short-path`'
        for an auto-memory path (#192)."""
        if is_memory_path(input.file_path):
            return f"🧠 Write memory `{memory_short_path(input.file_path)}`"
        return f"✍️  Write `{Path(input.file_path).name}`"

    def title_EditInput(self, input: EditInput, _: TemplateMessage) -> str:
        """Title → '✏️  Edit `filename`', or '🧠 Edit memory `short-path`' for
        an auto-memory path (#192)."""
        if is_memory_path(input.file_path):
            return f"🧠 Edit memory `{memory_short_path(input.file_path)}`"
        return f"✏️  Edit `{Path(input.file_path).name}`"

    def title_MultiEditInput(self, input: MultiEditInput, _: TemplateMessage) -> str:
        """Title → '✏️  MultiEdit `filename`'."""
        return f"✏️  MultiEdit `{Path(input.file_path).name}`"

    def title_GlobInput(self, input: GlobInput, _: TemplateMessage) -> str:
        """Title → '📂 Glob `pattern`[ in `path`]'."""
        title = f"📂 Glob `{input.pattern}`"
        return f"{title} in `{input.path}`" if input.path else title

    def title_GrepInput(self, input: GrepInput, _: TemplateMessage) -> str:
        """Title → '🔎 Grep `pattern`[ in `path`]'."""
        base = f"🔎 Grep `{input.pattern}`"
        return f"{base} in `{input.path}`" if input.path else base

    def title_TaskInput(self, input: TaskInput, _: TemplateMessage) -> str:
        """Title → '🤖 Task (subagent): *description* [async #<id>]'.

        ``[async]`` muted hint when ``run_in_background=True``; once the
        launch confirmation has been parsed, the minted ``#<agent_id>``
        is appended (PR #158 follow-up, parallels the Bash spawn shape).
        """
        subagent = f" ({input.subagent_type})" if input.subagent_type else ""
        async_hint = (
            " " + self._async_id_hint(input.minted_agent_id)
            if input.run_in_background
            else ""
        )
        if desc := input.description:
            return f"🤖 Task{subagent}: *{self._escape_stars(desc)}*{async_hint}"
        return f"🤖 Task{subagent}{async_hint}"

    @staticmethod
    def _async_id_hint(minted_id: Optional[str]) -> str:
        """Compose the trailing ``*[async ...]*`` Markdown marker for
        spawn cards of background ``Bash`` / async ``Task`` calls.

        ``*[async]*`` when the id hasn't been hoisted yet (no matching
        tool_result observed); ``*[async `#<id>`]*`` once it has.
        Markdown has no clickable anchor here on purpose — backlinks
        and forward-links are HTML-only (per the #154 convention).
        """
        if not minted_id:
            return "*[async]*"
        return f"*[async `#{minted_id}`]*"

    def title_TaskOutputInput(self, input: TaskOutputInput, _: TemplateMessage) -> str:
        """Title → '🔍 TaskOutput `#<task_id>`' for the async-agent
        polling tool (issue #90)."""
        if input.task_id:
            return f"🔍 TaskOutput `#{input.task_id}`"
        return "🔍 TaskOutput"

    def title_TaskStopInput(self, input: TaskStopInput, _: TemplateMessage) -> str:
        """Title → '🛑 TaskStop `#<task_id>`' (PR #158 follow-up — was
        rendered as a generic tool block before)."""
        if input.task_id:
            return f"🛑 TaskStop `#{input.task_id}`"
        return "🛑 TaskStop"

    def title_TaskNotificationMessage(
        self, content: TaskNotificationMessage, _: TemplateMessage
    ) -> str:
        """Title → '🔄 Async result · *<summary>*' for an async-agent
        completion notification (issue #90).

        Empty at ``DetailLevel.LOW`` for duplicate-flagged
        notifications — pairs with
        ``format_TaskNotificationMessage`` to "ghost" the entry.
        """
        if self.detail == DetailLevel.LOW and content.result_is_duplicate:
            return ""
        if content.summary:
            return f"🔄 Async result · *{self._escape_stars(content.summary)}*"
        if content.task_id:
            return f"🔄 Async result `#{content.task_id}`"
        return "🔄 Async result"

    def title_TodoWriteInput(self, _input: TodoWriteInput, _: TemplateMessage) -> str:
        """Title → '✅ Todo List'."""
        return "✅ Todo List"

    def title_AskUserQuestionInput(
        self, _input: AskUserQuestionInput, _: TemplateMessage
    ) -> str:
        """Title → '❓ Asking questions...'."""
        return "❓ Asking questions..."

    def title_ExitPlanModeInput(
        self, _input: ExitPlanModeInput, _: TemplateMessage
    ) -> str:
        """Title → '📝 Exiting plan mode'."""
        return "📝 Exiting plan mode"

    def title_WebSearchInput(self, input: WebSearchInput, _: TemplateMessage) -> str:
        """Title → '🔎 WebSearch `query`'."""
        return f"🔎 WebSearch `{input.query}`"

    def title_WebFetchInput(self, input: WebFetchInput, _: TemplateMessage) -> str:
        """Title → '🌐 WebFetch `url`' (truncated if > 60 chars)."""
        url = input.url[:60] + "…" if len(input.url) > 60 else input.url
        return f"🌐 WebFetch `{url}`"

    def title_MonitorInput(self, input: MonitorInput, _: TemplateMessage) -> str:
        """Title → '🔭 Monitor `<description>`'.

        Wraps the description in inline code via ``_inline_code`` —
        same recipe as ``title_WebSearchInput`` / ``title_WebFetchInput``
        / ``title_SkillInput``. The helper widens the fence past any
        backtick run in the value and escapes the value from
        Markdown emphasis / heading metacharacters that would otherwise
        leak into the rendered title (e.g. ``*`` / ``_`` / ``[``).
        """
        return f"🔭 Monitor {_inline_code(input.description)}"

    def title_ScheduleWakeupInput(
        self, input: ScheduleWakeupInput, _: TemplateMessage
    ) -> str:
        """Title → '⏰ ScheduleWakeup +<delay>s — `<reason>`'."""
        return (
            f"⏰ ScheduleWakeup +{input.delaySeconds}s — {_inline_code(input.reason)}"
        )

    def title_CronCreateInput(self, input: CronCreateInput, _: TemplateMessage) -> str:
        """Title → '⏰ CronCreate `<cron>`'."""
        return f"⏰ CronCreate {_inline_code(input.cron)}"

    def title_CronListInput(self, _input: CronListInput, _: TemplateMessage) -> str:
        """Title → '⏰ CronList'."""
        return "⏰ CronList"

    def title_CronDeleteInput(self, input: CronDeleteInput, _: TemplateMessage) -> str:
        """Title → '⏰ CronDelete `<id>`'."""
        return f"⏰ CronDelete {_inline_code(input.id)}"

    def title_SkillInput(self, input: SkillInput, _: TemplateMessage) -> str:
        """Title → '💡 Skill `<skill_name>`'."""
        return f"💡 Skill `{input.skill}`"

    def title_ThinkingMessage(
        self, _content: ThinkingMessage, _message: TemplateMessage
    ) -> str:
        """Title → '🤖 Assistant: *excerpt*' (paired) or '💭 Thinking: *excerpt*'."""
        is_sidechain = _message.meta.is_sidechain

        # When paired with Assistant, use Assistant title with assistant excerpt
        if _message.is_first_in_pair and _message.pair_last is not None:
            if (
                pair_msg := self._ctx.get(_message.pair_last) if self._ctx else None
            ) and isinstance(pair_msg.content, AssistantTextMessage):
                if is_sidechain:
                    if excerpt := self._excerpt(self._get_message_text(pair_msg)):
                        return f"🔗 Sub-assistant: *{self._escape_stars(excerpt)}*"
                    return "🔗 Sub-assistant"
                if excerpt := self._excerpt(self._get_message_text(pair_msg)):
                    return f"🤖 Assistant: *{self._escape_stars(excerpt)}*"
                return "🤖 Assistant"

        # Standalone thinking (use "Thinking" for both main and sidechain)
        if excerpt := self._excerpt(self._get_message_text(_message)):
            return f"💭 Thinking: *{self._escape_stars(excerpt)}*"
        return "💭 Thinking"

    def title_AssistantTextMessage(
        self, _content: AssistantTextMessage, message: TemplateMessage
    ) -> str:
        """Title → '🤖 Assistant: *excerpt*' or '' (if paired)."""
        # When paired (after Thinking), skip title (already rendered with Thinking)
        if message.is_last_in_pair:
            return ""
        # Sidechain assistant messages get excerpt too
        if message.meta.is_sidechain:
            if excerpt := self._excerpt(self._get_message_text(message)):
                return f"🔗 Sub-assistant: *{self._escape_stars(excerpt)}*"
            return "🔗 Sub-assistant"
        if excerpt := self._excerpt(self._get_message_text(message)):
            return f"🤖 Assistant: *{self._escape_stars(excerpt)}*"
        return "🤖 Assistant"

    # -------------------------------------------------------------------------
    # Core Generate Methods
    # -------------------------------------------------------------------------

    def _generate_toc(self, session_nav: list[dict[str, Any]]) -> str:
        """Generate table of contents from session navigation."""
        lines = ["## Sessions", ""]
        for session in session_nav:
            session_id = session.get("id", "")
            # Skip fork-point and compact-point nav items: both navigate
            # the index in HTML via ``msg-d-{N}`` anchors, but Markdown
            # only has session-level ``<a id="…">`` anchors and neither
            # creates one of their own. Compact items also carry an
            # ``id`` shaped like ``compact-{message_index}`` whose
            # ``[:8]`` slice (``"compact-"``) collapses to a single
            # malformed ``session-compact-`` anchor key for every
            # compact event in a long compacted session.
            if session.get("is_fork_point") or session.get("is_compaction_point"):
                continue
            anchor = _session_anchor(session_id)
            summary = session.get("summary")
            if session.get("is_branch"):
                # Branches reuse the rich ``Branch • <uuid8> • <preview>``
                # label that ``prepare_session_navigation`` already
                # composed via ``_branch_label`` and stored on
                # ``first_user_message`` — keeps the TOC entry aligned
                # with the body branch header and the HTML index nav.
                # Fallback (defensive — ``first_user_message`` is always
                # populated for branches today) mirrors the
                # ``_branch_label`` shape rather than diverging into a
                # backtick-quoted variant.
                label = session.get(
                    "first_user_message",
                    f"Branch • {branch_short_uuid(session_id)}",
                )
            else:
                session_short = session_id[:8]
                label = (
                    f"Session `{session_short}`: {summary}"
                    if summary
                    else f"Session `{session_short}`"
                )
            lines.append(f"- [{label}](#{anchor})")
        lines.append("")
        return "\n".join(lines)

    def _format_message_timestamp(self, msg: TemplateMessage) -> str:
        """Render the per-message timestamp line for *msg* (issue #160).

        Date-on-change rule: when *msg*'s date differs from
        ``self._last_date_seen`` (which is reset to ``None`` at the
        start of each ``generate()`` and on every ``SessionHeaderMessage``)
        the line carries the full ``**`YYYY-MM-DD`** `HH:MM:SS``` form;
        otherwise just the `` `HH:MM:SS` `` time component. Returns ``""``
        when the source timestamp is missing or unparseable, so the
        caller can append unconditionally.
        """
        raw = getattr(msg.meta, "timestamp", "") if msg.meta else ""
        if not raw:
            return ""
        formatted = format_timestamp(raw)
        # ``format_timestamp`` returns ``"YYYY-MM-DD HH:MM:SS"`` on
        # success and (per its contract) the raw input unchanged on
        # parse failure. A raw string with an embedded space would
        # otherwise pass a naive presence-of-space check and produce
        # garbage like ``**`not`** `a timestamp` ``; validate the
        # rendered shape instead.
        if not _TIMESTAMP_DISPLAY_RE.match(formatted):
            return ""
        date_part, _, time_part = formatted.partition(" ")
        if date_part != self._last_date_seen:
            self._last_date_seen = date_part
            return f"**`{date_part}`** `{time_part}`"
        return f"`{time_part}`"

    def _render_message(self, msg: TemplateMessage, level: int) -> str:
        """Render a message and its children recursively."""
        # Skip pair_middle and pair_last — both render under pair_first
        # (pair_first owns the heading and delegates body formatting).
        if msg.is_middle_in_pair or msg.is_last_in_pair:
            return ""

        parts: list[str] = []

        # Format content - for session headers, anchor goes before heading
        content = self.format_content(msg)
        is_session_header = isinstance(msg.content, SessionHeaderMessage)
        if is_session_header and content:
            parts.append(content)
            content = None  # Don't output again below

        # Heading with title (skip if empty)
        title = self.title_content(msg)
        if title:
            # Track the *rendered* heading category, not `msg_type`: a
            # paired `ThinkingMessage` renders an "🤖 Assistant: ..."
            # title, so a following standalone Assistant reuses that
            # category and should be compacted even though the raw
            # `message_type` strings differ ("thinking" vs "assistant").
            # The category is everything before the first ":" in the
            # rendered title (e.g. "🤖 Assistant", "🤷 User").
            heading_category = title.split(":", 1)[0].strip()

            # Compact mode: suppress heading for consecutive same-category
            # messages. Reset tracking on session boundaries — and also
            # reset the per-message timestamp `last_date_seen` so the
            # first message of every session re-emits its full date
            # (issue #160).
            if is_session_header:
                self._last_heading_category = None
                self._last_date_seen = None
            suppress_heading = (
                self.compact
                and not is_session_header
                and heading_category == self._last_heading_category
            )
            self._last_heading_category = heading_category

            if not suppress_heading:
                heading_level = min(level, 6)  # Markdown max is h6
                parts.append(f"{'#' * heading_level} {title}")
                # Per-message timestamp line (issue #160). Skip for
                # session headers (they have no meaningful per-msg time)
                # and when the heading was suppressed by `compact` mode
                # (the body would dangle behind a stray timestamp).
                if not self.no_timestamps and not is_session_header:
                    ts_line = self._format_message_timestamp(msg)
                    if ts_line:
                        parts.append(ts_line)

            # Format content (if not already output above)
            if content:
                parts.append(content)

        # Format paired message bodies (middle then last, when present).
        # Triples (slash-command META → CMD → OUT) deliver three bodies
        # under one heading; standard 2-msg pairs only have pair_last.
        pair_partners: list[TemplateMessage] = []
        if msg.is_first_in_pair:
            for partner_idx in (msg.pair_middle, msg.pair_last):
                if partner_idx is None or self._ctx is None:
                    continue
                if partner := self._ctx.get(partner_idx):
                    pair_partners.append(partner)
                    if pair_content := self.format_content(partner):
                        parts.append(pair_content)

        # Render children at next level (from this message and any paired members)
        all_children = list(msg.children)
        for partner in pair_partners:
            if partner.children:
                all_children.extend(partner.children)
        for child in all_children:
            if child_output := self._render_message(child, level + 1):
                parts.append(child_output)

        return "\n\n".join(parts)

    def generate(
        self,
        messages: list[TranscriptEntry],
        title: Optional[str] = None,
        combined_transcript_link: Optional[str] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
    ) -> str:
        """Generate Markdown from transcript messages."""
        from ..git_remote import canonical_cwd_from_messages, render_with_repo_context

        # SHA → commit-URL linkifier (issue #156); see HtmlRenderer.generate.
        repo_cwd = canonical_cwd_from_messages(messages)
        with render_with_repo_context(repo_cwd):
            return self._generate_inner(
                messages,
                title=title,
                combined_transcript_link=combined_transcript_link,
                output_dir=output_dir,
                session_tree=session_tree,
            )

    def _generate_inner(
        self,
        messages: list[TranscriptEntry],
        title: Optional[str] = None,
        combined_transcript_link: Optional[str] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
    ) -> str:
        """Body of ``generate`` running inside the SHA-resolver context."""
        self._output_dir = output_dir
        self._image_counter = 0
        self._last_heading_category: Optional[str] = None
        # Per-message timestamp date-on-change state (issue #160).
        # Reset each generate() so reusing a renderer across pages /
        # files doesn't leak the previous run's date.
        self._last_date_seen = None

        if not title:
            title = "Claude Transcript"

        # Get root messages (tree), session navigation, and rendering context
        root_messages, session_nav, ctx = generate_template_messages(
            messages,
            session_tree=session_tree,
            detail=self.detail,
            no_recaps=self.no_recaps,
        )
        self._ctx = ctx
        self._teammate_colors_by_session = {
            sid: dict(colors) for sid, colors in ctx.teammate_colors.items()
        }

        parts = [f"<!-- Generated by claude-code-log v{get_library_version()} -->", ""]
        parts.append(f"# {title}")

        # Table of Contents
        if session_nav:
            parts.append(self._generate_toc(session_nav))

        # Back link
        if combined_transcript_link:
            parts.append(f"[← Back to combined transcript]({combined_transcript_link})")
            parts.append("")

        # Render message tree
        for root in root_messages:
            if rendered := self._render_message(root, level=1):
                parts.append(rendered)

        return "\n\n".join(parts)

    def generate_session(
        self,
        messages: list[TranscriptEntry],
        session_id: str,
        title: Optional[str] = None,
        cache_manager: Optional["CacheManager"] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
        suppress_combined_link: bool = False,
    ) -> str:
        """Generate Markdown for a single session."""
        # Include subagent entries whose sessionId was rewritten to
        # ``{session_id}#agent-{agent_id}`` by ``_integrate_agent_entries``;
        # otherwise per-session exports drop the inlined subagent
        # conversation entirely (CodeRabbit on PR #125).
        agent_prefix = f"{session_id}#agent-"
        session_messages = [
            msg
            for msg in messages
            if msg.sessionId == session_id
            or (msg.sessionId or "").startswith(agent_prefix)
        ]
        # Back-link points at the same variant's combined file so users
        # don't bounce between detail levels when navigating. Suppressed
        # under `--combined no` where the combined file is never written.
        if cache_manager is not None and not suppress_combined_link:
            from ..utils import variant_suffix as _variant_suffix

            suffix = _variant_suffix(
                self.detail, self.compact, "md", self.no_timestamps, self.no_recaps
            )
            combined_link = f"combined_transcripts{suffix}.md"
        else:
            combined_link = None
        return self.generate(
            session_messages,
            title or f"Session {session_id[:8]}",
            combined_transcript_link=combined_link,
            output_dir=output_dir,
            session_tree=session_tree,
        )

    def generate_projects_index(
        self,
        project_summaries: list[dict[str, Any]],
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        expand_paths_tree: bool = False,
    ) -> str:
        """Generate a Markdown projects index page.

        Args:
            project_summaries: Per-project summary dicts.
            from_date / to_date: Date-filter labels for the title.
            expand_paths_tree: When True (Obsidian mode — `--expand-paths`),
                render the index as a nested bullet-list directory tree
                derived from each project's `html_file` (or per-session
                `file`) path. Each directory level becomes a bullet,
                with sessions as nested leaf bullets. Falls back to the
                flat per-project layout otherwise.
        """
        title = title_for_projects_index(project_summaries, from_date, to_date)
        template_projects, template_summary = prepare_projects_index(project_summaries)

        parts = [f"<!-- Generated by claude-code-log v{get_library_version()} -->", ""]
        parts.append(f"# {title}")

        # Summary stats
        parts.append(
            f"**Total:** {template_summary.total_projects} projects, "
            f"{template_summary.total_jsonl} sessions, "
            f"{template_summary.total_messages} messages"
        )
        parts.append("")

        # `--expand-paths` Obsidian mode: render the index as a nested
        # bullet-list mirroring the projected directory tree. Each path
        # component becomes a bullet; per-session files are the leaves.
        # The flat per-project listing remains the default for HTML and
        # for non-expand-paths Markdown.
        if expand_paths_tree:
            tree_lines = _render_expand_paths_tree(template_projects)
            if tree_lines:
                parts.extend(tree_lines)
                return "\n".join(parts)

        # Project list
        for project in template_projects:
            if project.combined_suppressed:
                # `--combined no` mode: header is a plain heading (no
                # link to the non-existent combined file); per-session
                # bullets link directly to `session-{id}.md` files.
                parts.append(f"## {project.display_name}")
            else:
                # Derive markdown link from html_file path
                md_link = project.html_file.replace(".html", ".md")
                parts.append(f"## [{project.display_name}]({md_link})")
            # Use actual session count (filtered) like HTML does
            session_count = (
                len(project.sessions) if project.sessions else project.jsonl_count
            )
            parts.append(f"- Sessions: {session_count}")
            parts.append(f"- Messages: {project.message_count}")
            if project.formatted_time_range:
                parts.append(f"- Date range: {project.formatted_time_range}")
            # Per-session bullet links (only when combined is
            # suppressed — otherwise the combined-link header already
            # serves as the project's single entry point).
            if project.combined_suppressed and project.sessions:
                parts.append("")
                parts.append("### Sessions")
                for session in project.sessions:
                    file_link = session.get("file")
                    if not file_link:
                        continue
                    # Derive label: prefer summary / title, fall back
                    # to the short session-id prefix.
                    summary = session.get("summary")
                    short_id = (session.get("id") or "")[:8]
                    label = summary if summary else f"session {short_id}"
                    timestamp_range = session.get("timestamp_range") or ""
                    timestamp_suffix = (
                        f" — *{timestamp_range}*" if timestamp_range else ""
                    )
                    parts.append(f"- [{label}]({file_link}){timestamp_suffix}")
            parts.append("")

        return "\n".join(parts)

    def is_outdated(self, file_path: Path) -> bool:
        """Check if a Markdown file is outdated based on version comment."""
        # is_file() (not exists()) so a non-regular destination — e.g.
        # /dev/stdout, which is a pipe when stdout is piped — is treated as
        # outdated rather than opened read-only, which would deadlock the
        # version sniff on readline() (issue #223).
        if not file_path.is_file():
            return True
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    if "<!-- Generated by claude-code-log v" in line:
                        start = line.find("v") + 1
                        end = line.find(" -->")
                        if start > 0 and end > start:
                            return line[start:end] != get_library_version()
        except (OSError, UnicodeDecodeError):
            pass
        return True

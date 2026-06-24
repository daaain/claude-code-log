#!/usr/bin/env python3
"""Utility functions for message filtering and processing."""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import (
    ContentItem,
    DetailLevel,
    TextContent,
    TranscriptEntry,
    UserTranscriptEntry,
)
from .factories import (
    IDE_DIAGNOSTICS_PATTERN,
    IDE_OPENED_FILE_PATTERN,
    IDE_SELECTION_PATTERN,
    is_command_message,
    is_local_command_output,
    is_system_message,
    simplify_command_tags,
)


# Per-level output file naming
#
# Variants of the same project render to distinct filenames so each
# variant has its own on-disk artifact and its own cache row. Examples:
#
#   --detail full                          → combined_transcripts.html
#   --detail low                           → combined_transcripts.low.html
#   --detail low --compact (md only)       → combined_transcripts.low.compact.md
#   --compact (md only)                    → combined_transcripts.compact.md
#
# Pagination composes after the variant suffix:
#   combined_transcripts.low_2.html        (detail=low, page 2)
#
# `_compact` only participates in the suffix for Markdown output — HTML
# rendering ignores the flag, so `--compact --format html` is a silent
# no-op on the filename (matching the CLI description that compact is
# Markdown-only).

VARIANT_ENTRY_RE = re.compile(r"^combined_transcripts((?:\.[a-z-]+)*)\.html$")


def variant_suffix(
    detail: DetailLevel | str = DetailLevel.FULL,
    compact: bool = False,
    format: str = "html",
    no_timestamps: bool = False,
    no_recaps: bool = False,
) -> str:
    """Compute the filename infix for a given render variant.

    Returns the empty string for the default variant
    (full detail, no compact). Otherwise returns a dot-prefixed
    suffix that is inserted after the basename and before the page
    number / extension.
    """
    # `DetailLevel` inherits from `str`, so `isinstance(detail, str)` is
    # always True — narrow only on `DetailLevel` to coerce plain strings.
    if not isinstance(detail, DetailLevel):
        detail = DetailLevel(detail)
    parts: list[str] = []
    if detail != DetailLevel.FULL:
        parts.append(detail.value)
    # `--no-recaps` filters *messages* out of the rendered tree, so it
    # affects EVERY format (html/md/json) — unlike compact/no-timestamps
    # below. It must earn a suffix slot regardless of format, else a
    # `--no-recaps` export collides with the plain one on filename + cache
    # key and the path-existence/cache check serves the stale variant
    # (same class as the #165 no-timestamps finding; #179).
    if no_recaps:
        parts.append("no-recaps")
    # `--compact` and `--no-timestamps` are Markdown-only (merges of
    # same-category headings / suppression of per-message timestamp
    # lines). They are silent no-ops for HTML, so they don't earn a
    # suffix slot under non-markdown output.
    is_markdown = format in ("md", "markdown")
    if compact and is_markdown:
        parts.append("compact")
    # `no_timestamps` participates in the suffix so toggling the flag
    # produces a distinct filename and the cache/path-existence check
    # doesn't treat the prior export as up-to-date (CR finding on #165).
    if no_timestamps and is_markdown:
        parts.append("no-timestamps")
    return "".join(f".{p}" for p in parts)


def format_timestamp(timestamp_str: str | None) -> str:
    """Format ISO timestamp for display, converting to UTC."""
    if timestamp_str is None:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        # Convert to UTC if timezone-aware
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return timestamp_str


def format_timestamp_range(first_timestamp: str, last_timestamp: str) -> str:
    """Format timestamp range for display.

    Args:
        first_timestamp: ISO timestamp for range start
        last_timestamp: ISO timestamp for range end

    Returns:
        Formatted string like "2025-01-01 10:00:00 - 2025-01-01 11:00:00"
        or single timestamp if both are equal, or empty string if neither provided.
    """
    if first_timestamp and last_timestamp:
        if first_timestamp == last_timestamp:
            return format_timestamp(first_timestamp)
        else:
            return f"{format_timestamp(first_timestamp)} - {format_timestamp(last_timestamp)}"
    elif first_timestamp:
        return format_timestamp(first_timestamp)
    else:
        return ""


def _is_temp_path(path_str: str) -> bool:
    """Check if a path is a temporary/test path that should be filtered out."""
    temp_patterns = [
        "/private/var/folders/",  # macOS temp
        "/tmp/",  # Unix temp
        "/var/folders/",  # macOS temp (alternate)
    ]
    return any(pattern in path_str for pattern in temp_patterns)


def get_project_display_name(
    project_dir_name: str, working_directories: Optional[list[str]] = None
) -> str:
    """Get the display name for a project based on working directories.

    Args:
        project_dir_name: The Claude project directory name (e.g., "-Users-dain-workspace-claude-code-log")
        working_directories: List of working directories from cache data

    Returns:
        The project display name (e.g., "claude-code-log")
    """
    if working_directories:
        # Filter out temporary paths (pytest, macOS temp dirs, etc.)
        real_dirs = [wd for wd in working_directories if not _is_temp_path(wd)]

        # If all directories were filtered out, fall back to project_dir_name conversion
        if not real_dirs:
            display_name = project_dir_name
            if display_name.startswith("-"):
                display_name = display_name[1:].replace("-", "/")
            return display_name

        # Convert to Path objects with their original indices for tracking recency
        paths_with_indices = [(Path(wd), i) for i, wd in enumerate(real_dirs)]

        # Sort by: 1) path depth (fewer parts = less nested), 2) recency (lower index = more recent)
        # This gives us the least nested path, with ties broken by recency
        best_path, _ = min(paths_with_indices, key=lambda p: (len(p[0].parts), p[1]))
        return best_path.name
    else:
        # Fall back to converting project directory name
        display_name = project_dir_name
        if display_name.startswith("-"):
            display_name = display_name[1:].replace("-", "/")
        return display_name


def path_looks_absolute(s: str) -> bool:
    """True if ``s`` looks like an absolute path on either POSIX or
    Windows. Decoupled from the host OS so JSONL-stored cwds don't
    silently mismatch when a Linux-recorded transcript is processed
    on Windows or vice versa (#151)."""
    if not s:
        return False
    from pathlib import PurePosixPath, PureWindowsPath

    return PurePosixPath(s).is_absolute() or PureWindowsPath(s).is_absolute()


def _split_real_path_for_join(real_path_str: str) -> list[str]:
    """Decompose a real-path string into the parts that should be
    joined under ``output_dir`` for ``--expand-paths``.

    Form-aware: POSIX-shaped strings (``/foo/bar``) yield
    ``['foo', 'bar']``; Windows-shaped strings (``C:\\foo\\bar``)
    yield ``['C', 'foo', 'bar']`` (drive letter kept as a path
    component, colon stripped). Relative inputs pass through as-is.

    Pure path-string inspection — no host-OS dependence; same JSONL
    cwd produces the same destination tree on Linux, macOS, or
    Windows.
    """
    from pathlib import PurePosixPath, PureWindowsPath

    p_posix = PurePosixPath(real_path_str)
    if p_posix.is_absolute():
        return list(p_posix.parts[1:])  # drop leading '/'
    p_win = PureWindowsPath(real_path_str)
    if p_win.is_absolute():
        # 'C:\foo\bar' → drive='C:', parts=('C:\\', 'foo', 'bar')
        # Keep the drive as a leading dirname segment, strip the colon
        # so it works as a real directory name on POSIX too.
        drive = p_win.drive.rstrip(":")
        rest = list(p_win.parts[1:])
        return [drive, *rest] if drive else rest
    # Relative — POSIX-style component split.
    return list(p_posix.parts)


def project_dir_to_real_path(
    project_dir: Path,
    cached_working_directories: Optional[list[str]] = None,
) -> Path:
    """Recover the real on-disk path for a Claude project directory.

    Claude Code encodes project paths flatly: ``/`` and leading ``.``
    both become ``-`` (e.g. ``/home/joe/.claude`` →
    ``-home-joe--claude``). The encoding is **lossy** — ``-home-joe-x-y``
    could mean either ``/home/joe/x/y`` or ``/home/joe/x-y``. The cache
    (and live JSONLs) preserve the original ``cwd`` so we can disambiguate
    without parsing the encoded name.

    Resolution strategy (issue #151):

    1. **Cache hit** — if ``cached_working_directories`` is non-empty,
       use its first entry. Authoritative — that's what Claude Code
       recorded at session time.
    2. **JSONL peek** — open the project's first JSONL, scan up to a
       handful of lines for the first entry with a ``cwd`` field,
       return that. Cheap (one ``json.loads`` per line, no model
       validation).
    3. **Naive last-resort** — strip the leading ``-`` and replace
       remaining ``-``s with ``/``. Best-effort only; collapses
       ambiguity in the lossy direction. Used when the project dir
       has been emptied (orphan archived dir) and no cache survives.

    Args:
        project_dir: The encoded project directory
            (e.g. ``~/.claude/projects/-home-joe-project-A``).
        cached_working_directories: Optional cached ``working_directories``
            list from the project's cache (``ProjectCache.working_directories``).

    Returns:
        The recovered real path. May be a best-effort guess in the
        last-resort case.
    """
    # Tier 1: cache. Only accept absolute paths — relative or oddly
    # shaped values fall through (e.g. test fixtures with synthetic
    # `cwd` entries). Absoluteness check is form-aware (POSIX or
    # Windows shapes), so a Linux-recorded cwd processed on Windows
    # still resolves through this tier.
    if cached_working_directories:
        real_dirs = [
            wd
            for wd in cached_working_directories
            if not _is_temp_path(wd) and path_looks_absolute(wd)
        ]
        if real_dirs:
            return Path(real_dirs[0])

    # Tier 2: peek the first JSONL for a `cwd` field. Same
    # form-aware absoluteness guard as tier 1.
    if project_dir.is_dir():
        # Skip agent-* sidechain files; they may not carry the
        # top-level project cwd. Take any other JSONL.
        for jsonl_path in sorted(project_dir.glob("*.jsonl")):
            if jsonl_path.name.startswith("agent-"):
                continue
            cwd_from_peek = _peek_jsonl_for_cwd(jsonl_path)
            if cwd_from_peek and path_looks_absolute(cwd_from_peek):
                return Path(cwd_from_peek)
            # First non-agent JSONL exhausted with no usable cwd —
            # bail out rather than scanning every file.
            break

    # Tier 3: naive last-resort. Recovers leading-dot dir components
    # via `--` → `/.` mapping (Claude Code encodes `/.foo` as `--foo`).
    # Remaining ambiguity (`/foo-bar` vs `/foo/bar`) collapses toward
    # the more-segments interpretation; documented as best-effort.
    name = project_dir.name
    if name.startswith("-"):
        body = name[1:].replace("--", "/.").replace("-", "/")
        return Path("/" + body)
    return Path(name.replace("--", "/.").replace("-", "/"))


# Maximum number of lines we read from a project's first JSONL when
# trying to recover the project's `cwd`. Real-world JSONLs put `cwd`
# on the very first user/assistant entry, so 32 is generous.
_PEEK_JSONL_MAX_LINES = 32


def _peek_jsonl_for_cwd(jsonl_path: Path) -> Optional[str]:
    """Return the first non-empty ``cwd`` value found in the JSONL,
    or ``None`` if none is found within the peek window."""
    import json
    from typing import cast

    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(_PEEK_JSONL_MAX_LINES):
                line = fh.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    entry: object = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                # `json.loads` produces Unknown-typed values; cast to
                # a concrete shape for pyright. Runtime is unaffected.
                cwd = cast("dict[str, object]", entry).get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


# Recognised output format suffixes for the `--output` dir-vs-file
# heuristic and for format inference. If a user passes
# ``--output /tmp/out.md`` we treat it as a file (and, when ``-f`` is
# omitted, infer markdown); ``--output /tmp/obsidian/`` is a directory.
# ``.md`` / ``.markdown`` both map to the canonical ``markdown`` format.
_SUFFIX_TO_FORMAT: dict[str, str] = {
    ".html": "html",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
}
_OUTPUT_FILE_SUFFIXES = frozenset(_SUFFIX_TO_FORMAT)


def output_path_is_file(output: Path) -> bool:
    """Heuristic for ``--output`` interpretation (issue #151).

    A path is a *file* destination when its suffix is one of the
    recognised output-format extensions; otherwise it's a *directory*
    destination. Doesn't touch the filesystem — pure path-string
    inspection.
    """
    return output.suffix.lower() in _OUTPUT_FILE_SUFFIXES


def format_from_output_suffix(output: Path) -> Optional[str]:
    """Canonical output format implied by an ``--output`` file suffix.

    Returns ``"html"`` / ``"markdown"`` / ``"json"`` for a recognised
    suffix, or ``None`` otherwise (issue #222). ``.md`` and ``.markdown``
    both map to ``"markdown"``.
    """
    return _SUFFIX_TO_FORMAT.get(output.suffix.lower())


def project_destination(
    project_dir: Path,
    *,
    output_dir: Optional[Path],
    expand_paths: bool,
    filter_path: Optional[str],
    cached_working_directories: Optional[list[str]] = None,
) -> Optional[Path]:
    """Compute the per-project output destination directory (issue #151).

    Implements the flag interaction matrix from
    ``work/obsidian-friendly-output.md``. Pure function — no I/O beyond
    what ``project_dir_to_real_path`` may do (cache or one JSONL peek).

    Args:
        project_dir: The source project directory under
            ``~/.claude/projects/`` (e.g. ``-home-joe-project-A``).
        output_dir: Target root, or None for legacy in-place behaviour.
        expand_paths: When True, project's flat name is expanded back
            to its real on-disk path under ``output_dir``.
        filter_path: When set, restrict to projects whose path
            (real path if ``expand_paths``, else flat dir name)
            starts with the prefix. With ``expand_paths``, the
            matched prefix is also truncated from the destination.
        cached_working_directories: Optional cached working dirs for
            ``project_dir_to_real_path``.

    Returns:
        Destination directory, or ``None`` if the project should be
        skipped (filter excluded it).
    """
    # Legacy: no --output → write into the source dir (current behaviour).
    if output_dir is None:
        return project_dir

    # With --expand-paths: resolve the real path and (optionally) trim
    # the filter prefix. Form-aware throughout — POSIX and Windows
    # path strings are handled symmetrically so a transcript recorded
    # on one platform projects predictably on the other.
    if expand_paths:
        from pathlib import PurePosixPath, PureWindowsPath

        real_path = project_dir_to_real_path(project_dir, cached_working_directories)
        # `as_posix()` preserves the original form across platforms:
        # POSIX-form paths stay `/home/...`, Windows-form paths stay
        # `C:/Users/...`. The bare `str()` would convert `/home/joe`
        # to `\home\joe` on Windows, which then mismatches our
        # form-aware detection and joins to drive root.
        real_str = real_path.as_posix()
        if filter_path:
            # Match using the same path-shape family as the real path
            # (POSIX-form `/home/joe` filters POSIX-form real paths;
            # Windows-form `C:\Users\joe` filters Windows-form real
            # paths). Mixing forms is a user error and produces None.
            if PurePosixPath(real_str).is_absolute():
                pp_cls = PurePosixPath
            elif PureWindowsPath(real_str).is_absolute():
                pp_cls = PureWindowsPath
            else:
                pp_cls = PurePosixPath
            try:
                rel = pp_cls(real_str).relative_to(pp_cls(filter_path))
            except ValueError:
                # Real path is not under filter prefix — skip.
                return None
            return output_dir.joinpath(*rel.parts)
        # Real-path tree directly under output_dir. Decompose the
        # path string in a form-aware way: POSIX shapes drop the
        # leading '/', Windows shapes keep the drive letter as a
        # leading path component (so `C:\foo\bar` lands at
        # `<output>/C/foo/bar`).
        rel_parts = _split_real_path_for_join(real_str)
        return output_dir.joinpath(*rel_parts) if rel_parts else output_dir

    # No --expand-paths: filter against the flat dir name (per Q2),
    # destination keeps the flat name. Require an exact match OR a
    # `-`-terminated prefix so `--filter-path -home-joe` doesn't also
    # accept sibling-prefix names like `-home-joe-bar` style
    # (matches) but reject `-home-joet-...` (would over-match without
    # the boundary).
    if filter_path:
        name = project_dir.name
        if name != filter_path and not name.startswith(filter_path + "-"):
            return None
    return output_dir / project_dir.name


def should_skip_message(text_content: str) -> bool:
    """
    Determine if a message should be skipped in transcript rendering.

    This is the centralized logic for filtering out unwanted messages.
    """
    is_system = is_system_message(text_content)
    is_command = is_command_message(text_content)
    is_output = is_local_command_output(text_content)

    # Skip system messages that are not command messages AND not local command output
    return is_system and not is_command and not is_output


def should_use_as_session_starter(text_content: str) -> bool:
    """
    Determine if a user message should be used as a session starter preview.

    This filters out system messages, warmup messages, and most command messages,
    except for 'init' commands which are typically the start of a new session.
    """
    # Skip warmup messages
    if text_content.strip() == "Warmup":
        return False

    # Skip system messages
    if is_system_message(text_content):
        return False

    # Skip command messages except for 'init' commands
    if "<command-name>" in text_content:
        return "<command-name>init" in text_content

    return True


# Constants
FIRST_USER_MESSAGE_PREVIEW_LENGTH = 1000


def create_session_preview(text_content: str) -> str:
    """Create a truncated preview of first user message for session display.

    Args:
        text_content: The raw text content from the first user message

    Returns:
        A preview string, truncated to FIRST_USER_MESSAGE_PREVIEW_LENGTH with
        ellipsis if needed, with command-tag XML stripped to its semantic
        core (``/X``-style slash commands and ``<local-command-stdout>``
        dialog hints), and IDE tags replaced with compact emoji
        indicators.

    The ``simplify_command_tags`` cleanup serves two distinct callers
    that ended up wanting the same shape: branch headers (whose first
    user entry is often a ``/exit`` slash command, formerly displayed
    as the raw ``<command-name>/exit</command-name>...`` soup) and the
    occasional session preview that does start with a slash command
    (``init`` historically had a hardcoded English description here;
    ``simplify_command_tags`` collapses it to ``/init`` instead — bare
    legacy emissions are normalised to the ``/cmd`` shape so previews
    stay consistent in mixed transcripts). #129.
    """
    # Strip command-tag XML soup down to ``/cmd`` or inner-text shape.
    preview_content = simplify_command_tags(text_content)

    # Apply compact IDE tag indicators BEFORE truncation
    preview_content = _compact_ide_tags_for_preview(preview_content)

    # Then truncate if needed
    if len(preview_content) > FIRST_USER_MESSAGE_PREVIEW_LENGTH:
        return preview_content[:FIRST_USER_MESSAGE_PREVIEW_LENGTH] + "..."
    return preview_content


def extract_text_content_length(content: list[ContentItem]) -> int:
    """Get the length of text content for quick checks without full extraction."""
    total_length = 0
    for item in content:
        # Only count TextContent items, skip tool/thinking/image items
        if isinstance(item, TextContent):
            total_length += len(item.text.strip())
    return total_length


# IDE tag patterns imported from factories for compact preview rendering


def _compact_ide_tags_for_preview(text_content: str) -> str:
    """Replace verbose IDE/system tags with compact emoji indicators for previews.

    Only processes tags at the START of the content (where VS Code places them).
    Tags appearing later in the text (e.g., inside quoted JSONL) are left unchanged.

    Transforms:
    - <ide_opened_file>...path/to/file...</ide_opened_file> -> 📎 /path/to/file
    - <ide_selection>...path/to/file...</ide_selection> -> ✂️ /path/to/file
    - <ide_diagnostics>...</ide_diagnostics> -> 🩺 diagnostics
    - <bash-input>command</bash-input> -> 💻 command

    Args:
        text_content: Raw text content that may contain IDE/system tags

    Returns:
        Text with leading tags replaced by compact indicators
    """

    def _extract_file_path(content: str) -> str | None:
        """Extract file path from IDE tag content."""
        # Try to find an absolute path (starts with /)
        # Stop at: whitespace, colon followed by newline, or "in the IDE"
        path_match = re.search(
            r"(/[^\s:]+(?:\.[^\s:]+)?)(?::\s|\s+in\s+the\s+IDE|\s*$|\s)", content
        )
        if path_match:
            return path_match.group(1).rstrip(".:")

        # Fallback: look for "file" or "from" followed by a path
        path_match = re.search(r"(?:file|from)\s+(/[^\s:]+)", content)
        if path_match:
            return path_match.group(1).rstrip(".:")

        return None

    # Process only LEADING IDE tags - stop when we hit non-IDE content
    # This prevents replacing tags inside quoted strings/JSONL content
    # Uses shared patterns from parser.py for consistency
    compact_parts: list[str] = []
    remaining = text_content

    # Compiled pattern for bash-input (not in parser.py as it's preview-specific)
    bash_input_pattern = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)

    while remaining:
        # Strip leading whitespace for matching
        stripped = remaining.lstrip()

        # Try to match each IDE tag type at the start of stripped text
        # Check for <ide_opened_file> at start (using shared pattern)
        match = IDE_OPENED_FILE_PATTERN.match(stripped)
        if match:
            content = match.group(1).strip()
            filepath = _extract_file_path(content)
            compact_parts.append(f"📎 {filepath}" if filepath else "📎 file")
            remaining = stripped[match.end() :]
            continue

        # Check for <ide_selection> at start (using shared pattern)
        match = IDE_SELECTION_PATTERN.match(stripped)
        if match:
            content = match.group(1).strip()
            filepath = _extract_file_path(content)
            compact_parts.append(f"✂️ {filepath}" if filepath else "✂️ selection")
            remaining = stripped[match.end() :]
            continue

        # Check for <post-tool-use-hook><ide_diagnostics>... (using shared pattern)
        match = IDE_DIAGNOSTICS_PATTERN.match(stripped)
        if match:
            compact_parts.append("🩺 diagnostics")
            remaining = stripped[match.end() :]
            continue

        # Check for <bash-input>command</bash-input> at start
        match = bash_input_pattern.match(stripped)
        if match:
            command = match.group(1).strip()
            # Truncate very long commands
            if len(command) > 50:
                command = command[:47] + "..."
            compact_parts.append(f"💻 {command}")
            remaining = stripped[match.end() :]
            continue

        # No more tags at start - stop processing
        break

    # Combine compact indicators with remaining content
    if compact_parts:
        # Add newline between indicators and content if there's remaining text
        prefix = "\n".join(compact_parts)
        if remaining.strip():
            return f"{prefix}\n{remaining.lstrip()}"
        return prefix

    return text_content


def is_agent_session(session_id: str) -> bool:
    """Check if a session ID is a synthetic agent session.

    Agent sessions use the format ``{sessionId}#agent-{agentId}``,
    assigned by ``_integrate_agent_entries()`` during DAG construction.
    """
    return "#agent-" in session_id


def get_parent_session_id(session_id: str) -> str:
    """Return the parent session ID for an agent session, or the ID itself.

    For ``{sessionId}#agent-{agentId}`` returns ``{sessionId}``.
    For non-agent sessions returns the input unchanged.
    """
    return session_id.split("#agent-")[0] if "#agent-" in session_id else session_id


def get_warmup_session_ids(messages: list[TranscriptEntry]) -> set[str]:
    """Get set of session IDs that are warmup-only sessions.

    Pre-computes warmup status for all sessions for efficiency (O(n) once,
    then O(1) lookup per session).

    Args:
        messages: List of all transcript entries

    Returns:
        Set of session IDs that contain only warmup messages
    """
    from .parser import extract_text_content

    # Group user message text by session
    session_user_messages: dict[str, list[str]] = {}

    for message in messages:
        if isinstance(message, UserTranscriptEntry) and hasattr(message, "message"):
            session_id = getattr(message, "sessionId", "")
            if session_id:
                text_content = extract_text_content(message.message.content).strip()
                if session_id not in session_user_messages:
                    session_user_messages[session_id] = []
                session_user_messages[session_id].append(text_content)

    # Find sessions where ALL user messages are "Warmup"
    warmup_sessions: set[str] = set()
    for session_id, user_msgs in session_user_messages.items():
        if user_msgs and all(msg == "Warmup" for msg in user_msgs):
            warmup_sessions.add(session_id)

    return warmup_sessions


def strip_error_tags(text: str) -> str:
    """Strip <tool_use_error>...</tool_use_error> tags, keeping content.

    Claude Code uses these XML-style tags to wrap error messages in tool results.
    This function strips the tags while preserving the error message content.

    Args:
        text: Text that may contain tool_use_error tags

    Returns:
        Text with error tags removed but content preserved
    """
    return re.sub(
        r"<tool_use_error>(.*?)</tool_use_error>",
        r"\1",
        text,
        flags=re.DOTALL,
    )


def generate_unified_diff(old_string: str, new_string: str) -> str:
    """Generate a unified diff between old and new strings.

    Args:
        old_string: The original content
        new_string: The modified content

    Returns:
        Unified diff as a string (without header lines)
    """
    import difflib

    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)

    # Ensure last lines end with newline for proper diff output
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"

    diff_lines = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=3))

    # Skip the header lines (--- and +++) if present
    if len(diff_lines) >= 2 and diff_lines[0].startswith("---"):
        diff_lines = diff_lines[2:]

    return "".join(diff_lines).rstrip("\n")

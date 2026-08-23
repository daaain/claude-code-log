#!/usr/bin/env python3
"""Code rendering utilities for syntax highlighting and diffs.

This module provides utilities for rendering source code with syntax highlighting
(using Pygments) and rendering diffs with intra-line highlighting.
"""

import difflib
import fnmatch
import html
import os
import re
from typing import Callable, Optional

from pygments import highlight
from pygments.lexer import Lexer
from pygments.lexers import TextLexer, get_lexer_by_name, get_all_lexers
from pygments.formatter import Formatter
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound

from ..render_cache import pygments_cache
from ..renderer_timings import timing_stat


def _escape_html(text: str) -> str:
    """Escape HTML special characters in text.

    Also normalizes line endings (CRLF -> LF) to prevent double spacing in <pre> blocks.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(normalized)


# Cache for Pygments lexer pattern matching
_pattern_cache: Optional[dict[str, str]] = None
_extension_cache: Optional[dict[str, str]] = None


def _init_lexer_caches() -> tuple[dict[str, str], dict[str, str]]:
    """Initialize lexer pattern and extension caches.

    Returns:
        Tuple of (pattern_cache, extension_cache)
    """
    global _pattern_cache, _extension_cache

    if _pattern_cache is not None and _extension_cache is not None:
        return _pattern_cache, _extension_cache

    pattern_cache: dict[str, str] = {}
    extension_cache: dict[str, str] = {}

    # Use public API: get_all_lexers() returns (name, aliases, patterns, mimetypes) tuples
    for _name, aliases, patterns, _mimetypes in get_all_lexers():
        if aliases and patterns:
            # Use first alias as the lexer name
            lexer_alias = aliases[0]
            # Map each filename pattern to this lexer alias
            for pattern in patterns:
                pattern_lower = pattern.lower()
                pattern_cache[pattern_lower] = lexer_alias
                # Extract simple extension patterns (*.ext) for fast lookup
                if (
                    pattern_lower.startswith("*.")
                    and "*" not in pattern_lower[2:]
                    and "?" not in pattern_lower[2:]
                ):
                    ext = pattern_lower[2:]  # Remove "*."
                    # Prefer first match for each extension
                    if ext not in extension_cache:
                        extension_cache[ext] = lexer_alias

    _pattern_cache = pattern_cache
    _extension_cache = extension_cache
    return pattern_cache, extension_cache


def highlight_code_with_pygments(
    code: str, file_path: str, show_linenos: bool = True, linenostart: int = 1
) -> str:
    """Highlight code using Pygments with appropriate lexer based on file path.

    Args:
        code: The source code to highlight
        file_path: Path to determine the appropriate lexer
        show_linenos: Whether to show line numbers (default: True)
        linenostart: Starting line number for display (default: 1)

    Returns:
        HTML string with syntax-highlighted code
    """
    # Memoized: highlighting is a pure function of these four arguments,
    # and the same content is highlighted repeatedly within a run — once
    # for the combined page and again for the session file, plus whenever
    # the same file is Read more than once. See ``render_cache``.
    memo_key = (code, file_path, show_linenos, linenostart)
    cached = pygments_cache.get(memo_key)
    if cached is not None:
        return cached

    # Get caches (initialized lazily)
    pattern_cache, extension_cache = _init_lexer_caches()

    # Get basename for matching (patterns are like "*.py")
    basename = os.path.basename(file_path).lower()

    # Default to plain text lexer
    lexer: Lexer = TextLexer()

    try:
        # OPTIMIZATION: Try fast extension lookup first (O(1) dict lookup)
        lexer_alias = None
        if "." in basename:
            ext = basename.split(".")[-1]  # Get last extension (handles .tar.gz, etc.)
            lexer_alias = extension_cache.get(ext)

        # Fall back to pattern matching only if extension lookup failed
        if lexer_alias is None:
            for pattern, lex_alias in pattern_cache.items():
                if fnmatch.fnmatch(basename, pattern):
                    lexer_alias = lex_alias
                    break

        # Get lexer based on file extension
        # Note: stripall=False preserves leading whitespace (important for code indentation)
        if lexer_alias:
            lexer = get_lexer_by_name(lexer_alias, stripall=False)
    except ClassNotFound:
        # Fall back to plain text lexer (already set as default)
        pass

    # Create formatter with line numbers in table format
    formatter: Formatter = HtmlFormatter(
        linenos="table" if show_linenos else False,
        cssclass="highlight",
        wrapcode=True,
        linenostart=linenostart,
    )

    # Highlight the code with timing if enabled
    with timing_stat("_pygments_timings"):
        highlighted = str(highlight(code, lexer, formatter))
    pygments_cache.put(memo_key, highlighted)
    return highlighted


def truncate_highlighted_preview(highlighted_html: str, max_lines: int) -> str:
    """Truncate Pygments highlighted HTML to first N lines.

    HtmlFormatter(linenos="table") produces a single <tr> with two <td>s:
      <td class="linenos"><div class="linenodiv"><pre>LINE_NUMS</pre></div></td>
      <td class="code"><div><pre>CODE</pre></div></td>

    We truncate content within each <pre> tag to the first max_lines lines.

    Args:
        highlighted_html: Full Pygments-highlighted HTML
        max_lines: Maximum number of lines to include in preview

    Returns:
        Truncated HTML with same structure but fewer lines
    """

    def truncate_pre_content(match: re.Match[str]) -> str:
        """Truncate content inside a <pre> tag to max_lines."""
        prefix, content, suffix = match.groups()
        lines = content.split("\n")
        truncated = "\n".join(lines[:max_lines])
        return prefix + truncated + suffix

    # Truncate linenos <pre> content (line numbers separated by newlines)
    result = re.sub(
        r'(<div class="linenodiv"><pre>)(.*?)(</pre></div>)',
        truncate_pre_content,
        highlighted_html,
        flags=re.DOTALL,
    )

    # Truncate code <pre> content
    result = re.sub(
        r'(<td class="code"><div><pre[^>]*>)(.*?)(</pre></div></td>)',
        truncate_pre_content,
        result,
        flags=re.DOTALL,
    )

    return result


def render_line_diff(
    old_line: str, new_line: str, escape_fn: Optional[Callable[[str], str]] = None
) -> str:
    """Render a pair of changed lines with character-level highlighting.

    Args:
        old_line: The original line
        new_line: The new line
        escape_fn: Optional HTML escape function (defaults to internal _escape_html)

    Returns:
        HTML string with both lines and character-level diff highlighting
    """
    if escape_fn is None:
        escape_fn = _escape_html

    # Use SequenceMatcher for character-level diff
    sm = difflib.SequenceMatcher(None, old_line.rstrip("\n"), new_line.rstrip("\n"))

    # Build old line with highlighting
    old_parts: list[str] = []
    old_parts.append(
        "<div class='diff-line diff-removed'><span class='diff-marker'>-</span>"
    )
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk = old_line[i1:i2]
        if tag == "equal":
            old_parts.append(escape_fn(chunk))
        elif tag in ("delete", "replace"):
            old_parts.append(
                f"<mark class='diff-char-removed'>{escape_fn(chunk)}</mark>"
            )
    old_parts.append("</div>")

    # Build new line with highlighting
    new_parts: list[str] = []
    new_parts.append(
        "<div class='diff-line diff-added'><span class='diff-marker'>+</span>"
    )
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk = new_line[j1:j2]
        if tag == "equal":
            new_parts.append(escape_fn(chunk))
        elif tag in ("insert", "replace"):
            new_parts.append(f"<mark class='diff-char-added'>{escape_fn(chunk)}</mark>")
    new_parts.append("</div>")

    return "".join(old_parts) + "".join(new_parts)


def render_single_diff(
    old_string: str, new_string: str, escape_fn: Optional[Callable[[str], str]] = None
) -> str:
    """Render a single diff between old_string and new_string.

    Args:
        old_string: The original content
        new_string: The new content
        escape_fn: Optional HTML escape function (defaults to internal _escape_html)

    Returns:
        HTML string with diff view and intra-line highlighting
    """
    if escape_fn is None:
        escape_fn = _escape_html

    # Split into lines for diff
    old_lines = old_string.splitlines(keepends=True)
    new_lines = new_string.splitlines(keepends=True)

    # Generate unified diff to identify changed lines
    differ = difflib.Differ()
    diff: list[str] = list(differ.compare(old_lines, new_lines))

    html_parts = ["<div class='edit-diff'>"]

    i = 0
    while i < len(diff):
        line = diff[i]
        prefix = line[0:2]
        content = line[2:]

        if prefix == "- ":
            # Removed line - look ahead for corresponding addition
            removed_lines: list[str] = [content]
            j = i + 1

            # Collect consecutive removed lines
            while j < len(diff) and diff[j].startswith("- "):
                removed_lines.append(diff[j][2:])
                j += 1

            # Skip '? ' hint lines
            while j < len(diff) and diff[j].startswith("? "):
                j += 1

            # Collect consecutive added lines
            added_lines: list[str] = []
            while j < len(diff) and diff[j].startswith("+ "):
                added_lines.append(diff[j][2:])
                j += 1

            # Skip '? ' hint lines
            while j < len(diff) and diff[j].startswith("? "):
                j += 1

            # Generate character-level diff for paired lines
            if added_lines:
                for old_line, new_line in zip(removed_lines, added_lines):
                    html_parts.append(render_line_diff(old_line, new_line, escape_fn))

                # Handle any unpaired lines
                for old_line in removed_lines[len(added_lines) :]:
                    escaped = escape_fn(old_line.rstrip("\n"))
                    html_parts.append(
                        f"<div class='diff-line diff-removed'><span class='diff-marker'>-</span>{escaped}</div>"
                    )

                for new_line in added_lines[len(removed_lines) :]:
                    escaped = escape_fn(new_line.rstrip("\n"))
                    html_parts.append(
                        f"<div class='diff-line diff-added'><span class='diff-marker'>+</span>{escaped}</div>"
                    )
            else:
                # No corresponding addition - just removed
                for old_line in removed_lines:
                    escaped = escape_fn(old_line.rstrip("\n"))
                    html_parts.append(
                        f"<div class='diff-line diff-removed'><span class='diff-marker'>-</span>{escaped}</div>"
                    )

            i = j

        elif prefix == "+ ":
            # Added line without corresponding removal
            escaped = escape_fn(content.rstrip("\n"))
            html_parts.append(
                f"<div class='diff-line diff-added'><span class='diff-marker'>+</span>{escaped}</div>"
            )
            i += 1

        elif prefix == "? ":
            # Skip hint lines (already processed)
            i += 1

        else:
            # Unchanged line - show for context
            escaped = escape_fn(content.rstrip("\n"))
            html_parts.append(
                f"<div class='diff-line diff-context'><span class='diff-marker'> </span>{escaped}</div>"
            )
            i += 1

    html_parts.append("</div>")
    return "".join(html_parts)

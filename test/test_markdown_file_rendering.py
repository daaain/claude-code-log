"""Render Read/Write of any Markdown file as Markdown, not Pygments (issue #232).

A follow-up to the auto-memory work (#192): a fully-contained Markdown file
should render the usual way (rendered Markdown) instead of syntax-highlighted
source. This applies to every ``.md`` file, not just memory files.

- **Write** always carries the whole file → always rendered as Markdown.
- **Read** renders as Markdown only for a *full* read; a partial slice
  (offset/limit) can split a code fence, so partial reads keep Pygments.
- Memory keeps its extra specialization (🧠 title, relative-link resolution,
  and always-Markdown body even when partial) — see test_memory_rendering.py.
"""

import re
from typing import Optional

from claude_code_log.html.tool_formatters import (
    format_read_output,
    format_write_input,
)
from claude_code_log.html.utils import is_markdown_path
from claude_code_log.models import ReadOutput, WriteInput


MD = "/home/u/proj/docs/guide.md"
MD_CAPS = "/home/u/proj/README.MARKDOWN"
PY = "/home/u/proj/src/app.py"
MEM = "/home/u/.claude/projects/-home-u-proj/memory/MEMORY.md"

MD_BODY = "# Guide\n\nUse `just ci` before pushing.\n"


def _read(
    file_path: str, content: str, *, start_line: int = 1, total: Optional[int] = None
):
    n = len(content.splitlines())
    total = n if total is None else total
    return ReadOutput(
        file_path=file_path,
        content=content,
        start_line=start_line,
        num_lines=n,
        total_lines=total,
        is_truncated=n < total,
    )


# ----------------------------- is_markdown_path ------------------------------


class TestIsMarkdownPath:
    def test_md_and_markdown_extensions(self):
        assert is_markdown_path(MD)
        assert is_markdown_path("/x/y.markdown")

    def test_case_insensitive(self):
        assert is_markdown_path(MD_CAPS)
        assert is_markdown_path("/x/Notes.Md")

    def test_windows_separators(self):
        assert is_markdown_path(r"C:\Users\u\docs\guide.md")

    def test_memory_paths_are_markdown(self):
        # Memory files are a subset: is_memory_path ⊂ is_markdown_path.
        assert is_markdown_path(MEM)

    def test_non_markdown_and_none(self):
        assert not is_markdown_path(PY)
        assert not is_markdown_path("/x/data.json")
        assert not is_markdown_path("/x/MD")  # bare, no extension
        assert not is_markdown_path(None)
        assert not is_markdown_path("")


# ----------------------------- Read rendering --------------------------------
# The full/partial/truncated split (the ``_is_full_read`` predicate) is pinned
# behaviorally through ``format_read_output`` rather than by importing the
# private helper — keeps the test off the private symbol so it stays clean if
# ``test/`` ever joins the pyright include scope (cf. #216's _PARAMS_TABLE_MAX_
# DEPTH reportPrivateUsage).


class TestReadMarkdownRendering:
    def test_full_md_read_rendered_as_markdown(self):
        # Whole file from line 1, not truncated → full read → Markdown.
        html = format_read_output(_read(MD, MD_BODY))
        assert re.search(r'class="read-tool-result markdown"', html)
        assert re.search(r"<h1[^>]*>Guide", html)
        assert "<code>just ci</code>" in html

    def test_partial_md_read_keeps_pygments(self):
        # A slice (start_line > 1) could land mid-fence → keep highlighted source.
        html = format_read_output(_read(MD, MD_BODY, start_line=10, total=999))
        assert 'class="read-tool-result markdown"' not in html
        assert "<h1" not in html

    def test_truncated_md_read_keeps_pygments(self):
        # Starts at line 1 but truncated (num_lines < total_lines) → not full.
        html = format_read_output(_read(MD, MD_BODY, total=999))
        assert 'class="read-tool-result markdown"' not in html

    def test_non_markdown_read_keeps_pygments(self):
        html = format_read_output(_read(PY, "x = 1\n"))
        assert 'class="read-tool-result markdown"' not in html

    def test_markdown_body_escapes_raw_html(self):
        # File content is untrusted regardless of being a memory file.
        html = format_read_output(_read(MD, "# T\n\n<script>alert(1)</script>\n"))
        assert "&lt;script&gt;" in html
        assert "<script>alert(1)</script>" not in html

    def test_general_md_read_has_no_memory_link_resolution(self):
        # Relative links in a non-memory .md stay as-authored (no file:// rewrite).
        html = format_read_output(_read(MD, "[peer](peer.md)\n"))
        assert 'href="peer.md"' in html
        assert "file://" not in html


# ----------------------------- Write rendering -------------------------------


class TestWriteMarkdownRendering:
    def test_md_write_rendered_as_markdown(self):
        html = format_write_input(WriteInput(file_path=MD, content=MD_BODY))
        assert re.search(r'class="write-tool-content markdown"', html)
        assert re.search(r"<h1[^>]*>Guide", html)

    def test_non_markdown_write_keeps_pygments(self):
        html = format_write_input(WriteInput(file_path=PY, content="x = 1\n"))
        assert 'class="write-tool-content markdown"' not in html

    def test_md_write_escapes_raw_html(self):
        html = format_write_input(
            WriteInput(file_path=MD, content="# T\n\n<script>alert(1)</script>")
        )
        assert "&lt;script&gt;" in html
        assert "<script>alert(1)</script>" not in html

    def test_general_md_write_has_no_memory_link_resolution(self):
        html = format_write_input(WriteInput(file_path=MD, content="[peer](peer.md)\n"))
        assert 'href="peer.md"' in html
        assert "file://" not in html

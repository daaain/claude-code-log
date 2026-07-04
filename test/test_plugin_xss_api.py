"""Plugin XSS API contract (#245 follow-up).

Two things a plugin author needs and previously couldn't get from the stable
surface:

1. The escaping helpers are re-exported from ``claude_code_log.plugins``
   (escape_html, safe_markdown_inline) alongside render_markdown[_collapsible].
2. A plugin that follows the documented contract (dev-docs/plugins.md §4.2 —
   escape transcript-derived interpolation, both output paths) produces safe
   output on BOTH the HTML and Markdown render paths.
"""

from __future__ import annotations

from dataclasses import dataclass

import claude_code_log.plugins as plugins
from claude_code_log.html.renderer import HtmlRenderer
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import MessageContent, MessageMeta
from claude_code_log.plugins import escape_html, safe_markdown_inline
from claude_code_log.renderer import TemplateMessage

PAYLOAD = "<img src=x onerror=alert(1)>"


# ----------------------------- API re-export ---------------------------------


class TestPluginApiReExports:
    def test_helpers_importable_and_in_all(self):
        for name in (
            "render_markdown",
            "render_markdown_collapsible",
            "escape_html",
            "safe_markdown_inline",
            "is_safe_web_url",
            "safe_markdown_link_target",
        ):
            assert hasattr(plugins, name), name
            assert name in plugins.__all__, name

    def test_escape_html_behaviour(self):
        assert escape_html(PAYLOAD) == "&lt;img src=x onerror=alert(1)&gt;"

    def test_safe_markdown_inline_behaviour(self):
        assert safe_markdown_inline(PAYLOAD) == "&lt;img src=x onerror=alert(1)&gt;"
        # Tag-free text passes through unchanged (no markdown re-normalisation).
        assert safe_markdown_inline("a **bold** label") == "a **bold** label"

    def test_is_safe_web_url_behaviour(self):
        from claude_code_log.plugins import is_safe_web_url

        assert is_safe_web_url("https://example.com/page")
        assert is_safe_web_url("http://example.com")
        # Hostile or odd schemes fail closed — including case variants.
        assert not is_safe_web_url("javascript:alert(1)")
        assert not is_safe_web_url("JAVASCRIPT:alert(1)")
        assert not is_safe_web_url("data:text/html,<script>alert(1)</script>")
        assert not is_safe_web_url("file:///etc/passwd")
        assert not is_safe_web_url("HTTP://example.com")  # fails closed, by design
        assert not is_safe_web_url("")

    def test_safe_markdown_link_target_behaviour(self):
        from claude_code_log.plugins import safe_markdown_link_target

        # Breakout characters are percent-encoded so an accepted URL cannot
        # escape a Markdown (target) slot or smuggle attributes through a
        # downstream Markdown→HTML conversion (#262).
        assert (
            safe_markdown_link_target('https://x/f.png" onerror="alert(1)')
            == "https://x/f.png%22%20onerror=%22alert%281%29"
        )
        assert safe_markdown_link_target("https://x/a(b)'c<d>") == (
            "https://x/a%28b%29%27c%3Cd%3E"
        )
        # ASCII control characters also terminate an un-bracketed CommonMark
        # destination — the full range (U+0000-U+001F, U+007F) is encoded.
        assert (
            safe_markdown_link_target("https://x/a\nb\tc\rd\x00e\x7ff")
            == "https://x/a%0Ab%09c%0Dd%00e%7Ff"
        )
        # A clean URL passes through unchanged.
        assert (
            safe_markdown_link_target("https://example.com/page?q=1&r=2")
            == "https://example.com/page?q=1&r=2"
        )


# ----------------------------- plugin-author contract ------------------------


@dataclass
class _PluginContent(MessageContent):
    """A plugin-style MessageContent that interpolates transcript-derived data
    (``payload``) into all three render methods, escaping per dev-docs §4.2."""

    payload: str = ""

    @property
    def message_type(self) -> str:
        return "plugin_demo"

    # HTML output: the return is injected as live DOM → escape_html.
    def format_html(self, _renderer, _message) -> str:
        return f"<div class='plug'>{escape_html(self.payload)}</div>"

    # Markdown output: emitted verbatim → safe_markdown_inline for inline text.
    def format_markdown(self, _renderer, _message) -> str:
        return f"plug: {safe_markdown_inline(self.payload)}"

    # title() goes to {{ message_title | safe }} (HTML, no core escaping) →
    # escape_html; the Markdown heading path is additionally core-gated.
    def title(self, _renderer, _message) -> str:
        return f"Plug: {escape_html(self.payload)}"


def _msg() -> TemplateMessage:
    content = _PluginContent(
        meta=MessageMeta(uuid="u", session_id="s", timestamp="2025-01-01T00:00:00Z"),
        payload=PAYLOAD,
    )
    return TemplateMessage(content)


class TestPluginAuthorContractIsSafeBothPaths:
    def test_html_format_escaped(self):
        out = HtmlRenderer().format_content(_msg())
        assert "<img" not in out.lower()
        assert "&lt;img" in out.lower()

    def test_html_title_escaped(self):
        out = HtmlRenderer().title_content(_msg())
        assert "<img" not in out.lower()
        assert "&lt;img" in out.lower()

    def test_markdown_format_escaped(self):
        out = MarkdownRenderer().format_content(_msg())
        assert "<img" not in out.lower()
        assert "&lt;img" in out.lower()

    def test_markdown_title_escaped(self):
        out = MarkdownRenderer().title_content(_msg())
        assert "<img" not in out.lower()
        assert "&lt;img" in out.lower()

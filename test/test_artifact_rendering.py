#!/usr/bin/env python3
"""Test cases for Artifact tool rendering (issue #257).

The Artifact tool deploys a self-contained HTML or Markdown file as a
claude.ai web page. The page content never reaches the transcript (the
tool takes a file path), but every string field — description, label,
title, URL — is transcript-derived and must render escaped, and the
URL must never become a clickable link for a non-http(s) scheme.
"""

from pathlib import Path

from claude_code_log.converter import load_transcript
from claude_code_log.factories.tool_factory import (
    create_tool_input,
    parse_artifact_output,
)
from claude_code_log.html import format_artifact_input, format_artifact_output
from claude_code_log.html.renderer import HtmlRenderer, generate_html
from claude_code_log.markdown.renderer import MarkdownRenderer
from claude_code_log.models import (
    ArtifactInput,
    ArtifactOutput,
    MessageMeta,
    ToolResultContent,
    ToolResultMessage,
    ToolUseMessage,
)
from claude_code_log.renderer import TemplateMessage


def _input_message(artifact_input: ArtifactInput) -> TemplateMessage:
    return TemplateMessage(
        ToolUseMessage(
            MessageMeta.empty(),
            input=artifact_input,
            tool_use_id="toolu_artifact",
            tool_name="Artifact",
        )
    )


def _output_message(output: ArtifactOutput) -> TemplateMessage:
    return TemplateMessage(
        ToolResultMessage(
            MessageMeta.empty(),
            output=output,
            tool_use_id="toolu_artifact",
            tool_name="Artifact",
        )
    )


class TestArtifactInput:
    """Test Artifact input model and HTML formatting."""

    def test_input_minimal(self):
        artifact_input = ArtifactInput(file_path="/workspace/page.html")
        assert artifact_input.file_path == "/workspace/page.html"
        assert artifact_input.favicon == ""
        assert artifact_input.description is None
        assert artifact_input.url is None

    def test_input_full(self):
        artifact_input = ArtifactInput(
            file_path="/workspace/page.html",
            favicon="📊",
            description="A dashboard",
            label="v2",
            url="https://claude.ai/code/artifact/abc",
            force=True,
        )
        assert artifact_input.favicon == "📊"
        assert artifact_input.force is True

    def test_input_tolerates_unknown_fields(self):
        """Schema drift across Claude Code versions must not drop the
        message to the generic params table."""
        parsed = create_tool_input(
            "Artifact",
            {"file_path": "/workspace/page.html", "favicon": "📊", "novel": "field"},
        )
        assert isinstance(parsed, ArtifactInput)

    def test_format_input_metadata(self):
        html = format_artifact_input(
            ArtifactInput(
                file_path="/workspace/page.html",
                favicon="📊",
                description="A dashboard",
                label="v2",
            )
        )
        assert "A dashboard" in html
        assert "📊" in html
        assert "v2" in html

    def test_format_input_redeploy_link(self):
        html = format_artifact_input(
            ArtifactInput(
                file_path="/workspace/page.html",
                favicon="📊",
                url="https://claude.ai/code/artifact/abc",
                force=True,
            )
        )
        assert '<a href="https://claude.ai/code/artifact/abc">' in html
        assert "force" in html

    def test_format_input_escapes_html(self):
        """XSS guard: hostile description/label render escaped, never live."""
        html = format_artifact_input(
            ArtifactInput(
                file_path="/workspace/page.html",
                favicon="🧪",
                description="<img src=x onerror=alert(1)>",
                label="<b>bold</b>",
            )
        )
        assert "<img" not in html
        assert "&lt;img" in html
        assert "<b>" not in html

    def test_format_input_javascript_url_not_linked(self):
        """A hostile scheme must not become a clickable redeploy link."""
        html = format_artifact_input(
            ArtifactInput(
                file_path="/workspace/page.html",
                favicon="🧪",
                url="javascript:alert(1)",
            )
        )
        assert "href" not in html
        assert "javascript:alert(1)" in html  # Shown as text, not a link


class TestArtifactParser:
    """Test Artifact output parsing."""

    def test_parse_structured_output(self):
        tool_result = ToolResultContent(
            type="tool_result",
            tool_use_id="toolu_test",
            content="Published /workspace/page.html at https://claude.ai/code/artifact/abc",
        )
        output = parse_artifact_output(
            tool_result,
            None,
            {
                "url": "https://claude.ai/code/artifact/abc",
                "path": "/workspace/page.html",
                "title": "My page",
            },
        )
        assert isinstance(output, ArtifactOutput)
        assert output.url == "https://claude.ai/code/artifact/abc"
        assert output.path == "/workspace/page.html"
        assert output.title == "My page"
        assert output.raw_text is None

    def test_parse_structured_minimal(self):
        tool_result = ToolResultContent(
            type="tool_result", tool_use_id="toolu_test", content=""
        )
        output = parse_artifact_output(
            tool_result, None, {"url": "https://claude.ai/code/artifact/abc"}
        )
        assert isinstance(output, ArtifactOutput)
        assert output.url == "https://claude.ai/code/artifact/abc"
        assert output.path is None
        assert output.title is None

    def test_parse_text_fallback_published_line(self):
        tool_result = ToolResultContent(
            type="tool_result",
            tool_use_id="toolu_test",
            content="Published /workspace/page.html at https://claude.ai/code/artifact/abc",
        )
        output = parse_artifact_output(tool_result, None, None)
        assert isinstance(output, ArtifactOutput)
        assert output.url == "https://claude.ai/code/artifact/abc"
        assert output.path == "/workspace/page.html"

    def test_parse_text_fallback_error_message(self):
        """Denied/error results (is_error, no toolUseResult) keep raw text."""
        tool_result = ToolResultContent(
            type="tool_result",
            tool_use_id="toolu_test",
            content="Permission for this action was denied by the classifier.",
        )
        output = parse_artifact_output(tool_result, None, None)
        assert isinstance(output, ArtifactOutput)
        assert output.url is None
        assert output.raw_text is not None
        assert "denied" in output.raw_text

    def test_parse_empty_returns_none(self):
        tool_result = ToolResultContent(
            type="tool_result", tool_use_id="toolu_test", content=""
        )
        assert parse_artifact_output(tool_result, None, None) is None


class TestArtifactOutputFormatting:
    """Test Artifact output HTML formatting."""

    def test_format_output_full(self):
        html = format_artifact_output(
            ArtifactOutput(
                url="https://claude.ai/code/artifact/abc",
                path="/workspace/page.html",
                title="My page",
            )
        )
        assert "Published" in html
        assert "My page" in html
        assert '<a href="https://claude.ai/code/artifact/abc">' in html

    def test_format_output_no_title(self):
        html = format_artifact_output(
            ArtifactOutput(url="https://claude.ai/code/artifact/abc")
        )
        assert "Published" in html
        assert '<a href="https://claude.ai/code/artifact/abc">' in html

    def test_format_output_raw_text(self):
        html = format_artifact_output(ArtifactOutput(raw_text="Denied by policy"))
        assert "Denied by policy" in html
        assert "<a " not in html

    def test_format_output_empty(self):
        assert format_artifact_output(ArtifactOutput()) == ""

    def test_format_output_escapes_hostile_title(self):
        """XSS guard: a hostile page title renders escaped, never live."""
        html = format_artifact_output(
            ArtifactOutput(
                url="https://claude.ai/code/artifact/abc",
                title="<script>alert(1)</script>",
            )
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_format_output_javascript_url_not_linked(self):
        """A hostile scheme in toolUseResult.url must not become an href."""
        html = format_artifact_output(
            ArtifactOutput(url="javascript:alert(1)", title="page")
        )
        assert "href" not in html
        assert "javascript:alert(1)" in html  # Escaped text, not a link


class TestArtifactHtmlRenderer:
    """Test HtmlRenderer dispatch methods."""

    def test_title(self):
        renderer = HtmlRenderer()
        artifact_input = ArtifactInput(
            file_path="/workspace/dashboard.html", favicon="📊"
        )
        title = renderer.title_ArtifactInput(
            artifact_input, _input_message(artifact_input)
        )
        assert "🖼️" in title
        assert "/workspace/dashboard.html" in title

    def test_format_input_dispatch(self):
        renderer = HtmlRenderer()
        artifact_input = ArtifactInput(
            file_path="/workspace/dashboard.html",
            favicon="📊",
            description="A dashboard",
        )
        html = renderer.format_ArtifactInput(
            artifact_input, _input_message(artifact_input)
        )
        assert "A dashboard" in html

    def test_format_output_dispatch(self):
        renderer = HtmlRenderer()
        output = ArtifactOutput(
            url="https://claude.ai/code/artifact/abc", title="My page"
        )
        html = renderer.format_ArtifactOutput(output, _output_message(output))
        assert "My page" in html


class TestArtifactMarkdownRenderer:
    """Test MarkdownRenderer dispatch methods."""

    def test_title(self):
        renderer = MarkdownRenderer()
        artifact_input = ArtifactInput(
            file_path="/workspace/dashboard.html", favicon="📊"
        )
        title = renderer.title_ArtifactInput(
            artifact_input, _input_message(artifact_input)
        )
        assert title == "🖼️ Artifact `/workspace/dashboard.html`"

    def test_format_input(self):
        renderer = MarkdownRenderer()
        artifact_input = ArtifactInput(
            file_path="/workspace/dashboard.html",
            favicon="📊",
            description="A dashboard",
            label="v2",
        )
        md = renderer.format_ArtifactInput(
            artifact_input, _input_message(artifact_input)
        )
        assert "*A dashboard*" in md
        assert "📊 · v2" in md

    def test_format_output_link(self):
        renderer = MarkdownRenderer()
        output = ArtifactOutput(
            url="https://claude.ai/code/artifact/abc", title="My page"
        )
        md = renderer.format_ArtifactOutput(output, _output_message(output))
        assert "**My page**" in md
        assert "(https://claude.ai/code/artifact/abc)" in md

    def test_format_output_javascript_url_not_linked(self):
        """A hostile scheme must render as inline code, not a Markdown link."""
        renderer = MarkdownRenderer()
        output = ArtifactOutput(url="javascript:alert(1)")
        md = renderer.format_ArtifactOutput(output, _output_message(output))
        assert "](javascript:" not in md
        assert "`javascript:alert(1)`" in md

    def test_format_output_paren_url_stays_inside_link_target(self):
        """Parentheses in the URL must not break out of the (url) syntax."""
        renderer = MarkdownRenderer()
        output = ArtifactOutput(url="https://example.com/a(b)c")
        md = renderer.format_ArtifactOutput(output, _output_message(output))
        assert "(https://example.com/a%28b%29c)" in md

    def test_format_output_raw_text_quoted(self):
        renderer = MarkdownRenderer()
        output = ArtifactOutput(raw_text="Denied by policy")
        md = renderer.format_ArtifactOutput(output, _output_message(output))
        assert md.startswith("> ")


class TestArtifactEndToEnd:
    """Render the checked-in fixture through both pipelines."""

    FIXTURE = Path(__file__).parent / "test_data" / "artifact_tool.jsonl"

    def test_html_end_to_end(self):
        messages = load_transcript(self.FIXTURE)
        html = generate_html(messages, "Artifact Test")

        # Success + redeploy + markdown variants all render with links
        assert "Flaky tests report" in html
        assert (
            '<a href="https://claude.ai/code/artifact/11111111-2222-3333-4444-555555555555">'
            in html
        )
        assert "notes.md" in html
        # Denied variant renders its text
        assert "denied by the Claude Code auto mode classifier" in html
        # Title icon suppresses the default wrench (no double icon)
        assert "🖼️" in html
        assert "🛠️ 🖼️" not in html

    def test_frame_link_entry_skipped_silently(self, capsys):
        """The ``frame-link`` entry Claude Code writes alongside a
        successful publish is bookkeeping (path → deployed URL, no uuid,
        redundant with the tool_result) — it must be skipped without an
        'unrecognized message type' warning."""
        load_transcript(self.FIXTURE)
        captured = capsys.readouterr()
        assert "unrecognized message type" not in captured.out

    def test_html_end_to_end_xss(self):
        """The hostile entry's payloads must never survive unescaped."""
        messages = load_transcript(self.FIXTURE)
        html = generate_html(messages, "Artifact XSS Test")

        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror=alert(1)>" not in html
        assert 'href="javascript:' not in html
        assert "href='javascript:" not in html

    def test_markdown_end_to_end_xss(self):
        messages = load_transcript(self.FIXTURE)
        renderer = MarkdownRenderer()
        md = renderer.generate(messages, "Artifact XSS Test")

        # No hostile scheme survives into a Markdown link destination.
        assert "](javascript:" not in md
        # Inline-text surfaces are entity-escaped (#245 contract). The
        # hostile file path still appears raw inside an ``_inline_code``
        # span in the title — code spans are the sanctioned literal
        # carrier; a Markdown converter escapes their content itself.
        assert "<img" not in md.lower()
        assert "&lt;img" in md.lower()
        assert "<script>alert(1)</script> hostile title" not in md
        assert "&lt;script&gt;alert(1)&lt;/script&gt; hostile title" in md

"""Tool-result transformer: specialize ToolResultMessage for a known MCP tool.

Companion to ``tool_communicate.py`` (which specializes the matching
``ToolUseMessage``). Demonstrates two patterns plugin authors often
need but the other two reference transformers don't exercise:

- ``applies_to = (ToolResultMessage,)`` instead of the more common
  ``ToolUseMessage`` / ``UserTextMessage`` matches.
- Long-Markdown-body rendering via the public
  :func:`claude_code_log.plugins.render_markdown_collapsible` helper.
  The plugin returns rich HTML from ``format_html`` (collapsible
  ``<details>`` block with a preview) without writing its own
  ``<details>`` template, mistune call, or pygments wiring.

The tool name is the same fixture id used by ``tool_communicate.py``
so an integration test can cover the input + result pair end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional

from claude_code_log.factories.priorities import TOOL_OUTPUT_GENERIC
from claude_code_log.models import (
    DetailLevel,
    MessageContent,
    MessageMeta,
    ToolResultMessage,
)
from claude_code_log.plugins import render_markdown_collapsible


# Same fixture id as the input transformer (tool_communicate.py) so
# the input/result pair can be exercised end-to-end in tests.
TOOL_NAME = "mcp__test_plugin__clmail__communicate"


def _body_text(content: object) -> str:
    """Best-effort extraction of the result body as a plain string.

    The actual ``ToolResultMessage.output`` shape varies: in this
    transformer's path it's typically a ``ToolResultContent`` whose
    ``content`` is a string. We treat anything else as empty so
    ``format_html`` always has something safe to hand to
    ``render_markdown_collapsible``.
    """
    raw = getattr(content, "content", "")
    return raw if isinstance(raw, str) else ""


@dataclass
class TestClmailCommunicateResultMessage(ToolResultMessage):
    """Plugin-defined ToolResultMessage subclass with class-side formatters.

    Subclasses ``ToolResultMessage`` (not bare ``MessageContent``) to
    satisfy the runtime contract in ``apply_transformers`` — a
    transformer declaring ``applies_to=(ToolResultMessage,)`` MUST
    return an instance of that class or a subclass.
    """

    # Visible at LOW so a "read the mail thread" flow surfaces the
    # actual reply text in the default summary view.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    def format_markdown(self, _renderer, _message) -> str:
        body = _body_text(self.output)
        if not body:
            return "_(test) ClMail result (empty)_"
        return body  # The Markdown body is the natural rendering.

    def format_html(self, _renderer, _message) -> Optional[str]:
        body = _body_text(self.output)
        if not body:
            return "<em>(test) ClMail result (empty)</em>"
        # Long bodies (e.g. multi-paragraph mail) collapse to a
        # preview with an expand toggle; short bodies render inline.
        # The wrapper div carries ``markdown`` so the host theme's
        # ``.markdown table`` / ``.markdown pre`` styling fires —
        # important for replies that contain tables or code blocks.
        return render_markdown_collapsible(
            raw_content=body,
            css_class="test-clmail-result",
            line_threshold=20,
            preview_line_count=5,
        )

    def title(self, _renderer, _message) -> Optional[str]:
        return "✉ ClMail result"


class ClmailCommunicateResultTransformer:
    """Specialize ToolResultMessage for the test clmail communicate tool."""

    name: ClassVar[str] = "test.clmail.communicate.result"
    # Smaller number = earlier in the transformer chain. Under the v1
    # post-classification implementation, this orders us against other
    # transformers (not against built-in classifiers, which have
    # already run). Sit 500 units before TOOL_OUTPUT_GENERIC so a
    # future plugin targeting the same tool at TOOL_OUTPUT_GENERIC
    # would lose to us.
    priority: ClassVar[int] = TOOL_OUTPUT_GENERIC - 500
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolResultMessage,)

    def transform(
        self,
        content: MessageContent,
        _meta: MessageMeta,
    ) -> Optional[MessageContent]:
        if not isinstance(content, ToolResultMessage):
            return None
        if content.tool_name != TOOL_NAME:
            return None
        return TestClmailCommunicateResultMessage(
            meta=content.meta,
            tool_use_id=content.tool_use_id,
            output=content.output,
            is_error=content.is_error,
            tool_name=content.tool_name,
            file_path=content.file_path,
        )

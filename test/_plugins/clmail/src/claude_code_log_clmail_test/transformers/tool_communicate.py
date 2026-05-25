"""Tool-rendering transformer: specialize a ToolUseMessage for a known MCP tool.

Exercises the tool-side branch of the plugin contract:

- ``applies_to = (ToolUseMessage,)`` MRO filter
- Narrows by ``content.tool_name`` inside ``transform()``
- Returns a plugin-defined subclass of ``ToolUseMessage`` carrying its
  own class-side ``format_markdown`` / ``title`` methods
- The plugin subclass declares ``detail_visibility = LOW`` so it
  shows up at ``--detail low`` without core needing to update
  ``_LOW_KEEP_TOOLS``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Optional

from claude_code_log.factories.priorities import TOOL_INPUT_GENERIC
from claude_code_log.models import (
    DetailLevel,
    MessageContent,
    MessageMeta,
    ToolUseMessage,
)


# The specific MCP tool name this transformer claims. The real clmail
# plugin would use ``mcp__plugin_clmail_clmail__communicate``; we use
# a test-fixture name to avoid colliding with any real tool that test
# fixtures might emit.
TOOL_NAME = "mcp__test_plugin__clmail__communicate"


@dataclass
class TestClmailCommunicateInputMessage(ToolUseMessage):
    """Plugin-defined ToolUseMessage subclass with class-side formatters."""

    # Plugin-owned visibility: visible at --detail low (the user-relevant
    # default for "show me clmail-style mail-handling activity"). Bypasses
    # the core _LOW_KEEP_TOOLS allowlist.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.LOW

    # No ``format_html`` defined: HtmlRenderer dispatch synthesizes
    # HTML from this ``format_markdown`` output via mistune and wraps
    # in ``<div class="markdown">``. See dev-docs/plugins.md §4 for
    # when to add ``format_html`` (custom HTML construction) vs
    # leaving it absent (mistune-of-format_markdown is enough).
    def format_markdown(self, _renderer, _message) -> str:
        # Pull the action out of the parsed input (or the raw input dict).
        raw_input = getattr(self.input, "input", None)
        if isinstance(raw_input, dict):
            action = raw_input.get("action", "?")
        else:
            action = "?"
        return f"_(test) ClMail communicate action={action}_"

    def title(self, _renderer, _message) -> Optional[str]:
        raw_input = getattr(self.input, "input", None)
        if isinstance(raw_input, dict):
            action = raw_input.get("action", "?")
        else:
            action = "?"
        return f"✉ ClMail communicate · {action}"


class ClmailCommunicateInputTransformer:
    """Specialize ToolUseMessage for the test clmail communicate tool."""

    name: ClassVar[str] = "test.clmail.communicate.input"
    # Smaller number = earlier in the transformer chain. Under the v1
    # post-classification implementation, this orders us against other
    # transformers (not against built-in classifiers, which have already
    # run). TOOL_INPUT_GENERIC is the priority slot conceptually
    # associated with the generic ToolUseMessage classification; we
    # sit 500 units before it so a future plugin targeting the same
    # tool at TOOL_INPUT_GENERIC would lose to us.
    priority: ClassVar[int] = TOOL_INPUT_GENERIC - 500
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (ToolUseMessage,)

    def transform(
        self,
        content: MessageContent,
        _meta: MessageMeta,
    ) -> Optional[MessageContent]:
        if not isinstance(content, ToolUseMessage):
            return None
        if content.tool_name != TOOL_NAME:
            return None
        # Build a new instance carrying the same field values; the
        # plugin subclass's class-side formatters take over at render.
        return TestClmailCommunicateInputMessage(
            meta=content.meta,
            input=content.input,
            tool_use_id=content.tool_use_id,
            tool_name=content.tool_name,
            skill_body=content.skill_body,
        )

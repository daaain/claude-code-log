"""Hook-demotion transformer: rewrite UserTextMessage by text-prefix match.

Exercises the user-side branch of the plugin contract:

- ``applies_to = (UserTextMessage,)`` MRO filter
- Reads ``content.items`` to access the user's text
- Returns a plugin-defined ``MessageContent`` subclass
- The subclass declares ``detail_visibility`` and carries its own
  ``format_markdown`` / ``title`` methods (Strategy 2 of
  ``_dispatch_format``)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Optional

from claude_code_log.factories.priorities import HOOK_NOTIFICATION
from claude_code_log.models import (
    DetailLevel,
    MessageContent,
    MessageMeta,
    UserTextMessage,
)


@dataclass
class TestHookNotificationMessage(UserTextMessage):
    """Plugin-defined typed wrapper for ``[testhook] ...`` user turns.

    Subclasses ``UserTextMessage`` (not bare ``MessageContent``) so the
    runtime contract in ``apply_transformers`` accepts it: a transformer
    with ``applies_to=(UserTextMessage,)`` MUST return an instance of
    that class or a subclass. Sibling rewrites would be rejected.

    The inherited ``items`` field is set to an empty list by default;
    this class carries the parsed ``source`` and ``text`` separately,
    so ``items`` is unused at render time (the class-side
    ``format_markdown`` reads ``self.source`` / ``self.text`` directly).
    """

    source: str = ""
    text: str = ""

    # Plugin-owned visibility: dropped at HIGH and below.
    detail_visibility: ClassVar[DetailLevel] = DetailLevel.FULL

    @property
    def message_type(self) -> str:
        return "test_hook_notification"

    # Class-side format/title methods (Strategy 2 of _dispatch_format).
    # The dispatcher calls these with (self, renderer, message) when no
    # renderer-side method shadows them.
    def format_markdown(self, _renderer, _message) -> str:
        return f"*[{self.source}] {self.text}*"

    def format_html(self, _renderer, _message) -> str:
        # v1: render the Markdown body explicitly. Returning ``None``
        # would NOT fall back to mistune today — it'd render as the
        # literal string "None" in the rendered card (see dev-docs/
        # plugins.md §4 caveat + §12 v2 dispatch auto-wrap plan).
        from claude_code_log.plugins import render_markdown

        return render_markdown(self.format_markdown(_renderer, _message))

    def title(self, _renderer, _message) -> Optional[str]:
        # Headless — appears inline.
        return None


_PATTERN = re.compile(r"^\s*\[testhook\]\s*(.*?)\s*\Z", re.DOTALL)


class TestHookDemotion:
    """Match ``[testhook] <body>`` user turns; demote to a plugin class."""

    name: ClassVar[str] = "test.hook-demotion"
    priority: ClassVar[int] = HOOK_NOTIFICATION
    applies_to: ClassVar[tuple[type[MessageContent], ...]] = (UserTextMessage,)

    def transform(
        self,
        content: MessageContent,
        meta: MessageMeta,
    ) -> Optional[MessageContent]:
        # Defensive: should always be true given applies_to, but the
        # transformer protocol allows any MessageContent so we narrow.
        if not isinstance(content, UserTextMessage):
            return None
        # Reconstruct the joined text from the user's content items.
        text = "\n".join(
            getattr(item, "text", "") for item in content.items if hasattr(item, "text")
        )
        # Multi-line guard — checked on the *whole* text, before regex.
        # The pattern's ``\s*`` after ``[testhook]`` would otherwise
        # consume a newline immediately after the marker, hiding the
        # multi-line shape from the post-match ``"\n" in m.group(1)``
        # check. Real human prompts that happen to start with
        # ``[testhook]`` and continue on the next line pass through
        # unchanged.
        if "\n" in text:
            return None
        m = _PATTERN.match(text)
        if m is None:
            return None
        return TestHookNotificationMessage(
            meta=meta, source="testhook", text=m.group(1)
        )

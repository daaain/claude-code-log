"""System reminders embedded in user messages (issue #275).

A user message can carry a ``<system-reminder>`` block (e.g. the ``/cd``
working-directory notice) and it is *not necessarily the whole message* — a
CLAUDE.md can follow it. The reminder is peeled out as an annotation while the
remaining text still renders as user content (mirroring IdeNotificationContent).

Reminders are emitted at their ORIGINAL position (not prepended): ~14% of real
reminder messages carry user text before the reminder, and some carry multiple.

Covers: the UserTextMessage item structure (positional ordering, multiple
reminders), HTML rendering (the #275 fix — styled annotation, no raw tags),
escaping on both the HTML and Markdown paths, the reminder-only case, and the
session-starter/preview edge (a reminder-only opener must not surface raw
reminder tags on the index).
"""

from claude_code_log.factories import (
    create_user_message,
)
from claude_code_log.parser import extract_text_content
from claude_code_log.html.user_formatters import (
    format_system_reminder_content,
    format_user_text_model_content,
)
from claude_code_log.models import (
    MessageMeta,
    SystemReminderContent,
    TextContent,
    UserTextMessage,
)
from claude_code_log.utils import create_session_preview, should_use_as_session_starter


def _user_message(text: str) -> UserTextMessage:
    content_list = [TextContent(type="text", text=text)]
    model = create_user_message(
        MessageMeta.empty(), content_list, extract_text_content(content_list)
    )
    assert isinstance(model, UserTextMessage)
    return model


# ---------------------------------------------------------------------------
# UserTextMessage item structure — reminders are emitted at their ORIGINAL
# position. Measured over the real corpus: of 21 user messages carrying a
# reminder, 14 are reminder-at-start-no-tail, 4 are reminder+tail (the /cd +
# CLAUDE.md case), 2 have text BEFORE the reminder, 1 has text before AND after,
# and 2 carry multiple reminders. Prepending an aggregate (the pre-fix
# behaviour) reordered the ~14% with leading text — hence positional splitting.
# ---------------------------------------------------------------------------
def _kinds(model: UserTextMessage) -> list[str]:
    return [type(i).__name__ for i in model.items]


def _reminders(item: object) -> list[str]:
    assert isinstance(item, SystemReminderContent)
    return item.reminders


def _text(item: object) -> str:
    assert isinstance(item, TextContent)
    return item.text


def test_reminder_leads_then_tail_preserves_order() -> None:
    model = _user_message(
        "<system-reminder>cwd changed</system-reminder>\n\n# Project\nreal content"
    )
    assert _kinds(model) == ["SystemReminderContent", "TextContent"]
    assert _reminders(model.items[0]) == ["cwd changed"]
    assert "real content" in _text(model.items[1])


def test_text_before_reminder_keeps_leading_text_first() -> None:
    """~10% of real reminder messages have user text BEFORE the reminder.
    Prepending would hoist the reminder above 'earlier note' — this pins the
    order text → reminder."""
    model = _user_message(
        "earlier note\n<system-reminder>cwd changed</system-reminder>"
    )
    assert _kinds(model) == ["TextContent", "SystemReminderContent"]
    assert "earlier note" in _text(model.items[0])
    assert _reminders(model.items[1]) == ["cwd changed"]


def test_text_before_and_after_reminder_keeps_all_three_in_order() -> None:
    """The 'session continued …' shape: prose, a reminder, then more prose.
    All three survive in source order (prepend collapsed 'before'/'after' and
    hoisted the reminder)."""
    model = _user_message(
        "before text\n<system-reminder>note</system-reminder>\nafter text"
    )
    assert _kinds(model) == [
        "TextContent",
        "SystemReminderContent",
        "TextContent",
    ]
    assert "before text" in _text(model.items[0])
    assert _reminders(model.items[1]) == ["note"]
    assert "after text" in _text(model.items[2])


def test_multiple_reminders_each_emitted_in_place() -> None:
    """Two messages in the corpus carry 3 and 4 reminders. Each is emitted at
    its own position with the text between preserved — not merged into one
    prepended aggregate."""
    model = _user_message(
        "start<system-reminder>first</system-reminder>"
        "mid<system-reminder>second</system-reminder>end"
    )
    assert _kinds(model) == [
        "TextContent",
        "SystemReminderContent",
        "TextContent",
        "SystemReminderContent",
        "TextContent",
    ]
    assert "start" in _text(model.items[0])
    assert _reminders(model.items[1]) == ["first"]
    assert "mid" in _text(model.items[2])
    assert _reminders(model.items[3]) == ["second"]
    assert "end" in _text(model.items[4])


def test_reminder_only_message_yields_just_the_annotation() -> None:
    model = _user_message("<system-reminder>cwd changed via /cd</system-reminder>")
    assert len(model.items) == 1
    assert isinstance(model.items[0], SystemReminderContent)


def test_message_without_reminder_is_plain_user_text() -> None:
    """No SystemReminderContent is introduced for a reminder-free message."""
    model = _user_message("plain user message")
    assert not any(isinstance(i, SystemReminderContent) for i in model.items)
    assert len(model.items) == 1
    assert isinstance(model.items[0], TextContent)


# ---------------------------------------------------------------------------
# HTML rendering — the #275 fix (RED before this change, GREEN after)
# ---------------------------------------------------------------------------
def test_html_renders_styled_annotation_not_raw_tags() -> None:
    """The bug in #275: today the raw <system-reminder> tags render as user
    text. After the fix they render as a styled 🤖 annotation, and the CLAUDE.md
    tail still renders as user content."""
    model = _user_message(
        "<system-reminder>cwd changed to /main</system-reminder>\n\n"
        "# Project\nCLAUDE.md body."
    )
    html = "".join(format_user_text_model_content(model))
    assert "system-reminder" in html  # styled annotation class
    assert "🤖" in html
    assert "CLAUDE.md body" in html  # remaining content preserved
    # Neither the OPENING nor the CLOSING tag may survive — raw or escaped.
    assert "<system-reminder>" not in html
    assert "</system-reminder>" not in html
    assert "&lt;system-reminder&gt;" not in html
    assert "&lt;/system-reminder&gt;" not in html


def test_html_escapes_reminder_body() -> None:
    parts = format_system_reminder_content(
        SystemReminderContent(reminders=["<script>alert(1)</script>"])
    )
    joined = "".join(parts)
    assert "&lt;script&gt;" in joined
    assert "<script>" not in joined


def test_html_one_div_per_reminder() -> None:
    parts = format_system_reminder_content(
        SystemReminderContent(reminders=["a", "b", "c"])
    )
    assert len(parts) == 3
    # Each fragment is a styled reminder div, not just a count.
    assert all(fragment.lstrip().startswith("<div") for fragment in parts)
    assert all("system-reminder" in fragment for fragment in parts)


# ---------------------------------------------------------------------------
# Markdown mirror path (N1): the reminder must be rendered, not dropped
# ---------------------------------------------------------------------------
def test_markdown_renders_reminder_as_blockquote_not_dropped() -> None:
    """The HTML/MD mirror pair: Markdown deliberately renders the reminder as a
    blockquote rather than dropping it (IDE notifications ARE dropped in MD;
    peeling the reminder out must not lose content that rendered inline before).
    Pins that decision so a future refactor can't silently revert it."""
    from claude_code_log.markdown.renderer import MarkdownRenderer
    from claude_code_log.renderer import TemplateMessage

    model = _user_message(
        "<system-reminder>cwd changed to /main</system-reminder>\n\n# Project\nreal content"
    )
    md = MarkdownRenderer().format_content(TemplateMessage(model))
    assert "System reminder" in md  # reminder survives in Markdown
    assert "🤖" in md
    assert "> " in md  # rendered as a blockquote
    assert "real content" in md  # remaining content preserved
    assert "<system-reminder>" not in md  # no raw opening tag
    assert "</system-reminder>" not in md  # nor closing tag


def test_markdown_protects_html_in_reminder_body() -> None:
    """The MD mirror of the HTML escape pin: a reminder containing <script> must
    not survive as live HTML in generated Markdown (the TextContent path already
    runs _protect_html_tags; the reminder path must too)."""
    from claude_code_log.markdown.renderer import MarkdownRenderer
    from claude_code_log.renderer import TemplateMessage

    model = _user_message(
        "<system-reminder><script>alert(1)</script></system-reminder>"
    )
    md = MarkdownRenderer().format_content(TemplateMessage(model))
    assert "<script>" not in md  # not raw live HTML


# ---------------------------------------------------------------------------
# Session-starter / preview edge (must not surface raw reminder tags on index)
# ---------------------------------------------------------------------------
def test_reminder_only_message_is_not_a_session_starter() -> None:
    """A bare /cd reminder with no real content must not become the session
    preview — the next real user message does."""
    assert not should_use_as_session_starter(
        "<system-reminder>cwd changed via /cd</system-reminder>"
    )


def test_reminder_plus_content_is_a_starter_and_preview_strips_tags() -> None:
    text = "<system-reminder>cwd changed</system-reminder>\n# Project\nreal content"
    assert should_use_as_session_starter(text)
    preview = create_session_preview(text)
    assert "real content" in preview
    assert "<system-reminder>" not in preview
    assert "</system-reminder>" not in preview


def test_preview_text_before_and_after_reminder_does_not_weld_words() -> None:
    """The text-before+after shape is real (measured: 3 of 21 corpus reminder
    messages, one with 12 KB of tail). Stripping the reminder must not weld the
    surrounding words together on the index preview — the most-read surface."""
    preview = create_session_preview(
        "before text<system-reminder>note</system-reminder>after text"
    )
    assert "before text after text" in preview  # single space, not "textafter"
    assert "beforeafter" not in preview
    assert "  " not in preview  # no double gap where the reminder was
    assert "<system-reminder>" not in preview


def test_reminder_free_preview_is_unchanged() -> None:
    assert create_session_preview("just a normal message").startswith(
        "just a normal message"
    )

"""System reminders embedded in user messages (issue #275).

A user message can carry a ``<system-reminder>`` block (e.g. the ``/cd``
working-directory notice) and it is *not necessarily the whole message* — a
CLAUDE.md can follow it. The reminder is peeled out as an annotation while the
remaining text still renders as user content (mirroring IdeNotificationContent).

Covers: the extractor, the UserTextMessage item structure, HTML rendering (the
#275 fix — styled annotation, no raw tags), escaping, the reminder-only case,
and the session-starter/preview edge (a reminder-only opener must not surface
raw reminder tags on the index).
"""

from claude_code_log.factories import (
    create_user_message,
    extract_system_reminder_content,
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
# Extractor
# ---------------------------------------------------------------------------
def test_extract_peels_reminder_and_preserves_remaining() -> None:
    rc, remaining = extract_system_reminder_content(
        "<system-reminder>cwd changed to /main via /cd</system-reminder>\n\n"
        "# Project\nCLAUDE.md content."
    )
    assert rc is not None
    assert rc.reminders == ["cwd changed to /main via /cd"]
    assert remaining.strip() == "# Project\nCLAUDE.md content."


def test_extract_no_reminder_returns_none_and_unchanged_text() -> None:
    """Byte-stability at the extractor: a reminder-free message is untouched."""
    text = "just a normal user message, no reminder"
    rc, remaining = extract_system_reminder_content(text)
    assert rc is None
    assert remaining == text  # identical object semantics, not merely equal value


def test_extract_multiple_reminders() -> None:
    rc, remaining = extract_system_reminder_content(
        "<system-reminder>first</system-reminder>middle"
        "<system-reminder>second</system-reminder>tail"
    )
    assert rc is not None
    assert rc.reminders == ["first", "second"]
    assert remaining == "middletail"


# ---------------------------------------------------------------------------
# UserTextMessage item structure
# ---------------------------------------------------------------------------
def test_message_with_reminder_and_tail_yields_annotation_then_text() -> None:
    model = _user_message(
        "<system-reminder>cwd changed</system-reminder>\n\n# Project\nreal content"
    )
    assert len(model.items) == 2
    assert isinstance(model.items[0], SystemReminderContent)
    assert model.items[0].reminders == ["cwd changed"]
    assert isinstance(model.items[1], TextContent)
    assert "real content" in model.items[1].text


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
    # The raw tag must NOT survive as (escaped) text anywhere.
    assert "&lt;system-reminder&gt;" not in html
    assert "<system-reminder>" not in html


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
    assert "<system-reminder>" not in md  # no raw tag


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


def test_reminder_free_preview_is_unchanged() -> None:
    assert create_session_preview("just a normal message").startswith(
        "just a normal message"
    )

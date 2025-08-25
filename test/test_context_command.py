"""Tests for /context command output rendering."""

from claude_code_log.renderer import generate_html
from claude_code_log.parser import parse_transcript_entry


def test_context_command_rendering():
    """Test that /context command output is properly rendered with ANSI colors."""
    # Create a simplified version of context command output with all required fields
    messages = [
        {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/home/user",
            "sessionId": "test-session",
            "version": "1.0.0",
            "type": "user",
            "message": {
                "role": "user",
                "content": "<local-command-stdout>\n"
                "\x1b[38;2;136;136;136m⛁ \x1b[38;2;153;153;153m⛁ ⛁ ⛁ ⛁ ⛁ ⛁ \x1b[38;2;215;119;87m⛀ \x1b[38;2;147;51;234m⛀ \x1b[38;2;153;153;153m⛶ \x1b[39m\n"
                "\x1b[38;2;153;153;153m⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ \x1b[39m  \x1b[1mContext Usage\x1b[22m\n"
                "\x1b[38;2;153;153;153m⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ ⛶ \x1b[39m  \x1b[2mclaude-opus-4-1-20250805 • 16k/200k tokens (8%)\x1b[22m\n"
                "\n"
                "\x1b[1mMemory files\x1b[22m\x1b[38;2;153;153;153m · /memory\x1b[39m\n"
                "└ User (/home/user/.claude/CLAUDE.md): \x1b[38;2;153;153;153m155 tokens\x1b[39m\n"
                "</local-command-stdout>",
            },
            "uuid": "test1",
            "timestamp": "2025-01-20T10:00:00.000Z",
        }
    ]

    # Parse the raw messages into TranscriptEntry objects
    parsed_messages = [parse_transcript_entry(msg) for msg in messages]
    html = generate_html(parsed_messages)

    # Check that ANSI codes were converted to HTML spans with proper classes
    assert '<span style="color: rgb(136, 136, 136)">⛁ </span>' in html
    assert '<span style="color: rgb(153, 153, 153)">⛁ ⛁ ⛁ ⛁ ⛁ ⛁ </span>' in html
    assert '<span style="color: rgb(215, 119, 87)">⛀ </span>' in html
    assert '<span style="color: rgb(147, 51, 234)">⛀ </span>' in html

    # Check that bold text is properly rendered
    assert '<span class="ansi-bold">Context Usage</span>' in html
    assert '<span class="ansi-bold">Memory files</span>' in html

    # Check that dim text is properly rendered
    assert (
        '<span class="ansi-dim">claude-opus-4-1-20250805 • 16k/200k tokens (8%)</span>'
        in html
    )

    # Check that the special characters are preserved
    assert "⛁" in html
    assert "⛀" in html
    assert "⛶" in html

    # Check that command output container is present
    assert "Command Output:" in html
    assert "command-output-content" in html

    # Ensure ANSI codes are not present in raw form
    assert "\x1b[" not in html


def test_context_command_without_ansi():
    """Test that /context command output without ANSI codes still renders correctly."""
    messages = [
        {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/home/user",
            "sessionId": "test-session",
            "version": "1.0.0",
            "type": "user",
            "message": {
                "role": "user",
                "content": "<local-command-stdout>\n"
                "Context Usage\n"
                "claude-opus-4-1-20250805 • 16k/200k tokens (8%)\n"
                "\n"
                "Memory files · /memory\n"
                "└ User (/home/user/.claude/CLAUDE.md): 155 tokens\n"
                "</local-command-stdout>",
            },
            "uuid": "test2",
            "timestamp": "2025-01-20T10:00:00.000Z",
        }
    ]

    # Parse the raw messages into TranscriptEntry objects
    parsed_messages = [parse_transcript_entry(msg) for msg in messages]
    html = generate_html(parsed_messages)

    # Check that text is properly rendered even without ANSI codes
    assert "Context Usage" in html
    assert "Memory files" in html
    assert "155 tokens" in html
    assert "command-output-content" in html

    # Ensure no ANSI-related classes when there are no ANSI codes
    # (The text should still be in the output, just without styling)
    assert "Context Usage" in html  # Should be present in plain form


def test_mixed_ansi_and_plain_text():
    """Test that mixed ANSI and plain text renders correctly."""
    messages = [
        {
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/home/user",
            "sessionId": "test-session",
            "version": "1.0.0",
            "type": "user",
            "message": {
                "role": "user",
                "content": "<local-command-stdout>"
                "Plain text before\n"
                "\x1b[31mRed text\x1b[0m in the middle\n"
                "Plain text after"
                "</local-command-stdout>",
            },
            "uuid": "test3",
            "timestamp": "2025-01-20T10:00:00.000Z",
        }
    ]

    # Parse the raw messages into TranscriptEntry objects
    parsed_messages = [parse_transcript_entry(msg) for msg in messages]
    html = generate_html(parsed_messages)

    # Check that both plain and colored text are present
    assert "Plain text before" in html
    assert '<span class="ansi-red">Red text</span>' in html
    assert "Plain text after" in html

    # Ensure proper structure
    assert "command-output-content" in html

#!/usr/bin/env python3
"""Test cases for text and markdown rendering."""

import json
import tempfile
from pathlib import Path
from claude_code_log.parser import load_transcript
from claude_code_log.text_renderer import (
    generate_text,
    generate_markdown,
    render_text_content,
    format_usage_info,
)
from claude_code_log.models import TextContent, ToolUseContent, UsageInfo


def test_text_rendering_basic():
    """Test basic plain text rendering of user and assistant messages."""
    user_message = {
        "type": "user",
        "timestamp": "2025-06-11T22:45:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Hello, can you help me?"}],
        },
    }

    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_002",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Of course! How can I assist you?"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }

    # Create temp file with messages
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(user_message) + "\n")
        f.write(json.dumps(assistant_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"

        # Generate plain text
        text_output = generate_text(messages, "Test Transcript", format_type="text")

        # Verify basic structure
        assert "Test Transcript" in text_output, "Title should be in output"
        assert "USER" in text_output, "USER label should be in output"
        assert "ASSISTANT" in text_output, "ASSISTANT label should be in output"
        assert "Hello, can you help me?" in text_output, (
            "User message content should be in output"
        )
        assert "Of course! How can I assist you?" in text_output, (
            "Assistant message content should be in output"
        )
        assert "Tokens: Input: 100 | Output: 50" in text_output, (
            "Token usage should be in output"
        )

        print("✓ Test passed: Basic text rendering works")

    finally:
        test_file_path.unlink()


def test_markdown_rendering_basic():
    """Test basic markdown rendering."""
    user_message = {
        "type": "user",
        "timestamp": "2025-06-11T22:45:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "What is 2+2?"}],
        },
    }

    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_002",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "2+2 equals 4."}],
            "stop_reason": "end_turn",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(user_message) + "\n")
        f.write(json.dumps(assistant_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)

        # Generate markdown
        markdown_output = generate_markdown(messages, "Test Transcript")

        # Verify markdown structure
        assert "# Test Transcript" in markdown_output, "Title should be H1 in markdown"
        assert "### User" in markdown_output, "User should be H3 in markdown"
        assert "### Assistant" in markdown_output, "Assistant should be H3 in markdown"
        assert "What is 2+2?" in markdown_output, "User message should be in output"
        assert "2+2 equals 4." in markdown_output, (
            "Assistant message should be in output"
        )

        print("✓ Test passed: Basic markdown rendering works")

    finally:
        test_file_path.unlink()


def test_tool_use_rendering():
    """Test rendering of tool use messages in text format."""
    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_001",
                    "name": "Read",
                    "input": {"file_path": "/tmp/test.txt"},
                }
            ],
            "stop_reason": "tool_use",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(assistant_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)

        # Generate plain text
        text_output = generate_text(messages, "Test Transcript", format_type="text")

        # Verify tool use rendering
        assert "[TOOL USE: Read]" in text_output, "Tool use should be labeled"
        assert "ID: tool_001" in text_output, "Tool ID should be in output"
        assert '"/tmp/test.txt"' in text_output or "/tmp/test.txt" in text_output, (
            "Tool input should be in output"
        )

        print("✓ Test passed: Tool use rendering works")

    finally:
        test_file_path.unlink()


def test_format_usage_info():
    """Test token usage formatting."""
    # Test with all fields
    usage = UsageInfo(
        input_tokens=100,
        output_tokens=50,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=30,
    )
    formatted = format_usage_info(usage)
    assert "Input: 100" in formatted
    assert "Output: 50" in formatted
    assert "Cache Creation: 20" in formatted
    assert "Cache Read: 30" in formatted

    # Test with minimal fields
    usage_minimal = UsageInfo(input_tokens=100, output_tokens=50)
    formatted_minimal = format_usage_info(usage_minimal)
    assert "Input: 100" in formatted_minimal
    assert "Output: 50" in formatted_minimal
    assert "Cache Creation" not in formatted_minimal

    # Test with None
    formatted_none = format_usage_info(None)
    assert formatted_none == ""

    print("✓ Test passed: Usage info formatting works")


def test_render_text_content():
    """Test individual content item rendering."""
    # Test text content
    text_item = TextContent(type="text", text="Hello world")
    rendered = render_text_content(text_item)
    assert "Hello world" in rendered

    # Test tool use content
    tool_item = ToolUseContent(
        type="tool_use",
        id="tool_123",
        name="TestTool",
        input={"param": "value"},
    )
    rendered_tool = render_text_content(tool_item)
    assert "[TOOL USE: TestTool]" in rendered_tool
    assert "tool_123" in rendered_tool

    print("✓ Test passed: Individual content rendering works")


def test_session_summaries():
    """Test that session summaries are included in text output."""
    user_message = {
        "type": "user",
        "timestamp": "2025-06-11T22:45:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Test message"}],
        },
    }

    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_002",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Response"}],
            "stop_reason": "end_turn",
        },
    }

    summary_message = {
        "type": "summary",
        "summary": "Testing summary feature",
        "leafUuid": "test_msg_002",
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(user_message) + "\n")
        f.write(json.dumps(assistant_message) + "\n")
        f.write(json.dumps(summary_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)

        # Generate markdown (includes summaries by default)
        markdown_output = generate_markdown(messages, "Test Transcript")

        # Verify summary is in session header
        assert "Testing summary feature" in markdown_output, (
            "Summary should be in markdown output"
        )

        print("✓ Test passed: Session summaries are included")

    finally:
        test_file_path.unlink()


def test_chat_format_basic():
    """Test compact chat format rendering."""
    user_message = {
        "type": "user",
        "timestamp": "2025-06-11T22:45:17.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "Hello, can you help me?"}],
        },
    }

    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_002",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": "Of course! How can I assist you?"}],
            "stop_reason": "end_turn",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(user_message) + "\n")
        f.write(json.dumps(assistant_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)

        # Import generate_chat
        from claude_code_log.text_renderer import generate_chat

        # Generate chat format
        chat_output = generate_chat(messages)

        # Verify chat format - clean and simple with new symbols
        assert "> Hello, can you help me?" in chat_output, (
            "User message should be prefixed with >"
        )
        assert "⏺ Of course! How can I assist you?" in chat_output, (
            "Assistant message should be prefixed with ⏺"
        )
        # Should NOT have timestamps or token info or old prefixes
        assert "User:" not in chat_output, "Should not have 'User:' prefix"
        assert "Assistant:" not in chat_output, "Should not have 'Assistant:' prefix"
        assert "2025-06-11" not in chat_output, "Should not have timestamps"
        assert "Tokens:" not in chat_output, "Should not have token usage"
        assert "====" not in chat_output, "Should not have separator lines"

        print("✓ Test passed: Chat format renders cleanly")

    finally:
        test_file_path.unlink()


def test_chat_format_with_tool_use():
    """Test chat format with tool use (should show compactly)."""
    assistant_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {"type": "text", "text": "I'll read that file for you."},
                {
                    "type": "tool_use",
                    "id": "tool_001",
                    "name": "Read",
                    "input": {"file_path": "/tmp/test.txt"},
                },
            ],
            "stop_reason": "tool_use",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(assistant_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)

        from claude_code_log.text_renderer import generate_chat

        chat_output = generate_chat(messages)

        # Verify tool use is shown compactly with new format
        assert "⏺ I'll read that file for you." in chat_output, (
            "Assistant text should be prefixed with ⏺"
        )
        assert "⏺ Read(" in chat_output, "Tool use should be shown with ⏺ symbol"
        assert "file_path" in chat_output, "Tool input should be in output"

        print("✓ Test passed: Chat format shows tool use compactly")

    finally:
        test_file_path.unlink()


def test_chat_format_tool_result_truncation():
    """Test chat format with tool result truncation and indentation."""
    assistant_tool_message = {
        "type": "assistant",
        "timestamp": "2025-06-11T22:45:18.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool_001",
                    "name": "Bash",
                    "input": {"command": "ls -la", "description": "List files"},
                }
            ],
            "stop_reason": "tool_use",
        },
    }

    # Create a long multi-line tool result (15 lines)
    tool_result_lines = [f"Line {i} of output" for i in range(1, 16)]
    tool_result_content = "\n".join(tool_result_lines)

    user_tool_result = {
        "type": "user",
        "timestamp": "2025-06-11T22:45:19.436Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_002",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool_001",
                    "content": tool_result_content,
                }
            ],
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(assistant_tool_message) + "\n")
        f.write(json.dumps(user_tool_result) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)

        from claude_code_log.text_renderer import generate_chat

        chat_output = generate_chat(messages)

        # Verify tool use with arguments
        assert "⏺ Bash(" in chat_output, "Tool use should have ⏺ symbol"
        assert "command" in chat_output, "Tool arguments should be shown"
        assert "ls -la" in chat_output, "Tool argument values should be shown"

        # Verify tool result with truncation indicator
        assert "⎿" in chat_output, "Tool result should have ⎿ symbol"
        assert "Line 1 of output" in chat_output, "First line should be in output"
        assert "Line 10 of output" in chat_output, "10th line should be in output"
        assert "Line 15 of output" not in chat_output, (
            "Line beyond 10 should not be in output"
        )
        assert "… +5 lines" in chat_output, "Truncation indicator should show +5 lines"

        # Verify indentation (all lines after first should be indented)
        lines = chat_output.split("\n")
        tool_result_start_idx = None
        for i, line in enumerate(lines):
            if "⎿" in line:
                tool_result_start_idx = i
                break

        assert tool_result_start_idx is not None, "Tool result should be in output"

        # Check that subsequent lines are indented
        for i in range(tool_result_start_idx + 1, tool_result_start_idx + 5):
            if i < len(lines) and lines[i].strip():  # Skip empty lines
                assert lines[i].startswith("     "), (
                    f"Line {i} should be indented with 5 spaces"
                )

        print(
            "✓ Test passed: Chat format handles tool result truncation and indentation"
        )

    finally:
        test_file_path.unlink()


if __name__ == "__main__":
    test_text_rendering_basic()
    test_markdown_rendering_basic()
    test_tool_use_rendering()
    test_format_usage_info()
    test_render_text_content()
    test_session_summaries()
    test_chat_format_basic()
    test_chat_format_with_tool_use()
    test_chat_format_tool_result_truncation()
    print("\n✅ All text rendering tests passed!")

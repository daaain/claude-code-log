#!/usr/bin/env python3
"""Test cases for different message types: summary, user, assistant, queue-operation."""

import json
import tempfile
from pathlib import Path
from claude_code_log.converter import (
    load_transcript,
    generate_html,
)
from claude_code_log.models import QueueOperationTranscriptEntry


def test_summary_type_support():
    """Test that summary type messages are properly handled."""
    summary_message = {
        "type": "summary",
        "summary": "User Initializes Project Documentation",
        "leafUuid": "test_msg_001",  # Should match a message UUID
    }

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
            "content": [{"type": "text", "text": "Hello, this is a test message."}],
        },
    }

    # Test loading summary messages
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(summary_message) + "\n")
        f.write(json.dumps(user_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"

        # Generate HTML
        html = generate_html(messages, "Test Transcript")

        # Summary should be attached to session header, not as separate message
        assert "User Initializes Project Documentation" in html, (
            "Summary text should be included"
        )
        assert "session-header" in html, (
            "Summary should appear in session header section"
        )

        print("✓ Test passed: Summary type messages are properly handled")

    finally:
        test_file_path.unlink()


def test_queue_operation_type_support():
    """Test that queue-operation type messages are properly parsed and not rendered."""
    enqueue_message = {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2025-11-08T15:16:08.703Z",
        "content": [
            {
                "type": "text",
                "text": "This is a queued message",
            }
        ],
        "sessionId": "test_session",
    }

    dequeue_message = {
        "type": "queue-operation",
        "operation": "dequeue",
        "timestamp": "2025-11-08T15:16:08.704Z",
        "sessionId": "test_session",
    }

    user_message = {
        "type": "user",
        "timestamp": "2025-11-08T15:16:08.711Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": "test_msg_001",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "This is a queued message"}],
        },
    }

    # Test loading queue-operation messages
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(enqueue_message) + "\n")
        f.write(json.dumps(dequeue_message) + "\n")
        f.write(json.dumps(user_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        # All 3 messages should be parsed
        assert len(messages) == 3, f"Expected 3 messages, got {len(messages)}"

        # Check that queue-operation messages were parsed correctly
        assert messages[0].type == "queue-operation", (
            "First message should be queue-operation"
        )
        assert isinstance(messages[0], QueueOperationTranscriptEntry)
        assert messages[0].operation == "enqueue", "First message should be enqueue"
        assert messages[0].content is not None, "Enqueue should have content"

        assert messages[1].type == "queue-operation", (
            "Second message should be queue-operation"
        )
        assert isinstance(messages[1], QueueOperationTranscriptEntry)
        assert messages[1].operation == "dequeue", "Second message should be dequeue"
        assert messages[1].content is None, "Dequeue should not have content"

        assert messages[2].type == "user", "Third message should be user"

        # Generate HTML - queue-operation messages should not appear in rendered output
        html = generate_html(messages, "Test Transcript")

        # Count how many times "This is a queued message" appears
        # It should appear only once (from user message, not from queue-operation)
        message_count = html.count("This is a queued message")
        assert message_count == 1, (
            f"Message should appear once (user message only), but appeared {message_count} times"
        )

        # Verify queue-operation messages don't create visible elements
        assert "queue-operation" not in html.lower(), (
            "Queue-operation should not appear in HTML"
        )

        print("✓ Test passed: Queue-operation messages are parsed but not rendered")

    finally:
        test_file_path.unlink()


if __name__ == "__main__":
    test_summary_type_support()
    test_queue_operation_type_support()
    print("\n✅ All message type tests passed!")

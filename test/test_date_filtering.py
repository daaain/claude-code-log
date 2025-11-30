#!/usr/bin/env python3
"""Test cases for date filtering functionality."""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from claude_code_log.converter import filter_messages_by_date, convert_jsonl_to_html
from claude_code_log.models import parse_transcript_entry


def create_test_message(timestamp_str: str, text: str) -> dict:
    """Create a test message with given timestamp and text."""
    return {
        "type": "user",
        "timestamp": timestamp_str,
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/tmp",
        "sessionId": "test_session",
        "version": "1.0.0",
        "uuid": f"test_msg_{timestamp_str}",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def test_date_filtering():
    """Test filtering messages by date range."""

    # Create test messages with different timestamps
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)
    three_days_ago = today - timedelta(days=3)

    message_dicts = [
        create_test_message(
            three_days_ago.isoformat() + "Z", "Message from 3 days ago"
        ),
        create_test_message(two_days_ago.isoformat() + "Z", "Message from 2 days ago"),
        create_test_message(yesterday.isoformat() + "Z", "Message from yesterday"),
        create_test_message(today.isoformat() + "Z", "Message from today"),
    ]

    # Parse dictionaries into TranscriptEntry objects
    messages = [parse_transcript_entry(msg_dict) for msg_dict in message_dicts]

    # Test filtering from yesterday onwards
    filtered = filter_messages_by_date(messages, "yesterday", None)
    assert len(filtered) == 2, (
        f"Expected 2 messages from yesterday onwards, got {len(filtered)}"
    )

    # Test filtering up to yesterday
    filtered = filter_messages_by_date(messages, None, "yesterday")
    assert len(filtered) >= 2, (
        f"Expected at least 2 messages up to yesterday, got {len(filtered)}"
    )

    # Test filtering for today only
    filtered = filter_messages_by_date(messages, "today", "today")
    assert len(filtered) == 1, f"Expected 1 message for today only, got {len(filtered)}"
    assert "Message from today" in str(filtered[0])

    # Test no filtering
    filtered = filter_messages_by_date(messages, None, None)
    assert len(filtered) == 4, (
        f"Expected all 4 messages when no filtering, got {len(filtered)}"
    )

    print("✓ Test passed: Date filtering works correctly")


def test_invalid_date_handling():
    """Test handling of invalid date strings."""
    messages = [
        parse_transcript_entry(
            create_test_message("2025-06-08T12:00:00Z", "Test message")
        )
    ]

    try:
        filter_messages_by_date(messages, "invalid-date", None)
        assert False, "Should have raised ValueError for invalid date"
    except ValueError as e:
        assert "Could not parse from-date" in str(e)
        print("✓ Test passed: Invalid from-date raises ValueError")

    try:
        filter_messages_by_date(messages, None, "another-invalid-date")
        assert False, "Should have raised ValueError for invalid date"
    except ValueError as e:
        assert "Could not parse to-date" in str(e)
        print("✓ Test passed: Invalid to-date raises ValueError")


def test_end_to_end_date_filtering():
    """Test end-to-end date filtering with JSONL file."""

    # Create test messages
    today = datetime.now()
    yesterday = today - timedelta(days=1)

    messages = [
        create_test_message(yesterday.isoformat() + "Z", "Yesterday's message"),
        create_test_message(today.isoformat() + "Z", "Today's message"),
    ]

    # Write to temporary JSONL file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        # Test conversion with date filtering
        with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as output_f:
            output_path = Path(output_f.name)

        # Filter for today only
        result_path = convert_jsonl_to_html(
            test_file_path, output_path, "today", "today"
        )

        # Check the generated HTML
        html_content = result_path.read_text(encoding="utf-8")

        # Should contain today's message (HTML escaped)
        assert "Today&#x27;s message" in html_content, (
            "HTML should contain today's message"
        )

        # Should NOT contain yesterday's message
        assert "Yesterday&#x27;s message" not in html_content, (
            "HTML should not contain yesterday's message"
        )

        # Should include date range in title
        assert "from today" in html_content and "to today" in html_content, (
            "HTML title should include date range"
        )

        print("✓ Test passed: End-to-end date filtering works")

    finally:
        # Clean up
        test_file_path.unlink()
        if output_path.exists():
            output_path.unlink()


def test_natural_language_dates():
    """Test various natural language date formats."""

    message_dict = create_test_message("2025-06-08T12:00:00Z", "Test message")
    messages = [parse_transcript_entry(message_dict)]

    # Test various natural language formats
    date_formats = ["today", "yesterday", "last week", "3 days ago", "1 week ago"]

    for date_format in date_formats:
        try:
            filter_messages_by_date(messages, date_format, None)
            print(f"✓ Successfully parsed date format: {date_format}")
        except ValueError:
            print(f"✗ Failed to parse date format: {date_format}")
            raise


if __name__ == "__main__":
    test_date_filtering()
    test_invalid_date_handling()
    test_end_to_end_date_filtering()
    test_natural_language_dates()
    print("\n✓ All date filtering tests passed!")

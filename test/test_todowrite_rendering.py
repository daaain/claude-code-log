#!/usr/bin/env python3
"""Test cases for TodoWrite tool rendering."""

import json
import tempfile
from pathlib import Path
import pytest
from claude_code_log.converter import convert_jsonl_to_html
from claude_code_log.renderer import format_todowrite_content
from claude_code_log.models import ToolUseContent


class TestTodoWriteRendering:
    """Test TodoWrite tool rendering functionality."""

    def test_format_todowrite_basic(self):
        """Test basic TodoWrite formatting with mixed statuses and priorities."""
        tool_use = ToolUseContent(
            type="tool_use",
            id="toolu_01test123",
            name="TodoWrite",
            input={
                "todos": [
                    {
                        "id": "1",
                        "content": "Implement user authentication",
                        "status": "completed",
                        "priority": "high",
                    },
                    {
                        "id": "2",
                        "content": "Add error handling",
                        "status": "in_progress",
                        "priority": "medium",
                    },
                    {
                        "id": "3",
                        "content": "Write documentation",
                        "status": "pending",
                        "priority": "low",
                    },
                ]
            },
        )

        html = format_todowrite_content(tool_use)

        # Check overall structure (TodoWrite now has streamlined format)
        assert 'class="todo-list"' in html
        # Title and ID are now in the message header, not in content
        assert "todo-item" in html

        # Check individual todo items
        assert "Implement user authentication" in html
        assert "Add error handling" in html
        assert "Write documentation" in html

        # Check status emojis
        assert "✅" in html  # completed
        assert "🔄" in html  # in_progress
        assert "⏳" in html  # pending

        # Check checkboxes
        assert 'type="checkbox"' in html
        assert "checked" in html  # for completed item
        assert "disabled" in html  # for completed item

        # Check CSS classes
        assert "todo-item completed high" in html
        assert "todo-item in_progress medium" in html
        assert "todo-item pending low" in html

        # Check IDs
        assert "#1" in html
        assert "#2" in html
        assert "#3" in html

    def test_format_todowrite_empty(self):
        """Test TodoWrite formatting with no todos."""
        tool_use = ToolUseContent(
            type="tool_use", id="toolu_empty", name="TodoWrite", input={"todos": []}
        )

        html = format_todowrite_content(tool_use)

        assert 'class="todo-content"' in html
        # Title and ID are now in the message header, not in content
        assert "No todos found" in html

    def test_format_todowrite_missing_todos(self):
        """Test TodoWrite formatting with missing todos field."""
        tool_use = ToolUseContent(
            type="tool_use", id="toolu_missing", name="TodoWrite", input={}
        )

        html = format_todowrite_content(tool_use)

        assert 'class="todo-content"' in html
        assert "No todos found" in html

    def test_format_todowrite_html_escaping(self):
        """Test that TodoWrite content is properly HTML escaped."""
        tool_use = ToolUseContent(
            type="tool_use",
            id="toolu_escape",
            name="TodoWrite",
            input={
                "todos": [
                    {
                        "id": "1",
                        "content": "Fix <script>alert('xss')</script> & \"quotes\"",
                        "status": "pending",
                        "priority": "high",
                    }
                ]
            },
        )

        html = format_todowrite_content(tool_use)

        # Check that HTML is escaped
        assert "&lt;script&gt;" in html
        assert "&amp;" in html
        assert "&quot;" in html
        # Should not contain unescaped HTML
        assert "<script>" not in html

    def test_format_todowrite_invalid_status_priority(self):
        """Test TodoWrite formatting with invalid status/priority values."""
        tool_use = ToolUseContent(
            type="tool_use",
            id="toolu_invalid",
            name="TodoWrite",
            input={
                "todos": [
                    {
                        "id": "1",
                        "content": "Test invalid values",
                        "status": "unknown_status",
                        "priority": "unknown_priority",
                    }
                ]
            },
        )

        html = format_todowrite_content(tool_use)

        # Should use default emojis for unknown values
        assert "⏳" in html  # default status emoji
        assert "Test invalid values" in html

    def test_todowrite_integration_with_full_message(self):
        """Test TodoWrite integration in full message rendering."""
        # Create a test message with TodoWrite tool use
        test_data = {
            "type": "assistant",
            "timestamp": "2025-06-14T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": "test_session",
            "version": "1.0.0",
            "uuid": "test_001",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_todowrite_test",
                        "name": "TodoWrite",
                        "input": {
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "Create new feature",
                                    "status": "in_progress",
                                    "priority": "high",
                                },
                                {
                                    "id": "2",
                                    "content": "Write tests",
                                    "status": "pending",
                                    "priority": "medium",
                                },
                            ]
                        },
                    }
                ],
                "stop_reason": None,
                "stop_sequence": None,
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            jsonl_file = temp_path / "todowrite_test.jsonl"

            with open(jsonl_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(test_data) + "\n")

            html_file = convert_jsonl_to_html(jsonl_file)
            html_content = html_file.read_text(encoding="utf-8")

            # Check TodoWrite specific rendering (now in message header)
            assert "📝 Todo List" in html_content  # in message header
            assert "Create new feature" in html_content
            assert "Write tests" in html_content
            assert "🔄" in html_content  # in_progress emoji
            assert "⏳" in html_content  # pending emoji
            assert (
                "class='message tool_use'" in html_content
            )  # tool as top-level message

            # Check CSS classes are applied
            assert "todo-item in_progress high" in html_content
            assert "todo-item pending medium" in html_content

    def test_todowrite_vs_regular_tool_use(self):
        """Test that TodoWrite is handled differently from regular tool use."""
        # Create regular tool use with longer content to ensure collapsible details
        long_content = (
            "This is a very long content that should definitely exceed 200 characters so that we can test the collapsible details functionality properly. "
            * 3
        )
        regular_tool = ToolUseContent(
            type="tool_use",
            id="toolu_regular",
            name="Edit",
            input={"file_path": "/tmp/test.py", "content": long_content},
        )

        # Create TodoWrite tool use
        todowrite_tool = ToolUseContent(
            type="tool_use",
            id="toolu_todowrite",
            name="TodoWrite",
            input={
                "todos": [
                    {
                        "id": "1",
                        "content": "Test todo",
                        "status": "pending",
                        "priority": "medium",
                    }
                ]
            },
        )

        # Test both through the main format function
        from claude_code_log.renderer import format_tool_use_content

        regular_html = format_tool_use_content(regular_tool)
        todowrite_html = format_tool_use_content(todowrite_tool)

        # Edit tool should use diff formatting (not table)
        assert "edit-diff" in regular_html
        # File path no longer in content, moved to message header
        # Tool name/ID no longer in content, moved to message header

        # TodoWrite should use special formatting
        assert "todo-list" in todowrite_html
        assert "todo-item" in todowrite_html
        assert "<details>" not in todowrite_html  # No collapsible details
        # Title/ID no longer in content, moved to message header

    def test_css_classes_inclusion(self):
        """Test that TodoWrite CSS classes are included in the template."""
        test_data = {
            "type": "assistant",
            "timestamp": "2025-06-14T10:00:00Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": "test_session",
            "version": "1.0.0",
            "uuid": "test_001",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-3-sonnet",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_todowrite_css",
                        "name": "TodoWrite",
                        "input": {
                            "todos": [
                                {
                                    "id": "1",
                                    "content": "CSS test todo",
                                    "status": "completed",
                                    "priority": "high",
                                }
                            ]
                        },
                    }
                ],
                "stop_reason": None,
                "stop_sequence": None,
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            jsonl_file = temp_path / "css_test.jsonl"

            with open(jsonl_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(test_data) + "\n")

            html_file = convert_jsonl_to_html(jsonl_file)
            html_content = html_file.read_text(encoding="utf-8")

            # Check that TodoWrite CSS is included
            assert ".todo-write" in html_content
            assert ".tool-header" in html_content
            assert ".todo-list" in html_content
            assert ".todo-item" in html_content
            assert ".todo-content" in html_content
            assert ".todo-status" in html_content
            assert ".todo-id" in html_content

            # Check priority-based CSS classes
            assert ".todo-item.high" in html_content
            assert ".todo-item.medium" in html_content
            assert ".todo-item.low" in html_content

            # Check status-based CSS classes
            assert ".todo-item.in_progress" in html_content
            assert ".todo-item.completed" in html_content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

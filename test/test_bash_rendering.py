#!/usr/bin/env python3
"""Test cases for bash command rendering functionality."""

import json
import tempfile
from pathlib import Path

from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html


def test_bash_input_rendering():
    """Test that bash input commands are rendered with proper styling."""
    bash_input_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:00Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_001",
        "message": {
            "role": "user",
            "content": "<bash-input>pwd</bash-input>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_input_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Test")

        # Check that bash prompt is rendered
        assert "❯" in html, "Bash prompt symbol should be in HTML"
        assert "bash-prompt" in html, "Bash prompt CSS class should be present"
        assert "bash-command" in html, "Bash command CSS class should be present"
        assert "pwd" in html, "The actual command should be visible"

        # Check that raw tags are not visible
        assert "<bash-input>" not in html, "Raw bash-input tags should not be visible"
        assert "</bash-input>" not in html, "Raw bash-input tags should not be visible"

    finally:
        test_file_path.unlink()


def test_bash_stdout_rendering():
    """Test that bash stdout is rendered properly."""
    bash_output_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:01Z",
        "parentUuid": "bash_001",
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_002",
        "message": {
            "role": "user",
            "content": "<bash-stdout>/home/user/documents</bash-stdout><bash-stderr></bash-stderr>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_output_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Output Test")

        # Check that stdout content is rendered
        assert "/home/user/documents" in html, "Stdout content should be visible"
        assert "bash-stdout" in html, "Bash stdout CSS class should be present"

        # Check that raw tags are not visible
        assert "<bash-stdout>" not in html, "Raw bash-stdout tags should not be visible"
        assert "</bash-stdout>" not in html, (
            "Raw bash-stdout tags should not be visible"
        )
        assert "<bash-stderr>" not in html, "Raw bash-stderr tags should not be visible"

    finally:
        test_file_path.unlink()


def test_bash_stderr_rendering():
    """Test that bash stderr is rendered with error styling."""
    bash_error_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:02Z",
        "parentUuid": "bash_001",
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_003",
        "message": {
            "role": "user",
            "content": "<bash-stdout></bash-stdout><bash-stderr>Error: Permission denied</bash-stderr>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_error_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Error Test")

        # Check that stderr content is rendered
        assert "Error: Permission denied" in html, "Stderr content should be visible"
        assert "bash-stderr" in html, "Bash stderr CSS class should be present"

        # Check that raw tags are not visible
        assert "<bash-stderr>" not in html, "Raw bash-stderr tags should not be visible"
        assert "</bash-stderr>" not in html, (
            "Raw bash-stderr tags should not be visible"
        )

    finally:
        test_file_path.unlink()


def test_bash_empty_output_rendering():
    """Test that empty bash output is handled gracefully."""
    bash_empty_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:03Z",
        "parentUuid": "bash_001",
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_004",
        "message": {
            "role": "user",
            "content": "<bash-stdout></bash-stdout><bash-stderr></bash-stderr>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_empty_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Empty Output Test")

        # Check that empty output is handled
        assert "(no output)" in html, "Empty output should show '(no output)' message"
        assert "bash-empty" in html, "Bash empty CSS class should be present"

    finally:
        test_file_path.unlink()


def test_bash_mixed_output_rendering():
    """Test that mixed stdout and stderr are both rendered."""
    bash_mixed_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:04Z",
        "parentUuid": "bash_001",
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_005",
        "message": {
            "role": "user",
            "content": "<bash-stdout>File created successfully</bash-stdout><bash-stderr>Warning: Overwriting existing file</bash-stderr>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_mixed_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Mixed Output Test")

        # Check that both stdout and stderr are rendered
        assert "File created successfully" in html, "Stdout content should be visible"
        assert "Warning: Overwriting existing file" in html, (
            "Stderr content should be visible"
        )
        assert "bash-stdout" in html, "Bash stdout CSS class should be present"
        assert "bash-stderr" in html, "Bash stderr CSS class should be present"

    finally:
        test_file_path.unlink()


def test_bash_complex_command_rendering():
    """Test rendering of complex bash commands with special characters."""
    bash_complex_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:05Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_006",
        "message": {
            "role": "user",
            "content": "<bash-input>find . -name '*.py' | xargs grep -l 'TODO' > todo_files.txt</bash-input>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_complex_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Complex Command Test")

        # Check that complex command is properly escaped and rendered
        assert "find . -name" in html, "Complex command should be visible"
        assert "*.py" in html or "&#x27;*.py&#x27;" in html, (
            "File pattern should be visible (possibly escaped)"
        )
        assert "xargs grep" in html, "Pipe commands should be visible"
        assert "todo_files.txt" in html, "Output redirect should be visible"

    finally:
        test_file_path.unlink()


def test_bash_multiline_output_rendering():
    """Test rendering of multiline bash output."""
    bash_multiline_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:06Z",
        "parentUuid": "bash_001",
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_007",
        "message": {
            "role": "user",
            "content": "<bash-stdout>file1.txt\nfile2.txt\nsubdir/\n  nested_file.txt\n  another_file.txt</bash-stdout><bash-stderr></bash-stderr>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_multiline_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash Multiline Output Test")

        # Check that multiline content is preserved
        assert "file1.txt" in html, "First line should be visible"
        assert "file2.txt" in html, "Second line should be visible"
        assert "nested_file.txt" in html, "Nested content should be visible"

        # Check that content is in a <pre> tag for formatting
        assert "<pre" in html, "Output should be in a pre tag"

    finally:
        test_file_path.unlink()


def test_bash_css_styles_included():
    """Test that bash-specific CSS styles are included in the HTML."""
    bash_message = {
        "type": "user",
        "timestamp": "2025-06-11T10:00:00Z",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "human",
        "cwd": "/home/user",
        "sessionId": "test_bash",
        "version": "1.0.0",
        "uuid": "bash_008",
        "message": {
            "role": "user",
            "content": "<bash-input>echo 'test'</bash-input>",
        },
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps(bash_message) + "\n")
        f.flush()
        test_file_path = Path(f.name)

    try:
        messages = load_transcript(test_file_path)
        html = generate_html(messages, "Bash CSS Test")

        # Check that CSS classes are defined
        assert ".bash-input" in html, "Bash input CSS should be defined"
        assert ".bash-prompt" in html, "Bash prompt CSS should be defined"
        assert ".bash-command" in html, "Bash command CSS should be defined"
        assert ".bash-output" in html, "Bash output CSS should be defined"
        assert ".bash-stdout" in html, "Bash stdout CSS should be defined"
        assert ".bash-stderr" in html, "Bash stderr CSS should be defined"
        assert ".bash-empty" in html, "Bash empty CSS should be defined"

    finally:
        test_file_path.unlink()


if __name__ == "__main__":
    # Run all tests
    test_bash_input_rendering()
    test_bash_stdout_rendering()
    test_bash_stderr_rendering()
    test_bash_empty_output_rendering()
    test_bash_mixed_output_rendering()
    test_bash_complex_command_rendering()
    test_bash_multiline_output_rendering()
    test_bash_css_styles_included()
    print("✅ All bash rendering tests passed!")

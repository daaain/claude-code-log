#!/usr/bin/env python3
"""Tests for sidechain agent insertion and deduplication functionality."""

import tempfile
from pathlib import Path


from claude_code_log.parser import load_transcript
from claude_code_log.renderer import generate_html


def test_agent_insertion():
    """Test that agent messages are inserted after their referencing tool result."""
    # Create test data files
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Write main transcript
        main_file = tmpdir_path / "main.jsonl"
        main_file.write_text(
            (Path(__file__).parent / "test_data" / "sidechain_main.jsonl").read_text()
        )

        # Write agent transcript (must match agentId in main file)
        agent_file = tmpdir_path / "agent-e1c84ba5.jsonl"
        agent_file.write_text(
            (Path(__file__).parent / "test_data" / "sidechain_agent.jsonl").read_text()
        )

        # Load transcript with agent insertion (agent files discovered automatically)
        messages = load_transcript(main_file)

        # Verify agent messages were inserted (3 main + 3 agent = 6 total)
        assert len(messages) == 6, f"Expected 6 messages, got {len(messages)}"

        # Find the tool result message (uuid 97965533-18f3-41b7-aa07-0c438f2a5893)
        tool_result_idx = next(
            i
            for i, msg in enumerate(messages)
            if getattr(msg, "uuid", None) == "97965533-18f3-41b7-aa07-0c438f2a5893"
        )

        # Verify agent messages come right after tool result
        # The agent messages should be inserted between tool_result and next main message
        assert tool_result_idx + 3 < len(messages), "Agent messages should be inserted"
        # Verify at least one message after tool result is from sidechain
        assert any(
            getattr(messages[i], "isSidechain", False)
            for i in range(tool_result_idx + 1, len(messages))
        )


def test_deduplication_task_result_vs_sidechain():
    """Test that sidechain assistant final message is deduplicated when it matches Task result."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Write deduplication test data
        main_file = tmpdir_path / "main.jsonl"
        main_file.write_text(
            (Path(__file__).parent / "test_data" / "dedup_main.jsonl").read_text()
        )

        agent_file = tmpdir_path / "agent-e1c84ba5.jsonl"
        agent_file.write_text(
            (Path(__file__).parent / "test_data" / "dedup_agent.jsonl").read_text()
        )

        # Load and render (agent files discovered automatically)
        messages = load_transcript(main_file)
        html = generate_html(messages, title="Test")

        # Verify deduplication occurred:
        # The sidechain assistant's final message should be replaced with a forward link
        assert "(Task summary" in html
        assert "already displayed in" in html
        assert "Task tool result above" in html

        # The actual content "I created the test file successfully" should only appear once
        # in the Task result, not in the sidechain assistant
        content_count = html.count("I created the test file successfully")
        assert content_count == 1, (
            f"Expected content to appear once, found {content_count} times"
        )


def test_no_deduplication_when_content_different():
    """Test that deduplication doesn't occur when Task result and sidechain differ."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create test data with different content
        # Note: agentId is just "ghi789", the filename is "agent-ghi789.jsonl"
        main_file = tmpdir_path / "main.jsonl"
        main_file.write_text(
            '{"parentUuid":null,"isSidechain":false,"userType":"external","cwd":"e:\\\\test","sessionId":"test-3","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"text","text":"Do something"}]},"uuid":"d-0","timestamp":"2025-01-15T12:00:00.000Z"}\n'
            '{"parentUuid":"d-0","isSidechain":false,"userType":"external","cwd":"e:\\\\test","sessionId":"test-3","version":"2.0.46","gitBranch":"main","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_01test1","type":"message","role":"assistant","content":[{"type":"tool_use","id":"task-3","name":"Task","input":{"prompt":"Do it"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":20}},"requestId":"req_01test1","type":"assistant","uuid":"d-1","timestamp":"2025-01-15T12:00:05.000Z"}\n'
            '{"parentUuid":"d-1","isSidechain":false,"userType":"external","cwd":"e:\\\\test","sessionId":"test-3","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"task-3","content":"Done A"}]},"uuid":"d-2","timestamp":"2025-01-15T12:00:15.000Z","toolUseResult":{"agentId":"ghi789","content":"Done A"},"agentId":"ghi789"}\n'
        )

        agent_file = tmpdir_path / "agent-ghi789.jsonl"
        agent_file.write_text(
            '{"parentUuid":null,"isSidechain":true,"userType":"external","cwd":"e:\\\\test","sessionId":"test-3","version":"2.0.46","gitBranch":"main","agentId":"ghi789","type":"user","message":{"role":"user","content":[{"type":"text","text":"Do it"}]},"uuid":"agent-d-0","timestamp":"2025-01-15T12:00:06.000Z"}\n'
            '{"parentUuid":"agent-d-0","isSidechain":true,"userType":"external","cwd":"e:\\\\test","sessionId":"test-3","version":"2.0.46","gitBranch":"main","agentId":"ghi789","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_01testagent1","type":"message","role":"assistant","content":[{"type":"text","text":"Done B"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":5,"output_tokens":10}},"requestId":"req_01testagent1","type":"assistant","uuid":"agent-d-1","timestamp":"2025-01-15T12:00:14.000Z"}\n'
        )

        messages = load_transcript(main_file)
        html = generate_html(messages, title="Test")

        # No deduplication should occur - both "Done A" and "Done B" should appear
        assert "Done A" in html
        assert "Done B" in html
        assert "(Task summary" not in html


def test_agent_messages_marked_as_sidechain():
    """Test that agent messages are properly marked with sidechain class."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        main_file = tmpdir_path / "main.jsonl"
        main_file.write_text(
            (Path(__file__).parent / "test_data" / "sidechain_main.jsonl").read_text()
        )

        agent_file = tmpdir_path / "agent-e1c84ba5.jsonl"
        agent_file.write_text(
            (Path(__file__).parent / "test_data" / "sidechain_agent.jsonl").read_text()
        )

        messages = load_transcript(main_file)
        html = generate_html(messages, title="Test")

        # Agent messages should have sidechain class
        assert (
            "class='message assistant sidechain" in html
            or "class='message user sidechain" in html
        )

        # Verify sidechain messages are indented (have ancestry classes)
        assert (
            "d-2" in html
        )  # Tool result ID should appear in ancestry for sidechain messages


def test_sidechain_tool_results_rendered():
    """Test that tool results from sidechain agents are rendered (not skipped).

    Regression test for bug where all sidechain user messages were skipped,
    which incorrectly also skipped tool results that come in user messages.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create a main file with Task tool use and result
        main_file = tmpdir_path / "main.jsonl"
        main_file.write_text(
            '{"parentUuid":null,"isSidechain":false,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"text","text":"Search for files"}]},"uuid":"u-0","timestamp":"2025-01-15T14:00:00.000Z"}\n'
            '{"parentUuid":"u-0","isSidechain":false,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_main","type":"message","role":"assistant","content":[{"type":"tool_use","id":"task-glob","name":"Task","input":{"prompt":"Find test files"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":100,"output_tokens":50}},"requestId":"req_main","type":"assistant","uuid":"a-0","timestamp":"2025-01-15T14:00:05.000Z"}\n'
            '{"parentUuid":"a-0","isSidechain":false,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"task-glob","content":"Found files: test.py"}]},"uuid":"u-1","timestamp":"2025-01-15T14:00:20.000Z","toolUseResult":{"agentId":"glob-agent","content":"Found files: test.py"},"agentId":"glob-agent"}\n'
        )

        # Create agent file with tool use (Glob) and its result
        agent_file = tmpdir_path / "agent-glob-agent.jsonl"
        agent_file.write_text(
            # Sidechain user prompt (should be skipped in output)
            '{"parentUuid":null,"isSidechain":true,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","agentId":"glob-agent","type":"user","message":{"role":"user","content":[{"type":"text","text":"Find test files"}]},"uuid":"agent-u-0","timestamp":"2025-01-15T14:00:06.000Z"}\n'
            # Assistant uses Glob tool
            '{"parentUuid":"agent-u-0","isSidechain":true,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","agentId":"glob-agent","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_agent","type":"message","role":"assistant","content":[{"type":"tool_use","id":"glob-123","name":"Glob","input":{"pattern":"**/*.py"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":50,"output_tokens":25}},"requestId":"req_agent","type":"assistant","uuid":"agent-a-0","timestamp":"2025-01-15T14:00:08.000Z"}\n'
            # Tool result comes in a sidechain user message - THIS SHOULD BE RENDERED
            '{"parentUuid":"agent-a-0","isSidechain":true,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","agentId":"glob-agent","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"glob-123","content":"/workspace/test.py"}]},"uuid":"agent-u-1","timestamp":"2025-01-15T14:00:10.000Z"}\n'
            # Final assistant message
            '{"parentUuid":"agent-u-1","isSidechain":true,"userType":"external","cwd":"/workspace","sessionId":"test-tool-results","version":"2.0.46","gitBranch":"main","agentId":"glob-agent","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_agent_final","type":"message","role":"assistant","content":[{"type":"text","text":"Found files: test.py"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":60,"output_tokens":15}},"requestId":"req_agent_final","type":"assistant","uuid":"agent-a-1","timestamp":"2025-01-15T14:00:15.000Z"}\n'
        )

        messages = load_transcript(main_file)
        html = generate_html(messages, title="Test")

        # The sidechain Glob tool use should be rendered
        assert "🛠️ Glob" in html or "Glob" in html

        # The sidechain tool RESULT should be rendered (was the bug - it was skipped)
        # The content "/workspace/test.py" should appear in the output
        assert "/workspace/test.py" in html, (
            "Sidechain tool result content should be rendered"
        )

        # The sidechain prompt text "Find test files" should NOT be rendered
        # as a separate user message (only the tool results should come through)
        # No sidechain user messages with text content should be rendered
        # (tool results are rendered as tool_result, not as user messages)
        assert (
            "<div class='content'><p>Find test files</p></div>" not in html
            or "class='message user sidechain" not in html
        ), "Sidechain user prompts should be skipped"


def test_multiple_agent_invocations():
    """Test handling of multiple Task invocations in same session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create scenario with two Task invocations
        main_file = tmpdir_path / "main.jsonl"
        main_file.write_text(
            '{"parentUuid":null,"isSidechain":false,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"text","text":"Do two things"}]},"uuid":"d-0","timestamp":"2025-01-15T13:00:00.000Z"}\n'
            '{"parentUuid":"d-0","isSidechain":false,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_01","type":"message","role":"assistant","content":[{"type":"tool_use","id":"task-4a","name":"Task","input":{"prompt":"First task"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":100,"output_tokens":50}},"requestId":"req_01","type":"assistant","uuid":"d-1","timestamp":"2025-01-15T13:00:05.000Z"}\n'
            '{"parentUuid":"d-1","isSidechain":false,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"task-4a","content":"First done"}]},"uuid":"d-2","timestamp":"2025-01-15T13:00:15.000Z","toolUseResult":{"status":"completed","agentId":"first","content":[{"type":"text","text":"First done"}]},"agentId":"first"}\n'
            '{"parentUuid":"d-2","isSidechain":false,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_02","type":"message","role":"assistant","content":[{"type":"tool_use","id":"task-4b","name":"Task","input":{"prompt":"Second task"}}],"stop_reason":"tool_use","stop_sequence":null,"usage":{"input_tokens":150,"output_tokens":60}},"requestId":"req_02","type":"assistant","uuid":"d-3","timestamp":"2025-01-15T13:00:20.000Z"}\n'
            '{"parentUuid":"d-3","isSidechain":false,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"task-4b","content":"Second done"}]},"uuid":"d-4","timestamp":"2025-01-15T13:00:30.000Z","toolUseResult":{"status":"completed","agentId":"second","content":[{"type":"text","text":"Second done"}]},"agentId":"second"}\n'
        )

        (tmpdir_path / "agent-first.jsonl").write_text(
            '{"parentUuid":null,"isSidechain":true,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","agentId":"first","type":"user","message":{"role":"user","content":[{"type":"text","text":"First task"}]},"uuid":"agent-d-0","timestamp":"2025-01-15T13:00:06.000Z"}\n'
            '{"parentUuid":"agent-d-0","isSidechain":true,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","agentId":"first","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_agent_01","type":"message","role":"assistant","content":[{"type":"text","text":"First done"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":50,"output_tokens":25}},"requestId":"req_agent_01","type":"assistant","uuid":"agent-d-1","timestamp":"2025-01-15T13:00:14.000Z"}\n'
        )

        (tmpdir_path / "agent-second.jsonl").write_text(
            '{"parentUuid":null,"isSidechain":true,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","agentId":"second","type":"user","message":{"role":"user","content":[{"type":"text","text":"Second task"}]},"uuid":"agent2-d-0","timestamp":"2025-01-15T13:00:21.000Z"}\n'
            '{"parentUuid":"agent2-d-0","isSidechain":true,"userType":"external","cwd":"/workspace/test","sessionId":"test-4","version":"2.0.46","gitBranch":"main","agentId":"second","message":{"model":"claude-sonnet-4-5-20250929","id":"msg_agent_02","type":"message","role":"assistant","content":[{"type":"text","text":"Second done"}],"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":55,"output_tokens":30}},"requestId":"req_agent_02","type":"assistant","uuid":"agent2-d-1","timestamp":"2025-01-15T13:00:29.000Z"}\n'
        )

        messages = load_transcript(main_file)

        # Should have 5 main + 2 + 2 agent messages = 9 total
        assert len(messages) == 9

        # Verify both agent sequences are inserted correctly
        html = generate_html(messages, title="Test")
        assert "First done" in html
        assert "Second done" in html

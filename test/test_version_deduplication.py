#!/usr/bin/env python3
"""Tests for version-based deduplication during Claude Code upgrades."""

from datetime import datetime
from claude_code_log.models import (
    AssistantTranscriptEntry,
    AssistantMessageModel,
    SummaryTranscriptEntry,
    ThinkingContent,
    TranscriptEntry,
    UserTranscriptEntry,
    UserMessageModel,
    ToolUseContent,
    ToolResultContent,
)
from claude_code_log.converter import deduplicate_messages
from claude_code_log.html.renderer import generate_html


class TestVersionDeduplication:
    """Test that duplicate messages from version upgrades are deduplicated."""

    def test_assistant_message_deduplication(self):
        """Test deduplication of assistant messages by version."""
        timestamp = datetime.now().isoformat()

        # Same assistant message in two different Claude Code versions
        msg_v1 = AssistantTranscriptEntry(
            type="assistant",
            uuid="uuid-v1",
            parentUuid="parent-001",
            timestamp=timestamp,
            version="2.0.31",  # Older version
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=AssistantMessageModel(
                id="msg_duplicate",
                type="message",
                role="assistant",
                model="claude-sonnet-4-5",
                content=[
                    ToolUseContent(
                        type="tool_use",
                        id="toolu_edit",
                        name="Edit",
                        input={
                            "file_path": "/test/file.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    ),
                ],
                stop_reason="tool_use",
            ),
        )

        msg_v2 = AssistantTranscriptEntry(
            type="assistant",
            uuid="uuid-v2",
            parentUuid="parent-002",
            timestamp=timestamp,
            version="2.0.34",  # Newer version
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=AssistantMessageModel(
                id="msg_duplicate",  # SAME message.id
                type="message",
                role="assistant",
                model="claude-sonnet-4-5",
                content=[
                    ToolUseContent(
                        type="tool_use",
                        id="toolu_edit",
                        name="Edit",
                        input={
                            "file_path": "/test/file.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                    ),
                ],
                stop_reason="tool_use",
            ),
        )

        # Test both orderings
        for messages in [[msg_v1, msg_v2], [msg_v2, msg_v1]]:
            deduped = deduplicate_messages(messages)
            html = generate_html(deduped, "Version Test")

            # Should appear only once
            tool_summary_count = html.count(
                "<span class='tool-summary'>/test/file.py</span>"
            )
            assert tool_summary_count == 1, (
                f"Expected 1 tool summary, got {tool_summary_count}"
            )

    def test_tool_result_deduplication(self):
        """Test deduplication of tool results by version."""
        timestamp = datetime.now().isoformat()

        # Same tool result in two different Claude Code versions
        result_v1 = UserTranscriptEntry(
            type="user",
            uuid="uuid-result-v1",
            parentUuid="parent-001",
            timestamp=timestamp,
            version="2.0.31",  # Older version
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=UserMessageModel(
                role="user",
                content=[
                    ToolResultContent(
                        type="tool_result",
                        tool_use_id="toolu_read_test",
                        content="File contents here",
                    )
                ],
            ),
        )

        result_v2 = UserTranscriptEntry(
            type="user",
            uuid="uuid-result-v2",
            parentUuid="parent-002",
            timestamp=timestamp,
            version="2.0.34",  # Newer version
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=UserMessageModel(
                role="user",
                content=[
                    ToolResultContent(
                        type="tool_result",
                        tool_use_id="toolu_read_test",  # SAME tool_use_id
                        content="File contents here",
                    )
                ],
            ),
        )

        # Test both orderings
        for messages in [[result_v1, result_v2], [result_v2, result_v1]]:
            deduped = deduplicate_messages(messages)
            html = generate_html(deduped, "Tool Result Test")

            # Should appear only once
            content_count = html.count("File contents here")
            assert content_count == 1, f"Expected 1 tool result, got {content_count}"

    def test_full_stutter_pair(self):
        """Test complete assistant+tool_result pair deduplication."""
        timestamp = datetime.now().isoformat()

        # Version 2.0.31 pair
        assist_v1 = AssistantTranscriptEntry(
            type="assistant",
            uuid="assist-v1",
            parentUuid="parent-001",
            timestamp=timestamp,
            version="2.0.31",
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=AssistantMessageModel(
                id="msg_full_test",
                type="message",
                role="assistant",
                model="claude-sonnet-4-5",
                content=[
                    ToolUseContent(
                        type="tool_use",
                        id="toolu_full_test",
                        name="Read",
                        input={"file_path": "/test/data.txt"},
                    ),
                ],
                stop_reason="tool_use",
            ),
        )

        result_v1 = UserTranscriptEntry(
            type="user",
            uuid="result-v1",
            parentUuid="assist-v1",
            timestamp=timestamp,
            version="2.0.31",
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=UserMessageModel(
                role="user",
                content=[
                    ToolResultContent(
                        type="tool_result",
                        tool_use_id="toolu_full_test",
                        content="Data content",
                    )
                ],
            ),
        )

        # Version 2.0.34 pair (same IDs)
        assist_v2 = AssistantTranscriptEntry(
            type="assistant",
            uuid="assist-v2",
            parentUuid="parent-002",
            timestamp=timestamp,
            version="2.0.34",
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=AssistantMessageModel(
                id="msg_full_test",  # SAME
                type="message",
                role="assistant",
                model="claude-sonnet-4-5",
                content=[
                    ToolUseContent(
                        type="tool_use",
                        id="toolu_full_test",  # SAME
                        name="Read",
                        input={"file_path": "/test/data.txt"},
                    ),
                ],
                stop_reason="tool_use",
            ),
        )

        result_v2 = UserTranscriptEntry(
            type="user",
            uuid="result-v2",
            parentUuid="assist-v2",
            timestamp=timestamp,
            version="2.0.34",
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=UserMessageModel(
                role="user",
                content=[
                    ToolResultContent(
                        type="tool_result",
                        tool_use_id="toolu_full_test",  # SAME
                        content="Data content",
                    )
                ],
            ),
        )

        # Combine: v1 pair, then v2 pair
        messages = [assist_v1, result_v1, assist_v2, result_v2]
        deduped = deduplicate_messages(messages)
        html = generate_html(deduped, "Full Pair Test")

        # Each should appear only once
        file_path_count = html.count("/test/data.txt")
        assert file_path_count == 1, f"Expected 1 file path, got {file_path_count}"

        content_count = html.count("Data content")
        assert content_count == 1, f"Expected 1 data content, got {content_count}"

    def test_user_text_messages_with_different_uuids_not_deduped(self):
        """User text messages with different UUIDs are distinct DAG nodes.

        Even at the same timestamp, user text messages with different UUIDs
        must be preserved to maintain DAG parent references.
        """
        from claude_code_log.models import TextContent

        timestamp = "2025-11-13T11:44:08.771Z"

        msg1 = UserTranscriptEntry(
            type="user",
            uuid="uuid-msg1",
            parentUuid="parent-001",
            timestamp=timestamp,
            version="2.0.37",
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=UserMessageModel(
                role="user",
                content=[
                    TextContent(type="text", text="First message"),
                ],
            ),
        )

        msg2 = UserTranscriptEntry(
            type="user",
            uuid="uuid-msg2",
            parentUuid="parent-002",
            timestamp=timestamp,
            version="2.0.37",
            isSidechain=False,
            userType="external",
            cwd="/test",
            sessionId="session-test",
            message=UserMessageModel(
                role="user",
                content=[
                    TextContent(type="text", text="Second message"),
                ],
            ),
        )

        deduped = deduplicate_messages([msg1, msg2])
        assert len(deduped) == 2, "Different UUIDs should not be deduped"


def _make_assistant(
    uuid: str,
    parent_uuid: str | None,
    timestamp: str,
    content: list,
    message_id: str = "msg_stream",
    session_id: str = "session-test",
) -> AssistantTranscriptEntry:
    """Build a minimal assistant entry for dedup DAG tests."""
    return AssistantTranscriptEntry(
        type="assistant",
        uuid=uuid,
        parentUuid=parent_uuid,
        timestamp=timestamp,
        version="2.1.198",
        isSidechain=False,
        userType="external",
        cwd="/test",
        sessionId=session_id,
        message=AssistantMessageModel(
            id=message_id,
            type="message",
            role="assistant",
            model="claude-fable-5",
            content=content,
            stop_reason=None,
        ),
    )


def _empty_thinking() -> list:
    return [ThinkingContent(type="thinking", thinking="", signature="sig")]


def _uuids(entries: list[TranscriptEntry]) -> list["str | None"]:
    return [getattr(e, "uuid", None) for e in entries]


def _assert_no_dangling_parents(
    original: list[TranscriptEntry], deduped: list[TranscriptEntry]
) -> None:
    """No surviving parentUuid may reference a dropped entry (issue #259).

    Parents outside the loaded slice (never in ``original``) are fine — the
    DAG layer handles those; dedup must only not create NEW dangling refs.
    """
    loaded = {getattr(e, "uuid", None) for e in original if getattr(e, "uuid", None)}
    surviving = {getattr(e, "uuid", None) for e in deduped if getattr(e, "uuid", None)}
    dropped = loaded - surviving
    for entry in deduped:
        parent = getattr(entry, "parentUuid", None)
        assert parent not in dropped, (
            f"Entry {getattr(entry, 'uuid', '?')} references dropped parent {parent}"
        )


class TestDedupDagRemap:
    """Dedup must not orphan children of dropped entries (issue #259).

    Newer Claude Code streams one API response as several assistant entries
    sharing one message.id, and can emit two consecutive empty thinking
    blocks within the same millisecond. The second is dropped as a version
    stutter — its child's parentUuid must be remapped to the survivor.
    """

    def test_empty_thinking_stutter_reparents_child(self):
        """The issue #259 trigger: same-timestamp empty-thinking pair + tool_use child."""
        ts = "2026-07-01T17:16:21.820Z"
        first = _make_assistant("uuid-think-1", "uuid-turn-root", ts, _empty_thinking())
        second = _make_assistant("uuid-think-2", "uuid-think-1", ts, _empty_thinking())
        child = _make_assistant(
            "uuid-tool-use",
            "uuid-think-2",
            "2026-07-01T17:16:22.717Z",
            [
                ToolUseContent(
                    type="tool_use",
                    id="toolu_x",
                    name="Read",
                    input={"file_path": "/f"},
                )
            ],
        )

        original = [first, second, child]
        deduped = deduplicate_messages(original)

        uuids = _uuids(deduped)
        assert "uuid-think-1" in uuids, "Survivor must be kept"
        assert "uuid-think-2" not in uuids, "Same-key stutter must still be dropped"
        assert child.parentUuid == "uuid-think-1", (
            "Child of the dropped entry must be re-parented to the survivor, "
            f"got {child.parentUuid}"
        )
        _assert_no_dangling_parents(original, deduped)

    def test_empty_thinking_run_merged_across_timestamps(self):
        """Consecutive empty-thinking entries merge even with distinct timestamps.

        They carry no renderable content and essentially never become fork
        points a posteriori, so the run collapses to its first entry and
        children re-parent to it (issue #259 follow-up comment).
        """
        first = _make_assistant(
            "uuid-think-1",
            "uuid-turn-root",
            "2026-07-01T17:16:21.820Z",
            _empty_thinking(),
        )
        second = _make_assistant(
            "uuid-think-2",
            "uuid-think-1",
            "2026-07-01T17:16:21.950Z",
            _empty_thinking(),
        )
        third = _make_assistant(
            "uuid-think-3",
            "uuid-think-2",
            "2026-07-01T17:16:22.100Z",
            _empty_thinking(),
        )
        child = _make_assistant(
            "uuid-tool-use",
            "uuid-think-3",
            "2026-07-01T17:16:22.717Z",
            [
                ToolUseContent(
                    type="tool_use",
                    id="toolu_x",
                    name="Read",
                    input={"file_path": "/f"},
                )
            ],
        )

        original = [first, second, third, child]
        deduped = deduplicate_messages(original)

        uuids = _uuids(deduped)
        assert uuids == ["uuid-think-1", "uuid-tool-use"], (
            f"Empty-thinking run should collapse to its head, got {uuids}"
        )
        assert child.parentUuid == "uuid-think-1"
        _assert_no_dangling_parents(original, deduped)

    def test_non_empty_thinking_not_merged(self):
        """Distinct-timestamp thinking entries with real content stay separate."""
        first = _make_assistant(
            "uuid-think-1",
            "uuid-turn-root",
            "2026-07-01T17:16:21.820Z",
            [
                ThinkingContent(
                    type="thinking", thinking="Real reasoning", signature="s"
                )
            ],
        )
        second = _make_assistant(
            "uuid-think-2",
            "uuid-think-1",
            "2026-07-01T17:16:21.950Z",
            [
                ThinkingContent(
                    type="thinking", thinking="More reasoning", signature="s"
                )
            ],
        )

        deduped = deduplicate_messages([first, second])
        assert _uuids(deduped) == ["uuid-think-1", "uuid-think-2"]

    def test_empty_thinking_not_merged_across_sessions(self):
        """Session boundaries block the merge — fork attachment points must survive."""
        first = _make_assistant(
            "uuid-think-1",
            None,
            "2026-07-01T17:16:21.820Z",
            _empty_thinking(),
            session_id="session-a",
        )
        second = _make_assistant(
            "uuid-think-2",
            "uuid-think-1",
            "2026-07-01T17:16:21.950Z",
            _empty_thinking(),
            message_id="msg_other",
            session_id="session-b",
        )

        deduped = deduplicate_messages([first, second])
        assert _uuids(deduped) == ["uuid-think-1", "uuid-think-2"]

    def test_summary_leaf_uuid_remapped(self):
        """A summary pointing at a dropped entry re-attaches to the survivor."""
        ts = "2026-07-01T17:16:21.820Z"
        first = _make_assistant("uuid-think-1", "uuid-turn-root", ts, _empty_thinking())
        second = _make_assistant("uuid-think-2", "uuid-think-1", ts, _empty_thinking())
        summary = SummaryTranscriptEntry(
            type="summary", summary="Session about X", leafUuid="uuid-think-2"
        )

        deduplicate_messages([first, second, summary])
        assert summary.leafUuid == "uuid-think-1"

    def test_version_stutter_children_reparented(self):
        """The pre-existing stutter case also benefits: children of the dropped
        copy re-parent to the surviving copy instead of dangling."""
        ts = "2026-07-01T10:00:00.000Z"
        v1 = _make_assistant(
            "uuid-v1",
            "parent-001",
            ts,
            [
                ToolUseContent(
                    type="tool_use",
                    id="toolu_e",
                    name="Edit",
                    input={"file_path": "/f"},
                )
            ],
            message_id="msg_dup",
        )
        v2 = _make_assistant(
            "uuid-v2",
            "parent-002",
            ts,
            [
                ToolUseContent(
                    type="tool_use",
                    id="toolu_e",
                    name="Edit",
                    input={"file_path": "/f"},
                )
            ],
            message_id="msg_dup",
        )
        child_of_v2 = _make_assistant(
            "uuid-child",
            "uuid-v2",
            "2026-07-01T10:00:01.000Z",
            [
                ToolUseContent(
                    type="tool_use",
                    id="toolu_n",
                    name="Read",
                    input={"file_path": "/g"},
                )
            ],
            message_id="msg_next",
        )

        original = [v1, v2, child_of_v2]
        deduped = deduplicate_messages(original)

        assert _uuids(deduped) == ["uuid-v1", "uuid-child"]
        assert child_of_v2.parentUuid == "uuid-v1"
        _assert_no_dangling_parents(original, deduped)

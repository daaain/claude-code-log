"""Snapshot tests for HTML output regression detection.

These tests use syrupy to capture and compare HTML output, detecting
unintended changes to the rendered HTML structure.
"""

import shutil
from pathlib import Path

import pytest

from claude_code_log.converter import convert_jsonl_to_html, load_transcript
from claude_code_log.html.renderer import (
    generate_html,
    generate_projects_index_html,
    generate_session_html,
)

# Run snapshot tests serially to ensure deterministic file ordering
pytestmark = pytest.mark.snapshot


class TestTranscriptHTMLSnapshots:
    """Snapshot tests for transcript HTML output."""

    def test_representative_messages_html(self, html_snapshot, test_data_dir):
        """Snapshot test for representative messages - core message types."""
        test_file = test_data_dir / "representative_messages.jsonl"
        messages = load_transcript(test_file)
        html = generate_html(messages, "Test Transcript")
        assert html == html_snapshot

    def test_edge_cases_html(self, html_snapshot, test_data_dir):
        """Snapshot test for edge cases - errors, special chars, long text.

        Uses the library render default (FULL) so system/hook/noise content
        stays in the snapshot — the CLI default is now HIGH (--depth tool,
        #159), but generate_html renders everything by default.
        """
        test_file = test_data_dir / "edge_cases.jsonl"
        messages = load_transcript(test_file)
        html = generate_html(messages, "Edge Cases")
        assert html == html_snapshot

    def test_system_reminder_html(self, html_snapshot, test_data_dir):
        """End-to-end snapshot for <system-reminder> user annotations (#275)."""
        test_file = test_data_dir / "system_reminder.jsonl"
        messages = load_transcript(test_file)
        html = generate_html(messages, "System Reminders")
        assert html == html_snapshot

    def test_multi_session_html(self, html_snapshot, test_data_dir, tmp_path):
        """Snapshot test for multi-session combined output."""
        shutil.copy(
            test_data_dir / "representative_messages.jsonl",
            tmp_path / "session_a.jsonl",
        )
        shutil.copy(test_data_dir / "session_b.jsonl", tmp_path / "session_b.jsonl")

        html_file = convert_jsonl_to_html(tmp_path, use_cache=False)
        html = html_file.read_text(encoding="utf-8")
        assert html == html_snapshot

    def test_steering_chronological_order_html(
        self, html_snapshot, test_data_dir, tmp_path
    ):
        """End-to-end guard for #295: a mid-conversation `remove` steering
        entry rendered through the dir/combined path.

        UUID-less queue-ops carry no uuid, so the DAG builder drops them and
        directory-mode loading re-adds them; before the fix they were appended
        at the END, so this steering op (07:00:02, between a1 and u2) rendered
        after the last assistant message. That whole bug class was invisible to
        the snapshot suite — no existing fixture contained a queue-operation —
        so this fixture exists specifically so any future reordering of the
        composition moves this snapshot and has to be explained.
        """
        shutil.copy(
            test_data_dir / "session_id_steering_order.jsonl",
            tmp_path / "session_id_steering_order.jsonl",
        )

        html_file = convert_jsonl_to_html(tmp_path, use_cache=False)
        html = html_file.read_text(encoding="utf-8")
        assert html == html_snapshot


class TestSessionHTMLSnapshots:
    """Snapshot tests for individual session HTML output."""

    def test_individual_session_html(self, html_snapshot, test_data_dir):
        """Snapshot test for individual session file."""
        test_file = test_data_dir / "representative_messages.jsonl"
        messages = load_transcript(test_file)
        html = generate_session_html(messages, "test_session", "Test Session")
        assert html == html_snapshot


class TestTeammatesHTMLSnapshots:
    """Snapshot tests for the experimental teammates feature rendering."""

    def test_teammates_fixture_html(self, html_snapshot, test_data_dir):
        """Snapshot the teammates fixture rendered as a combined transcript.

        Locks in the rendered shape of:
        - <teammate-message> block cards with colored borders/badges
        - the six teammate tool cards (TeamCreate/Delete, TaskCreate/Update/
          List, SendMessage)
        - the task-list HTML table with per-row status classes
        - the teammate-aware extensions on Task spawn inputs/outputs
        """
        teammates_dir = test_data_dir / "teammates"
        # Load main session + both linked subagents via the standard pipeline.
        main_jsonl = teammates_dir / "ef000000-0000-4000-8000-000000000001.jsonl"
        messages = load_transcript(main_jsonl)
        html = generate_html(messages, "Teammates Fixture")
        assert html == html_snapshot


class TestAsyncAgentsHTMLSnapshots:
    """Snapshot tests for the async-agents feature rendering (issue #90)."""

    def test_async_agents_fixture_html(self, html_snapshot, test_data_dir):
        """Snapshot the async-agents fixture rendered end-to-end.

        Locks in:
        - the [async] hint badge on the spawning Task tool_use title
        - the ``Async agent launched successfully`` stub on the tool_result
        - the ``Result (from async notification)`` fold rendered below
          the stub when the last sub-assistant matches the notification
        - the TaskOutput poll card (cyan border, retrieval_status row)
        - the <task-notification> card collapsed to a backlink stub
          when its result_text was folded into the spawn
        """
        async_dir = test_data_dir / "async_agents"
        main_jsonl = async_dir / "eb000000-0000-4000-8000-000000000001.jsonl"
        messages = load_transcript(main_jsonl)
        html = generate_html(messages, "Async Agents Fixture")
        assert html == html_snapshot

    def test_async_agents_fixture_html_low(self, html_snapshot, test_data_dir):
        """Snapshot the async-agents fixture at ``--detail low``.

        Regression guard for the "fold lost at low" report (mail #2620 →
        Plan A): the spawn-fold is now sourced from the notification's
        ``result_text``, so it survives the LOW depth level where
        sidechain entries are stripped pre-render. The locked-in shape
        confirms the ``Result (from async notification)`` fold is
        still rendered under the ``Async agent launched successfully``
        stub at LOW.
        """
        from claude_code_log.html.renderer import HtmlRenderer
        from claude_code_log.models import RenderingDepth

        async_dir = test_data_dir / "async_agents"
        main_jsonl = async_dir / "eb000000-0000-4000-8000-000000000001.jsonl"
        messages = load_transcript(main_jsonl)
        renderer = HtmlRenderer()
        renderer.depth = RenderingDepth.AGENT
        html = renderer.generate(messages, "Async Agents Fixture (LOW)")
        assert html == html_snapshot


class TestIndexHTMLSnapshots:
    """Snapshot tests for project index HTML output."""

    def test_project_index_html(self, html_snapshot):
        """Snapshot test for project index template."""
        project_summaries = [
            {
                "name": "-Users-test-project-alpha",
                "path": Path("/tmp/project-alpha"),
                "html_file": "-Users-test-project-alpha/combined_transcripts.html",
                "jsonl_count": 5,
                "message_count": 42,
                "last_modified": 1700000000.0,
                "total_input_tokens": 1000,
                "total_output_tokens": 2000,
                "total_cache_creation_tokens": 500,
                "total_cache_read_tokens": 1500,
                "latest_timestamp": "2025-01-15T10:00:00Z",
                "earliest_timestamp": "2025-01-01T09:00:00Z",
                "working_directories": ["/Users/test/projects/alpha"],
                "sessions": [
                    {
                        "id": "session-abc12345",
                        "summary": "Test session summary",
                        "timestamp_range": "2025-01-15 10:00:00",
                        "message_count": 10,
                        "first_user_message": "Hello, this is a test",
                    }
                ],
            },
            {
                "name": "-Users-test-project-beta",
                "path": Path("/tmp/project-beta"),
                "html_file": "-Users-test-project-beta/combined_transcripts.html",
                "jsonl_count": 3,
                "message_count": 25,
                "last_modified": 1700000100.0,
            },
        ]

        html = generate_projects_index_html(project_summaries)
        assert html == html_snapshot

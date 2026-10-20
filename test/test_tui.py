#!/usr/bin/env python3
"""Tests for the TUI module."""

import json
import sys
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

import pytest
from textual.css.query import NoMatches
from textual.widgets import DataTable, Label

from claude_code_log.cache import CacheManager, SessionCacheData
from claude_code_log.tui import ProjectSelector, SessionBrowser, run_session_browser


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory with test JSONL files."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)

        # Create sample JSONL files with test data
        test_data = [
            {
                "type": "user",
                "sessionId": "session-123",
                "timestamp": "2025-01-01T10:00:00Z",
                "uuid": "user-uuid-1",
                "message": {
                    "role": "user",
                    "content": "Hello, this is my first message",
                },
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/test",
                "version": "1.0.0",
                "isMeta": False,
            },
            {
                "type": "assistant",
                "sessionId": "session-123",
                "timestamp": "2025-01-01T10:01:00Z",
                "uuid": "assistant-uuid-1",
                "message": {
                    "id": "msg-123",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-sonnet",
                    "content": [
                        {"type": "text", "text": "Hello! How can I help you today?"}
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 15,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
                "parentUuid": "user-uuid-1",
                "isSidechain": False,
                "userType": "human",
                "cwd": "/test",
                "version": "1.0.0",
                "requestId": "req-123",
            },
            {
                "type": "user",
                "sessionId": "session-456",
                "timestamp": "2025-01-02T14:30:00Z",
                "uuid": "user-uuid-2",
                "message": {"role": "user", "content": "This is a different session"},
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/test",
                "version": "1.0.0",
                "isMeta": False,
            },
            {
                "type": "summary",
                "summary": "User asked about session management",
                "leafUuid": "user-uuid-2",
            },
        ]

        # Write test data to JSONL files - one per session (matching real-world usage)
        # Session 123 entries
        session_123_file = project_path / "session-123.jsonl"
        with open(session_123_file, "w", encoding="utf-8") as f:
            for entry in test_data:
                if entry.get("sessionId") == "session-123":
                    f.write(json.dumps(entry) + "\n")

        # Session 456 entries (includes summary)
        session_456_file = project_path / "session-456.jsonl"
        with open(session_456_file, "w", encoding="utf-8") as f:
            for entry in test_data:
                if (
                    entry.get("sessionId") == "session-456"
                    or entry.get("type") == "summary"
                ):
                    f.write(json.dumps(entry) + "\n")

        yield project_path


@pytest.mark.tui
class TestSessionBrowser:
    """Test cases for the SessionBrowser TUI application."""

    def test_init(self, temp_project_dir):
        """Test SessionBrowser initialization."""
        app = SessionBrowser(temp_project_dir)
        # SessionBrowser resolves path, so compare resolved paths
        assert app.project_path == temp_project_dir.resolve()
        assert isinstance(app.cache_manager, CacheManager)
        assert app.sessions == {}
        assert app.selected_session_id is None

    @pytest.mark.asyncio
    async def test_load_sessions_from_cache(self, temp_project_dir):
        """Test loading sessions from cache when available and no files modified."""
        app = SessionBrowser(temp_project_dir)

        # Mock cached session data
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            )
        }

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app.run_test() as pilot:
                # Wait for the app to load
                await pilot.pause(0.1)

                # Check that sessions were loaded from cache
                assert len(app.sessions) == 1
                assert "session-123" in app.sessions
                assert app.sessions["session-123"].message_count == 2

    @pytest.mark.asyncio
    async def test_archiving_last_jsonl_marks_session_archived(self):
        """A cached session must be classified as archived, not current, once
        the project's last source JSONL is gone, so the empty valid set still
        routes it to archived_sessions (Restore available)."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)  # deliberately contains no JSONL files
            app = SessionBrowser(project_path)
            orphan = {
                "orphan-session": SessionCacheData(
                    session_id="orphan-session",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="only in cache now",
                )
            }
            with (
                patch.object(
                    app.cache_manager, "get_cached_project_data"
                ) as mock_cache,
                patch.object(app.cache_manager, "get_modified_files", return_value=[]),
                patch.object(
                    app.cache_manager, "get_existing_session_ids", return_value=set()
                ),
                patch.object(
                    app.cache_manager, "get_archived_sessions", return_value=orphan
                ),
            ):
                mock_cache.return_value = Mock(
                    sessions=orphan, working_directories=[str(project_path)]
                )
                async with app.run_test() as pilot:
                    await pilot.pause(0.1)
                    assert app.sessions == {}
                    assert "orphan-session" in app.archived_sessions

    @pytest.mark.asyncio
    async def test_load_sessions_with_modified_files(self, temp_project_dir):
        """Test loading sessions when files have been modified since cache."""
        app = SessionBrowser(temp_project_dir)

        # Mock cached session data but with modified files
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            )
        }

        # Mock the updated cache data after rebuild
        updated_mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            ),
            "session-456": SessionCacheData(
                session_id="session-456",
                first_timestamp="2025-01-02T14:30:00Z",
                last_timestamp="2025-01-02T14:30:00Z",
                message_count=1,
                first_user_message="This is a different session",
                total_input_tokens=0,
                total_output_tokens=0,
            ),
        }

        modified_file = temp_project_dir / "test-transcript.jsonl"

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
            patch("claude_code_log.tui.ensure_fresh_cache") as mock_ensure,
        ):
            # First call returns initial cache, second call returns updated cache
            mock_cache.side_effect = [
                Mock(
                    sessions=mock_session_data,
                    working_directories=[str(temp_project_dir)],
                ),
                Mock(
                    sessions=updated_mock_session_data,
                    working_directories=[str(temp_project_dir)],
                ),
            ]
            mock_modified.return_value = [modified_file]  # One modified file

            async with app.run_test() as pilot:
                # Wait for the app to load and rebuild cache
                await pilot.pause(1.0)

                # Check that convert function was called due to modified files
                mock_ensure.assert_called_once()

                # Check that sessions were rebuilt from JSONL files
                assert len(app.sessions) >= 2  # Should have session-123 and session-456
                assert "session-123" in app.sessions
                assert "session-456" in app.sessions

    @pytest.mark.asyncio
    async def test_load_sessions_build_cache(self, temp_project_dir):
        """Test loading sessions when cache needs to be built."""
        app = SessionBrowser(temp_project_dir)

        # Mock the cache data that will be available after building
        built_cache_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            ),
            "session-456": SessionCacheData(
                session_id="session-456",
                first_timestamp="2025-01-02T14:30:00Z",
                last_timestamp="2025-01-02T14:30:00Z",
                message_count=1,
                first_user_message="This is a different session",
                total_input_tokens=0,
                total_output_tokens=0,
            ),
        }

        # Mock no cached data available
        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
            patch("claude_code_log.tui.ensure_fresh_cache") as mock_ensure,
        ):
            # First call returns empty cache, second call returns built cache
            mock_cache.side_effect = [
                Mock(sessions={}, working_directories=[str(temp_project_dir)]),
                Mock(
                    sessions=built_cache_data,
                    working_directories=[str(temp_project_dir)],
                ),
            ]
            mock_modified.return_value = []  # No modified files (but no cache either)

            async with app.run_test() as pilot:
                # Wait for the app to load and build cache
                await pilot.pause(1.0)

                # Check that convert function was called to build cache
                mock_ensure.assert_called_once()

                # Check that sessions were built from JSONL files
                assert len(app.sessions) >= 2  # Should have session-123 and session-456
                assert "session-123" in app.sessions
                assert "session-456" in app.sessions

    @pytest.mark.asyncio
    async def test_populate_table(self, temp_project_dir):
        """Test that the sessions table is populated correctly."""
        app = SessionBrowser(temp_project_dir)

        # Mock session data - testing summary prioritization
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                summary="Session with Claude-generated summary",  # Should be displayed
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
                cwd="/test/project",
            ),
            "session-456": SessionCacheData(
                session_id="session-456",
                summary=None,  # No summary, should fall back to first_user_message
                first_timestamp="2025-01-02T14:30:00Z",
                last_timestamp="2025-01-02T14:30:00Z",
                message_count=1,
                first_user_message="This is a different session",
                total_input_tokens=0,
                total_output_tokens=0,
                cwd="/test/other",
            ),
        }

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Get the data table
                table = cast(DataTable, app.query_one("#sessions-table"))

                # Check that table has correct number of rows
                assert table.row_count == 2

                # Check column headers - Textual 4.x API
                columns = table.columns
                assert len(columns) == 6
                # Check that columns exist (column access varies in Textual versions)
                assert table.row_count == 2

    @pytest.mark.asyncio
    async def test_populate_table_with_bracket_content(self, temp_project_dir):
        """Test that session previews containing square brackets don't cause MarkupError.

        Rich interprets square brackets as markup tags, so content like
        '[/Users/foo/bar]' would be parsed as a closing tag and raise MarkupError.
        Session summaries and first messages must be escaped before display.
        """
        app = SessionBrowser(temp_project_dir)

        # Use session IDs matching the JSONL files in temp_project_dir fixture
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                summary=None,
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=5,
                first_user_message="10:06:02.383: [/Users/guowang/PycharmProjects/h5st_student] \ngit -c credential.helper=",
                total_input_tokens=100,
                total_output_tokens=200,
                cwd="/test/project",
            ),
            "session-456": SessionCacheData(
                session_id="session-456",
                summary="Working on [feature-branch] implementation",
                first_timestamp="2025-01-02T14:30:00Z",
                last_timestamp="2025-01-02T14:30:00Z",
                message_count=3,
                first_user_message="Normal message",
                total_input_tokens=50,
                total_output_tokens=75,
                cwd="/test/other",
            ),
        }

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []

            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Should not raise MarkupError - table should render successfully
                table = cast(DataTable, app.query_one("#sessions-table"))
                assert table.row_count == 2

    @pytest.mark.asyncio
    async def test_row_selection(self, temp_project_dir):
        """Test selecting a row in the sessions table."""
        app = SessionBrowser(temp_project_dir)

        # Mock session data
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            )
        }

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Select the first row
                await pilot.click("#sessions-table")
                await pilot.press("enter")

                # Check that selection was handled
                assert app.selected_session_id is not None

    @pytest.mark.asyncio
    async def test_export_action_no_selection(self, temp_project_dir):
        """Test export action when no session is selected."""
        app = SessionBrowser(temp_project_dir)

        with patch("claude_code_log.tui.webbrowser.open") as mock_browser:
            async with app.run_test() as pilot:
                await pilot.pause()

                # Manually clear the selection (since DataTable auto-selects first row)
                app.selected_session_id = None

                # Try to export without selecting a session
                app.action_export_selected()

                # Should still have no selection (action should not change it)
                assert app.selected_session_id is None
                # Browser should not have been opened
                mock_browser.assert_not_called()

    @pytest.mark.asyncio
    async def test_export_action_with_selection(self, temp_project_dir):
        """Test export action with a selected session."""
        app = SessionBrowser(temp_project_dir)
        app.selected_session_id = "session-123"

        with patch("claude_code_log.tui.webbrowser.open") as mock_browser:
            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Test export action
                app.action_export_selected()

                # Check that browser was opened with the session HTML file
                # Use resolved path since SessionBrowser resolves project_path
                expected_file = temp_project_dir.resolve() / "session-session-123.html"
                mock_browser.assert_called_once_with(f"file://{expected_file}")

    @pytest.mark.asyncio
    async def test_resume_action_no_selection(self, temp_project_dir):
        """Test resume action when no session is selected."""
        app = SessionBrowser(temp_project_dir)

        async with app.run_test() as pilot:
            await pilot.pause()

            # Manually clear the selection (since DataTable auto-selects first row)
            app.selected_session_id = None

            # Try to resume without selecting a session
            app.action_resume_selected()

            # Should still have no selection (action should not change it)
            assert app.selected_session_id is None

    @pytest.mark.asyncio
    async def test_resume_action_with_selection(self, temp_project_dir):
        """Test resume action with a selected session."""
        app = SessionBrowser(temp_project_dir)
        app.selected_session_id = "session-123"

        with (
            patch("claude_code_log.tui.os.execvp") as mock_execvp,
            patch.object(app, "suspend") as mock_suspend,
        ):
            # Make suspend work as a context manager that executes the body
            mock_suspend.return_value.__enter__ = Mock(return_value=None)
            mock_suspend.return_value.__exit__ = Mock(return_value=False)

            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Test resume action
                app.action_resume_selected()

                # Check that suspend was called
                mock_suspend.assert_called_once()
                # Check that execvp was called with correct arguments
                mock_execvp.assert_called_once_with(
                    "claude", ["claude", "-r", "session-123"]
                )

    @pytest.mark.asyncio
    async def test_resume_action_command_not_found(self, temp_project_dir):
        """Test resume action when Claude CLI is not found."""
        app = SessionBrowser(temp_project_dir)
        app.selected_session_id = "session-123"

        with (
            patch("claude_code_log.tui.os.execvp") as mock_execvp,
            patch.object(app, "suspend") as mock_suspend,
        ):
            mock_execvp.side_effect = FileNotFoundError()
            # Make suspend work as a context manager that executes the body
            mock_suspend.return_value.__enter__ = Mock(return_value=None)
            mock_suspend.return_value.__exit__ = Mock(return_value=False)

            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Test resume action
                app.action_resume_selected()

                # Should handle the error gracefully
                mock_suspend.assert_called_once()
                mock_execvp.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_action(self, temp_project_dir):
        """Test refresh action - no longer applicable since refresh button was removed."""
        # This test is no longer applicable since the refresh action was removed
        # The TUI now automatically handles cache updates when needed
        app = SessionBrowser(temp_project_dir)

        async with app.run_test() as pilot:
            await pilot.pause()
            # Test that the app loads properly without refresh functionality
            assert len(app.sessions) >= 0  # Just ensure sessions are loaded

    @pytest.mark.asyncio
    async def test_button_actions(self, temp_project_dir):
        """Test button press events - no longer applicable since buttons were removed."""
        # This test is no longer applicable since the buttons were removed
        # Actions are now only triggered via keyboard shortcuts
        app = SessionBrowser(temp_project_dir)

        async with app.run_test() as pilot:
            await pilot.pause()

            # Test that the app loads without buttons
            sessions_table = app.query_one("#sessions-table")
            assert sessions_table is not None

            # Test that the interface loads without the removed buttons
            try:
                app.query_one("#export-btn")
                assert False, "Export button should not exist"
            except NoMatches:
                pass  # Expected - button was removed

    def test_summary_prioritization(self, temp_project_dir):
        """Test that summaries are prioritized over first user messages in display."""

        # Test session with summary
        session_with_summary = SessionCacheData(
            session_id="session-with-summary",
            summary="This is a Claude-generated summary",
            first_timestamp="2025-01-01T10:00:00Z",
            last_timestamp="2025-01-01T10:01:00Z",
            message_count=2,
            first_user_message="This should not be displayed",
            cwd="/test/project",
        )

        # Test session without summary
        session_without_summary = SessionCacheData(
            session_id="session-without-summary",
            summary=None,
            first_timestamp="2025-01-01T10:00:00Z",
            last_timestamp="2025-01-01T10:01:00Z",
            message_count=2,
            first_user_message="This should be displayed",
            cwd="/test/project",
        )

        # Test the preview generation logic from populate_table
        # Session with summary should show summary
        preview_with_summary = (
            session_with_summary.summary
            or session_with_summary.first_user_message
            or "No preview available"
        )
        assert preview_with_summary == "This is a Claude-generated summary"

        # Session without summary should show first user message
        preview_without_summary = (
            session_without_summary.summary
            or session_without_summary.first_user_message
            or "No preview available"
        )
        assert preview_without_summary == "This should be displayed"

    def test_format_timestamp(self, temp_project_dir):
        """Test timestamp formatting."""
        app = SessionBrowser(temp_project_dir)

        # Test valid timestamp (default long format includes year)
        formatted = app.format_timestamp("2025-01-01T10:00:00Z")
        assert formatted == "2025-01-01 10:00"

        # Test short format (no year)
        formatted_short = app.format_timestamp(
            "2025-01-01T10:00:00Z", short_format=True
        )
        assert formatted_short == "01-01 10:00"

        # Test date only
        formatted_date = app.format_timestamp("2025-01-01T10:00:00Z", date_only=True)
        assert formatted_date == "2025-01-01"

        # Test invalid timestamp
        formatted_invalid = app.format_timestamp("invalid")
        assert formatted_invalid == "Unknown"

    @pytest.mark.asyncio
    async def test_keyboard_shortcuts(self, temp_project_dir):
        """Test keyboard shortcuts."""
        app = SessionBrowser(temp_project_dir)
        app.selected_session_id = "session-123"

        with (
            patch.object(app, "action_export_selected") as mock_export,
            patch.object(app, "action_resume_selected") as mock_resume,
        ):
            async with app.run_test() as pilot:
                await pilot.pause(0.1)

                # Test keyboard shortcuts
                await pilot.press("h")  # Export
                await pilot.press("c")  # Resume

                # Check that actions were called
                mock_export.assert_called_once()
                mock_resume.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_resize(self, temp_project_dir):
        """Test that the TUI properly handles terminal resizing."""
        app = SessionBrowser(temp_project_dir)

        # Mock session data
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            ),
            "session-456": SessionCacheData(
                session_id="session-456",
                first_timestamp="2025-01-02T14:30:00Z",
                last_timestamp="2025-01-02T14:30:00Z",
                message_count=1,
                first_user_message="This is a different session with a very long title that should be truncated",
                total_input_tokens=0,
                total_output_tokens=0,
            ),
        }

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.1)

                # Set up session data manually
                app.sessions = mock_session_data
                app.populate_table()
                app.update_stats()

                # Get initial table state
                table = cast(DataTable, app.query_one("#sessions-table"))
                initial_columns = table.columns
                initial_column_count = len(initial_columns)

                # Test resize handling by manually calling on_resize
                # This simulates what happens when terminal is resized
                app.on_resize()
                await pilot.pause(0.1)

                # Check that resize was handled - columns should still be the same
                resized_columns = table.columns
                resized_column_count = len(resized_columns)

                # Should have same number of columns after resize
                assert resized_column_count == initial_column_count

                # Verify the table still has the correct number of rows
                assert table.row_count == 2

    @pytest.mark.asyncio
    async def test_column_width_calculation(self, temp_project_dir):
        """Test that column widths are calculated correctly for different terminal sizes."""
        # Mock session data
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            ),
        }

        # Test wide terminal (120 columns)
        app_wide = SessionBrowser(temp_project_dir)
        with (
            patch.object(
                app_wide.cache_manager, "get_cached_project_data"
            ) as mock_cache,
            patch.object(app_wide.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app_wide.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.1)

                app_wide.sessions = mock_session_data
                app_wide.populate_table()

                # Check that the table was populated correctly
                table = cast(DataTable, app_wide.query_one("#sessions-table"))
                assert table.row_count == 1

        # Test narrow terminal (80 columns) - separate app instance
        app_narrow = SessionBrowser(temp_project_dir)
        with (
            patch.object(
                app_narrow.cache_manager, "get_cached_project_data"
            ) as mock_cache,
            patch.object(
                app_narrow.cache_manager, "get_modified_files"
            ) as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app_narrow.run_test(size=(80, 40)) as pilot:
                await pilot.pause(0.1)

                app_narrow.sessions = mock_session_data
                app_narrow.populate_table()

                # Check that the table was populated correctly
                table = cast(DataTable, app_narrow.query_one("#sessions-table"))
                assert table.row_count == 1

    @pytest.mark.asyncio
    async def test_stats_layout_responsiveness(self, temp_project_dir):
        """Test that stats layout switches between single-row and multi-row based on terminal width."""
        # Mock session data
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            ),
        }

        # Test wide terminal (should use single-row layout)
        app_wide = SessionBrowser(temp_project_dir)
        with (
            patch.object(
                app_wide.cache_manager, "get_cached_project_data"
            ) as mock_cache,
            patch.object(app_wide.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app_wide.run_test(size=(130, 40)) as pilot:
                await pilot.pause(0.1)

                app_wide.sessions = mock_session_data
                app_wide.update_stats()

                stats = cast(Label, app_wide.query_one("#stats"))
                stats_text = str(stats.content)

                # Wide terminal should display project and session info
                assert "Project:" in stats_text
                assert "Sessions:" in stats_text

        # Test narrow terminal (should use multi-row layout) - separate app instance
        app_narrow = SessionBrowser(temp_project_dir)
        with (
            patch.object(
                app_narrow.cache_manager, "get_cached_project_data"
            ) as mock_cache,
            patch.object(
                app_narrow.cache_manager, "get_modified_files"
            ) as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app_narrow.run_test(size=(80, 40)) as pilot:
                await pilot.pause(0.1)

                app_narrow.sessions = mock_session_data
                app_narrow.update_stats()

                stats = cast(Label, app_narrow.query_one("#stats"))
                stats_text = str(stats.content)

                # Narrow terminal should also display project and session info
                assert "Project:" in stats_text
                assert "Sessions:" in stats_text


@pytest.mark.tui
class TestRunSessionBrowser:
    """Test cases for the run_session_browser function."""

    def test_run_session_browser_nonexistent_path(self, capsys):
        """Test running session browser with nonexistent path."""
        fake_path = Path("/nonexistent/path")
        run_session_browser(fake_path)

        captured = capsys.readouterr()
        assert "Error: Project path" in captured.out
        assert "does not exist" in captured.out

    def test_run_session_browser_not_directory(self, capsys, temp_project_dir):
        """Test running session browser with a file instead of directory."""
        # Create a file
        test_file = temp_project_dir / "test.txt"
        test_file.write_text("test", encoding="utf-8")

        run_session_browser(test_file)

        captured = capsys.readouterr()
        assert "is not a directory" in captured.out

    def test_run_session_browser_no_jsonl_files(self, capsys):
        """Test running session browser with directory containing no JSONL files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_path = Path(temp_dir)
            run_session_browser(empty_path)

            captured = capsys.readouterr()
            assert "No JSONL transcript files found" in captured.out

    def test_run_session_browser_success(self, temp_project_dir):
        """Test successful run of session browser."""
        with patch("claude_code_log.tui.SessionBrowser.run") as mock_run:
            run_session_browser(temp_project_dir)

            # Should create and run the app
            mock_run.assert_called_once()


@pytest.mark.tui
class TestIntegration:
    """Integration tests for TUI functionality."""

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self, temp_project_dir):
        """Test complete session browsing lifecycle."""
        app = SessionBrowser(temp_project_dir)

        # Mock session data for integration test
        mock_session_data = {
            "session-123": SessionCacheData(
                session_id="session-123",
                first_timestamp="2025-01-01T10:00:00Z",
                last_timestamp="2025-01-01T10:01:00Z",
                message_count=2,
                first_user_message="Hello, this is my first message",
                total_input_tokens=10,
                total_output_tokens=15,
            ),
            "session-456": SessionCacheData(
                session_id="session-456",
                first_timestamp="2025-01-02T14:30:00Z",
                last_timestamp="2025-01-02T14:30:00Z",
                message_count=1,
                first_user_message="This is a different session",
                total_input_tokens=0,
                total_output_tokens=0,
            ),
        }

        with (
            patch.object(app.cache_manager, "get_cached_project_data") as mock_cache,
            patch.object(app.cache_manager, "get_modified_files") as mock_modified,
        ):
            mock_cache.return_value = Mock(
                sessions=mock_session_data, working_directories=[str(temp_project_dir)]
            )
            mock_modified.return_value = []  # No modified files

            async with app.run_test() as pilot:
                # Wait for initial load
                await pilot.pause(1.0)

                # Manually trigger load_sessions to ensure data is loaded with mocked cache
                app.sessions = mock_session_data
                app.populate_table()
                app.update_stats()

                # Check that sessions are loaded
                assert len(app.sessions) > 0

                # Check that table is populated
                table = cast(DataTable, app.query_one("#sessions-table"))
                assert table.row_count > 0

                # Check that stats are updated
                stats = cast(Label, app.query_one("#stats"))
                stats_text = str(stats.content)
                assert "Project:" in stats_text

    @pytest.mark.asyncio
    async def test_empty_project_handling(self):
        """Test handling of project with no sessions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create empty JSONL file
            jsonl_file = project_path / "empty.jsonl"
            jsonl_file.touch()

            app = SessionBrowser(project_path)

            async with app.run_test() as pilot:
                # Wait for initial load - longer on Windows due to Path.resolve() overhead
                pause_time = 1.0 if sys.platform == "win32" else 0.1
                await pilot.pause(pause_time)

                # Should handle empty project gracefully
                assert len(app.sessions) == 0

                # Stats should show zero sessions
                stats = cast(Label, app.query_one("#stats"))
                stats_text = str(stats.content)
                assert "Sessions:[/bold] 0" in stats_text

    @pytest.mark.asyncio
    async def test_archived_project_loads_archived_sessions(self):
        """Test that an archived project (no JSONL files) loads sessions in archived_sessions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)

            # Create empty JSONL file to initialize
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.touch()

            # Create app with is_archived=True (simulating archived project)
            app = SessionBrowser(project_path, is_archived=True)

            # Mock the cache manager to return some sessions
            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    summary="Archived session",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=5,
                    first_user_message="Hello from archived",
                    total_input_tokens=100,
                    total_output_tokens=200,
                ),
            }

            with (
                patch.object(
                    app.cache_manager, "get_cached_project_data"
                ) as mock_cache,
            ):
                mock_cache.return_value = Mock(
                    sessions=mock_session_data,
                    working_directories=[str(project_path)],
                )

                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    # Manually call load_sessions (since mocking)
                    app.load_sessions()

                    # Sessions should be in archived_sessions, not sessions
                    assert len(app.archived_sessions) > 0
                    assert len(app.sessions) == 0

                    # Stats should show "archived" count
                    stats = cast(Label, app.query_one("#stats"))
                    stats_text = str(stats.content)
                    assert "archived" in stats_text.lower()


@pytest.mark.tui
class TestUnifiedSessionList:
    """Tests for the unified session list showing both current and archived sessions."""

    @pytest.mark.asyncio
    async def test_unified_list_shows_both_current_and_archived(self):
        """Test that both current and archived sessions appear in the same list."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-current.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            current_session = {
                "session-current": SessionCacheData(
                    session_id="session-current",
                    first_timestamp="2025-01-02T10:00:00Z",
                    last_timestamp="2025-01-02T10:01:00Z",
                    message_count=1,
                    first_user_message="Current session",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }
            archived_session = {
                "session-archived": SessionCacheData(
                    session_id="session-archived",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Archived session",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = current_session
                app.archived_sessions = archived_session
                app.populate_table()

                # Get the table
                table = cast(DataTable, app.query_one("#sessions-table"))

                # Should have 2 rows (both sessions in one list)
                assert table.row_count == 2

    @pytest.mark.asyncio
    async def test_unified_list_sorted_by_timestamp_newest_first(self):
        """Test that sessions are sorted by timestamp with newest first."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-old.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            # Create sessions with different timestamps
            old_session = {
                "session-old": SessionCacheData(
                    session_id="session-old",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Old session",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }
            new_archived_session = {
                "session-new": SessionCacheData(
                    session_id="session-new",
                    first_timestamp="2025-01-03T10:00:00Z",
                    last_timestamp="2025-01-03T10:01:00Z",
                    message_count=1,
                    first_user_message="New archived session",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = old_session
                app.archived_sessions = new_archived_session
                app.populate_table()

                table = cast(DataTable, app.query_one("#sessions-table"))

                # Get first row - should be the newest (archived) session
                first_row = table.get_row_at(0)
                # Session ID column shows first 8 chars
                assert str(first_row[0]).startswith("session-")
                # Title should have [ARCHIVED] prefix since newest is archived
                assert "[ARCHIVED]" in str(first_row[1])

    @pytest.mark.asyncio
    async def test_archived_sessions_have_archived_indicator(self):
        """Test that archived sessions display [ARCHIVED] indicator in title."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-current.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            current_session = {
                "session-current": SessionCacheData(
                    session_id="session-current",
                    first_timestamp="2025-01-02T10:00:00Z",
                    last_timestamp="2025-01-02T10:01:00Z",
                    message_count=1,
                    first_user_message="Current session message",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }
            archived_session = {
                "session-archived": SessionCacheData(
                    session_id="session-archived",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Archived session message",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = current_session
                app.archived_sessions = archived_session
                app.populate_table()

                table = cast(DataTable, app.query_one("#sessions-table"))

                # Check both rows
                found_archived_indicator = False
                found_current_without_indicator = False

                for row_idx in range(table.row_count):
                    row = table.get_row_at(row_idx)
                    title = str(row[1])
                    if "[ARCHIVED]" in title:
                        found_archived_indicator = True
                        assert "Archived session message" in title
                    else:
                        found_current_without_indicator = True
                        assert "Current session message" in title

                assert found_archived_indicator, (
                    "Archived session should have [ARCHIVED] indicator"
                )
                assert found_current_without_indicator, (
                    "Current session should not have [ARCHIVED] indicator"
                )

    @pytest.mark.asyncio
    async def test_stats_show_combined_totals(self):
        """Test that stats display combined totals from both current and archived sessions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-current.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            current_session = {
                "session-current": SessionCacheData(
                    session_id="session-current",
                    first_timestamp="2025-01-02T10:00:00Z",
                    last_timestamp="2025-01-02T10:01:00Z",
                    message_count=5,
                    first_user_message="Current",
                    total_input_tokens=100,
                    total_output_tokens=200,
                ),
            }
            archived_session = {
                "session-archived": SessionCacheData(
                    session_id="session-archived",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=3,
                    first_user_message="Archived",
                    total_input_tokens=50,
                    total_output_tokens=100,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = current_session
                app.archived_sessions = archived_session
                app.update_stats()

                stats = cast(Label, app.query_one("#stats"))
                stats_text = str(stats.content)

                # Should show combined sessions count (2)
                assert "Sessions:[/bold] 2" in stats_text
                # Should show combined messages count (5 + 3 = 8)
                assert "Messages:[/bold] 8" in stats_text
                # Should show combined tokens (100+200+50+100 = 450)
                assert "Tokens:[/bold] 450" in stats_text
                # Should indicate archived count
                assert "1 archived" in stats_text


@pytest.mark.tui
class TestArchiveConfirmScreen:
    """Tests for archive confirmation via the archive action."""

    @pytest.mark.asyncio
    async def test_archive_confirm_y_key_deletes_file(self):
        """Test confirming archive with 'y' key deletes the JSONL file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = mock_session_data
                app.selected_session_id = "session-123"

                assert jsonl_file.exists()

                # Trigger archive (opens modal)
                await pilot.press("a")
                await pilot.pause(0.1)

                # Confirm with 'y'
                await pilot.press("y")
                await pilot.pause(0.1)

                assert not jsonl_file.exists()

    @pytest.mark.asyncio
    async def test_archive_confirm_enter_key_deletes_file(self):
        """Test confirming archive with Enter key deletes the JSONL file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = mock_session_data
                app.selected_session_id = "session-123"

                assert jsonl_file.exists()

                # Trigger archive (opens modal)
                await pilot.press("a")
                await pilot.pause(0.1)

                # Confirm with Enter
                await pilot.press("enter")
                await pilot.pause(0.1)

                assert not jsonl_file.exists()

    @pytest.mark.asyncio
    async def test_archive_cancel_n_key_keeps_file(self):
        """Test cancelling archive with 'n' key keeps the JSONL file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = mock_session_data
                app.selected_session_id = "session-123"

                # Trigger archive (opens modal)
                await pilot.press("a")
                await pilot.pause(0.1)

                # Cancel with 'n'
                await pilot.press("n")
                await pilot.pause(0.1)

                # File should still exist
                assert jsonl_file.exists()

    @pytest.mark.asyncio
    async def test_archive_cancel_escape_key_keeps_file(self):
        """Test cancelling archive with Escape key keeps the JSONL file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                app.sessions = mock_session_data
                app.selected_session_id = "session-123"

                # Trigger archive (opens modal)
                await pilot.press("a")
                await pilot.pause(0.1)

                # Cancel with Escape
                await pilot.press("escape")
                await pilot.pause(0.1)

                # File should still exist
                assert jsonl_file.exists()


@pytest.mark.tui
class TestDeleteConfirmScreen:
    """Tests for delete confirmation with smart options."""

    @pytest.mark.asyncio
    async def test_delete_current_session_cache_only_keeps_jsonl(self):
        """Test delete with 'c' (cache only) keeps JSONL file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            with patch.object(
                app.cache_manager, "delete_session", return_value=True
            ) as mock_delete:
                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    app.sessions = mock_session_data
                    app.selected_session_id = "session-123"

                    # Trigger delete (opens modal)
                    await pilot.press("d")
                    await pilot.pause(0.1)

                    # Choose cache only with 'c'
                    await pilot.press("c")
                    await pilot.pause(0.1)

                    # JSONL should still exist
                    assert jsonl_file.exists()
                    mock_delete.assert_called_once_with("session-123")

    @pytest.mark.asyncio
    async def test_delete_current_session_both_deletes_jsonl(self):
        """Test delete with 'b' (both) deletes JSONL file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            with patch.object(
                app.cache_manager, "delete_session", return_value=True
            ) as mock_delete:
                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    app.sessions = mock_session_data
                    app.selected_session_id = "session-123"

                    assert jsonl_file.exists()

                    # Trigger delete (opens modal)
                    await pilot.press("d")
                    await pilot.pause(0.1)

                    # Choose both with 'b'
                    await pilot.press("b")
                    await pilot.pause(0.1)

                    # JSONL should be deleted
                    assert not jsonl_file.exists()
                    mock_delete.assert_called_once_with("session-123")

    @pytest.mark.asyncio
    async def test_delete_archived_session_with_enter_key(self):
        """Test deleting archived session with Enter key."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_archived_data = {
                "session-archived": SessionCacheData(
                    session_id="session-archived",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            with patch.object(
                app.cache_manager, "delete_session", return_value=True
            ) as mock_delete:
                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    app.sessions = {}
                    app.archived_sessions = mock_archived_data
                    app.selected_session_id = "session-archived"

                    # Trigger delete (opens modal)
                    await pilot.press("d")
                    await pilot.pause(0.1)

                    # Confirm with Enter (for archived sessions)
                    await pilot.press("enter")
                    await pilot.pause(0.1)

                    mock_delete.assert_called_once_with("session-archived")

    @pytest.mark.asyncio
    async def test_delete_cancel_n_key(self):
        """Test cancelling delete with 'n' key."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            with patch.object(
                app.cache_manager, "delete_session", return_value=True
            ) as mock_delete:
                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    app.sessions = mock_session_data
                    app.selected_session_id = "session-123"

                    # Trigger delete (opens modal)
                    await pilot.press("d")
                    await pilot.pause(0.1)

                    # Cancel with 'n'
                    await pilot.press("n")
                    await pilot.pause(0.1)

                    # Should not have deleted
                    mock_delete.assert_not_called()
                    assert jsonl_file.exists()


@pytest.mark.tui
class TestArchiveActionEdgeCases:
    """Edge case tests for the archive session action."""

    @pytest.mark.asyncio
    async def test_archive_action_no_selection(self):
        """Test archive action with no session selected shows warning."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Ensure no session is selected
                app.selected_session_id = None

                # Try to archive - should notify warning
                await pilot.press("a")
                await pilot.pause(0.1)

                # No modal should be pushed (we can't easily check notifications)
                # but at least verify no crash occurred

    @pytest.mark.asyncio
    async def test_archive_action_on_archived_session_shows_warning(self):
        """Test archive action on already archived session shows warning."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            mock_session_data = {
                "session-archived": SessionCacheData(
                    session_id="session-archived",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Set up archived session
                app.archived_sessions = mock_session_data
                app.sessions = {}
                app.selected_session_id = "session-archived"

                # Try to archive - should notify warning (already archived)
                await pilot.press("a")
                await pilot.pause(0.1)


@pytest.mark.tui
class TestDeleteActionEdgeCases:
    """Edge case tests for the delete session action."""

    @pytest.mark.asyncio
    async def test_delete_action_no_selection(self):
        """Test delete action with no session selected shows warning."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir)
            jsonl_file = project_path / "session-123.jsonl"
            jsonl_file.write_text('{"type":"user"}\n', encoding="utf-8")

            app = SessionBrowser(project_path)

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Ensure no session is selected
                app.selected_session_id = None

                # Try to delete - should notify warning
                await pilot.press("d")
                await pilot.pause(0.1)


@pytest.mark.tui
class TestRestoreWithMkdir:
    """Tests for restore action creating directory if needed."""

    @pytest.mark.asyncio
    async def test_restore_creates_directory_if_missing(self):
        """Test that restore creates the project directory if it was deleted."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "deleted_project"
            # Don't create the directory - it should be created on restore

            app = SessionBrowser(project_path, is_archived=True)

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            with (
                patch.object(
                    app.cache_manager,
                    "export_session_to_jsonl",
                    return_value=['{"type":"user"}'],
                ),
                patch.object(
                    app.cache_manager, "get_cached_project_data"
                ) as mock_cache,
                patch.object(
                    app.cache_manager, "get_archived_sessions", return_value={}
                ),
            ):
                mock_cache.return_value = Mock(
                    sessions=mock_session_data,
                    working_directories=[str(project_path)],
                )

                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    # Set up archived session
                    app.archived_sessions = mock_session_data
                    app.selected_session_id = "session-123"

                    # Directory should not exist
                    assert not project_path.exists()

                    # Trigger restore
                    app.action_restore_jsonl()
                    await pilot.pause(0.1)

                    # Directory should now exist
                    assert project_path.exists()

                    # JSONL file should be created
                    assert (project_path / "session-123.jsonl").exists()


@pytest.mark.tui
class TestProjectSelector:
    """Tests for the ProjectSelector TUI."""

    @pytest.mark.asyncio
    async def test_enter_key_selects_project(self):
        """Test that Enter key selects the highlighted project."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project1 = Path(temp_dir) / "project1"
            project1.mkdir()
            (project1 / "session-1.jsonl").write_text(
                '{"type":"user"}\n', encoding="utf-8"
            )

            project2 = Path(temp_dir) / "project2"
            project2.mkdir()
            (project2 / "session-2.jsonl").write_text(
                '{"type":"user"}\n', encoding="utf-8"
            )

            app = ProjectSelector(
                projects=[project1, project2],
                matching_projects=[],
                archived_projects=set(),
            )

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Select first project and press Enter
                await pilot.press("enter")
                await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_escape_key_quits(self):
        """Test that Escape key quits the application."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project1 = Path(temp_dir) / "project1"
            project1.mkdir()

            app = ProjectSelector(
                projects=[project1],
                matching_projects=[],
                archived_projects=set(),
            )

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Press Escape to quit
                await pilot.press("escape")
                await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_archive_project_action(self):
        """Test archiving a project deletes JSONL files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project1"
            project_path.mkdir()
            jsonl1 = project_path / "session-1.jsonl"
            jsonl2 = project_path / "session-2.jsonl"
            jsonl1.write_text('{"type":"user"}\n', encoding="utf-8")
            jsonl2.write_text('{"type":"user"}\n', encoding="utf-8")

            app = ProjectSelector(
                projects=[project_path],
                matching_projects=[],
                archived_projects=set(),
            )

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Select the project
                app.selected_project_path = project_path

                # Both JSONL files should exist
                assert jsonl1.exists()
                assert jsonl2.exists()

                # Press 'a' to archive and then confirm
                await pilot.press("a")
                await pilot.pause(0.1)
                await pilot.press("y")
                await pilot.pause(0.1)

                # JSONL files should be deleted
                assert not jsonl1.exists()
                assert not jsonl2.exists()

                # Project should now be in archived set
                assert project_path in app.archived_projects

    @pytest.mark.asyncio
    async def test_archive_project_already_archived_shows_warning(self):
        """Test archiving an already archived project shows warning."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project1"
            project_path.mkdir()

            app = ProjectSelector(
                projects=[project_path],
                matching_projects=[],
                archived_projects={project_path},  # Already archived
            )

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Select the archived project
                app.selected_project_path = project_path

                # Try to archive - should show warning
                await pilot.press("a")
                await pilot.pause(0.1)

    @pytest.mark.asyncio
    async def test_delete_project_cache_only(self):
        """Test deleting project cache only keeps JSONL files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project1"
            project_path.mkdir()
            jsonl = project_path / "session-1.jsonl"
            jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

            app = ProjectSelector(
                projects=[project_path],
                matching_projects=[],
                archived_projects=set(),
            )

            with patch.object(CacheManager, "clear_cache"):
                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    # Select the project
                    app.selected_project_path = project_path

                    # Press 'd' to delete and choose cache only
                    await pilot.press("d")
                    await pilot.pause(0.1)
                    await pilot.press("c")  # Cache only
                    await pilot.pause(0.1)

                    # JSONL file should still exist
                    assert jsonl.exists()

    @pytest.mark.asyncio
    async def test_delete_project_both(self):
        """Test deleting project cache and JSONL files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project1"
            project_path.mkdir()
            jsonl = project_path / "session-1.jsonl"
            jsonl.write_text('{"type":"user"}\n', encoding="utf-8")

            app = ProjectSelector(
                projects=[project_path],
                matching_projects=[],
                archived_projects=set(),
            )

            with patch.object(CacheManager, "clear_cache"):
                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    # Select the project
                    app.selected_project_path = project_path

                    assert jsonl.exists()

                    # Press 'd' to delete and choose both
                    await pilot.press("d")
                    await pilot.pause(0.1)
                    await pilot.press("b")  # Both
                    await pilot.pause(0.1)

                    # JSONL file should be deleted
                    assert not jsonl.exists()

    @pytest.mark.asyncio
    async def test_restore_project_creates_directory(self):
        """Test restoring a project creates directory if missing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "deleted_project"
            # Don't create the directory

            mock_session_data = {
                "session-123": SessionCacheData(
                    session_id="session-123",
                    first_timestamp="2025-01-01T10:00:00Z",
                    last_timestamp="2025-01-01T10:01:00Z",
                    message_count=1,
                    first_user_message="Test",
                    total_input_tokens=10,
                    total_output_tokens=10,
                ),
            }

            app = ProjectSelector(
                projects=[project_path],
                matching_projects=[],
                archived_projects={project_path},  # Archived project
            )

            with (
                patch.object(CacheManager, "get_cached_project_data") as mock_cache,
                patch.object(
                    CacheManager,
                    "export_session_to_jsonl",
                    return_value=['{"type":"user"}'],
                ),
            ):
                mock_cache.return_value = Mock(sessions=mock_session_data)

                async with app.run_test() as pilot:
                    await pilot.pause(0.2)

                    # Select the project
                    app.selected_project_path = project_path

                    # Directory should not exist
                    assert not project_path.exists()

                    # Press 'r' to restore and confirm
                    await pilot.press("r")
                    await pilot.pause(0.1)
                    await pilot.press("y")
                    await pilot.pause(0.1)

                    # Directory should now exist
                    assert project_path.exists()

    @pytest.mark.asyncio
    async def test_restore_project_not_archived_shows_warning(self):
        """Test restoring a non-archived project shows warning."""
        with tempfile.TemporaryDirectory() as temp_dir:
            project_path = Path(temp_dir) / "project1"
            project_path.mkdir()
            (project_path / "session-1.jsonl").write_text(
                '{"type":"user"}\n', encoding="utf-8"
            )

            app = ProjectSelector(
                projects=[project_path],
                matching_projects=[],
                archived_projects=set(),  # Not archived
            )

            async with app.run_test() as pilot:
                await pilot.pause(0.2)

                # Select the non-archived project
                app.selected_project_path = project_path

                # Try to restore - should show warning
                await pilot.press("r")
                await pilot.pause(0.1)


@pytest.mark.tui
class TestMarkdownViewerScreen:
    """Tests for the MarkdownViewerScreen modal."""

    @pytest.mark.asyncio
    async def test_toc_toggle_binding_exists(self):
        """Test that 't' key binding exists for ToC toggle."""
        from claude_code_log.tui import MarkdownViewerScreen

        binding_keys = [
            b.key if hasattr(b, "key") else b[0] for b in MarkdownViewerScreen.BINDINGS
        ]
        assert "t" in binding_keys, "Should have 't' binding for ToC toggle"

    @pytest.mark.asyncio
    async def test_toc_toggle_action_toggles_visibility(self):
        """Test that pressing 't' toggles ToC visibility."""
        from claude_code_log.tui import MarkdownViewerScreen
        from textual.app import App
        from textual.widgets import MarkdownViewer

        content = "# Heading 1\n\nSome content\n\n## Heading 2\n\nMore content"
        screen = MarkdownViewerScreen(content, "Test Title")

        class TestApp(App):
            def compose(self):
                yield from []

        app = TestApp()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause(0.3)

            viewer = screen.query_one("#md-viewer", MarkdownViewer)

            # Initial state: ToC visible
            assert viewer.show_table_of_contents is True

            # Press 't' to toggle
            await pilot.press("t")
            await pilot.pause(0.1)

            # ToC should now be hidden
            assert viewer.show_table_of_contents is False

            # Press 't' again
            await pilot.press("t")
            await pilot.pause(0.1)

            # ToC should be visible again
            assert viewer.show_table_of_contents is True

    @pytest.mark.asyncio
    async def test_safe_markdown_viewer_overrides_go(self):
        """Test that SafeMarkdownViewer overrides the go method."""
        from claude_code_log.tui import SafeMarkdownViewer
        from textual.widgets import MarkdownViewer

        # SafeMarkdownViewer should have its own go method
        assert "go" in SafeMarkdownViewer.__dict__, "Should override go method"
        # And it should be different from the parent
        assert SafeMarkdownViewer.go is not MarkdownViewer.go

    @pytest.mark.asyncio
    async def test_file_link_click_does_not_crash(self):
        """Test that clicking file link shows notification instead of crashing."""
        from claude_code_log.tui import MarkdownViewerScreen, SafeMarkdownViewer
        from textual.app import App
        from textual.widgets.markdown import Markdown

        content = "# Test\n\n[Back to combined](combined_transcripts.md)"
        screen = MarkdownViewerScreen(content, "Link Test")

        class TestApp(App):
            def compose(self):
                yield from []

        app = TestApp()
        notifications = []

        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause(0.3)

            # Track notifications on the viewer (where they're called from)
            viewer = screen.query_one("#md-viewer", SafeMarkdownViewer)
            original_notify = viewer.notify

            def tracking_notify(
                message: str,
                *,
                title: str = "",
                severity: str = "information",
                timeout: float | None = None,
                markup: bool = True,
            ) -> None:
                notifications.append(str(message))
                original_notify(
                    message,
                    title=title,
                    severity=severity,  # type: ignore[arg-type]
                    timeout=timeout,
                    markup=markup,
                )

            viewer.notify = tracking_notify  # type: ignore[method-assign]

            # Simulate link click by posting the event
            markdown_widget = viewer.query_one(Markdown)
            markdown_widget.post_message(
                Markdown.LinkClicked(markdown_widget, "combined_transcripts.md")
            )
            await pilot.pause(0.2)

            # Should not crash - screen still mounted
            assert screen.is_mounted
            # Should have shown a notification
            assert len(notifications) > 0
            assert any("not supported" in n.lower() for n in notifications)

    @pytest.mark.asyncio
    async def test_http_link_opens_browser(self):
        """Test that HTTP links open in browser."""
        from claude_code_log.tui import MarkdownViewerScreen, SafeMarkdownViewer
        from textual.app import App
        from textual.widgets.markdown import Markdown

        content = "# Test\n\n[Example](https://example.com)"
        screen = MarkdownViewerScreen(content, "Link Test")

        class TestApp(App):
            def compose(self):
                yield from []

        app = TestApp()

        with patch("claude_code_log.tui.webbrowser.open") as mock_open:
            async with app.run_test() as pilot:
                app.push_screen(screen)
                await pilot.pause(0.3)

                viewer = screen.query_one("#md-viewer", SafeMarkdownViewer)
                markdown_widget = viewer.query_one(Markdown)
                markdown_widget.post_message(
                    Markdown.LinkClicked(markdown_widget, "https://example.com")
                )
                await pilot.pause(0.2)

                # Should be called at least once (may be called twice due to event propagation)
                mock_open.assert_called_with("https://example.com")
                assert mock_open.call_count >= 1


@pytest.mark.tui
class TestMarkdownViewerPagination:
    """Tests for pagination in MarkdownViewerScreen."""

    @pytest.mark.asyncio
    async def test_pagination_constants_defined(self):
        """Test that pagination constants exist."""
        from claude_code_log.tui import MarkdownViewerScreen

        assert hasattr(MarkdownViewerScreen, "PAGE_SIZE_CHARS"), (
            "Should have PAGE_SIZE_CHARS constant"
        )
        assert MarkdownViewerScreen.PAGE_SIZE_CHARS > 0

    @pytest.mark.asyncio
    async def test_small_content_no_pagination(self):
        """Test that small content loads without pagination controls."""
        from claude_code_log.tui import MarkdownViewerScreen
        from textual.app import App

        small_content = "# Small\n\nJust a bit of content."
        screen = MarkdownViewerScreen(small_content, "Small Test")

        class TestApp(App):
            def compose(self):
                yield from []

        app = TestApp()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause(0.3)

            # Should NOT have pagination controls
            try:
                screen.query_one("#pagination-controls")
                assert False, "Small content should not show pagination controls"
            except NoMatches:
                pass  # Expected - no pagination for small content

    @pytest.mark.asyncio
    async def test_large_content_shows_pagination(self):
        """Test that large content shows pagination controls."""
        from claude_code_log.tui import MarkdownViewerScreen
        from textual.app import App

        # Generate content larger than PAGE_SIZE_CHARS to trigger pagination
        # Use line breaks so the algorithm can split properly
        page_size = MarkdownViewerScreen.PAGE_SIZE_CHARS
        line = "Content line with some text here.\n"
        num_lines = int(page_size * 2.5 / len(line))
        large_content = "# Large Session\n\n" + (line * num_lines)

        screen = MarkdownViewerScreen(large_content, "Large Test")

        # Screen should be paginated (test without UI for speed)
        assert screen._is_paginated
        assert len(screen._pages) >= 2

        class TestApp(App):
            def compose(self):
                yield from []

        app = TestApp()
        async with app.run_test() as pilot:
            app.push_screen(screen)
            await pilot.pause(0.5)

            # Should have pagination controls
            controls = screen.query_one("#pagination-controls")
            assert controls is not None

    @pytest.mark.asyncio
    async def test_pagination_bindings_exist(self):
        """Test that pagination key bindings exist."""
        from claude_code_log.tui import MarkdownViewerScreen

        binding_keys = [
            b.key if hasattr(b, "key") else b[0] for b in MarkdownViewerScreen.BINDINGS
        ]
        assert "n" in binding_keys, "Should have 'n' binding for next page"
        assert "p" in binding_keys, "Should have 'p' binding for previous page"
        assert "right" in binding_keys, (
            "Should have 'right' arrow binding for next page"
        )
        assert "left" in binding_keys, "Should have 'left' arrow binding for prev page"

    @pytest.mark.asyncio
    async def test_next_page_action_updates_state(self):
        """Test that action_next_page advances internal page state."""
        from claude_code_log.tui import MarkdownViewerScreen

        # Generate content larger than PAGE_SIZE_CHARS (creates 3+ pages)
        # Use line breaks so the algorithm can split properly
        page_size = MarkdownViewerScreen.PAGE_SIZE_CHARS
        line = "Content line with some text here.\n"
        num_lines = int(page_size * 2.5 / len(line))
        large_content = "# Large Session\n\n" + (line * num_lines)

        screen = MarkdownViewerScreen(large_content, "Pagination Test")

        # Initial page should be 0
        assert screen._current_page == 0
        assert screen._is_paginated
        assert len(screen._pages) >= 3, f"Expected 3+ pages, got {len(screen._pages)}"

        # Call action directly (bypass UI)
        screen.action_next_page()
        assert screen._current_page == 1

        screen.action_next_page()
        assert screen._current_page == 2

    @pytest.mark.asyncio
    async def test_prev_page_action_updates_state(self):
        """Test that action_prev_page goes to previous page."""
        from claude_code_log.tui import MarkdownViewerScreen

        # Generate content larger than PAGE_SIZE_CHARS (creates 3+ pages)
        # Use line breaks so the algorithm can split properly
        page_size = MarkdownViewerScreen.PAGE_SIZE_CHARS
        line = "Content line with some text here.\n"
        num_lines = int(page_size * 2.5 / len(line))
        large_content = "# Large Session\n\n" + (line * num_lines)

        screen = MarkdownViewerScreen(large_content, "Pagination Test")

        # Verify we have enough pages
        assert len(screen._pages) >= 3, f"Expected 3+ pages, got {len(screen._pages)}"

        # Go forward first
        screen.action_next_page()
        screen.action_next_page()
        assert screen._current_page == 2

        # Now go back
        screen.action_prev_page()
        assert screen._current_page == 1

        screen.action_prev_page()
        assert screen._current_page == 0

    @pytest.mark.asyncio
    async def test_page_boundaries_respected(self):
        """Test can't go past first or last page."""
        from claude_code_log.tui import MarkdownViewerScreen

        # Generate content larger than PAGE_SIZE_CHARS
        # Use line breaks so the algorithm can split properly
        page_size = MarkdownViewerScreen.PAGE_SIZE_CHARS
        line = "Content line with some text here.\n"
        num_lines = int(page_size * 2.5 / len(line))
        large_content = "# Large Session\n\n" + (line * num_lines)

        screen = MarkdownViewerScreen(large_content, "Pagination Test")

        # On first page, prev should stay on first page
        assert screen._current_page == 0
        screen.action_prev_page()
        assert screen._current_page == 0

        # Go to last page
        total_pages = len(screen._pages)
        for _ in range(total_pages + 5):  # Call more than needed
            screen.action_next_page()

        # Should be on last page, not beyond
        assert screen._current_page == total_pages - 1

        # Try to go beyond last page
        screen.action_next_page()
        assert screen._current_page == total_pages - 1

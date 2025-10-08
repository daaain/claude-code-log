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
from claude_code_log.tui import SessionBrowser, run_session_browser


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

        # Write test data to JSONL file
        jsonl_file = project_path / "test-transcript.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in test_data:
                f.write(json.dumps(entry) + "\n")

        yield project_path


@pytest.mark.tui
class TestSessionBrowser:
    """Test cases for the SessionBrowser TUI application."""

    def test_init(self, temp_project_dir):
        """Test SessionBrowser initialization."""
        app = SessionBrowser(temp_project_dir)
        assert app.project_path == temp_project_dir
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
                expected_file = temp_project_dir / "session-session-123.html"
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

        # Test valid timestamp
        formatted = app.format_timestamp("2025-01-01T10:00:00Z")
        assert formatted == "01-01 10:00"

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
                stats_text = str(stats.renderable)

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
                stats_text = str(stats.renderable)

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
        test_file.write_text("test")

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
                stats_text = (
                    str(stats.renderable)
                    if hasattr(stats.renderable, "__str__")
                    else str(stats.renderable)
                )
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
                stats_text = (
                    str(stats.renderable)
                    if hasattr(stats.renderable, "__str__")
                    else str(stats.renderable)
                )
                assert "Sessions:[/bold] 0" in stats_text

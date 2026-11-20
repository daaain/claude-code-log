#!/usr/bin/env python3
"""Test the TUI row expansion functionality."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from textual.widgets import Static, DataTable
from claude_code_log.tui import SessionBrowser
from claude_code_log.cache import SessionCacheData


@pytest.mark.tui
def test_row_expansion_functionality():
    """Test that row expansion shows full summary and first user message."""
    # Create a temporary project path
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)

        # Create session browser instance
        browser = SessionBrowser(project_path)

        # Mock session data
        browser.sessions = {
            "test-session-123": SessionCacheData(
                session_id="test-session-123",
                summary="This is a comprehensive test session summary that should be displayed in full when expanded",
                first_timestamp="2024-01-01T00:00:00Z",
                last_timestamp="2024-01-01T00:01:00Z",
                message_count=5,
                first_user_message="This is the first user message that should be displayed in full when the row is expanded",
                cwd="/test/working/directory",
                total_input_tokens=150,
                total_output_tokens=75,
                total_cache_creation_tokens=25,
                total_cache_read_tokens=10,
            )
        }

        browser.selected_session_id = "test-session-123"

        # Create mock expanded content widget
        expanded_content = Mock(spec=Static)
        expanded_content.content = ""
        mock_styles = Mock()
        mock_styles.display = "none"
        expanded_content.styles = mock_styles

        # Track the content that gets updated
        updated_content = ""

        def track_update(content):
            nonlocal updated_content
            updated_content = content

        expanded_content.update.side_effect = track_update

        # Create mock sessions table
        sessions_table = Mock(spec=DataTable)

        # Mock query_one to return our widgets
        def mock_query_one(selector, expected_type=None):
            if selector == "#expanded-content":
                return expanded_content
            elif selector == "#sessions-table":
                return sessions_table
            return None

        with patch.object(browser, "query_one", side_effect=mock_query_one):
            # Initially not expanded
            assert not browser.is_expanded

            # Toggle expansion
            browser.action_toggle_expanded()

            # Should now be expanded
            assert browser.is_expanded

            # Check the content
            content = str(updated_content)

            # Should contain session ID
            assert "Session ID:" in content
            assert "test-session-123" in content

            # Should contain full summary
            assert "Summary:" in content
            assert (
                "This is a comprehensive test session summary that should be displayed in full when expanded"
                in content
            )

            # Should contain first user message
            assert "First User Message:" in content
            assert (
                "This is the first user message that should be displayed in full when the row is expanded"
                in content
            )

            # Should contain working directory
            assert "Working Directory:" in content
            assert "/test/working/directory" in content

            # Should contain token usage
            assert "Token Usage:" in content
            assert "Input: 150" in content
            assert "Output: 75" in content
            assert "Cache Creation: 25" in content
            assert "Cache Read: 10" in content

            # Toggle off
            browser.action_toggle_expanded()
            assert not browser.is_expanded

            # Content should be cleared
            content = str(updated_content)
            assert content == ""


@pytest.mark.tui
def test_row_expansion_with_missing_data():
    """Test row expansion when some data is missing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        browser = SessionBrowser(project_path)

        # Mock session data with missing summary and cwd
        browser.sessions = {
            "test-session-456": SessionCacheData(
                session_id="test-session-456",
                summary=None,  # No summary
                first_timestamp="2024-01-01T00:00:00Z",
                last_timestamp="2024-01-01T00:01:00Z",
                message_count=2,
                first_user_message="Only first user message available",
                cwd=None,  # No working directory
                total_input_tokens=0,
                total_output_tokens=0,
                total_cache_creation_tokens=0,
                total_cache_read_tokens=0,
            )
        }

        browser.selected_session_id = "test-session-456"

        # Create mock expanded content widget
        expanded_content = Mock(spec=Static)
        expanded_content.content = ""
        mock_styles = Mock()
        mock_styles.display = "none"
        expanded_content.styles = mock_styles

        # Track the content that gets updated
        updated_content = ""

        def track_update(content):
            nonlocal updated_content
            updated_content = content

        expanded_content.update.side_effect = track_update

        # Create mock sessions table
        sessions_table = Mock(spec=DataTable)

        # Mock query_one to return our widgets
        def mock_query_one(selector, expected_type=None):
            if selector == "#expanded-content":
                return expanded_content
            elif selector == "#sessions-table":
                return sessions_table
            return None

        with patch.object(browser, "query_one", side_effect=mock_query_one):
            # Toggle expansion
            browser.action_toggle_expanded()

            # Should be expanded
            assert browser.is_expanded

            # Check the content
            content = str(updated_content)

            # Should contain session ID
            assert "Session ID:" in content
            assert "test-session-456" in content

            # Should NOT contain summary section (since it's None)
            assert "Summary:" not in content

            # Should contain first user message
            assert "First User Message:" in content
            assert "Only first user message available" in content

            # Should NOT contain working directory section (since it's None)
            assert "Working Directory:" not in content

            # Should NOT contain token usage section (since all tokens are 0)
            assert "Token Usage:" not in content


@pytest.mark.tui
def test_row_expansion_with_no_selected_session():
    """Test that expansion does nothing when no session is selected."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        browser = SessionBrowser(project_path)

        # No selected session
        browser.selected_session_id = None
        browser.sessions = {}

        # Create mock expanded content widget
        expanded_content = Mock(spec=Static)
        expanded_content.content = ""

        # Track the content that gets updated
        updated_content = ""

        def track_update(content):
            nonlocal updated_content
            updated_content = content

        expanded_content.update.side_effect = track_update

        # Mock query_one to return our widget
        def mock_query_one(selector, expected_type=None):
            if selector == "#expanded-content":
                return expanded_content
            return None

        with patch.object(browser, "query_one", side_effect=mock_query_one):
            # Initially not expanded
            assert not browser.is_expanded

            # Try to toggle expansion
            browser.action_toggle_expanded()

            # Should still not be expanded
            assert not browser.is_expanded

            # Content should be empty (method returns early, so update isn't called)
            content = str(updated_content)
            assert content == ""

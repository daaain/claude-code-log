#!/usr/bin/env python3
"""Tests for pagination functionality."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_log.cache import (
    CacheManager,
    SessionCacheData,
)
from claude_code_log.converter import (
    _get_page_html_path,
    _assign_sessions_to_pages,
)


class TestPageHtmlPath:
    """Tests for _get_page_html_path function."""

    def test_page_1_returns_base_filename(self):
        """Page 1 should return combined_transcripts.html."""
        assert _get_page_html_path(1) == "combined_transcripts.html"

    def test_page_2_returns_numbered_filename(self):
        """Page 2 should return combined_transcripts_2.html."""
        assert _get_page_html_path(2) == "combined_transcripts_2.html"

    def test_page_10_returns_numbered_filename(self):
        """Page 10 should return combined_transcripts_10.html."""
        assert _get_page_html_path(10) == "combined_transcripts_10.html"


class TestAssignSessionsToPages:
    """Tests for _assign_sessions_to_pages function."""

    def _make_session(
        self, session_id: str, message_count: int, timestamp: str
    ) -> SessionCacheData:
        """Helper to create a SessionCacheData instance."""
        return SessionCacheData(
            session_id=session_id,
            message_count=message_count,
            first_timestamp=timestamp,
            last_timestamp=timestamp,
            first_user_message="Test message",
            total_input_tokens=0,
            total_output_tokens=0,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

    def test_single_session_below_threshold(self):
        """Single session below page_size should result in one page."""
        sessions = {
            "s1": self._make_session("s1", 100, "2023-01-01T10:00:00Z"),
        }
        pages = _assign_sessions_to_pages(sessions, page_size=5000)

        assert len(pages) == 1
        assert pages[0] == ["s1"]

    def test_multiple_sessions_below_threshold(self):
        """Multiple sessions below page_size should be on one page."""
        sessions = {
            "s1": self._make_session("s1", 1000, "2023-01-01T10:00:00Z"),
            "s2": self._make_session("s2", 2000, "2023-01-02T10:00:00Z"),
            "s3": self._make_session("s3", 1500, "2023-01-03T10:00:00Z"),
        }
        pages = _assign_sessions_to_pages(sessions, page_size=5000)

        assert len(pages) == 1
        assert sorted(pages[0]) == ["s1", "s2", "s3"]

    def test_session_exceeds_threshold_creates_new_page(self):
        """When adding a session exceeds threshold, it becomes last on current page."""
        sessions = {
            "s1": self._make_session("s1", 3000, "2023-01-01T10:00:00Z"),
            "s2": self._make_session("s2", 3000, "2023-01-02T10:00:00Z"),
            "s3": self._make_session("s3", 2000, "2023-01-03T10:00:00Z"),
        }
        pages = _assign_sessions_to_pages(sessions, page_size=5000)

        # s1 (3000) + s2 (3000) > 5000, so s2 becomes last on page 1
        # s3 (2000) goes to page 2
        assert len(pages) == 2
        assert pages[0] == ["s1", "s2"]
        assert pages[1] == ["s3"]

    def test_large_session_allows_overflow(self):
        """A single large session is allowed to exceed page_size (no splitting)."""
        sessions = {
            "s1": self._make_session("s1", 10000, "2023-01-01T10:00:00Z"),
        }
        pages = _assign_sessions_to_pages(sessions, page_size=5000)

        # Single session, even if large, stays on one page
        assert len(pages) == 1
        assert pages[0] == ["s1"]

    def test_sessions_sorted_chronologically(self):
        """Sessions should be assigned to pages in chronological order."""
        sessions = {
            "s3": self._make_session("s3", 1000, "2023-01-03T10:00:00Z"),
            "s1": self._make_session("s1", 1000, "2023-01-01T10:00:00Z"),
            "s2": self._make_session("s2", 1000, "2023-01-02T10:00:00Z"),
        }
        pages = _assign_sessions_to_pages(sessions, page_size=5000)

        assert len(pages) == 1
        # Should be in chronological order
        assert pages[0] == ["s1", "s2", "s3"]

    def test_multiple_pages_with_overflow(self):
        """Test complex pagination with multiple pages."""
        sessions = {
            "s1": self._make_session("s1", 2000, "2023-01-01T10:00:00Z"),
            "s2": self._make_session("s2", 4000, "2023-01-02T10:00:00Z"),  # exceeds
            "s3": self._make_session("s3", 3000, "2023-01-03T10:00:00Z"),
            "s4": self._make_session("s4", 3000, "2023-01-04T10:00:00Z"),  # exceeds
            "s5": self._make_session("s5", 1000, "2023-01-05T10:00:00Z"),
        }
        pages = _assign_sessions_to_pages(sessions, page_size=5000)

        # s1 (2000) + s2 (4000) > 5000, s2 last on page 1
        # s3 (3000) + s4 (3000) > 5000, s4 last on page 2
        # s5 (1000) on page 3
        assert len(pages) == 3
        assert pages[0] == ["s1", "s2"]
        assert pages[1] == ["s3", "s4"]
        assert pages[2] == ["s5"]

    def test_empty_sessions(self):
        """Empty sessions dict should return empty list."""
        pages = _assign_sessions_to_pages({}, page_size=5000)
        assert pages == []


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def mock_version():
    """Mock library version for consistent testing."""
    return "1.0.0-test"


@pytest.fixture
def cache_manager(temp_project_dir, mock_version):
    """Create a cache manager for testing."""
    with patch("claude_code_log.cache.get_library_version", return_value=mock_version):
        return CacheManager(temp_project_dir, mock_version)


class TestPageCacheMethods:
    """Tests for page cache methods in CacheManager."""

    def test_get_page_count_empty(self, cache_manager):
        """get_page_count should return 0 when no pages exist."""
        assert cache_manager.get_page_count() == 0

    def test_get_page_size_config_empty(self, cache_manager):
        """get_page_size_config should return None when no pages exist."""
        assert cache_manager.get_page_size_config() is None

    def test_update_and_get_page_cache(self, cache_manager):
        """Test updating and retrieving page cache data."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1", "s2"],
            message_count=3000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-02T10:00:00Z",
            total_input_tokens=1000,
            total_output_tokens=500,
            total_cache_creation_tokens=200,
            total_cache_read_tokens=100,
        )

        page_data = cache_manager.get_page_data(1)
        assert page_data is not None
        assert page_data.page_number == 1
        assert page_data.html_path == "combined_transcripts.html"
        assert page_data.page_size_config == 5000
        assert page_data.session_ids == ["s1", "s2"]
        assert page_data.message_count == 3000
        assert page_data.first_timestamp == "2023-01-01T10:00:00Z"
        assert page_data.last_timestamp == "2023-01-02T10:00:00Z"
        assert page_data.total_input_tokens == 1000
        assert page_data.total_output_tokens == 500

    def test_get_page_count_after_adding_pages(self, cache_manager):
        """get_page_count should return correct count after adding pages."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        cache_manager.update_page_cache(
            page_number=2,
            html_path="combined_transcripts_2.html",
            page_size_config=5000,
            session_ids=["s2"],
            message_count=2000,
            first_timestamp="2023-01-02T10:00:00Z",
            last_timestamp="2023-01-02T11:00:00Z",
            total_input_tokens=200,
            total_output_tokens=100,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

        assert cache_manager.get_page_count() == 2

    def test_get_page_size_config_after_adding_page(self, cache_manager):
        """get_page_size_config should return the configured page size."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

        assert cache_manager.get_page_size_config() == 5000

    def test_is_page_stale_no_cache(self, cache_manager):
        """is_page_stale should return True when page not in cache."""
        is_stale, reason = cache_manager.is_page_stale(1, 5000)
        assert is_stale is True
        assert "not_cached" in reason or "not in cache" in reason.lower()

    def test_is_page_stale_page_size_changed(self, cache_manager):
        """is_page_stale should return True when page_size changed."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

        is_stale, reason = cache_manager.is_page_stale(1, 10000)  # Different page_size
        assert is_stale is True
        assert "page_size" in reason.lower() or "size" in reason.lower()

    def test_is_page_stale_session_missing(self, cache_manager, temp_project_dir):
        """is_page_stale should return True when a session is missing from sessions table."""
        # Create page cache entry referencing session "s1"
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        # Create the HTML file so it passes the file existence check
        (temp_project_dir / "combined_transcripts.html").write_text("<html></html>")

        # Don't add session "s1" to sessions table - it should be detected as missing
        # Mock is_html_outdated to skip HTML version check (tested separately)
        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(1, 5000)
        assert is_stale is True
        assert "session_missing" in reason

    def test_is_page_stale_message_count_changed(self, cache_manager, temp_project_dir):
        """is_page_stale should return True when session message count has changed."""
        # Create page cache entry with message_count=1000
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,  # Page expects 1000 messages
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        # Create the HTML file
        (temp_project_dir / "combined_transcripts.html").write_text("<html></html>")

        # Add session with different message count
        cache_manager.update_session_cache(
            {
                "s1": SessionCacheData(
                    session_id="s1",
                    message_count=1500,  # Different from page's 1000
                    first_timestamp="2023-01-01T10:00:00Z",
                    last_timestamp="2023-01-01T11:00:00Z",
                    first_user_message="Test",
                    total_input_tokens=100,
                    total_output_tokens=50,
                    total_cache_creation_tokens=0,
                    total_cache_read_tokens=0,
                )
            }
        )

        # Mock is_html_outdated to skip HTML version check (tested separately)
        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(1, 5000)
        assert is_stale is True
        assert "message_count" in reason

    def test_is_page_stale_timestamp_changed(self, cache_manager, temp_project_dir):
        """is_page_stale should return True when session last_timestamp has changed."""
        # Create page cache entry
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",  # Page expects this timestamp
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        # Create the HTML file
        (temp_project_dir / "combined_transcripts.html").write_text("<html></html>")

        # Add session with same message_count but different last_timestamp
        cache_manager.update_session_cache(
            {
                "s1": SessionCacheData(
                    session_id="s1",
                    message_count=1000,  # Same as page
                    first_timestamp="2023-01-01T10:00:00Z",
                    last_timestamp="2023-01-01T12:00:00Z",  # Different timestamp
                    first_user_message="Test",
                    total_input_tokens=100,
                    total_output_tokens=50,
                    total_cache_creation_tokens=0,
                    total_cache_read_tokens=0,
                )
            }
        )

        # Mock is_html_outdated to skip HTML version check (tested separately)
        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(1, 5000)
        assert is_stale is True
        assert "timestamp" in reason

    def test_is_page_stale_up_to_date(self, cache_manager, temp_project_dir):
        """is_page_stale should return False when page matches session data."""
        # Create page cache entry
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        # Create the HTML file
        (temp_project_dir / "combined_transcripts.html").write_text("<html></html>")

        # Add session with matching data
        cache_manager.update_session_cache(
            {
                "s1": SessionCacheData(
                    session_id="s1",
                    message_count=1000,  # Same as page
                    first_timestamp="2023-01-01T10:00:00Z",
                    last_timestamp="2023-01-01T11:00:00Z",  # Same as page
                    first_user_message="Test",
                    total_input_tokens=100,
                    total_output_tokens=50,
                    total_cache_creation_tokens=0,
                    total_cache_read_tokens=0,
                )
            }
        )

        # Mock is_html_outdated to skip HTML version check (tested separately)
        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(1, 5000)
        assert is_stale is False
        assert "up_to_date" in reason

    def _cache_single_session_page(self, cache_manager, temp_project_dir):
        """Cache a 1-session page whose session data matches exactly."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        (temp_project_dir / "combined_transcripts.html").write_text("<html></html>")
        cache_manager.update_session_cache(
            {
                "s1": SessionCacheData(
                    session_id="s1",
                    message_count=1000,
                    first_timestamp="2023-01-01T10:00:00Z",
                    last_timestamp="2023-01-01T11:00:00Z",
                    first_user_message="Test",
                    total_input_tokens=100,
                    total_output_tokens=50,
                    total_cache_creation_tokens=0,
                    total_cache_read_tokens=0,
                )
            }
        )

    def test_is_page_stale_sessions_added_to_page(
        self, cache_manager, temp_project_dir
    ):
        """A page that gains a session is stale even if its own sessions didn't change.

        Every other check reads the page's cached membership, so an added
        session is invisible without ``expected_session_ids`` (issue #308).
        """
        self._cache_single_session_page(cache_manager, temp_project_dir)

        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(
                1, 5000, expected_session_ids=["s1", "s2"]
            )
        assert is_stale is True
        assert reason == "sessions_changed"

    def test_is_page_stale_sessions_reordered_on_page(
        self, cache_manager, temp_project_dir
    ):
        """A page whose sessions shift order is stale — order is render order."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1", "s2"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        (temp_project_dir / "combined_transcripts.html").write_text("<html></html>")

        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(
                1, 5000, expected_session_ids=["s2", "s1"]
            )
        assert is_stale is True
        assert reason == "sessions_changed"

    def test_is_page_stale_up_to_date_with_matching_expected_sessions(
        self, cache_manager, temp_project_dir
    ):
        """Passing the same membership must not defeat the cache."""
        self._cache_single_session_page(cache_manager, temp_project_dir)

        with patch("claude_code_log.renderer.is_html_outdated", return_value=False):
            is_stale, reason = cache_manager.is_page_stale(
                1, 5000, expected_session_ids=["s1"]
            )
        assert is_stale is False
        assert reason == "up_to_date"

    def test_invalidate_all_pages(self, cache_manager):
        """invalidate_all_pages should remove all page cache entries."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        cache_manager.update_page_cache(
            page_number=2,
            html_path="combined_transcripts_2.html",
            page_size_config=5000,
            session_ids=["s2"],
            message_count=2000,
            first_timestamp="2023-01-02T10:00:00Z",
            last_timestamp="2023-01-02T11:00:00Z",
            total_input_tokens=200,
            total_output_tokens=100,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

        old_paths = cache_manager.invalidate_all_pages()

        assert len(old_paths) == 2
        assert cache_manager.get_page_count() == 0
        assert cache_manager.get_page_data(1) is None
        assert cache_manager.get_page_data(2) is None

    def test_get_all_pages(self, cache_manager):
        """get_all_pages should return all page cache entries."""
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=5000,
            session_ids=["s1"],
            message_count=1000,
            first_timestamp="2023-01-01T10:00:00Z",
            last_timestamp="2023-01-01T11:00:00Z",
            total_input_tokens=100,
            total_output_tokens=50,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )
        cache_manager.update_page_cache(
            page_number=2,
            html_path="combined_transcripts_2.html",
            page_size_config=5000,
            session_ids=["s2"],
            message_count=2000,
            first_timestamp="2023-01-02T10:00:00Z",
            last_timestamp="2023-01-02T11:00:00Z",
            total_input_tokens=200,
            total_output_tokens=100,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

        all_pages = cache_manager.get_all_pages()

        assert len(all_pages) == 2
        assert all_pages[0].page_number == 1
        assert all_pages[1].page_number == 2


# Integration tests for pagination with converter


def _create_session_messages(session_id: str, num_messages: int, base_timestamp: str):
    """Helper to create messages for a session."""
    messages = []
    for i in range(num_messages):
        # Alternate between user and assistant messages
        if i % 2 == 0:
            messages.append(
                {
                    "type": "user",
                    "uuid": f"{session_id}-user-{i}",
                    "timestamp": f"{base_timestamp}T{10 + i // 60:02d}:{i % 60:02d}:00Z",
                    "sessionId": session_id,
                    "version": "1.0.0",
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "user",
                    "cwd": "/test",
                    "message": {"role": "user", "content": f"Message {i} from user"},
                }
            )
        else:
            messages.append(
                {
                    "type": "assistant",
                    "uuid": f"{session_id}-assistant-{i}",
                    "timestamp": f"{base_timestamp}T{10 + i // 60:02d}:{i % 60:02d}:00Z",
                    "sessionId": session_id,
                    "version": "1.0.0",
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "assistant",
                    "cwd": "/test",
                    "requestId": f"req-{session_id}-{i}",
                    "message": {
                        "id": f"msg-{session_id}-{i}",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-3",
                        "content": [{"type": "text", "text": f"Response {i}"}],
                        "usage": {"input_tokens": 10, "output_tokens": 15},
                    },
                }
            )
    return messages


class TestPaginationIntegration:
    """Integration tests for pagination with the converter."""

    def test_small_project_no_pagination(self, temp_project_dir):
        """Projects below page_size should create single combined file."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create a project with 50 messages (below default 5000)
        jsonl_file = temp_project_dir / "session1.jsonl"
        messages = _create_session_messages("session1", 50, "2023-01-01")
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

        # Convert with default page_size
        output = convert_jsonl_to_html(temp_project_dir, page_size=5000, silent=True)

        # Should create single combined file
        assert output.name == "combined_transcripts.html"
        assert (temp_project_dir / "combined_transcripts.html").exists()
        assert not (temp_project_dir / "combined_transcripts_2.html").exists()

    def test_large_project_creates_multiple_pages(self, temp_project_dir):
        """Projects above page_size should create multiple page files."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create multiple sessions totaling > 30 messages with page_size=10
        for i, session_id in enumerate(
            ["session1", "session2", "session3", "session4"]
        ):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 15, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        # Convert with small page_size to force pagination
        output = convert_jsonl_to_html(temp_project_dir, page_size=20, silent=True)

        # Should create multiple page files
        assert output.name == "combined_transcripts.html"
        assert (temp_project_dir / "combined_transcripts.html").exists()
        # With 4 sessions x 15 messages = 60 messages, page_size=20
        # Should create at least 2 pages
        assert (temp_project_dir / "combined_transcripts_2.html").exists()

    def test_page_size_change_regenerates_all(self, temp_project_dir):
        """Changing page_size should regenerate all pages."""
        from claude_code_log.converter import convert_jsonl_to_html
        from claude_code_log.cache import CacheManager, get_library_version

        # Create sessions
        for i, session_id in enumerate(["session1", "session2", "session3"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 20, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        # First conversion with page_size=30
        convert_jsonl_to_html(temp_project_dir, page_size=30, silent=True)

        # Check cache has page_size=30
        cache_manager = CacheManager(temp_project_dir, get_library_version())
        assert cache_manager.get_page_size_config() == 30

        # Second conversion with different page_size=25
        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)

        # Cache should now have page_size=25
        cache_manager2 = CacheManager(temp_project_dir, get_library_version())
        assert cache_manager2.get_page_size_config() == 25

    def test_pagination_with_very_small_page_size(self, temp_project_dir):
        """Test pagination with very small page size respects session boundaries."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create 4 sessions with 10 messages each
        for i, session_id in enumerate(["s1", "s2", "s3", "s4"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 10, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        # Convert with tiny page_size=5 (each session has 10 messages)
        # New simpler pagination logic:
        # - Add session, then check if page > limit
        # - If over, close page immediately
        # s1: add, count=10 > 5 -> page 1 = [s1]
        # s2: add, count=10 > 5 -> page 2 = [s2]
        # s3: add, count=10 > 5 -> page 3 = [s3]
        # s4: add, count=10 > 5 -> page 4 = [s4]
        convert_jsonl_to_html(temp_project_dir, page_size=5, silent=True)

        # Should create 4 pages (one per session, each exceeds threshold)
        assert (temp_project_dir / "combined_transcripts.html").exists()
        assert (temp_project_dir / "combined_transcripts_2.html").exists()
        assert (temp_project_dir / "combined_transcripts_3.html").exists()
        assert (temp_project_dir / "combined_transcripts_4.html").exists()

    def test_pagination_html_contains_navigation(self, temp_project_dir):
        """Paginated pages should contain navigation links."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create 4 sessions that will span multiple pages
        # With page_size=15 and sessions of 10 messages:
        # s1 (10): page empty, add s1 (count=10)
        # s2 (10): 10+10 > 15 and page not empty -> s2 becomes last, page 1 = [s1, s2]
        # s3 (10): page empty, add s3 (count=10)
        # s4 (10): 10+10 > 15 and page not empty -> s4 becomes last, page 2 = [s3, s4]
        for i, session_id in enumerate(["s1", "s2", "s3", "s4"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 10, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        convert_jsonl_to_html(temp_project_dir, page_size=15, silent=True)

        # Check page 1 has Next link (pre-enabled when page exceeds threshold)
        page1_content = (temp_project_dir / "combined_transcripts.html").read_text(
            encoding="utf-8"
        )
        assert "Next" in page1_content or "combined_transcripts_2.html" in page1_content

        # Check page 2 has Previous link
        page2_content = (temp_project_dir / "combined_transcripts_2.html").read_text(
            encoding="utf-8"
        )
        assert (
            "Previous" in page2_content or "combined_transcripts.html" in page2_content
        )

    def test_page_contains_stats(self, temp_project_dir):
        """Paginated pages should contain stats (message count, date range)."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create sessions
        for i, session_id in enumerate(["s1", "s2"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 20, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        convert_jsonl_to_html(temp_project_dir, page_size=15, silent=True)

        # Check page contains stats
        page1_content = (temp_project_dir / "combined_transcripts.html").read_text(
            encoding="utf-8"
        )
        assert "messages" in page1_content.lower()
        assert "Page 1" in page1_content or "page-navigation" in page1_content


class TestPaginationPicksUpNewSessions:
    """Regression tests for issue #308.

    A rebuild recomputes the session→page assignment from scratch, but page
    staleness used to be judged purely on the sessions a page *already* held.
    A session that appeared without disturbing any of them — a new session
    landing on the partly-filled last page, or sessions copied in from another
    machine sorting into the middle by their original timestamps — was written
    to the cache and to its own ``session-*.html``, yet never rendered into any
    combined page. The combined output stayed frozen until it was deleted by
    hand.
    """

    @staticmethod
    def _write_session(project_dir: Path, session_id: str, date: str, count: int = 10):
        messages = _create_session_messages(session_id, count, date)
        with open(project_dir / f"{session_id}.jsonl", "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

    @staticmethod
    def _paginated_session_ids(project_dir: Path) -> list[str]:
        """Session ids reachable from the rendered pages, in page order."""
        from claude_code_log.cache import CacheManager, get_library_version

        cache_manager = CacheManager(project_dir, get_library_version())
        return [
            session_id
            for page in cache_manager.get_all_pages()
            for session_id in page.session_ids
        ]

    def test_new_session_appended_lands_on_last_page(self, temp_project_dir):
        """A newer session added later must reach the (partly filled) last page."""
        from claude_code_log.converter import convert_jsonl_to_html

        for i, session_id in enumerate(["s1", "s2", "s3", "s4"]):
            self._write_session(temp_project_dir, session_id, f"2023-01-0{i + 1}")

        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)
        assert "s5" not in self._paginated_session_ids(temp_project_dir)

        # A brand-new session, newer than every existing one. None of the
        # already-paginated sessions change.
        self._write_session(temp_project_dir, "s5", "2023-01-09")
        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)

        assert "s5" in self._paginated_session_ids(temp_project_dir)
        combined = "".join(
            p.read_text(encoding="utf-8")
            for p in sorted(temp_project_dir.glob("combined_transcripts*.html"))
        )
        assert "s5-user-0" in combined

    def test_imported_older_sessions_shift_existing_pages(self, temp_project_dir):
        """Sessions imported with older timestamps must re-flow the pages after them."""
        from claude_code_log.converter import convert_jsonl_to_html

        for i, session_id in enumerate(["s5", "s6", "s7", "s8"]):
            self._write_session(temp_project_dir, session_id, f"2023-01-0{i + 5}")

        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)
        before = self._paginated_session_ids(temp_project_dir)
        assert before == ["s5", "s6", "s7", "s8"]

        # Copied in from another machine: original (older) timestamps, so they
        # sort to the front and push every later session along.
        for i, session_id in enumerate(["s1", "s2"]):
            self._write_session(temp_project_dir, session_id, f"2023-01-0{i + 1}")
        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)

        after = self._paginated_session_ids(temp_project_dir)
        assert after == ["s1", "s2", "s5", "s6", "s7", "s8"]
        combined = "".join(
            p.read_text(encoding="utf-8")
            for p in sorted(temp_project_dir.glob("combined_transcripts*.html"))
        )
        for session_id in after:
            assert f"{session_id}-user-0" in combined

    def test_unchanged_project_still_skips_every_page(self, temp_project_dir):
        """The membership check must not cost incrementality on a no-op rebuild."""
        from claude_code_log.converter import convert_jsonl_to_html

        for i, session_id in enumerate(["s1", "s2", "s3", "s4"]):
            self._write_session(temp_project_dir, session_id, f"2023-01-0{i + 1}")

        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)
        pages = sorted(temp_project_dir.glob("combined_transcripts*.html"))
        assert len(pages) > 1
        before = {p: p.read_bytes() for p in pages}

        convert_jsonl_to_html(temp_project_dir, page_size=25, silent=True)

        for page, content in before.items():
            assert page.read_bytes() == content


class TestNextLinkInPlaceUpdate:
    """Tests for in-place next link updates."""

    def test_enable_next_link_removes_last_page_class(self, temp_project_dir):
        """_enable_next_link_on_previous_page should remove last-page class."""
        from claude_code_log.converter import (
            _enable_next_link_on_previous_page,
            _get_page_html_path,
        )

        # Create a page with hidden next link
        page_path = temp_project_dir / _get_page_html_path(1)
        page_path.write_text(
            """
        <!-- PAGINATION_NEXT_LINK_START -->
        <a href="combined_transcripts_2.html" class="page-nav-link next last-page">Next →</a>
        <!-- PAGINATION_NEXT_LINK_END -->
        """,
            encoding="utf-8",
        )

        result = _enable_next_link_on_previous_page(temp_project_dir, 1)

        assert result is True
        content = page_path.read_text(encoding="utf-8")
        assert "last-page" not in content
        assert 'class="page-nav-link next"' in content

    def test_enable_next_link_no_op_if_already_visible(self, temp_project_dir):
        """_enable_next_link_on_previous_page should not modify if already visible."""
        from claude_code_log.converter import (
            _enable_next_link_on_previous_page,
            _get_page_html_path,
        )

        page_path = temp_project_dir / _get_page_html_path(1)
        original_content = """
        <!-- PAGINATION_NEXT_LINK_START -->
        <a href="combined_transcripts_2.html" class="page-nav-link next">Next →</a>
        <!-- PAGINATION_NEXT_LINK_END -->
        """
        page_path.write_text(original_content, encoding="utf-8")

        result = _enable_next_link_on_previous_page(temp_project_dir, 1)

        assert result is False
        assert page_path.read_text(encoding="utf-8") == original_content

    def test_enable_next_link_handles_missing_file(self, temp_project_dir):
        """_enable_next_link_on_previous_page should handle missing files gracefully."""
        from claude_code_log.converter import _enable_next_link_on_previous_page

        result = _enable_next_link_on_previous_page(temp_project_dir, 99)

        assert result is False

    def test_enable_next_link_handles_invalid_page_number(self, temp_project_dir):
        """_enable_next_link_on_previous_page should handle invalid page numbers."""
        from claude_code_log.converter import _enable_next_link_on_previous_page

        result = _enable_next_link_on_previous_page(temp_project_dir, 0)
        assert result is False

        result = _enable_next_link_on_previous_page(temp_project_dir, -1)
        assert result is False


class TestPaginationNextLinkVisibility:
    """Integration tests for next link visibility across pages."""

    def test_single_page_has_hidden_next_link(self, temp_project_dir):
        """Single page should have next link with last-page class when pagination is enabled."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create a session with enough messages to trigger pagination
        # but only enough to fit on one page
        jsonl_file = temp_project_dir / "session1.jsonl"
        messages = _create_session_messages("session1", 15, "2023-01-01")
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg) + "\n")

        # Use page_size=10 to trigger pagination (15 messages > 10)
        # This will result in a single page since session can't be split
        convert_jsonl_to_html(temp_project_dir, page_size=10, silent=True)

        content = (temp_project_dir / "combined_transcripts.html").read_text(
            encoding="utf-8"
        )
        assert "last-page" in content
        assert "PAGINATION_NEXT_LINK_START" in content

    def test_multi_page_first_has_visible_next_link(self, temp_project_dir):
        """First page of multi-page should have visible next link (no last-page class)."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create sessions that will span 2 pages
        for i, session_id in enumerate(["s1", "s2"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 20, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        convert_jsonl_to_html(temp_project_dir, page_size=15, silent=True)

        # Page 1 should have visible next link (not last page)
        page1 = (temp_project_dir / "combined_transcripts.html").read_text(
            encoding="utf-8"
        )
        assert "PAGINATION_NEXT_LINK_START" in page1
        # Should NOT have last-page class on its next link
        # The pattern should be: class="page-nav-link next" without last-page
        assert 'class="page-nav-link next"' in page1 or 'next "' not in page1

    def test_multi_page_last_has_hidden_next_link(self, temp_project_dir):
        """Last page of multi-page should have hidden next link (with last-page class)."""
        from claude_code_log.converter import convert_jsonl_to_html

        # Create sessions that will span 2 pages
        for i, session_id in enumerate(["s1", "s2"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 20, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        convert_jsonl_to_html(temp_project_dir, page_size=15, silent=True)

        # Page 2 should have hidden next link (is last page)
        page2 = (temp_project_dir / "combined_transcripts_2.html").read_text(
            encoding="utf-8"
        )
        assert "PAGINATION_NEXT_LINK_START" in page2
        assert "last-page" in page2


class TestPaginationFallbackWithoutCache:
    """Tests for pagination when cache data is unavailable."""

    def test_pagination_renders_messages_when_cache_unavailable(self, temp_project_dir):
        """Pagination should render messages even when get_cached_project_data returns None.

        This tests the fallback path where cached_data is None but pagination is triggered
        because total_message_count exceeds page_size.
        """
        from unittest.mock import patch
        from claude_code_log.converter import convert_jsonl_to_html
        from claude_code_log.cache import CacheManager

        # Create sessions with messages
        for i, session_id in enumerate(["s1", "s2"]):
            jsonl_file = temp_project_dir / f"{session_id}.jsonl"
            messages = _create_session_messages(session_id, 20, f"2023-01-0{i + 1}")
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for msg in messages:
                    f.write(json.dumps(msg) + "\n")

        # First pass: Build cache but then simulate cache unavailable
        convert_jsonl_to_html(temp_project_dir, page_size=5000, silent=True)

        # Delete combined file to force regeneration
        combined_path = temp_project_dir / "combined_transcripts.html"
        if combined_path.exists():
            combined_path.unlink()

        # Patch get_cached_project_data to return None (simulating cache unavailable)
        # but keep total_message_count high enough to trigger pagination
        def mock_get_cached_project_data(self):
            return None

        with patch.object(
            CacheManager, "get_cached_project_data", mock_get_cached_project_data
        ):
            # Force pagination with small page_size
            convert_jsonl_to_html(temp_project_dir, page_size=15, silent=True)

        # Verify the generated HTML contains actual messages, not empty content
        page1_content = combined_path.read_text(encoding="utf-8")

        # The page should contain message content from the sessions
        assert "Message 0 from user" in page1_content or "Response" in page1_content, (
            "Paginated HTML should contain messages when cache is unavailable"
        )


class TestBuildSessionDataRequestIdDedup:
    """Pin the requestId-dedup behavior of ``_build_session_data_from_messages``
    — the cache-miss fallback exercised when ``get_cached_project_data``
    returns ``None`` during pagination.

    The function must:

    1. Dedup repeat assistant entries that carry the SAME requestId
       (e.g. a retried request preserved in the JSONL stream) so their
       usage is counted exactly once.
    2. KEEP counting assistant entries that have NO requestId at all.

    The literal mirror of the canonical cache path's guard
    (``request_id and request_id not in seen``) would silently drop
    rule 2 — these tests guard against that regression.
    """

    def _make_assistant_entry(
        self,
        session_id: str,
        seq: int,
        request_id,
        input_tokens: int,
        output_tokens: int,
    ):
        """Helper: one assistant JSONL entry with a chosen requestId
        (``None`` means the key is omitted entirely, mirroring real
        transcripts that pre-date the field).
        """
        entry = {
            "type": "assistant",
            "uuid": f"{session_id}-assistant-{seq}",
            "timestamp": f"2023-01-01T10:00:{seq:02d}Z",
            "sessionId": session_id,
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "assistant",
            "cwd": "/test",
            "message": {
                "id": f"msg-{session_id}-{seq}",
                "type": "message",
                "role": "assistant",
                "model": "claude-3",
                "content": [{"type": "text", "text": f"Response {seq}"}],
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        }
        if request_id is not None:
            entry["requestId"] = request_id
        return entry

    def _make_user_entry(self, session_id: str, seq: int):
        return {
            "type": "user",
            "uuid": f"{session_id}-user-{seq}",
            "timestamp": f"2023-01-01T09:00:{seq:02d}Z",
            "sessionId": session_id,
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "user",
            "cwd": "/test",
            "message": {"role": "user", "content": f"Hi {seq}"},
        }

    def _build(self, raw_entries):
        """Write a single JSONL, parse via ``load_transcript`` (real
        path) and feed the result to ``_build_session_data_from_messages``.
        """
        from claude_code_log.converter import (
            load_transcript,
            _build_session_data_from_messages,
        )

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "session.jsonl"
            with open(jsonl, "w", encoding="utf-8") as f:
                for entry in raw_entries:
                    f.write(json.dumps(entry) + "\n")
            messages = load_transcript(jsonl)
        return _build_session_data_from_messages(messages)

    def test_repeated_request_id_is_counted_once(self):
        """Two assistant entries sharing a requestId count as one
        usage credit — guards against a retried/duplicated request
        inflating session totals on the cache-miss path."""
        sid = "session-dedup"
        entries = [
            self._make_user_entry(sid, 0),
            self._make_assistant_entry(sid, 1, "req-dup", 100, 50),
            self._make_assistant_entry(sid, 2, "req-dup", 100, 50),
        ]
        data = self._build(entries)

        assert sid in data
        assert data[sid].total_input_tokens == 100, (
            "duplicate requestId must not double-count input_tokens "
            f"(got {data[sid].total_input_tokens})"
        )
        assert data[sid].total_output_tokens == 50, (
            "duplicate requestId must not double-count output_tokens "
            f"(got {data[sid].total_output_tokens})"
        )

    def test_assistant_entry_without_request_id_is_still_counted(self):
        """Assistant entries with NO requestId must still contribute
        their usage. This is the whole point of opp 1: do not adopt
        the canonical cache path's truthy guard, which would silently
        drop these entries.
        """
        sid = "session-unkeyed"
        entries = [
            self._make_user_entry(sid, 0),
            self._make_assistant_entry(sid, 1, None, 30, 10),
        ]
        data = self._build(entries)

        assert sid in data
        assert data[sid].total_input_tokens == 30, (
            "un-keyed assistant usage must be counted "
            f"(got {data[sid].total_input_tokens}; a literal mirror "
            "of the canonical guard would drop it)"
        )
        assert data[sid].total_output_tokens == 10

    def test_distinct_request_ids_both_counted(self):
        """Two assistant entries with DIFFERENT requestIds each
        contribute their usage — the dedup set must not over-fire."""
        sid = "session-distinct"
        entries = [
            self._make_user_entry(sid, 0),
            self._make_assistant_entry(sid, 1, "req-A", 10, 5),
            self._make_assistant_entry(sid, 2, "req-B", 20, 7),
        ]
        data = self._build(entries)

        assert sid in data
        assert data[sid].total_input_tokens == 30
        assert data[sid].total_output_tokens == 12

    def test_mixed_keyed_and_unkeyed(self):
        """End-to-end mix: a fresh requestId (counted), a repeat of
        that id (dedup'd), and an un-keyed entry (counted). Pins
        rules 1 + 2 together on one session."""
        sid = "session-mixed"
        entries = [
            self._make_user_entry(sid, 0),
            self._make_assistant_entry(sid, 1, "req-X", 7, 3),
            self._make_assistant_entry(sid, 2, "req-X", 7, 3),
            self._make_assistant_entry(sid, 3, None, 4, 2),
        ]
        data = self._build(entries)

        assert sid in data
        # 7 (req-X first) + 0 (req-X repeat dedup'd) + 4 (un-keyed) = 11
        assert data[sid].total_input_tokens == 11
        # 3 + 0 + 2 = 5
        assert data[sid].total_output_tokens == 5

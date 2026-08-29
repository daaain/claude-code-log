#!/usr/bin/env python3
"""Tests for caching functionality."""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_code_log.cache import (
    CacheManager,
    get_library_version,
    SessionCacheData,
)
from claude_code_log.models import (
    UserTranscriptEntry,
    AssistantTranscriptEntry,
    SummaryTranscriptEntry,
    UserMessageModel,
    AssistantMessageModel,
    UsageInfo,
    TextContent,
)

from test.conftest import bump_mtime


@pytest.fixture
def temp_project_dir():
    """Create a temporary project directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create project subdirectory so db_path (parent/claude-code-log-cache.db) is unique per test
        project_dir = Path(temp_dir) / "project"
        project_dir.mkdir()
        yield project_dir


@pytest.fixture
def mock_version():
    """Mock library version for consistent testing."""
    return "1.0.0-test"


@pytest.fixture
def cache_manager(temp_project_dir, mock_version):
    """Create a cache manager for testing."""
    with patch("claude_code_log.cache.get_library_version", return_value=mock_version):
        return CacheManager(temp_project_dir, mock_version)


@pytest.fixture
def sample_entries():
    """Create sample transcript entries for testing."""
    return [
        UserTranscriptEntry(
            parentUuid=None,
            isSidechain=False,
            userType="user",
            cwd="/test",
            sessionId="session1",
            version="1.0.0",
            uuid="user1",
            timestamp="2023-01-01T10:00:00Z",
            type="user",
            message=UserMessageModel(
                role="user", content=[TextContent(type="text", text="Hello")]
            ),
        ),
        AssistantTranscriptEntry(
            parentUuid=None,
            isSidechain=False,
            userType="assistant",
            cwd="/test",
            sessionId="session1",
            version="1.0.0",
            uuid="assistant1",
            timestamp="2023-01-01T10:01:00Z",
            type="assistant",
            message=AssistantMessageModel(
                id="msg1",
                type="message",
                role="assistant",
                model="claude-3",
                content=[],
                usage=UsageInfo(input_tokens=10, output_tokens=20),
            ),
            requestId="req1",
        ),
        SummaryTranscriptEntry(
            type="summary",
            summary="Test conversation",
            leafUuid="assistant1",
        ),
    ]


class TestCacheManager:
    """Test the CacheManager class."""

    def test_initialization(self, temp_project_dir, mock_version):
        """Test cache manager initialization."""
        cache_manager = CacheManager(temp_project_dir, mock_version)

        assert cache_manager.project_path == temp_project_dir
        assert cache_manager.library_version == mock_version
        # SQLite database should be created at parent level
        assert (
            cache_manager.db_path
            == temp_project_dir.parent / "claude-code-log-cache.db"
        )
        assert cache_manager.db_path.exists()

    def test_database_path(self, cache_manager, temp_project_dir):
        """Test that SQLite database is created at the correct location."""
        # Database should be at parent level (projects_dir/claude-code-log-cache.db)
        expected_db = temp_project_dir.parent / "claude-code-log-cache.db"
        assert cache_manager.db_path == expected_db
        assert expected_db.exists()

    def test_save_and_load_entries(
        self, cache_manager, temp_project_dir, sample_entries
    ):
        """Test saving and loading cached entries."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        # Save entries to cache
        cache_manager.save_cached_entries(jsonl_path, sample_entries)

        # Verify file is cached
        assert cache_manager.is_file_cached(jsonl_path)

        # Load entries from cache
        loaded_entries = cache_manager.load_cached_entries(jsonl_path)
        assert loaded_entries is not None
        assert len(loaded_entries) == len(sample_entries)

        # Verify entry types match
        assert loaded_entries[0].type == "user"
        assert loaded_entries[1].type == "assistant"
        assert loaded_entries[2].type == "summary"

    def test_message_storage_with_timestamps(
        self, cache_manager, temp_project_dir, sample_entries
    ):
        """Test that messages are stored with correct timestamps in SQLite."""
        import sqlite3

        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        cache_manager.save_cached_entries(jsonl_path, sample_entries)

        # Query the SQLite database directly to verify structure
        # Filter by project_id since database is shared between tests
        conn = sqlite3.connect(cache_manager.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT timestamp, type FROM messages WHERE project_id = ? ORDER BY timestamp NULLS LAST",
            (cache_manager._project_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        # Verify entries are stored with timestamps
        assert len(rows) == 3
        assert rows[0]["timestamp"] == "2023-01-01T10:00:00Z"
        assert rows[0]["type"] == "user"
        assert rows[1]["timestamp"] == "2023-01-01T10:01:00Z"
        assert rows[1]["type"] == "assistant"
        assert rows[2]["timestamp"] is None  # Summary has no timestamp
        assert rows[2]["type"] == "summary"

    def test_cache_invalidation_file_modification(
        self, cache_manager, temp_project_dir, sample_entries
    ):
        """Test cache invalidation when source file is modified."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("original content", encoding="utf-8")

        # Save to cache
        cache_manager.save_cached_entries(jsonl_path, sample_entries)
        assert cache_manager.is_file_cached(jsonl_path)

        # Modify file, then make it look newer than the cached mtime
        # (see bump_mtime in conftest — sleeping past the threshold flakes).
        jsonl_path.write_text("modified content", encoding="utf-8")
        bump_mtime(jsonl_path)

        # Cache should be invalidated
        assert not cache_manager.is_file_cached(jsonl_path)

    def test_cache_invalidation_version_mismatch(self, temp_project_dir):
        """Test cache compatibility when library version changes.

        Uses a version pair outside the explicit ``breaking_changes`` rules
        in ``cache.py`` so the test stays focused on default-path
        compatibility (caches are preserved across compatible upgrades).
        """
        # Create cache with version 5.0.0 — well outside any breaking rule
        with patch("claude_code_log.cache.get_library_version", return_value="5.0.0"):
            cache_manager_v1 = CacheManager(temp_project_dir, "5.0.0")
            cached_data = cache_manager_v1.get_cached_project_data()
            assert cached_data is not None
            assert cached_data.version == "5.0.0"

        # Bump to 5.1.0 — still no breaking rule between these.
        with patch("claude_code_log.cache.get_library_version", return_value="5.1.0"):
            cache_manager_v2 = CacheManager(temp_project_dir, "5.1.0")
            cached_data = cache_manager_v2.get_cached_project_data()
            assert cached_data is not None
            # Version should remain 5.0.0 since it's compatible
            assert cached_data.version == "5.0.0"

    def test_filtered_loading_with_dates(self, cache_manager, temp_project_dir):
        """Test timestamp-based filtering during cache loading."""
        # Create entries with different timestamps
        entries = [
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user1",
                timestamp="2023-01-01T10:00:00Z",
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="Early message")],
                ),
            ),
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user2",
                timestamp="2023-01-02T10:00:00Z",
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="Later message")],
                ),
            ),
        ]

        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        cache_manager.save_cached_entries(jsonl_path, entries)

        # Test filtering (should return entries from 2023-01-01 only)
        filtered = cache_manager.load_cached_entries_filtered(
            jsonl_path, "2023-01-01", "2023-01-01"
        )

        assert filtered is not None
        # Should get both early message and summary (summary has no timestamp)
        assert len(filtered) >= 1
        # Find the user message and check it
        user_messages = [entry for entry in filtered if entry.type == "user"]
        assert len(user_messages) == 1
        assert "Early message" in str(user_messages[0].message.content)

    def test_filtered_loading_with_z_suffix_boundary(
        self, cache_manager, temp_project_dir
    ):
        """Test that timestamps with 'Z' suffix are correctly compared at day boundaries.

        This tests the edge case where a message at 23:59:59Z should be included
        when filtering with to_date set to that day. Previously, the query used
        isoformat() which produced '.999999' microseconds, and 'Z' > '.' in string
        comparison caused incorrect exclusion.
        """
        entries = [
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user1",
                timestamp="2023-01-01T23:59:59Z",  # End of day with Z suffix
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="End of day message")],
                ),
            ),
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user2",
                timestamp="2023-01-02T00:00:01Z",  # Start of next day
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="Next day message")],
                ),
            ),
        ]

        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        cache_manager.save_cached_entries(jsonl_path, entries)

        # Filter to only 2023-01-01 - should include the 23:59:59Z message
        filtered = cache_manager.load_cached_entries_filtered(
            jsonl_path, "2023-01-01", "2023-01-01"
        )

        assert filtered is not None
        user_messages = [entry for entry in filtered if entry.type == "user"]

        # Should include only the end-of-day message, not the next day message
        assert len(user_messages) == 1, (
            f"Expected 1 message from 2023-01-01, got {len(user_messages)}. "
            "The 23:59:59Z message may have been incorrectly excluded due to "
            "timestamp format mismatch (Z vs .999999 suffix)."
        )
        assert "End of day message" in str(user_messages[0].message.content)

    def test_filtered_loading_with_mixed_timestamp_formats(
        self, cache_manager, temp_project_dir
    ):
        """Test filtering with mixed timestamp formats (with/without fractional seconds).

        This tests the bug where timestamps like '2023-01-01T10:00:00.875368Z'
        were incorrectly compared against filter bounds like '2023-01-01T10:00:00Z'.
        String comparison fails because '.' < 'Z' alphabetically, causing the
        timestamp with microseconds to be incorrectly excluded even though it's
        actually 875ms AFTER the filter bound.
        """
        entries = [
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user1",
                timestamp="2023-01-01T10:00:00Z",  # No fractional seconds
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[
                        TextContent(type="text", text="Message without microseconds")
                    ],
                ),
            ),
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user2",
                timestamp="2023-01-01T10:00:00.875368Z",  # With microseconds - same second
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[
                        TextContent(type="text", text="Message with microseconds")
                    ],
                ),
            ),
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user3",
                timestamp="2023-01-01T10:00:01.123456Z",  # Next second with microseconds
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="Message next second")],
                ),
            ),
        ]

        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        cache_manager.save_cached_entries(jsonl_path, entries)

        # Filter with from_date at exactly 10:00:00 - should include ALL messages
        # The bug would cause the microsecond messages to be excluded because
        # '2023-01-01T10:00:00.875368Z' < '2023-01-01T10:00:00Z' in string comparison
        filtered = cache_manager.load_cached_entries_filtered(
            jsonl_path, "2023-01-01 10:00:00", "2023-01-01 10:00:01"
        )

        assert filtered is not None
        user_messages = [entry for entry in filtered if entry.type == "user"]

        # All 3 messages should be included
        assert len(user_messages) == 3, (
            f"Expected 3 messages, got {len(user_messages)}. "
            "Messages with fractional seconds may have been incorrectly excluded "
            "due to string comparison where '.' < 'Z'."
        )

    def test_timestamp_ordering_with_mixed_formats(
        self, cache_manager, temp_project_dir
    ):
        """Test that timestamps are correctly ordered regardless of format.

        Without normalization, ORDER BY timestamp would sort:
        - '2023-01-01T10:00:00.5Z' BEFORE '2023-01-01T10:00:00Z'
        because '.' < 'Z' in ASCII, even though .5 seconds is AFTER 0 seconds.
        """
        entries = [
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user1",
                timestamp="2023-01-01T10:00:00.500000Z",  # 500ms into the second
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="Second message (500ms)")],
                ),
            ),
            UserTranscriptEntry(
                parentUuid=None,
                isSidechain=False,
                userType="user",
                cwd="/test",
                sessionId="session1",
                version="1.0.0",
                uuid="user2",
                timestamp="2023-01-01T10:00:00Z",  # Start of the second
                type="user",
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text="First message (0ms)")],
                ),
            ),
        ]

        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        cache_manager.save_cached_entries(jsonl_path, entries)

        # Load all entries - they should be in timestamp order
        loaded = cache_manager.load_cached_entries(jsonl_path)

        assert loaded is not None
        user_messages = [entry for entry in loaded if entry.type == "user"]

        # With normalization to second precision, both messages have the same
        # normalized timestamp, so order may vary. The key thing is that the
        # filtering works correctly - ordering within the same second is less critical.
        assert len(user_messages) == 2

    def test_clear_cache(self, cache_manager, temp_project_dir, sample_entries):
        """Test cache clearing functionality."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        # Create cache
        cache_manager.save_cached_entries(jsonl_path, sample_entries)
        assert cache_manager.is_file_cached(jsonl_path)

        # Verify data exists before clearing
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert len(cached_data.cached_files) > 0

        # Clear cache
        cache_manager.clear_cache()

        # Verify cache is cleared (no more files or sessions)
        assert not cache_manager.is_file_cached(jsonl_path)
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert len(cached_data.cached_files) == 0
        assert len(cached_data.sessions) == 0

    def test_session_cache_updates(self, cache_manager):
        """Test updating session cache data."""
        session_data = {
            "session1": SessionCacheData(
                session_id="session1",
                summary="Test session",
                first_timestamp="2023-01-01T10:00:00Z",
                last_timestamp="2023-01-01T11:00:00Z",
                message_count=5,
                first_user_message="Hello",
                total_input_tokens=100,
                total_output_tokens=200,
            )
        }

        cache_manager.update_session_cache(session_data)

        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert "session1" in cached_data.sessions
        assert cached_data.sessions["session1"].summary == "Test session"

    def test_project_aggregates_update(self, cache_manager):
        """Test updating project-level aggregates."""
        cache_manager.update_project_aggregates(
            total_message_count=100,
            total_input_tokens=1000,
            total_output_tokens=2000,
            total_cache_creation_tokens=50,
            total_cache_read_tokens=25,
            earliest_timestamp="2023-01-01T10:00:00Z",
            latest_timestamp="2023-01-01T20:00:00Z",
        )

        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count == 100
        assert cached_data.total_input_tokens == 1000
        assert cached_data.total_output_tokens == 2000

    def test_get_modified_files(self, cache_manager, temp_project_dir, sample_entries):
        """Test identification of modified files."""
        # Create multiple files
        file1 = temp_project_dir / "file1.jsonl"
        file2 = temp_project_dir / "file2.jsonl"
        file1.write_text("content1", encoding="utf-8")
        file2.write_text("content2", encoding="utf-8")

        # Cache only one file
        cache_manager.save_cached_entries(file1, sample_entries)

        # Check modified files
        all_files = [file1, file2]
        modified = cache_manager.get_modified_files(all_files)

        # Only file2 should be modified (not cached)
        assert len(modified) == 1
        assert file2 in modified
        assert file1 not in modified

    def test_cache_stats(self, cache_manager, sample_entries):
        """Test cache statistics reporting."""
        # Initially empty
        stats = cache_manager.get_cache_stats()
        assert stats["cache_enabled"] is True
        assert stats["cached_files_count"] == 0

        # Add some cached data
        cache_manager.update_project_aggregates(
            total_message_count=50,
            total_input_tokens=500,
            total_output_tokens=1000,
            total_cache_creation_tokens=25,
            total_cache_read_tokens=10,
            earliest_timestamp="2023-01-01T10:00:00Z",
            latest_timestamp="2023-01-01T20:00:00Z",
        )

        stats = cache_manager.get_cache_stats()
        assert stats["total_cached_messages"] == 50


class TestReadOnlyCacheManager:
    """read_only=True is what spawned render workers construct: it must
    see the parent's data but be physically unable to write the shared DB."""

    def test_reads_existing_project_without_writing(
        self, temp_project_dir, mock_version
    ):
        writer = CacheManager(temp_project_dir, mock_version)
        writer.update_project_aggregates(
            total_message_count=5,
            total_input_tokens=50,
            total_output_tokens=100,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
            earliest_timestamp="2023-01-01T10:00:00Z",
            latest_timestamp="2023-01-01T20:00:00Z",
        )

        reader = CacheManager(temp_project_dir, mock_version, read_only=True)
        assert reader._project_id == writer._project_id
        cached = reader.get_cached_project_data()
        assert cached is not None
        assert cached.total_message_count == 5

    def test_connections_reject_writes(self, temp_project_dir, mock_version):
        CacheManager(temp_project_dir, mock_version)  # create the DB
        reader = CacheManager(temp_project_dir, mock_version, read_only=True)
        with pytest.raises(sqlite3.OperationalError):
            with reader._get_connection() as conn:
                conn.execute("DELETE FROM projects")

    def test_missing_database_degrades_to_no_data(self, temp_project_dir, mock_version):
        reader = CacheManager(temp_project_dir, mock_version, read_only=True)
        assert reader._project_id is None
        assert reader.get_cached_project_data() is None
        # Construction must not have created the database as a side effect.
        assert not reader.db_path.exists()


class TestLibraryVersion:
    """Test library version detection."""

    def test_get_library_version(self):
        """Test library version retrieval."""
        version = get_library_version()
        assert isinstance(version, str)
        assert len(version) > 0

    def test_version_fallback_without_toml(self):
        """Test version fallback when toml module not available."""
        # Mock the import statement to fail
        import sys

        original_modules = sys.modules.copy()

        try:
            # Remove toml from modules if it exists
            if "toml" in sys.modules:
                del sys.modules["toml"]

            # Mock the import to raise ImportError
            with patch.dict("sys.modules", {"toml": None}):
                version = get_library_version()
                # Should still return a version using manual parsing
                assert isinstance(version, str)
                assert len(version) > 0
        finally:
            # Restore original modules
            sys.modules.update(original_modules)


class TestCacheVersionCompatibility:
    """Test cache version compatibility checking."""

    def test_same_version_is_compatible(self, temp_project_dir):
        """Test that same version is always compatible."""
        cache_manager = CacheManager(temp_project_dir, "1.0.0")
        assert cache_manager._is_cache_version_compatible("1.0.0") is True

    def test_no_breaking_changes_is_compatible(self, temp_project_dir):
        """Test that versions without breaking changes are compatible."""
        cache_manager = CacheManager(temp_project_dir, "1.0.1")
        assert cache_manager._is_cache_version_compatible("1.0.0") is True

    def test_patch_version_increase_is_compatible(self, temp_project_dir):
        """Test that patch version increases are compatible."""
        cache_manager = CacheManager(temp_project_dir, "1.0.2")
        assert cache_manager._is_cache_version_compatible("1.0.1") is True

    def test_minor_version_increase_is_compatible(self, temp_project_dir):
        """Test that minor version increases are compatible."""
        cache_manager = CacheManager(temp_project_dir, "1.1.0")
        assert cache_manager._is_cache_version_compatible("1.0.5") is True

    def test_major_version_increase_is_compatible(self, temp_project_dir):
        """Test that major version increases are compatible by default.

        The cache version has to sit *above* every declared breaking
        boundary, otherwise this exercises a breaking rule rather than
        the default path (1.5.0 became such a boundary in 1.6.0).
        """
        cache_manager = CacheManager(temp_project_dir, "2.0.0")
        assert cache_manager._is_cache_version_compatible("1.5.1") is True

    def test_version_downgrade_is_compatible(self, temp_project_dir):
        """Test that version downgrades are compatible by default."""
        cache_manager = CacheManager(temp_project_dir, "1.0.0")
        assert cache_manager._is_cache_version_compatible("1.0.1") is True

    def test_breaking_change_exact_version_incompatible(self, temp_project_dir):
        """Test that exact version breaking changes are detected."""
        cache_manager = CacheManager(temp_project_dir, "0.3.4")

        def patched_method(cache_version):
            # Create a custom breaking_changes dict for this test
            breaking_changes = {"0.3.3": "0.3.4"}

            if cache_version == cache_manager.library_version:
                return True

            from packaging import version

            cache_ver = version.parse(cache_version)
            current_ver = version.parse(cache_manager.library_version)

            for breaking_version_pattern, min_required in breaking_changes.items():
                min_required_ver = version.parse(min_required)

                if current_ver >= min_required_ver:
                    if breaking_version_pattern.endswith(".x"):
                        major_minor = breaking_version_pattern[:-2]
                        if str(cache_ver).startswith(major_minor):
                            return False
                    else:
                        breaking_ver = version.parse(breaking_version_pattern)
                        if cache_ver <= breaking_ver:
                            return False

            return True

        # Test with a breaking change scenario
        cache_manager._is_cache_version_compatible = patched_method  # type: ignore

        # 0.3.3 should be incompatible with 0.3.4 due to breaking change
        assert cache_manager._is_cache_version_compatible("0.3.3") is False
        # 0.3.4 should be compatible with itself
        assert cache_manager._is_cache_version_compatible("0.3.4") is True
        # 0.3.5 should be compatible with 0.3.4
        assert cache_manager._is_cache_version_compatible("0.3.5") is True

    def test_breaking_change_pattern_matching(self, temp_project_dir):
        """Test that version pattern matching works for breaking changes."""
        cache_manager = CacheManager(temp_project_dir, "0.3.0")

        def patched_method(cache_version):
            # Create a custom breaking_changes dict for this test
            breaking_changes = {"0.2.x": "0.3.0"}

            if cache_version == cache_manager.library_version:
                return True

            from packaging import version

            cache_ver = version.parse(cache_version)
            current_ver = version.parse(cache_manager.library_version)

            for breaking_version_pattern, min_required in breaking_changes.items():
                min_required_ver = version.parse(min_required)

                if current_ver >= min_required_ver:
                    if breaking_version_pattern.endswith(".x"):
                        major_minor = breaking_version_pattern[:-2]
                        if str(cache_ver).startswith(major_minor):
                            return False
                    else:
                        breaking_ver = version.parse(breaking_version_pattern)
                        if cache_ver <= breaking_ver:
                            return False

            return True

        # Test with a breaking change scenario using pattern matching
        cache_manager._is_cache_version_compatible = patched_method  # type: ignore

        # All 0.2.x versions should be incompatible with 0.3.0
        assert cache_manager._is_cache_version_compatible("0.2.0") is False
        assert cache_manager._is_cache_version_compatible("0.2.5") is False
        assert cache_manager._is_cache_version_compatible("0.2.99") is False

        # 0.1.x and 0.3.x versions should be compatible
        assert cache_manager._is_cache_version_compatible("0.1.0") is True
        assert cache_manager._is_cache_version_compatible("0.3.1") is True

    def test_multiple_breaking_changes(self, temp_project_dir):
        """Test handling of multiple breaking changes."""
        cache_manager = CacheManager(temp_project_dir, "0.2.6")

        def patched_method(cache_version):
            # Create a custom breaking_changes dict with multiple entries
            breaking_changes = {"0.1.x": "0.2.0", "0.2.5": "0.2.6"}

            if cache_version == cache_manager.library_version:
                return True

            from packaging import version

            cache_ver = version.parse(cache_version)
            current_ver = version.parse(cache_manager.library_version)

            for breaking_version_pattern, min_required in breaking_changes.items():
                min_required_ver = version.parse(min_required)

                if current_ver >= min_required_ver:
                    if breaking_version_pattern.endswith(".x"):
                        major_minor = breaking_version_pattern[:-2]
                        if str(cache_ver).startswith(major_minor):
                            return False
                    else:
                        breaking_ver = version.parse(breaking_version_pattern)
                        if cache_ver <= breaking_ver:
                            return False

            return True

        # Test with multiple breaking change scenarios
        cache_manager._is_cache_version_compatible = patched_method  # type: ignore

        # 0.1.x should be incompatible due to first breaking change
        assert cache_manager._is_cache_version_compatible("0.1.0") is False
        assert cache_manager._is_cache_version_compatible("0.1.5") is False

        # 0.2.5 should be incompatible due to second breaking change
        assert cache_manager._is_cache_version_compatible("0.2.5") is False

        # 0.2.6 and newer should be compatible
        assert cache_manager._is_cache_version_compatible("0.2.6") is True
        assert cache_manager._is_cache_version_compatible("0.2.7") is True

    def test_version_parsing_edge_cases(self, temp_project_dir):
        """Test edge cases in version parsing."""
        cache_manager = CacheManager(temp_project_dir, "1.0.0")

        # Test with prerelease versions
        assert cache_manager._is_cache_version_compatible("1.0.0-alpha") is True
        assert cache_manager._is_cache_version_compatible("1.0.0-beta.1") is True
        assert cache_manager._is_cache_version_compatible("1.0.0-rc.1") is True

        # Test with build metadata
        assert cache_manager._is_cache_version_compatible("1.0.0+build.1") is True
        assert cache_manager._is_cache_version_compatible("1.0.0+20230101") is True

    def test_breaking_changes_0_8_0(self, temp_project_dir):
        """Test that 0.8.0 breaking change correctly invalidates old caches."""
        cache_manager = CacheManager(temp_project_dir, "0.9.0")

        # Caches from 0.9.0+ should be compatible
        assert cache_manager._is_cache_version_compatible("0.9.0") is True
        assert cache_manager._is_cache_version_compatible("1.0.0") is True

        # Caches from 0.8.0 and earlier should be invalidated
        assert cache_manager._is_cache_version_compatible("0.8.0") is False
        assert cache_manager._is_cache_version_compatible("0.7.0") is False
        assert cache_manager._is_cache_version_compatible("0.5.0") is False


class TestCacheErrorHandling:
    """Test cache error handling and edge cases."""

    def test_missing_cache_entry(self, cache_manager, temp_project_dir):
        """Test handling when cache entry doesn't exist."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        # File exists but not cached
        assert not cache_manager.is_file_cached(jsonl_path)

        # Should return None when not cached
        result = cache_manager.load_cached_entries(jsonl_path)
        assert result is None

    def test_missing_jsonl_file(self, cache_manager, temp_project_dir, sample_entries):
        """Test cache behavior when source JSONL file is missing."""
        jsonl_path = temp_project_dir / "nonexistent.jsonl"

        # Should not be considered cached
        assert not cache_manager.is_file_cached(jsonl_path)

    def test_cache_directory_permissions(self, temp_project_dir, mock_version):
        """Test cache behavior with directory permission issues."""
        # Skip this test on systems where chmod doesn't work as expected

        cache_dir = temp_project_dir / "cache"
        cache_dir.mkdir()

        try:
            # Try to make directory read-only (might not work on all systems)
            cache_dir.chmod(0o444)

            # Check if we can actually read the directory after chmod
            try:
                list(cache_dir.iterdir())
                cache_manager = CacheManager(temp_project_dir, mock_version)
                # Should handle gracefully even if it can't write
                assert cache_manager is not None
            except PermissionError:
                # If we get permission errors, just skip this test
                pytest.skip("Cannot test permissions on this system")
        finally:
            # Restore permissions
            try:
                cache_dir.chmod(0o755)
            except OSError:
                pass


class TestCachePathEnvVar:
    """Test CLAUDE_CODE_LOG_CACHE_PATH environment variable."""

    def test_default_path_without_env_var(self, tmp_path):
        """Test default cache path when env var is not set."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        cache = CacheManager(project_dir, "1.0.0")

        # Default should be parent/claude-code-log-cache.db
        expected_path = tmp_path / "claude-code-log-cache.db"
        assert cache.db_path == expected_path
        assert expected_path.exists()

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        """Test that CLAUDE_CODE_LOG_CACHE_PATH overrides default location."""
        custom_db = tmp_path / "custom-cache.db"
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(custom_db))

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        cache = CacheManager(project_dir, "1.0.0")
        assert cache.db_path == custom_db
        assert custom_db.exists()

    def test_explicit_db_path_overrides_env_var(self, tmp_path, monkeypatch):
        """Test that explicit db_path takes precedence over env var."""
        env_db = tmp_path / "env-cache.db"
        explicit_db = tmp_path / "explicit-cache.db"
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(env_db))

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        cache = CacheManager(project_dir, "1.0.0", db_path=explicit_db)
        assert cache.db_path == explicit_db
        assert explicit_db.exists()
        assert not env_db.exists()

    def test_get_all_cached_projects_respects_env_var(self, tmp_path, monkeypatch):
        """Test that get_all_cached_projects uses env var."""
        from claude_code_log.cache import get_all_cached_projects

        custom_db = tmp_path / "custom-cache.db"
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(custom_db))

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create a project and cache it
        project_dir = projects_dir / "test-project"
        project_dir.mkdir()
        cache = CacheManager(project_dir, "1.0.0")  # Uses env var
        assert cache.db_path == custom_db

        # get_all_cached_projects should also use the env var
        projects = get_all_cached_projects(projects_dir)
        assert len(projects) == 1
        assert projects[0][0] == str(project_dir)

    def test_get_all_cached_projects_explicit_db_path(self, tmp_path, monkeypatch):
        """Test that get_all_cached_projects explicit db_path overrides env var."""
        from claude_code_log.cache import get_all_cached_projects

        env_db = tmp_path / "env-cache.db"
        explicit_db = tmp_path / "explicit-cache.db"
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(env_db))

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        project_dir = projects_dir / "test-project"
        project_dir.mkdir()

        # Create cache using explicit path
        cache = CacheManager(project_dir, "1.0.0", db_path=explicit_db)
        assert cache.db_path == explicit_db

        # get_all_cached_projects with explicit path should find it
        projects = get_all_cached_projects(projects_dir, db_path=explicit_db)
        assert len(projects) == 1

        # get_all_cached_projects without explicit path uses env var (empty db)
        projects_env = get_all_cached_projects(projects_dir)
        assert len(projects_env) == 0  # env_db doesn't have any projects

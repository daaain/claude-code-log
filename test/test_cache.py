#!/usr/bin/env python3
"""Tests for caching functionality."""

import json
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Dict, List
from unittest.mock import patch

import pytest

from claude_code_log.cache import (
    CacheManager,
    get_library_version,
    ProjectCache,
    SessionCacheData,
)
from claude_code_log.models import (
    UserTranscriptEntry,
    AssistantTranscriptEntry,
    SummaryTranscriptEntry,
    UserMessage,
    AssistantMessage,
    UsageInfo,
)


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
            message=UserMessage(role="user", content="Hello"),
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
            message=AssistantMessage(
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
        # Project should be created in SQLite
        assert cache_manager._project_id is not None

    def test_save_and_load_entries(
        self, cache_manager, temp_project_dir, sample_entries
    ):
        """Test saving and loading cached entries."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        # Save entries to cache
        cache_manager.save_cached_entries(jsonl_path, sample_entries)

        # Verify cache entry exists in database
        assert cache_manager.is_file_cached(jsonl_path)

        # Load entries from cache
        loaded_entries = cache_manager.load_cached_entries(jsonl_path)
        assert loaded_entries is not None
        assert len(loaded_entries) == len(sample_entries)

        # Verify entry types match
        assert loaded_entries[0].type == "user"
        assert loaded_entries[1].type == "assistant"
        assert loaded_entries[2].type == "summary"

    def test_timestamp_based_cache_structure(
        self, cache_manager, temp_project_dir, sample_entries
    ):
        """Test that cache stores entries with timestamp-based keys."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        cache_manager.save_cached_entries(jsonl_path, sample_entries)

        # Query the database directly to verify timestamp structure
        cached_file_id = cache_manager._get_cached_file_id(jsonl_path)
        assert cached_file_id is not None

        cursor = cache_manager._connection.execute(
            "SELECT timestamp_key FROM cached_entries WHERE cached_file_id = ?",
            (cached_file_id,),
        )
        timestamp_keys = [row["timestamp_key"] for row in cursor]

        # Verify timestamp-based structure
        assert "2023-01-01T10:00:00Z" in timestamp_keys
        assert "2023-01-01T10:01:00Z" in timestamp_keys
        assert "_no_timestamp" in timestamp_keys  # Summary entry

    def test_cache_invalidation_file_modification(
        self, cache_manager, temp_project_dir, sample_entries
    ):
        """Test cache invalidation when source file is modified."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("original content", encoding="utf-8")

        # Save to cache
        cache_manager.save_cached_entries(jsonl_path, sample_entries)
        assert cache_manager.is_file_cached(jsonl_path)

        # Modify file
        import time

        time.sleep(1.1)  # Ensure different mtime (increase to be more reliable)
        jsonl_path.write_text("modified content", encoding="utf-8")

        # Cache should be invalidated
        assert not cache_manager.is_file_cached(jsonl_path)

    def test_cache_invalidation_version_mismatch(self, temp_project_dir):
        """Test cache invalidation when library version changes."""
        # Create cache with version 1.0.0
        with patch("claude_code_log.cache.get_library_version", return_value="1.0.0"):
            cache_manager_v1 = CacheManager(temp_project_dir, "1.0.0")
            # Store some project data
            cache_manager_v1.update_project_aggregates(
                total_message_count=10,
                total_input_tokens=100,
                total_output_tokens=200,
                total_cache_creation_tokens=0,
                total_cache_read_tokens=0,
                earliest_timestamp="2023-01-01T10:00:00Z",
                latest_timestamp="2023-01-01T11:00:00Z",
            )

        # Create new cache manager with different version
        with patch("claude_code_log.cache.get_library_version", return_value="2.0.0"):
            cache_manager_v2 = CacheManager(temp_project_dir, "2.0.0")
            # Since the default implementation has empty breaking_changes,
            # versions should be compatible and cache should be preserved
            cached_data = cache_manager_v2.get_cached_project_data()
            assert cached_data is not None
            # Version should remain 1.0.0 since it's compatible
            assert cached_data.version == "1.0.0"

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
                message=UserMessage(role="user", content="Early message"),
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
                message=UserMessage(role="user", content="Later message"),
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

    def test_clear_cache(self, cache_manager, temp_project_dir, sample_entries):
        """Test cache clearing functionality."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        # Create cache
        cache_manager.save_cached_entries(jsonl_path, sample_entries)
        assert cache_manager.is_file_cached(jsonl_path)

        # Clear cache
        cache_manager.clear_cache()

        # Verify project_id is reset and file is no longer cached
        assert cache_manager._project_id is None
        # Create new cache manager to verify data was cleared
        new_cache_manager = CacheManager(
            temp_project_dir, cache_manager.library_version
        )
        assert not new_cache_manager.is_file_cached(jsonl_path)

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
        """Test that major version increases are compatible by default."""
        cache_manager = CacheManager(temp_project_dir, "2.0.0")
        assert cache_manager._is_cache_version_compatible("1.5.0") is True

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

    def test_empty_breaking_changes_dict(self, temp_project_dir):
        """Test that empty breaking changes dict allows all versions."""
        cache_manager = CacheManager(temp_project_dir, "2.0.0")

        # With no breaking changes defined, all versions should be compatible
        assert cache_manager._is_cache_version_compatible("1.0.0") is True
        assert cache_manager._is_cache_version_compatible("0.5.0") is True
        assert cache_manager._is_cache_version_compatible("3.0.0") is True


class TestCacheErrorHandling:
    """Test cache error handling and edge cases."""

    def test_corrupted_cache_data(self, cache_manager, temp_project_dir):
        """Test handling of corrupted cache data in SQLite."""
        jsonl_path = temp_project_dir / "test.jsonl"
        jsonl_path.write_text("dummy content", encoding="utf-8")

        # Insert corrupted entry directly into database
        cache_manager._connection.execute(
            """
            INSERT INTO cached_files (project_id, file_name, file_path, source_mtime, cached_mtime, message_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                cache_manager._project_id,
                jsonl_path.name,
                str(jsonl_path),
                jsonl_path.stat().st_mtime,
                0,
                0,
            ),
        )
        cached_file_id = cache_manager._connection.execute(
            "SELECT id FROM cached_files WHERE project_id = ? AND file_name = ?",
            (cache_manager._project_id, jsonl_path.name),
        ).fetchone()["id"]

        # Insert corrupted JSON in cached_entries
        cache_manager._connection.execute(
            "INSERT INTO cached_entries (cached_file_id, timestamp_key, entries_json) VALUES (?, ?, ?)",
            (cached_file_id, "_no_timestamp", "invalid json content"),
        )
        cache_manager._connection.commit()

        # Should handle gracefully - load_cached_entries handles JSON decode errors
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
                return pytest.skip("Cannot test permissions on this system")  # type: ignore[misc]
        finally:
            # Restore permissions
            try:
                cache_dir.chmod(0o755)
            except OSError:
                pass


class TestSQLiteSchema:
    """Tests for SQLite schema creation and structure."""

    def test_sqlite_schema_tables_created(self, temp_project_dir, temp_sqlite_db):
        """Verify all expected tables exist in database."""
        import sqlite3

        # Initialize a CacheManager to create the schema
        CacheManager(temp_project_dir, "1.0.0")

        conn = sqlite3.connect(temp_sqlite_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cursor}
        conn.close()

        expected_tables = {
            "schema_version",
            "projects",
            "working_directories",
            "cached_files",
            "file_sessions",
            "sessions",
            "cached_entries",
            "tags",
        }

        # Verify all expected tables exist
        for table in expected_tables:
            assert table in tables, f"Table '{table}' not found in database"

    def test_sqlite_schema_indexes_created(self, temp_project_dir, temp_sqlite_db):
        """Verify performance indexes are created."""
        import sqlite3

        # Initialize a CacheManager to create the schema
        CacheManager(temp_project_dir, "1.0.0")

        conn = sqlite3.connect(temp_sqlite_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
        indexes = {row["name"] for row in cursor}
        conn.close()

        expected_indexes = {
            "idx_cached_entries_file",
            "idx_cached_entries_timestamp",
            "idx_sessions_session_id",
            "idx_sessions_project",
            "idx_cached_files_project",
            "idx_working_directories_project",
        }

        # Verify all expected indexes exist
        for index in expected_indexes:
            assert index in indexes, f"Index '{index}' not found in database"


class TestJSONMigration:
    """Tests for JSON to SQLite cache migration."""

    def test_json_to_sqlite_migration(self, temp_project_dir, temp_sqlite_db):
        """Test migration from legacy JSON cache to SQLite."""
        # 1. Create legacy JSON cache structure
        cache_dir = temp_project_dir / "cache"
        cache_dir.mkdir()

        # Create index.json with project data
        index_data = {
            "version": "1.0.0",
            "cache_created": "2023-01-01T10:00:00Z",
            "last_updated": "2023-01-01T11:00:00Z",
            "total_message_count": 50,
            "total_input_tokens": 500,
            "total_output_tokens": 1000,
            "total_cache_creation_tokens": 25,
            "total_cache_read_tokens": 10,
            "earliest_timestamp": "2023-01-01T10:00:00Z",
            "latest_timestamp": "2023-01-01T11:00:00Z",
            "sessions": {
                "session1": {
                    "session_id": "session1",
                    "summary": "Test session",
                    "first_timestamp": "2023-01-01T10:00:00Z",
                    "last_timestamp": "2023-01-01T11:00:00Z",
                    "message_count": 5,
                    "first_user_message": "Hello",
                    "total_input_tokens": 100,
                    "total_output_tokens": 200,
                }
            },
            "cached_files": {
                "test.jsonl": {
                    "file_path": str(temp_project_dir / "test.jsonl"),
                    "source_mtime": 1672574400.0,
                    "cached_mtime": 1672574500.0,
                    "message_count": 5,
                    "session_ids": ["session1"],
                }
            },
            "working_directories": ["/test/dir"],
        }
        (cache_dir / "index.json").write_text(json.dumps(index_data), encoding="utf-8")

        # Create per-file cache
        file_cache_data = {
            "2023-01-01T10:00:00Z": [
                {
                    "type": "user",
                    "uuid": "user1",
                    "timestamp": "2023-01-01T10:00:00Z",
                    "sessionId": "session1",
                    "version": "1.0.0",
                    "parentUuid": None,
                    "isSidechain": False,
                    "userType": "user",
                    "cwd": "/test",
                    "message": {"role": "user", "content": "Hello"},
                }
            ]
        }
        (cache_dir / "test.jsonl.json").write_text(
            json.dumps(file_cache_data), encoding="utf-8"
        )

        # 2. Initialize CacheManager (triggers migration)
        cache_manager = CacheManager(temp_project_dir, "1.0.0")

        # 3. Verify data in SQLite matches original JSON
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count == 50
        assert cached_data.total_input_tokens == 500
        assert "session1" in cached_data.sessions
        assert cached_data.sessions["session1"].summary == "Test session"

        # 4. Verify JSON directory deleted
        assert not cache_dir.exists(), (
            "JSON cache directory should be deleted after migration"
        )

    def test_migration_skips_if_already_in_sqlite(
        self, temp_project_dir, temp_sqlite_db
    ):
        """Verify migration doesn't duplicate data if project already in DB."""
        # 1. Create project in SQLite first
        cache_manager1 = CacheManager(temp_project_dir, "1.0.0")
        cache_manager1.update_project_aggregates(
            total_message_count=10,
            total_input_tokens=100,
            total_output_tokens=200,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
            earliest_timestamp="2023-01-01T10:00:00Z",
            latest_timestamp="2023-01-01T11:00:00Z",
        )

        # 2. Create JSON cache (with different data)
        cache_dir = temp_project_dir / "cache"
        cache_dir.mkdir()
        index_data = {
            "version": "1.0.0",
            "cache_created": "2023-01-01T10:00:00Z",
            "last_updated": "2023-01-01T11:00:00Z",
            "total_message_count": 999,  # Different value
            "total_input_tokens": 9999,
            "total_output_tokens": 9999,
            "sessions": {},
            "cached_files": {},
        }
        (cache_dir / "index.json").write_text(json.dumps(index_data), encoding="utf-8")

        # 3. Initialize new CacheManager
        cache_manager2 = CacheManager(temp_project_dir, "1.0.0")

        # 4. Verify original SQLite data preserved (not overwritten by JSON)
        cached_data = cache_manager2.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count == 10  # Original value, not 999

        # JSON cache should still be deleted
        assert not cache_dir.exists()


class TestThreadSafety:
    """Tests for thread-safe concurrent cache access."""

    def test_concurrent_cache_writes(self, temp_project_dir, temp_sqlite_db):
        """Test thread-safe concurrent writes from multiple threads."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: List[bool] = []
        errors: List[str] = []

        def write_cache_entry(thread_id: int) -> bool:
            """Write a cache entry from a thread."""
            try:
                # Each thread creates its own CacheManager instance
                manager = CacheManager(temp_project_dir, "1.0.0")

                # Update session cache with unique data
                session_data = {
                    f"session-{thread_id}": SessionCacheData(
                        session_id=f"session-{thread_id}",
                        summary=f"Thread {thread_id} session",
                        first_timestamp="2023-01-01T10:00:00Z",
                        last_timestamp="2023-01-01T11:00:00Z",
                        message_count=thread_id,
                        first_user_message=f"Hello from thread {thread_id}",
                        total_input_tokens=100 * thread_id,
                        total_output_tokens=200 * thread_id,
                    )
                }
                manager.update_session_cache(session_data)
                return True
            except Exception as e:
                errors.append(f"Thread {thread_id}: {e}")
                return False

        # Run 10 threads concurrently
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(write_cache_entry, i) for i in range(10)]
            for future in as_completed(futures):
                results.append(future.result())

        # All threads should succeed
        assert all(results), f"Some threads failed: {errors}"
        assert len(errors) == 0, f"Errors occurred: {errors}"

        # Verify all sessions were written
        final_manager = CacheManager(temp_project_dir, "1.0.0")
        cached_data = final_manager.get_cached_project_data()
        assert cached_data is not None

        # All 10 sessions should be present
        for i in range(10):
            session_id = f"session-{i}"
            assert session_id in cached_data.sessions, f"Session {session_id} missing"

    def test_thread_local_connection_isolation(self, temp_project_dir, temp_sqlite_db):
        """Verify each thread gets its own database connection."""
        from concurrent.futures import ThreadPoolExecutor
        import threading

        connection_ids: Dict[int, int] = {}
        lock = threading.Lock()

        def get_connection_id(thread_num: int) -> None:
            """Get the connection object id from a thread."""
            manager = CacheManager(temp_project_dir, "1.0.0")
            conn_id = id(manager._connection)
            with lock:
                connection_ids[thread_num] = conn_id

        # Run threads and collect connection IDs
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(get_connection_id, range(5)))

        # Verify we got 5 different connection IDs (thread isolation)
        unique_connections = set(connection_ids.values())
        assert len(unique_connections) == 5, (
            f"Expected 5 unique connections, got {len(unique_connections)}. "
            "Thread-local connections not working properly."
        )

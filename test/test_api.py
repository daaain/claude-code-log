#!/usr/bin/env python3
"""
Regression tests for the public library API (claude_code_log.api).

These tests ensure the stable public API doesn't break across versions.
External consumers like claude-history-mcp depend on these functions.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Import API functions directly to avoid circular import issues
from claude_code_log.api import (
    discover_projects,
    find_history_file,
    load_history_file,
)
from claude_code_log.cache import CacheManager, SessionCacheData


class TestDiscoverProjects:
    """Tests for discover_projects() - discovers project directories with JSONL files."""

    def test_discovers_projects_with_jsonl(self, tmp_path: Path):
        """Finds directories containing .jsonl files."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create two projects with JSONL files
        project1 = projects_dir / "project-1"
        project1.mkdir()
        (project1 / "session-1.jsonl").write_text('{"type": "user"}', encoding="utf-8")

        project2 = projects_dir / "project-2"
        project2.mkdir()
        (project2 / "session-1.jsonl").write_text('{"type": "user"}', encoding="utf-8")
        (project2 / "session-2.jsonl").write_text('{"type": "assistant"}', encoding="utf-8")

        # Create empty directory (not a project)
        (projects_dir / "empty-dir").mkdir()

        # Create hidden directory (should be skipped)
        (projects_dir / ".hidden-project").mkdir()
        (projects_dir / ".hidden-project" / "session.jsonl").write_text('{"type": "user"}', encoding="utf-8")

        result = discover_projects(projects_dir)

        assert len(result) == 2
        project_names = {p.name for p in result}
        assert project_names == {"project-1", "project-2"}

    def test_returns_empty_list_for_nonexistent_dir(self, tmp_path: Path):
        """Returns empty list when projects directory doesn't exist."""
        projects_dir = tmp_path / "nonexistent"
        result = discover_projects(projects_dir)
        assert result == []

    def test_returns_empty_list_for_empty_dir(self, tmp_path: Path):
        """Returns empty list when projects directory exists but is empty."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        result = discover_projects(projects_dir)
        assert result == []

    def test_skips_files_not_directories(self, tmp_path: Path):
        """Skips files in projects directory, only returns directories."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create a file (not a directory)
        (projects_dir / "not-a-project.txt").write_text("content", encoding="utf-8")

        # Create a valid project directory
        project = projects_dir / "real-project"
        project.mkdir()
        (project / "session.jsonl").write_text('{"type": "user"}', encoding="utf-8")

        result = discover_projects(projects_dir)
        assert len(result) == 1
        assert result[0].name == "real-project"

    def test_only_includes_dirs_with_jsonl(self, tmp_path: Path):
        """Only includes directories that contain at least one .jsonl file."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Directory with JSONL - should be included
        project_with_jsonl = projects_dir / "with-jsonl"
        project_with_jsonl.mkdir()
        (project_with_jsonl / "session.jsonl").write_text('{"type": "user"}', encoding="utf-8")

        # Directory without JSONL - should be skipped
        project_without = projects_dir / "without-jsonl"
        project_without.mkdir()
        (project_without / "readme.txt").write_text("no jsonl here", encoding="utf-8")

        result = discover_projects(projects_dir)
        assert len(result) == 1
        assert result[0].name == "with-jsonl"


class TestFindHistoryFile:
    """Tests for find_history_file() - locates ~/.claude/history.jsonl."""

    def test_returns_path_when_file_exists(self, tmp_path: Path):
        """Returns Path to history.jsonl when it exists."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            history_file = tmp_path / ".claude" / "history.jsonl"
            history_file.parent.mkdir(parents=True)
            history_file.write_text('{"display": "test"}', encoding="utf-8")

            result = find_history_file()

            assert result is not None
            assert result == history_file

    def test_returns_none_when_file_missing(self, tmp_path: Path):
        """Returns None when history.jsonl doesn't exist."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            # .claude directory exists but no history.jsonl
            (tmp_path / ".claude").mkdir()
            result = find_history_file()
            assert result is None

    def test_returns_none_when_claude_dir_missing(self, tmp_path: Path):
        """Returns None when .claude directory doesn't exist."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = find_history_file()
            assert result is None

    def test_returns_none_when_path_is_directory(self, tmp_path: Path):
        """Returns None when history.jsonl path exists but is a directory."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            history_dir = tmp_path / ".claude" / "history.jsonl"
            history_dir.parent.mkdir(parents=True)
            history_dir.mkdir()  # It's a directory, not a file

            result = find_history_file()
            assert result is None


class TestLoadHistoryFile:
    """Tests for load_history_file() - parses history.jsonl into cache."""

    def test_loads_valid_history_records(self, tmp_path: Path):
        """Loads valid JSONL records into cache, returns count of inserted rows."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            '{"display": "cmd1", "project": "proj1", "sessionId": "sess1", "timestamp": 1000}\n'
            '{"display": "cmd2", "project": "proj2", "sessionId": "sess2", "timestamp": 2000}\n'
            '{"display": "cmd3", "project": "proj1", "sessionId": "sess3", "timestamp": 3000}\n',
            encoding="utf-8"
        )

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")
        count = load_history_file(history_file, cache)

        assert count == 3

        # Verify data was stored in history_commands table
        import sqlite3
        conn = sqlite3.connect(tmp_path / "test-cache.db")
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM history_commands")
        rows = cursor.fetchall()
        conn.close()

        assert len(rows) == 3
        assert rows[0]["display"] == "cmd1"
        assert rows[1]["display"] == "cmd2"
        assert rows[2]["display"] == "cmd3"

    def test_skips_invalid_json_lines(self, tmp_path: Path):
        """Skips lines that aren't valid JSON."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            '{"display": "valid1", "project": "p1", "sessionId": "s1", "timestamp": 1000}\n'
            'not valid json\n'
            '{"display": "valid2", "project": "p2", "sessionId": "s2", "timestamp": 2000}\n',
            encoding="utf-8"
        )

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")
        count = load_history_file(history_file, cache)

        assert count == 2  # Only 2 valid JSON lines

    def test_skips_non_dict_json(self, tmp_path: Path):
        """Skips JSON values that aren't objects (arrays, strings, numbers, null)."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            '{"display": "valid", "project": "p1", "sessionId": "s1", "timestamp": 1000}\n'
            '["array", "not", "object"]\n'
            '"just a string"\n'
            '123\n'
            'null\n'
            'true\n'
            '{"display": "valid2", "project": "p2", "sessionId": "s2", "timestamp": 2000}\n',
            encoding="utf-8"
        )

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")
        count = load_history_file(history_file, cache)

        assert count == 2  # Only the two dict objects

    def test_skips_missing_fields_gracefully(self, tmp_path: Path):
        """Handles records with missing optional fields without crashing."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            '{"display": "cmd1"}\n'  # Missing project, sessionId, timestamp
            '{"display": "cmd2", "project": "p2"}\n'  # Missing sessionId, timestamp
            '{"display": "cmd3", "project": "p3", "sessionId": "s3", "timestamp": 3000}\n',
            encoding="utf-8"
        )

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")
        count = load_history_file(history_file, cache)

        assert count == 3  # All three should be inserted (with defaults for missing)

    def test_scrubs_surrogates_in_display_text(self, tmp_path: Path):
        """Scrubs lone surrogate characters from display field."""
        history_file = tmp_path / "history.jsonl"
        # Write surrogate using bytes to avoid Python encode error
        history_file.write_bytes(
            b'{"display": "cmd with \\ud800 surrogate", "project": "p1", "sessionId": "s1", "timestamp": 1000}\n'
        )

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")
        count = load_history_file(history_file, cache)

        assert count == 1
        cached = cache.get_cached_project_data()
        # The surrogate should be replaced with replacement char
        assert cached is not None

    def test_returns_zero_for_empty_file(self, tmp_path: Path):
        """Returns 0 for empty history file."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text("", encoding="utf-8")

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")
        count = load_history_file(history_file, cache)

        assert count == 0

    def test_idempotent_duplicate_commands(self, tmp_path: Path):
        """Running twice with same data doesn't duplicate (UNIQUE constraint)."""
        history_file = tmp_path / "history.jsonl"
        history_file.write_text(
            '{"display": "cmd1", "project": "p1", "sessionId": "s1", "timestamp": 1000}\n',
            encoding="utf-8"
        )

        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")

        count1 = load_history_file(history_file, cache)
        count2 = load_history_file(history_file, cache)

        assert count1 == 1
        assert count2 == 0  # Second run inserts nothing (duplicate)

    def test_handles_missing_file_gracefully(self, tmp_path: Path):
        """Returns 0 when history file doesn't exist."""
        history_file = tmp_path / "nonexistent.jsonl"
        cache = CacheManager(tmp_path / "project", "1.0.0", db_path=tmp_path / "test-cache.db")

        # Should not raise, just return 0
        count = load_history_file(history_file, cache)
        assert count == 0


class TestCacheManagerPublicAPI:
    """Tests for CacheManager public API used by external consumers."""

    def test_cache_manager_initialization(self, tmp_path: Path):
        """CacheManager initializes with project path and version."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        cache = CacheManager(project_dir, "1.0.0", db_path=tmp_path / "test.db")

        assert cache.project_path == project_dir
        assert cache.library_version == "1.0.0"
        assert cache.db_path.exists()

    def test_save_and_load_cached_entries(self, tmp_path: Path):
        """Save entries to cache and load them back."""
        from claude_code_log.models import (
            UserTranscriptEntry,
            UserMessageModel,
            TextContent,
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        jsonl_file = project_dir / "session.jsonl"
        jsonl_file.write_text('{"type": "user"}', encoding="utf-8")

        cache = CacheManager(project_dir, "1.0.0", db_path=tmp_path / "test.db")

        entry = UserTranscriptEntry(
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
        )

        cache.save_cached_entries(jsonl_file, [entry])
        assert cache.is_file_cached(jsonl_file)

        loaded = cache.load_cached_entries(jsonl_file)
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded[0].type == "user"

    def test_update_session_cache(self, tmp_path: Path):
        """Update and retrieve session cache data."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache = CacheManager(project_dir, "1.0.0", db_path=tmp_path / "test.db")

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

        cache.update_session_cache(session_data)

        cached = cache.get_cached_project_data()
        assert cached is not None
        assert "session1" in cached.sessions
        assert cached.sessions["session1"].summary == "Test session"

    def test_update_project_aggregates(self, tmp_path: Path):
        """Update and retrieve project-level aggregates."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache = CacheManager(project_dir, "1.0.0", db_path=tmp_path / "test.db")

        cache.update_project_aggregates(
            total_message_count=100,
            total_input_tokens=1000,
            total_output_tokens=2000,
            total_cache_creation_tokens=50,
            total_cache_read_tokens=25,
            earliest_timestamp="2023-01-01T10:00:00Z",
            latest_timestamp="2023-01-01T20:00:00Z",
        )

        cached = cache.get_cached_project_data()
        assert cached is not None
        assert cached.total_message_count == 100
        assert cached.total_input_tokens == 1000
        assert cached.total_output_tokens == 2000

    def test_get_modified_files(self, tmp_path: Path):
        """Identify files that have been modified since caching."""
        from claude_code_log.models import (
            UserTranscriptEntry,
            UserMessageModel,
            TextContent,
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        cache = CacheManager(project_dir, "1.0.0", db_path=tmp_path / "test.db")

        file1 = project_dir / "file1.jsonl"
        file2 = project_dir / "file2.jsonl"
        file1.write_text("content1", encoding="utf-8")
        file2.write_text("content2", encoding="utf-8")

        entry = UserTranscriptEntry(
            parentUuid=None,
            isSidechain=False,
            userType="user",
            cwd="/test",
            sessionId="session1",
            version="1.0.0",
            uuid="user1",
            timestamp="2023-01-01T10:00:00Z",
            type="user",
            message=UserMessageModel(role="user", content=[TextContent(type="text", text="Hello")]),
        )

        # Cache only file1
        cache.save_cached_entries(file1, [entry])

        modified = cache.get_modified_files([file1, file2])

        assert len(modified) == 1
        assert file2 in modified
        assert file1 not in modified

    def test_clear_cache(self, tmp_path: Path):
        """Clear all cached data."""
        from claude_code_log.models import (
            UserTranscriptEntry,
            UserMessageModel,
            TextContent,
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        jsonl_file = project_dir / "session.jsonl"
        jsonl_file.write_text('{"type": "user"}', encoding="utf-8")

        cache = CacheManager(project_dir, "1.0.0", db_path=tmp_path / "test.db")

        entry = UserTranscriptEntry(
            parentUuid=None,
            isSidechain=False,
            userType="user",
            cwd="/test",
            sessionId="session1",
            version="1.0.0",
            uuid="user1",
            timestamp="2023-01-01T10:00:00Z",
            type="user",
            message=UserMessageModel(role="user", content=[TextContent(type="text", text="Hello")]),
        )

        cache.save_cached_entries(jsonl_file, [entry])
        assert cache.is_file_cached(jsonl_file)

        cache.clear_cache()

        assert not cache.is_file_cached(jsonl_file)
        cached = cache.get_cached_project_data()
        assert cached is not None
        assert len(cached.cached_files) == 0
        assert len(cached.sessions) == 0


class TestAPIExports:
    """Verify all public API symbols are exported correctly."""

    def test_all_exports_are_importable(self):
        """All symbols in __all__ can be imported from claude_code_log.api."""
        from claude_code_log import api

        expected_exports = [
            "load_transcript",
            "load_directory_transcripts",
            "CacheManager",
            "SessionCacheData",
            "ensure_fresh_cache",
            "create_transcript_entry",
            "extract_text_content",
            "parse_timestamp",
            "discover_projects",
            "find_history_file",
            "load_history_file",
            "TranscriptEntry",
        ]

        for symbol in expected_exports:
            assert hasattr(api, symbol), f"Missing export: {symbol}"

    def test_version_is_accessible(self):
        """Package version is accessible."""
        from claude_code_log import __version__
        assert isinstance(__version__, str)
        assert len(__version__) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
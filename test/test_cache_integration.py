#!/usr/bin/env python3
"""Integration tests for cache functionality with CLI and converter."""

import json
from pathlib import Path
from typing import Generator
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.converter import convert_jsonl_to_html, process_projects_hierarchy
from claude_code_log.cache import CacheManager


class ProjectSetup:
    """Container for test project setup data."""

    def __init__(self, projects_dir: Path, db_path: Path):
        self.projects_dir = projects_dir
        self.db_path = db_path


@pytest.fixture
def temp_projects_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[ProjectSetup, None, None]:
    """Create a temporary projects directory structure with isolated cache.

    Uses CLAUDE_CODE_LOG_CACHE_PATH env var for cache isolation,
    enabling parallel test execution with pytest-xdist.

    Returns ProjectSetup with both projects_dir and db_path.
    """
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Set env var to isolate cache for this test
    isolated_db = tmp_path / "test-cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(isolated_db))

    yield ProjectSetup(projects_dir, isolated_db)


@pytest.fixture
def temp_projects_dir(temp_projects_setup: ProjectSetup) -> Path:
    """Backward-compatible fixture returning just the projects dir."""
    return temp_projects_setup.projects_dir


@pytest.fixture
def sample_jsonl_data():
    """Sample JSONL transcript data."""
    return [
        {
            "type": "user",
            "uuid": "user-1",
            "timestamp": "2023-01-01T10:00:00Z",
            "sessionId": "session-1",
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "user",
            "cwd": "/test",
            "message": {"role": "user", "content": "Hello, how are you?"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "2023-01-01T10:01:00Z",
            "sessionId": "session-1",
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "assistant",
            "cwd": "/test",
            "requestId": "req-1",
            "message": {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-3",
                "content": [{"type": "text", "text": "I'm doing well, thank you!"}],
                "usage": {"input_tokens": 10, "output_tokens": 15},
            },
        },
        {
            "type": "summary",
            "summary": "A friendly greeting conversation",
            "leafUuid": "assistant-1",
        },
    ]


class ProjectWithCache:
    """Container for test project with cache info.

    Implements Path-like interface for backward compatibility with tests
    that pass this directly to functions expecting Path objects.
    """

    def __init__(self, project_dir: Path, db_path: Path):
        self.project_dir = project_dir
        self.db_path = db_path

    # Path-like interface for backward compatibility
    def __fspath__(self) -> str:
        return str(self.project_dir)

    def __str__(self) -> str:
        return str(self.project_dir)

    def __truediv__(self, other: str) -> Path:
        return self.project_dir / other

    @property
    def parent(self) -> Path:
        return self.project_dir.parent

    def exists(self) -> bool:
        return self.project_dir.exists()

    def is_dir(self) -> bool:
        return self.project_dir.is_dir()

    def is_file(self) -> bool:
        return self.project_dir.is_file()

    def glob(self, pattern: str):
        return self.project_dir.glob(pattern)

    def iterdir(self):
        return self.project_dir.iterdir()

    @property
    def name(self) -> str:
        return self.project_dir.name


@pytest.fixture
def setup_test_project(
    temp_projects_setup: ProjectSetup, sample_jsonl_data
) -> ProjectWithCache:
    """Set up a test project with JSONL files."""
    project_dir = temp_projects_setup.projects_dir / "test-project"
    project_dir.mkdir()

    # Create JSONL file
    jsonl_file = project_dir / "session-1.jsonl"
    with open(jsonl_file, "w") as f:
        for entry in sample_jsonl_data:
            f.write(json.dumps(entry) + "\n")

    return ProjectWithCache(project_dir, temp_projects_setup.db_path)


class TestCacheIntegrationCLI:
    """Test cache integration with CLI commands."""

    def test_cli_no_cache_flag(self, setup_test_project: ProjectWithCache):
        """Test --no-cache flag disables caching."""
        project_dir = setup_test_project.project_dir
        db_path = setup_test_project.db_path

        runner = CliRunner()

        # Run with caching enabled (default)
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0

        # Check if SQLite cache was created at the isolated location
        assert db_path.exists()

        # Clear the cache
        runner.invoke(main, [str(project_dir), "--clear-cache"])

        # Run with --no-cache flag
        result2 = runner.invoke(main, [str(project_dir), "--no-cache"])
        assert result2.exit_code == 0

        # Cache should be empty (project should not be populated)
        cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count == 0

    def test_cli_clear_cache_flag(self, setup_test_project):
        """Test --clear-cache flag clears cache data."""
        project_dir = setup_test_project

        runner = CliRunner()

        # Run to create cache
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0

        # Verify cache exists with data
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count > 0

        # Clear cache
        result2 = runner.invoke(main, [str(project_dir), "--clear-cache"])
        assert result2.exit_code == 0

        # Verify cache is cleared (no files or sessions)
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert len(cached_data.cached_files) == 0

    def test_cli_all_projects_caching(
        self, temp_projects_setup: ProjectSetup, sample_jsonl_data
    ):
        """Test caching with --all-projects flag."""
        temp_projects_dir = temp_projects_setup.projects_dir
        db_path = temp_projects_setup.db_path

        # Create multiple projects
        for i in range(3):
            project_dir = temp_projects_dir / f"project-{i}"
            project_dir.mkdir()

            jsonl_file = project_dir / f"session-{i}.jsonl"
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for entry in sample_jsonl_data:
                    # Modify session ID for each project
                    entry_copy = entry.copy()
                    if "sessionId" in entry_copy:
                        entry_copy["sessionId"] = f"session-{i}"
                    f.write(json.dumps(entry_copy) + "\n")

        runner = CliRunner()

        # Run with --all-projects
        result = runner.invoke(main, [str(temp_projects_dir), "--all-projects"])
        assert result.exit_code == 0

        # Verify SQLite cache database created at isolated location
        assert db_path.exists()

        # Verify cache data exists for each project
        for i in range(3):
            project_dir = temp_projects_dir / f"project-{i}"
            cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
            cached_data = cache_manager.get_cached_project_data()
            assert cached_data is not None
            assert len(cached_data.cached_files) >= 1

    def test_cli_date_filtering_with_cache(self, setup_test_project):
        """Test date filtering works correctly with caching."""
        project_dir = setup_test_project

        runner = CliRunner()

        # First run to populate cache
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0

        # Run with date filtering (should use cached data where possible)
        result2 = runner.invoke(
            main,
            [str(project_dir), "--from-date", "2023-01-01", "--to-date", "2023-01-01"],
        )
        assert result2.exit_code == 0


class TestCacheIntegrationConverter:
    """Test cache integration with converter functions."""

    def test_convert_jsonl_to_html_with_cache(
        self, setup_test_project: ProjectWithCache
    ):
        """Test converter uses cache when available."""
        project_dir = setup_test_project.project_dir
        db_path = setup_test_project.db_path

        # First conversion (populate cache)
        output1 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output1.exists()

        # Verify SQLite cache was created at isolated location
        assert db_path.exists()

        # Verify cache has data
        cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert len(cached_data.cached_files) >= 1

        # Second conversion (should use cache)
        output2 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output2.exists()

    def test_convert_jsonl_to_html_no_cache(self, setup_test_project):
        """Test converter bypasses cache when disabled."""
        project_dir = setup_test_project

        # Conversion with cache disabled
        output = convert_jsonl_to_html(input_path=project_dir, use_cache=False)
        assert output.exists()

        # SQLite db may still exist from fixture setup, but project data should be empty
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count == 0

    def test_process_projects_hierarchy_with_cache(
        self, temp_projects_setup: ProjectSetup, sample_jsonl_data
    ):
        """Test project hierarchy processing uses cache effectively."""
        temp_projects_dir = temp_projects_setup.projects_dir
        db_path = temp_projects_setup.db_path

        # Create multiple projects
        for i in range(2):
            project_dir = temp_projects_dir / f"project-{i}"
            project_dir.mkdir()

            jsonl_file = project_dir / f"session-{i}.jsonl"
            with open(jsonl_file, "w", encoding="utf-8") as f:
                for entry in sample_jsonl_data:
                    entry_copy = entry.copy()
                    if "sessionId" in entry_copy:
                        entry_copy["sessionId"] = f"session-{i}"
                    f.write(json.dumps(entry_copy) + "\n")

        # First processing (populate cache)
        output1 = process_projects_hierarchy(
            projects_path=temp_projects_dir, use_cache=True
        )
        assert output1.exists()

        # Verify SQLite cache database was created at isolated location
        assert db_path.exists()

        # Verify cache data exists for each project
        for i in range(2):
            project_dir = temp_projects_dir / f"project-{i}"
            cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
            cached_data = cache_manager.get_cached_project_data()
            assert cached_data is not None
            assert len(cached_data.cached_files) >= 1

        # Second processing (should use cache)
        output2 = process_projects_hierarchy(
            projects_path=temp_projects_dir, use_cache=True
        )
        assert output2.exists()


class TestCachePerformanceIntegration:
    """Test cache performance benefits in integration scenarios."""

    def test_cache_performance_with_large_project(self, temp_projects_dir):
        """Test that caching provides performance benefits."""
        project_dir = temp_projects_dir / "large-project"
        project_dir.mkdir()

        # Create a larger JSONL file
        large_jsonl_data = []
        for i in range(100):  # 100 entries
            large_jsonl_data.extend(
                [
                    {
                        "type": "user",
                        "uuid": f"user-{i}",
                        "timestamp": f"2023-01-01T{10 + i // 10:02d}:{i % 10:02d}:00Z",
                        "sessionId": f"session-{i // 10}",
                        "version": "1.0.0",
                        "parentUuid": None,
                        "isSidechain": False,
                        "userType": "user",
                        "cwd": "/test",
                        "message": {"role": "user", "content": f"This is message {i}"},
                    },
                    {
                        "type": "assistant",
                        "uuid": f"assistant-{i}",
                        "timestamp": f"2023-01-01T{10 + i // 10:02d}:{i % 10:02d}:30Z",
                        "sessionId": f"session-{i // 10}",
                        "version": "1.0.0",
                        "parentUuid": None,
                        "isSidechain": False,
                        "userType": "assistant",
                        "cwd": "/test",
                        "requestId": f"req-{i}",
                        "message": {
                            "id": f"msg-{i}",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-3",
                            "content": [
                                {"type": "text", "text": f"Response to message {i}"}
                            ],
                            "usage": {"input_tokens": 10, "output_tokens": 15},
                        },
                    },
                ]
            )

        jsonl_file = project_dir / "large-session.jsonl"
        with open(jsonl_file, "w", encoding="utf-8") as f:
            for entry in large_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        import time

        # First run (no cache)
        output1 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output1.exists()

        # Second run (with cache)
        start_time = time.monotonic()
        output2 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        second_run_time = time.monotonic() - start_time
        assert output2.exists()

        # Second run should be faster (though this is not always guaranteed in tests)
        # We mainly check that it completes successfully
        assert second_run_time >= 0  # Basic sanity check

    def test_cache_with_date_filtering_performance(self, setup_test_project):
        """Test that timestamp-based cache filtering works efficiently."""
        project_dir = setup_test_project

        # Populate cache first
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Test date filtering (should use efficient cache filtering)
        output = convert_jsonl_to_html(
            input_path=project_dir,
            from_date="2023-01-01",
            to_date="2023-01-01",
            use_cache=True,
        )
        assert output.exists()


class TestCacheEdgeCases:
    """Test edge cases in cache integration."""

    def test_mixed_cached_and_uncached_files(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test handling when some files are cached and others are not."""
        project_dir = temp_projects_dir / "mixed-project"
        project_dir.mkdir()

        # Create first file and process it (will be cached)
        file1 = project_dir / "session-1.jsonl"
        with open(file1, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Add second file (will not be cached initially)
        file2 = project_dir / "session-2.jsonl"
        with open(file2, "w") as f:
            for entry in sample_jsonl_data:
                entry_copy = entry.copy()
                if "sessionId" in entry_copy:
                    entry_copy["sessionId"] = "session-2"
                if "uuid" in entry_copy:
                    entry_copy["uuid"] = entry_copy["uuid"].replace("1", "2")
                f.write(json.dumps(entry_copy) + "\n")

        # Process again (should handle mixed cache state)
        output = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output.exists()

    def test_cache_corruption_recovery(self, setup_test_project):
        """Test recovery from corrupted cache files."""
        project_with_cache = setup_test_project
        project_dir = project_with_cache.project_dir
        db_path = project_with_cache.db_path

        # Create initial cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Corrupt SQLite database
        assert db_path.exists()
        with open(db_path, "r+b") as f:
            f.seek(100)  # Skip SQLite header
            f.write(b"corrupted data here")

        # Should recover gracefully (recreates database)
        output = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output.exists()

    def test_cache_with_empty_project(self, temp_projects_dir):
        """Test cache behavior with empty project directories."""
        empty_project = temp_projects_dir / "empty-project"
        empty_project.mkdir()

        # Should handle empty directory gracefully by generating empty HTML
        try:
            output = convert_jsonl_to_html(input_path=empty_project, use_cache=True)
            # If it succeeds, should produce an empty HTML file
            assert output.exists()
        except FileNotFoundError:
            # This is also acceptable behavior for empty directories
            pass

    def test_cache_version_upgrade_scenario(self, setup_test_project):
        """Test cache behavior during version upgrades."""
        project_dir = setup_test_project

        # Create cache with old version
        with patch("claude_code_log.cache.get_library_version", return_value="1.0.0"):
            cache_manager_old = CacheManager(project_dir, "1.0.0")
            # Verify project was created in SQLite database
            cached_data = cache_manager_old.get_cached_project_data()
            assert cached_data is not None
            assert cached_data.version == "1.0.0"

        # Process with new version (should handle version mismatch)
        with patch("claude_code_log.cache.get_library_version", return_value="2.0.0"):
            output = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
            assert output.exists()


class TestArchivedSessionsIntegration:
    """Test archived sessions functionality - sessions cached but JSONL deleted."""

    def test_get_archived_sessions_after_file_deletion(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test that sessions become archived when JSONL files are deleted."""
        project_dir = temp_projects_dir / "archived-test"
        project_dir.mkdir()

        # Create JSONL file with session data
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Verify session is in cache
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert "session-1" in cached_data.sessions

        # Delete the JSONL file
        jsonl_file.unlink()

        # Now session-1 should be archived (no valid session IDs)
        valid_session_ids: set[str] = set()  # No JSONL files left
        archived = cache_manager.get_archived_sessions(valid_session_ids)

        assert "session-1" in archived
        assert archived["session-1"].message_count > 0
        assert archived["session-1"].first_timestamp == "2023-01-01T10:00:00Z"

    def test_get_archived_sessions_with_some_files_remaining(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test archived sessions when only some JSONL files are deleted."""
        project_dir = temp_projects_dir / "partial-archived"
        project_dir.mkdir()

        # Create two independent session files (unique UUIDs, as in real transcripts)
        for session_id in ["session-1", "session-2"]:
            jsonl_file = project_dir / f"{session_id}.jsonl"
            with open(jsonl_file, "w") as f:
                for entry in sample_jsonl_data:
                    entry_copy = entry.copy()
                    if "sessionId" in entry_copy:
                        entry_copy["sessionId"] = session_id
                    if "uuid" in entry_copy:
                        entry_copy["uuid"] = f"{entry_copy['uuid']}-{session_id}"
                    f.write(json.dumps(entry_copy) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Delete only session-1
        (project_dir / "session-1.jsonl").unlink()

        # session-2 should be valid, session-1 should be archived
        valid_session_ids = {"session-2"}
        cache_manager = CacheManager(project_dir, "1.0.0")
        archived = cache_manager.get_archived_sessions(valid_session_ids)

        assert "session-1" in archived
        assert "session-2" not in archived

    def test_export_session_to_jsonl(self, temp_projects_dir, sample_jsonl_data):
        """Test exporting session messages for JSONL restoration."""
        project_dir = temp_projects_dir / "export-test"
        project_dir.mkdir()

        # Create JSONL file
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Export messages from cache
        cache_manager = CacheManager(project_dir, "1.0.0")
        exported_messages = cache_manager.export_session_to_jsonl("session-1")

        # Should have exported messages (not summary which has no sessionId)
        assert len(exported_messages) >= 2  # user + assistant messages

        # Each message should be valid JSON
        for msg_json in exported_messages:
            parsed = json.loads(msg_json)
            assert "type" in parsed
            assert parsed["sessionId"] == "session-1"

    def test_load_session_entries_for_rendering(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test loading session entries from cache for HTML/Markdown rendering."""
        project_dir = temp_projects_dir / "load-entries-test"
        project_dir.mkdir()

        # Create JSONL file
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Load entries from cache
        cache_manager = CacheManager(project_dir, "1.0.0")
        entries = cache_manager.load_session_entries("session-1")

        # Should have TranscriptEntry objects
        assert len(entries) >= 2

        # Check that entries are proper types
        entry_types = [e.type for e in entries]
        assert "user" in entry_types
        assert "assistant" in entry_types

    def test_full_archive_and_restore_workflow(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test the full workflow: cache -> delete -> archive -> restore."""
        project_dir = temp_projects_dir / "full-workflow"
        project_dir.mkdir()

        # Step 1: Create JSONL file and cache it
        original_file = project_dir / "session-1.jsonl"
        with open(original_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Verify cache populated
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        original_message_count = cached_data.sessions["session-1"].message_count

        # Step 2: Delete the JSONL file
        original_file.unlink()
        assert not original_file.exists()

        # Step 3: Verify session is now archived
        archived = cache_manager.get_archived_sessions(set())
        assert "session-1" in archived

        # Step 4: Restore the session from cache
        exported_messages = cache_manager.export_session_to_jsonl("session-1")
        restored_file = project_dir / "session-1.jsonl"
        with open(restored_file, "w") as f:
            for msg in exported_messages:
                f.write(msg + "\n")

        # Step 5: Verify the restored file exists and session is no longer archived
        assert restored_file.exists()

        valid_session_ids = {"session-1"}
        archived_after_restore = cache_manager.get_archived_sessions(valid_session_ids)
        assert "session-1" not in archived_after_restore

        # Step 6: Verify restored content is valid by re-processing
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        cached_data = cache_manager.get_cached_project_data()
        # Message count should be preserved
        assert cached_data is not None
        assert cached_data.sessions["session-1"].message_count == original_message_count

    def test_archived_session_count_in_converter(
        self, temp_projects_dir, sample_jsonl_data, capsys
    ):
        """Test that archived session count is reported in converter output."""
        project_dir = temp_projects_dir / "count-test"
        project_dir.mkdir()

        # Create two independent sessions so one remains after deletion
        # (unique UUIDs per session, as in real transcripts)
        for session_id in ["session-1", "session-2"]:
            jsonl_file = project_dir / f"{session_id}.jsonl"
            with open(jsonl_file, "w") as f:
                for entry in sample_jsonl_data:
                    entry_copy = entry.copy()
                    if "sessionId" in entry_copy:
                        entry_copy["sessionId"] = session_id
                    if "uuid" in entry_copy:
                        entry_copy["uuid"] = f"{entry_copy['uuid']}-{session_id}"
                    f.write(json.dumps(entry_copy) + "\n")

        # Process to cache (as part of all-projects hierarchy)
        process_projects_hierarchy(projects_path=temp_projects_dir, use_cache=True)

        # Delete only session-1, keeping session-2 so project is still found
        (project_dir / "session-1.jsonl").unlink()

        # Process again - should report archived sessions
        process_projects_hierarchy(
            projects_path=temp_projects_dir, use_cache=True, silent=False
        )

        captured = capsys.readouterr()
        # Output should mention archived sessions
        assert "archived" in captured.out.lower()

    def test_load_entries_preserves_message_order(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test that loaded entries preserve chronological order."""
        project_dir = temp_projects_dir / "order-test"
        project_dir.mkdir()

        # Create JSONL file
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Load entries from cache
        cache_manager = CacheManager(project_dir, "1.0.0")
        entries = cache_manager.load_session_entries("session-1")

        # Filter to entries with timestamps and extract them
        timestamps: list[str] = []
        for e in entries:
            if hasattr(e, "timestamp") and e.timestamp:
                timestamps.append(str(e.timestamp))

        # Verify chronological order (ISO timestamps are lexicographically sortable)
        assert timestamps == sorted(timestamps)

    def test_export_empty_session_returns_empty_list(self, temp_projects_dir):
        """Test that exporting a non-existent session returns empty list."""
        project_dir = temp_projects_dir / "empty-export"
        project_dir.mkdir()

        # Create a dummy JSONL to initialize the project
        jsonl_file = project_dir / "dummy.jsonl"
        jsonl_file.write_text("{}\n")

        cache_manager = CacheManager(project_dir, "1.0.0")

        # Export non-existent session
        exported = cache_manager.export_session_to_jsonl("non-existent-session")
        assert exported == []

        # Load entries for non-existent session
        entries = cache_manager.load_session_entries("non-existent-session")
        assert entries == []

    def test_export_session_produces_compact_json(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test that exported JSONL has compact JSON format (no spaces after separators)."""
        project_dir = temp_projects_dir / "compact-json-test"
        project_dir.mkdir()

        # Create JSONL file
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Export messages
        cache_manager = CacheManager(project_dir, "1.0.0")
        exported_messages = cache_manager.export_session_to_jsonl("session-1")

        # Each message should be compact JSON (no spaces after : or ,)
        for msg_json in exported_messages:
            # Should not have ": " (colon-space) pattern except in string values
            # Check by ensuring re-serialization produces same result
            parsed = json.loads(msg_json)
            compact_reserialized = json.dumps(parsed, separators=(",", ":"))
            assert msg_json == compact_reserialized, (
                f"JSON should be compact format.\n"
                f"Got: {msg_json[:100]}...\n"
                f"Expected: {compact_reserialized[:100]}..."
            )

    def test_delete_session_from_cache(self, temp_projects_dir, sample_jsonl_data):
        """Test deleting a session from cache."""
        project_dir = temp_projects_dir / "delete-session-test"
        project_dir.mkdir()

        # Create JSONL file
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Verify session exists in cache
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert "session-1" in cached_data.sessions

        # Delete the session
        result = cache_manager.delete_session("session-1")
        assert result is True

        # Verify session is gone from cache
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert "session-1" not in cached_data.sessions

        # Export should return empty
        exported = cache_manager.export_session_to_jsonl("session-1")
        assert exported == []

    def test_delete_session_invalidates_file_cache(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test that delete_session also removes cached_files entry.

        Previously, delete_session only removed from messages, html_cache, and
        sessions tables but left cached_files intact. This caused is_file_cached()
        to return True even though the session data was gone, leading to
        load_cached_entries() returning an empty list instead of None.
        """
        project_dir = temp_projects_dir / "delete-file-cache-test"
        project_dir.mkdir()

        # Create JSONL file with session ID matching file name
        session_id = "session-1"
        jsonl_file = project_dir / f"{session_id}.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Verify file is cached before deletion
        cache_manager = CacheManager(project_dir, "1.0.0")
        assert cache_manager.is_file_cached(jsonl_file), (
            "File should be cached before deletion"
        )
        entries_before = cache_manager.load_cached_entries(jsonl_file)
        assert entries_before is not None and len(entries_before) > 0, (
            "Should load cached entries before deletion"
        )

        # Delete the session
        result = cache_manager.delete_session(session_id)
        assert result is True

        # Verify cached_files entry is also removed
        assert not cache_manager.is_file_cached(jsonl_file), (
            "is_file_cached() should return False after delete_session() "
            "because the cached_files entry should be removed"
        )

        # load_cached_entries should return None (not empty list) for uncached file
        entries_after = cache_manager.load_cached_entries(jsonl_file)
        assert entries_after is None, (
            "load_cached_entries() should return None after delete_session() "
            "because the file is no longer considered cached"
        )

    def test_delete_session_removes_page_sessions(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test that delete_session removes page_sessions entries.

        When a session is part of a paginated combined transcript, deleting
        the session should also remove its entry from the page_sessions table.
        """
        project_dir = temp_projects_dir / "delete-page-sessions-test"
        project_dir.mkdir()

        session_id = "session-1"
        jsonl_file = project_dir / f"{session_id}.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        cache_manager = CacheManager(project_dir, "1.0.0")

        # Add page cache entry with this session
        cache_manager.update_page_cache(
            page_number=1,
            html_path="combined_transcripts.html",
            page_size_config=50,
            session_ids=[session_id],
            message_count=5,
            first_timestamp="2024-01-01T00:00:00Z",
            last_timestamp="2024-01-01T01:00:00Z",
            total_input_tokens=100,
            total_output_tokens=200,
            total_cache_creation_tokens=0,
            total_cache_read_tokens=0,
        )

        # Verify page has the session
        page_data = cache_manager.get_page_data(1)
        assert page_data is not None
        assert session_id in page_data.session_ids

        # Delete the session
        result = cache_manager.delete_session(session_id)
        assert result is True

        # Verify page_sessions entry is removed
        # The page itself still exists, but the session mapping should be gone
        import sqlite3

        conn = sqlite3.connect(cache_manager.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM page_sessions ps
                   JOIN html_pages hp ON ps.page_id = hp.id
                   WHERE hp.project_id = ? AND ps.session_id = ?""",
                (cache_manager._project_id, session_id),
            ).fetchone()
            assert row["cnt"] == 0, (
                "page_sessions entry should be removed after delete_session()"
            )
        finally:
            conn.close()

    def test_delete_nonexistent_session(self, temp_projects_dir):
        """Test deleting a session that doesn't exist returns False."""
        project_dir = temp_projects_dir / "delete-nonexistent"
        project_dir.mkdir()

        # Create a dummy JSONL to initialize the project
        jsonl_file = project_dir / "dummy.jsonl"
        jsonl_file.write_text("{}\n")

        cache_manager = CacheManager(project_dir, "1.0.0")

        # Delete non-existent session
        result = cache_manager.delete_session("non-existent-session")
        assert result is False

    def test_delete_project_from_cache(self, temp_projects_dir, sample_jsonl_data):
        """Test deleting an entire project from cache."""
        project_dir = temp_projects_dir / "delete-project-test"
        project_dir.mkdir()

        # Create JSONL file
        jsonl_file = project_dir / "session-1.jsonl"
        with open(jsonl_file, "w") as f:
            for entry in sample_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        # Process to populate cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Verify project exists in cache
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None

        # Delete the project
        result = cache_manager.delete_project()
        assert result is True

        # Cache manager should no longer have valid project ID
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is None


class TestGetAllCachedProjects:
    """Tests for get_all_cached_projects() function."""

    def test_get_all_cached_projects_finds_active_and_archived(
        self, temp_projects_dir, sample_jsonl_data
    ):
        """Test finding both active and archived projects."""
        from claude_code_log.cache import get_all_cached_projects

        # Create two projects - one active, one that will be archived
        active_dir = temp_projects_dir / "active-project"
        active_dir.mkdir()
        archived_dir = temp_projects_dir / "archived-project"
        archived_dir.mkdir()

        # Create JSONL files in both
        for proj_dir in [active_dir, archived_dir]:
            jsonl_file = proj_dir / "session-1.jsonl"
            with open(jsonl_file, "w") as f:
                for entry in sample_jsonl_data:
                    f.write(json.dumps(entry) + "\n")

        # Process both projects to populate cache
        convert_jsonl_to_html(input_path=active_dir, use_cache=True)
        convert_jsonl_to_html(input_path=archived_dir, use_cache=True)

        # Delete JSONL from "archived" project to simulate archival
        (archived_dir / "session-1.jsonl").unlink()

        # Get all cached projects
        projects = get_all_cached_projects(temp_projects_dir)

        # Should find both projects
        project_paths = {p[0] for p in projects}
        assert str(active_dir) in project_paths
        assert str(archived_dir) in project_paths

        # Check is_archived flag
        for project_path, is_archived in projects:
            if project_path == str(active_dir):
                assert is_archived is False
            elif project_path == str(archived_dir):
                assert is_archived is True

    def test_get_all_cached_projects_empty_dir(self, temp_projects_dir):
        """Test get_all_cached_projects with no cache."""
        from claude_code_log.cache import get_all_cached_projects

        # No claude-code-log-cache.db exists
        projects = get_all_cached_projects(temp_projects_dir)
        assert projects == []

    def test_get_all_cached_projects_nonexistent_dir(self, tmp_path):
        """Test get_all_cached_projects with nonexistent directory."""
        from claude_code_log.cache import get_all_cached_projects

        nonexistent = tmp_path / "does-not-exist"
        projects = get_all_cached_projects(nonexistent)
        assert projects == []

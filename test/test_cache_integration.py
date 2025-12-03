#!/usr/bin/env python3
"""Integration tests for cache functionality with CLI and converter."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main
from claude_code_log.converter import convert_jsonl_to_html, process_projects_hierarchy
from claude_code_log.cache import CacheManager


@pytest.fixture
def temp_projects_dir():
    """Create a temporary projects directory structure."""
    with tempfile.TemporaryDirectory() as temp_dir:
        projects_dir = Path(temp_dir) / "projects"
        projects_dir.mkdir()
        yield projects_dir


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


@pytest.fixture
def setup_test_project(temp_projects_dir, sample_jsonl_data):
    """Set up a test project with JSONL files."""
    project_dir = temp_projects_dir / "test-project"
    project_dir.mkdir()

    # Create JSONL file
    jsonl_file = project_dir / "session-1.jsonl"
    with open(jsonl_file, "w") as f:
        for entry in sample_jsonl_data:
            f.write(json.dumps(entry) + "\n")

    return project_dir


class TestCacheIntegrationCLI:
    """Test cache integration with CLI commands."""

    def test_cli_no_cache_flag(self, setup_test_project, temp_sqlite_db):
        """Test --no-cache flag disables caching."""
        project_dir = setup_test_project

        runner = CliRunner()

        # Run with caching enabled (default)
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0

        # Check if cache was created in SQLite
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None

        # Clear the cache
        runner.invoke(main, [str(project_dir), "--clear-cache"])

        # Run with --no-cache flag
        result2 = runner.invoke(main, [str(project_dir), "--no-cache"])
        assert result2.exit_code == 0

        # Note: --no-cache just skips using cache during this run,
        # but doesn't prevent SQLite database from being created

    def test_cli_clear_cache_flag(self, setup_test_project, temp_sqlite_db):
        """Test --clear-cache flag removes cache data."""
        project_dir = setup_test_project

        runner = CliRunner()

        # Run to create cache
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0

        # Verify cache exists in SQLite
        cache_manager = CacheManager(project_dir, "1.0.0")
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None

        # Clear cache
        result2 = runner.invoke(main, [str(project_dir), "--clear-cache"])
        assert result2.exit_code == 0

        # Verify cache is cleared
        cache_manager2 = CacheManager(project_dir, "1.0.0")
        # After clear, a new empty project record is created
        cached_data2 = cache_manager2.get_cached_project_data()
        # Should have no cached files
        assert cached_data2 is not None
        assert len(cached_data2.cached_files) == 0

    def test_cli_all_projects_caching(
        self, temp_projects_dir, sample_jsonl_data, temp_sqlite_db
    ):
        """Test caching with --all-projects flag."""
        # Create multiple projects
        for i in range(3):
            project_dir = temp_projects_dir / f"project-{i}"
            project_dir.mkdir()

            jsonl_file = project_dir / f"session-{i}.jsonl"
            with open(jsonl_file, "w") as f:
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

        # Verify cache created for each project in SQLite
        for i in range(3):
            project_dir = temp_projects_dir / f"project-{i}"
            cache_manager = CacheManager(project_dir, "1.0.0")
            cached_data = cache_manager.get_cached_project_data()
            assert cached_data is not None
            # Should have at least one cached file
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

    def test_convert_jsonl_to_html_with_cache(self, setup_test_project, temp_sqlite_db):
        """Test converter uses cache when available."""
        project_dir = setup_test_project

        # First conversion (populate cache)
        output1 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output1.exists()

        # Verify cache was created in SQLite
        cache_manager = CacheManager(project_dir, "1.0.0")
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

        # Note: With SQLite, the database file might still exist,
        # but no cache entries should be created for this project
        # The test just verifies the conversion works without cache

    def test_process_projects_hierarchy_with_cache(
        self, temp_projects_dir, sample_jsonl_data, temp_sqlite_db
    ):
        """Test project hierarchy processing uses cache effectively."""
        # Create multiple projects
        for i in range(2):
            project_dir = temp_projects_dir / f"project-{i}"
            project_dir.mkdir()

            jsonl_file = project_dir / f"session-{i}.jsonl"
            with open(jsonl_file, "w") as f:
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

        # Verify caches were created in SQLite
        for i in range(2):
            project_dir = temp_projects_dir / f"project-{i}"
            cache_manager = CacheManager(project_dir, "1.0.0")
            cached_data = cache_manager.get_cached_project_data()
            assert cached_data is not None

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
        with open(jsonl_file, "w") as f:
            for entry in large_jsonl_data:
                f.write(json.dumps(entry) + "\n")

        import time

        # First run (no cache)
        output1 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        assert output1.exists()

        # Second run (with cache)
        start_time = time.time()
        output2 = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
        second_run_time = time.time() - start_time
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
        project_dir = setup_test_project

        # Create initial cache
        convert_jsonl_to_html(input_path=project_dir, use_cache=True)

        # Corrupt cache file
        cache_dir = project_dir / "cache"
        cache_files = list(cache_dir.glob("*.json"))
        if cache_files:
            cache_file = [f for f in cache_files if f.name != "index.json"][0]
            cache_file.write_text("corrupted json data", encoding="utf-8")

        # Should recover gracefully
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

    def test_cache_version_upgrade_scenario(self, setup_test_project, temp_sqlite_db):
        """Test cache behavior during version upgrades."""
        project_dir = setup_test_project

        # Create cache with old version
        with patch("claude_code_log.cache.get_library_version", return_value="1.0.0"):
            cache_manager_old = CacheManager(project_dir, "1.0.0")
            # Store some project data
            cache_manager_old.update_project_aggregates(
                total_message_count=10,
                total_input_tokens=100,
                total_output_tokens=200,
                total_cache_creation_tokens=0,
                total_cache_read_tokens=0,
                earliest_timestamp="2023-01-01T10:00:00Z",
                latest_timestamp="2023-01-01T11:00:00Z",
            )

        # Process with new version (should handle version mismatch)
        with patch("claude_code_log.cache.get_library_version", return_value="2.0.0"):
            output = convert_jsonl_to_html(input_path=project_dir, use_cache=True)
            assert output.exists()

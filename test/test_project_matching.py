#!/usr/bin/env python3
"""Tests for project working directory matching functionality."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from claude_code_log.cli import find_projects_by_cwd


class TestProjectMatching:
    """Test cases for working directory based project matching."""

    def test_find_projects_by_cwd_with_cache(self):
        """Test finding projects by current working directory using cache data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_dir = Path(temp_dir)

            # Create mock project directories
            project1 = projects_dir / "project1"
            project2 = projects_dir / "project2"
            project1.mkdir()
            project2.mkdir()

            # Create mock JSONL files
            (project1 / "test1.jsonl").touch()
            (project2 / "test2.jsonl").touch()

            # Mock cache data for projects
            mock_cache1 = Mock()
            mock_cache1.working_directories = ["/Users/test/workspace/myproject"]

            mock_cache2 = Mock()
            mock_cache2.working_directories = ["/Users/test/other/project"]

            with patch("claude_code_log.cli.CacheManager") as mock_cache_manager:

                def cache_side_effect(project_dir, version):
                    cache_instance = Mock()
                    if project_dir == project1:
                        cache_instance.get_cached_project_data.return_value = (
                            mock_cache1
                        )
                    elif project_dir == project2:
                        cache_instance.get_cached_project_data.return_value = (
                            mock_cache2
                        )
                    else:
                        cache_instance.get_cached_project_data.return_value = None
                    return cache_instance

                mock_cache_manager.side_effect = cache_side_effect

                # Test matching current working directory
                matching_projects = find_projects_by_cwd(
                    projects_dir, "/Users/test/workspace/myproject"
                )
                assert len(matching_projects) == 1
                assert matching_projects[0] == project1

                # Test non-matching current working directory
                matching_projects = find_projects_by_cwd(
                    projects_dir, "/Users/test/completely/different"
                )
                assert len(matching_projects) == 0

    def test_find_projects_by_cwd_subdirectory_matching(self):
        """Test that subdirectories of project working directories are matched."""
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_dir = Path(temp_dir)

            # Create mock project directory
            project1 = projects_dir / "project1"
            project1.mkdir()
            (project1 / "test1.jsonl").touch()

            # Mock cache data with parent directory
            mock_cache1 = Mock()
            mock_cache1.working_directories = ["/Users/test/workspace/myproject"]

            with patch("claude_code_log.cli.CacheManager") as mock_cache_manager:

                def cache_side_effect(project_dir, version):
                    cache_instance = Mock()
                    if project_dir == project1:
                        cache_instance.get_cached_project_data.return_value = (
                            mock_cache1
                        )
                    else:
                        cache_instance.get_cached_project_data.return_value = None
                    return cache_instance

                mock_cache_manager.side_effect = cache_side_effect

                # Test from subdirectory
                matching_projects = find_projects_by_cwd(
                    projects_dir, "/Users/test/workspace/myproject/subdir"
                )
                assert len(matching_projects) == 1
                assert matching_projects[0] == project1

    def test_find_projects_by_cwd_fallback_to_name_matching(self):
        """Test fallback to project name matching when no cache data available."""
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_dir = Path(temp_dir)

            # Create a real directory to test with (platform-independent)
            with tempfile.TemporaryDirectory() as test_project_dir:
                test_project_path = Path(test_project_dir)

                # Import here to use the function
                from claude_code_log.cli import convert_project_path_to_claude_dir

                # Get the expected project name for this platform
                expected_project_dir = convert_project_path_to_claude_dir(
                    test_project_path
                )
                project_name = expected_project_dir.name

                # Create project directory with Claude-style naming
                project1 = projects_dir / project_name
                project1.mkdir()
                (project1 / "test1.jsonl").touch()

                with patch("claude_code_log.cli.CacheManager") as mock_cache_manager:
                    # Mock no cache data available
                    cache_instance = Mock()
                    cache_instance.get_cached_project_data.return_value = None
                    mock_cache_manager.return_value = cache_instance

                    # Test matching based on reconstructed path from project name
                    matching_projects = find_projects_by_cwd(
                        projects_dir, str(test_project_path)
                    )
                    assert len(matching_projects) == 1
                    assert matching_projects[0] == project1

    def test_find_projects_by_cwd_default_current_directory(self):
        """Test using current working directory when none specified."""
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_dir = Path(temp_dir)

            # Use a real temporary directory for the current working directory
            # to avoid issues with Path.resolve() calling os.getcwd() on Windows
            with tempfile.TemporaryDirectory() as cwd_temp_dir:
                cwd_path = str(Path(cwd_temp_dir).resolve())

                with (
                    patch("claude_code_log.cli.os.getcwd") as mock_getcwd,
                    patch("claude_code_log.cli.CacheManager") as mock_cache_manager,
                ):
                    mock_getcwd.return_value = cwd_path

                    # Mock no projects found
                    cache_instance = Mock()
                    cache_instance.get_cached_project_data.return_value = None
                    mock_cache_manager.return_value = cache_instance

                    # Should use current working directory from os.getcwd()
                    find_projects_by_cwd(projects_dir)  # No cwd specified

                    # Verify os.getcwd() was called at least once
                    # On Windows, Path.resolve() may call os.getcwd() internally,
                    # so we check it was called rather than called_once
                    assert mock_getcwd.called, "os.getcwd() should have been called"

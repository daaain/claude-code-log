#!/usr/bin/env python3
"""Integration tests with realistic JSONL data from open-source projects.

These tests use real-world data copied from ~/.claude/projects/ to test:
- Multi-project hierarchy processing
- Cache operations with realistic data volumes
- Edge cases found in actual usage (empty files, naming patterns, etc.)
- CLI operations with the new --projects-dir option
"""

import json
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Generator

import pytest
from click.testing import CliRunner

from claude_code_log.cli import (
    main,
    convert_project_path_to_claude_dir,
    find_projects_by_cwd,
    get_default_projects_dir,
)
from claude_code_log.converter import (
    convert_jsonl_to,
    convert_jsonl_to_html,
    process_projects_hierarchy,
)
from claude_code_log.cache import CacheManager, get_library_version

# Path to realistic test data
REAL_PROJECTS_DIR = Path(__file__).parent / "test_data" / "real_projects"


def make_valid_user_entry(
    content: str,
    session_id: str,
    timestamp: str = "2024-12-01T10:00:00Z",
    uuid_val: str | None = None,
) -> dict:
    """Create a valid user transcript entry with all required fields."""
    return {
        "type": "user",
        "timestamp": timestamp,
        "sessionId": session_id,
        "uuid": uuid_val or f"test-uuid-{uuid.uuid4().hex[:8]}",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/Users/test/project",
        "version": "1.0.0",
        "message": {"role": "user", "content": content},
    }


def make_valid_assistant_entry(
    content: str,
    session_id: str,
    timestamp: str = "2024-12-01T10:00:01Z",
    uuid_val: str | None = None,
) -> dict:
    """Create a valid assistant transcript entry with all required fields."""
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": session_id,
        "uuid": uuid_val or f"test-uuid-{uuid.uuid4().hex[:8]}",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/Users/test/project",
        "version": "1.0.0",
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:16]}",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-5-sonnet-20241022",
            "content": [{"type": "text", "text": content}],
        },
    }


@pytest.fixture(scope="module")
def real_projects_path() -> Path:
    """Return path to realistic test projects."""
    if not REAL_PROJECTS_DIR.exists():
        pytest.skip(
            "Real projects test data not found. Run: ./scripts/setup_test_data.sh"
        )
    return REAL_PROJECTS_DIR


@pytest.fixture
def temp_projects_copy(real_projects_path: Path) -> Generator[Path, None, None]:
    """Create a temporary copy of test projects for isolated testing.

    This fixture copies all test data to a temp directory and cleans up
    any existing cache/HTML files to ensure fresh state for each test.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_projects = Path(temp_dir) / "projects"
        shutil.copytree(real_projects_path, temp_projects)

        # Clean any existing cache/HTML to ensure fresh state
        for project_dir in temp_projects.iterdir():
            if project_dir.is_dir():
                # Remove cache directory
                cache_dir = project_dir / "cache"
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
                # Remove HTML files
                for html_file in project_dir.glob("*.html"):
                    html_file.unlink()

        yield temp_projects


@pytest.fixture
def projects_with_cache(real_projects_path: Path) -> Generator[Path, None, None]:
    """Create a temporary copy with pre-generated cache/HTML for JSSoundRecorder.

    This fixture generates cache and HTML files for the smallest project
    (JSSoundRecorder) to test cache/HTML persistence scenarios.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_projects = Path(temp_dir) / "projects"
        shutil.copytree(real_projects_path, temp_projects)

        # Clean any existing cache/HTML first
        for project_dir in temp_projects.iterdir():
            if project_dir.is_dir():
                cache_dir = project_dir / "cache"
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)
                for html_file in project_dir.glob("*.html"):
                    html_file.unlink()

        # Generate cache/HTML for JSSoundRecorder (smallest project)
        jss_project = temp_projects / "-Users-dain-workspace-JSSoundRecorder"
        if jss_project.exists():
            convert_jsonl_to_html(jss_project)

        yield temp_projects


@pytest.mark.integration
class TestProjectHierarchyProcessing:
    """Test processing multiple projects hierarchy."""

    def test_process_all_real_projects(self, temp_projects_copy: Path) -> None:
        """Process all realistic projects and verify index generation."""
        index_path = process_projects_hierarchy(temp_projects_copy)

        assert index_path.exists()
        assert index_path.name == "index.html"

        # Verify each project got a combined transcript
        for project_dir in temp_projects_copy.iterdir():
            if project_dir.is_dir() and list(project_dir.glob("*.jsonl")):
                combined_html = project_dir / "combined_transcripts.html"
                assert combined_html.exists(), (
                    f"Missing combined HTML for {project_dir.name}"
                )

    def test_projects_dont_merge_by_prefix(self, temp_projects_copy: Path) -> None:
        """Verify projects with similar prefixes stay separate.

        This tests the edge case where project names differ only by suffix,
        like platform-frontend-next vs platform-frontend-next-main.
        """
        # Create projects matching platform-frontend-next* pattern
        for suffix in ["", "-main", "-another", "-more"]:
            name = f"-Users-test-platform-frontend-next{suffix}"
            project_dir = temp_projects_copy / name
            project_dir.mkdir()
            # Create a minimal JSONL file
            entry = make_valid_user_entry(
                content=f"test message {suffix}",
                session_id=f"session{suffix}",
            )
            (project_dir / "test.jsonl").write_text(
                json.dumps(entry) + "\n", encoding="utf-8"
            )

        # Process all projects
        process_projects_hierarchy(temp_projects_copy)

        # Each should have separate HTML - verify they didn't merge
        for suffix in ["", "-main", "-another", "-more"]:
            name = f"-Users-test-platform-frontend-next{suffix}"
            project_dir = temp_projects_copy / name
            combined_html = project_dir / "combined_transcripts.html"
            assert combined_html.exists(), f"Project {name} should have its own HTML"

    def test_empty_jsonl_files_handled(self, temp_projects_copy: Path) -> None:
        """Verify empty JSONL files don't cause failures.

        The coderabbit-review-helper test data contains 9 empty JSONL files.
        """
        project_dir = (
            temp_projects_copy / "-Users-dain-workspace-coderabbit-review-helper"
        )
        if not project_dir.exists():
            pytest.skip("coderabbit-review-helper test data not available")

        # Count empty files to verify test data
        empty_count = sum(
            1 for f in project_dir.glob("*.jsonl") if f.stat().st_size == 0
        )
        assert empty_count > 0, "Expected empty JSONL files in test data"

        # Should not raise an exception
        convert_jsonl_to_html(project_dir)

        assert (project_dir / "combined_transcripts.html").exists()


@pytest.mark.integration
class TestCLIWithProjectsDir:
    """Test CLI commands with --projects-dir option."""

    def test_all_projects_with_custom_dir(self, temp_projects_copy: Path) -> None:
        """Test --all-projects with custom --projects-dir."""
        runner = CliRunner()
        result = runner.invoke(
            main, ["--all-projects", "--projects-dir", str(temp_projects_copy)]
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Successfully processed" in result.output
        assert (temp_projects_copy / "index.html").exists()

    def test_clear_cache_with_projects_dir(self, temp_projects_copy: Path) -> None:
        """Test cache clearing with custom projects directory."""
        runner = CliRunner()

        # First, create caches by processing
        result = runner.invoke(
            main, ["--all-projects", "--projects-dir", str(temp_projects_copy)]
        )
        assert result.exit_code == 0

        # Verify SQLite cache was created
        cache_db = temp_projects_copy / "claude-code-log-cache.db"
        assert cache_db.exists(), "SQLite cache should exist after processing"

        # Clear caches
        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--clear-cache",
            ],
        )

        assert result.exit_code == 0
        assert "clear" in result.output.lower()

        # Verify SQLite database was deleted
        assert not cache_db.exists(), "SQLite cache database should be deleted"

    def test_clear_html_with_projects_dir(self, temp_projects_copy: Path) -> None:
        """Test HTML clearing with custom projects directory."""
        runner = CliRunner()

        # First, create HTML by processing
        result = runner.invoke(
            main, ["--all-projects", "--projects-dir", str(temp_projects_copy)]
        )
        assert result.exit_code == 0

        # Verify HTML files were created
        html_exists = any(
            (project_dir / "combined_transcripts.html").exists()
            for project_dir in temp_projects_copy.iterdir()
            if project_dir.is_dir()
        )
        assert html_exists, "HTML should exist after processing"

        # Clear HTML files
        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--clear-html",
            ],
        )

        assert result.exit_code == 0
        assert "clear" in result.output.lower() or "removed" in result.output.lower()

        # Verify all HTML files were actually deleted
        remaining_html: list[Path] = []
        for project_dir in temp_projects_copy.iterdir():
            if not project_dir.is_dir():
                continue
            combined = project_dir / "combined_transcripts.html"
            if combined.exists():
                remaining_html.append(combined)
            # Also check for session HTML files
            remaining_html.extend(project_dir.glob("session-*.html"))

        assert not remaining_html, (
            f"HTML files should be deleted but found: {remaining_html}"
        )


@pytest.mark.integration
class TestPathConversionEdgeCases:
    """Test path conversion with real-world directory names."""

    def test_dot_in_directory_name(self) -> None:
        """Test conversion of paths with dots (danieldemmel.me-next).

        The project 'danieldemmel.me-next' has a dot in the name, which gets
        converted to a hyphen in the Claude project directory name.
        """
        input_path = Path("/Users/dain/workspace/danieldemmel.me-next")
        result = convert_project_path_to_claude_dir(input_path)

        # The dot should be preserved - dots don't get converted
        # The result should be -Users-dain-workspace-danieldemmel.me-next
        assert "danieldemmel" in result.name
        # Note: dots in the actual path remain dots, only slashes become dashes

    def test_multiple_similar_project_names_are_distinct(self) -> None:
        """Test that similar project names produce distinct paths."""
        paths = [
            Path("/Users/test/platform-frontend-next"),
            Path("/Users/test/platform-frontend-next-main"),
            Path("/Users/test/platform-frontend-next-another"),
        ]

        results = [convert_project_path_to_claude_dir(p) for p in paths]
        result_names = [r.name for r in results]

        # All names should be unique
        assert len(result_names) == len(set(result_names)), (
            "Similar project names should produce distinct Claude directory names"
        )

    def test_conversion_with_custom_base_dir(self, tmp_path: Path) -> None:
        """Test path conversion with custom base directory."""
        import sys

        input_path = Path("/Users/test/myproject")
        result = convert_project_path_to_claude_dir(input_path, tmp_path)

        # Result should be under the custom base directory
        assert result.parent == tmp_path

        if sys.platform == "win32":
            # On Windows, Path("/Users/test/myproject").resolve() adds the current drive
            # e.g., "D:\Users\test\myproject" -> "D--Users-test-myproject"
            # The result includes the drive letter with double dash
            assert result.name.endswith("-Users-test-myproject")
            assert "--Users-test-myproject" in result.name
        else:
            assert result.name == "-Users-test-myproject"


@pytest.mark.integration
class TestMultiCwdSessions:
    """Test handling of sessions with multiple working directories."""

    def test_session_with_varying_cwd(self, temp_projects_copy: Path) -> None:
        """Test that sessions with multiple cwds are captured in cache.

        The danieldemmel-me-next project has sessions spanning multiple
        working directories.
        """
        project_dir = temp_projects_copy / "-Users-dain-workspace-danieldemmel-me-next"
        if not project_dir.exists():
            pytest.skip("danieldemmel-me-next test data not available")

        # Process the project
        convert_jsonl_to_html(project_dir)

        # Check cache for working directories
        cache_manager = CacheManager(project_dir, get_library_version())
        project_cache = cache_manager.get_cached_project_data()

        assert project_cache is not None, "Cache should exist after processing"
        # The project may have working directories recorded
        # (depends on what messages are in the test data)

    def test_working_directories_in_all_projects(
        self, temp_projects_copy: Path
    ) -> None:
        """Verify working directories are extracted from all projects."""
        process_projects_hierarchy(temp_projects_copy)

        for project_dir in temp_projects_copy.iterdir():
            if not project_dir.is_dir() or not list(project_dir.glob("*.jsonl")):
                continue

            cache_manager = CacheManager(project_dir, get_library_version())
            project_cache = cache_manager.get_cached_project_data()

            # Cache should exist for each project
            assert project_cache is not None, f"Cache missing for {project_dir.name}"


@pytest.mark.integration
class TestCacheWithRealData:
    """Test cache functionality with realistic project data."""

    def test_cache_creation_all_projects(self, temp_projects_copy: Path) -> None:
        """Test cache is created correctly for all projects."""
        process_projects_hierarchy(temp_projects_copy)

        # Verify SQLite cache database was created
        cache_db = temp_projects_copy / "claude-code-log-cache.db"
        assert cache_db.exists(), "SQLite cache database should exist"

        for project_dir in temp_projects_copy.iterdir():
            if not project_dir.is_dir() or not list(project_dir.glob("*.jsonl")):
                continue

            cache_manager = CacheManager(project_dir, get_library_version())
            cached_data = cache_manager.get_cached_project_data()
            assert cached_data is not None, f"Cache missing for {project_dir.name}"
            assert cached_data.version is not None
            assert isinstance(cached_data.sessions, dict)

    def test_cache_invalidation_on_modification(self, temp_projects_copy: Path) -> None:
        """Test cache detects file modifications."""
        project_dir = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project_dir.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Build initial cache
        convert_jsonl_to_html(project_dir)
        cache_manager = CacheManager(project_dir, get_library_version())

        # Get a non-empty JSONL file
        jsonl_files = [f for f in project_dir.glob("*.jsonl") if f.stat().st_size > 0]
        if not jsonl_files:
            pytest.skip("No non-empty JSONL files in test data")

        # Modify a file
        test_file = jsonl_files[0]
        original_content = test_file.read_text(encoding="utf-8")
        entry = make_valid_user_entry(
            content="test modification",
            session_id="test-modification",
        )
        test_file.write_text(
            original_content + "\n" + json.dumps(entry) + "\n", encoding="utf-8"
        )

        # Check if modification is detected
        modified = cache_manager.get_modified_files(list(project_dir.glob("*.jsonl")))
        assert len(modified) > 0, "Cache should detect file modification"

    def test_cache_version_stored(self, temp_projects_copy: Path) -> None:
        """Test that cache version is stored and can be retrieved."""
        project_dir = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project_dir.exists():
            pytest.skip("JSSoundRecorder test data not available")

        convert_jsonl_to_html(project_dir)

        cache_manager = CacheManager(project_dir, get_library_version())
        cached_data = cache_manager.get_cached_project_data()

        assert cached_data is not None
        assert cached_data.version == get_library_version()


@pytest.mark.integration
class TestGitWorktreeScenarios:
    """Test handling of git worktree-like patterns."""

    def test_worktree_projects_stay_separate(self, temp_projects_copy: Path) -> None:
        """Test worktree-like projects don't merge.

        Git worktrees often result in projects like:
        - myapp (main branch)
        - myapp-main (explicit main worktree)
        - myapp-feature (feature branch worktree)
        """
        # Create worktree-like projects
        for suffix in ["", "-main", "-feature-branch"]:
            name = f"-Users-test-myapp{suffix}"
            project_dir = temp_projects_copy / name
            project_dir.mkdir()
            entry = make_valid_user_entry(
                content=f"worktree test {suffix}",
                session_id=f"session{suffix}",
            )
            (project_dir / "test.jsonl").write_text(
                json.dumps(entry) + "\n", encoding="utf-8"
            )

        # Process all
        process_projects_hierarchy(temp_projects_copy)

        # Verify each has its own output
        for suffix in ["", "-main", "-feature-branch"]:
            name = f"-Users-test-myapp{suffix}"
            project_dir = temp_projects_copy / name
            assert (project_dir / "combined_transcripts.html").exists(), (
                f"Worktree project {name} should have separate HTML"
            )


@pytest.mark.integration
class TestGetDefaultProjectsDir:
    """Test the get_default_projects_dir helper function."""

    def test_returns_path_object(self) -> None:
        """Test that get_default_projects_dir returns a Path."""
        result = get_default_projects_dir()
        assert isinstance(result, Path)

    def test_returns_expected_path(self) -> None:
        """Test that get_default_projects_dir returns ~/.claude/projects."""
        result = get_default_projects_dir()
        expected = Path.home() / ".claude" / "projects"
        assert result == expected


@pytest.mark.integration
class TestFindProjectsByCwd:
    """Test project discovery with custom projects directory."""

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Test data uses Unix-style directory names that require Unix path conversion",
    )
    def test_find_projects_with_custom_dir(self, temp_projects_copy: Path) -> None:
        """Test find_projects_by_cwd works with custom projects directory.

        This test uses Unix-style paths because the test data directories are named
        using Unix path conventions (e.g., -Users-dain-workspace-JSSoundRecorder).
        The path conversion logic is platform-specific, so this test only runs on Unix.
        """
        # This tests that when we have a custom projects_dir, the matching still works
        results = find_projects_by_cwd(
            temp_projects_copy, "/Users/dain/workspace/JSSoundRecorder"
        )

        # The project should be found if path conversion works correctly
        assert results, (
            "Expected find_projects_by_cwd to return at least one matching project"
        )
        assert any("JSSoundRecorder" in r.name for r in results)


@pytest.mark.integration
class TestCacheHTMLPersistence:
    """Test cache and HTML persistence scenarios.

    These tests verify that pre-existing cache/HTML files are handled correctly:
    - Cache reuse when valid
    - HTML regeneration when cache changes
    - Cache version mismatch handling
    - Missing cache file recovery
    """

    def test_uses_existing_cache(self, projects_with_cache: Path) -> None:
        """Verify pre-existing cache is used without regeneration."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        cache_file = project / "cache" / "index.json"
        if not cache_file.exists():
            pytest.skip("Cache was not generated by fixture")

        # Record original mtime
        original_mtime = cache_file.stat().st_mtime

        # Wait a small amount to ensure mtime would change if file is rewritten
        import time

        time.sleep(0.01)

        # Process again (should use existing cache)
        convert_jsonl_to_html(project)

        # Cache should not be modified (mtime unchanged)
        assert cache_file.stat().st_mtime == original_mtime

    def test_uses_existing_html(self, projects_with_cache: Path) -> None:
        """Verify pre-existing HTML is not regenerated if current."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        html_file = project / "combined_transcripts.html"
        if not html_file.exists():
            pytest.skip("HTML was not generated by fixture")

        original_mtime = html_file.stat().st_mtime

        import time

        time.sleep(0.01)

        convert_jsonl_to_html(project)

        # HTML should not be modified (is_html_outdated returns False)
        assert html_file.stat().st_mtime == original_mtime

    def test_regenerates_html_when_version_outdated(
        self, projects_with_cache: Path
    ) -> None:
        """Verify HTML is regenerated when its version is outdated."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        html_file = project / "combined_transcripts.html"
        if not html_file.exists():
            pytest.skip("HTML not generated by fixture")

        original_mtime = html_file.stat().st_mtime

        import time

        time.sleep(0.01)

        # Modify the HTML version comment to simulate outdated version
        html_content = html_file.read_text(encoding="utf-8")
        # Replace the version comment with an old version
        # Format: <!-- Generated by claude-code-log v0.8.0 -->
        modified_content = html_content.replace(
            f"<!-- Generated by claude-code-log v{get_library_version()} -->",
            "<!-- Generated by claude-code-log v0.0.0-old -->",
        )
        html_file.write_text(modified_content, encoding="utf-8")

        # Process should regenerate HTML due to version mismatch
        convert_jsonl_to_html(project)

        # HTML should be regenerated with correct version
        new_content = html_file.read_text(encoding="utf-8")
        assert (
            f"<!-- Generated by claude-code-log v{get_library_version()} -->"
            in new_content
        )
        assert html_file.stat().st_mtime > original_mtime

    def test_cache_version_mismatch_triggers_rebuild(
        self, projects_with_cache: Path
    ) -> None:
        """Verify stale cache (wrong version) triggers rebuild."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        cache_index = project / "cache" / "index.json"
        if not cache_index.exists():
            pytest.skip("Cache not generated by fixture")

        # Corrupt version in cache
        cache_data = json.loads(cache_index.read_text(encoding="utf-8"))
        cache_data["version"] = "0.0.0-fake"
        cache_index.write_text(json.dumps(cache_data), encoding="utf-8")

        # Process should rebuild cache
        convert_jsonl_to_html(project)

        # Cache should have correct version now
        new_cache_data = json.loads(cache_index.read_text(encoding="utf-8"))
        assert new_cache_data["version"] == get_library_version()

    def test_missing_cache_files_regenerated(self, projects_with_cache: Path) -> None:
        """Verify missing per-file cache entries are regenerated."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        cache_dir = project / "cache"
        if not cache_dir.exists():
            pytest.skip("Cache not generated by fixture")

        # Count original cache files
        original_cache_files = list(
            f for f in cache_dir.glob("*.json") if f.name != "index.json"
        )
        if not original_cache_files:
            pytest.skip("No per-file cache entries to delete")

        original_count = len(original_cache_files)

        # Delete one cache file (not index.json)
        original_cache_files[0].unlink()

        # Process should regenerate missing file
        convert_jsonl_to_html(project)

        # Count should be restored
        new_cache_files = list(
            f for f in cache_dir.glob("*.json") if f.name != "index.json"
        )
        assert len(new_cache_files) == original_count

    def test_individual_session_html_exists(self, projects_with_cache: Path) -> None:
        """Verify individual session HTML files are generated."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        session_files = list(project.glob("session-*.html"))

        # Should have session files for non-empty JSONL files
        # Count non-empty JSONL files
        non_empty_jsonl = [f for f in project.glob("*.jsonl") if f.stat().st_size > 0]

        if non_empty_jsonl:
            assert len(session_files) > 0, (
                "Expected individual session HTML files for non-empty JSONL files"
            )


@pytest.mark.integration
class TestIncrementalJSONLUpdates:
    """Test cache/HTML regeneration when JSONL files are modified."""

    def test_adding_lines_triggers_cache_update(
        self, projects_with_cache: Path
    ) -> None:
        """Adding new lines to JSONL triggers cache rebuild."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if not jsonl_files:
            pytest.skip("No JSONL files available")

        jsonl_file = jsonl_files[0]
        cache_index = project / "cache" / "index.json"
        if not cache_index.exists():
            pytest.skip("Cache not generated by fixture")

        original_cache_mtime = cache_index.stat().st_mtime

        # Append new message to JSONL
        entry = make_valid_user_entry(
            content="New message added",
            session_id="test-incremental",
        )
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write("\n" + json.dumps(entry) + "\n")

        time.sleep(0.01)
        convert_jsonl_to_html(project)

        # Cache should be updated
        assert cache_index.stat().st_mtime > original_cache_mtime

    def test_adding_lines_triggers_html_regeneration(
        self, projects_with_cache: Path
    ) -> None:
        """Adding new lines to JSONL triggers HTML regeneration."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if not jsonl_files:
            pytest.skip("No JSONL files available")

        jsonl_file = jsonl_files[0]
        html_file = project / "combined_transcripts.html"
        if not html_file.exists():
            pytest.skip("HTML not generated by fixture")

        original_html_mtime = html_file.stat().st_mtime

        # Append new message
        entry = make_valid_user_entry(
            content="Another new message",
            session_id="test-incremental",
        )
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write("\n" + json.dumps(entry) + "\n")

        time.sleep(0.01)
        convert_jsonl_to_html(project)

        # HTML should be regenerated
        assert html_file.stat().st_mtime > original_html_mtime

    def test_new_content_appears_in_html(self, projects_with_cache: Path) -> None:
        """New message content appears in regenerated HTML."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if not jsonl_files:
            pytest.skip("No JSONL files available")

        jsonl_file = jsonl_files[0]
        html_file = project / "combined_transcripts.html"

        unique_content = f"UNIQUE_TEST_CONTENT_{uuid.uuid4().hex[:8]}"
        entry = make_valid_user_entry(
            content=unique_content,
            session_id="test-content-check",
        )
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write("\n" + json.dumps(entry) + "\n")

        convert_jsonl_to_html(project)

        html_content = html_file.read_text(encoding="utf-8")
        assert unique_content in html_content


@pytest.mark.integration
class TestAddingNewJSONLFiles:
    """Test handling of new JSONL files added to existing projects."""

    def test_new_file_detected_and_processed(self, projects_with_cache: Path) -> None:
        """New JSONL file is detected and processed."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        cache_index = project / "cache" / "index.json"
        if not cache_index.exists():
            pytest.skip("Cache not generated by fixture")

        original_cache = json.loads(cache_index.read_text(encoding="utf-8"))
        original_session_count = len(original_cache.get("sessions", {}))

        # Add new JSONL file
        new_file = project / "new-session-file.jsonl"
        entry = make_valid_user_entry(
            content="First message in new file",
            session_id="brand-new-session",
        )
        new_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        convert_jsonl_to_html(project)

        # Cache should include new session
        new_cache = json.loads(cache_index.read_text(encoding="utf-8"))
        assert len(new_cache.get("sessions", {})) > original_session_count

    def test_new_session_html_generated(self, projects_with_cache: Path) -> None:
        """New JSONL file generates corresponding session HTML."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        new_session_id = "new-session-for-html"
        new_file = project / f"{new_session_id}.jsonl"
        entry = make_valid_user_entry(
            content="Message for new session",
            session_id=new_session_id,
        )
        new_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        convert_jsonl_to_html(project)

        # Session HTML should be generated
        session_html = project / f"session-{new_session_id}.html"
        assert session_html.exists()

    def test_index_html_updated_with_new_project_stats(
        self, temp_projects_copy: Path
    ) -> None:
        """Index HTML updates when new files change project stats."""
        # Process all projects first
        process_projects_hierarchy(temp_projects_copy)

        index_file = temp_projects_copy / "index.html"
        original_mtime = index_file.stat().st_mtime

        # Add new file to one project
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        new_file = project / "extra-session.jsonl"
        entry = make_valid_user_entry(
            content="Extra session message",
            session_id="extra-session",
        )
        new_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        time.sleep(0.01)

        # Reprocess
        process_projects_hierarchy(temp_projects_copy)

        # Index should be regenerated
        assert index_file.stat().st_mtime > original_mtime


@pytest.mark.integration
class TestCLIOutputOption:
    """Test --output/-o option for custom output paths."""

    def test_custom_output_single_file(self, temp_projects_copy: Path) -> None:
        """Custom output path works for single JSONL file."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if not jsonl_files:
            pytest.skip("No JSONL files available")

        jsonl_file = jsonl_files[0]
        custom_output = temp_projects_copy / "custom_output.html"

        result = runner.invoke(main, [str(jsonl_file), "-o", str(custom_output)])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert custom_output.exists()

    def test_custom_output_directory(self, temp_projects_copy: Path) -> None:
        """Custom output path works for directory processing."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        custom_output = temp_projects_copy / "my_transcript.html"

        result = runner.invoke(main, [str(project), "--output", str(custom_output)])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert custom_output.exists()

    def test_output_overwrites_existing(self, temp_projects_copy: Path) -> None:
        """Custom output overwrites existing file."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        custom_output = temp_projects_copy / "overwrite_test.html"
        custom_output.write_text("original content", encoding="utf-8")

        result = runner.invoke(main, [str(project), "-o", str(custom_output)])

        assert result.exit_code == 0
        assert custom_output.exists()
        # Content should be different from original
        content = custom_output.read_text(encoding="utf-8")
        assert "original content" not in content
        assert "<!DOCTYPE html>" in content


@pytest.mark.integration
class TestNoIndividualSessionsFlag:
    """Test --no-individual-sessions flag."""

    def test_no_session_files_generated(self, temp_projects_copy: Path) -> None:
        """Flag suppresses individual session HTML generation."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Clean any existing session files
        for f in project.glob("session-*.html"):
            f.unlink()

        result = runner.invoke(main, [str(project), "--no-individual-sessions"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"

        # Combined transcript should exist
        assert (project / "combined_transcripts.html").exists()

        # Individual session files should NOT exist
        session_files = list(project.glob("session-*.html"))
        assert len(session_files) == 0

    def test_combined_transcript_still_generated(
        self, temp_projects_copy: Path
    ) -> None:
        """Combined transcript is generated even with flag."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        result = runner.invoke(main, [str(project), "--no-individual-sessions"])

        assert result.exit_code == 0
        assert (project / "combined_transcripts.html").exists()

    def test_flag_with_all_projects(self, temp_projects_copy: Path) -> None:
        """Flag works with --all-projects - no session files should be generated."""
        runner = CliRunner()

        # Clean any pre-existing session files first
        for project in temp_projects_copy.iterdir():
            if project.is_dir():
                for session_file in project.glob("session-*.html"):
                    session_file.unlink()

        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--no-individual-sessions",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"

        # Verify combined transcripts are generated but no session files
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if project.exists():
            assert (project / "combined_transcripts.html").exists()
            session_files = list(project.glob("session-*.html"))
            assert len(session_files) == 0, (
                f"Expected no session files with --no-individual-sessions, found: {session_files}"
            )


@pytest.mark.integration
class TestDateFilteringIntegration:
    """Test date filtering with realistic project data."""

    def test_from_date_filters_messages(self, temp_projects_copy: Path) -> None:
        """--from-date filters old messages from output."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        result = runner.invoke(main, [str(project), "--from-date", "2025-01-01"])

        assert result.exit_code == 0
        # HTML should exist (may be empty of messages)
        assert (project / "combined_transcripts.html").exists()

    def test_date_filtering_with_all_projects(self, temp_projects_copy: Path) -> None:
        """Date filtering works with --all-projects."""
        runner = CliRunner()

        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--from-date",
                "yesterday",
            ],
        )

        assert result.exit_code == 0
        assert (temp_projects_copy / "index.html").exists()

    def test_date_range_filtering(self, temp_projects_copy: Path) -> None:
        """Both --from-date and --to-date work together."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        result = runner.invoke(
            main,
            [str(project), "--from-date", "2024-01-01", "--to-date", "2024-12-31"],
        )

        assert result.exit_code == 0
        assert (project / "combined_transcripts.html").exists()


@pytest.mark.integration
class TestIndexHTMLRegeneration:
    """Test index.html regeneration logic."""

    def test_index_regenerated_when_project_cache_updates(
        self, temp_projects_copy: Path
    ) -> None:
        """Index HTML is rewritten on every `process_projects_hierarchy`
        call (always-regenerate semantics — see PR #168). The JSONL edit
        here is incidental cargo from when index regen was gated on
        project-cache changes; today the assertion holds even without it,
        which is the contract we want pinned."""
        # Initial processing
        process_projects_hierarchy(temp_projects_copy)
        index_file = temp_projects_copy / "index.html"
        original_mtime = index_file.stat().st_mtime

        # Modify one project's JSONL
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if not jsonl_files:
            pytest.skip("No JSONL files available")

        jsonl_file = jsonl_files[0]
        entry = make_valid_user_entry(
            content="Trigger index update",
            session_id="index-test",
        )
        with open(jsonl_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        time.sleep(0.01)

        # Reprocess
        process_projects_hierarchy(temp_projects_copy)

        # Index should be regenerated
        assert index_file.stat().st_mtime > original_mtime

    def test_index_version_mismatch_triggers_regeneration(
        self, temp_projects_copy: Path
    ) -> None:
        """Index HTML with old version is regenerated."""
        process_projects_hierarchy(temp_projects_copy)
        index_file = temp_projects_copy / "index.html"

        # Corrupt version in index
        content = index_file.read_text(encoding="utf-8")
        modified = content.replace(
            f"<!-- Generated by claude-code-log v{get_library_version()} -->",
            "<!-- Generated by claude-code-log v0.0.0-old -->",
        )
        index_file.write_text(modified, encoding="utf-8")

        # Reprocess
        process_projects_hierarchy(temp_projects_copy)

        # Index should be regenerated with correct version
        new_content = index_file.read_text(encoding="utf-8")
        assert f"v{get_library_version()}" in new_content


@pytest.mark.integration
class TestErrorHandlingRealistic:
    """Test error handling with realistic project data."""

    def test_invalid_jsonl_line_handled(self, temp_projects_copy: Path) -> None:
        """Invalid JSON line doesn't crash processing."""
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if not jsonl_files:
            pytest.skip("No JSONL files available")

        jsonl_file = jsonl_files[0]

        # Append invalid JSON
        with open(jsonl_file, "a") as f:
            f.write("\n{invalid json here}\n")

        # Should not raise exception
        convert_jsonl_to_html(project)
        assert (project / "combined_transcripts.html").exists()

    def test_corrupted_cache_index_handled(self, projects_with_cache: Path) -> None:
        """Corrupted cache index is recovered."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        cache_index = project / "cache" / "index.json"
        if not cache_index.exists():
            pytest.skip("Cache not generated by fixture")

        # Corrupt the cache index
        cache_index.write_text("{invalid json", encoding="utf-8")

        # Should recover and reprocess
        convert_jsonl_to_html(project)

        # Cache should be valid again
        cache_data = json.loads(cache_index.read_text(encoding="utf-8"))
        assert "version" in cache_data

    def test_missing_cache_directory_handled(self, projects_with_cache: Path) -> None:
        """Missing cache directory is recreated."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        cache_dir = project / "cache"
        if not cache_dir.exists():
            pytest.skip("Cache not generated by fixture")

        # Delete entire cache directory
        shutil.rmtree(cache_dir)

        # Should recreate cache
        convert_jsonl_to_html(project)

        assert cache_dir.exists()
        assert (cache_dir / "index.json").exists()

    def test_empty_project_directory_handled(self, temp_projects_copy: Path) -> None:
        """Empty project directory (no JSONL files) handled gracefully."""
        empty_project = temp_projects_copy / "-Users-test-empty-project"
        empty_project.mkdir()

        # Should not crash
        runner = CliRunner()
        result = runner.invoke(
            main, ["--all-projects", "--projects-dir", str(temp_projects_copy)]
        )

        assert result.exit_code == 0


@pytest.mark.integration
class TestFileDeletionScenarios:
    """Test handling of deleted JSONL files."""

    def test_deleted_file_processing_continues(self, projects_with_cache: Path) -> None:
        """Deleting a JSONL file doesn't crash processing."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Get list of JSONL files
        jsonl_files = [
            f for f in project.glob("*.jsonl") if not f.name.startswith("agent-")
        ]
        if len(jsonl_files) < 2:
            pytest.skip("Need multiple files for deletion test")

        # Delete one file
        deleted_file = jsonl_files[0]
        deleted_file.unlink()

        time.sleep(0.01)

        # Processing should still work
        convert_jsonl_to_html(project)

        # HTML should exist
        assert (project / "combined_transcripts.html").exists()

    def test_delete_all_jsonl_files(self, temp_projects_copy: Path) -> None:
        """Deleting all JSONL files from project is handled gracefully."""
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Process first to create cache/HTML
        convert_jsonl_to_html(project)

        # Delete all JSONL files
        for f in project.glob("*.jsonl"):
            f.unlink()

        # Should handle empty project without crash
        convert_jsonl_to_html(project)
        # Combined transcript should exist (may be empty)
        assert (project / "combined_transcripts.html").exists()

    def test_project_with_only_empty_files_after_deletion(
        self, temp_projects_copy: Path
    ) -> None:
        """Project with only empty JSONL files after deletion is handled."""
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Delete all files and create empty ones
        for f in project.glob("*.jsonl"):
            f.unlink()

        # Create empty JSONL file
        (project / "empty.jsonl").touch()

        # Should handle gracefully
        convert_jsonl_to_html(project)
        assert (project / "combined_transcripts.html").exists()


@pytest.mark.integration
class TestNoCacheFlag:
    """Test --no-cache flag disables caching."""

    def test_no_cache_flag_prevents_cache_creation(
        self, temp_projects_copy: Path
    ) -> None:
        """--no-cache flag prevents cache directory creation."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Ensure no cache exists
        cache_dir = project / "cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

        result = runner.invoke(main, [str(project), "--no-cache"])

        assert result.exit_code == 0
        assert (project / "combined_transcripts.html").exists()
        # Cache should NOT be created
        assert not cache_dir.exists()

    def test_no_cache_with_all_projects(self, temp_projects_copy: Path) -> None:
        """--no-cache works with --all-projects."""
        runner = CliRunner()

        # Ensure no caches exist
        for project in temp_projects_copy.iterdir():
            if project.is_dir():
                cache_dir = project / "cache"
                if cache_dir.exists():
                    shutil.rmtree(cache_dir)

        result = runner.invoke(
            main,
            ["--all-projects", "--projects-dir", str(temp_projects_copy), "--no-cache"],
        )

        assert result.exit_code == 0
        assert (temp_projects_copy / "index.html").exists()

        # Verify no cache directories created
        for project in temp_projects_copy.iterdir():
            if project.is_dir():
                assert not (project / "cache").exists()


@pytest.mark.integration
class TestSessionHTMLSelectiveRegeneration:
    """Test selective regeneration of individual session HTML files."""

    def test_deleted_session_html_regenerated(self, projects_with_cache: Path) -> None:
        """Deleted session HTML file is regenerated."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        session_files = list(project.glob("session-*.html"))
        if not session_files:
            pytest.skip("No session files to delete")

        # Delete one session file
        deleted_file = session_files[0]
        deleted_name = deleted_file.name
        deleted_file.unlink()

        convert_jsonl_to_html(project)

        # Session file should be regenerated
        assert (project / deleted_name).exists()

    def test_session_html_version_mismatch_regenerated(
        self, projects_with_cache: Path
    ) -> None:
        """Session HTML with outdated version is regenerated."""
        project = projects_with_cache / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        session_files = list(project.glob("session-*.html"))
        if not session_files:
            pytest.skip("No session files available")

        session_file = session_files[0]

        # Corrupt version in session file
        content = session_file.read_text(encoding="utf-8")
        modified = content.replace(
            f"<!-- Generated by claude-code-log v{get_library_version()} -->",
            "<!-- Generated by claude-code-log v0.0.0-old -->",
        )
        session_file.write_text(modified, encoding="utf-8")

        convert_jsonl_to_html(project)

        # Session file should be regenerated
        new_content = session_file.read_text(encoding="utf-8")
        assert f"v{get_library_version()}" in new_content


@pytest.mark.integration
class TestCombinedFlagScenarios:
    """Test combinations of CLI flags."""

    def test_date_filter_with_no_individual_sessions(
        self, temp_projects_copy: Path
    ) -> None:
        """--from-date with --no-individual-sessions."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        result = runner.invoke(
            main,
            [str(project), "--from-date", "2024-01-01", "--no-individual-sessions"],
        )

        assert result.exit_code == 0
        assert (project / "combined_transcripts.html").exists()
        assert len(list(project.glob("session-*.html"))) == 0

    def test_output_with_date_filter(self, temp_projects_copy: Path) -> None:
        """--output with --from-date."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        custom_output = temp_projects_copy / "filtered_output.html"

        result = runner.invoke(
            main,
            [str(project), "-o", str(custom_output), "--from-date", "2024-06-01"],
        )

        assert result.exit_code == 0
        assert custom_output.exists()

    def test_no_cache_with_date_filter(self, temp_projects_copy: Path) -> None:
        """--no-cache with --from-date."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        result = runner.invoke(
            main, [str(project), "--no-cache", "--from-date", "2024-01-01"]
        )

        assert result.exit_code == 0
        assert (project / "combined_transcripts.html").exists()
        assert not (project / "cache").exists()

    def test_all_flags_combined(self, temp_projects_copy: Path) -> None:
        """Multiple flags together."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        custom_output = temp_projects_copy / "all_flags.html"

        result = runner.invoke(
            main,
            [
                str(project),
                "-o",
                str(custom_output),
                "--no-individual-sessions",
                "--from-date",
                "2024-01-01",
                "--to-date",
                "2025-12-31",
            ],
        )

        assert result.exit_code == 0
        assert custom_output.exists()
        assert len(list(project.glob("session-*.html"))) == 0


@pytest.mark.integration
class TestLargeProjectHandling:
    """Test handling of projects with many files or large files."""

    def test_project_with_many_sessions(self, temp_projects_copy: Path) -> None:
        """Process project with many JSONL files (simulated)."""
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Create 20 additional small JSONL files
        for i in range(20):
            session_file = project / f"stress-test-{i}.jsonl"
            entry = make_valid_user_entry(
                content=f"Stress test message {i}",
                session_id=f"stress-{i}",
                timestamp=f"2024-12-{10 + i % 20:02d}T10:00:00Z",
            )
            session_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Should handle many files without error
        convert_jsonl_to_html(project)

        assert (project / "combined_transcripts.html").exists()

    def test_large_single_session(self, temp_projects_copy: Path) -> None:
        """Process large single session file."""
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        large_file = project / "large-session.jsonl"

        # Create file with 500 messages
        with open(large_file, "w", encoding="utf-8") as f:
            for i in range(500):
                timestamp = f"2024-12-15T{10 + (i // 60):02d}:{i % 60:02d}:00Z"
                content = f"Message number {i} with some content"
                if i % 2 == 0:
                    entry = make_valid_user_entry(
                        content=content,
                        session_id="large-session",
                        timestamp=timestamp,
                    )
                else:
                    entry = make_valid_assistant_entry(
                        content=content,
                        session_id="large-session",
                        timestamp=timestamp,
                    )
                f.write(json.dumps(entry) + "\n")

        convert_jsonl_to_html(project)

        assert (project / "combined_transcripts.html").exists()
        # HTML should contain all messages
        html_content = (project / "combined_transcripts.html").read_text(
            encoding="utf-8"
        )
        assert "Message number 499" in html_content


@pytest.mark.integration
@pytest.mark.parametrize("output_format,ext", [("html", "html"), ("md", "md")])
class TestMultiFormatOutput:
    """Test output generation in both HTML and Markdown formats.

    These tests verify that --format option works correctly for the full
    projects hierarchy processing, generating the appropriate output files.
    """

    def test_process_all_projects_creates_index(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Process all projects and verify index generation in specified format."""
        index_path = process_projects_hierarchy(
            temp_projects_copy, output_format=output_format
        )

        assert index_path.exists()
        assert index_path.name == f"index.{ext}"

        # Verify each project got a combined transcript in the right format
        for project_dir in temp_projects_copy.iterdir():
            if project_dir.is_dir() and list(project_dir.glob("*.jsonl")):
                combined = project_dir / f"combined_transcripts.{ext}"
                assert combined.exists(), (
                    f"Missing combined {ext.upper()} for {project_dir.name}"
                )

    def test_cli_all_projects_with_format(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Test --all-projects with --format option."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--format",
                output_format,
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Successfully processed" in result.output
        assert (temp_projects_copy / f"index.{ext}").exists()

    def test_index_contains_correct_links(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Verify index contains links to correct file extensions."""
        process_projects_hierarchy(temp_projects_copy, output_format=output_format)

        index_path = temp_projects_copy / f"index.{ext}"
        content = index_path.read_text(encoding="utf-8")

        # Index should link to combined_transcripts with matching extension
        assert f"combined_transcripts.{ext}" in content

    def test_individual_session_files_format(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Verify individual session files are created with correct extension."""
        process_projects_hierarchy(temp_projects_copy, output_format=output_format)

        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        # Should have session files in the right format
        session_files = list(project.glob(f"session-*.{ext}"))
        # Count non-empty, non-agent JSONL files to compare
        non_empty_jsonl = [
            f
            for f in project.glob("*.jsonl")
            if f.stat().st_size > 0 and not f.name.startswith("agent-")
        ]

        if non_empty_jsonl:
            assert len(session_files) > 0, (
                f"Expected individual session {ext.upper()} files"
            )

    def test_single_project_with_format(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Test single project conversion with format option."""
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        output_path = convert_jsonl_to(output_format, project)

        assert output_path.exists()
        assert output_path.name == f"combined_transcripts.{ext}"

    def test_cli_single_project_with_format(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Test CLI with format option for single project."""
        runner = CliRunner()
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if not project.exists():
            pytest.skip("JSSoundRecorder test data not available")

        result = runner.invoke(main, [str(project), "--format", output_format])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert (project / f"combined_transcripts.{ext}").exists()

    def test_version_comment_in_output(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Verify version comment is present in output files."""
        process_projects_hierarchy(temp_projects_copy, output_format=output_format)

        index_path = temp_projects_copy / f"index.{ext}"
        content = index_path.read_text(encoding="utf-8")

        assert (
            f"<!-- Generated by claude-code-log v{get_library_version()} -->" in content
        )

    def test_format_with_date_filtering(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Test format option works with date filtering."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--format",
                output_format,
                "--from-date",
                "2024-01-01",
            ],
        )

        assert result.exit_code == 0
        assert (temp_projects_copy / f"index.{ext}").exists()

    def test_format_with_no_individual_sessions(
        self, temp_projects_copy: Path, output_format: str, ext: str
    ) -> None:
        """Test format option works with --no-individual-sessions."""
        runner = CliRunner()

        # Clean any pre-existing session files
        for project in temp_projects_copy.iterdir():
            if project.is_dir():
                for session_file in project.glob(f"session-*.{ext}"):
                    session_file.unlink()

        result = runner.invoke(
            main,
            [
                "--all-projects",
                "--projects-dir",
                str(temp_projects_copy),
                "--format",
                output_format,
                "--no-individual-sessions",
            ],
        )

        assert result.exit_code == 0
        assert (temp_projects_copy / f"index.{ext}").exists()

        # Verify no session files were created
        project = temp_projects_copy / "-Users-dain-workspace-JSSoundRecorder"
        if project.exists():
            session_files = list(project.glob(f"session-*.{ext}"))
            assert len(session_files) == 0


@pytest.mark.integration
class TestExperimentsWorktreesTeammates:
    """End-to-end integration tests against the real teammates fixture.

    The ``-experiments-worktrees`` project (session ``ef958aa1-…``) is a
    real Claude Code transcript that drove the post-merge refactor: it
    has two waves of three sequentially-spawned subagents (alice / bob /
    carol round 1, then round 2), each linking via the structured
    ``toolUseResult.agentId`` path. It exercises:

    - ``Agent`` tool aliasing (the spawn tool is emitted under
      ``Agent``, not ``Task`` — see ``tool_factory`` registration).
    - ``_relocate_subagent_blocks`` — six subagent threads must each
      land under their respective trunk anchor, not collapse under the
      last one.
    - per-anchor ``_cleanup_sidechain_duplicates`` — first-User and
      last-Sub-assistant dups elided for ALL six agents.
    - dropped subagent ceremony — no ``<details
      class="subagent-session-block">`` wrap, no
      ``session-teammate-badge`` pill, no empty session-header card at
      the document tail.
    - compact tool-card titles for TaskCreate / TaskUpdate / SendMessage.
    """

    PROJECT_NAME = "-experiments-worktrees"
    SESSION_ID = "ef958aa1-0e93-49f0-bb71-150f63befcd1"

    def _project_dir(self, projects: Path) -> Path:
        project = projects / self.PROJECT_NAME
        if not project.exists():
            pytest.skip(f"{self.PROJECT_NAME} test data not available")
        return project

    def test_directory_loader_links_all_six_subagents(
        self, temp_projects_copy: Path
    ) -> None:
        """``load_directory_transcripts`` should pick up every
        ``subagents/agent-*.jsonl`` and integrate them via the primary
        agentId path."""
        from claude_code_log.converter import load_directory_transcripts

        project = self._project_dir(temp_projects_copy)
        subagents_dir = project / self.SESSION_ID / "subagents"
        agent_files = sorted(p.stem for p in subagents_dir.glob("agent-*.jsonl"))
        # Wave 1: three agents (alice/bob/carol). Wave 2: three more.
        assert len(agent_files) == 6, f"expected 6 agent files, got {agent_files}"

        messages, _tree = load_directory_transcripts(project, silent=True)

        # Each subagent's UUIDs should be present in the loaded message
        # set, with the synthetic `{trunk}#agent-<id>` sessionId stamped
        # on by `_integrate_agent_entries`.
        agent_ids = {name.removeprefix("agent-") for name in agent_files}
        seen_subagent_sids: set[str] = set()
        for msg in messages:
            sid = getattr(msg, "sessionId", "") or ""
            if "#agent-" in sid:
                seen_subagent_sids.add(sid.rsplit("#agent-", 1)[-1])
        assert seen_subagent_sids == agent_ids, (
            f"missing subagent stamps: {agent_ids - seen_subagent_sids}"
        )

    def test_combined_html_renders_without_errors(
        self, temp_projects_copy: Path
    ) -> None:
        """Smoke test — the combined transcript HTML is produced and
        non-trivial."""
        project = self._project_dir(temp_projects_copy)
        convert_jsonl_to_html(project)

        html_path = project / "combined_transcripts.html"
        assert html_path.exists()
        # The fixture is ~300 KB of JSONL; the rendered HTML should be
        # substantial (six subagents inline + trunk content).
        assert html_path.stat().st_size > 200_000

    def test_subagents_relocated_under_their_anchors(
        self, temp_projects_copy: Path
    ) -> None:
        """Each subagent's sidechain content must nest under its OWN
        Task/Agent tool_result, not collapse under whichever rendered
        last. Pre-relocation, all sidechain ``ancestry`` ended in the
        same tool_result d-id; post-relocation each agent has a distinct
        anchor.
        """
        import re

        project = self._project_dir(temp_projects_copy)
        convert_jsonl_to_html(project)
        html = (project / "combined_transcripts.html").read_text(encoding="utf-8")

        # Every `assistant sidechain` row carries an ancestry tail like
        # `d-0 d-1 d-26 d-<anchor>`. Collect the immediate parent (last
        # d-id before message_index) — there should be at least 6
        # distinct values, one per subagent. Pre-fix the count was 1.
        pattern = re.compile(
            r"message assistant sidechain ((?:d-\d+ ?)+)' data-message-id"
        )
        anchors: set[str] = set()
        for match in pattern.finditer(html):
            ancestry = match.group(1).strip().split()
            anchors.add(ancestry[-1])  # immediate parent
        assert len(anchors) >= 6, (
            f"expected ≥6 distinct anchors for the 6 subagents, got {anchors}"
        )

    def test_no_subagent_collapse_markup(self, temp_projects_copy: Path) -> None:
        """The dropped ``<details class="subagent-session-block">`` wrap
        and ``session-teammate-badge`` pill must not appear (commit
        ``27e43fb`` removed them)."""
        project = self._project_dir(temp_projects_copy)
        convert_jsonl_to_html(project)
        html = (project / "combined_transcripts.html").read_text(encoding="utf-8")

        assert "subagent-session-block" not in html
        assert "session-teammate-badge" not in html

    def test_no_orphan_subagent_session_headers(self, temp_projects_copy: Path) -> None:
        """``_render_messages`` no longer creates standalone session
        headers for synthetic ``#agent-…`` sessions (commit ``fdd28ec``).
        The synthetic id should not appear as a ``data-session-id`` on
        any rendered session-header div."""
        project = self._project_dir(temp_projects_copy)
        convert_jsonl_to_html(project)
        html = (project / "combined_transcripts.html").read_text(encoding="utf-8")

        assert "data-session-id='" + self.SESSION_ID + "#agent-" not in html
        assert 'data-session-id="' + self.SESSION_ID + "#agent-" not in html

    def test_compact_tool_titles(self, temp_projects_copy: Path) -> None:
        """TaskCreate / TaskUpdate / SendMessage titles are compacted
        (commits ``7c364bc`` and ``47bc50e``)."""
        project = self._project_dir(temp_projects_copy)
        convert_jsonl_to_html(project)
        html = (project / "combined_transcripts.html").read_text(encoding="utf-8")

        # TaskCreate compaction → `Task <code>#N</code> ... [created]`
        assert "task-action'>[created]" in html
        # TaskUpdate compaction → same shape with [updated]
        assert "task-action'>[updated]" in html
        # SendMessage gets the ✉️ emoji + "to <badge>" inline title
        assert "✉️ SendMessage" in html

    def test_low_detail_preserves_agent_spawns(self, temp_projects_copy: Path) -> None:
        """``--detail low`` must keep ``Agent`` tool_use/tool_result
        pairs visible (commit ``dc134bf``).

        Real teammate transcripts emit the spawn under the ``Agent``
        tool name, not ``Task``. ``Agent`` was missing from
        ``_LOW_KEEP_TOOLS``, so every teammate spawn-and-result pair
        silently dropped out of low-detail rendering.

        The fixture has six ``Agent`` spawns total (wave-1 + wave-2
        alice/bob/carol). All six must survive at LOW detail — the
        nested-spawn assistant entries are surfaced via
        ``_collect_agent_anchors`` so the DAG walker doesn't drop
        them when the parent fork lands on the dead-end side.
        """
        from claude_code_log.models import DetailLevel
        from claude_code_log.converter import convert_jsonl_to

        project = self._project_dir(temp_projects_copy)
        out_path = convert_jsonl_to(
            "html", project, silent=True, detail=DetailLevel.LOW
        )
        html = out_path.read_text(encoding="utf-8")

        # Title formatter for TaskInput emits "🔧 Agent <description>"
        # since Agent is aliased to TaskInput in the tool factory.
        agent_titles = html.count("🔧 Agent ")
        assert agent_titles >= 6, (
            f"expected ≥6 Agent tool_use titles at --detail low, got {agent_titles}"
        )

        # Pair_last tool_result counterparts must also be there
        # (one ``teammate-spawn-card`` per Agent tool_result).
        agent_results = html.count("teammate-tool-card teammate-spawn-card")
        assert agent_results >= 6, (
            f"expected ≥6 Agent tool_result cards at --detail low, got {agent_results}"
        )

        # Sanity: TaskCreate / TaskUpdate (not in ``_LOW_KEEP_TOOLS``)
        # are stripped — their compact ``[created]`` / ``[updated]``
        # action tag is a unique marker.
        assert "task-action'>[created]" not in html
        assert "task-action'>[updated]" not in html

    def test_minimal_detail_strips_agent_spawns(self, temp_projects_copy: Path) -> None:
        """``--detail minimal`` strips all tool content — ``Agent``
        included. Only user/assistant text survives, so the spawn
        titles and result cards must be absent.

        Pairs with ``test_low_detail_preserves_agent_spawns`` to lock
        in both halves of the contract: LOW keeps the Agent pair, but
        MINIMAL goes further and strips every tool.
        """
        from claude_code_log.models import DetailLevel
        from claude_code_log.converter import convert_jsonl_to

        project = self._project_dir(temp_projects_copy)
        out_path = convert_jsonl_to(
            "html", project, silent=True, detail=DetailLevel.MINIMAL
        )
        html = out_path.read_text(encoding="utf-8")

        # No Agent tool_use titles, no Agent tool_result cards.
        assert "🔧 Agent " not in html
        assert "teammate-tool-card teammate-spawn-card" not in html
        # Web tools are kept at LOW but not at MINIMAL — verify the
        # filter actually escalates between levels.
        assert "🔎 WebSearch" not in html
        assert "🌐 WebFetch" not in html

    def test_per_session_export_includes_subagent_content(
        self, temp_projects_copy: Path
    ) -> None:
        """Per-session HTML/Markdown exports must include subagent
        sidechain content even though ``_integrate_agent_entries``
        rewrote those entries' ``sessionId`` to
        ``{trunk}#agent-{agent_id}``.

        Regression for CodeRabbit's "Synthetic sidechain IDs now drop
        subagents from per-session exports" finding on PR #125. The
        ``generate_session`` filter in both renderers used to do an
        exact ``sessionId == session_id`` match, dropping the
        rewritten subagent entries. The fix accepts the synthetic
        prefix too.
        """
        project = self._project_dir(temp_projects_copy)
        convert_jsonl_to_html(project)

        combined = (project / "combined_transcripts.html").read_text(encoding="utf-8")
        per_session = (project / f"session-{self.SESSION_ID}.html").read_text(
            encoding="utf-8"
        )

        # Subagent inlined content must reach the per-session export at
        # parity with the combined transcript.
        combined_subasst = combined.count("Sub-assistant")
        session_subasst = per_session.count("Sub-assistant")
        assert session_subasst >= combined_subasst, (
            f"per-session export lost subagent content: "
            f"{session_subasst} vs combined {combined_subasst}"
        )

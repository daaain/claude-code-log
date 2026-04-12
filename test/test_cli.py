#!/usr/bin/env python3
"""Tests for CLI functionality and helper functions."""

import json
from pathlib import Path
from typing import Generator

import pytest
from click.testing import CliRunner

from claude_code_log.cli import (
    _clear_caches,
    _clear_output_files,
    _discover_projects,
    get_default_projects_dir,
    main,
)
from claude_code_log.cache import CacheManager


class ProjectsSetup:
    """Container for test projects setup."""

    def __init__(self, projects_dir: Path, db_path: Path):
        self.projects_dir = projects_dir
        self.db_path = db_path


@pytest.fixture
def cli_projects_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[ProjectsSetup, None, None]:
    """Create isolated projects setup for CLI tests."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    # Set env var to isolate cache
    isolated_db = tmp_path / "test-cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(isolated_db))

    yield ProjectsSetup(projects_dir, isolated_db)


@pytest.fixture
def sample_jsonl_content() -> list[dict]:
    """Sample JSONL data for tests."""
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
            "message": {"role": "user", "content": "Hello"},
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
                "content": [{"type": "text", "text": "Hi there!"}],
                "usage": {"input_tokens": 10, "output_tokens": 15},
            },
        },
        {"type": "summary", "summary": "A greeting", "leafUuid": "assistant-1"},
    ]


def create_project_with_jsonl(
    projects_dir: Path, name: str, jsonl_data: list[dict]
) -> Path:
    """Helper to create a project directory with JSONL file."""
    project_dir = projects_dir / name
    project_dir.mkdir(exist_ok=True)
    jsonl_file = project_dir / "session-1.jsonl"
    with open(jsonl_file, "w") as f:
        for entry in jsonl_data:
            f.write(json.dumps(entry) + "\n")
    return project_dir


class TestGetDefaultProjectsDir:
    """Tests for get_default_projects_dir helper."""

    def test_returns_expected_path(self):
        """Default projects dir is ~/.claude/projects."""
        result = get_default_projects_dir()
        assert result == Path.home() / ".claude" / "projects"


class TestDiscoverProjects:
    """Tests for _discover_projects helper."""

    def test_discovers_active_projects(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Finds directories with JSONL files."""
        projects_dir = cli_projects_setup.projects_dir

        # Create two active projects
        create_project_with_jsonl(projects_dir, "project-1", sample_jsonl_content)
        create_project_with_jsonl(projects_dir, "project-2", sample_jsonl_content)

        # Create an empty directory (not a project)
        (projects_dir / "empty-dir").mkdir()

        project_dirs, archived = _discover_projects(projects_dir)

        assert len(project_dirs) == 2
        assert len(archived) == 0
        project_names = {p.name for p in project_dirs}
        assert project_names == {"project-1", "project-2"}

    def test_discovers_archived_projects(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Finds archived projects from cache."""
        projects_dir = cli_projects_setup.projects_dir
        db_path = cli_projects_setup.db_path

        # Create a project and cache it
        project_dir = create_project_with_jsonl(
            projects_dir, "my-project", sample_jsonl_content
        )
        cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)

        # Save entries to cache
        from claude_code_log.converter import load_transcript

        jsonl_file = project_dir / "session-1.jsonl"
        entries = load_transcript(jsonl_file, silent=True)
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Delete the JSONL file to simulate archival
        jsonl_file.unlink()

        project_dirs, archived = _discover_projects(projects_dir)

        assert len(project_dirs) == 1
        assert len(archived) == 1
        assert project_dir in archived

    def test_empty_directory(self, cli_projects_setup: ProjectsSetup):
        """Empty projects directory returns empty lists."""
        project_dirs, archived = _discover_projects(cli_projects_setup.projects_dir)
        assert project_dirs == []
        assert archived == set()


class TestClearCaches:
    """Tests for _clear_caches helper."""

    def test_clear_cache_single_project(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Clears cache for a single project."""
        projects_dir = cli_projects_setup.projects_dir
        db_path = cli_projects_setup.db_path

        project_dir = create_project_with_jsonl(
            projects_dir, "test-project", sample_jsonl_content
        )

        # Create cache
        cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
        from claude_code_log.converter import load_transcript

        jsonl_file = project_dir / "session-1.jsonl"
        entries = load_transcript(jsonl_file, silent=True)
        cache_manager.save_cached_entries(jsonl_file, entries)

        # Verify cache has data
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert len(cached_data.cached_files) >= 1

        # Clear cache
        _clear_caches(project_dir, all_projects=False)

        # Verify cache is cleared
        cache_manager2 = CacheManager(project_dir, "1.0.0", db_path=db_path)
        cached_data2 = cache_manager2.get_cached_project_data()
        assert cached_data2 is not None
        assert len(cached_data2.cached_files) == 0

    def test_clear_cache_all_projects(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Clears cache database for all projects."""
        projects_dir = cli_projects_setup.projects_dir
        db_path = cli_projects_setup.db_path

        # Create multiple projects
        for i in range(3):
            create_project_with_jsonl(
                projects_dir, f"project-{i}", sample_jsonl_content
            )

        # Create cache entries
        for i in range(3):
            project_dir = projects_dir / f"project-{i}"
            cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
            from claude_code_log.converter import load_transcript

            jsonl_file = project_dir / "session-1.jsonl"
            entries = load_transcript(jsonl_file, silent=True)
            cache_manager.save_cached_entries(jsonl_file, entries)

        # Verify cache exists
        assert db_path.exists()

        # Clear all caches
        _clear_caches(projects_dir, all_projects=True)

        # Database file should be deleted
        assert not db_path.exists()

    def test_clear_cache_single_file_noop(self, tmp_path: Path):
        """Clearing cache for single file has no effect."""
        # Create a single JSONL file (not in a project structure)
        jsonl_file = tmp_path / "test.jsonl"
        jsonl_file.write_text('{"type": "user"}')

        # Should complete without error
        _clear_caches(jsonl_file, all_projects=False)


class TestClearOutputFiles:
    """Tests for _clear_output_files helper."""

    def test_clear_html_single_project(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Clears HTML files from single project."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        # Create some HTML files
        (project_dir / "combined_transcripts.html").write_text("<html></html>")
        (project_dir / "session-1.html").write_text("<html></html>")

        assert len(list(project_dir.glob("*.html"))) == 2

        _clear_output_files(project_dir, all_projects=False, file_ext="html")

        assert len(list(project_dir.glob("*.html"))) == 0

    def test_clear_html_all_projects(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Clears HTML files from all projects."""
        projects_dir = cli_projects_setup.projects_dir

        # Create projects with HTML files
        for i in range(2):
            project_dir = create_project_with_jsonl(
                projects_dir, f"project-{i}", sample_jsonl_content
            )
            (project_dir / "combined_transcripts.html").write_text("<html></html>")

        # Create index file
        (projects_dir / "index.html").write_text("<html></html>")

        _clear_output_files(projects_dir, all_projects=True, file_ext="html")

        # All HTML files should be gone
        assert not (projects_dir / "index.html").exists()
        for i in range(2):
            project_dir = projects_dir / f"project-{i}"
            assert len(list(project_dir.glob("*.html"))) == 0

    def test_clear_md_files(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Clears Markdown files."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        (project_dir / "combined_transcripts.md").write_text("# Test")
        assert len(list(project_dir.glob("*.md"))) == 1

        _clear_output_files(project_dir, all_projects=False, file_ext="md")

        assert len(list(project_dir.glob("*.md"))) == 0

    def test_clear_no_files_to_remove(self, cli_projects_setup: ProjectsSetup):
        """No error when no files to remove."""
        project_dir = cli_projects_setup.projects_dir / "empty-project"
        project_dir.mkdir()
        (project_dir / "test.jsonl").write_text('{"type": "user"}')

        # Should complete without error
        _clear_output_files(project_dir, all_projects=False, file_ext="html")


class TestCLIMainCommand:
    """Tests for main CLI command."""

    def test_help_shows_options(self):
        """Help shows all expected options."""
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0
        assert "--output" in result.output
        assert "--all-projects" in result.output
        assert "--clear-cache" in result.output
        assert "--open-browser" in result.output

    def test_no_arguments_uses_default_or_cwd(self, monkeypatch: pytest.MonkeyPatch):
        """Running without arguments attempts to find projects."""
        runner = CliRunner()
        # Mock to avoid actual file system operations
        result = runner.invoke(main, [])
        # Should either succeed or fail gracefully (no crash)
        assert result.exit_code in (0, 1)

    def test_clear_cache_flag(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--clear-cache flag clears cache."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        runner = CliRunner()

        # First run to create cache
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0

        # Clear cache
        result2 = runner.invoke(main, [str(project_dir), "--clear-cache"])
        assert result2.exit_code == 0
        assert "clearing" in result2.output.lower() or "clear" in result2.output.lower()

    def test_clear_html_flag(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--clear-html flag clears HTML files."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        runner = CliRunner()

        # Generate HTML
        result1 = runner.invoke(main, [str(project_dir)])
        assert result1.exit_code == 0
        assert len(list(project_dir.glob("*.html"))) > 0

        # Clear HTML
        result2 = runner.invoke(main, [str(project_dir), "--clear-html"])
        assert result2.exit_code == 0
        assert len(list(project_dir.glob("*.html"))) == 0

    def test_format_option_md(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--format md generates Markdown output."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir), "--format", "md"])

        assert result.exit_code == 0
        assert len(list(project_dir.glob("*.md"))) > 0

    def test_no_cache_flag(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--no-cache flag processes without caching."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )
        db_path = cli_projects_setup.db_path

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir), "--no-cache"])

        assert result.exit_code == 0

        # Cache should exist but be empty for this project
        cache_manager = CacheManager(project_dir, "1.0.0", db_path=db_path)
        cached_data = cache_manager.get_cached_project_data()
        assert cached_data is not None
        assert cached_data.total_message_count == 0

    def test_nonexistent_path_error(self):
        """Nonexistent path shows appropriate error."""
        runner = CliRunner()
        result = runner.invoke(main, ["/nonexistent/path/to/file.jsonl"])

        # Should fail gracefully
        assert result.exit_code != 0 or "error" in result.output.lower()

    def test_output_option(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--output option specifies output path."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )
        output_path = cli_projects_setup.projects_dir / "custom_output.html"

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir), "--output", str(output_path)])

        assert result.exit_code == 0
        assert output_path.exists()


class TestSessionIdOption:
    """Tests for --session-id CLI option."""

    def test_session_id_no_path_not_in_cache_errors(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """--session-id without path and no cache match exits with error."""
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))
        runner = CliRunner()
        result = runner.invoke(main, ["--session-id", "abc12345"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_session_id_no_path_global_lookup(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--session-id without path finds session via cache global lookup."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "my-project", sample_jsonl_content
        )
        runner = CliRunner()
        # First, generate with path to populate cache
        result = runner.invoke(main, [str(project_dir), "--session-id", "session-1"])
        assert result.exit_code == 0

        # Now use --session-id without path, relying on global cache lookup
        result = runner.invoke(
            main,
            [
                "--session-id",
                "session-1",
                "--projects-dir",
                str(cli_projects_setup.projects_dir),
            ],
        )
        assert result.exit_code == 0
        assert "Successfully exported session" in result.output

    def test_session_id_valid_full_id(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--session-id with a valid full session ID generates file and prints success."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "my-project", sample_jsonl_content
        )
        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir), "--session-id", "session-1"])
        assert result.exit_code == 0
        assert "Successfully exported session" in result.output
        # Generated file must exist
        assert len(list(project_dir.glob("session-session-1.*"))) > 0

    def test_session_id_prefix(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--session-id with a unique prefix resolves to the full ID."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "my-project", sample_jsonl_content
        )
        runner = CliRunner()
        # "sess" is a valid prefix of "session-1"
        result = runner.invoke(main, [str(project_dir), "--session-id", "sess"])
        assert result.exit_code == 0
        assert "Successfully exported session" in result.output

    def test_session_id_unknown_exits_nonzero(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--session-id with an unknown ID exits non-zero."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "my-project", sample_jsonl_content
        )
        runner = CliRunner()
        result = runner.invoke(
            main, [str(project_dir), "--session-id", "zzzzzzzz-does-not-exist"]
        )
        assert result.exit_code != 0

    def test_session_id_with_output_flag(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """--session-id combined with --output writes to the specified path."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "my-project", sample_jsonl_content
        )
        output_path = cli_projects_setup.projects_dir / "custom-session.html"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                str(project_dir),
                "--session-id",
                "session-1",
                "--output",
                str(output_path),
            ],
        )
        assert result.exit_code == 0
        assert output_path.exists()


class TestCLIErrorHandling:
    """Tests for CLI error handling paths."""

    def test_invalid_format_option(
        self, cli_projects_setup: ProjectsSetup, sample_jsonl_content: list[dict]
    ):
        """Invalid format option shows error."""
        project_dir = create_project_with_jsonl(
            cli_projects_setup.projects_dir, "test-project", sample_jsonl_content
        )

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir), "--format", "invalid"])

        assert result.exit_code != 0

    def test_empty_project_directory(self, cli_projects_setup: ProjectsSetup):
        """Empty project directory handled gracefully."""
        project_dir = cli_projects_setup.projects_dir / "empty-project"
        project_dir.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir)])

        # Should complete (possibly with warning)
        assert result.exit_code == 0

    def test_malformed_jsonl_handled(self, cli_projects_setup: ProjectsSetup):
        """Malformed JSONL handled gracefully."""
        project_dir = cli_projects_setup.projects_dir / "bad-project"
        project_dir.mkdir()
        (project_dir / "test.jsonl").write_text("not valid json\n{also: bad}")

        runner = CliRunner()
        result = runner.invoke(main, [str(project_dir)])

        # Should not crash
        assert result.exit_code in (0, 1)

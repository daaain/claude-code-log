"""The CLI refuses, once and up front, an SQLite too old for the cache.

Without the up-front check the migration runner still refused, but per
CacheManager: a pass printed the same warning twice per project and ran on
without a cache. The version is faked; only `sqlite3.sqlite_version_info` is
read by the check, and every command refused here stops before a pool worker
could start.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main

OLD = (3, 31, 1)
SAMPLE = Path(__file__).parent / "test_data" / "representative_messages.jsonl"


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "projects"
    project = root / "-tmp-demo"
    project.mkdir(parents=True)
    shutil.copy(SAMPLE, project / SAMPLE.name)
    monkeypatch.setenv("CLAUDE_CODE_LOG_RENDER_JOBS", "1")
    monkeypatch.delenv("CLAUDE_CODE_LOG_CACHE_PATH", raising=False)
    return root


def _refused(result_output: str, exit_code: int) -> None:
    assert exit_code == 1, result_output
    lines = [line for line in result_output.splitlines() if line.strip()]
    assert len(lines) == 1, result_output
    assert lines[0].startswith("Error: the cache needs SQLite 3.35.0 or later")
    assert "SQLite 3.31.1" in lines[0]


def test_convert_refuses_and_names_no_cache(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", OLD)
    result = CliRunner().invoke(
        main, ["--projects-dir", str(projects), "--all-projects"]
    )
    _refused(result.output, result.exit_code)
    assert "--no-cache" in result.output
    assert not (projects / "claude-code-log-cache.db").exists()
    assert not (projects / "index.html").exists()


def test_convert_with_no_cache_still_runs(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", OLD)
    result = CliRunner().invoke(
        main, ["--projects-dir", str(projects), "--all-projects", "--no-cache"]
    )
    assert result.exit_code == 0, result.output
    assert "SQLite" not in result.output
    assert (projects / "index.html").exists()


def test_a_single_file_needs_no_cache(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", OLD)
    jsonl = projects / "-tmp-demo" / SAMPLE.name
    result = CliRunner().invoke(main, [str(jsonl)])
    assert result.exit_code == 0, result.output
    assert jsonl.with_suffix(".html").exists()


def test_serve_refuses_and_names_no_convert(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", OLD)
    result = CliRunner().invoke(main, ["serve", "--projects-dir", str(projects)])
    _refused(result.output, result.exit_code)
    assert "--no-convert" in result.output


def test_watch_refuses(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", OLD)
    result = CliRunner().invoke(main, ["watch", str(projects / "-tmp-demo")])
    _refused(result.output, result.exit_code)

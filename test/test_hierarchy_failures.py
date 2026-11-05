"""A pass that loses projects says so, in its summary and its exit status.

A project that fails is reported as a warning and the pass goes on, which
is right for one bad project among hundreds — but the run used to exit 0
with nothing but that warning, deep in the log, to show for it, so an
unattended ``--all-projects`` export lost projects unnoticed (issue #332).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest
from click.testing import CliRunner

from claude_code_log import converter
from claude_code_log.cli import main
from claude_code_log.converter import process_projects_hierarchy

PROJECTS = ["-proj-alpha", "-proj-beta", "-proj-gamma"]
BROKEN = "-proj-beta"


def _build_projects_dir(root: Path) -> Path:
    projects_dir = root / "projects"
    for name in PROJECTS:
        project = projects_dir / name
        project.mkdir(parents=True)
        sid = f"sess{name}"
        entry = {
            "type": "user",
            "uuid": f"{sid}-u0",
            "parentUuid": None,
            "sessionId": sid,
            "timestamp": "2026-01-01T10:00:00Z",
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "version": "1.0.0",
            "message": {"role": "user", "content": f"hello {name}"},
        }
        (project / f"{sid}.jsonl").write_text(
            json.dumps(entry) + "\n", encoding="utf-8"
        )
    return projects_dir


@pytest.fixture
def projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    return _build_projects_dir(tmp_path)


def _break_one_project(monkeypatch: pytest.MonkeyPatch) -> None:
    real = converter.convert_jsonl_to

    def convert(*args: Any, **kwargs: Any) -> Any:
        if Path(kwargs["input_path"]).name == BROKEN:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(converter, "convert_jsonl_to", convert)


def test_failed_projects_are_counted_and_handed_back(
    projects_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _break_one_project(monkeypatch)
    failed: List[str] = []
    process_projects_hierarchy(projects_dir, jobs=1, failed_projects=failed)

    assert failed == [BROKEN]
    assert f"1 projects failed: {BROKEN}" in capsys.readouterr().out


def test_a_clean_pass_reports_no_failures(projects_dir: Path) -> None:
    failed: List[str] = []
    process_projects_hierarchy(projects_dir, jobs=1, failed_projects=failed)
    assert failed == []


def test_cli_exits_non_zero_when_a_project_failed(
    projects_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_one_project(monkeypatch)
    result = CliRunner().invoke(
        main, [str(projects_dir), "--all-projects", "--jobs", "1"]
    )
    assert result.exit_code == 1, result.output
    assert f"1 of {len(PROJECTS)} projects failed" in result.output
    assert BROKEN in result.output
    # The rest of the pass still ran, and the index was still written.
    assert (projects_dir / "index.html").exists()
    for name in PROJECTS:
        if name != BROKEN:
            assert (projects_dir / name / "combined_transcripts.html").exists()


def test_cli_exits_zero_on_a_clean_pass(projects_dir: Path) -> None:
    result = CliRunner().invoke(
        main, [str(projects_dir), "--all-projects", "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "Successfully processed" in result.output

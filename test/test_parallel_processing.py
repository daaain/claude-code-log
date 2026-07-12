"""Tests for parallel per-project processing in process_projects_hierarchy.

The hierarchy pass fans stale projects out over a process pool (see
`_convert_project_worker` / the plan→execute→collect phases in
converter.py). These tests pin the two properties that make that safe:

- Determinism: `jobs=2` produces byte-identical output to `jobs=1`.
- Warm-cache behaviour: a second run takes the fast path (no pool, all
  projects reported as cached) and still rewrites the index.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from claude_code_log.converter import process_projects_hierarchy

TEST_DATA_DIR = Path(__file__).parent / "test_data"

# Small-but-real transcripts; stems become session IDs, so each project
# gets distinct filenames.
SAMPLE_FILES = ["edge_cases.jsonl", "sidechain.jsonl", "edit_tool.jsonl"]


def _build_projects_dir(root: Path, name: str) -> Path:
    """Create a projects dir with three projects of 1-3 sessions each."""
    projects_dir = root / name
    for i, project in enumerate(["-proj-alpha", "-proj-beta", "-proj-gamma"]):
        project_dir = projects_dir / project
        project_dir.mkdir(parents=True)
        for j, sample in enumerate(SAMPLE_FILES[: i + 1]):
            # Unique stem per (project, file) pair — stems are session IDs.
            shutil.copy(TEST_DATA_DIR / sample, project_dir / f"session-{i}-{j}.jsonl")
    return projects_dir


def _snapshot_outputs(projects_dir: Path) -> dict[str, bytes]:
    """Relative path → content for every generated output file."""
    return {
        str(f.relative_to(projects_dir)): f.read_bytes()
        for f in sorted(projects_dir.rglob("*.html"))
    }


def test_parallel_output_matches_sequential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seq_dir = _build_projects_dir(tmp_path, "sequential")
    par_dir = _build_projects_dir(tmp_path, "parallel")

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "seq-cache.db"))
    process_projects_hierarchy(seq_dir, jobs=1)

    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "par-cache.db"))
    process_projects_hierarchy(par_dir, jobs=2)

    seq_outputs = _snapshot_outputs(seq_dir)
    par_outputs = _snapshot_outputs(par_dir)

    assert seq_outputs.keys() == par_outputs.keys()
    assert len(seq_outputs) > 3  # index + combined + per-session files
    for rel_path, seq_content in seq_outputs.items():
        assert par_outputs[rel_path] == seq_content, (
            f"{rel_path} differs between jobs=1 and jobs=2"
        )


def test_parallel_second_run_takes_cached_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    projects_dir = _build_projects_dir(tmp_path, "projects")
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    process_projects_hierarchy(projects_dir, jobs=2)
    first_run = capsys.readouterr().out
    assert "cached" not in first_run

    index_path = projects_dir / "index.html"
    assert index_path.exists()
    index_mtime = index_path.stat().st_mtime

    process_projects_hierarchy(projects_dir, jobs=2)
    second_run = capsys.readouterr().out
    # All three projects hit the fast path... (": cached" is the
    # per-project progress line; bare "cached" also appears in the
    # archived-sessions footer note)
    assert second_run.count(": cached") == 3
    # ...and the index is still (unconditionally) regenerated.
    assert index_path.stat().st_mtime >= index_mtime


def test_jobs_exceeding_project_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker count is clamped to the number of stale projects."""
    projects_dir = _build_projects_dir(tmp_path, "projects")
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    process_projects_hierarchy(projects_dir, jobs=64)
    assert (projects_dir / "index.html").exists()
    for project in ["-proj-alpha", "-proj-beta", "-proj-gamma"]:
        assert (projects_dir / project / "combined_transcripts.html").exists()

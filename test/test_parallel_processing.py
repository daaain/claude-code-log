"""Tests for parallel per-project processing in process_projects_hierarchy.

The hierarchy pass fans stale projects out over a process pool (see
`_convert_project_worker` / the plan→execute→collect phases in
converter.py). These tests pin the properties that make that safe:

- Determinism: `jobs=2` produces byte-identical output to `jobs=1`.
- Warm-cache behaviour: a second run takes the fast path (no pool, all
  projects reported as cached) and still rewrites the index.
- Worker count is clamped to the number of stale projects.
- Pool-level failure degrades to the sequential inline path.
"""

from __future__ import annotations

import os
import shutil
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pytest

from claude_code_log import converter
from claude_code_log.converter import process_projects_hierarchy

TEST_DATA_DIR = Path(__file__).parent / "test_data"

# Small-but-real transcripts; stems become session IDs, so each project
# gets distinct filenames.
SAMPLE_FILES = ["edge_cases.jsonl", "sidechain.jsonl", "edit_tool.jsonl"]

# Fixed, distinct source mtimes. The projects index orders cards by
# each project's newest source mtime (`last_modified`), so the copies
# must carry identical mtimes in every tree the tests build. Relying on
# copy-time mtimes is flaky on Windows: the filesystem clock ticks
# ~15.6ms, so back-to-back copies can tie (or not) per run, flipping
# the card order between otherwise-identical trees.
_MTIME_BASE = 1_600_000_000


def _build_projects_dir(root: Path, name: str) -> Path:
    """Create a projects dir with three projects of 1-3 sessions each."""
    projects_dir = root / name
    for i, project in enumerate(["-proj-alpha", "-proj-beta", "-proj-gamma"]):
        project_dir = projects_dir / project
        project_dir.mkdir(parents=True)
        for j, sample in enumerate(SAMPLE_FILES[: i + 1]):
            # Unique stem per (project, file) pair — stems are session IDs.
            dest = project_dir / f"session-{i}-{j}.jsonl"
            shutil.copy(TEST_DATA_DIR / sample, dest)
            mtime = _MTIME_BASE + i * 100 + j * 10
            os.utime(dest, (mtime, mtime))
    return projects_dir


def _snapshot_outputs(projects_dir: Path) -> dict[str, bytes]:
    """Relative path → content for every generated output file."""
    return {
        str(f.relative_to(projects_dir)): f.read_bytes()
        for f in sorted(projects_dir.rglob("*.html"))
    }


def _assert_all_outputs_exist(projects_dir: Path) -> None:
    assert (projects_dir / "index.html").exists()
    for project in ["-proj-alpha", "-proj-beta", "-proj-gamma"]:
        assert (projects_dir / project / "combined_transcripts.html").exists()


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
    # Sentinel: prove the second run rewrites the index (mtime
    # comparison is unreliable on coarse-granularity filesystems).
    index_path.write_text("SENTINEL — must be replaced", encoding="utf-8")

    process_projects_hierarchy(projects_dir, jobs=2)
    second_run = capsys.readouterr().out
    # All three projects hit the fast path... (": cached" is the
    # per-project progress line; bare "cached" also appears in the
    # archived-sessions footer note)
    assert second_run.count(": cached") == 3
    # ...and the index is still (unconditionally) regenerated.
    assert "SENTINEL" not in index_path.read_text(encoding="utf-8")


def test_jobs_clamped_to_stale_project_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jobs=64 with three stale projects spawns a pool of exactly 3."""
    projects_dir = _build_projects_dir(tmp_path, "projects")
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    recorded_max_workers: list[int | None] = []
    real_pool = converter.ProcessPoolExecutor

    class RecordingPool(real_pool):
        def __init__(self, max_workers: int | None = None, **kwargs: object) -> None:
            recorded_max_workers.append(max_workers)
            super().__init__(max_workers=max_workers, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(converter, "ProcessPoolExecutor", RecordingPool)

    process_projects_hierarchy(projects_dir, jobs=64)
    assert recorded_max_workers == [3]
    _assert_all_outputs_exist(projects_dir)


def test_pool_failure_falls_back_to_sequential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pool that can't start degrades to inline processing, not a crash."""
    projects_dir = _build_projects_dir(tmp_path, "projects")
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    def broken_pool(*args: object, **kwargs: object) -> None:
        raise BrokenProcessPool("simulated pool bootstrap failure")

    monkeypatch.setattr(converter, "ProcessPoolExecutor", broken_pool)

    process_projects_hierarchy(projects_dir, jobs=2)
    out = capsys.readouterr().out
    assert "falling back to sequential processing" in out
    _assert_all_outputs_exist(projects_dir)


def _plan(project_dir: Path, **overrides) -> converter._ProjectPlan:
    kwargs = dict(
        use_cache=True,
        library_version="0.0.0",
        variant="",
        combined_ext="html",
        combined_name="combined_transcripts.html",
        output_dir=None,
        expand_paths=False,
        filter_path=None,
        write_combined=True,
        page_size=0,
    )
    kwargs.update(overrides)
    plan = converter._plan_project(project_dir, **kwargs)
    assert plan is not None
    return plan


def test_plan_needs_work_without_cache_for_individual_sessions(
    tmp_path: Path,
) -> None:
    """Individual-session-only run with no cache must still be planned as work.

    With `use_cache=False` there is no cache manager, so `modified_files`
    and `stale_sessions` are always empty; treating that as "nothing to
    do" skipped every per-session file while the index still linked to
    them (issue #274).
    """
    project_dir = _build_projects_dir(tmp_path, "no-cache") / "-proj-alpha"

    plan = _plan(
        project_dir,
        use_cache=False,
        write_combined=False,
        generate_individual_sessions=True,
    )

    assert plan.needs_work is True


def test_plan_stats_without_cache_report_all_files_and_sessions(tmp_path: Path) -> None:
    """No-cache plan stats must not report cache hits or zero regenerated sessions.

    With `use_cache=False` every source file is (re)processed and every
    requested session output is (re)generated, so the summary must count all
    requested source files as processed and the corresponding sessions as
    regenerated (CodeRabbit review on #297).
    """
    project_dir = _build_projects_dir(tmp_path, "no-cache-stats") / "-proj-alpha"

    plan = _plan(
        project_dir,
        use_cache=False,
        write_combined=False,
        generate_individual_sessions=True,
    )

    assert plan.needs_work is True
    assert plan.stats.files_updated == 1
    assert plan.stats.files_loaded_from_cache == 0
    assert plan.stats.sessions_regenerated == 1


def test_no_cache_run_generates_individual_session_files(tmp_path: Path) -> None:
    """End-to-end: a no-cache individual-sessions-only run writes session files.

    Regression test for issue #274: `_generate_individual_session_files`
    intersected the trunk session IDs with the cache-derived session data,
    which is empty without a cache, so zero session files were ever written
    while the index still linked to them (404 links).
    """
    project_dir = _build_projects_dir(tmp_path, "no-cache-run") / "-proj-alpha"

    report = converter.RegenerationReport()
    converter.convert_jsonl_to(
        "html",
        project_dir,
        None,
        generate_individual_sessions=True,
        write_combined=False,
        use_cache=False,
        silent=True,
        report=report,
    )

    session_files = sorted(project_dir.glob("session-*.html"))
    assert len(session_files) > 0
    assert report.sessions_regenerated == len(session_files)


def test_plan_combined_only_run_reports_no_sessions_regenerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Combined-only plan must not query or count stale sessions.

    On a combined-only run (`generate_individual_sessions=False`) no
    per-session files are generated, so the plan must skip the
    per-session staleness query entirely and report zero regenerated
    sessions even when the cache holds stale session rows (CodeRabbit
    review on #297). Counting them would also leak into the progress
    line via `stats.sessions_regenerated`.
    """
    project_dir = _build_projects_dir(tmp_path, "combined-only") / "-proj-alpha"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    # Seed the cache with a full run.
    report = converter.RegenerationReport()
    converter.convert_jsonl_to(
        "html",
        project_dir,
        None,
        use_cache=True,
        silent=True,
        report=report,
    )

    # Make one session stale (rendered file deleted -> "file_missing")
    # and the combined output missing so the combined-only plan has
    # work to do.
    stale_session_file = next(project_dir.glob("session-*.html"))
    stale_session_file.unlink()
    (project_dir / "combined_transcripts.html").unlink()

    stale_queries: list[int] = []
    real_get_stale_sessions = converter.CacheManager.get_stale_sessions

    def spy_get_stale_sessions(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        stale_queries.append(1)
        return real_get_stale_sessions(self, *args, **kwargs)

    monkeypatch.setattr(
        converter.CacheManager, "get_stale_sessions", spy_get_stale_sessions
    )

    plan = _plan(
        project_dir,
        use_cache=True,
        library_version=converter.get_library_version(),
        write_combined=True,
        generate_individual_sessions=False,
    )

    assert plan.needs_work is True
    assert stale_queries == []
    assert plan.stats.sessions_regenerated == 0

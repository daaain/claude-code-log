"""Cache-database contention between concurrent writers (issue #332).

`serve` and `--all-projects` fan stale projects out over a process pool,
and every worker writes the same WAL-mode cache database. SQLite allows
one writer at a time, so a worker that cannot get the write lock within
its busy timeout fails with ``database is locked``. The reporter hit that
on 23 of 57 projects in one start, next to a WAL that had grown to
17.8 GB because nothing ever truncated it.

These tests reproduce the two halves without needing a slow disk or a
multi-gigabyte archive:

- **Lock-out.** A synthetic writer holds the write lock for the whole
  pool phase — the shape of "one write transaction that outlasts the
  timeout" — so every pooled project fails exactly as reported. The pass
  must still leave every project converted.
- **WAL growth.** A second connection stays open across a pass (as the
  `serve` search API's reader does), so SQLite's close-time checkpoint
  never runs. The pass must still leave the WAL truncated.

Plus the smaller points from the report: writers bound the WAL file,
entries are serialised before the write lock is taken rather than while
holding it, the search-index builder waits as long as every other cache
connection, and `serve` accepts `--jobs`.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import pytest
from click.testing import CliRunner

from claude_code_log import converter
from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.converter import load_transcript, process_projects_hierarchy
from claude_code_log.migrations.runner import WAL_SIZE_LIMIT_BYTES, cache_busy_timeout

TEST_DATA_DIR = Path(__file__).parent / "test_data"
SAMPLE_FILES = ["edge_cases.jsonl", "sidechain.jsonl", "edit_tool.jsonl"]
PROJECTS = ["-proj-alpha", "-proj-beta", "-proj-gamma"]


def _build_projects_dir(root: Path) -> Path:
    """Three projects of 1-3 small sessions each (stems are session IDs)."""
    projects_dir = root / "projects"
    for i, project in enumerate(PROJECTS):
        project_dir = projects_dir / project
        project_dir.mkdir(parents=True)
        for j, sample in enumerate(SAMPLE_FILES[: i + 1]):
            shutil.copy(TEST_DATA_DIR / sample, project_dir / f"session-{i}-{j}.jsonl")
    return projects_dir


def _make_all_stale(projects_dir: Path) -> None:
    """Grow every session by one line, so the next pass rewrites them all."""
    for path in sorted(projects_dir.glob("*/*.jsonl")):
        last = path.read_text(encoding="utf-8").splitlines()[-1]
        with path.open("a", encoding="utf-8") as f:
            f.write(last + "\n")
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 10))


class _PoolUnderHeldWriteLock(converter.ProcessPoolExecutor):
    """A process pool that runs while another connection holds the write lock.

    The lock is taken as the pool starts and released only once the pool
    has drained, so every write a pooled worker attempts meets it — the
    worst case of a long writer, made deterministic. Nothing in the parent
    writes during the pool phase, so the hold only ever blocks workers.
    """

    db_path: Path

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._holder = sqlite3.connect(self.db_path, isolation_level=None)
        self._holder.execute("BEGIN IMMEDIATE")
        super().__init__(*args, **kwargs)

    def __exit__(self, *exc: Any) -> Any:
        try:
            return super().__exit__(*exc)
        finally:
            self._holder.execute("ROLLBACK")
            self._holder.close()


def test_projects_locked_out_of_the_pool_still_convert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    projects_dir = _build_projects_dir(tmp_path)
    db_path = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    # Workers inherit the environment under `spawn`; keep the lock-out quick.
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_TIMEOUT", "0.5")
    # A pool must actually run, whatever the hold-back heuristic decides.
    monkeypatch.setattr(converter, "_holdback_plans", lambda *a, **k: [])

    # Create the database (and each project's row) first, so the planning
    # phase of the pass under test has nothing to wait for.
    process_projects_hierarchy(projects_dir, jobs=1)
    _make_all_stale(projects_dir)
    for page in projects_dir.glob("*/combined_transcripts.html"):
        page.unlink()
    capsys.readouterr()

    monkeypatch.setattr(_PoolUnderHeldWriteLock, "db_path", db_path, raising=False)
    monkeypatch.setattr(converter, "ProcessPoolExecutor", _PoolUnderHeldWriteLock)
    process_projects_hierarchy(projects_dir, jobs=3)
    out = capsys.readouterr().out

    missing = [
        p
        for p in PROJECTS
        if not (projects_dir / p / "combined_transcripts.html").exists()
    ]
    assert missing == [], out
    assert "Failed to process" not in out, out


def test_hierarchy_pass_truncates_the_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_dir = _build_projects_dir(tmp_path)
    db_path = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))

    process_projects_hierarchy(projects_dir, jobs=1)
    _make_all_stale(projects_dir)

    # An idle connection that outlives the pass, like the `serve` search
    # API's reader: while it is open, closing the pass's own connection is
    # not the last close, so SQLite's checkpoint-on-close never runs.
    observer = sqlite3.connect(db_path)
    try:
        observer.execute("SELECT 1 FROM projects").fetchall()
        process_projects_hierarchy(projects_dir, jobs=1)
        wal = db_path.with_name(db_path.name + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0, (
            f"WAL left at {wal.stat().st_size} bytes after the pass"
        )
    finally:
        observer.close()


def test_writer_connections_bound_the_wal_file(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "-proj"
    project.mkdir(parents=True)
    manager = CacheManager(project, get_library_version())
    with manager._get_connection() as conn:
        limit = conn.execute("PRAGMA journal_size_limit").fetchone()[0]
    assert limit == WAL_SIZE_LIMIT_BYTES
    assert limit > 0


def test_entries_are_serialised_before_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The json+zlib pass over a file's entries is the costly part of a
    save, and it scales with the session. Done inside the transaction it
    holds every other worker's writes off for its whole duration."""
    project = tmp_path / "projects" / "-proj"
    project.mkdir(parents=True)
    jsonl = project / "session.jsonl"
    shutil.copy(TEST_DATA_DIR / "edge_cases.jsonl", jsonl)
    entries = load_transcript(jsonl, silent=True)
    assert entries

    manager = CacheManager(project, get_library_version())
    lock_free_during_serialisation: List[bool] = []
    original = CacheManager._serialize_entry

    def probing_serialize(self: CacheManager, *args: Any, **kwargs: Any) -> Dict:
        if not lock_free_during_serialisation:
            other = sqlite3.connect(manager.db_path, timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                lock_free_during_serialisation.append(True)
            except sqlite3.OperationalError:
                lock_free_during_serialisation.append(False)
            finally:
                other.close()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CacheManager, "_serialize_entry", probing_serialize)
    manager.save_cached_entries(jsonl, entries)

    assert lock_free_during_serialisation == [True]
    loaded = manager.load_cached_entries(jsonl)
    assert loaded is not None and len(loaded) == len(entries)


def test_search_index_builder_waits_like_other_cache_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from claude_code_log import cli, search

    project = tmp_path / "projects" / "-proj"
    project.mkdir(parents=True)
    manager = CacheManager(project, get_library_version())
    seen: List[int] = []

    def capture(conn: sqlite3.Connection, **_kwargs: Any) -> Any:
        seen.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        raise sqlite3.DatabaseError("stop here")

    monkeypatch.setattr(search, "ensure_index", capture)
    monkeypatch.setattr(search, "fts5_available", lambda _conn: True)
    with pytest.raises(sqlite3.DatabaseError, match="stop here"):
        cli._build_search_index(manager.db_path, (), rebuild=False)

    assert seen == [int(cache_busy_timeout() * 1000)]
    assert seen[0] >= 30_000


def test_serve_accepts_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_code_log import cli, server

    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    calls: List[Dict[str, Any]] = []

    def fake_hierarchy(path: Path, **kwargs: Any) -> Path:
        calls.append(kwargs)
        return path

    class FakeServer:
        url = "http://127.0.0.1:0"

        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def stop(self) -> None:
            pass

    monkeypatch.setattr(cli, "process_projects_hierarchy", fake_hierarchy)
    monkeypatch.setattr(server, "ArchiveServer", FakeServer)
    result = CliRunner().invoke(
        cli.main,
        ["serve", "--projects-dir", str(projects_dir), "--no-index", "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output
    assert calls and calls[0].get("jobs") == 1


def test_a_lock_that_outlasts_the_retry_is_still_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The retry is one more attempt, not a way to swallow the failure: a
    writer from another process that is still holding on gets the project
    reported exactly as before."""
    projects_dir = _build_projects_dir(tmp_path)
    db_path = tmp_path / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_TIMEOUT", "0.5")
    monkeypatch.setattr(converter, "_holdback_plans", lambda *a, **k: [])

    process_projects_hierarchy(projects_dir, jobs=1)
    _make_all_stale(projects_dir)
    capsys.readouterr()

    holder = sqlite3.connect(db_path, isolation_level=None)

    class _HoldFromPoolStart(converter.ProcessPoolExecutor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            holder.execute("BEGIN IMMEDIATE")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(converter, "ProcessPoolExecutor", _HoldFromPoolStart)
    try:
        process_projects_hierarchy(projects_dir, jobs=3)
    finally:
        if holder.in_transaction:
            holder.execute("ROLLBACK")
        holder.close()
    out = capsys.readouterr().out

    for project in PROJECTS:
        assert f"{project}: cache busy, will retry" in out, out
        assert f"Failed to process {projects_dir / project}" in out, out
    assert "database is locked" in out


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 30.0), ("120", 120.0), ("2.5", 2.5), ("0", 30.0), ("soon", 30.0)],
)
def test_cache_busy_timeout_env(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    if raw is None:
        monkeypatch.delenv("CLAUDE_CODE_LOG_CACHE_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_TIMEOUT", raw)
    assert cache_busy_timeout() == expected

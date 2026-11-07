"""How a refresh commits: one transaction, one per file, or no spool at all.

A refresh over a project the cache already holds spools its rows and
merges them at its end. In one transaction when that costs little more
lock than merging file by file (the rows beyond the largest file's are
at most `atomic_merge_max_extra_bytes`); otherwise one transaction per
file, the first marking the project unfinished (its message total at
0, which the next run reads as "refresh in full") and the last writing
the derived rows, which restore it, only if every file it merged is
still as it left it. A cold build, of a project with no cached rows,
spools nothing and writes per file as it parses.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, List

import pytest

from claude_code_log import cache as cache_mod
from claude_code_log import converter
from claude_code_log.cache import (
    ATOMIC_MERGE_MAX_EXTRA_BYTES,
    MERGE_CACHE_SIZE_KIB,
    CacheManager,
    atomic_merge_max_extra_bytes,
    get_library_version,
)
from test.test_cache_atomic_refresh import (
    _append,
    _dump,
    _entries,
    _grow,
    _locked,
    _manager,
    _never_busy,
    _pass,
    _refresh,
    _rows_by_file,
    _spools,
    _turn,
    _fail_second_file_write,
    after_columns,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Two sessions of one turn each (as in test_cache_atomic_refresh)."""
    project = tmp_path / "projects" / "-proj"
    project.mkdir(parents=True)
    for sid in ("sess-alpha", "sess-beta"):
        _append(project / f"{sid}.jsonl", _turn(sid, 0, f"first {sid}"))
    return project


def _per_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every merge with more than one file goes per file."""
    monkeypatch.setenv("CLAUDE_CODE_LOG_ATOMIC_MERGE_MB", "0")


def _total(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT total_message_count FROM projects").fetchone()[0]
    finally:
        conn.close()


def _sessions(project: Path) -> dict[str, int]:
    data = _manager(project).get_cached_project_data()
    assert data is not None
    return {sid: s.message_count for sid, s in data.sessions.items()}


def _built(project: Path) -> CacheManager:
    """A manager over a cache that already holds the project."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    return manager


def _begins(manager: CacheManager, files: List[Path], changed: List[Path]) -> List[str]:
    """The BEGINs on the scope's connection while it refreshes ``changed``."""
    begins: List[str] = []
    with manager.atomic_refresh():
        conn = manager._shared_conn  # pyright: ignore[reportPrivateUsage]
        assert conn is not None
        conn.set_trace_callback(
            lambda sql: begins.append(sql) if sql.upper().startswith("BEGIN") else None
        )
        _refresh(manager, files, changed)
    return begins


# ---- the threshold -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ATOMIC_MERGE_MAX_EXTRA_BYTES),
        ("", ATOMIC_MERGE_MAX_EXTRA_BYTES),
        ("0", 0),
        ("0.5", 512 * 1024),
        ("64", 64 * 1024 * 1024),
        ("-1", ATOMIC_MERGE_MAX_EXTRA_BYTES),
        ("lots", ATOMIC_MERGE_MAX_EXTRA_BYTES),
    ],
)
def test_the_threshold_reads_its_override_in_megabytes(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    if value is None:
        monkeypatch.delenv("CLAUDE_CODE_LOG_ATOMIC_MERGE_MB", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_LOG_ATOMIC_MERGE_MB", value)
    assert atomic_merge_max_extra_bytes() == expected
    assert ATOMIC_MERGE_MAX_EXTRA_BYTES == 16 * 1024 * 1024


# ---- which mode ------------------------------------------------------------


def test_a_small_refresh_merges_in_one_transaction(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_LOG_ATOMIC_MERGE_MB", raising=False)
    manager = _built(project)
    files, changed = _grow(project)
    assert len(_begins(manager, files, changed)) == 1


def test_a_spread_refresh_merges_per_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mark, one transaction per spooled file, the derived rows."""
    _per_file(monkeypatch)
    manager = _built(project)
    files, changed = _grow(project)
    assert len(_begins(manager, files, changed)) == len(changed) + 2
    assert _sessions(project) == {"sess-alpha": 4, "sess-beta": 2, "sess-gamma": 2}
    assert _total(manager.db_path) == 8


def test_a_refresh_of_one_file_stays_atomic_whatever_its_size(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Merging per file cannot shorten a one-file merge's hold."""
    _per_file(monkeypatch)
    manager = _built(project)
    alpha = project / "sess-alpha.jsonl"
    _append(alpha, _turn("sess-alpha", 1, "late alpha"))
    files = sorted(project.glob("*.jsonl"))
    assert len(_begins(manager, files, [alpha])) == 1


def test_a_cold_build_writes_per_file_without_a_spool(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spools: List[object] = []
    from claude_code_log.refresh_spool import RefreshSpool

    class Recording(RefreshSpool):
        def __init__(self, db_path: Path) -> None:
            spools.append(db_path)
            super().__init__(db_path)

    monkeypatch.setattr(cache_mod, "RefreshSpool", Recording)
    manager = _manager(project)
    converter.ensure_fresh_cache(project, manager, silent=True)
    assert spools == []
    assert _sessions(project) == {"sess-alpha": 2, "sess-beta": 2}
    assert _total(manager.db_path) == 4

    # Positive control: over the rows it now holds, a refresh spools.
    _grow(project)
    converter.ensure_fresh_cache(project, _manager(project), silent=True)
    assert len(spools) == 1
    assert _total(manager.db_path) == 8


@pytest.mark.parametrize("mode", ["one", "per-file"])
def test_both_modes_leave_the_same_cache(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Row for row, per file is the one-transaction merge."""
    if mode == "per-file":
        _per_file(monkeypatch)
    manager = _built(project)
    files, changed = _grow(project)
    with manager.atomic_refresh():
        _refresh(manager, files, changed)

    monkeypatch.delenv("CLAUDE_CODE_LOG_ATOMIC_MERGE_MB", raising=False)
    direct = CacheManager(project, get_library_version(), db_path=tmp_path / "d.db")
    _refresh(direct, files, files)

    assert _rows_by_file(manager.db_path) == _rows_by_file(direct.db_path)
    merged, reference = _dump(manager.db_path), _dump(direct.db_path)
    assert [r[2:] for r in merged["sessions"]] == [r[2:] for r in reference["sessions"]]
    cols = after_columns(manager.db_path)
    skip = {"id", "cache_created", "last_updated"}
    assert [v for c, v in zip(cols, merged["projects"][0]) if c not in skip] == [
        v for c, v in zip(cols, reference["projects"][0]) if c not in skip
    ]


def test_the_merge_runs_with_a_larger_page_cache_and_gives_it_back(
    project: Path,
) -> None:
    manager = _built(project)
    files, changed = _grow(project)
    during: List[int] = []
    real = CacheManager._merge_spooled_file  # pyright: ignore[reportPrivateUsage]

    def spy(self: CacheManager, conn: sqlite3.Connection, *args: Any) -> int:
        during.append(conn.execute("PRAGMA cache_size").fetchone()[0])
        return real(self, conn, *args)

    with manager.batch():
        conn = manager._shared_conn  # pyright: ignore[reportPrivateUsage]
        assert conn is not None
        before = conn.execute("PRAGMA cache_size").fetchone()[0]
        with pytest.MonkeyPatch.context() as m:
            m.setattr(CacheManager, "_merge_spooled_file", spy)
            with manager.atomic_refresh():
                _refresh(manager, files, changed)
        after = conn.execute("PRAGMA cache_size").fetchone()[0]
    assert during and set(during) == {-MERGE_CACHE_SIZE_KIB}
    assert after == before


# ---- interrupted, overtaken ------------------------------------------------


def _fail_mark(m: pytest.MonkeyPatch) -> None:
    m.setattr(CacheManager, "_mark_refresh_unfinished", _locked)


def _fail_second_file(m: pytest.MonkeyPatch) -> None:
    real = CacheManager._merge_spooled_file  # pyright: ignore[reportPrivateUsage]
    calls: List[str] = []

    def second_fails(self: CacheManager, conn: sqlite3.Connection, *args: Any) -> int:
        calls.append("x")
        if len(calls) == 2:
            _locked()
        return real(self, conn, *args)

    m.setattr(CacheManager, "_merge_spooled_file", second_fails)


def _fail_derived(m: pytest.MonkeyPatch) -> None:
    def deferred_failure(self: CacheManager, **_kwargs: Any) -> None:
        self._write_or_defer(_locked)  # pyright: ignore[reportPrivateUsage]

    m.setattr(CacheManager, "update_project_aggregates", deferred_failure)


@pytest.mark.parametrize(
    "inject",
    [_fail_mark, _fail_second_file, _fail_derived],
    ids=["mark", "file", "derived"],
)
def test_an_interrupted_per_file_merge_is_marked_and_heals(
    project: Path, monkeypatch: pytest.MonkeyPatch, inject: Any
) -> None:
    _per_file(monkeypatch)
    manager = _built(project)
    before = _dump(manager.db_path)
    files, changed = _grow(project)

    with monkeypatch.context() as m:
        inject(m)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with manager.atomic_refresh():
                _refresh(manager, files, changed)
    assert _spools(manager.db_path) == []
    if inject is _fail_mark:
        # Nothing committed: the cache is as it was, total included.
        assert _dump(manager.db_path) == before
    else:
        assert _total(manager.db_path) == 0

    converter.ensure_fresh_cache(project, _manager(project), silent=True)
    assert _sessions(project) == {"sess-alpha": 4, "sess-beta": 2, "sess-gamma": 2}
    assert _total(manager.db_path) == 8


def test_a_per_file_merge_overtaken_by_another_writer_stays_marked(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between A's file transactions, B rewrites a file A has merged,
    from a newer parse. A's derived rows describe A's parse: written,
    they would read current over B's rows. A leaves the mark instead,
    and the next run derives from what the cache holds."""
    _per_file(monkeypatch)
    manager = _built(project)
    files, changed = _grow(project)
    alpha = project / "sess-alpha.jsonl"
    overtaken: List[str] = []
    real = CacheManager._merge_one_file  # pyright: ignore[reportPrivateUsage]

    def then_b(
        self: CacheManager, conn: sqlite3.Connection, spooled: Any, *args: Any
    ) -> Any:
        result = real(self, conn, spooled, *args)
        if not overtaken and spooled.file_name == alpha.name:
            overtaken.append(spooled.file_name)
            _append(alpha, _turn("sess-alpha", 2, "turn two"))
            b = CacheManager(project, get_library_version(), db_path=manager.db_path)
            b.save_cached_entries(alpha, _entries(alpha))
        return result

    with monkeypatch.context() as m:
        m.setattr(CacheManager, "_merge_one_file", then_b)
        with manager.atomic_refresh():
            _refresh(manager, files, changed)
    assert overtaken == [alpha.name]  # positive control: B ran in between
    assert len(_rows_by_file(manager.db_path)["sess-alpha.jsonl"]) == 6
    assert _total(manager.db_path) == 0

    converter.ensure_fresh_cache(project, _manager(project), silent=True)
    assert _sessions(project) == {"sess-alpha": 6, "sess-beta": 2, "sess-gamma": 2}
    assert _total(manager.db_path) == 10


def test_an_interrupted_cold_build_is_marked_and_heals(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    projects_dir = project.parent
    db_path = projects_dir / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    with monkeypatch.context() as m:
        _fail_second_file_write(m)
        m.setattr(converter, "is_busy_database_error", _never_busy)
        _pass(projects_dir)
    assert "projects failed" in capsys.readouterr().out
    assert _spools(db_path) == []
    # Per file: the first file is in, the project reads unfinished.
    assert len(_rows_by_file(db_path)) == 1
    assert _total(db_path) == 0

    _pass(projects_dir)
    assert _sessions(project) == {"sess-alpha": 2, "sess-beta": 2}
    assert _total(db_path) == 4


def test_a_per_file_merge_with_nothing_derived_takes_no_mark(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scope that writes rows only (as `atomic_refresh` allows any
    caller): nothing derived can fall out of step with them, and nothing
    would lift a mark, so the project keeps its total instead of being
    refreshed in full by the next run for no reason."""
    _per_file(monkeypatch)
    manager = _built(project)
    files, changed = _grow(project)
    begins: List[str] = []
    with manager.atomic_refresh():
        conn = manager._shared_conn  # pyright: ignore[reportPrivateUsage]
        assert conn is not None
        conn.set_trace_callback(
            lambda sql: begins.append(sql) if sql.upper().startswith("BEGIN") else None
        )
        for f in changed:
            manager.save_cached_entries(f, _entries(f))
    # Per file (one per file, then the last), and no mark before them.
    assert len(begins) == len(changed) + 1
    assert len(_rows_by_file(manager.db_path)["sess-gamma.jsonl"]) == 2
    assert _total(manager.db_path) == 4

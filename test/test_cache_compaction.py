"""A pass hands the cache's free pages back to the disk when there are many.

SQLite keeps the pages a write frees inside the file for later writes; a
re-parse after a content-schema change frees a lot at once.
"""

import sqlite3
from pathlib import Path

import pytest

from claude_code_log.cache import compact_cache


def _sparse_db(path: Path, rows: int = 400, keep: int = 40) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE blobs (id INTEGER PRIMARY KEY, data BLOB)")
        conn.executemany(
            "INSERT INTO blobs (data) VALUES (?)",
            [(b"x" * 16384,) for _ in range(rows)],
        )
        conn.commit()
        conn.execute("DELETE FROM blobs WHERE id > ?", (keep,))
        conn.commit()
    conn.close()


def _pages(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as conn:
        count = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    conn.close()
    return count, free


def test_a_sparse_cache_is_compacted(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    _sparse_db(db)
    count, free = _pages(db)
    assert free > count / 2

    reclaimed = compact_cache(db, min_free_bytes=1024)

    assert reclaimed and reclaimed > 0
    after_count, after_free = _pages(db)
    assert after_free == 0 and after_count < count
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 40
    conn.close()


def test_a_cache_with_few_free_pages_is_left_alone(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    _sparse_db(db, keep=390)
    before = _pages(db)

    assert compact_cache(db, min_free_bytes=1024) is None
    assert _pages(db) == before


def test_small_caches_stay_below_the_default_floor(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    _sparse_db(db)
    assert compact_cache(db) is None


def test_it_can_be_switched_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "cache.db"
    _sparse_db(db)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_COMPACT", "0")
    assert compact_cache(db, min_free_bytes=1024) is None


def test_a_busy_cache_is_left_for_the_next_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import claude_code_log.cache as cache

    db = tmp_path / "cache.db"
    _sparse_db(db)
    monkeypatch.setattr(cache, "_COMPACT_BUSY_TIMEOUT_MS", 50)
    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        assert compact_cache(db, min_free_bytes=1024) is None
    finally:
        writer.rollback()
        writer.close()
    assert compact_cache(db, min_free_bytes=1024)


def test_a_missing_cache_is_not_created(tmp_path: Path) -> None:
    assert compact_cache(tmp_path / "missing.db") is None
    assert not (tmp_path / "missing.db").exists()

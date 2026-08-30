"""The cached file-size check (migration 011).

Freshness used to compare mtimes with a 1.0s tolerance and nothing else,
so a write landing within a second of the mtime recorded at cache time
was invisible. These tests pin the fix and its backward-compatible
fallback.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from claude_code_log.cache import CacheManager


def _entry(uuid: str, text: str) -> str:
    return (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2025-07-03T16:15:00Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "s1",
                "version": "1.0.0",
                "uuid": uuid,
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "s1.jsonl").write_text(_entry("u1", "first"), encoding="utf-8")
    return d


def _cache(project: Path) -> CacheManager:
    cm = CacheManager(project, "test-version")
    cm.save_cached_entries(project / "s1.jsonl", [])
    return cm


def test_append_the_mtime_check_cannot_see_is_detected(project: Path) -> None:
    """The case the tolerance used to hide, and the reason for 011.

    The mtime is restored after the append so the mtime term provably
    reports "fresh" — the detection can only be coming from the size.
    (Left to real timing this would depend on whether the append landed
    inside the 1.0s tolerance, which under parallel test execution goes
    either way.)
    """
    import os

    cm = _cache(project)
    jsonl = project / "s1.jsonl"
    assert cm.is_file_cached(jsonl), "sanity: freshly cached"
    before = jsonl.stat()

    with jsonl.open("a", encoding="utf-8") as f:
        f.write(_entry("u2", "second"))
    os.utime(jsonl, (before.st_atime, before.st_mtime))

    assert not cm.is_file_cached(jsonl)
    assert cm.get_modified_files([jsonl]) == [jsonl]


def test_untouched_file_stays_cached(project: Path) -> None:
    """The rule only tightens; it must not invalidate a stable file."""
    cm = _cache(project)
    jsonl = project / "s1.jsonl"
    assert cm.is_file_cached(jsonl)
    assert cm.get_modified_files([jsonl]) == []


def test_pre_011_rows_fall_back_to_the_mtime_check(project: Path) -> None:
    """A populated cache from before the migration must not mass-invalidate.

    Rows written before 011 carry NULL, matching what the ALTER TABLE
    gives existing rows.
    """
    cm = _cache(project)
    jsonl = project / "s1.jsonl"

    with sqlite3.connect(cm.db_path) as conn:
        conn.execute("UPDATE cached_files SET source_size = NULL")
        conn.commit()

    # NULL size => mtime-only rule => an untouched file is still fresh.
    assert cm.is_file_cached(jsonl)
    assert cm.get_modified_files([jsonl]) == []


def test_size_is_recorded_on_save(project: Path) -> None:
    cm = _cache(project)
    jsonl = project / "s1.jsonl"
    with sqlite3.connect(cm.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT source_size FROM cached_files WHERE file_name = ?", ("s1.jsonl",)
        ).fetchone()
    assert row["source_size"] == jsonl.stat().st_size


def test_pre_011_fallback_cannot_see_an_append_the_mtime_hides(
    project: Path,
) -> None:
    """The negative control that proves 011 does something.

    With the size NULLed out the rule is exactly what it was before the
    migration, and it cannot see an append the mtime doesn't report.
    Also an honest note on the fallback's limit: a cache populated before
    011 keeps the old blind spot until its rows are rewritten.

    The mtime is restored explicitly rather than relying on the append
    landing inside the 1.0s tolerance — under parallel test execution
    that race resolves either way, and a timing-dependent test is worth
    less than the thing it pins.
    """
    import os

    cm = _cache(project)
    jsonl = project / "s1.jsonl"
    before = jsonl.stat()

    with sqlite3.connect(cm.db_path) as conn:
        conn.execute("UPDATE cached_files SET source_size = NULL")
        conn.commit()

    with jsonl.open("a", encoding="utf-8") as f:
        f.write(_entry("u2", "second"))
    os.utime(jsonl, (before.st_atime, before.st_mtime))

    assert cm.is_file_cached(jsonl), (
        "pre-011 rows fall back to mtime-only, which cannot see this"
    )

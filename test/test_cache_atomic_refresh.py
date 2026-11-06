"""A refresh commits as one transaction, or not at all.

A cache refresh writes each transcript file's entry rows, then the
session rows, the project aggregates and the cross-session sidecar
derived from them. Written one transaction each, a refresh that stops in
between (a lock-out, a kill, a full disk) leaves files recorded as
current over derived rows that predate them. Inside
``CacheManager.atomic_refresh`` the entry rows go to a private spool
file as they are serialised, the derived writes are deferred, and one
merge transaction at the end commits everything: an interruption at any
point leaves the cache exactly as it was.

These tests drive the scope directly, with the writes a refresh makes,
and read the cache back through plain SQLite.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from claude_code_log import converter
from claude_code_log.cache import CacheManager, SessionSidecar, get_library_version
from claude_code_log.converter import (
    compute_project_aggregates,
    compute_session_data,
    load_transcript,
    process_projects_hierarchy,
)
from claude_code_log.models import TranscriptEntry

TABLES = (
    "projects",
    "cached_files",
    "messages",
    "sessions",
    "session_parents",
    "junction_uuids",
    "dedup_winners",
    "sidecar_state",
)


def _user(
    sid: str, uid: str, parent: str | None, minute: int, text: str
) -> Dict[str, Any]:
    return {
        "type": "user",
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": f"2026-01-01T10:{minute:02d}:00Z",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0.0",
        "message": {"role": "user", "content": text},
    }


def _assistant(
    sid: str, uid: str, parent: str, minute: int, text: str
) -> Dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": f"2026-01-01T10:{minute:02d}:00Z",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0.0",
        "requestId": f"req_{uid}",
        "message": {
            "id": f"msg_{uid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-3-sonnet",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _turn(sid: str, n: int, text: str) -> List[Dict[str, Any]]:
    parent = f"{sid}-a{n - 1}" if n else None
    return [
        _user(sid, f"{sid}-u{n}", parent, 2 * n, text),
        _assistant(sid, f"{sid}-a{n}", f"{sid}-u{n}", 2 * n + 1, f"reply {n}"),
    ]


def _append(path: Path, entries: List[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _entries(path: Path) -> List[TranscriptEntry]:
    return load_transcript(path, None, silent=True)


def _dump(db_path: Path) -> Dict[str, List[Tuple[Any, ...]]]:
    """Every row of every table a refresh touches, in a plain connection."""
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: sorted(conn.execute(f"SELECT * FROM {table}").fetchall())
            for table in TABLES
        }
    finally:
        conn.close()


def _rows_by_file(db_path: Path) -> Dict[str, List[Tuple[Any, ...]]]:
    """Each file's message rows in insertion order, without the ids."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT f.file_name, m.type, m.timestamp, m.session_id, m._uuid,
                      m._parent_uuid, m._request_id, m.content
               FROM messages m JOIN cached_files f ON f.id = m.file_id
               ORDER BY f.file_name, m.id"""
        ).fetchall()
    finally:
        conn.close()
    by_file: Dict[str, List[Tuple[Any, ...]]] = {}
    for row in rows:
        by_file.setdefault(row[0], []).append(row[1:])
    return by_file


def _spools(db_path: Path) -> List[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.spool-*"))


def _derive(manager: CacheManager, files: List[Path]) -> None:
    """The derived writes of a refresh, from the files' current entries."""
    messages = [e for f in files for e in _entries(f)]
    manager.update_session_cache(compute_session_data(messages, include_cwd=True))
    manager.save_session_sidecar(
        SessionSidecar(parents={}, junctions={}, dedup_winners={})
    )
    manager.update_project_aggregates(**compute_project_aggregates(messages))


def _refresh(manager: CacheManager, files: List[Path], changed: List[Path]) -> None:
    """The writes of a refresh in which ``changed`` were (re)parsed."""
    for f in changed:
        manager.save_cached_entries(f, _entries(f))
    _derive(manager, files)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "-proj"
    project.mkdir(parents=True)
    for sid in ("sess-alpha", "sess-beta"):
        _append(project / f"{sid}.jsonl", _turn(sid, 0, f"first {sid}"))
    return project


def _manager(project: Path) -> CacheManager:
    return CacheManager(
        project, get_library_version(), db_path=project.parent / "cache.db"
    )


def _grow(project: Path) -> Tuple[List[Path], List[Path]]:
    """Append to one session and start another; returns (all, changed)."""
    alpha = project / "sess-alpha.jsonl"
    gamma = project / "sess-gamma.jsonl"
    _append(alpha, _turn("sess-alpha", 1, "late alpha"))
    _append(gamma, _turn("sess-gamma", 0, "first gamma"))
    return sorted(project.glob("*.jsonl")), [alpha, gamma]


def test_a_refresh_that_fails_before_its_merge_leaves_the_cache_as_it_was(
    project: Path,
) -> None:
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    before = _dump(manager.db_path)
    assert before["messages"] and before["sessions"]  # the build happened

    files, changed = _grow(project)
    with pytest.raises(RuntimeError, match="interrupted"):
        with manager.atomic_refresh():
            _refresh(manager, files, changed)
            # Positive control: the rows went to a spool, not the cache.
            assert len(_spools(manager.db_path)) == 1
            assert _dump(manager.db_path) == before
            raise RuntimeError("interrupted")

    assert _dump(manager.db_path) == before
    assert _spools(manager.db_path) == []


def test_a_refresh_commits_everything_in_one_transaction_at_its_end(
    project: Path,
) -> None:
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    before = _dump(manager.db_path)

    files, changed = _grow(project)
    begins: List[str] = []
    with manager.atomic_refresh():
        # The scope opened the connection it will merge on.
        conn = manager._shared_conn  # pyright: ignore[reportPrivateUsage]
        assert conn is not None
        conn.set_trace_callback(
            lambda sql: begins.append(sql) if sql.upper().startswith("BEGIN") else None
        )
        _refresh(manager, files, changed)
        # Another connection sees nothing of it yet.
        assert _dump(manager.db_path) == before
        assert begins == []
    after = _dump(manager.db_path)

    assert len(begins) == 1, begins
    assert _spools(manager.db_path) == []
    # And everything of it afterwards: the new file, the appended rows,
    # the session rows and the aggregates derived from them.
    rows = _rows_by_file(manager.db_path)
    assert set(rows) == {f.name for f in files}
    assert len(rows["sess-alpha.jsonl"]) == 4
    assert len(rows["sess-gamma.jsonl"]) == 2
    sessions = {row[2]: row for row in after["sessions"]}
    assert set(sessions) == {"sess-alpha", "sess-beta", "sess-gamma"}
    assert (
        after["projects"][0][
            after_columns(manager.db_path).index("total_message_count")
        ]
        == 8
    )


def after_columns(db_path: Path) -> List[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [c[1] for c in conn.execute("PRAGMA table_info(projects)")]
    finally:
        conn.close()


def test_a_merged_refresh_writes_what_the_direct_writes_would(
    project: Path, tmp_path: Path
) -> None:
    """Row for row, the merge is the direct path: same records, same
    rows in the same order, same derived rows."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    files, changed = _grow(project)
    with manager.atomic_refresh():
        _refresh(manager, files, changed)

    # The same end state written directly, into a cache of its own.
    direct = CacheManager(project, get_library_version(), db_path=tmp_path / "d.db")
    _refresh(direct, files, files)

    assert _rows_by_file(manager.db_path) == _rows_by_file(direct.db_path)
    merged, reference = _dump(manager.db_path), _dump(direct.db_path)

    def records(dump: Dict[str, List[Tuple[Any, ...]]]) -> List[Tuple[Any, ...]]:
        # (file_name, file_path, source_mtime, message_count, fp, version)
        return sorted(
            (r[2], r[3], r[4], r[6], r[7], r[9]) for r in dump["cached_files"]
        )

    assert records(merged) == records(reference)
    assert [r[2:] for r in merged["sessions"]] == [r[2:] for r in reference["sessions"]]
    # The aggregates, minus the timestamps of the two builds.
    cols = after_columns(manager.db_path)
    skip = {"id", "cache_created", "last_updated"}
    assert [v for c, v in zip(cols, merged["projects"][0]) if c not in skip] == [
        v for c, v in zip(cols, reference["projects"][0]) if c not in skip
    ]


def test_a_spooled_tail_whose_rows_moved_underneath_is_rewritten_whole(
    project: Path,
) -> None:
    """The append's row check runs at the merge, under its lock."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    alpha = project / "sess-alpha.jsonl"
    old_entries = _entries(alpha)
    _append(alpha, _turn("sess-alpha", 1, "late alpha"))
    all_entries = _entries(alpha)
    appended = all_entries[len(old_entries) :]

    with manager.atomic_refresh():
        assert manager.extend_cached_entries(alpha, all_entries, appended)
        # Another process rewrites the file's rows before the merge: the
        # tail no longer has the rows it expected to follow.
        other = _manager(project)
        other.save_cached_entries(alpha, old_entries[:1])
        assert len(_rows_by_file(manager.db_path)["sess-alpha.jsonl"]) == 1
        _derive(manager, files)

    rows = _rows_by_file(manager.db_path)["sess-alpha.jsonl"]
    assert len(rows) == len(all_entries)
    assert [r[3] for r in rows] == [getattr(e, "uuid", None) for e in all_entries]


def test_a_tail_for_a_file_already_spooled_is_refused(project: Path) -> None:
    """The caller then saves the file whole, as after a direct refusal."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    alpha = project / "sess-alpha.jsonl"
    old_entries = _entries(alpha)
    _append(alpha, _turn("sess-alpha", 1, "late alpha"))
    all_entries = _entries(alpha)
    with manager.atomic_refresh():
        manager.save_cached_entries(alpha, old_entries)
        assert not manager.extend_cached_entries(
            alpha, all_entries, all_entries[len(old_entries) :]
        )
        manager.save_cached_entries(alpha, all_entries)
        _derive(manager, files)
    rows = _rows_by_file(manager.db_path)["sess-alpha.jsonl"]
    assert [r[3] for r in rows] == [getattr(e, "uuid", None) for e in all_entries]


def test_a_sidecar_that_cannot_be_written_is_dropped_and_the_rest_commits(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Such a failure can recur on every run; it must not fail the
    refresh forever. The marker goes, so readers decline to the full
    load until a sidecar is written whole, as for a direct write."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    assert manager.load_session_sidecar() is not None
    files, changed = _grow(project)

    # A winner row SQLite refuses (NOT NULL), past the model's validation.
    broken = SessionSidecar.model_construct(
        parents={}, junctions={}, dedup_winners={"u": None}
    )
    with manager.atomic_refresh():
        for f in changed:
            manager.save_cached_entries(f, _entries(f))
        messages = [e for f in files for e in _entries(f)]
        manager.update_session_cache(compute_session_data(messages, include_cwd=True))
        manager.save_session_sidecar(broken)
        manager.update_project_aggregates(**compute_project_aggregates(messages))

    assert "dropped it" in caplog.text
    assert manager.load_session_sidecar() is None
    rows = _rows_by_file(manager.db_path)
    assert len(rows["sess-gamma.jsonl"]) == 2
    assert manager.get_modified_files(files) == []


def test_a_sidecar_locked_out_at_the_merge_fails_the_whole_refresh(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contention is transient: the refresh fails and is redone, with
    nothing of it in the cache, rather than committing without the
    sidecar."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    before = _dump(manager.db_path)
    files, changed = _grow(project)

    def locked(conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("database is locked")

    def locked_sidecar(self: CacheManager, sidecar: SessionSidecar) -> None:
        self._write_or_defer(locked, sidecar=True)  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(CacheManager, "save_session_sidecar", locked_sidecar)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with manager.atomic_refresh():
            _refresh(manager, files, changed)
    assert _dump(manager.db_path) == before
    assert _spools(manager.db_path) == []


def _pass(projects_dir: Path) -> None:
    process_projects_hierarchy(
        projects_dir, output_format="md", write_combined=False, jobs=1
    )


def _locked(*_args: Any, **_kwargs: Any) -> None:
    raise sqlite3.OperationalError("database is locked")


def _never_busy(_exc: BaseException) -> bool:
    return False


def _fail_second_file_write(m: pytest.MonkeyPatch) -> None:
    """The entry write of the second file parsed, after the first."""
    seen: List[str] = []
    for name in ("save_cached_entries", "extend_cached_entries"):
        original: Any = getattr(CacheManager, name)

        def wrapper(
            self: CacheManager,
            jsonl_path: Path,
            *args: Any,
            _original: Any = original,
            **kwargs: Any,
        ) -> Any:
            if seen and str(jsonl_path) not in seen:
                _locked()
            seen.append(str(jsonl_path))
            return _original(self, jsonl_path, *args, **kwargs)

        m.setattr(CacheManager, name, wrapper)


def _fail_session_rows_write(m: pytest.MonkeyPatch) -> None:
    m.setattr(CacheManager, "update_session_cache", _locked)


def _fail_sidecar_write(m: pytest.MonkeyPatch) -> None:
    m.setattr(CacheManager, "save_session_sidecar", _locked)


@pytest.mark.parametrize(
    "inject",
    [_fail_second_file_write, _fail_session_rows_write, _fail_sidecar_write],
    ids=["second-file-write", "session-rows-write", "sidecar-write"],
)
def test_an_interrupted_full_refresh_leaves_the_cache_as_it_was(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inject: Any,
) -> None:
    """A pass whose full refresh is locked out at any write.

    Whichever write it is, the project is reported failed and the cache
    is what it was before the pass: no file recorded as current, no
    session row, no aggregate from the refresh that did not finish.
    """
    projects_dir = project.parent
    db_path = projects_dir / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    monkeypatch.setenv("CLAUDE_CODE_LOG_INCREMENTAL_CACHE", "0")
    _pass(projects_dir)
    before = _dump(db_path)
    assert before["messages"]  # the first pass built the cache

    _grow(project)
    capsys.readouterr()
    with monkeypatch.context() as m:
        inject(m)
        m.setattr(converter, "is_busy_database_error", _never_busy)
        _pass(projects_dir)
    assert "projects failed" in capsys.readouterr().out
    assert _dump(db_path) == before
    assert _spools(db_path) == []

    # The next pass redoes the whole of it.
    _pass(projects_dir)
    rows = _rows_by_file(db_path)
    assert len(rows["sess-alpha.jsonl"]) == 4
    assert len(rows["sess-gamma.jsonl"]) == 2


def test_a_nested_scope_joins_the_outer_one(project: Path) -> None:
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    before = _dump(manager.db_path)
    with manager.atomic_refresh():
        with manager.atomic_refresh():
            _refresh(manager, files, files)
        assert _dump(manager.db_path) == before
        assert len(_spools(manager.db_path)) == 1
    assert _rows_by_file(manager.db_path).keys() == {f.name for f in files}
    assert _spools(manager.db_path) == []

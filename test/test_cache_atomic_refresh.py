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


def test_a_file_whose_rows_fail_part_way_leaves_the_spool_whole(
    tmp_path: Path,
) -> None:
    """``add`` rolls back the file it was writing; the spool stays valid.

    With the journal off, SQLite leaves ROLLBACK undefined, and a
    failure past the page cache corrupts the file. A small page cache
    makes a short run spill, which is what the journal setting has to
    survive.
    """
    from claude_code_log.refresh_spool import RefreshSpool

    spool = RefreshSpool(tmp_path / "cache.db")
    conn = spool._conn  # pyright: ignore[reportPrivateUsage]
    conn.execute("PRAGMA cache_size = -64")  # 64 KiB
    record = {
        "file_name": "a.jsonl",
        "file_path": "/a.jsonl",
        "source_mtime": 1.0,
        "source_size": 1,
        "cached_mtime": 1.0,
        "message_count": 1,
        "subagents_fingerprint": None,
        "content_version": None,
    }

    def rows(n: int, fail_at: int | None = None) -> Any:
        for i in range(n):
            if i == fail_at:
                raise RuntimeError("row %d" % i)
            yield {name: None for name in ("timestamp", "session_id")} | {
                "type": "user",
                "_uuid": f"u{i}",
                "_parent_uuid": None,
                "_is_sidechain": 0,
                "_user_type": None,
                "_cwd": None,
                "_version": None,
                "_is_meta": None,
                "_agent_id": None,
                "_request_id": None,
                "input_tokens": None,
                "output_tokens": None,
                "cache_creation_tokens": None,
                "cache_read_tokens": None,
                "_leaf_uuid": None,
                "_level": None,
                "_operation": None,
                "content": b"x" * 2048,
            }

    spool.add(record, rows(10), mode="replace")
    with pytest.raises(RuntimeError, match="row 400"):
        spool.add({**record, "file_name": "b.jsonl"}, rows(401, 400), mode="replace")

    assert [f.file_name for f in spool.files()] == ["a.jsonl"]
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    spool.close()
    spool.unlink()


def test_reads_inside_a_scope_see_the_spooled_rows(project: Path) -> None:
    """The incremental refresh reads back the rows it has just written:
    file states, uuid owners, the closure's entries. Inside a scope they
    are in the spool, and the reads find them there, a new file's
    under a provisional id."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    alpha = project / "sess-alpha.jsonl"
    gamma = project / "sess-gamma.jsonl"
    old_alpha = _entries(alpha)
    files, _changed = _grow(project)
    all_alpha = _entries(alpha)

    def read_back() -> None:
        states = manager.get_file_states(["sess-alpha.jsonl", "sess-gamma.jsonl"])
        assert len(states["sess-alpha.jsonl"].row_fingerprints) == 4
        assert len(states["sess-gamma.jsonl"].row_fingerprints) == 2
        assert states["sess-gamma.jsonl"].sessions == {"sess-gamma"}
        owners = manager.get_uuid_owners(["sess-gamma-u0", "sess-alpha-u1"])
        assert owners["sess-gamma-u0"] == {("sess-gamma", "user")}
        assert owners["sess-alpha-u1"] == {("sess-alpha", "user")}
        assert manager.get_session_file_map()["sess-gamma"] == {"sess-gamma.jsonl"}
        assert manager.is_file_cached(alpha) and manager.is_file_cached(gamma)
        loaded = manager.load_cached_entries(gamma)
        assert loaded is not None and len(loaded) == 2
        loaded = manager.load_cached_entries(alpha)
        assert loaded is not None and [getattr(e, "uuid", None) for e in loaded] == [
            getattr(e, "uuid", None) for e in all_alpha
        ]

    with manager.atomic_refresh():
        assert manager.extend_cached_entries(
            alpha, all_alpha, all_alpha[len(old_alpha) :]
        )
        manager.save_cached_entries(gamma, _entries(gamma))
        read_back()
        # Unspooled files read as before, through the same views.
        assert manager.is_file_cached(project / "sess-beta.jsonl")
        _derive(manager, files)
    read_back()
    # The provisional id was replaced by a real one at the merge.
    assert all(
        row[0] > 0
        for row in sqlite3.connect(manager.db_path)
        .execute("SELECT id FROM cached_files")
        .fetchall()
    )


def test_the_overlay_searches_the_indexes_of_both_arms(project: Path) -> None:
    """The lookups the incremental refresh makes stay index searches on
    the cache's side of the overlay, and on the spool's."""
    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    _refresh(manager, files, files)
    files, changed = _grow(project)
    with manager.atomic_refresh():
        for f in changed:
            manager.save_cached_entries(f, _entries(f))
        conn = manager._shared_conn  # pyright: ignore[reportPrivateUsage]
        assert conn is not None

        def plan(sql: str) -> str:
            return "\n".join(
                row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql)
            )

        by_uuid = plan(
            "SELECT _uuid, session_id, type FROM messages"
            " WHERE project_id = 1 AND _uuid IN ('a', 'b')"
        )
        assert "idx_messages_project_uuid" in by_uuid, by_uuid
        assert "messages_uuid" in by_uuid, by_uuid
        by_file = plan(
            "SELECT content FROM messages WHERE file_id IN (1, -1) ORDER BY file_id, id"
        )
        assert "idx_messages_file" in by_file, by_file
        assert "SCAN main.messages" not in by_file and "SCAN m\n" not in by_file, (
            by_file
        )
        _derive(manager, files)


def _fail_sidecar_merge(m: pytest.MonkeyPatch) -> None:
    m.setattr(CacheManager, "merge_session_sidecar", _locked)


@pytest.mark.parametrize(
    "inject",
    [_fail_second_file_write, _fail_session_rows_write, _fail_sidecar_merge],
    ids=["second-file-write", "session-rows-write", "sidecar-merge"],
)
def test_an_interrupted_incremental_refresh_leaves_the_cache_as_it_was(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inject: Any,
) -> None:
    """The incremental path, locked out at any of its writes."""
    projects_dir = project.parent
    db_path = projects_dir / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    _pass(projects_dir)
    before = _dump(db_path)

    _grow(project)
    capsys.readouterr()
    with monkeypatch.context() as m:
        inject(m)
        m.setattr(converter, "is_busy_database_error", _never_busy)
        _pass(projects_dir)
    out = capsys.readouterr().out
    # The pass prints the failure's traceback: the path under test is in it.
    assert "_incremental_cache_refresh" in out, out
    assert "projects failed" in out, out
    assert _dump(db_path) == before
    assert _spools(db_path) == []

    _pass(projects_dir)
    rows = _rows_by_file(db_path)
    assert len(rows["sess-alpha.jsonl"]) == 4
    assert len(rows["sess-gamma.jsonl"]) == 2


def test_two_refreshes_of_one_project_overlapping_leave_it_repairable(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later commit wins whole; the cache never records a file as
    current over derived rows from an earlier parse of it.

    Refresh A parses the session at one turn; while A is between its
    entry write and its derived writes, refresh B (another process's,
    here another manager's) parses a newer turn and commits. With
    several transactions, A's derived rows then land over B's entry
    rows and the file reads current while the session row predates it:
    the newer turn is never rendered. In one transaction A's snapshot
    wins whole, its file record is older than the file, and the next
    pass repairs it.
    """
    projects_dir = project.parent
    db_path = projects_dir / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    _pass(projects_dir)
    alpha = project / "sess-alpha.jsonl"
    _append(alpha, _turn("sess-alpha", 1, "turn one"))

    fired: List[int] = []
    original = CacheManager.update_session_cache

    def interleave(self: CacheManager, *args: Any, **kwargs: Any) -> None:
        if not fired:
            fired.append(1)
            _append(alpha, _turn("sess-alpha", 2, "turn two"))
            other = CacheManager(project, get_library_version(), db_path=db_path)
            converter.ensure_fresh_cache(project, other, silent=True)
        original(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(CacheManager, "update_session_cache", interleave)
        _pass(projects_dir)
    assert fired  # positive control: B ran inside A

    # The derived rows are where the race shows: with several
    # transactions, A's session row (turn one: 4 messages) lands over
    # B's entry rows (turn two: 6), the file reads current, and no pass
    # recomputes it. The entry rows themselves are B's either way.
    _pass(projects_dir)
    cached = CacheManager(project, get_library_version(), db_path=db_path)
    data = cached.get_cached_project_data()
    assert data is not None
    assert data.sessions["sess-alpha"].message_count == 6
    assert data.total_message_count == 6 + 2
    assert "turn two" in (project / "session-sess-alpha.md").read_text(encoding="utf-8")


def test_migration_016_heals_a_project_015_left_pending_and_drops_the_flag(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Migration 015 marked every project a cache held before it, since
    nothing else can tell whether its derived rows predate its entries;
    016 drops the flag, and keeps that heal by zeroing the marked
    project's total, which the next run reads as a cache to refresh in
    full, from the cached entries."""
    from claude_code_log.migrations.runner import get_current_version, run_migrations

    projects_dir = project.parent
    db_path = projects_dir / "cache.db"
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(db_path))
    _pass(projects_dir)

    # The cache as 015 leaves it: the flag set on the project, and
    # derived rows that may predate its entries (here: a stale count).
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN refresh_pending INTEGER NOT NULL DEFAULT 0"
        )
        conn.execute("UPDATE projects SET refresh_pending = 1")
        conn.execute("UPDATE sessions SET message_count = 1")
        conn.execute("DELETE FROM _schema_version WHERE version = 16")
    assert run_migrations(db_path) == 1
    with sqlite3.connect(db_path) as conn:
        assert get_current_version(conn) == 16
        columns = [row[1] for row in conn.execute("PRAGMA table_info(projects)")]
        assert "refresh_pending" not in columns
        assert conn.execute("SELECT total_message_count FROM projects").fetchone() == (
            0,
        )

    manager = _manager(project)
    files = sorted(project.glob("*.jsonl"))
    assert manager.get_modified_files(files) == []  # no file re-parsed
    assert converter.ensure_fresh_cache(project, manager, silent=True)
    data = manager.get_cached_project_data()
    assert data is not None
    assert {sid: s.message_count for sid, s in data.sessions.items()} == {
        "sess-alpha": 2,
        "sess-beta": 2,
    }
    assert data.total_message_count == 4


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

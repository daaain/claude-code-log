"""A refresh that fails part-way must not leave the project looking current.

A cache refresh writes each transcript file's entry rows in its own
transaction, and only afterwards the per-session rows and project
aggregates derived from them. Staleness reads both halves: a file is
modified when its ``cached_files`` row is older than the file, and a
rendered session is stale when its ``sessions`` row disagrees with the
``html_cache`` row of its last render. A write that fails in between
(``database is locked`` under ``--all-projects``, issue #332, or a kill)
used to leave the files fresh and the session rows as old as the
renders, so every later run said "cached" and never rendered the
missing messages.

These tests fail one write of a real refresh with the error SQLite
raises under lock contention, then run again cleanly and check that the
late messages reach the rendered sessions.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from claude_code_log.cache import CacheManager, get_library_version
from claude_code_log.converter import process_projects_hierarchy

SESSIONS = ["sess-alpha", "sess-beta", "sess-gamma"]
# Starts after the first run, so the failing run is the first to render it.
NEW_SESSION = "sess-delta"


def _user(
    sid: str, uid: str, parent: Optional[str], minute: int, text: str
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
    """The n-th user/assistant exchange of session ``sid``."""
    parent = f"{sid}-a{n - 1}" if n else None
    return [
        _user(sid, f"{sid}-u{n}", parent, 2 * n, text),
        _assistant(sid, f"{sid}-a{n}", f"{sid}-u{n}", 2 * n + 1, f"reply {n}"),
    ]


def _append(path: Path, entries: List[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    # Coarse-mtime filesystems (and Windows) must still see a change.
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _build_project(root: Path) -> Path:
    project = root / "projects" / "-proj"
    project.mkdir(parents=True)
    for sid in SESSIONS:
        _append(project / f"{sid}.jsonl", _turn(sid, 0, f"first {sid}"))
    return project


def _late_marker(sid: str) -> str:
    return f"LATE-{sid}"


def _run(projects_dir: Path) -> None:
    # The reporter's shape: Markdown, no combined page, so a session file
    # is the only render and its staleness is the only signal.
    process_projects_hierarchy(
        projects_dir, output_format="md", write_combined=False, jobs=1
    )


def _locked(*_args: Any, **_kwargs: Any) -> None:
    # What sqlite3 raises when the busy timeout expires on the write lock.
    raise sqlite3.OperationalError("database is locked")


def _fail_second_file_write(m: pytest.MonkeyPatch, fired: List[str]) -> None:
    """Lock out the entry write of the second file, after the first committed."""
    for name in ("save_cached_entries", "extend_cached_entries"):
        original: Callable[..., Any] = getattr(CacheManager, name)

        def wrapper(
            self: CacheManager,
            jsonl_path: Path,
            *args: Any,
            _original: Callable[..., Any] = original,
            **kwargs: Any,
        ) -> Any:
            seen = {p for p in fired if p != "LOCKED"}
            if "LOCKED" not in fired and seen and str(jsonl_path) not in seen:
                fired.append("LOCKED")
                _locked()
            fired.append(str(jsonl_path))
            return _original(self, jsonl_path, *args, **kwargs)

        m.setattr(CacheManager, name, wrapper)


def _fail_session_rows_write(m: pytest.MonkeyPatch, fired: List[str]) -> None:
    """Lock out the session-row write, after every file's entries committed."""
    original = CacheManager.update_session_cache

    def wrapper(self: CacheManager, *args: Any, **kwargs: Any) -> None:
        if "LOCKED" not in fired:
            fired.append("LOCKED")
            _locked()
        original(self, *args, **kwargs)

    m.setattr(CacheManager, "update_session_cache", wrapper)


def _fail_second_session_render_record(m: pytest.MonkeyPatch, fired: List[str]) -> None:
    """Lock out the render bookkeeping of the second session file written.

    The render phase, after the refresh finished: that session's file is
    on disk but unrecorded, and the sessions after it are not written at
    all. The commonest site of the lock-outs on a real archive.
    """
    original = CacheManager.update_html_cache

    def wrapper(self: CacheManager, html_path: str, *args: Any, **kwargs: Any) -> Any:
        if html_path.startswith("session-") and "LOCKED" not in fired:
            fired.append(html_path)
            if len(fired) == 2:
                fired.append("LOCKED")
                _locked()
        return original(self, html_path, *args, **kwargs)

    m.setattr(CacheManager, "update_html_cache", wrapper)


@pytest.mark.parametrize(
    "inject",
    [
        _fail_second_file_write,
        _fail_session_rows_write,
        _fail_second_session_render_record,
    ],
    ids=["second-file-write", "session-rows-write", "second-session-render-record"],
)
def test_a_locked_out_refresh_is_redone_on_the_next_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    inject: Callable[[pytest.MonkeyPatch, List[str]], None],
) -> None:
    project = _build_project(tmp_path)
    projects_dir = project.parent
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))

    _run(projects_dir)
    for sid in SESSIONS:
        assert (project / f"session-{sid}.md").exists()

    for sid in SESSIONS:
        _append(project / f"{sid}.jsonl", _turn(sid, 1, _late_marker(sid)))
    _append(
        project / f"{NEW_SESSION}.jsonl",
        _turn(NEW_SESSION, 0, _late_marker(NEW_SESSION)),
    )

    fired: List[str] = []
    capsys.readouterr()
    with monkeypatch.context() as m:
        inject(m, fired)
        _run(projects_dir)
    out = capsys.readouterr().out
    # Positive control: the lock-out happened (once — contention is
    # transient) and reached the pass, which either reported the project
    # or deferred it to its in-pass retry.
    assert fired.count("LOCKED") == 1
    assert "database is locked" in out or "cache busy, will retry" in out, out

    _run(projects_dir)

    stale = [
        sid
        for sid in [*SESSIONS, NEW_SESSION]
        if not (project / f"session-{sid}.md").exists()
        or _late_marker(sid)
        not in (project / f"session-{sid}.md").read_text(encoding="utf-8")
    ]
    assert stale == [], "sessions never re-rendered after the failed refresh"


def test_a_failed_refresh_leaves_every_file_reported_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag that carries the failure from one run to the next."""
    project = _build_project(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "cache.db"))
    _run(project.parent)
    files = sorted(project.glob("*.jsonl"))

    manager = CacheManager(project, get_library_version())
    assert manager.get_modified_files(files) == []

    # An entry write alone — the derived rows not yet recomputed.
    from claude_code_log.converter import load_transcript

    _append(files[0], _turn(files[0].stem, 1, "late"))
    manager.save_cached_entries(files[0], load_transcript(files[0], silent=True))
    assert sorted(manager.get_modified_files(files)) == files

    # A fresh manager (a later run) sees the same.
    later = CacheManager(project, get_library_version())
    assert sorted(later.get_modified_files(files)) == files

    # Recomputing the derived rows is what clears it.
    _run(project.parent)
    assert CacheManager(project, get_library_version()).get_modified_files(files) == []

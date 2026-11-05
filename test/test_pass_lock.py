"""One hierarchy pass per cache file: `pass_lock`.

Two passes over the same cache — a second CLI run, or `serve --watch`
ticking while a CLI pass runs — write through two separate ticket
queues, so to each other they are one tight-loop writer and SQLite's
unfair lock polling applies (issue #332's mechanism). The pass lock makes
the second pass wait for the first instead.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from claude_code_log.pass_lock import (
    ENV_PASS_LOCK,
    PassLockTimeout,
    _wait_budget,
    pass_lock,
    pass_lock_path,
)

_released: Any = None
_held: Any = None


def _init(released: Any, held: Any) -> None:
    global _released, _held
    _released = released
    _held = held


def _hold(db: str, env: dict[str, str]) -> str:
    """Hold the pass lock on ``db`` until the parent sets ``_released``."""
    os.environ.update(env)
    with pass_lock(Path(db)):
        _held.set()
        _released.wait(60)
    return "released"


def _spawn_holder(db_path: Path, env: dict[str, str] | None = None) -> Any:
    ctx = multiprocessing.get_context("spawn")
    released = ctx.Event()
    held = ctx.Event()
    pool = ProcessPoolExecutor(
        max_workers=1, mp_context=ctx, initializer=_init, initargs=(released, held)
    )
    future = pool.submit(_hold, str(db_path), env or {})
    assert held.wait(60), "the holder never took the lock"
    return pool, released, future


def test_a_second_process_waits_for_the_pass_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "cache.db"
    pool, released, future = _spawn_holder(db_path)
    try:
        monkeypatch.setenv(ENV_PASS_LOCK, "0.3")
        started = time.monotonic()
        with pytest.raises(PassLockTimeout) as excinfo:
            with pass_lock(db_path):
                pass
        assert time.monotonic() - started >= 0.3
        assert str(pass_lock_path(db_path)) in str(excinfo.value)

        released.set()
        assert future.result(timeout=60) == "released"
        monkeypatch.delenv(ENV_PASS_LOCK)
        with pass_lock(db_path):
            pass  # free again, and the default (unbounded wait) returns at once
    finally:
        released.set()
        pool.shutdown(wait=True)


def test_the_pass_lock_is_reentrant_in_a_process(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.db"
    with pass_lock(db_path):
        with pass_lock(db_path):
            pass
        # Still held by the outer scope: another process cannot take it.
        conn = sqlite3.connect(pass_lock_path(db_path), timeout=0.1)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("BEGIN EXCLUSIVE")
        finally:
            conn.close()
    # Released with the outer scope.
    conn = sqlite3.connect(pass_lock_path(db_path), timeout=0.1)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        conn.rollback()
    finally:
        conn.close()


def test_the_pass_lock_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CLAUDE_CODE_LOG_PASS_LOCK=0`` restores the pre-lock behaviour:
    nothing is taken, so the file is never created and nobody waits."""
    db_path = tmp_path / "cache.db"
    monkeypatch.setenv(ENV_PASS_LOCK, "0")
    pool, released, future = _spawn_holder(db_path, {ENV_PASS_LOCK: "0"})
    try:
        with pass_lock(db_path):
            pass
        assert not pass_lock_path(db_path).exists()
    finally:
        released.set()
        future.result(timeout=60)
        pool.shutdown(wait=True)


@pytest.mark.parametrize(
    ("value", "enabled", "budget"),
    [
        (None, True, None),
        ("", True, None),
        ("  ", True, None),
        ("0", False, None),
        ("off", False, None),
        ("no", False, None),
        ("false", False, None),
        ("-1", False, None),
        ("2.5", True, 2.5),
        (" 30 ", True, 30.0),
    ],
)
def test_the_environment_sets_the_wait_budget(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    enabled: bool,
    budget: float | None,
) -> None:
    """Unset or empty means the default (the lock, an unbounded wait): an
    empty value is what ``VAR= command`` and a blank config line produce,
    and every other ``CLAUDE_CODE_LOG_*`` switch reads it as unset. ``0``
    and the usual spellings of "off" disable the lock; a positive number
    bounds the wait."""
    if value is None:
        monkeypatch.delenv(ENV_PASS_LOCK, raising=False)
    else:
        monkeypatch.setenv(ENV_PASS_LOCK, value)
    assert _wait_budget() == (enabled, budget)


def test_an_unreadable_wait_budget_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PASS_LOCK, "soon")
    with pytest.raises(ValueError, match="CLAUDE_CODE_LOG_PASS_LOCK='soon'"):
        _wait_budget()


def test_waiting_is_announced_with_the_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "cache.db"
    pool, released, future = _spawn_holder(db_path)
    try:
        monkeypatch.setenv(ENV_PASS_LOCK, "1.0")
        with pytest.raises(PassLockTimeout):
            with pass_lock(db_path, notice_after=0.2):
                pass
    finally:
        released.set()
        future.result(timeout=60)
        pool.shutdown(wait=True)
    out = capsys.readouterr().out
    assert "another claude-code-log pass" in out
    assert str(pass_lock_path(db_path)) in out


def _entry_line(text: str) -> str:
    import json

    return (
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "parentUuid": None,
                "isSidechain": False,
                "userType": "human",
                "cwd": "/tmp",
                "sessionId": "sess-lock",
                "version": "1.0.0",
                "uuid": "uuid-lock",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                },
            }
        )
        + "\n"
    )


def test_a_hierarchy_pass_takes_the_pass_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While another process holds the lock, the pass waits — and with a
    bounded wait, fails without converting anything."""
    from claude_code_log.cache import get_cache_db_path
    from claude_code_log.converter import process_projects_hierarchy

    projects = tmp_path / "projects"
    project = projects / "-home-u-proj"
    project.mkdir(parents=True)
    (project / "session.jsonl").write_text(_entry_line("hello"))
    db_path = get_cache_db_path(projects)

    pool, released, future = _spawn_holder(db_path)
    try:
        monkeypatch.setenv(ENV_PASS_LOCK, "0.3")
        with pytest.raises(PassLockTimeout):
            process_projects_hierarchy(projects, silent=True)
        assert not (projects / "index.html").exists(), "converted without the lock"

        released.set()
        assert future.result(timeout=60) == "released"
    finally:
        released.set()
        pool.shutdown(wait=True)

    monkeypatch.delenv(ENV_PASS_LOCK)
    process_projects_hierarchy(projects, silent=True)
    assert (projects / "index.html").exists()


def _run_pass(projects: str) -> tuple[float, float] | None:
    """Run one hierarchy pass; return the wall-clock window during which it
    held the pass lock (None if it never took one)."""
    from contextlib import contextmanager

    import claude_code_log.pass_lock as pass_lock_module
    from claude_code_log.converter import process_projects_hierarchy

    real = pass_lock_module.pass_lock
    window: list[float] = []

    @contextmanager
    def timed(db_path: Path, **kwargs: Any):
        with real(db_path, **kwargs):
            window.append(time.time())
            try:
                yield
            finally:
                window.append(time.time())

    pass_lock_module.pass_lock = timed  # the converter imports it per call
    process_projects_hierarchy(Path(projects), silent=True)
    return (window[0], window[-1]) if window else None


def test_two_passes_over_one_cache_run_one_after_the_other(tmp_path: Path) -> None:
    """Started together, the two passes hold the cache one after the other."""
    projects = tmp_path / "projects"
    for name in ("-home-u-a", "-home-u-b"):
        project = projects / name
        project.mkdir(parents=True)
        (project / "session.jsonl").write_text(_entry_line(f"text of {name}"))

    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
        futures = [pool.submit(_run_pass, str(projects)) for _ in range(2)]
        results = [f.result(timeout=120) for f in futures]
    assert all(results), f"a pass ran without the lock: {results}"
    windows = sorted(w for w in results if w is not None)
    (_, end_first), (start_second, _) = windows
    assert end_first <= start_second, f"passes overlapped: {windows}"
    assert (projects / "index.html").exists()

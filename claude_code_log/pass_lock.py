"""One hierarchy pass per cache file.

Inside a pass, the project workers share a fair write queue
(`write_queue`), so none of them gives up on a sibling. Two *passes* over
the same cache — a second ``claude-code-log`` run started while one is
going, or ``serve --watch`` ticking while a CLI run refreshes the same
archive — share nothing: each is, to the other, one tight-loop writer,
and SQLite's lock polling is unfair to outsiders (a writer that just
committed tends to re-acquire before a sleeper wakes), so the second pass
can lose projects to ``database is locked`` however patient its own
workers are. It is also the realistic way two refreshes of one project
overlap and clear each other's ``refresh_pending``.

So a pass takes the **pass lock** first: an exclusive transaction on a
sidecar SQLite file next to the cache (``<cache>.pass-lock``), held until
the pass — its WAL trim, its conversions, its checkpoint — is over.
SQLite supplies the cross-platform advisory lock and releases it with
the holder's process, so a killed pass leaves nothing stale to clean up.
Nothing else opens that file. A second pass waits, says so after a
moment, and then runs — usually to find the work done. The wait is
unbounded by default; ``CLAUDE_CODE_LOG_PASS_LOCK=<seconds>`` bounds it
(a pass that runs out of patience fails with `PassLockTimeout`), and
``=0`` disables the lock altogether.

Re-entrant within a process — process-wide, not per thread: a pass
started while this process already holds the lock does not wait on
itself. Nothing nests today (``serve`` finishes its startup pass before
its watch thread starts ticking), so this is a guard against a future
caller, not something the server relies on. The TUI's writes and a
single-file ``convert_jsonl_to`` are short transactions outside any
pass; they keep the ordinary busy timeout and are not gated here.

The lock degrades rather than fails: when the lock file cannot be
created (a read-only directory), is not a SQLite file (junk written
there), or cannot be written (another user's file — a read-only
``BEGIN EXCLUSIVE`` excludes nobody, so holding one would only pretend),
the pass says so, names the file, and runs without the lock.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Dict, Iterator, Optional, Tuple

#: ``0``/``off`` disables the pass lock; a number bounds the wait in seconds.
ENV_PASS_LOCK = "CLAUDE_CODE_LOG_PASS_LOCK"

#: Seconds of waiting before the pass says what it is waiting for.
DEFAULT_NOTICE_AFTER = 2.0

_POLL_SECONDS = 0.25


class PassLockTimeout(RuntimeError):
    """Another pass held the cache for longer than this one may wait."""


def pass_lock_path(db_path: Path) -> Path:
    """The lock file for ``db_path``: beside it, ``<name>.pass-lock``.

    Keyed on the resolved path, so a cache reached through a symlink has
    the same lock as the cache reached directly.
    """
    db_path = Path(db_path).resolve()
    return db_path.with_name(db_path.name + ".pass-lock")


def _wait_budget() -> Tuple[bool, Optional[float]]:
    """``(enabled, seconds)``: seconds is None for an unbounded wait."""
    raw = os.getenv(ENV_PASS_LOCK, "")
    value = raw.strip().lower()
    if not value:
        # Unset, or set to nothing (`VAR= command`): the default, like every
        # other CLAUDE_CODE_LOG_* switch.
        return True, None
    if value in ("off", "false", "no"):
        return False, None
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError(
            f"{ENV_PASS_LOCK}={raw!r}: expected a number of seconds, or 0 to "
            "disable the pass lock"
        ) from None
    if seconds <= 0:
        return False, None
    return True, seconds


# This process's held locks: lock file -> (connection, re-entry depth).
_holders: Dict[str, Tuple[sqlite3.Connection, int]] = {}
_guard = threading.Lock()


def pass_lock_held(db_path: Path) -> bool:
    """Whether this process holds the pass lock for ``db_path``'s cache.

    For tests pinning what runs under the lock; a disabled or degraded
    lock is never "held".
    """
    with _guard:
        return str(pass_lock_path(db_path)) in _holders


def _acquire(
    path: Path,
    budget: Optional[float],
    notice_after: float,
    announce: Callable[[str], None],
) -> sqlite3.Connection:
    from .cache import is_busy_database_error

    # `timeout=0`: SQLite's own busy handler would wait too, but silently;
    # polling here is what lets the pass say what it is waiting for.
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    started = time.monotonic()
    announced = False
    try:
        while True:
            try:
                conn.execute("BEGIN EXCLUSIVE")
                # A file this process cannot write opens read-only, and a
                # read-only exclusive transaction takes no lock at all. One
                # write inside the transaction makes that surface here, as
                # `attempt to write a readonly database`; it is rolled back
                # with the lock, so the file stays empty.
                conn.execute("PRAGMA user_version = 1")
                return conn
            except sqlite3.OperationalError as exc:
                if not is_busy_database_error(exc):
                    raise
            waited = time.monotonic() - started
            if budget is not None and waited >= budget:
                raise PassLockTimeout(
                    f"another claude-code-log pass has held {path} for the "
                    f"{budget:g} s this one was allowed to wait "
                    f"({ENV_PASS_LOCK}); nothing was converted"
                )
            if not announced and waited >= notice_after:
                announce(
                    "another claude-code-log pass is running on this cache "
                    f"({path}); waiting for it to finish"
                )
                announced = True
            remaining = _POLL_SECONDS if budget is None else budget - waited
            time.sleep(max(0.0, min(_POLL_SECONDS, remaining)))
    except BaseException:
        conn.close()
        raise


@contextmanager
def pass_lock(
    db_path: Path,
    *,
    notice_after: float = DEFAULT_NOTICE_AFTER,
    announce: Callable[[str], None] = print,
) -> Iterator[None]:
    """Hold the pass lock for ``db_path``'s cache for the scope.

    Waits for another process's pass to finish (announcing that after
    ``notice_after`` seconds), re-enters freely within this process, and
    is a no-op when disabled by the environment — or when the lock file
    cannot be used (not creatable, not a database, not writable), which
    is announced rather than allowed to fail a pass that could otherwise
    run.
    """
    enabled, budget = _wait_budget()
    if not enabled:
        yield
        return
    path = pass_lock_path(db_path)
    key = str(path)

    with _guard:
        held = _holders.get(key)
        if held is not None:
            _holders[key] = (held[0], held[1] + 1)
    if held is not None:
        try:
            yield
        finally:
            with _guard:
                conn, depth = _holders[key]
                _holders[key] = (conn, depth - 1)
        return

    try:
        conn = _acquire(path, budget, notice_after, announce)
    except sqlite3.DatabaseError as exc:
        # OperationalError (cannot create or write the file) and its parent
        # DatabaseError (`file is not a database`): without the lock, not
        # without the pass.
        announce(
            f"Warning: could not take the pass lock at {path} ({exc}); "
            "running without it"
        )
        yield
        return
    with _guard:
        _holders[key] = (conn, 1)
    try:
        yield
    finally:
        with _guard:
            _holders.pop(key, None)
        try:
            conn.rollback()
        finally:
            conn.close()

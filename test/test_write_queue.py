"""The fair cross-process write queue (`write_queue`, issue #332).

Pins the three properties the hierarchy pass relies on:

- **No giving up on a sibling.** A worker whose sibling holds the write
  lock for longer than the busy timeout waits its turn instead of
  failing — the control run, with no queue installed, fails exactly as
  the issue reports.
- **Fairness.** Writers doing back-to-back transactions all make
  progress; without a queue SQLite's polling busy handler can starve one
  for its whole timeout.
- **No hang on a crash.** A worker that dies holding its turn breaks the
  pool (whose remaining workers are then terminated) rather than leaving
  them queued forever.

Workers run under ``spawn``, as the real pool does, so the functions they
run live at module level.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from claude_code_log.cache import write_transaction
from claude_code_log.write_queue import (
    WriteTurns,
    as_completed_watching_workers,
    install_write_turns,
    write_turn,
)

_started: Any = None


def _init(turns: Optional[WriteTurns], started: Any) -> None:
    global _started
    install_write_turns(turns)
    _started = started


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t (worker INTEGER, payload BLOB)")
    conn.commit()
    conn.close()


def _hold_then_write(db: str, hold_seconds: float) -> str:
    conn = sqlite3.connect(db, timeout=0.2)
    with write_transaction(conn):
        conn.execute("INSERT INTO t VALUES (0, x'00')")
        _started.set()
        time.sleep(hold_seconds)
    conn.close()
    return "held"


def _write_after_holder(db: str) -> str:
    _started.wait(30)
    conn = sqlite3.connect(db, timeout=0.2)
    try:
        with write_transaction(conn):
            conn.execute("INSERT INTO t VALUES (1, x'00')")
    except sqlite3.OperationalError as exc:
        return f"failed: {exc}"
    finally:
        conn.close()
    return "wrote"


def _run_long_holder_pair(db_path: Path, turns: bool) -> List[str]:
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=ctx,
        initializer=_init,
        initargs=(WriteTurns(ctx) if turns else None, started),
    ) as pool:
        holder = pool.submit(_hold_then_write, str(db_path), 1.5)
        waiter = pool.submit(_write_after_holder, str(db_path))
        return [holder.result(timeout=60), waiter.result(timeout=60)]


def test_without_a_queue_a_long_sibling_write_locks_the_other_out(
    tmp_path: Path,
) -> None:
    """Control: the failure from issue #332, with a 0.2 s busy timeout
    standing in for the 30 s one."""
    db = tmp_path / "cache.db"
    _make_db(db)
    holder, waiter = _run_long_holder_pair(db, turns=False)
    assert holder == "held"
    assert waiter.startswith("failed:") and "locked" in waiter


def test_with_the_queue_the_sibling_waits_its_turn(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    _make_db(db)
    assert _run_long_holder_pair(db, turns=True) == ["held", "wrote"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT worker FROM t ORDER BY rowid").fetchall() == [
        (0,),
        (1,),
    ]
    conn.close()


def _hammer(db: str, worker: int, seconds: float) -> Tuple[int, float]:
    """Back-to-back short write transactions, like a worker saving file
    after file. Returns (transactions done, worst wait for a turn)."""
    _started.wait(30)
    conn = sqlite3.connect(db, timeout=60)
    payload = os.urandom(1000)
    done, worst = 0, 0.0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        asked = time.monotonic()
        with write_transaction(conn):
            worst = max(worst, time.monotonic() - asked)
            conn.executemany("INSERT INTO t VALUES (?, ?)", [(worker, payload)] * 500)
        done += 1
    conn.close()
    return done, worst


def test_the_queue_shares_turns_fairly(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    _make_db(db)
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    workers = 4
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=_init,
        initargs=(WriteTurns(ctx), started),
    ) as pool:
        futures = [pool.submit(_hammer, str(db), i, 3.0) for i in range(workers)]
        # Let every worker reach the starting line before any begins.
        time.sleep(1.0)
        started.set()
        results = [f.result(timeout=120) for f in futures]

    counts = [done for done, _worst in results]
    assert min(counts) > 0, results
    # A ticket lock hands out turns round-robin, so every worker does
    # about the same number; starvation shows up as a gross imbalance.
    assert min(counts) >= max(counts) // 2, results


def _die_holding_a_turn() -> None:
    with write_turn():
        _started.set()
        os._exit(1)


def _wait_for_a_turn() -> str:
    _started.wait(30)
    with write_turn():
        return "got a turn"


def test_a_worker_dying_with_its_turn_breaks_the_pool_instead_of_hanging() -> None:
    """The worker holding the turn dies; its sibling is queued behind it.

    Drained with plain ``as_completed`` this hangs more often than not on
    CPython 3.11: the executor misses the death of a worker it spawned on
    demand, and the only other result can never arrive. The watching
    drain must see it, terminate the queued sibling, and raise.
    """
    ctx = multiprocessing.get_context("spawn")
    started = ctx.Event()
    began = time.monotonic()
    with pytest.raises(BrokenProcessPool):
        with ProcessPoolExecutor(
            max_workers=2,
            mp_context=ctx,
            initializer=_init,
            initargs=(WriteTurns(ctx), started),
        ) as pool:
            futures = [pool.submit(_wait_for_a_turn), pool.submit(_die_holding_a_turn)]
            for future in as_completed_watching_workers(pool, futures):
                future.result()
    assert time.monotonic() - began < 30


def test_write_turn_is_reentrant_and_free_without_a_queue() -> None:
    install_write_turns(None)
    with write_turn():
        with write_turn():
            pass

    ctx = multiprocessing.get_context("spawn")
    install_write_turns(WriteTurns(ctx))
    try:
        # A nested turn on the same thread must not queue behind itself.
        with write_turn():
            with write_turn():
                pass
        with write_turn():
            pass
    finally:
        install_write_turns(None)


def test_write_transaction_nests_and_rolls_back(tmp_path: Path) -> None:
    db = tmp_path / "cache.db"
    _make_db(db)
    conn = sqlite3.connect(db)
    with write_transaction(conn):
        conn.execute("INSERT INTO t VALUES (1, x'00')")
        with write_transaction(conn):
            conn.execute("INSERT INTO t VALUES (2, x'00')")
        # The inner scope joined the outer transaction rather than
        # committing half of it.
        assert conn.in_transaction
    assert not conn.in_transaction

    with pytest.raises(RuntimeError):
        with write_transaction(conn):
            conn.execute("INSERT INTO t VALUES (3, x'00')")
            raise RuntimeError
    assert not conn.in_transaction
    rows = conn.execute("SELECT worker FROM t ORDER BY rowid").fetchall()
    assert rows == [(1,), (2,)]
    conn.close()

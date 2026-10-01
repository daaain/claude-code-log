"""A fair, cross-process queue for the cache database's one write lock.

SQLite lets one connection write at a time, and a connection that wants
the lock while another holds it *polls*: its busy handler sleeps with a
growing back-off and retries, and whoever happens to be awake when the
lock frees takes it. Nothing is queued. With several project workers each
committing file after file, the worker that just committed is usually the
one that wins again, and a sleeper can lose every round until its busy
timeout expires — issue #332 lost 23 of 57 projects to ``database is
locked`` this way. Measured with eight processes doing back-to-back short
write transactions for 20 s, the worst-off one waited the full 20 s for a
single turn; no individual transaction was anywhere near that long. A
plain cross-process mutex was no better (one worker got a single
transaction in 20 s): the releasing process re-acquires before a woken
waiter runs.

`WriteTurns` is a ticket lock instead: take a number, wait to be called.
The same measurement under it gave every worker 90-91 transactions and a
worst wait of 0.41 s. The hierarchy pass creates one per process pool and
installs it in every project worker (`install_write_turns`, the pool's
initializer); `CacheManager` takes a turn around each write transaction
via `write_turn`. Within a pass, then, a worker never gives up on a
sibling: it waits its turn, which is bounded by the transactions queued
ahead of it. SQLite's busy timeout still applies while a turn holder
waits for the lock itself, so a writer in *another* process (a TUI, a
second ``serve``) is handled exactly as before.

Outside a pool — the inline path, the TUI, single-file conversions —
nothing is installed and `write_turn` is free.

A worker that dies while holding a turn (the OOM killer is the realistic
cause) leaves the queue stuck: its siblings wait for a turn that never
comes. ``ProcessPoolExecutor`` is meant to notice a dead worker, mark the
pool broken and terminate the rest — but under ``spawn`` it starts workers
on demand, and its manager thread can be left waiting on a list of worker
sentinels that predates the one that died (reproduced on CPython 3.11:
the dead worker shows its exit code, the pool reports itself healthy). It
then notices only when some other result arrives, which with every
sibling queued behind the dead turn is never. Before the queue that was a
late diagnosis; with it, it would be a hang. So the pool is drained
through `as_completed_watching_workers`, which checks the workers itself:
on any abrupt exit it terminates the rest and raises `BrokenProcessPool`,
and the parent falls back to converting inline, where no turns are
installed. ``test_write_queue.py`` pins both the hazard and the cure.
"""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from multiprocessing.context import BaseContext
from multiprocessing.process import BaseProcess
from typing import Any, Dict, Generator, Iterable, Iterator, Optional


class WriteTurns:
    """FIFO cross-process lock: take a number, wait to be called.

    Built from a multiprocessing context's primitives, so it crosses a
    ``spawn`` boundary as a process argument (a pool ``initargs``), not
    through a queue. Not reentrant on its own; `write_turn` adds that.
    """

    def __init__(self, ctx: BaseContext) -> None:
        self._cond = ctx.Condition()
        # Unsynchronised shared counters: both are only touched with
        # `_cond`'s lock held, which is the synchronisation.
        self._next_ticket = ctx.RawValue("q", 0)
        self._now_serving = ctx.RawValue("q", 0)

    def acquire(self) -> None:
        with self._cond:
            ticket = self._next_ticket.value
            self._next_ticket.value += 1
            while self._now_serving.value != ticket:
                self._cond.wait()

    def release(self) -> None:
        with self._cond:
            self._now_serving.value += 1
            self._cond.notify_all()


_installed: Optional[WriteTurns] = None
_held = threading.local()


def install_write_turns(turns: Optional[WriteTurns]) -> None:
    """Make ``turns`` this process's write queue (None removes it).

    The project pool's ``initializer``: each worker installs the queue
    its parent created, so all of them share one.
    """
    global _installed
    _installed = turns


@contextmanager
def write_turn() -> Generator[None, None, None]:
    """Hold this process's turn to write for the scope, if a queue is installed.

    Reentrant per thread: a write nested inside another on the same thread
    already holds the turn, and waiting for a second one behind itself
    would never end.
    """
    turns = _installed
    depth: int = getattr(_held, "depth", 0)
    if turns is None or depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth = depth
        return
    turns.acquire()
    _held.depth = 1
    try:
        yield
    finally:
        _held.depth = 0
        turns.release()


def as_completed_watching_workers(
    pool: ProcessPoolExecutor,
    futures: Iterable["Future[Any]"],
    *,
    poll_seconds: float = 0.5,
) -> Iterator["Future[Any]"]:
    """``as_completed`` that also notices a worker dying, and says so.

    Yields each future as it finishes. Between results it looks at the
    pool's worker processes; a worker only exits before shutdown if it was
    killed or crashed, so any exit code there means one is gone. Then the
    surviving workers are terminated — they may be queued forever behind
    a turn the dead one held — and `BrokenProcessPool` is raised, as the
    executor itself would have raised had it noticed. See the module
    docstring for why it may not.

    Reads the executor's ``_processes``, which every supported CPython
    has; if that ever goes away this degrades to plain ``as_completed``.
    """
    pending = set(futures)
    while pending:
        done, pending = wait(pending, timeout=poll_seconds, return_when=FIRST_COMPLETED)
        yield from done
        if not pending:
            return
        workers: Dict[int, BaseProcess] = getattr(pool, "_processes", None) or {}
        processes = list(workers.values())
        dead = [p for p in processes if p.exitcode is not None]
        if dead:
            for process in processes:
                if process.exitcode is None:
                    process.terminate()
            raise BrokenProcessPool(
                f"a worker process exited abruptly (exit code {dead[0].exitcode}) "
                f"with {len(pending)} task(s) unfinished"
            )

#!/usr/bin/env python3
"""Benchmark the migration runner's connection pragmas.

Times `run_migrations` over N fresh cache databases under four pragma
arms, to answer one question: how much does the runner's own connection
cost when it is *not* configured as the writer it is?

    uv run python scripts/bench_migration_pragmas.py

It needs no data — every arm builds throwaway databases from the
migration chain alone — so it runs anywhere in a few seconds. That is
why it is a separate script rather than part of `bench_render.py`, which
copies a real projects tree.

Why this is worth keeping: the fix it measures
(`migrations.runner.apply_write_pragmas`, documented in
`dev-docs/application_model.md` § cache connections) rests on a ~10x
figure quoted in more than one place, and a figure that outlives its
method gets re-derived or quietly doubted.

## Reading the arms

    default             what the runner did before the fix: `foreign_keys`
                        only, so SQLite's defaults — delete journal,
                        `synchronous=FULL`, an fsync per write transaction.
    journal_mode=WAL    WAL alone. **Not the fix**, and the interesting
                        row: `journal_mode` persists in the database file,
                        but `synchronous` is per-connection and stays at
                        FULL, so most of the fsync cost survives while the
                        journal mode makes it look done.
    WAL + NORMAL        the shipped pairing.
    synchronous=OFF     the floor, for scale only — not a proposal.

The baseline arm is produced by substituting `apply_write_pragmas` with a
no-op, which is exactly what the pre-fix runner did; the arms are not
re-implementations of the runner.

Every row is self-verifying: after the timed loop each arm's pragma
function is applied to one more connection and both pragmas are read
back, so the reported `journal_mode`/`synchronous` are what the arm
actually achieved, not what it tried to set. A row whose timing moved
but whose pragmas did not is a broken arm, not a finding.

## Controls

The cost being measured is fsync, not CPU. Two ways to see that:

    # tmpfs has no fsync to pay: every arm should collapse to the
    # fast arm's time.
    uv run python scripts/bench_migration_pragmas.py --tmpdir /dev/shm

    # arm order is fixed across repeats, so a systematic ordering
    # artifact would not show up as variance. Reversing must not
    # change the ranking.
    uv run python scripts/bench_migration_pragmas.py --reverse-arms

Expect the absolute numbers to move with your disk, and expect the gap
to *widen* under concurrent load: fsync cost scales with device
contention, so a single-threaded run like this one understates the win
during a parallel test suite.
"""

import argparse
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from claude_code_log.migrations import runner

PragmaFn = Callable[[sqlite3.Connection], None]

# 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
_SYNCHRONOUS_NAMES = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}


def _no_pragmas(conn: sqlite3.Connection) -> None:
    """The pre-fix runner: `foreign_keys` only, set by run_migrations itself."""


def _wal_only(conn: sqlite3.Connection) -> None:
    """The trap arm: WAL without the pairing that makes it pay off."""
    conn.execute("PRAGMA journal_mode = WAL")


def _synchronous_off(conn: sqlite3.Connection) -> None:
    """The floor arm: no fsync at all. For scale, not as a proposal."""
    conn.execute("PRAGMA synchronous = OFF")


def _arms() -> List[Tuple[str, PragmaFn]]:
    """The arms, in reporting order. `apply_write_pragmas` is the real one."""
    return [
        ("default (pre-fix)", _no_pragmas),
        ("journal_mode=WAL only", _wal_only),
        ("WAL + synchronous=NORMAL", runner.apply_write_pragmas),
        ("synchronous=OFF only", _synchronous_off),
    ]


def _observed_pragmas(db_path: Path, pragma_fn: PragmaFn) -> Tuple[str, str]:
    """Apply an arm to a real connection and read both pragmas back.

    `synchronous` is per-connection and unobservable once the handle is
    closed, so it has to be read from a connection the arm configured —
    reading it from a fresh default connection would report FULL for every
    arm and look like the arms had no effect.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        pragma_fn(conn)
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        raw = int(conn.execute("PRAGMA synchronous").fetchone()[0])
    finally:
        conn.close()
    return journal_mode, _SYNCHRONOUS_NAMES.get(raw, str(raw))


def _time_arm(
    pragma_fn: PragmaFn, databases: int, tmpdir: Optional[Path]
) -> Tuple[float, str, str]:
    """Return (ms per database, journal_mode, synchronous) for one arm."""
    original = runner.apply_write_pragmas
    # Substituting the runner's own pragma function is the point of the
    # benchmark: `run_migrations` looks the name up in module globals at
    # call time, so each arm swaps what the real runner applies rather
    # than re-implementing it. setattr, because ty reads a plain
    # assignment here as an accidental shadowing of the function.
    setattr(runner, "apply_write_pragmas", pragma_fn)
    try:
        with tempfile.TemporaryDirectory(dir=tmpdir) as td:
            root = Path(td)
            # Warm up outside the timed loop: the first database in a fresh
            # directory pays costs (imports, directory metadata) that would
            # otherwise be charged to whichever arm runs first.
            runner.run_migrations(root / "warmup.db")

            started = time.perf_counter()
            for i in range(databases):
                runner.run_migrations(root / f"db{i}.db")
            elapsed = time.perf_counter() - started

            journal_mode, synchronous = _observed_pragmas(root / "db0.db", pragma_fn)
    finally:
        setattr(runner, "apply_write_pragmas", original)
    return elapsed / databases * 1000, journal_mode, synchronous


def main() -> None:
    """Parse arguments, time every arm per repeat, report medians."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--databases",
        type=int,
        default=20,
        help="fresh databases migrated per arm per repeat (default: 20)",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="times to run the whole set of arms (default: 3)",
    )
    parser.add_argument(
        "--tmpdir",
        type=Path,
        default=None,
        help=(
            "where to build the throwaway databases (default: the system "
            "temp dir). Point it at a tmpfs such as /dev/shm for the "
            "no-fsync control."
        ),
    )
    parser.add_argument(
        "--reverse-arms",
        action="store_true",
        help="run the arms in reverse order — the ordering-artifact control",
    )
    args = parser.parse_args()

    # Both are divisors: `--databases 0` divides by zero in _time_arm, and
    # `--repeats 0` leaves statistics.median() no samples. Fail through the
    # parser rather than deep in a benchmark that has already run.
    if args.databases < 1:
        parser.error("--databases must be at least 1")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    arms = _arms()
    if args.reverse_arms:
        arms = list(reversed(arms))

    where = args.tmpdir if args.tmpdir is not None else Path(tempfile.gettempdir())
    print(
        f"{args.databases} fresh databases per arm, {args.repeats} repeats, "
        f"under {where}"
    )
    print()

    timings: Dict[str, List[float]] = {name: [] for name, _ in arms}
    pragmas: Dict[str, Tuple[str, str]] = {}
    for repeat in range(1, args.repeats + 1):
        print(f"repeat {repeat}")
        for name, pragma_fn in arms:
            ms, journal_mode, synchronous = _time_arm(
                pragma_fn, args.databases, args.tmpdir
            )
            timings[name].append(ms)
            pragmas[name] = (journal_mode, synchronous)
            print(
                f"  {name:26s} {ms:7.1f} ms/db   "
                f"journal_mode={journal_mode}, synchronous={synchronous}"
            )
        print()

    print("median ms/db")
    medians = {name: statistics.median(values) for name, values in timings.items()}
    fastest = min(medians.values())
    for name, _ in arms:
        journal_mode, synchronous = pragmas[name]
        print(
            f"  {name:26s} {medians[name]:7.1f}   "
            f"{medians[name] / fastest:5.1f}x the fastest arm   "
            f"journal_mode={journal_mode}, synchronous={synchronous}"
        )


if __name__ == "__main__":
    main()

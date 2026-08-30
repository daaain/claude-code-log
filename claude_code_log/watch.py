"""Watch a project's transcripts and re-convert as they grow.

The engine is deliberately small and knows nothing about HTTP, the CLI,
or rendering. It answers one question on a timer — *might something have
changed?* — debounces the answer, and calls a callback. Everything else
is the callback's problem.

That division is the point. The watcher's scan is a cheap trigger, not a
source of truth: a false positive costs one no-op conversion (~0.2s on a
warm cache), while the conversion itself already knows precisely what is
stale. Duplicating the cache's freshness semantics out here would mean
two implementations that could disagree, and the cache is the one that
gets it right — since migration 011 it compares size as well as mtime,
so it no longer misses an append that lands inside the mtime tolerance.

Why polling rather than inotify/FSEvents: no dependency, works on network
filesystems, and at watch scope (one project, a few dozen files) a scan
is one `scandir` per directory. The latency floor that matters is the
conversion's, not the detector's.

Testability comes from injecting the clock and from splitting "poll"
(`tick`) from "wait" (`run`). Unit tests call `tick()` by hand against a
fake clock and never sleep; a watcher tested with real timing is a
flaky-test generator.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# One poll every quarter second. Below the debounce quiet period, so the
# debounce (not the poll rate) decides latency.
DEFAULT_POLL_INTERVAL = 0.25

# Claude Code appends several entries per turn, each landing within a
# second or so of the last. Without a quiet period every one of them
# would trigger its own conversion, and most of a turn would be spent
# rendering states nobody sees.
DEFAULT_QUIET_PERIOD = 0.3

# ...but a long unbroken stream of appends must still surface. This caps
# how long a change can sit undelivered while its neighbours keep
# resetting the quiet period.
DEFAULT_MAX_LATENCY = 2.0

# Files whose content feeds a render. Agent sidecars are included because
# spawn discovery reads them, and one can appear without the trunk
# transcript being touched at all (#213).
WATCHED_GLOBS = ("**/*.jsonl", "**/agent-*.meta.json")

# The watcher writes generated output into the tree it is watching, and
# atomic writes leave a `.name.pid.tmp` file there for an instant. Seeing
# our own output as a change would make the loop feed itself forever.
IGNORED_PREFIXES = (".",)


FileStamp = tuple[int, int]
"""(size, mtime_ns) — the pair a change has to preserve to go unnoticed."""


def scan(roots: Iterable[Path]) -> dict[Path, FileStamp]:
    """Stamp every watched file under `roots`.

    Missing files are simply absent from the result, which makes deletion
    a change like any other. A file that vanishes mid-scan is skipped
    rather than raising: the next tick will see the settled state.
    """
    stamps: dict[Path, FileStamp] = {}
    for root in roots:
        for pattern in WATCHED_GLOBS:
            for path in root.glob(pattern):
                if path.name.startswith(IGNORED_PREFIXES):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                stamps[path] = (st.st_size, st.st_mtime_ns)
    return stamps


@dataclass
class WatchStats:
    """Counters worth surfacing when someone asks why nothing happened."""

    polls: int = 0
    changes_seen: int = 0
    conversions: int = 0
    errors: int = 0


@dataclass
class _Pending:
    paths: set[Path] = field(default_factory=set[Path])
    first_seen: float = 0.0
    last_seen: float = 0.0


class WatchEngine:
    """Poll `roots`, debounce, and call `on_change` with the changed paths.

    `on_change` receives the set of paths that changed since the last
    delivery. It is called on the engine's own thread, one call at a
    time — never concurrently with itself — so it does not need to be
    reentrant, and a slow conversion simply delays the next poll rather
    than piling up.
    """

    def __init__(
        self,
        roots: Iterable[Path],
        on_change: Callable[[set[Path]], None],
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        quiet_period: float = DEFAULT_QUIET_PERIOD,
        max_latency: float = DEFAULT_MAX_LATENCY,
        clock: Callable[[], float] = time.monotonic,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self.roots = [Path(r) for r in roots]
        self.on_change = on_change
        self.poll_interval = poll_interval
        self.quiet_period = quiet_period
        self.max_latency = max_latency
        self._clock = clock
        self._on_error = on_error
        self.stats = WatchStats()
        self._stamps: dict[Path, FileStamp] = {}
        self._pending = _Pending()
        self._primed = False

    # ---- state ----------------------------------------------------------

    def prime(self) -> None:
        """Adopt the current tree as the baseline, without firing.

        Called once before the loop so an existing archive isn't reported
        as one enormous change on the first tick.
        """
        self._stamps = scan(self.roots)
        self._primed = True

    @property
    def pending_paths(self) -> frozenset[Path]:
        return frozenset(self._pending.paths)

    # ---- the unit of work -----------------------------------------------

    def tick(self) -> Optional[set[Path]]:
        """Poll once. Returns the delivered paths, or None if nothing fired.

        Separating "poll" from "wait" is what makes this testable: tests
        drive `tick()` against a fake clock and never sleep.
        """
        if not self._primed:
            self.prime()

        self.stats.polls += 1
        now = self._clock()

        stamps = scan(self.roots)
        changed = {
            path for path, stamp in stamps.items() if self._stamps.get(path) != stamp
        }
        changed |= set(self._stamps) - set(stamps)  # deletions
        self._stamps = stamps

        if changed:
            self.stats.changes_seen += len(changed)
            if not self._pending.paths:
                self._pending.first_seen = now
            self._pending.paths |= changed
            self._pending.last_seen = now

        if not self._pending.paths:
            return None

        quiet_enough = now - self._pending.last_seen >= self.quiet_period
        waited_long_enough = now - self._pending.first_seen >= self.max_latency
        if not (quiet_enough or waited_long_enough):
            return None

        delivered = self._pending.paths
        self._pending = _Pending()
        self.stats.conversions += 1
        try:
            self.on_change(set(delivered))
        except BaseException as exc:  # noqa: BLE001 - reported, never fatal
            # One bad conversion must not end the watch. A transcript can
            # be mid-write, a disk can fill, a plugin can throw; the next
            # tick usually succeeds, and a dead watcher is worse than a
            # skipped render.
            self.stats.errors += 1
            if self._on_error is None:
                raise
            self._on_error(exc)
        return set(delivered)

    # ---- the loop -------------------------------------------------------

    def run(self, stop: Optional[threading.Event] = None) -> None:
        """Tick until `stop` is set (or forever).

        Primes only if the caller hasn't. Priming unconditionally here
        would make the baseline moment depend on when this thread got
        scheduled, so a change landing between `run_in_thread` returning
        and this line would be absorbed into the baseline and never
        reported. Callers that care — anything that starts the watch and
        then does something observable — should `prime()` first.
        """
        stop = stop or threading.Event()
        if not self._primed:
            self.prime()
        while not stop.is_set():
            self.tick()
            # Event.wait doubles as the sleep so a stop is instant rather
            # than up to one poll interval late.
            if stop.wait(self.poll_interval):
                break

    def run_in_thread(self, stop: threading.Event) -> threading.Thread:
        """Start `run` on a daemon thread and return it."""
        thread = threading.Thread(
            target=self.run, args=(stop,), name="claude-code-log-watch", daemon=True
        )
        thread.start()
        return thread

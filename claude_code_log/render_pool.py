#!/usr/bin/env python3
"""Intra-project render fan-out.

Why this exists
---------------
``process_projects_hierarchy`` already fans whole projects out over a
process pool, but a project's *own* conversion is single-threaded. That
leaves two gaps:

1. **The tail.** With N workers the all-projects wall clock is bounded by
   the largest single project, which runs on one core while the rest of
   the pool drains. Measured on 5 real projects across 4 cores: 195s of
   work, 65.0s wall — and 65.0s is exactly the largest project's own time.
   Average parallelism was 2.18 of 4 cores.
2. **Incremental runs.** A day-to-day run has one or two stale projects,
   so only one or two workers ever start.

Both are fixed by the same thing: rendering *within* a project is
embarrassingly parallel. A conversion writes one file per combined page
and one per session — 88 independent units for the project measured above
— and each is a pure function of the loaded transcript plus its own
metadata.

Design
------
Workers are **fed, not self-sufficient**. Each unit crosses the process
boundary carrying exactly what its render reads: the unit's own entry
slice (a page's sessions, or one session's messages — the same objects
the parent's master list holds), those entries' master-list ordinals
(so fragment-store keys mean the same thing in every process), and — for
session units — the session's slice of the parent's fragment store
(``RenderUnit.fed_fragments``), formatted by the combined-page pass that
always completes first. The one per-*worker* piece of state is a slimmed
``SessionTree`` (``dag.slim_session_tree``) sent through the pool
initializer: the render path reads only its DAG lines, junction points
and workflow dicts, so the per-message ``nodes`` mapping — whose pickled
size is the whole transcript — stays behind in the parent.

Workers previously re-loaded the whole transcript from the SQLite cache
instead (once per worker, ~0.7s at 12k messages, ~12s of CPU at 329MB of
transcript), which made per-worker cost — CPU *and* the ~3x-bytes-on-disk
resident copy — scale with project size rather than unit size. Feeding
moves each entry at most twice per conversion (its page unit, then its
session unit) rather than once per worker, and what a worker holds is
bounded by its largest single unit.

Every fed fragment is verified the same way as any store hit (content
digest + tree signature), so a stale or mis-keyed fragment costs a
recompute, never wrong output. Staleness checks and all cache writes stay
in the parent — workers render and write output files, nothing else — so
the DB keeps a single writer (workers still open the cache read-only for
the per-session combined-link lookup).

The pool is created lazily on first use: a project with one stale session
should not pay ~1s of ``spawn`` + import per worker to save 60ms.
"""

import multiprocessing
import os
import re
import subprocess
import sys
import traceback
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .models import RenderingDepth, TranscriptEntry

__all__ = [
    "RENDER_JOBS_ENV",
    "RenderPool",
    "RenderUnit",
    "available_memory_bytes",
    "memory_capped_workers",
    "resolve_render_jobs",
]


@dataclass
class RenderUnit:
    """One output file to render.

    ``kind`` is ``"page"`` or ``"session"``; ``key`` identifies the unit
    within its kind (page number / session id) and is echoed back in the
    result so the parent can attribute cache writes and failures.
    """

    kind: str
    key: Any
    file_name: str
    title: str
    # The unit's slice of the parent's master message list — a page's
    # sessions' entries, or one session's (trunk + integrated agents).
    # This is what the worker renders; it never loads the transcript
    # itself. None only for inline rendering, which reads the parent's
    # own lists instead.
    entries: "Optional[List[TranscriptEntry]]" = None
    # Master-list ordinal of each entry in ``entries`` (parallel list).
    # Fragment-store keys are these ordinals, so the parent and every
    # worker agree on what a key names without holding the same list.
    entry_ordinals: Optional[List[int]] = None
    # Page units: the session ids whose messages make up the page (kept
    # for the parent's cache bookkeeping; workers render ``entries``).
    session_ids: Optional[List[str]] = None
    page_info: Optional[Dict[str, Any]] = None
    page_stats: Optional[Dict[str, Any]] = None
    suppress_combined_link: bool = False
    # Session units: this session's slice of the parent's fragment store
    # (digests + strings, picklable), formatted by the combined-page pass
    # that always completes before sessions dispatch. The worker absorbs
    # it into its own store so the session render reuses instead of
    # re-formatting. Purely an optimisation: a stale, missing or
    # mis-keyed slice degrades to a digest conflict or a miss — formatted
    # fresh — never to wrong output.
    fed_fragments: Optional[Dict[Any, Any]] = None


@dataclass
class _WorkerSetup:
    """Everything a worker needs to reconstruct the parent's render state.

    Everything here must survive a ``spawn`` pickle. ``session_tree`` is
    the *slim* tree (``dag.slim_session_tree``) — its per-message
    ``nodes`` mapping raises on lookup, so it must never be the full one:
    that would ship the whole transcript to every worker.
    """

    format: str
    project_dir: str
    output_dir: str
    from_date: Optional[str]
    to_date: Optional[str]
    depth: "RenderingDepth"
    compact: bool
    no_timestamps: bool
    no_recaps: bool
    image_export_mode: Optional[str]
    archive_search_link: Optional[str]
    library_version: str
    session_tree: Any = None


RENDER_JOBS_ENV = "CLAUDE_CODE_LOG_RENDER_JOBS"


def resolve_render_jobs(requested: Optional[int]) -> int:
    """How many workers to render this project's output files over.

    ``1`` means the inline path — no pool, no worker processes.

    **On by default** at the CPU count, since measuring it on a 16-core
    machine settled the question: an incremental run (one stale project,
    the everyday shape) went 93.2s → 34.6s, a 2.7x improvement using 10.6
    of 16 cores instead of 1. ``$RENDER_JOBS_ENV`` overrides — ``1``
    disables it, ``auto`` is the default, an integer pins a worker count.

    The cost is total CPU: workers start with cold memo caches and redo
    the page-vs-session formatting the memo would have collapsed, so that
    run burned 367.9s of CPU against 90.8s serial. Wall clock is what the
    user waits for, so the trade is worth making by default — but it is
    why ``_MIN_MESSAGES_FOR_RENDER_POOL`` keeps small projects out (below
    the crossover the fan-out is a net loss) and why the count is capped
    against available memory. See ``application_model.md`` § 2.10.

    ``requested`` is an explicit caller override (the ``render_jobs``
    argument to ``convert_jsonl_to``) and wins over the environment;
    ``None`` consults it. A number below 1 means "none of it" (mirroring
    ``CLAUDE_CODE_LOG_RENDER_CACHE_MB=0``); an unparseable setting falls
    back to the default rather than crashing a conversion.
    """
    if requested is not None:
        return max(1, requested)

    def _default() -> int:
        return max(1, os.cpu_count() or 1)

    raw = os.getenv(RENDER_JOBS_ENV)
    if raw is None:
        return _default()
    value = raw.strip().lower()
    if not value:
        return _default()
    if value in ("auto", "cpu"):
        return _default()
    if value in ("off", "no", "false", "serial"):
        return 1
    try:
        return max(1, int(value))
    except ValueError:
        return _default()


# Post-feeding footprints, measured 2026-08-26 on the 8-core/16GB dev VM
# (the work/render-format-once.md § 7.5 re-measure) by polling VmHWM
# across fanned-out conversions of two real projects — 140MB/47k-message
# and 329MB/97k-message:
#
#   conversion parent   598MB / 1458MB  ->  ~4.4x transcript bytes
#   largest worker      218MB /  329MB  ->  ~136MB + 0.59x transcript
#
# The parent is the heavyweight: the JSONL becomes Pydantic entries, a
# TemplateMessage tree and a SessionTree (~3x on its own), plus the
# fragment store's text and the in-flight pickled slices. Fed workers
# hold only base imports, their memo caches and the in-flight unit's
# entry slice, so their cost scales weakly with project size. The
# charges below carry a ~1.2-1.35x margin over the measured fit. The
# pathological case — one session spanning most of the transcript, whose
# unit slice re-inflates toward the parent's ~3x inside its worker — is
# deliberately not charged for: it is a single outlier that fits inside
# the headroom fraction, and such a project yields too few units to fan
# wide anyway.
_PARENT_RSS_PER_TRANSCRIPT_BYTE = 4.5
_WORKER_RSS_PER_TRANSCRIPT_BYTE = 0.8

# Interpreter, imports, Pygments' lexer tables and the render memo caches,
# before any unit arrives. Measured worker intercept was ~136MB; 150MB
# leaves room for a Pygments-heavy project to fill more of its memo budget
# without making the estimate so pessimistic that small projects never get
# a worker. Charged to the parent too (same interpreter, same caches).
_WORKER_BASE_BYTES = 150 * 1024 * 1024

# Never hand the whole of available memory to workers — the parent still
# holds its own copy of the transcript and keeps rendering alongside them.
_MEMORY_HEADROOM_FRACTION = 0.6


# Pages `vm_stat` reports that can be handed to a new process without
# pushing anything to swap. "inactive" and "speculative" are clean, cheaply
# reclaimable page cache — excluding them reports a busy Mac as having
# almost nothing free, which is exactly the under-read that capped a 16-core
# machine at the 2-worker fallback.
_DARWIN_RECLAIMABLE_PAGE_KINDS = ("free", "inactive", "speculative", "purgeable")

_VM_STAT_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE_RE = re.compile(r'^"?Pages ([A-Za-z -]+?)"?:\s+(\d+)\.?\s*$')


def _parse_vm_stat(output: str) -> Optional[int]:
    """Turn ``vm_stat`` output into a reclaimable byte count.

    Split out from the subprocess call so it can be tested off-macOS.
    """
    page_size_match = _VM_STAT_PAGE_SIZE_RE.search(output)
    # vm_stat's own default when the header is missing; every current macOS
    # states it explicitly (4096 on Intel, 16384 on Apple silicon).
    page_size = int(page_size_match.group(1)) if page_size_match else 4096

    pages = 0
    matched = False
    for line in output.splitlines():
        match = _VM_STAT_LINE_RE.match(line.strip())
        if match and match.group(1).strip() in _DARWIN_RECLAIMABLE_PAGE_KINDS:
            pages += int(match.group(2))
            matched = True
    if not matched:
        return None
    return pages * page_size


def _darwin_available_bytes() -> Optional[int]:
    """Reclaimable memory on macOS, or None off-macOS / on any failure.

    macOS has no ``MemAvailable`` and no ``SC_AVPHYS_PAGES``, so this shells
    out to ``vm_stat`` (present on every macOS, no dependency needed). Any
    failure returns None and the caller falls back to its conservative
    unknown-memory behaviour.
    """
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return _parse_vm_stat(completed.stdout)


def _windows_available_bytes() -> Optional[int]:
    """Available physical memory on Windows, or None elsewhere / on failure.

    Windows has no ``/proc/meminfo`` and no ``os.sysconf`` at all, so
    without this it lands in the same conservative unknown-memory branch
    that capped macOS at 2 workers. ``GlobalMemoryStatusEx`` is the
    documented API and reachable through ``ctypes``, so this needs no
    dependency; ``ullAvailPhys`` is what the OS considers immediately
    available, which is the same thing the other probes estimate.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        # windll exists only on Windows, which the platform check above
        # has already established.
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
            ctypes.byref(status)
        ):
            return None
        return int(status.ullAvailPhys) or None
    except Exception:
        # ctypes is optional in some builds and the call is a foreign
        # function into the OS; an unknown reading is recoverable (the
        # caller just stays conservative), a crash mid-conversion is not.
        return None


def available_memory_bytes() -> Optional[int]:
    """Best-effort read of memory we may actually use, or None if unknown.

    Checks the cgroup limit first: inside a container the host's totals are
    a lie, and this is exactly where an over-eager fan-out gets the whole
    machine OOM-killed or swap-thrashed into unresponsiveness.
    """
    limits: list[int] = []

    # cgroup v2 (containers, systemd slices).
    try:
        with open("/sys/fs/cgroup/memory.max") as handle:
            raw = handle.read().strip()
        if raw != "max":
            with open("/sys/fs/cgroup/memory.current") as handle:
                current = int(handle.read().strip())
            limits.append(max(0, int(raw) - current))
    except (OSError, ValueError):
        pass

    # Linux: MemAvailable accounts for reclaimable page cache, which
    # SC_AVPHYS_PAGES (free pages only) badly under-reports.
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    limits.append(int(line.split()[1]) * 1024)
                    break
    except (OSError, ValueError, IndexError):
        pass

    if not limits:
        # Exactly one of these can return a reading on any given machine;
        # both return None elsewhere.
        for probe in (_darwin_available_bytes, _windows_available_bytes):
            reading = probe()
            if reading is not None:
                limits.append(reading)
                break

    if not limits:
        # Anything else with a POSIX free-page count. Conservative — it
        # ignores memory the OS could reclaim, which is the right way to
        # be wrong here. Absent on both macOS and Windows, which is why
        # the probes above exist: falling through to "unknown" capped
        # those platforms at 2 workers regardless of their RAM.
        try:
            limits.append(
                os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")  # type: ignore[arg-type]
            )
        except (ValueError, OSError, AttributeError):
            return None

    return min(limits) if limits else None


def memory_capped_workers(
    requested: int, transcript_bytes: int, *, concurrent_projects: int = 1
) -> int:
    """Reduce ``requested`` to what memory can actually hold.

    Sized from the measured post-feeding footprints above: the parent —
    master entry list, session tree, fragment store — is charged
    ``_PARENT_RSS_PER_TRANSCRIPT_BYTE`` times the transcript's bytes on
    disk, while each fed worker (base imports + memo caches + its
    in-flight unit's slice) is charged the flat base plus a weak multiple
    of transcript size. The pre-feeding formula charged every *worker*
    the parent's ~3x copy — the footprint of the era when workers
    re-loaded the whole project, where an unguarded ``auto`` on a large
    archive exhausted RAM, drove the machine into swap and pegged every
    core. Swap-thrash is far worse than rendering serially, which is why
    the charges keep a margin over the measured fit and the headroom
    fraction stays where it is.

    ``concurrent_projects`` is how many project conversions run at once
    (the ``--all-projects`` pool). Each holds a master list of its own
    *and* spawns its own render workers, so the footprint is multiplicative
    across the two levels and the budget has to be split before it is spent.

    Returns at least 1 (the inline path). When available memory can't be
    determined, allows at most 2 workers — enough to be useful, small
    enough not to be dangerous on an unknown machine.
    """
    if requested <= 1:
        return 1

    parent_cost = (
        int(transcript_bytes * _PARENT_RSS_PER_TRANSCRIPT_BYTE) + _WORKER_BASE_BYTES
    )
    worker_cost = (
        int(transcript_bytes * _WORKER_RSS_PER_TRANSCRIPT_BYTE) + _WORKER_BASE_BYTES
    )
    available = available_memory_bytes()
    if available is None:
        return min(requested, 2)

    budget = int(available * _MEMORY_HEADROOM_FRACTION) // max(1, concurrent_projects)
    # The parent's share is already resident when the conversion itself is
    # the caller (its master list loads before the pool is sized) but not
    # yet when the all-projects parent pre-sizes budgets for project
    # workers it hasn't spawned; subtracting it in both cases errs safe.
    affordable = (budget - parent_cost) // max(1, worker_cost)
    return max(1, min(requested, affordable))


# --------------------------------------------------------------------------
# Worker side
# --------------------------------------------------------------------------

# Per-worker state, populated once by the initializer and reused for every
# unit that worker handles. Module-level because that is the only state a
# `spawn`ed worker can carry between tasks.
_worker_setup: Optional[_WorkerSetup] = None
_worker_cache_manager: Any = None


def _init_render_worker(setup: _WorkerSetup) -> None:
    """Set up the worker's render state. Deliberately cheap.

    The transcript itself never loads here: every unit arrives carrying
    its own entry slice (``RenderUnit.entries``), already date-filtered
    and deduplicated by the parent — so a worker's messages are the
    parent's, not a reconstruction that could drift. The cache manager is
    only constructed (no load) for the per-session combined-link lookup,
    and read-only: the parent already migrated the shared cache DB and
    wrote this project's row, so N workers must not race those writes —
    or any other.
    """
    global _worker_setup, _worker_cache_manager

    from .cache import CacheManager

    _worker_setup = setup
    _worker_cache_manager = CacheManager(
        Path(setup.project_dir), setup.library_version, read_only=True
    )


def _build_worker_renderer() -> Any:
    """A renderer configured exactly like the parent's.

    Goes through ``get_renderer`` rather than constructing ``HtmlRenderer``
    directly so page/session units render identically to the inline path,
    for every format the caller supports.
    """
    from .renderer import get_renderer

    assert _worker_setup is not None
    return get_renderer(
        _worker_setup.format,
        _worker_setup.image_export_mode,
        depth=_worker_setup.depth,
        compact=_worker_setup.compact,
        no_timestamps=_worker_setup.no_timestamps,
        no_recaps=_worker_setup.no_recaps,
    )


def _unit_fragment_store(unit: RenderUnit, renderer: Any) -> Any:
    """Attach a per-unit fragment store to an HTML renderer, or None.

    Page units get an empty store whose contents are exported back to the
    parent (the delta in the result tuple); session units get a store
    seeded with the slice the parent fed on the unit. The ordinal map
    comes from the unit's own ``entry_ordinals``, so keys name the same
    master-list positions the parent's store uses — without either side
    holding the other's list.
    """
    from .fragment_store import RenderFragmentStore, fragment_store_enabled

    assert _worker_setup is not None
    if _worker_setup.format != "html" or not fragment_store_enabled():
        return None
    from .html.renderer import HtmlRenderer

    if not isinstance(renderer, HtmlRenderer):
        return None
    store = RenderFragmentStore()
    if unit.entries is not None and unit.entry_ordinals is not None:
        store.set_entry_ordinal_map(
            {
                id(entry): ordinal
                for entry, ordinal in zip(unit.entries, unit.entry_ordinals)
            }
        )
    if unit.fed_fragments:
        store.absorb(unit.fed_fragments)
    renderer.fragment_store = store
    return store


def _render_unit_worker(
    unit: RenderUnit,
) -> "tuple[str, Any, Optional[str], Optional[Dict[Any, Any]]]":
    """Render and write one unit.

    Returns ``(kind, key, error_or_None, fragments_delta_or_None)``. The
    delta is a page unit's exported fragment store — the parent absorbs it
    and feeds slices of it to the session units it dispatches afterwards.
    Session units return None there (nothing renders after them).

    Failures come back as a formatted traceback rather than propagating,
    matching ``_convert_project_worker``: the parent needs to attribute the
    failure to a specific page/session and keep going.
    """
    try:
        assert _worker_setup is not None
        # The unit's entries ARE its transcript — the parent slices its
        # own (date-filtered, deduplicated) master list per unit.
        # ``submit`` declines entry-less units to the inline path, so
        # this is a backstop, not a reachable fallback.
        assert unit.entries is not None, "render unit dispatched without entries"

        output_dir = Path(_worker_setup.output_dir)
        renderer = _build_worker_renderer()
        store = _unit_fragment_store(unit, renderer)

        if unit.kind == "page":
            content = renderer.generate(
                unit.entries,
                unit.title,
                output_dir=output_dir,
                page_info=unit.page_info,
                page_stats=unit.page_stats,
                session_tree=_worker_setup.session_tree,
                archive_search_link=_worker_setup.archive_search_link,
            )
        else:
            # generate_session re-applies its own session filter to the
            # fed slice; the parent sliced with the same predicate, so
            # this is idempotent and keeps one code path with inline.
            content = renderer.generate_session(
                unit.entries,
                unit.key,
                unit.title,
                _worker_cache_manager,
                output_dir,
                session_tree=_worker_setup.session_tree,
                suppress_combined_link=unit.suppress_combined_link,
            )

        # errors="replace" for lone-surrogate safety — see issue #139.
        (output_dir / unit.file_name).write_text(
            content, encoding="utf-8", errors="replace"
        )
    except Exception:
        return unit.kind, unit.key, traceback.format_exc(), None
    delta = (
        store.export_fragments() if store is not None and unit.kind == "page" else None
    )
    return unit.kind, unit.key, None, delta


# --------------------------------------------------------------------------
# Parent side
# --------------------------------------------------------------------------


class RenderPool:
    """Lazily-started process pool for one project's render units.

    Use as a context manager for the duration of a conversion so pages and
    session files share the same warmed-up workers — starting a second pool
    would pay ``spawn`` + package import all over again.

    ``submit`` falls back to rendering inline (returning None) whenever the
    pool can't or shouldn't be used, so callers keep a single code path:
    ``if pool is None or (fut := pool.submit(unit)) is None: render inline``.
    """

    def __init__(self, setup: _WorkerSetup, max_workers: int) -> None:
        self._setup = setup
        self._max_workers = max_workers
        self._executor: Optional[ProcessPoolExecutor] = None
        self._broken = False

    def _ensure_executor(self) -> Optional[ProcessPoolExecutor]:
        if self._executor is not None or self._broken:
            return self._executor
        try:
            # `spawn` everywhere for the same reason the project-level pool
            # uses it: fork is unsafe with threads and inherits arbitrary
            # parent state — including, here, the parent's open SQLite
            # connections.
            ctx = multiprocessing.get_context("spawn")
            self._executor = ProcessPoolExecutor(
                max_workers=self._max_workers,
                mp_context=ctx,
                initializer=_init_render_worker,
                initargs=(self._setup,),
            )
        except Exception:
            # Can't bootstrap (e.g. a library caller without the
            # `if __name__ == "__main__"` guard `spawn` requires).
            # Degrade to inline rendering rather than failing the run.
            self._broken = True
            self._executor = None
        return self._executor

    def submit(
        self, unit: RenderUnit
    ) -> Optional["Future[tuple[str, Any, str | None, Dict[Any, Any] | None]]"]:
        """Queue a unit, or return None if the caller should render inline.

        A unit with no entry slice is declined rather than queued: workers
        never load the transcript, so only the parent's inline path (which
        holds the full master list) can render it.
        """
        if unit.entries is None:
            return None
        executor = self._ensure_executor()
        if executor is None:
            return None
        try:
            return executor.submit(_render_unit_worker, unit)
        except Exception:
            self._broken = True
            return None

    @property
    def broken(self) -> bool:
        """True once the pool has failed; callers should render inline."""
        return self._broken

    @property
    def started(self) -> bool:
        """True once workers exist, so further dispatch is nearly free.

        The executor is created on the first submitted unit and lives for
        the rest of the conversion. Until then a batch has to be worth the
        ~1s of ``spawn`` + package import each worker pays; afterwards that
        cost is sunk, and a later batch only has to beat rendering inline
        (see ``converter._worth_dispatching``).
        """
        return self._executor is not None and not self._broken

    def mark_broken(self) -> None:
        """Called by the parent when a pool-level failure surfaces on a
        result, so subsequent units go inline instead of re-failing."""
        self._broken = True

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    def __enter__(self) -> "RenderPool":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def make_render_pool(
    *,
    format: str,
    project_dir: Path,
    output_dir: Path,
    from_date: Optional[str],
    to_date: Optional[str],
    depth: "RenderingDepth",
    compact: bool,
    no_timestamps: bool,
    no_recaps: bool,
    image_export_mode: Optional[str],
    archive_search_link: Optional[str],
    library_version: str,
    max_workers: int,
    session_tree: Any = None,
) -> RenderPool:
    """Construct a pool. Cheap — no process starts until the first submit.

    ``session_tree`` must be the slim form (``dag.slim_session_tree``);
    it is pickled into every worker via the pool initializer.
    """
    return RenderPool(
        _WorkerSetup(
            format=format,
            project_dir=str(project_dir),
            output_dir=str(output_dir),
            from_date=from_date,
            to_date=to_date,
            depth=depth,
            compact=compact,
            no_timestamps=no_timestamps,
            no_recaps=no_recaps,
            image_export_mode=image_export_mode,
            archive_search_link=archive_search_link,
            library_version=library_version,
            session_tree=session_tree,
        ),
        max_workers,
    )

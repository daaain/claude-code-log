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
Workers are **self-sufficient rather than fed**. The alternative — pickling
the parent's parsed transcript to each worker — moves ~114MB per worker
for a mid-sized project. Instead each worker re-loads from the (already
warm) SQLite cache in its own ``initializer``, measured at 0.71s, and the
parent sends only small per-unit metadata. That also keeps the parent's
peak memory flat.

This makes a warm cache a hard prerequisite: without one a worker would
re-parse every JSONL file (~3.3s each, per worker), so ``RenderPool`` is
only created when the caller has a ``CacheManager``. Staleness checks and
all cache writes stay in the parent — workers render and write output
files, nothing else — so the DB keeps a single writer.

The pool is created lazily on first use: a project with one stale session
should not pay ~1s of ``spawn`` + import per worker to save 60ms.
"""

import multiprocessing
import os
import traceback
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from .models import RenderingDepth, TranscriptEntry

__all__ = ["RenderPool", "RenderUnit", "resolve_render_jobs"]


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
    # Page units: the session ids whose messages make up the page.
    # Session units: unused (the worker filters by session id itself).
    session_ids: Optional[List[str]] = None
    page_info: Optional[Dict[str, Any]] = None
    page_stats: Optional[Dict[str, Any]] = None
    suppress_combined_link: bool = False


@dataclass
class _WorkerSetup:
    """Everything a worker needs to reconstruct the parent's render state.

    Deliberately all-picklable primitives so it survives ``spawn``.
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


def resolve_render_jobs(requested: Optional[int]) -> int:
    """Resolve a render-worker count from an explicit request.

    ``None`` means "decide for me" → CPU count. Values below 1 (and an
    unavailable CPU count) collapse to 1, i.e. the inline path.
    """
    if requested is not None:
        return max(1, requested)
    return max(1, os.cpu_count() or 1)


# --------------------------------------------------------------------------
# Worker side
# --------------------------------------------------------------------------

# Per-worker state, populated once by the initializer and reused for every
# unit that worker handles. Module-level because that is the only state a
# `spawn`ed worker can carry between tasks.
_worker_setup: Optional[_WorkerSetup] = None
_worker_messages: "Optional[List[TranscriptEntry]]" = None
_worker_session_tree: Any = None
_worker_cache_manager: Any = None
_worker_messages_by_session: "Optional[Dict[str, List[TranscriptEntry]]]" = None


def _init_render_worker(setup: _WorkerSetup) -> None:
    """Load the project's transcript once, at worker start.

    Mirrors what ``convert_jsonl_to`` does in the parent — load from cache,
    date-filter, deduplicate — so a worker's message list is identical to
    the parent's. Any divergence here shows up as differing output files,
    which the test suite pins byte-for-byte against a serial run.
    """
    global _worker_setup, _worker_messages, _worker_session_tree
    global _worker_cache_manager, _worker_messages_by_session

    from .cache import CacheManager
    from .converter import (
        deduplicate_messages,
        filter_messages_by_date,
        load_directory_transcripts,
    )
    from .utils import get_parent_session_id

    project_dir = Path(setup.project_dir)
    cache_manager = CacheManager(project_dir, setup.library_version)
    messages, session_tree = load_directory_transcripts(
        project_dir, cache_manager, setup.from_date, setup.to_date, True
    )
    messages = filter_messages_by_date(messages, setup.from_date, setup.to_date)
    messages = deduplicate_messages(messages)

    by_session: "Dict[str, List[TranscriptEntry]]" = {}
    for msg in messages:
        session_id = getattr(msg, "sessionId", None)
        if session_id:
            by_session.setdefault(get_parent_session_id(session_id), []).append(msg)

    _worker_setup = setup
    _worker_messages = messages
    _worker_session_tree = session_tree
    _worker_cache_manager = cache_manager
    _worker_messages_by_session = by_session


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


def _render_unit_worker(unit: RenderUnit) -> "tuple[str, Any, Optional[str]]":
    """Render and write one unit. Returns ``(kind, key, error_or_None)``.

    Failures come back as a formatted traceback rather than propagating,
    matching ``_convert_project_worker``: the parent needs to attribute the
    failure to a specific page/session and keep going.
    """
    try:
        assert _worker_setup is not None
        assert _worker_messages is not None
        assert _worker_messages_by_session is not None

        output_dir = Path(_worker_setup.output_dir)
        renderer = _build_worker_renderer()

        if unit.kind == "page":
            page_messages: "List[TranscriptEntry]" = []
            for session_id in unit.session_ids or []:
                page_messages.extend(_worker_messages_by_session.get(session_id, []))
            content = renderer.generate(
                page_messages,
                unit.title,
                page_info=unit.page_info,
                page_stats=unit.page_stats,
                session_tree=_worker_session_tree,
                archive_search_link=_worker_setup.archive_search_link,
            )
        else:
            content = renderer.generate_session(
                _worker_messages,
                unit.key,
                unit.title,
                _worker_cache_manager,
                output_dir,
                session_tree=_worker_session_tree,
                suppress_combined_link=unit.suppress_combined_link,
            )

        # errors="replace" for lone-surrogate safety — see issue #139.
        (output_dir / unit.file_name).write_text(
            content, encoding="utf-8", errors="replace"
        )
    except Exception:
        return unit.kind, unit.key, traceback.format_exc()
    return unit.kind, unit.key, None


# --------------------------------------------------------------------------
# Parent side
# --------------------------------------------------------------------------


class RenderPool:
    """Lazily-started process pool for one project's render units.

    Use as a context manager for the duration of a conversion so pages and
    session files share the same warmed-up workers — starting a second pool
    would pay ``spawn`` + import + transcript load all over again.

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
    ) -> Optional["Future[tuple[str, Any, str | None]]"]:
        """Queue a unit, or return None if the caller should render inline."""
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
) -> RenderPool:
    """Construct a pool. Cheap — no process starts until the first submit."""
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
        ),
        max_workers,
    )

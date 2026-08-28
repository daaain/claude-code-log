#!/usr/bin/env python3
"""When a conversion fans its rendering out, and how a batch is run.

The policy layer over :mod:`render_pool`. That module is the mechanism —
what a worker is, what crosses the process boundary, how many workers
memory allows. This one answers the two questions a *conversion* has:

1. Should this project have a pool at all (``build_render_pool``)?
2. Is this particular batch of units worth sending to it
   (``worth_dispatching``), and how does the batch actually run
   (``dispatch_render_units``)?

Both thresholds live here because both are statements about conversion
work, not about pools; ``render_pool`` deliberately knows nothing about
projects, transcripts or pages. The dependency runs one way — this module
imports ``render_pool``, never the reverse — so a worker process never
loads the policy it is executing.

See ``dev-docs/application_model.md`` § 2.10 for the measurements behind
the thresholds.
"""

from concurrent.futures import as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .render_pool import RenderPool, RenderUnit, memory_capped_workers
from .utils import project_transcript_bytes

if TYPE_CHECKING:
    from .cache import CacheManager
    from .dag import SessionTree
    from .fragment_store import RenderFragmentStore
    from .models import RenderingDepth

__all__ = [
    "build_render_pool",
    "dispatch_render_units",
    "worth_dispatching",
]


# Below this much work in one batch, the fan-out is not worth the ~1s of
# `spawn` + package import each worker pays before it can render anything.
#
# This used to be a count of units (8), on the premise that "units average
# well under 200ms". That holds for *session* units — cheap, because the
# page pass already put their fragments in the store — but not for *page*
# units, which carry ~`page_size` messages each and are where nearly all of
# a conversion's render time lives. Counting them made a project's pages
# dispatch only once it had 8 of them (~16k messages at the default page
# size), so every smaller project rendered its expensive page batch inline
# and fanned out only its cheap session batch — paying the pool's startup
# for the batch that had the least to gain.
#
# Measured over 16 real projects on the 8-core VM (full rebuild, warm
# cache — work/render-format-once.md § 7.5): projects between ~4k and ~15k
# messages went from 0.86-1.05x under the count gate to 1.24-2.19x under
# this one, while the ones below it went from 0.82-0.94x (a real loss —
# their session batch was being fanned out for nothing) back to 1.00x.
# 4,000 entries is just above the measured knee: the pool's startup is
# worth roughly 2,000 entries of rendering at the ~2,000 entries/s the
# render phase sustains, and a batch has to save more than it costs.
_MIN_ENTRIES_FOR_RENDER_POOL = 4_000


# A project whose entire message list is below the batch gate can never
# produce a batch that clears it — every batch is a subset of that list —
# so building a pool for it is wasted setup. Purely a short-circuit: the
# batch gate in `worth_dispatching` is the decision that matters, which
# is why this is the same number rather than a policy of its own. It was
# 25,000 while the two gates were sized independently, back when a worker
# had to load the whole transcript before it could render anything.
_MIN_MESSAGES_FOR_RENDER_POOL = _MIN_ENTRIES_FOR_RENDER_POOL


def build_render_pool(
    *,
    format: str,
    input_path: Path,
    effective_output_dir: Path,
    cache_manager: Optional["CacheManager"],
    message_count: int,
    from_date: Optional[str],
    to_date: Optional[str],
    depth: "RenderingDepth",
    compact: bool,
    no_timestamps: bool,
    no_recaps: bool,
    image_export_mode: Optional[str],
    archive_search_link: Optional[str],
    render_jobs: Optional[int],
    session_tree: Optional["SessionTree"],
) -> Optional[RenderPool]:
    """Build a render pool for this conversion, or None to render inline.

    Returns None whenever fanning out would be wrong or wasteful:

    - ``render_jobs`` resolves to 1. The fan-out is on by default (an
      unset ``$CLAUDE_CODE_LOG_RENDER_JOBS`` means the CPU count), so this
      takes ``1``/``off`` opting out via the environment (see
      ``render_pool.resolve_render_jobs``) — or a project worker in the
      all-projects pool getting 1 because there is no spare capacity,
      since nesting pools would oversubscribe.
    - Single-file mode, or no cache manager (staleness planning needs one).
    - No pre-built ``session_tree``. Workers render fed entry slices
      against the conversion's tree; without one they would rebuild a DAG
      from their slice alone, which can genuinely differ (missing
      cross-session hierarchy) — a correctness cliff, so decline instead.
    - Projects too small for any batch to clear ``worth_dispatching``,
      where the real decision lives; a pool this returns is only ever
      *started* by a batch worth dispatching, so this is a short-circuit
      rather than a second policy.

    ``image_export_mode="referenced"`` used to decline too, when each
    render allocated ``images/image_NNNN.png`` names from a per-call
    counter. Filenames are content-addressed now (see
    ``image_export.export_image``), so concurrent workers exporting the
    same image write the same name atomically with identical bytes — the
    mode is pool-safe.
    - Not enough memory for the fan-out's footprint.

    The worker count is also capped by available memory, with the parent
    charged its full master-list footprint and each fed worker only its
    measured slice-holding cost — see ``render_pool.memory_capped_workers``.
    """
    from .render_pool import resolve_render_jobs

    max_workers = resolve_render_jobs(render_jobs)
    if max_workers <= 1:
        return None
    if cache_manager is None or not input_path.is_dir():
        return None
    if session_tree is None:
        return None
    if message_count < _MIN_MESSAGES_FOR_RENDER_POOL:
        return None

    transcript_bytes = project_transcript_bytes(input_path)
    max_workers = memory_capped_workers(max_workers, transcript_bytes)
    if max_workers <= 1:
        return None

    from .cache import get_library_version
    from .dag import slim_session_tree
    from .render_pool import make_render_pool

    return make_render_pool(
        session_tree=slim_session_tree(session_tree),
        format=format,
        project_dir=input_path,
        output_dir=effective_output_dir,
        from_date=from_date,
        to_date=to_date,
        depth=depth,
        compact=compact,
        no_timestamps=no_timestamps,
        no_recaps=no_recaps,
        image_export_mode=image_export_mode,
        archive_search_link=archive_search_link,
        library_version=get_library_version(),
        max_workers=max_workers,
    )


def worth_dispatching(units: list[RenderUnit], render_pool: RenderPool) -> bool:
    """Is fanning this batch out worth what the pool costs to use?

    Three cases, in order:

    - **One unit.** Never. A lone unit renders in a worker while the parent
      waits, which is strictly slower than rendering it here.
    - **Workers already exist.** Always. The startup cost is sunk (the
      executor is lazy and lives for the whole conversion), so a second
      batch only has to beat rendering serially, which any spread does.
      This is what lets a project's cheap session batch ride along behind
      its expensive page batch.
    - **Otherwise**, the batch has to carry enough work to repay starting
      the pool — see ``_MIN_ENTRIES_FOR_RENDER_POOL``. Entry count is the
      cost proxy because it is what the unit actually renders; units with
      no slice are excluded, since ``RenderPool.submit`` declines those to
      the inline path anyway.
    """
    if len(units) < 2:
        return False
    if render_pool.started:
        return True
    entries = sum(len(unit.entries) for unit in units if unit.entries)
    return entries >= _MIN_ENTRIES_FOR_RENDER_POOL


def dispatch_render_units(
    units: list[RenderUnit],
    render_pool: Optional[RenderPool],
    render_inline: Callable[[RenderUnit], None],
    on_written: Callable[[RenderUnit], None],
    label: Callable[[RenderUnit], str],
    fragment_store: "Optional[RenderFragmentStore]" = None,
) -> None:
    """Render ``units``, over the pool when there is one, else inline.

    ``on_written`` runs in the parent for every unit that made it to disk,
    in completion order — it owns the cache bookkeeping, which is why the
    workers never touch the DB for writes.

    ``fragment_store`` absorbs the fragment deltas page workers return, so
    the fragments a worker formatted still reach the conversion's store —
    the session pass then feeds on them exactly as if the pages had
    rendered inline (see RenderUnit.fed_fragments).

    Every path back to inline rendering is a *fallback*, never an error:
    a pool that can't bootstrap (a library caller without the
    ``if __name__ == "__main__"`` guard ``spawn`` requires) or a worker
    that dies mid-run must still produce complete output. Only a genuine
    render failure inside a unit propagates.
    """
    if not units:
        return

    if render_pool is None or not worth_dispatching(units, render_pool):
        for unit in units:
            render_inline(unit)
            on_written(unit)
        return

    futures: dict[Any, RenderUnit] = {}
    unsubmitted: list[RenderUnit] = []
    for unit in units:
        future = render_pool.submit(unit)
        if future is None:
            unsubmitted.append(unit)
        else:
            futures[future] = unit

    for future in as_completed(futures):
        unit = futures[future]
        try:
            _kind, _key, error, fragments_delta = future.result()
        except Exception as e:
            # The pool itself broke (BrokenProcessPool, a worker killed by
            # the OOM killer, …). Re-render this unit inline and send the
            # rest of the batch the same way.
            print(
                f"Warning: render worker failed on {label(unit)} "
                f"({e.__class__.__name__}: {e}); rendering inline."
            )
            render_pool.mark_broken()
            unsubmitted.append(unit)
            continue
        if error is not None:
            raise RuntimeError(f"Failed to render {label(unit)}:\n{error}")
        if fragments_delta and fragment_store is not None:
            fragment_store.absorb(fragments_delta)
        on_written(unit)

    for unit in unsubmitted:
        render_inline(unit)
        on_written(unit)

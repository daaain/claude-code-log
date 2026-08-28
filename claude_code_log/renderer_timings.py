"""Timing utilities for renderer performance profiling.

This module provides timing and performance profiling utilities for the renderer.
All timing-related configuration and functionality is centralized here.
"""

import os
import time
from contextlib import contextmanager
from typing import Tuple, Iterator, Any, Callable, Union, Optional

# Performance debugging - enabled via CLAUDE_CODE_LOG_DEBUG_TIMING environment variable
# Set to "1", "true", or "yes" to enable timing output
DEBUG_TIMING = os.getenv("CLAUDE_CODE_LOG_DEBUG_TIMING", "").lower() in (
    "1",
    "true",
    "yes",
)

# Global timing data storage
_timing_data: dict[str, Any] = {}


def set_timing_var(name: str, value: Any) -> None:
    """Set a timing variable in the global timing data dict.

    Args:
        name: Variable name (e.g., "_markdown_timings", "_pygments_timings", "_current_msg_id")
        value: Value to set
    """
    if DEBUG_TIMING:
        _timing_data[name] = value


@contextmanager
def log_timing(
    phase: Union[str, Callable[[], str]],
    t_start: Optional[float] = None,
) -> Iterator[None]:
    """Context manager for logging phase timing.

    Args:
        phase: Phase name (static string) or callable returning phase name (for dynamic names)
        t_start: Optional start reading for the running total. Must come
            from ``time.monotonic()`` — this module measures durations on
            the monotonic clock, and mixing in a ``time.time()`` reading
            would print a nonsense total (the two have unrelated epochs).

    Example:
        # Static phase name
        with log_timing("Initialization", t_start):
            setup_code()

        # Dynamic phase name (evaluated at end)
        with log_timing(lambda: f"Processing ({len(items)} items)", t_start):
            items = process()
    """
    if not DEBUG_TIMING:
        yield
        return

    t_phase_start = time.monotonic()

    try:
        yield
    finally:
        t_now = time.monotonic()
        phase_time = t_now - t_phase_start

        # Evaluate phase name (call if callable, use directly if string)
        phase_name = phase() if callable(phase) else phase

        # Calculate total time if t_start provided
        if t_start is not None:
            total_time = t_now - t_start
            print(
                f"[TIMING] {phase_name:40s} {phase_time:8.3f}s (total: {total_time:8.3f}s)",
                flush=True,
            )
        else:
            print(
                f"[TIMING] {phase_name:40s} {phase_time:8.3f}s",
                flush=True,
            )

        # Update last timing checkpoint
        _timing_data["_t_last"] = t_now


@contextmanager
def timing_stat(list_name: str) -> Iterator[None]:
    """Context manager for tracking timing statistics.

    Args:
        list_name: Name of the timing list to append to
                  (e.g., "_markdown_timings", "_pygments_timings")

    Example:
        with timing_stat("_pygments_timings"):
            result = expensive_operation()
    """
    if not DEBUG_TIMING:
        yield
        return

    t_start = time.monotonic()
    try:
        yield
    finally:
        duration = time.monotonic() - t_start
        if list_name in _timing_data:
            msg_id = _timing_data.get("_current_msg_id", "")
            _timing_data[list_name].append((duration, msg_id))


def report_cache_statistics(
    cache_stats: list[Tuple[str, dict[str, int]]],
) -> None:
    """Report memo-cache effectiveness for the pure render leaves.

    Args:
        cache_stats: List of (name, stats) pairs, where stats is the dict
                     returned by ``render_cache.ByteBoundedCache.stats()``.
    """
    for name, stats in cache_stats:
        lookups = stats["hits"] + stats["misses"]
        if not lookups:
            continue
        rate = 100.0 * stats["hits"] / lookups
        extras = [
            f"{stats[extra_key]} {extra_key}"
            for extra_key in ("conflicts", "skipped")
            if stats.get(extra_key)
        ]
        skipped_note = f", {', '.join(extras)}" if extras else ""
        print(
            f"[TIMING] {name} memo: {stats['hits']}/{lookups} hits ({rate:.0f}%), "
            f"{stats['entries']} entries, {stats['bytes'] / 1e6:.1f}MB{skipped_note}",
            flush=True,
        )


def report_timing_statistics(
    operation_timings: list[Tuple[str, list[Tuple[float, str]]]],
) -> None:
    """Report timing statistics for rendering operations.

    Args:
        operation_timings: List of (name, timings) tuples where timings is a list of (duration, msg_id)
                          e.g., [("Markdown", markdown_timings), ("Pygments", pygments_timings)]
    """
    for operation_name, timings in operation_timings:
        if timings:
            sorted_ops = sorted(timings, key=lambda x: x[0], reverse=True)
            total_time = sum(t[0] for t in timings)
            print(f"\n[TIMING] {operation_name} rendering:", flush=True)
            print(f"[TIMING]   Total operations: {len(timings)}", flush=True)
            print(f"[TIMING]   Total time: {total_time:.3f}s", flush=True)
            print("[TIMING]   Slowest 10 operations:", flush=True)
            for duration, msg_id in sorted_ops[:10]:
                print(
                    f"[TIMING]     {msg_id}: {duration * 1000:.1f}ms",
                    flush=True,
                )

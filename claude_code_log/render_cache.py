#!/usr/bin/env python3
"""Byte-bounded memo caches for the expensive pure leaves of rendering.

Why this exists
---------------
A full project conversion renders every message **twice**: once into its
combined-transcript page and once into its individual ``session-*.html``.
Both go through ``HtmlRenderer.generate``, which rebuilds the tree and
re-formats every message from the same source entries — so the two passes
duplicate all of the per-message formatting work. Measured on a 118-file,
12k-message project: 22,420 ``format_content`` calls covering 11,113
distinct messages.

The two dominant leaves of that work are Pygments highlighting and mistune
Markdown rendering, and both are pure functions of their string inputs
(with one caveat, below). Memoizing just those two removes the duplication
without touching the render pipeline's structure — and also collapses the
*intra*-pass repeats, since the same file contents are commonly re-read
across many messages (measured hit rate for Pygments: ~68%, higher than
the 50% that page-vs-session duplication alone would give).

The one caveat: Markdown rendering is **not** purely a function of its
text. The SHA-linkifier plugin resolves commit hashes against the
per-render repo cwd carried by ``git_remote._render_repo_cwd``
(a ContextVar), so a cache shared across projects would leak commit links
between repos. Markdown keys therefore include that cwd; Pygments has no
such coupling and keys on its arguments alone.

Sizing
------
Rendered fragments are large (a highlighted 2000-line file is hundreds of
KB), so an entry-count bound like ``functools.lru_cache(maxsize=N)`` gives
no control over footprint. These caches are bounded by the *bytes* of the
values they hold, evicting least-recently-used, and refuse to admit any
single entry larger than a fraction of the budget so one huge file can't
evict everything else.

Configure with ``CLAUDE_CODE_LOG_RENDER_CACHE_MB`` (default 192 MB;
``0`` disables memoization entirely, restoring the previous behaviour).
"""

import contextlib
import os
import threading
from collections import OrderedDict
from typing import Hashable, Iterator, Optional

__all__ = [
    "ByteBoundedCache",
    "markdown_cache",
    "pygments_cache",
    "clear_all",
    "disabled",
]


DEFAULT_CACHE_MB = 192

# A single entry may occupy at most this fraction of the budget. Without
# it, one multi-MB highlighted file admitted into a small cache evicts
# every cheap entry behind it and the cache thrashes to a 0% hit rate.
_MAX_ENTRY_FRACTION = 0.125


def _configured_budget_bytes() -> int:
    """Resolve the per-cache byte budget from the environment.

    Returns 0 when memoization is disabled (``...CACHE_MB=0``) or the
    value is unparseable — an unreadable setting should degrade to the
    old, always-recompute behaviour rather than crash a conversion.
    """
    raw = os.getenv("CLAUDE_CODE_LOG_RENDER_CACHE_MB")
    if raw is None:
        return DEFAULT_CACHE_MB * 1024 * 1024
    try:
        mb = int(raw.strip())
    except ValueError:
        return DEFAULT_CACHE_MB * 1024 * 1024
    return max(0, mb) * 1024 * 1024


class ByteBoundedCache:
    """An LRU cache of ``str`` values bounded by their total size in bytes.

    Thread-safe: the render fan-out (``render_pool``) uses
    processes rather than threads, so contention is not expected, but the
    HTML helpers are also reachable from the TUI and from ``serve`` request
    handlers, and a torn ``OrderedDict`` would be a very unpleasant bug to
    chase for a pure performance feature.
    """

    def __init__(self, budget_bytes: Optional[int] = None) -> None:
        self._budget = (
            _configured_budget_bytes() if budget_bytes is None else budget_bytes
        )
        self._max_entry = int(self._budget * _MAX_ENTRY_FRACTION)
        self._entries: "OrderedDict[Hashable, str]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self._budget > 0

    def get(self, key: Hashable) -> Optional[str]:
        if self._budget <= 0:
            return None
        with self._lock:
            value = self._entries.get(key)
            if value is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: Hashable, value: str) -> None:
        if self._budget <= 0:
            return
        # len() on a str is close enough to the retained size for a
        # budget knob, and avoids encoding every value just to measure it.
        size = len(value)
        if size > self._max_entry:
            return
        with self._lock:
            if key in self._entries:
                self._bytes -= len(self._entries[key])
                self._entries.pop(key)
            self._entries[key] = value
            self._bytes += size
            while self._bytes > self._budget and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0
            self.hits = 0
            self.misses = 0

    @property
    def budget_bytes(self) -> int:
        return self._budget

    def set_budget(self, budget_bytes: int) -> None:
        """Resize (or, at 0, switch off) this cache, dropping its contents.

        Used by ``disabled()`` to toggle the shared singletons in place —
        call sites imported the objects by value, so replacing the module
        attributes would not reach them.
        """
        self.clear()
        with self._lock:
            self._budget = max(0, budget_bytes)
            self._max_entry = int(self._budget * _MAX_ENTRY_FRACTION)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._entries),
                "bytes": self._bytes,
                "hits": self.hits,
                "misses": self.misses,
            }


# Module-level singletons. Separate budgets so a Pygments-heavy project
# can't starve the Markdown cache (and vice versa).
pygments_cache = ByteBoundedCache()
markdown_cache = ByteBoundedCache()
_ALL_CACHES = (pygments_cache, markdown_cache)


def clear_all() -> None:
    """Drop both caches. Used by tests and by long-lived hosts (``serve``)."""
    pygments_cache.clear()
    markdown_cache.clear()


@contextlib.contextmanager
def disabled() -> Iterator[None]:
    """Turn memoization off for the duration of the block.

    Flips the budgets on the existing singletons rather than replacing
    them, because every call site imported the objects by value
    (``from ..render_cache import pygments_cache``) — rebinding the module
    attributes would not reach them.

    This is the in-process equivalent of ``CLAUDE_CODE_LOG_RENDER_CACHE_MB=0``,
    and it exists so a conversion can be run both ways and the outputs
    compared (see ``test_render_cache_equivalence.py``).
    """
    saved = [(cache, cache.budget_bytes) for cache in _ALL_CACHES]
    for cache in _ALL_CACHES:
        cache.set_budget(0)
    try:
        yield
    finally:
        for cache, budget in saved:
            cache.set_budget(budget)

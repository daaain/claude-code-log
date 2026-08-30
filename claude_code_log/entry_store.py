"""Per-conversion store of parsed transcript entries.

A conversion that refreshes the cache incrementally materialises the same
entries up to three times: ``_incremental_cache_refresh`` parses each
modified file from source, then the closure load
(``_load_sessions_partial``) and the session-scoped render
(``_load_stale_session_transcripts``) each rebuild those same entries
from the rows just written — ``zlib.decompress`` + ``json.loads`` +
Pydantic validation, once per consumer. On a 39.7 MB session file that
was 488 ms + 129 ms + 141 ms of a 1.1 s watch tick, all three
proportional to the *file* rather than to the handful of appended lines
(work/watch-mode.md, C14).

This store keeps the list the first pass already produced and serves it
to the other two.

Scope and lifetime are deliberately narrow — the same posture as
``fragment_store.py``, one layer down:

- **One store per conversion, threaded explicitly.** Never a global and
  never hung off ``CacheManager``, which long-lived hosts (the TUI) keep
  across many conversions. A store cannot outlive the conversion that
  made it.
- **Only the incremental-refresh path fills it.** ``put`` is called from
  ``_incremental_cache_refresh`` alone, with the files it just parsed, so
  a cold or full conversion never stores anything and its residency is
  unchanged. The streaming page loads (application_model.md § 2.13)
  deliberately do *not* get a store: their whole point is that a page's
  entries are dropped before the next page loads, and a store spanning
  pages would pin every one of them. What this holds is therefore bounded
  by *what changed*, not by the archive.
- **Hits are verified against the file.** ``get`` re-stats and compares
  ``(size, mtime_ns)`` against the stamp captured *before* the parse; any
  mismatch declines to the cache. A stamp taken before the parse can only
  be older than the entries describe, so a file that grew mid-parse
  declines rather than serving a list that does not match its stamp.
- **Handouts are deep copies.** The pipeline mutates entries in place —
  ``_integrate_agent_entries`` appends ``#agent-{id}`` to ``sessionId``
  (not idempotent) and dedup re-parents around dropped copies — and today
  each consumer gets freshly deserialised objects. Serving the same
  objects twice would let one consumer's mutations leak into another's
  view, so ``get`` returns a ``deepcopy``. That is cheap and stays cheap
  because the bulk of an entry is immutable strings, which ``deepcopy``
  shares rather than copies: measured **2.0 ms and 0.83 MB** for the
  207-entry, 39.7 MB session above, against 123 ms to rebuild it from the
  cache.

The store is a pure performance feature: a conversion with no store (or
a declined ``put``) behaves exactly as before.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from hashlib import blake2b
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .models import TranscriptEntry

FileStamp = tuple[int, int]
"""``(size, mtime_ns)`` — the identity a stored list is pinned to."""

# Prefix hashing reads the file, so the digest is chosen for speed over
# collision margin beyond what a local cache needs: 16 bytes of BLAKE2b
# runs at ~1.2 GB/s, i.e. 32 ms over a 39.7 MB session — against 143 ms
# to re-parse the same bytes, which is what it buys.
_HASH_DIGEST_SIZE = 16
_READ_CHUNK = 1 << 20


def new_hasher() -> Any:
    """A fresh hasher for prefix identity."""
    return blake2b(digest_size=_HASH_DIGEST_SIZE)


def read_prefix_and_tail(path: Path, prefix_len: int, hasher: Any) -> Optional[bytes]:
    """Feed the first ``prefix_len`` bytes into ``hasher``; return the rest.

    None when the file is shorter than ``prefix_len`` (truncated or
    replaced) or unreadable — both of which mean "no usable prefix", and
    the caller re-reads from the top.
    """
    try:
        with path.open("rb") as fh:
            remaining = prefix_len
            while remaining > 0:
                chunk = fh.read(min(_READ_CHUNK, remaining))
                if not chunk:
                    return None
                hasher.update(chunk)
                remaining -= len(chunk)
            return fh.read()
    except OSError:
        return None


@dataclass
class HeldPrefix:
    """Entries covering the first ``prefix_len`` bytes of a source file.

    ``entries`` are the *pre-post-processing* parse products — before
    subagent meta linking, prompt-hash linking and agent-block splicing,
    all of which are whole-file functions re-run over the concatenated
    list. They cost 0.1 ms together on the reference file, so re-running
    them is free; reusing their *output* would not be sound, because a
    new sidecar can relink an entry that was parsed ticks ago.

    ``line_count`` continues the line numbering that parse warnings quote.
    """

    prefix_len: int
    digest: bytes
    entries: list["TranscriptEntry"] = field(default_factory=list["TranscriptEntry"])
    agent_ids: set[str] = field(default_factory=set[str])
    line_count: int = 0


# Total source bytes the store will hold before evicting in insertion
# order. It only ever holds files a refresh actually parsed, so this is a
# backstop against a pathological closure, not a tuning knob.
DEFAULT_BUDGET_BYTES = 256 * 1024 * 1024

# A parsed transcript costs roughly 3x its bytes on disk in RAM
# (CONTRIBUTING, "A note on memory"). Require headroom well above that
# before holding one, so a tight machine keeps today's footprint instead
# of trading into swap for 270 ms.
MIN_AVAILABLE_MEMORY_PER_FILE_BYTE = 6.0


def entry_store_enabled() -> bool:
    """Whether the entry store may be used, per the environment.

    ``CLAUDE_CODE_LOG_ENTRY_STORE=0`` (or ``off``/``false``) disables it,
    mirroring the branch's other kill switches — for bisecting a
    rendering difference, not for tuning.
    """
    value = os.environ.get("CLAUDE_CODE_LOG_ENTRY_STORE", "").strip().lower()
    return value not in ("0", "off", "false")


def entry_store_forced() -> bool:
    """Whether the environment *explicitly* asks for the store.

    An explicit ``=1`` (or ``on``/``true``) overrides the memory valve in
    :meth:`ParsedEntryStore.put`. Unset means "enabled, but let the valve
    decide".
    """
    value = os.environ.get("CLAUDE_CODE_LOG_ENTRY_STORE", "").strip().lower()
    return value in ("1", "on", "true")


def stamp_file(path: Path) -> Optional[FileStamp]:
    """``(size, mtime_ns)`` for ``path``, or None if it can't be stat'd."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


class ParsedEntryStore:
    """Entries a conversion has already parsed, keyed by source file."""

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET_BYTES) -> None:
        self._budget = budget_bytes
        self._held: dict[str, tuple[FileStamp, list["TranscriptEntry"]]] = {}
        self._prefixes: dict[str, HeldPrefix] = {}
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        self.declines = 0
        self.prefix_hits = 0
        self.prefix_misses = 0

    # ---- prefixes (across ticks) -----------------------------------------

    def put_prefix(
        self,
        path: Path,
        prefix_len: int,
        digest: bytes,
        entries: list["TranscriptEntry"],
        agent_ids: set[str],
        line_count: int,
    ) -> None:
        """Hold ``entries`` as the parse of ``path``'s first ``prefix_len`` bytes.

        Copied on the way in, because the caller goes on to run
        post-processing that mutates entries in place (see the module
        docstring) and a later tick must resume from the pre-mutation
        state.
        """
        if prefix_len <= 0 or not entries:
            return
        if prefix_len > self._budget or not self._has_memory_for(prefix_len):
            self.declines += 1
            self._prefixes.pop(str(path), None)
            return
        self._prefixes[str(path)] = HeldPrefix(
            prefix_len=prefix_len,
            digest=digest,
            entries=copy.deepcopy(entries),
            agent_ids=set(agent_ids),
            line_count=line_count,
        )

    def get_prefix(self, path: Path) -> Optional[HeldPrefix]:
        """The held prefix for ``path``, entries copied, or None.

        The digest is **not** checked here — verifying it means hashing
        the file's first ``prefix_len`` bytes, which the caller does
        anyway on its way to reading the tail. The caller compares and
        calls :meth:`drop_prefix` on a mismatch.
        """
        held = self._prefixes.get(str(path))
        if held is None:
            return None
        return HeldPrefix(
            prefix_len=held.prefix_len,
            digest=held.digest,
            entries=copy.deepcopy(held.entries),
            agent_ids=set(held.agent_ids),
            line_count=held.line_count,
        )

    def drop_prefix(self, path: Path) -> None:
        """Forget the held prefix — its file no longer starts with those bytes."""
        self._prefixes.pop(str(path), None)

    # ---- writing ---------------------------------------------------------

    def put(
        self, path: Path, stamp: Optional[FileStamp], entries: list["TranscriptEntry"]
    ) -> None:
        """Hold ``entries`` for ``path``, pinned to ``stamp``.

        ``stamp`` must be the file's identity as captured *before* the
        parse (see the module docstring); None — an unstattable file —
        declines, as does an empty list, since there is nothing to save.
        """
        if stamp is None or not entries:
            return
        size = stamp[0]
        if size > self._budget:
            self.declines += 1
            return
        if not self._has_memory_for(size):
            self.declines += 1
            return

        key = str(path)
        existing = self._held.pop(key, None)
        if existing is not None:
            self._bytes -= existing[0][0]
        self._held[key] = (stamp, entries)
        self._bytes += size
        self._evict_to_budget()

    def _has_memory_for(self, size: int) -> bool:
        """Whether the machine has room to hold a file of ``size`` bytes."""
        if entry_store_forced():
            return True
        from .render_pool import available_memory_bytes

        available = available_memory_bytes()
        if available is None:  # unreadable probe — don't second-guess it
            return True
        return available >= size * MIN_AVAILABLE_MEMORY_PER_FILE_BYTE

    def _evict_to_budget(self) -> None:
        """Drop oldest entries until the held source bytes fit the budget."""
        while self._bytes > self._budget and self._held:
            _key, (stamp, _entries) = next(iter(self._held.items()))
            self._held.pop(_key)
            self._bytes -= stamp[0]

    # ---- reading ---------------------------------------------------------

    def get(self, path: Path) -> Optional[list["TranscriptEntry"]]:
        """The stored entries for ``path``, or None to fall back to the cache.

        Declines whenever the file's current ``(size, mtime_ns)`` differs
        from the stamp the entries were pinned to — the file changed
        under us, so the cache (which the refresh has just rewritten) is
        the authority, not this.
        """
        held = self._held.get(str(path))
        if held is None:
            self.misses += 1
            return None
        stamp, entries = held
        if stamp_file(path) != stamp:
            self.misses += 1
            return None
        self.hits += 1
        # Deep copy, because consumers mutate: see the module docstring.
        return copy.deepcopy(entries)

    # ---- introspection ---------------------------------------------------

    @property
    def held_bytes(self) -> int:
        """Source bytes currently pinned (the residency this store adds)."""
        return self._bytes

    def stats_line(self) -> str:
        return (
            f"entry store: {self.hits} hit(s), {self.misses} miss(es), "
            f"{self.declines} decline(s), {self._bytes / 1e6:.1f} MB held"
        )

"""Per-conversion store of formatted message fragments.

Step 3 of the render optimisation work ("format once, assemble many" —
see work/render-format-once.md). A project conversion renders every
message twice: once into its combined-transcript page and once into its
``session-*.html``. The leaf memo in ``render_cache.py`` recovers the two
expensive leaves of that duplication (Pygments, Markdown); this store
removes the duplication itself by caching each message's complete
formatted fragment — the ``(title, html, timestamp)`` triple that
``HtmlRenderer._annotate_tree_for_render`` writes onto the tree — and
serving it to every subsequent tree that contains the same message.

Scope and lifetime are deliberately narrow, which is what keeps the
invalidation surface at zero:

- **One store per conversion call.** A conversion runs a single renderer
  configuration (depth / compact / repo cwd / plugins), so none of those
  need to appear in the key, and a fragment can never outlive the code or
  config that produced it. Long-lived hosts (``serve``, the TUI) create a
  fresh store per conversion just like the CLI does.
- **Keys are entry identity plus tree context, not content.**
  ``MessageContent.fragment_key`` is ``(id(source_entry), part_ordinal)``,
  stamped by the pass-2 render loop — the only identity available where
  differently-filtered subsets are rendered. The store translates the id
  to the entry's ordinal in the conversion's master message list
  (``set_entry_ordinals`` / ``stable_key``), which every tree — and any
  process that loads the same master list — agrees on; an unknown id
  declines caching. The consumer appends a signature of the tree-derived
  render inputs (pair presence, model badge, agent depth, …) so a
  message that legitimately renders differently in the combined tree
  than in its session tree occupies two slots instead of poisoning one.
  Transcript uuids are NOT usable here because resumed/forked sessions
  reuse them across distinct messages (work/render-format-once.md
  § 4.1).
- **Hits are verified against the content.** ``get`` compares a digest
  of the requesting tree's content against the digest stored at ``put``
  time (:func:`content_digest` — same field coverage as dataclass
  equality, with the per-tree ``compare=False`` fields excluded); a
  mismatch is a conflict, not a hit. This is the backstop for content
  built from cross-message context that resolves differently per tree —
  e.g. a tool result whose paired ``tool_use`` lives in another session,
  so the factory resolves the tool name in the combined tree but not in
  the session tree (work/render-format-once.md § 4.8). A digest is
  stored rather than the content object itself so the store holds only
  strings and bytes — picklable, spillable, and unable to pin object
  graphs — which is what lets phase 2 feed fragments to workers.
  Measured peak-RSS-neutral on the reference 803MB archive (the stored
  contents there alias objects the master entry list keeps alive
  anyway — work/render-format-once.md § 4.9), but the retention it
  removes is workload-shaped, and the structural property is the point.

The store is a pure performance feature: a renderer with no store (or a
content with no key) formats exactly as before.
"""

from __future__ import annotations

import dataclasses
import os
from hashlib import blake2b
from typing import TYPE_CHECKING, Optional, cast

from collections.abc import Iterable, Sequence

from pydantic import BaseModel

if TYPE_CHECKING:
    from .models import MessageContent

# A formatted fragment: (rendered_title, rendered_html, rendered_timestamp),
# exactly the triple _annotate_tree_for_render writes onto a TemplateMessage.
Fragment = tuple[str, str, str]

# (id(entry), part_ordinal) from MessageContent.fragment_key, extended by
# the consumer with the tree-context signature tuple.
StoreKey = tuple[object, ...]


def content_digest(content: "MessageContent") -> bytes:
    """Canonical 16-byte digest of a content's compare-relevant state.

    Stands in for dataclass ``__eq__`` in the store's hit-verification so
    the store need not retain the content object itself
    (work/render-format-once.md § 4.9). Field coverage matches dataclass
    equality exactly: compare-excluded fields (the per-tree
    ``message_index`` / ``fragment_key``) are skipped, everything else is
    fed into the hash with type tags and length prefixes so no two
    distinct structures can collide by concatenation.

    Divergence from ``__eq__`` is tolerated only in the safe direction:
    where Python equality is looser than this canonical form (``True ==
    1``, equal dicts with different insertion order, identity-``repr``
    objects), the digests differ, the lookup counts as a conflict, and
    the fragment is formatted fresh — never the reverse.
    """
    h = blake2b(digest_size=16)
    _feed(h, content)
    return h.digest()


def _feed(h: "blake2b", obj: object) -> None:  # noqa: C901 - flat type dispatch
    # Singletons / primitives first. bool before int (bool subclasses
    # int); str before Enum (value-equality StrEnums digest as their
    # value, matching ``==``), and likewise int catches IntEnums.
    if obj is None:
        h.update(b"N")
    elif obj is True:
        h.update(b"T")
    elif obj is False:
        h.update(b"F")
    elif isinstance(obj, str):
        b = obj.encode("utf-8", "surrogatepass")
        h.update(b"s%d:" % len(b))
        h.update(b)
    elif isinstance(obj, int):
        b = b"%d" % obj
        h.update(b"i%d:" % len(b))
        h.update(b)
    elif isinstance(obj, float):
        b = repr(obj).encode()
        h.update(b"f%d:" % len(b))
        h.update(b)
    elif isinstance(obj, bytes):
        h.update(b"y%d:" % len(obj))
        h.update(obj)
    elif isinstance(obj, (list, tuple)):
        # Distinct tags: [1] == (1,) is False in Python too.
        items = cast("Sequence[object]", obj)
        h.update(
            b"l%d:" % len(items) if isinstance(obj, list) else b"t%d:" % len(items)
        )
        for item in items:
            _feed(h, item)
    elif isinstance(obj, dict):
        mapping = cast("dict[object, object]", obj)
        h.update(b"d%d:" % len(mapping))
        for k, v in mapping.items():
            _feed(h, k)
            _feed(h, v)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # Dataclass __eq__ requires identical classes, then compares the
        # compare=True fields positionally — mirror both.
        cls = type(obj)
        _feed_class_tag(h, b"D", cls)
        for f in dataclasses.fields(obj):
            if f.compare:
                _feed(h, getattr(obj, f.name))
        h.update(b".")
    elif isinstance(obj, BaseModel):
        # Pydantic v2 __eq__ compares class, __dict__ and (for
        # extra="allow" models) __pydantic_extra__. Keys are sorted
        # because dict *order* does not affect pydantic equality.
        _feed_class_tag(h, b"P", type(obj))
        for k in sorted(obj.__dict__):
            _feed(h, k)
            _feed(h, obj.__dict__[k])
        extra = getattr(obj, "__pydantic_extra__", None)
        if extra:
            h.update(b"x")
            for k in sorted(extra):
                _feed(h, k)
                _feed(h, extra[k])
        h.update(b".")
    else:
        # Unknown object: fall back to repr. An identity-based repr makes
        # every lookup for that content a conflict (served fresh — safe),
        # and the conflict counter makes the cost visible.
        b = repr(obj).encode("utf-8", "surrogatepass")
        _feed_class_tag(h, b"r", type(obj))
        h.update(b"%d:" % len(b))
        h.update(b)


def _feed_class_tag(h: "blake2b", tag: bytes, cls: type) -> None:
    name = f"{cls.__module__}.{cls.__qualname__}".encode()
    h.update(tag)
    h.update(b"%d:" % len(name))
    h.update(name)


def fragment_store_enabled() -> bool:
    """Whether the fragment store should be used, per the environment.

    ``CLAUDE_CODE_LOG_FRAGMENT_STORE=0`` (or ``off``/``false``) disables it,
    mirroring ``CLAUDE_CODE_LOG_RENDER_CACHE_MB=0`` for the leaf memo — the
    switch exists for bisecting rendering differences, not for tuning.
    """
    value = os.environ.get("CLAUDE_CODE_LOG_FRAGMENT_STORE", "").strip().lower()
    return value not in ("0", "off", "false")


def fragment_store_forced() -> bool:
    """Whether the environment *explicitly* asks for the store.

    An explicit ``CLAUDE_CODE_LOG_FRAGMENT_STORE=1`` (or ``on``/``true``)
    overrides the memory valve in ``converter._make_fragment_store`` — the
    store is a RAM-for-CPU trade the valve declines on tight machines, and
    this is the knob for someone who wants the trade anyway. Unset means
    "enabled, but let the valve decide".
    """
    value = os.environ.get("CLAUDE_CODE_LOG_FRAGMENT_STORE", "").strip().lower()
    return value in ("1", "on", "true")


class RenderFragmentStore:
    """In-memory fragment store for a single conversion.

    Single-threaded by design: the parent renders pages and session files
    sequentially within one conversion (the process-level fan-out gives
    each worker its own store), so no locking is needed.
    """

    def __init__(self) -> None:
        self._fragments: dict[StoreKey, tuple[bytes, Fragment]] = {}
        # id(entry) → the entry's ordinal in the conversion's master
        # message list. Stamped fragment_keys carry the entry's Python
        # id (the only identity available where subsets are rendered);
        # stable_key() translates it to the ordinal, which two render
        # trees — and, later, two *processes* that load the same master
        # list — agree on. The translation also retires the id-reuse
        # caveat: an id missing from the map (a stamped entry that is
        # somehow not in the master list) declines caching instead of
        # risking a stale-id collision.
        self._entry_ordinals: dict[int, int] = {}
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        # Lookups whose stored content digest did not match the
        # requesting tree's — served fresh instead. A non-zero count is
        # expected on real archives (cross-tree factory context); a LARGE
        # one means a per-tree field is missing compare=False.
        self.conflicts = 0
        # Fragments the consumer computed but declined to store because
        # their output is tree-specific (per-tree ``#msg-d-{N}`` anchors —
        # see HtmlRenderer._annotate_tree_for_render).
        self.skipped = 0

    def set_entry_ordinals(self, entries: "Iterable[object]") -> None:
        """Record the master message list this conversion renders subsets of."""
        self._entry_ordinals = {id(entry): i for i, entry in enumerate(entries)}

    def set_entry_ordinal_map(self, mapping: dict[int, int]) -> None:
        """Share a prebuilt ``id(entry) → ordinal`` map (worker-side reuse)."""
        self._entry_ordinals = mapping

    def export_fragments(self) -> dict[StoreKey, tuple[bytes, Fragment]]:
        """The raw fragment mapping — digests and strings only, picklable.

        This is what crosses the process boundary in the render fan-out: a
        page worker exports its fragments back to the parent, and the
        parent slices them into the session units it dispatches (see
        render_dispatch.dispatch_render_units / render_pool._render_unit_worker).
        Returned by reference; callers must not mutate it.
        """
        return self._fragments

    def absorb(self, fragments: dict[StoreKey, tuple[bytes, Fragment]]) -> None:
        """Merge fragments computed elsewhere (a worker's delta or a fed
        slice). First writer wins, matching ``put`` — for a given key the
        digest pins the content either way, so order cannot change output.
        """
        for key, entry in fragments.items():
            if key not in self._fragments:
                self._fragments[key] = entry
                self._bytes += sum(len(part) for part in entry[1])

    def stable_key(self, fragment_key: tuple[int, int]) -> Optional[tuple[int, int]]:
        """Translate a stamped ``(id(entry), part_ordinal)`` to a stable key.

        Returns ``(master_list_ordinal, part_ordinal)``, or None when the
        id is unknown — the caller then skips the store for that message,
        which is always safe.
        """
        ordinal = self._entry_ordinals.get(fragment_key[0])
        if ordinal is None:
            return None
        return (ordinal, fragment_key[1])

    def get(self, key: StoreKey, content: "MessageContent") -> Optional[Fragment]:
        entry = self._fragments.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_digest, fragment = entry
        # Verify the stored render really was computed from an equal
        # content. The digest covers exactly the compare=True fields, so
        # a type mismatch (uuid-collision-style key accidents) or any
        # cross-tree content divergence compares unequal here.
        if stored_digest != content_digest(content):
            self.conflicts += 1
            return None
        self.hits += 1
        return fragment

    def put(self, key: StoreKey, content: "MessageContent", fragment: Fragment) -> None:
        if key in self._fragments:
            return
        self._fragments[key] = (content_digest(content), fragment)
        self._bytes += sum(len(part) for part in fragment)

    def stats(self) -> dict[str, int]:
        # Same shape as render_cache.ByteBoundedCache.stats() so the
        # DEBUG_TIMING report can print all three caches uniformly.
        return {
            "entries": len(self._fragments),
            "bytes": self._bytes,
            "hits": self.hits,
            "misses": self.misses,
            "conflicts": self.conflicts,
            "skipped": self.skipped,
        }

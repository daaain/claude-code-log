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
  stamped by the pass-2 render loop; the consumer appends a signature of
  the tree-derived render inputs (pair presence, model badge, agent
  depth, …) so a message that legitimately renders differently in the
  combined tree than in its session tree occupies two slots instead of
  poisoning one. The master message list keeps every entry alive for the
  whole conversion, so the ``id()`` is stable; transcript uuids are NOT
  usable here because resumed/forked sessions reuse them across distinct
  messages (work/render-format-once.md § 4.1).
- **Hits are verified against the content.** ``get`` compares the stored
  content object with the requesting tree's content (dataclass equality,
  with per-tree fields excluded via ``compare=False``); a mismatch is a
  conflict, not a hit. This is the backstop for content built from
  cross-message context that resolves differently per tree — e.g. a tool
  result whose paired ``tool_use`` lives in another session, so the
  factory resolves the tool name in the combined tree but not in the
  session tree (work/render-format-once.md § 4.8).

The store is a pure performance feature: a renderer with no store (or a
content with no key) formats exactly as before.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import MessageContent

# A formatted fragment: (rendered_title, rendered_html, rendered_timestamp),
# exactly the triple _annotate_tree_for_render writes onto a TemplateMessage.
Fragment = tuple[str, str, str]

# (id(entry), part_ordinal) from MessageContent.fragment_key, extended by
# the consumer with the tree-context signature tuple.
StoreKey = tuple[object, ...]


def fragment_store_enabled() -> bool:
    """Whether the fragment store should be used, per the environment.

    ``CLAUDE_CODE_LOG_FRAGMENT_STORE=0`` (or ``off``/``false``) disables it,
    mirroring ``CLAUDE_CODE_LOG_RENDER_CACHE_MB=0`` for the leaf memo — the
    switch exists for bisecting rendering differences, not for tuning.
    """
    value = os.environ.get("CLAUDE_CODE_LOG_FRAGMENT_STORE", "").strip().lower()
    return value not in ("0", "off", "false")


class RenderFragmentStore:
    """In-memory fragment store for a single conversion.

    Single-threaded by design: the parent renders pages and session files
    sequentially within one conversion (the process-level fan-out gives
    each worker its own store), so no locking is needed.
    """

    def __init__(self) -> None:
        self._fragments: dict[StoreKey, tuple["MessageContent", Fragment]] = {}
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        # Lookups whose stored content did not compare equal to the
        # requesting tree's content — served fresh instead. A non-zero
        # count is expected on real archives (cross-tree factory context);
        # a LARGE one means a per-tree field is missing compare=False.
        self.conflicts = 0
        # Fragments the consumer computed but declined to store because
        # their output is tree-specific (per-tree ``#msg-d-{N}`` anchors —
        # see HtmlRenderer._annotate_tree_for_render).
        self.skipped = 0

    def get(self, key: StoreKey, content: "MessageContent") -> Optional[Fragment]:
        entry = self._fragments.get(key)
        if entry is None:
            self.misses += 1
            return None
        stored_content, fragment = entry
        # Verify the stored render really was computed from an equal
        # content. Same-typed dataclasses compare field-wise; a type
        # mismatch (uuid-collision-style key accidents) compares unequal.
        if stored_content != content:
            self.conflicts += 1
            return None
        self.hits += 1
        return fragment

    def put(self, key: StoreKey, content: "MessageContent", fragment: Fragment) -> None:
        if key in self._fragments:
            return
        self._fragments[key] = (content, fragment)
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

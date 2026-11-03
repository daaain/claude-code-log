"""Nesting depth of decoded JSON from a transcript.

Transcript JSON is untrusted, and two things recurse once per level of
it: the decoder (``json.loads`` raises ``RecursionError`` past its
limit, so every decode site catches that beside ``ValueError``), and the
params renderers that turn a decoded value into nested tables. A value
nested deeper than ``MAX_DATA_DEPTH`` is not shown as a table; its site
falls back to its text rendering.
"""

from __future__ import annotations

from typing import Any, cast

MAX_DATA_DEPTH = 32


def _containers(value: Any) -> list[Any]:
    """The dict/list children of a dict or list (scalars are skipped)."""
    if isinstance(value, dict):
        items: Any = cast("dict[Any, Any]", value).values()
    else:
        items = cast("list[Any]", value)
    return [item for item in items if isinstance(item, (dict, list))]


def exceeds_depth(value: Any, limit: int = MAX_DATA_DEPTH) -> bool:
    """True when ``value``'s dicts/lists nest more than ``limit`` deep.

    A top-level container counts as one level. Iterative, and only
    containers are pushed, so a wide flat object costs one pass.
    """
    if not isinstance(value, (dict, list)):
        return False
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in _containers(item))
    return False

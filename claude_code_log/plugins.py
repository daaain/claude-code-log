"""Plugin discovery and dispatch for claude-code-log.

Implements the unified message-transformer plugin system described in
``work/tool-renderer-plugins.md``.

Plugins are discovered via the ``claude_code_log.plugins`` setuptools
entry-point group. Each entry yields a class implementing the
:class:`MessageTransformer` Protocol. The loader sorts transformers by
``(priority, __module__, __qualname__)`` and exposes them to factories
through :func:`apply_transformers`.

v1 scope: transformers run as a *post-classification* pass — each
factory builds its candidate ``MessageContent``, then the loader walks
the priority-ordered transformer list and lets the first matching
transformer (via ``applies_to`` MRO filter) rewrite the candidate.
This deviates slightly from the RFC's "interleaved with built-in
detectors" framing for implementation simplicity; the effect is the
same for every use case the RFC names (clmail hook-demotion, MCP tool
rendering) because plugin transformers always operate on a candidate
that the built-in chain has already classified (typically as
:class:`UserTextMessage` or generic :class:`ToolUseContent`).
"""

from __future__ import annotations

import logging
from importlib.metadata import EntryPoint, entry_points
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Optional,
    Protocol,
    cast,
    runtime_checkable,
)

if TYPE_CHECKING:
    # Visible to static type-checkers (pyright/mypy) and to
    # ``__all__`` validation; resolved at runtime via the
    # PEP-562 ``__getattr__`` further down.
    from .html.utils import render_markdown, render_markdown_collapsible

from .models import MessageContent, MessageMeta

logger = logging.getLogger(__name__)


# Entry-point group plugins register under.
ENTRY_POINT_GROUP = "claude_code_log.plugins"


@runtime_checkable
class MessageTransformer(Protocol):
    """A plugin contribution that rewrites a parsed ``MessageContent``.

    A transformer matches a candidate by its ``applies_to`` tuple (an
    MRO/subclass check) and, when matched, may return a replacement
    ``MessageContent`` (typically a plugin-defined subclass of one of
    the ``applies_to`` types) or ``None`` to pass through.

    Class attributes ``name``, ``priority``, ``applies_to`` are
    required metadata; the loader validates their presence explicitly
    because ``runtime_checkable`` only verifies method presence.

    See ``work/tool-renderer-plugins.md`` for the design rationale,
    priority table, and worked clmail example.
    """

    name: ClassVar[str]
    priority: ClassVar[int]
    applies_to: ClassVar[tuple[type[MessageContent], ...]]

    def transform(
        self,
        content: MessageContent,
        meta: MessageMeta,
    ) -> Optional[MessageContent]: ...

    # Contract — v1 trust requirement (not runtime-enforced):
    #
    # When ``transform()`` returns a non-None replacement, the
    # replacement SHOULD be an instance of either:
    #   - one of the ``applies_to`` types, or
    #   - a subclass thereof (typically a plugin-defined
    #     specialization).
    #
    # Returning a wholly unrelated MessageContent subclass (e.g. a
    # transformer with ``applies_to=(UserTextMessage,)`` returning a
    # ``SystemMessage``) is accepted at runtime but breaks the
    # caller's typing assumption (e.g. ``create_user_message``
    # narrows to ``UserMessageContent``). A v2 enhancement may add a
    # runtime isinstance check; v1 trusts plugin authors. Don't get
    # clever.


# ----------------------------------------------------------------------
# Loader (cached at module level so discovery happens once per process)
# ----------------------------------------------------------------------


_cached_transformers: Optional[list[MessageTransformer]] = None


def _validate_transformer_class(cls: type, ep_name: str) -> bool:
    """Return True iff ``cls`` looks like a valid MessageTransformer.

    Required class attributes:

    - ``name``: non-empty str
    - ``priority``: int
    - ``applies_to``: non-empty tuple of MessageContent subclasses

    ``transform`` is verified by the runtime_checkable Protocol on the
    instance; this function checks only the ClassVar metadata.
    """
    # We intentionally introspect arbitrary classes here; pyright can't
    # know their attribute types, so cast to Any for the metadata reads.
    cls_any = cast(Any, cls)
    missing: list[str] = [
        attr for attr in ("name", "priority", "applies_to") if not hasattr(cls, attr)
    ]
    if missing:
        logger.warning(
            "plugin %r (%r) missing required class attribute(s): %s",
            ep_name,
            cls,
            ", ".join(missing),
        )
        return False

    name: Any = cls_any.name
    if not isinstance(name, str) or not name:
        logger.warning("plugin %r: name must be non-empty str (got %r)", ep_name, name)
        return False
    priority: Any = cls_any.priority
    if not isinstance(priority, int):
        logger.warning("plugin %r: priority must be int (got %r)", ep_name, priority)
        return False
    applies_to: Any = cls_any.applies_to
    # All `repr(...)` calls here turn unknown-typed introspection values
    # into plain strings up front, so pyright sees only ``str`` flowing
    # into the logger args (avoids ``reportUnknownArgumentType``).
    if not isinstance(applies_to, tuple) or not applies_to:
        logger.warning(
            "plugin %r: applies_to must be a non-empty tuple (got %s)",
            ep_name,
            repr(cast(object, applies_to)),
        )
        return False
    for t in applies_to:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(t, type) or not issubclass(t, MessageContent):
            logger.warning(
                "plugin %r: applies_to entry %s is not a MessageContent subclass",
                ep_name,
                repr(cast(object, t)),
            )
            return False
    return True


def _load_single(ep: EntryPoint) -> Optional[MessageTransformer]:
    """Load and validate a single entry point. Returns instance or None."""
    try:
        cls = ep.load()
    except Exception as e:  # noqa: BLE001 — surface any load failure as a warning
        logger.warning("failed to load plugin entry point %r: %s", ep.name, e)
        return None
    if not isinstance(cls, type):
        logger.warning(
            "plugin %r: entry point must yield a class (got %r)", ep.name, cls
        )
        return None
    if not _validate_transformer_class(cls, ep.name):
        return None
    try:
        instance = cls()
    except Exception as e:  # noqa: BLE001
        logger.warning("plugin %r: failed to instantiate %r: %s", ep.name, cls, e)
        return None
    if not isinstance(instance, MessageTransformer):
        # Protocol check catches missing transform() method.
        logger.warning(
            "plugin %r: instance does not implement MessageTransformer "
            "(missing transform() method?)",
            ep.name,
        )
        return None
    return instance


def _sort_and_warn(transformers: list[MessageTransformer]) -> list[MessageTransformer]:
    """Sort by (priority, __module__, __qualname__) and warn on collisions.

    Tie-break key uses fully-qualified class identifier so two plugins
    shipping classes with the same short name don't get OS-dependent
    ordering. Collisions on (priority, applies_to) emit a warning.
    """
    transformers = sorted(
        transformers,
        key=lambda t: (t.priority, type(t).__module__, type(t).__qualname__),
    )
    # Group by (priority, applies_to) rather than walking adjacent pairs:
    # the sort key is (priority, module, qualname), so two transformers with
    # the same priority but different applies_to can sit between two
    # genuine collision partners. The adjacent-pair check would miss that
    # case. Group-by gives us every collision regardless of sort position.
    seen: dict[tuple[int, tuple[type[MessageContent], ...]], MessageTransformer] = {}
    for t in transformers:
        key = (t.priority, t.applies_to)
        first = seen.get(key)
        if first is not None:
            logger.warning(
                "priority tie for applies_to=%r at priority=%d: "
                "using %s.%s before %s.%s",
                t.applies_to,
                t.priority,
                type(first).__module__,
                type(first).__qualname__,
                type(t).__module__,
                type(t).__qualname__,
            )
        else:
            seen[key] = t
    return transformers


def load_transformers(*, force_reload: bool = False) -> list[MessageTransformer]:
    """Discover and return the priority-sorted transformer list.

    Cached at module scope; pass ``force_reload=True`` to re-scan
    (primarily for tests that install/uninstall plugins mid-run).
    """
    global _cached_transformers
    if _cached_transformers is not None and not force_reload:
        return _cached_transformers

    discovered: list[MessageTransformer] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if transformer := _load_single(ep):
            discovered.append(transformer)

    _cached_transformers = _sort_and_warn(discovered)
    return _cached_transformers


def reset_cache() -> None:
    """Clear the loader cache. Used by tests."""
    global _cached_transformers
    _cached_transformers = None


# ----------------------------------------------------------------------
# Dispatch helper for factories
# ----------------------------------------------------------------------


def apply_transformers(
    candidate: MessageContent,
    meta: MessageMeta,
) -> MessageContent:
    """Run transformers against ``candidate``; return the rewrite (or candidate).

    Walks the priority-ordered transformer list, calling ``transform()``
    on the first transformer whose ``applies_to`` matches the
    candidate's class (subclass check). First non-None return wins;
    candidate passes through unchanged if no transformer matches.

    Two defensive surfaces protect downstream code from misbehaving plugins:

    1. **Exception capture.** Transformer exceptions are caught and
       logged at WARNING so a buggy plugin doesn't crash the whole
       conversion; the candidate falls through to the next transformer.
    2. **Return-type enforcement.** The replacement must be a
       ``MessageContent`` instance AND match the transformer's
       ``applies_to`` MRO filter (typically a subclass of one of the
       declared types). A wholly-unrelated MessageContent — e.g. a
       UserTextMessage-targeting transformer returning a SystemMessage
       — is rejected with a warning; the candidate continues to the
       next transformer. This enforces the contract documented on the
       :class:`MessageTransformer` Protocol.
    """
    for transformer in load_transformers():
        if not isinstance(candidate, transformer.applies_to):
            continue
        try:
            replacement = transformer.transform(candidate, meta)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "plugin %r: transform() raised %s on %r; skipping",
                transformer.name,
                type(e).__name__,
                type(candidate).__name__,
            )
            continue
        if replacement is None:
            continue
        # Static type-checkers see the Protocol's Optional[MessageContent]
        # return annotation and consider this isinstance() unnecessary
        # — but plugin authors aren't bound by static typing at runtime,
        # so this catches the "returned a string / dict / None-ish" class
        # of plugin bug. Keep the runtime check; suppress the linter.
        if not isinstance(replacement, MessageContent):  # pyright: ignore[reportUnnecessaryIsInstance]
            logger.warning(
                "plugin %r: transform() returned non-MessageContent %r; skipping",
                transformer.name,
                type(replacement).__name__,
            )
            continue
        if not isinstance(replacement, transformer.applies_to):
            logger.warning(
                "plugin %r: transform() returned %r not matching "
                "applies_to=%r; skipping",
                transformer.name,
                type(replacement).__name__,
                tuple(t.__name__ for t in transformer.applies_to),
            )
            continue
        return replacement
    return candidate


# ----------------------------------------------------------------------
# Public re-exports for plugin authors
# ----------------------------------------------------------------------
# Helpers that live in ``claude_code_log/html/utils.py`` and are useful
# in plugin ``format_html`` methods. Re-exported under
# ``claude_code_log.plugins`` so plugin code imports from a stable
# namespace; the internal ``html/utils.py`` can churn (rename, split,
# restructure) without breaking plugin authors as long as the names
# below keep working.
#
# Resolved lazily via PEP-562 ``__getattr__`` because ``html`` itself
# imports from this module's siblings (``factories``, ``utils``), and
# an eager top-level import would close a circular loop during package
# init. Plugin authors are unaffected — ``from claude_code_log.plugins
# import render_markdown_collapsible`` Just Works because Python calls
# ``__getattr__`` when the name isn't already in the module dict.
#
# Add to ``_PUBLIC_HELPERS`` only on concrete plugin-author demand —
# a wider public surface is a wider commitment. See
# ``dev-docs/plugins.md`` §4.1 "Plugin-facing helpers" for the
# documented signatures.

_PUBLIC_HELPERS: frozenset[str] = frozenset(
    {"render_markdown", "render_markdown_collapsible"}
)


def __getattr__(name: str) -> Any:  # PEP 562
    if name in _PUBLIC_HELPERS:
        from .html.utils import (
            render_markdown as _rm,
            render_markdown_collapsible as _rmc,
        )

        globals()["render_markdown"] = _rm
        globals()["render_markdown_collapsible"] = _rmc
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ENTRY_POINT_GROUP",
    "MessageTransformer",
    "apply_transformers",
    "load_transformers",
    "render_markdown",
    "render_markdown_collapsible",
    "reset_cache",
]

"""Images held by reference, and their bytes found by digest.

A transcript carries each image inline, base64-encoded inside its entry:
a pasted screenshot, a Read tool's result. Held that way, every entry in
memory, every slice fed to a render worker and every cached row carries
the image again, a third larger than its bytes. So parsing swaps each
image's source for a reference,

    {"type": "ref", "digest": <blake2b-128 of the bytes>, "media_type": ...}

(the `ImageRef` model, or that dict inside a tool result), and keeps the
bytes here, once per distinct image, until the cache has stored them
(`cache_images`). From then on they are read back from the cache, and
only where an image is written out: exported as a file that does not
exist yet, embedded as a ``data:`` URL, or exported as JSON.

The swap is made only when it is lossless: a source with exactly the
keys ``type``, ``media_type`` and ``data``, whose base64 re-encodes to
the same text, and at least `MIN_EXTRACTED_CHARS` long (a smaller image
costs less inline than as a reference). Anything else stays inline, so
every reader takes both forms, through `image_bytes` and friends.

Where the bytes are, in the order a lookup tries them:

- **Held**: parsed in this process and not yet committed to a cache, or
  parsed with no cache at all. The cache releases them once a write
  that stores them commits (`release`).
- **Recently read**: a small cache of bytes read back from a cache file,
  so that rendering an image twice (combined page, then session file)
  reads it once.
- **The caches this process opened**: each `CacheManager` registers its
  database (`register_cache`), read-only render workers included, and a
  digest names the same bytes in any of them.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import re
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    ParamSpec,
    TypeVar,
    cast,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")

REF_TYPE = "ref"
#: Base64 shorter than this stays inline: a reference and a stored blob
#: cost more than the bytes they would move.
MIN_EXTRACTED_CHARS = 2048
#: In a JSON text, the sign that it may hold an inline base64 source
#: (never inside a string value, where its quotes are escaped).
BASE64_MARK = '"base64"'
#: The same for a source that is already a reference, which a transcript
#: never carries: one there is forged (`take_images`).
REF_MARK = f'"{REF_TYPE}"'
#: What a digest looks like: it becomes part of a file name.
DIGEST_RE = re.compile(r"[0-9a-f]{32}")
_SOURCE_KEYS = frozenset({"type", "media_type", "data"})
# The caches a lookup tries, most recently registered first.
_MAX_CACHES = 8
# Bytes read back from a cache, kept for the next lookup of the same image.
_RECENT_MAX_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ImageBlob:
    """One image's bytes, keyed by their digest."""

    digest: str
    media_type: str
    data: bytes


def digest_of(raw: bytes) -> str:
    """An image's digest: what a reference names it by."""
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


def image_sources(node: Any) -> Iterable[Dict[str, Any]]:
    """Every ``image`` item's ``source`` dict under ``node``, a dumped or
    parsed entry."""
    stack: List[Any] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            mapping = cast(Dict[str, Any], item)
            source = mapping.get("source")
            if mapping.get("type") == "image" and isinstance(source, dict):
                yield cast(Dict[str, Any], source)
            stack.extend(mapping.values())
        elif isinstance(item, list):
            stack.extend(cast(List[Any], item))


def extract_blob(source: Dict[str, Any]) -> Optional[ImageBlob]:
    """The blob for an inline base64 source, if it can be swapped for a
    reference losslessly."""
    if source.keys() != _SOURCE_KEYS or source.get("type") != "base64":
        return None
    media_type = source.get("media_type")
    data = source.get("data")
    if not isinstance(media_type, str) or not isinstance(data, str):
        return None
    if len(data) < MIN_EXTRACTED_CHARS:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    if base64.b64encode(raw).decode("ascii") != data:
        return None
    return ImageBlob(digest_of(raw), media_type, raw)


def ref_source(digest: str, media_type: str) -> Dict[str, Any]:
    """A reference, as a dumped `ImageRef` reads."""
    return {"type": REF_TYPE, "digest": digest, "media_type": media_type}


# ---------------------------------------------------------------------------
# The held bytes
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_held: Dict[str, bytes] = {}
_recent: "OrderedDict[str, bytes]" = OrderedDict()
_recent_bytes = 0
_caches: List[Path] = []


def take_images(entry: Dict[str, Any]) -> None:
    """Swap every image source in a parsed entry (a JSONL line's dict) for
    a reference, in place, holding its bytes.

    Called by every transcript parser before validation, so the models
    are built with references. Gate it on the line's text holding
    `BASE64_MARK` or `REF_MARK`: most entries hold no image and are never
    walked.

    A source that is already a reference did not come from Claude Code,
    which writes images inline: it is emptied (a placeholder), so a
    transcript can't point a page at an image of another session's.
    """
    for source in list(image_sources(entry)):
        if source.get("type") == REF_TYPE:
            media_type = source.get("media_type")
            source.clear()
            source.update(
                {
                    "type": "base64",
                    "media_type": media_type if isinstance(media_type, str) else "",
                    "data": "",
                }
            )
            continue
        blob = extract_blob(source)
        if blob is None:
            continue
        hold(blob.digest, blob.data)
        source.clear()
        source.update(ref_source(blob.digest, blob.media_type))


def hold(digest: str, data: bytes) -> None:
    """Keep an image's bytes until a cache stores them."""
    with _lock:
        _held.setdefault(digest, data)


def hold_image(raw: bytes, media_type: str) -> Dict[str, Any]:
    """Hold an image's bytes; returns the reference to put in its entry
    (for a provider that builds image sources itself)."""
    digest = digest_of(raw)
    hold(digest, raw)
    return ref_source(digest, media_type)


def held_bytes(digest: str) -> Optional[bytes]:
    """The bytes held for ``digest``, if they are not yet in a cache."""
    with _lock:
        return _held.get(digest)


def held_images() -> Dict[str, bytes]:
    """A copy of every held image, for a process that cannot read them
    from a cache (a render worker spawned before they were stored)."""
    with _lock:
        return dict(_held)


def release(digests: Iterable[str]) -> None:
    """Drop the held bytes a cache write has just committed: from now on
    they are read from the cache."""
    with _lock:
        for digest in digests:
            _held.pop(digest, None)


def releases_what_it_parses(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Let go, when ``fn`` returns, of the images it parsed that are still
    held: those no cache stored (it ran without one), which nothing reads
    once its output is written. Keeps a long-lived process that converts
    without a cache (or an all-projects pass with ``--no-cache``) from
    holding every image it ever parsed."""

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _lock:
            before = set(_held)
        try:
            return fn(*args, **kwargs)
        finally:
            with _lock:
                for digest in [d for d in _held if d not in before]:
                    del _held[digest]

    return wrapper


def register_cache(db_path: Path) -> None:
    """Look in this cache database for images not held in memory."""
    try:
        resolved = db_path.resolve()
    except OSError:
        return
    with _lock:
        if resolved in _caches:
            _caches.remove(resolved)
        _caches.insert(0, resolved)
        del _caches[_MAX_CACHES:]


def registered_caches() -> List[Path]:
    """The caches a lookup tries, in order."""
    with _lock:
        return list(_caches)


def reset() -> None:
    """Forget every held image and registered cache (between tests, or
    for a long-lived library caller that parses without a cache and is
    done with what it parsed)."""
    global _recent_bytes
    with _lock:
        _held.clear()
        _recent.clear()
        _recent_bytes = 0
        _caches.clear()


def _remember(digest: str, data: bytes) -> None:
    global _recent_bytes
    if len(data) > _RECENT_MAX_BYTES // 4:
        return
    with _lock:
        if digest in _recent:
            _recent.move_to_end(digest)
            return
        _recent[digest] = data
        _recent_bytes += len(data)
        while _recent_bytes > _RECENT_MAX_BYTES:
            _, dropped = _recent.popitem(last=False)
            _recent_bytes -= len(dropped)


def _recalled(digest: str) -> Optional[bytes]:
    with _lock:
        data = _held.get(digest)
        if data is None:
            data = _recent.get(digest)
            if data is not None:
                _recent.move_to_end(digest)
        return data


def _read_from_cache(db_path: Path, digest: str) -> Optional[bytes]:
    """The bytes a cache database holds for ``digest``.

    A connection of its own, read-only and closed at once: the lookup may
    come from any thread, and a handle kept open would stop the cache
    file from being removed on Windows.
    """
    try:
        conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT data FROM image_blobs WHERE digest = ?", (digest,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return bytes(row[0]) if row is not None else None


def bytes_for_digest(digest: str) -> Optional[bytes]:
    """An image's bytes by digest: held, recently read, or from a cache;
    None when none of them has it."""
    if not DIGEST_RE.fullmatch(digest):
        return None
    data = _recalled(digest)
    if data is not None:
        return data
    with _lock:
        caches = list(_caches)
    for db_path in caches:
        data = _read_from_cache(db_path, digest)
        if data is not None:
            _remember(digest, data)
            return data
    return None


# ---------------------------------------------------------------------------
# Reading an image source, whichever form it has
# ---------------------------------------------------------------------------

# An image source: an `ImageSource` or `ImageRef` model, or the dict
# either dumps to (a tool result's image).
SourceLike = Any


def _field(source: SourceLike, name: str) -> Any:
    if isinstance(source, Mapping):
        return cast(Mapping[str, Any], source).get(name)
    return getattr(source, name, None)


def image_media_type(source: SourceLike) -> str:
    """The media type a source declares ("" if none)."""
    value = _field(source, "media_type")
    return value if isinstance(value, str) else ""


def is_ref(source: SourceLike) -> bool:
    """Whether a source is a reference rather than inline base64."""
    return _field(source, "type") == REF_TYPE


def image_digest(source: SourceLike) -> Optional[str]:
    """The digest of a source's bytes: a reference's own, or computed from
    inline base64. None for a source that is neither (or bad base64)."""
    if is_ref(source):
        digest = _field(source, "digest")
        if isinstance(digest, str) and DIGEST_RE.fullmatch(digest):
            return digest
        return None
    raw = image_bytes(source)
    return digest_of(raw) if raw is not None else None


def image_bytes(source: SourceLike) -> Optional[bytes]:
    """A source's decoded bytes, or None when they can't be had: inline
    data that is not strict base64, or a reference no cache holds any
    longer (render a placeholder)."""
    if is_ref(source):
        digest = _field(source, "digest")
        return bytes_for_digest(digest) if isinstance(digest, str) else None
    if _field(source, "type") != "base64":
        return None
    data = _field(source, "data")
    if not isinstance(data, str) or not data:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def image_base64(source: SourceLike) -> Optional[str]:
    """A source's bytes as base64, as a ``data:`` URL or a JSON export
    carries them; None as for `image_bytes`."""
    if not is_ref(source):
        data = _field(source, "data")
        if _field(source, "type") == "base64" and isinstance(data, str) and data:
            return data
        return None
    raw = image_bytes(source)
    return base64.b64encode(raw).decode("ascii") if raw is not None else None


def inline_images(node: Any, *, missing_ok: bool = True) -> None:
    """Put each referenced image's base64 back into ``node`` (a dumped
    entry), in place, as the transcript had it.

    A reference whose bytes can't be found becomes an empty source
    (rendered as a placeholder), or with ``missing_ok=False`` raises
    `LookupError`.
    """
    for source in list(image_sources(node)):
        if source.get("type") != REF_TYPE:
            continue
        data = image_base64(source)
        if data is None:
            if not missing_ok:
                raise LookupError(source.get("digest"))
            data = ""
        media_type = image_media_type(source)
        source.clear()
        source.update({"type": "base64", "media_type": media_type, "data": data})

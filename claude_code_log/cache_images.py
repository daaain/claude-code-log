"""Cached images, stored once outside the entry blobs.

The cache stores an entry as ``zlib(json(entry.model_dump()))``. An image
in it (``{"type": "image", "source": {"type": "base64", ...}}``, pasted or
returned by a tool) would sit there base64-encoded, zlib-wrapped for no
gain, and once per file whose rows carry it: a sub-agent's rows are also
spliced into its trunk's. So the serialiser swaps each image's source for
a reference to the ``image_blobs`` table, which holds the decoded bytes
once per distinct image, and the reader swaps it back. Every consumer
downstream sees the entries exactly as parsed.

The swap is lossless or not made: a source with any other key, or whose
base64 does not re-encode to the same text, stays inline, as does a small
image, whose row is cheaper than a reference.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, cast

REF_TYPE = "ref"
# Base64 shorter than this stays inline: the blob row and the reference
# cost more than the bytes they would move.
MIN_EXTRACTED_CHARS = 2048
# The cheap gate before walking an entry: its JSON names a base64 source.
_BASE64_MARK = '"base64"'
_SOURCE_KEYS = frozenset({"type", "media_type", "data"})
# How many digests one blob lookup binds (well under SQLite's variable limit).
_LOOKUP_CHUNK = 500


@dataclass(frozen=True)
class ImageBlob:
    """One image's bytes, keyed by their digest."""

    digest: str
    media_type: str
    data: bytes


class MissingImageBlob(LookupError):
    """A cached row references an image the cache no longer holds."""


def _image_sources(node: Any) -> Iterable[Dict[str, Any]]:
    """Every ``image`` item's ``source`` dict under ``node``."""
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


def _extract(source: Dict[str, Any]) -> Optional[ImageBlob]:
    """The blob for an inline base64 source, if it can be swapped losslessly."""
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
    digest = hashlib.blake2b(raw, digest_size=16).hexdigest()
    return ImageBlob(digest, media_type, raw)


def dump_entry(dumped: Dict[str, Any]) -> Tuple[str, List[str], List[ImageBlob]]:
    """An entry's cached JSON, the digests it references, and their blobs.

    ``dumped`` is ``entry.model_dump()`` and is changed in place: each image
    source that can be swapped becomes ``{"type": "ref", "digest": ...,
    "media_type": ...}``. Most entries hold no image, and their JSON is
    only searched for one, never walked.
    """
    text = json.dumps(dumped, separators=(",", ":"))
    if _BASE64_MARK not in text:
        return text, [], []
    refs: List[str] = []
    blobs: Dict[str, ImageBlob] = {}
    for source in list(_image_sources(dumped)):
        blob = _extract(source)
        if blob is None:
            continue
        source.clear()
        source.update(
            {"type": REF_TYPE, "digest": blob.digest, "media_type": blob.media_type}
        )
        refs.append(blob.digest)
        blobs.setdefault(blob.digest, blob)
    if not refs:
        return text, [], []
    return json.dumps(dumped, separators=(",", ":")), refs, list(blobs.values())


def restore_images(
    content: Dict[str, Any],
    blobs: Mapping[str, Tuple[str, bytes]],
    *,
    missing_ok: bool = False,
) -> None:
    """Put each referenced image's base64 back into ``content``, in place.

    ``blobs`` maps a digest to its media type and bytes; each source keeps
    the media type of its own reference. A reference with
    no blob raises `MissingImageBlob`, or with ``missing_ok`` becomes an
    empty source, which renders as a placeholder (for a session whose
    transcript is gone, so cannot be parsed again).
    """
    for source in list(_image_sources(content)):
        if source.get("type") != REF_TYPE:
            continue
        digest = source.get("digest")
        found = blobs.get(digest) if isinstance(digest, str) else None
        # The reference's own media type: identical bytes cited under two
        # media types share one blob, which keeps only the first.
        media_type = str(source.get("media_type", ""))
        if found is None:
            if not missing_ok:
                raise MissingImageBlob(digest)
            data = ""
        else:
            data = base64.b64encode(found[1]).decode("ascii")
        source.clear()
        source.update({"type": "base64", "media_type": media_type, "data": data})


def row_refs(image_refs: Optional[str]) -> List[str]:
    """The digests in a row's ``image_refs`` column."""
    if not image_refs:
        return []
    try:
        decoded: Any = json.loads(image_refs)
    except ValueError:
        return []
    if not isinstance(decoded, list):
        return []
    return [d for d in cast(List[Any], decoded) if isinstance(d, str)]


def load_blobs(
    conn: sqlite3.Connection, digests: Iterable[str]
) -> Dict[str, Tuple[str, bytes]]:
    """The blobs for ``digests`` (unqualified ``image_blobs``, so a refresh's
    overlay view is read when there is one)."""
    wanted: Sequence[str] = sorted(set(digests))
    found: Dict[str, Tuple[str, bytes]] = {}
    for start in range(0, len(wanted), _LOOKUP_CHUNK):
        chunk = wanted[start : start + _LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        for digest, media_type, data in conn.execute(
            f"SELECT digest, media_type, data FROM image_blobs WHERE digest IN ({marks})",
            tuple(chunk),
        ):
            found[digest] = (media_type, bytes(data))
    return found


# Insert a blob, or mark an existing one in use again: a blob the sweep
# found unused restarts its grace period when a row references it.
UPSERT_BLOB = (
    "INSERT INTO image_blobs (digest, media_type, data) VALUES (?, ?, ?)"
    " ON CONFLICT(digest) DO UPDATE SET orphaned_at = NULL"
)


def write_blobs(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> None:
    """Store the blobs ``rows`` carry (``_image_blobs``), before the rows
    themselves, in the caller's transaction."""
    blobs: Dict[str, ImageBlob] = {}
    for row in rows:
        for blob in row.get("_image_blobs") or ():
            blobs.setdefault(blob.digest, blob)
    if blobs:
        conn.executemany(
            UPSERT_BLOB,
            [(b.digest, b.media_type, b.data) for b in blobs.values()],
        )


# A spooled file's blobs, into the cache, before its rows: exactly those
# its rows reference, found through their own `image_refs`.
MERGE_BLOBS = """
    INSERT INTO image_blobs (digest, media_type, data)
    SELECT b.digest, b.media_type, b.data FROM spool.image_blobs b
    WHERE b.digest IN (
        SELECT j.value FROM spool.messages s, json_each(s.image_refs) j
        WHERE s.file_id = ? AND s.image_refs IS NOT NULL
    )
    ON CONFLICT(digest) DO UPDATE SET orphaned_at = NULL
"""

# The blobs no row references, as of the statement.
_LIVE = """
    SELECT j.value FROM messages m, json_each(m.image_refs) j
    WHERE m.image_refs IS NOT NULL
"""

#: A blob no row has referenced for this long is deleted by the sweep.
SWEEP_GRACE_SECONDS = 3600.0


def sweep_blobs(conn: sqlite3.Connection, now: float, grace: float) -> int:
    """Mark the blobs no row references, unmark the ones referenced again,
    and delete those unreferenced for longer than ``grace``; returns the
    number deleted. The caller holds the write transaction (and, so that
    two sweeps never race, the pass lock)."""
    conn.execute(
        f"UPDATE image_blobs SET orphaned_at = NULL"
        f" WHERE orphaned_at IS NOT NULL AND digest IN ({_LIVE})"
    )
    deleted = conn.execute(
        f"DELETE FROM image_blobs WHERE orphaned_at < ? AND digest NOT IN ({_LIVE})",
        (now - grace,),
    ).rowcount
    conn.execute(
        f"UPDATE image_blobs SET orphaned_at = ?"
        f" WHERE orphaned_at IS NULL AND digest NOT IN ({_LIVE})",
        (now,),
    )
    return int(deleted or 0)

"""Cached images, stored once outside the entry blobs.

The cache stores an entry as ``zlib(json(entry.model_dump()))``. An
image in it would sit there base64-encoded, zlib-wrapped for no gain,
and once per file whose rows carry it: a sub-agent's rows are also
spliced into its trunk's. Parsed entries already hold each image as a
reference (`image_store`), so a row carries the reference, the
``image_blobs`` table holds the decoded bytes once per distinct image,
and the row's ``image_refs`` column lists the digests it references,
for the merge and the sweep to read with ``json_each``.

An entry built with an inline base64 source (by a test, or by code that
makes its own) has it swapped here, under the same rules as at parse
time; one that can't be swapped exactly stays inline.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from typing import cast

from .image_store import (
    BASE64_MARK,
    REF_TYPE,
    ImageBlob,
    extract_blob,
    held_bytes,
    image_sources,
    ref_source,
)

# In a compact JSON text, the sign that it may hold a reference.
_REF_MARK = f'"type":"{REF_TYPE}"'
# How many digests one blob lookup binds (well under SQLite's variable limit).
_LOOKUP_CHUNK = 500


class MissingImageBlob(LookupError):
    """A cached row references an image the cache no longer holds."""


def dump_entry(dumped: Dict[str, Any]) -> Tuple[str, List[str], List[ImageBlob]]:
    """An entry's cached JSON, the digests it references, and the blobs to
    store with it.

    ``dumped`` is ``entry.model_dump()`` and is changed in place: an
    inline source that can be swapped becomes a reference. The blobs are
    those whose bytes this process still holds (`image_store.held_bytes`)
    or has just swapped out. A reference to bytes no longer held names an
    image a cache already stores, since that is where they went. Most
    entries hold no image, and their JSON is only searched for one, never
    walked.
    """
    text = json.dumps(dumped, separators=(",", ":"))
    if BASE64_MARK not in text and _REF_MARK not in text:
        return text, [], []
    refs: List[str] = []
    blobs: Dict[str, ImageBlob] = {}
    swapped = False
    for source in list(image_sources(dumped)):
        if source.get("type") == REF_TYPE:
            digest = source.get("digest")
            if not isinstance(digest, str):
                continue
            refs.append(digest)
            if digest not in blobs:
                data = held_bytes(digest)
                if data is not None:
                    media_type = str(source.get("media_type", ""))
                    blobs[digest] = ImageBlob(digest, media_type, data)
            continue
        blob = extract_blob(source)
        if blob is None:
            continue
        source.clear()
        source.update(ref_source(blob.digest, blob.media_type))
        refs.append(blob.digest)
        blobs.setdefault(blob.digest, blob)
        swapped = True
    if swapped:
        text = json.dumps(dumped, separators=(",", ":"))
    return text, refs, list(blobs.values())


def restore_images(
    content: Dict[str, Any],
    blobs: Mapping[str, Tuple[str, bytes]],
    *,
    missing_ok: bool = False,
) -> None:
    """Put each referenced image's base64 back into ``content``, in place,
    for a copy that leaves the cache (a session exported as JSONL).

    ``blobs`` maps a digest to its media type and bytes; each source keeps
    the media type of its own reference (identical bytes cited under two
    media types share one blob). A reference with no blob raises
    `MissingImageBlob`, or with ``missing_ok`` becomes an empty source.
    """
    for source in list(image_sources(content)):
        if source.get("type") != REF_TYPE:
            continue
        digest = source.get("digest")
        found = blobs.get(digest) if isinstance(digest, str) else None
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


def _chunks(digests: Iterable[str]) -> Iterable[Sequence[str]]:
    wanted: Sequence[str] = sorted(set(digests))
    for start in range(0, len(wanted), _LOOKUP_CHUNK):
        yield wanted[start : start + _LOOKUP_CHUNK]


def load_blobs(
    conn: sqlite3.Connection, digests: Iterable[str]
) -> Dict[str, Tuple[str, bytes]]:
    """The blobs for ``digests`` (unqualified ``image_blobs``, so a refresh's
    overlay view is read when there is one)."""
    found: Dict[str, Tuple[str, bytes]] = {}
    for chunk in _chunks(digests):
        marks = ",".join("?" * len(chunk))
        for digest, media_type, data in conn.execute(
            f"SELECT digest, media_type, data FROM image_blobs WHERE digest IN ({marks})",
            tuple(chunk),
        ):
            found[digest] = (media_type, bytes(data))
    return found


def missing_blobs(conn: sqlite3.Connection, digests: Iterable[str]) -> Set[str]:
    """Which of ``digests`` the cache holds no blob for (read through the
    overlay as `load_blobs` is), without reading any bytes."""
    missing: Set[str] = set()
    for chunk in _chunks(digests):
        marks = ",".join("?" * len(chunk))
        present = {
            row[0]
            for row in conn.execute(
                f"SELECT digest FROM image_blobs WHERE digest IN ({marks})",
                tuple(chunk),
            )
        }
        missing.update(d for d in chunk if d not in present)
    return missing


# Insert a blob, or mark an existing one in use again: a blob the sweep
# found unused restarts its grace period when a row references it.
UPSERT_BLOB = (
    "INSERT INTO image_blobs (digest, media_type, data) VALUES (?, ?, ?)"
    " ON CONFLICT(digest) DO UPDATE SET orphaned_at = NULL"
)


def row_blobs(rows: Iterable[Mapping[str, Any]]) -> Dict[str, ImageBlob]:
    """The blobs serialised rows carry (``_image_blobs``), once each."""
    blobs: Dict[str, ImageBlob] = {}
    for row in rows:
        for blob in row.get("_image_blobs") or ():
            blobs.setdefault(blob.digest, blob)
    return blobs


def write_blobs(
    conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]
) -> Set[str]:
    """Store the blobs ``rows`` carry, before the rows themselves, in the
    caller's transaction; returns their digests, for the caller to
    `image_store.release` once that commits."""
    blobs = row_blobs(rows)
    if blobs:
        conn.executemany(
            UPSERT_BLOB,
            [(b.digest, b.media_type, b.data) for b in blobs.values()],
        )
    return set(blobs)


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

"""Full-archive search over the SQLite cache.

This module is deliberately HTTP-free: it is plain functions over a
`sqlite3.Connection`, so the tests that matter run without a socket and the
server (`server.py`) is only an adapter.

## Why an extracted-text index at all

The cache already holds every message, but the body is
`zlib.compress(json.dumps(entry.model_dump()))` in a BLOB, which SQL cannot
scan. So the searchable text has to be extracted into an FTS5 index.

## Why contentless

`content=''` costs +20% on a real 1.29 GB cache (253 MB) where a
content-stored FTS5 table costs +84% (1,080 MB) — it stores its own copy of
all the text. The price is that `snippet()` and `highlight()` **silently
return NULL** on contentless tables (not an error), so excerpts are built in
Python from the BLOB for the handful of rows in a result page. That costs
~0.3 ms for 20 rows, and it is the reason the BLOB stays in the query path.

## Two invariants that are easy to break

1. **`CROSS JOIN`, always, when filtering** (see `_build_query`). With a
   plain `JOIN` SQLite makes `messages` the outer loop and probes FTS by
   rowid — the FTS5 anti-pattern — and a project-filtered search goes from
   2.4 ms to 2,012 ms. There is a test asserting the query plan.
2. **Base64 never enters the index.** A naive "flatten every JSON value"
   extractor pulls inline image payloads in, inflating the index by 28%
   (253 MB -> 335 MB). `_flatten` drops image sources and long
   base64-shaped strings.
"""

from __future__ import annotations

import json
import re
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence, cast

# ---------------------------------------------------------------------------
# Field groups
# ---------------------------------------------------------------------------

#: Every group the extractor can produce, in FTS column order.
SEARCH_FIELDS: tuple[str, ...] = (
    "text",
    "thinking",
    "tool_input",
    "tool_result",
    "attachment",
    "meta",
)

#: Searched unless told otherwise. `tool_result` is indexed but excluded
#: here: it is 69% of the archive's text and dominates results with file
#: dumps and tracebacks. Users opt into it per-request or by config.
DEFAULT_SEARCH_FIELDS: tuple[str, ...] = (
    "text",
    "thinking",
    "tool_input",
    "attachment",
    "meta",
)

#: Indexed by default — everything, so enabling `tool_result` at search time
#: never requires a reindex.
DEFAULT_INDEX_FIELDS: tuple[str, ...] = SEARCH_FIELDS

#: Message types that never carry searchable text *and* never render a card,
#: so indexing them would only add rows that can't be linked to. `progress`
#: is 10% of a real archive at 0 bytes of text.
SKIP_TYPES: frozenset[str] = frozenset({"progress"})

#: Message types that carry text but render no message card, so results must
#: link at session granularity rather than to a message anchor.
SESSION_ONLY_TYPES: frozenset[str] = frozenset({"ai-title", "queue-operation"})

#: Bump when the extractor's output changes; forces a rebuild.
EXTRACTOR_VERSION = 1

FTS_TABLE = "message_fts"
META_TABLE = "search_index_meta"
FILES_TABLE = "search_indexed_files"
ROWS_TABLE = "search_indexed_rows"

ENV_SEARCH_FIELDS = "CLAUDE_CODE_LOG_SEARCH_FIELDS"
ENV_INDEX_FIELDS = "CLAUDE_CODE_LOG_INDEX_FIELDS"


# ---------------------------------------------------------------------------
# Field-spec parsing (shared by the CLI flags and the env vars)
# ---------------------------------------------------------------------------


def parse_field_spec(
    spec: Optional[str],
    default: Sequence[str],
    *,
    valid: Sequence[str] = SEARCH_FIELDS,
) -> tuple[str, ...]:
    """Resolve a field spec to a concrete, ordered tuple of field names.

    Accepts an absolute list (``text,thinking``), the words ``all`` /
    ``none``, or additive/subtractive deltas against the default
    (``+tool_result``, ``-thinking``, and combinations). Mixing an absolute
    name with a delta is rejected rather than guessed at.
    """
    if spec is None or not spec.strip():
        return tuple(default)

    tokens = [t.strip() for t in spec.split(",") if t.strip()]
    if not tokens:
        return tuple(default)

    lowered = [t.lower() for t in tokens]
    if lowered == ["all"]:
        return tuple(valid)
    if lowered == ["none"]:
        return ()

    deltas = [t for t in lowered if t[0] in "+-"]
    absolutes = [t for t in lowered if t[0] not in "+-"]
    if deltas and absolutes:
        raise ValueError(
            "Field spec mixes absolute names with +/- deltas "
            f"({spec!r}); use one style or the other."
        )

    if deltas:
        selected = list(default)
        for token in deltas:
            name, op = token[1:], token[0]
            if name not in valid:
                raise ValueError(
                    f"Unknown search field {name!r}. Valid: {', '.join(valid)}"
                )
            if op == "+":
                if name not in selected:
                    selected.append(name)
            elif name in selected:
                selected.remove(name)
        return tuple(f for f in valid if f in selected)

    for name in absolutes:
        if name not in valid:
            raise ValueError(
                f"Unknown search field {name!r}. Valid: {', '.join(valid)}"
            )
    # Normalise to canonical order so the stored index-field list is stable.
    return tuple(f for f in valid if f in absolutes)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

# A long run with no whitespace, in the base64 alphabet: an inline payload,
# not prose. Deliberately conservative (512+ chars) so real tokens survive.
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_\-]{512,}$")

_MAX_DEPTH = 12


def _flatten(value: Any, depth: int = 0) -> str:
    """Flatten an arbitrary JSON value to searchable text.

    Drops image payloads: the `source` / `data` keys of an image block and
    any base64-shaped string. See the module docstring for why this matters.
    """
    if value is None or depth > _MAX_DEPTH:
        return ""
    if isinstance(value, str):
        return "" if _BASE64_RE.match(value) else value
    if isinstance(value, bool):
        # `True`/`False` as words would be noise in every tool input.
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        items = cast("list[Any]", value)
        parts = (_flatten(item, depth + 1) for item in items)
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        if mapping.get("type") == "base64":
            return ""
        parts = (
            _flatten(item, depth + 1)
            for key, item in mapping.items()
            if key not in ("data", "source")
        )
        return "\n".join(p for p in parts if p)
    return ""


def _walk_content_items(items: Iterable[Any], out: dict[str, list[str]]) -> None:
    """Sort a list of content items into field groups."""
    for item in items:
        if not isinstance(item, dict):
            out["text"].append(_flatten(item))
            continue
        block = cast("dict[str, Any]", item)
        kind = block.get("type")
        if kind == "text":
            out["text"].append(_flatten(block.get("text")))
        elif kind == "thinking":
            out["thinking"].append(_flatten(block.get("thinking")))
        elif kind == "tool_use":
            out["tool_input"].append(_flatten(block.get("name")))
            out["tool_input"].append(_flatten(block.get("input")))
        elif kind == "tool_result":
            content = block.get("content")
            if isinstance(content, list):
                # A tool result can itself hold text/image blocks. Collect
                # them all under tool_result rather than leaking text blocks
                # into the `text` group, which would make a file dump look
                # like prose the user wrote.
                nested: dict[str, list[str]] = {f: [] for f in SEARCH_FIELDS}
                _walk_content_items(cast("list[Any]", content), nested)
                out["tool_result"].extend(
                    part for group in SEARCH_FIELDS for part in nested[group] if part
                )
            else:
                out["tool_result"].append(_flatten(content))
        elif kind == "image":
            pass  # base64 only, never searchable


def extract_search_text(entry: dict[str, Any]) -> dict[str, str]:
    """Extract searchable text from one decoded transcript entry.

    Returns a dict keyed by every name in `SEARCH_FIELDS` (empty strings for
    groups this entry has nothing for).
    """
    out: dict[str, list[str]] = {name: [] for name in SEARCH_FIELDS}
    entry_type = entry.get("type")

    if entry_type == "summary":
        out["meta"].append(_flatten(entry.get("summary")))
    elif entry_type == "ai-title":
        out["meta"].append(_flatten(entry.get("aiTitle")))
    elif entry_type == "system":
        out["meta"].append(_flatten(entry.get("content")))
    elif entry_type == "attachment":
        out["attachment"].append(_flatten(entry.get("attachment")))
    elif entry_type == "queue-operation":
        content = entry.get("content")
        if isinstance(content, list):
            _walk_content_items(cast("list[Any]", content), out)
        else:
            out["text"].append(_flatten(content))

    message = entry.get("message")
    if isinstance(message, dict):
        message_dict = cast("dict[str, Any]", message)
        content = message_dict.get("content")
        if isinstance(content, str):
            out["text"].append(_flatten(content))
        elif isinstance(content, list):
            _walk_content_items(cast("list[Any]", content), out)

    return {name: "\n".join(p for p in parts if p) for name, parts in out.items()}


def decode_entry(blob: bytes) -> dict[str, Any]:
    """Decompress a `messages.content` BLOB into its entry dict."""
    decoded: Any = json.loads(zlib.decompress(blob).decode("utf-8"))
    if not isinstance(decoded, dict):
        return {}
    return cast("dict[str, Any]", decoded)


# ---------------------------------------------------------------------------
# Index lifecycle
# ---------------------------------------------------------------------------


def fts5_available(conn: sqlite3.Connection) -> bool:
    """Runtime feature check — FTS5 is standard but not guaranteed."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts5_probe USING fts5(x, content='')")
        conn.execute("DROP TABLE temp._fts5_probe")
        return True
    except sqlite3.Error:
        return False


def _contentless_delete_supported() -> bool:
    """`contentless_delete=1` needs SQLite >= 3.43."""
    return sqlite3.sqlite_version_info >= (3, 43, 0)


@dataclass
class IndexStatus:
    """What `index_status` reports and `ensure_index` returns."""

    available: bool
    ready: bool
    fields: tuple[str, ...] = ()
    indexed_messages: int = 0
    total_messages: int = 0
    built_at: Optional[str] = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "ready": self.ready,
            "fields": list(self.fields),
            "indexed_messages": self.indexed_messages,
            "total_messages": self.total_messages,
            "built_at": self.built_at,
            "reason": self.reason,
        }


def _create_tables(conn: sqlite3.Connection, fields: Sequence[str]) -> None:
    columns = ", ".join(fields)
    options = ["content=''"]
    if _contentless_delete_supported():
        # Without this, deleting from a contentless table requires supplying
        # the original column values back, which we don't keep.
        options.append("contentless_delete=1")
    options.append("tokenize='unicode61 remove_diacritics 2'")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
        f"USING fts5({columns}, {', '.join(options)})"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {FILES_TABLE} ("
        "  file_id INTEGER PRIMARY KEY,"
        "  indexed_at TEXT NOT NULL,"
        "  message_count INTEGER NOT NULL DEFAULT 0,"
        # The cache stamps cached_files.cached_mtime every time it rewrites a
        # file's messages, so comparing it is how we notice that an
        # already-indexed file has *changed*. Without it, `ensure_index`
        # skips the file (its id is known) and search serves stale hits.
        "  cached_mtime REAL NOT NULL DEFAULT 0"
        ")"
    )
    # Which FTS rowids came from which file. This has to be tracked here
    # rather than re-derived from `messages`, because both of the paths that
    # delete rows run *after* the messages are already gone: the cache
    # replaces a file's rows before we reindex it, and a removed file has no
    # rows left to look up. Deriving the list from `messages` orphans every
    # row in exactly those two cases. It also makes the index self-contained,
    # so it can be dropped and rebuilt without consulting anything else.
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {ROWS_TABLE} ("
        "  rowid INTEGER PRIMARY KEY,"
        "  file_id INTEGER NOT NULL"
        ")"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{ROWS_TABLE}_file ON {ROWS_TABLE}(file_id)"
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    try:
        row = conn.execute(
            f"SELECT value FROM {META_TABLE} WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"INSERT INTO {META_TABLE}(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _index_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (FTS_TABLE,),
    ).fetchone()
    return row is not None


def drop_index(conn: sqlite3.Connection) -> None:
    """Remove the index entirely. It is derived data; rebuilding is cheap."""
    conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {ROWS_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {FILES_TABLE}")
    conn.execute(f"DROP TABLE IF EXISTS {META_TABLE}")
    conn.commit()


def _refresh_indexed_count(conn: sqlite3.Connection) -> None:
    """Record the row count so `index_status` never has to count rows.

    Called only when the index changes, so the 57 ms `count(*)` is paid at
    build time rather than per request.
    """
    count = conn.execute(f"SELECT count(*) FROM {ROWS_TABLE}").fetchone()[0]
    _meta_set(conn, "indexed_messages", str(int(count)))


def index_ready(conn: sqlite3.Connection) -> bool:
    """Cheap readiness check for the request path.

    `index_status` is comparatively expensive and must not be called per
    request; this is two indexed lookups.
    """
    return _index_exists(conn) and _meta_get(conn, "index_fields") is not None


def index_status(conn: sqlite3.Connection) -> IndexStatus:
    """Report the index's state without building anything.

    Both counts come from small aggregate tables rather than `messages`.
    Counting `messages` directly costs ~1.8 s on a real archive, because the
    content BLOB is stored inline and a full scan therefore reads ~930 MB;
    `count(*)` on the FTS table is 57 ms. Neither is acceptable on a request
    path, so the indexed total is recorded at build time instead.
    """
    if not fts5_available(conn):
        return IndexStatus(
            available=False, ready=False, reason="SQLite was built without FTS5"
        )
    if not _index_exists(conn):
        return IndexStatus(available=True, ready=False, reason="not built yet")

    fields_raw = _meta_get(conn, "index_fields") or ""
    indexed_raw = _meta_get(conn, "indexed_messages")
    if indexed_raw is None:
        # Only when the meta row is missing (an interrupted first build).
        indexed = int(conn.execute(f"SELECT count(*) FROM {FTS_TABLE}").fetchone()[0])
    else:
        indexed = int(indexed_raw)
    total_row = conn.execute(
        "SELECT coalesce(sum(message_count), 0) FROM cached_files"
    ).fetchone()
    return IndexStatus(
        available=True,
        ready=True,
        fields=tuple(f for f in fields_raw.split(",") if f),
        indexed_messages=indexed,
        total_messages=int(total_row[0]) if total_row else 0,
        built_at=_meta_get(conn, "built_at"),
    )


def _rows_for_file(
    conn: sqlite3.Connection, file_id: int, fields: Sequence[str]
) -> Iterator[tuple[Any, ...]]:
    """Yield insertable FTS rows for one cached file."""
    skip = tuple(SKIP_TYPES)
    placeholders = ",".join("?" * len(skip))
    cursor = conn.execute(
        "SELECT id, content FROM messages "
        f"WHERE file_id = ? AND type NOT IN ({placeholders})",
        (file_id, *skip),
    )
    for row_id, blob in cursor:
        extracted = extract_search_text(decode_entry(blob))
        yield (row_id, *[extracted[name] for name in fields])


def _index_file(
    conn: sqlite3.Connection, file_id: int, fields: Sequence[str], insert_sql: str
) -> int:
    """Index one file's messages, recording the rowid->file mapping.

    Materialised per file (a few thousand rows at most) so the rowids can be
    written to ROWS_TABLE alongside the FTS insert.
    """
    rows = list(_rows_for_file(conn, file_id, fields))
    if not rows:
        return 0
    conn.executemany(insert_sql, rows)
    conn.executemany(
        f"INSERT OR REPLACE INTO {ROWS_TABLE}(rowid, file_id) VALUES(?, ?)",
        [(row[0], file_id) for row in rows],
    )
    return len(rows)


def _delete_file_rows(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove every FTS row belonging to a cached file.

    Uses the ROWS_TABLE mapping rather than `messages`, because callers run
    after the messages have already been replaced or deleted.
    """
    ids = [
        int(row[0])
        for row in conn.execute(
            f"SELECT rowid FROM {ROWS_TABLE} WHERE file_id = ?", (file_id,)
        )
    ]
    if not ids:
        return
    conn.executemany(f"DELETE FROM {FTS_TABLE} WHERE rowid = ?", [(i,) for i in ids])
    conn.execute(f"DELETE FROM {ROWS_TABLE} WHERE file_id = ?", (file_id,))


def ensure_index(
    conn: sqlite3.Connection,
    *,
    index_fields: Sequence[str] = DEFAULT_INDEX_FIELDS,
    progress: Optional[Callable[[int, int], None]] = None,
    rebuild: bool = False,
) -> IndexStatus:
    """Create and populate the index, doing only the work that's missing.

    A full backfill of a 532k-message archive takes ~31 s; keeping an
    already-built index current costs ~180 ms for the largest file in that
    archive, because only changed `cached_files` are re-extracted.

    `progress` is called as `(files_done, files_total)`.
    """
    if not fts5_available(conn):
        return IndexStatus(
            available=False, ready=False, reason="SQLite was built without FTS5"
        )

    fields = tuple(index_fields)
    if not fields:
        raise ValueError("index_fields must not be empty")

    stored_version = _meta_get(conn, "extractor_version")
    stored_fields = _meta_get(conn, "index_fields")
    stale = (
        rebuild
        or not _index_exists(conn)
        or stored_version != str(EXTRACTOR_VERSION)
        or stored_fields != ",".join(fields)
    )
    if stale:
        # The index is derived data and the column set is baked into the
        # virtual table, so a field or extractor change means a fresh table.
        drop_index(conn)
        _create_tables(conn, fields)
        indexed_files: dict[int, float] = {}
    else:
        _create_tables(conn, fields)
        indexed_files = {
            int(row[0]): float(row[1] or 0.0)
            for row in conn.execute(f"SELECT file_id, cached_mtime FROM {FILES_TABLE}")
        }

    current_files = {
        int(row[0]): (int(row[1] or 0), float(row[2] or 0.0))
        for row in conn.execute(
            "SELECT id, message_count, cached_mtime FROM cached_files"
        )
    }

    # Files that vanished from the cache take their rows with them.
    for gone in set(indexed_files) - set(current_files):
        _delete_file_rows(conn, gone)
        conn.execute(f"DELETE FROM {FILES_TABLE} WHERE file_id = ?", (gone,))

    # New files, plus already-indexed ones the cache has rewritten since.
    todo = [
        file_id
        for file_id, (_, mtime) in current_files.items()
        if file_id not in indexed_files or abs(indexed_files[file_id] - mtime) >= 1e-6
    ]
    total = len(todo)
    now = datetime.now(timezone.utc).isoformat()
    placeholders = ",".join("?" * (len(fields) + 1))
    insert_sql = (
        f"INSERT INTO {FTS_TABLE}(rowid, {', '.join(fields)}) VALUES({placeholders})"
    )

    for done, file_id in enumerate(todo, start=1):
        if file_id in indexed_files:
            # Re-indexing a changed file: clear its old rows first.
            _delete_file_rows(conn, file_id)
        _index_file(conn, file_id, fields, insert_sql)
        conn.execute(
            f"INSERT INTO {FILES_TABLE}"
            "(file_id, indexed_at, message_count, cached_mtime) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(file_id) DO UPDATE SET "
            "indexed_at = excluded.indexed_at, "
            "message_count = excluded.message_count, "
            "cached_mtime = excluded.cached_mtime",
            (file_id, now, current_files[file_id][0], current_files[file_id][1]),
        )
        if progress is not None:
            progress(done, total)
        # Commit per file so an interrupted backfill resumes rather than
        # restarting: the files already recorded are simply skipped.
        conn.commit()

    _meta_set(conn, "extractor_version", str(EXTRACTOR_VERSION))
    _meta_set(conn, "index_fields", ",".join(fields))
    _meta_set(conn, "built_at", now)
    _refresh_indexed_count(conn)
    conn.commit()

    if total:
        conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('optimize')")
        conn.commit()

    return index_status(conn)


def reindex_files(conn: sqlite3.Connection, file_ids: Sequence[int]) -> None:
    """Refresh specific files in place — the incremental maintenance hook.

    Cheap enough to run on the cache's normal invalidation path: ~180 ms for
    the largest file in an 8 GB archive.
    """
    if not file_ids or not _index_exists(conn):
        return
    fields_raw = _meta_get(conn, "index_fields")
    if not fields_raw:
        return
    fields = tuple(f for f in fields_raw.split(",") if f)
    placeholders = ",".join("?" * (len(fields) + 1))
    insert_sql = (
        f"INSERT INTO {FTS_TABLE}(rowid, {', '.join(fields)}) VALUES({placeholders})"
    )
    now = datetime.now(timezone.utc).isoformat()
    for file_id in file_ids:
        _delete_file_rows(conn, file_id)
        _index_file(conn, file_id, fields, insert_sql)
        row = conn.execute(
            "SELECT message_count, cached_mtime FROM cached_files WHERE id = ?",
            (file_id,),
        ).fetchone()
        conn.execute(
            f"INSERT INTO {FILES_TABLE}"
            "(file_id, indexed_at, message_count, cached_mtime) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(file_id) DO UPDATE SET "
            "indexed_at = excluded.indexed_at, "
            "message_count = excluded.message_count, "
            "cached_mtime = excluded.cached_mtime",
            (
                file_id,
                now,
                int(row[0] or 0) if row else 0,
                float(row[1] or 0.0) if row else 0.0,
            ),
        )
    _refresh_indexed_count(conn)
    conn.commit()


# ---------------------------------------------------------------------------
# Querying
# ---------------------------------------------------------------------------


def fts_escape(user_query: str) -> str:
    """Turn arbitrary user text into a valid FTS5 MATCH expression.

    Without this, ordinary input is a hard error: `vis-timeline` raises
    `OperationalError: no such column: timeline`, because a bare hyphen
    reads as FTS5 column-filter/NOT syntax.

    Double-quoted runs are preserved as phrases; a trailing `*` still means
    prefix search. Everything else becomes a quoted term.
    """
    tokens: list[str] = []
    for raw in re.findall(r'"[^"]*"\*?|\S+', user_query):
        if raw.startswith('"'):
            closing = raw.rfind('"')
            phrase = raw[1:closing]
            suffix = "*" if raw.endswith("*") else ""
            if phrase.strip():
                tokens.append('"' + phrase.replace('"', '""') + '"' + suffix)
            continue
        prefix = raw.endswith("*") and len(raw) > 1
        term = raw[:-1] if prefix else raw
        if not term.strip():
            continue
        tokens.append('"' + term.replace('"', '""') + '"' + ("*" if prefix else ""))
    return " ".join(tokens)


@dataclass
class SearchResult:
    """One hit, already resolved to something the frontend can link to."""

    message_id: int
    project_path: str
    project_slug: str
    session_id: Optional[str]
    message_uuid: Optional[str]
    timestamp: Optional[str]
    type: str
    field: str
    snippet: str
    link: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "project": self.project_slug,
            "project_path": self.project_path,
            "session_id": self.session_id,
            "message_uuid": self.message_uuid,
            "timestamp": self.timestamp,
            "type": self.type,
            "field": self.field,
            "snippet": self.snippet,
            "link": self.link,
        }


def _match_expression(escaped: str, fields: Sequence[str]) -> str:
    """Apply an FTS5 column filter when searching a subset of fields."""
    if not escaped:
        return escaped
    if not fields or set(fields) == set(SEARCH_FIELDS):
        return escaped
    return "{" + " ".join(fields) + "} : " + escaped


def _build_query(
    *,
    project_id: Optional[int],
    session_id: Optional[str],
    message_type: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[str, list[Any]]:
    """Build the SELECT, preserving the FTS-outer-loop invariant.

    The `CROSS JOIN` is load-bearing, not stylistic: with a plain JOIN the
    planner drives from `messages` and probes FTS by rowid, which turns a
    2.4 ms project-filtered search into 2,012 ms. `test_search.py` asserts
    the plan.
    """
    where: list[str] = [f"f.{FTS_TABLE} MATCH ?"]
    params: list[Any] = []
    if project_id is not None:
        where.append("m.project_id = ?")
    if session_id is not None:
        where.append("m.session_id = ?")
    if message_type is not None:
        where.append("m.type = ?")
    if date_from is not None:
        where.append("m.timestamp >= ?")
    if date_to is not None:
        where.append("m.timestamp <= ?")

    sql = (
        "SELECT f.rowid, m.type, m.timestamp, m.session_id, m._uuid, "
        "       m.content, p.project_path "
        f"  FROM {FTS_TABLE} f "
        "  CROSS JOIN messages m ON m.id = f.rowid "
        "  JOIN projects p ON p.id = m.project_id "
        f" WHERE {' AND '.join(where)} "
        " ORDER BY f.rank LIMIT ? OFFSET ?"
    )
    return sql, params


def build_excerpt(text: str, terms: Sequence[str], width: int = 240) -> str:
    """Build a snippet around the first matching term.

    `snippet()` is unavailable on contentless FTS5 tables (it returns NULL
    without erroring), so excerpts are made here from the decompressed BLOB.
    """
    if not text:
        return ""
    lowered = text.lower()
    positions = [
        pos
        for pos in (lowered.find(term.lower()) for term in terms if term)
        if pos >= 0
    ]
    if not positions:
        return text[:width].replace("\n", " ").strip()
    start = max(0, min(positions) - width // 3)
    end = min(len(text), start + width)
    fragment = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + fragment + ("…" if end < len(text) else "")


def _plain_terms(user_query: str) -> list[str]:
    """The literal strings to look for when building an excerpt."""
    terms: list[str] = []
    for raw in re.findall(r'"[^"]*"\*?|\S+', user_query):
        cleaned = raw.strip("*").strip('"').strip()
        if cleaned:
            terms.append(cleaned)
    return terms


def search(
    conn: sqlite3.Connection,
    user_query: str,
    *,
    fields: Sequence[str] = DEFAULT_SEARCH_FIELDS,
    project_id: Optional[int] = None,
    session_id: Optional[str] = None,
    message_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> list[SearchResult]:
    """Run a search and return linkable results with excerpts."""
    escaped = fts_escape(user_query)
    if not escaped:
        return []
    if not _index_exists(conn):
        raise RuntimeError("search index has not been built")

    match = _match_expression(escaped, fields)
    sql, _ = _build_query(
        project_id=project_id,
        session_id=session_id,
        message_type=message_type,
        date_from=date_from,
        date_to=date_to,
    )
    params: list[Any] = [match]
    for value in (project_id, session_id, message_type, date_from, date_to):
        if value is not None:
            params.append(value)
    params += [limit, offset]

    terms = _plain_terms(user_query)
    results: list[SearchResult] = []
    for row in conn.execute(sql, params):
        row_id, msg_type, timestamp, sess_id, uuid, blob, project_path = row
        extracted = extract_search_text(decode_entry(blob))
        matched_field = next(
            (
                name
                for name in fields
                if extracted.get(name)
                and any(t.lower() in extracted[name].lower() for t in terms)
            ),
            # Fall back to whichever group has content: the FTS tokenizer can
            # match where a naive substring scan doesn't (diacritics folding).
            next((name for name in fields if extracted.get(name)), "text"),
        )
        slug = project_path.rstrip("/").rsplit("/", 1)[-1]
        results.append(
            SearchResult(
                message_id=int(row_id),
                project_path=project_path,
                project_slug=slug,
                session_id=sess_id,
                message_uuid=uuid,
                timestamp=timestamp,
                type=msg_type,
                field=matched_field,
                snippet=build_excerpt(extracted.get(matched_field, ""), terms),
                link=build_link(slug, sess_id, uuid, msg_type, user_query),
            )
        )
    return results


def count_matches(
    conn: sqlite3.Connection,
    user_query: str,
    *,
    fields: Sequence[str] = DEFAULT_SEARCH_FIELDS,
) -> int:
    """Total matches, ignoring row filters — cheap even on huge result sets."""
    escaped = fts_escape(user_query)
    if not escaped or not _index_exists(conn):
        return 0
    match = _match_expression(escaped, fields)
    row = conn.execute(
        f"SELECT count(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", (match,)
    ).fetchone()
    return int(row[0]) if row else 0


def build_link(
    project_slug: str,
    session_id: Optional[str],
    message_uuid: Optional[str],
    message_type: str,
    user_query: str,
) -> str:
    """Build the deep link for a hit.

    `?uuid=` anchors the message card and `&q=` lets the page's existing
    search open the collapsed `<details>` the match may sit inside — in a
    sample of real hits, 5 of 8 matched text inside a closed block, so the
    uuid alone lands on the card but not on the match.

    Types that render no message card get a session-level link instead of a
    uuid that would resolve to nothing.
    """
    from urllib.parse import quote

    if not session_id:
        return f"{project_slug}/"
    base = f"{project_slug}/session-{session_id}.html"
    params: list[str] = []
    if message_uuid and message_type not in SESSION_ONLY_TYPES:
        params.append(f"uuid={quote(message_uuid)}")
    if user_query.strip():
        params.append(f"q={quote(user_query)}")
    return base + ("?" + "&".join(params) if params else "")

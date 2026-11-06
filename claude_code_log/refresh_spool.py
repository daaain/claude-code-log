"""A refresh's entry rows, spooled to a private file until one merge.

A cache refresh parses each modified transcript and writes its rows,
then derives the session rows, the project aggregates and the
cross-session sidecar from the whole. Written straight into the shared
cache, those are many transactions, and a refresh that stops between
two of them (a lock-out, a kill, a full disk) leaves files recorded as
current over derived rows that predate them. Spooled, the rows go into a
file of their own as they are serialised, with no shared lock held, and
the refresh commits everything in one transaction at its end: it
``ATTACH``es the spool and copies with ``INSERT ... SELECT``, which holds
the shared write lock for the copy alone, with no Python per row.

The spool is an ordinary SQLite file beside the cache database, named
``<cache>.spool-<pid>-<n>``. It sits beside the cache rather than in the
system temp dir because the temp dir may be RAM, and a spool is of the
order of the modified files' compressed size. Its journal is off and
nothing is synced: a spool that outlives its process is garbage, and the
refresh that owned it never merged. ``CacheManager.atomic_refresh`` owns
the lifecycle: open, fill, merge, unlink.
"""

from __future__ import annotations

import itertools
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SPOOL_INFIX = ".spool-"

# The columns of ``messages`` a writer fills, in the order the cache
# writes them. ``project_id`` and ``file_id`` are not among them: the
# merge sets both, since a new file's id only exists once its
# ``cached_files`` row is written, inside the merge transaction.
MESSAGE_COLUMNS = (
    ("type", "TEXT NOT NULL"),
    ("timestamp", "TEXT"),
    ("session_id", "TEXT"),
    ("_uuid", "TEXT"),
    ("_parent_uuid", "TEXT"),
    ("_is_sidechain", "INTEGER DEFAULT 0"),
    ("_user_type", "TEXT"),
    ("_cwd", "TEXT"),
    ("_version", "TEXT"),
    ("_is_meta", "INTEGER"),
    ("_agent_id", "TEXT"),
    ("_request_id", "TEXT"),
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cache_creation_tokens", "INTEGER"),
    ("cache_read_tokens", "INTEGER"),
    ("_leaf_uuid", "TEXT"),
    ("_level", "TEXT"),
    ("_operation", "TEXT"),
    ("content", "BLOB NOT NULL"),
)
MESSAGE_COLUMN_NAMES = tuple(name for name, _ in MESSAGE_COLUMNS)
MESSAGE_COLUMN_LIST = ", ".join(MESSAGE_COLUMN_NAMES)

_SCHEMA = f"""
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    file_name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,
    mode TEXT NOT NULL,
    expected_existing INTEGER NOT NULL DEFAULT 0,
    source_mtime REAL NOT NULL,
    source_size INTEGER NOT NULL,
    cached_mtime REAL NOT NULL,
    message_count INTEGER NOT NULL,
    subagents_fingerprint TEXT,
    content_version TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL,
    {", ".join(f"{name} {decl}" for name, decl in MESSAGE_COLUMNS)}
);
CREATE INDEX messages_file ON messages(file_id);
"""

_INSERT_ROW = (
    f"INSERT INTO messages (file_id, {MESSAGE_COLUMN_LIST}) VALUES (:file_id, "
    + ", ".join(f":{name}" for name in MESSAGE_COLUMN_NAMES)
    + ")"
)

_counter = itertools.count(1)


def spool_path(db_path: Path) -> Path:
    """A spool file name of this process's own, beside ``db_path``."""
    return db_path.with_name(
        f"{db_path.name}{SPOOL_INFIX}{os.getpid()}-{next(_counter)}"
    )


def is_spool_path(path: Path, db_path: Path) -> bool:
    """Whether ``path`` is a spool of ``db_path``, this process's or not."""
    return path.name.startswith(f"{db_path.name}{SPOOL_INFIX}")


@dataclass(frozen=True)
class SpooledFile:
    """What the merge needs to know about one spooled file.

    ``mode`` is ``replace`` (the rows are the file's whole new content)
    or ``append`` (the rows are a tail, to follow exactly
    ``expected_existing`` rows already in the cache). The remaining
    fields are the ``cached_files`` row as of the parse, which the merge
    writes verbatim.
    """

    id: int
    file_name: str
    file_path: str
    mode: str
    expected_existing: int
    source_mtime: float
    source_size: int
    cached_mtime: float
    message_count: int
    subagents_fingerprint: Optional[str]
    content_version: Optional[str]


class RefreshSpool:
    """One refresh's spool: open, fill per file, hand to the merge, unlink."""

    def __init__(self, db_path: Path) -> None:
        self.path = spool_path(db_path)
        # Journal and sync off: the file is scratch whose only reader is
        # the merge in this same process, and a crash makes it garbage
        # either way. Autocommit, so a file's rows are visible to the
        # cache connection that attaches it as soon as they are written.
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            self._conn.execute("PRAGMA journal_mode = OFF")
            self._conn.execute("PRAGMA synchronous = OFF")
            self._conn.executescript(_SCHEMA)
        except BaseException:
            self.close()
            raise

    def has(self, file_name: str) -> bool:
        """Whether a file of that name is already spooled."""
        row = self._conn.execute(
            "SELECT 1 FROM files WHERE file_name = ?", (file_name,)
        ).fetchone()
        return row is not None

    def add(
        self,
        record: Dict[str, Any],
        rows: Iterable[Dict[str, Any]],
        *,
        mode: str,
        expected_existing: int = 0,
    ) -> int:
        """Spool ``rows`` as ``record``'s new content (or tail).

        ``record`` carries the ``cached_files`` columns of the parse. A
        file spooled twice is replaced: the later parse is the newer
        one, as a later direct write would be.
        """
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "DELETE FROM messages WHERE file_id IN"
                " (SELECT id FROM files WHERE file_name = ?)",
                (record["file_name"],),
            )
            self._conn.execute(
                "DELETE FROM files WHERE file_name = ?", (record["file_name"],)
            )
            cursor = self._conn.execute(
                """INSERT INTO files
                   (file_name, file_path, mode, expected_existing, source_mtime,
                    source_size, cached_mtime, message_count,
                    subagents_fingerprint, content_version)
                   VALUES (:file_name, :file_path, :mode, :expected_existing,
                           :source_mtime, :source_size, :cached_mtime,
                           :message_count, :subagents_fingerprint,
                           :content_version)""",
                {**record, "mode": mode, "expected_existing": expected_existing},
            )
            file_id = cursor.lastrowid or 0
            self._conn.executemany(
                _INSERT_ROW, ({**row, "file_id": file_id} for row in rows)
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return file_id

    def files(self) -> List[SpooledFile]:
        """The spooled files, in the order they were first spooled."""
        rows = self._conn.execute(
            """SELECT id, file_name, file_path, mode, expected_existing,
                      source_mtime, source_size, cached_mtime, message_count,
                      subagents_fingerprint, content_version
               FROM files ORDER BY id"""
        ).fetchall()
        return [SpooledFile(*row) for row in rows]

    def close(self) -> None:
        """Close the spool's own connection (the merge attaches by path)."""
        self._conn.close()

    def unlink(self) -> None:
        """Remove the file; it must be closed and detached everywhere first."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

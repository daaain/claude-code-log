"""JSON API handlers for the local server.

Kept separate from `server.py` (HTTP plumbing) and `search.py` (the
HTTP-free core): this is the thin layer that turns query-string strings
into typed arguments and results into JSON.

Connections are thread-local because `ThreadingHTTPServer` handles requests
on separate threads and a `sqlite3.Connection` is not shareable across them.
WAL is already enabled on the cache, so read-only connections never block —
even while a backfill holds a write transaction (measured at 0.2 ms).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .cache import get_library_version
from .search import (
    DEFAULT_SEARCH_FIELDS,
    SEARCH_FIELDS,
    count_matches,
    index_ready,
    index_status,
    indexed_fields,
    parse_field_spec,
    project_slug,
    search,
)

#: Refuse absurd page sizes rather than letting one request build 100k
#: excerpts (each of which decompresses a BLOB).
MAX_LIMIT = 200


def _int_param(params: dict[str, str], name: str, default: int) -> int:
    raw = params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


class SearchApi:
    """Route handlers over one cache database."""

    def __init__(
        self,
        db_path: Path,
        *,
        default_fields: Sequence[str] = DEFAULT_SEARCH_FIELDS,
    ) -> None:
        self.db_path = db_path
        self.default_fields = tuple(default_fields)
        self._local = threading.local()

    # -- connection -------------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False
            )
            conn.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = conn
        return conn

    # -- routes -----------------------------------------------------------

    def ping(self, _params: dict[str, str]) -> dict[str, Any]:
        """Presence check. The frontend uses this to decide whether the
        archive-search page is usable at all — on `file://` the fetch fails
        and the page shows setup instructions instead."""
        status = index_status(self.connection())
        return {
            "ok": True,
            "version": get_library_version(),
            "fields": {
                "available": list(SEARCH_FIELDS),
                "default": list(self.default_fields),
            },
            "index": status.as_dict(),
        }

    def projects(self, _params: dict[str, str]) -> dict[str, Any]:
        """Project list for the filter dropdown, newest activity first."""
        rows = self.connection().execute(
            "SELECT id, project_path, total_message_count, earliest_timestamp, "
            "       latest_timestamp "
            "  FROM projects ORDER BY latest_timestamp DESC"
        )
        return {
            "projects": [
                {
                    "id": row[0],
                    # Never trust the stored path for resolution: it is
                    # absolute and points at the archive's *original*
                    # location. Only the basename is portable.
                    "slug": project_slug(str(row[1])),
                    "path": row[1],
                    "message_count": row[2],
                    "first_timestamp": row[3],
                    "last_timestamp": row[4],
                }
                for row in rows
            ]
        }

    def _resolve_project_id(self, slug: str) -> Optional[int]:
        """Map a project slug (directory name) to its cache id."""
        for row in self.connection().execute("SELECT id, project_path FROM projects"):
            if project_slug(str(row[1])) == slug:
                return int(row[0])
        return None

    def search(self, params: dict[str, str]) -> dict[str, Any]:
        query = params.get("q", "").strip()
        if not query:
            return {"results": [], "total": 0, "query": query}

        conn = self.connection()
        # Cheap readiness check, not full `index_status`: that one reports
        # counts, and even the aggregate version is more work than a search
        # needs to do on every keystroke.
        if not index_ready(conn):
            status = index_status(conn)
            raise ValueError(
                f"search index is not available ({status.reason or 'not built'})"
            )

        fields = parse_field_spec(params.get("fields"), self.default_fields)

        # The index's column set is baked in at build time and can be
        # narrower than SEARCH_FIELDS (CLAUDE_CODE_LOG_INDEX_FIELDS). An
        # FTS5 column filter naming a missing column is an OperationalError,
        # not an empty result — so search only what exists, and report what
        # was dropped rather than silently narrowing the scope.
        available = set(indexed_fields(conn))
        unindexed = [f for f in fields if f not in available]
        if unindexed:
            fields = tuple(f for f in fields if f in available)

        if not fields:
            return {
                "results": [],
                "total": 0,
                "query": query,
                "fields": [],
                "unindexed_fields": unindexed,
            }

        project_id: Optional[int] = None
        slug = params.get("project")
        if slug:
            project_id = self._resolve_project_id(slug)
            if project_id is None:
                raise ValueError(f"unknown project: {slug}")

        limit = max(1, min(_int_param(params, "limit", 20), MAX_LIMIT))
        offset = max(0, _int_param(params, "offset", 0))

        results = search(
            conn,
            query,
            fields=fields,
            project_id=project_id,
            session_id=params.get("session") or None,
            message_type=params.get("type") or None,
            date_from=params.get("from") or None,
            date_to=params.get("to") or None,
            limit=limit,
            offset=offset,
        )
        return {
            "query": query,
            "fields": list(fields),
            "unindexed_fields": unindexed,
            "project": slug or None,
            "limit": limit,
            "offset": offset,
            # `total` ignores the row filters — it is the index-wide match
            # count, which is cheap even for 219k hits (~5 ms) where paging
            # through a filtered count would not be.
            "total": count_matches(conn, query, fields=fields),
            "results": [r.as_dict() for r in results],
        }

    def routes(self) -> dict[str, Callable[[dict[str, str]], Any]]:
        return {
            "/api/ping": self.ping,
            "/api/search": self.search,
            "/api/projects": self.projects,
        }

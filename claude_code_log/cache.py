#!/usr/bin/env python3
"""SQLite-based cache management for Claude Code Log."""

import json
import logging
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from packaging import version
from pydantic import BaseModel

from .factories import create_transcript_entry
from .migrations.runner import run_migrations
from .models import (
    AssistantTranscriptEntry,
    QueueOperationTranscriptEntry,
    SummaryTranscriptEntry,
    SystemTranscriptEntry,
    TranscriptEntry,
    UserTranscriptEntry,
)

logger = logging.getLogger(__name__)


# ========== Data Models ==========


class CachedFileInfo(BaseModel):
    """Information about a cached JSONL file."""

    file_path: str
    source_mtime: float
    cached_mtime: float
    message_count: int
    session_ids: list[str]


class SessionCacheData(BaseModel):
    """Cached session-level information."""

    session_id: str
    summary: Optional[str] = None
    first_timestamp: str
    last_timestamp: str
    message_count: int
    first_user_message: str
    cwd: Optional[str] = None  # Working directory from session messages
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    # Teammates feature — set when the session was active in a team.
    # First non-None ``teamName`` of any entry in the session.
    team_name: Optional[str] = None


class HtmlCacheEntry(BaseModel):
    """Information about a generated HTML file."""

    html_path: str  # e.g., "session-abc123.html" or "combined_transcripts.html"
    generated_at: str  # ISO timestamp when HTML was generated
    source_session_id: Optional[str] = (
        None  # session_id for individual files, None for combined
    )
    message_count: int = 0  # for sanity checking
    library_version: str  # which version generated it


class PageCacheData(BaseModel):
    """Information about a paginated combined transcript page."""

    page_number: int
    html_path: str  # e.g., "combined_transcripts.html" or "combined_transcripts_2.html"
    page_size_config: int  # the --page-size value used
    message_count: int  # total messages on this page
    session_ids: List[str]  # sessions on this page, in order
    first_session_id: str
    last_session_id: str
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    generated_at: str  # ISO timestamp when page was generated
    library_version: str


class ProjectCache(BaseModel):
    """Project-level cache index structure for index.json."""

    version: str
    cache_created: str
    last_updated: str
    project_path: str

    # File-level cache information
    cached_files: dict[str, CachedFileInfo]

    # Aggregated project information
    total_message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0

    # Session metadata
    sessions: dict[str, SessionCacheData]

    # Working directories associated with this project
    working_directories: list[str] = []

    # Timeline information
    earliest_timestamp: str = ""
    latest_timestamp: str = ""


# ========== Helper Functions ==========


def scrub_surrogates(s: Optional[str]) -> Optional[str]:
    """Replace lone surrogates (U+DC80…U+DCFF) with U+FFFD.

    Public helper used wherever ``surrogateescape``-decoded JSONL data
    might cross a strict-UTF-8 boundary (issue #139). Examples:
    sqlite3's text-binding path (``cache.update_session_cache``) and
    TUI export (``tui.SessionBrowser._ensure_session_file``) — both
    raise ``UnicodeEncodeError`` on lone surrogates the moment we try
    to persist them.

    Encoding via ``surrogateescape`` (which round-trips lone surrogates
    back to their raw bytes — ``\\udcb2`` → ``b"\\xb2"``) followed by
    decoding with ``errors="replace"`` substitutes the invalid byte
    sequences with the canonical Unicode replacement character U+FFFD
    (``\\ufffd``). The simpler ``encode(..., errors="replace")``
    round-trip would emit ASCII ``?`` (U+003F) instead — also valid
    UTF-8, but a less informative sentinel.
    """
    if s is None:
        return None
    return s.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def get_library_version() -> str:
    """Get the current library version from package metadata or pyproject.toml."""
    # First try to get version from installed package metadata
    try:
        from importlib.metadata import version as get_version

        return get_version("claude-code-log")
    except Exception:
        # Package not installed or other error, continue to file-based detection
        pass

    # Second approach: Use importlib.resources for more robust package location detection
    try:
        from importlib import resources
        import toml

        # Get the package directory and navigate to parent for pyproject.toml
        package_files = resources.files("claude_code_log")
        # Convert to Path to access parent reliably
        package_root = Path(str(package_files)).parent
        pyproject_path = package_root / "pyproject.toml"

        if pyproject_path.exists():
            with open(pyproject_path, "r", encoding="utf-8") as f:
                pyproject_data = toml.load(f)
            return pyproject_data.get("project", {}).get("version", "unknown")
    except Exception:
        pass

    # Final fallback: Try to read from pyproject.toml using file-relative path
    try:
        import toml

        project_root = Path(__file__).parent.parent
        pyproject_path = project_root / "pyproject.toml"

        if pyproject_path.exists():
            with open(pyproject_path, "r", encoding="utf-8") as f:
                pyproject_data = toml.load(f)
            return pyproject_data.get("project", {}).get("version", "unknown")
    except Exception:
        pass

    return "unknown"


# ========== Cache Path Configuration ==========


def get_cache_db_path(projects_dir: Path) -> Path:
    """Get cache database path, respecting CLAUDE_CODE_LOG_CACHE_PATH env var.

    Priority: CLAUDE_CODE_LOG_CACHE_PATH env var > default location.

    Args:
        projects_dir: Path to the projects directory (e.g., ~/.claude/projects)

    Returns:
        Path to the SQLite cache database.
    """
    env_path = os.getenv("CLAUDE_CODE_LOG_CACHE_PATH")
    if env_path:
        return Path(env_path)
    return projects_dir / "claude-code-log-cache.db"


# ========== Cache Manager ==========


class CacheManager:
    """SQLite-based cache manager for Claude Code Log."""

    def __init__(
        self,
        project_path: Path,
        library_version: str,
        db_path: Optional[Path] = None,
    ):
        """Initialise cache manager for a project.

        Args:
            project_path: Path to the project directory containing JSONL files
            library_version: Current version of the library for cache invalidation
            db_path: Optional explicit path to the cache database. If not provided,
                uses CLAUDE_CODE_LOG_CACHE_PATH env var or default location.
        """
        self.project_path = project_path
        self.library_version = library_version

        # Priority: explicit db_path > env var > default location
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = get_cache_db_path(project_path.parent)

        # Initialise database and ensure project exists
        self._init_database()
        self._project_id: Optional[int] = None
        self._ensure_project_exists()

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with proper settings."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Create schema if needed using migration runner."""
        # Run any pending migrations
        run_migrations(self.db_path)

    def _ensure_project_exists(self) -> None:
        """Ensure project record exists and get its ID."""
        project_path_str = str(self.project_path)

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT id, version FROM projects WHERE project_path = ?",
                (project_path_str,),
            ).fetchone()

            if row:
                self._project_id = row["id"]
                cached_version = row["version"]

                # Check version compatibility
                if not self._is_cache_version_compatible(cached_version):
                    print(
                        f"Cache version incompatible: {cached_version} -> {self.library_version}, invalidating cache"
                    )
                    self._clear_project_data(conn)
                    self._project_id = self._create_project(conn)
            else:
                self._project_id = self._create_project(conn)

            conn.commit()

    def _create_project(self, conn: sqlite3.Connection) -> int:
        """Create a new project record."""
        now = datetime.now().isoformat()
        cursor = conn.execute(
            """
            INSERT INTO projects (project_path, version, cache_created, last_updated)
            VALUES (?, ?, ?, ?)
            """,
            (str(self.project_path), self.library_version, now, now),
        )
        return cursor.lastrowid or 0

    def _clear_project_data(self, conn: sqlite3.Connection) -> None:
        """Clear all data for the current project."""
        if self._project_id is None:
            return

        # Cascade delete will handle messages and files
        conn.execute("DELETE FROM projects WHERE id = ?", (self._project_id,))

    def _update_last_updated(self, conn: sqlite3.Connection) -> None:
        """Update the last_updated timestamp for the project."""
        if self._project_id is None:
            return

        conn.execute(
            "UPDATE projects SET last_updated = ? WHERE id = ?",
            (datetime.now().isoformat(), self._project_id),
        )

    def _normalize_timestamp(self, timestamp: Optional[str]) -> Optional[str]:
        """Normalize timestamp to consistent format for reliable string comparison.

        Converts various ISO 8601 formats to a canonical form:
        - Strips fractional seconds (e.g., '.875368')
        - Normalizes timezone to 'Z' suffix

        This ensures lexicographic string comparison works correctly in SQL queries.
        Without normalization, '2023-01-01T10:00:00.5Z' < '2023-01-01T10:00:00Z'
        because '.' < 'Z' in ASCII, even though the first is 500ms later.

        Args:
            timestamp: ISO 8601 timestamp string, or None

        Returns:
            Normalized timestamp in 'YYYY-MM-DDTHH:MM:SSZ' format, or None
        """
        if timestamp is None:
            return None

        # Pattern matches: YYYY-MM-DDTHH:MM:SS followed by optional fractional seconds
        # and timezone (Z or +HH:MM or +HH or +HHMM)
        match = re.match(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"  # Base datetime
            r"(?:\.\d+)?"  # Optional fractional seconds (discard)
            r"(?:Z|[+-]\d{2}:?\d{0,2})?$",  # Optional timezone
            timestamp,
        )

        if match:
            # Return just the base datetime with Z suffix
            return match.group(1) + "Z"

        # If pattern doesn't match, return original (shouldn't happen with valid data)
        return timestamp

    def _serialize_entry(self, entry: TranscriptEntry, file_id: int) -> Dict[str, Any]:
        """Convert TranscriptEntry to dict for SQLite insertion."""
        raw_timestamp = getattr(entry, "timestamp", None)
        base: Dict[str, Any] = {
            "project_id": self._project_id,
            "file_id": file_id,
            "type": entry.type,
            "timestamp": self._normalize_timestamp(raw_timestamp),
            "session_id": getattr(entry, "sessionId", None),
            "_uuid": getattr(entry, "uuid", None),
            "_parent_uuid": getattr(entry, "parentUuid", None),
            "_is_sidechain": 1 if getattr(entry, "isSidechain", False) else 0,
            "_user_type": getattr(entry, "userType", None),
            "_cwd": getattr(entry, "cwd", None),
            "_version": getattr(entry, "version", None),
            "_is_meta": (
                1
                if getattr(entry, "isMeta", None) is True
                else (0 if getattr(entry, "isMeta", None) is False else None)
            ),
            "_agent_id": getattr(entry, "agentId", None),
            "_request_id": None,
            "input_tokens": None,
            "output_tokens": None,
            "cache_creation_tokens": None,
            "cache_read_tokens": None,
            "_leaf_uuid": None,
            "_level": None,
            "_operation": None,
            "content": zlib.compress(
                json.dumps(entry.model_dump(), separators=(",", ":")).encode("utf-8")
            ),
        }

        # Extract flattened usage for assistant messages
        if isinstance(entry, AssistantTranscriptEntry):
            base["_request_id"] = entry.requestId
            if entry.message and entry.message.usage:
                usage = entry.message.usage
                base["input_tokens"] = usage.input_tokens
                base["output_tokens"] = usage.output_tokens
                base["cache_creation_tokens"] = usage.cache_creation_input_tokens
                base["cache_read_tokens"] = usage.cache_read_input_tokens

        # User entry specific
        if isinstance(entry, UserTranscriptEntry):
            if entry.agentId:
                base["_agent_id"] = entry.agentId

        # Summary specific
        if isinstance(entry, SummaryTranscriptEntry):
            base["_leaf_uuid"] = entry.leafUuid

        # System specific
        if isinstance(entry, SystemTranscriptEntry):
            base["_level"] = entry.level

        # Queue-operation specific
        if isinstance(entry, QueueOperationTranscriptEntry):
            base["_operation"] = entry.operation

        return base

    def _deserialize_entry(self, row: sqlite3.Row) -> TranscriptEntry:
        """Convert SQLite row back to TranscriptEntry."""
        content_dict = json.loads(zlib.decompress(row["content"]).decode("utf-8"))
        return create_transcript_entry(content_dict)

    def _get_file_id(self, jsonl_path: Path) -> Optional[int]:
        """Get the file ID for a JSONL file."""
        if self._project_id is None:
            return None

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT id FROM cached_files WHERE project_id = ? AND file_name = ?",
                (self._project_id, jsonl_path.name),
            ).fetchone()

        return row["id"] if row else None

    def is_file_cached(self, jsonl_path: Path) -> bool:
        """Check if a JSONL file has a valid cache entry."""
        if self._project_id is None:
            return False

        if not jsonl_path.exists():
            return False

        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT source_mtime FROM cached_files WHERE project_id = ? AND file_name = ?",
                (self._project_id, jsonl_path.name),
            ).fetchone()

        if not row:
            return False

        source_mtime = jsonl_path.stat().st_mtime
        cached_mtime = row["source_mtime"]

        # Cache is valid if modification times match (within 1 second tolerance)
        return abs(source_mtime - cached_mtime) < 1.0

    def load_cached_entries(self, jsonl_path: Path) -> Optional[List[TranscriptEntry]]:
        """Load cached transcript entries for a JSONL file."""
        if not self.is_file_cached(jsonl_path):
            return None

        file_id = self._get_file_id(jsonl_path)
        if file_id is None:
            return None

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT content FROM messages WHERE file_id = ? ORDER BY timestamp NULLS LAST",
                (file_id,),
            ).fetchall()

        return [self._deserialize_entry(row) for row in rows]

    def load_cached_entries_filtered(
        self, jsonl_path: Path, from_date: Optional[str], to_date: Optional[str]
    ) -> Optional[List[TranscriptEntry]]:
        """Load cached entries with SQL-based timestamp filtering."""
        if not self.is_file_cached(jsonl_path):
            return None

        # If no date filtering needed, fall back to regular loading
        if not from_date and not to_date:
            return self.load_cached_entries(jsonl_path)

        file_id = self._get_file_id(jsonl_path)
        if file_id is None:
            return None

        # Parse dates
        import dateparser

        from_dt = None
        to_dt = None

        if from_date:
            from_dt = dateparser.parse(from_date)
            if from_dt and (
                from_date in ["today", "yesterday"] or "days ago" in from_date
            ):
                from_dt = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)

        if to_date:
            to_dt = dateparser.parse(to_date)
            if to_dt:
                to_dt = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

        # Build query with SQL-based filtering
        sql = "SELECT content FROM messages WHERE file_id = ?"
        params: List[Any] = [file_id]

        if from_dt:
            # Normalize to UTC 'Z' format for consistent string comparison
            # with stored timestamps (which use 'Z' suffix from JSONL)
            if from_dt.tzinfo is None:
                from_dt = from_dt.replace(tzinfo=timezone.utc)
            from_bound = from_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            # Include entries with NULL timestamp (like summaries) OR within date range
            sql += " AND (timestamp IS NULL OR timestamp >= ?)"
            params.append(from_bound)

        if to_dt:
            # Normalize to UTC 'Z' format for consistent string comparison
            if to_dt.tzinfo is None:
                to_dt = to_dt.replace(tzinfo=timezone.utc)
            to_bound = to_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            sql += " AND (timestamp IS NULL OR timestamp <= ?)"
            params.append(to_bound)

        sql += " ORDER BY timestamp NULLS LAST"

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [self._deserialize_entry(row) for row in rows]

    def save_cached_entries(
        self, jsonl_path: Path, entries: List[TranscriptEntry]
    ) -> None:
        """Save parsed transcript entries to cache."""
        if self._project_id is None:
            return

        source_mtime = jsonl_path.stat().st_mtime
        cached_mtime = datetime.now().timestamp()

        with self._get_connection() as conn:
            # Insert or update file record
            # Use ON CONFLICT to preserve file ID and avoid cascade deletes on messages
            conn.execute(
                """
                INSERT INTO cached_files
                (project_id, file_name, file_path, source_mtime, cached_mtime, message_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, file_name) DO UPDATE SET
                    file_path = excluded.file_path,
                    source_mtime = excluded.source_mtime,
                    cached_mtime = excluded.cached_mtime,
                    message_count = excluded.message_count
                """,
                (
                    self._project_id,
                    jsonl_path.name,
                    str(jsonl_path),
                    source_mtime,
                    cached_mtime,
                    len(entries),
                ),
            )

            # Get the file ID
            row = conn.execute(
                "SELECT id FROM cached_files WHERE project_id = ? AND file_name = ?",
                (self._project_id, jsonl_path.name),
            ).fetchone()
            file_id = row["id"]

            # Delete existing messages for this file
            conn.execute("DELETE FROM messages WHERE file_id = ?", (file_id,))

            # Insert all entries in a batch
            serialized_entries = [
                self._serialize_entry(entry, file_id) for entry in entries
            ]
            conn.executemany(
                """
                INSERT INTO messages (
                    project_id, file_id, type, timestamp, session_id,
                    _uuid, _parent_uuid, _is_sidechain, _user_type, _cwd, _version,
                    _is_meta, _agent_id, _request_id,
                    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                    _leaf_uuid, _level, _operation, content
                ) VALUES (
                    :project_id, :file_id, :type, :timestamp, :session_id,
                    :_uuid, :_parent_uuid, :_is_sidechain, :_user_type, :_cwd, :_version,
                    :_is_meta, :_agent_id, :_request_id,
                    :input_tokens, :output_tokens, :cache_creation_tokens, :cache_read_tokens,
                    :_leaf_uuid, :_level, :_operation, :content
                )
                """,
                serialized_entries,
            )

            self._update_last_updated(conn)
            conn.commit()

    def update_session_cache(self, session_data: Dict[str, SessionCacheData]) -> None:
        """Update cached session information."""
        if self._project_id is None:
            return

        with self._get_connection() as conn:
            for session_id, data in session_data.items():
                conn.execute(
                    """
                    INSERT INTO sessions (
                        project_id, session_id, summary, first_timestamp, last_timestamp,
                        message_count, first_user_message, cwd,
                        total_input_tokens, total_output_tokens,
                        total_cache_creation_tokens, total_cache_read_tokens,
                        team_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(project_id, session_id) DO UPDATE SET
                        summary = excluded.summary,
                        first_timestamp = excluded.first_timestamp,
                        last_timestamp = excluded.last_timestamp,
                        message_count = excluded.message_count,
                        first_user_message = excluded.first_user_message,
                        cwd = excluded.cwd,
                        total_input_tokens = excluded.total_input_tokens,
                        total_output_tokens = excluded.total_output_tokens,
                        total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                        total_cache_read_tokens = excluded.total_cache_read_tokens,
                        team_name = excluded.team_name
                    """,
                    (
                        self._project_id,
                        session_id,
                        # Scrub surrogate-bearing strings before binding —
                        # sqlite3 raises UnicodeEncodeError on lone
                        # surrogates that surrogateescape-decoded JSONL
                        # may have leaked into these text fields. (#139)
                        scrub_surrogates(data.summary),
                        data.first_timestamp,
                        data.last_timestamp,
                        data.message_count,
                        scrub_surrogates(data.first_user_message),
                        scrub_surrogates(data.cwd),
                        data.total_input_tokens,
                        data.total_output_tokens,
                        data.total_cache_creation_tokens,
                        data.total_cache_read_tokens,
                        scrub_surrogates(data.team_name),
                    ),
                )

            self._update_last_updated(conn)
            conn.commit()

    def update_project_aggregates(
        self,
        total_message_count: int,
        total_input_tokens: int,
        total_output_tokens: int,
        total_cache_creation_tokens: int,
        total_cache_read_tokens: int,
        earliest_timestamp: str,
        latest_timestamp: str,
    ) -> None:
        """Update project-level aggregate information."""
        if self._project_id is None:
            return

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE projects SET
                    total_message_count = ?,
                    total_input_tokens = ?,
                    total_output_tokens = ?,
                    total_cache_creation_tokens = ?,
                    total_cache_read_tokens = ?,
                    earliest_timestamp = ?,
                    latest_timestamp = ?,
                    last_updated = ?
                WHERE id = ?
                """,
                (
                    total_message_count,
                    total_input_tokens,
                    total_output_tokens,
                    total_cache_creation_tokens,
                    total_cache_read_tokens,
                    earliest_timestamp,
                    latest_timestamp,
                    datetime.now().isoformat(),
                    self._project_id,
                ),
            )
            conn.commit()

    def get_working_directories(self) -> List[str]:
        """Get list of working directories associated with this project.

        Queries distinct cwd values from sessions table.
        """
        if self._project_id is None:
            return []

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT cwd FROM sessions WHERE project_id = ? AND cwd IS NOT NULL",
                (self._project_id,),
            ).fetchall()

        return [row["cwd"] for row in rows]

    def get_modified_files(self, jsonl_files: List[Path]) -> List[Path]:
        """Get list of JSONL files that need to be reprocessed."""
        return [
            jsonl_file
            for jsonl_file in jsonl_files
            if not self.is_file_cached(jsonl_file)
        ]

    def get_cached_project_data(self) -> Optional[ProjectCache]:
        """Get the cached project data if available."""
        if self._project_id is None:
            return None

        with self._get_connection() as conn:
            # Get project data
            project_row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (self._project_id,)
            ).fetchone()

            if not project_row:
                return None

            # Get cached files
            file_rows = conn.execute(
                "SELECT * FROM cached_files WHERE project_id = ?", (self._project_id,)
            ).fetchall()

            cached_files: Dict[str, CachedFileInfo] = {}
            for row in file_rows:
                # Get session IDs for this file from messages
                session_rows = conn.execute(
                    "SELECT DISTINCT session_id FROM messages WHERE file_id = ? AND session_id IS NOT NULL",
                    (row["id"],),
                ).fetchall()
                session_ids = [r["session_id"] for r in session_rows]

                cached_files[row["file_name"]] = CachedFileInfo(
                    file_path=row["file_path"],
                    source_mtime=row["source_mtime"],
                    cached_mtime=row["cached_mtime"],
                    message_count=row["message_count"],
                    session_ids=session_ids,
                )

            # Get sessions
            session_rows = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ?", (self._project_id,)
            ).fetchall()

            sessions: Dict[str, SessionCacheData] = {}
            for row in session_rows:
                sessions[row["session_id"]] = SessionCacheData(
                    session_id=row["session_id"],
                    summary=row["summary"],
                    first_timestamp=row["first_timestamp"],
                    last_timestamp=row["last_timestamp"],
                    message_count=row["message_count"],
                    first_user_message=row["first_user_message"],
                    cwd=row["cwd"],
                    total_input_tokens=row["total_input_tokens"],
                    total_output_tokens=row["total_output_tokens"],
                    total_cache_creation_tokens=row["total_cache_creation_tokens"],
                    total_cache_read_tokens=row["total_cache_read_tokens"],
                    team_name=row["team_name"] if "team_name" in row.keys() else None,
                )

        return ProjectCache(
            version=project_row["version"],
            cache_created=project_row["cache_created"],
            last_updated=project_row["last_updated"],
            project_path=project_row["project_path"],
            cached_files=cached_files,
            total_message_count=project_row["total_message_count"],
            total_input_tokens=project_row["total_input_tokens"],
            total_output_tokens=project_row["total_output_tokens"],
            total_cache_creation_tokens=project_row["total_cache_creation_tokens"],
            total_cache_read_tokens=project_row["total_cache_read_tokens"],
            sessions=sessions,
            working_directories=self.get_working_directories(),
            earliest_timestamp=project_row["earliest_timestamp"],
            latest_timestamp=project_row["latest_timestamp"],
        )

    def clear_cache(self) -> None:
        """Clear all cache data for this project."""
        if self._project_id is None:
            return

        with self._get_connection() as conn:
            self._clear_project_data(conn)
            self._project_id = self._create_project(conn)
            conn.commit()

    def _is_cache_version_compatible(self, cache_version: str) -> bool:
        """Check if a cache version is compatible with the current library version."""
        if cache_version == self.library_version:
            return True

        # Define compatibility rules
        breaking_changes: dict[str, str] = {
            # 0.9.0 introduced _compact_ide_tags_for_preview() which transforms
            # first_user_message to use emoji indicators instead of raw IDE tags
            "0.8.0": "0.9.0",
        }

        cache_ver = version.parse(cache_version)
        current_ver = version.parse(self.library_version)

        for breaking_version_pattern, min_required in breaking_changes.items():
            min_required_ver = version.parse(min_required)

            if current_ver >= min_required_ver:
                if breaking_version_pattern.endswith(".x"):
                    major_minor = breaking_version_pattern[:-2]
                    if str(cache_ver).startswith(major_minor):
                        return False
                else:
                    breaking_ver = version.parse(breaking_version_pattern)
                    if cache_ver <= breaking_ver:
                        return False

        return True

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics for reporting."""
        if self._project_id is None:
            return {"cache_enabled": False}

        with self._get_connection() as conn:
            project_row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (self._project_id,)
            ).fetchone()

            file_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM cached_files WHERE project_id = ?",
                (self._project_id,),
            ).fetchone()

            session_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM sessions WHERE project_id = ?",
                (self._project_id,),
            ).fetchone()

        if not project_row:
            return {"cache_enabled": False}

        return {
            "cache_enabled": True,
            "cached_files_count": file_count["cnt"] if file_count else 0,
            "total_cached_messages": project_row["total_message_count"],
            "total_sessions": session_count["cnt"] if session_count else 0,
            "cache_created": project_row["cache_created"],
            "last_updated": project_row["last_updated"],
        }

    # ========== HTML Cache Methods ==========

    def get_html_cache(self, html_path: str) -> Optional[HtmlCacheEntry]:
        """Get HTML cache entry for a given path."""
        if self._project_id is None:
            return None

        with self._get_connection() as conn:
            row = conn.execute(
                """SELECT html_path, generated_at, source_session_id, message_count, library_version
                   FROM html_cache
                   WHERE project_id = ? AND html_path = ?""",
                (self._project_id, html_path),
            ).fetchone()

        if not row:
            return None

        return HtmlCacheEntry(
            html_path=row["html_path"],
            generated_at=row["generated_at"],
            source_session_id=row["source_session_id"],
            message_count=row["message_count"] or 0,
            library_version=row["library_version"],
        )

    def update_html_cache(
        self,
        html_path: str,
        session_id: Optional[str],
        message_count: int,
    ) -> None:
        """Update or insert HTML cache entry."""
        if self._project_id is None:
            return

        with self._get_connection() as conn:
            conn.execute(
                """INSERT INTO html_cache
                   (project_id, html_path, generated_at, source_session_id, message_count, library_version)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, html_path)
                   DO UPDATE SET
                       generated_at = excluded.generated_at,
                       source_session_id = excluded.source_session_id,
                       message_count = excluded.message_count,
                       library_version = excluded.library_version""",
                (
                    self._project_id,
                    html_path,
                    datetime.now().isoformat(),
                    session_id,
                    message_count,
                    self.library_version,
                ),
            )
            conn.commit()

    def is_html_stale(
        self, html_path: str, session_id: Optional[str] = None
    ) -> tuple[bool, str]:
        """Check if HTML file needs regeneration.

        Args:
            html_path: Path to HTML file (e.g., "session-abc123.html")
            session_id: Session ID for individual session files, None for combined

        Returns:
            Tuple of (is_stale: bool, reason: str)
        """
        from .renderer import is_html_outdated

        if self._project_id is None:
            return True, "no_cache"

        # Get existing HTML cache entry
        html_cache = self.get_html_cache(html_path)
        if html_cache is None:
            return True, "not_cached"

        # Check library version in cache
        if html_cache.library_version != self.library_version:
            return True, "version_mismatch"

        # Check if file exists and has correct version
        actual_file = self.project_path / html_path
        if not actual_file.exists():
            return True, "file_missing"
        if is_html_outdated(actual_file):
            return True, "file_version_mismatch"

        with self._get_connection() as conn:
            if session_id is not None:
                # For individual session HTML: check if session message count changed
                row = conn.execute(
                    """SELECT message_count FROM sessions
                       WHERE project_id = ? AND session_id = ?""",
                    (self._project_id, session_id),
                ).fetchone()

                if not row:
                    return True, "session_not_found"

                # Compare message counts
                if row["message_count"] != html_cache.message_count:
                    return True, "session_updated"
            else:
                # For combined transcript: check if total message count changed
                # This is more reliable than timestamp comparison, which can
                # trigger false positives when cache metadata is updated
                row = conn.execute(
                    """SELECT total_message_count FROM projects
                       WHERE id = ?""",
                    (self._project_id,),
                ).fetchone()

                if row and row["total_message_count"] != html_cache.message_count:
                    return True, "project_updated"

        return False, "up_to_date"

    def get_stale_sessions(
        self, valid_session_ids: Optional[set[str]] = None
    ) -> List[tuple[str, str]]:
        """Get list of sessions that need HTML regeneration.

        Args:
            valid_session_ids: If provided, only check sessions in this set.
                Sessions not in this set are considered "archived" (JSONL deleted)
                and are skipped to avoid perpetual staleness.

        Returns:
            List of (session_id, reason) tuples for sessions needing regeneration
        """
        if self._project_id is None:
            return []

        stale_sessions: List[tuple[str, str]] = []

        with self._get_connection() as conn:
            # Get all sessions
            session_rows = conn.execute(
                """SELECT session_id, last_timestamp FROM sessions
                   WHERE project_id = ?""",
                (self._project_id,),
            ).fetchall()

            for row in session_rows:
                session_id = row["session_id"]

                # Skip archived sessions (JSONL deleted but cache remains)
                if (
                    valid_session_ids is not None
                    and session_id not in valid_session_ids
                ):
                    continue

                html_path = f"session-{session_id}.html"

                is_stale, reason = self.is_html_stale(html_path, session_id)
                if is_stale:
                    stale_sessions.append((session_id, reason))

        return stale_sessions

    def get_archived_session_count(self, valid_session_ids: set[str]) -> int:
        """Count sessions in cache whose JSONL files have been deleted.

        These are preserved for potential future archiving/restore features.

        Args:
            valid_session_ids: Set of session IDs that currently exist in source data

        Returns:
            Number of archived (orphan) sessions
        """
        if self._project_id is None:
            return 0

        with self._get_connection() as conn:
            cached_rows = conn.execute(
                "SELECT session_id FROM sessions WHERE project_id = ?",
                (self._project_id,),
            ).fetchall()

            return sum(
                1 for row in cached_rows if row["session_id"] not in valid_session_ids
            )

    def get_archived_sessions(
        self, valid_session_ids: set[str]
    ) -> Dict[str, SessionCacheData]:
        """Get session data for archived sessions (cached but JSONL deleted).

        Args:
            valid_session_ids: Set of session IDs that currently exist in source data

        Returns:
            Dict mapping session_id to SessionCacheData for archived sessions
        """
        if self._project_id is None:
            return {}

        archived_sessions: Dict[str, SessionCacheData] = {}

        with self._get_connection() as conn:
            session_rows = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ?",
                (self._project_id,),
            ).fetchall()

            for row in session_rows:
                session_id = row["session_id"]
                if session_id not in valid_session_ids:
                    archived_sessions[session_id] = SessionCacheData(
                        session_id=session_id,
                        summary=row["summary"],
                        first_timestamp=row["first_timestamp"],
                        last_timestamp=row["last_timestamp"],
                        message_count=row["message_count"],
                        first_user_message=row["first_user_message"],
                        cwd=row["cwd"],
                        total_input_tokens=row["total_input_tokens"],
                        total_output_tokens=row["total_output_tokens"],
                        total_cache_creation_tokens=row["total_cache_creation_tokens"],
                        total_cache_read_tokens=row["total_cache_read_tokens"],
                        team_name=row["team_name"]
                        if "team_name" in row.keys()
                        else None,
                    )

        return archived_sessions

    def export_session_to_jsonl(self, session_id: str) -> List[str]:
        """Export all message content JSONs for a session, for JSONL restoration.

        Args:
            session_id: The session ID to export

        Returns:
            List of JSON strings (one per line for JSONL file), compact format
        """
        if self._project_id is None:
            return []

        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT content FROM messages
                   WHERE project_id = ? AND session_id = ?
                   ORDER BY timestamp NULLS LAST""",
                (self._project_id, session_id),
            ).fetchall()

        # Content is stored as compressed, compact JSON - just decompress
        return [zlib.decompress(row["content"]).decode("utf-8") for row in rows]

    def load_session_entries(self, session_id: str) -> List[TranscriptEntry]:
        """Load transcript entries for a session from cache.

        Used for rendering archived sessions to HTML/Markdown when
        the original JSONL file no longer exists.

        Args:
            session_id: The session ID to load

        Returns:
            List of TranscriptEntry objects for the session
        """
        if self._project_id is None:
            return []

        with self._get_connection() as conn:
            rows = conn.execute(
                """SELECT content FROM messages
                   WHERE project_id = ? AND session_id = ?
                   ORDER BY timestamp NULLS LAST""",
                (self._project_id, session_id),
            ).fetchall()

        return [self._deserialize_entry(row) for row in rows]

    # ========== Page Cache Methods (Pagination) ==========

    def get_page_size_config(self) -> Optional[int]:
        """Get the configured page size, if any pages exist.

        All pages in a project share the same page_size_config value.
        """
        if self._project_id is None:
            return None

        with self._get_connection() as conn:
            row = conn.execute(
                """SELECT page_size_config FROM html_pages
                   WHERE project_id = ?
                   LIMIT 1""",
                (self._project_id,),
            ).fetchone()

        return row["page_size_config"] if row else None

    def get_page_data(
        self, page_number: int, variant_suffix: str = ""
    ) -> Optional[PageCacheData]:
        """Get cache data for a specific page.

        Args:
            page_number: Page number (1-indexed).
            variant_suffix: Detail/compact variant infix (e.g. ``""``,
                ``".low"``, ``".low.compact"``). Each variant has its
                own pagination cache row.
        """
        if self._project_id is None:
            return None

        with self._get_connection() as conn:
            # Get page info
            page_row = conn.execute(
                """SELECT * FROM html_pages
                   WHERE project_id = ? AND variant_suffix = ?
                         AND page_number = ?""",
                (self._project_id, variant_suffix, page_number),
            ).fetchone()

            if not page_row:
                return None

            # Get sessions for this page
            session_rows = conn.execute(
                """SELECT session_id FROM page_sessions
                   WHERE page_id = ?
                   ORDER BY session_order ASC""",
                (page_row["id"],),
            ).fetchall()

            session_ids = [row["session_id"] for row in session_rows]

        return PageCacheData(
            page_number=page_row["page_number"],
            html_path=page_row["html_path"],
            page_size_config=page_row["page_size_config"],
            message_count=page_row["message_count"],
            session_ids=session_ids,
            first_session_id=page_row["first_session_id"],
            last_session_id=page_row["last_session_id"],
            first_timestamp=page_row["first_timestamp"],
            last_timestamp=page_row["last_timestamp"],
            total_input_tokens=page_row["total_input_tokens"] or 0,
            total_output_tokens=page_row["total_output_tokens"] or 0,
            total_cache_creation_tokens=page_row["total_cache_creation_tokens"] or 0,
            total_cache_read_tokens=page_row["total_cache_read_tokens"] or 0,
            generated_at=page_row["generated_at"],
            library_version=page_row["library_version"],
        )

    def get_all_pages(self) -> List[PageCacheData]:
        """Get all cached pages for this project."""
        if self._project_id is None:
            return []

        pages: List[PageCacheData] = []
        with self._get_connection() as conn:
            page_rows = conn.execute(
                """SELECT * FROM html_pages
                   WHERE project_id = ?
                   ORDER BY page_number ASC""",
                (self._project_id,),
            ).fetchall()

            for page_row in page_rows:
                session_rows = conn.execute(
                    """SELECT session_id FROM page_sessions
                       WHERE page_id = ?
                       ORDER BY session_order ASC""",
                    (page_row["id"],),
                ).fetchall()

                session_ids = [row["session_id"] for row in session_rows]

                pages.append(
                    PageCacheData(
                        page_number=page_row["page_number"],
                        html_path=page_row["html_path"],
                        page_size_config=page_row["page_size_config"],
                        message_count=page_row["message_count"],
                        session_ids=session_ids,
                        first_session_id=page_row["first_session_id"],
                        last_session_id=page_row["last_session_id"],
                        first_timestamp=page_row["first_timestamp"],
                        last_timestamp=page_row["last_timestamp"],
                        total_input_tokens=page_row["total_input_tokens"] or 0,
                        total_output_tokens=page_row["total_output_tokens"] or 0,
                        total_cache_creation_tokens=page_row[
                            "total_cache_creation_tokens"
                        ]
                        or 0,
                        total_cache_read_tokens=page_row["total_cache_read_tokens"]
                        or 0,
                        generated_at=page_row["generated_at"],
                        library_version=page_row["library_version"],
                    )
                )

        return pages

    def update_page_cache(
        self,
        page_number: int,
        html_path: str,
        page_size_config: int,
        session_ids: List[str],
        message_count: int,
        first_timestamp: Optional[str],
        last_timestamp: Optional[str],
        total_input_tokens: int,
        total_output_tokens: int,
        total_cache_creation_tokens: int,
        total_cache_read_tokens: int,
        variant_suffix: str = "",
    ) -> None:
        """Update or insert page cache entry.

        Args:
            variant_suffix: Detail/compact variant infix (e.g. ``""``,
                ``".low"``). Each variant owns its own cache row; the
                UNIQUE constraint is on (project_id, variant_suffix,
                page_number).
        """
        if self._project_id is None or not session_ids:
            return

        with self._get_connection() as conn:
            # Insert or update page
            conn.execute(
                """INSERT INTO html_pages
                   (project_id, variant_suffix, page_number, html_path,
                    page_size_config, message_count,
                    first_session_id, last_session_id, first_timestamp, last_timestamp,
                    total_input_tokens, total_output_tokens,
                    total_cache_creation_tokens, total_cache_read_tokens,
                    generated_at, library_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, variant_suffix, page_number)
                   DO UPDATE SET
                       html_path = excluded.html_path,
                       page_size_config = excluded.page_size_config,
                       message_count = excluded.message_count,
                       first_session_id = excluded.first_session_id,
                       last_session_id = excluded.last_session_id,
                       first_timestamp = excluded.first_timestamp,
                       last_timestamp = excluded.last_timestamp,
                       total_input_tokens = excluded.total_input_tokens,
                       total_output_tokens = excluded.total_output_tokens,
                       total_cache_creation_tokens = excluded.total_cache_creation_tokens,
                       total_cache_read_tokens = excluded.total_cache_read_tokens,
                       generated_at = excluded.generated_at,
                       library_version = excluded.library_version""",
                (
                    self._project_id,
                    variant_suffix,
                    page_number,
                    html_path,
                    page_size_config,
                    message_count,
                    session_ids[0],
                    session_ids[-1],
                    first_timestamp,
                    last_timestamp,
                    total_input_tokens,
                    total_output_tokens,
                    total_cache_creation_tokens,
                    total_cache_read_tokens,
                    datetime.now().isoformat(),
                    self.library_version,
                ),
            )

            # Get the page ID
            row = conn.execute(
                """SELECT id FROM html_pages
                   WHERE project_id = ? AND variant_suffix = ?
                         AND page_number = ?""",
                (self._project_id, variant_suffix, page_number),
            ).fetchone()
            page_id = row["id"]

            # Delete existing session mappings
            conn.execute("DELETE FROM page_sessions WHERE page_id = ?", (page_id,))

            # Insert session mappings
            for order, session_id in enumerate(session_ids):
                conn.execute(
                    """INSERT INTO page_sessions (page_id, session_id, session_order)
                       VALUES (?, ?, ?)""",
                    (page_id, session_id, order),
                )

            conn.commit()

    def is_page_stale(
        self,
        page_number: int,
        page_size_config: int,
        variant_suffix: str = "",
    ) -> tuple[bool, str]:
        """Check if a page needs regeneration.

        Args:
            page_number: The page number to check.
            page_size_config: The current page size configuration.
            variant_suffix: Detail/compact variant infix; each variant's
                cache is checked independently.

        Returns:
            Tuple of (is_stale: bool, reason: str)
        """
        from .renderer import is_html_outdated

        if self._project_id is None:
            return True, "no_cache"

        page_data = self.get_page_data(page_number, variant_suffix)
        if page_data is None:
            return True, "not_cached"

        # Check if page size config changed
        if page_data.page_size_config != page_size_config:
            return True, "page_size_changed"

        # Check library version
        if page_data.library_version != self.library_version:
            return True, "version_mismatch"

        # Check if HTML file exists and has correct version
        actual_file = self.project_path / page_data.html_path
        if not actual_file.exists():
            return True, "file_missing"
        if is_html_outdated(actual_file):
            return True, "file_version_mismatch"

        # Check if any session on this page has changed
        with self._get_connection() as conn:
            # Build placeholders for IN clause
            placeholders = ",".join("?" for _ in page_data.session_ids)
            params = [self._project_id, *page_data.session_ids]

            row = conn.execute(
                f"""SELECT COUNT(*) as session_count,
                           COALESCE(SUM(message_count), 0) as total_messages,
                           MAX(last_timestamp) as max_timestamp
                    FROM sessions
                    WHERE project_id = ? AND session_id IN ({placeholders})""",
                params,
            ).fetchone()

            # Check if any sessions are missing
            if row["session_count"] != len(page_data.session_ids):
                return True, "session_missing"

            # Check if message count changed
            if row["total_messages"] != page_data.message_count:
                return True, "message_count_changed"

            # Check if last timestamp changed (session content updated)
            if row["max_timestamp"] != page_data.last_timestamp:
                return True, "timestamp_changed"

        return False, "up_to_date"

    def invalidate_all_pages(self) -> List[str]:
        """Delete all page cache entries for this project.

        Returns:
            List of HTML file paths that were invalidated (for cleanup)
        """
        if self._project_id is None:
            return []

        html_paths: List[str] = []

        with self._get_connection() as conn:
            # Get all page paths before deleting
            rows = conn.execute(
                """SELECT html_path FROM html_pages WHERE project_id = ?""",
                (self._project_id,),
            ).fetchall()
            html_paths = [row["html_path"] for row in rows]

            # Delete all pages (cascade deletes page_sessions)
            conn.execute(
                "DELETE FROM html_pages WHERE project_id = ?", (self._project_id,)
            )
            conn.commit()

        return html_paths

    def get_page_count(self, variant_suffix: str = "") -> int:
        """Get the number of cached pages for this project and variant.

        Orphan-cleanup loops in the converter must pass the same
        ``variant_suffix`` used for rendering, otherwise they'd count
        pages across variants and delete live pages of another variant.
        """
        if self._project_id is None:
            return 0

        with self._get_connection() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM html_pages
                   WHERE project_id = ? AND variant_suffix = ?""",
                (self._project_id, variant_suffix),
            ).fetchone()

        return row["cnt"] if row else 0

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its messages from cache.

        Args:
            session_id: The session ID to delete

        Returns:
            True if session was deleted, False if not found
        """
        if self._project_id is None:
            return False

        with self._get_connection() as conn:
            # Check if session exists
            row = conn.execute(
                "SELECT id FROM sessions WHERE project_id = ? AND session_id = ?",
                (self._project_id, session_id),
            ).fetchone()

            if not row:
                return False

            # Delete messages for this session
            conn.execute(
                "DELETE FROM messages WHERE project_id = ? AND session_id = ?",
                (self._project_id, session_id),
            )

            # Delete HTML cache entries for this session
            conn.execute(
                "DELETE FROM html_cache WHERE project_id = ? AND source_session_id = ?",
                (self._project_id, session_id),
            )

            # Delete page_sessions entries referencing this session
            conn.execute(
                """DELETE FROM page_sessions WHERE session_id = ?
                   AND page_id IN (SELECT id FROM html_pages WHERE project_id = ?)""",
                (session_id, self._project_id),
            )

            # Delete cached_files entry for this session's JSONL file
            # File name pattern is {session_id}.jsonl
            conn.execute(
                "DELETE FROM cached_files WHERE project_id = ? AND file_name = ?",
                (self._project_id, f"{session_id}.jsonl"),
            )

            # Delete the session record
            conn.execute(
                "DELETE FROM sessions WHERE project_id = ? AND session_id = ?",
                (self._project_id, session_id),
            )

            self._update_last_updated(conn)
            conn.commit()

        return True

    def delete_project(self) -> bool:
        """Delete this project and all its data from cache.

        Returns:
            True if project was deleted, False if not found
        """
        if self._project_id is None:
            return False

        with self._get_connection() as conn:
            # Cascade delete handles messages, sessions, cached_files, html_cache, html_pages
            conn.execute("DELETE FROM projects WHERE id = ?", (self._project_id,))
            conn.commit()

        self._project_id = None
        return True


def get_all_cached_projects(
    projects_dir: Path,
    db_path: Optional[Path] = None,
) -> List[tuple[str, bool]]:
    """Get all projects from cache, indicating which are archived.

    This is a standalone function that queries the cache database directly
    to find all project paths, without needing to instantiate CacheManager
    for each project.

    Args:
        projects_dir: Path to the projects directory (e.g., ~/.claude/projects)
        db_path: Optional explicit path to the cache database. If not provided,
            uses CLAUDE_CODE_LOG_CACHE_PATH env var or default location.

    Returns:
        List of (project_path, is_archived) tuples.
        is_archived is True if the project has no JSONL files but exists in cache.
    """
    # Priority: explicit db_path > env var > default location
    if db_path:
        actual_db_path = db_path
    else:
        actual_db_path = get_cache_db_path(projects_dir)

    if not actual_db_path.exists():
        return []

    result: List[tuple[str, bool]] = []

    try:
        conn = sqlite3.connect(actual_db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT project_path FROM projects ORDER BY project_path"
            ).fetchall()

            for row in rows:
                project_path = Path(row["project_path"])
                # Check if project has JSONL files (non-archived)
                has_jsonl = (
                    bool(list(project_path.glob("*.jsonl")))
                    if project_path.exists()
                    else False
                )
                # is_archived = project exists in cache but has no JSONL files
                is_archived = not has_jsonl
                result.append((row["project_path"], is_archived))
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        logger.debug("Failed to read cached projects from %s: %s", actual_db_path, e)

    return result


def find_session_in_cache(
    session_id: str,
    projects_dir: Path,
    db_path: Optional[Path] = None,
) -> list[tuple[str, str]]:
    """Find a session by ID or prefix across all projects in cache.

    Standalone function that queries the cache database directly.

    Args:
        session_id: Full session ID or prefix to match.
        projects_dir: Path to the projects directory (for default DB location).
        db_path: Optional explicit path to the cache database.

    Returns:
        List of (project_path, full_session_id) tuples for all matches.
    """
    actual_db_path = db_path or get_cache_db_path(projects_dir)
    if not actual_db_path.exists():
        return []

    try:
        conn = sqlite3.connect(actual_db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT p.project_path, s.session_id
                   FROM sessions s
                   JOIN projects p ON s.project_id = p.id
                   WHERE s.session_id = ? OR s.session_id LIKE ?
                   ORDER BY s.first_timestamp DESC""",
                (session_id, f"{session_id}%"),
            ).fetchall()
            return [(row["project_path"], row["session_id"]) for row in rows]
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as e:
        logger.debug("Failed to find session in cache: %s", e)
        return []


__all__ = [
    "CacheManager",
    "CachedFileInfo",
    "HtmlCacheEntry",
    "PageCacheData",
    "ProjectCache",
    "SessionCacheData",
    "find_session_in_cache",
    "get_all_cached_projects",
    "get_cache_db_path",
    "get_library_version",
]

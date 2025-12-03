#!/usr/bin/env python3
"""Cache management for Claude Code Log to improve performance."""

import json
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, cast
from datetime import datetime
from pydantic import BaseModel
from packaging import version

from .models import TranscriptEntry


# =============================================================================
# Exception Classes
# =============================================================================


class CacheError(Exception):
    """Base exception for cache operations."""

    pass


class CacheDatabaseError(CacheError):
    """SQLite database error."""

    pass


class CacheMigrationError(CacheError):
    """Error during JSON to SQLite migration."""

    pass


# =============================================================================
# SQLite Schema
# =============================================================================

SQLITE_SCHEMA = """
-- Schema versioning
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    migrated_at TEXT NOT NULL,
    library_version TEXT NOT NULL
);

-- Projects (replaces ProjectCache)
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_path TEXT UNIQUE NOT NULL,
    library_version TEXT NOT NULL,
    cache_created TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    total_message_count INTEGER DEFAULT 0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens INTEGER DEFAULT 0,
    earliest_timestamp TEXT DEFAULT '',
    latest_timestamp TEXT DEFAULT ''
);

-- Working directories
CREATE TABLE IF NOT EXISTS working_directories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    directory_path TEXT NOT NULL,
    UNIQUE(project_id, directory_path)
);

-- Cached files (replaces CachedFileInfo)
CREATE TABLE IF NOT EXISTS cached_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    source_mtime REAL NOT NULL,
    cached_mtime REAL NOT NULL,
    message_count INTEGER NOT NULL,
    UNIQUE(project_id, file_name)
);

-- Session IDs per file
CREATE TABLE IF NOT EXISTS file_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cached_file_id INTEGER NOT NULL REFERENCES cached_files(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    UNIQUE(cached_file_id, session_id)
);

-- Sessions (replaces SessionCacheData)
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    summary TEXT,
    first_timestamp TEXT NOT NULL,
    last_timestamp TEXT NOT NULL,
    message_count INTEGER NOT NULL,
    first_user_message TEXT DEFAULT '',
    cwd TEXT,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens INTEGER DEFAULT 0,
    UNIQUE(project_id, session_id)
);

-- Cached entries (JSON blobs keyed by timestamp)
CREATE TABLE IF NOT EXISTS cached_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cached_file_id INTEGER NOT NULL REFERENCES cached_files(id) ON DELETE CASCADE,
    timestamp_key TEXT NOT NULL,
    entries_json TEXT NOT NULL
);

-- Future: tags table placeholder
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    tag_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notes TEXT,
    UNIQUE(session_id, tag_name)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_cached_entries_file ON cached_entries(cached_file_id);
CREATE INDEX IF NOT EXISTS idx_cached_entries_timestamp ON cached_entries(timestamp_key);
CREATE INDEX IF NOT EXISTS idx_sessions_session_id ON sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
CREATE INDEX IF NOT EXISTS idx_cached_files_project ON cached_files(project_id);
CREATE INDEX IF NOT EXISTS idx_working_directories_project ON working_directories(project_id);
"""

# Current schema version - increment when making breaking schema changes
CURRENT_SCHEMA_VERSION = 1


class CachedFileInfo(BaseModel):
    """Information about a cached JSONL file."""

    file_path: str
    source_mtime: float
    cached_mtime: float
    message_count: int
    session_ids: List[str]


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


class ProjectCache(BaseModel):
    """Project-level cache index structure for index.json."""

    version: str
    cache_created: str
    last_updated: str
    project_path: str

    # File-level cache information
    cached_files: Dict[str, CachedFileInfo]

    # Aggregated project information
    total_message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0

    # Session metadata
    sessions: Dict[str, SessionCacheData]

    # Working directories associated with this project
    working_directories: List[str] = []

    # Timeline information
    earliest_timestamp: str = ""
    latest_timestamp: str = ""


class CacheManager:
    """Manages cache operations for a project directory using SQLite."""

    # Class-level database configuration
    _db_path: ClassVar[Path] = Path.home() / ".claude" / "cache.db"
    _local: ClassVar[threading.local] = threading.local()
    _db_initialized: ClassVar[bool] = False
    _init_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, project_path: Path, library_version: str):
        """Initialize cache manager for a project.

        Args:
            project_path: Path to the project directory containing JSONL files
            library_version: Current version of the library for cache invalidation
        """
        self.project_path = project_path
        self.library_version = library_version
        self._project_id: Optional[int] = None

        # Legacy paths for JSON cache migration
        self.cache_dir = project_path / "cache"
        self.index_file = self.cache_dir / "index.json"

        # Ensure database exists and schema is current
        self._ensure_database()

        # Migrate JSON cache if it exists
        self._migrate_json_cache_if_needed()

        # Load or create project record
        self._ensure_project_record()

    @classmethod
    def set_db_path(cls, path: Path) -> None:
        """Set custom database path (useful for testing)."""
        cls._db_path = path
        cls._db_initialized = False

    @property
    def _connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            self._local.connection.row_factory = sqlite3.Row
            # Enable foreign keys and WAL mode for better concurrency
            self._local.connection.execute("PRAGMA foreign_keys = ON")
            self._local.connection.execute("PRAGMA journal_mode = WAL")
        return self._local.connection

    @classmethod
    def close_all_connections(cls) -> None:
        """Close all thread-local connections (for cleanup)."""
        if hasattr(cls._local, "connection") and cls._local.connection is not None:
            cls._local.connection.close()
            cls._local.connection = None

    def _ensure_database(self) -> None:
        """Ensure database exists and schema is current."""
        with self._init_lock:
            if CacheManager._db_initialized:
                return

            # Ensure parent directory exists
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                # Create schema
                self._connection.executescript(SQLITE_SCHEMA)

                # Check/update schema version
                cursor = self._connection.execute(
                    "SELECT MAX(version) FROM schema_version"
                )
                row = cursor.fetchone()
                current_version = row[0] if row and row[0] else 0

                if current_version < CURRENT_SCHEMA_VERSION:
                    # Record new schema version
                    self._connection.execute(
                        """
                        INSERT OR REPLACE INTO schema_version (version, migrated_at, library_version)
                        VALUES (?, ?, ?)
                        """,
                        (
                            CURRENT_SCHEMA_VERSION,
                            datetime.now().isoformat(),
                            self.library_version,
                        ),
                    )
                    self._connection.commit()

                CacheManager._db_initialized = True
            except sqlite3.Error as e:
                raise CacheDatabaseError(f"Failed to initialize database: {e}") from e

    def _ensure_project_record(self) -> None:
        """Ensure project record exists in database and cache project_id."""
        project_path_str = str(self.project_path)

        try:
            # Try to get existing project
            cursor = self._connection.execute(
                "SELECT id, library_version FROM projects WHERE project_path = ?",
                (project_path_str,),
            )
            row = cursor.fetchone()

            if row:
                self._project_id = row["id"]
                cached_version = row["library_version"]

                # Check version compatibility
                if not self._is_cache_version_compatible(cached_version):
                    print(
                        f"Cache version incompatible: {cached_version} -> {self.library_version}, invalidating cache"
                    )
                    self.clear_cache()
                    # Re-create project after clearing
                    self._create_project_record(project_path_str)
            else:
                # Create new project record
                self._create_project_record(project_path_str)

        except sqlite3.Error as e:
            raise CacheDatabaseError(f"Failed to ensure project record: {e}") from e

    def _create_project_record(self, project_path_str: str) -> None:
        """Create a new project record in the database."""
        now = datetime.now().isoformat()
        cursor = self._connection.execute(
            """
            INSERT INTO projects (project_path, library_version, cache_created, last_updated)
            VALUES (?, ?, ?, ?)
            """,
            (project_path_str, self.library_version, now, now),
        )
        self._connection.commit()
        self._project_id = cursor.lastrowid

    def _migrate_json_cache_if_needed(self) -> None:
        """Migrate existing JSON cache to SQLite if present."""
        if not self.index_file.exists():
            return  # No JSON cache to migrate

        # Check if project already exists in SQLite
        cursor = self._connection.execute(
            "SELECT id FROM projects WHERE project_path = ?",
            (str(self.project_path),),
        )
        if cursor.fetchone():
            # Project already in SQLite, just clean up JSON cache
            self._remove_json_cache()
            return

        try:
            # Load JSON cache
            with open(self.index_file, "r", encoding="utf-8") as f:
                json_cache = json.load(f)

            # Begin migration transaction
            now = datetime.now().isoformat()

            # Insert project
            cursor = self._connection.execute(
                """
                INSERT INTO projects (
                    project_path, library_version, cache_created, last_updated,
                    total_message_count, total_input_tokens, total_output_tokens,
                    total_cache_creation_tokens, total_cache_read_tokens,
                    earliest_timestamp, latest_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(self.project_path),
                    json_cache.get("version", self.library_version),
                    json_cache.get("cache_created", now),
                    now,
                    json_cache.get("total_message_count", 0),
                    json_cache.get("total_input_tokens", 0),
                    json_cache.get("total_output_tokens", 0),
                    json_cache.get("total_cache_creation_tokens", 0),
                    json_cache.get("total_cache_read_tokens", 0),
                    json_cache.get("earliest_timestamp", ""),
                    json_cache.get("latest_timestamp", ""),
                ),
            )
            project_id = cursor.lastrowid

            # Migrate sessions
            for session_id, session_data in json_cache.get("sessions", {}).items():
                self._connection.execute(
                    """
                    INSERT INTO sessions (
                        project_id, session_id, summary, first_timestamp, last_timestamp,
                        message_count, first_user_message, cwd,
                        total_input_tokens, total_output_tokens,
                        total_cache_creation_tokens, total_cache_read_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        session_id,
                        session_data.get("summary"),
                        session_data.get("first_timestamp", ""),
                        session_data.get("last_timestamp", ""),
                        session_data.get("message_count", 0),
                        session_data.get("first_user_message", ""),
                        session_data.get("cwd"),
                        session_data.get("total_input_tokens", 0),
                        session_data.get("total_output_tokens", 0),
                        session_data.get("total_cache_creation_tokens", 0),
                        session_data.get("total_cache_read_tokens", 0),
                    ),
                )

            # Migrate working directories
            for directory in json_cache.get("working_directories", []):
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO working_directories (project_id, directory_path)
                    VALUES (?, ?)
                    """,
                    (project_id, directory),
                )

            # Migrate cached files and their entries
            for file_name, file_info in json_cache.get("cached_files", {}).items():
                cursor = self._connection.execute(
                    """
                    INSERT INTO cached_files (
                        project_id, file_name, file_path, source_mtime, cached_mtime, message_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        file_name,
                        file_info.get("file_path", ""),
                        file_info.get("source_mtime", 0),
                        file_info.get("cached_mtime", 0),
                        file_info.get("message_count", 0),
                    ),
                )
                cached_file_id = cursor.lastrowid

                # Migrate session IDs for this file
                for session_id in file_info.get("session_ids", []):
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO file_sessions (cached_file_id, session_id)
                        VALUES (?, ?)
                        """,
                        (cached_file_id, session_id),
                    )

                # Migrate cached entries from separate JSON file
                entry_file = self.cache_dir / f"{Path(file_name).stem}.json"
                if entry_file.exists():
                    try:
                        with open(entry_file, "r", encoding="utf-8") as f:
                            entries_by_timestamp = json.load(f)
                        for timestamp_key, entries in entries_by_timestamp.items():
                            self._connection.execute(
                                """
                                INSERT INTO cached_entries (cached_file_id, timestamp_key, entries_json)
                                VALUES (?, ?, ?)
                                """,
                                (cached_file_id, timestamp_key, json.dumps(entries)),
                            )
                    except Exception as e:
                        print(
                            f"Warning: Failed to migrate entries from {entry_file}: {e}"
                        )

            self._connection.commit()
            self._project_id = project_id

            # Clean up JSON cache after successful migration
            self._remove_json_cache()
            print(f"Migrated JSON cache to SQLite for {self.project_path}")

        except Exception as e:
            self._connection.rollback()
            raise CacheMigrationError(f"Failed to migrate JSON cache: {e}") from e

    def _remove_json_cache(self) -> None:
        """Remove the old JSON cache directory."""
        if self.cache_dir.exists():
            try:
                shutil.rmtree(self.cache_dir)
            except Exception as e:
                print(f"Warning: Failed to remove JSON cache directory: {e}")

    def _get_cached_file_id(self, jsonl_path: Path) -> Optional[int]:
        """Get the cached_file_id for a JSONL file, or None if not cached."""
        cursor = self._connection.execute(
            "SELECT id FROM cached_files WHERE project_id = ? AND file_name = ?",
            (self._project_id, jsonl_path.name),
        )
        row = cursor.fetchone()
        return row["id"] if row else None

    def is_file_cached(self, jsonl_path: Path) -> bool:
        """Check if a JSONL file has a valid cache entry."""
        if self._project_id is None:
            return False

        # Check if source file exists
        if not jsonl_path.exists():
            return False

        cursor = self._connection.execute(
            "SELECT source_mtime FROM cached_files WHERE project_id = ? AND file_name = ?",
            (self._project_id, jsonl_path.name),
        )
        row = cursor.fetchone()

        if row is None:
            return False

        # Check modification time
        source_mtime = jsonl_path.stat().st_mtime
        return abs(source_mtime - row["source_mtime"]) < 1.0

    def load_cached_entries(self, jsonl_path: Path) -> Optional[List[TranscriptEntry]]:
        """Load cached transcript entries for a JSONL file."""
        if not self.is_file_cached(jsonl_path):
            return None

        cached_file_id = self._get_cached_file_id(jsonl_path)
        if cached_file_id is None:
            return None

        try:
            cursor = self._connection.execute(
                "SELECT entries_json FROM cached_entries WHERE cached_file_id = ?",
                (cached_file_id,),
            )

            # Flatten all entries from all timestamps
            entries_data: List[Dict[str, Any]] = []
            for row in cursor:
                timestamp_entries = json.loads(row["entries_json"])
                if isinstance(timestamp_entries, list):
                    entries_data.extend(cast(List[Dict[str, Any]], timestamp_entries))

            # Deserialize back to TranscriptEntry objects
            from .models import parse_transcript_entry

            entries = [
                parse_transcript_entry(entry_dict) for entry_dict in entries_data
            ]
            return entries
        except Exception as e:
            print(f"Warning: Failed to load cached entries for {jsonl_path}: {e}")
            return None

    def load_cached_entries_filtered(
        self, jsonl_path: Path, from_date: Optional[str], to_date: Optional[str]
    ) -> Optional[List[TranscriptEntry]]:
        """Load cached entries with efficient timestamp-based filtering."""
        if not self.is_file_cached(jsonl_path):
            return None

        # If no date filtering needed, fall back to regular loading
        if not from_date and not to_date:
            return self.load_cached_entries(jsonl_path)

        cached_file_id = self._get_cached_file_id(jsonl_path)
        if cached_file_id is None:
            return None

        try:
            # Parse date filters
            from .parser import parse_timestamp
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
                    if to_date in ["today", "yesterday"] or "days ago" in to_date:
                        to_dt = to_dt.replace(
                            hour=23, minute=59, second=59, microsecond=999999
                        )
                    else:
                        to_dt = to_dt.replace(
                            hour=23, minute=59, second=59, microsecond=999999
                        )

            # Query entries - we'll filter in Python since timestamp format varies
            cursor = self._connection.execute(
                "SELECT timestamp_key, entries_json FROM cached_entries WHERE cached_file_id = ?",
                (cached_file_id,),
            )

            filtered_entries_data: List[Dict[str, Any]] = []

            for row in cursor:
                timestamp_key = row["timestamp_key"]
                timestamp_entries = json.loads(row["entries_json"])

                if timestamp_key == "_no_timestamp":
                    # Always include entries without timestamps (like summaries)
                    if isinstance(timestamp_entries, list):
                        filtered_entries_data.extend(
                            cast(List[Dict[str, Any]], timestamp_entries)
                        )
                else:
                    # Check if timestamp falls within range
                    message_dt = parse_timestamp(timestamp_key)
                    if message_dt:
                        # Convert to naive datetime for comparison
                        if message_dt.tzinfo:
                            message_dt = message_dt.replace(tzinfo=None)

                        # Apply date filtering
                        if from_dt and message_dt < from_dt:
                            continue
                        if to_dt and message_dt > to_dt:
                            continue

                    if isinstance(timestamp_entries, list):
                        filtered_entries_data.extend(
                            cast(List[Dict[str, Any]], timestamp_entries)
                        )

            # Deserialize filtered entries
            from .models import parse_transcript_entry

            entries = [
                parse_transcript_entry(entry_dict)
                for entry_dict in filtered_entries_data
            ]
            return entries
        except Exception as e:
            print(
                f"Warning: Failed to load filtered cached entries for {jsonl_path}: {e}"
            )
            return None

    def save_cached_entries(
        self, jsonl_path: Path, entries: List[TranscriptEntry]
    ) -> None:
        """Save parsed transcript entries to cache with timestamp-based structure."""
        if self._project_id is None:
            return

        try:
            source_mtime = jsonl_path.stat().st_mtime
            cached_mtime = datetime.now().timestamp()

            # Extract session IDs from entries
            session_ids: List[str] = []
            for entry in entries:
                if hasattr(entry, "sessionId"):
                    session_id = getattr(entry, "sessionId", "")
                    if session_id:
                        session_ids.append(session_id)
            session_ids = list(set(session_ids))  # Remove duplicates

            # Group entries by timestamp
            entries_by_timestamp: Dict[str, List[Dict[str, Any]]] = {}
            for entry in entries:
                timestamp = (
                    getattr(entry, "timestamp", "")
                    if hasattr(entry, "timestamp")
                    else ""
                )
                if not timestamp:
                    timestamp = "_no_timestamp"

                if timestamp not in entries_by_timestamp:
                    entries_by_timestamp[timestamp] = []
                entries_by_timestamp[timestamp].append(entry.model_dump())

            # Insert or update cached_files
            cursor = self._connection.execute(
                """
                INSERT INTO cached_files (project_id, file_name, file_path, source_mtime, cached_mtime, message_count)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, file_name) DO UPDATE SET
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

            # Get the cached_file_id
            cursor = self._connection.execute(
                "SELECT id FROM cached_files WHERE project_id = ? AND file_name = ?",
                (self._project_id, jsonl_path.name),
            )
            cached_file_id = cursor.fetchone()["id"]

            # Clear old entries and session mappings for this file
            self._connection.execute(
                "DELETE FROM cached_entries WHERE cached_file_id = ?",
                (cached_file_id,),
            )
            self._connection.execute(
                "DELETE FROM file_sessions WHERE cached_file_id = ?",
                (cached_file_id,),
            )

            # Insert new entries
            for timestamp_key, timestamp_entries in entries_by_timestamp.items():
                self._connection.execute(
                    """
                    INSERT INTO cached_entries (cached_file_id, timestamp_key, entries_json)
                    VALUES (?, ?, ?)
                    """,
                    (cached_file_id, timestamp_key, json.dumps(timestamp_entries)),
                )

            # Insert session mappings
            for session_id in session_ids:
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO file_sessions (cached_file_id, session_id)
                    VALUES (?, ?)
                    """,
                    (cached_file_id, session_id),
                )

            # Update last_updated timestamp for project
            self._connection.execute(
                "UPDATE projects SET last_updated = ? WHERE id = ?",
                (datetime.now().isoformat(), self._project_id),
            )

            self._connection.commit()
        except Exception as e:
            print(f"Warning: Failed to save cached entries for {jsonl_path}: {e}")
            self._connection.rollback()

    def update_session_cache(self, session_data: Dict[str, SessionCacheData]) -> None:
        """Update cached session information."""
        if self._project_id is None:
            return

        try:
            for session_id, data in session_data.items():
                self._connection.execute(
                    """
                    INSERT INTO sessions (
                        project_id, session_id, summary, first_timestamp, last_timestamp,
                        message_count, first_user_message, cwd,
                        total_input_tokens, total_output_tokens,
                        total_cache_creation_tokens, total_cache_read_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        total_cache_read_tokens = excluded.total_cache_read_tokens
                    """,
                    (
                        self._project_id,
                        session_id,
                        data.summary,
                        data.first_timestamp,
                        data.last_timestamp,
                        data.message_count,
                        data.first_user_message,
                        data.cwd,
                        data.total_input_tokens,
                        data.total_output_tokens,
                        data.total_cache_creation_tokens,
                        data.total_cache_read_tokens,
                    ),
                )

            # Update last_updated timestamp for project
            self._connection.execute(
                "UPDATE projects SET last_updated = ? WHERE id = ?",
                (datetime.now().isoformat(), self._project_id),
            )

            self._connection.commit()
        except Exception as e:
            print(f"Warning: Failed to update session cache: {e}")
            self._connection.rollback()

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

        try:
            self._connection.execute(
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
            self._connection.commit()
        except Exception as e:
            print(f"Warning: Failed to update project aggregates: {e}")
            self._connection.rollback()

    def update_working_directories(self, working_directories: List[str]) -> None:
        """Update the list of working directories associated with this project."""
        if self._project_id is None:
            return

        try:
            # Delete existing working directories
            self._connection.execute(
                "DELETE FROM working_directories WHERE project_id = ?",
                (self._project_id,),
            )

            # Insert new working directories
            for directory in working_directories:
                self._connection.execute(
                    """
                    INSERT INTO working_directories (project_id, directory_path)
                    VALUES (?, ?)
                    """,
                    (self._project_id, directory),
                )

            # Update last_updated timestamp for project
            self._connection.execute(
                "UPDATE projects SET last_updated = ? WHERE id = ?",
                (datetime.now().isoformat(), self._project_id),
            )

            self._connection.commit()
        except Exception as e:
            print(f"Warning: Failed to update working directories: {e}")
            self._connection.rollback()

    def get_modified_files(self, jsonl_files: List[Path]) -> List[Path]:
        """Get list of JSONL files that need to be reprocessed."""
        modified_files: List[Path] = []

        for jsonl_file in jsonl_files:
            if not self.is_file_cached(jsonl_file):
                modified_files.append(jsonl_file)

        return modified_files

    def get_cached_project_data(self) -> Optional[ProjectCache]:
        """Get the cached project data, reconstructing from SQLite."""
        if self._project_id is None:
            return None

        try:
            # Load project record
            cursor = self._connection.execute(
                "SELECT * FROM projects WHERE id = ?",
                (self._project_id,),
            )
            project_row = cursor.fetchone()
            if project_row is None:
                return None

            # Load cached files
            cached_files: Dict[str, CachedFileInfo] = {}
            cursor = self._connection.execute(
                """
                SELECT cf.*, GROUP_CONCAT(fs.session_id) as session_ids_str
                FROM cached_files cf
                LEFT JOIN file_sessions fs ON cf.id = fs.cached_file_id
                WHERE cf.project_id = ?
                GROUP BY cf.id
                """,
                (self._project_id,),
            )
            for row in cursor:
                session_ids_str: Optional[str] = row["session_ids_str"]
                file_session_ids: List[str] = (
                    session_ids_str.split(",") if session_ids_str else []
                )
                cached_files[row["file_name"]] = CachedFileInfo(
                    file_path=row["file_path"],
                    source_mtime=row["source_mtime"],
                    cached_mtime=row["cached_mtime"],
                    message_count=row["message_count"],
                    session_ids=file_session_ids,
                )

            # Load sessions
            sessions: Dict[str, SessionCacheData] = {}
            cursor = self._connection.execute(
                "SELECT * FROM sessions WHERE project_id = ?",
                (self._project_id,),
            )
            for row in cursor:
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
                )

            # Load working directories
            cursor = self._connection.execute(
                "SELECT directory_path FROM working_directories WHERE project_id = ?",
                (self._project_id,),
            )
            working_directories = [row["directory_path"] for row in cursor]

            # Construct ProjectCache
            return ProjectCache(
                version=project_row["library_version"],
                cache_created=project_row["cache_created"],
                last_updated=project_row["last_updated"],
                project_path=project_row["project_path"],
                cached_files=cached_files,
                sessions=sessions,
                working_directories=working_directories,
                total_message_count=project_row["total_message_count"],
                total_input_tokens=project_row["total_input_tokens"],
                total_output_tokens=project_row["total_output_tokens"],
                total_cache_creation_tokens=project_row["total_cache_creation_tokens"],
                total_cache_read_tokens=project_row["total_cache_read_tokens"],
                earliest_timestamp=project_row["earliest_timestamp"],
                latest_timestamp=project_row["latest_timestamp"],
            )
        except Exception as e:
            print(f"Warning: Failed to load cached project data: {e}")
            return None

    def clear_cache(self) -> None:
        """Clear all cache data for this project from SQLite."""
        if self._project_id is None:
            return

        try:
            # Delete project (cascades to all related tables)
            self._connection.execute(
                "DELETE FROM projects WHERE id = ?",
                (self._project_id,),
            )
            self._connection.commit()

            # Reset project_id so it will be recreated on next access
            self._project_id = None

            # Also clean up any legacy JSON cache if it exists
            if self.cache_dir.exists():
                shutil.rmtree(self.cache_dir)

        except Exception as e:
            print(f"Warning: Failed to clear cache: {e}")
            self._connection.rollback()

    def _is_cache_version_compatible(self, cache_version: str) -> bool:
        """Check if a cache version is compatible with the current library version.

        This uses a compatibility matrix to determine if cache invalidation is needed.
        Only breaking changes require cache invalidation, not every version bump.
        """
        if cache_version == self.library_version:
            return True

        # Define compatibility rules
        # Format: "cache_version": "minimum_library_version_required"
        # If cache version is older than the minimum required, it needs invalidation
        breaking_changes: dict[str, str] = {
            # Example breaking changes (adjust as needed):
            # "0.3.3": "0.3.4",  # 0.3.4 introduced breaking changes to cache format
            # "0.2.x": "0.3.0",  # 0.3.0 introduced major cache format changes
        }

        cache_ver = version.parse(cache_version)
        current_ver = version.parse(self.library_version)

        # Check if cache version requires invalidation due to breaking changes
        for breaking_version_pattern, min_required in breaking_changes.items():
            min_required_ver = version.parse(min_required)

            # If current version is at or above the minimum required for this breaking change
            if current_ver >= min_required_ver:
                # Check if cache version is affected by this breaking change
                if breaking_version_pattern.endswith(".x"):
                    # Pattern like "0.2.x" matches any 0.2.* version
                    major_minor = breaking_version_pattern[:-2]
                    if str(cache_ver).startswith(major_minor):
                        return False
                else:
                    # Exact version or version comparison
                    breaking_ver = version.parse(breaking_version_pattern)
                    if cache_ver <= breaking_ver:
                        return False

        # If no breaking changes affect this cache version, it's compatible
        return True

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics for reporting."""
        if self._project_id is None:
            return {"cache_enabled": False}

        try:
            # Get project info
            cursor = self._connection.execute(
                "SELECT cache_created, last_updated, total_message_count FROM projects WHERE id = ?",
                (self._project_id,),
            )
            project_row = cursor.fetchone()
            if project_row is None:
                return {"cache_enabled": False}

            # Count cached files
            cursor = self._connection.execute(
                "SELECT COUNT(*) as count FROM cached_files WHERE project_id = ?",
                (self._project_id,),
            )
            cached_files_count = cursor.fetchone()["count"]

            # Count sessions
            cursor = self._connection.execute(
                "SELECT COUNT(*) as count FROM sessions WHERE project_id = ?",
                (self._project_id,),
            )
            sessions_count = cursor.fetchone()["count"]

            return {
                "cache_enabled": True,
                "cached_files_count": cached_files_count,
                "total_cached_messages": project_row["total_message_count"],
                "total_sessions": sessions_count,
                "cache_created": project_row["cache_created"],
                "last_updated": project_row["last_updated"],
            }
        except Exception as e:
            print(f"Warning: Failed to get cache stats: {e}")
            return {"cache_enabled": False}


def get_library_version() -> str:
    """Get the current library version from package metadata or pyproject.toml."""
    # First try to get version from installed package metadata
    try:
        from importlib.metadata import version

        return version("claude-code-log")
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

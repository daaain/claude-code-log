#!/usr/bin/env python3
"""SQLite-based cache management for Claude Code Log."""

import functools
import hashlib
import json
import logging
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

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


class CachedFileState(BaseModel):
    """One file's identity-relevant cached state, for the incremental
    cache refresh (work/render-format-once.md, stage 4).

    Captured from the messages table *before* a modified file is
    re-parsed (the parse replaces its rows), so the refresh can compute
    the append-only check, the coupling closure, and the metadata /
    attachment count deltas against what the last refresh saw.
    """

    sessions: set[str] = set()
    uuids: set[str] = set()
    parent_uuids: set[str] = set()
    request_ids: set[str] = set()
    # Counts by entry type for the classes compute_session_data skips
    # (summary / ai-title / attachment) — the residual terms of the
    # total_message_count identity.
    type_counts: Dict[str, int] = {}
    # Metadata this file *names*: summary leafUuids (resolve to owner
    # sessions) and ai-title sessionIds. A summary in one session's file
    # routinely titles another session, so these sessions' cache rows
    # must be recomputed when this file changes even though none of
    # their own entries did.
    summary_leaf_uuids: set[str] = set()
    ai_title_sessions: set[str] = set()
    # SHA-256 of each cached entry's complete serialized model, in source
    # insertion order. The old sequence must be an exact prefix of the new
    # one for an incremental refresh to be append-only.
    row_fingerprints: List[bytes] = []


class SessionCacheData(BaseModel):
    """Cached session-level information."""

    session_id: str
    summary: Optional[str] = None
    # Claude Code's AI-generated short session title, sourced from
    # `ai-title` JSONL entries (last one wins). Preferred over `summary`
    # for display when present.
    ai_title: Optional[str] = None
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
    # Migration 010: traversed entries this session owns that
    # ``compute_session_data`` does not count (attachment / ai-title).
    # ``None`` marks a row written before the migration — an unknown
    # basis the incremental refresh must decline on, never a zero.
    residual_count: Optional[int] = None
    # Migration 009: warmup-only and empty/agent-only sessions are
    # persisted with hidden=True instead of being filtered before the
    # write, so the incremental cache refresh can compute aggregate
    # deltas over every session's contribution. Read sites that mean
    # "sessions a human would render" filter these out, preserving the
    # pre-009 visible set byte-for-byte.
    hidden: bool = False


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


class SessionSidecar(BaseModel):
    """Cross-session facts persisted at full-load time (migration 008).

    Compact projections of a full directory load's ``SessionTree``, kept
    so a later run can regenerate one stale session's file from that
    session's own JSONL alone (the session-scoped incremental path —
    ``converter._load_stale_session_transcripts``). Each field answers
    one question a partial load cannot answer from its own entries:

    - ``parents``: ``{session_id: (parent_session_id, attachment_uuid)}``
      for every DAG-line attached to a parent session — restores the
      resume/fork linkage when the parent session isn't loaded.
    - ``junctions``: ``{uuid: (session_id, [target_session_ids])}`` for
      every junction point, targets in the tree's chronological order —
      restores fork markers whose target sessions aren't loaded.
    - ``dedup_winners``: ``{uuid: winner_session_id}`` for uuids carried
      by more than one session (resume replay prefixes) — tells a
      partial load which of its entries the whole-project dedup drops.
    """

    parents: dict[str, tuple[Optional[str], Optional[str]]]
    junctions: dict[str, tuple[str, list[str]]]
    dedup_winners: dict[str, str]


# ========== Helper Functions ==========


# Lone TOOL surrogates (U+D800–U+DBFF) are not in surrogateescape's
# back-mapping range (which only covers low surrogates U+DC80–U+DCFF
# produced from raw byte decoding), so the surrogateescape-encode step
# in scrub_surrogates would raise on them. Pre-substitute high
# surrogates directly with U+FFFD before the encode step.
_HIGH_SURROGATE_RE = re.compile(r"[\ud800-\udbff]")


def scrub_surrogates(s: Optional[str]) -> Optional[str]:
    """Replace lone surrogates (U+D800…U+DFFF) with U+FFFD.

    Public helper used wherever ``surrogateescape``-decoded JSONL data
    might cross a strict-UTF-8 boundary (issue #139). Examples:
    sqlite3's text-binding path (``cache.update_session_cache``) and
    TUI export (``tui.SessionBrowser._ensure_session_file``) — both
    raise ``UnicodeEncodeError`` on lone surrogates the moment we try
    to persist them.

    Two ranges to handle, with two different mechanisms:

    - **Low surrogates** (U+DC80…U+DCFF) — what ``surrogateescape``
      uses when decoding raw bytes. ``encode("utf-8",
      errors="surrogateescape")`` reverses the mapping back to the
      original raw byte (``\\udcb2`` → ``b"\\xb2"``); the subsequent
      ``decode("utf-8", errors="replace")`` then substitutes those
      invalid bytes with U+FFFD on the decode side.
    - **High surrogates** (U+D800…U+DBFF) — ``surrogateescape`` has
      no mapping for these (they're not produced by raw-byte decoding),
      so the encode step would raise ``UnicodeEncodeError``. Pre-scrub
      them directly via regex substitution before the encode step.

    Why not the simpler ``encode(..., errors="replace")``? That emits
    ASCII ``?`` (U+003F) on encode rather than the canonical Unicode
    replacement U+FFFD on decode — also valid UTF-8, but a less
    informative sentinel.
    """
    if s is None:
        return None
    s = _HIGH_SURROGATE_RE.sub("�", s)
    return s.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


@functools.lru_cache(maxsize=1)
def get_library_version() -> str:
    """Get the current library version from package metadata or pyproject.toml.

    Memoised because it is called *per rendered file* on the staleness
    path (``is_transcript_stale`` → ``is_html_outdated``), and each call
    re-parses the installed package metadata through
    ``importlib.metadata``: 173 calls and 35 ms of a 1.1 s watch tick on
    a 217-session project, scaling with the session count rather than
    with anything that changed. An installed version cannot change inside
    a process, so a one-slot cache is exact; tests that need a different
    value patch this name on the module, which is unaffected.
    """
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


def subagents_fingerprint(jsonl_path: Path) -> str:
    """Fingerprint of the sidecar inputs that feed spawn discovery (#213).

    Parsing a transcript also reads ``agent-*.meta.json`` sidecars (see
    ``converter._subagent_meta_map``), so they must take part in cache
    invalidation: a sidecar appearing after the transcript was cached
    (e.g. a still-running agent spawning a child without touching the
    trunk file) would otherwise go unnoticed by the mtime-only check.

    The fingerprint is ``"<count>:<newest int mtime>"`` over the sidecars
    of the dirs where this transcript's children can live — the sibling
    ``<stem>/subagents/`` dir, plus the containing dir for agent files
    (the flat layout puts children next to their parent). Sidecars are
    written once at spawn time, so count+newest-mtime captures every
    addition and removal. Empty string when there are none.

    Deliberately narrower than ``converter._subagent_meta_map``'s scan
    for TRUNK files: the parse also defensively checks the project dir
    itself, but Claude Code never writes sidecars there, and
    fingerprinting it would rescan a potentially large dir on every
    cache read for a theoretical-only input.
    """
    candidates = [jsonl_path.parent / jsonl_path.stem / "subagents"]
    if jsonl_path.parent.name == "subagents":
        candidates.append(jsonl_path.parent)
    metas: list[Path] = []
    for directory in candidates:
        if directory.is_dir():
            metas.extend(directory.glob("agent-*.meta.json"))
    if not metas:
        return ""
    try:
        newest = max(int(p.stat().st_mtime) for p in metas)
    except OSError:
        # A sidecar vanished mid-scan; treat as unstable so the next
        # check re-reads (an always-mismatching fingerprint is safe).
        return f"{len(metas)}:unstable"
    return f"{len(metas)}:{newest}"


def _cache_row_is_fresh(
    row: sqlite3.Row,
    source_mtime: float,
    current_fp: Callable[[], str],
    source_size: Optional[int] = None,
) -> bool:
    """Decide whether a cached_files row is still fresh.

    Single source of truth for the freshness rule shared by
    is_file_cached() and get_modified_files(). current_fp is a callable
    so callers only pay for the sidecar fingerprint scan when the mtime
    check passes (and get_modified_files() can plug in its
    scandir-optimized variant).

    Cache is valid if the size matches, modification times match
    (within 1 second tolerance), and the sidecar inputs of spawn
    discovery (#213) match too — new agent-*.meta.json files appear
    without touching the source jsonl. Pre-007 rows carry a NULL
    fingerprint: accept those only when the file has no sidecars today
    (nothing to miss), so legacy caches don't mass-invalidate while
    sessions WITH sidecars reparse once.

    The size check exists because the mtime tolerance — which is there
    for coarse filesystem timestamp granularity — hides any write that
    lands within a second of the mtime recorded at cache time. Appending
    to a transcript and converting immediately alternates between seeing
    and missing the change; the last message of a turn can stay stranded
    indefinitely. Size is exact and free (callers already stat the
    file), and the combined rule is strictly tightening: it marks more
    files stale, never fewer. Pre-011 rows carry NULL and fall back to
    the mtime-only check so a populated cache doesn't mass-invalidate.
    """
    if source_size is not None:
        cached_size = row["source_size"]
        if cached_size is not None and cached_size != source_size:
            return False
    if abs(source_mtime - row["source_mtime"]) >= 1.0:
        return False
    cached_fp = row["subagents_fingerprint"]
    if cached_fp is None:
        return current_fp() == ""
    return cached_fp == current_fp()


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


# Process-level memo of cache DBs whose migrations have already been
# checked. CacheManager is constructed per project (the TUI project
# selector builds one per table row, on every resize), so without the
# memo every instance re-pays the migration check: a connection plus
# version-table and migration-directory reads, ~2ms each (issue #12).
# The exists() guard in _init_database covers --clear-cache deleting
# the DB file mid-process.
_migrated_db_paths: set[str] = set()


class CacheManager:
    """SQLite-based cache manager for Claude Code Log."""

    def __init__(
        self,
        project_path: Path,
        library_version: str,
        db_path: Optional[Path] = None,
        *,
        read_only: bool = False,
    ):
        """Initialise cache manager for a project.

        Args:
            project_path: Path to the project directory containing JSONL files
            library_version: Current version of the library for cache invalidation
            db_path: Optional explicit path to the cache database. If not provided,
                uses CLAUDE_CODE_LOG_CACHE_PATH env var or default location.
            read_only: Open the cache strictly for reading. Skips the
                migration run and the project-row upsert, and opens every
                connection in SQLite's ``mode=ro``, so this instance can
                never write the shared database. Built for spawned render
                workers, which would otherwise all race those writes. The
                project id is looked up rather than created, so a missing
                database or row degrades to "no cached data" instead of
                erroring.
        """
        self.project_path = project_path
        self.library_version = library_version
        self._read_only = read_only

        # Priority: explicit db_path > env var > default location
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = get_cache_db_path(project_path.parent)

        # When inside a batch() scope this holds the single shared connection
        # reused by every _get_connection() call; None means the default
        # open-a-connection-per-call behaviour. Set before _init_database() so
        # the field always exists before any _get_connection() could run, even
        # if migrations are ever routed through it.
        self._shared_conn: Optional[sqlite3.Connection] = None

        self._project_id: Optional[int] = None
        if read_only:
            # Migrations and the project-row upsert both write; a reader
            # takes the schema and row exactly as the parent process left
            # them, and only looks its project up.
            self._lookup_project_id()
        else:
            # Initialise database and ensure project exists
            self._init_database()
            self._ensure_project_exists()

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Apply the standard pragmas/row factory to a fresh connection."""
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self._read_only:
            # A mode=ro connection cannot switch journal modes, and a
            # reader needs neither pragma: the writing parent already
            # keeps the database in WAL.
            return
        conn.execute("PRAGMA journal_mode = WAL")
        # synchronous=NORMAL is the recommended pairing for WAL: it keeps
        # durability across application crashes (only a power/OS crash can lose
        # the last committed transaction) while skipping an fsync on every
        # commit. The cache is fully regenerable from the JSONL source, so that
        # residual risk is acceptable.
        conn.execute("PRAGMA synchronous = NORMAL")

    def _open_configured_connection(self) -> sqlite3.Connection:
        """Open a connection and apply pragmas, closing it if setup fails.

        If a PRAGMA in ``_configure_connection`` raises, the just-opened
        handle is closed before re-raising so it can't leak and lock the
        .db/.db-wal/.db-shm files — the exact failure mode the connection
        lifecycle elsewhere is careful to avoid (Windows WinError 32).
        """
        if self._read_only:
            # as_uri() percent-encodes the (absolute) path, so URI mode is
            # safe for paths with spaces or query-ish characters.
            conn = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0
            )
        else:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            self._configure_connection(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def _get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with proper settings.

        Inside a ``batch()`` scope this yields the shared connection without
        closing it (the batch owns its lifecycle). Otherwise it opens a fresh
        connection and closes it on exit — the default, Windows-safe behaviour
        (no lingering file handle on the .db/.db-wal/.db-shm files).
        """
        if self._shared_conn is not None:
            yield self._shared_conn
            return

        conn = self._open_configured_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def batch(self) -> Generator[None, None, None]:
        """Reuse a single connection for every operation within the scope.

        A full project build issues ~190 ``_get_connection`` calls; opening a
        fresh SQLite connection each time dominates cache-build cost. Wrapping
        the build in ``with cache_manager.batch():`` collapses those to one
        connection.

        Lifecycle guarantees (the risky part — see the integrity tests):
        - The shared connection is **always closed on scope exit**, including
          when the body raises (the ``finally``). This releases the file lock
          on the .db/.db-wal/.db-shm files *before* any caller tears down a
          TemporaryDirectory or runs ``clear_cache``/rmtree — critical on
          Windows, which refuses to delete open files (WinError 32).
        - Outside a batch, behaviour is unchanged (connection-per-call).
        - Nesting is a no-op: an inner ``batch()`` reuses the outer connection
          and does NOT close it, so the converter loop can't double-open or
          close a connection still in use by an enclosing scope.
        """
        if self._shared_conn is not None:
            # Already batching — reuse the existing shared connection and leave
            # its lifecycle to the outermost batch().
            yield
            return

        # Open+configure first; only publish to _shared_conn once setup has
        # succeeded, so a failed PRAGMA never leaves a half-initialised (and
        # now-closed) handle assigned for other calls to reuse.
        conn = self._open_configured_connection()
        self._shared_conn = conn
        try:
            yield
        finally:
            self._shared_conn = None
            conn.close()

    def _init_database(self) -> None:
        """Create schema if needed using migration runner."""
        key = str(self.db_path)
        if key in _migrated_db_paths and self.db_path.exists():
            return
        run_migrations(self.db_path)
        _migrated_db_paths.add(key)

    def _lookup_project_id(self) -> None:
        """Read-only counterpart of ``_ensure_project_exists``: find, never create.

        No version-compatibility handling either — invalidating on a
        mismatch is a write, and the (writing) parent that spawned this
        reader already resolved any mismatch before handing over. Leaves
        ``_project_id`` as None — reads return "no cached data" — when the
        database, schema, or row is absent, rather than raising out of a
        worker's initialiser.
        """
        if not self.db_path.exists():
            return
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT id FROM projects WHERE project_path = ?",
                    (str(self.project_path),),
                ).fetchone()
        except sqlite3.Error:
            return
        if row:
            self._project_id = row["id"]

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
                "SELECT source_mtime, source_size, subagents_fingerprint"
                " FROM cached_files"
                " WHERE project_id = ? AND file_name = ?",
                (self._project_id, jsonl_path.name),
            ).fetchone()

        if not row:
            return False

        source_stat = jsonl_path.stat()
        return _cache_row_is_fresh(
            row,
            source_stat.st_mtime,
            lambda: subagents_fingerprint(jsonl_path),
            source_stat.st_size,
        )

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
        self,
        jsonl_path: Path,
        entries: List[TranscriptEntry],
        subagents_fp: Optional[str] = None,
    ) -> None:
        """Save parsed transcript entries to cache.

        ``subagents_fp`` is the sidecar fingerprint AS OF THE PARSE —
        callers that scanned sidecars should pass the value captured
        before the scan, so a sidecar landing mid-parse mismatches on the
        next read and forces a reparse (over-invalidation; computing it
        here at save time would instead validate a parse that never saw
        the late sidecar). Falls back to computing now when omitted.
        """
        if self._project_id is None:
            return

        source_stat = jsonl_path.stat()
        source_mtime = source_stat.st_mtime
        source_size = source_stat.st_size
        cached_mtime = datetime.now().timestamp()
        if subagents_fp is None:
            subagents_fp = subagents_fingerprint(jsonl_path)

        with self._get_connection() as conn:
            # Insert or update file record
            # Use ON CONFLICT to preserve file ID and avoid cascade deletes on messages
            conn.execute(
                """
                INSERT INTO cached_files
                (project_id, file_name, file_path, source_mtime, source_size,
                 cached_mtime, message_count, subagents_fingerprint)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, file_name) DO UPDATE SET
                    file_path = excluded.file_path,
                    source_mtime = excluded.source_mtime,
                    source_size = excluded.source_size,
                    cached_mtime = excluded.cached_mtime,
                    message_count = excluded.message_count,
                    subagents_fingerprint = excluded.subagents_fingerprint
                """,
                (
                    self._project_id,
                    jsonl_path.name,
                    str(jsonl_path),
                    source_mtime,
                    source_size,
                    cached_mtime,
                    len(entries),
                    subagents_fp,
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

            # Keep the full-archive search index in step with the rows we
            # just replaced, inside the same transaction. This is a no-op
            # (one sqlite_master lookup) unless someone has run
            # `claude-code-log serve` and built an index, so users who never
            # search pay essentially nothing — and those who do never meet a
            # stale index, because the file's old rows go out with its old
            # messages rather than waiting for the next server start.
            from .search import auto_index_enabled, reindex_files

            if auto_index_enabled():
                reindex_files(conn, [file_id], commit=False)

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
                        team_name, ai_title, hidden, residual_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        team_name = excluded.team_name,
                        ai_title = excluded.ai_title,
                        hidden = excluded.hidden,
                        residual_count = excluded.residual_count
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
                        scrub_surrogates(data.ai_title),
                        1 if data.hidden else 0,
                        data.residual_count or 0,
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

    def save_session_sidecar(self, sidecar: SessionSidecar) -> None:
        """Persist the cross-session sidecar (migration 008), wholesale.

        Called at the end of every full directory load, inside the same
        ``batch()`` scope as the per-file cache writes, so the sidecar is
        exactly as fresh as ``cached_files``: whenever ``get_modified_files``
        reports nothing modified, a present sidecar describes the current
        source tree. Delete + insert in one transaction — a crash leaves
        either the old sidecar or the new one, never a mix, and the
        ``sidecar_state`` marker row is only written alongside the rows it
        vouches for.
        """
        if self._project_id is None or self._read_only:
            return

        with self._get_connection() as conn:
            pid = self._project_id
            conn.execute("DELETE FROM session_parents WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM junction_uuids WHERE project_id = ?", (pid,))
            conn.execute("DELETE FROM dedup_winners WHERE project_id = ?", (pid,))
            conn.executemany(
                """INSERT INTO session_parents
                   (project_id, session_id, parent_session_id, attachment_uuid)
                   VALUES (?, ?, ?, ?)""",
                [
                    (pid, sid, parent, attachment)
                    for sid, (parent, attachment) in sidecar.parents.items()
                ],
            )
            conn.executemany(
                """INSERT INTO junction_uuids
                   (project_id, uuid, session_id, target_session_id, seq)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (pid, uuid, session_id, target, seq)
                    for uuid, (session_id, targets) in sidecar.junctions.items()
                    for seq, target in enumerate(targets)
                ],
            )
            conn.executemany(
                """INSERT INTO dedup_winners (project_id, uuid, winner_session_id)
                   VALUES (?, ?, ?)""",
                [(pid, uuid, winner) for uuid, winner in sidecar.dedup_winners.items()],
            )
            conn.execute(
                """INSERT INTO sidecar_state (project_id, populated_at)
                   VALUES (?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                       populated_at = excluded.populated_at""",
                (pid, datetime.now().isoformat()),
            )
            conn.commit()

    def load_session_sidecar(self) -> Optional[SessionSidecar]:
        """Load the cross-session sidecar, or None when never populated.

        ``None`` (no ``sidecar_state`` row — e.g. a cache built before
        migration 008, or a project never fully loaded since) tells the
        caller to decline the session-scoped path and fall back to a full
        load, which repopulates the sidecar. An *empty* sidecar with a
        state row is a valid answer: most projects have no cross-session
        coupling at all.
        """
        if self._project_id is None:
            return None

        with self._get_connection() as conn:
            pid = self._project_id
            state = conn.execute(
                "SELECT 1 FROM sidecar_state WHERE project_id = ?", (pid,)
            ).fetchone()
            if state is None:
                return None

            parents: dict[str, tuple[Optional[str], Optional[str]]] = {}
            for row in conn.execute(
                """SELECT session_id, parent_session_id, attachment_uuid
                   FROM session_parents WHERE project_id = ?""",
                (pid,),
            ):
                parents[row["session_id"]] = (
                    row["parent_session_id"],
                    row["attachment_uuid"],
                )

            junctions: dict[str, tuple[str, list[str]]] = {}
            for row in conn.execute(
                """SELECT uuid, session_id, target_session_id
                   FROM junction_uuids WHERE project_id = ?
                   ORDER BY uuid, seq""",
                (pid,),
            ):
                entry = junctions.setdefault(row["uuid"], (row["session_id"], []))
                entry[1].append(row["target_session_id"])

            dedup_winners: dict[str, str] = {}
            for row in conn.execute(
                "SELECT uuid, winner_session_id FROM dedup_winners WHERE project_id = ?",
                (pid,),
            ):
                dedup_winners[row["uuid"]] = row["winner_session_id"]

        return SessionSidecar(
            parents=parents, junctions=junctions, dedup_winners=dedup_winners
        )

    def get_session_file_map(self) -> Dict[str, set[str]]:
        """``{session_id: {file_name, ...}}`` from the cached messages table.

        Which source files hold each session's entries. Usually 1:1 with
        the ``<session-id>.jsonl`` naming, but not always — real archives
        contain files whose entries span two sessions (a continuation
        written into the previous session's file), and sessions with no
        file of their own. The session-scoped incremental path loads by
        this map instead of by filename stem for exactly that reason.

        Entries with a NULL session_id (summaries) are skipped; agent
        file rows are included and it is the caller's business to know
        that ``agent-*`` files load via trunk-file recursion.
        """
        if self._project_id is None:
            return {}

        result: Dict[str, set[str]] = {}
        with self._get_connection() as conn:
            for row in conn.execute(
                """SELECT DISTINCT m.session_id, cf.file_name
                   FROM messages m JOIN cached_files cf ON m.file_id = cf.id
                   WHERE m.project_id = ? AND m.session_id IS NOT NULL""",
                (self._project_id,),
            ):
                result.setdefault(row["session_id"], set()).add(row["file_name"])
        return result

    # ----- Incremental cache refresh queries (stage 4) -----------------
    # All of these are metadata projections over the messages table —
    # the point is answering coupling/identity questions *without*
    # materializing entries. See work/render-format-once.md, stage-4
    # design, for who consumes what.

    _IN_CHUNK = 500

    _RESIDUAL_TYPES = ("summary", "ai-title", "attachment")

    def get_cached_file_names(self) -> set[str]:
        """Every file name the cache has rows for (trunk + agent files)."""
        if self._project_id is None:
            return set()
        with self._get_connection() as conn:
            return {
                row["file_name"]
                for row in conn.execute(
                    "SELECT file_name FROM cached_files WHERE project_id = ?",
                    (self._project_id,),
                )
            }

    def get_file_states(self, file_names: List[str]) -> Dict[str, CachedFileState]:
        """Identity-relevant cached state per file (see CachedFileState).

        Files without cached rows simply have no key in the result —
        that is the "new file" signature.
        """
        result: Dict[str, CachedFileState] = {}
        if self._project_id is None or not file_names:
            return result
        with self._get_connection() as conn:
            for i in range(0, len(file_names), self._IN_CHUNK):
                chunk = file_names[i : i + self._IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"""SELECT cf.file_name, m.session_id, m._uuid, m._parent_uuid,
                               m._request_id, m.type, m._leaf_uuid, m.content
                        FROM messages m JOIN cached_files cf ON m.file_id = cf.id
                        WHERE m.project_id = ? AND cf.file_name IN ({placeholders})
                        ORDER BY cf.file_name, m.id""",
                    (self._project_id, *chunk),
                ):
                    state = result.setdefault(row["file_name"], CachedFileState())
                    state.row_fingerprints.append(
                        hashlib.sha256(bytes(row["content"])).digest()
                    )
                    row_type = row["type"]
                    if row_type == "summary":
                        if row["_leaf_uuid"]:
                            state.summary_leaf_uuids.add(row["_leaf_uuid"])
                    elif row_type == "ai-title":
                        if row["session_id"]:
                            state.ai_title_sessions.add(row["session_id"])
                    if row["session_id"]:
                        state.sessions.add(row["session_id"])
                    if row["_uuid"]:
                        state.uuids.add(row["_uuid"])
                    if row["_parent_uuid"]:
                        state.parent_uuids.add(row["_parent_uuid"])
                    if row["_request_id"]:
                        state.request_ids.add(row["_request_id"])
                    if row_type in self._RESIDUAL_TYPES:
                        state.type_counts[row_type] = (
                            state.type_counts.get(row_type, 0) + 1
                        )
                # A cached file whose rows are all filtered (e.g. empty
                # file) still needs a key, or it would read as "new".
                for row in conn.execute(
                    f"""SELECT file_name FROM cached_files
                        WHERE project_id = ? AND file_name IN ({placeholders})""",
                    (self._project_id, *chunk),
                ):
                    result.setdefault(row["file_name"], CachedFileState())
        return result

    def get_parent_uuid_dependents(self, uuids: List[str]) -> Dict[str, set[str]]:
        """Return sessions whose entries reference any named parent UUID.

        This is the inverse edge of :meth:`get_uuid_owners`. A newly added
        UUID can resolve an orphan already cached in an untouched file, so
        the incremental DAG closure needs both directions.
        """
        result: Dict[str, set[str]] = {}
        if self._project_id is None or not uuids:
            return result
        with self._get_connection() as conn:
            for i in range(0, len(uuids), self._IN_CHUNK):
                chunk = uuids[i : i + self._IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"""SELECT _parent_uuid, session_id FROM messages
                        WHERE project_id = ? AND _parent_uuid IN ({placeholders})
                          AND session_id IS NOT NULL""",
                    (self._project_id, *chunk),
                ):
                    result.setdefault(row["_parent_uuid"], set()).add(row["session_id"])
        return result

    def get_uuid_owners(self, uuids: List[str]) -> Dict[str, set[Tuple[str, str]]]:
        """``{uuid: {(session_id, entry_type), ...}}`` across the project.

        The coupling-closure query: which sessions hold copies of these
        uuids (dedup partners / attachment-point owners), and of what
        entry type (the attachment-dedup decline guard).
        """
        result: Dict[str, set[Tuple[str, str]]] = {}
        if self._project_id is None or not uuids:
            return result
        with self._get_connection() as conn:
            for i in range(0, len(uuids), self._IN_CHUNK):
                chunk = uuids[i : i + self._IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"""SELECT _uuid, session_id, type FROM messages
                        WHERE project_id = ? AND _uuid IN ({placeholders})
                          AND session_id IS NOT NULL""",
                    (self._project_id, *chunk),
                ):
                    result.setdefault(row["_uuid"], set()).add(
                        (row["session_id"], row["type"])
                    )
        return result

    def get_request_id_entries(
        self, rids: List[str]
    ) -> Dict[str, set[Tuple[str, str]]]:
        """``{request_id: {(uuid, session_id), ...}}`` across the project.

        The refresh's token-attribution guard needs the uuid alongside
        the session: a requestId spanning sessions via *the same* uuid
        is a dedup-resolved replay (only the winner's copy survives to
        be counted), while distinct uuids mean genuinely independent
        surviving copies whose D1 attribution depends on global order.
        """
        result: Dict[str, set[Tuple[str, str]]] = {}
        if self._project_id is None or not rids:
            return result
        with self._get_connection() as conn:
            for i in range(0, len(rids), self._IN_CHUNK):
                chunk = rids[i : i + self._IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"""SELECT _request_id, _uuid, session_id FROM messages
                        WHERE project_id = ? AND _request_id IN ({placeholders})
                          AND session_id IS NOT NULL""",
                    (self._project_id, *chunk),
                ):
                    result.setdefault(row["_request_id"], set()).add(
                        (row["_uuid"] or "", row["session_id"])
                    )
        return result

    def get_session_request_ids(self, session_ids: List[str]) -> set[str]:
        """Every requestId carried by entries of the named sessions."""
        result: set[str] = set()
        if self._project_id is None or not session_ids:
            return result
        with self._get_connection() as conn:
            for i in range(0, len(session_ids), self._IN_CHUNK):
                chunk = session_ids[i : i + self._IN_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                for row in conn.execute(
                    f"""SELECT DISTINCT _request_id FROM messages
                        WHERE project_id = ? AND session_id IN ({placeholders})
                          AND _request_id IS NOT NULL""",
                    (self._project_id, *chunk),
                ):
                    result.add(row["_request_id"])
        return result

    def get_metadata_target_files(self) -> List[Tuple[str, Optional[str], str]]:
        """Summary / ai-title rows as ``(type, target, file_name)``.

        ``target`` is the summary's leafUuid (resolve to a session via
        ``get_uuid_owners``) or the ai-title's session_id. The refresh
        uses this to pull files holding cross-file metadata for closure
        sessions, so a partial ``prepare_session_summaries`` sees the
        same rows the full load would.
        """
        result: List[Tuple[str, Optional[str], str]] = []
        if self._project_id is None:
            return result
        with self._get_connection() as conn:
            for row in conn.execute(
                """SELECT m.type, m._leaf_uuid, m.session_id, cf.file_name
                   FROM messages m JOIN cached_files cf ON m.file_id = cf.id
                   WHERE m.project_id = ? AND m.type IN ('summary', 'ai-title')""",
                (self._project_id,),
            ):
                target = (
                    row["_leaf_uuid"] if row["type"] == "summary" else row["session_id"]
                )
                result.append((row["type"], target, row["file_name"]))
        return result

    def get_all_session_rows(self) -> Dict[str, SessionCacheData]:
        """Every persisted session row, hidden ones included.

        The delta basis for the incremental refresh's aggregate
        arithmetic — unlike ``get_cached_project_data``, which serves
        render-facing consumers and filters ``hidden = 0``.
        """
        result: Dict[str, SessionCacheData] = {}
        if self._project_id is None:
            return result
        with self._get_connection() as conn:
            for row in conn.execute(
                "SELECT * FROM sessions WHERE project_id = ?",
                (self._project_id,),
            ):
                result[row["session_id"]] = SessionCacheData(
                    session_id=row["session_id"],
                    summary=row["summary"],
                    ai_title=row["ai_title"] if "ai_title" in row.keys() else None,
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
                    hidden=bool(row["hidden"]),
                    residual_count=row["residual_count"],
                )
        return result

    def merge_session_sidecar(
        self,
        parent_updates: Dict[str, Optional[Tuple[Optional[str], Optional[str]]]],
        junction_replacements: Dict[str, Tuple[str, List[str]]],
        winner_upserts: Dict[str, str],
    ) -> None:
        """Targeted sidecar maintenance for the incremental refresh.

        Unlike ``save_session_sidecar`` (wholesale delete + insert on a
        full load), this touches only the named facts: parent rows are
        replaced per session (None value = ensure absent), junction rows
        are replaced per uuid with the given ordered target list, winner
        rows are upserted per uuid. Everything else stands — the stage-4
        design's argument is that untouched facts remain valid under the
        append-only precondition. One transaction; ``sidecar_state`` is
        refreshed so the marker's timestamp reflects the merge.
        """
        if self._project_id is None or self._read_only:
            return
        with self._get_connection() as conn:
            pid = self._project_id
            for sid, value in parent_updates.items():
                conn.execute(
                    "DELETE FROM session_parents"
                    " WHERE project_id = ? AND session_id = ?",
                    (pid, sid),
                )
                if value is not None:
                    conn.execute(
                        """INSERT INTO session_parents
                           (project_id, session_id, parent_session_id,
                            attachment_uuid)
                           VALUES (?, ?, ?, ?)""",
                        (pid, sid, value[0], value[1]),
                    )
            for uuid, (owner_sid, targets) in junction_replacements.items():
                conn.execute(
                    "DELETE FROM junction_uuids WHERE project_id = ? AND uuid = ?",
                    (pid, uuid),
                )
                for seq, target in enumerate(targets):
                    conn.execute(
                        """INSERT INTO junction_uuids
                           (project_id, uuid, session_id, target_session_id, seq)
                           VALUES (?, ?, ?, ?, ?)""",
                        (pid, uuid, owner_sid, target, seq),
                    )
            for uuid, winner in winner_upserts.items():
                conn.execute(
                    """INSERT INTO dedup_winners (project_id, uuid, winner_session_id)
                       VALUES (?, ?, ?)
                       ON CONFLICT(project_id, uuid) DO UPDATE SET
                           winner_session_id = excluded.winner_session_id""",
                    (pid, uuid, winner),
                )
            conn.execute(
                """INSERT INTO sidecar_state (project_id, populated_at)
                   VALUES (?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                       populated_at = excluded.populated_at""",
                (pid, datetime.now().isoformat()),
            )
            conn.commit()

    def get_working_directories(self) -> List[str]:
        """Get list of working directories associated with this project.

        Queries distinct cwd values from sessions table.
        """
        if self._project_id is None:
            return []

        with self._get_connection() as conn:
            return self._get_working_directories(conn)

    def _get_working_directories(self, conn: sqlite3.Connection) -> List[str]:
        """get_working_directories() on an already-open connection."""
        rows = conn.execute(
            "SELECT DISTINCT cwd FROM sessions"
            " WHERE project_id = ? AND cwd IS NOT NULL AND hidden = 0",
            (self._project_id,),
        ).fetchall()
        return [row["cwd"] for row in rows]

    def get_modified_files(self, jsonl_files: List[Path]) -> List[Path]:
        """Get list of JSONL files that need to be reprocessed.

        Batched equivalent of calling is_file_cached() per file: one SQL
        query fetches every cached row (one connection open instead of
        one per file), and one scandir per parent directory rules out
        subagent-sidecar fingerprints, replacing the per-file query +
        directory probes that dominated freshness-check time on large
        archives (issue #12).
        """
        if not jsonl_files:
            return []
        if self._project_id is None:
            return list(jsonl_files)

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT file_name, source_mtime, source_size,"
                " subagents_fingerprint"
                " FROM cached_files WHERE project_id = ?",
                (self._project_id,),
            ).fetchall()
        cached = {row["file_name"]: row for row in rows}

        # A session file's sidecars live under <parent>/<stem>/subagents,
        # so one scandir of the parent tells us which files can have a
        # non-empty fingerprint without any per-file stat probes.
        subdir_names: Dict[Path, set[str]] = {}

        def current_fp(jsonl_file: Path) -> str:
            parent = jsonl_file.parent
            if parent.name == "subagents":
                # Agent file in a subagents dir: its sidecars sit next to
                # it, so the full fingerprint scan is required.
                return subagents_fingerprint(jsonl_file)
            names = subdir_names.get(parent)
            if names is None:
                try:
                    with os.scandir(parent) as entries:
                        names = {e.name for e in entries if e.is_dir()}
                except OSError:
                    names = set[str]()
                subdir_names[parent] = names
            return subagents_fingerprint(jsonl_file) if jsonl_file.stem in names else ""

        modified: List[Path] = []
        for jsonl_file in jsonl_files:
            row = cached.get(jsonl_file.name)
            if row is None:
                modified.append(jsonl_file)
                continue

            try:
                # One stat serves both checks — st_size rides along free.
                source_stat = jsonl_file.stat()
            except OSError:
                # Missing file: same outcome as is_file_cached()'s
                # exists() check returning False.
                modified.append(jsonl_file)
                continue

            if not _cache_row_is_fresh(
                row,
                source_stat.st_mtime,
                lambda file=jsonl_file: current_fp(file),
                source_stat.st_size,
            ):
                modified.append(jsonl_file)

        return modified

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
                cached_files[row["file_name"]] = CachedFileInfo(
                    file_path=row["file_path"],
                    source_mtime=row["source_mtime"],
                    cached_mtime=row["cached_mtime"],
                    message_count=row["message_count"],
                )

            # Get sessions
            session_rows = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND hidden = 0",
                (self._project_id,),
            ).fetchall()

            sessions: Dict[str, SessionCacheData] = {}
            for row in session_rows:
                sessions[row["session_id"]] = SessionCacheData(
                    session_id=row["session_id"],
                    summary=row["summary"],
                    ai_title=row["ai_title"] if "ai_title" in row.keys() else None,
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

            # On the same connection: calling the public method here
            # would open a second connection per call.
            working_directories = self._get_working_directories(conn)

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
            working_directories=working_directories,
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
            # 1.3.0 added handling for `ai-title` JSONL entries: existing
            # caches have a NULL `ai_title` column for every session until
            # JSONLs are re-ingested, so the project index keeps showing
            # the old session-id title until the cache is rebuilt.
            "1.2.0": "1.3.0",
            # 1.5.0 filters <local-command-caveat>-wrapped caveat messages
            # (Claude Code ≥ ~2.1) from session previews: caches built
            # earlier have the caveat text baked into first_user_message
            # for affected sessions.
            "1.4.0": "1.5.0",
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
                "SELECT COUNT(*) as cnt FROM sessions"
                " WHERE project_id = ? AND hidden = 0",
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

    def is_transcript_stale(
        self,
        html_path: str,
        session_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> tuple[bool, str]:
        """Check if a rendered output file needs regeneration.

        Despite the historical "html" naming, this works for any output
        format: cache rows are keyed by the variant-specific filename
        (e.g. ``session-abc123.low.compact.md``) and the version sniff
        reads the same ``<!-- Generated by claude-code-log v… -->``
        marker that both the HTML and Markdown renderers emit.

        Args:
            html_path: Output filename (e.g., "session-abc123.html")
            session_id: Session ID for individual session files, None for combined
            output_dir: Directory the rendered file lives in. Defaults to
                the source project directory (legacy in-place layout);
                ``--output`` runs must pass their destination or the file
                check reports ``file_missing`` forever.

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
        actual_file = (output_dir or self.project_path) / html_path
        if not actual_file.exists():
            return True, "file_missing"
        if is_html_outdated(actual_file):
            return True, "file_version_mismatch"

        with self._get_connection() as conn:
            if session_id is not None:
                # For individual session HTML: check if session message count changed
                row = conn.execute(
                    """SELECT message_count FROM sessions
                       WHERE project_id = ? AND session_id = ? AND hidden = 0""",
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
        self,
        valid_session_ids: Optional[set[str]] = None,
        variant: str = "",
        ext: str = "html",
        output_dir: Optional[Path] = None,
    ) -> List[tuple[str, str]]:
        """Get list of sessions whose rendered file needs regeneration.

        Args:
            valid_session_ids: If provided, only check sessions in this set.
                Sessions not in this set are considered "archived" (JSONL deleted)
                and are skipped to avoid perpetual staleness.
            variant: Filename variant infix (e.g. ``.low.compact``) and
            ext: extension — together they must reproduce the
                ``session-{id}{variant}.{ext}`` names the generation pass
                writes, or every session cache-misses ("not_cached") on
                every run and the project re-renders forever.
            output_dir: Where the rendered files live (``--output`` runs);
                defaults to the source project directory.

        Returns:
            List of (session_id, reason) tuples for sessions needing regeneration
        """
        if self._project_id is None:
            return []

        from .renderer import is_html_outdated

        stale_sessions: List[tuple[str, str]] = []
        base_dir = output_dir or self.project_path

        with self._get_connection() as conn:
            # Both tables are read whole, once, rather than per session.
            # `is_transcript_stale` issues two queries per call, so a
            # project with many sessions spent most of this function in
            # SQLite round-trips for rows it was going to read anyway
            # (work/watch-mode.md, C15). The per-session logic below is a
            # faithful inline of that method — same order of checks, same
            # reason strings — minus the queries.
            session_rows = conn.execute(
                """SELECT session_id, message_count FROM sessions
                   WHERE project_id = ? AND hidden = 0""",
                (self._project_id,),
            ).fetchall()
            html_rows = conn.execute(
                """SELECT html_path, message_count, library_version
                   FROM html_cache WHERE project_id = ?""",
                (self._project_id,),
            ).fetchall()
            html_cache = {
                r["html_path"]: (r["message_count"] or 0, r["library_version"])
                for r in html_rows
            }

            for row in session_rows:
                session_id = row["session_id"]

                # Skip archived sessions (JSONL deleted but cache remains)
                if (
                    valid_session_ids is not None
                    and session_id not in valid_session_ids
                ):
                    continue

                html_path = f"session-{session_id}{variant}.{ext}"
                cached = html_cache.get(html_path)
                if cached is None:
                    stale_sessions.append((session_id, "not_cached"))
                    continue
                cached_count, cached_version = cached
                if cached_version != self.library_version:
                    stale_sessions.append((session_id, "version_mismatch"))
                    continue
                actual_file = base_dir / html_path
                if not actual_file.exists():
                    stale_sessions.append((session_id, "file_missing"))
                    continue
                if is_html_outdated(actual_file):
                    stale_sessions.append((session_id, "file_version_mismatch"))
                    continue
                # `session_not_found` cannot arise here — the candidate
                # list *is* the sessions table — so only the count check
                # remains of `is_transcript_stale`'s session branch.
                if row["message_count"] != cached_count:
                    stale_sessions.append((session_id, "session_updated"))

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
                "SELECT session_id FROM sessions WHERE project_id = ? AND hidden = 0",
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
                "SELECT * FROM sessions WHERE project_id = ? AND hidden = 0",
                (self._project_id,),
            ).fetchall()

            for row in session_rows:
                session_id = row["session_id"]
                if session_id not in valid_session_ids:
                    archived_sessions[session_id] = SessionCacheData(
                        session_id=session_id,
                        summary=row["summary"],
                        ai_title=row["ai_title"] if "ai_title" in row.keys() else None,
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
            variant_suffix: Depth/compact variant infix (e.g. ``""``,
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
            variant_suffix: Depth/compact variant infix (e.g. ``""``,
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
        output_dir: Optional[Path] = None,
        expected_session_ids: Optional[List[str]] = None,
    ) -> tuple[bool, str]:
        """Check if a page needs regeneration.

        Args:
            page_number: The page number to check.
            page_size_config: The current page size configuration.
            variant_suffix: Depth/compact variant infix; each variant's
                cache is checked independently.
            output_dir: Directory the rendered page lives in. Defaults to
                the source project directory (legacy in-place layout);
                ``--output`` runs must pass their destination or the file
                check reports ``file_missing`` forever (mirrors
                ``is_transcript_stale``).
            expected_session_ids: The sessions this page should hold on
                *this* run, in render order. Every other check below reads
                the page's *cached* membership, so without this a page whose
                membership changed while its previously-held sessions stayed
                untouched reports ``up_to_date`` (see ``sessions_changed``).

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
        actual_file = (output_dir or self.project_path) / page_data.html_path
        if not actual_file.exists():
            return True, "file_missing"
        if is_html_outdated(actual_file):
            return True, "file_version_mismatch"

        # Check if the page's *membership* changed. The caller recomputes the
        # session→page assignment from scratch every run, so a page can gain,
        # lose, or reorder sessions while every session it previously held is
        # byte-for-byte untouched:
        #   - a brand-new session lands on the partially-filled last page;
        #   - a session imported with older timestamps sorts into the middle,
        #     shifting the contents of every page after it.
        # All the checks below (and above) read only the *cached* membership,
        # so they see nothing amiss and report `up_to_date` — the new sessions
        # then never get rendered into any page, and the combined output stays
        # frozen until the HTML is deleted by hand (issue #308).
        if (
            expected_session_ids is not None
            and list(expected_session_ids) != page_data.session_ids
        ):
            return True, "sessions_changed"

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

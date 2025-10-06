#!/usr/bin/env python3
"""Cache management for Claude Code Log to improve performance."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, cast
from datetime import datetime
from pydantic import BaseModel
from packaging import version

from .models import TranscriptEntry


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
    """Manages cache operations for a project directory."""

    def __init__(self, project_path: Path, library_version: str):
        """Initialize cache manager for a project.

        Args:
            project_path: Path to the project directory containing JSONL files
            library_version: Current version of the library for cache invalidation
        """
        self.project_path = project_path
        self.library_version = library_version
        self.cache_dir = project_path / "cache"
        self.index_file = self.cache_dir / "index.json"

        # Ensure cache directory exists
        self.cache_dir.mkdir(exist_ok=True)

        # Load existing cache index if available
        self._project_cache: Optional[ProjectCache] = None
        self._load_project_cache()

    def _load_project_cache(self) -> None:
        """Load the project cache index from disk."""
        if self.index_file.exists():
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                self._project_cache = ProjectCache.model_validate(cache_data)

                # Check if cache version is compatible with current library version
                if not self._is_cache_version_compatible(self._project_cache.version):
                    print(
                        f"Cache version incompatible: {self._project_cache.version} -> {self.library_version}, invalidating cache"
                    )
                    self.clear_cache()
                    self._project_cache = None
            except Exception as e:
                print(f"Warning: Failed to load cache index, will rebuild: {e}")
                self._project_cache = None

        # Initialize empty cache if none exists
        if self._project_cache is None:
            self._project_cache = ProjectCache(
                version=self.library_version,
                cache_created=datetime.now().isoformat(),
                last_updated=datetime.now().isoformat(),
                project_path=str(self.project_path),
                cached_files={},
                sessions={},
            )

    def _save_project_cache(self) -> None:
        """Save the project cache index to disk."""
        if self._project_cache is None:
            return

        self._project_cache.last_updated = datetime.now().isoformat()

        with open(self.index_file, "w", encoding="utf-8") as f:
            json.dump(self._project_cache.model_dump(), f, indent=2)

    def _get_cache_file_path(self, jsonl_path: Path) -> Path:
        """Get the cache file path for a given JSONL file."""
        return self.cache_dir / f"{jsonl_path.stem}.json"

    def is_file_cached(self, jsonl_path: Path) -> bool:
        """Check if a JSONL file has a valid cache entry."""
        if self._project_cache is None:
            return False

        file_key = jsonl_path.name
        if file_key not in self._project_cache.cached_files:
            return False

        # Check if source file exists and modification time matches
        if not jsonl_path.exists():
            return False

        cached_info = self._project_cache.cached_files[file_key]
        source_mtime = jsonl_path.stat().st_mtime

        # Cache is valid if modification times match and cache file exists
        cache_file = self._get_cache_file_path(jsonl_path)
        return (
            abs(source_mtime - cached_info.source_mtime) < 1.0 and cache_file.exists()
        )

    def load_cached_entries(self, jsonl_path: Path) -> Optional[List[TranscriptEntry]]:
        """Load cached transcript entries for a JSONL file."""
        if not self.is_file_cached(jsonl_path):
            return None

        cache_file = self._get_cache_file_path(jsonl_path)
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)

            # Expect timestamp-keyed format - flatten all entries
            entries_data: List[Dict[str, Any]] = []
            for timestamp_entries in cache_data.values():
                if isinstance(timestamp_entries, list):
                    # Type cast to ensure Pyright knows this is List[Dict[str, Any]]
                    entries_data.extend(cast(List[Dict[str, Any]], timestamp_entries))

            # Deserialize back to TranscriptEntry objects
            from .models import parse_transcript_entry

            entries = [
                parse_transcript_entry(entry_dict) for entry_dict in entries_data
            ]
            return entries
        except Exception as e:
            print(f"Warning: Failed to load cached entries from {cache_file}: {e}")
            return None

    def load_cached_entries_filtered(
        self, jsonl_path: Path, from_date: Optional[str], to_date: Optional[str]
    ) -> Optional[List[TranscriptEntry]]:
        """Load cached entries with efficient timestamp-based filtering."""
        if not self.is_file_cached(jsonl_path):
            return None

        cache_file = self._get_cache_file_path(jsonl_path)
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)

            # If no date filtering needed, fall back to regular loading
            if not from_date and not to_date:
                return self.load_cached_entries(jsonl_path)

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
                        # For simple date strings like "2023-01-01", set to end of day
                        to_dt = to_dt.replace(
                            hour=23, minute=59, second=59, microsecond=999999
                        )

            # Filter entries by timestamp
            filtered_entries_data: List[Dict[str, Any]] = []

            for timestamp_key, timestamp_entries in cache_data.items():
                if timestamp_key == "_no_timestamp":
                    # Always include entries without timestamps (like summaries)
                    if isinstance(timestamp_entries, list):
                        # Type cast to ensure Pyright knows this is List[Dict[str, Any]]
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
                        # Type cast to ensure Pyright knows this is List[Dict[str, Any]]
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
                f"Warning: Failed to load filtered cached entries from {cache_file}: {e}"
            )
            return None

    def save_cached_entries(
        self, jsonl_path: Path, entries: List[TranscriptEntry]
    ) -> None:
        """Save parsed transcript entries to cache with timestamp-based structure."""
        cache_file = self._get_cache_file_path(jsonl_path)

        try:
            # Create timestamp-keyed cache structure for efficient date filtering
            cache_data: Dict[str, Any] = {}

            for entry in entries:
                # Get timestamp - use empty string as fallback for entries without timestamps
                timestamp = (
                    getattr(entry, "timestamp", "")
                    if hasattr(entry, "timestamp")
                    else ""
                )
                if not timestamp:
                    # Use a special key for entries without timestamps (like summaries)
                    timestamp = "_no_timestamp"

                # Store entry data under timestamp
                if timestamp not in cache_data:
                    cache_data[timestamp] = []

                cache_data[timestamp].append(entry.model_dump())

            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, indent=2)

            # Update cache index
            if self._project_cache is not None:
                source_mtime = jsonl_path.stat().st_mtime
                cached_mtime = cache_file.stat().st_mtime

                # Extract session IDs from entries
                session_ids: List[str] = []
                for entry in entries:
                    if hasattr(entry, "sessionId"):
                        session_id = getattr(entry, "sessionId", "")
                        if session_id:
                            session_ids.append(session_id)
                session_ids = list(set(session_ids))  # Remove duplicates

                self._project_cache.cached_files[jsonl_path.name] = CachedFileInfo(
                    file_path=str(jsonl_path),
                    source_mtime=source_mtime,
                    cached_mtime=cached_mtime,
                    message_count=len(entries),
                    session_ids=session_ids,
                )

                self._save_project_cache()
        except Exception as e:
            print(f"Warning: Failed to save cached entries to {cache_file}: {e}")

    def update_session_cache(self, session_data: Dict[str, SessionCacheData]) -> None:
        """Update cached session information."""
        if self._project_cache is None:
            return

        self._project_cache.sessions.update(
            {session_id: data for session_id, data in session_data.items()}
        )
        self._save_project_cache()

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
        if self._project_cache is None:
            return

        self._project_cache.total_message_count = total_message_count
        self._project_cache.total_input_tokens = total_input_tokens
        self._project_cache.total_output_tokens = total_output_tokens
        self._project_cache.total_cache_creation_tokens = total_cache_creation_tokens
        self._project_cache.total_cache_read_tokens = total_cache_read_tokens
        self._project_cache.earliest_timestamp = earliest_timestamp
        self._project_cache.latest_timestamp = latest_timestamp

        self._save_project_cache()

    def update_working_directories(self, working_directories: List[str]) -> None:
        """Update the list of working directories associated with this project."""
        if self._project_cache is None:
            return

        self._project_cache.working_directories = working_directories
        self._save_project_cache()

    def get_modified_files(self, jsonl_files: List[Path]) -> List[Path]:
        """Get list of JSONL files that need to be reprocessed."""
        modified_files: List[Path] = []

        for jsonl_file in jsonl_files:
            if not self.is_file_cached(jsonl_file):
                modified_files.append(jsonl_file)

        return modified_files

    def get_cached_project_data(self) -> Optional[ProjectCache]:
        """Get the cached project data if available."""
        return self._project_cache

    def clear_cache(self) -> None:
        """Clear all cache files and reset the project cache."""
        if self.cache_dir.exists():
            for cache_file in self.cache_dir.glob("*.json"):
                if cache_file.name != "index.json":  # Don't delete the index file here
                    try:
                        cache_file.unlink()
                    except Exception as e:
                        print(f"Warning: Failed to delete cache file {cache_file}: {e}")

        if self.index_file.exists():
            try:
                self.index_file.unlink()
            except Exception as e:
                print(f"Warning: Failed to delete cache index {self.index_file}: {e}")

        # Reset the project cache
        self._project_cache = ProjectCache(
            version=self.library_version,
            cache_created=datetime.now().isoformat(),
            last_updated=datetime.now().isoformat(),
            project_path=str(self.project_path),
            cached_files={},
            sessions={},
        )

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
        if self._project_cache is None:
            return {"cache_enabled": False}

        return {
            "cache_enabled": True,
            "cached_files_count": len(self._project_cache.cached_files),
            "total_cached_messages": self._project_cache.total_message_count,
            "total_sessions": len(self._project_cache.sessions),
            "cache_created": self._project_cache.cache_created,
            "last_updated": self._project_cache.last_updated,
        }


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

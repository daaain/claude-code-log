#!/usr/bin/env python3
"""
Public library API for claude-code-log.

This module exposes the core parsing, loading, and caching functionality
for external consumers (like claude-history-mcp) without pulling in
CLI, TUI, or rendering dependencies.
"""

from pathlib import Path
from typing import Optional

from .cache import CacheManager, SessionCacheData
from .converter import (
    ensure_fresh_cache,
    load_directory_transcripts,
    load_transcript,
)
from .factories import create_transcript_entry
from .models import TranscriptEntry
from .parser import extract_text_content, parse_timestamp


__all__ = [
    # Core loading
    "load_transcript",
    "load_directory_transcripts",
    # Cache management
    "CacheManager",
    "SessionCacheData",
    "ensure_fresh_cache",
    # Parsing utilities
    "create_transcript_entry",
    "extract_text_content",
    "parse_timestamp",
    # Types
    "TranscriptEntry",
]


def discover_projects(projects_dir: Path) -> list[Path]:
    """Discover all project directories containing JSONL files.

    Args:
        projects_dir: Path to ~/.claude/projects/

    Returns:
        List of project directory paths (each containing *.jsonl files)
    """
    projects = []
    if not projects_dir.exists():
        return projects

    for entry in projects_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith("."):
            jsonl_files = list(entry.glob("*.jsonl"))
            if jsonl_files:
                projects.append(entry)
    return projects


def find_history_file() -> Optional[Path]:
    """Find the global history.jsonl file.

    Returns:
        Path to ~/.claude/history.jsonl if it exists, None otherwise.
    """
    history = Path.home() / ".claude" / "history.jsonl"
    return history if history.exists() else None


def load_history_file(file_path: Path, cache: CacheManager) -> int:
    """Parse history.jsonl and store commands in cache.

    Idempotent: relies on UNIQUE constraint + INSERT OR IGNORE in cache layer.

    Args:
        file_path: Path to history.jsonl
        cache: CacheManager instance

    Returns:
        Number of new rows inserted.
    """
    import json
    from .cache import scrub_surrogates

    commands = []
    with file_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            commands.append(
                {
                    "display": scrub_surrogates(data.get("display", "")) or "",
                    "project": data.get("project", ""),
                    "sessionId": data.get("sessionId", ""),
                    "timestamp": data.get("timestamp", 0),
                }
            )
    if not commands:
        return 0
    return cache.insert_history_commands(commands)

"""Migration runner for SQLite cache database."""

import hashlib
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Tuple


def _get_migrations_dir() -> Path:
    """Get the migrations directory path."""
    return Path(__file__).parent


def _compute_checksum(content: str) -> str:
    """Compute SHA256 checksum of migration content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _parse_migration_number(filename: str) -> int:
    """Extract migration number from filename (e.g., '001_initial.sql' -> 1)."""
    match = re.match(r"^(\d+)_", filename)
    if match:
        return int(match.group(1))
    raise ValueError(f"Invalid migration filename: {filename}")


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    """Create _schema_version table if it doesn't exist or upgrade from old format."""
    # Check if table exists
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='_schema_version'"
    ).fetchone()

    if row:
        # Check if it has the new schema (with checksum column)
        columns = conn.execute("PRAGMA table_info(_schema_version)").fetchall()
        column_names = {col[1] for col in columns}

        if "checksum" not in column_names:
            # Old format table - drop it and recreate
            # This triggers a fresh start as per migration plan
            conn.execute("DROP TABLE _schema_version")
            conn.commit()

    # Create table with new schema
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        )
    """)
    conn.commit()


def get_applied_migrations(conn: sqlite3.Connection) -> List[Tuple[int, str]]:
    """Get list of applied migrations as (version, checksum) tuples."""
    _ensure_schema_version_table(conn)
    rows = conn.execute(
        "SELECT version, checksum FROM _schema_version ORDER BY version"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def get_available_migrations() -> List[Tuple[int, Path]]:
    """Get list of available migration files as (version, path) tuples."""
    migrations_dir = _get_migrations_dir()
    sql_files = sorted(migrations_dir.glob("*.sql"))

    migrations: List[Tuple[int, Path]] = []
    seen: dict[int, str] = {}
    for sql_file in sql_files:
        try:
            version = _parse_migration_number(sql_file.name)
        except ValueError:
            # Skip files that don't match the naming convention
            continue
        # Two files sharing a number is not a style problem: `_schema_version`
        # keys on the number alone, so on any database that already applied
        # one of them the other is considered applied and its DDL never
        # runs -- silently, permanently, and only on the caches that came
        # through the first one. The pair cannot be seen from either branch
        # while they are developed in parallel; they become visible in the
        # same tree the moment both merge, which is where this fires.
        if version in seen:
            raise ValueError(
                f"Duplicate migration number {version:03d}: "
                f"{seen[version]} and {sql_file.name}. Two branches claimed "
                f"the same number; renumber the later one, since a database "
                f"that applied either will silently skip the other."
            )
        seen[version] = sql_file.name
        migrations.append((version, sql_file))

    return migrations


def get_pending_migrations(conn: sqlite3.Connection) -> List[Tuple[int, Path]]:
    """Get list of migrations that haven't been applied yet."""
    applied = {v for v, _ in get_applied_migrations(conn)}
    available = get_available_migrations()
    return [(v, p) for v, p in available if v not in applied]


def apply_migration(
    conn: sqlite3.Connection, version: int, migration_path: Path
) -> None:
    """Apply a single migration and record it in _schema_version."""
    content = migration_path.read_text(encoding="utf-8")
    checksum = _compute_checksum(content)

    # Execute the migration SQL
    conn.executescript(content)

    # Record the migration
    conn.execute(
        """
        INSERT INTO _schema_version (version, filename, applied_at, checksum)
        VALUES (?, ?, ?, ?)
        """,
        (version, migration_path.name, datetime.now().isoformat(), checksum),
    )
    conn.commit()


def verify_migrations(conn: sqlite3.Connection) -> List[str]:
    """Verify applied migrations match their checksums.

    Returns list of warnings for any mismatches.
    """
    warnings: List[str] = []
    applied = get_applied_migrations(conn)
    available = {v: p for v, p in get_available_migrations()}

    for version, stored_checksum in applied:
        if version in available:
            current_content = available[version].read_text(encoding="utf-8")
            current_checksum = _compute_checksum(current_content)
            if current_checksum != stored_checksum:
                warnings.append(
                    f"Migration {version} ({available[version].name}) has been modified "
                    f"since it was applied. This may indicate database inconsistency."
                )

    return warnings


def run_migrations(db_path: Path) -> int:
    """Apply all pending migrations to the database.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        Number of migrations applied
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        _ensure_schema_version_table(conn)
        pending = get_pending_migrations(conn)

        for version, migration_path in sorted(pending):
            apply_migration(conn, version, migration_path)

        return len(pending)
    finally:
        conn.close()


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current schema version (highest applied migration number)."""
    _ensure_schema_version_table(conn)
    row = conn.execute("SELECT MAX(version) FROM _schema_version").fetchone()
    return row[0] if row[0] is not None else 0

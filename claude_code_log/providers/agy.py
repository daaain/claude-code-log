"""Antigravity CLI (agy) session provider."""

import json
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from claude_code_log.models import (
    AssistantMessageModel,
    AssistantTranscriptEntry,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
    TranscriptEntry,
    UserMessageModel,
    UserTranscriptEntry,
)

from .base import BaseProvider, SessionInfo


class AgyProvider(BaseProvider):
    """Provider for Antigravity CLI (agy) sessions.

    Supports two storage formats:
    - SQLite .db files (newer CLI releases >= 1.0.4)
    - Encrypted .pb files (older releases, requires agy-reader for decryption)

    Storage location: ~/.gemini/antigravity-cli/conversations/
    """

    def get_provider_name(self) -> str:
        return "agy"

    def get_session_format(self) -> str:
        return "sqlite"

    def get_data_dir(self) -> Optional[Path]:
        """Return the agy-cli conversations directory."""
        data_dir = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all agy-cli sessions.

        Discovers both SQLite .db files (newer) and encrypted .pb files (older).
        """
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        # Discover SQLite .db files (newer format)
        for db_file in data_dir.glob("*.db"):
            session_id = db_file.stem
            yield SessionInfo(
                provider="agy",
                session_id=session_id,
                created_at=self._get_file_mtime(db_file),
            )

        # Discover encrypted .pb files (older format)
        for pb_file in data_dir.glob("*.pb"):
            session_id = pb_file.stem
            # Check if a .trajectory.json sidecar exists (decrypted by agy-reader)
            trajectory_file = pb_file.with_suffix(".trajectory.json")
            if trajectory_file.exists():
                yield SessionInfo(
                    provider="agy",
                    session_id=session_id,
                    created_at=self._get_file_mtime(pb_file),
                )

    def load_session(self, session_id: str) -> Iterator[TranscriptEntry]:
        """Load an agy-cli session.

        For SQLite .db files: Directly queries the database.
        For encrypted .pb files: Requires agy-reader to have generated
        a .trajectory.json sidecar.
        """
        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Antigravity CLI data directory not found")

        # Try SQLite .db first (newer format)
        db_file = data_dir / f"{session_id}.db"
        if db_file.exists():
            yield from self._load_sqlite_session(db_file, session_id)
            return

        # Fall back to .trajectory.json sidecar (agy-reader decrypted)
        trajectory_file = data_dir / f"{session_id}.trajectory.json"
        if trajectory_file.exists():
            yield from self._load_trajectory_session(trajectory_file, session_id)
            return

        raise FileNotFoundError(f"Session {session_id} not found")

    def _load_sqlite_session(
        self, db_file: Path, session_id: str
    ) -> Iterator[TranscriptEntry]:
        """Load session from SQLite database."""
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()

            # Query the steps table (schema may vary by version)
            # This is a heuristic based on common agy-cli DB schemas
            try:
                cursor.execute(
                    "SELECT timestamp, role, content, tool_name, tool_input, tool_output "
                    "FROM steps ORDER BY timestamp"
                )
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                # Table structure may be different, try alternative schema
                try:
                    cursor.execute(
                        "SELECT created_at, role, content FROM messages ORDER BY created_at"
                    )
                    rows = cursor.fetchall()
                except sqlite3.OperationalError:
                    conn.close()
                    return

            for i, row in enumerate(rows):
                timestamp = str(row[0]) if row[0] else ""
                role = row[1] if len(row) > 1 else "user"
                content = row[2] if len(row) > 2 else ""

                if role == "user":
                    yield UserTranscriptEntry(
                        type="user",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{i}",
                        timestamp=timestamp,
                        message=UserMessageModel(
                            role="user",
                            content=[TextContent(type="text", text=str(content))],
                        ),
                    )
                elif role == "assistant":
                    yield AssistantTranscriptEntry(
                        type="assistant",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{i}",
                        timestamp=timestamp,
                        message=AssistantMessageModel(
                            id=f"{session_id}-{i}",
                            type="message",
                            role="assistant",
                            model="antigravity",
                            content=[TextContent(type="text", text=str(content))],
                        ),
                    )

            conn.close()

        except Exception as e:
            raise ValueError(f"Failed to load SQLite session: {e}")

    def _load_trajectory_session(
        self, trajectory_file: Path, session_id: str
    ) -> Iterator[TranscriptEntry]:
        """Load session from .trajectory.json sidecar (agy-reader output)."""
        try:
            with open(trajectory_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            steps = data.get("steps", [])
            for i, step in enumerate(steps):
                timestamp = step.get("metadata", {}).get("createdAt", "")
                role = step.get("role", "user")
                content = step.get("content", "")

                if role == "user":
                    yield UserTranscriptEntry(
                        type="user",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{i}",
                        timestamp=timestamp,
                        message=UserMessageModel(
                            role="user",
                            content=[TextContent(type="text", text=str(content))],
                        ),
                    )
                elif role == "assistant":
                    yield AssistantTranscriptEntry(
                        type="assistant",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{i}",
                        timestamp=timestamp,
                        message=AssistantMessageModel(
                            id=f"{session_id}-{i}",
                            type="message",
                            role="assistant",
                            model="antigravity",
                            content=[TextContent(type="text", text=str(content))],
                        ),
                    )

        except Exception as e:
            raise ValueError(f"Failed to load trajectory session: {e}")

    def _get_file_mtime(self, path: Path) -> str:
        """Get file modification time as ISO string."""
        from datetime import datetime

        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat()

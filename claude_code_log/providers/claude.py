"""Claude Code session provider."""

from pathlib import Path
from typing import Iterator, Optional

from claude_code_log.models import TranscriptEntry

from .base import BaseProvider, SessionInfo


class ClaudeProvider(BaseProvider):
    """Provider for Claude Code sessions.

    Wraps existing claude-code-log functionality to implement the provider interface.
    """

    def get_provider_name(self) -> str:
        return "claude"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        """Return the Claude projects directory."""
        data_dir = Path.home() / ".claude" / "projects"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all Claude Code sessions."""
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        # Find all project directories
        for project_dir in data_dir.iterdir():
            if not project_dir.is_dir():
                continue

            # Find all JSONL files in the project
            for jsonl_file in project_dir.glob("*.jsonl"):
                if jsonl_file.name.startswith("agent-"):
                    continue  # Skip agent files

                session_id = jsonl_file.stem
                yield SessionInfo(
                    provider="claude",
                    session_id=session_id,
                    project_path=project_dir,
                    created_at=self._get_file_mtime(jsonl_file),
                )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load a Claude Code session.

        This uses the existing load_transcript function from converter.py.

        Args:
            session_id: Session ID to load
            max_messages: Optional maximum number of messages to yield (for previews)
        """
        from claude_code_log.converter import load_transcript

        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Claude data directory not found")

        # Find the session file
        for project_dir in data_dir.iterdir():
            if not project_dir.is_dir():
                continue

            jsonl_file = project_dir / f"{session_id}.jsonl"
            if jsonl_file.exists():
                return iter(load_transcript(jsonl_file))

        raise FileNotFoundError(f"Session {session_id} not found")

    def _get_file_mtime(self, path: Path) -> str:
        """Get file modification time as ISO string."""
        from datetime import datetime

        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat()

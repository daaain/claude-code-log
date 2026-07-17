"""Claude Code session provider."""

from pathlib import Path
import re
from typing import Iterator, Optional

from claude_code_log.models import TranscriptEntry

from .base import BaseProvider, SessionInfo, file_mtime_iso


_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


class ClaudeProvider(BaseProvider):
    def get_provider_name(self) -> str:
        return "claude"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        data_dir = Path.home() / ".claude" / "projects"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        for project_dir in sorted(data_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            for jsonl_file in sorted(project_dir.glob("*.jsonl")):
                if jsonl_file.name.startswith("agent-"):
                    continue
                yield SessionInfo(
                    provider="claude",
                    session_id=jsonl_file.stem,
                    project_path=project_dir,
                    created_at=file_mtime_iso(jsonl_file),
                )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        from claude_code_log.converter import load_transcript

        if not session_id or _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError(f"Invalid session_id: {session_id}")
        if max_messages is not None and max_messages <= 0:
            return iter(())

        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Claude data directory not found")

        matches = sorted(
            project_dir / f"{session_id}.jsonl"
            for project_dir in data_dir.iterdir()
            if project_dir.is_dir() and (project_dir / f"{session_id}.jsonl").is_file()
        )
        if len(matches) > 1:
            raise ValueError(f"Multiple Claude sessions have id {session_id}")
        if matches:
            messages = load_transcript(matches[0])
            if max_messages is not None:
                messages = messages[:max_messages]
            return iter(messages)

        raise FileNotFoundError(f"Session {session_id} not found")

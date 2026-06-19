"""Abstract base class for session providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from claude_code_log.models import TranscriptEntry


@dataclass
class SessionInfo:
    """Metadata about a discovered session."""

    provider: str
    session_id: str
    title: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    project_path: Optional[Path] = None
    message_count: int = 0
    total_tokens: int = 0


class BaseProvider(ABC):
    """Abstract base class for session providers.

    Each provider must implement:
    - get_provider_name(): Return the provider identifier
    - get_session_format(): Return the session format (e.g., "jsonl", "json")
    - discover_sessions(): Find all sessions for this provider
    - load_session(): Load a session and return transcript entries
    """

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return the provider identifier (e.g., 'claude', 'codex', 'gemini', 'opencode')."""
        ...

    @abstractmethod
    def get_session_format(self) -> str:
        """Return the session format (e.g., 'jsonl', 'json', 'sqlite')."""
        ...

    @abstractmethod
    def get_data_dir(self) -> Optional[Path]:
        """Return the provider's data directory, or None if not found."""
        ...

    @abstractmethod
    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all available sessions for this provider.

        Yields SessionInfo objects with metadata about each session.
        """
        ...

    @abstractmethod
    def load_session(self, session_id: str) -> Iterator[TranscriptEntry]:
        """Load a session and return transcript entries.

        Args:
            session_id: The unique identifier for the session.

        Yields:
            TranscriptEntry objects in chronological order.
        """
        ...

    def is_available(self) -> bool:
        """Check if this provider is available (data directory exists)."""
        data_dir = self.get_data_dir()
        return data_dir is not None and data_dir.exists()

    def get_session_stats(self, session_id: str) -> dict:
        """Get statistics for a specific session.

        Default implementation returns empty dict. Providers can override
        to return provider-specific stats.
        """
        return {}

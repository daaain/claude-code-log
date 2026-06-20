"""Antigravity CLI (agy) session provider."""

import json
import re
from pathlib import Path
from typing import Iterator, Optional

from claude_code_log.models import (
    AssistantMessageModel,
    AssistantTranscriptEntry,
    TextContent,
    TranscriptEntry,
    UserMessageModel,
    UserTranscriptEntry,
)

from .base import BaseProvider, SessionInfo


class AgyProvider(BaseProvider):
    """Provider for Antigravity CLI (agy) sessions.

    Session storage layout:
        ~/.gemini/antigravity-cli/
            conversations/<uuid>.db     - SQLite protobuf (binary, not used directly)
            brain/<uuid>/.system_generated/logs/transcript.jsonl  - Human-readable transcript
            history.jsonl               - User input history with timestamps

    This provider reads transcript.jsonl for message content, falling back
    to history.jsonl for session discovery metadata.
    """

    def get_provider_name(self) -> str:
        return "agy"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        """Return the agy-cli root directory."""
        data_dir = Path.home() / ".gemini" / "antigravity-cli"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all agy-cli sessions from brain/ transcript logs."""
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        brain_dir = data_dir / "brain"
        if not brain_dir.exists():
            return

        for session_dir in brain_dir.iterdir():
            if not session_dir.is_dir():
                continue

            transcript_file = (
                session_dir / ".system_generated" / "logs" / "transcript.jsonl"
            )
            if not transcript_file.exists():
                continue

            session_id = session_dir.name
            yield SessionInfo(
                provider="agy",
                session_id=session_id,
                created_at=self._get_file_mtime(transcript_file),
            )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load an agy-cli session from transcript.jsonl."""
        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Antigravity CLI data directory not found")

        transcript_file = (
            data_dir
            / "brain"
            / session_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )

        if not transcript_file.exists():
            raise FileNotFoundError(
                f"Transcript for session {session_id} not found at {transcript_file}"
            )

        with open(transcript_file, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue

                entry = json.loads(line)
                yield from self._parse_entry(entry, session_id, i)

                if max_messages is not None and i >= max_messages:
                    break

    def _parse_entry(
        self, entry: dict, session_id: str, index: int
    ) -> Iterator[TranscriptEntry]:
        """Parse a single transcript entry into TranscriptEntry objects."""
        entry_type = entry.get("type", "")
        timestamp = entry.get("created_at", "")
        content = entry.get("content", "")

        if entry_type == "USER_INPUT":
            # Extract the actual user message from <USER_REQUEST> tags
            text = self._extract_user_request(content)
            if text:
                yield UserTranscriptEntry(
                    type="user",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=f"agy-{session_id}-{index}",
                    timestamp=timestamp,
                    message=UserMessageModel(
                        role="user",
                        content=[TextContent(type="text", text=text)],
                    ),
                )

        elif entry_type in ("PLANNER_RESPONSE", "CHECKPOINT"):
            # Assistant responses
            text = content if isinstance(content, str) else json.dumps(content)
            if text:
                # For PLANNER_RESPONSE, check for tool calls first
                tool_calls = entry.get("tool_calls", [])
                if tool_calls:
                    yield from self._parse_tool_calls(
                        tool_calls, text, session_id, index, timestamp
                    )
                else:
                    yield AssistantTranscriptEntry(
                        type="assistant",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"agy-{session_id}-{index}",
                        timestamp=timestamp,
                        message=AssistantMessageModel(
                            id=f"agy-{session_id}-{index}",
                            type="message",
                            role="assistant",
                            model="antigravity",
                            content=[TextContent(type="text", text=text)],
                        ),
                    )

        elif entry_type == "LIST_DIRECTORY":
            # Tool result — emit as assistant message with tool context
            text = content if isinstance(content, str) else json.dumps(content)
            if text:
                yield AssistantTranscriptEntry(
                    type="assistant",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=f"agy-{session_id}-{index}",
                    timestamp=timestamp,
                    message=AssistantMessageModel(
                        id=f"agy-{session_id}-{index}",
                        type="message",
                        role="assistant",
                        model="antigravity",
                        content=[
                            TextContent(type="text", text=f"[tool: list_dir]\n{text}")
                        ],
                    ),
                )

        # CONVERSATION_HISTORY entries have no content — skip them

    def _parse_tool_calls(
        self,
        tool_calls: list,
        fallback_text: str,
        session_id: str,
        index: int,
        timestamp: str,
    ) -> Iterator[AssistantTranscriptEntry]:
        """Parse tool calls into assistant entries."""
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            args = tc.get("args", {})

            # Format tool call as readable text
            args_str = json.dumps(args, indent=2) if args else ""
            text = f"[tool: {name}]\n{args_str}" if args_str else f"[tool: {name}]"

            yield AssistantTranscriptEntry(
                type="assistant",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="",
                sessionId=session_id,
                version="",
                uuid=f"agy-{session_id}-{index}-{name}",
                timestamp=timestamp,
                message=AssistantMessageModel(
                    id=f"agy-{session_id}-{index}-{name}",
                    type="message",
                    role="assistant",
                    model="antigravity",
                    content=[TextContent(type="text", text=text)],
                ),
            )

        # If there was also a content response, emit it
        if fallback_text and not fallback_text.startswith("[tool:"):
            yield AssistantTranscriptEntry(
                type="assistant",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="",
                sessionId=session_id,
                version="",
                uuid=f"agy-{session_id}-{index}-response",
                timestamp=timestamp,
                message=AssistantMessageModel(
                    id=f"agy-{session_id}-{index}-response",
                    type="message",
                    role="assistant",
                    model="antigravity",
                    content=[TextContent(type="text", text=fallback_text)],
                ),
            )

    def _extract_user_request(self, content: str) -> str:
        """Extract user message from <USER_REQUEST> tags."""
        match = re.search(
            r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL
        )
        if match:
            return match.group(1).strip()
        # Fallback: return raw content if no tags found
        return content.strip() if content else ""

    def _get_file_mtime(self, path: Path) -> str:
        """Get file modification time as ISO string."""
        from datetime import datetime

        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat()

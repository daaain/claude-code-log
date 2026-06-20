"""Gemini CLI session provider."""

import json
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


class GeminiProvider(BaseProvider):
    """Provider for Gemini CLI sessions.

    Parses Gemini CLI JSONL files from ~/.gemini/tmp/<project_hash>/chats/.
    Format: https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/services/chatRecordingTypes.ts
    """

    def get_provider_name(self) -> str:
        return "gemini"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        """Return the Gemini CLI data directory."""
        data_dir = Path.home() / ".gemini" / "tmp"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all Gemini CLI sessions."""
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        for project_dir in data_dir.iterdir():
            if not project_dir.is_dir():
                continue

            chats_dir = project_dir / "chats"
            if not chats_dir.exists():
                continue

            for session_file in chats_dir.glob("session-*.jsonl"):
                session_id = session_file.stem
                yield SessionInfo(
                    provider="gemini",
                    session_id=session_id,
                    project_path=project_dir,
                    created_at=self._get_file_mtime(session_file),
                )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load a Gemini CLI session.

        Parses JSONL format with individual messages per line:
        - Session metadata (first line)
        - Individual messages with type: user|info|error|warning|gemini
        - $set operations (metadata updates)

        Args:
            session_id: Session ID to load
            max_messages: Optional maximum number of messages to yield (for previews)
        """
        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Gemini data directory not found")

        session_file = self._find_session_file(data_dir, session_id)
        if session_file is None:
            raise FileNotFoundError(f"Session {session_id} not found")

        yield from self._parse_session_file(session_file)

    def _find_session_file(self, data_dir: Path, session_id: str) -> Optional[Path]:
        """Find a session file by session ID."""
        for project_dir in data_dir.iterdir():
            if not project_dir.is_dir():
                continue

            chats_dir = project_dir / "chats"
            if not chats_dir.exists():
                continue

            session_file = chats_dir / f"{session_id}.jsonl"
            if session_file.exists():
                return session_file

        return None

    def _parse_session_file(
        self, session_file: Path, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Parse a Gemini CLI session JSONL file."""
        session_id = session_file.stem
        timestamp_counter = 0
        message_count = 0

        with open(session_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                if max_messages is not None and message_count >= max_messages:
                    break
                message_count += 1
                line = line.strip()
                if not line:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(entry, dict):
                    continue

                if "$rewindTo" in entry:
                    continue

                if "$set" in entry:
                    continue

                # Skip session metadata (first line with sessionId, projectHash, etc.)
                if (
                    "sessionId" in entry
                    and "projectHash" in entry
                    and "messages" not in entry
                ):
                    continue

                # Handle individual messages (the actual format)
                if "type" in entry and "timestamp" in entry:
                    yield from self._parse_individual_message(
                        entry, session_id, timestamp_counter
                    )
                    timestamp_counter += 1

    def _parse_individual_message(
        self,
        entry: dict,
        session_id: str,
        counter: int,
    ) -> Iterator[TranscriptEntry]:
        """Parse an individual message entry."""
        msg_type = entry.get("type")
        timestamp = entry.get("timestamp", "")
        content = entry.get("content", "")
        msg_id = entry.get("id", f"{session_id}-{counter}")

        if msg_type == "user":
            # Handle content that might be a list of parts
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        text_parts.append(part["text"])
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = "\n".join(text_parts)

            yield UserTranscriptEntry(
                type="user",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="",
                sessionId=session_id,
                version="",
                uuid=msg_id,
                timestamp=timestamp,
                message=UserMessageModel(
                    role="user",
                    content=[TextContent(type="text", text=str(content))],
                ),
            )

        elif msg_type == "gemini":
            tool_calls = entry.get("toolCalls", [])
            thoughts = entry.get("thoughts", [])
            model = entry.get("model", "gemini")

            if content:
                yield AssistantTranscriptEntry(
                    type="assistant",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=msg_id,
                    timestamp=timestamp,
                    message=AssistantMessageModel(
                        id=msg_id,
                        type="message",
                        role="assistant",
                        model=model,
                        content=[TextContent(type="text", text=str(content))],
                    ),
                )

            for thought in thoughts:
                if isinstance(thought, dict):
                    thought_text = thought.get("summary", "")
                    if thought_text:
                        yield AssistantTranscriptEntry(
                            type="assistant",
                            parentUuid=None,
                            isSidechain=False,
                            userType="external",
                            cwd="",
                            sessionId=session_id,
                            version="",
                            uuid=f"{msg_id}-thought-{counter}",
                            timestamp=thought.get("timestamp", timestamp),
                            message=AssistantMessageModel(
                                id=f"{msg_id}-thought-{counter}",
                                type="message",
                                role="assistant",
                                model=model,
                                content=[
                                    ThinkingContent(
                                        type="thinking",
                                        thinking=thought_text,
                                    )
                                ],
                            ),
                        )

            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue

                call_id = tool_call.get("id", f"{msg_id}-tool-{counter}")
                name = tool_call.get("name", "unknown")
                args = tool_call.get("args", {})
                result = tool_call.get("result")

                yield AssistantTranscriptEntry(
                    type="assistant",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=f"{msg_id}-tooluse-{counter}",
                    timestamp=tool_call.get("timestamp", timestamp),
                    message=AssistantMessageModel(
                        id=f"{msg_id}-tooluse-{counter}",
                        type="message",
                        role="assistant",
                        model=model,
                        content=[
                            ToolUseContent(
                                type="tool_use",
                                id=call_id,
                                name=name,
                                input=args,
                            )
                        ],
                    ),
                )

                if result is not None:
                    yield UserTranscriptEntry(
                        type="user",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{msg_id}-toolresult-{counter}",
                        timestamp=tool_call.get("timestamp", timestamp),
                        message=UserMessageModel(
                            role="user",
                            content=[
                                ToolResultContent(
                                    type="tool_result",
                                    tool_use_id=call_id,
                                    content=str(result),
                                )
                            ],
                        ),
                    )

        elif msg_type in ("info", "error", "warning"):
            yield AssistantTranscriptEntry(
                type="assistant",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="",
                sessionId=session_id,
                version="",
                uuid=msg_id,
                timestamp=timestamp,
                message=AssistantMessageModel(
                    id=msg_id,
                    type="message",
                    role="assistant",
                    model="gemini",
                    content=[TextContent(type="text", text=str(content))],
                ),
            )

    def _get_file_mtime(self, path: Path) -> str:
        """Get file modification time as ISO string."""
        from datetime import datetime

        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat()

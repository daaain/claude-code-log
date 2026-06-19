"""Gemini CLI session provider."""

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from claude_code_log.models import (
    AssistantMessageModel,
    AssistantTranscriptEntry,
    TextContent,
    ThinkingContent,
    ToolResultContent,
    ToolUseContent,
    TranscriptEntry,
    UsageInfo,
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

    def load_session(self, session_id: str) -> Iterator[TranscriptEntry]:
        """Load a Gemini CLI session.

        Parses JSONL format:
        - ConversationRecord with messages array
        - Each message has type: user|info|error|warning|gemini
        - Gemini messages include toolCalls, thoughts, tokens
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

    def _parse_session_file(self, session_file: Path) -> Iterator[TranscriptEntry]:
        """Parse a Gemini CLI session JSONL file."""
        session_id = session_file.stem
        timestamp_counter = 0

        with open(session_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
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

                if "messages" in entry:
                    yield from self._parse_conversation_record(
                        entry, session_id, timestamp_counter
                    )
                    break

    def _parse_conversation_record(
        self,
        record: dict,
        session_id: str,
        counter: int,
    ) -> Iterator[TranscriptEntry]:
        """Parse a ConversationRecord."""
        messages = record.get("messages", [])

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type")
            timestamp = msg.get("timestamp", "")
            content = msg.get("content", "")

            if msg_type == "user":
                yield UserTranscriptEntry(
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=f"{session_id}-{counter}",
                    timestamp=timestamp,
                    message=UserMessageModel(
                        role="user",
                        content=[TextContent(type="text", text=str(content))],
                    ),
                )
                counter += 1

            elif msg_type == "gemini":
                tool_calls = msg.get("toolCalls", [])
                thoughts = msg.get("thoughts", [])
                tokens = msg.get("tokens")
                model = msg.get("model", "gemini")

                if content:
                    yield AssistantTranscriptEntry(
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{counter}",
                        timestamp=timestamp,
                        message=AssistantMessageModel(
                            id=f"{session_id}-{counter}",
                            type="message",
                            role="assistant",
                            model=model,
                            content=[TextContent(type="text", text=str(content))],
                        ),
                    )
                    counter += 1

                for thought in thoughts:
                    if isinstance(thought, dict):
                        thought_text = thought.get("summary", "")
                        if thought_text:
                            yield AssistantTranscriptEntry(
                                parentUuid=None,
                                isSidechain=False,
                                userType="external",
                                cwd="",
                                sessionId=session_id,
                                version="",
                                uuid=f"{session_id}-{counter}",
                                timestamp=thought.get("timestamp", timestamp),
                                message=AssistantMessageModel(
                                    id=f"{session_id}-{counter}",
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
                            counter += 1

                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue

                    call_id = tool_call.get("id", f"{session_id}-{counter}")
                    name = tool_call.get("name", "unknown")
                    args = tool_call.get("args", {})
                    result = tool_call.get("result")
                    status = tool_call.get("status")

                    yield AssistantTranscriptEntry(
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{counter}",
                        timestamp=tool_call.get("timestamp", timestamp),
                        message=AssistantMessageModel(
                            id=f"{session_id}-{counter}",
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
                    counter += 1

                    if result is not None:
                        yield UserTranscriptEntry(
                            parentUuid=None,
                            isSidechain=False,
                            userType="external",
                            cwd="",
                            sessionId=session_id,
                            version="",
                            uuid=f"{session_id}-{counter}",
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
                        counter += 1

            elif msg_type in ("info", "error", "warning"):
                yield AssistantTranscriptEntry(
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=f"{session_id}-{counter}",
                    timestamp=timestamp,
                    message=AssistantMessageModel(
                        id=f"{session_id}-{counter}",
                        type="message",
                        role="assistant",
                        model="gemini",
                        content=[TextContent(type="text", text=str(content))],
                    ),
                )
                counter += 1

    def _get_file_mtime(self, path: Path) -> str:
        """Get file modification time as ISO string."""
        from datetime import datetime

        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime).isoformat()

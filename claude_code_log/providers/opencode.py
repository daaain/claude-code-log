"""OpenCode session provider."""

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


class OpenCodeProvider(BaseProvider):
    """Provider for OpenCode sessions.

    Parses OpenCode sharded JSON files from ~/.local/share/opencode/storage/.
    Format: https://opencode.ai/docs/sdk/types
    """

    def get_provider_name(self) -> str:
        return "opencode"

    def get_session_format(self) -> str:
        return "json"

    def get_data_dir(self) -> Optional[Path]:
        """Return the OpenCode storage directory."""
        data_dir = Path.home() / ".local" / "share" / "opencode" / "storage"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all OpenCode sessions."""
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        session_dir = data_dir / "session"
        if not session_dir.exists():
            return

        for project_dir in session_dir.iterdir():
            if not project_dir.is_dir():
                continue

            for session_file in project_dir.glob("*.json"):
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        session_data = json.load(f)

                    if not isinstance(session_data, dict):
                        continue

                    session_id = session_data.get("id", session_file.stem)
                    title = session_data.get("title", "")
                    time_data = session_data.get("time", {})
                    created_at = time_data.get("created")

                    if created_at and isinstance(created_at, (int, float)):
                        from datetime import datetime

                        created_at = datetime.fromtimestamp(
                            created_at / 1000
                        ).isoformat()

                    yield SessionInfo(
                        provider="opencode",
                        session_id=session_id,
                        title=title,
                        created_at=created_at,
                        project_path=project_dir,
                    )
                except (json.JSONDecodeError, OSError):
                    continue

    def load_session(self, session_id: str) -> Iterator[TranscriptEntry]:
        """Load an OpenCode session.

        Parses sharded JSON format:
        - session/*.json: Session metadata
        - message/{session-id}/*.json: Messages with role (user/assistant)
        - part/{message-id}/*.json: Parts (text, tool-invocation, tool-result, reasoning)
        """
        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("OpenCode data directory not found")

        session_data = self._load_session_data(data_dir, session_id)
        if session_data is None:
            raise FileNotFoundError(f"Session {session_id} not found")

        yield from self._parse_session(data_dir, session_id, session_data)

    def _load_session_data(self, data_dir: Path, session_id: str) -> Optional[dict]:
        """Load session metadata from JSON file."""
        session_dir = data_dir / "session"
        if not session_dir.exists():
            return None

        for project_dir in session_dir.iterdir():
            if not project_dir.is_dir():
                continue

            session_file = project_dir / f"{session_id}.json"
            if session_file.exists():
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

        return None

    def _parse_session(
        self,
        data_dir: Path,
        session_id: str,
        session_data: dict,
    ) -> Iterator[TranscriptEntry]:
        """Parse an OpenCode session."""
        message_dir = data_dir / "message" / session_id
        if not message_dir.exists():
            return

        part_dir = data_dir / "part"
        timestamp_counter = 0

        for message_file in sorted(message_dir.glob("*.json")):
            try:
                with open(message_file, "r", encoding="utf-8") as f:
                    message_data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if not isinstance(message_data, dict):
                continue

            role = message_data.get("role")
            message_id = message_data.get("id", message_file.stem)
            time_data = message_data.get("time", {})
            created_at = time_data.get("created")

            if created_at and isinstance(created_at, (int, float)):
                from datetime import datetime

                timestamp = datetime.fromtimestamp(created_at / 1000).isoformat()
            else:
                timestamp = ""

            parts = self._load_parts(part_dir, message_id)

            if role == "user":
                text_parts = []
                for part in parts:
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))

                if text_parts:
                    yield UserTranscriptEntry(
                        type="user",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=f"{session_id}-{timestamp_counter}",
                        timestamp=timestamp,
                        message=UserMessageModel(
                            role="user",
                            content=[
                                TextContent(type="text", text="\n".join(text_parts))
                            ],
                        ),
                    )
                    timestamp_counter += 1

            elif role == "assistant":
                tokens = message_data.get("tokens", {})
                model_id = message_data.get("modelID", "unknown")
                provider_id = message_data.get("providerID", "unknown")
                model_name = f"{provider_id}/{model_id}"

                for part in parts:
                    part_type = part.get("type")

                    if part_type == "text":
                        text = part.get("text", "")
                        if text:
                            yield AssistantTranscriptEntry(
                        type="assistant",
                                parentUuid=None,
                                isSidechain=False,
                                userType="external",
                                cwd="",
                                sessionId=session_id,
                                version="",
                                uuid=f"{session_id}-{timestamp_counter}",
                                timestamp=timestamp,
                                message=AssistantMessageModel(
                                    id=f"{session_id}-{timestamp_counter}",
                                    type="message",
                                    role="assistant",
                                    model=model_name,
                                    content=[TextContent(type="text", text=text)],
                                ),
                            )
                            timestamp_counter += 1

                    elif part_type == "reasoning":
                        text = part.get("text", "")
                        if text:
                            yield AssistantTranscriptEntry(
                        type="assistant",
                                parentUuid=None,
                                isSidechain=False,
                                userType="external",
                                cwd="",
                                sessionId=session_id,
                                version="",
                                uuid=f"{session_id}-{timestamp_counter}",
                                timestamp=timestamp,
                                message=AssistantMessageModel(
                                    id=f"{session_id}-{timestamp_counter}",
                                    type="message",
                                    role="assistant",
                                    model=model_name,
                                    content=[
                                        ThinkingContent(
                                            type="thinking",
                                            thinking=text,
                                        )
                                    ],
                                ),
                            )
                            timestamp_counter += 1

                    elif part_type and part_type.startswith("tool-"):
                        tool_name = part_type[5:]
                        tool_call_id = part.get(
                            "toolCallId", f"{session_id}-{timestamp_counter}"
                        )
                        input_data = part.get("input", {})

                        yield AssistantTranscriptEntry(
                        type="assistant",
                            parentUuid=None,
                            isSidechain=False,
                            userType="external",
                            cwd="",
                            sessionId=session_id,
                            version="",
                            uuid=f"{session_id}-{timestamp_counter}",
                            timestamp=timestamp,
                            message=AssistantMessageModel(
                                id=f"{session_id}-{timestamp_counter}",
                                type="message",
                                role="assistant",
                                model=model_name,
                                content=[
                                    ToolUseContent(
                                        type="tool_use",
                                        id=tool_call_id,
                                        name=tool_name,
                                        input=(
                                            input_data
                                            if isinstance(input_data, dict)
                                            else {"raw": str(input_data)}
                                        ),
                                    )
                                ],
                            ),
                        )
                        timestamp_counter += 1

                        state = part.get("state", {})
                        if state == "output" or isinstance(state, dict):
                            output = part.get("output", "")
                            if output:
                                yield UserTranscriptEntry(
                        type="user",
                                    parentUuid=None,
                                    isSidechain=False,
                                    userType="external",
                                    cwd="",
                                    sessionId=session_id,
                                    version="",
                                    uuid=f"{session_id}-{timestamp_counter}",
                                    timestamp=timestamp,
                                    message=UserMessageModel(
                                        role="user",
                                        content=[
                                            ToolResultContent(
                                                type="tool_result",
                                                tool_use_id=tool_call_id,
                                                content=str(output),
                                            )
                                        ],
                                    ),
                                )
                                timestamp_counter += 1

    def _load_parts(self, part_dir: Path, message_id: str) -> list[dict]:
        """Load parts for a message."""
        message_part_dir = part_dir / message_id
        if not message_part_dir.exists():
            return []

        parts = []
        for part_file in sorted(message_part_dir.glob("*.json")):
            try:
                with open(part_file, "r", encoding="utf-8") as f:
                    part_data = json.load(f)
                if isinstance(part_data, dict):
                    parts.append(part_data)
            except (json.JSONDecodeError, OSError):
                continue

        return parts

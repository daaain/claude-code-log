"""Codex CLI session provider."""

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from claude_code_log.models import (
    AssistantMessageModel,
    AssistantTranscriptEntry,
    BaseTranscriptEntry,
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


class CodexProvider(BaseProvider):
    """Provider for Codex CLI sessions.

    Parses Codex rollout JSONL files from ~/.codex/sessions/.
    Format: https://github.com/openai/codex/blob/main/codex-rs/rollout/src/recorder.rs
    """

    def get_provider_name(self) -> str:
        return "codex"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        """Return the Codex sessions directory."""
        data_dir = Path.home() / ".codex" / "sessions"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        """Discover all Codex CLI sessions."""
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        # Codex stores sessions in YYYY/MM/DD/rollout-*.jsonl
        for year_dir in data_dir.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue

            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue

                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir() or not day_dir.name.isdigit():
                        continue

                    for rollout_file in day_dir.glob("rollout-*.jsonl"):
                        session_id = rollout_file.stem
                        yield SessionInfo(
                            provider="codex",
                            session_id=session_id,
                            created_at=f"{year_dir.name}-{month_dir.name}-{day_dir.name}",
                        )

    def load_session(self, session_id: str) -> Iterator[TranscriptEntry]:
        """Load a Codex CLI session.

        Parses rollout JSONL format:
        - session_meta: First line, session-level metadata
        - response_item: Messages, tool calls, tool outputs
        - event_msg: Token counts, task lifecycle, agent reasoning
        """
        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Codex data directory not found")

        rollout_file = self._find_session_file(data_dir, session_id)
        if rollout_file is None:
            raise FileNotFoundError(f"Session {session_id} not found")

        yield from self._parse_rollout_file(rollout_file)

    def _find_session_file(self, data_dir: Path, session_id: str) -> Optional[Path]:
        """Find a session file by session ID."""
        for year_dir in data_dir.iterdir():
            if not year_dir.is_dir() or not year_dir.name.isdigit():
                continue

            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or not month_dir.name.isdigit():
                    continue

                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir() or not day_dir.name.isdigit():
                        continue

                    rollout_file = day_dir / f"{session_id}.jsonl"
                    if rollout_file.exists():
                        return rollout_file

                    for f in day_dir.glob("rollout-*.jsonl"):
                        if session_id in f.stem:
                            return f

        return None

    def _parse_rollout_file(self, rollout_file: Path) -> Iterator[TranscriptEntry]:
        """Parse a Codex rollout JSONL file."""
        session_id = rollout_file.stem
        timestamp_counter = 0

        with open(rollout_file, "r", encoding="utf-8") as f:
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
                if "timestamp" not in entry or "type" not in entry:
                    continue

                entry_type = entry.get("type")
                payload = entry.get("payload", {})
                timestamp = entry.get("timestamp", "")

                if entry_type == "session_meta":
                    continue

                elif entry_type == "response_item":
                    yield from self._parse_response_item(
                        payload, session_id, timestamp, timestamp_counter
                    )
                    timestamp_counter += 1

                elif entry_type == "event_msg":
                    yield from self._parse_event_msg(
                        payload, session_id, timestamp, timestamp_counter
                    )
                    timestamp_counter += 1

                elif entry_type == "turn_context":
                    continue

                elif entry_type == "compacted":
                    continue

    def _parse_response_item(
        self,
        payload: dict,
        session_id: str,
        timestamp: str,
        counter: int,
    ) -> Iterator[TranscriptEntry]:
        """Parse a response_item payload."""
        payload_type = payload.get("type")
        uuid = f"{session_id}-{counter}"

        if payload_type == "message":
            role = payload.get("role")
            content = payload.get("content", [])

            if role == "assistant":
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "output_text":
                        text_parts.append(item.get("text", ""))

                if text_parts:
                    yield AssistantTranscriptEntry(
                        type="assistant",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=uuid,
                        timestamp=timestamp,
                        message=AssistantMessageModel(
                            id=uuid,
                            type="message",
                            role="assistant",
                            model="codex",
                            content=[
                                TextContent(type="text", text="\n".join(text_parts))
                            ],
                        ),
                    )

            elif role == "developer":
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "output_text":
                        text_parts.append(item.get("text", ""))

                if text_parts:
                    yield AssistantTranscriptEntry(
                        type="assistant",
                        parentUuid=None,
                        isSidechain=False,
                        userType="external",
                        cwd="",
                        sessionId=session_id,
                        version="",
                        uuid=uuid,
                        timestamp=timestamp,
                        message=AssistantMessageModel(
                            id=uuid,
                            type="message",
                            role="assistant",
                            model="codex",
                            content=[
                                ThinkingContent(
                                    type="thinking",
                                    thinking="\n".join(text_parts),
                                )
                            ],
                        ),
                    )

        elif payload_type == "function_call":
            name = payload.get("name", "unknown")
            arguments_str = payload.get("arguments", "{}")
            call_id = payload.get("call_id", uuid)

            try:
                arguments = json.loads(arguments_str) if arguments_str else {}
            except json.JSONDecodeError:
                arguments = {"raw": arguments_str}

            yield AssistantTranscriptEntry(
                        type="assistant",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="",
                sessionId=session_id,
                version="",
                uuid=uuid,
                timestamp=timestamp,
                message=AssistantMessageModel(
                    id=uuid,
                    type="message",
                    role="assistant",
                    model="codex",
                    content=[
                        ToolUseContent(
                            type="tool_use",
                            id=call_id,
                            name=name,
                            input=arguments,
                        )
                    ],
                ),
            )

        elif payload_type == "function_call_output":
            call_id = payload.get("call_id", uuid)
            output = payload.get("output", "")

            yield UserTranscriptEntry(
                        type="user",
                parentUuid=None,
                isSidechain=False,
                userType="external",
                cwd="",
                sessionId=session_id,
                version="",
                uuid=uuid,
                timestamp=timestamp,
                message=UserMessageModel(
                    role="user",
                    content=[
                        ToolResultContent(
                            type="tool_result",
                            tool_use_id=call_id,
                            content=output,
                        )
                    ],
                ),
            )

    def _parse_event_msg(
        self,
        payload: dict,
        session_id: str,
        timestamp: str,
        counter: int,
    ) -> Iterator[TranscriptEntry]:
        """Parse an event_msg payload."""
        payload_type = payload.get("type")
        uuid = f"{session_id}-{counter}"

        if payload_type == "agent_message":
            message = payload.get("message", "")
            if message:
                yield AssistantTranscriptEntry(
                        type="assistant",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=session_id,
                    version="",
                    uuid=uuid,
                    timestamp=timestamp,
                    message=AssistantMessageModel(
                        id=uuid,
                        type="message",
                        role="assistant",
                        model="codex",
                        content=[
                            ThinkingContent(
                                type="thinking",
                                thinking=message,
                            )
                        ],
                    ),
                )

        elif payload_type == "token_count":
            pass

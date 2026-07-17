"""Abstract base class for session providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional, cast

from claude_code_log.models import (
    AssistantMessageModel,
    AssistantTranscriptEntry,
    TextContent,
    ThinkingContent,
    ToolUseContent,
    TranscriptEntry,
    UserMessageModel,
    UserTranscriptEntry,
)


@dataclass
class SessionInfo:
    provider: str
    session_id: str
    title: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    project_path: Optional[Path] = None
    message_count: int = 0
    total_tokens: int = 0


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        items: list[Any] = cast(list[Any], content)
        parts: list[str] = []
        for item in items:
            item_dict = cast(dict[str, Any], item) if isinstance(item, dict) else None
            if item_dict is not None:
                parts.append(str(item_dict.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def file_mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat()


def make_user_entry(
    session_id: str,
    uuid: str,
    timestamp: str,
    content: Any,
) -> UserTranscriptEntry:
    return UserTranscriptEntry(
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
            content=[TextContent(type="text", text=extract_text(content))],
        ),
    )


def make_tool_result_entry(
    session_id: str,
    uuid: str,
    timestamp: str,
    tool_use_id: str,
    content: str,
) -> UserTranscriptEntry:
    from claude_code_log.models import ToolResultContent

    return UserTranscriptEntry(
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
                    tool_use_id=tool_use_id,
                    content=content,
                )
            ],
        ),
    )


def make_assistant_entry(
    session_id: str,
    uuid: str,
    timestamp: str,
    model: str,
    content: Any,
) -> AssistantTranscriptEntry:
    content_list: list[Any] = (
        cast(list[Any], content)
        if isinstance(content, list)
        else [TextContent(type="text", text=str(content))]
    )
    return AssistantTranscriptEntry(
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
            model=model,
            content=content_list,
        ),
    )


def make_thinking_entry(
    session_id: str,
    uuid: str,
    timestamp: str,
    model: str,
    text: str,
) -> AssistantTranscriptEntry:
    return AssistantTranscriptEntry(
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
            model=model,
            content=[ThinkingContent(type="thinking", thinking=text)],
        ),
    )


def make_tool_use_entry(
    session_id: str,
    uuid: str,
    timestamp: str,
    model: str,
    tool_id: str,
    tool_name: str,
    tool_input: Any,
) -> AssistantTranscriptEntry:
    return AssistantTranscriptEntry(
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
            model=model,
            content=[
                ToolUseContent(
                    type="tool_use",
                    id=tool_id,
                    name=tool_name,
                    input=tool_input,
                )
            ],
        ),
    )


class BaseProvider(ABC):
    @abstractmethod
    def get_provider_name(self) -> str: ...

    @abstractmethod
    def get_session_format(self) -> str: ...

    @abstractmethod
    def get_data_dir(self) -> Optional[Path]: ...

    @abstractmethod
    def discover_sessions(self) -> Iterator[SessionInfo]: ...

    @abstractmethod
    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]: ...

    def is_available(self) -> bool:
        data_dir = self.get_data_dir()
        return data_dir is not None and data_dir.exists()

    def get_session_stats(self, session_id: str) -> dict[str, Any]:
        return {}

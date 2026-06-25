"""Antigravity CLI (agy) session provider."""

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator, Optional, cast

logger = logging.getLogger(__name__)

from claude_code_log.models import TranscriptEntry

from .base import (
    BaseProvider,
    SessionInfo,
    extract_text,
    file_mtime_iso,
    make_assistant_entry,
    make_user_entry,
)


class AgyProvider(BaseProvider):
    def get_provider_name(self) -> str:
        return "agy"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        data_dir = Path.home() / ".gemini" / "antigravity-cli"
        return data_dir if data_dir.exists() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
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
            yield SessionInfo(
                provider="agy",
                session_id=session_dir.name,
                created_at=file_mtime_iso(transcript_file),
            )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        if not self._is_valid_session_id(session_id):
            raise ValueError(f"Invalid session_id: {session_id}")

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

        prev_uuid: Optional[str] = None
        message_count = 0

        with open(transcript_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    raw_entry: Any = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed JSON line in %s", transcript_file
                    )
                    continue

                if isinstance(raw_entry, dict):
                    entry = cast(dict[str, Any], raw_entry)
                    for transcript_entry in self._parse_entry(
                        entry, session_id, message_count, prev_uuid
                    ):
                        if hasattr(transcript_entry, "uuid"):
                            prev_uuid = cast(Any, transcript_entry).uuid
                        yield transcript_entry
                        message_count += 1

                if max_messages is not None and message_count >= max_messages:
                    break

    def _parse_entry(
        self,
        entry: dict[str, Any],
        session_id: str,
        index: int,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        entry_type = str(entry.get("type", ""))
        timestamp = str(entry.get("created_at", ""))
        content = entry.get("content", "")

        if entry_type == "USER_INPUT":
            yield from self._parse_user_input(
                content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "PLANNER_RESPONSE":
            yield from self._parse_planner_response(
                entry, content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "CHECKPOINT":
            yield from self._parse_checkpoint(
                content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "LIST_DIRECTORY":
            yield from self._make_tool_entry(
                "list_dir", content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "GENERIC":
            yield from self._parse_generic(
                content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "RUN_COMMAND":
            yield from self._parse_run_command(
                entry, content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "VIEW_FILE":
            yield from self._parse_view_file(
                entry, content, session_id, index, timestamp, parent_uuid
            )

        elif entry_type == "CODE_ACTION":
            yield from self._parse_code_action(
                entry, content, session_id, index, timestamp, parent_uuid
            )

        # CONVERSATION_HISTORY entries are internal bookkeeping, skip them

    # -- Entry type parsers --

    def _parse_user_input(
        self,
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        content_str = content if isinstance(content, str) else json.dumps(content)
        text = self._extract_user_request(content_str)
        if text:
            uid = f"agy-{session_id}-{index}"
            entry = make_user_entry(session_id, uid, timestamp, text)
            entry.parentUuid = parent_uuid
            yield entry

    def _parse_planner_response(
        self,
        raw_entry: dict[str, Any],
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        text = content if isinstance(content, str) else json.dumps(content)
        tool_calls_raw = raw_entry.get("tool_calls", [])
        tool_calls = self._coerce_tool_calls(tool_calls_raw)

        if tool_calls:
            yield from self._parse_tool_calls(
                tool_calls, text, session_id, index, timestamp, parent_uuid
            )
        elif text:
            uid = f"agy-{session_id}-{index}"
            entry = make_assistant_entry(
                session_id, uid, timestamp, "antigravity", text
            )
            entry.parentUuid = parent_uuid
            yield entry

    def _parse_checkpoint(
        self,
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        """CHECKPOINT entries are compaction summaries — render as system context."""
        text = content if isinstance(content, str) else json.dumps(content)
        if text:
            uid = f"agy-{session_id}-{index}"
            entry = make_assistant_entry(
                session_id, uid, timestamp, "antigravity", f"[checkpoint]\n{text}"
            )
            entry.parentUuid = parent_uuid
            yield entry

    def _parse_generic(
        self,
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        """GENERIC entries are uncategorized model output."""
        text = extract_text(content)
        if text:
            uid = f"agy-{session_id}-{index}"
            entry = make_assistant_entry(
                session_id, uid, timestamp, "antigravity", text
            )
            entry.parentUuid = parent_uuid
            yield entry

    def _parse_run_command(
        self,
        raw_entry: dict[str, Any],
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        """RUN_COMMAND entries are shell command executions."""
        command = str(raw_entry.get("command", ""))
        text = content if isinstance(content, str) else json.dumps(content)
        display = (
            f"[run_command: {command}]\n{text}" if command else f"[run_command]\n{text}"
        )
        uid = f"agy-{session_id}-{index}"
        entry = make_assistant_entry(session_id, uid, timestamp, "antigravity", display)
        entry.parentUuid = parent_uuid
        yield entry

    def _parse_view_file(
        self,
        raw_entry: dict[str, Any],
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        """VIEW_FILE entries are file reads."""
        file_path = str(raw_entry.get("file_path", raw_entry.get("path", "")))
        text = content if isinstance(content, str) else json.dumps(content)
        display = (
            f"[view_file: {file_path}]\n{text}" if file_path else f"[view_file]\n{text}"
        )
        uid = f"agy-{session_id}-{index}"
        entry = make_assistant_entry(session_id, uid, timestamp, "antigravity", display)
        entry.parentUuid = parent_uuid
        yield entry

    def _parse_code_action(
        self,
        raw_entry: dict[str, Any],
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        """CODE_ACTION entries are code modifications (edits, writes)."""
        action = str(raw_entry.get("action", ""))
        file_path = str(raw_entry.get("file_path", raw_entry.get("path", "")))
        text = content if isinstance(content, str) else json.dumps(content)
        label = f"[code_action: {action} {file_path}]".strip()
        display = f"{label}\n{text}" if text else label
        uid = f"agy-{session_id}-{index}"
        entry = make_assistant_entry(session_id, uid, timestamp, "antigravity", display)
        entry.parentUuid = parent_uuid
        yield entry

    # -- Helpers --

    def _make_tool_entry(
        self,
        tool_name: str,
        content: Any,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        text = content if isinstance(content, str) else json.dumps(content)
        if text:
            uid = f"agy-{session_id}-{index}"
            entry = make_assistant_entry(
                session_id,
                uid,
                timestamp,
                "antigravity",
                f"[tool: {tool_name}]\n{text}",
            )
            entry.parentUuid = parent_uuid
            yield entry

    def _parse_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        fallback_text: str,
        session_id: str,
        index: int,
        timestamp: str,
        parent_uuid: Optional[str],
    ) -> Iterator[TranscriptEntry]:
        last_uuid = parent_uuid

        for tc_index, tc in enumerate(tool_calls):
            name = str(tc.get("name", "unknown"))
            args_raw = tc.get("args", {})
            args: dict[str, Any] = (
                cast(dict[str, Any], args_raw) if isinstance(args_raw, dict) else {}
            )
            args_str = json.dumps(args, indent=2) if args else ""
            text = f"[tool: {name}]\n{args_str}" if args_str else f"[tool: {name}]"
            uid = f"agy-{session_id}-{index}-{tc_index}-{name}"
            entry = make_assistant_entry(
                session_id, uid, timestamp, "antigravity", text
            )
            entry.parentUuid = last_uuid
            last_uuid = uid
            yield entry

        # Emit the response text after tool calls, chained to the last tool
        if fallback_text and not fallback_text.startswith("[tool:"):
            uid = f"agy-{session_id}-{index}-response"
            entry = make_assistant_entry(
                session_id, uid, timestamp, "antigravity", fallback_text
            )
            entry.parentUuid = last_uuid
            yield entry

    def _coerce_tool_calls(self, tool_calls_raw: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if not isinstance(tool_calls_raw, list):
            return result
        for tc_raw in cast(list[Any], tool_calls_raw):
            if isinstance(tc_raw, dict):
                result.append(cast(dict[str, Any], tc_raw))
            else:
                result.append({"name": "unknown", "args": {"raw": str(tc_raw)}})
        return result

    def _extract_user_request(self, content: str) -> str:
        match = re.search(
            r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL
        )
        if match:
            return match.group(1).strip()
        return content.strip() if content else ""

    def _is_valid_session_id(self, session_id: str) -> bool:
        return bool(re.fullmatch(r"[a-f0-9\-]+", session_id))

"""Codex CLI rollout session provider.

The rollout format is an implementation detail of Codex rather than a stable
file-format API.  Parsing here is deliberately tolerant: the provider keeps
the raw-record decoder small, ignores unknown records, and normalizes only
shapes for which it has useful semantics.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Iterator, Optional, TypeAlias, cast

from claude_code_log.models import (
    AssistantTranscriptEntry,
    ToolResultContent,
    TranscriptEntry,
    UserMessageModel,
    UserTranscriptEntry,
)

from .base import (
    BaseProvider,
    SessionInfo,
    file_mtime_iso,
    make_assistant_entry,
    make_thinking_entry,
    make_tool_use_entry,
    make_user_entry,
)
from .codex_tools import adapt_codex_tool_call

logger = logging.getLogger(__name__)

_CodexEntry: TypeAlias = UserTranscriptEntry | AssistantTranscriptEntry

_ROLLOUT_GLOB = "rollout-*.jsonl"
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_FILENAME_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_RUNNING_CELL_RE = re.compile(r"Script running with cell ID ([^\s]+)")


@dataclass(frozen=True)
class CodexSessionIdentity:
    """Identity and lineage retained from the first session metadata record."""

    thread_id: str
    path: Path
    created_at: Optional[str] = None
    cwd: Optional[Path] = None
    model: str = "codex"
    version: str = ""
    parent_thread_id: Optional[str] = None
    forked_from_id: Optional[str] = None
    source_kind: Optional[str] = None
    spawn_call_id: Optional[str] = None
    inherited_prefix_records: int = 0


@dataclass
class CodexSessionInfo(SessionInfo):
    """Discovered Codex session with retained cross-thread lineage."""

    parent_thread_id: Optional[str] = None
    forked_from_id: Optional[str] = None
    spawn_call_id: Optional[str] = None
    source_kind: Optional[str] = None
    inherited_prefix_records: int = 0


@dataclass(frozen=True)
class _DecodedRecord:
    line_no: int
    timestamp: str
    kind: str
    payload: dict[str, Any]


class CodexProvider(BaseProvider):
    """Read active Codex rollout files from ``$CODEX_HOME/sessions``."""

    def get_provider_name(self) -> str:
        return "codex"

    def get_session_format(self) -> str:
        return "jsonl"

    def get_data_dir(self) -> Optional[Path]:
        configured_home = os.environ.get("CODEX_HOME")
        codex_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
        sessions_dir = codex_home / "sessions"
        return codex_home if sessions_dir.is_dir() else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        data_dir = self.get_data_dir()
        if data_dir is None:
            return

        # A duplicated thread id is corrupt/ambiguous.  Discovery remains
        # useful and deterministic by retaining the lexicographically first
        # path; loading that id reports the ambiguity instead of guessing.
        identities: dict[str, CodexSessionIdentity] = {}
        index = self._session_index(data_dir)
        for path in self._rollout_paths(data_dir):
            identity = self._read_identity(path)
            if identity.thread_id in identities:
                logger.warning(
                    "Duplicate Codex thread id %s; retaining first discovered rollout",
                    identity.thread_id,
                )
                continue
            identities[identity.thread_id] = self._with_inherited_prefix(
                identity, index
            )

        for identity in identities.values():
            yield CodexSessionInfo(
                provider="codex",
                session_id=identity.thread_id,
                created_at=identity.created_at or file_mtime_iso(identity.path),
                updated_at=file_mtime_iso(identity.path),
                project_path=identity.cwd,
                parent_thread_id=identity.parent_thread_id,
                forked_from_id=identity.forked_from_id,
                spawn_call_id=identity.spawn_call_id,
                source_kind=identity.source_kind,
                inherited_prefix_records=identity.inherited_prefix_records,
            )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        if not session_id or _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError(f"Invalid session_id: {session_id}")
        if max_messages is not None and max_messages <= 0:
            return

        data_dir = self.get_data_dir()
        if data_dir is None:
            raise ValueError("Codex data directory not found")

        index = self._session_index(data_dir)
        if session_id not in index:
            raise FileNotFoundError(f"Codex session {session_id} not found")
        paths = index[session_id]
        if len(paths) != 1:
            raise ValueError(f"Multiple Codex rollouts have thread id {session_id}")

        identity = self._with_inherited_prefix(self._read_identity(paths[0]), index)
        records = list(self._decode_records(identity.path))
        records = self._without_inherited_prefix(
            records, identity.inherited_prefix_records
        )
        yield from self._normalize_records(identity, records, max_messages)

    def _rollout_paths(self, data_dir: Path) -> list[Path]:
        # Recursive discovery supports both current date shards and old flat
        # layouts.  archived_sessions is deliberately outside this v1 root.
        sessions_dir = data_dir / "sessions"
        return sorted(
            path for path in sessions_dir.rglob(_ROLLOUT_GLOB) if path.is_file()
        )

    def _session_index(self, data_dir: Path) -> dict[str, list[Path]]:
        index: dict[str, list[Path]] = {}
        for path in self._rollout_paths(data_dir):
            identity = self._read_identity(path)
            index.setdefault(identity.thread_id, []).append(path)
        return index

    def _with_inherited_prefix(
        self,
        identity: CodexSessionIdentity,
        index: dict[str, list[Path]],
    ) -> CodexSessionIdentity:
        parent_id = identity.parent_thread_id
        parent_paths = index.get(parent_id, []) if parent_id else []
        if len(parent_paths) != 1:
            return identity
        child_records = self._prefix_candidates(
            list(self._decode_records(identity.path))
        )
        parent_records = self._prefix_candidates(
            list(self._decode_records(parent_paths[0]))
        )
        prefix_length = self._contiguous_prefix_length(child_records, parent_records)
        if prefix_length == 0:
            return identity
        return CodexSessionIdentity(
            thread_id=identity.thread_id,
            path=identity.path,
            created_at=identity.created_at,
            cwd=identity.cwd,
            model=identity.model,
            version=identity.version,
            parent_thread_id=identity.parent_thread_id,
            forked_from_id=identity.forked_from_id,
            source_kind=identity.source_kind,
            spawn_call_id=identity.spawn_call_id,
            inherited_prefix_records=prefix_length,
        )

    def _prefix_candidates(self, records: list[_DecodedRecord]) -> list[_DecodedRecord]:
        """Exclude thread-local metadata when comparing copied history."""
        return [
            record
            for record in records
            if record.kind not in {"session_meta", "turn_context"}
        ]

    def _contiguous_prefix_length(
        self,
        child_records: list[_DecodedRecord],
        parent_records: list[_DecodedRecord],
    ) -> int:
        """Find the longest leading child sequence occurring in its parent."""
        best = 0
        for start in range(len(parent_records)):
            length = 0
            while (
                length < len(child_records)
                and start + length < len(parent_records)
                and self._same_semantic_record(
                    child_records[length], parent_records[start + length]
                )
            ):
                length += 1
            best = max(best, length)
        return best

    def _same_semantic_record(
        self, left: _DecodedRecord, right: _DecodedRecord
    ) -> bool:
        # Envelope timestamps may be rewritten while copying a rollout; the
        # semantic family and payload identify the inherited record.
        return left.kind == right.kind and left.payload == right.payload

    def _without_inherited_prefix(
        self, records: list[_DecodedRecord], prefix_length: int
    ) -> list[_DecodedRecord]:
        if prefix_length <= 0:
            return records
        remaining = prefix_length
        result: list[_DecodedRecord] = []
        for record in records:
            if record.kind in {"session_meta", "turn_context"}:
                result.append(record)
            elif remaining:
                remaining -= 1
            else:
                result.append(record)
        return result

    def _read_identity(self, path: Path) -> CodexSessionIdentity:
        fallback_id = self._filename_thread_id(path)
        for record in self._decode_records(path):
            if record.kind != "session_meta":
                continue
            payload = record.payload
            thread_id = self._nonempty_string(payload.get("id")) or fallback_id
            cwd_text = self._nonempty_string(payload.get("cwd"))
            source = payload.get("source")
            source_dict = (
                cast(dict[str, Any], source) if isinstance(source, dict) else {}
            )
            source_kind, spawn_call_id = self._source_metadata(source_dict)
            return CodexSessionIdentity(
                thread_id=thread_id,
                path=path,
                created_at=record.timestamp
                or self._nonempty_string(payload.get("timestamp")),
                cwd=Path(cwd_text) if cwd_text else None,
                model=self._nonempty_string(payload.get("model")) or "codex",
                version=(
                    self._nonempty_string(payload.get("cli_version"))
                    or self._nonempty_string(payload.get("version"))
                    or ""
                ),
                parent_thread_id=(
                    self._nonempty_string(payload.get("parent_thread_id"))
                    or self._source_string(source_dict, "parent_thread_id")
                ),
                forked_from_id=(
                    self._nonempty_string(payload.get("forked_from_id"))
                    or self._source_string(source_dict, "forked_from_id")
                ),
                source_kind=source_kind,
                spawn_call_id=spawn_call_id,
            )
        return CodexSessionIdentity(thread_id=fallback_id, path=path)

    def _filename_thread_id(self, path: Path) -> str:
        match = _FILENAME_UUID_RE.search(path.stem)
        return match.group(1) if match else path.stem.removeprefix("rollout-")

    def _decode_records(self, path: Path) -> Iterator[_DecodedRecord]:
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Unable to read Codex rollout %s: %s", path, exc)
            return

        with stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    raw: Any = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Malformed JSON in Codex rollout %s line %d", path, line_no
                    )
                    continue
                if not isinstance(raw, dict):
                    logger.warning(
                        "Non-object record in Codex rollout %s line %d", path, line_no
                    )
                    continue
                raw_dict = cast(dict[str, Any], raw)
                kind = self._nonempty_string(raw_dict.get("type"))
                payload_raw = raw_dict.get("payload")
                # Early Codex rollouts used a flat header followed by flat
                # response items. Normalize that small legacy family into the
                # modern envelope before applying the shared decoder rules.
                if kind is None and self._nonempty_string(raw_dict.get("id")):
                    kind = "session_meta"
                    payload_raw = raw_dict
                elif kind in {
                    "message",
                    "reasoning",
                    "function_call",
                    "function_call_output",
                    "custom_tool_call",
                    "custom_tool_call_output",
                } and not isinstance(payload_raw, dict):
                    kind = "response_item"
                    payload_raw = raw_dict
                if not kind or not isinstance(payload_raw, dict):
                    logger.warning(
                        "Malformed record in Codex rollout %s line %d", path, line_no
                    )
                    continue
                yield _DecodedRecord(
                    line_no=line_no,
                    timestamp=self._nonempty_string(raw_dict.get("timestamp")) or "",
                    kind=kind,
                    payload=cast(dict[str, Any], payload_raw),
                )

    def _normalize_records(
        self,
        identity: CodexSessionIdentity,
        records: list[_DecodedRecord],
        max_messages: Optional[int],
    ) -> Iterator[_CodexEntry]:
        records = self._coalesce_command_sessions(records)
        preferred = Counter(
            fingerprint
            for record in records
            if record.kind == "event_msg"
            for fingerprint in [self._event_message_fingerprint(record.payload)]
            if fingerprint is not None
        )
        suppressed = Counter[tuple[str, str]]()
        model = identity.model
        cwd = str(identity.cwd) if identity.cwd else ""
        version = identity.version
        parent_uuid: Optional[str] = None
        emitted = 0

        for record in records:
            if record.kind == "turn_context":
                model = self._nonempty_string(record.payload.get("model")) or model
                cwd = self._nonempty_string(record.payload.get("cwd")) or cwd
                continue
            if record.kind == "session_meta":
                # Only context fields may evolve; identity/lineage always comes
                # from the first metadata record read above.
                model = self._nonempty_string(record.payload.get("model")) or model
                cwd = self._nonempty_string(record.payload.get("cwd")) or cwd
                version = (
                    self._nonempty_string(record.payload.get("cli_version")) or version
                )
                continue

            candidates = self._normalize_record(
                identity.thread_id, record, model, preferred, suppressed
            )
            for subindex, entry in enumerate(candidates):
                if max_messages is not None and emitted >= max_messages:
                    return
                entry.uuid = self._entry_uuid(
                    identity.thread_id, record.line_no, subindex
                )
                entry.parentUuid = parent_uuid
                entry.cwd = cwd
                entry.version = version
                entry.sessionId = identity.thread_id
                if hasattr(entry, "message") and entry.type == "assistant":
                    entry.message.id = entry.uuid
                    entry.message.model = model
                parent_uuid = entry.uuid
                emitted += 1
                yield entry

    def _coalesce_command_sessions(
        self, records: list[_DecodedRecord]
    ) -> list[_DecodedRecord]:
        """Fold adjacent terminal polling calls into their spawning Bash result.

        A long-running ``exec_command`` first returns a cell id.  Codex then
        emits ``wait(cell_id=...)`` and usually ``write_stdin(session_id=...)``
        as separate tools.  They are transport details of one command, not
        independent transcript actions.  Coalesce only consecutive visible
        tool events whose identifiers form that exact chain; otherwise retain
        every record unchanged.
        """
        tool_records = [
            index
            for index, record in enumerate(records)
            if record.kind == "response_item"
            and self._nonempty_string(record.payload.get("type"))
            in {
                "function_call",
                "custom_tool_call",
                "function_call_output",
                "custom_tool_call_output",
            }
        ]
        suppressed: set[int] = set()
        replacements: dict[int, _DecodedRecord] = {}
        position = 0
        while position + 3 < len(tool_records):
            call_index, result_index = tool_records[position : position + 2]
            call = self._adapted_call(records[call_index])
            result = records[result_index]
            if call is None or call[1].name != "Bash":
                position += 1
                continue
            call_id = call[0]
            if not self._is_call_output(result, call_id):
                position += 1
                continue
            initial_output = result.payload.get("output")
            match = (
                _RUNNING_CELL_RE.search(initial_output)
                if isinstance(initial_output, str)
                else None
            )
            if match is None:
                position += 1
                continue

            cell_id = match.group(1)
            cursor = position + 2
            chunks: list[str] = []
            continuation_indices: list[int] = []
            session_id: Optional[int] = None
            while cursor + 1 < len(tool_records):
                next_call_index = tool_records[cursor]
                next_result_index = tool_records[cursor + 1]
                next_call = self._adapted_call(records[next_call_index])
                if next_call is None or not self._is_call_output(
                    records[next_result_index], next_call[0]
                ):
                    break
                name, input_data = next_call[1].name, next_call[1].input
                matches_wait = name == "wait" and str(input_data.get("cell_id")) == cell_id
                matches_write = (
                    name == "write_stdin"
                    and session_id is not None
                    and input_data.get("session_id") == session_id
                )
                if not (matches_wait or matches_write):
                    break
                envelope = self._command_result(
                    records[next_result_index].payload.get("output")
                )
                if envelope is None:
                    break
                output = envelope.get("output")
                if isinstance(output, str) and output:
                    chunks.append(output)
                raw_session_id = envelope.get("session_id")
                if isinstance(raw_session_id, int):
                    session_id = raw_session_id
                continuation_indices.extend([next_call_index, next_result_index])
                cursor += 2
                if isinstance(envelope.get("exit_code"), int):
                    break

            if chunks and continuation_indices:
                payload = dict(result.payload)
                payload["output"] = "".join(chunks)
                replacements[result_index] = _DecodedRecord(
                    line_no=result.line_no,
                    timestamp=result.timestamp,
                    kind=result.kind,
                    payload=payload,
                )
                suppressed.update(continuation_indices)
                position = cursor
            else:
                position += 1

        return [
            replacements.get(index, record)
            for index, record in enumerate(records)
            if index not in suppressed
        ]

    def _adapted_call(
        self, record: _DecodedRecord
    ) -> Optional[tuple[str, Any]]:
        payload_type = self._nonempty_string(record.payload.get("type"))
        if payload_type not in {"function_call", "custom_tool_call"}:
            return None
        call_id = self._nonempty_string(record.payload.get("call_id"))
        if call_id is None:
            return None
        name = self._nonempty_string(record.payload.get("name")) or payload_type
        raw_input = (
            record.payload.get("arguments")
            if payload_type == "function_call"
            else record.payload.get("input")
        )
        return (
            call_id,
            adapt_codex_tool_call(
                name, self._tool_input(raw_input), raw_input=raw_input
            ),
        )

    def _is_call_output(self, record: _DecodedRecord, call_id: str) -> bool:
        return (
            self._nonempty_string(record.payload.get("type"))
            in {"function_call_output", "custom_tool_call_output"}
            and record.payload.get("call_id") == call_id
        )

    def _normalize_record(
        self,
        thread_id: str,
        record: _DecodedRecord,
        model: str,
        preferred: Counter[tuple[str, str]],
        suppressed: Counter[tuple[str, str]],
    ) -> list[_CodexEntry]:
        if record.kind == "event_msg":
            return self._normalize_event(thread_id, record, model)
        if record.kind == "response_item":
            return self._normalize_response(
                thread_id, record, model, preferred, suppressed
            )
        return []

    def _normalize_event(
        self, thread_id: str, record: _DecodedRecord, model: str
    ) -> list[_CodexEntry]:
        payload_type = self._nonempty_string(record.payload.get("type")) or ""
        text = self._event_text(record.payload)
        uuid = self._entry_uuid(thread_id, record.line_no, 0)
        if payload_type == "user_message" and text:
            return [make_user_entry(thread_id, uuid, record.timestamp, text)]
        if payload_type == "agent_message" and text:
            return [
                make_assistant_entry(thread_id, uuid, record.timestamp, model, text)
            ]
        if payload_type in {"agent_reasoning", "reasoning"} and text:
            return [make_thinking_entry(thread_id, uuid, record.timestamp, model, text)]
        return []

    def _normalize_response(
        self,
        thread_id: str,
        record: _DecodedRecord,
        model: str,
        preferred: Counter[tuple[str, str]],
        suppressed: Counter[tuple[str, str]],
    ) -> list[_CodexEntry]:
        payload = record.payload
        payload_type = self._nonempty_string(payload.get("type")) or ""
        uuid = self._entry_uuid(thread_id, record.line_no, 0)

        if payload_type == "message":
            role = self._nonempty_string(payload.get("role")) or ""
            text = self._message_text(payload.get("content"))
            normalized_role = (
                "user" if role == "user" else "assistant" if role == "assistant" else ""
            )
            fingerprint = (normalized_role, text)
            if (
                normalized_role
                and text
                and suppressed[fingerprint] < preferred[fingerprint]
            ):
                suppressed[fingerprint] += 1
                return []
            if normalized_role == "user" and text:
                return [make_user_entry(thread_id, uuid, record.timestamp, text)]
            if normalized_role == "assistant" and text:
                return [
                    make_assistant_entry(thread_id, uuid, record.timestamp, model, text)
                ]
            # Developer/system messages are context, not model reasoning.
            return []

        if payload_type == "reasoning":
            summary = self._reasoning_summary(payload)
            if summary:
                return [
                    make_thinking_entry(
                        thread_id, uuid, record.timestamp, model, summary
                    )
                ]
            return []

        if payload_type in {"function_call", "custom_tool_call"}:
            call_id = self._nonempty_string(payload.get("call_id")) or uuid
            name = self._nonempty_string(payload.get("name")) or payload_type
            raw_input = (
                payload.get("arguments")
                if payload_type == "function_call"
                else payload.get("input")
            )
            tool_input = self._tool_input(raw_input)
            adapted = adapt_codex_tool_call(
                name,
                tool_input,
                raw_input=raw_input,
            )
            return [
                make_tool_use_entry(
                    thread_id,
                    uuid,
                    record.timestamp,
                    model,
                    call_id,
                    adapted.name,
                    adapted.input,
                )
            ]

        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = self._nonempty_string(payload.get("call_id")) or uuid
            output = self._tool_output(payload.get("output", ""))
            return [
                UserTranscriptEntry(
                    type="user",
                    parentUuid=None,
                    isSidechain=False,
                    userType="external",
                    cwd="",
                    sessionId=thread_id,
                    version="",
                    uuid=uuid,
                    timestamp=record.timestamp,
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
            ]
        return []

    def _event_message_fingerprint(
        self, payload: dict[str, Any]
    ) -> Optional[tuple[str, str]]:
        payload_type = self._nonempty_string(payload.get("type"))
        role = (
            "user"
            if payload_type == "user_message"
            else "assistant"
            if payload_type == "agent_message"
            else None
        )
        text = self._event_text(payload)
        return (role, text) if role and text else None

    def _event_text(self, payload: dict[str, Any]) -> str:
        value = payload.get("message", payload.get("text", ""))
        return value if isinstance(value, str) else self._message_text(value)

    def _message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for raw_item in cast(list[Any], content):
            if isinstance(raw_item, str):
                parts.append(raw_item)
            elif isinstance(raw_item, dict):
                item = cast(dict[str, Any], raw_item)
                item_type = item.get("type")
                if item_type in {"input_text", "output_text", "text"}:
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(parts)

    def _reasoning_summary(self, payload: dict[str, Any]) -> str:
        # encrypted_content is intentionally never inspected or emitted.
        summary = payload.get("summary")
        if isinstance(summary, str):
            return summary
        if not isinstance(summary, list):
            return ""
        parts: list[str] = []
        for raw_item in cast(list[Any], summary):
            if isinstance(raw_item, str):
                parts.append(raw_item)
            elif isinstance(raw_item, dict):
                text = cast(dict[str, Any], raw_item).get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)

    def _tool_input(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        if isinstance(value, str):
            try:
                parsed: Any = json.loads(value)
            except json.JSONDecodeError:
                return {"raw": value}
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"input": parsed}
        return {"input": value}

    def _tool_output(self, value: Any) -> str | list[dict[str, Any]]:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            items = cast(list[Any], value)
            if all(isinstance(item, dict) for item in items):
                structured = cast(list[dict[str, Any]], items)
                command_output = self._command_output(structured)
                if command_output is not None:
                    return command_output
                return structured
        try:
            return json.dumps(cast(Any, value), ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(cast(object, value))

    def _command_output(self, items: list[dict[str, Any]]) -> Optional[str]:
        """Unwrap the Codex ``exec_command`` result envelope.

        Unified command results are persisted as a short status item followed
        by an ``input_text`` item whose text is a JSON object.  Keeping that
        transport wrapper makes the shared renderer treat a Bash result as a
        generic structured value.  Recognize only the characteristic command
        envelope and return its stdout/stderr payload; other structured tool
        results retain their original representation.
        """
        result = self._command_result(items)
        output = result.get("output") if result is not None else None
        return output if isinstance(output, str) else None

    def _command_result(self, value: Any) -> Optional[dict[str, Any]]:
        if not isinstance(value, list):
            return None
        items = cast(list[Any], value)
        for raw_item in reversed(items):
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            if item.get("type") not in {"input_text", "output_text", "text"}:
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            try:
                decoded: Any = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(decoded, dict):
                continue
            result = cast(dict[str, Any], decoded)
            output = result.get("output")
            if (
                isinstance(output, str)
                and (
                    isinstance(result.get("exit_code"), int)
                    or isinstance(result.get("session_id"), int)
                )
                and (
                    "wall_time_seconds" in result
                    or "original_token_count" in result
                    or "chunk_id" in result
                )
            ):
                return result
        return None

    def _source_metadata(
        self, source: dict[str, Any]
    ) -> tuple[Optional[str], Optional[str]]:
        # Source has appeared both as {"subagent": {"thread_spawn": ...}}
        # and as a shallow tagged mapping. Retain the kind and spawning item
        # without making lineage recognition depend on one exact version.
        if not source:
            return None, None
        kind = self._nonempty_string(source.get("type"))
        spawn_call_id = self._nonempty_string(source.get("spawn_call_id"))
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            subagent_dict = cast(dict[str, Any], subagent)
            kind = kind or "subagent"
            spawn = subagent_dict.get("thread_spawn")
            if isinstance(spawn, dict):
                spawn_dict = cast(dict[str, Any], spawn)
                kind = "subagent"
                spawn_call_id = (
                    self._nonempty_string(spawn_dict.get("spawn_call_id"))
                    or self._nonempty_string(spawn_dict.get("call_id"))
                    or self._nonempty_string(spawn_dict.get("item_id"))
                    or spawn_call_id
                )
        if kind is None and len(source) == 1:
            kind = str(next(iter(source)))
        return kind, spawn_call_id

    def _source_string(self, source: dict[str, Any], key: str) -> Optional[str]:
        """Find a lineage field in the small nested source-tag structure."""
        direct = self._nonempty_string(source.get(key))
        if direct:
            return direct
        for value in source.values():
            if isinstance(value, dict):
                found = self._source_string(cast(dict[str, Any], value), key)
                if found:
                    return found
        return None

    def _entry_uuid(self, thread_id: str, line_no: int, subindex: int) -> str:
        return f"codex-{thread_id}-{line_no}-{subindex}"

    def _nonempty_string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

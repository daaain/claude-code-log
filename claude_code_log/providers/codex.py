"""Codex CLI rollout session provider.

The rollout format is an implementation detail of Codex rather than a stable
file-format API.  Parsing here is deliberately tolerant: the provider keeps
the raw-record decoder small, ignores unknown records, and normalizes only
shapes for which it has useful semantics.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
from typing import Any, Iterator, Optional, TypeAlias, cast

from claude_code_log.models import (
    AssistantTranscriptEntry,
    ContentItem,
    ImageContent,
    ImageSource,
    TextContent,
    ToolResultContent,
    ToolUseResult,
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
    make_tool_result_entry,
    make_tool_use_entry,
    make_user_entry,
)
from .codex_tools import AdaptedToolCall, adapt_codex_tool_batch, adapt_codex_tool_call
from .codex_javascript import analyze_javascript_tools
from .codex_messages import format_codex_user_message, parse_codex_user_shell_command

logger = logging.getLogger(__name__)

_CodexEntry: TypeAlias = UserTranscriptEntry | AssistantTranscriptEntry

_ROLLOUT_GLOB = "rollout-*.jsonl"
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
_FILENAME_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_RUNNING_CELL_RE = re.compile(r"Script running with cell ID ([^\s]+)")
_COMPLETED_COMMAND_RE = re.compile(
    r"\AScript completed\r?\nWall time:? [^\r\n]+\r?\nOutput:\r?\n?\Z"
)
_IMAGE_TAG_RE = re.compile(r"</?image(?:\s[^>]*)?>", re.IGNORECASE)
_IMAGE_OPEN_TAG_RE = re.compile(r"<image(?P<attributes>\s[^>]*)?>", re.IGNORECASE)
_IMAGE_NAME_RE = re.compile(
    r"\bname\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|"
    r"(?P<bare>\[Image\s+#[^\]]+\]|[^\s>]+))",
    re.IGNORECASE,
)
_IMAGE_PATH_RE = re.compile(
    r"\bpath\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|"
    r"(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_IMAGE_MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_MAX_JSON_NESTING = 512


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


@dataclass(frozen=True)
class _WebOpenItem:
    ref_id: str
    result: str
    result_timestamp: str


@dataclass(frozen=True)
class _ToolBatch:
    calls: list[AdaptedToolCall]
    results: list[str]
    result_timestamp: str


@dataclass(frozen=True)
class _SessionMarkerOutput:
    output: str
    session_id: Optional[int]


@dataclass(frozen=True)
class _SessionMarkerProgram:
    call_index: int
    result_index: int
    calls: list[AdaptedToolCall]
    results: list[_SessionMarkerOutput]
    output_mode: str


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
        sessions_root = sessions_dir.resolve()
        paths: set[Path] = set()
        for path in sessions_dir.rglob(_ROLLOUT_GLOB):
            try:
                resolved = path.resolve()
                if path.is_file() and resolved.is_relative_to(sessions_root):
                    paths.add(resolved)
            except OSError:
                continue
        return sorted(paths)

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
        if prefix_length == 0 and identity.spawn_call_id:
            boundaries = [
                index + 1
                for index, record in enumerate(parent_records)
                if record.payload.get("call_id") == identity.spawn_call_id
                and record.payload.get("type") in {"function_call", "custom_tool_call"}
            ]
            if len(boundaries) == 1:
                prefix_length = self._prefix_length_at_parent_boundary(
                    child_records, parent_records, boundaries[0]
                )
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
        """Find a strong leading-child match at the end of its parent."""
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
            if start + length == len(parent_records) and length >= 2:
                best = max(best, length)
        return best

    def _prefix_length_at_parent_boundary(
        self,
        child_records: list[_DecodedRecord],
        parent_records: list[_DecodedRecord],
        boundary: int,
    ) -> int:
        """Match copied history ending at an explicit stable fork item."""
        best = 0
        for start in range(boundary):
            length = boundary - start
            if length < 2 or length > len(child_records):
                continue
            if all(
                self._same_semantic_record(
                    child_records[offset], parent_records[start + offset]
                )
                for offset in range(length)
            ):
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
                except (ValueError, RecursionError):
                    logger.warning(
                        "Malformed JSON in Codex rollout %s line %d", path, line_no
                    )
                    continue
                if self._json_nesting_exceeds(raw, _MAX_JSON_NESTING):
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
        records = self._deduplicate_visible_messages(records)
        records = self._coalesce_exec_wrapper_cells(records)
        records = self._coalesce_command_sessions(records)
        records = self._coalesce_marker_command_sessions(records)
        tool_names = self._adapted_tool_names(records)
        web_open_batches = self._web_open_batches(records)
        tool_batches = self._tool_batches(records)
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
                identity.thread_id,
                record,
                model,
                tool_names,
                web_open_batches,
                tool_batches,
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
                if isinstance(entry, AssistantTranscriptEntry):
                    entry.message.id = entry.uuid
                    entry.message.model = model
                parent_uuid = entry.uuid
                emitted += 1
                yield entry

    def _deduplicate_visible_messages(
        self, records: list[_DecodedRecord]
    ) -> list[_DecodedRecord]:
        """Collapse adjacent event/response mirrors without losing images."""
        suppressed: set[int] = set()
        for index in range(len(records) - 1):
            left = records[index]
            right = records[index + 1]
            fingerprint = self._visible_message_fingerprint(left)
            if {left.kind, right.kind} != {"event_msg", "response_item"}:
                continue
            if (
                fingerprint is not None
                and fingerprint == self._visible_message_fingerprint(right)
            ):
                suppressed.add(index if left.kind == "response_item" else index + 1)
                continue
            image_mirror = self._image_message_mirror(left, right)
            if image_mirror is not None:
                suppressed.add(index + image_mirror)
        return [
            record for index, record in enumerate(records) if index not in suppressed
        ]

    def _image_message_mirror(
        self, left: _DecodedRecord, right: _DecodedRecord
    ) -> Optional[int]:
        """Return the event-side index for a richer image response mirror."""
        event_index = 0 if left.kind == "event_msg" else 1
        event = left if event_index == 0 else right
        response = right if event_index == 0 else left
        if (
            event.payload.get("type") != "user_message"
            or response.payload.get("type") != "message"
            or response.payload.get("role") != "user"
        ):
            return None
        event_text = self._event_text(event.payload)
        response_text = self._image_message_text(response.payload.get("content"))
        return event_index if event_text and event_text == response_text else None

    def _image_message_text(self, content: Any) -> Optional[str]:
        """Extract text from a response message known to contain an image."""
        if not isinstance(content, list):
            return None
        items = cast(list[Any], content)
        parts: list[str] = []
        has_image = False
        for raw_item in items:
            if isinstance(raw_item, str):
                parts.append(raw_item)
                continue
            if not isinstance(raw_item, dict):
                continue
            item = cast(dict[str, Any], raw_item)
            if item.get("type") == "input_image":
                has_image = True
                continue
            if item.get("type") not in {"input_text", "output_text", "text"}:
                continue
            text = item.get("text")
            if isinstance(text, str):
                cleaned = _IMAGE_TAG_RE.sub("", text)
                if cleaned:
                    parts.append(cleaned)
        return "\n".join(parts) if has_image else None

    def _coalesce_exec_wrapper_cells(
        self, records: list[_DecodedRecord]
    ) -> list[_DecodedRecord]:
        """Remove outer ``exec`` cell polling around a completed JS wrapper."""
        tool_records = self._tool_record_indexes(records)
        suppressed: set[int] = set()
        replacements: dict[int, _DecodedRecord] = {}
        position = 0
        while position + 3 < len(tool_records):
            call_index, result_index, wait_index, wait_result_index = tool_records[
                position : position + 4
            ]
            call = records[call_index]
            result = records[result_index]
            wait = self._adapted_call(records[wait_index])
            call_id = self._nonempty_string(call.payload.get("call_id"))
            raw_output = result.payload.get("output")
            match = (
                _RUNNING_CELL_RE.search(raw_output)
                if isinstance(raw_output, str)
                else None
            )
            if (
                call.payload.get("type") != "custom_tool_call"
                or call.payload.get("name") != "exec"
                or call_id is None
                or not self._is_call_output(result, call_id)
                or match is None
                or wait is None
                or wait[1].name != "wait"
                or str(wait[1].input.get("cell_id")) != match.group(1)
                or not self._is_call_output(records[wait_result_index], wait[0])
                or not self._only_invisible_between(records, result_index, wait_index)
                or not self._only_invisible_between(
                    records, wait_index, wait_result_index
                )
            ):
                position += 1
                continue

            completed = records[wait_result_index].payload.get("output")
            if not self._completed_wrapper_output(completed):
                position += 1
                continue
            # A serialized command result still belongs to the inner Bash
            # session and is handled by _coalesce_command_sessions instead.
            if self._command_result(completed) is not None:
                position += 1
                continue

            payload = dict(result.payload)
            payload["output"] = completed
            replacements[result_index] = _DecodedRecord(
                line_no=result.line_no,
                timestamp=result.timestamp,
                kind=result.kind,
                payload=payload,
            )
            suppressed.update({wait_index, wait_result_index})
            position += 4

        return [
            replacements.get(index, record)
            for index, record in enumerate(records)
            if index not in suppressed
        ]

    def _completed_wrapper_output(self, value: Any) -> bool:
        if not isinstance(value, list) or not value:
            return False
        first = cast(list[Any], value)[0]
        if not isinstance(first, dict):
            return False
        item = cast(dict[str, Any], first)
        text = item.get("text")
        return (
            item.get("type") in {"input_text", "output_text", "text"}
            and isinstance(text, str)
            and _COMPLETED_COMMAND_RE.fullmatch(text) is not None
        )

    def _tool_record_indexes(self, records: list[_DecodedRecord]) -> list[int]:
        return [
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
        tool_records = self._tool_record_indexes(records)
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
            if not self._only_invisible_between(
                records, call_index, result_index
            ) or not self._is_call_output(result, call_id):
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
            previous_result_index = result_index
            terminal = False
            terminal_exit_code: Optional[int] = None
            while cursor + 1 < len(tool_records):
                next_call_index = tool_records[cursor]
                next_result_index = tool_records[cursor + 1]
                next_call = self._adapted_call(records[next_call_index])
                if (
                    not self._only_invisible_between(
                        records, previous_result_index, next_call_index
                    )
                    or not self._only_invisible_between(
                        records, next_call_index, next_result_index
                    )
                    or next_call is None
                    or not self._is_call_output(
                        records[next_result_index], next_call[0]
                    )
                ):
                    break
                name, input_data = next_call[1].name, next_call[1].input
                matches_wait = (
                    name == "wait" and str(input_data.get("cell_id")) == cell_id
                )
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
                exit_code = envelope.get("exit_code")
                if isinstance(exit_code, int):
                    terminal_exit_code = exit_code
                    terminal = True
                    break
                previous_result_index = next_result_index

            if terminal and continuation_indices:
                payload = dict(result.payload)
                payload["output"] = "".join(chunks)
                payload["is_error"] = terminal_exit_code != 0
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

    def _coalesce_marker_command_sessions(
        self, records: list[_DecodedRecord]
    ) -> list[_DecodedRecord]:
        """Fold parallel ``SESSION_ID=`` polling back into its Bash batch."""
        tool_records = self._tool_record_indexes(records)
        programs: dict[int, _SessionMarkerProgram] = {}
        position = 0
        while position + 1 < len(tool_records):
            call_index, result_index = tool_records[position : position + 2]
            if self._only_invisible_between(records, call_index, result_index):
                program = self._session_marker_program(
                    records, call_index, result_index
                )
                if program is not None:
                    programs[position] = program
            position += 2

        suppressed: set[int] = set()
        replacements: dict[int, _DecodedRecord] = {}
        for origin_position, origin in programs.items():
            if origin.output_mode != "markers" or not all(
                call.name == "Bash" for call in origin.calls
            ):
                continue
            live: dict[int, int] = {}
            chunks = [[result.output] for result in origin.results]
            valid = True
            for index, result in enumerate(origin.results):
                if result.session_id is None:
                    continue
                if result.session_id in live:
                    valid = False
                    break
                live[result.session_id] = index
            if not valid or not live:
                continue

            consumed: list[tuple[int, int]] = []
            previous_result = origin.result_index
            cursor = origin_position + 2
            while live and cursor + 1 < len(tool_records):
                poll = programs.get(cursor)
                if (
                    poll is None
                    or not self._only_invisible_between(
                        records, previous_result, poll.call_index
                    )
                    or not all(call.name == "write_stdin" for call in poll.calls)
                ):
                    valid = False
                    break

                requested: list[int] = []
                for call in poll.calls:
                    session_id = call.input.get("session_id")
                    if not isinstance(session_id, int):
                        valid = False
                        break
                    requested.append(session_id)
                if (
                    not valid
                    or len(set(requested)) != len(requested)
                    or any(session_id not in live for session_id in requested)
                ):
                    valid = False
                    break

                for session_id, result in zip(requested, poll.results):
                    origin_index = live.pop(session_id)
                    chunks[origin_index].append(result.output)
                    if result.session_id is not None:
                        if result.session_id in live:
                            valid = False
                            break
                        live[result.session_id] = origin_index
                if not valid:
                    break
                consumed.append((poll.call_index, poll.result_index))
                previous_result = poll.result_index
                cursor += 2

            if not valid or live or not consumed:
                continue

            original_output = records[origin.result_index].payload.get("output")
            status = self._first_text_item(original_output)
            if status is None:
                continue
            rewritten: list[dict[str, str]] = [status]
            for index, parts in enumerate(chunks, 1):
                rewritten.append({"type": "input_text", "text": f"RESULT_{index}"})
                rewritten.append({"type": "input_text", "text": "".join(parts)})
            result = records[origin.result_index]
            payload = dict(result.payload)
            payload["output"] = rewritten
            replacements[origin.result_index] = _DecodedRecord(
                line_no=result.line_no,
                timestamp=result.timestamp,
                kind=result.kind,
                payload=payload,
            )
            for call_index, result_index in consumed:
                suppressed.update({call_index, result_index})

        return [
            replacements.get(index, record)
            for index, record in enumerate(records)
            if index not in suppressed
        ]

    def _session_marker_program(
        self, records: list[_DecodedRecord], call_index: int, result_index: int
    ) -> Optional[_SessionMarkerProgram]:
        call_record = records[call_index]
        result_record = records[result_index]
        if (
            call_record.payload.get("type") != "custom_tool_call"
            or call_record.payload.get("name") != "exec"
        ):
            return None
        call_id = self._nonempty_string(call_record.payload.get("call_id"))
        source = call_record.payload.get("input")
        if (
            call_id is None
            or not isinstance(source, str)
            or not self._is_call_output(result_record, call_id)
        ):
            return None
        analyzed = analyze_javascript_tools(source)
        if analyzed is None or not analyzed.session_markers:
            return None
        parsed = self._session_marker_outputs(
            result_record.payload.get("output"),
            analyzed.output_mode,
            len(analyzed.calls),
        )
        if parsed is None:
            return None
        calls = [
            adapt_codex_tool_call(call.name, call.input) for call in analyzed.calls
        ]
        return _SessionMarkerProgram(
            call_index=call_index,
            result_index=result_index,
            calls=calls,
            results=[parsed[index] for index in analyzed.result_indexes],
            output_mode=analyzed.output_mode,
        )

    def _session_marker_outputs(
        self, value: Any, output_mode: str, expected: int
    ) -> Optional[list[_SessionMarkerOutput]]:
        texts = self._text_items(value)
        if (
            texts is None
            or len(texts) < 2
            or not texts[0].startswith("Script completed")
        ):
            return None
        if output_mode == "ordered":
            if expected != 1:
                return None
            parsed = self._session_marker_group(texts[1:])
            return [parsed] if parsed is not None else None

        groups: list[list[str]] = []
        for text in texts[1:]:
            marker = re.fullmatch(r"RESULT_([1-9][0-9]*)", text)
            if marker is not None:
                if int(marker.group(1)) != len(groups) + 1:
                    return None
                groups.append([])
            elif not groups:
                return None
            else:
                groups[-1].append(text)
        if len(groups) != expected:
            return None
        parsed_groups = [self._session_marker_group(group) for group in groups]
        return (
            cast(list[_SessionMarkerOutput], parsed_groups)
            if all(group is not None for group in parsed_groups)
            else None
        )

    def _session_marker_group(self, texts: list[str]) -> Optional[_SessionMarkerOutput]:
        session_id: Optional[int] = None
        chunks: list[str] = []
        for text in texts:
            marker = re.fullmatch(r"SESSION_ID=([1-9][0-9]*)", text)
            if marker is None:
                chunks.append(text)
                continue
            if session_id is not None:
                return None
            session_id = int(marker.group(1))
        return _SessionMarkerOutput("".join(chunks), session_id)

    def _text_items(self, value: Any) -> Optional[list[str]]:
        if not isinstance(value, list):
            return None
        texts: list[str] = []
        for raw_item in cast(list[Any], value):
            if not isinstance(raw_item, dict):
                return None
            item = cast(dict[str, Any], raw_item)
            text = item.get("text")
            if item.get("type") not in {
                "input_text",
                "output_text",
                "text",
            } or not isinstance(text, str):
                return None
            texts.append(text)
        return texts

    def _first_text_item(self, value: Any) -> Optional[dict[str, str]]:
        if not isinstance(value, list) or not value:
            return None
        first = cast(list[Any], value)[0]
        if not isinstance(first, dict):
            return None
        item = cast(dict[str, Any], first)
        text = item.get("text")
        item_type = item.get("type")
        if item_type not in {"input_text", "output_text", "text"} or not isinstance(
            text, str
        ):
            return None
        return {"type": cast(str, item_type), "text": text}

    def _only_invisible_between(
        self, records: list[_DecodedRecord], left: int, right: int
    ) -> bool:
        return all(
            self._is_ignorable_command_interstitial(record)
            for record in records[left + 1 : right]
        )

    def _is_ignorable_command_interstitial(self, record: _DecodedRecord) -> bool:
        """Whether a non-tool record may sit inside one command poll chain."""
        if record.kind == "session_meta":
            return True
        if record.kind == "event_msg":
            # Token accounting is emitted after nearly every model/tool step.
            # Task boundaries and other events remain barriers even when the
            # renderer currently ignores them.
            return record.payload.get("type") == "token_count"
        if record.kind != "response_item":
            return False

        payload_type = record.payload.get("type")
        if payload_type == "reasoning":
            return not self._reasoning_summary(record.payload)
        if payload_type == "message":
            # Approval bookkeeping is persisted as developer context between
            # a timed-out command result and its first wait call.  User and
            # assistant messages are visible and must break correlation.
            return record.payload.get("role") not in {"user", "assistant"}
        return False

    def _adapted_call(self, record: _DecodedRecord) -> Optional[tuple[str, Any]]:
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

    def _adapted_tool_names(self, records: list[_DecodedRecord]) -> dict[str, str]:
        """Index canonical tool names for result-side normalization."""
        names: dict[str, str] = {}
        for record in records:
            adapted = self._adapted_call(record)
            if adapted is not None:
                names[adapted[0]] = adapted[1].name
        return names

    def _web_open_batches(
        self, records: list[_DecodedRecord]
    ) -> dict[str, list[_WebOpenItem]]:
        """Find open-only web batches whose results split without guessing."""
        requests: dict[str, list[str]] = {}
        outputs: dict[str, tuple[str, str]] = {}
        for record in records:
            adapted = self._adapted_call(record)
            if adapted is not None and adapted[1].name == "web__run":
                call_id, call = adapted
                other_actions = set(call.input) - {"open", "response_length"}
                raw_open = call.input.get("open")
                if other_actions or not isinstance(raw_open, list):
                    continue
                open_items = cast(list[Any], raw_open)
                refs: list[str] = []
                for raw_item in open_items:
                    if not isinstance(raw_item, dict):
                        break
                    ref_id = cast(dict[str, Any], raw_item).get("ref_id")
                    if not isinstance(ref_id, str):
                        break
                    refs.append(ref_id)
                if refs and len(refs) == len(open_items):
                    requests[call_id] = refs
                continue

            payload_type = self._nonempty_string(record.payload.get("type"))
            call_id = self._nonempty_string(record.payload.get("call_id"))
            if call_id is None or payload_type not in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                continue
            value = record.payload.get("output")
            if isinstance(value, str):
                outputs[call_id] = (value, record.timestamp)
            elif isinstance(value, list) and all(
                isinstance(item, dict) for item in cast(list[Any], value)
            ):
                text = self._command_output(cast(list[dict[str, Any]], value))
                if text is not None:
                    outputs[call_id] = (text, record.timestamp)

        batches: dict[str, list[_WebOpenItem]] = {}
        for call_id, refs in requests.items():
            output = outputs.get(call_id)
            if output is None:
                continue
            text, timestamp = output
            chunks = re.split(r"\r?\n-{40,}\r?\n", text)
            if len(chunks) != len(refs):
                continue
            batches[call_id] = [
                _WebOpenItem(
                    ref_id=ref_id,
                    result=chunk.strip("\r\n"),
                    result_timestamp=timestamp,
                )
                for ref_id, chunk in zip(refs, chunks)
            ]
        return batches

    def _tool_batches(self, records: list[_DecodedRecord]) -> dict[str, _ToolBatch]:
        """Correlate static multi-tool programs with their output groups."""
        requests: dict[
            str,
            tuple[
                list[AdaptedToolCall],
                str,
                list[int],
                bool,
                tuple[Optional[str], ...],
                tuple[Optional[str], ...],
                Optional[int],
            ],
        ] = {}
        outputs: dict[str, tuple[list[dict[str, Any]], str]] = {}
        for record in records:
            payload_type = self._nonempty_string(record.payload.get("type"))
            call_id = self._nonempty_string(record.payload.get("call_id"))
            if call_id is None:
                continue
            if (
                payload_type == "custom_tool_call"
                and record.payload.get("name") == "exec"
            ):
                source = record.payload.get("input")
                if isinstance(source, str):
                    batch = adapt_codex_tool_batch(source)
                    if batch is not None:
                        requests[call_id] = (
                            batch.calls,
                            batch.output_mode,
                            batch.result_indexes,
                            batch.session_markers,
                            batch.result_prefixes,
                            batch.synthetic_results,
                            batch.output_count,
                        )
            elif payload_type in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                output = record.payload.get("output")
                if isinstance(output, list) and all(
                    isinstance(item, dict) for item in cast(list[Any], output)
                ):
                    outputs[call_id] = (
                        cast(list[dict[str, Any]], output),
                        record.timestamp,
                    )

        batches: dict[str, _ToolBatch] = {}
        for call_id, (
            calls,
            output_mode,
            result_indexes,
            session_markers,
            result_prefixes,
            synthetic_results,
            output_count,
        ) in requests.items():
            output = outputs.get(call_id)
            if output is None:
                continue
            if session_markers and self._contains_session_marker(output[0]):
                continue
            split = self._batch_outputs(
                output[0],
                output_mode,
                output_count if output_count is not None else len(calls),
                result_prefixes,
            )
            if split is None:
                continue
            results = [
                synthetic if synthetic is not None else split[result_indexes[index]]
                for index, synthetic in enumerate(synthetic_results)
            ]
            status = self._empty_result_status(output[0])
            if status is not None:
                results = [
                    status
                    if call.name in {"Write", "Edit", "MultiEdit"}
                    and result.strip() == "{}"
                    else result
                    for call, result in zip(calls, results)
                ]
            batches[call_id] = _ToolBatch(
                calls=calls,
                results=results,
                result_timestamp=output[1],
            )
        return batches

    def _contains_session_marker(self, items: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(text := item.get("text"), str)
            and re.fullmatch(r"SESSION_ID=[1-9][0-9]*", text) is not None
            for item in items
        )

    def _batch_outputs(
        self,
        items: list[dict[str, Any]],
        output_mode: str,
        expected: int,
        result_prefixes: tuple[Optional[str], ...] = (),
    ) -> Optional[list[str]]:
        texts: list[str] = []
        for item in items:
            if item.get("type") not in {"input_text", "output_text", "text"}:
                return None
            text = item.get("text")
            if not isinstance(text, str):
                return None
            texts.append(text)
        if not texts or not texts[0].startswith("Script completed"):
            return None
        if output_mode == "ordered":
            if len(texts) == expected + 1:
                return texts[1:]
            if len(texts) == 2 and len(result_prefixes) == expected:
                return self._split_prefixed_batch_output(texts[1], result_prefixes)
            return None

        groups: list[list[str]] = []
        for text in texts[1:]:
            marker = re.fullmatch(r"RESULT_([1-9][0-9]*)", text)
            if marker is not None:
                if int(marker.group(1)) != len(groups) + 1:
                    return None
                groups.append([])
                continue
            if not groups:
                return None
            if groups[-1] and groups[-1][-1] and not groups[-1][-1].endswith("\n"):
                groups[-1].append("\n")
            groups[-1].append(text)
        return ["".join(group) for group in groups] if len(groups) == expected else None

    def _split_prefixed_batch_output(
        self, output: str, prefixes: tuple[Optional[str], ...]
    ) -> Optional[list[str]]:
        """Split consolidated emissions on their distinct static prefixes."""
        if not prefixes or any(not prefix for prefix in prefixes):
            return None
        concrete = cast(tuple[str, ...], prefixes)
        if len(set(concrete)) != len(concrete):
            return None

        positions = [output.find(prefix) for prefix in concrete]
        if any(
            position >= 0 and output.count(prefix) != 1
            for prefix, position in zip(concrete, positions)
        ):
            return None
        found = [
            (index, position)
            for index, position in enumerate(positions)
            if position >= 0
        ]
        if len(found) < 2 or any(
            left[1] >= right[1] for left, right in zip(found, found[1:])
        ):
            return None
        if len(found) != len(concrete) and not output.startswith(
            "Warning: truncated output"
        ):
            return None

        results = ["[Output omitted by Codex truncation]" for _ in concrete]
        for found_index, (result_index, position) in enumerate(found):
            start = 0 if found_index == 0 else position
            end = (
                found[found_index + 1][1]
                if found_index + 1 < len(found)
                else len(output)
            )
            results[result_index] = output[start:end].rstrip("\n")
        return results

    def _normalize_record(
        self,
        thread_id: str,
        record: _DecodedRecord,
        model: str,
        tool_names: dict[str, str],
        web_open_batches: dict[str, list[_WebOpenItem]],
        tool_batches: dict[str, _ToolBatch],
    ) -> list[_CodexEntry]:
        if record.kind == "event_msg":
            return self._normalize_event(thread_id, record, model)
        if record.kind == "response_item":
            return self._normalize_response(
                thread_id,
                record,
                model,
                tool_names,
                web_open_batches,
                tool_batches,
            )
        return []

    def _normalize_event(
        self, thread_id: str, record: _DecodedRecord, model: str
    ) -> list[_CodexEntry]:
        payload_type = self._nonempty_string(record.payload.get("type")) or ""
        text = self._event_text(record.payload)
        uuid = self._entry_uuid(thread_id, record.line_no, 0)
        if payload_type == "user_message" and text:
            return self._normalize_user_text(thread_id, uuid, record.timestamp, text)
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
        tool_names: dict[str, str],
        web_open_batches: dict[str, list[_WebOpenItem]],
        tool_batches: dict[str, _ToolBatch],
    ) -> list[_CodexEntry]:
        payload = record.payload
        payload_type = self._nonempty_string(payload.get("type")) or ""
        uuid = self._entry_uuid(thread_id, record.line_no, 0)

        if payload_type == "message":
            role = self._nonempty_string(payload.get("role")) or ""
            content = payload.get("content")
            text = self._message_text(content)
            normalized_role = (
                "user" if role == "user" else "assistant" if role == "assistant" else ""
            )
            if normalized_role == "user":
                image_entry = self._normalize_user_images(
                    thread_id, uuid, record.timestamp, content
                )
                if image_entry is not None:
                    return [image_entry]
            if normalized_role == "user" and text:
                return self._normalize_user_text(
                    thread_id, uuid, record.timestamp, text
                )
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
            tool_batch = tool_batches.get(call_id)
            if tool_batch is not None:
                expanded: list[_CodexEntry] = []
                for index, (call, result) in enumerate(
                    zip(tool_batch.calls, tool_batch.results)
                ):
                    derived_id = f"{call_id}:batch:{index}"
                    expanded.append(
                        make_tool_use_entry(
                            thread_id,
                            uuid,
                            record.timestamp,
                            model,
                            derived_id,
                            call.name,
                            call.input,
                        )
                    )
                    raw_result: Any = result
                    is_error = False
                    forwarded = self._forwarded_tool_emission(result)
                    if forwarded is not None:
                        raw_result, is_error = forwarded
                    output, tool_use_result = self._adapt_tool_result(
                        raw_result, tool_name=call.name, is_error=is_error
                    )
                    rendered_output = (
                        output
                        if isinstance(output, str)
                        else json.dumps(output, ensure_ascii=False)
                    )
                    result_entry = make_tool_result_entry(
                        thread_id,
                        uuid,
                        tool_batch.result_timestamp,
                        derived_id,
                        rendered_output,
                    )
                    result_content = result_entry.message.content[0]
                    if isinstance(result_content, ToolResultContent):
                        result_content.is_error = is_error
                    result_entry.toolUseResult = tool_use_result
                    expanded.append(result_entry)
                return expanded
            batch = web_open_batches.get(call_id)
            if batch is not None:
                expanded: list[_CodexEntry] = []
                for index, item in enumerate(batch):
                    derived_id = f"{call_id}:open:{index}"
                    expanded.append(
                        make_tool_use_entry(
                            thread_id,
                            uuid,
                            record.timestamp,
                            model,
                            derived_id,
                            "WebFetch",
                            {"url": item.ref_id, "prompt": ""},
                        )
                    )
                    expanded.append(
                        make_tool_result_entry(
                            thread_id,
                            uuid,
                            item.result_timestamp,
                            derived_id,
                            item.result,
                        )
                    )
                return expanded
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
            if call_id in web_open_batches or call_id in tool_batches:
                return []
            tool_name = tool_names.get(call_id)
            raw_is_error = payload.get("is_error")
            is_error = raw_is_error if isinstance(raw_is_error, bool) else None
            raw_output = payload.get("output", "")
            forwarded = (
                None
                if tool_name == "Workflow"
                else self._forwarded_tool_result(raw_output)
            )
            if forwarded is not None:
                raw_output, forwarded_is_error = forwarded
                is_error = bool(is_error) or forwarded_is_error
            output, tool_use_result = self._adapt_tool_result(
                raw_output,
                tool_name=tool_name,
                is_error=is_error is True,
            )
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
                    toolUseResult=tool_use_result,
                    message=UserMessageModel(
                        role="user",
                        content=[
                            ToolResultContent(
                                type="tool_result",
                                tool_use_id=call_id,
                                content=output,
                                is_error=is_error,
                            )
                        ],
                    ),
                )
            ]
        return []

    def _normalize_user_text(
        self, thread_id: str, uuid: str, timestamp: str, text: str
    ) -> list[_CodexEntry]:
        shell = parse_codex_user_shell_command(text)
        if shell is not None:
            if shell.exit_code != 0:
                return [make_user_entry(thread_id, uuid, timestamp, text)]
            return [
                make_user_entry(
                    thread_id,
                    uuid,
                    timestamp,
                    f"<bash-input>{shell.command}</bash-input>",
                ),
                make_user_entry(
                    thread_id,
                    uuid,
                    timestamp,
                    f"<bash-stdout>{shell.output}</bash-stdout>",
                ),
            ]
        return [
            make_user_entry(
                thread_id,
                uuid,
                timestamp,
                format_codex_user_message(text),
            )
        ]

    def _normalize_user_images(
        self, thread_id: str, uuid: str, timestamp: str, content: Any
    ) -> Optional[UserTranscriptEntry]:
        """Turn Codex image wrappers into Claude-compatible content blocks."""
        if isinstance(content, str):
            raw_items: list[Any] = [content]
        elif isinstance(content, list):
            raw_items = cast(list[Any], content)
        else:
            return None

        text_parts: list[str] = []
        descriptors: dict[str, Optional[ImageContent]] = {}
        found_tag = False
        for raw_item in raw_items:
            text: Optional[str] = None
            if isinstance(raw_item, str):
                text = raw_item
            elif isinstance(raw_item, dict):
                item = cast(dict[str, Any], raw_item)
                if item.get("type") in {"input_text", "output_text", "text"}:
                    value = item.get("text")
                    text = value if isinstance(value, str) else None
            if text is None:
                continue

            tags = list(_IMAGE_TAG_RE.finditer(text))
            found_tag = found_tag or bool(tags)
            for tag in _IMAGE_OPEN_TAG_RE.finditer(text):
                attributes = tag.group("attributes") or ""
                name = self._image_attribute(_IMAGE_NAME_RE, attributes)
                path = self._image_attribute(_IMAGE_PATH_RE, attributes)
                if name and name not in descriptors:
                    descriptors[name] = self._read_image(path) if path else None

            cleaned = _IMAGE_TAG_RE.sub("", text)
            if cleaned:
                text_parts.append(cleaned)

        if not found_tag:
            return None

        text = format_codex_user_message("\n".join(text_parts))
        items: list[ContentItem] = []
        if descriptors:
            placeholder_re = re.compile(
                "(" + "|".join(re.escape(name) for name in descriptors) + ")"
            )
            for part in placeholder_re.split(text):
                if not part:
                    continue
                if part not in descriptors:
                    self._append_image_text(items, part)
                    continue
                image = descriptors[part]
                if image is not None:
                    items.append(image)
                else:
                    self._append_image_text(items, f"`{part}`")
        elif text:
            items.append(TextContent(type="text", text=text))

        return UserTranscriptEntry(
            type="user",
            parentUuid=None,
            isSidechain=False,
            userType="external",
            cwd="",
            sessionId=thread_id,
            version="",
            uuid=uuid,
            timestamp=timestamp,
            message=UserMessageModel(role="user", content=items),
        )

    def _append_image_text(self, items: list[ContentItem], text: str) -> None:
        if items and isinstance(items[-1], TextContent):
            items[-1].text += text
        else:
            items.append(TextContent(type="text", text=text))

    def _image_attribute(self, pattern: re.Pattern[str], attributes: str) -> str:
        match = pattern.search(attributes)
        if match is None:
            return ""
        return next((value for value in match.groupdict().values() if value), "")

    def _read_image(self, raw_path: str) -> Optional[ImageContent]:
        path = Path(raw_path).expanduser()
        media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            media_type, _ = mimetypes.guess_type(path.name)
        if media_type is None or not media_type.startswith("image/"):
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return ImageContent(
            type="image",
            source=ImageSource(
                type="base64",
                media_type=media_type,
                data=base64.b64encode(data).decode("ascii"),
            ),
        )

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

    def _visible_message_fingerprint(
        self, record: _DecodedRecord
    ) -> Optional[tuple[str, str]]:
        if record.kind == "event_msg":
            return self._event_message_fingerprint(record.payload)
        if record.kind != "response_item" or record.payload.get("type") != "message":
            return None
        role = record.payload.get("role")
        if role not in {"user", "assistant"}:
            return None
        text = self._message_text(record.payload.get("content"))
        return (cast(str, role), text) if text else None

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
            except (ValueError, RecursionError):
                return {"raw": value}
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return {"input": parsed}
        return {"input": value}

    def _tool_output(
        self, value: Any, *, tool_name: Optional[str] = None
    ) -> str | list[dict[str, Any]]:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            items = cast(list[Any], value)
            if all(isinstance(item, dict) for item in items):
                structured = cast(list[dict[str, Any]], items)
                if tool_name in {"Bash", "WebSearch"}:
                    command_output = self._command_output(structured)
                    if command_output is not None:
                        return command_output
                return structured
        try:
            return json.dumps(cast(Any, value), ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(cast(object, value))

    def _adapt_tool_result(
        self,
        value: Any,
        *,
        tool_name: Optional[str],
        is_error: bool,
    ) -> tuple[str | list[dict[str, Any]], Optional[ToolUseResult]]:
        output = self._tool_output(value, tool_name=tool_name)
        if not is_error and tool_name == "Task" and isinstance(output, str):
            output = self._task_acknowledgement(output)
        if not is_error and tool_name == "TodoWrite" and isinstance(output, list):
            acknowledgement = self._todo_acknowledgement(output)
            if acknowledgement is not None:
                output = acknowledgement
        if (
            not is_error
            and tool_name in {"Write", "Edit", "MultiEdit"}
            and isinstance(output, list)
        ):
            status = self._empty_result_status(output)
            if status is not None:
                output = status
        if not is_error and tool_name == "TaskList" and isinstance(output, str):
            task_list = self._list_agents_output(output)
            if task_list is not None:
                output = task_list
        tool_use_result: Optional[ToolUseResult] = None
        if not is_error and tool_name == "WebSearch" and isinstance(output, str):
            tool_use_result = {
                "query": "",
                "results": [{"content": []}, output],
            }
        return output, tool_use_result

    def _list_agents_output(self, content: str) -> Optional[str]:
        try:
            decoded: Any = json.loads(content)
        except (ValueError, RecursionError):
            return None
        if not isinstance(decoded, dict):
            return None
        agents = cast(dict[str, Any], decoded).get("agents")
        if not isinstance(agents, list):
            return None

        rows: list[str] = []
        for index, raw_agent in enumerate(cast(list[Any], agents), 1):
            if not isinstance(raw_agent, dict):
                return None
            agent = cast(dict[str, Any], raw_agent)
            agent_name = agent.get("agent_name")
            raw_status = agent.get("agent_status")
            if not isinstance(agent_name, str):
                return None
            if isinstance(raw_status, str):
                status = raw_status
            elif isinstance(raw_status, dict):
                fields = cast(dict[str, Any], raw_status)
                status = next(iter(fields)) if len(fields) == 1 else "unknown"
            else:
                status = "unknown"
            short_name = agent_name.rstrip("/").rsplit("/", 1)[-1] or agent_name
            last_message = agent.get("last_task_message")
            subject = (
                last_message
                if isinstance(last_message, str) and last_message
                else short_name
            )
            rows.append(f"#{index} [{status}] {subject} ({short_name})")
        return "\n".join(rows) if rows else None

    def _task_acknowledgement(self, content: str) -> str:
        try:
            acknowledgement: Any = json.loads(content)
        except (ValueError, RecursionError):
            return content
        if not isinstance(acknowledgement, dict):
            return content
        acknowledgement_dict = cast(dict[str, Any], acknowledgement)
        if not isinstance(acknowledgement_dict.get("task_name"), str):
            return content
        remainder = dict(acknowledgement_dict)
        remainder.pop("task_name", None)
        if not remainder:
            return ""
        return (
            "```json\n" + json.dumps(remainder, indent=2, ensure_ascii=False) + "\n```"
        )

    def _todo_acknowledgement(self, items: list[dict[str, Any]]) -> Optional[str]:
        return self._empty_result_acknowledgement(items, "Todo list updated.")

    def _empty_result_acknowledgement(
        self, items: list[dict[str, Any]], acknowledgement: str
    ) -> Optional[str]:
        return acknowledgement if self._empty_result_status(items) is not None else None

    def _empty_result_status(self, items: list[dict[str, Any]]) -> Optional[str]:
        texts: list[str] = []
        for item in items:
            if item.get("type") not in {"input_text", "output_text", "text"}:
                return None
            text = item.get("text")
            if not isinstance(text, str):
                return None
            texts.append(text)
        if not any(text.startswith("Script completed") for text in texts):
            return None
        for text in texts:
            try:
                decoded: Any = json.loads(text)
            except (ValueError, RecursionError):
                continue
            if decoded == {}:
                return texts[0]
        return None

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
        if isinstance(output, str):
            return output

        if not items:
            return None
        status = items[0]
        status_text = status.get("text")
        if (
            status.get("type") not in {"input_text", "output_text", "text"}
            or not isinstance(status_text, str)
            or _COMPLETED_COMMAND_RE.fullmatch(status_text) is None
        ):
            return None

        chunks: list[str] = []
        for item in items[1:]:
            if item.get("type") not in {"input_text", "output_text", "text"}:
                return None
            text = item.get("text")
            if not isinstance(text, str):
                return None
            if chunks and chunks[-1] and not chunks[-1].endswith("\n") and text:
                chunks.append("\n")
            chunks.append(text)
        return "".join(chunks)

    def _forwarded_tool_result(
        self, value: Any
    ) -> Optional[tuple[str | list[dict[str, Any]], bool]]:
        """Unwrap a direct nested-tool result emitted by ``functions.exec``.

        A recognized one-tool wrapper emits exactly two text items: Codex's
        execution status followed by a JSON-serialized MCP ``CallToolResult``.
        Claude Code stores the inner content directly in ``tool_result``;
        mirroring that shape lets generic and plugin transformers behave the
        same across providers. Compound ``Workflow`` calls are excluded by the
        caller so their transport remains lossless.
        """
        if not isinstance(value, list):
            return None
        items = cast(list[Any], value)
        if len(items) != 2:
            return None
        if not all(isinstance(item, dict) for item in items):
            return None
        status, emitted = cast(list[dict[str, Any]], items)
        status_text = status.get("text")
        emitted_text = emitted.get("text")
        if (
            status.get("type") not in {"input_text", "output_text", "text"}
            or not isinstance(status_text, str)
            or _COMPLETED_COMMAND_RE.fullmatch(status_text) is None
            or emitted.get("type") not in {"input_text", "output_text", "text"}
            or not isinstance(emitted_text, str)
        ):
            return None
        return self._forwarded_tool_emission(emitted_text)

    def _forwarded_tool_emission(
        self, emitted_text: str
    ) -> Optional[tuple[str | list[dict[str, Any]], bool]]:
        """Decode one JSON-serialized nested-tool result emission.

        Direct wrappers carry this emission after a status block, while an
        expanded ordered batch has already split the status from each emitted
        result.  Keeping the envelope decoder shared gives both forms the same
        canonical tool-result shape for downstream plugins.
        """
        try:
            decoded: Any = json.loads(emitted_text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(decoded, dict):
            return None
        envelope = cast(dict[str, Any], decoded)
        content = envelope.get("content")
        is_error = envelope.get("isError")
        if (
            not isinstance(content, list)
            or not isinstance(is_error, bool)
            or not all(isinstance(item, dict) for item in cast(list[Any], content))
        ):
            return None
        blocks = cast(list[dict[str, Any]], content)
        if len(blocks) == 1:
            block = blocks[0]
            text = block.get("text")
            if block.get("type") == "text" and isinstance(text, str):
                return text, is_error
        return blocks, is_error

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
            except (ValueError, RecursionError):
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
        return f"c{line_no}-{subindex}-{thread_id}"

    def _json_nesting_exceeds(self, value: Any, maximum: int) -> bool:
        pending: list[tuple[Any, int]] = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if not isinstance(item, (dict, list)):
                continue
            if depth >= maximum:
                return True
            children: list[Any] = (
                list(cast(dict[Any, Any], item).values())
                if isinstance(item, dict)
                else cast(list[Any], item)
            )
            pending.extend((child, depth + 1) for child in children)
        return False

    def _nonempty_string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None

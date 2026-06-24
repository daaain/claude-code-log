"""JSON renderer implementation for Claude Code transcripts."""

from __future__ import annotations

import dataclasses
import json
from enum import Enum
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING, cast

from pydantic import BaseModel

from ..cache import get_library_version
from ..models import TranscriptEntry
from ..renderer import (
    Renderer,
    TemplateMessage,
    generate_template_messages,
)


def _json_default(obj: Any) -> Any:
    """Serialization fallback for types dataclasses.asdict doesn't unwrap.

    Tool inputs/outputs on MessageContent are Pydantic models embedded inside
    dataclasses, and dataclasses.asdict leaves them untouched. Without this
    hook, json.dumps(default=str) would stringify them via __repr__ and lose
    all structure.
    """
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


if TYPE_CHECKING:
    from ..cache import CacheManager
    from ..dag import SessionTree


class JsonRenderer(Renderer):
    """JSON renderer - exports the processed transcript tree as structured JSON.

    Mirrors HtmlRenderer / MarkdownRenderer: runs transcripts through
    ``generate_template_messages`` so the output honours ``--detail`` filtering
    and exposes the same processed tree (pairing, children, session nav) that
    the other renderers consume.
    """

    def _message_to_dict(self, msg: TemplateMessage) -> dict[str, Any]:
        """Serialize a TemplateMessage (and its subtree) to a JSON-friendly dict."""
        content_dump = dataclasses.asdict(msg.content)
        # Meta is surfaced at the node level; drop the nested copy for clarity.
        content_dump.pop("meta", None)
        # `message_index` is a render-time back-reference, already exposed as `index`.
        content_dump.pop("message_index", None)

        node: dict[str, Any] = {
            "index": msg.message_index,
            "type": msg.type,
            "title": self.title_content(msg),
            "timestamp": msg.meta.timestamp,
            "session_id": msg.session_id,
            "content": content_dump,
        }
        # Expose the node's own uuid so parent_uuid references are resolvable.
        if msg.meta.uuid:
            node["uuid"] = msg.meta.uuid
        # Within-session fork branches regroup under a derived session id.
        if msg.render_session_id and msg.render_session_id != msg.session_id:
            node["render_session_id"] = msg.render_session_id
        if msg.parent_uuid:
            node["parent_uuid"] = msg.parent_uuid
        if msg.is_sidechain:
            node["is_sidechain"] = True
        if msg.agent_id:
            node["agent_id"] = msg.agent_id
        if msg.token_usage:
            node["token_usage"] = msg.token_usage
        if msg.pair_first is not None:
            node["pair_first"] = msg.pair_first
        if msg.pair_middle is not None:
            node["pair_middle"] = msg.pair_middle
        if msg.pair_last is not None:
            node["pair_last"] = msg.pair_last
        if msg.pair_duration:
            node["pair_duration"] = msg.pair_duration
        if msg.children:
            node["children"] = [self._message_to_dict(c) for c in msg.children]
        return node

    def generate(
        self,
        messages: list[TranscriptEntry],
        title: Optional[str] = None,
        combined_transcript_link: Optional[str] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
    ) -> str:
        """Serialize the processed transcript tree to JSON."""
        root_messages, session_nav, _ = generate_template_messages(
            messages,
            session_tree=session_tree,
            detail=self.detail,
            no_recaps=self.no_recaps,
        )

        payload: dict[str, Any] = {
            "version": get_library_version(),
            "title": title or "Claude Transcript",
            "detail": self.detail.value,
            "compact": self.compact,
            "sessions": session_nav,
            "messages": [self._message_to_dict(m) for m in root_messages],
        }
        if combined_transcript_link:
            payload["combined_transcript_link"] = combined_transcript_link

        return json.dumps(payload, indent=2, default=_json_default, ensure_ascii=False)

    def generate_session(
        self,
        messages: list[TranscriptEntry],
        session_id: str,
        title: Optional[str] = None,
        cache_manager: Optional["CacheManager"] = None,
        output_dir: Optional[Path] = None,
        session_tree: Optional["SessionTree"] = None,
        suppress_combined_link: bool = False,
    ) -> str:
        """Generate JSON for a single session."""
        from ..utils import get_parent_session_id

        # Include entries whose sessionId matches directly or via the
        # synthetic "{sessionId}#agent-{agentId}" form used for subagents.
        session_messages = [
            msg
            for msg in messages
            if get_parent_session_id(getattr(msg, "sessionId", "") or "") == session_id
        ]

        # Suppress the back-link under `--combined no` where the
        # combined transcript file is never written.
        combined_link: Optional[str] = None
        if cache_manager is not None and not suppress_combined_link:
            from ..utils import variant_suffix as _variant_suffix

            suffix = _variant_suffix(
                self.detail, self.compact, "json", no_recaps=self.no_recaps
            )
            combined_link = f"combined_transcripts{suffix}.json"

        return self.generate(
            session_messages,
            title or f"Session {session_id[:8]}",
            combined_transcript_link=combined_link,
            output_dir=output_dir,
            session_tree=session_tree,
        )

    def generate_projects_index(
        self,
        project_summaries: list[dict[str, Any]],
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> str:
        """Generate a JSON projects index."""
        projects: list[dict[str, Any]] = []
        total_messages = 0
        total_sessions = 0

        for summary in project_summaries:
            sessions = summary.get("sessions", [])
            total_sessions += len(sessions)
            total_messages += summary.get("message_count", 0)

            projects.append(
                {
                    "name": summary.get("name", ""),
                    "path": str(summary.get("path", "")),
                    "jsonl_count": summary.get("jsonl_count", 0),
                    "message_count": summary.get("message_count", 0),
                    "total_input_tokens": summary.get("total_input_tokens", 0),
                    "total_output_tokens": summary.get("total_output_tokens", 0),
                    "total_cache_creation_tokens": summary.get(
                        "total_cache_creation_tokens", 0
                    ),
                    "total_cache_read_tokens": summary.get(
                        "total_cache_read_tokens", 0
                    ),
                    "earliest_timestamp": summary.get("earliest_timestamp", ""),
                    "latest_timestamp": summary.get("latest_timestamp", ""),
                    "working_directories": summary.get("working_directories", []),
                    "is_archived": summary.get("is_archived", False),
                    "sessions": sessions,
                }
            )

        return json.dumps(
            {
                "version": get_library_version(),
                "total_projects": len(projects),
                "total_sessions": total_sessions,
                "total_messages": total_messages,
                "date_range": {"from": from_date, "to": to_date},
                "projects": projects,
            },
            indent=2,
            default=_json_default,
            ensure_ascii=False,
        )

    def is_outdated(self, file_path: Path) -> bool:
        """Check if a JSON file is outdated based on version field."""
        # is_file() (not exists()) so a non-regular destination — e.g.
        # /dev/stdout, which is a pipe when stdout is piped — is treated as
        # outdated rather than opened read-only, which would deadlock the
        # version sniff on json.load() (issue #223).
        if not file_path.is_file():
            return True
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Non-object payloads (list/scalar) have no version field; treat
            # as outdated so the next run regenerates cleanly.
            if not isinstance(data, dict):
                return True
            payload = cast("dict[str, Any]", data)
            return payload.get("version") != get_library_version()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return True

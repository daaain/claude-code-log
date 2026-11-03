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


@dataclass(frozen=True)
class ProviderTokenTotals:
    """Cumulative session token totals surfaced by a provider that records
    them at the session level (e.g. Codex ``token_count`` events), as opposed
    to the Claude path which sums per-assistant-message ``usage``.

    Mapped onto the same four columns the index renders, minus one:
    ``cache_creation`` is deliberately ABSENT, not zero. Codex has no
    cache-creation concept, and an omitted column ("we don't record this") is
    a different, honest claim than a zero one ("we recorded zero of it").

    ``total_tokens`` is the record's own authoritative total — never
    recomputed from the components. For the well-formed cumulative records
    that back session totals the identity ``input + cache_read + output ==
    total`` holds, but degenerate records (all components zero, non-zero
    total) do occur in the per-step stream, and there the stored total is the
    only trustworthy figure. It is **currently unconsumed by the render/cache
    paths** (the four displayed columns come from input/cache_read/output); it
    is kept as the reconstruction anchor the tests validate and the reserve a
    future per-turn layer would need.
    """

    input_tokens: int  # billable non-cached input = input_tokens - cached
    cache_read_tokens: int  # cached_input_tokens
    output_tokens: int  # output_tokens, which already includes reasoning
    total_tokens: int  # record's authoritative total; never recomputed


@dataclass(frozen=True)
class LoadedSession:
    """One session's rendered entries together with its cumulative token
    totals, as returned by :meth:`BaseProvider.load_session_with_totals`.

    The pair travels together because the caller needs both and a provider may
    be able to produce both from a single parse. ``token_totals`` is ``None``
    for the providers (and the sessions) that record none — omitted, never
    zeroed, since a zero total is a different claim from an absent one.
    """

    entries: list[TranscriptEntry]
    token_totals: Optional[ProviderTokenTotals]


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
    # Absolute path to the session's source file, when it has a single one.
    # The wholesale walker keys source-mtime cache staleness off this.
    source_path: Optional[Path] = None


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

    def detect_path(self, path: Path) -> bool:
        """Cheaply decide whether an INPUT_PATH belongs to this provider.

        Default: no auto-detection. A provider that can recognize its own
        session files by a cheap check (a filename pattern or a first-line
        sniff) overrides this so an INPUT_PATH routes to the provider pipeline
        instead of the Claude parser (which would silently skip the records and
        emit a near-empty page). Implementations MUST NOT fully parse the file.
        """
        return False

    def load_session_from_path(
        self, path: Path, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load a single session file handed in directly as an INPUT_PATH.

        Only providers that participate in INPUT_PATH detection (``detect_path``)
        need this. The default raises: a provider that never claims a path will
        never be asked to load one.
        """
        raise NotImplementedError(
            f"{self.get_provider_name()} cannot load a session directly by path"
        )

    def discover_sessions_under(self, root: Path) -> Iterator[SessionInfo]:
        """Discover sessions within an arbitrary *root* directory.

        The wholesale walker calls this for both the provider's own data dir
        and a directory handed in as an INPUT_PATH (a mini sessions root).
        Unlike :meth:`discover_sessions` (which is pinned to ``get_data_dir``),
        the root is explicit, so one code path serves both. Sibling context
        within *root* (e.g. fork-prefix stripping) is honored, unlike the
        standalone :meth:`load_session_from_path`.

        Default raises: only providers that support wholesale rendering
        override this.
        """
        raise NotImplementedError(
            f"{self.get_provider_name()} does not support wholesale rendering"
        )

    def load_session_under(
        self, root: Path, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load one session by id within an explicit *root* (see
        :meth:`discover_sessions_under`), with sibling context.

        Default raises: only providers that support wholesale rendering
        override this.
        """
        raise NotImplementedError(
            f"{self.get_provider_name()} does not support wholesale rendering"
        )

    def get_session_stats(self, session_id: str) -> dict[str, Any]:
        return {}

    def session_token_totals(
        self, root: Path, session_id: str
    ) -> Optional[ProviderTokenTotals]:
        """Cumulative session token totals for the session ``session_id`` under
        ``root``, or ``None`` when the provider records none.

        The default is ``None``: providers whose token accounting is
        per-assistant-message ``usage`` (Claude) leave this alone — those
        totals flow through the message-usage accumulators in ``converter``,
        not this seam. A provider that records session-level cumulative totals
        (Codex) overrides this so the wholesale/index path can surface them
        directly, bypassing the per-message summation that would otherwise
        double-count a cumulative figure.

        Still the seam for a totals-only lookup. The wholesale walker uses
        :meth:`load_session_with_totals` instead, so that a provider whose
        totals live in the same source it just parsed need not re-read it.
        """
        return None

    def load_session_with_totals(
        self, root: Path, session_id: str, max_messages: Optional[int] = None
    ) -> LoadedSession:
        """Entries *and* cumulative token totals for one session, in one call.

        The wholesale walker needs both, and for a provider that reads them
        from the same file this is the difference between parsing that file
        once and parsing it twice — the second parse being work the first
        already did and discarded, not a recomputation worth caching (the
        decoded records of one real archive reach 124 MB for a single session,
        so any cache here would need a byte budget rather than an entry count).

        **The default is exactly the pair of calls the walker used to make**,
        so a provider that does not override this cannot change behaviour by
        the seam existing. Override it only when the two can genuinely share
        work; leave it alone otherwise.
        """
        return LoadedSession(
            entries=list(self.load_session_under(root, session_id, max_messages)),
            token_totals=self.session_token_totals(root, session_id),
        )

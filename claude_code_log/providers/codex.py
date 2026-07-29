"""Codex CLI rollout session provider.

The rollout format is an implementation detail of Codex rather than a stable
file-format API.  Parsing here is deliberately tolerant: the provider keeps
the raw-record decoder small, ignores unknown records, and normalizes only
shapes for which it has useful semantics.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from fnmatch import fnmatch
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
    LoadedSession,
    ProviderTokenTotals,
    SessionInfo,
    file_mtime_iso,
    make_assistant_entry,
    make_thinking_entry,
    make_tool_result_entry,
    make_tool_use_entry,
    make_user_entry,
)
from .codex_tools import AdaptedToolCall, adapt_codex_tool_batch, adapt_codex_tool_call
from .codex_quickjs import analyze_javascript_tools
from .codex_messages import format_codex_user_message, parse_codex_user_shell_command
from .codex_web import normalize_codex_web_result

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
_TRUNCATED_OUTPUT_PREAMBLE_RE = re.compile(
    r"\AWarning: truncated output \([^\r\n]+\)\r?\n"
    r"Total output lines: [0-9]+\r?\n\r?\n"
)
_TRUNCATED_OUTPUT_MARKER_RE = re.compile(r"…[0-9]+ tokens truncated…")
_TRUNCATED_OUTPUT_PLACEHOLDER = "[Output omitted by Codex truncation]"
# Truncation recovery only ever yields the FINAL top-level property (the
# closing-brace tail check below), so at most a handful of reverse matches can
# succeed — every earlier one is a nested same-key occurrence inside that final
# value. Cap the reverse ``raw_decode`` attempts to a small constant so a
# hostile truncated output with many ``"key":`` occurrences cannot drive the
# loop quadratic (nested same-key inputs re-parse the surviving subtree at each
# match). K=16 covers any realistic nesting with wide margin; exceeding it just
# falls back to the truncation placeholder — never wrong data.
_MAX_TRUNCATION_RECOVERY_ATTEMPTS = 16
# Anchored, non-slicing check that the value's tail is exactly the outer
# closing brace. ``re.match(output, pos)`` scans from ``pos`` without
# materializing ``output[pos:]`` and short-circuits in O(1) on the common
# failing char (``,`` / ``"``), so it does not add a per-match O(N) slice.
_OUTER_BRACE_TAIL_RE = re.compile(r"\s*\}\s*\Z")
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


def _looks_like_rollout_file(path: Path) -> bool:
    """Cheap check: does *path* look like a Codex rollout JSONL file?

    A positive filename match (``rollout-*.jsonl``) short-circuits; otherwise a
    single first-line sniff for the ``session_meta`` header (modern ``type``
    field, or the legacy no-``type``/``id`` flat header). Never parses the body.
    """
    if not path.is_file():
        return False
    if fnmatch(path.name, _ROLLOUT_GLOB):
        return True
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    return False
                if not isinstance(raw, dict):
                    return False
                raw_dict = cast("dict[str, Any]", raw)
                return raw_dict.get("type") == "session_meta" or (
                    "type" not in raw_dict and bool(raw_dict.get("id"))
                )
    except OSError:
        return False
    return False


def _contained_rollouts(root: Path) -> Iterator[Path]:
    """Yield rollout files under *root*, resolving symlinks but keeping
    containment (mirrors ``_rollout_paths``): a symlink escaping *root* is
    skipped, so an INPUT_PATH directory can't pull in outside files.

    A file counts as a rollout by the SAME rule ``_looks_like_rollout_file``
    applies to a single file — the ``rollout-*.jsonl`` name, else a first-line
    ``session_meta`` sniff — so a directory of sniff-only-named rollouts is
    discovered exactly as the equivalent standalone file is, never silently
    dropped to an empty Claude parse."""
    resolved_root = root.resolve()
    for candidate in root.rglob("*.jsonl"):
        try:
            resolved = candidate.resolve()
            if (
                candidate.is_file()
                and resolved.is_relative_to(resolved_root)
                and _looks_like_rollout_file(candidate)
            ):
                yield resolved
        except OSError:
            continue


def _token_totals_from_records(
    records: list[_DecodedRecord],
) -> Optional[ProviderTokenTotals]:
    """Session token totals from the LAST cumulative ``token_count`` record.

    The session total is the final ``payload.info.total_token_usage``, never a
    sum of the per-step deltas: each ``total_token_usage`` is cumulative and
    monotonic over the session, so the last one already subsumes every prior
    turn. Compaction lowers the live context window but does NOT reset the
    cumulative counter, so "last record" stays correct across a compacted
    session. That monotonicity is ENFORCED, not merely assumed — if
    ``total_tokens`` ever decreases the guard below omits the session's totals.
    Returns ``None`` when the session emitted no ``token_count`` (a
    pre-accounting rollout) or when monotonicity is violated — the totals are
    then OMITTED, not zeroed, and never a guessed-through wrong number.

    WHY SESSION-LEVEL ONLY (evidenced design limit — do not "fix" into
    per-message numbers):
    The argument is STRUCTURAL, not statistical. A ``token_count`` delta
    (``last_token_usage``) measures everything consumed since the *previous*
    ``token_count`` — and one agent-loop step bundles reasoning + assistant
    text + a tool call + its (often large, cached) tool result under a single
    delta. The window contains more than one rendered thing, so a delta cannot
    be attributed to any one message the transcript renders, regardless of
    which record the step happens to end on. The corpus distribution merely
    confirms that steps overwhelmingly end on tool work: measured post-
    inherited-prefix-strip (the records that actually render), n=4138 events
    across 34 sessions, ~75.6% of ``token_count`` events follow a tool-
    execution step and ~22.5% follow an assistant/agent message — but even the
    22.5% are not attributable, because that message shares its delta with the
    reasoning and the next turn's cached context re-read. Per-message (and even
    per-turn) attribution is therefore not recoverable from this stream; the
    session cumulative is the finest honest unit, which is why this returns a
    whole-session total.
    """
    last_usage: Optional[dict[str, Any]] = None
    prev_total: Optional[int] = None
    malformed_total_shapes: list[str] = []
    first_malformed_at: Optional[str] = None
    for record in records:
        if record.kind != "event_msg":
            continue
        if record.payload.get("type") != "token_count":
            continue
        info = record.payload.get("info")
        if not isinstance(info, dict):
            continue
        usage_raw = cast(dict[str, Any], info).get("total_token_usage")
        if not isinstance(usage_raw, dict):
            continue
        usage = cast(dict[str, Any], usage_raw)
        # Monotonicity guard. total_token_usage is cumulative, so total_tokens
        # must never decrease. If it does, the cumulative-counter assumption has
        # broken (e.g. a future Codex build that resets the counter mid-session),
        # and NO single record is the honest total — "last" would understate,
        # and max() would report a pre-reset peak; both are confidently wrong.
        # Fail closed: omit the session's totals and warn, exactly as a
        # pre-accounting rollout (no token_count) is omitted. A wrong number is
        # worse than an absent one. Enforced, not merely assumed — monk and I
        # measured 0 violations across the corpus, so this fires only on a
        # future spec change, loudly.
        # A record whose ``total_tokens`` is absent or not an int tells us
        # nothing about the ordering. Coercing it to 0 (the pre-review
        # behaviour) made it look like a counter reset, so a single malformed
        # record tripped the guard below and omitted the WHOLE session's totals
        # for what is a data-quality problem, not a broken counter. Skip it
        # instead — out of the comparison AND out of last-record selection,
        # since ``_map_cumulative_usage`` treats the stored total as
        # authoritative and would carry a fabricated 0 through — and say so
        # once at the end, naming the shape actually seen. ``bool`` is excluded
        # deliberately: it is an ``int`` subclass, so a JSON ``true`` would
        # otherwise pass as the total 1.
        total_raw = usage.get("total_tokens")
        if not isinstance(total_raw, int) or isinstance(total_raw, bool):
            malformed_total_shapes.append(
                "absent" if total_raw is None else type(total_raw).__name__
            )
            if first_malformed_at is None:
                # Enough to open the rollout at the offending record instead of
                # re-scanning it. One warning per session stays O(1), but a bare
                # count would leave the reader nothing to search on.
                first_malformed_at = record.timestamp or "(no timestamp)"
            continue
        total = total_raw
        if prev_total is not None and total < prev_total:
            logger.warning(
                "Codex token_count total_tokens decreased (%d < %d); cumulative "
                "monotonicity broken — omitting session totals",
                total,
                prev_total,
            )
            return None
        prev_total = total
        last_usage = usage
    if malformed_total_shapes:
        # Degrade visibly: the totals we return are real, but they are drawn
        # from fewer records than the session actually holds.
        logger.warning(
            "Codex token_count: skipped %d record(s) whose total_tokens was "
            "not an integer (saw: %s; first at %s); session totals come from "
            "the remaining records",
            len(malformed_total_shapes),
            ", ".join(sorted(set(malformed_total_shapes))),
            first_malformed_at,
        )
    if last_usage is None:
        return None
    return _map_cumulative_usage(last_usage)


def _map_cumulative_usage(usage: dict[str, Any]) -> ProviderTokenTotals:
    """Map a Codex ``total_token_usage`` dict onto the index's token columns.

    THE SUBTRACTION PIN: billable input EXCLUDES the cached portion, and
    ``cache_read`` is the sole home of the cached tokens. A future edit that
    folded ``cached`` back into ``input`` here (or dropped the subtraction)
    would double-count the cached tokens — index totals would balloon by the
    cache-read column. Keep ``input = input_tokens - cached`` and
    ``cache_read = cached`` disjoint. ``max(..., 0)`` guards a malformed record
    where ``cached > input``.

    ``total_tokens`` is carried through from the record, authoritative and
    never recomputed (a degenerate record with zero components but a non-zero
    total must keep its stored total). ``output`` already includes
    ``reasoning_output_tokens``, so reasoning is not added again.
    """

    def _as_int(value: Any) -> int:
        """Every component field routes through here, so this is the ONE place
        the int predicate is stated — add a new component and it is covered by
        construction rather than by remembering to repeat the check.

        ``bool`` is excluded explicitly because it is an ``int`` subclass: a JSON
        ``true`` would otherwise contribute a phantom 1 to a token column. The
        record-selection guard in :func:`_token_totals_from_records` already
        excludes bools for ``total_tokens``; it did not cover the components,
        which reach this mapping on any record whose *total* is well-formed.
        """
        if isinstance(value, bool):
            return 0
        return value if isinstance(value, int) else 0

    input_tokens = _as_int(usage.get("input_tokens"))
    cached = _as_int(usage.get("cached_input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    total_tokens = _as_int(usage.get("total_tokens"))
    return ProviderTokenTotals(
        input_tokens=max(input_tokens - cached, 0),
        cache_read_tokens=cached,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


class CodexProvider(BaseProvider):
    """Read active Codex rollout files from ``$CODEX_HOME/sessions``."""

    def __init__(self) -> None:
        # Memoize the thread-id → paths index per resolved sessions root so a
        # wholesale run (discovery + per-session loads) reads each rollout's
        # header once instead of O(sessions) times. Safe within a single CLI
        # run — the sessions tree does not change mid-render.
        self._index_cache: dict[Path, dict[str, list[Path]]] = {}

    def detect_path(self, path: Path) -> bool:
        """A Codex rollout file, or a directory containing at least one."""
        if path.is_file():
            return _looks_like_rollout_file(path)
        if path.is_dir():
            return any(True for _ in _contained_rollouts(path))
        return False

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

    def _sessions_root(self) -> Optional[Path]:
        data_dir = self.get_data_dir()
        return data_dir / "sessions" if data_dir is not None else None

    def discover_sessions(self) -> Iterator[SessionInfo]:
        sessions_root = self._sessions_root()
        if sessions_root is None:
            return
        yield from self._discover_in(sessions_root)

    def discover_sessions_under(self, root: Path) -> Iterator[SessionInfo]:
        """Discover sessions within an arbitrary root (INPUT_PATH dir or the
        data dir), honoring sibling fork-prefix context within *root*."""
        yield from self._discover_in(root)

    def _discover_in(self, sessions_root: Path) -> Iterator[SessionInfo]:
        # A duplicated thread id is corrupt/ambiguous.  Discovery remains
        # useful and deterministic by retaining the lexicographically first
        # path; loading that id reports the ambiguity instead of guessing.
        identities: dict[str, CodexSessionIdentity] = {}
        index = self._session_index(sessions_root)
        for path in self._rollout_paths(sessions_root):
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
                source_path=identity.path,
                parent_thread_id=identity.parent_thread_id,
                forked_from_id=identity.forked_from_id,
                spawn_call_id=identity.spawn_call_id,
                source_kind=identity.source_kind,
                inherited_prefix_records=identity.inherited_prefix_records,
            )

    def load_session(
        self, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        sessions_root = self._sessions_root()
        if sessions_root is None:
            raise ValueError("Codex data directory not found")
        yield from self._load_in(sessions_root, session_id, max_messages)

    def load_session_under(
        self, root: Path, session_id: str, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load one session by id within an explicit *root* (a mini sessions
        tree or the data dir), with sibling fork-prefix stripping."""
        yield from self._load_in(root, session_id, max_messages)

    def _resolve_and_decode(
        self, sessions_root: Path, session_id: str
    ) -> tuple[CodexSessionIdentity, list[_DecodedRecord]]:
        """Resolve ``session_id`` under ``sessions_root`` and decode its rollout
        once, returning the identity and the post-inherited-prefix records.

        The single expensive step in every path that reads a session: one
        rollout decode. :meth:`_load_in` and :meth:`load_session_with_totals`
        share it so a caller that needs both entries and totals pays for one
        decode rather than two.

        Raises (rather than soft-missing) on an unresolvable or ambiguous id,
        matching the loader's contract; :meth:`session_token_totals` keeps its
        own tolerant resolution, since a totals lookup must never crash a
        render.
        """
        if not session_id or _SESSION_ID_RE.fullmatch(session_id) is None:
            raise ValueError(f"Invalid session_id: {session_id}")

        index = self._session_index(sessions_root)
        if session_id not in index:
            raise FileNotFoundError(f"Codex session {session_id} not found")
        paths = index[session_id]
        if len(paths) != 1:
            raise ValueError(f"Multiple Codex rollouts have thread id {session_id}")

        identity = self._with_inherited_prefix(self._read_identity(paths[0]), index)
        records = self._without_inherited_prefix(
            list(self._decode_records(identity.path)),
            identity.inherited_prefix_records,
        )
        return identity, records

    def _load_in(
        self, sessions_root: Path, session_id: str, max_messages: Optional[int]
    ) -> Iterator[TranscriptEntry]:
        if max_messages is not None and max_messages <= 0:
            # Still validate the id, so an invalid one raises regardless of
            # max_messages — the check used to precede this early return.
            if not session_id or _SESSION_ID_RE.fullmatch(session_id) is None:
                raise ValueError(f"Invalid session_id: {session_id}")
            return

        identity, records = self._resolve_and_decode(sessions_root, session_id)
        yield from self._normalize_records(identity, records, max_messages)

    def load_session_with_totals(
        self, root: Path, session_id: str, max_messages: Optional[int] = None
    ) -> LoadedSession:
        """Entries and cumulative totals from a SINGLE rollout decode.

        The base implementation would call the loader and then
        :meth:`session_token_totals`, and the two repeat the same index lookup,
        identity resolution, decode and prefix strip — measured at +118 decodes
        and +636 MB re-decoded across a 34-rollout archive, purely to recompute
        what the first pass had already produced. Only the *tail* differs:
        normalize for entries, last cumulative ``token_count`` for totals.

        Totals are computed **before** normalizing, deliberately: the normalize
        passes are free to transform their input, and computing totals first
        means correctness here does not depend on whether they do. Do not
        reorder these two lines.
        """
        if max_messages is not None and max_messages <= 0:
            # Nothing to share: with no entries requested there is no decode for
            # the totals to piggyback on, so the override has no advantage here —
            # and every attempt to hand-write this branch diverged from the base
            # in some input. Delegating makes equivalence hold BY CONSTRUCTION
            # rather than by argument, at the same one decode the base pays.
            #
            # Two divergences this avoids, both found by measurement rather than
            # by reading: returning ``None`` reported no totals for a session
            # that HAS them (base reports them, since its ``load_session_under``
            # returns early while ``session_token_totals`` still runs); and
            # resolving eagerly raised ``FileNotFoundError`` on an unknown id
            # where the base silently returns empty, because the base never
            # resolves at all on this path.
            return super().load_session_with_totals(root, session_id, max_messages)

        identity, records = self._resolve_and_decode(root, session_id)
        totals = _token_totals_from_records(records)
        entries: list[TranscriptEntry] = list(
            self._normalize_records(identity, records, max_messages)
        )
        return LoadedSession(entries=entries, token_totals=totals)

    def load_session_from_path(
        self, path: Path, max_messages: Optional[int] = None
    ) -> Iterator[TranscriptEntry]:
        """Load a single rollout file directly by path (an INPUT_PATH),
        independent of the configured data dir.

        There is no sibling index, so inherited-prefix stripping is a no-op and
        the file renders standalone — the right behavior for "render that one
        rollout" without discovering an ambient sessions tree around it.
        """
        if max_messages is not None and max_messages <= 0:
            return
        if not path.is_file():
            raise FileNotFoundError(f"Codex rollout not found: {path}")
        identity = self._read_identity(path)
        records = list(self._decode_records(path))
        yield from self._normalize_records(identity, records, max_messages)

    def session_token_totals(
        self, root: Path, session_id: str
    ) -> Optional[ProviderTokenTotals]:
        """Cumulative token totals for one Codex session, read from its
        ``token_count`` events.

        Resolution mirrors :meth:`_load_in` — same index lookup and the same
        ``_without_inherited_prefix`` strip — so the totals are computed over
        exactly the records this session renders. A fork must not inherit its
        parent's cumulative ``token_count`` (currently ``inherited_prefix_records``
        is 0 in observed corpora, but computing post-strip keeps this correct
        by construction rather than by that coincidence).

        Returns ``None`` — totals OMITTED, not zeroed — when the session has no
        ``token_count`` events (pre-accounting rollouts) or cannot be resolved
        unambiguously. A totals lookup must never crash a wholesale render, so
        ambiguity is a soft miss here, unlike the hard errors :meth:`_load_in`
        raises.
        """
        index = self._session_index(root)
        paths = index.get(session_id)
        if not paths or len(paths) != 1:
            return None
        identity = self._with_inherited_prefix(self._read_identity(paths[0]), index)
        records = self._without_inherited_prefix(
            list(self._decode_records(identity.path)),
            identity.inherited_prefix_records,
        )
        return _token_totals_from_records(records)

    def _rollout_paths(self, sessions_root: Path) -> list[Path]:
        # Recursive discovery supports both current date shards and old flat
        # layouts.  archived_sessions is deliberately outside this v1 root.
        # ``sessions_root`` is the directory to walk directly: the data dir's
        # ``sessions/`` subdir, or a directory handed in as an INPUT_PATH.
        # Match by ``_looks_like_rollout_file`` (name OR first-line sniff), the
        # same rule single-file detection uses, so a sniff-only-named rollout in
        # a directory is discovered — not silently dropped. Real data dirs hold
        # only ``rollout-*.jsonl``, which short-circuits on the name (no read).
        resolved_root = sessions_root.resolve()
        paths: set[Path] = set()
        for path in sessions_root.rglob("*.jsonl"):
            try:
                resolved = path.resolve()
                if (
                    path.is_file()
                    and resolved.is_relative_to(resolved_root)
                    and _looks_like_rollout_file(path)
                ):
                    paths.add(resolved)
            except OSError:
                continue
        return sorted(paths)

    def _session_index(self, sessions_root: Path) -> dict[str, list[Path]]:
        # Memoized per resolved root: discovery and every per-session load in a
        # wholesale run share one index build (see ``_index_cache``).
        try:
            cache_key = sessions_root.resolve()
        except OSError:
            cache_key = sessions_root
        cached = self._index_cache.get(cache_key)
        if cached is not None:
            return cached
        index: dict[str, list[Path]] = {}
        for path in self._rollout_paths(sessions_root):
            identity = self._read_identity(path)
            index.setdefault(identity.thread_id, []).append(path)
        self._index_cache[cache_key] = index
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
                or not self._only_exec_wrapper_interstitials(
                    records, result_index, wait_index
                )
                or not self._only_exec_wrapper_interstitials(
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

        A long-running ``exec_command`` may first return either an outer cell id
        or a serialized result carrying the inner command's session id.  Codex
        then emits ``wait(cell_id=...)`` and/or ``write_stdin(session_id=...)``
        as separate tools; a slow ``write_stdin`` wrapper can itself yield an
        outer cell that requires another ``wait``.  These are transport details
        of one command, not independent transcript actions.  Coalesce only
        consecutive visible tool events whose identifiers form that exact
        chain and reach an exit code; otherwise retain every record unchanged.
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
            initial_envelope = self._command_result(initial_output)
            initial_session_id = (
                initial_envelope.get("session_id")
                if initial_envelope is not None
                else None
            )
            if match is None and not isinstance(initial_session_id, int):
                position += 1
                continue
            if initial_envelope is not None and isinstance(
                initial_envelope.get("exit_code"), int
            ):
                position += 1
                continue

            cell_id = match.group(1) if match is not None else None
            cursor = position + 2
            chunks: list[str] = []
            if initial_envelope is not None:
                initial_chunk = initial_envelope.get("output")
                if isinstance(initial_chunk, str) and initial_chunk:
                    chunks.append(initial_chunk)
            continuation_indices: list[int] = []
            session_id = (
                initial_session_id if isinstance(initial_session_id, int) else None
            )
            previous_result_index = result_index
            terminal = False
            terminal_exit_code: Optional[int] = None
            terminal_result: Optional[_DecodedRecord] = None
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
                    name == "wait"
                    and cell_id is not None
                    and str(input_data.get("cell_id")) == cell_id
                )
                matches_write = (
                    name == "write_stdin"
                    and cell_id is None
                    and session_id is not None
                    and input_data.get("session_id") == session_id
                )
                if not (matches_wait or matches_write):
                    break
                continuation_output = records[next_result_index].payload.get("output")
                envelope = self._command_result(continuation_output)
                if envelope is None:
                    wrapper_match = (
                        _RUNNING_CELL_RE.search(continuation_output)
                        if isinstance(continuation_output, str)
                        else None
                    )
                    if wrapper_match is None:
                        break
                    cell_id = wrapper_match.group(1)
                    continuation_indices.extend([next_call_index, next_result_index])
                    cursor += 2
                    previous_result_index = next_result_index
                    continue
                output = envelope.get("output")
                if isinstance(output, str) and output:
                    chunks.append(output)
                raw_session_id = envelope.get("session_id")
                if isinstance(raw_session_id, int):
                    session_id = raw_session_id
                if matches_wait:
                    cell_id = None
                continuation_indices.extend([next_call_index, next_result_index])
                cursor += 2
                exit_code = envelope.get("exit_code")
                if isinstance(exit_code, int):
                    terminal_exit_code = exit_code
                    terminal_result = records[next_result_index]
                    terminal = True
                    break
                previous_result_index = next_result_index

            if terminal and continuation_indices and terminal_result is not None:
                payload = dict(result.payload)
                payload["output"] = "".join(chunks)
                payload["is_error"] = terminal_exit_code != 0
                replacements[result_index] = _DecodedRecord(
                    line_no=terminal_result.line_no,
                    timestamp=terminal_result.timestamp,
                    kind=terminal_result.kind,
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
        """Fold ``SESSION_ID=`` polling back into its originating Bash calls."""
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
            if origin.output_mode not in {"ordered", "markers"} or not all(
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
            if origin.output_mode == "ordered":
                if len(chunks) != 1:
                    continue
                rewritten.append({"type": "input_text", "text": "".join(chunks[0])})
            else:
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

    def _only_exec_wrapper_interstitials(
        self, records: list[_DecodedRecord], left: int, right: int
    ) -> bool:
        """Allow an inner MCP completion event inside an outer exec cell poll.

        Code-mode MCP calls emit ``mcp_tool_call_end`` immediately before the
        enclosing ``wait`` result. The event duplicates the forwarded result
        envelope and is not rendered independently, so it must not prevent the
        outer wrapper from being coalesced. Keep this exception local to exec
        wrappers; command-session polling retains its stricter boundaries.
        """
        return all(
            self._is_ignorable_command_interstitial(record)
            or (
                record.kind == "event_msg"
                and record.payload.get("type") == "mcp_tool_call_end"
            )
            for record in records[left + 1 : right]
        )

    def _is_ignorable_command_interstitial(self, record: _DecodedRecord) -> bool:
        """Whether a non-tool record may sit inside one command poll chain."""
        if record.kind in {"session_meta", "inter_agent_communication_metadata"}:
            return True
        if record.kind == "event_msg":
            # Token accounting is emitted after nearly every model/tool step.
            # Task boundaries and other events remain barriers even when the
            # renderer currently ignores them.
            return record.payload.get("type") == "token_count"
        if record.kind != "response_item":
            return False

        payload_type = record.payload.get("type")
        if payload_type == "agent_message":
            # Internal collaboration delivery is paired with
            # inter_agent_communication_metadata and is not a visible
            # assistant response. It may arrive while a cell keeps running.
            return True
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
                tuple[Optional[str], ...],
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
                    # Defense-in-depth: a batch-adapter bug must degrade this
                    # exec to its raw fallback, never crash the whole transcript
                    # render. The adapter's own contract (never raise on any
                    # analyze output) is pinned in the tests; this guard keeps a
                    # contract violation from being fatal in production.
                    try:
                        batch = adapt_codex_tool_batch(source)
                    except Exception:
                        # Name WHICH exec faulted (call_id) and carry WHAT broke
                        # (exc_info): this guard exists to diagnose an adapter
                        # contract violation, so a non-specific warning defeats
                        # its purpose — it was exactly this path that hid a
                        # consumer IndexError until a corpus probe surfaced it.
                        logger.warning(
                            "Codex batch adapter raised on exec snippet %s; "
                            "falling back to raw rendering",
                            call_id,
                            exc_info=True,
                        )
                        batch = None
                    if batch is not None:
                        requests[call_id] = (
                            batch.calls,
                            batch.output_mode,
                            batch.result_indexes,
                            batch.session_markers,
                            batch.result_prefixes,
                            batch.synthetic_results,
                            batch.output_count,
                            batch.result_object_keys,
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
            result_object_keys,
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
            if len(results) != len(result_object_keys):
                continue
            extracted_results: list[str] = []
            for result, object_key in zip(results, result_object_keys):
                if object_key is None:
                    extracted_results.append(result)
                    continue
                extracted = self._object_batch_result(result, object_key)
                if extracted is None:
                    break
                extracted_results.append(extracted)
            if len(extracted_results) != len(results):
                continue
            results = extracted_results
            status = self._empty_result_status(output[0])
            if status is not None:
                results = [
                    status
                    if call.name in {"Write", "Delete", "Edit", "MultiEdit"}
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

    def _object_batch_result(self, output: str, key: str) -> Optional[str]:
        """Extract one statically-proven property from a JSON result object."""
        if output == _TRUNCATED_OUTPUT_PLACEHOLDER:
            return output
        truncation = _TRUNCATED_OUTPUT_PREAMBLE_RE.match(output)
        if truncation is not None:
            output = output[truncation.end() :]
        was_truncated = (
            truncation is not None
            or _TRUNCATED_OUTPUT_MARKER_RE.search(output) is not None
        )
        try:
            decoded: Any = json.loads(output)
        except (ValueError, RecursionError):
            if not was_truncated:
                return None
            recovered = self._truncated_object_batch_result(output, key)
            return recovered if recovered is not None else _TRUNCATED_OUTPUT_PLACEHOLDER
        if not isinstance(decoded, dict):
            return None
        if key not in decoded:
            return _TRUNCATED_OUTPUT_PLACEHOLDER if was_truncated else None
        return self._batch_result_value(cast(dict[str, Any], decoded)[key])

    def _truncated_object_batch_result(self, output: str, key: str) -> Optional[str]:
        """Recover an intact property from the surviving tail of truncated JSON."""
        encoded_key = json.dumps(key, ensure_ascii=False)
        matches = list(re.finditer(re.escape(encoded_key) + r"\s*:\s*", output))
        decoder = json.JSONDecoder()
        for attempt, match in enumerate(reversed(matches)):
            if attempt >= _MAX_TRUNCATION_RECOVERY_ATTEMPTS:
                break  # bound the loop against quadratic hostile inputs.
            try:
                value, end = decoder.raw_decode(output, match.end())
            except (ValueError, RecursionError):
                continue
            # Only the final top-level property is recoverable without trusting
            # the damaged nesting/string state that precedes it.  Requiring
            # exactly the outer closing brace also avoids selecting a same-name
            # property from a nested surviving object.  The anchored regex checks
            # this from ``end`` without slicing the (potentially huge) tail.
            if _OUTER_BRACE_TAIL_RE.match(output, end) is None:
                continue
            return self._batch_result_value(value)
        return None

    def _batch_result_value(self, value: Any) -> Optional[str]:
        """Serialize one decoded object-batch property for a tool result."""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None

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

        results = [_TRUNCATED_OUTPUT_PLACEHOLDER for _ in concrete]
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
                    result, source_refs = normalize_codex_web_result(item.result)
                    result_entry = make_tool_result_entry(
                        thread_id,
                        uuid,
                        item.result_timestamp,
                        derived_id,
                        result,
                    )
                    result_entry.toolUseResult = {
                        "url": item.ref_id,
                        "result": result,
                        "sourceRefs": source_refs,
                        "codexWebResult": True,
                    }
                    expanded.append(result_entry)
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
                if tool_name in {"Workflow", "ToolExecution"}
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
                if tool_name in {"Bash", "WebSearch", "WebFetch"}:
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
        if not is_error and tool_name in {"CodexDoc", "CodexDocSearch"}:
            document = (
                self._openai_doc_result(output)
                if isinstance(output, list)
                else self._openai_doc_emission(output)
            )
            if document is not None:
                output = document
        if (
            not is_error
            and tool_name in {"Write", "Delete", "Edit", "MultiEdit"}
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
            output, source_refs = normalize_codex_web_result(output)
            tool_use_result = {
                "query": "",
                "results": [{"content": []}, output],
                "sourceRefs": source_refs,
            }
        elif not is_error and tool_name == "WebFetch" and isinstance(output, str):
            output, source_refs = normalize_codex_web_result(output)
            tool_use_result = {
                "url": "",
                "result": output,
                "sourceRefs": source_refs,
                "codexWebResult": True,
            }
        return output, tool_use_result

    def _openai_doc_result(self, items: list[dict[str, Any]]) -> Optional[str]:
        """Unwrap Codex's successful OpenAI Docs MCP result envelope."""
        if len(items) != 2:
            return None
        status, emitted = items
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
        return self._openai_doc_emission(emitted_text)

    def _openai_doc_emission(self, emitted_text: str) -> Optional[str]:
        try:
            decoded: Any = json.loads(emitted_text)
        except (ValueError, RecursionError):
            return None
        if not isinstance(decoded, dict):
            return None
        envelope = cast(dict[str, Any], decoded)
        if envelope.get("isError") is True:
            return None
        raw_content = envelope.get("content")
        if not isinstance(raw_content, list):
            return None
        content = cast(list[Any], raw_content)
        if len(content) != 1:
            return None
        block = content[0]
        if not isinstance(block, dict):
            return None
        text = cast(dict[str, Any], block).get("text")
        return (
            text
            if cast(dict[str, Any], block).get("type") == "text"
            and isinstance(text, str)
            else None
        )

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

#!/usr/bin/env python3
"""Convert Claude transcript JSONL files to HTML."""

import contextlib
import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
import traceback
from typing import Any, Dict, List, Optional, TYPE_CHECKING, cast

import dateparser

if TYPE_CHECKING:
    from .cache import CacheManager

from .utils import (
    format_timestamp_range,
    get_parent_session_id,
    get_project_display_name,
    is_agent_session,
    should_use_as_session_starter,
    create_session_preview,
    get_warmup_session_ids,
)
from .cache import (
    CacheManager,
    SessionCacheData,
    get_all_cached_projects,
    get_library_version,
)
from .parser import parse_timestamp
from .factories import create_transcript_entry
from .factories.teammate_factory import find_team_lead_body
from .models import (
    AiTitleTranscriptEntry,
    AttachmentTranscriptEntry,
    BaseTranscriptEntry,
    DetailLevel,
    PassthroughTranscriptEntry,
    TranscriptEntry,
    AssistantTranscriptEntry,
    QueueOperationTranscriptEntry,
    SummaryTranscriptEntry,
    SystemTranscriptEntry,
    UserTranscriptEntry,
    ToolResultContent,
    ToolUseContent,
)
from .dag import SessionTree, build_dag_from_entries, traverse_session_tree
from .renderer import get_renderer, is_html_outdated


@contextlib.contextmanager
def _dag_warnings_suppressed(silent: bool) -> Iterator[None]:
    """Temporarily raise the DAG module's log level under ``silent=True``.

    The DAG layer routes its cycle / multi-root / orphan diagnostics
    through ``logging`` (so users running with ``--debug`` still see
    them), but cache rebuilds and silent CLI flows shouldn't surface
    that noise. Bumping the logger level for the duration of the call
    suppresses the warnings without changing call signatures.
    """
    if not silent:
        yield
        return
    dag_logger = logging.getLogger("claude_code_log.dag")
    previous = dag_logger.level
    dag_logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        dag_logger.setLevel(previous)


# Internal Claude Code message types that carry no DAG fields and are
# dropped without warning. Unknown types outside this set are surfaced
# so we notice new kinds worth supporting (see the else branch in
# load_transcript). `progress` is not here because it has uuid+sessionId
# and participates in the DAG as a PassthroughTranscriptEntry.
SILENT_SKIP_TYPES: frozenset[str] = frozenset(
    {
        "file-history-snapshot",  # Internal file backup metadata
        "last-prompt",  # Trailing marker written as the last line of a .jsonl
        # Session metadata snapshots (positional state, no uuid/timestamp).
        # Recorded whenever Claude Code writes a state checkpoint to the
        # transcript; see #94 for the wider "propagate this state to
        # surrounding messages" follow-up.
        "permission-mode",  # {permissionMode: 'acceptEdits'|...}
        "custom-title",  # {customTitle: <str>}
        "agent-name",  # {agentName: <str>}
        "agent-color",  # {agentColor: <str>}
    }
)


def get_file_extension(format: str) -> str:
    """Get the file extension for a format.

    Normalizes 'markdown' to 'md' for consistent file extensions.
    """
    return "md" if format in ("md", "markdown") else format


def get_index_filename(format: str) -> str:
    """Get the all-projects index filename for a format.

    JSON uses `all-projects-summary.json` so it doesn't collide with the
    per-project JSON exports; other formats use `index.{ext}`.
    """
    ext = get_file_extension(format)
    return "all-projects-summary.json" if ext == "json" else f"index.{ext}"


# =============================================================================
# Progress Chain Repair
# =============================================================================


def _scan_file_progress(path: Path, chain: dict[str, Optional[str]]) -> None:
    """Extract progress entry uuid->parentUuid from a single JSONL file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "progress" not in line:  # Fast pre-filter
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    if not isinstance(raw, dict):
                        continue
                    d = cast(dict[str, Any], raw)
                    if d.get("type") == "progress":
                        uuid = d.get("uuid")
                        if isinstance(uuid, str):
                            chain[uuid] = d.get("parentUuid")
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass  # Race condition: file may have been deleted


def _scan_progress_chains(*paths: Path) -> dict[str, Optional[str]]:
    """Fast scan of JSONL files for progress entry uuid->parentUuid mappings."""
    chain: dict[str, Optional[str]] = {}
    for path in paths:
        if path.is_file():
            _scan_file_progress(path, chain)
        elif path.is_dir():
            for f in path.glob("*.jsonl"):
                _scan_file_progress(f, chain)
            # Also scan subagent directories
            for f in path.glob("*/subagents/*.jsonl"):
                _scan_file_progress(f, chain)
    return chain


def _scan_sidechain_uuids(directory: Path) -> set[str]:
    """Collect UUIDs from sidechain/subagent files not loaded into the DAG.

    Some subagent files (e.g. aprompt_suggestion) are never referenced
    via agentId in the main session, so they aren't loaded by
    load_transcript(). Their UUIDs are needed to suppress false orphan
    warnings when main-chain entries reference sidechain parents.
    """
    uuids: set[str] = set()
    for f in directory.glob("*/subagents/*.jsonl"):
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        if isinstance(raw, dict):
                            uuid = cast(dict[str, Any], raw).get("uuid")
                            if isinstance(uuid, str):
                                uuids.add(uuid)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
    return uuids


def _repair_parent_chains(
    messages: list[TranscriptEntry],
    progress_chain: dict[str, Optional[str]],
) -> None:
    """Repair parentUuid fields that point to dropped progress entries.

    Walks the progress chain to find the nearest non-progress ancestor.
    Only repairs links to progress entries that are NOT in the messages
    list (i.e. those that were truly dropped, not preserved as
    PassthroughTranscriptEntry).
    Mutates entries in place (Pydantic v2 models are mutable by default).
    """
    if not progress_chain:
        return
    # Filter out progress UUIDs that are present as parsed entries —
    # those are PassthroughTranscriptEntry nodes in the DAG and valid parents.
    present_uuids = {getattr(m, "uuid", None) for m in messages}
    dropped_progress = {
        uuid: parent
        for uuid, parent in progress_chain.items()
        if uuid not in present_uuids
    }
    if not dropped_progress:
        return
    for msg in messages:
        parent = getattr(msg, "parentUuid", None)
        if parent and parent in dropped_progress:
            current: Optional[str] = parent
            seen: set[str] = set()
            while current is not None and current in dropped_progress:
                if current in seen:
                    current = None
                    break
                seen.add(current)
                current = dropped_progress[current]
            msg.parentUuid = current  # type: ignore[union-attr]


# =============================================================================
# Transcript Loading Functions
# =============================================================================


def filter_messages_by_date(
    messages: list[TranscriptEntry], from_date: Optional[str], to_date: Optional[str]
) -> list[TranscriptEntry]:
    """Filter messages based on date range.

    Date parsing is done in UTC to match transcript timestamps which are stored in UTC.
    """
    if not from_date and not to_date:
        return messages

    # Parse dates in UTC to match transcript timestamps (which are stored in UTC)
    dateparser_settings: Any = {"TIMEZONE": "UTC", "RETURN_AS_TIMEZONE_AWARE": False}
    from_dt = None
    to_dt = None

    if from_date:
        from_dt = dateparser.parse(from_date, settings=dateparser_settings)
        if not from_dt:
            raise ValueError(f"Could not parse from-date: {from_date}")
        # If parsing relative dates like "today", start from beginning of day
        if from_date in ["today", "yesterday"] or "days ago" in from_date:
            from_dt = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    if to_date:
        to_dt = dateparser.parse(to_date, settings=dateparser_settings)
        if not to_dt:
            raise ValueError(f"Could not parse to-date: {to_date}")
        # If parsing relative dates like "today", end at end of day
        if to_date in ["today", "yesterday"] or "days ago" in to_date:
            to_dt = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    filtered_messages: list[TranscriptEntry] = []
    for message in messages:
        # Summary / ai-title entries carry no timestamp — keep them so
        # the title/summary survives date filtering.
        if isinstance(message, (SummaryTranscriptEntry, AiTitleTranscriptEntry)):
            filtered_messages.append(message)
            continue

        timestamp_str = message.timestamp
        if not timestamp_str:
            continue

        message_dt = parse_timestamp(timestamp_str)
        if not message_dt:
            continue

        # Convert to naive datetime for comparison (dateparser returns naive datetimes)
        if message_dt.tzinfo:
            message_dt = message_dt.replace(tzinfo=None)

        # Check if message falls within date range
        if from_dt and message_dt < from_dt:
            continue
        if to_dt and message_dt > to_dt:
            continue

        filtered_messages.append(message)

    return filtered_messages


def load_transcript(
    jsonl_path: Path,
    cache_manager: Optional["CacheManager"] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    silent: bool = False,
    _loaded_files: Optional[set[Path]] = None,
) -> list[TranscriptEntry]:
    """Load and parse JSONL transcript file, using cache if available.

    Args:
        _loaded_files: Internal parameter to track loaded files and prevent infinite recursion.
    """
    # Initialize loaded files set on first call
    if _loaded_files is None:
        _loaded_files = set()

    # Prevent infinite recursion by checking if this file is already being loaded
    if jsonl_path in _loaded_files:
        return []

    _loaded_files.add(jsonl_path)
    # Try to load from cache first
    if cache_manager is not None:
        # Use filtered loading if date parameters are provided
        if from_date or to_date:
            cached_entries = cache_manager.load_cached_entries_filtered(
                jsonl_path, from_date, to_date
            )
        else:
            cached_entries = cache_manager.load_cached_entries(jsonl_path)

        if cached_entries is not None:
            if not silent:
                print(f"Loading {jsonl_path} from cache...")
            return cached_entries

    # Parse from source file
    messages: list[TranscriptEntry] = []
    agent_ids: set[str] = set()  # Collect agentId references while parsing

    try:
        f = open(jsonl_path, "r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        # Handle race condition: file may have been deleted between glob and open
        # (e.g., Claude Code session cleanup)
        if not silent:
            print(f"Warning: File not found (may have been deleted): {jsonl_path}")
        return []

    with f:
        if not silent:
            print(f"Processing {jsonl_path}...")
        for line_no, line in enumerate(f, 1):  # Start counting from 1
            line = line.strip()
            if line:
                try:
                    entry_dict: dict[str, Any] | str = json.loads(line)
                    if not isinstance(entry_dict, dict):
                        print(
                            f"Line {line_no} of {jsonl_path} is not a JSON object: {line}"
                        )
                        continue

                    # Check for agentId BEFORE Pydantic parsing
                    # agentId can be at top level OR nested in toolUseResult
                    # For UserTranscriptEntry, we need to copy it to top level so Pydantic preserves it
                    if "agentId" in entry_dict:
                        agent_id = entry_dict.get("agentId")
                        if agent_id:
                            agent_ids.add(agent_id)
                    elif "toolUseResult" in entry_dict:
                        tool_use_result = entry_dict.get("toolUseResult")
                        if (
                            isinstance(tool_use_result, dict)
                            and "agentId" in tool_use_result
                        ):
                            agent_id_value = cast(Any, tool_use_result).get("agentId")
                            if isinstance(agent_id_value, str):
                                agent_ids.add(agent_id_value)
                                # Copy agentId to top level for Pydantic to preserve
                                entry_dict["agentId"] = agent_id_value

                    entry_type: str | None = entry_dict.get("type")

                    if entry_type in [
                        "user",
                        "assistant",
                        "summary",
                        "ai-title",
                        "system",
                        "queue-operation",
                        "attachment",
                    ]:
                        # Parse using Pydantic models. ``attachment`` joined
                        # this list in #128 so the hook payload survives to
                        # the rendering layer at full detail (was a
                        # PassthroughTranscriptEntry before, which threw
                        # the payload away at parse time).
                        entry = create_transcript_entry(entry_dict)
                        messages.append(entry)
                    elif entry_type in SILENT_SKIP_TYPES:
                        # Internal Claude Code entries with no DAG fields.
                        pass
                    elif entry_dict.get("uuid") and entry_dict.get("sessionId"):
                        # Unknown type with DAG-relevant fields — create a
                        # PassthroughTranscriptEntry to preserve DAG chain
                        # continuity (e.g. "progress", "agent-setting",
                        # "pr-link", "ai-title").
                        messages.append(
                            PassthroughTranscriptEntry(
                                uuid=entry_dict["uuid"],
                                parentUuid=entry_dict.get("parentUuid"),
                                sessionId=entry_dict["sessionId"],
                                timestamp=entry_dict.get("timestamp", ""),
                                type=entry_type,
                                isSidechain=entry_dict.get("isSidechain", False),
                                agentId=entry_dict.get("agentId"),
                            )
                        )
                    else:
                        # Unknown type with no DAG fields (e.g. custom-title,
                        # agent-name). Warn so we notice when Claude Code
                        # introduces new metadata worth supporting — add to
                        # SILENT_SKIP_TYPES once confirmed safe to drop.
                        if not silent:
                            print(
                                f"Line {line_no} of {jsonl_path}: unrecognized message type "
                                f"{entry_type!r} - skipping"
                            )
                except json.JSONDecodeError as e:
                    print(
                        f"Line {line_no} of {jsonl_path} | JSON decode error: {str(e)}"
                    )
                except ValueError as e:
                    # Extract a more descriptive error message
                    error_msg = str(e)
                    if "validation error" in error_msg.lower():
                        err_no_url = re.sub(
                            r"    For further information visit https://errors.pydantic(.*)\n?",
                            "",
                            error_msg,
                        )
                        print(f"Line {line_no} of {jsonl_path} | {err_no_url}")
                    else:
                        print(
                            f"Line {line_no} of {jsonl_path} | ValueError: {error_msg}"
                            f"\n{traceback.format_exc()}"
                        )
                except Exception as e:
                    print(
                        f"Line {line_no} of {jsonl_path} | Unexpected error: {str(e)}"
                        f"\n{traceback.format_exc()}"
                    )

    # Prompt-hash fallback: link Task tool_results that lack a structured
    # agentId (common for true teammate subagents) by matching the
    # tool_use's prompt input against the <teammate-message
    # teammate_id="team-lead"> body of each unlinked subagent file's
    # first entry. See issue #91 — also fixes #79 and #90 along the way.
    _link_subagents_by_prompt_hash(messages, jsonl_path, agent_ids)

    # Load agent files if any were referenced
    # Build a map of agentId -> agent messages
    agent_messages_map: dict[str, list[TranscriptEntry]] = {}
    if agent_ids:
        parent_dir = jsonl_path.parent
        session_basename = (
            jsonl_path.stem
        )  # e.g., "29ccd257-68b1-427f-ae5f-6524b7cb6f20"
        for agent_id in agent_ids:
            # Try legacy location first (same directory as session file)
            agent_file = parent_dir / f"agent-{agent_id}.jsonl"
            # Skip if the agent file is the same as the current file (self-reference)
            if agent_file == jsonl_path:
                continue
            # Try new subagents directory structure (Claude Code 2.1.2+)
            if not agent_file.exists():
                subagent_file = (
                    parent_dir
                    / session_basename
                    / "subagents"
                    / f"agent-{agent_id}.jsonl"
                )
                if subagent_file.exists():
                    agent_file = subagent_file
            if agent_file.exists():
                if not silent:
                    print(f"Loading agent file {agent_file}...")
                # Recursively load the agent file (it might reference other agents)
                agent_messages = load_transcript(
                    agent_file,
                    cache_manager,
                    from_date,
                    to_date,
                    silent=True,
                    _loaded_files=_loaded_files,
                )
                agent_messages_map[agent_id] = agent_messages

    # Insert agent messages at their point of use (only once per agent)
    if agent_messages_map:
        # Iterate through messages and insert agent messages after the FIRST message
        # that references them (via UserTranscriptEntry.agentId)
        result_messages: list[TranscriptEntry] = []
        for message in messages:
            result_messages.append(message)

            # Check if this is a UserTranscriptEntry with agentId
            if isinstance(message, UserTranscriptEntry) and message.agentId:
                agent_id = message.agentId
                if agent_id in agent_messages_map:
                    # Insert agent messages right after this message (pop to insert only once)
                    result_messages.extend(agent_messages_map.pop(agent_id))

        messages = result_messages

    # Save to cache if cache manager is available
    if cache_manager is not None:
        cache_manager.save_cached_entries(jsonl_path, messages)

    return messages


def _link_subagents_by_prompt_hash(
    messages: list[TranscriptEntry],
    jsonl_path: Path,
    agent_ids: set[str],
) -> None:
    """Link teammate subagent JSONLs whose agentId isn't in the main transcript.

    Teammate-spawned Tasks sometimes produce tool_results that don't carry a
    structured ``agentId`` — the linking info only appears in the Markdown
    metadata tail (parsed separately) or is absent altogether. Older
    transcripts predate the tail too. For these we fall back to matching
    the Task tool_use's ``prompt`` input against each unmatched
    ``subagents/agent-*.jsonl`` file's first-entry content. When the first
    entry wraps the prompt in ``<teammate-message teammate_id="team-lead">``,
    that body is compared; otherwise the raw text is.

    On a match, the agent id is added to *agent_ids* (so the existing loader
    picks the file up) and the corresponding tool_result entry's ``agentId``
    field is back-patched (so ``_integrate_agent_entries`` anchors the
    subagent DAG-line to the right place).

    No-op when the subagents dir doesn't exist or every Task is already
    linked; safe to call unconditionally.
    """
    unresolved = _collect_unresolved_task_results(messages)
    if not unresolved:
        return

    subagents_dir = jsonl_path.parent / jsonl_path.stem / "subagents"
    if not subagents_dir.is_dir():
        return

    # Pre-normalize prompts once, and track which result entries are still
    # up for grabs. Without this a second agent file with the same
    # normalized prompt would re-match an already-patched entry, wiping
    # the first match (concrete repro: team-lead sends identical
    # instructions to multiple teammates in parallel).
    remaining: list[tuple[str, UserTranscriptEntry]] = [
        (_normalize_prompt(prompt), entry) for prompt, entry in unresolved
    ]

    for agent_file in sorted(subagents_dir.glob("agent-*.jsonl")):
        candidate_agent_id = agent_file.stem[len("agent-") :]
        if not candidate_agent_id or candidate_agent_id in agent_ids:
            continue

        first_text = _read_first_message_text(agent_file)
        if first_text is None:
            continue

        candidate_body = find_team_lead_body(first_text) or first_text
        candidate_norm = _normalize_prompt(candidate_body)
        if not candidate_norm:
            continue

        for i, (norm_prompt, result_entry) in enumerate(remaining):
            if norm_prompt == candidate_norm:
                agent_ids.add(candidate_agent_id)
                result_entry.agentId = candidate_agent_id
                remaining.pop(i)
                break


def _collect_unresolved_task_results(
    messages: list[TranscriptEntry],
) -> list[tuple[str, UserTranscriptEntry]]:
    """Return (prompt, tool_result_entry) for Task results lacking an agentId."""
    task_prompts: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, AssistantTranscriptEntry):
            continue
        for item in msg.message.content:
            if isinstance(item, ToolUseContent) and item.name == "Task":
                prompt = item.input.get("prompt")
                if isinstance(prompt, str) and prompt:
                    task_prompts[item.id] = prompt

    unresolved: list[tuple[str, UserTranscriptEntry]] = []
    for msg in messages:
        if not isinstance(msg, UserTranscriptEntry):
            continue
        if msg.agentId:
            continue
        for item in msg.message.content:
            if not isinstance(item, ToolResultContent):
                continue
            prompt = task_prompts.get(item.tool_use_id)
            if prompt is not None:
                unresolved.append((prompt, msg))
                break
    return unresolved


def _read_first_message_text(agent_file: Path) -> Optional[str]:
    """Return the textual content of the first entry's ``message.content``.

    Handles both string-shaped content (teammate flow) and list-shaped
    content (plain prompt flow). Returns None for unreadable files.
    """
    try:
        with agent_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                stripped = raw.strip()
                if stripped:
                    line = stripped
                    break
            else:
                return None
        entry: Any = json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict):
        return None

    entry_dict = cast(dict[str, Any], entry)
    message = entry_dict.get("message")
    if not isinstance(message, dict):
        return None
    message_dict = cast(dict[str, Any], message)
    content = message_dict.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in cast(list[Any], content):
            if isinstance(item, dict):
                item_dict = cast(dict[str, Any], item)
                if item_dict.get("type") == "text":
                    texts.append(str(item_dict.get("text", "")))
        return "\n".join(texts) if texts else None
    return None


def _normalize_prompt(text: str) -> str:
    """Whitespace-collapsed, lowercase form for equality comparison."""
    return " ".join(text.split()).lower()


def _integrate_agent_entries(messages: list[TranscriptEntry]) -> None:
    """Parent subagent entries and stamp them with a per-agent session id.

    Two adjustments per subagent:
      1. Re-parent the sidechain root (parentUuid=None) to the trunk
         tool_result that referenced this agentId — so the DAG threads
         the subagent's conversation under its spawning Agent/Task call.
      2. Rewrite ``sessionId`` for every sidechain entry of that agent
         to ``{trunk}#agent-{agentId}``. Without this all subagents
         share the trunk's sessionId, and ``_walk_session_with_forks``
         folds subagent UUIDs into the trunk DAG-line — its anchor
         logic then can't separate one subagent's content from another's.
         The synthetic id splits each subagent into its own DAG-line that
         attaches at the anchor uuid, rendering as a sub-session branch.

    Mutates entries in place (Pydantic v2 models are mutable by default).
    """
    # Build agentId -> anchor UUID map.
    # An anchor is any entry whose agentId references a sidechain transcript.
    # Prefer non-sidechain anchors (main session), but also accept sidechain
    # anchors (nested agents: agent A spawns agent B, so B's anchor lives
    # inside A's sidechain).
    #
    # For sidechain anchors we require a *cross-agent boundary* — the
    # candidate's parent must belong to a different agent's transcript.
    # Without that guard, the agent's own root entry (parentUuid=None,
    # agentId=X) gets registered as the anchor for X, and the re-parent
    # step below sets ``parentUuid = uuid``, producing a self-loop and
    # a "Cycle detected" warning at DAG construction. Repros when an
    # orphan subagent transcript is loaded but its trunk session isn't
    # (e.g. warmup-only sessions); see ``test_data/dag_cycle.jsonl``.
    sidechain_agent_by_uuid: dict[str, str] = {}
    for msg in messages:
        if (
            isinstance(msg, (BaseTranscriptEntry, PassthroughTranscriptEntry))
            and msg.isSidechain
            and msg.agentId
        ):
            sidechain_agent_by_uuid[msg.uuid] = msg.agentId

    agent_anchors: dict[str, str] = {}
    agent_anchors_from_sidechain: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, (BaseTranscriptEntry, PassthroughTranscriptEntry)):
            continue
        if not msg.agentId:
            continue
        if msg.isSidechain:
            # Skip entries that are part of agent X's own transcript chain
            # (root with parentUuid=None, or parent shares the same agentId).
            if msg.parentUuid is None:
                continue
            parent_agent_id = sidechain_agent_by_uuid.get(msg.parentUuid)
            if parent_agent_id is None or parent_agent_id == msg.agentId:
                continue
            agent_anchors_from_sidechain.setdefault(msg.agentId, msg.uuid)
        else:
            agent_anchors[msg.agentId] = msg.uuid
    # Merge: non-sidechain anchors take priority
    for agent_id, uuid in agent_anchors_from_sidechain.items():
        agent_anchors.setdefault(agent_id, uuid)

    if not agent_anchors:
        return

    for msg in messages:
        if not isinstance(msg, (BaseTranscriptEntry, PassthroughTranscriptEntry)):
            continue
        if not msg.isSidechain or not msg.agentId:
            continue
        agent_id = msg.agentId
        if msg.parentUuid is None and agent_id in agent_anchors:
            msg.parentUuid = agent_anchors[agent_id]
        msg.sessionId = f"{msg.sessionId}#agent-{agent_id}"


def load_directory_transcripts(
    directory_path: Path,
    cache_manager: Optional["CacheManager"] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    silent: bool = False,
) -> tuple[list[TranscriptEntry], SessionTree]:
    """Load all JSONL transcript files from a directory and combine them.

    Returns (messages, session_tree) — the tree is reused by the renderer
    to avoid rebuilding the DAG.
    """
    all_messages: list[TranscriptEntry] = []

    # Find all .jsonl files, excluding agent files (they are loaded via load_transcript
    # when a session references them via agentId)
    jsonl_files = [
        f for f in directory_path.glob("*.jsonl") if not f.name.startswith("agent-")
    ]

    for jsonl_file in jsonl_files:
        messages = load_transcript(
            jsonl_file, cache_manager, from_date, to_date, silent
        )
        all_messages.extend(messages)

    # Repair parent chains: progress entries create UUID gaps
    progress_chain = _scan_progress_chains(directory_path)
    _repair_parent_chains(all_messages, progress_chain)

    # Parent agent entries and assign synthetic session IDs so they
    # form separate DAG-lines spliced at their anchor points.
    _integrate_agent_entries(all_messages)

    # Collect UUIDs from unloaded subagent files (e.g. aprompt_suggestion
    # agents never referenced via agentId) to suppress orphan warnings
    unloaded_sidechain_uuids = _scan_sidechain_uuids(directory_path)

    # Build DAG and traverse (entries grouped by session, depth-first).
    # Cycle / multi-root / orphan warnings are suppressed under silent
    # so cache rebuilds and quiet CLI flows stay clean.
    with _dag_warnings_suppressed(silent):
        tree = build_dag_from_entries(
            all_messages, sidechain_uuids=unloaded_sidechain_uuids
        )
        dag_ordered = traverse_session_tree(tree)

    # Re-add summaries/ai-titles/queue-ops (excluded from DAG since they
    # lack uuid).
    non_dag_entries: list[TranscriptEntry] = [
        e
        for e in all_messages
        if isinstance(
            e,
            (
                SummaryTranscriptEntry,
                AiTitleTranscriptEntry,
                QueueOperationTranscriptEntry,
            ),
        )
    ]

    return dag_ordered + non_dag_entries, tree


# =============================================================================
# Deduplication
# =============================================================================


def deduplicate_messages(messages: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Remove duplicate messages based on (type, timestamp, sessionId, content_key).

    Messages with the exact same timestamp are duplicates by definition -
    the differences (like IDE selection tags) are just logging artifacts.

    We need a content-based key to handle two cases:
    1. Version stutter: Same message logged twice during Claude Code upgrade
       -> Same timestamp, same message.id or tool_use_id -> SHOULD deduplicate
    2. Concurrent tool results: Multiple tool results with same timestamp
       -> Same timestamp, different tool_use_ids -> should NOT deduplicate
    3. User text messages with same timestamp but different UUIDs (branch switch artifacts)
       -> Same timestamp, no tool_use_id -> SHOULD deduplicate, keep the one with most content

    Args:
        messages: List of transcript entries to deduplicate

    Returns:
        List of deduplicated messages, preserving order (first occurrence kept,
        but replaced in-place if a better version is found later)
    """
    # Track seen dedup_key -> index in deduplicated list (for in-place replacement)
    seen: dict[tuple[str, str, bool, str, str], int] = {}
    deduplicated: list[TranscriptEntry] = []

    for message in messages:
        # Get basic message type
        message_type = getattr(message, "type", "unknown")

        # For system messages, include level to differentiate info/warning/error
        if isinstance(message, SystemTranscriptEntry):
            level = message.level or "info"
            message_type = f"system-{level}"

        # Get timestamp
        timestamp = getattr(message, "timestamp", "")

        # Get isMeta flag (slash command prompts have isMeta=True with same timestamp as parent)
        is_meta = getattr(message, "isMeta", False)

        # Get sessionId for multi-session report deduplication
        session_id = getattr(message, "sessionId", "")

        # Get content key for differentiating concurrent messages
        # - For assistant messages: use message.id + content block types
        #   (stutters share the same message.id AND content types;
        #   split content blocks share message.id but have distinct types)
        # - For user messages with tool results: use first tool_use_id
        # - For user text messages: use uuid (DAG parent references must stay valid)
        # - For summary messages: use leafUuid (summaries have no timestamp/uuid)
        # - For system messages: use uuid (different system events can share a timestamp)
        # - For passthrough entries: use uuid (DAG chain nodes)
        content_key = ""
        is_user_text = False
        if isinstance(message, AssistantTranscriptEntry):
            block_types = ":".join(c.type for c in message.message.content)
            content_key = f"{message.message.id}:{block_types}"
        elif isinstance(message, UserTranscriptEntry):
            # For user messages, check for tool results
            for item in message.message.content:
                if isinstance(item, ToolResultContent):
                    content_key = item.tool_use_id
                    break
            else:
                # No tool result found - this is a user text message.
                is_user_text = True
                content_key = message.uuid
        elif isinstance(message, SummaryTranscriptEntry):
            # Summaries have no timestamp or uuid - use leafUuid to keep them distinct
            content_key = message.leafUuid
        elif isinstance(message, AiTitleTranscriptEntry):
            # ai-title entries have no timestamp/uuid; collapse duplicates
            # per session so we don't carry the same title 12x downstream.
            # The last entry wins via prepare_session_ai_titles either way,
            # but deduping here keeps message lists tidy.
            session_id = message.sessionId
            content_key = "ai-title"
        elif isinstance(
            message,
            (
                SystemTranscriptEntry,
                PassthroughTranscriptEntry,
                AttachmentTranscriptEntry,
            ),
        ):
            content_key = message.uuid

        # Create deduplication key
        dedup_key = (message_type, timestamp, is_meta, session_id, content_key)

        if dedup_key in seen:
            # For user text messages, replace if new one has more content items
            if is_user_text and isinstance(message, UserTranscriptEntry):
                idx = seen[dedup_key]
                existing = deduplicated[idx]
                if isinstance(existing, UserTranscriptEntry) and len(
                    message.message.content
                ) > len(existing.message.content):
                    deduplicated[idx] = message  # Replace with better version
            elif isinstance(message, AiTitleTranscriptEntry):
                # Always keep the most recent ai-title per session — Claude
                # Code may refine the curated title across the session.
                deduplicated[seen[dedup_key]] = message
            # Otherwise skip duplicate
        else:
            seen[dedup_key] = len(deduplicated)
            deduplicated.append(message)

    return deduplicated


@dataclass
class GenerationStats:
    """Track statistics for HTML generation across a project."""

    # Cache statistics
    files_loaded_from_cache: int = 0
    files_updated: int = 0

    # HTML generation statistics
    sessions_total: int = 0
    sessions_regenerated: int = 0
    combined_regenerated: bool = False

    # Timing (seconds)
    cache_time: float = 0.0
    render_time: float = 0.0
    total_time: float = 0.0

    # Errors/warnings collected during processing
    warnings: List[str] = field(default_factory=lambda: [])
    errors: List[str] = field(default_factory=lambda: [])

    def add_warning(self, msg: str) -> None:
        """Add a warning message."""
        self.warnings.append(msg)

    def add_error(self, msg: str) -> None:
        """Add an error message."""
        self.errors.append(msg)

    def summary(self, project_name: str) -> str:
        """Generate a concise summary line for this project."""
        parts: List[str] = [f"Project: {project_name}"]

        # Cache info
        cache_parts: List[str] = []
        if self.files_loaded_from_cache > 0:
            cache_parts.append(f"{self.files_loaded_from_cache} cached")
        if self.files_updated > 0:
            cache_parts.append(f"{self.files_updated} updated")
        if cache_parts:
            parts.append(f"  Cache: {', '.join(cache_parts)}")

        # HTML info
        html_parts: List[str] = []
        if self.sessions_total > 0:
            html_parts.append(
                f"{self.sessions_regenerated}/{self.sessions_total} sessions"
            )
        if self.combined_regenerated:
            html_parts.append("combined")
        if html_parts:
            parts.append(f"  HTML: {', '.join(html_parts)} regenerated")
        elif self.sessions_total > 0:
            parts.append("  HTML: up to date")

        # Timing
        if self.total_time > 0:
            time_str = f"  Time: {self.total_time:.1f}s"
            if self.cache_time > 0 or self.render_time > 0:
                time_str += (
                    f" (cache: {self.cache_time:.1f}s, render: {self.render_time:.1f}s)"
                )
            parts.append(time_str)

        return "\n".join(parts)


def _get_page_html_path(page_number: int, variant_suffix: str = "") -> str:
    """Get the HTML filename for a given page number.

    Page 1 is combined_transcripts{suffix}.html, page 2+ are
    combined_transcripts{suffix}_N.html. The `variant_suffix` encodes
    ``--detail``/``--compact`` variants (see `utils.variant_suffix`)
    so each variant owns its own page files and cache rows.
    """
    base = f"combined_transcripts{variant_suffix}"
    if page_number == 1:
        return f"{base}.html"
    return f"{base}_{page_number}.html"


def _variant_label_from_suffix(suffix: str) -> str:
    """Human-readable label for a filename suffix (e.g. '.low.compact')."""
    if not suffix:
        return "Full"
    parts = [p for p in suffix.split(".") if p]
    # Capitalise each segment; "compact" stays lowercased as the adverb.
    # Replace hyphens with spaces so "user-only" renders as "User only"
    # rather than "User-only" in the UI.
    nice = [p.capitalize().replace("-", " ") if p != "compact" else p for p in parts]
    return " · ".join(nice)


def _enumerate_project_variants(
    project_dir: Path, project_name: str
) -> List[Dict[str, str]]:
    """List variant entry files present in a project directory.

    Looks for top-level `combined_transcripts*.html` entries (page 1 of
    each variant), sorted so the default (full) variant comes first.
    Paginated `_N` trailers are excluded by the regex.

    Returns a list of ``{"file": relative-path, "label": human-name,
    "suffix": variant-suffix-string}`` dicts the index template can
    iterate over.
    """
    from .utils import VARIANT_ENTRY_RE

    variants: List[Dict[str, str]] = []
    if not project_dir.is_dir():
        return variants
    for entry in sorted(project_dir.glob("combined_transcripts*.html")):
        m = VARIANT_ENTRY_RE.match(entry.name)
        if m is None:
            continue
        suffix = m.group(1) or ""
        variants.append(
            {
                "file": f"{project_name}/{entry.name}",
                "label": _variant_label_from_suffix(suffix),
                "suffix": suffix,
            }
        )
    # Default (empty suffix) first, others alphabetical.
    variants.sort(key=lambda v: (v["suffix"] != "", v["suffix"]))
    return variants


# Regex pattern to match and update the next link marker block
_NEXT_LINK_PATTERN = re.compile(
    r'(<!-- PAGINATION_NEXT_LINK_START -->.*?class="page-nav-link next) last-page(".*?<!-- PAGINATION_NEXT_LINK_END -->)',
    re.DOTALL,
)


def _enable_next_link_on_previous_page(
    output_dir: Path, page_number: int, variant_suffix: str = ""
) -> bool:
    """Enable the next link on a previous page by removing the last-page class.

    When a new page is created, the previous page's "Next" link (which was hidden
    with the last-page CSS class) needs to be revealed. This function performs
    an in-place edit to remove that class.

    Args:
        output_dir: Directory containing the HTML files
        page_number: The page number whose next link should be enabled
        variant_suffix: Variant infix for path resolution.

    Returns:
        True if the file was modified, False otherwise
    """
    if page_number < 1:
        return False

    page_path = output_dir / _get_page_html_path(page_number, variant_suffix)
    if not page_path.exists():
        return False

    # ``errors="replace"`` guards against lone surrogates that may have
    # been written here previously (issue #139): pre-fix runs encoded
    # JSON-decoded surrogates straight into the HTML, and a strict
    # read-back would crash on revisit. Replacing them here lets older
    # corrupt pages still rewrite cleanly.
    content = page_path.read_text(encoding="utf-8", errors="replace")

    # Check if there's a last-page class to remove
    if "last-page" not in content:
        return False

    # Replace the pattern to remove last-page class
    new_content, count = _NEXT_LINK_PATTERN.subn(r"\1\2", content)

    if count > 0:
        page_path.write_text(new_content, encoding="utf-8", errors="replace")
        return True

    return False


def _assign_sessions_to_pages(
    sessions: Dict[str, SessionCacheData], page_size: int
) -> List[List[str]]:
    """Assign sessions to pages, never splitting sessions across pages.

    Args:
        sessions: Dict mapping session_id to SessionCacheData
        page_size: Maximum messages per page (overflow allowed to keep sessions intact)

    Returns:
        List of pages, each containing a list of session_ids
    """
    pages: List[List[str]] = []
    current_page: List[str] = []
    current_count = 0

    # Sort sessions chronologically by first_timestamp
    sorted_sessions = sorted(sessions.values(), key=lambda s: s.first_timestamp or "")

    for session in sorted_sessions:
        # Add session to current page (never split sessions)
        current_page.append(session.session_id)
        current_count += session.message_count

        # If page now exceeds limit, close it and start fresh
        if current_count > page_size:
            pages.append(current_page)
            current_page = []
            current_count = 0

    # Don't forget the last page
    if current_page:
        pages.append(current_page)

    return pages


def _build_session_data_from_messages(
    messages: List[TranscriptEntry],
) -> Dict[str, SessionCacheData]:
    """Build session data from messages when cache is unavailable.

    This is a fallback for pagination when get_cached_project_data() returns None.

    Args:
        messages: All messages (deduplicated)

    Returns:
        Dict mapping session_id to SessionCacheData
    """
    from .parser import extract_text_content

    # Pre-compute warmup session IDs to filter them out
    warmup_session_ids = get_warmup_session_ids(messages)

    # Map summaries to sessions via leafUuid -> message UUID -> session ID.
    # Mirrors _update_cache_with_session_data so the title fallback chain
    # (ai_title > summary > preview > id) survives the cache-miss path.
    uuid_to_session: Dict[str, str] = {}
    uuid_to_session_backup: Dict[str, str] = {}
    for message in messages:
        if hasattr(message, "uuid") and hasattr(message, "sessionId"):
            message_uuid = getattr(message, "uuid", "")
            session_id = getattr(message, "sessionId", "")
            if message_uuid and session_id:
                if type(message) is AssistantTranscriptEntry:
                    uuid_to_session[message_uuid] = session_id
                else:
                    uuid_to_session_backup[message_uuid] = session_id

    session_summaries: Dict[str, str] = {}
    for message in messages:
        if isinstance(message, SummaryTranscriptEntry):
            leaf_uuid = message.leafUuid
            if leaf_uuid in uuid_to_session:
                session_summaries[uuid_to_session[leaf_uuid]] = message.summary
            elif (
                leaf_uuid in uuid_to_session_backup
                and uuid_to_session_backup[leaf_uuid] not in session_summaries
            ):
                session_summaries[uuid_to_session_backup[leaf_uuid]] = message.summary

    # Map AI-generated titles to sessions (last entry per sessionId wins).
    session_ai_titles: Dict[str, str] = {}
    for message in messages:
        if isinstance(message, AiTitleTranscriptEntry):
            session_ai_titles[message.sessionId] = message.aiTitle

    # Group messages by session
    sessions: Dict[str, Dict[str, Any]] = {}
    for message in messages:
        if not hasattr(message, "sessionId") or isinstance(
            message,
            (
                SummaryTranscriptEntry,
                AiTitleTranscriptEntry,
                PassthroughTranscriptEntry,
                AttachmentTranscriptEntry,
            ),
        ):
            continue

        session_id = get_parent_session_id(getattr(message, "sessionId", ""))
        if not session_id or session_id in warmup_session_ids:
            continue

        if session_id not in sessions:
            sessions[session_id] = {
                "first_timestamp": getattr(message, "timestamp", ""),
                "last_timestamp": getattr(message, "timestamp", ""),
                "message_count": 0,
                "first_user_message": "",
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cache_read_tokens": 0,
                "team_name": None,
            }

        sessions[session_id]["message_count"] += 1
        current_timestamp = getattr(message, "timestamp", "")
        if current_timestamp:
            sessions[session_id]["last_timestamp"] = current_timestamp

        # Capture the first non-None teamName seen in the session
        # (teammates feature). Same shape as renderer.prepare_session_team_names.
        if not sessions[session_id]["team_name"]:
            tn = getattr(message, "teamName", None)
            if tn:
                sessions[session_id]["team_name"] = tn

        # Get first user message for preview
        if (
            isinstance(message, UserTranscriptEntry)
            and not sessions[session_id]["first_user_message"]
            and hasattr(message, "message")
        ):
            first_user_content = extract_text_content(message.message.content)
            if should_use_as_session_starter(first_user_content):
                sessions[session_id]["first_user_message"] = create_session_preview(
                    first_user_content
                )

        # Extract token usage from assistant messages
        if isinstance(message, AssistantTranscriptEntry) and hasattr(
            message, "message"
        ):
            msg_data = message.message
            if hasattr(msg_data, "usage") and msg_data.usage:
                usage = msg_data.usage
                sessions[session_id]["total_input_tokens"] += (
                    getattr(usage, "input_tokens", 0) or 0
                )
                sessions[session_id]["total_output_tokens"] += (
                    getattr(usage, "output_tokens", 0) or 0
                )
                sessions[session_id]["total_cache_creation_tokens"] += (
                    getattr(usage, "cache_creation_input_tokens", 0) or 0
                )
                sessions[session_id]["total_cache_read_tokens"] += (
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                )

    # Convert to Dict[str, SessionCacheData]
    result: Dict[str, SessionCacheData] = {}
    for session_id, data in sessions.items():
        result[session_id] = SessionCacheData(
            session_id=session_id,
            summary=session_summaries.get(session_id),
            ai_title=session_ai_titles.get(session_id),
            first_timestamp=data["first_timestamp"],
            last_timestamp=data["last_timestamp"],
            message_count=data["message_count"],
            first_user_message=data["first_user_message"],
            total_input_tokens=data["total_input_tokens"],
            total_output_tokens=data["total_output_tokens"],
            total_cache_creation_tokens=data["total_cache_creation_tokens"],
            total_cache_read_tokens=data["total_cache_read_tokens"],
            team_name=data["team_name"],
        )

    return result


def _generate_paginated_html(
    messages: List[TranscriptEntry],
    output_dir: Path,
    title: str,
    page_size: int,
    cache_manager: "CacheManager",
    session_data: Dict[str, SessionCacheData],
    working_directories: List[str],
    silent: bool = False,
    session_tree: Optional[SessionTree] = None,
    detail: DetailLevel = DetailLevel.FULL,
    compact: bool = False,
) -> Path:
    """Generate paginated HTML files for combined transcript.

    Args:
        messages: All messages (deduplicated)
        output_dir: Directory to write HTML files
        title: Base title for the pages
        page_size: Maximum messages per page
        cache_manager: Cache manager for the project
        session_data: Session metadata from cache
        working_directories: Working directories for project display name
        silent: Suppress verbose output

    Returns:
        Path to the first page (combined_transcripts.html)
    """
    from .html.renderer import HtmlRenderer
    from .utils import format_timestamp, variant_suffix as _variant_suffix

    suffix = _variant_suffix(detail, compact, "html")

    # Check if page size changed - if so, invalidate all pages
    cached_page_size = cache_manager.get_page_size_config()
    if cached_page_size is not None and cached_page_size != page_size:
        if not silent:
            print(
                f"Page size changed from {cached_page_size} to {page_size}, regenerating all pages"
            )
        old_paths = cache_manager.invalidate_all_pages()
        # Delete old page files
        for html_path in old_paths:
            page_file = output_dir / html_path
            if page_file.exists():
                page_file.unlink()

    # Assign sessions to pages
    pages: List[List[str]] = _assign_sessions_to_pages(session_data, page_size)

    if not pages:
        # No sessions, generate empty page
        pages = [[]]

    # Clean up orphan pages if page count decreased — scoped to this
    # variant so we don't delete another variant's live pages.
    old_page_count = cache_manager.get_page_count(suffix)
    new_page_count = len(pages)
    if old_page_count > new_page_count:
        for orphan_page_num in range(new_page_count + 1, old_page_count + 1):
            orphan_path = output_dir / _get_page_html_path(orphan_page_num, suffix)
            if orphan_path.exists():
                orphan_path.unlink()

    # Group messages by session for fast lookup (agent messages grouped
    # under their parent session since they don't have their own pages)
    messages_by_session: Dict[str, List[TranscriptEntry]] = {}
    for msg in messages:
        session_id = getattr(msg, "sessionId", None)
        if session_id:
            key = get_parent_session_id(session_id)
            if key not in messages_by_session:
                messages_by_session[key] = []
            messages_by_session[key].append(msg)

    first_page_path = output_dir / _get_page_html_path(1, suffix)

    # Generate each page
    for page_num, page_session_ids in enumerate(pages, start=1):
        html_path = _get_page_html_path(page_num, suffix)
        page_file = output_dir / html_path

        # Check if page is stale
        is_stale, reason = cache_manager.is_page_stale(page_num, page_size, suffix)

        if not is_stale and page_file.exists():
            if not silent:
                print(f"Page {page_num} is current, skipping regeneration")
            continue

        if not silent:
            print(f"Generating page {page_num} ({reason})...")

        # Collect messages for this page
        page_messages: List[TranscriptEntry] = []
        for session_id in page_session_ids:
            if session_id in messages_by_session:
                page_messages.extend(messages_by_session[session_id])

        # Calculate page stats
        page_message_count = len(page_messages)
        first_timestamp = None
        last_timestamp = None
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_creation_tokens = 0
        total_cache_read_tokens = 0

        for session_id in page_session_ids:
            if session_id in session_data:
                s = session_data[session_id]
                if s.first_timestamp and (
                    first_timestamp is None or s.first_timestamp < first_timestamp
                ):
                    first_timestamp = s.first_timestamp
                if s.last_timestamp and (
                    last_timestamp is None or s.last_timestamp > last_timestamp
                ):
                    last_timestamp = s.last_timestamp
                total_input_tokens += s.total_input_tokens
                total_output_tokens += s.total_output_tokens
                total_cache_creation_tokens += s.total_cache_creation_tokens
                total_cache_read_tokens += s.total_cache_read_tokens

        # Build page_info for navigation
        has_prev = page_num > 1
        is_last_page = page_num == len(pages)

        page_info = {
            "page_number": page_num,
            "prev_link": _get_page_html_path(page_num - 1, suffix)
            if has_prev
            else None,
            "next_link": _get_page_html_path(page_num + 1, suffix),
            "is_last_page": is_last_page,
        }

        # Enable previous page's next link when creating a new page
        if page_num > 1:
            _enable_next_link_on_previous_page(output_dir, page_num - 1, suffix)

        # Build page_stats
        date_range = ""
        if first_timestamp and last_timestamp:
            first_fmt = format_timestamp(first_timestamp)
            last_fmt = format_timestamp(last_timestamp)
            if first_fmt == last_fmt:
                date_range = first_fmt
            else:
                date_range = f"{first_fmt} - {last_fmt}"
        elif first_timestamp:
            date_range = format_timestamp(first_timestamp)

        token_parts: List[str] = []
        if total_input_tokens:
            token_parts.append(f"Input: {total_input_tokens:,}")
        if total_output_tokens:
            token_parts.append(f"Output: {total_output_tokens:,}")
        if total_cache_creation_tokens:
            token_parts.append(f"Cache Create: {total_cache_creation_tokens:,}")
        if total_cache_read_tokens:
            token_parts.append(f"Cache Read: {total_cache_read_tokens:,}")
        token_summary = " | ".join(token_parts) if token_parts else None

        page_stats = {
            "message_count": page_message_count,
            "date_range": date_range,
            "token_summary": token_summary,
        }

        # Generate HTML for this page
        page_title = f"{title} - Page {page_num}" if page_num > 1 else title
        page_renderer = HtmlRenderer()
        page_renderer.detail = detail
        page_renderer.compact = compact
        html_content = page_renderer.generate(
            page_messages,
            page_title,
            page_info=page_info,
            page_stats=page_stats,
            session_tree=session_tree,
        )
        # errors="replace": surrogateescape-decoded bytes from upstream
        # JSONL may carry lone surrogates (issue #139); strict UTF-8
        # encoding crashes here. Replace with U+FFFD so output stays
        # valid UTF-8.
        page_file.write_text(html_content, encoding="utf-8", errors="replace")

        # Update cache
        cache_manager.update_page_cache(
            page_number=page_num,
            html_path=html_path,
            page_size_config=page_size,
            session_ids=page_session_ids,
            message_count=page_message_count,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_cache_creation_tokens=total_cache_creation_tokens,
            total_cache_read_tokens=total_cache_read_tokens,
            variant_suffix=suffix,
        )

    return first_page_path


def convert_jsonl_to_html(
    input_path: Path,
    output_path: Optional[Path] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    generate_individual_sessions: bool = True,
    use_cache: bool = True,
    silent: bool = False,
    page_size: int = 2000,
) -> Path:
    """Convert JSONL transcript(s) to HTML file(s).

    Convenience wrapper around convert_jsonl_to() for HTML format.
    """
    return convert_jsonl_to(
        "html",
        input_path,
        output_path,
        from_date,
        to_date,
        generate_individual_sessions,
        use_cache,
        silent,
        page_size=page_size,
    )


def convert_jsonl_to(
    format: str,
    input_path: Path,
    output_path: Optional[Path] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    generate_individual_sessions: bool = True,
    use_cache: bool = True,
    silent: bool = False,
    image_export_mode: Optional[str] = None,
    page_size: int = 2000,
    detail: DetailLevel = DetailLevel.FULL,
    compact: bool = False,
    update_cache: bool = True,
    output_root: Optional[Path] = None,
    write_combined: bool = True,
    no_timestamps: bool = False,
) -> Path:
    """Convert JSONL transcript(s) to the specified format.

    Args:
        format: Output format ("html", "md", or "markdown").
        input_path: Path to JSONL file or directory.
        output_path: Optional output path.
        from_date: Optional start date filter.
        to_date: Optional end date filter.
        generate_individual_sessions: Whether to generate individual session files.
        use_cache: Whether to use caching.
        silent: Whether to suppress output.
        image_export_mode: Image export mode ("placeholder", "embedded", "referenced").
        page_size: Maximum messages per page for combined transcript pagination.
            If None, uses format default (embedded for HTML, referenced for Markdown).
        detail: Output detail level (full, high, low, minimal).
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    # Initialize cache manager for directory mode
    cache_manager = None
    if use_cache and input_path.is_dir():
        try:
            library_version = get_library_version()
            cache_manager = CacheManager(input_path, library_version)
        except Exception as e:
            print(f"Warning: Failed to initialize cache manager: {e}")

    ext = get_file_extension(format)

    # Initialize working_directories for both branches (used by pagination in directory mode)
    working_directories: List[str] = []

    # session_tree is populated in directory mode (DAG already built);
    # None in single-file mode (renderer builds it on demand)
    session_tree: Optional[SessionTree] = None

    from .utils import variant_suffix as _variant_suffix

    suffix = _variant_suffix(detail, compact, format, no_timestamps)

    # Output destination decoupled from `input_path` (#151). Both
    # branches below assign to `effective_output_dir`; declare it
    # upfront so pyright sees it as defined unconditionally.
    effective_output_dir: Path = output_root if output_root is not None else input_path

    if input_path.is_file():
        # Single file mode - cache only available for directory mode
        if output_path is None:
            output_path = input_path.with_suffix(f"{suffix}.{ext}")
        messages = load_transcript(input_path, silent=silent)
        # Repair progress chain gaps for single-file mode
        progress_chain = _scan_progress_chains(input_path)
        _repair_parent_chains(messages, progress_chain)
        # Parent agent entries and assign synthetic session IDs (same as
        # directory mode) so DAG-based ordering handles sidechain placement.
        _integrate_agent_entries(messages)
        title = f"Claude Transcript - {input_path.stem}"
        cache_was_updated = False  # No cache in single file mode
    else:
        # Directory mode - Cache-First Approach
        # `output_root` (#151) decouples the output destination from
        # the source `input_path` so we can write under e.g.
        # ~/Documents/Obsidian/<expanded-path>/ while still reading
        # from ~/.claude/projects/<flat>/. (`effective_output_dir`
        # is declared above the if/else; this branch only ensures the
        # destination dir exists and supplies the default output_path.)
        if output_root is not None:
            effective_output_dir.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            output_path = effective_output_dir / f"combined_transcripts{suffix}.{ext}"

        # Phase 1: Ensure cache is fresh and populated
        cache_was_updated = ensure_fresh_cache(
            input_path, cache_manager, from_date, to_date, silent
        )

        # Phase 1b: Early exit if nothing needs regeneration
        # Skip expensive message loading if all HTML is up to date
        if (
            cache_manager is not None
            and not cache_was_updated
            and from_date is None
            and to_date is None
        ):
            # Check if combined HTML is stale
            combined_stale, _ = cache_manager.is_html_stale(output_path.name, None)
            if not combined_stale and not is_html_outdated(output_path):
                # Check if any session HTML is stale
                stale_sessions = cache_manager.get_stale_sessions()
                if not stale_sessions or not generate_individual_sessions:
                    # Nothing needs regeneration - skip loading
                    if not silent:
                        print(
                            f"All HTML files are current for {input_path.name}, "
                            "skipping regeneration"
                        )
                    return output_path

        # Phase 2: Load messages (will use fresh cache when available)
        messages, session_tree = load_directory_transcripts(
            input_path, cache_manager, from_date, to_date, silent
        )

        # Get working directories from cache
        working_directories = (
            cache_manager.get_working_directories() if cache_manager else []
        )

        project_title = get_project_display_name(input_path.name, working_directories)
        title = f"Claude Transcripts - {project_title}"

    # Apply date filtering
    messages = filter_messages_by_date(messages, from_date, to_date)

    # Deduplicate messages (removes version stutters while preserving concurrent tool results)
    messages = deduplicate_messages(messages)

    # Update title to include date range if specified
    if from_date or to_date:
        date_range_parts: list[str] = []
        if from_date:
            date_range_parts.append(f"from {from_date}")
        if to_date:
            date_range_parts.append(f"to {to_date}")
        date_range_str = " ".join(date_range_parts)
        title += f" ({date_range_str})"

    # Generate combined output file (check if regeneration needed)
    assert output_path is not None
    renderer = get_renderer(
        format,
        image_export_mode,
        detail=detail,
        compact=compact,
        no_timestamps=no_timestamps,
    )

    # Decide whether to use pagination (HTML only, directory mode, no date filter)
    use_pagination = False
    cached_data = cache_manager.get_cached_project_data() if cache_manager else None
    total_message_count = (
        cached_data.total_message_count if cached_data else len(messages)
    )
    existing_page_count = cache_manager.get_page_count(suffix) if cache_manager else 0

    if (
        format == "html"
        and cache_manager is not None
        and input_path.is_dir()
        and from_date is None
        and to_date is None
    ):
        # Use pagination if total messages exceed page_size or there are existing pages
        use_pagination = total_message_count > page_size or existing_page_count > 1

    # `write_combined=False` (#151 follow-up: --combined no) skips
    # combined-transcript generation entirely. Per-session files (if
    # requested) are still produced by `_generate_individual_session_files`
    # below. The function still returns `output_path` for the caller's
    # index linking, but the file at that path is not (re-)written.
    if not write_combined:
        pass
    elif use_pagination:
        # Use paginated HTML generation
        assert cache_manager is not None  # Ensured by use_pagination condition
        # Use cached session data if available, otherwise build from messages
        if cached_data is not None:
            warmup_session_ids = get_warmup_session_ids(messages)
            current_session_ids: set[str] = set()
            for message in messages:
                session_id = getattr(message, "sessionId", "")
                if (
                    session_id
                    and session_id not in warmup_session_ids
                    and not is_agent_session(session_id)
                ):
                    current_session_ids.add(session_id)
            session_data = {
                session_id: session_cache
                for session_id, session_cache in cached_data.sessions.items()
                if session_id in current_session_ids
            }
        else:
            session_data = _build_session_data_from_messages(messages)
        output_path = _generate_paginated_html(
            messages,
            effective_output_dir,
            title,
            page_size,
            cache_manager,
            session_data,
            working_directories,
            silent=silent,
            session_tree=session_tree,
            detail=detail,
            compact=compact,
        )
    else:
        # Use single-file generation for small projects or filtered views
        # Use incremental regeneration via html_cache when available
        if cache_manager is not None and input_path.is_dir():
            is_stale, _reason = cache_manager.is_html_stale(output_path.name, None)
            should_regenerate = (
                is_stale
                or renderer.is_outdated(output_path)
                or from_date is not None
                or to_date is not None
                or not output_path.exists()
            )
        else:
            # Fallback: old logic for single file mode or no cache
            should_regenerate = (
                renderer.is_outdated(output_path)
                or from_date is not None
                or to_date is not None
                or not output_path.exists()
                or (input_path.is_dir() and cache_was_updated)
            )

        if should_regenerate:
            # For referenced images, pass the output directory
            output_dir = output_path.parent
            content = renderer.generate(
                messages, title, output_dir=output_dir, session_tree=session_tree
            )
            assert content is not None
            # See issue #139: errors="replace" for lone-surrogate safety.
            output_path.write_text(content, encoding="utf-8", errors="replace")

            # Update html_cache for combined transcript (HTML only).
            # Skip when the caller explicitly disabled cache writes — the
            # CLI does this for `-o custom.html` exports so a user's
            # one-off destination doesn't occupy a cache slot keyed by
            # their arbitrary path.
            if format == "html" and cache_manager is not None and update_cache:
                cache_manager.update_html_cache(
                    output_path.name, None, total_message_count
                )
        elif not silent:
            print(
                f"{format.upper()} file {output_path.name} is current, skipping regeneration"
            )

    # Generate individual session files if requested and in directory mode
    if generate_individual_sessions and input_path.is_dir():
        _generate_individual_session_files(
            format,
            messages,
            effective_output_dir,
            from_date,
            to_date,
            cache_manager,
            cache_was_updated,
            image_export_mode,
            silent=silent,
            session_tree=session_tree,
            detail=detail,
            compact=compact,
            write_combined=write_combined,
            no_timestamps=no_timestamps,
        )

    return output_path


def ensure_fresh_cache(
    project_dir: Path,
    cache_manager: Optional[CacheManager],
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    silent: bool = False,
) -> bool:
    """Ensure cache is fresh and populated. Returns True if cache was updated.

    This does the heavy lifting of loading and parsing files.
    """
    if cache_manager is None:
        return False

    # Check if cache needs updating
    # Exclude agent files from direct check - they are loaded via session references
    # Note: If only an agent file changes (session unchanged), cache won't detect it.
    # This is acceptable since agent files typically change alongside their sessions.
    session_jsonl_files = [
        f for f in project_dir.glob("*.jsonl") if not f.name.startswith("agent-")
    ]
    if not session_jsonl_files:
        return False

    # Get cached project data
    cached_project_data = cache_manager.get_cached_project_data()

    # Check various invalidation conditions
    modified_files = cache_manager.get_modified_files(session_jsonl_files)
    needs_update = (
        cached_project_data is None
        or from_date is not None
        or to_date is not None
        or bool(modified_files)  # Session files changed
        or (
            cached_project_data.total_message_count == 0 and session_jsonl_files
        )  # Stale cache
    )

    if not needs_update:
        return False  # Cache is already fresh

    # Load and process messages to populate cache
    if not silent:
        print(f"Updating cache for {project_dir.name}...")
    messages, _tree = load_directory_transcripts(
        project_dir, cache_manager, from_date, to_date, silent
    )

    # Update cache with fresh data
    _update_cache_with_session_data(cache_manager, messages)
    return True


def _update_cache_with_session_data(
    cache_manager: CacheManager, messages: list[TranscriptEntry]
) -> None:
    """Update cache with session and project aggregate data."""
    from .parser import extract_text_content

    # Collect session data (similar to _collect_project_sessions but for cache)
    session_summaries: dict[str, str] = {}
    uuid_to_session: dict[str, str] = {}
    uuid_to_session_backup: dict[str, str] = {}

    # Build mapping from message UUID to session ID
    for message in messages:
        if hasattr(message, "uuid") and hasattr(message, "sessionId"):
            message_uuid = getattr(message, "uuid", "")
            session_id = getattr(message, "sessionId", "")
            if message_uuid and session_id:
                if type(message) is AssistantTranscriptEntry:
                    uuid_to_session[message_uuid] = session_id
                else:
                    uuid_to_session_backup[message_uuid] = session_id

    # Map summaries to sessions
    for message in messages:
        if isinstance(message, SummaryTranscriptEntry):
            leaf_uuid = message.leafUuid
            if leaf_uuid in uuid_to_session:
                session_summaries[uuid_to_session[leaf_uuid]] = message.summary
            elif (
                leaf_uuid in uuid_to_session_backup
                and uuid_to_session_backup[leaf_uuid] not in session_summaries
            ):
                session_summaries[uuid_to_session_backup[leaf_uuid]] = message.summary

    # Map AI-generated titles to sessions (last entry per sessionId wins).
    session_ai_titles: dict[str, str] = {}
    for message in messages:
        if isinstance(message, AiTitleTranscriptEntry):
            session_ai_titles[message.sessionId] = message.aiTitle

    # Group messages by session and calculate session data
    sessions_cache_data: dict[str, SessionCacheData] = {}

    # Track token usage and timestamps for project aggregates
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_creation_tokens = 0
    total_cache_read_tokens = 0
    total_message_count = len(messages)
    earliest_timestamp = ""
    latest_timestamp = ""
    seen_request_ids: set[str] = set()

    for message in messages:
        # Update project-level timestamp tracking
        if hasattr(message, "timestamp"):
            message_timestamp = getattr(message, "timestamp", "")
            if message_timestamp:
                if not latest_timestamp or message_timestamp > latest_timestamp:
                    latest_timestamp = message_timestamp
                if not earliest_timestamp or message_timestamp < earliest_timestamp:
                    earliest_timestamp = message_timestamp

        # Process session-level data (skip summaries and ai-title — they
        # carry no DAG fields and are folded into session metadata above).
        if hasattr(message, "sessionId") and not isinstance(
            message,
            (
                SummaryTranscriptEntry,
                AiTitleTranscriptEntry,
                AttachmentTranscriptEntry,
            ),
        ):
            session_id = get_parent_session_id(getattr(message, "sessionId", ""))
            if not session_id:
                continue

            if session_id not in sessions_cache_data:
                sessions_cache_data[session_id] = SessionCacheData(
                    session_id=session_id,
                    summary=session_summaries.get(session_id),
                    ai_title=session_ai_titles.get(session_id),
                    first_timestamp=getattr(message, "timestamp", ""),
                    last_timestamp=getattr(message, "timestamp", ""),
                    message_count=0,
                    first_user_message="",
                    cwd=getattr(message, "cwd", None),
                )

            session_cache = sessions_cache_data[session_id]
            session_cache.message_count += 1
            current_timestamp = getattr(message, "timestamp", "")
            if current_timestamp:
                session_cache.last_timestamp = current_timestamp

            # Capture first non-None teamName per session (teammates feature).
            if not session_cache.team_name:
                tn = getattr(message, "teamName", None)
                if tn:
                    session_cache.team_name = tn

            # Get first user message for preview
            if (
                isinstance(message, UserTranscriptEntry)
                and not session_cache.first_user_message
                and hasattr(message, "message")
            ):
                first_user_content = extract_text_content(message.message.content)
                if should_use_as_session_starter(first_user_content):
                    session_cache.first_user_message = create_session_preview(
                        first_user_content
                    )

        # Calculate token usage for assistant messages
        if message.type == "assistant" and hasattr(message, "message"):
            assistant_message = getattr(message, "message")
            request_id = getattr(message, "requestId", None)
            session_id = get_parent_session_id(getattr(message, "sessionId", ""))

            if (
                hasattr(assistant_message, "usage")
                and assistant_message.usage
                and request_id
                and request_id not in seen_request_ids
            ):
                seen_request_ids.add(request_id)
                usage = assistant_message.usage

                # Add to project totals
                total_input_tokens += usage.input_tokens or 0
                total_output_tokens += usage.output_tokens or 0
                if usage.cache_creation_input_tokens:
                    total_cache_creation_tokens += usage.cache_creation_input_tokens
                if usage.cache_read_input_tokens:
                    total_cache_read_tokens += usage.cache_read_input_tokens

                # Add to session totals
                if session_id in sessions_cache_data:
                    session_cache = sessions_cache_data[session_id]
                    session_cache.total_input_tokens += usage.input_tokens or 0
                    session_cache.total_output_tokens += usage.output_tokens or 0
                    if usage.cache_creation_input_tokens:
                        session_cache.total_cache_creation_tokens += (
                            usage.cache_creation_input_tokens
                        )
                    if usage.cache_read_input_tokens:
                        session_cache.total_cache_read_tokens += (
                            usage.cache_read_input_tokens
                        )

    # Filter out warmup-only and empty sessions before caching
    warmup_session_ids = get_warmup_session_ids(messages)
    sessions_cache_data = {
        sid: data
        for sid, data in sessions_cache_data.items()
        if sid not in warmup_session_ids
        and data.first_user_message  # Filter empty sessions (agent-only)
    }

    # Update cache with filtered session data
    cache_manager.update_session_cache(sessions_cache_data)

    # Update cache with project aggregates
    cache_manager.update_project_aggregates(
        total_message_count=total_message_count,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_cache_creation_tokens=total_cache_creation_tokens,
        total_cache_read_tokens=total_cache_read_tokens,
        earliest_timestamp=earliest_timestamp,
        latest_timestamp=latest_timestamp,
    )


def _collect_project_sessions(messages: list[TranscriptEntry]) -> list[dict[str, Any]]:
    """Collect session data for project index navigation."""
    from .parser import extract_text_content

    # Pre-compute warmup session IDs to filter them out
    warmup_session_ids = get_warmup_session_ids(messages)

    # Pre-process to find and attach session summaries
    # This matches the logic from renderer.py generate_html() exactly
    session_summaries: dict[str, str] = {}
    uuid_to_session: dict[str, str] = {}
    uuid_to_session_backup: dict[str, str] = {}

    # Build mapping from message UUID to session ID across ALL messages
    # This allows summaries from later sessions to be matched to earlier sessions
    for message in messages:
        if hasattr(message, "uuid") and hasattr(message, "sessionId"):
            message_uuid = getattr(message, "uuid", "")
            session_id = getattr(message, "sessionId", "")
            if message_uuid and session_id:
                # There is often duplication, in that case we want to prioritise the assistant
                # message because summaries are generated from Claude's (last) success message
                if type(message) is AssistantTranscriptEntry:
                    uuid_to_session[message_uuid] = session_id
                else:
                    uuid_to_session_backup[message_uuid] = session_id

    # Map summaries to sessions via leafUuid -> message UUID -> session ID
    # Summaries can be in different sessions than the messages they summarize
    for message in messages:
        if isinstance(message, SummaryTranscriptEntry):
            leaf_uuid = message.leafUuid
            if leaf_uuid in uuid_to_session:
                session_summaries[uuid_to_session[leaf_uuid]] = message.summary
            elif (
                leaf_uuid in uuid_to_session_backup
                and uuid_to_session_backup[leaf_uuid] not in session_summaries
            ):
                session_summaries[uuid_to_session_backup[leaf_uuid]] = message.summary

    # Overlay AI-generated titles (last per session wins) — these take
    # precedence over leafUuid-mapped summaries for display purposes.
    for message in messages:
        if isinstance(message, AiTitleTranscriptEntry):
            session_summaries[message.sessionId] = message.aiTitle

    # Group messages by session (excluding warmup-only sessions,
    # coalescing agent sessions into their parent)
    sessions: dict[str, dict[str, Any]] = {}
    for message in messages:
        if hasattr(message, "sessionId") and not isinstance(
            message,
            (
                SummaryTranscriptEntry,
                AiTitleTranscriptEntry,
                AttachmentTranscriptEntry,
            ),
        ):
            session_id = get_parent_session_id(getattr(message, "sessionId", ""))
            if not session_id or session_id in warmup_session_ids:
                continue

            if session_id not in sessions:
                sessions[session_id] = {
                    "id": session_id,
                    "summary": session_summaries.get(session_id),
                    "first_timestamp": getattr(message, "timestamp", ""),
                    "last_timestamp": getattr(message, "timestamp", ""),
                    "message_count": 0,
                    "first_user_message": "",
                }

            sessions[session_id]["message_count"] += 1
            current_timestamp = getattr(message, "timestamp", "")
            if current_timestamp:
                sessions[session_id]["last_timestamp"] = current_timestamp

            # Get first user message for preview (skip system messages)
            if (
                isinstance(message, UserTranscriptEntry)
                and not sessions[session_id]["first_user_message"]
                and hasattr(message, "message")
            ):
                first_user_content = extract_text_content(message.message.content)
                if should_use_as_session_starter(first_user_content):
                    sessions[session_id]["first_user_message"] = create_session_preview(
                        first_user_content
                    )

    # Convert to list format with formatted timestamps
    session_list: list[dict[str, Any]] = []
    for session_data in sessions.values():
        timestamp_range = format_timestamp_range(
            session_data["first_timestamp"],
            session_data["last_timestamp"],
        )
        session_dict: dict[str, Any] = {
            "id": session_data["id"],
            "summary": session_data["summary"],
            "timestamp_range": timestamp_range,
            "message_count": session_data["message_count"],
            "first_user_message": session_data["first_user_message"]
            if session_data["first_user_message"] != ""
            else "[No user message found in session.]",
        }
        # Skip sessions with no user messages (empty sessions / agent-only)
        if session_data["first_user_message"] == "":
            continue
        session_list.append(session_dict)

    # Sort by first timestamp (ascending order, oldest first like transcript page)
    return sorted(
        session_list, key=lambda s: s.get("timestamp_range", ""), reverse=False
    )


def build_session_title(
    project_title: str,
    session_id: str,
    session_cache: Optional[SessionCacheData],
) -> str:
    """Build a display title for a session.

    Priority: Claude Code's curated ``ai_title`` (if any), then the
    session summary, then a 50-char-truncated first-user-message preview,
    finally "Session {id[:8]}".
    """
    if session_cache:
        if session_cache.ai_title:
            return f"{project_title}: {session_cache.ai_title}"
        if session_cache.summary:
            return f"{project_title}: {session_cache.summary}"
        preview = session_cache.first_user_message
        if preview:
            if len(preview) > 50:
                preview = preview[:50] + "..."
            return f"{project_title}: {preview}"
    return f"{project_title}: Session {session_id[:8]}"


def _generate_individual_session_files(
    format: str,
    messages: list[TranscriptEntry],
    output_dir: Path,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    cache_manager: Optional["CacheManager"] = None,
    cache_was_updated: bool = False,
    image_export_mode: Optional[str] = None,
    silent: bool = False,
    session_tree: Optional[SessionTree] = None,
    detail: DetailLevel = DetailLevel.FULL,
    compact: bool = False,
    write_combined: bool = True,
    no_timestamps: bool = False,
) -> int:
    """Generate individual files for each session in the specified format.

    Returns:
        Number of sessions regenerated
    """
    from .utils import variant_suffix as _variant_suffix

    ext = get_file_extension(format)
    suffix = _variant_suffix(detail, compact, format, no_timestamps)
    # Pre-compute warmup sessions to exclude them
    warmup_session_ids = get_warmup_session_ids(messages)

    # Find all unique session IDs (excluding warmup and agent sessions)
    session_ids: set[str] = set()
    for message in messages:
        if hasattr(message, "sessionId"):
            session_id: str = getattr(message, "sessionId")
            if (
                session_id
                and session_id not in warmup_session_ids
                and not is_agent_session(session_id)
            ):
                session_ids.add(session_id)

    # Get session data from cache for better titles
    session_data: dict[str, Any] = {}
    working_directories: list[str] = []
    if cache_manager is not None:
        project_cache = cache_manager.get_cached_project_data()
        if project_cache:
            session_data = {s.session_id: s for s in project_cache.sessions.values()}
        # Get working directories for project title
        working_directories = cache_manager.get_working_directories()

    # Only generate HTML for sessions that are tracked in the sessions table
    # (filters out warmup-only and sessions without user messages)
    session_ids = session_ids & set(session_data.keys())

    project_title = get_project_display_name(output_dir.name, working_directories)

    # Get renderer once outside the loop
    renderer = get_renderer(
        format,
        image_export_mode,
        detail=detail,
        compact=compact,
        no_timestamps=no_timestamps,
    )
    regenerated_count = 0

    # Generate HTML file for each session
    for session_id in session_ids:
        # Create session-specific title using cache data if available
        session_title = build_session_title(
            project_title,
            session_id,
            session_data.get(session_id),
        )

        # Add date range if specified
        if from_date or to_date:
            date_range_parts: list[str] = []
            if from_date:
                date_range_parts.append(f"from {from_date}")
            if to_date:
                date_range_parts.append(f"to {to_date}")
            date_range_str = " ".join(date_range_parts)
            session_title += f" ({date_range_str})"

        # Check if session file needs regeneration
        session_file_name = f"session-{session_id}{suffix}.{ext}"
        session_file_path = output_dir / session_file_name

        # Use incremental regeneration: check per-session staleness via html_cache
        if cache_manager is not None and format == "html":
            is_stale, _reason = cache_manager.is_html_stale(
                session_file_name, session_id
            )
            should_regenerate_session = (
                is_stale
                or renderer.is_outdated(session_file_path)
                or from_date is not None
                or to_date is not None
                or not session_file_path.exists()
            )
        else:
            # Fallback without cache or non-HTML formats
            should_regenerate_session = (
                renderer.is_outdated(session_file_path)
                or from_date is not None
                or to_date is not None
                or not session_file_path.exists()
                or cache_was_updated
            )

        if should_regenerate_session:
            # Generate session content. Under `--combined no` the
            # combined file is never written, so the per-session
            # back-link would 404 — suppress it.
            session_content = renderer.generate_session(
                messages,
                session_id,
                session_title,
                cache_manager,
                output_dir,
                session_tree=session_tree,
                suppress_combined_link=not write_combined,
            )
            assert session_content is not None
            # Write session file
            # See issue #139: errors="replace" for lone-surrogate safety.
            session_file_path.write_text(
                session_content, encoding="utf-8", errors="replace"
            )
            regenerated_count += 1

            # Update html_cache to track this generation (HTML only)
            if cache_manager is not None and format == "html":
                # Use message count from cache (pre-deduplication) to match
                # the count used in is_html_stale()
                if session_id in session_data:
                    session_message_count = session_data[session_id].message_count
                else:
                    # Fallback: count from messages list (less accurate due to dedup)
                    session_message_count = sum(
                        1
                        for m in messages
                        if hasattr(m, "sessionId")
                        and getattr(m, "sessionId") == session_id
                    )
                cache_manager.update_html_cache(
                    session_file_name, session_id, session_message_count
                )
        elif not silent:
            print(
                f"Session file {session_file_path.name} is current, skipping regeneration"
            )

    return regenerated_count


def generate_single_session_file(
    format: str,
    input_path: Path,
    session_id: str,
    output: Optional[Path] = None,
    use_cache: bool = True,
    image_export_mode: Optional[str] = None,
    detail: DetailLevel = DetailLevel.FULL,
    compact: bool = False,
    no_timestamps: bool = False,
) -> Path:
    """Generate a single session output file for the given session ID.

    Args:
        format: Output format ('html', 'md', 'markdown')
        input_path: Project directory containing JSONL files
        session_id: Full or 8-char prefix session ID
        output: Optional output file path (defaults to session-{id}.{ext} in input_path)
        use_cache: Whether to use caching
        image_export_mode: Image export mode
        detail: Output detail level.
        compact: Whether to merge consecutive same-type headings (Markdown only).

    Returns:
        Path to the generated file

    Raises:
        ValueError: If session ID not found or ambiguous
        FileNotFoundError: If input_path doesn't exist or is not a directory
    """
    if not input_path.exists() or not input_path.is_dir():
        raise FileNotFoundError(f"Project directory not found: {input_path}")

    # Setup cache
    cache_manager = None
    if use_cache:
        try:
            cache_manager = CacheManager(input_path, get_library_version())
        except Exception as e:
            print(f"Warning: Failed to initialize cache manager: {e}")

    # Ensure fresh cache
    ensure_fresh_cache(input_path, cache_manager, silent=True)

    # Load messages from JSONL files
    messages, _session_tree = load_directory_transcripts(input_path, cache_manager)

    # Collect all known session IDs: from loaded messages + cache metadata
    all_session_ids: set[str] = {
        getattr(msg, "sessionId")
        for msg in messages
        if hasattr(msg, "sessionId") and getattr(msg, "sessionId")
    }
    if cache_manager:
        project_cache = cache_manager.get_cached_project_data()
        if project_cache:
            all_session_ids |= set(project_cache.sessions.keys())

    # Resolve short ID prefix to full ID
    matched_id: Optional[str] = None
    if session_id in all_session_ids:
        matched_id = session_id
    else:
        matches = [sid for sid in all_session_ids if sid.startswith(session_id)]
        if len(matches) == 1:
            matched_id = matches[0]
        elif len(matches) > 1:
            raise ValueError(
                f"Ambiguous session ID prefix '{session_id}' matches multiple sessions: "
                + ", ".join(sorted(m[:8] for m in matches))
            )

    if matched_id is None:
        raise ValueError(f"Session '{session_id}' not found in {input_path}")

    # For archived sessions, load messages from cache if not in JSONL files
    session_messages = [
        m
        for m in messages
        if hasattr(m, "sessionId") and getattr(m, "sessionId") == matched_id
    ]
    if not session_messages and cache_manager:
        archived = cache_manager.load_session_entries(matched_id)
        if archived:
            session_messages = archived

    session_messages = deduplicate_messages(session_messages)

    if not session_messages:
        raise ValueError(f"No messages found for session '{matched_id[:8]}'")

    # Build session title from cache metadata
    session_data: dict[str, Any] = {}
    working_directories: list[str] = []
    if cache_manager:
        project_cache = cache_manager.get_cached_project_data()
        if project_cache:
            session_data = {s.session_id: s for s in project_cache.sessions.values()}
        working_directories = cache_manager.get_working_directories()

    project_title = get_project_display_name(input_path.name, working_directories)

    session_title = build_session_title(
        project_title,
        matched_id,
        session_data.get(matched_id),
    )

    # Determine output path
    from .utils import variant_suffix as _variant_suffix

    ext = get_file_extension(format)
    suffix = _variant_suffix(detail, compact, format, no_timestamps)
    output_dir = input_path
    if output is not None:
        # User's explicit path wins; no suffix appended.
        output_file = output
        output_dir = output.parent
    else:
        output_file = input_path / f"session-{matched_id}{suffix}.{ext}"

    # Generate content and write
    renderer = get_renderer(
        format,
        image_export_mode,
        detail=detail,
        compact=compact,
        no_timestamps=no_timestamps,
    )
    session_content = renderer.generate_session(
        session_messages, matched_id, session_title, cache_manager, output_dir
    )
    assert session_content is not None
    # See issue #139: errors="replace" for lone-surrogate safety.
    output_file.write_text(session_content, encoding="utf-8", errors="replace")

    return output_file


def _get_cleanup_period_days() -> Optional[int]:
    """Read cleanupPeriodDays from Claude Code settings.

    Checks ~/.claude/settings.json for the cleanupPeriodDays setting.

    Returns:
        The configured cleanup period in days, or None if not set/readable.
    """
    import json

    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return None

    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        return settings.get("cleanupPeriodDays")
    except (json.JSONDecodeError, OSError):
        return None


def _print_archived_sessions_note(total_archived: int) -> None:
    """Print a note about archived sessions and how to restore them.

    Args:
        total_archived: Total number of archived sessions across all projects.
    """
    cleanup_days = _get_cleanup_period_days()
    cleanup_info = (
        f" (cleanupPeriodDays: {cleanup_days})"
        if cleanup_days is not None
        else " (cleanupPeriodDays: 30 default)"
    )

    print(
        f"\nNote: {total_archived} archived session(s) found{cleanup_info}.\n"
        "  These sessions were cached before their JSONL files were deleted.\n"
        "  To restore them or adjust cleanup settings, see:\n"
        "  https://github.com/daaain/claude-code-log/blob/main/docs/restoring-archived-sessions.md"
    )


def process_projects_hierarchy(
    projects_path: Path,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    use_cache: bool = True,
    generate_individual_sessions: bool = True,
    output_format: str = "html",
    image_export_mode: Optional[str] = None,
    silent: bool = True,
    page_size: int = 2000,
    detail: DetailLevel = DetailLevel.FULL,
    compact: bool = False,
    output_dir: Optional[Path] = None,
    expand_paths: bool = False,
    filter_path: Optional[str] = None,
    write_combined: bool = True,
    no_timestamps: bool = False,
) -> Path:
    """Process the entire ~/.claude/projects/ hierarchy and create linked output files.

    Args:
        projects_path: Path to the projects directory
        from_date: Optional date filter start
        to_date: Optional date filter end
        use_cache: Whether to use SQLite cache
        generate_individual_sessions: Whether to generate per-session HTML files
        output_format: Output format (html, md, markdown)
        image_export_mode: Image export mode for markdown
        silent: If True, suppress verbose per-file logging (show summary only)
        page_size: Maximum messages per page for combined transcript pagination
        output_dir: Optional destination root for projected outputs (#151).
            When None, outputs land under each source ``project_dir`` as
            before (legacy in-place behaviour).
        expand_paths: When True (and ``output_dir`` is set), expand each
            project's flat encoded dir name to its real on-disk path
            under ``output_dir``.
        filter_path: When set, restrict to projects matching the prefix.
            See ``utils.project_destination`` for the matching semantics.
    """
    import time

    start_time = time.time()

    if not projects_path.exists():
        raise FileNotFoundError(f"Projects path not found: {projects_path}")

    # Find all project directories (those with JSONL files)
    project_dirs: list[Path] = []
    for child in projects_path.iterdir():
        if child.is_dir() and list(child.glob("*.jsonl")):
            project_dirs.append(child)

    # Find archived projects (projects in cache but without JSONL files)
    archived_project_dirs: list[Path] = []
    if use_cache:
        cached_projects = get_all_cached_projects(projects_path)
        active_project_paths = {str(p) for p in project_dirs}
        for project_path_str, is_archived in cached_projects:
            if is_archived and project_path_str not in active_project_paths:
                archived_project_dirs.append(Path(project_path_str))

    if not project_dirs and not archived_project_dirs:
        raise FileNotFoundError(
            f"No project directories with JSONL files found in {projects_path}"
        )

    # Get library version for cache management
    library_version = get_library_version()

    # Process each project directory
    project_summaries: list[dict[str, Any]] = []

    # Aggregated stats
    total_projects = len(project_dirs)
    projects_with_updates = 0
    total_sessions = 0
    total_archived = 0

    # Per-project stats for summary output
    project_stats: List[tuple[str, GenerationStats]] = []

    # `--filter-path` selection happens at the top of the loop
    # (#151). Resolve once per project — using the cache when
    # populated, else a quick JSONL peek — so `_collect_project_sessions`
    # / cache rebuilds are skipped for filtered-out projects entirely.
    from .utils import project_destination, variant_suffix as _variant_suffix

    # Combined-transcript filename. `convert_jsonl_to` writes
    # `combined_transcripts{variant}.{ext}` (e.g.
    # `combined_transcripts.low.compact.md`); the cache lookup keys,
    # `output_path` existence check, and `html_file` index entries
    # all need to use the same name. Hard-coding "combined_transcripts.html"
    # would make non-default --format / --detail / --compact
    # combinations cache-miss forever and link to the wrong file.
    variant = _variant_suffix(detail, compact, output_format, no_timestamps)
    combined_ext = get_file_extension(output_format)
    combined_name = f"combined_transcripts{variant}.{combined_ext}"

    # Index page lives at the root of whatever output destination we
    # use (either `--output` if set, or the legacy in-place projects
    # tree). Per-project `html_file` entries are relative to this root.
    index_root = output_dir if output_dir is not None else projects_path

    def _rel_to_index(p: Path) -> str:
        """Posix-form path of `p` relative to the index root.

        Returned as a forward-slash string so downstream f-strings
        (`f"{rel_dest}/..."`) embed cleanly in Markdown links and
        HTML hrefs on Windows too — `str(Path("home/joe"))` is
        `home\\joe` there, which broke the Markdown bullet-tree
        index that splits on `/`.

        The `relative_to` fallback is a paranoia rail: every
        ``project_destination`` shape produces a ``dest_dir`` that
        lives under ``index_root`` (legacy → ``projects_path``;
        ``--output`` modes → ``output_dir``)."""
        try:
            rel = p.relative_to(index_root)
        except ValueError:
            rel = p
        return rel.as_posix()

    for project_dir in sorted(project_dirs):
        project_start_time = time.time()
        stats = GenerationStats()

        try:
            # Initialize cache manager for this project
            cache_manager = None
            if use_cache:
                try:
                    cache_manager = CacheManager(project_dir, library_version)
                except Exception as e:
                    stats.add_warning(f"Failed to initialize cache: {e}")

            # Per-project destination (#151). When `output_dir` /
            # `expand_paths` / `filter_path` are unset this returns
            # `project_dir` (legacy in-place behaviour). When the
            # filter excludes this project, returns None.
            cached_working_dirs: Optional[list[str]] = None
            if cache_manager is not None:
                try:
                    cached_working_dirs = cache_manager.get_working_directories()
                except Exception:
                    cached_working_dirs = None
            dest_dir = project_destination(
                project_dir,
                output_dir=output_dir,
                expand_paths=expand_paths,
                filter_path=filter_path,
                cached_working_directories=cached_working_dirs,
            )
            if dest_dir is None:
                # Filter-out: don't process this project at all.
                if not silent:
                    print(f"  {project_dir.name}: skipped (filter)")
                continue

            # Phase 1: Fast check if anything needs updating (mtime comparison only)
            # Exclude agent files - they are loaded via session references, not directly
            jsonl_files = [
                f
                for f in project_dir.glob("*.jsonl")
                if not f.name.startswith("agent-")
            ]
            # Valid session IDs are from existing JSONL files (file stem = session ID)
            valid_session_ids = {f.stem for f in jsonl_files}
            modified_files = (
                cache_manager.get_modified_files(jsonl_files) if cache_manager else []
            )
            # Pass valid_session_ids to skip archived sessions (JSONL deleted)
            stale_sessions = (
                cache_manager.get_stale_sessions(valid_session_ids)
                if cache_manager
                else []
            )
            # Count archived sessions (cached but JSONL deleted)
            archived_count = (
                cache_manager.get_archived_session_count(valid_session_ids)
                if cache_manager
                else 0
            )
            total_archived += archived_count
            # Output destination — `dest_dir` for #151's `--output` /
            # `--expand-paths` / `--filter-path`, falling back to the
            # source project_dir for legacy in-place behaviour. Filename
            # uses the same {variant}.{ext} convention as
            # `convert_jsonl_to`.
            output_path = dest_dir / combined_name
            # Check combined_stale using the appropriate cache:
            # - Paginated projects store data in html_pages table (via save_page_cache)
            # - Non-paginated projects store data in html_cache table (via update_html_cache)
            if cache_manager is not None:
                existing_page_count = cache_manager.get_page_count(variant)
                if existing_page_count > 0:
                    # Paginated project: check page 1 staleness for the
                    # current --format/--detail/--compact variant.
                    combined_stale = cache_manager.is_page_stale(1, page_size, variant)[
                        0
                    ]
                else:
                    # Non-paginated project: check html_cache for the
                    # variant-specific filename (e.g.
                    # `combined_transcripts.low.compact.md`), not the
                    # default `combined_transcripts.html`.
                    combined_stale = cache_manager.is_html_stale(
                        output_path.name, None
                    )[0]
            else:
                combined_stale = True

            # Determine if we need to do any work. With
            # `write_combined=False`, the combined-transcript file
            # isn't produced — its staleness / on-disk presence is
            # irrelevant; only modified sources / stale per-session
            # files matter.
            if write_combined:
                needs_work = (
                    bool(modified_files)
                    or bool(stale_sessions)
                    or combined_stale
                    or not output_path.exists()
                )
            else:
                needs_work = bool(modified_files) or bool(stale_sessions)

            # Build archived suffix for output (shown on both cached and work paths)
            archived_suffix = (
                f", {archived_count} archived" if archived_count > 0 else ""
            )

            if not needs_work:
                # Fast path: nothing to do, just collect stats for index
                stats.files_loaded_from_cache = len(jsonl_files)
                stats.total_time = time.time() - project_start_time
                # Show progress
                print(
                    f"  {project_dir.name}: cached{archived_suffix} ({stats.total_time:.1f}s)"
                )
            else:
                # Slow path: update cache and regenerate output
                stats.files_updated = len(modified_files) if modified_files else 0
                stats.files_loaded_from_cache = len(jsonl_files) - stats.files_updated
                stats.sessions_regenerated = len(stale_sessions)

                if modified_files:
                    projects_with_updates += 1

                # Generate output for this project (handles cache updates internally)
                output_path = convert_jsonl_to(
                    output_format,
                    project_dir,
                    None,
                    from_date,
                    to_date,
                    generate_individual_sessions,
                    use_cache,
                    silent=silent,
                    image_export_mode=image_export_mode,
                    page_size=page_size,
                    detail=detail,
                    compact=compact,
                    output_root=(dest_dir if dest_dir != project_dir else None),
                    write_combined=write_combined,
                    no_timestamps=no_timestamps,
                )

                # Track timing
                stats.total_time = time.time() - project_start_time
                # Show progress
                progress_parts: List[str] = []
                if stats.files_updated > 0:
                    progress_parts.append(f"{stats.files_updated} files updated")
                if stats.sessions_regenerated > 0:
                    progress_parts.append(f"{stats.sessions_regenerated} sessions")
                progress_detail = (
                    ", ".join(progress_parts) if progress_parts else "regenerated"
                )
                print(
                    f"  {project_dir.name}: {progress_detail}{archived_suffix} ({stats.total_time:.1f}s)"
                )

            # Get project info for index - use cached data if available
            # Exclude agent files (they are loaded via session references)
            jsonl_files = [
                f
                for f in project_dir.glob("*.jsonl")
                if not f.name.startswith("agent-")
            ]
            jsonl_count = len(jsonl_files)
            last_modified: float = (
                max(f.stat().st_mtime for f in jsonl_files) if jsonl_files else 0.0
            )

            # Phase 3: Use fresh cached data for index aggregation
            if cache_manager is not None:
                cached_project_data = cache_manager.get_cached_project_data()
                if cached_project_data is not None:
                    # Track total sessions for stats
                    stats.sessions_total = len(cached_project_data.sessions)
                    # Path the index uses to link to this project's
                    # combined transcript (and to enumerate variants).
                    # Same as `project_dir.name` in legacy mode.
                    rel_dest = _rel_to_index(dest_dir)
                    # Use cached aggregation data
                    project_summaries.append(
                        {
                            "name": project_dir.name,
                            "path": project_dir,
                            "html_file": f"{rel_dest}/{output_path.name}",
                            "html_variants": _enumerate_project_variants(
                                dest_dir, str(rel_dest)
                            ),
                            "jsonl_count": jsonl_count,
                            "message_count": cached_project_data.total_message_count,
                            "last_modified": last_modified,
                            "total_input_tokens": cached_project_data.total_input_tokens,
                            "total_output_tokens": cached_project_data.total_output_tokens,
                            "total_cache_creation_tokens": cached_project_data.total_cache_creation_tokens,
                            "total_cache_read_tokens": cached_project_data.total_cache_read_tokens,
                            "latest_timestamp": cached_project_data.latest_timestamp,
                            "earliest_timestamp": cached_project_data.earliest_timestamp,
                            "working_directories": cache_manager.get_working_directories(),
                            "is_archived": False,
                            "combined_suppressed": not write_combined,
                            "sessions": [
                                {
                                    "id": session_data.session_id,
                                    # Display title: ai_title (Claude Code's
                                    # curated short title) wins over summary.
                                    "summary": session_data.ai_title
                                    or session_data.summary,
                                    "timestamp_range": format_timestamp_range(
                                        session_data.first_timestamp,
                                        session_data.last_timestamp,
                                    ),
                                    "first_timestamp": session_data.first_timestamp,
                                    "last_timestamp": session_data.last_timestamp,
                                    "message_count": session_data.message_count,
                                    "first_user_message": session_data.first_user_message
                                    or "[No user message found in session.]",
                                    # Per-session link relative to the index
                                    # root. Used by the index renderer when
                                    # `combined_suppressed` is True so the
                                    # index can link directly to the
                                    # `session-{id}{variant}.{ext}` files
                                    # written by ``_generate_individual_session_files``
                                    # — the ``{variant}`` infix (e.g. ``.low``,
                                    # ``.high``) must match or links 404.
                                    "file": (
                                        f"{rel_dest}/session-{session_data.session_id}{variant}.{combined_ext}"
                                    ),
                                }
                                for session_data in cached_project_data.sessions.values()
                                # Filter out warmup-only and empty sessions (agent-only)
                                # AND synthetic agent sessions
                                # (`{sid}#agent-{aid}` — `_integrate_agent_entries`
                                # inlines them into the parent's transcript;
                                # `_generate_individual_session_files` skips them
                                # too, so a link in the index would 404).
                                if session_data.first_user_message
                                and session_data.first_user_message != "Warmup"
                                and not is_agent_session(session_data.session_id)
                            ],
                            # Distinct teamName values across this project's
                            # sessions (teammates feature). Powers the
                            # "Team: …" annotation on the project card.
                            "team_names": sorted(
                                {
                                    s.team_name
                                    for s in cached_project_data.sessions.values()
                                    if s.team_name
                                }
                            ),
                        }
                    )
                    # Add project stats
                    project_stats.append((project_dir.name, stats))
                    continue

            # Fallback for when cache is not available (should be rare)
            print(
                f"Warning: No cached data available for {project_dir.name}, using fallback processing"
            )
            messages, _tree = load_directory_transcripts(
                project_dir, cache_manager, from_date, to_date, silent=silent
            )
            # Ensure cache is populated with session data (including working directories)
            if cache_manager:
                _update_cache_with_session_data(cache_manager, messages)
            if from_date or to_date:
                messages = filter_messages_by_date(messages, from_date, to_date)

            # Calculate token usage aggregation and find first/last interaction timestamps
            total_input_tokens = 0
            total_output_tokens = 0
            total_cache_creation_tokens = 0
            total_cache_read_tokens = 0
            latest_timestamp = ""
            earliest_timestamp = ""

            # Track requestIds to avoid double-counting tokens
            seen_request_ids: set[str] = set()

            # Collect session data for this project
            sessions_data = _collect_project_sessions(messages)

            for message in messages:
                # Track latest and earliest timestamps across all messages
                if hasattr(message, "timestamp"):
                    message_timestamp = getattr(message, "timestamp", "")
                    if message_timestamp:
                        # Track latest timestamp
                        if not latest_timestamp or message_timestamp > latest_timestamp:
                            latest_timestamp = message_timestamp

                        # Track earliest timestamp
                        if (
                            not earliest_timestamp
                            or message_timestamp < earliest_timestamp
                        ):
                            earliest_timestamp = message_timestamp

                # Calculate token usage for assistant messages
                if message.type == "assistant" and hasattr(message, "message"):
                    assistant_message = getattr(message, "message")
                    request_id = getattr(message, "requestId", None)

                    if (
                        hasattr(assistant_message, "usage")
                        and assistant_message.usage
                        and request_id
                        and request_id not in seen_request_ids
                    ):
                        # Mark requestId as seen to avoid double-counting
                        seen_request_ids.add(request_id)

                        usage = assistant_message.usage
                        total_input_tokens += usage.input_tokens or 0
                        total_output_tokens += usage.output_tokens or 0
                        if usage.cache_creation_input_tokens:
                            total_cache_creation_tokens += (
                                usage.cache_creation_input_tokens
                            )
                        if usage.cache_read_input_tokens:
                            total_cache_read_tokens += usage.cache_read_input_tokens

            # Distinct teamName values across this project's sessions.
            # Mirror the cached path's filtering: skip warmup-only
            # sessions, coalesce agent synthetic-sessionIds into their
            # parent, and only consider non-summary entries (matches
            # _collect_project_sessions / _update_cache_with_session_data
            # so cached and no-cache paths produce the same annotation).
            warmup_for_teams = get_warmup_session_ids(messages)
            team_name_per_session: dict[str, str] = {}
            for _msg in messages:
                if isinstance(
                    _msg,
                    (
                        SummaryTranscriptEntry,
                        AiTitleTranscriptEntry,
                        AttachmentTranscriptEntry,
                    ),
                ):
                    continue
                if not hasattr(_msg, "sessionId"):
                    continue
                _sid = get_parent_session_id(getattr(_msg, "sessionId", ""))
                if not _sid or _sid in warmup_for_teams:
                    continue
                _tn = getattr(_msg, "teamName", None)
                if _tn and _sid not in team_name_per_session:
                    team_name_per_session[_sid] = _tn
            team_names_set: set[str] = set(team_name_per_session.values())

            rel_dest = _rel_to_index(dest_dir)
            # Post-decorate `sessions_data` with per-session file links
            # (matches the cached path's shape so the index renderer
            # can use `session.file` uniformly under
            # `combined_suppressed`).
            for _sd in sessions_data:
                if "file" not in _sd:
                    # `{variant}` mirrors the on-disk session filename
                    # (`session-{id}{variant}.{ext}`) so the index link
                    # resolves under `--detail low|high|...`.
                    _sd["file"] = (
                        f"{rel_dest}/session-{_sd['id']}{variant}.{combined_ext}"
                    )
            project_summaries.append(
                {
                    "name": project_dir.name,
                    "path": project_dir,
                    "html_file": f"{rel_dest}/{output_path.name}",
                    "html_variants": _enumerate_project_variants(
                        dest_dir, str(rel_dest)
                    ),
                    "jsonl_count": jsonl_count,
                    "message_count": len(messages),
                    "last_modified": last_modified,
                    "total_input_tokens": total_input_tokens,
                    "total_output_tokens": total_output_tokens,
                    "total_cache_creation_tokens": total_cache_creation_tokens,
                    "total_cache_read_tokens": total_cache_read_tokens,
                    "latest_timestamp": latest_timestamp,
                    "earliest_timestamp": earliest_timestamp,
                    "working_directories": cache_manager.get_working_directories()
                    if cache_manager
                    else [],
                    "is_archived": False,
                    "combined_suppressed": not write_combined,
                    "sessions": sessions_data,
                    "team_names": sorted(team_names_set),
                }
            )
            # Track session count in stats for fallback path
            stats.sessions_total = len(sessions_data)
            project_stats.append((project_dir.name, stats))

        except Exception as e:
            prev_project = project_summaries[-1] if project_summaries else "(none)"
            stats.add_error(str(e))
            project_stats.append((project_dir.name, stats))
            print(
                f"Warning: Failed to process {project_dir}: {e}\n"
                f"Previous (in alphabetical order) project before error: {prev_project}"
                f"\n{traceback.format_exc()}"
            )
            continue

    # Process archived projects (projects in cache but without JSONL files)
    archived_project_count = 0
    for archived_dir in sorted(archived_project_dirs):
        try:
            # Initialize cache manager for archived project
            cache_manager = CacheManager(archived_dir, library_version)
            cached_project_data = cache_manager.get_cached_project_data()

            if cached_project_data is None:
                continue

            # Apply --filter-path / --expand-paths to archived
            # projects too. Note: archived dirs have no JSONLs to peek,
            # so resolution falls back to cache (which exists for
            # archived projects) or naive last-resort.
            archived_cached_dirs: Optional[list[str]] = None
            try:
                archived_cached_dirs = cache_manager.get_working_directories()
            except Exception:
                archived_cached_dirs = None
            archived_dest = project_destination(
                archived_dir,
                output_dir=output_dir,
                expand_paths=expand_paths,
                filter_path=filter_path,
                cached_working_directories=archived_cached_dirs,
            )
            if archived_dest is None:
                continue

            archived_project_count += 1
            print(
                f"  {archived_dir.name}: [ARCHIVED] ({len(cached_project_data.sessions)} sessions)"
            )

            # Index entry for an archived project; the file may not
            # exist at the projected path until the user re-renders.
            archived_rel = _rel_to_index(archived_dest)
            project_summaries.append(
                {
                    "name": archived_dir.name,
                    "path": archived_dir,
                    "html_file": f"{archived_rel}/{combined_name}",
                    "html_variants": _enumerate_project_variants(
                        archived_dest, str(archived_rel)
                    ),
                    "jsonl_count": 0,
                    "message_count": cached_project_data.total_message_count,
                    "last_modified": 0.0,
                    "total_input_tokens": cached_project_data.total_input_tokens,
                    "total_output_tokens": cached_project_data.total_output_tokens,
                    "total_cache_creation_tokens": cached_project_data.total_cache_creation_tokens,
                    "total_cache_read_tokens": cached_project_data.total_cache_read_tokens,
                    "latest_timestamp": cached_project_data.latest_timestamp,
                    "earliest_timestamp": cached_project_data.earliest_timestamp,
                    "working_directories": cache_manager.get_working_directories(),
                    "is_archived": True,
                    "combined_suppressed": not write_combined,
                    "sessions": [
                        {
                            "id": session_data.session_id,
                            "summary": session_data.ai_title or session_data.summary,
                            "timestamp_range": format_timestamp_range(
                                session_data.first_timestamp,
                                session_data.last_timestamp,
                            ),
                            "first_timestamp": session_data.first_timestamp,
                            "last_timestamp": session_data.last_timestamp,
                            "message_count": session_data.message_count,
                            "first_user_message": session_data.first_user_message
                            or "[No user message found in session.]",
                            # `{variant}` keeps the link in step with
                            # `_generate_individual_session_files`'s
                            # filename (`session-{id}{variant}.{ext}`).
                            "file": (
                                f"{archived_rel}/session-{session_data.session_id}{variant}.{combined_ext}"
                            ),
                        }
                        for session_data in cached_project_data.sessions.values()
                        # Same filter as the live-cached path above:
                        # warmup-only / empty / agent sessions don't
                        # belong in the index.
                        if session_data.first_user_message
                        and session_data.first_user_message != "Warmup"
                        and not is_agent_session(session_data.session_id)
                    ],
                    # Distinct teamName values across this archived project's
                    # cached sessions (teammates feature).
                    "team_names": sorted(
                        {
                            s.team_name
                            for s in cached_project_data.sessions.values()
                            if s.team_name
                        }
                    ),
                }
            )
        except Exception as e:
            print(f"Warning: Failed to process archived project {archived_dir}: {e}")
            continue

    # Update total projects count to include archived
    total_projects = len(project_dirs) + archived_project_count

    # Generate index — always regenerated. Skipping when "nothing
    # changed" would let stale links survive a variant-flag toggle
    # (e.g. `--compact` / `--no-timestamps` / `--detail`), which
    # produces new per-project filenames without touching the cache.
    # The index is built from the already-aggregated
    # `project_summaries` in memory (one template pass + one write),
    # so unconditional regeneration is cheap.
    index_path = index_root / get_index_filename(output_format)
    renderer = get_renderer(output_format, image_export_mode)
    # Under `--expand-paths` (Obsidian mode), both Markdown and HTML
    # render the index as a nested directory hierarchy that mirrors
    # the projected folder tree. JSON keeps a flat list (structured
    # data — tree shape isn't meaningful) so it does not accept the
    # kwarg.
    index_kwargs: dict[str, Any] = {}
    if expand_paths and output_format in ("md", "markdown", "html"):
        index_kwargs["expand_paths_tree"] = True
    index_content = renderer.generate_projects_index(
        project_summaries, from_date, to_date, **index_kwargs
    )
    assert index_content is not None
    # Ensure the index root exists when projecting into a fresh dir.
    index_path.parent.mkdir(parents=True, exist_ok=True)
    # See issue #139: errors="replace" for lone-surrogate safety.
    index_path.write_text(index_content, encoding="utf-8", errors="replace")

    # Count total sessions from project summaries
    for summary in project_summaries:
        total_sessions += len(summary.get("sessions", []))

    # Print summary
    elapsed = time.time() - start_time

    # Print any errors/warnings that occurred
    for project_name, stats in project_stats:
        for warning in stats.warnings:
            print(f"  Warning ({project_name}): {warning}")
        for error in stats.errors:
            print(f"  Error ({project_name}): {error}")

    # Global summary
    summary_parts: List[str] = []
    summary_parts.append(f"Processed {total_projects} projects in {elapsed:.1f}s")
    if projects_with_updates > 0:
        summary_parts.append(f"  {projects_with_updates} projects updated")
    print("\n".join(summary_parts))

    # Show archived sessions note if any exist
    if total_archived > 0:
        _print_archived_sessions_note(total_archived)

    return index_path

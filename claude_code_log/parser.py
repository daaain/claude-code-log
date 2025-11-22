#!/usr/bin/env python3
"""Parse and extract data from Claude transcript JSONL files."""

import json
from pathlib import Path
import re
from typing import Any, List, Optional, Union, TYPE_CHECKING
from datetime import datetime
import dateparser

from .models import (
    TranscriptEntry,
    UserTranscriptEntry,
    SummaryTranscriptEntry,
    parse_transcript_entry,
    ContentItem,
    TextContent,
    ThinkingContent,
)

if TYPE_CHECKING:
    from .cache import CacheManager


def extract_text_content(content: Union[str, List[ContentItem], None]) -> str:
    """Extract text content from Claude message content structure (supports both custom and Anthropic types)."""
    if content is None:
        return ""
    if isinstance(content, list):
        text_parts: List[str] = []
        for item in content:
            # Handle both custom TextContent and official Anthropic TextBlock
            if isinstance(item, TextContent):
                text_parts.append(item.text)
            elif (
                hasattr(item, "type")
                and hasattr(item, "text")
                and getattr(item, "type") == "text"
            ):
                # Official Anthropic TextBlock
                text_parts.append(getattr(item, "text"))
            elif isinstance(item, ThinkingContent):
                # Skip thinking content in main text extraction
                continue
            elif hasattr(item, "type") and getattr(item, "type") == "thinking":
                # Skip official Anthropic thinking content too
                continue
        return "\n".join(text_parts)
    else:
        return str(content) if content else ""


def parse_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse ISO timestamp to datetime object."""
    try:
        return datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def filter_messages_by_date(
    messages: List[TranscriptEntry], from_date: Optional[str], to_date: Optional[str]
) -> List[TranscriptEntry]:
    """Filter messages based on date range."""
    if not from_date and not to_date:
        return messages

    # Parse the date strings using dateparser
    from_dt = None
    to_dt = None

    if from_date:
        from_dt = dateparser.parse(from_date)
        if not from_dt:
            raise ValueError(f"Could not parse from-date: {from_date}")
        # If parsing relative dates like "today", start from beginning of day
        if from_date in ["today", "yesterday"] or "days ago" in from_date:
            from_dt = from_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    if to_date:
        to_dt = dateparser.parse(to_date)
        if not to_dt:
            raise ValueError(f"Could not parse to-date: {to_date}")
        # If parsing relative dates like "today", end at end of day
        if to_date in ["today", "yesterday"] or "days ago" in to_date:
            to_dt = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    filtered_messages: List[TranscriptEntry] = []
    for message in messages:
        # Handle SummaryTranscriptEntry which doesn't have timestamp
        if isinstance(message, SummaryTranscriptEntry):
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
) -> List[TranscriptEntry]:
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
    messages: List[TranscriptEntry] = []
    agent_ids: set[str] = set()  # Collect agentId references while parsing

    with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
        if not silent:
            print(f"Processing {jsonl_path}...")
        for line_no, line in enumerate(f):
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
                            agent_id_value = tool_use_result.get("agentId")  # type: ignore[reportUnknownVariableType, reportUnknownMemberType]
                            if isinstance(agent_id_value, str):
                                agent_ids.add(agent_id_value)
                                # Copy agentId to top level for Pydantic to preserve
                                entry_dict["agentId"] = agent_id_value

                    entry_type: str | None = entry_dict.get("type")

                    if entry_type in [
                        "user",
                        "assistant",
                        "summary",
                        "system",
                        "queue-operation",
                    ]:
                        # Parse using Pydantic models
                        entry = parse_transcript_entry(entry_dict)
                        messages.append(entry)
                    else:
                        print(
                            f"Line {line_no} of {jsonl_path} is not a recognised message type: {line}"
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
                            "\n{traceback.format_exc()}"
                        )
                except Exception as e:
                    print(
                        f"Line {line_no} of {jsonl_path} | Unexpected error: {str(e)}"
                        "\n{traceback.format_exc()}"
                    )

    # Load agent files if any were referenced
    # Build a map of agentId -> agent messages
    agent_messages_map: dict[str, List[TranscriptEntry]] = {}
    if agent_ids:
        parent_dir = jsonl_path.parent
        for agent_id in agent_ids:
            agent_file = parent_dir / f"agent-{agent_id}.jsonl"
            # Skip if the agent file is the same as the current file (self-reference)
            if agent_file == jsonl_path:
                continue
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

    # Insert agent messages at their point of use
    if agent_messages_map:
        # Iterate through messages and insert agent messages after the message
        # that references them (via UserTranscriptEntry.agentId)
        result_messages: List[TranscriptEntry] = []
        for message in messages:
            result_messages.append(message)

            # Check if this is a UserTranscriptEntry with agentId
            if isinstance(message, UserTranscriptEntry) and message.agentId:
                agent_id = message.agentId
                if agent_id in agent_messages_map:
                    # Insert agent messages right after this message
                    result_messages.extend(agent_messages_map[agent_id])

        messages = result_messages

    # Save to cache if cache manager is available
    if cache_manager is not None:
        cache_manager.save_cached_entries(jsonl_path, messages)

    return messages


def load_directory_transcripts(
    directory_path: Path,
    cache_manager: Optional["CacheManager"] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    silent: bool = False,
) -> List[TranscriptEntry]:
    """Load all JSONL transcript files from a directory and combine them."""
    all_messages: List[TranscriptEntry] = []

    # Find all .jsonl files
    jsonl_files = list(directory_path.glob("*.jsonl"))

    for jsonl_file in jsonl_files:
        messages = load_transcript(
            jsonl_file, cache_manager, from_date, to_date, silent
        )
        all_messages.extend(messages)

    # Sort all messages chronologically
    def get_timestamp(entry: TranscriptEntry) -> str:
        if hasattr(entry, "timestamp"):
            return entry.timestamp  # type: ignore
        return ""

    all_messages.sort(key=get_timestamp)
    return all_messages

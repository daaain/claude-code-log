"""Tests for ``--branches main`` (abandoned rewind-fork pruning).

Rewinding in Claude Code appends a message whose ``parentUuid`` points at an
earlier message rather than deleting anything, so the transcript is a tree and
abandoned attempts survive in the JSONL. See ``select_main_lines``.
"""

from typing import Any

from claude_code_log.dag import (
    build_dag,
    build_message_index,
    extract_session_dag_lines,
    filter_to_main_line,
    select_main_lines,
)
from claude_code_log.factories import create_transcript_entry
from claude_code_log.models import TranscriptEntry


def _user(uuid: str, parent: str | None, text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "sessionId": "s1",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "2.0.0",
        "message": {"role": "user", "content": text},
    }


def _assistant(uuid: str, parent: str, text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "sessionId": "s1",
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "2.0.0",
        "message": {
            "id": f"msg_{uuid}",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4",
            "content": [{"type": "text", "text": text}],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1, "service_tier": "standard"},
        },
    }


def _parse(rows: list[dict[str, Any]]) -> list[TranscriptEntry]:
    return [create_transcript_entry(row) for row in rows]


def _rewound_session() -> list[TranscriptEntry]:
    """u1 → a1 → (u2a → a2a  |  u2b → a2b → u3 → a3).

    ``u2a`` was rewound away: the user edited the prompt into ``u2b`` and the
    session continued there. The abandoned attempt keeps its own reply.
    """
    return _parse(
        [
            _user("u1", None, "first question", "2026-07-25T10:00:00Z"),
            _assistant("a1", "u1", "first answer", "2026-07-25T10:00:10Z"),
            _user("u2a", "a1", "abandoned attempt", "2026-07-25T10:01:00Z"),
            _assistant("a2a", "u2a", "abandoned reply", "2026-07-25T10:01:10Z"),
            _user("u2b", "a1", "retyped question", "2026-07-25T10:02:00Z"),
            _assistant("a2b", "u2b", "real answer", "2026-07-25T10:02:10Z"),
            _user("u3", "a2b", "follow-up", "2026-07-25T10:03:00Z"),
            _assistant("a3", "u3", "final answer", "2026-07-25T10:03:10Z"),
        ]
    )


def _uuids(entries: list[TranscriptEntry]) -> set[str]:
    return {u for e in entries if (u := getattr(e, "uuid", None)) is not None}


def _lines(entries: list[TranscriptEntry]):
    nodes = build_message_index(entries)
    build_dag(nodes)
    return extract_session_dag_lines(nodes)


def test_abandoned_branch_is_dropped() -> None:
    """The rewound attempt and its reply disappear; the longer path survives."""
    kept = _uuids(filter_to_main_line(_rewound_session()))
    assert "u2a" not in kept
    assert "a2a" not in kept
    assert {"u1", "a1", "u2b", "a2b", "u3", "a3"} <= kept


def test_longest_path_wins_not_latest_timestamp() -> None:
    """A stray prompt typed after the real work must not win the selection.

    The newest leaf in a real transcript is routinely a one-line afterthought;
    selecting by recency would keep it and discard everything else.
    """
    entries = _rewound_session()
    entries.extend(
        _parse([_user("stray", "a1", "oops", "2026-07-25T23:59:00Z")])
    )
    kept = _uuids(filter_to_main_line(entries))
    assert "stray" not in kept
    assert {"u3", "a3"} <= kept


def test_all_branches_kept_without_pruning() -> None:
    """Default behaviour is unchanged: every fork is still a DAG-line."""
    sessions = _lines(_rewound_session())
    assert sum(1 for line in sessions.values() if line.is_branch) >= 1
    total = sum(len(line.uuids) for line in sessions.values())
    assert total == 8


def test_pruned_result_is_linear() -> None:
    """After pruning there is nothing left to fork, so no branch lines remain."""
    pruned = filter_to_main_line(_rewound_session())
    sessions = _lines(pruned)
    assert not any(line.is_branch for line in sessions.values())


def test_ancestor_truncated_at_fork_point() -> None:
    """The trunk keeps its history up to the fork and loses the dead tail."""
    sessions = _lines(_rewound_session())
    kept = select_main_lines(sessions)
    surviving = {uuid for line in kept.values() for uuid in line.uuids}
    assert "a1" in surviving  # fork point itself is history, not a casualty
    assert "a2a" not in surviving


def test_unforked_session_is_untouched() -> None:
    """A transcript with no rewinds must pass through byte-identical."""
    entries = _parse(
        [
            _user("u1", None, "question", "2026-07-25T10:00:00Z"),
            _assistant("a1", "u1", "answer", "2026-07-25T10:00:10Z"),
        ]
    )
    assert filter_to_main_line(entries) == entries


def test_entries_without_uuid_pass_through() -> None:
    """Summaries carry no uuid and never participate in the DAG."""
    entries = _rewound_session()
    summary = create_transcript_entry(
        {"type": "summary", "summary": "A session", "leafUuid": "a3"}
    )
    entries.append(summary)
    assert summary in filter_to_main_line(entries)

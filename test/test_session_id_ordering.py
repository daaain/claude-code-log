"""Directory-mode chronological ordering of UUID-less queue-op/steering entries
(issue #295).

`load_directory_transcripts` builds the DAG from entries that carry a uuid and
re-adds the UUID-less ones (summaries, ai-titles, queue-operations) afterwards.
Before the fix it appended them *at the end*, so a `remove` steering entry that
belongs in the middle of a conversation rendered after the last assistant
message — the visible #295 symptom, in `--session-id` export AND the default
combined directory render. These tests pin that queue-op entries land in
chronological position instead.

Synthetic fixtures only (no private data — see the #295 spec).
"""

import json
from pathlib import Path

from claude_code_log.converter import (
    compute_session_data,
    convert_jsonl_to_html,
    generate_single_session_file,
    load_directory_transcripts,
)
from claude_code_log.models import QueueOperationTranscriptEntry

_SID = "sess-order-1"


def _user(uuid, parent, text, ts):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": "2.1.207",
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _assistant(uuid, parent, text, ts):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": _SID,
        "version": "2.1.207",
        "timestamp": ts,
        "message": {
            "id": "msg-" + uuid,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-4",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _remove(text, ts):
    """Legacy queue-operation 'remove' — chain-less, uuid-less (steering)."""
    return {
        "type": "queue-operation",
        "operation": "remove",
        "timestamp": ts,
        "content": text,
        "sessionId": _SID,
    }


def _write_project(tmp_path: Path, entries) -> Path:
    d = tmp_path / "-proj-order"
    d.mkdir()
    (d / f"{_SID}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
    )
    return d


def _index(messages, pred) -> int:
    return next(i for i, m in enumerate(messages) if pred(m))


def test_dir_mode_places_steering_in_chronological_position(tmp_path):
    """A 'remove' steering entry whose timestamp sits between a1 and u2 must
    load in that position, not be appended after the last assistant (#295)."""
    entries = [
        _user("u1", None, "start", "2026-07-11T07:00:00.000Z"),
        _assistant("a1", "u1", "working", "2026-07-11T07:00:01.000Z"),
        _remove("please focus on the parser", "2026-07-11T07:00:02.000Z"),
        _user("u2", "a1", "next", "2026-07-11T07:00:03.000Z"),
        _assistant("a2", "u2", "ok", "2026-07-11T07:00:04.000Z"),
    ]
    d = _write_project(tmp_path, entries)

    messages, _tree = load_directory_transcripts(d, None, silent=True)

    i_a1 = _index(messages, lambda m: getattr(m, "uuid", None) == "a1")
    i_u2 = _index(messages, lambda m: getattr(m, "uuid", None) == "u2")
    i_rm = _index(
        messages,
        lambda m: isinstance(m, QueueOperationTranscriptEntry)
        and m.operation == "remove",
    )

    assert i_a1 < i_rm < i_u2, (
        f"steering 'remove' at index {i_rm} not in chronological position "
        f"(expected between a1@{i_a1} and u2@{i_u2}); it was appended out of "
        "order — the #295 bug"
    )


def test_session_id_export_renders_steering_in_position(tmp_path):
    """The user-visible #295 path: a `--session-id` export (directory mode)
    must render a mid-conversation steering card BEFORE the final assistant
    message, not dumped after it."""
    entries = [
        _user("u1", None, "start", "2026-07-11T07:00:00.000Z"),
        _assistant("a1", "u1", "working", "2026-07-11T07:00:01.000Z"),
        _remove("STEER_MID_MARKER", "2026-07-11T07:00:02.000Z"),
        _user("u2", "a1", "next", "2026-07-11T07:00:03.000Z"),
        _assistant("a2", "u2", "FINAL_ASSISTANT_MARKER", "2026-07-11T07:00:04.000Z"),
    ]
    d = _write_project(tmp_path, entries)

    out = generate_single_session_file(
        "html", d, _SID, tmp_path / "out.html", use_cache=False
    )
    html = out.read_text(encoding="utf-8")

    assert "STEER_MID_MARKER" in html and "FINAL_ASSISTANT_MARKER" in html
    # Steering (07:00:02) precedes the final assistant (07:00:04) chronologically,
    # so its card must appear earlier in the top-to-bottom render — not after it.
    assert html.index("STEER_MID_MARKER") < html.index("FINAL_ASSISTANT_MARKER"), (
        "steering card rendered AFTER the final assistant message (#295): the "
        "mid-conversation 'remove' was appended at the end instead of placed "
        "in chronological position"
    )


def test_combined_directory_render_places_steering_in_position(tmp_path):
    """The wider-impact surface: the default combined directory render
    (`combined_transcripts.html`) shares the same load path, so it must show
    the same correction — proving #295 was never a `--session-id`-only bug."""
    entries = [
        _user("u1", None, "start", "2026-07-11T07:00:00.000Z"),
        _assistant("a1", "u1", "working", "2026-07-11T07:00:01.000Z"),
        _remove("STEER_MID_MARKER", "2026-07-11T07:00:02.000Z"),
        _user("u2", "a1", "next", "2026-07-11T07:00:03.000Z"),
        _assistant("a2", "u2", "FINAL_ASSISTANT_MARKER", "2026-07-11T07:00:04.000Z"),
    ]
    d = _write_project(tmp_path, entries)

    out = convert_jsonl_to_html(d, use_cache=False)
    html = out.read_text(encoding="utf-8")

    assert "STEER_MID_MARKER" in html and "FINAL_ASSISTANT_MARKER" in html
    assert html.index("STEER_MID_MARKER") < html.index("FINAL_ASSISTANT_MARKER")


def test_last_timestamp_reflects_chronologically_last_entry(tmp_path):
    """Intent pin (issue #295 follow-up): a session's ``last_timestamp`` must
    equal the timestamp of its chronologically LAST entry, not whichever entry
    happens to be iterated last.

    Deliberately implementation-agnostic: it passes under today's
    last-iterated computation *once the merge places entries chronologically*,
    AND under a future ``max()`` refactor. It fails on the pre-fix code, where
    the mid-conversation ``remove`` (07:00:02) was appended last and became the
    session's reported end-time instead of the real final entry (07:00:04).
    See the backlog note on ``compute_session_data`` order-dependence.
    """
    entries = [
        _user("u1", None, "start", "2026-07-11T07:00:00.000Z"),
        _assistant("a1", "u1", "working", "2026-07-11T07:00:01.000Z"),
        _remove("mid-conversation steering", "2026-07-11T07:00:02.000Z"),
        _user("u2", "a1", "next", "2026-07-11T07:00:03.000Z"),
        _assistant("a2", "u2", "final", "2026-07-11T07:00:04.000Z"),
    ]
    d = _write_project(tmp_path, entries)

    messages, _tree = load_directory_transcripts(d, None, silent=True)
    session_data = compute_session_data(messages)

    assert session_data[_SID].last_timestamp == "2026-07-11T07:00:04.000Z", (
        "session end-time should reflect the chronologically last entry "
        "(a2 @ 07:00:04), not the out-of-order 'remove' (07:00:02)"
    )


def test_timestamp_tie_places_queue_ops_after_same_timestamp_entry(tmp_path):
    """Tie-break (the COMMON path — ~30% of real-archive queue-ops share a
    timestamp with a DAG entry, so this is pinned directly, not as an edge).

    Two 'remove' ops share a1's exact timestamp. They must land AFTER a1 (the
    same-timestamp entry), in their original file order, and before the next
    entry. A "simplification" that assumed timestamps are unique — anchoring
    before a1, or reordering the pair — breaks this.
    """
    entries = [
        _user("u1", None, "start", "2026-07-11T07:00:00.000Z"),
        _assistant("a1", "u1", "working", "2026-07-11T07:00:01.000Z"),
        _remove("STEER_ONE", "2026-07-11T07:00:01.000Z"),  # ties a1 exactly
        _remove("STEER_TWO", "2026-07-11T07:00:01.000Z"),  # ties a1 exactly
        _user("u2", "a1", "next", "2026-07-11T07:00:03.000Z"),
    ]
    d = _write_project(tmp_path, entries)

    messages, _tree = load_directory_transcripts(d, None, silent=True)

    i_a1 = _index(messages, lambda m: getattr(m, "uuid", None) == "a1")
    i_u2 = _index(messages, lambda m: getattr(m, "uuid", None) == "u2")
    i_q1 = _index(
        messages,
        lambda m: isinstance(m, QueueOperationTranscriptEntry)
        and m.content == "STEER_ONE",
    )
    i_q2 = _index(
        messages,
        lambda m: isinstance(m, QueueOperationTranscriptEntry)
        and m.content == "STEER_TWO",
    )

    assert i_a1 < i_q1 < i_q2 < i_u2, (
        f"tie-break wrong: a1@{i_a1}, STEER_ONE@{i_q1}, STEER_TWO@{i_q2}, "
        f"u2@{i_u2} — same-timestamp queue-ops must follow a1 in file order"
    )

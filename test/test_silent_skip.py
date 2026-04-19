"""Regression tests for load_transcript handling of non-conversation types.

Covers:
- `file-history-snapshot`, `last-prompt`: known-internal types with no DAG
  fields; silently dropped. Regression for issue #102.
- `progress`: has uuid+sessionId; preserved as PassthroughTranscriptEntry
  for DAG continuity (and not rendered).
- `custom-title`, `agent-name`: unknown types with sessionId but no uuid;
  warn so new Claude Code metadata surfaces instead of being lost.
- Unknown type with uuid+sessionId: falls through to
  PassthroughTranscriptEntry, no warning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_code_log.converter import SILENT_SKIP_TYPES, load_transcript
from claude_code_log.models import PassthroughTranscriptEntry


def _write_jsonl(path: Path, entries: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


class TestSilentSkipTypes:
    """Known-internal types are dropped without warnings."""

    def test_constant_covers_issue_102(self) -> None:
        assert "last-prompt" in SILENT_SKIP_TYPES
        assert "file-history-snapshot" in SILENT_SKIP_TYPES

    def test_constant_covers_session_metadata(self) -> None:
        """Issue #94: session-metadata types (no uuid/timestamp) drop silently."""
        for t in ("permission-mode", "custom-title", "agent-name", "agent-color"):
            assert t in SILENT_SKIP_TYPES

    @pytest.mark.parametrize(
        "entry",
        [
            {
                "type": "permission-mode",
                "permissionMode": "acceptEdits",
                "sessionId": "s1",
            },
            {"type": "custom-title", "customTitle": "CCL (Monk)", "sessionId": "s1"},
            {"type": "agent-name", "agentName": "CCL (Monk)", "sessionId": "s1"},
            {"type": "agent-color", "agentColor": "purple", "sessionId": "s1"},
        ],
    )
    def test_session_metadata_silent(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        entry: dict[str, object],
    ) -> None:
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [entry])

        messages = load_transcript(jsonl, silent=True)
        captured = capsys.readouterr()

        assert messages == []
        assert captured.out == ""

    def test_file_history_snapshot_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "file-history-snapshot",
                    "messageId": "m1",
                    "snapshot": {
                        "messageId": "m1",
                        "trackedFileBackups": {},
                        "timestamp": "2026-01-01T00:00:00.000Z",
                    },
                    "isSnapshotUpdate": False,
                },
            ],
        )

        messages = load_transcript(jsonl, silent=True)
        captured = capsys.readouterr()

        assert messages == []
        assert captured.out == ""

    def test_last_prompt_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "last-prompt",
                    "lastPrompt": "Summarize this file",
                    "sessionId": "s1",
                },
            ],
        )

        messages = load_transcript(jsonl, silent=True)
        captured = capsys.readouterr()

        assert messages == []
        assert captured.out == ""


class TestProgressStaysInDag:
    """`progress` has uuid+parentUuid+sessionId and must survive as a
    PassthroughTranscriptEntry so the DAG chain stays connected — it is
    intentionally not in SILENT_SKIP_TYPES."""

    def test_progress_not_in_silent_skip(self) -> None:
        assert "progress" not in SILENT_SKIP_TYPES

    def test_progress_becomes_passthrough(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "progress",
                    "uuid": "p1",
                    "parentUuid": None,
                    "sessionId": "s1",
                    "timestamp": "2026-01-01T00:00:00.000Z",
                    "data": {"type": "hook_progress", "hookEvent": "SessionStart"},
                },
            ],
        )

        messages = load_transcript(jsonl, silent=True)
        captured = capsys.readouterr()

        assert len(messages) == 1
        assert isinstance(messages[0], PassthroughTranscriptEntry)
        assert messages[0].type == "progress"
        assert messages[0].uuid == "p1"
        assert captured.out == ""


class TestUnrecognizedTypesWarn:
    """Unknown types with no DAG fields surface a warning so we notice
    when Claude Code ships new metadata worth supporting — anything
    outside the explicit silent-skip list or the Passthrough fallback."""

    @pytest.mark.parametrize(
        "entry",
        [
            {"type": "future-metadata-type", "payload": 42, "sessionId": "s1"},
            {"type": "another-hypothetical", "something": "value"},
        ],
    )
    def test_unknown_without_uuid_warns(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        entry: dict[str, object],
    ) -> None:
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [entry])

        messages = load_transcript(jsonl, silent=False)
        captured = capsys.readouterr()

        assert messages == []
        assert "unrecognized message type" in captured.out
        assert repr(entry["type"]) in captured.out

    def test_silent_mode_suppresses_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(jsonl, [{"type": "future-unknown-type", "payload": 1}])

        messages = load_transcript(jsonl, silent=True)
        captured = capsys.readouterr()

        assert messages == []
        assert captured.out == ""

    def test_unknown_with_uuid_becomes_passthrough_silently(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unknown type with DAG fields → PassthroughTranscriptEntry, no warning.
        Preserves DAG continuity when Claude Code ships a new conversational
        type before we add explicit handling."""
        jsonl = tmp_path / "session.jsonl"
        _write_jsonl(
            jsonl,
            [
                {
                    "type": "attachment",
                    "uuid": "att1",
                    "parentUuid": None,
                    "sessionId": "s1",
                    "timestamp": "2026-01-01T00:00:00.000Z",
                },
            ],
        )

        messages = load_transcript(jsonl, silent=True)
        captured = capsys.readouterr()

        assert len(messages) == 1
        assert isinstance(messages[0], PassthroughTranscriptEntry)
        assert captured.out == ""

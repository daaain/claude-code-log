#!/usr/bin/env python3
"""Unit tests for generate_single_session_file() in converter.py."""

import json
from pathlib import Path
from typing import Generator

import pytest

from claude_code_log.converter import generate_single_session_file

# A UUID-style session ID whose 8-char prefix is "abc12345"
SESSION_ID_A = "abc12345-1111-2222-3333-444444444444"
SESSION_ID_B = "abc1xxxx-5555-6666-7777-888888888888"


def _make_jsonl_entries(session_id: str, user_message: str = "Hello") -> list[dict]:
    """Minimal valid JSONL entries for a session."""
    return [
        {
            "type": "user",
            "uuid": f"{session_id}-user",
            "timestamp": "2023-01-01T10:00:00Z",
            "sessionId": session_id,
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "user",
            "cwd": "/test",
            "message": {"role": "user", "content": user_message},
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-asst",
            "timestamp": "2023-01-01T10:01:00Z",
            "sessionId": session_id,
            "version": "1.0.0",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "assistant",
            "cwd": "/test",
            "requestId": "req-1",
            "message": {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-3",
                "content": [{"type": "text", "text": "Hi!"}],
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        },
    ]


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    """Isolated project directory with one session (SESSION_ID_A)."""
    proj = tmp_path / "my-project"
    proj.mkdir()
    _write_jsonl(proj / "session-a.jsonl", _make_jsonl_entries(SESSION_ID_A))
    monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))
    yield proj


class TestInputValidation:
    def test_raises_file_not_found_for_missing_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))
        missing = tmp_path / "does-not-exist"
        with pytest.raises(FileNotFoundError):
            generate_single_session_file("html", missing, SESSION_ID_A)

    def test_raises_file_not_found_for_file_not_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))
        f = tmp_path / "not-a-dir.jsonl"
        f.write_text("{}")
        with pytest.raises(FileNotFoundError):
            generate_single_session_file("html", f, SESSION_ID_A)

    def test_raises_value_error_for_unknown_session(self, project_dir: Path):
        with pytest.raises(ValueError, match="not found"):
            generate_single_session_file(
                "html", project_dir, "zzzzzzzz-0000-0000-0000-000000000000"
            )


class TestSessionIdResolution:
    def test_exact_session_id_match(self, project_dir: Path):
        result = generate_single_session_file("html", project_dir, SESSION_ID_A)
        assert result.exists()

    def test_short_prefix_match(self, project_dir: Path):
        # "abc12345" is the first 8 chars of SESSION_ID_A
        result = generate_single_session_file("html", project_dir, "abc12345")
        assert result.exists()

    def test_ambiguous_prefix_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        proj = tmp_path / "ambiguous-project"
        proj.mkdir()
        # Both IDs start with "abc1" — providing that prefix is ambiguous
        entries = _make_jsonl_entries(SESSION_ID_A) + _make_jsonl_entries(SESSION_ID_B)
        _write_jsonl(proj / "sessions.jsonl", entries)
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))

        with pytest.raises(ValueError, match="[Aa]mbiguous"):
            generate_single_session_file("html", proj, "abc1")


class TestOutputPath:
    def test_default_output_path(self, project_dir: Path):
        result = generate_single_session_file("html", project_dir, SESSION_ID_A)
        expected = project_dir / f"session-{SESSION_ID_A}.html"
        assert result == expected
        assert result.exists()

    def test_custom_output_path(self, project_dir: Path, tmp_path: Path):
        custom = tmp_path / "out" / "my-session.html"
        custom.parent.mkdir()
        result = generate_single_session_file(
            "html", project_dir, SESSION_ID_A, output=custom
        )
        assert result == custom
        assert custom.exists()

    def test_markdown_format_uses_md_extension(self, project_dir: Path):
        result = generate_single_session_file("md", project_dir, SESSION_ID_A)
        assert result.suffix == ".md"
        assert result.exists()


class TestNoCacheMode:
    def test_generates_file_without_cache(self, project_dir: Path):
        result = generate_single_session_file(
            "html", project_dir, SESSION_ID_A, use_cache=False
        )
        assert result.exists()
        assert result.stat().st_size > 0


class TestSessionTitle:
    def test_title_uses_summary(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        proj = tmp_path / "proj-summary"
        proj.mkdir()
        entries = _make_jsonl_entries(SESSION_ID_A)
        entries.append(
            {
                "type": "summary",
                "summary": "My special summary",
                "leafUuid": f"{SESSION_ID_A}-asst",
            }
        )
        _write_jsonl(proj / "session-a.jsonl", entries)
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))

        result = generate_single_session_file("html", proj, SESSION_ID_A)
        content = result.read_text(encoding="utf-8")
        assert "My special summary" in content

    def test_title_uses_first_user_message_truncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        long_msg = "A" * 60  # > 50 chars, no trailing summary entry
        proj = tmp_path / "proj-preview"
        proj.mkdir()
        _write_jsonl(
            proj / "session-a.jsonl",
            _make_jsonl_entries(SESSION_ID_A, user_message=long_msg),
        )
        monkeypatch.setenv("CLAUDE_CODE_LOG_CACHE_PATH", str(tmp_path / "test.db"))

        result = generate_single_session_file("html", proj, SESSION_ID_A)
        content = result.read_text(encoding="utf-8")
        # Truncated preview ends with "..."
        assert "..." in content
        # The first 50 chars of the long message should appear
        assert long_msg[:50] in content

    def test_title_falls_back_to_session_short_id(self, project_dir: Path):
        # use_cache=False means no session metadata → falls back to short ID
        result = generate_single_session_file(
            "html", project_dir, SESSION_ID_A, use_cache=False
        )
        content = result.read_text(encoding="utf-8")
        # The first 8 chars of the session ID must appear as the fallback label
        assert SESSION_ID_A[:8] in content

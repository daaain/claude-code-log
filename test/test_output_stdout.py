"""Tests for stdout streaming (PR 2 of the #220-223 set, issue #223).

`-o -` (and `-o /dev/stdout`) streams the rendered document to stdout:
always regenerate, no cache, no browser, and status text on stderr so the
stream carries only the document. Previously `-o /dev/stdout` hung and
progress was written to stdout.
"""

import json
import uuid
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from claude_code_log.cli import main


def _user_entry(text: str, session_id: str = "sess-stream") -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": "2025-01-01T10:00:00Z",
        "sessionId": session_id,
        "uuid": f"u-{uuid.uuid4().hex[:8]}",
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "version": "1.0.0",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _write_jsonl(path: Path, text: str, session_id: str = "sess-stream") -> Path:
    path.write_text(json.dumps(_user_entry(text, session_id)) + "\n", encoding="utf-8")
    return path


class TestStreamToStdout:
    def setup_method(self):
        self.runner = CliRunner()

    def test_markdown_document_on_stdout_status_on_stderr(self, tmp_path: Path):
        src = _write_jsonl(tmp_path / "s.jsonl", "STREAMED_MD_BODY")
        r = self.runner.invoke(main, [str(src), "-f", "markdown", "-o", "-"])
        assert r.exit_code == 0, r.output
        # Document on stdout...
        assert "STREAMED_MD_BODY" in r.stdout
        assert "<!DOCTYPE html>" not in r.stdout
        # ...and stdout is clean of progress/summary noise.
        assert "Processing" not in r.stdout
        assert "Successfully" not in r.stdout
        # Confirmation routed to stderr.
        assert "to stdout" in r.stderr

    def test_html_document_on_stdout(self, tmp_path: Path):
        src = _write_jsonl(tmp_path / "s.jsonl", "STREAMED_HTML_BODY")
        r = self.runner.invoke(main, [str(src), "-f", "html", "-o", "-"])
        assert r.exit_code == 0, r.output
        assert "<!DOCTYPE html>" in r.stdout
        assert "Successfully" not in r.stdout

    def test_json_document_on_stdout(self, tmp_path: Path):
        src = _write_jsonl(tmp_path / "s.jsonl", "STREAMED_JSON_BODY")
        r = self.runner.invoke(main, [str(src), "-f", "json", "-o", "-"])
        assert r.exit_code == 0, r.output
        json.loads(r.stdout)  # stdout is pure JSON, not polluted

    @pytest.mark.skipif(
        sys.platform == "win32", reason="/dev/stdout is POSIX-only; use '-' on Windows"
    )
    def test_dev_stdout_does_not_hang(self, tmp_path: Path):
        """The original bug: `-o /dev/stdout` deadlocked in the version sniff."""
        src = _write_jsonl(tmp_path / "s.jsonl", "DEVSTDOUT_BODY")
        r = self.runner.invoke(main, [str(src), "-f", "markdown", "-o", "/dev/stdout"])
        assert r.exit_code == 0, r.output
        assert "DEVSTDOUT_BODY" in r.stdout

    def test_format_inferred_is_irrelevant_dash_has_no_suffix(self, tmp_path: Path):
        """`-` has no suffix, so format stays the default/explicit one."""
        src = _write_jsonl(tmp_path / "s.jsonl", "DASH_DEFAULT")
        r = self.runner.invoke(main, [str(src), "-o", "-"])  # default html
        assert r.exit_code == 0, r.output
        assert "<!DOCTYPE html>" in r.stdout

    def test_all_projects_with_stream_errors(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_jsonl(proj / "s.jsonl", "X")
        r = self.runner.invoke(main, [str(tmp_path), "-o", "-", "--all-projects"])
        assert r.exit_code != 0
        assert "not supported with --all-projects" in r.output

    def test_session_id_stream_to_stdout(self, tmp_path: Path):
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_jsonl(proj / "a.jsonl", "SESSION_STREAM_BODY", session_id="sess-aaa")
        r = self.runner.invoke(
            main, [str(proj), "--session-id", "sess-aaa", "-f", "markdown", "-o", "-"]
        )
        assert r.exit_code == 0, r.output
        assert "SESSION_STREAM_BODY" in r.stdout
        # stdout is ONLY the document — no progress/summary noise leaks
        # (generate_single_session_file has no silent switch; the stream
        # path captures its stdout and routes it to stderr).
        assert "Successfully" not in r.stdout
        assert "Processing" not in r.stdout
        assert "Loading" not in r.stdout
        assert "to stdout" in r.stderr

    def test_unicode_document_round_trips(self, tmp_path: Path):
        """The document is streamed as raw UTF-8 bytes, so non-ASCII content
        (transcripts are emoji-heavy) survives regardless of stdout's locale
        encoding — not re-encoded via sys.stdout.write (CodeRabbit)."""
        src = _write_jsonl(tmp_path / "s.jsonl", "emoji 🎉 accent café 日本語")
        r = self.runner.invoke(main, [str(src), "-f", "markdown", "-o", "-"])
        assert r.exit_code == 0, r.output
        assert "🎉" in r.stdout
        assert "café" in r.stdout
        assert "日本語" in r.stdout

    def test_global_session_id_stream_not_rejected_by_all_projects_guard(
        self, tmp_path: Path
    ):
        """`--session-id <id> -o -` with no input path (global cache lookup)
        must NOT be rejected by the --all-projects+stream guard just because
        input_path is None (CodeRabbit). It should reach session resolution
        and fail there (session not in cache), not at the guard."""
        r = self.runner.invoke(
            main,
            [
                "--session-id",
                "deadbeef",
                "-o",
                "-",
                "--projects-dir",
                str(tmp_path / "empty"),
            ],
        )
        assert r.exit_code != 0
        # Reached session resolution, not the all-projects guard.
        assert "not supported with --all-projects" not in r.output
        assert "not found in cache" in r.output

    def test_combined_no_with_stream_errors(self, tmp_path: Path):
        """`--combined no` (per-session only) is incompatible with streaming a
        single document to stdout — fail fast instead of silently forcing the
        combined doc (CodeRabbit)."""
        src = _write_jsonl(tmp_path / "s.jsonl", "X")
        r = self.runner.invoke(main, [str(src), "-o", "-", "--combined", "no"])
        assert r.exit_code != 0
        assert "--combined no is incompatible" in r.output

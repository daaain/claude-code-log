"""TUI export-path regression tests for issue #139.

Counterpart to ``test_surrogate_encoding.py`` — that file exercises the
CLI path through ``convert_jsonl_to_html``; this one exercises
``SessionBrowser._ensure_session_file``, which the TUI calls when the
user opens a session export. Pre-fix, a lone surrogate flowing from the
in-memory renderer into ``Path.write_text(encoding="utf-8")`` would
crash, but the TUI's wrapping ``try/except Exception: return None``
silently swallows the traceback — the user sees only "Failed to
generate".
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from claude_code_log.tui import SessionBrowser


# Lone low surrogate U+DCB2 — what surrogateescape uses for byte 0xB2.
LONE_SURROGATE = "\udcb2"
REPLACEMENT_CHAR = "�"


def _seed_project_with_session(project_dir: Path, session_id: str) -> None:
    """Write a minimal JSONL session file the TUI can pick up. Content
    doesn't matter for this test — `generate_session` is mocked — but
    the file must exist so cache discovery sees something."""
    entry = {
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": "/tmp",
        "sessionId": session_id,
        "version": "2.1.0",
        "type": "user",
        "uuid": "55555555-5555-5555-5555-555555555555",
        "timestamp": "2026-05-08T10:00:00.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "doesn't matter — mocked"}],
        },
    }
    (project_dir / f"{session_id}.jsonl").write_text(
        json.dumps(entry) + "\n", encoding="utf-8"
    )


@pytest.mark.tui
class TestTUIExportSurrogateHandling:
    """The TUI export path must scrub lone surrogates so the on-disk file
    is valid UTF-8. The wrapping ``try/except`` at the call site means a
    crash here would silently surface as 'Failed to generate' rather than
    a loud exception — bug-class worth a regression test."""

    def test_session_export_scrubs_lone_surrogate(self, tmp_path: Path):
        """The renderer is mocked to return surrogate-bearing HTML; the
        on-disk file must be strict-UTF-8 decodable, the surrogate must
        be gone, and U+FFFD must be in its place (the canonical Unicode
        replacement char that ``scrub_surrogates`` produces)."""
        session_id = "11111111-1111-1111-1111-111111111111"
        _seed_project_with_session(tmp_path, session_id)

        # Surrogate-bearing content the renderer "would" emit. Pre-fix,
        # `Path.write_text(encoding="utf-8")` of this would crash.
        surrogate_html = (
            f"<html><body><p>marker {LONE_SURROGATE} (issue #139 TUI)</p></body></html>"
        )

        browser = SessionBrowser(tmp_path)
        # Populate sessions dict so build_session_title gets useful data.
        browser.sessions = {}

        # Mock the renderer's generate_session to inject surrogate-bearing
        # content directly. Also short-circuit `is_outdated` so we always
        # write, and `load_directory_transcripts` to skip JSONL parsing.
        mock_renderer = MagicMock()
        mock_renderer.is_outdated.return_value = True
        mock_renderer.generate_session.return_value = surrogate_html

        with (
            patch("claude_code_log.tui.get_renderer", return_value=mock_renderer),
            patch(
                "claude_code_log.tui.load_directory_transcripts",
                return_value=([{"placeholder": True}], None),
            ),
            patch.object(browser.cache_manager, "get_cached_project_data") as mock_pd,
            patch(
                "claude_code_log.tui.build_session_title", return_value="Test Session"
            ),
        ):
            mock_pd.return_value = MagicMock(working_directories=[str(tmp_path)])
            session_file = browser._ensure_session_file(session_id, "html", force=True)

        # Method must return a Path (not None — None would mean the
        # try/except silently swallowed the original surrogate crash).
        assert session_file is not None, (
            "TUI export silently failed — the broader try/except likely "
            "swallowed an UnicodeEncodeError that scrubbing should have "
            "prevented."
        )
        assert session_file.exists()

        # Strict UTF-8 decode of the bytes — would raise pre-fix if the
        # crash had happened or if the surrogate had been written through
        # somehow.
        on_disk = session_file.read_bytes().decode("utf-8")
        assert "issue #139 TUI" in on_disk
        assert LONE_SURROGATE not in on_disk
        # The surrogate should land as U+FFFD (canonical Unicode
        # replacement character), not ASCII '?' — we use the
        # `scrub_surrogates` helper which uses surrogateescape →
        # decode-replace specifically to emit U+FFFD.
        assert REPLACEMENT_CHAR in on_disk

"""Tests for the Resume Session floating button and its command builder."""

from typing import Optional

from claude_code_log.html.renderer import generate_html
from claude_code_log.models import (
    TextContent,
    UserMessageModel,
    UserTranscriptEntry,
)
from claude_code_log.utils import resume_command_for_session

SESSION_A = "c2688f20-2ca1-410d-a82b-1a7f11761315"
SESSION_B = "37f83ec9-f2ea-42a9-925e-0d5c105cb6e8"


def _user_entry(session_id: str, cwd: Optional[str], uuid: str) -> UserTranscriptEntry:
    """Build a minimal user entry for rendering tests."""
    return UserTranscriptEntry(
        type="user",
        timestamp="2025-06-14T10:00:00.000Z",
        parentUuid=None,
        isSidechain=False,
        userType="human",
        cwd=cwd or "",
        sessionId=session_id,
        version="1.0.0",
        uuid=uuid,
        message=UserMessageModel(
            role="user",
            content=[TextContent(type="text", text=f"Hello from {uuid}")],
        ),
    )


class TestResumeCommandForSession:
    """Command shape follows the OS the transcript was recorded on."""

    def test_windows_cwd_uses_double_quotes(self):
        """A drive-lettered cwd gets Windows double-quote quoting."""
        assert (
            resume_command_for_session(SESSION_A, "C:\\Users\\maxno")
            == f'pushd "C:\\Users\\maxno" && claude -r {SESSION_A}'
        )

    def test_windows_cwd_with_spaces(self):
        """Spaces in a Windows cwd stay inside the double quotes."""
        assert (
            resume_command_for_session(SESSION_A, "C:\\My Projects\\app")
            == f'pushd "C:\\My Projects\\app" && claude -r {SESSION_A}'
        )

    def test_windows_other_drive_uses_pushd(self):
        """A D: cwd must switch drive too — cmd's `cd` alone wouldn't,
        so a terminal started on C: would resume in the wrong project."""
        command = resume_command_for_session(SESSION_A, "D:\\work\\app")
        assert command == f'pushd "D:\\work\\app" && claude -r {SESSION_A}'

    def test_posix_cwd(self):
        """A plain POSIX cwd needs no quoting at all."""
        assert (
            resume_command_for_session(SESSION_A, "/Users/dain/workspace")
            == f"cd /Users/dain/workspace && claude -r {SESSION_A}"
        )

    def test_posix_cwd_with_spaces_is_quoted(self):
        """Spaces in a POSIX cwd get shlex single-quoting."""
        assert (
            resume_command_for_session(SESSION_A, "/home/u/my project")
            == f"cd '/home/u/my project' && claude -r {SESSION_A}"
        )

    def test_no_cwd_falls_back_to_bare_resume(self):
        """Without a recorded cwd the command is a bare claude -r."""
        assert resume_command_for_session(SESSION_A, None) == f"claude -r {SESSION_A}"

    def test_posix_cwd_with_single_quote_is_escaped(self):
        """shlex neutralizes an embedded single quote in a POSIX cwd."""
        command = resume_command_for_session(SESSION_A, "/home/u/it's")
        assert command == f"""cd '/home/u/it'"'"'s' && claude -r {SESSION_A}"""

    def test_session_id_with_shell_metacharacters_is_rejected(self):
        """A session id carrying shell syntax yields no command at all."""
        assert resume_command_for_session("evil; rm -rf ~", "/tmp") is None
        assert resume_command_for_session("$(whoami)", "/tmp") is None
        assert resume_command_for_session("", "/tmp") is None

    def test_windows_cwd_with_embedded_quote_is_rejected(self):
        """A double quote in a Windows cwd would break out of the quoting."""
        assert resume_command_for_session(SESSION_A, 'C:\\evil" & calc & "') is None

    def test_windows_cwd_with_expansion_characters_is_rejected(self):
        """cmd %var%/!var! and PowerShell $var/`x expand inside double quotes."""
        assert resume_command_for_session(SESSION_A, "C:\\x%TEMP%") is None
        assert resume_command_for_session(SESSION_A, "C:\\x$(calc)") is None
        assert resume_command_for_session(SESSION_A, "C:\\x`n") is None
        assert resume_command_for_session(SESSION_A, "C:\\x!var!") is None

    def test_cwd_with_newline_is_rejected(self):
        """Pasting multi-line text can execute each line — reject outright."""
        assert resume_command_for_session(SESSION_A, "/tmp/a\nrm -rf ~") is None
        assert resume_command_for_session(SESSION_A, "C:\\a\r\ncalc") is None


class TestResumeButtonInHtml:
    """The button renders only on single-session pages."""

    def test_single_session_page_has_button_with_command(self):
        """A single-session page renders the button with its command."""
        html = generate_html(
            [_user_entry(SESSION_A, "/Users/dain/workspace", "uuid-1")],
            "Test Resume",
        )
        assert 'id="resumeSession"' in html
        assert "▶️</button>" in html
        assert (
            f'data-command="cd /Users/dain/workspace &amp;&amp; claude -r {SESSION_A}"'
            in html
        )

    def test_windows_session_command_is_escaped_into_attribute(self):
        """The Windows command lands HTML-escaped in the data attribute."""
        html = generate_html(
            [_user_entry(SESSION_A, "C:\\Users\\maxno", "uuid-1")],
            "Test Resume Windows",
        )
        assert (
            f"pushd &#34;C:\\Users\\maxno&#34; &amp;&amp; claude -r {SESSION_A}" in html
        )

    def test_multi_session_page_has_no_button(self):
        """Combined pages spanning several sessions render no button."""
        html = generate_html(
            [
                _user_entry(SESSION_A, "/Users/dain/workspace", "uuid-1"),
                _user_entry(SESSION_B, "/Users/dain/workspace", "uuid-2"),
            ],
            "Test Combined",
        )
        assert 'id="resumeSession"' not in html

    def test_unsafe_session_id_renders_no_button(self):
        """A transcript with a shell-unsafe session id gets no button."""
        html = generate_html(
            [_user_entry("evil; rm -rf ~", "/tmp", "uuid-1")],
            "Test Unsafe",
        )
        assert 'id="resumeSession"' not in html

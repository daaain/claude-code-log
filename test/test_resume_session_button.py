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
        assert (
            resume_command_for_session(SESSION_A, "C:\\Users\\maxno")
            == f'cd "C:\\Users\\maxno" && claude -r {SESSION_A}'
        )

    def test_windows_cwd_with_spaces(self):
        assert (
            resume_command_for_session(SESSION_A, "C:\\My Projects\\app")
            == f'cd "C:\\My Projects\\app" && claude -r {SESSION_A}'
        )

    def test_posix_cwd(self):
        assert (
            resume_command_for_session(SESSION_A, "/Users/dain/workspace")
            == f"cd /Users/dain/workspace && claude -r {SESSION_A}"
        )

    def test_posix_cwd_with_spaces_is_quoted(self):
        assert (
            resume_command_for_session(SESSION_A, "/home/u/my project")
            == f"cd '/home/u/my project' && claude -r {SESSION_A}"
        )

    def test_no_cwd_falls_back_to_bare_resume(self):
        assert resume_command_for_session(SESSION_A, None) == f"claude -r {SESSION_A}"


class TestResumeButtonInHtml:
    """The button renders only on single-session pages."""

    def test_single_session_page_has_button_with_command(self):
        html = generate_html(
            [_user_entry(SESSION_A, "/Users/dain/workspace", "uuid-1")],
            "Test Resume",
        )
        assert 'id="resumeSession"' in html
        assert "▶ Resume Session" in html
        assert (
            f'data-command="cd /Users/dain/workspace &amp;&amp; claude -r {SESSION_A}"'
            in html
        )

    def test_windows_session_command_is_escaped_into_attribute(self):
        html = generate_html(
            [_user_entry(SESSION_A, "C:\\Users\\maxno", "uuid-1")],
            "Test Resume Windows",
        )
        assert f"cd &#34;C:\\Users\\maxno&#34; &amp;&amp; claude -r {SESSION_A}" in html

    def test_multi_session_page_has_no_button(self):
        html = generate_html(
            [
                _user_entry(SESSION_A, "/Users/dain/workspace", "uuid-1"),
                _user_entry(SESSION_B, "/Users/dain/workspace", "uuid-2"),
            ],
            "Test Combined",
        )
        assert 'id="resumeSession"' not in html

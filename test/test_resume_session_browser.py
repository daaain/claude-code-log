"""Playwright tests for the Resume Session floating button.

Covers the live-JS behavior the server-side unit tests can't reach:
clicking the button writes the resume command to the clipboard and
shows the paste-into-terminal toast.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import List, Optional

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.models import TranscriptEntry


def _user_entry(
    uuid: str,
    text: str,
    session_id: str = "test_session",
    cwd: str = "/tmp/project",
    ts: str = "2026-01-01T10:00:00.000Z",
) -> dict:
    """Build a raw user-entry dict for a JSONL fixture."""
    return {
        "type": "user",
        "timestamp": ts,
        "parentUuid": None,
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd,
        "sessionId": session_id,
        "version": "1.0.0",
        "uuid": uuid,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


class TestResumeSessionBrowser:
    """Live-browser tests for the Resume Session button."""

    def setup_method(self) -> None:
        """Track temp files created by the test for cleanup."""
        self.temp_files: List[Path] = []

    def teardown_method(self) -> None:
        """Remove the temp files created during the test."""
        for f in self.temp_files:
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    def _render(self, entries: List[dict], title: str = "Resume Test") -> Path:
        """Write entries to a JSONL, render to HTML, return the HTML path."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
            jsonl_path = Path(f.name)
        self.temp_files.append(jsonl_path)

        messages: List[TranscriptEntry] = load_transcript(jsonl_path)
        html_content = generate_html(messages, title)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html_content)
            html_path = Path(f.name)
        self.temp_files.append(html_path)
        return html_path

    def _goto_with_clipboard_stub(self, page: Page, html: Path) -> None:
        """Navigate to the rendered HTML with ``navigator.clipboard``
        stubbed to record writes into ``window.__copied``. file:// pages
        can't reliably get real clipboard permission in the test
        browser, and a stub also lets the test assert the exact text."""
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'clipboard', {
                value: {
                    writeText: function (text) {
                        window.__copied = text;
                        return Promise.resolve();
                    }
                },
                configurable: true
            });
            """
        )
        page.goto(html.as_uri())

    @pytest.mark.browser
    def test_click_copies_command_and_shows_toast(self, page: Page) -> None:
        """Clicking the button copies the exact command and shows the toast."""
        html = self._render(
            [_user_entry("u1", "hello", session_id="ab12cd34", cwd="/tmp/project")]
        )
        self._goto_with_clipboard_stub(page, html)

        button = page.locator("#resumeSession")
        expect(button).to_be_visible()
        expect(button).to_have_text("▶️")

        button.click()
        copied: Optional[str] = page.evaluate("() => window.__copied")
        assert copied == "cd /tmp/project && claude -r ab12cd34"

        toast = page.locator("#resumeToast")
        expect(toast).to_be_visible()
        expect(toast).to_contain_text("Paste into your terminal")

    @pytest.mark.browser
    def test_the_toast_sits_beside_its_button_and_clears_the_stack(
        self, page: Page
    ) -> None:
        """It used to stack *above* the floating buttons, which meant every
        button added to the stack pushed the toast further up, over
        controls it has nothing to do with. Beside its own button, the
        column is empty and stays empty."""
        html = self._render(
            [_user_entry("u1", "hello", session_id="ab12cd34", cwd="/tmp/project")]
        )
        self._goto_with_clipboard_stub(page, html)
        page.locator("#resumeSession").click()
        page.wait_for_selector("#resumeToast.visible")

        geometry = page.evaluate(
            "() => {"
            " const toast = document.getElementById('resumeToast')"
            "   .getBoundingClientRect();"
            " const btn = document.getElementById('resumeSession')"
            "   .getBoundingClientRect();"
            " const hits = [...document.querySelectorAll('.floating-btn')]"
            "   .filter(el => { const r = el.getBoundingClientRect();"
            "     return !(r.right < toast.left || r.left > toast.right"
            "              || r.bottom < toast.top || r.top > toast.bottom); })"
            "   .map(el => el.id);"
            " return { leftOf: toast.right <= btn.left,"
            "          onScreen: toast.left >= 0,"
            "          centred: Math.abs((toast.top + toast.bottom) / 2"
            "                            - (btn.top + btn.bottom) / 2) < 2,"
            "          hits }; }"
        )
        assert geometry["leftOf"], "the toast is not to the left of its button"
        assert geometry["centred"], "the toast is not centred on its button"
        assert geometry["onScreen"], "the toast runs off the left edge"
        assert geometry["hits"] == [], (
            f"the toast covers other floating buttons: {geometry['hits']}"
        )

    @pytest.mark.browser
    def test_no_button_on_multi_session_page(self, page: Page) -> None:
        """Combined pages spanning several sessions render no button."""
        html = self._render(
            [
                _user_entry("u1", "first", session_id="session_a"),
                _user_entry(
                    "u2",
                    "second",
                    session_id="session_b",
                    ts="2026-01-01T10:01:00.000Z",
                ),
            ]
        )
        page.goto(html.as_uri())
        expect(page.locator("#resumeSession")).to_have_count(0)

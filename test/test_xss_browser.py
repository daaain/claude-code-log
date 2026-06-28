"""Empirical XSS regression: opening a transcript must never execute
content-supplied script.

Unit tests assert the rendered HTML escapes raw tags, but the property we
actually care about is behavioural: when a human opens the generated file in
a real browser, no ``alert()`` dialog pops and no content-supplied node ends
up live in the DOM. This exercises that directly with Playwright.

Why a browser test catches what string assertions can miss: a parser-inserted
``<script>`` runs on load, and an ``<img onerror>`` fires on load — both
surface here as a dialog event even if a future refactor escapes one path but
regresses another. The dialog handler is the ground truth.

The payload is placed in every transcript-content channel that flows through
the shared (assistant/tool/web-authored) Markdown renderer — assistant prose,
a tool result, and a Write tool's file content — because the assistant
routinely echoes arbitrary user/file/web input verbatim (the real-world
trigger: "write an E2E test that types <script>alert(1)</script> into the
field"). See ``html/utils.py::_get_markdown_renderer`` for the escape policy.

It is ALSO placed in a TITLE field — a generic tool_use's ``name`` (#245):
the header title path (``{{ message_title | safe }}``) has no central
escaping, so a tool with no specialized title method renders its raw name
live in the header span. That channel is blind to the body-render escape and
needs the per-field ``escape_html`` on the HTML title methods.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Page

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html

# Auto-firing vectors: <script> runs when the parser inserts it; <img onerror>
# fires on the failed load. Both pop a dialog the moment the page loads.
PAYLOAD = (
    "<b>bold</b>"
    '<img src="x" onerror="alert(\'img-xss\')">'
    "<script>alert('script-xss')</script>"
)

_BASE = {
    "cwd": "/app",
    "isSidechain": False,
    "sessionId": "11110000-0000-4000-8000-000000000001",
    "userType": "external",
    "version": "1.0.0",
}
_USAGE = {"input_tokens": 1, "output_tokens": 1}


def _write_transcript(path: Path) -> None:
    rows = [
        {
            **_BASE,
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "timestamp": "2026-06-27T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": f"Write an E2E test that types this: {PAYLOAD}",
            },
        },
        {
            **_BASE,
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "requestId": "r1",
            "timestamp": "2026-06-27T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "claude",
                "id": "m1",
                "type": "message",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": _USAGE,
                "content": [
                    {"type": "text", "text": f"Sure! I'll enter {PAYLOAD} there."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {
                            "file_path": "/app/t.spec.ts",
                            "content": f"await page.fill('#in', '{PAYLOAD}');",
                        },
                    },
                ],
            },
        },
        {
            **_BASE,
            "type": "user",
            "uuid": "u2",
            "parentUuid": "a1",
            "timestamp": "2026-06-27T10:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": f"Wrote file containing {PAYLOAD}",
                    }
                ],
            },
        },
        {
            # TITLE-path vector (#245): a tool with no specialized title method
            # falls back to its raw name in the header span. The name IS the
            # payload — auto-firing if the title isn't escaped.
            **_BASE,
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "u2",
            "requestId": "r2",
            "timestamp": "2026-06-27T10:00:03.000Z",
            "message": {
                "role": "assistant",
                "model": "claude",
                "id": "m2",
                "type": "message",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": _USAGE,
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": PAYLOAD,
                        "input": {"q": "noop"},
                    }
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class TestTranscriptXss:
    @pytest.mark.browser
    def test_opening_transcript_fires_no_dialog(
        self, page: Page, tmp_path: Path
    ) -> None:
        jsonl = tmp_path / "xss.jsonl"
        _write_transcript(jsonl)
        entries = load_transcript(jsonl, silent=True)
        html_path = tmp_path / "xss.html"
        html_path.write_text(generate_html(entries, "XSS"), encoding="utf-8")

        dialogs: list[str] = []

        def _on_dialog(dialog: object) -> None:  # pragma: no cover - event cb
            dialogs.append(getattr(dialog, "message", ""))
            dialog.dismiss()  # type: ignore[attr-defined]

        page.on("dialog", _on_dialog)
        page.goto(html_path.as_uri())
        # Give any onerror/script a tick to fire.
        page.wait_for_timeout(200)

        assert dialogs == [], f"XSS executed — dialogs fired: {dialogs}"

        # And the payload is still visible to the reader, as escaped text.
        body_text = page.inner_text("body")
        assert "script-xss" in body_text
        assert "alert('img-xss')" in body_text

    @pytest.mark.browser
    def test_no_content_supplied_nodes_in_dom(self, page: Page, tmp_path: Path) -> None:
        """The payload tags must not materialise as live DOM nodes."""
        jsonl = tmp_path / "xss.jsonl"
        _write_transcript(jsonl)
        entries = load_transcript(jsonl, silent=True)
        html_path = tmp_path / "xss.html"
        html_path.write_text(generate_html(entries, "XSS"), encoding="utf-8")

        page.goto(html_path.as_uri())

        # No <img> whose src is the payload's bogus "x" was injected, and no
        # content-supplied <b> leaked as a live element.
        injected = page.evaluate(
            "() => ({"
            "  imgs: document.querySelectorAll('img[src=\"x\"]').length,"
            "  bolds: Array.from(document.querySelectorAll('b'))"
            "    .filter(b => b.textContent === 'bold').length,"
            "})"
        )
        assert injected == {"imgs": 0, "bolds": 0}, injected

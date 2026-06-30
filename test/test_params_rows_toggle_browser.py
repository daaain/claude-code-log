"""Playwright tests for the params-table fold controls.

An open structured-value fold shows "⏵ expand all rows" in a controls
strip after the summary (never inside it — interactive elements within
<summary> are an accessibility violation); pressing it opens every
row-level fold of that table and turns into "⏷ collapse all rows";
closing the outer fold restores the initial state. Fold-valued rows
carry their ⏵/⏷ toggle in the KEY column, derived from the actual open
state.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import List

import pytest
from playwright.sync_api import Page, expect

from claude_code_log.converter import load_transcript
from claude_code_log.html.renderer import generate_html
from claude_code_log.models import TranscriptEntry

ROW_DETAILS_JS = (
    "el => Array.from("
    "el.querySelectorAll(':scope > table > tbody > tr > td > details')"
    ").map(d => d.open)"
)


def _entries_with_structured_list() -> List[dict]:
    """A generic tool whose input holds a list of dicts, each large
    enough (>200 chars JSON) that every row renders as its own fold."""
    items = [
        {
            "name": f"item_{i}",
            "role": f"row {i}: " + "padding words " * 20,
        }
        for i in range(4)
    ]
    return [
        {
            "type": "assistant",
            "timestamp": "2026-01-01T10:00:00.000Z",
            "parentUuid": None,
            "isSidechain": False,
            "userType": "external",
            "cwd": "/tmp",
            "sessionId": "s",
            "version": "1.0.0",
            "uuid": "a1",
            "message": {
                "role": "assistant",
                "id": "m1",
                "type": "message",
                "model": "claude",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "SomeTool",
                        "input": {"items": items},
                    }
                ],
            },
        },
    ]


class TestParamsRowsToggleBrowser:
    def setup_method(self) -> None:
        self.temp_files: List[Path] = []

    def teardown_method(self) -> None:
        for f in self.temp_files:
            try:
                f.unlink()
            except FileNotFoundError:
                pass

    def _render(self, entries: List[dict]) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
            jsonl_path = Path(f.name)
        self.temp_files.append(jsonl_path)

        messages: List[TranscriptEntry] = load_transcript(jsonl_path)
        html_content = generate_html(messages, "Rows Toggle Test")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False, encoding="utf-8"
        ) as f:
            f.write(html_content)
            html_path = Path(f.name)
        self.temp_files.append(html_path)
        return html_path

    @pytest.mark.browser
    def test_rows_toggle_cycle(self, page: Page) -> None:
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")

        outer = page.locator("details.tool-param-collapsible-rows").first
        button = outer.locator(
            ".tool-param-fold-controls .tool-param-rows-toggle"
        ).first

        # Collapsed fold: the button is hidden.
        assert not button.is_visible()

        outer.locator("summary").first.click()
        assert button.is_visible()
        assert "expand all rows" in (button.text_content() or "")
        row_states = outer.evaluate(ROW_DETAILS_JS)
        assert row_states and not any(row_states), "rows must start collapsed"

        # Expand all rows.
        button.click()
        assert outer.evaluate("el => el.open"), (
            "button click must not toggle the enclosing details"
        )
        row_states = outer.evaluate(ROW_DETAILS_JS)
        assert all(row_states), "all rows must be open after expand all rows"
        assert "collapse all rows" in (button.text_content() or "")

        # Collapse all rows back.
        button.click()
        row_states = outer.evaluate(ROW_DETAILS_JS)
        assert not any(row_states), "all rows must close after collapse all rows"
        assert "expand all rows" in (button.text_content() or "")

    @pytest.mark.browser
    def test_closing_fold_restores_initial_state(self, page: Page) -> None:
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")

        outer = page.locator("details.tool-param-collapsible-rows").first
        button = outer.locator(
            ".tool-param-fold-controls .tool-param-rows-toggle"
        ).first

        outer.locator("summary").first.click()
        button.click()
        assert all(outer.evaluate(ROW_DETAILS_JS))

        # Close the outer fold via its key-column toggle (the open summary
        # is hidden for keyed rows — the ⏷ in the key cell is the collapse
        # control), then reopen the same way.
        key_toggle = page.locator(
            "tr.tool-param-row-fold > td.tool-param-key > .tool-param-key-toggle"
        ).first
        key_toggle.click()
        assert not outer.evaluate("el => el.open")
        key_toggle.click()

        row_states = outer.evaluate(ROW_DETAILS_JS)
        assert not any(row_states), "reopened fold must show rows collapsed"
        expect(button).to_contain_text("expand all rows")

    @pytest.mark.browser
    def test_toggle_all_keeps_button_in_sync(self, page: Page) -> None:
        """The global toggle-all button opens row folds without going
        through the rows-toggle; its label must still flip (the state is
        derived from the actual row state, not from past clicks)."""
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")

        outer = page.locator("details.tool-param-collapsible-rows").first
        button = outer.locator(
            ".tool-param-fold-controls .tool-param-rows-toggle"
        ).first

        page.locator("#toggleDetails").click()
        assert outer.evaluate("el => el.open")
        assert all(outer.evaluate(ROW_DETAILS_JS))
        expect(button).to_contain_text("collapse all rows")

        # And the button still works from this externally-reached state.
        button.click()
        assert not any(outer.evaluate(ROW_DETAILS_JS))
        assert "expand all rows" in (button.text_content() or "")

    @pytest.mark.browser
    def test_expand_all_control_cascades(self, page: Page) -> None:
        """The top-level expand-all opens every fold in the renderer and
        flips the nested rows-toggle buttons; selectively closing one
        fold flips the top button back to 'expand all'."""
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")

        root = page.locator(".tool-params-root").first
        expand_all = root.locator(".tool-params-expand-all").first
        all_open_js = (
            "el => Array.from(el.querySelectorAll('details')).every(d => d.open)"
        )
        all_closed_js = (
            "el => Array.from(el.querySelectorAll('details')).every(d => !d.open)"
        )

        assert root.evaluate(all_closed_js), "everything starts collapsed"
        assert "expand all" in (expand_all.text_content() or "")

        expand_all.click()
        assert root.evaluate(all_open_js), "expand all must open every fold"
        assert "collapse all" in (expand_all.text_content() or "")
        # The toggle event is dispatched as a queued task, so label
        # updates from the toggle listener are asynchronous — poll.
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll("
            "'.tool-params-root .tool-param-rows-toggle'))"
            ".every(b => b.textContent.includes('collapse all'))"
        )

        # Selectively close one row: mixed state → top offers expand again.
        # (Open keyed summaries are hidden — close via the key-column toggle.)
        outer = root.locator("details.tool-param-collapsible-rows").first
        row_toggle = outer.locator(
            ":scope > table > tbody > tr.tool-param-row-fold"
            " > td.tool-param-key > .tool-param-key-toggle"
        ).first
        row_toggle.click()
        expect(expand_all).to_contain_text("expand all")

        # Re-expand, then collapse all back to the initial state.
        expand_all.click()
        assert root.evaluate(all_open_js)
        expand_all.click()
        assert root.evaluate(all_closed_js)
        assert "expand all" in (expand_all.text_content() or "")

    @pytest.mark.browser
    def test_global_toggle_all_activates_expand_all(self, page: Page) -> None:
        """The global 'Open all details' button counts as expand-all for
        every params renderer: its top button must read 'collapse all'."""
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")

        root = page.locator(".tool-params-root").first
        expand_all = root.locator(".tool-params-expand-all").first

        page.locator("#toggleDetails").click()
        assert root.evaluate(
            "el => Array.from(el.querySelectorAll('details')).every(d => d.open)"
        )
        expect(expand_all).to_contain_text("collapse all")

    @pytest.mark.browser
    def test_key_column_toggle_cycle_and_glyph_sync(self, page: Page) -> None:
        """The whole-key button drives the row's fold; its state
        (aria-expanded → native disclosure marker) is derived from the actual
        open state, so the expand-all path flips it too, not just direct
        clicks."""
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")

        outer = page.locator("details.tool-param-collapsible-rows").first
        key_toggle = page.locator(
            "tr.tool-param-row-fold > td.tool-param-key > .tool-param-key-toggle"
        ).first

        # The glyph is the native disclosure marker (a ::marker pseudo-
        # element, not text); open/closed state is carried by aria-expanded.
        expect(key_toggle).to_have_attribute("aria-expanded", "false")
        key_toggle.click()
        assert outer.evaluate("el => el.open")
        # State updates arrive via the queued toggle event — poll.
        expect(key_toggle).to_have_attribute("aria-expanded", "true")
        key_toggle.click()
        assert not outer.evaluate("el => el.open")
        expect(key_toggle).to_have_attribute("aria-expanded", "false")

        # Externally-driven open (expand-all) must flip the state too.
        page.locator(".tool-params-expand-all").first.click()
        expect(key_toggle).to_have_attribute("aria-expanded", "true")

    @pytest.mark.browser
    def test_no_interactive_elements_inside_summaries(self, page: Page) -> None:
        """Accessibility contract (Chrome DevTools issue): no button/link/
        input may live inside any <summary> on the rendered page."""
        html = self._render(_entries_with_structured_list())
        page.goto(f"file://{html}")
        offenders = page.evaluate(
            "() => document.querySelectorAll("
            "'summary button, summary a, summary input, summary select'"
            ").length"
        )
        assert offenders == 0
